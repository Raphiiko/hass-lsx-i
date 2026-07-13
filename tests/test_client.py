"""Real-TCP behavior tests for the typed LSX client."""

from __future__ import annotations

import asyncio
import socket
from unittest.mock import patch

import pytest
from fake_lsx import (
    AbortConnection,
    CloseGracefully,
    CombinedReply,
    DropReply,
    FakeLsxServer,
    GateThen,
    Malformed,
    Reject,
    ReplyChunks,
    Step,
    get_reply,
)

from custom_components.kef_lsx.client import LsxClient
from custom_components.kef_lsx.errors import (
    ClientClosedError,
    CommandRejectedError,
    ConnectionLostError,
    ConnectRefusedError,
    ConnectTimeoutError,
    MalformedResponseError,
    ResponseTimeoutError,
)
from custom_components.kef_lsx.protocol import Source

pytestmark = pytest.mark.usefixtures("socket_enabled")


async def test_client_gets_source_and_volume_over_real_tcp() -> None:
    """Typed state reads use exact GETs and decode normal fake replies."""
    async with FakeLsxServer() as speaker:
        client = LsxClient(speaker.host, speaker.port)

        source = await client.async_get_source()
        volume = await client.async_get_volume()

        assert source.source is Source.OPT
        assert source.standby is None
        assert source.power_on is True
        assert volume.volume == 20
        assert volume.muted is False
        assert [command.raw for command in speaker.commands] == [
            b"G0\x80",
            b"G%\x80",
        ]
        assert speaker.max_in_flight_exchanges == 1
        await client.async_close()

    speaker.assert_clean()


async def test_client_sends_absolute_source_and_volume_sets() -> None:
    """Control methods put exact absolute LSX SETs on the wire."""
    steps = [
        Step(b"S0\x81\x6b", label="inverse optical wake"),
        Step(b"S%\x81\xa8", label="muted volume 40"),
    ]
    async with FakeLsxServer(steps) as speaker:
        client = LsxClient(speaker.host, speaker.port)
        assert (
            await client.async_set_source(
                Source.OPT, standby=None, inverse=True, power_on=True
            )
            is None
        )
        assert await client.async_set_volume(40, muted=True) is None
        await client.async_close()

    speaker.assert_clean()


async def test_client_handles_chunked_and_combined_get_replies() -> None:
    """Partial replies and an unrelated combined frame are matched safely."""
    chunk_gate = asyncio.Event()
    steps = [
        Step(
            b"G0\x80",
            ReplyChunks((b"R0", b"\x81\x2b\x00"), gates=(chunk_gate,)),
            label="chunked",
        ),
        Step(
            b"G0\x80",
            CombinedReply((get_reply(0x25, 40), get_reply(0x30, 0x52))),
            label="combined",
        ),
    ]
    async with FakeLsxServer(steps) as speaker:
        client = LsxClient(speaker.host, speaker.port)
        chunked_task = asyncio.create_task(client.async_get_source())
        await steps[0].received.wait()
        await asyncio.sleep(0)
        assert not chunked_task.done()
        chunk_gate.set()
        assert (await chunked_task).source is Source.OPT
        combined = await client.async_get_source()
        assert combined.source is Source.WIFI
        assert combined.inverse is True
        assert combined.standby == 60
        await client.async_close()

    speaker.assert_clean()


@pytest.mark.parametrize(
    ("action", "error"),
    [
        (Malformed(b"bad"), MalformedResponseError),
        (Malformed(b"R0"), MalformedResponseError),
        (Malformed(b"R" * 4097), MalformedResponseError),
        (Malformed(get_reply(0x30, 0)), MalformedResponseError),
        (CloseGracefully(), ConnectionLostError),
    ],
)
async def test_client_types_malformed_truncated_oversized_and_lost_replies(
    action: object, error: type[Exception]
) -> None:
    """Poisoned or lost streams fail with stable public error categories."""
    async with FakeLsxServer([Step(b"G0\x80", action)]) as speaker:
        client = LsxClient(
            speaker.host,
            speaker.port,
            connect_timeout=0.1,
            response_timeout=0.05,
            close_timeout=0.05,
        )
        with pytest.raises(error) as raised:
            await client.async_get_source()
        assert raised.value.write_attempted is True
        await client.async_close()

    speaker.assert_clean()


async def test_set_rejection_is_not_reported_as_transport_failure() -> None:
    """A well-formed non-OK status exposes the speaker's status byte."""
    async with FakeLsxServer([Step(b"S0\x81\x2b", Reject())]) as speaker:
        client = LsxClient(speaker.host, speaker.port)
        with pytest.raises(CommandRejectedError) as raised:
            await client.async_set_source(
                Source.OPT, standby=None, inverse=False, power_on=True
            )
        assert raised.value.status == 0x12
        assert raised.value.write_attempted is True
        await client.async_close()

    speaker.assert_clean()


async def test_dropped_reply_uses_one_bounded_attempt() -> None:
    """The client has no retry layer and resets after its response deadline."""
    async with FakeLsxServer([Step(b"G0\x80", DropReply())]) as speaker:
        client = LsxClient(
            speaker.host,
            speaker.port,
            connect_timeout=0.1,
            response_timeout=0.05,
            close_timeout=0.05,
        )
        started = asyncio.get_running_loop().time()
        with pytest.raises(ResponseTimeoutError) as raised:
            await client.async_get_source()
        elapsed = asyncio.get_running_loop().time() - started

        assert raised.value.write_attempted is True
        assert len(speaker.commands) == 1
        assert elapsed < 0.5
        await client.async_close()

    speaker.assert_clean()


