# Architecture reviewer A: recommended implementation

Status: proposed for implementation approval, 2026-07-13

## Decision summary

- Domain and package: **`kef_lsx`**, display name **KEF LSX**.
- Baseline: **Home Assistant 2026.7.2**, Python 3.14.2+, `pytest-homeassistant-custom-component==0.13.346`.
- Scope: first-generation LSX on local TCP port 50001 only. No KEF cloud, modern KEF Connect API, or LS50W claim.
- Client: a small typed HA-independent client vendored in `custom_components/kef_lsx`; no PyPI dependency for the MVP.
- Framing: fixed-length incremental parser; never split a TCP read on `b"R"` and never assume one read is one frame.
- Ownership: one long-lived worker task per config entry is the sole owner of client, reader, writer, receive buffer, retries, and close.
- Scheduling: FIFO control `deque`, one coalesced poll slot, and one coalesced post-command verification slot. Controls always win.
- Polling: 30 seconds; one attempt only; source/state while off, source/state then volume/mute while on; no DSP in MVP.
- Retry: no retry inside the client. The worker may reset and retry an absolute/idempotent control once within one total budget. Never retry toggle/track semantics.
- Availability: stale/degraded after 45 seconds without success; unavailable only when **both** at least four consecutive communication operations have failed **and** 120 seconds have elapsed since success/start. Any successful exchange restores immediately.
- Wake: directly send the configured source/power SET, default source `Opt`; never pre-read. Verify asynchronously through the same worker.
- Identity: normalized host/port matching prevents duplicate entries; HA's stable `entry_id` identifies the device/entities because the protocol exposes no verified physical identifier.
- Translation authority: `translations/en.json`; add a mirrored `strings.json` only if the selected HACS/hassfest validator actually requires it.
- Diagnostics: one useful communication-status entity plus disabled-by-default diagnostic entities and redacted config-entry diagnostics.
- Release: prerelease only until real LSX hardware validates wake, source, volume, mute, and recovery.

## Runtime shape

```text
HA config entry / entities
          |
  KefLsxController interface
          |
 one worker: controls > verification > coalesced poll
          |
 typed LsxClient + incremental FrameParser
          |
      one TCP exchange at a time
```

The external seam is `KefLsxController`. Its interface is intentionally small:

- `async_start()`
- `async_control(ControlCommand) -> CommandResult`
- `async_request_poll() -> RuntimeSnapshot`
- `async_close()`
- immutable current `RuntimeSnapshot`

Callers do not open connections, choose retries, mutate cached state, or parse errors. The module is deep: command priority, poll coalescing, hysteresis, verification, cancellation, and result bookkeeping remain implementation details.

Store a runtime data object containing controller and coordinator in `ConfigEntry.runtime_data`. The coordinator supplies cadence and entity fan-out but does not own the socket. Expected communication failures return an updated health snapshot rather than raising `UpdateFailed`; otherwise one failed poll would recreate the old immediate-unavailability bug. Cancellation and programming errors still propagate.

## Protocol/client module

`protocol.py` owns pure command encoding, source/standby/orientation tables, immutable frame types, and `FrameParser.feed(bytes)`. It recognizes:

- three-byte SET ACK `52 11 ff`;
- five-byte GET frame `52 <register> 81 <value> <trailer>`;
- partial frames retained across reads; and
- multiple complete frames in one read, including payload byte `0x52`.

Bound buffer size and frame count. A malformed, oversized, or unmatched reply poisons the connection: raise a typed error and reset before another operation.

`client.py` owns connect, write/drain, bounded read, and close. It exposes typed device operations, not raw stream objects. Error hierarchy:

- `LsxError`
- `ConnectRefusedError`
- `ConnectTimeoutError`
- `ResponseTimeoutError`
- `ConnectionLostError`
- `MalformedResponseError`
- `CommandRejectedError`
- `ClientClosedError`

Do not add a transport interface: production and tests both use real TCP, so there is only one adapter at that seam. Pure parser tests and the deterministic TCP fake provide sufficient testability.

