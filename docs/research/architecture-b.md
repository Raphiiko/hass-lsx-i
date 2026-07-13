# Architecture reviewer B: adversarial review

Status: changes required before implementation, 2026-07-13. This reviews
`architecture-a.md` against the protocol and reliability reports. Severity means release risk if the
finding is left ambiguous or implemented literally.

## Findings

### 1. Initial setup can recreate the exact control outage

**Severity: critical.**

**Failure sequence:** HA restarts during a transient LSX timeout. If setup uses
`async_config_entry_first_refresh()` or translates the failed first poll into
`ConfigEntryNotReady`, no entity is installed. The proposed 120-second startup
availability grace then never exists, and the user cannot invoke direct wake.
The same can happen after an options-triggered reload.

**Required mitigation:** a previously validated entry must start the controller,
publish an `unknown` state with healthy lifecycle but degraded communication,
forward its platforms, and keep the media player available during the documented
grace. Expected connect/response failures in the first runtime poll are soft
snapshot updates, not setup failure. Reserve `ConfigEntryError` for invalid entry
schema or proven incompatible protocol. Config-flow validation remains the
one-time reachability check when creating/reconfiguring an entry.

### 2. “Batch-scoped connection” can hide a monopolizing multi-read poll

**Severity: high.**

**Failure sequence:** an on-state poll opens a batch, performs source, volume,
and later another maintenance read before returning to the scheduler. A control
arriving just after the first GET cannot preempt the remaining reads. Future
maintenance additions silently lengthen the batch, and a nominally prioritized
command waits behind polling.

**Required mitigation:** define one scheduler work unit as exactly one
request/reply exchange. After the source GET, publish/reselect; queue volume as a
single optional maintenance unit only if still on and stale. Controls must be
checked between every exchange. If the TCP connection is retained across two
units, only the worker may retain it and scheduler reselection must still occur;
“operation” must never mean a high-level refresh sequence.

### 3. Persistent versus operation-scoped TCP ownership is not fully resolved

**Severity: high.**

**Failure sequence:** closing after every individual GET creates two connections
per on-state poll plus up to three more for wake verification. Conversely,
retaining a connection indefinitely may exclude the KEF app and admits delayed
frames into a later exchange. Either behavior can stress the fragile controller
if implemented without an explicit policy.

**Required mitigation:** keep A's operation-scoped policy for the MVP, but make
an operation one exchange and always await bounded close before the next open.
There must never be two accepted connections. Do not add an idle-disconnect task.
Measure connection count in fake-server tests and during hardware validation. If
real hardware shows connection churn is harmful, change only the worker-owned
connection policy to deterministic reuse; do not weaken serialization or add an
independent timer.

### 4. Poll and verification slots can duplicate the same source GET

**Severity: high.**

**Failure sequence:** wake is acknowledged near a coordinator tick. The worker
runs a due verification GET and then the already-pending routine source GET.
Repeated command/tick alignment creates avoidable connection bursts precisely
while the speaker is waking.

**Required mitigation:** one successful source GET must satisfy both a pending
core poll and the current verification generation. Coalesce these intents before
selection, and call `coordinator.async_set_updated_data()` after successful
controls/verification so HA resets the next poll deadline. A superseded
verification generation must never issue traffic.

### 5. A bounded queue can still replay a harmful command burst

**Severity: high.**

**Failure sequence:** a volume slider or restarting automation fills the control
deque while one 2.5-second exchange is failing. Once the socket recovers, the
worker sends every stale volume/source command sequentially. Serialization avoids
overlap but not overload or obsolete side effects.

**Required mitigation:** set a concrete capacity (16 is an upper bound, not a
target), use non-blocking submission with a typed busy error, and give every
control an absolute deadline (recommended eight seconds). Skip cancelled or
expired jobs. Coalesce only consecutive, not-yet-started absolute volume commands
to the latest value; never coalesce across source/power commands or reorder FIFO
side effects. Test both saturation and expiration.

### 6. Shared/coalesced futures have cancellation hazards

**Severity: high.**

**Failure sequence:** two callers share a coalesced poll future. Cancellation of
one await propagates to that shared future; the worker later attempts
`set_result`, raises `InvalidStateError`, and dies. Alternatively, unload leaves
one waiter unresolved.

