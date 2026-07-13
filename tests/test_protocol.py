"""Protocol encoding, decoding, and stream-framing tests."""

from __future__ import annotations

import pytest

from custom_components.kef_lsx.errors import (
    AmbiguousCommandError,
    MalformedResponseError,
    QueueBusyError,
)
from custom_components.kef_lsx.protocol import (
    SOURCE_REGISTER,
    FrameParser,
    GetFrame,
    Source,
    decode_source_value,
    decode_volume_value,
    encode_get,
    encode_set_source,
    encode_set_volume,
)


@pytest.mark.parametrize(
    ("source", "standby", "normal_on"),
    [
        (Source.WIFI, 20, 0x02),
        (Source.WIFI, 60, 0x12),
        (Source.WIFI, None, 0x22),
        (Source.BLUETOOTH, 20, 0x09),
        (Source.BLUETOOTH, 60, 0x19),
        (Source.BLUETOOTH, None, 0x29),
        (Source.AUX, 20, 0x0A),
        (Source.AUX, 60, 0x1A),
        (Source.AUX, None, 0x2A),
        (Source.OPT, 20, 0x0B),
        (Source.OPT, 60, 0x1B),
        (Source.OPT, None, 0x2B),
    ],
)
@pytest.mark.parametrize("inverse", [False, True])
@pytest.mark.parametrize("power_on", [False, True])
def test_source_encoding_matches_lsx_gen1_table(
    source: Source,
    standby: int | None,
    normal_on: int,
    inverse: bool,
    power_on: bool,
) -> None:
    """Every supported source/standby/orientation/power value is exact."""
    expected = normal_on + (0x40 if inverse else 0) + (0 if power_on else 0x80)
    assert encode_set_source(source, standby, inverse, power_on) == bytes(
        (0x53, 0x30, 0x81, expected)
    )


def test_source_decoder_handles_off_state_and_response_48_compatibility() -> None:
    """Source replies expose power bits and retain aiokef's response-48 quirk."""
    assert decode_source_value(0xAB).source is Source.OPT
    assert decode_source_value(0xAB).power_on is False
    assert decode_source_value(0xAB).standby is None

    status = decode_source_value(48)
    assert status.source is Source.WIFI
    assert status.standby == 60
    assert status.inverse is True
    assert status.power_on is True


def test_volume_encoding_and_decoding_preserve_mute_bit() -> None:
    """Mute is bit 7 while the lower value remains absolute volume."""
    assert encode_set_volume(40, muted=False) == b"S%\x81\x28"
    assert encode_set_volume(40, muted=True) == b"S%\x81\xa8"
    assert decode_volume_value(0xA8).volume == 40
    assert decode_volume_value(0xA8).muted is True


def test_frame_parser_buffers_chunks_and_emits_combined_frames() -> None:
    """TCP chunk boundaries and combined frames do not change framing."""
    parser = FrameParser()
    assert parser.feed(b"R0") == []
    assert parser.feed(b"\x81\x52\x00R%\x81") == [GetFrame(SOURCE_REGISTER, 0x52, 0)]
    assert parser.feed(b"\x28\x01") == [GetFrame(0x25, 40, 1)]


@pytest.mark.parametrize("payload", [b"bad", b"R0\x82", b"R" * 4097])
def test_frame_parser_rejects_malformed_or_oversized_input(payload: bytes) -> None:
    """Invalid stream data becomes a stable typed error."""
    with pytest.raises(MalformedResponseError):
        FrameParser().feed(payload)


def test_protocol_rejects_invalid_values_before_writing() -> None:
    """Out-of-range values cannot produce protocol bytes."""
    with pytest.raises(ValueError):
        encode_set_volume(101, muted=False)
    with pytest.raises(ValueError):
        encode_set_source(Source.OPT, 30, False, True)
    with pytest.raises(ValueError):
        decode_source_value(0)
    assert encode_get(SOURCE_REGISTER) == b"G0\x80"


def test_runtime_scheduler_errors_share_the_typed_lsx_base_contract() -> None:
    """Runtime queue and ambiguous-result failures carry attempt metadata."""
    assert QueueBusyError("queue full").write_attempted is False
    assert (
        AmbiguousCommandError("reply lost", write_attempted=True).write_attempted
        is True
    )
