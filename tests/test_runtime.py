"""Vertical runtime tests over the deterministic TCP fake."""

from __future__ import annotations

import asyncio

import pytest
from fake_lsx import AutoReply, DropReply, GateThen, Reject, Reply, Step, get_reply

from custom_components.kef_lsx.client import LsxClient
from custom_components.kef_lsx.errors import (
    CommandRejectedError,
    QueueBusyError,
    ResponseTimeoutError,
)
from custom_components.kef_lsx.models import RuntimeSnapshot, SpeakerState
from custom_components.kef_lsx.protocol import Source, SourceStatus
from custom_components.kef_lsx.runtime import KefLsxRuntime, RuntimeTiming

pytestmark = pytest.mark.usefixtures("socket_enabled")

GET_SOURCE = bytes((0x47, 0x30, 0x80))
WAKE_OPT = bytes((0x53, 0x30, 0x81, 0x2B))
GET_VOLUME = bytes((0x47, 0x25, 0x80))
VOLUME_25 = bytes((0x53, 0x25, 0x81, 25))
OFF_OPT = bytes((0x53, 0x30, 0x81, 0xAB))


def _runtime(server, *, clock=None, verification_delays=(100,)) -> KefLsxRuntime:
    client = LsxClient(
        server.host,
        server.port,
        connect_timeout=0.05,
        response_timeout=0.05,
        close_timeout=0.05,
    )
    runtime = KefLsxRuntime(
        server.host,
        server.port,
        client=client,
        timing=RuntimeTiming(
            retry_delay=0,
            verification_delays=verification_delays,
            stale_after=45,
            unavailable_after=120,
            failure_threshold=4,
        ),
        monotonic=clock,
    )
    runtime.snapshot = RuntimeSnapshot(speaker=SpeakerState(volume=20, muted=False))
    return runtime


async def test_direct_wake_is_attempted_after_failed_poll(fake_lsx_server) -> None:
    """A failed background read cannot gate the direct optical wake SET."""
    server = fake_lsx_server
    server.queue(
        Step(GET_SOURCE, AutoReply(), "initial poll"),
        Step(GET_SOURCE, DropReply(), "dropped poll"),
        Step(WAKE_OPT, AutoReply(), "direct wake"),
    )
    runtime = _runtime(server)
    await runtime.async_start()
    failed = await runtime.async_poll()
    assert failed.health.available
    assert failed.health.consecutive_failures == 1

    result = await runtime.async_turn_on()

    assert server.commands[-1].raw == WAKE_OPT
    assert result.last_command is not None
    assert result.last_command.write_attempted
    assert result.last_command.acknowledged
    await runtime.async_close()
    server.assert_clean()


async def test_newer_failed_source_command_supersedes_old_verification(
    fake_lsx_server,
) -> None:
    """A wake verification cannot mark a later failed power-off as verified."""
    server = fake_lsx_server
    server.queue(
        Step(GET_SOURCE),
        Step(WAKE_OPT),
        Step(OFF_OPT, DropReply()),
        Step(OFF_OPT, DropReply()),
        Step(GET_SOURCE, Reply(get_reply(0x30, 0x2B))),
    )
    runtime = _runtime(server, verification_delays=(0.01,))
    await runtime.async_start()
    await runtime.async_turn_on()

    with pytest.raises(ResponseTimeoutError):
        await runtime.async_turn_off()
    await server.wait_for_commands(5)
    await asyncio.sleep(0.02)

    result = runtime.snapshot.last_command
    assert result is not None
    assert result.command == "turn_off"
    assert result.write_attempted
    assert not result.acknowledged
    assert result.verified is False
    await runtime.async_close()
    server.assert_clean()


async def test_off_state_does_not_schedule_volume_maintenance(fake_lsx_server) -> None:
    """An off-state source poll is the only routine exchange needed while off."""
    server = fake_lsx_server
    server.queue(Step(GET_SOURCE, Reply(get_reply(0x30, 0xAB))))
    runtime = _runtime(server)

    await runtime.async_start()
    await asyncio.sleep(0)

    assert runtime.snapshot.speaker.power_on is False
    assert [command.raw for command in server.commands] == [GET_SOURCE]
    await runtime.async_close()
    server.assert_clean()


async def test_queue_rejection_does_not_cancel_accepted_command_verification(
    fake_lsx_server,
) -> None:
    """An unaccepted command cannot supersede an accepted command's verification."""
    runtime = _runtime(fake_lsx_server)
    runtime.QUEUE_CAPACITY = 0
    runtime._verification_due = 123.0
    runtime._verification_kind = "turn_on"
    runtime._verification_expected = SpeakerState(power_on=True)

    with pytest.raises(QueueBusyError):
        await runtime.async_turn_on()

    assert runtime._verification_due == 123.0
    assert runtime._verification_kind == "turn_on"
    await runtime.async_close()


