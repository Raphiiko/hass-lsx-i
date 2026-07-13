# KEF LSX for Home Assistant

> **Personal-use project:** This is a vibecoded custom integration made to solve
> one specific problem with one first-generation KEF LSX setup: keeping control
> available when the legacy local protocol has a transient failure. It is not
> intended as a general release or supported product. Do not rely on it being
> thoroughly human-tested or maintained in the future.

KEF LSX is a HACS custom integration for **first-generation KEF LSX** speaker
pairs. It communicates directly with the speaker over the local legacy TCP
protocol on port 50001. It does not use a KEF account, the KEF cloud, or an
internet service.

This project is an early prerelease. Its protocol and failure behavior are tested
against a deterministic TCP speaker simulator, but real LSX hardware validation
has not yet been performed for this release. Do not publish or treat it as a
stable release until the hardware checklist in [releasing](docs/releasing.md) is
complete.

## Why this integration exists

Home Assistant's built-in `kef` platform uses the unmaintained `aiokef` client.
A single failed status exchange can mark the entity unavailable, after which Home
Assistant refuses ordinary media-player actions. The old wake path also reads
state before sending the power/source command, so a failed read can prevent wake
from being attempted.

This integration uses the distinct `kef_lsx` domain and does not replace or
monkeypatch Home Assistant Core. It keeps one serialized owner for speaker
communication, prioritizes controls over polls, retains last-known state through
short failures, and sends the configured wake/source command without a
prerequisite read.

> Never run the built-in `kef` platform and this integration against the same
> speaker. Two owners can overload or wedge the LSX control server. Follow the
> [migration guide](docs/migration.md) before opening the new config flow.

## Supported scope

| Capability | Prerelease implementation | Fake-server tested | Real LSX tested |
|---|---:|---:|---:|
| Power on with direct preferred-source SET | Yes | Yes | No |
| Power off | Yes | Yes | No |
| Source read/select: Wifi, Bluetooth, Aux, Opt | Yes | Yes | No |
| Absolute volume and configurable volume step | Yes | Yes | No |
| Mute/unmute | Yes | Yes | No |
| Transient-failure availability hysteresis | Yes | Yes | No |
| Communication diagnostics | Yes | Yes | No |
| Play/pause and track navigation | Deferred | No | No |
| DSP/EQ controls | Deferred | No | No |
| LS50 Wireless generation 1 | Not claimed | No | No |
| LSX II, LS50 Wireless II, LS60 and KEF Connect models | Not supported | No | No |

The newer KEF Connect/W2 speakers use a different HTTP API. Use an integration
designed for those models.

## Requirements

- First-generation KEF LSX with local TCP control available on port 50001.
- A fixed or reserved speaker address reachable from Home Assistant.
- Home Assistant 2026.7.2 or newer. Compatibility below this version is not
  claimed because it is not in the test matrix.
- Only one application/integration actively owning the legacy control channel.

## Installation

### HACS custom repository

1. In HACS, open the custom repositories dialog.
2. Add `https://github.com/Raphiiko/hass-lsx-i` with category **Integration**.
3. Install **KEF LSX**.
4. Restart Home Assistant only when HACS requests it.
5. Before configuring the integration, remove/stop the old built-in YAML `kef`
   platform as described in [migration](docs/migration.md).
6. Go to **Settings → Devices & services → Add integration**, search for
   **KEF LSX**, and enter the speaker host.

### Manual installation

Copy `custom_components/kef_lsx` into the same path beneath the Home Assistant
configuration directory, restart Home Assistant, then follow steps 5–6 above.
HACS is recommended because it manages file updates.

## Configuration and options

Setup is entirely through the UI. The config flow performs one short, read-only
source query; it never wakes or changes the speaker. Setup asks for a friendly
name plus the host and port (50001 by default). Duplicate endpoints are rejected.

The options flow contains only user-meaningful behavior:

- **Preferred wake source** — optional. When set, it overrides the cached source;
  otherwise the last known source is used, with `Opt` as the fallback if none is known.
- **Maximum volume** — caps volume commands; default 50%.
- **Volume step** — amount used by volume up/down; default 5%.
- **Inverse orientation** — swaps L/R speaker orientation in the encoded source.
- **Standby encoding** — 20 minutes, 60 minutes, or never; defaults to never.

Polling intervals, retry counts, socket timeouts, and availability thresholds are
safe internal policy and are intentionally not exposed as tuning knobs.

## Reliability behavior

The integration polls source/power every 30 seconds. It retains cached state and
remains controllable after short failures. Communication becomes stale after 45
seconds without a successful core-state read. Availability changes to unavailable
only after at least four primary failures **and** 120 seconds without channel
success. Any valid exchange restores availability immediately.

Controls are serialized ahead of routine polling. A poll is never allowed to
backlog, and DSP is not polled in the initial release. Direct wake sends the
configured source/power SET first and verifies asynchronously afterward.

Diagnostic entities expose communication status. Noisy last-success, failure
count, and last-command sensors are disabled by default and can be enabled from
the device page. Downloadable integration diagnostics redact host and identifiers.

## Migration and rollback

The current YAML platform has no config entry and cannot be safely left running
during validation of the new integration. Back up first, stop the old owner, then
configure this integration. Preserve `media_player.kef` by renaming the new entity
after the old registry entry is clear, or update all references atomically.

Two known automations in the target installation use turn-on, wait two seconds,
then select `Opt`; the new direct-wake default is compatible with that sequence.
See [migration](docs/migration.md) for the complete safety and rollback procedure.

## Troubleshooting and documentation

- [Architecture](docs/architecture.md)
- [Legacy protocol notes](docs/protocol.md)
- [Migration and rollback](docs/migration.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Release process](docs/releasing.md)

When reporting a problem, include the integration version, Home Assistant version,
speaker firmware if known, and redacted integration diagnostics. Do not publish
private addresses, webhook IDs, tokens, or raw Home Assistant configuration.

## Development

Python 3.14.2+ is required. The repository pins the Home Assistant test harness to
the live target generation.

```console
uv sync --python 3.14.2 --extra test --extra dev
uv run --frozen ruff format --check .
uv run --frozen ruff check .
uv run --frozen mypy
uv run --frozen pytest
```

CI also runs HACS validation and hassfest. The TCP reliability suite runs on
Linux and Windows.

## Licence and attribution

The project is distributed under the MIT licence. Reverse-engineered protocol
mappings are derived from the MIT-licensed `aiokef` project; required notices and
exact provenance are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
