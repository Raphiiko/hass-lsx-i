# Initial integration implementation plan

Status: approved for implementation on 2026-07-13.

This plan synthesizes the six research reports and both architecture reviews in
`docs/research/`. It is the implementation and review specification for the
initial `kef_lsx` prerelease.

## Product and compatibility decisions

- Domain: `kef_lsx`; name: KEF LSX.
- Scope: first-generation LSX only, local legacy TCP (default port 50001). No
  KEF cloud, KEF Connect/W2 API, or LS50W support claim.
- Baseline: Home Assistant 2026.7.2, Python 3.14.2+, and
  `pytest-homeassistant-custom-component==0.13.346`.
- Distribution: HACS custom integration with a vendored, HA-independent typed
  client. `manifest.json` has no third-party runtime requirements.
- Release state: `0.1.0b3` prerelease. No stable tag or hardware-validation
  claim before an approved real-speaker test.
- DSP and transport controls (play/pause/track) are deferred. The MVP exposes
  power, source, absolute/step volume, and mute only.

## Protocol contract

`protocol.py` contains independently implemented encoders, source/standby/
orientation mappings derived from `aiokef`, immutable response types, and an
incremental bounded parser. `THIRD_PARTY_NOTICES.md` preserves the upstream MIT
notice and exact provenance.

Requests are fixed three-byte GET and four-byte SET messages. The parser accepts
fixed three-byte SET status frames and five-byte GET frames, retains partial
suffixes, and handles multiple complete frames in one read. It never splits on
the byte `R`, because `0x52` is also a valid payload. While awaiting one request,
it may skip/cache a bounded number of structurally valid unrelated GET frames;
invalid shape, incomplete EOF, overflow, too many unmatched frames, or timeout
raises a typed error and poisons/resets the connection.

Typed public failures distinguish connection refusal, connect timeout, response
timeout, connection loss/reset/EOF, malformed response, rejected command, busy
queue, ambiguous non-idempotent result, and closed runtime. Cancellation is not
translated into a communication failure.

One protocol exchange has an absolute I/O budget covering connect, write/drain,
and read/framing. Initial defaults are 2.0 seconds to connect, 1.25 seconds for
response, 0.25 seconds for bounded close, and a 0.20-second listener-recycle delay
before another operation-scoped connection. A poll takes at most about 3.7 seconds;
an idempotent control gets two attempts with a 0.30-second retry gap within its
eight-second absolute deadline. Retry is owned only by the worker and always follows
a completed reset/close.

## Connection ownership and scheduler

Exactly one long-lived controller worker exists per config entry. Only that task
may create or mutate the client, reader, writer, parser buffer, retry state, or
close lifecycle. Producers never receive a raw client reference.

The scheduler owns:

- a bounded FIFO control deque (maximum 16, non-blocking submission, eight-second
  absolute job deadlines);
- one coalesced source-poll intent with protected waiter handling;
- one coalesced post-command verification generation;
- at most one optional volume-maintenance intent; and
- one wake event and explicit lifecycle (`NEW`, `RUNNING`, `STOPPING`, `CLOSED`).

Selection is controls, due verification, core poll, then optional maintenance.
One selected work unit is exactly one request/reply exchange, followed by
scheduler reselection. A source GET can satisfy both poll and verification.
Coordinator publication after a successful command or verification resets the
next routine poll. There is no poll retry, poll queue, reconnect loop, DSP burst,
or disconnect timer.

The MVP uses an operation-scoped TCP connection, where an operation is exactly
one exchange. Bounded close finishes before another connection is opened; the
fake server asserts at most one connection/exchange. Hardware validation will
measure whether connection churn is problematic. If deterministic reuse is later
needed, it remains a worker-only policy change and cannot weaken serialization.

Absolute/idempotent commands may retry once: source/power, absolute volume, and
explicit mute/unmute once their absolute byte is known. Non-idempotent toggles or
track actions are never retried or advertised. Consecutive queued absolute-volume
commands may coalesce to the newest target only when none has started and no
source/power boundary is crossed. Cancelled/expired jobs are skipped.

