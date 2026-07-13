"""Pure legacy protocol encoding, decoding, and TCP response framing."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .errors import MalformedResponseError

GET = 0x47
SET = 0x53
RESPONSE = 0x52
GET_MARKER = 0x80
SET_MARKER = 0x81
FRAME_END = 0xFF
SET_OK_STATUS = 0x11

SOURCE_REGISTER = 0x30
VOLUME_REGISTER = 0x25

DEFAULT_BUFFER_LIMIT = 4096


class Source(StrEnum):
    """Sources supported by first-generation LSX speakers."""

    WIFI = "Wifi"
    BLUETOOTH = "Bluetooth"
    AUX = "Aux"
    OPT = "Opt"


@dataclass(frozen=True, slots=True)
class SourceStatus:
    """Decoded source, standby, orientation, and power state."""

    source: Source
    standby: int | None
    inverse: bool
    power_on: bool


@dataclass(frozen=True, slots=True)
class VolumeStatus:
    """Decoded absolute volume and mute state."""

    volume: int
    muted: bool


@dataclass(frozen=True, slots=True)
class GetFrame:
    """One complete five-byte GET response."""

    register: int
    value: int
    trailer: int


@dataclass(frozen=True, slots=True)
class SetStatusFrame:
    """One complete three-byte SET status response."""

    status: int


type ResponseFrame = GetFrame | SetStatusFrame

_SOURCE_BASE = {
    Source.WIFI: 0x02,
    Source.BLUETOOTH: 0x09,
    Source.AUX: 0x0A,
    Source.OPT: 0x0B,
}
_STANDBY_OFFSET = {20: 0x00, 60: 0x10, None: 0x20}


def _source_lookup() -> dict[int, tuple[Source, int | None, bool]]:
    """Build the accepted source response table."""
    lookup = {
        base + standby_offset + (0x40 if inverse else 0): (
            source,
            standby,
            inverse,
        )
        for source, base in _SOURCE_BASE.items()
        for standby, standby_offset in _STANDBY_OFFSET.items()
        for inverse in (False, True)
    }
    # Bluetooth-paired is reported by speakers but is not a settable source.
    for standby, standby_offset in _STANDBY_OFFSET.items():
        for inverse in (False, True):
            lookup[0x0F + standby_offset + (0x40 if inverse else 0)] = (
                Source.BLUETOOTH,
                standby,
                inverse,
            )
    return lookup


_SOURCE_LOOKUP = _source_lookup()


def coerce_source(source: Source | str) -> Source:
    """Convert a configured source string to the protocol enum."""
    if isinstance(source, Source):
        return source
    for candidate in Source:
        if candidate.value.casefold() == source.casefold():
            return candidate
    raise ValueError(f"Unsupported LSX source: {source!r}")


def encode_get(register: int) -> bytes:
    """Encode one three-byte register GET."""
    if not 0 <= register <= 0xFF:
        raise ValueError("Register must fit in one byte")
    return bytes((GET, register, GET_MARKER))


def encode_set(register: int, value: int) -> bytes:
    """Encode one four-byte register SET."""
    if not 0 <= register <= 0xFF or not 0 <= value <= 0xFF:
        raise ValueError("Register and value must fit in one byte")
    return bytes((SET, register, SET_MARKER, value))


def encode_set_source(
    source: Source | str,
    standby: int | None,
    inverse: bool,
    power_on: bool,
) -> bytes:
    """Encode an absolute LSX source/power SET."""
    source = coerce_source(source)
    try:
        standby_offset = _STANDBY_OFFSET[standby]
    except KeyError as err:
        raise ValueError("Standby must be 20, 60, or None") from err
    value = (
        _SOURCE_BASE[source]
        + standby_offset
        + (0x40 if inverse else 0)
        + (0 if power_on else 0x80)
    )
    return encode_set(SOURCE_REGISTER, value)


def decode_source_value(value: int) -> SourceStatus:
    """Decode one source register value, including the known value-48 quirk."""
    if not 0 <= value <= 0xFF:
        raise ValueError("Source response must fit in one byte")
    if value == 48:
        value = 82
    power_on = value <= 0x80
    encoded_source = value % 0x80
    try:
        source, standby, inverse = _SOURCE_LOOKUP[encoded_source]
    except KeyError as err:
        raise ValueError(f"Unsupported LSX source response: {value}") from err
    return SourceStatus(source, standby, inverse, power_on)


def encode_set_volume(volume: int, *, muted: bool) -> bytes:
    """Encode an absolute volume/mute SET."""
    if not 0 <= volume <= 100:
        raise ValueError("Volume must be between 0 and 100")
    return encode_set(VOLUME_REGISTER, volume + (0x80 if muted else 0))


def decode_volume_value(value: int) -> VolumeStatus:
    """Decode the volume register and mute bit."""
    if not 0 <= value <= 0xFF:
        raise ValueError("Volume response must fit in one byte")
    muted = value >= 0x80
    volume = value % 0x80
    if volume > 100:
        raise ValueError(f"Unsupported LSX volume response: {value}")
    return VolumeStatus(volume, muted)


class FrameParser:
    """Incrementally parse fixed-shape legacy response frames."""

    def __init__(self, *, buffer_limit: int = DEFAULT_BUFFER_LIMIT) -> None:
        self._buffer = bytearray()
        self._buffer_limit = buffer_limit

    @property
    def has_pending_data(self) -> bool:
        """Return whether an incomplete suffix remains."""
        return bool(self._buffer)

    def feed(self, data: bytes) -> list[ResponseFrame]:
        """Append stream bytes and emit all complete recognized frames."""
        self._buffer.extend(data)
        if len(self._buffer) > self._buffer_limit:
            self._fail("Response buffer exceeded its limit")

        frames: list[ResponseFrame] = []
        while self._buffer:
            if self._buffer[0] != RESPONSE:
                self._fail("Response did not start with R")
            if len(self._buffer) < 3:
                break

            marker = self._buffer[2]
            if marker == FRAME_END:
                frames.append(SetStatusFrame(self._buffer[1]))
                del self._buffer[:3]
                continue
            if marker != SET_MARKER:
                self._fail(f"Unexpected response marker 0x{marker:02x}")
            if len(self._buffer) < 5:
                break
            frames.append(
                GetFrame(
                    register=self._buffer[1],
                    value=self._buffer[3],
                    trailer=self._buffer[4],
                )
            )
            del self._buffer[:5]
        return frames

    def _fail(self, message: str) -> None:
        """Clear poisoned state and raise a typed framing error."""
        self._buffer.clear()
        raise MalformedResponseError(message)