async def test_availability_hysteresis_and_immediate_recovery(fake_lsx_server) -> None:
    """Failures need both count and elapsed time; one success recovers immediately."""
    server = fake_lsx_server
    server.queue(
        Step(GET_SOURCE),
        *(Step(GET_SOURCE, DropReply()) for _ in range(4)),
        Step(GET_SOURCE),
    )
    now = 0.0
    runtime = _runtime(server, clock=lambda: now)
    await runtime.async_start()

    for index in range(4):
        now = (index + 1) * 30.0
        snapshot = await runtime.async_poll()
    assert not snapshot.health.available
    assert snapshot.speaker.source is not None

    now = 121.0
    recovered = await runtime.async_poll()
    assert recovered.health.available
    assert recovered.health.consecutive_failures == 0
    await runtime.async_close()
    server.assert_clean()


async def test_three_failed_polls_retain_state_and_remain_controllable(
    fake_lsx_server,
) -> None:
    """Several transient failures preserve cached state and do not gate controls."""
    server = fake_lsx_server
    server.queue(
        Step(GET_SOURCE),
        *(Step(GET_SOURCE, DropReply()) for _ in range(3)),
        Step(WAKE_OPT),
    )
    now = 0.0
    runtime = _runtime(server, clock=lambda: now)
    await runtime.async_start()
    for index in range(3):
        now = (index + 1) * 30.0
        snapshot = await runtime.async_poll()

    assert snapshot.health.available
    assert snapshot.health.stale
    assert snapshot.speaker.source is not None
    controlled = await runtime.async_turn_on()
    assert controlled.last_command is not None
    assert controlled.last_command.acknowledged
    assert server.commands[-1].raw == WAKE_OPT
    await runtime.async_close()
    server.assert_clean()


async def test_failed_initial_poll_is_soft_and_wake_still_runs(fake_lsx_server) -> None:
    """Config-entry startup survives one read failure and remains operable."""
    server = fake_lsx_server
    server.queue(
        Step(GET_SOURCE, DropReply()),
        Step(WAKE_OPT),
    )
    runtime = _runtime(server)

    await runtime.async_start()
    assert runtime.snapshot.health.available
    assert runtime.snapshot.health.consecutive_failures == 1
    await runtime.async_turn_on()
    assert [command.raw for command in server.commands] == [GET_SOURCE, WAKE_OPT]
    await runtime.async_close()
    server.assert_clean()


async def test_rejection_is_not_retried_or_counted_as_channel_failure(
    fake_lsx_server,
) -> None:
    server = fake_lsx_server
    server.queue(Step(GET_SOURCE), Step(WAKE_OPT, Reject()))
    runtime = _runtime(server)
    await runtime.async_start()
    with pytest.raises(CommandRejectedError):
        await runtime.async_turn_on()
    assert [command.raw for command in server.commands].count(WAKE_OPT) == 1
    assert runtime.snapshot.health.consecutive_failures == 0
    assert runtime.snapshot.last_command is not None
    assert runtime.snapshot.last_command.write_attempted
    await runtime.async_close()
    server.assert_clean()


async def test_turn_on_uses_preferred_source_despite_cached_source(
    fake_lsx_server,
) -> None:
    server = fake_lsx_server
    server.state.source = "Aux"
    server.queue(Step(GET_SOURCE), Step(WAKE_OPT))
    runtime = _runtime(server)
    await runtime.async_start()
    assert runtime.snapshot.speaker.source is not None
    await runtime.async_turn_on()
    assert server.commands[-1].raw == WAKE_OPT
    await runtime.async_close()
    server.assert_clean()


async def test_source_verification_requires_match_and_exhausts_bounded_retries(
    fake_lsx_server,
) -> None:
    server = fake_lsx_server
    mismatch = Reply(get_reply(0x30, 0xAA))  # Off, Aux, standby disabled.
    server.queue(
        Step(GET_SOURCE),
        Step(WAKE_OPT),
        Step(GET_SOURCE, mismatch),
        Step(GET_SOURCE, mismatch),
    )
    runtime = _runtime(server, verification_delays=(0.01, 0.01))
    await runtime.async_start()
    await runtime.async_turn_on()
    await asyncio.sleep(0.25)
    assert runtime.snapshot.last_command is not None
    assert runtime.snapshot.last_command.verified is False
    assert runtime.snapshot.health.degraded
    await runtime.async_close()
    server.assert_clean()


async def test_volume_ack_is_not_falsely_verified_by_source_get(
    fake_lsx_server,
) -> None:
    server = fake_lsx_server
    server.queue(Step(GET_SOURCE), Step(VOLUME_25))
    runtime = _runtime(server, verification_delays=(0.01,))
    await runtime.async_start()
    result = await runtime.async_set_volume(0.25)
    await asyncio.sleep(0.03)
    assert result.last_command is not None
    assert result.last_command.verified is None
    assert [command.raw for command in server.commands] == [GET_SOURCE, VOLUME_25]
    await runtime.async_close()
    server.assert_clean()