If volume is unknown, mute/unmute or volume-step first queues a high-priority
volume GET continuation under the original deadline; it never guesses a volume.
The worker reselects after the GET while preserving continuation order.

Shared poll waiters cannot cancel the worker-owned future. All completion paths
check whether a waiter is already done. Every accepted future reaches result,
exception, or cancellation exactly once.

## State, health, and availability

Snapshots are immutable and contain speaker state, communication health, and the
last command record. Speaker state is never erased by a failed exchange.

Health keeps separate `last_channel_success` and `last_state_success` monotonic
times. A valid GET or SET ACK proves channel success; only a core source GET makes
cached state fresh. The primary availability failure count increments once per
failed core poll or failed explicit high-level control, never per transport retry.
Verification/optional-volume failures set degraded metadata but cannot manufacture
the primary threshold. Any valid expected response immediately resets the primary
counter and restores availability.

Defaults:

```text
stale = last core-state success is at least 45 seconds old
degraded = primary failure exists or verification/maintenance is stale
available = not (primary failures >= 4 and channel-success age >= 120 seconds)
```

The controller recomputes and publishes the threshold on every 30-second tick.
Expected first runtime poll failures are soft: an already configured entry still
forwards platforms, publishes unknown/degraded state, and installs an available
media player during the 120-second startup grace. Only invalid entry schema or a
proven incompatible protocol prevents setup. Config-flow creation/reconfigure
still requires one bounded, read-only successful source query.

The coordinator is cadence and fan-out only. Expected communication failures
return health snapshots rather than `UpdateFailed`; entity availability comes
from health. One controller snapshot publication callback feeds
`coordinator.async_set_updated_data()` outside transport mutation. Diagnostic
entities remain available while the integration is loaded so they can explain
an outage.

## Direct wake and command accounting

`turn_on` encodes and sends a source SET directly, including configured standby/
orientation. No GET precedes it. An optional preferred source overrides the cached
last-known source; `Opt` is the safe fallback when neither is available.

Command accounting distinguishes submitted, `write_attempted` (set immediately
before `writer.write`), acknowledged (exact expected ACK), and verified. Queue
rejection/cancellation before write does not claim an attempt. Response or
verification failure cannot erase `write_attempted=True`.

Verification is asynchronous and coalesced through the worker, with bounded
attempts and no blocking sleep inside a service call. New state-changing commands
supersede stale generations and always take priority.

## Home Assistant surface

Config data contains normalized host and port; the config-entry title is the
friendly device name. Options contain an optional preferred wake source, maximum
volume (0.5), volume step (0.05), inverse
orientation, and standby encoding (`None` default). Poll/retry/timeout/hysteresis
knobs are intentionally not user-facing.

The legacy protocol exposes no confirmed immutable serial/MAC query. The flow
therefore assigns a random installation UUID as the `ConfigEntry.unique_id` and
never misuses IP/hostname as identity. It remains stable across reconfigure and
drives entity/device identifiers; removing and re-adding the same physical
speaker creates a new identity. Duplicate prevention separately matches the
normalized host+port. This limitation is preferable to fabricating hardware
identity. Reconfigure checks duplicates before I/O and skips probing an
unchanged endpoint.

The primary `MediaPlayerEntity` exposes standard feature flags only for the MVP.
An enabled diagnostic communication-status sensor reports healthy/degraded/
unavailable. Last success, consecutive failures, and last command result are
diagnostic and disabled by default. Media-player attributes remain restrained.

Diagnostics use HA redaction helpers and redact host, endpoint-derived values,
config/device/entity identifiers, and raw wire data. They retain typed error
categories, health counters/timestamps, and scheduler state. Ordinary timeouts do
not create repair issues.

`translations/en.json` is runtime translation authority for HA 2026.7.2. A
redundant `strings.json` is added only if the actual validator requires it and
contains no Core build placeholders.

## Shutdown, reload, and migration safety

Unload is two-phase. It first enters `STOPPING`, rejects submissions, cancels
verification scheduling, wakes the worker, and lets the sole owner close/reset
and terminally resolve queues. It awaits graceful exit under a short outer
deadline. Only then may it force-cancel, detach references, and run separately
bounded close cleanup. No retry/open is allowed after `STOPPING`. Reload awaits
old-controller completion before setup constructs a new owner.