async def test_connection_refusal_can_recover_on_the_next_operation() -> None:
    """A refused operation leaves no poisoned state for later recovery."""
    reservation = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    reservation.bind(("127.0.0.1", 0))
    port = int(reservation.getsockname()[1])
    speaker = FakeLsxServer()
    # Windows may take roughly two seconds to surface WSAECONNREFUSED for a
    # bound-but-not-listening socket, so this test budget must exceed that OS
    # reporting delay even though production keeps its shorter default.
    client = LsxClient(speaker.host, port, connect_timeout=3.0)

    try:
        with pytest.raises(ConnectRefusedError) as raised:
            await client.async_get_source()
    finally:
        reservation.close()
    assert raised.value.write_attempted is False

    await speaker.start(port)
    assert (await client.async_get_source()).source is Source.OPT
    await client.async_close()
    await speaker.close()
    speaker.assert_clean()


async def test_connection_reset_can_recover_on_the_next_operation() -> None:
    """An aborted exchange cannot poison the next operation."""
    async with FakeLsxServer(
        [Step(b"G0\x80", AbortConnection()), Step(b"G0\x80")]
    ) as speaker:
        client = LsxClient(speaker.host, speaker.port)
        with pytest.raises(ConnectionLostError):
            await client.async_get_source()

        assert (await client.async_get_source()).source is Source.OPT
        await client.async_close()

    speaker.assert_clean()


async def test_connect_timeout_has_its_own_typed_budget() -> None:
    """A stalled connector is cancelled at the connect deadline."""

    async def stalled_connection(*_args: object) -> None:
        await asyncio.sleep(10)

    client = LsxClient("127.0.0.1", 50001, connect_timeout=0.01)
    with (
        patch("asyncio.open_connection", new=stalled_connection),
        pytest.raises(ConnectTimeoutError) as raised,
    ):
        await client.async_get_source()
    assert raised.value.write_attempted is False
    await client.async_close()


async def test_default_client_budget_allows_a_slow_lsx_accept() -> None:
    """The production budget tolerates a listener that accepts after one second."""
    async with FakeLsxServer([Step(b"G0\x80")]) as speaker:
        real_open_connection = asyncio.open_connection

        async def slow_open_connection(*args: object, **kwargs: object) -> object:
            await asyncio.sleep(1.1)
            return await real_open_connection(*args, **kwargs)

        client = LsxClient(speaker.host, speaker.port)
        try:
            with patch("asyncio.open_connection", new=slow_open_connection):
                assert (await client.async_get_source()).source is Source.OPT
        finally:
            await client.async_close()

    speaker.assert_clean()


async def test_client_waits_before_reopening_after_close() -> None:
    """A completed exchange gives the single-connection listener time to recycle."""
    async with FakeLsxServer([Step(b"G0\x80"), Step(b"G%\x80")]) as speaker:
        client = LsxClient(speaker.host, speaker.port, reconnect_delay=0.05)
        await client.async_get_source()
        await client.async_get_volume()
        await client.async_close()

    accepted = [event for event in speaker.transcript if event.event == "accepted"]
    assert accepted[1].loop_time - accepted[0].loop_time >= 0.04
    speaker.assert_clean()


async def test_defensive_serialization_prevents_concurrent_exchanges() -> None:
    """Even accidental direct concurrent calls cannot overlap TCP exchanges."""
    gate = asyncio.Event()
    first = Step(
        b"G0\x80",
        GateThen(gate, CombinedReply((get_reply(0x30, 0x2B),))),
        label="blocked source",
    )
    second = Step(b"G%\x80", label="queued volume")
    async with FakeLsxServer([first, second]) as speaker:
        client = LsxClient(speaker.host, speaker.port)
        source_task = asyncio.create_task(client.async_get_source())
        await first.received.wait()
        volume_task = asyncio.create_task(client.async_get_volume())
        await asyncio.sleep(0.02)
        assert len(speaker.commands) == 1

        gate.set()
        assert (await source_task).source is Source.OPT
        assert (await volume_task).volume == 20
        assert speaker.max_in_flight_exchanges == 1
        assert speaker.concurrent_exchange_violations == []
        await client.async_close()

    speaker.assert_clean()


async def test_close_is_idempotent_and_rejects_new_operations() -> None:
    """Unload can close repeatedly and no later operation reopens a socket."""
    client = LsxClient("127.0.0.1", 9)
    await client.async_close()
    await client.async_close()
    with pytest.raises(ClientClosedError):
        await client.async_get_source()


async def test_cancellation_propagates_after_closing_the_active_stream() -> None:
    """Cancelling a read is never translated or swallowed by cleanup."""
    gate = asyncio.Event()
    first = Step(b"G0\x80", GateThen(gate, DropReply()), label="cancelled")
    second = Step(b"G0\x80", label="recovery")
    async with FakeLsxServer([first, second]) as speaker:
        client = LsxClient(speaker.host, speaker.port)
        task = asyncio.create_task(client.async_get_source())
        await first.received.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert (await client.async_get_source()).source is Source.OPT
        assert speaker.max_in_flight_exchanges == 1
        await client.async_close()

    speaker.assert_clean()
