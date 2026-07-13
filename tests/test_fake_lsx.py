"""Behavior tests for the deterministic LSX TCP fake."""

from __future__ import annotations

import asyncio
import socket

import pytest
from fake_lsx import (
    SET_OK,
    AbortConnection,
    Action,
    AutoReply,
    CloseGracefully,
    CombinedReply,
    Delay,
    DropReply,
    FakeLsxServer,
    GateThen,
    Malformed,
    Reject,
    Reply,
    ReplyChunks,
    Step,
    get_reply,
)


async def test_incremental_get_request_receives_state_reply() -> None:
    """The fake buffers a fragmented GET request and replies on real TCP."""
    async with FakeLsxServer() as speaker:
        assert speaker.host == "127.0.0.1"
        assert speaker.port > 0
        assert speaker.family == socket.AF_INET

        reader, writer = await asyncio.open_connection(speaker.host, speaker.port)
        writer.write(b"G")
        await writer.drain()
        await asyncio.sleep(0)
        assert speaker.commands == []

        writer.write(b"0\x80")
        await writer.drain()

        assert await reader.readexactly(5) == get_reply(0x30, 0x2B)
        await speaker.wait_for_commands(1)
        assert [command.raw for command in speaker.commands] == [b"G0\x80"]

        writer.close()
        await writer.wait_closed()

    speaker.assert_clean()


async def test_detects_cross_connection_exchange_overlap() -> None:
    """A gated exchange makes a simultaneous second exchange visible."""
    gate = asyncio.Event()
    first = Step(b"G0\x80", GateThen(gate, AutoReply()), label="blocked")
    second = Step(b"G%\x80", AutoReply(), label="overlap")
    async with FakeLsxServer([first, second]) as speaker:
        reader1, writer1 = await asyncio.open_connection(speaker.host, speaker.port)
        writer1.write(b"G0\x80")
        await writer1.drain()
        await first.received.wait()

        reader2, writer2 = await asyncio.open_connection(speaker.host, speaker.port)
        writer2.write(b"G%\x80")
        await writer2.drain()
        await second.received.wait()
        assert await reader2.readexactly(5) == get_reply(0x25, 20)

        assert speaker.max_in_flight_exchanges == 2
        assert len(speaker.concurrent_exchange_violations) == 1
        assert speaker.max_open_connections == 2

        gate.set()
        assert await reader1.readexactly(5) == get_reply(0x30, 0x2B)
        writer1.close()
        writer2.close()
        await writer1.wait_closed()
        await writer2.wait_closed()

    # The deliberate violation is asserted above rather than considered clean.
    assert speaker.errors == []


async def test_detects_same_connection_pipelining() -> None:
    """Two commands written on one stream are not silently serialized by the fake."""
    gate = asyncio.Event()
    first = Step(b"G0\x80", GateThen(gate, AutoReply()), label="first")
    second = Step(b"G%\x80", AutoReply(), label="pipelined")
    async with FakeLsxServer([first, second]) as speaker:
        reader, writer = await asyncio.open_connection(speaker.host, speaker.port)
        writer.write(b"G0\x80G%\x80")
        await writer.drain()
        await first.received.wait()
        await asyncio.wait_for(second.received.wait(), 0.2)

        assert speaker.max_in_flight_exchanges == 2
        assert len(speaker.concurrent_exchange_violations) == 1
        gate.set()
        assert len(await reader.readexactly(10)) == 10
        writer.close()
        await writer.wait_closed()

    assert speaker.errors == []


async def test_unexpected_command_is_reported_by_assert_clean() -> None:
    """A strict scenario mismatch cannot disappear in a handler task."""
    speaker = FakeLsxServer([Step(b"G0\x80", label="expected source")])
    async with speaker:
        reader, writer = await asyncio.open_connection(speaker.host, speaker.port)
        writer.write(b"G%\x80")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(1), 1) == b""
        writer.close()

    with pytest.raises(AssertionError, match="Unexpected command"):
        speaker.assert_clean()


