# Legacy LSX protocol notes

This document records the compatibility facts used by the prerelease. They are
derived from `aiokef` 0.2.16 and issue evidence, then independently implemented.
They are not an official KEF specification. See `THIRD_PARTY_NOTICES.md`.

## Requests and responses

Requests have fixed lengths and no delimiter:

```text
GET: 47 <register> 80
SET: 53 <register> 81 <value>
```

Known registers used by the MVP are `0x30` for source/power/orientation/standby
and `0x25` for volume/mute. `0x31` transport commands and `0x27`–`0x2d` DSP
registers are intentionally not exposed initially.

A GET response is five bytes:

```text
52 <register> 81 <value> <opaque-trailer>
```

The trailer's checksum/meaning is unknown and is not claimed as validated. A
successful SET acknowledgement observed by `aiokef` is `52 11 ff`.

TCP has no message boundaries. The client incrementally buffers partial data,
parses fixed frame lengths, and accepts multiple complete responses in one read.
It never splits on byte `0x52` (`R`) because that value can legally occur inside
a source payload. Buffer size and unmatched-frame count are bounded. Timeout,
malformed input, rejection, EOF, or unexpected framing poisons and closes the
connection before another attempt.

## Source encoding

Bit 7 (`0x80`) means off/standby. Bit 6 (`0x40`) means inverse orientation.
Standby selection occupies the remaining source encoding. The table lists on,
normal-orientation values; add `0x40` for inverse orientation and `0x80` for off.

| Source | 20 min | 60 min | Never |
|---|---:|---:|---:|
| Wifi | `0x02` | `0x12` | `0x22` |
| Bluetooth | `0x09` | `0x19` | `0x29` |
| Aux | `0x0a` | `0x1a` | `0x2a` |
| Opt | `0x0b` | `0x1b` | `0x2b` |

For example, Opt/never/normal direct wake is `53 30 81 2b`; inverse orientation
is `53 30 81 6b`. Power off adds `0x80` to the corresponding on value.

Response-only Bluetooth-paired variants are decoded but must not be sent. USB is
not exposed for LSX generation 1. A historical response-value-48 anomaly is
treated only as a compatibility decode rule and requires hardware confirmation.

## Volume and mute

Volume is an integer 0–100 in register `0x25`. Mute adds `0x80` to the current
volume; unmute clears it. The integration clamps commands to the configured
maximum before encoding. It never guesses the underlying volume for mute or
relative-step behavior: an unknown cached volume requires a volume GET first.

## Direct wake and verification

Wake is a source SET with bit 7 clear. The configured preferred source, standby,
and orientation are sufficient to construct it, so no state read precedes the
write. Verification is a later bounded source GET. A failed verification does not
erase the fact that the SET write was attempted.

## Known uncertainties

Real hardware validation is required for trailer semantics, split-frame behavior
on the speaker, unsolicited responses, firmware-specific standby behavior, the
response-48 anomaly, and all transport/DSP commands. These uncertainties block a
stable release claim but do not require speculative behavior in the MVP.