**Required mitigation:** the worker owns job futures. Awaiters must not be able
to cancel shared internal state (use a separate waiter or `asyncio.shield` only
around the await, not socket cleanup). Before completion, check `future.done()`.
Closing must terminally resolve every control, poll, and verification waiter.
The fake suite must cancel one coalesced waiter while another still completes.

### 7. Availability failures are counted too broadly

**Severity: high.**

**Failure sequence:** a successful wake ACK proves the control path is alive,
but three rapid verification reads and one optional volume read fail. If all
“communication operations” increment the main counter, the integration already
has four failures; it can flip unavailable as soon as age reaches 120 seconds,
even though optional work manufactured the threshold.

**Required mitigation:** maintain a primary availability counter for failed core
state polls and failed explicit control operations only. Verification and
optional-volume failures set degraded/verification metadata but do not increment
the primary counter more than once per coordinator interval or superseded command
generation. Any expected GET response or SET ACK resets the primary counter.
Define separately:

```text
degraded = primary failure exists or verification/maintenance is stale
stale = last successful core-state read is at least 45 seconds old
available = not (primary failures >= 4 and channel-success age >= 120 seconds)
```

Use separate last-channel-success and last-state-success timestamps; a SET ACK
proves controllability but does not make cached state fresh.

### 8. The 4-and-120 transition needs an update trigger and precise counting

**Severity: medium.**

**Failure sequence:** four failures occur rapidly, but no snapshot is published
at the exact 120-second boundary. Availability stays true until an unrelated
event. Or a two-attempt command increments twice and reaches the threshold early.

**Required mitigation:** count once after the complete high-level operation, not
per transport attempt. Recompute health on every 30-second coordinator tick, even
when a poll is coalesced or skipped, and publish the boundary transition. Tests
must prove failures 1-3 remain available, four failures before 120 seconds remain
available, and the first tick satisfying both conditions becomes unavailable.

### 9. Command-attempt bookkeeping currently overclaims writes

**Severity: high.**

**Failure sequence:** `turn_on` records `attempted=True` before enqueue, then the
queue is full, the caller is cancelled, or unload begins. Diagnostics claim the
wake SET was attempted although no byte reached `writer.write`.

**Required mitigation:** record `submitted` at API acceptance; set
`write_attempted` only immediately before `writer.write`; set `acknowledged` only
after the exact expected ACK. Preserve `write_attempted=True` through response
timeout and verification failure. This still meets the requirement that failed
verification must not erase the attempted command, without inventing an attempt.

### 10. Mute and volume-step lack a safe unknown/stale-volume path

**Severity: high.**

**Failure sequence:** LSX mute is the mute bit plus a volume value. After startup
or a long stale period there may be no trustworthy cached volume. Encoding mute
or step from `None`, a default, or a stale value can change volume unexpectedly.

**Required mitigation:** when no cached volume exists, enqueue one high-priority
volume GET followed by an absolute SET continuation under the original control's
deadline. Re-enter scheduler selection between exchanges, but keep later
same-priority controls FIFO behind the continuation. If the GET fails, report a
typed command failure and send no guessed SET. Retain a single absolute target
across any allowed retry. Add fake-server tests for unknown volume and for a
dropped ACK proving an absolute value is not applied twice semantically.

### 11. Combined/unmatched response behavior is contradictory

**Severity: critical.**

**Failure sequence:** A says an unmatched reply poisons the connection, while the
acceptance seam requires a combined unrelated valid frame followed by the
requested frame. A parser that aborts on the first unrelated frame fails valid
combined traffic; a parser that accepts the first `R` can misframe source value
`0x52` or let a delayed response satisfy the next command.

**Required mitigation:** parse by fixed shape, never by splitting on `R`:
three-byte `R <status> ff` status frames and five-byte
`R <register> 81 <value> <opaque-trailer>` GET frames. Examine a bounded number
of complete recognized frames while waiting for the exact request/register;
ignore or cache a recognized unrelated GET only within that exchange. Poison and
reset on invalid shape, buffer overflow, too many unmatched frames, timeout, or
EOF with an incomplete frame. Operation-scoped close discards all residual data,
so no frame crosses an exchange boundary. Treat a non-`0x11` SET status as
rejection only if this status-frame shape is confirmed by tests/evidence;
otherwise classify it as unexpected/malformed rather than inventing protocol.