async def test_listener_can_refuse_and_recover_on_same_port() -> None:
    """Stopping and restarting the listener injects refusal and later recovery."""
    speaker = FakeLsxServer()
    await speaker.start()
    port = speaker.port
    await speaker.close()

    with pytest.raises(OSError):
        await asyncio.open_connection(speaker.host, port)

    await speaker.start(port)
    reader, writer = await asyncio.open_connection(speaker.host, port)
    writer.write(b"G0\x80")
    await writer.drain()
    assert await reader.readexactly(5) == get_reply(0x30, 0x2B)
    writer.close()
    await writer.wait_closed()
    await speaker.close()
    speaker.assert_clean()


async def test_scripted_auto_reply_uses_current_state() -> None:
    """AutoReply can participate in an otherwise strict ordered scenario."""
    step = Step(b"G0\x80", AutoReply(), label="automatic")
    async with FakeLsxServer([step]) as speaker:
        reader, writer = await asyncio.open_connection(speaker.host, speaker.port)
        writer.write(b"G0\x80")
        await writer.drain()
        assert await reader.readexactly(5) == get_reply(0x30, 0x2B)
        writer.close()
        await writer.wait_closed()

    speaker.assert_clean()


@pytest.mark.parametrize(
    ("action", "expected"),
    [(Reject(), b"R\x12\xff"), (Malformed(b"bad"), b"bad")],
)
async def test_reject_and_malformed_actions_write_fault_payloads(
    action: Action, expected: bytes
) -> None:
    """Command rejection and malformed server data are independently injectable."""
    step = Step(b"S0\x81\x2b", action, label="fault")
    async with FakeLsxServer([step]) as speaker:
        reader, writer = await asyncio.open_connection(speaker.host, speaker.port)
        writer.write(b"S0\x81\x2b")
        await writer.drain()
        assert await reader.readexactly(len(expected)) == expected
        writer.close()
        await writer.wait_closed()

    speaker.assert_clean()


@pytest.mark.parametrize("action", [CloseGracefully(), AbortConnection()])
async def test_close_and_abort_actions_end_connection(action: Action) -> None:
    """The fake can inject both orderly close and reset-like abort."""
    step = Step(b"G0\x80", action, label="disconnect")
    async with FakeLsxServer([step]) as speaker:
        reader, writer = await asyncio.open_connection(speaker.host, speaker.port)
        writer.write(b"G0\x80")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(1), 1) == b""
        writer.close()

    speaker.assert_clean()


async def test_drop_reply_consumes_selected_command_without_writing() -> None:
    """A selected reply can be dropped while the TCP connection stays open."""
    step = Step(b"G0\x80", DropReply(), label="dropped poll")
    async with FakeLsxServer([step]) as speaker:
        reader, writer = await asyncio.open_connection(speaker.host, speaker.port)
        writer.write(b"G0\x80")
        await writer.drain()
        await step.received.wait()

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(reader.read(1), 0.02)
        assert "drop" in [event.event for event in speaker.transcript]
        assert not writer.is_closing()

        writer.close()
        await writer.wait_closed()

    speaker.assert_clean()


async def test_drop_remains_in_flight_until_connection_closes() -> None:
    """A second connection during a dropped exchange is reported as overlap."""
    dropped = Step(b"G0\x80", DropReply(), label="dropped")
    overlapping = Step(b"G%\x80", AutoReply(), label="overlapping")
    async with FakeLsxServer([dropped, overlapping]) as speaker:
        reader1, writer1 = await asyncio.open_connection(speaker.host, speaker.port)
        writer1.write(b"G0\x80")
        await writer1.drain()
        await dropped.received.wait()

        reader2, writer2 = await asyncio.open_connection(speaker.host, speaker.port)
        writer2.write(b"G%\x80")
        await writer2.drain()
        await overlapping.received.wait()
        assert await reader2.readexactly(5) == get_reply(0x25, 20)

        assert speaker.max_in_flight_exchanges == 2
        assert len(speaker.concurrent_exchange_violations) == 1
        assert not writer1.is_closing()

        writer1.close()
        writer2.close()
        await writer1.wait_closed()
        await writer2.wait_closed()
        assert await reader1.read() == b""

    assert speaker.errors == []


