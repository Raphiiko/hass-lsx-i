"""Deterministic real-TCP fake for the legacy KEF LSX protocol."""

from __future__ import annotations

import asyncio
import socket
from collections import deque
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, field

GET = 0x47
SET = 0x53
GET_END = 0x80
SET_MID = 0x81
RESPONSE = 0x52
SOURCE_REGISTER = 0x30
VOLUME_REGISTER = 0x25
SET_OK = bytes((RESPONSE, 0x11, 0xFF))

_SOURCE_BASE = {"Wifi": 2, "Bluetooth": 9, "Aux": 10, "Opt": 11}
_STANDBY_OFFSET = {20: 0, 60: 16, None: 32}
_SOURCE_CODES = {
    base + standby_offset + (64 if inverse else 0): (source, standby, inverse)
    for source, base in _SOURCE_BASE.items()
    for standby, standby_offset in _STANDBY_OFFSET.items()
    for inverse in (False, True)
}


def get_reply(register: int, value: int, trailer: int = 0) -> bytes:
    """Build a five-byte GET reply."""
    return bytes((RESPONSE, register, SET_MID, value, trailer))


@dataclass(frozen=True, slots=True)
class Command:
    """One parsed LSX command."""

    raw: bytes
    register: int
    value: int | None


@dataclass(slots=True)
class SpeakerState:
    """Mutable speaker state used by automatic replies."""

    power: bool = True
    source: str = "Opt"
    standby: int | None = None
    inverse: bool = False
    volume: int = 20
    muted: bool = False


class Action:
    """Marker base for deterministic scripted behavior."""


@dataclass(frozen=True, slots=True)
class AutoReply(Action):
    """Apply the state model and send its normal response."""


@dataclass(frozen=True, slots=True)
class Reply(Action):
    """Write one exact reply."""

    payload: bytes


@dataclass(frozen=True, slots=True)
class ReplyChunks(Action):
    """Write response chunks with explicit gates between them."""

    chunks: tuple[bytes, ...]
    gates: tuple[asyncio.Event, ...] = ()

    def __post_init__(self) -> None:
        if len(self.gates) > max(0, len(self.chunks) - 1):
            raise ValueError("ReplyChunks accepts at most one gate between chunks")


@dataclass(frozen=True, slots=True)
class CombinedReply(Action):
    """Write multiple complete frames in one transport write."""

    frames: tuple[bytes, ...]


@dataclass(frozen=True, slots=True)
class Delay(Action):
    """Wait real time before running another action."""

    seconds: float
    action: Action


@dataclass(frozen=True, slots=True)
class GateThen(Action):
    """Wait for an explicit event before running another action."""

    gate: asyncio.Event
    action: Action


@dataclass(frozen=True, slots=True)
class DropReply(Action):
    """Consume the command without writing a reply."""


@dataclass(frozen=True, slots=True)
class CloseGracefully(Action):
    """Close the accepted stream without a reply."""


@dataclass(frozen=True, slots=True)
class AbortConnection(Action):
    """Abort the accepted transport without flushing."""


@dataclass(frozen=True, slots=True)
class Reject(Action):
    """Return a deterministic non-OK SET response."""

    payload: bytes = bytes((RESPONSE, 0x12, 0xFF))


@dataclass(frozen=True, slots=True)
class Malformed(Action):
    """Write deliberately malformed response bytes."""

    payload: bytes


@dataclass(slots=True)
class Step:
    """One ordered command expectation and its action."""

    expect: bytes
    action: Action = field(default_factory=AutoReply)
    label: str = ""
    received: asyncio.Event = field(default_factory=asyncio.Event, init=False)


@dataclass(frozen=True, slots=True)
class TranscriptEvent:
    """Immutable diagnostic record for one fake-server event."""

    sequence: int
    loop_time: float
    connection_id: int
    exchange_id: int | None
    event: str
    raw_bytes: bytes = b""
    command: Command | None = None
    label: str = ""
    open_connection_count: int = 0
    in_flight_exchange_count: int = 0


@dataclass(frozen=True, slots=True)
class ConcurrencyViolation:
    """A new exchange started before earlier exchanges completed."""

    existing_exchange_ids: tuple[int, ...]
    new_exchange_id: int
    connection_id: int