Config-flow validation can itself collide with the built-in YAML poller. The
migration guide therefore requires: install files only, back up, stop/remove the
old YAML owner in an approved maintenance window, confirm it no longer polls,
then run the new config flow. Never run both integrations against the speaker.
No live installation, reload, speaker action, automation edit, or HA restart is
authorized by repository implementation.

## Test-driven seams and release-blocking scenarios

The pre-agreed public test seams are:

1. pure encoding/parser behavior;
2. typed client behavior over a deterministic real TCP fake;
3. controller scheduling/health/cancellation over that fake;
4. Home Assistant config-entry, service, state, registry, and unload behavior;
5. diagnostics output through HA's diagnostics helper.

Tests use a deterministic `127.0.0.1` fake LSX with scripted valid, partial,
combined, delayed, dropped, malformed, rejected, graceful-close, abort/reset,
refusal, and recovery actions. It records exact transcripts, connection and
exchange concurrency, and cleanup errors. Timing uses gates/fake clocks except
where real socket timeout bounds are the behavior under test.

All mandatory tests 1-18 from the project brief and
`docs/research/testing-fake-speaker.md` are release-blocking. Additional blockers
from the adversarial review are: soft failed first poll still installs an
available entity; a control is selected after at most one in-flight exchange;
poll and verification coalesce; stale jobs expire; one cancelled shared poll
waiter cannot kill another; actual-write metadata is accurate; unknown-volume
mute/step never guesses; and unload during connect/read/retry/verification leaves
zero workers, connections, timers, or unresolved futures.

## Implementation workstreams

Strict shared-worktree ownership is used because the environment limits child
agents. Agents do not edit outside their rows; the lead inspects and integrates
every diff.

| Workstream | Owned files | Dependencies |
|---|---|---|
| A: protocol/client | `custom_components/kef_lsx/errors.py`, `protocol.py`, `client.py`, `tests/test_protocol.py`, `tests/test_client.py` | B fake interface |
| B: fake server | `tests/fake_lsx.py`, `tests/test_fake_lsx.py`, `tests/conftest.py` | protocol report |
| C: HA setup/config | `const.py`, `manifest.json`, `__init__.py`, `config_flow.py`, `translations/en.json`, conditional `strings.json`, `tests/test_config_flow.py`, `tests/test_init.py` | controller public contract |
| D: runtime/entities | `models.py`, `runtime.py`, `coordinator.py`, `entity.py`, `media_player.py`, `sensor.py`, `diagnostics.py`, runtime/entity/diagnostics tests | A, B, C |
| E: packaging/docs | `pyproject.toml`, `hacs.json`, `.gitignore`, `.github/workflows/*`, `README.md`, `THIRD_PARTY_NOTICES.md`, non-research docs | approved plan; final feature list from D |

Landing order is B+C+initial E tooling, then A, then D, then E finalization.
Relevant focused tests run after each slice; the full suite, lint/format, typing,
HACS validation, hassfest, import/setup/unload, and sensitive-data scan run before
review and again after fixes.

## Architecture disagreements resolved

- Worker versus lock: worker, because priority, coalescing, deadlines, and
  shutdown cannot be represented safely by one fair lock.
- Persistent versus per-call connection: worker-owned, one-exchange-scoped
  connection for MVP; no timer. Hardware data may later justify deterministic
  reuse without changing ownership.
- Startup failure: soft degraded setup for existing entries, not
  `ConfigEntryNotReady`; creation still validates read-only reachability.
- Availability count: core polls and explicit commands only, with separate state
  freshness and channel controllability timestamps.
- Coordinator failure: expected device errors are data, not immediate
  `last_update_success=False`.
- Identity: endpoint duplicate matching plus stable HA entry identity; no
  IP-derived physical unique ID.
- Translations: current custom-integration runtime rules take precedence;
  validator output decides whether a redundant `strings.json` ships.
- Scope: no DSP or unverified transport controls in the MVP.
