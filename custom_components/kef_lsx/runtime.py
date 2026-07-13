"""Single-owner command scheduler for one fragile LSX control endpoint."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal

from homeassistant.const import CONF_HOST, CONF_PORT

from .client import LsxClient
from .const import (
    CONF_INVERSE_ORIENTATION,
    CONF_MAX_VOLUME,
    CONF_PREFERRED_WAKE_SOURCE,
    CONF_STANDBY_TIME,
    CONF_VOLUME_STEP,
    DEFAULT_INVERSE_ORIENTATION,
    DEFAULT_MAX_VOLUME,
    DEFAULT_PORT,
    DEFAULT_PREFERRED_WAKE_SOURCE,
    DEFAULT_STANDBY_TIME,
    DEFAULT_VOLUME_STEP,
)
from .errors import ClientClosedError, CommandRejectedError, LsxError, QueueBusyError
from .models import CommandResult, CommunicationHealth, RuntimeSnapshot, SpeakerState
from .protocol import Source, SourceStatus, VolumeStatus, coerce_source

type ControlKind = Literal[
    "turn_on",
    "turn_off",
    "select_source",
    "set_volume",
    "volume_up",
    "volume_down",
    "mute",
    "unmute",
]


@dataclass(frozen=True, slots=True)
class RuntimeTiming:
    """Explicit scheduling and health budgets, injectable for tests."""

    control_deadline: float = 8.0
    retry_delay: float = 0.1
    verification_delays: tuple[float, ...] = (1.0, 2.0, 4.0)
    stale_after: float = 45.0
    unavailable_after: float = 120.0
    failure_threshold: int = 4
    shutdown_timeout: float = 2.0
    volume_poll_interval: float = 300.0


@dataclass(slots=True)
class _Control:
    kind: ControlKind
    value: Any
    deadline: float
    waiters: list[asyncio.Future[RuntimeSnapshot]]
    result: CommandResult


class KefLsxRuntime:
    """Own the only client and the only task allowed to call it."""

    QUEUE_CAPACITY = 16

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        *,
        preferred_wake_source: Source | str = DEFAULT_PREFERRED_WAKE_SOURCE,
        maximum_volume: float = DEFAULT_MAX_VOLUME,
        volume_step: float = DEFAULT_VOLUME_STEP,
        inverse_orientation: bool = DEFAULT_INVERSE_ORIENTATION,
        standby_time: int | None = DEFAULT_STANDBY_TIME,
        client: LsxClient | None = None,
        timing: RuntimeTiming | None = None,
        monotonic: Callable[[], float] | None = None,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.preferred_wake_source = coerce_source(preferred_wake_source)
        self.maximum_volume = max(1, min(100, round(maximum_volume * 100)))
        self.volume_step = max(1, round(volume_step * 100))
        self.inverse_orientation = inverse_orientation
        self.standby_time = standby_time
        self._client = client or LsxClient(host, port)
        self._timing = timing or RuntimeTiming()
        self._monotonic = monotonic
        self._wall_clock = wall_clock or (lambda: datetime.now(UTC))
        self._started_mono = 0.0
        self._last_channel_mono: float | None = None
        self._last_state_mono: float | None = None
        self._controls: deque[_Control] = deque()
        self._active_control: _Control | None = None
        self._poll_pending = False
        self._poll_waiters: list[asyncio.Future[RuntimeSnapshot]] = []
        self._verification_attempt = -1
        self._verification_due: float | None = None
        self._verification_kind: ControlKind | None = None
        self._verification_expected: SpeakerState | None = None
        self._maintenance_pending = False
        self._last_volume_mono: float | None = None
        self._wake = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._closing = False
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._publisher: Callable[[RuntimeSnapshot], None] | None = None
        self.coordinator: Any = None
        self.snapshot = RuntimeSnapshot()

    def _now(self) -> float:
        return (
            self._monotonic() if self._monotonic else asyncio.get_running_loop().time()
        )

    @property
    def queue_size(self) -> int:
        return len(self._controls)

    def set_publisher(self, publisher: Callable[[RuntimeSnapshot], None]) -> None:
        """Install the sole state publication callback."""
        self._publisher = publisher

    async def async_start(self) -> None:
        """Start ownership and perform a soft first poll."""
        if self._worker is not None:
            return
        self._started_mono = self._now()
        self._worker = asyncio.create_task(self._async_worker(), name="kef_lsx_worker")
        await self.async_poll()

    async def async_close(self) -> None:
        """Share one bounded graceful-then-forced shutdown operation."""
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._async_close_impl())
        await asyncio.shield(self._close_task)

    async def _async_close_impl(self) -> None:
        """Stop submissions, let the owner exit, then force it if needed."""
        self._closing = True
        for job in self._controls:
            for waiter in job.waiters:
                if not waiter.done():
                    waiter.cancel("KEF LSX runtime is unloading")
        for waiter in self._poll_waiters:
            if not waiter.done():
                waiter.cancel("KEF LSX runtime is unloading")
        self._controls.clear()
        self._poll_waiters.clear()
        self._poll_pending = False
        self._maintenance_pending = False
        self._clear_verification()
        self._wake.set()
        worker = self._worker
        if worker is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(worker), self._timing.shutdown_timeout
                )
            except TimeoutError:
                worker.cancel()
                try:
                    await worker
                except asyncio.CancelledError:
                    pass
        self._worker = None
        if self._active_control is not None:
            for waiter in self._active_control.waiters:
                if not waiter.done():
                    waiter.cancel("KEF LSX runtime is unloading")
        await self._client.async_close()
        self._closed = True

    async def async_poll(self) -> RuntimeSnapshot:
        """Coalesce a state poll; cancellation of one waiter cannot cancel others."""
        self._ensure_open()
        future = asyncio.get_running_loop().create_future()
        self._poll_waiters.append(future)
        self._poll_pending = True
        self._wake.set()
        return await asyncio.shield(future)

    async def async_turn_on(self) -> RuntimeSnapshot:
        return await self._submit("turn_on")

    async def async_turn_off(self) -> RuntimeSnapshot:
        return await self._submit("turn_off")

    async def async_select_source(self, source: Source | str) -> RuntimeSnapshot:
        return await self._submit("select_source", coerce_source(source))

    async def async_set_volume(self, volume: float) -> RuntimeSnapshot:
        target = min(self.maximum_volume, max(0, round(volume * 100)))
        return await self._submit("set_volume", target)

    async def async_volume_up(self) -> RuntimeSnapshot:
        return await self._submit("volume_up")

    async def async_volume_down(self) -> RuntimeSnapshot:
        return await self._submit("volume_down")

    async def async_mute(self, muted: bool) -> RuntimeSnapshot:
        return await self._submit("mute" if muted else "unmute")

    async def _submit(self, kind: ControlKind, value: Any = None) -> RuntimeSnapshot:
        self._ensure_open()
        future = asyncio.get_running_loop().create_future()
        # Coalesce only adjacent absolute-volume requests; all callers share the result.
        if kind == "set_volume" and self._controls and self._controls[-1].kind == kind:
            job = self._controls[-1]
            job.value = value
            job.waiters.append(future)
        else:
            if len(self._controls) >= self.QUEUE_CAPACITY:
                raise QueueBusyError("KEF LSX control queue is full")
            # Supersede older verification only after this command is accepted.
            self._clear_verification()
            submitted = self._wall_clock()
            result = CommandResult(kind, submitted)
            job = _Control(
                kind,
                value,
                self._now() + self._timing.control_deadline,
                [future],
                result,
            )
            self._controls.append(job)
            self.snapshot = replace(self.snapshot, last_command=result)
            self._publish()
        self._wake.set()
        return await future

    def _ensure_open(self) -> None:
        if self._closing:
            raise ClientClosedError("KEF LSX runtime is closed")

    async def _async_worker(self) -> None:
        while True:
            if self._closing:
                return
            now = self._now()
            if self._controls:
                self._active_control = self._controls.popleft()
                try:
                    await self._async_control(self._active_control)
                finally:
                    self._active_control = None
                continue
            if self._verification_due is not None and now >= self._verification_due:
                await self._async_state_exchange("verification")
                continue
            if self._poll_pending:
                await self._async_state_exchange("poll")
                continue
            if self._maintenance_pending:
                self._maintenance_pending = False
                await self._async_volume_maintenance()
                continue

            delay = None
            if self._verification_due is not None:
                delay = max(0.0, self._verification_due - now)
            self._wake.clear()
            if self._controls or self._poll_pending or self._maintenance_pending:
                continue
            try:
                if delay is None:
                    await self._wake.wait()
                else:
                    async with asyncio.timeout(delay):
                        await self._wake.wait()
            except TimeoutError:
                pass

    async def _async_state_exchange(
        self, reason: Literal["poll", "verification"]
    ) -> None:
        try:
            state = await self._client.async_get_source()
        except LsxError as err:
            if reason == "poll" or self._poll_pending:
                self._record_primary_failure(err)
                self._poll_pending = False
                self._resolve_polls()
            if reason == "verification":
                self._record_optional_failure(err)
                self._schedule_next_verification()
            return

        self._apply_source(state)
        self._record_success(state_success=True)
        # A source GET satisfies both a pending poll and command verification.
        if (
            self._verification_due is not None
            and self._now() >= self._verification_due
            and (
                reason == "verification"
                or self._verification_kind in {"turn_on", "turn_off", "select_source"}
            )
            and self.snapshot.last_command is not None
            and self.snapshot.last_command.verified is None
        ):
            self._complete_verification()
        if self._poll_pending:
            self._poll_pending = False
            self._resolve_polls()
        now = self._now()
        if state.power_on and (
            (self._last_volume_mono is None and self.snapshot.speaker.volume is None)
            or (
                self._last_volume_mono is not None
                and now - self._last_volume_mono >= self._timing.volume_poll_interval
            )
        ):
            self._maintenance_pending = True
            self._wake.set()
        elif not state.power_on:
            self._maintenance_pending = False

    async def _async_volume_maintenance(self) -> None:
        if self.snapshot.speaker.power_on is not True:
            return
        try:
            status = await self._client.async_get_volume()
        except LsxError as err:
            self._record_optional_failure(err)
            return
        self._apply_volume(status)
        self._last_volume_mono = self._now()
        self._record_success(state_success=False)

    async def _async_control(self, job: _Control) -> None:
        if all(waiter.cancelled() for waiter in job.waiters):
            return
        if self._now() >= job.deadline:
            self._finish_control_error(
                job, TimeoutError("Control deadline expired"), False
            )
            return

        # Relative volume/mute commands first resolve unknown volume as one exchange.
        if job.kind in {"volume_up", "volume_down", "mute", "unmute"} and (
            self.snapshot.speaker.volume is None or self.snapshot.speaker.muted is None
        ):
            try:
                status = await self._client.async_get_volume(
                    deadline=self._client_deadline(job)
                )
            except LsxError as err:
                self._record_primary_failure(err)
                self._finish_control_error(job, err, err.write_attempted)
                return
            self._apply_volume(status)
            self._last_volume_mono = self._now()
            self._record_success(state_success=False)
            self._controls.appendleft(job)
            return

        attempted = False
        error: BaseException | None = None
        for attempt in range(2):
            if self._closing or self._now() >= job.deadline:
                break
            try:
                await self._execute_control(job)
            except LsxError as err:
                attempted = attempted or err.write_attempted
                error = err
                if isinstance(err, CommandRejectedError):
                    self._record_success(state_success=False)
                    break
                if attempt == 0:
                    await asyncio.sleep(
                        min(
                            self._timing.retry_delay, max(0, job.deadline - self._now())
                        )
                    )
                    continue
                break
            else:
                attempted = True
                error = None
                break
        if error is not None:
            if not isinstance(error, CommandRejectedError):
                self._record_primary_failure(error)
            self._finish_control_error(job, error, attempted)
            if (
                attempted
                and not isinstance(error, CommandRejectedError)
                and job.kind
                in {
                    "turn_on",
                    "turn_off",
                    "select_source",
                }
            ):
                self._start_verification(job, self._expected_state(job))
            return
        if not attempted:
            error = TimeoutError("Control deadline expired")
            self._record_primary_failure(error)
            self._finish_control_error(job, error, False)
            return

        self._apply_optimistic(job)
        if job.kind in {"set_volume", "volume_up", "volume_down", "mute", "unmute"}:
            self._last_volume_mono = self._now()
        self._record_success(state_success=False)
        result = replace(
            job.result,
            completed_at=self._wall_clock(),
            write_attempted=True,
            acknowledged=True,
            verified=None,
        )
        self.snapshot = replace(self.snapshot, last_command=result)
        if job.kind in {"turn_on", "turn_off", "select_source"}:
            self._start_verification(job, self.snapshot.speaker)
        self._publish()
        for waiter in job.waiters:
            if not waiter.done():
                waiter.set_result(self.snapshot)

    async def _execute_control(self, job: _Control) -> None:
        speaker = self.snapshot.speaker
        if job.kind == "turn_on":
            await self._client.async_set_source(
                self.preferred_wake_source,
                standby=self.standby_time,
                inverse=self.inverse_orientation,
                power_on=True,
                deadline=self._client_deadline(job),
            )
        elif job.kind == "turn_off":
            await self._client.async_set_source(
                speaker.source or self.preferred_wake_source,
                standby=self.standby_time,
                inverse=self.inverse_orientation,
                power_on=False,
                deadline=self._client_deadline(job),
            )
        elif job.kind == "select_source":
            await self._client.async_set_source(
                job.value,
                standby=self.standby_time,
                inverse=self.inverse_orientation,
                power_on=True,
                deadline=self._client_deadline(job),
            )
        else:
            volume = speaker.volume or 0
            muted = bool(speaker.muted)
            if job.kind == "set_volume":
                volume = job.value
            elif job.kind == "volume_up":
                volume = min(self.maximum_volume, volume + self.volume_step)
            elif job.kind == "volume_down":
                volume = max(0, volume - self.volume_step)
            elif job.kind == "mute":
                muted = True
            elif job.kind == "unmute":
                muted = False
            await self._client.async_set_volume(
                volume, muted=muted, deadline=self._client_deadline(job)
            )

    def _client_deadline(self, job: _Control) -> float:
        """Translate the injectable scheduler clock into the event-loop clock."""
        remaining = max(0.0, job.deadline - self._now())
        return asyncio.get_running_loop().time() + remaining

    def _apply_optimistic(self, job: _Control) -> None:
        self.snapshot = replace(self.snapshot, speaker=self._expected_state(job))
        if job.kind == "turn_off":
            self._maintenance_pending = False

    def _expected_state(self, job: _Control) -> SpeakerState:
        """Return the state expected if a control write took effect."""
        state = self.snapshot.speaker
        if job.kind == "turn_on":
            state = replace(state, power_on=True, source=self.preferred_wake_source)
        elif job.kind == "turn_off":
            state = replace(state, power_on=False)
        elif job.kind == "select_source":
            state = replace(state, power_on=True, source=job.value)
        else:
            volume = state.volume or 0
            muted = bool(state.muted)
            if job.kind == "set_volume":
                volume = job.value
            elif job.kind == "volume_up":
                volume = min(self.maximum_volume, volume + self.volume_step)
            elif job.kind == "volume_down":
                volume = max(0, volume - self.volume_step)
            elif job.kind == "mute":
                muted = True
            elif job.kind == "unmute":
                muted = False
            state = replace(state, volume=volume, muted=muted)
        return state

    def _start_verification(self, job: _Control, expected: SpeakerState) -> None:
        """Schedule bounded verification for one source-changing command."""
        self._verification_attempt = 0
        self._verification_kind = job.kind
        self._verification_expected = expected
        self._verification_due = self._now() + self._timing.verification_delays[0]

    def _clear_verification(self) -> None:
        """Cancel the current verification plan without changing command history."""
        self._verification_attempt = -1
        self._verification_due = None
        self._verification_kind = None
        self._verification_expected = None

    def _apply_source(self, status: SourceStatus) -> None:
        self.snapshot = replace(
            self.snapshot,
            speaker=replace(
                self.snapshot.speaker, power_on=status.power_on, source=status.source
            ),
        )

    def _apply_volume(self, status: VolumeStatus) -> None:
        self.snapshot = replace(
            self.snapshot,
            speaker=replace(
                self.snapshot.speaker, volume=status.volume, muted=status.muted
            ),
        )

    def _record_success(self, *, state_success: bool) -> None:
        now, wall = self._now(), self._wall_clock()
        self._last_channel_mono = now
        if state_success:
            self._last_state_mono = now
        old = self.snapshot.health
        health = replace(
            old,
            available=True,
            consecutive_failures=0,
            last_channel_success=wall,
            last_state_success=wall if state_success else old.last_state_success,
            last_error=None,
        )
        self.snapshot = replace(self.snapshot, health=self._compute_health(health))
        self._publish()

    def _record_primary_failure(self, error: BaseException) -> None:
        old = self.snapshot.health
        health = replace(
            old,
            consecutive_failures=old.consecutive_failures + 1,
            last_error=type(error).__name__,
        )
        self.snapshot = replace(
            self.snapshot, health=self._compute_health(health, failed=True)
        )
        self._publish()

    def _record_optional_failure(self, error: BaseException) -> None:
        health = replace(
            self.snapshot.health, degraded=True, last_error=type(error).__name__
        )
        self.snapshot = replace(
            self.snapshot, health=self._compute_health(health, failed=True)
        )
        self._publish()

    def _compute_health(
        self, health: CommunicationHealth, *, failed: bool = False
    ) -> CommunicationHealth:
        now = self._now()
        state_age = now - (
            self._last_state_mono
            if self._last_state_mono is not None
            else self._started_mono
        )
        channel_age = now - (
            self._last_channel_mono
            if self._last_channel_mono is not None
            else self._started_mono
        )
        stale = state_age >= self._timing.stale_after
        unavailable = (
            health.consecutive_failures >= self._timing.failure_threshold
            and channel_age >= self._timing.unavailable_after
        )
        return replace(
            health,
            available=not unavailable,
            stale=stale,
            degraded=failed or stale or health.consecutive_failures > 0,
        )

    def _schedule_next_verification(self) -> None:
        self._verification_attempt += 1
        if self._verification_attempt >= len(self._timing.verification_delays):
            self._verification_due = None
            if (
                self.snapshot.last_command
                and self.snapshot.last_command.verified is None
            ):
                self.snapshot = replace(
                    self.snapshot,
                    last_command=replace(self.snapshot.last_command, verified=False),
                )
                self._publish()
            self._verification_kind = None
            self._verification_expected = None
            return
        self._verification_due = (
            self._now() + self._timing.verification_delays[self._verification_attempt]
        )

    def _complete_verification(self) -> None:
        """Compare the confirming read with the optimistic command state."""
        kind = self._verification_kind
        expected = self._verification_expected
        if kind is None or expected is None:
            matches = False
        elif kind in {"turn_on", "turn_off", "select_source"}:
            matches = (
                self.snapshot.speaker.power_on == expected.power_on
                and self.snapshot.speaker.source == expected.source
            )
        else:
            matches = (
                self.snapshot.speaker.volume == expected.volume
                and self.snapshot.speaker.muted == expected.muted
            )
        if matches and self.snapshot.last_command is not None:
            self.snapshot = replace(
                self.snapshot,
                last_command=replace(self.snapshot.last_command, verified=True),
            )
        if not matches:
            self.snapshot = replace(
                self.snapshot,
                health=replace(
                    self.snapshot.health,
                    degraded=True,
                    last_error="VerificationMismatch",
                ),
            )
            self._publish()
            self._schedule_next_verification()
            return
        self._verification_due = None
        self._verification_attempt = -1
        self._verification_kind = None
        self._verification_expected = None
        self._publish()

    def _finish_control_error(
        self, job: _Control, error: BaseException, attempted: bool
    ) -> None:
        result = replace(
            job.result,
            completed_at=self._wall_clock(),
            write_attempted=attempted,
            acknowledged=False,
            error=type(error).__name__,
        )
        self.snapshot = replace(self.snapshot, last_command=result)
        self._publish()
        for waiter in job.waiters:
            if not waiter.done():
                waiter.set_exception(error)

    def _resolve_polls(self) -> None:
        waiters, self._poll_waiters = self._poll_waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(self.snapshot)

    def _publish(self) -> None:
        if self._publisher is not None:
            self._publisher(self.snapshot)


async def async_create_runtime(hass: Any, entry: Any) -> KefLsxRuntime:
    """Build and fully start the runtime expected by config-entry setup."""
    options = entry.options
    runtime = KefLsxRuntime(
        entry.data[CONF_HOST],
        entry.data.get(CONF_PORT, DEFAULT_PORT),
        preferred_wake_source=options.get(
            CONF_PREFERRED_WAKE_SOURCE, DEFAULT_PREFERRED_WAKE_SOURCE
        ),
        maximum_volume=options.get(CONF_MAX_VOLUME, DEFAULT_MAX_VOLUME),
        volume_step=options.get(CONF_VOLUME_STEP, DEFAULT_VOLUME_STEP),
        inverse_orientation=options.get(
            CONF_INVERSE_ORIENTATION, DEFAULT_INVERSE_ORIENTATION
        ),
        standby_time=options.get(CONF_STANDBY_TIME, DEFAULT_STANDBY_TIME),
    )
    from .coordinator import KefLsxCoordinator

    runtime.coordinator = KefLsxCoordinator(hass, runtime)
    runtime.set_publisher(runtime.coordinator.async_set_updated_data)
    await runtime.async_start()
    runtime.coordinator.async_set_updated_data(runtime.snapshot)
    return runtime