Use a batch-scoped connection: the worker may reuse one connection for the sequential exchanges in one poll/control operation, then closes it synchronously at the operation boundary. This avoids both a permanent connection that may exclude the KEF app and `aiokef`'s delayed one-second disconnect race. There is no disconnect timer.

Suggested budgets are connect 1.0 s, response 1.5 s, close 0.5 s, poll total 3.0 s, and idempotent control total 6.0 s across at most two attempts. One reset must complete before the second attempt. These values remain safely below the 30-second poll interval.

## Worker and command semantics

The worker owns:

- `deque[ControlRequest]` with bounded length;
- a boolean/coalesced poll future rather than a poll queue;
- at most one pending verification plan;
- one wake event; and
- all reader/writer/client state.

On each wake it drains controls FIFO before due verification, then performs at most one poll. Repeated coordinator refreshes share/coalesce into that poll. A poll never retries and never creates backlog. Verification cannot run ahead of a newly queued user control.

Only absolute commands may retry once: set source/power, absolute volume, and a mute/unmute value already resolved to an absolute byte. Volume step is converted once to a clamped absolute target and that target is retained across retry. Play/pause toggle and next/previous are non-idempotent and get one attempt; do not advertise them until real hardware proves their semantics.

Shutdown marks the worker closing, rejects new work, cancels verification timers, resolves/cancels queued futures, resets and closes the client under a bound, and awaits the worker. `CancelledError` propagates after cleanup.

## State, availability, and command result

Use immutable models:

- `SpeakerState`: power, source, volume, muted;
- `CommunicationHealth`: last success, consecutive failures, stale/degraded, available, last typed error;
- `CommandResult`: attempted time, command kind, attempted, acknowledged, verified, final error;
- `RuntimeSnapshot`: speaker state + health + last command.

On communication failure, retain the last `SpeakerState`. Increment failure count once per failed high-level operation, not once per low-level byte attempt. Validation errors and explicit command rejection do not imply transport unavailability.

Define `age` from last success, or worker start if no exchange has yet succeeded:

```text
stale = age >= 45 seconds
available = not (consecutive_failures >= 4 and age >= 120 seconds)
```

This gives an initially unreachable entry the same bounded control grace window. A successful GET or SET ACK sets last success, resets failures to zero, clears stale, and restores availability in the same published snapshot.

The media player uses health availability, not coordinator `last_update_success`. Diagnostic entities remain available while the integration is loaded so they still explain a speaker outage.

## Direct wake and controls

Options with ordinary-user value:

- preferred wake source, default `Opt`;
- maximum volume, default 0.5;
- volume step, default 0.05;
- inverse orientation, default false; and
- standby encoding, `None`/20/60, default `None`.

`turn_on` immediately records `attempted=True`, encodes preferred source + standby + orientation, and enqueues that SET. It does not call state/source first. ACK completes the synchronous action result. A single coalesced verification plan performs up to three source GETs at approximately 2, 7, and 15 seconds; explicit controls preempt them. Failed verification records `acknowledged=True, verified=False` and does not rewrite history as “not attempted.”

Power off uses cached source or the configured source to build an absolute off SET. Source selection is an absolute on/source SET. Polling while off never requests volume. Extensive DSP and DSP entities are deferred.

MVP advertised media features: turn on/off, source select/current source, volume set/up/down, and mute/unmute. Keep play/pause/next/previous out of feature flags until real LSX validation; this is allowed by the brief's “if verified” condition.

## Config flow, identity, diagnostics, and repairs

The user flow asks for host and optional port (default 50001), normalizes IP/hostname, and performs one bounded source GET. Use `cannot_connect`, `invalid_host`, and `already_configured` errors. Duplicate prevention compares normalized host + port against existing entry data, including reconfigure conflicts.

No audited command exposes a trustworthy serial or MAC. Do not pretend an IP address is physical identity. Leave `ConfigEntry.unique_id` unset and use HA's stable `entry_id` in device identifiers and entity unique ids. This deliberately interprets the request for stable config-entry identity as `entry_id`; inventing or endpoint-binding a physical unique id would make host changes unsafe. If later packet/hardware research exposes a serial, migrate identity explicitly.