class FakeLsxServer:
    """Stateful IPv4 loopback server speaking the legacy LSX wire format."""

    host = "127.0.0.1"
    family = socket.AF_INET

    def __init__(
        self, steps: Sequence[Step] | None = None, *, state: SpeakerState | None = None
    ) -> None:
        self._server: asyncio.Server | None = None
        self._handlers: set[asyncio.Task[None]] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self._command_changed = asyncio.Condition()
        self.commands: list[Command] = []
        self.errors: list[BaseException] = []
        self.state = state or SpeakerState()
        self.transcript: list[TranscriptEvent] = []
        self._steps = deque(steps or ())
        self._scripted = steps is not None
        self._event_sequence = 0
        self._connection_sequence = 0
        self._exchange_sequence = 0
        self._open_connection_count = 0
        self._in_flight_exchange_count = 0
        self._active_exchanges: dict[int, int] = {}
        self.max_in_flight_exchanges = 0
        self.max_open_connections = 0
        self.concurrent_exchange_violations: list[ConcurrencyViolation] = []
        self._gates: set[asyncio.Event] = set()
        self.port = 0

    async def __aenter__(self) -> FakeLsxServer:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def start(self, port: int = 0) -> None:
        """Start on an OS-assigned IPv4 loopback port by default."""
        if self._server is not None:
            raise RuntimeError("Fake LSX server is already started")
        self._server = await asyncio.start_server(
            self._connected, self.host, port, family=self.family
        )
        sockets: Sequence[socket.socket] = self._server.sockets or ()
        if not sockets:
            raise RuntimeError("Fake LSX server did not expose a listening socket")
        self.port = int(sockets[0].getsockname()[1])

    def queue(self, *steps: Step) -> None:
        """Append strict ordered steps, including after the server has started."""
        self._scripted = True
        self._steps.extend(steps)

    @property
    def remaining_steps(self) -> int:
        """Return the number of unconsumed strict expectations."""
        return len(self._steps)

    def _connected(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._connection_sequence += 1
        connection_id = self._connection_sequence
        task = asyncio.create_task(self._handle(reader, writer, connection_id))
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        connection_id: int,
    ) -> None:
        self._writers.add(writer)
        self._open_connection_count += 1
        self.max_open_connections = max(
            self.max_open_connections, self._open_connection_count
        )
        self._record(connection_id, None, "accepted")
        buffer = bytearray()
        actions: set[asyncio.Task[None]] = set()
        try:
            while chunk := await reader.read(100):
                buffer.extend(chunk)
                while len(buffer) >= 3:
                    length = 3 if buffer[0] == GET else 4 if buffer[0] == SET else 0
                    if length == 0:
                        raise ValueError(f"Unknown LSX command leader: {buffer[0]:#x}")
                    if len(buffer) < length:
                        break
                    raw = bytes(buffer[:length])
                    del buffer[:length]
                    command = self._parse(raw)
                    self.commands.append(command)
                    self._exchange_sequence += 1
                    exchange_id = self._exchange_sequence
                    if self._active_exchanges:
                        self.concurrent_exchange_violations.append(
                            ConcurrencyViolation(
                                tuple(self._active_exchanges),
                                exchange_id,
                                connection_id,
                            )
                        )
                    self._active_exchanges[exchange_id] = connection_id
                    self._in_flight_exchange_count += 1
                    self.max_in_flight_exchanges = max(
                        self.max_in_flight_exchanges,
                        self._in_flight_exchange_count,
                    )
                    step = self._take_step(command)
                    label = step.label if step is not None else ""
                    if step is not None:
                        step.received.set()
                    self._record(
                        connection_id,
                        exchange_id,
                        "command",
                        raw=raw,
                        command=command,
                        label=label,
                    )
                    async with self._command_changed:
                        self._command_changed.notify_all()
                    action = (
                        step.action
                        if step is not None
                        else Reply(self._auto_reply(command))
                    )
                    task = asyncio.create_task(
                        self._run_exchange(
                            action,
                            writer,
                            connection_id,
                            exchange_id,
                            command,
                            label,
                        )
                    )
                    actions.add(task)
                    task.add_done_callback(actions.discard)
        except asyncio.CancelledError:
            raise
        except BaseException as err:
            self.errors.append(err)
        finally:
            for task in tuple(actions):
                task.cancel()
            if actions:
                await asyncio.gather(*tuple(actions), return_exceptions=True)
            for exchange_id, owner_connection_id in tuple(
                self._active_exchanges.items()
            ):
                if owner_connection_id == connection_id:
                    self._finish_exchange(exchange_id)
            self._writers.discard(writer)
            self._open_connection_count -= 1
            self._record(connection_id, None, "closed")
            await self._close_writer(writer)

    @staticmethod
    def _parse(raw: bytes) -> Command:
        if raw[0] == GET:
            if raw[2] != GET_END:
                raise ValueError(f"Malformed GET command: {raw!r}")
            return Command(raw, raw[1], None)
        if raw[2] != SET_MID:
            raise ValueError(f"Malformed SET command: {raw!r}")
        return Command(raw, raw[1], raw[3])

    def _auto_reply(self, command: Command) -> bytes:
        """Apply one command to the state model and build its normal reply."""
        if command.value is None:
            if command.register == SOURCE_REGISTER:
                value = (
                    _SOURCE_BASE[self.state.source]
                    + _STANDBY_OFFSET[self.state.standby]
                    + (64 if self.state.inverse else 0)
                    + (0 if self.state.power else 128)
                )
            elif command.register == VOLUME_REGISTER:
                value = self.state.volume + (128 if self.state.muted else 0)
            else:
                value = 0
            return get_reply(command.register, value)

        if command.register == SOURCE_REGISTER:
            self.state.power = command.value < 128
            source_code = command.value % 128
            if source_code not in _SOURCE_CODES:
                return bytes((RESPONSE, 0x12, 0xFF))
            self.state.source, self.state.standby, self.state.inverse = _SOURCE_CODES[
                source_code
            ]
        elif command.register == VOLUME_REGISTER:
            self.state.muted = command.value >= 128
            self.state.volume = command.value % 128
        return SET_OK

    def _take_step(self, command: Command) -> Step | None:
        if not self._scripted:
            return None
        if not self._steps:
            raise AssertionError(
                f"Unexpected command with no scripted step: {command.raw!r}"
            )
        step = self._steps.popleft()
        if command.raw != step.expect:
            raise AssertionError(
                f"Unexpected command {command.raw!r}; expected {step.expect!r}"
                + (f" ({step.label})" if step.label else "")
            )
        return step

    async def _run_exchange(
        self,
        action: Action,
        writer: asyncio.StreamWriter,
        connection_id: int,
        exchange_id: int,
        command: Command,
        label: str,
    ) -> None:
        try:
            await self._execute_action(
                action, writer, connection_id, exchange_id, command, label
            )
        except asyncio.CancelledError:
            raise
        except BrokenPipeError, ConnectionResetError:
            self._record(
                connection_id,
                exchange_id,
                "write_failed",
                command=command,
                label=label,
            )
        except BaseException as err:
            self.errors.append(err)
        finally:
            if not self._contains_drop(action):
                self._finish_exchange(exchange_id)

    @staticmethod
    def _contains_drop(action: Action) -> bool:
        """Return whether an action intentionally leaves the caller waiting."""
        if isinstance(action, DropReply):
            return True
        if isinstance(action, (Delay, GateThen)):
            return FakeLsxServer._contains_drop(action.action)
        return False

    def _finish_exchange(self, exchange_id: int) -> None:
        """Remove one active exchange exactly once."""
        if self._active_exchanges.pop(exchange_id, None) is not None:
            self._in_flight_exchange_count -= 1

    async def _execute_action(
        self,
        action: Action,
        writer: asyncio.StreamWriter,
        connection_id: int,
        exchange_id: int,
        command: Command,
        label: str,
    ) -> None:
        if isinstance(action, AutoReply):
            await self._execute_action(
                Reply(self._auto_reply(command)),
                writer,
                connection_id,
                exchange_id,
                command,
                label,
            )
            return
        if isinstance(action, CloseGracefully):
            self._record(
                connection_id,
                exchange_id,
                "fin",
                command=command,
                label=label,
            )
            await self._close_writer(writer)
            return
        if isinstance(action, AbortConnection):
            self._record(
                connection_id,
                exchange_id,
                "abort",
                command=command,
                label=label,
            )
            writer.transport.abort()
            return
        if isinstance(action, (Reject, Malformed)):
            self._record(
                connection_id,
                exchange_id,
                "reject" if isinstance(action, Reject) else "malformed",
                raw=action.payload,
                command=command,
                label=label,
            )
            await self._execute_action(
                Reply(action.payload),
                writer,
                connection_id,
                exchange_id,
                command,
                label,
            )
            return
        if isinstance(action, DropReply):
            self._record(
                connection_id,
                exchange_id,
                "drop",
                command=command,
                label=label,
            )
            return
        if isinstance(action, GateThen):
            self._gates.add(action.gate)
            self._record(
                connection_id,
                exchange_id,
                "gate",
                command=command,
                label=label,
            )
            try:
                await action.gate.wait()
            finally:
                self._gates.discard(action.gate)
            await self._execute_action(
                action.action,
                writer,
                connection_id,
                exchange_id,
                command,
                label,
            )
            return
        if isinstance(action, Delay):
            self._record(
                connection_id,
                exchange_id,
                "delay",
                command=command,
                label=label,
            )
            await asyncio.sleep(action.seconds)
            await self._execute_action(
                action.action,
                writer,
                connection_id,
                exchange_id,
                command,
                label,
            )
            return
        if isinstance(action, ReplyChunks):
            chunks = action.chunks
        elif isinstance(action, CombinedReply):
            chunks = (b"".join(action.frames),)
        else:
            if not isinstance(action, Reply):
                raise TypeError(f"Unsupported fake LSX action: {action!r}")
            chunks = (action.payload,)
        gates = action.gates if isinstance(action, ReplyChunks) else ()
        self._gates.update(gates)
        try:
            for index, payload in enumerate(chunks):
                writer.write(payload)
                await writer.drain()
                self._record(
                    connection_id,
                    exchange_id,
                    "write",
                    raw=payload,
                    command=command,
                    label=label,
                )
                if index < len(gates):
                    await gates[index].wait()
        finally:
            self._gates.difference_update(gates)

    def _record(
        self,
        connection_id: int,
        exchange_id: int | None,
        event: str,
        *,
        raw: bytes = b"",
        command: Command | None = None,
        label: str = "",
    ) -> None:
        self._event_sequence += 1
        self.transcript.append(
            TranscriptEvent(
                sequence=self._event_sequence,
                loop_time=asyncio.get_running_loop().time(),
                connection_id=connection_id,
                exchange_id=exchange_id,
                event=event,
                raw_bytes=raw,
                command=command,
                label=label,
                open_connection_count=self._open_connection_count,
                in_flight_exchange_count=self._in_flight_exchange_count,
            )
        )

    async def wait_for_commands(self, count: int, wait_seconds: float = 1.0) -> None:
        """Wait until at least ``count`` commands have been parsed."""
        async with asyncio.timeout(wait_seconds):
            async with self._command_changed:
                await self._command_changed.wait_for(
                    lambda: len(self.commands) >= count
                )

    async def close(self) -> None:
        """Close listener, accepted streams, and handler tasks."""
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()
        for gate in tuple(self._gates):
            gate.set()
        for writer in tuple(self._writers):
            writer.close()
        for writer in tuple(self._writers):
            await self._close_writer(writer)
        if self._handlers:
            await asyncio.gather(*tuple(self._handlers), return_exceptions=True)
        self.port = 0

    @staticmethod
    async def _close_writer(writer: asyncio.StreamWriter) -> None:
        writer.close()
        with suppress(ConnectionError, TimeoutError):
            async with asyncio.timeout(0.5):
                await writer.wait_closed()

    def assert_clean(self) -> None:
        """Raise any error captured by a background connection handler."""
        if self.errors:
            details = "; ".join(repr(error) for error in self.errors)
            raise AssertionError(f"Fake LSX handler errors: {details}")
        if self._steps:
            labels = [step.label or repr(step.expect) for step in self._steps]
            raise AssertionError(f"Unconsumed fake LSX steps: {labels!r}")
        if self.concurrent_exchange_violations:
            raise AssertionError(
                "Concurrent fake LSX exchanges: "
                f"{self.concurrent_exchange_violations!r}"
            )