async def test_gate_and_delay_control_when_reply_is_sent() -> None:
    """Gate and delay actions compose without blocking the event loop."""
    gate = asyncio.Event()
    step = Step(
        b"G0\x80",
        GateThen(gate, Delay(0.01, Reply(get_reply(0x30, 0x2B)))),
        label="delayed",
    )
    async with FakeLsxServer([step]) as speaker:
        reader, writer = await asyncio.open_connection(speaker.host, speaker.port)
        writer.write(b"G0\x80")
        await writer.drain()
        await step.received.wait()

        pending = asyncio.create_task(reader.readexactly(5))
        await asyncio.sleep(0)
        assert not pending.done()
        gate.set()
        assert await pending == get_reply(0x30, 0x2B)
        assert "delay" in [event.event for event in speaker.transcript]

        writer.close()
        await writer.wait_closed()

    speaker.assert_clean()


async def test_combined_reply_is_one_write_with_multiple_frames() -> None:
    """Several reply frames can be injected as one server write."""
    frames = (get_reply(0x25, 40), get_reply(0x30, 0x2B))
    step = Step(b"G0\x80", CombinedReply(frames), label="combined")
    async with FakeLsxServer([step]) as speaker:
        reader, writer = await asyncio.open_connection(speaker.host, speaker.port)
        writer.write(b"G0\x80")
        await writer.drain()
        assert await reader.readexactly(10) == b"".join(frames)
        assert [
            event.raw_bytes for event in speaker.transcript if event.event == "write"
        ] == [b"".join(frames)]
        writer.close()
        await writer.wait_closed()

    speaker.assert_clean()


async def test_reply_chunks_waits_for_explicit_gate() -> None:
    """A response can be split deterministically without timing sleeps."""
    gate = asyncio.Event()
    step = Step(
        b"G0\x80",
        ReplyChunks((b"R0", b"\x81\x2b\x00"), gates=(gate,)),
        label="fragmented reply",
    )
    async with FakeLsxServer([step]) as speaker:
        reader, writer = await asyncio.open_connection(speaker.host, speaker.port)
        writer.write(b"G0\x80")
        await writer.drain()

        assert await reader.readexactly(2) == b"R0"
        pending = asyncio.create_task(reader.readexactly(3))
        await asyncio.sleep(0)
        assert not pending.done()
        gate.set()
        assert await pending == b"\x81\x2b\x00"

        writer.close()
        await writer.wait_closed()

    speaker.assert_clean()


async def test_ordered_step_sends_exact_reply_and_captures_transcript() -> None:
    """A scripted step matches exact wire bytes and labels transcript events."""
    step = Step(b"G0\x80", Reply(b"five!"), label="source poll")
    async with FakeLsxServer([step]) as speaker:
        reader, writer = await asyncio.open_connection(speaker.host, speaker.port)
        writer.write(b"G0\x80")
        await writer.drain()

        assert await reader.readexactly(5) == b"five!"
        await asyncio.wait_for(step.received.wait(), 1)
        assert [event.event for event in speaker.transcript if event.exchange_id] == [
            "command",
            "write",
        ]
        assert {event.label for event in speaker.transcript if event.exchange_id} == {
            "source poll"
        }

        writer.close()
        await writer.wait_closed()

    speaker.assert_clean()


async def test_auto_reply_tracks_source_volume_and_mute() -> None:
    """Normal SETs mutate the fake state and later GETs report it."""
    async with FakeLsxServer() as speaker:
        reader, writer = await asyncio.open_connection(speaker.host, speaker.port)

        # Optical, never-standby, normal orientation, off.
        writer.write(b"S0\x81\xab")
        await writer.drain()
        assert await reader.readexactly(3) == SET_OK
        assert speaker.state.source == "Opt"
        assert speaker.state.power is False
        assert speaker.state.standby is None
        assert speaker.state.inverse is False

        writer.write(b"S%\x81\x28")  # volume 40
        await writer.drain()
        assert await reader.readexactly(3) == SET_OK
        writer.write(b"S%\x81\xa8")  # muted volume 40
        await writer.drain()
        assert await reader.readexactly(3) == SET_OK

        writer.write(b"G%\x80")
        await writer.drain()
        assert await reader.readexactly(5) == get_reply(0x25, 0xA8)
        writer.write(b"G0\x80")
        await writer.drain()
        assert await reader.readexactly(5) == get_reply(0x30, 0xAB)
        assert speaker.state.volume == 40
        assert speaker.state.muted is True

        writer.close()
        await writer.wait_closed()

    speaker.assert_clean()