async def test_control_retry_is_exactly_two_bounded_attempts(fake_lsx_server) -> None:
    """An idempotent control gets one clean retry and cannot retry indefinitely."""
    server = fake_lsx_server
    server.queue(
        Step(GET_SOURCE),
        Step(WAKE_OPT, DropReply()),
        Step(WAKE_OPT, DropReply()),
    )
    runtime = _runtime(server)
    await runtime.async_start()
    started = asyncio.get_running_loop().time()
    with pytest.raises(ResponseTimeoutError):
        await runtime.async_turn_on()
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.3
    assert [command.raw for command in server.commands].count(WAKE_OPT) == 2
    assert runtime.snapshot.last_command is not None
    assert runtime.snapshot.last_command.write_attempted
    assert not runtime.snapshot.last_command.acknowledged
    await runtime.async_close()
    server.assert_clean()


async def test_unknown_volume_uses_get_then_front_of_queue_continuation(
    fake_lsx_server,
) -> None:
    """Relative volume never guesses: one GET is followed by the absolute SET."""
    server = fake_lsx_server
    server.queue(
        Step(GET_SOURCE),
        Step(GET_VOLUME),
        Step(VOLUME_25),
    )
    runtime = _runtime(server)
    runtime.snapshot = RuntimeSnapshot()
    await runtime.async_start()

    result = await runtime.async_volume_up()

    assert [command.raw for command in server.commands] == [
        GET_SOURCE,
        GET_VOLUME,
        VOLUME_25,
    ]
    assert result.speaker.volume == 25
    await runtime.async_close()
    server.assert_clean()


async def test_poll_waiters_coalesce_and_cancellation_isolated(fake_lsx_server) -> None:
    """A cancelled waiter cannot cancel the shared in-flight poll."""
    server = fake_lsx_server
    runtime = _runtime(server)
    await runtime.async_start()
    first = asyncio.create_task(runtime.async_poll())
    second = asyncio.create_task(runtime.async_poll())
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert (await second).health.available
    assert [command.raw for command in server.commands].count(GET_SOURCE) == 2
    await runtime.async_close()
    server.assert_clean()


async def test_no_concurrent_exchanges(fake_lsx_server) -> None:
    """Polls and controls always pass through the same serialized owner."""
    server = fake_lsx_server
    runtime = _runtime(server)
    await runtime.async_start()
    await asyncio.gather(runtime.async_poll(), runtime.async_set_volume(0.25))
    assert server.max_in_flight_exchanges == 1
    assert not server.concurrent_exchange_violations
    await runtime.async_close()
    server.assert_clean()


async def test_control_has_priority_over_new_poll_work(fake_lsx_server) -> None:
    """Once one exchange completes, accepted controls run before routine polls."""
    server = fake_lsx_server
    gate = asyncio.Event()
    server.queue(
        Step(GET_SOURCE),
        Step(GET_SOURCE, GateThen(gate, Reply(get_reply(0x30, 0x2B)))),
        Step(WAKE_OPT),
    )
    runtime = _runtime(server)
    await runtime.async_start()
    poll = asyncio.create_task(runtime.async_poll())
    await server.wait_for_commands(2)
    command = asyncio.create_task(runtime.async_turn_on())
    coalesced_poll = asyncio.create_task(runtime.async_poll())
    gate.set()
    await asyncio.gather(poll, coalesced_poll, command)
    assert server.commands[-1].raw == WAKE_OPT
    await runtime.async_close()
    server.assert_clean()


async def test_unload_cancels_waiters_and_closes_inflight_connection(
    fake_lsx_server,
) -> None:
    """Shutdown remains bounded even while a response is indefinitely delayed."""
    server = fake_lsx_server
    gate = asyncio.Event()
    server.queue(
        Step(GET_SOURCE),
        Step(GET_SOURCE, GateThen(gate, AutoReply())),
    )
    runtime = _runtime(server)
    await runtime.async_start()
    poll = asyncio.create_task(runtime.async_poll())
    await server.wait_for_commands(2)

    async with asyncio.timeout(0.5):
        await runtime.async_close()
    with pytest.raises(asyncio.CancelledError):
        await poll
    server.assert_clean()


async def test_forced_unload_cancels_long_io_without_blocking_event_loop() -> None:
    """Long I/O yields to HA and is force-cancelled at the shutdown budget."""

    class HangingClient:
        def __init__(self) -> None:
            self.calls = 0
            self.cancelled = False
            self.closed = False
            self.blocked = asyncio.Event()

        async def async_get_source(self) -> SourceStatus:
            self.calls += 1
            if self.calls == 1:
                return SourceStatus(Source.OPT, None, False, False)
            self.blocked.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

        async def async_close(self) -> None:
            self.closed = True

    client = HangingClient()
    runtime = KefLsxRuntime(
        "127.0.0.1",
        client=client,  # type: ignore[arg-type]
        timing=RuntimeTiming(shutdown_timeout=0.01),
    )
    await runtime.async_start()
    poll = asyncio.create_task(runtime.async_poll())
    await client.blocked.wait()

    heartbeat = asyncio.Event()
    asyncio.get_running_loop().call_soon(heartbeat.set)
    await asyncio.wait_for(heartbeat.wait(), 0.1)
    async with asyncio.timeout(0.2):
        await runtime.async_close()

    with pytest.raises(asyncio.CancelledError):
        await poll
    assert runtime._worker is None
    assert runtime._closed
    assert client.cancelled
    assert client.closed