### 12. Cancellation-first unload can wedge cleanup

**Severity: critical.**

**Failure sequence:** unload cancels the worker while it is inside read/drain.
Its `finally` enters `wait_closed()` in an already-cancelled task, which is
immediately cancelled again. Writer references survive, a shared future is left
pending, or a retry opens a new connection during reload.

**Required mitigation:** use two-phase shutdown: atomically enter `STOPPING` and
reject submissions; cancel verification scheduling; wake the worker; let the
sole owner reset/close and terminally resolve queues; await that graceful exit
under a short outer deadline. Only then force-cancel, detach references, call
`writer.close()`, and retain/await any shielded cleanup task under its own bound.
No retry is permitted after `STOPPING`. `async_unload_entry` must finish the old
controller before a reload constructs the new one. Test unload during connect,
read, retry settle, and verification.

### 13. Config-flow probing conflicts with safe migration

**Severity: high.**

**Failure sequence:** the user starts the new flow while Core's YAML `kef`
platform still polls the same speaker. The flow's source GET creates a second
connection and can reproduce the fragile-server failure before the integration
is even configured. A transiently sleeping/refusing speaker also prevents adding
the integration whose purpose includes direct wake.

**Required mitigation:** migration instructions must make the order explicit:
install files only; back up; stop/remove the old YAML owner using the approved
maintenance procedure; confirm it no longer polls; only then run the config
flow. The flow must be read-only and bounded and must not offer a “wake to
validate” fallback. Reconfigure should skip probing an unchanged endpoint and
must reject endpoint conflicts before I/O. Real deployment remains behind user
approval. Document that first-time setup requires a responsive control port;
runtime restart does not, per finding 1.

### 14. Coordinator publication and reload ownership need one explicit seam

**Severity: medium.**

**Failure sequence:** entity service methods publish command snapshots directly,
while background verification mutates controller state without notifying the
coordinator. Diagnostic entities lag. An options listener starts a new runtime
before the old worker has closed, briefly creating two owners.

**Required mitigation:** controller commits one immutable snapshot, then invokes
one registered publication callback outside transport mutation; the coordinator
is the only HA fan-out adapter and uses `async_set_updated_data`. Entity methods
do not mutate coordinator data independently. Register one options listener with
the config entry and implement reload as unload-to-completion followed by setup.
Changing options must not itself send a speaker command.

## Decisions from Architecture A that survive

- `kef_lsx`, LSX-gen1-only scope, HA 2026.7.2/Python 3.14 baseline.
- A small vendored, HA-independent typed client with MIT attribution and no
  runtime dependency remains the lowest-risk dependency design.
- One long-lived worker as the sole reader/writer/parser owner is mandatory; a
  simple shared lock is insufficient for priority, coalescing, and shutdown.
- Controls before verification before polling survives, once reselection occurs
  after every single exchange and jobs have deadlines/cancellation rules.
- One poll attempt, at most two attempts for absolute idempotent controls, reset
  before retry, and no retry for toggle/track semantics survive.
- Direct preferred-source wake without a pre-read survives unchanged. Default
  `Opt`, no DSP, and no unverified transport feature flags are appropriate.
- Soft expected coordinator failures plus explicit health availability survive;
  the refined counters/timestamps above are required.
- Normalized endpoint duplicate prevention plus `entry_id` entity identity is
  the least dishonest current choice. It does not create physical identity and
  must remain documented as such; never hash the IP or reuse the legacy
  hardware-derived identifier without a verified protocol source.
- `translations/en.json` is the runtime authority. A redundant `strings.json`
  remains conditional on actual validator behavior and must contain no Core-only
  placeholders.
- Diagnostic entities, redaction, no routine repairs, prerelease-only status,
  and the deterministic real-TCP fake-server seams survive.

## Approval condition

Architecture A is implementable after findings 1-14 are incorporated into the
runtime contracts and tests. The release-blocking checks are: first-poll failure
still installs a controllable entity; control is selected after one in-flight
exchange; no duplicate verification/poll GET; attempt metadata reflects actual
write; combined/delayed frames cannot cross exchange boundaries; and unload/reload
leaves zero workers, connections, timers, or unresolved futures.
