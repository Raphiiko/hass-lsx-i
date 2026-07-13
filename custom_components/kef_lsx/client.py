"""Typed asynchronous client for the legacy LSX TCP protocol."""

from __future__ import annotations

import asyncio
import errno

from .errors import (
    ClientClosedError,
    CommandRejectedError,
    ConnectionLostError,
    ConnectTimeoutError,
    LsxError,
    MalformedResponseError,
    ResponseTimeoutError,
)
from .errors import (
    ConnectRefusedError as LsxConnectRefusedError,
)
from .protocol import (
    SET_OK_STATUS,
    SOURCE_REGISTER,
    VOLUME_REGISTER,
    FrameParser,
    GetFrame,
    Source,
    SourceStatus,
    VolumeStatus,
    decode_source_value,
    decode_volume_value,
    encode_get,
    encode_set_source,
    encode_set_volume,
)

_MAX_FRAMES_PER_EXCHANGE = 16


class LsxClient:
    """Perform serialized, one-connection-per-exchange LSX operations."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        connect_timeout: float = 1.0,
        response_timeout: float = 1.5,
        close_timeout: float = 0.5,
    ) -> None:
        if connect_timeout <= 0 or response_timeout <= 0 or close_timeout <= 0:
            raise ValueError("Timeout values must be positive")
        self.host = host
        self.port = port
        self._connect_timeout = connect_timeout
        self._response_timeout = response_timeout
        self._close_timeout = close_timeout
        self._exchange_lock = asyncio.Lock()
        self._active_writer: asyncio.StreamWriter | None = None
        self._closed = False

    async def async_get_source(self) -> SourceStatus:
        """Read and decode source, power, standby, and orientation."""
        frame = await self._async_exchange(
            encode_get(SOURCE_REGISTER), expected_register=SOURCE_REGISTER
        )
        if not isinstance(frame, GetFrame):
            raise MalformedResponseError(
                "Source GET returned no value frame", write_attempted=True
            )
        try:
            return decode_source_value(frame.value)
        except ValueError as err:
            raise MalformedResponseError(str(err), write_attempted=True) from err

    async def async_get_volume(self) -> VolumeStatus:
        """Read and decode absolute volume and mute state."""
        frame = await self._async_exchange(
            encode_get(VOLUME_REGISTER), expected_register=VOLUME_REGISTER
        )
        if not isinstance(frame, GetFrame):
            raise MalformedResponseError(
                "Volume GET returned no value frame", write_attempted=True
            )
        try:
            return decode_volume_value(frame.value)
        except ValueError as err:
            raise MalformedResponseError(str(err), write_attempted=True) from err

    async def async_set_source(
        self,
        source: Source | str,
        *,
        standby: int | None,
        inverse: bool,
        power_on: bool,
    ) -> None:
        """Send one absolute source/power command."""
        await self._async_exchange(
            encode_set_source(source, standby, inverse, power_on),
            expected_register=None,
        )

    async def async_set_volume(self, volume: int, *, muted: bool) -> None:
        """Send one absolute volume/mute command."""
        await self._async_exchange(
            encode_set_volume(volume, muted=muted), expected_register=None
        )

    async def async_close(self) -> None:
        """Permanently close this client; repeated calls are harmless."""
        async with self._exchange_lock:
            if self._closed:
                return
            self._closed = True
            writer, self._active_writer = self._active_writer, None
            if writer is not None:
                await self._async_close_writer(writer)

    async def _async_exchange(
        self, request: bytes, *, expected_register: int | None
    ) -> GetFrame | None:
        """Execute one bounded request/reply exchange without retries."""
        async with self._exchange_lock:
            if self._closed:
                raise ClientClosedError("KEF LSX client is closed")

            loop = asyncio.get_running_loop()
            exchange_deadline = (
                loop.time() + self._connect_timeout + self._response_timeout
            )
            write_attempted = False
            reader: asyncio.StreamReader
            writer: asyncio.StreamWriter | None = None
            try:
                try:
                    async with asyncio.timeout_at(
                        min(exchange_deadline, loop.time() + self._connect_timeout)
                    ):
                        reader, writer = await asyncio.open_connection(
                            self.host, self.port
                        )
                except TimeoutError as err:
                    raise ConnectTimeoutError(
                        "Timed out connecting to KEF LSX"
                    ) from err
                except ConnectionRefusedError as err:
                    raise LsxConnectRefusedError(
                        "KEF LSX refused the TCP connection"
                    ) from err
                except OSError as err:
                    if err.errno in {errno.ECONNREFUSED, 10061}:
                        raise LsxConnectRefusedError(
                            "KEF LSX refused the TCP connection"
                        ) from err
                    raise ConnectionLostError(
                        "Unable to establish the KEF LSX connection"
                    ) from err

                self._active_writer = writer
                parser = FrameParser()
                frames_examined = 0
                response_deadline = min(
                    exchange_deadline, loop.time() + self._response_timeout
                )
                try:
                    async with asyncio.timeout_at(response_deadline):
                        write_attempted = True
                        writer.write(request)
                        await writer.drain()
                        while True:
                            chunk = await reader.read(1024)
                            if not chunk:
                                if parser.has_pending_data:
                                    raise MalformedResponseError(
                                        "Connection ended with a partial response"
                                    )
                                raise ConnectionLostError(
                                    "Connection ended before the expected response"
                                )
                            for frame in parser.feed(chunk):
                                frames_examined += 1
                                if frames_examined > _MAX_FRAMES_PER_EXCHANGE:
                                    raise MalformedResponseError(
                                        "Too many unrelated response frames"
                                    )
                                if expected_register is not None:
                                    if (
                                        isinstance(frame, GetFrame)
                                        and frame.register == expected_register
                                    ):
                                        return frame
                                    if isinstance(frame, GetFrame):
                                        continue
                                    raise MalformedResponseError(
                                        "Unexpected SET status while awaiting GET"
                                    )
                                if isinstance(frame, GetFrame):
                                    continue
                                if frame.status != SET_OK_STATUS:
                                    raise CommandRejectedError(frame.status)
                                return None
                except TimeoutError as err:
                    if parser.has_pending_data:
                        raise MalformedResponseError(
                            "Timed out with a partial response"
                        ) from err
                    raise ResponseTimeoutError(
                        "Timed out waiting for KEF LSX response"
                    ) from err
                except (BrokenPipeError, ConnectionResetError, OSError) as err:
                    raise ConnectionLostError(
                        "KEF LSX connection was lost during exchange"
                    ) from err
            except LsxError as err:
                err.write_attempted = write_attempted
                raise
            finally:
                if writer is not None:
                    if self._active_writer is writer:
                        self._active_writer = None
                    await self._async_close_writer(writer)

    async def _async_close_writer(self, writer: asyncio.StreamWriter) -> None:
        """Close a stream under a small independent cleanup budget."""
        writer.close()
        try:
            async with asyncio.timeout(self._close_timeout):
                await writer.wait_closed()
        except TimeoutError:
            writer.transport.abort()
        except ConnectionError, OSError:
            pass
        # Give the peer handler one scheduling turn before another connection.
        await asyncio.sleep(0)
