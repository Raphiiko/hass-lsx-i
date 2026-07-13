# Architecture

## Design objective

The LSX legacy control server is fragile: overlapping exchanges and long nested
retries can make it stop responding. The integration is therefore organized
around one invariant: **one task owns all communication with one speaker**.

## Module boundaries

| Module | Responsibility |
|---|---|
| `protocol.py` | Pure request encoders, response types, source mappings, bounded incremental framing. |
| `errors.py` | Typed transport, protocol, queue, and lifecycle errors. |
| `client.py` | One bounded TCP exchange and deterministic close; no HA imports or retries. |
| `runtime.py` | Single-owner worker, control priority, retry/reset policy, state and health publication. |
| `models.py` | Immutable cached speaker, health, and command-result snapshots. |
| `coordinator.py` | 30-second cadence and Home Assistant listener fan-out only. |
| Entity modules | Memory-only Home Assistant properties and service-to-worker submission. |
| `diagnostics.py` | Redacted runtime/config diagnostics. |

The vendored protocol/client layer deliberately has no Home Assistant imports so
it can be extracted later if a second consumer or Core contribution justifies a
separate package. The initial HACS release has no runtime PyPI dependency.

## Scheduling and connection lifecycle

Each config entry has a bounded FIFO control queue (capacity 16), one coalesced
poll intent, one coalesced verification generation, and at most one optional
volume-maintenance intent. Selection order is controls, verification, source
poll, then maintenance. Every selected unit performs one exchange and returns to
selection, so a user command waits behind at most the exchange already in flight
plus earlier controls.

The MVP opens one operation-scoped TCP connection per exchange and completes a
bounded close before another connection is opened. There is no idle disconnect
task, parallel connection attempt, free-running reconnect loop, or poll retry.
The worker owns the only retry layer: idempotent controls may retry once after a
full reset; routine polls do not retry.

Cancelled or expired queued work is skipped. Once unload begins, submissions are
rejected, verification scheduling stops, queued futures are resolved, and the
worker closes the transport before the runtime is discarded.

## State and availability

Cached speaker state is immutable and is never erased by a failed exchange.
Channel success and fresh core-state success are tracked separately:

```text
stale = core-state age >= 45 seconds
degraded = a primary failure exists or maintenance/verification is stale
available = not (primary failures >= 4 and channel-success age >= 120 seconds)
```

Primary failures are failed core polls or failed explicit high-level controls,
not individual retry attempts. Any valid expected response immediately restores
channel availability; a source GET refreshes state freshness. Expected device
errors are snapshot data, not an immediate coordinator `UpdateFailed`.

An existing config entry still installs an available, degraded entity during a
120-second startup grace if its first poll fails. New setup and reconfigure flows
must first complete one bounded, read-only source query.

## Command semantics

Power and source share register `0x30`. `turn_on` directly sends the configured
preferred source, cached last-known source, or `Opt` fallback without a prerequisite
GET. `turn_off` uses the same safe source selection. SET acknowledgement records channel success and
the command is verified asynchronously through the same worker.

Volume writes are absolute. Maximum volume is enforced before bytes are queued.
Mute and volume-step preserve the absolute volume; if it is unknown, the worker
performs a prioritized volume read rather than guessing.

Play/pause, track navigation, and DSP are not advertised in the MVP because they
have not been verified on target hardware.

## Home Assistant integration

Domain `kef_lsx` avoids overriding Core's built-in `kef`. Typed runtime data is
stored on the config entry. A coordinator provides cadence and fan-out, while the
worker remains the sole serialization boundary. Entities expose only cached
properties and set `PARALLEL_UPDATES = 0` because the integration owns request
parallelism.

The protocol has no confirmed immutable identity query. The integration rejects
duplicate normalized host/port entries but does not misuse an IP address as a
physical config-entry unique ID. Entity/device identifiers derive from Home
Assistant's stable config-entry ID. Reconfigure validates collision and endpoint
reachability.

`translations/en.json` is the runtime localization source for custom integrations
on Home Assistant 2026.7.2. Diagnostics redact endpoint and registry identifiers.
Ordinary communication outages do not create repair issues because they do not
offer a concrete user action beyond the troubleshooting guidance.

## Deliberately deferred scope

- DSP/EQ entities and polling;
- play/pause and track controls;
- LS50 Wireless support claims;
- discovery and MAC/serial identity heuristics;
- modern KEF Connect HTTP devices;
- extraction to a second PyPI package.

These can be reconsidered only with protocol evidence, fake-server coverage, and
real-hardware validation without weakening the one-owner invariant.