Create one device per entry. Entity unique ids are `<entry_id>_media_player` and `<entry_id>_<diagnostic_key>`. The options update listener reloads the entry; unload closes the worker before dropping runtime data.

Expose `communication_status` (`healthy`/`degraded`/`unavailable`) as an enabled diagnostic-category sensor. Disable by default: last successful communication, consecutive failures, and last command result. Also place stale/degraded and last command summary in restrained media-player attributes.

Diagnostics include configuration, options, snapshot, queue depths, and typed last error, but redact host, endpoint-derived values, entry/unique/device identifiers, and raw wire data. Do not add a repairs module for ordinary outages; there is no actionable repair beyond troubleshooting. Migration conflict with the built-in YAML integration is documented, not automatically mutated.

Use `translations/en.json` as current custom-integration runtime authority. Because current official guidance says custom integrations should not use Core's build-time `strings.json`, omit it initially and add an exact mirror only if repository validators fail without it. This is a documented deviation from the literal repository checklist, chosen to follow HA 2026.7.2.

## Test seams and acceptance focus

- Pure seam: encoding and incremental parser tests over byte chunks.
- Wire seam: typed client against `tests/fake_lsx.py` on real `127.0.0.1` TCP.
- Runtime seam: worker priority, coalescing, hysteresis, timeout, retry, and cancellation against that same fake.
- HA seam: config entry setup/unload, state machine, service registry, diagnostics helper, and registries.
- Time seam: inject a monotonic clock into health calculation; production uses `loop.time`, tests use a controllable adapter.

The release-blocking integration transcript is: successful off/Opt poll; all attempts of one later poll dropped; cached state retained and available/degraded; HA `turn_on`; next received frame is Opt SET, never GET; ACK; bounded verification. Also require the full mandatory 1-18 test matrix from `testing-fake-speaker.md`.

## Implementation workstreams and file ownership

| Workstream | Owned files | Dependencies |
|---|---|---|
| A: protocol client | `errors.py`, `protocol.py`, `client.py`, `tests/test_protocol.py` | Uses B's fake for socket tests; no HA dependency. |
| B: fake server | `tests/fake_lsx.py`, `tests/test_fake_lsx.py`, `tests/conftest.py` | Protocol report only; land fixture interface early. |
| C: HA setup/config | `const.py`, `manifest.json`, `__init__.py`, `config_flow.py`, `translations/en.json`, conditional `strings.json`, `tests/test_config_flow.py`, `tests/test_init.py` | Runtime interface agreed; C may stub typing only until D lands. |
| D: runtime/entities | `models.py`, `runtime.py`, `coordinator.py`, `media_player.py`, `sensor.py`, `diagnostics.py`, `tests/test_runtime.py`, `tests/test_media_player.py`, `tests/test_diagnostics.py` | A client, B fake, C constants/setup contract. |
| E: packaging/docs | `hacs.json`, `README.md`, `THIRD_PARTY_NOTICES.md`, `docs/` outside research, CI workflows, tooling config | Architecture approved; final feature list from D. |

Use separate `codex/` branches/worktrees. No file has two owners. B owns shared test fixtures; E owns tool configuration; the lead resolves integration changes rather than allowing agents to edit another workstream's files. Integrate B and C scaffolding first, A next, D after A/C, while E proceeds in parallel and finalizes after D.

## Deviations and resolved disagreements

1. Use `kef_lsx`, not `kef_legacy`: support is intentionally LSX-gen1-only.
2. Vendor the client now; extraction to PyPI requires a second consumer or independent release need.
3. Use entry-id identity and normalized endpoint duplicate matching, not an IP-derived physical unique id.
4. Use HA 2026.7.2 translation rules; `strings.json` is conditional on real validator output.
5. No DSP or unverified transport feature flags in MVP.
6. Keep the worker long-lived but make TCP connections operation-scoped; this avoids both permanent exclusion of other controllers and delayed-disconnect races.
7. Do not raise expected poll failures through the coordinator. Health snapshots, not coordinator failure state, govern availability hysteresis.
8. Preserve `aiokef` MIT attribution for derived mappings in `THIRD_PARTY_NOTICES.md`; copy no incompatible competitor/Core implementation.
