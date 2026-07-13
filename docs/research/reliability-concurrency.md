# Reliability and concurrency design

## Scope and recommendation

This report independently designs the runtime for one KEF LSX generation-1
speaker. Protocol byte values and model capability claims belong in the protocol
audit; this report treats one request/reply as the smallest transport exchange.

Use **one long-lived worker task per config entry** as the only owner of the TCP
reader, writer, receive buffer, connection state, and retry/reset logic. Producers
(Home Assistant service calls, the coordinator poll, and post-command
verification timers) submit work; they never call the protocol client directly.
The worker executes exactly one exchange at a time.

This is preferable to putting an `asyncio.Lock` around a shared client. A lock
does provide exclusive access and Python documents lock acquisition as fair/FIFO,
but it does not express control-versus-poll priority, coalesce redundant polls,
bound queued work, or give shutdown one place to reject/drain callers. A small
worker adds those properties without introducing a general-purpose framework.
[Python's lock documentation](https://docs.python.org/3/library/asyncio-sync.html#asyncio.Lock)
is the primary source for the lock guarantees.

## Explicit invariants

These are implementation requirements, not aspirations:

1. Exactly one task owns and mutates `StreamReader`, `StreamWriter`, the receive
   buffer, and connection state for a speaker.
2. At most one connection attempt and at most one request awaiting a response
   exist per speaker.
3. Each queued work unit performs at most one protocol request/reply exchange.
   Multi-field refreshes are split into work units so controls can run between
   them.
4. A routine poll has no in-place retry. There can be at most one pending routine
   poll, so a slow/failing speaker cannot create a poll backlog.
5. Explicit controls are FIFO relative to one another and are selected before
   pending verification, DSP, or routine-poll work. A control can wait only for
   the exchange already in flight plus earlier accepted controls; repeated polls
   can never get ahead of it.
6. The queue is bounded and submission never waits while holding integration
   state. Queue saturation returns a typed busy error; it cannot deadlock HA.
7. Every failed or cancelled in-flight exchange resets the connection and clears
   the receive buffer before any retry or next exchange.
8. A retry has one owner and one budget. The protocol exchange does not retry
   internally when the scheduler is already retrying.
9. An operation that may have reached the speaker is retried only when its
   semantics are idempotent. Ambiguous relative/toggle operations are not
   repeated.
10. Every accepted job future reaches exactly one terminal state: result,
    exception, or cancellation. The worker never calls `set_result` or
    `set_exception` on an already-done future.
11. Last-known media state is immutable across failed exchanges. A failure
    changes health metadata, not the cached state value.
12. Any valid expected protocol response immediately records communication
    success, clears consecutive failures, and restores availability. A write
    without a valid reply records an attempted command, not a confirmed success.
13. No synchronous socket I/O, `time.sleep`, blocking file I/O, or unbounded
    parsing runs on HA's event loop.
14. Unload is idempotent. After shutdown starts, no new job is accepted; timers
    are cancelled, the worker is cancelled and awaited, queued callers are
    failed, and the writer is closed with a bounded wait.

## Runtime structure

### Minimal scheduler

The scheduler needs only:

- a bounded `deque[ControlJob]`, recommended capacity 16;
- one coalesced pending verification record (generation, attempt number, due
  time), not a queue of verification reads;
- one `poll_pending` flag/future;
- at most one pending optional-maintenance/DSP item;
- an `asyncio.Event` to wake the worker;
- a strong reference to the worker task and tracked timer cancellation handles;
- immutable snapshot/health dataclasses published after each job.

The worker selection order is:

1. oldest non-expired control;
2. due post-command verification;
3. the coalesced core poll;
4. one optional volume/DSP maintenance exchange.

After *every exchange*, the worker returns to selection; a logical refresh must
not hold the connection through state, volume, and multiple DSP reads. Therefore
a command submitted during a volume/DSP refresh runs after at most the currently
in-flight exchange.

Routine poll starvation during an infinite command stream is intentional and
safe: successful controls already prove communication, update relevant cached
fields, and can move the next poll 30 seconds out. Once the finite command burst
ends, the single pending poll runs immediately. Do not insert a poll between a
`turn_on` and immediately queued `select_source`; preserving explicit command
order is more valuable than refreshing while the user is actively controlling
the speaker.

Python's `asyncio.Queue` is also viable, and its documented `maxsize` provides
backpressure, but a `PriorityQueue` alone permits unlimited low-priority poll
duplicates and starvation by a continuous higher priority. The small set of
coalesced slots above makes those states unrepresentable. If `asyncio.Queue` is
used for controls, use `put_nowait`, not an indefinitely awaited `put`.
[Python queue documentation](https://docs.python.org/3/library/asyncio-queue.html)
documents `maxsize`, `put_nowait`, and priority ordering.

### Submission and abandoned callers

Each control gets a monotonic sequence, a monotonic absolute deadline, retry
classification, and a result future. Submission while stopping/closed fails
immediately. Submission when 16 controls are already queued fails with a typed
`QueueBusyError`; it must not wait for space.

If a service caller is cancelled before the worker selects its job, its future is
cancelled and the worker skips the job. If cancellation arrives after the request
was written, the worker finishes/reset-cleans the exchange because the remote
side effect may already exist, but discards the result when the future is already
done. Integration unload is different: it cancels the worker itself, resets the
socket, and performs no further queued commands.

The worker task and any verification task/timer must be strongly referenced.
Python explicitly warns that the event loop retains only weak references to
tasks, and recommends retaining background-task references.
[Python task documentation](https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task)

### Home Assistant coordinator boundary

Use a `DataUpdateCoordinator` for the 30-second tick and entity notification, but
make the scheduler the serialization authority. The coordinator submits one
coalesced core-state poll and awaits that future. `PARALLEL_UPDATES = 0` is
appropriate because the integration explicitly controls all platform request
parallelism; Home Assistant documents that zero means the integration owns this
limit.
[Home Assistant fetching-data guidance](https://developers.home-assistant.io/docs/integration_fetching_data/)

Expected typed communication failures are health data, not an immediate
`UpdateFailed`: return/publish the immutable snapshot with its last state and new
health metadata. Otherwise `CoordinatorEntity` would inherit the coordinator's
one-failure unavailability behavior. The entity should calculate
`available = super().available and snapshot.health.available`; `super()` stays
true for handled transient device failures, while lifecycle/programming failures
can still fail the coordinator. Home Assistant's general rule is to mark entities
unavailable when communication truly cannot be established, and it explicitly
allows product-specific availability logic.
[Home Assistant availability rule](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/entity-unavailable/)

After command completion, publish the new snapshot through the coordinator. HA
documents that `async_set_updated_data` also resets the time until the next poll;
that is useful here because a successful explicit exchange makes an immediate
routine poll unnecessary.

## Connection and exchange state machine

Keep lifecycle and transport state separate.

### Lifecycle

```text
NEW -> RUNNING -> STOPPING -> CLOSED
         |           ^
         +-----------+  (setup failure or unload)
```

- `NEW`: no worker and no socket; submissions are rejected.
- `RUNNING`: worker exists; submissions accepted.
- `STOPPING`: atomically reject submissions; cancel timers/worker.
- `CLOSED`: socket/buffer gone and all futures terminal. Shutdown called again is
  a no-op.

### Transport

```text
DISCONNECTED -> CONNECTING -> READY -> EXCHANGING -> READY
      ^             |                    |
      |             +---- error ---------+
      +---------- RESETTING <-------------
```

- Only the worker transitions this machine.
- `DISCONNECTED` has `reader = writer = None` and an empty receive buffer.
- `CONNECTING` calls `asyncio.open_connection` under a connection deadline.
- `READY` represents one reusable TCP session. Do not create a separate
  one-second disconnect task. Keep the session until an error or unload; a peer
  close is detected by the next bounded exchange.
- `EXCHANGING` writes exactly one request, awaits `drain`, then parses responses
  until the expected complete response arrives or the absolute attempt deadline
  expires.
- `RESETTING` first detaches the reader/writer/buffer from live state, calls
  `writer.close()`, waits only briefly for `wait_closed()`, suppresses close-only
  transport errors, then enters `DISCONNECTED`. Detaching first prevents another
  code path from observing a half-closed connection.

`StreamWriter.drain()` is the asynchronous flow-control point, and Python advises
calling `wait_closed()` after `close()`; both therefore belong inside the bounded
exchange/reset lifecycle.
[Python stream documentation](https://docs.python.org/3/library/asyncio-stream.html)

## Timings and retry defaults

Recommended internal defaults (not user-facing options):

| Setting | Default | Rationale |
|---|---:|---|
| Routine poll interval | 30 s | Matches existing operational expectations while limiting traffic. |
| Connect timeout | 2.0 s | The LSX listener can accept slowly while recycling a prior connection. |
| Whole exchange I/O attempt | 3.25 s | Absolute deadline covering connect if needed, write/drain, framing, and response. |
| Close/reset wait | 0.25 s | Cleanup cannot wedge the worker or unload. |
| Listener recycle delay | 0.20 s | Gives the single-connection LSX listener time to accept the next operation. |
| Retry gap after failure | 0.30 s | Avoids landing both control attempts in one slow-accept window. |
| Routine/verification attempts per work item | 1 | Prevents a failed poll consuming the interval. |
| Idempotent control attempts | 2 total | One reset-before-retry; worst case is about 7.7 s. |
| Control absolute deadline from submission | 8 s | Includes queue wait and both attempts; expired controls are not sent late. |
| Verification schedule | +1 s, +2 s, +4 s | Three single-attempt reads, cumulative completion target about 7 s plus bounded exchange time. |
| Control queue capacity | 16 | Handles short UI/automation bursts while bounding memory and stale actions. |
| Parser receive-buffer limit | 4 KiB | Far above expected small LSX frames; bounds malformed streams. |
| Frames examined per exchange | 16 | Handles combined replies without an unbounded CPU loop. |

Use one **absolute monotonic deadline** per attempt (`asyncio.timeout_at` or
equivalent remaining-time calculations), rather than separately allowing full
connect, write, and read timeouts that add together. Python documents
`asyncio.timeout` as cancelling overdue work and translating that cancellation to
`TimeoutError`; cleanup must happen in `finally`, and `CancelledError` must be
re-raised after cleanup.
[Python timeout documentation](https://docs.python.org/3/library/asyncio-task.html#timeouts)
[Python cancellation guidance](https://docs.python.org/3/library/asyncio-task.html#task-cancellation)

`asyncio.wait_for` can exceed its nominal timeout while it waits for cancellation
to finish, so the underlying exchange and reset cleanup must themselves be
cancellation-safe and bounded; do not assume wrapping arbitrary nested retry code
in `wait_for(2.5)` alone proves a 2.5-second budget.

### Retry safety by command semantics

- Safe for one reset-before-retry after an ambiguous transport failure: absolute
  power/source wake SET, power off SET, absolute source, absolute volume, and
  explicit mute/unmute, provided the protocol audit confirms these are absolute
  idempotent values.
- Do not automatically repeat after bytes were drained: volume up/down,
  next/previous, and any play/pause toggle. They may have succeeded despite a
  dropped reply. Return an `AmbiguousCommandError`, record `attempted=true`, and
  schedule a read verification where a read can disambiguate.
- Any command may retry if failure is proven to precede writing bytes (for
  example connection refused), still under the same two-attempt/8-second budget.

This distinction prevents reliability retries from double-incrementing volume or
skipping two tracks.

## Direct wake and verification

`turn_on` is one high-priority absolute SET constructed from the configured wake
source (or a safe cached source). It must not enqueue or await `get_state` first.
Its command record separately tracks:

- submitted time;
- write attempted (`writer.write` reached);
- reply confirmed or typed failure/ambiguous result;
- verification state (`pending`, `confirmed`, `failed`);
- completion time/error class, without host or payload secrets.

After any write attempt, schedule the coalesced verification generation. Timer
callbacks only mark verification due and wake the worker; they never touch the
socket. A new state-changing command supersedes stale verification. Each failed
verification schedules the next delay; the worker never sleeps for seconds and
never nests all retries in one poll. User controls always run before a due
verification. Verification failure updates health/command diagnostics but never
rewrites `last_command.attempted` or rolls back optimistic state.

## Availability and stale-state hysteresis

Defaults:

- `communication_stale/degraded = true` immediately after the first failed
  top-level exchange, or whenever the last success is older than 45 seconds.
- Preserve and expose last-known state/source/volume with a `last_success` time
  and `consecutive_failures` count.
- Entity remains available until **both** conditions hold:
  `consecutive_failures >= 4` and time since the last success (or runtime start
  when there has never been a success) is at least 120 seconds.
- Any valid successful exchange immediately sets failures to zero, clears stale,
  records `last_success`, and makes the entity available on the same snapshot
  publication.

The conjunction prevents three rapid verification/DSP failures from immediately
changing availability and ensures ordinary 30-second one-poll outages are hidden.
At normal cadence, an actually unreachable speaker becomes unavailable after
roughly two minutes/four or more failed opportunities. The 45-second stale signal
still makes a missed 30-second poll visible without disabling controls.

Initial startup gets the same 120-second availability grace so a transient first
poll cannot prevent direct wake. This is justified because the configured fixed
IP and prior config-flow validation make a control attempt reasonable. After the
sustained threshold, routine polls continue (coalesced, one attempt) so a valid
response restores availability without reload.

Count a logical work item once after all of its allowed attempts, not every
internal attempt. A valid expected response from any exchange proves the control
channel is alive and resets the main health counter. Optional DSP parsing errors
should additionally mark DSP data stale; extensive DSP reads must not be used to
manufacture four rapid availability failures.

## Polling and DSP traffic

- The 30-second coordinator tick requests only the core state/source exchange.
- When off/standby, stop there. Do not query volume or DSP.
- When on, volume may be queued as one optional maintenance exchange if it is
  old enough to matter; controls are re-checked before it.
- DSP is opt-in/on-demand or very low frequency, one field per work item, one
  pending item maximum. Never use `gather` for DSP reads.
- A poll requested while one is pending or running shares/coalesces with that
  request; it is not appended.
- A poll whose coordinator caller is cancelled can be discarded if it has not
  started. No retry/backoff loop generates work independently of the coordinator.

## Framing and poisoned-connection defenses

The protocol parser must be incremental: append bounded reads, emit zero or more
complete frames, and retain only an incomplete suffix. Combined `R` frames in one
TCP read are normal stream behavior, not an error. For one outstanding request,
consume its expected frame and process additional recognized frames from the same
batch only as validated unsolicited/cache updates. Never treat arbitrary bytes or
an unexpected command identifier as the expected reply.

Within the absolute deadline, a recognized unrelated complete frame may update a
cache and parsing may continue for the expected frame. Exceeding 4 KiB, examining
more than 16 frames without the expected response, invalid length/terminator,
invalid encoding/value, or EOF-before-frame raises a distinct typed malformed or
connection error. All such failures reset the socket and discard the entire
buffer.

After a response timeout, reset before doing anything else. That rule ensures a
late response on the old TCP stream cannot be mistaken for the next command's
reply. Likewise, cancellation in `CONNECTING` or `EXCHANGING` always transitions
through reset in `finally`.

## Threat model and required mitigations

| Threat | Failure mode | Required mitigation |
|---|---|---|
| Overlapping poll/control | Two coroutines consume each other's reply. | Worker is sole client owner; no public raw client reference. |
| Poll backlog | Slow 30-second work accumulates indefinitely. | One coalesced poll and one bounded 3.25-second I/O attempt. |
| Command starvation | A recurring poll repeatedly acquires the connection first. | Controls selected before pending poll; current poll is bounded and cannot retry. |
| Deadlock on queue | Producer awaits queue capacity while holding state/lock. | Bounded non-blocking submit; no scheduler lock is held across `await`. |
| Self-deadlock | Worker awaits the future only it can complete. | Worker executes jobs and only producers await result futures. |
| Reset/refusal loop | Speaker refuses TCP and worker spins. | One poll attempt per 30 seconds; controls max two attempts; no free-running reconnect task. |
| Delayed reply poisoning | Timed-out response satisfies a later request. | Close/reset and clear buffer on every timeout before retry/next work. |
| Combined replies | Parser loses second frame or attributes it incorrectly. | Incremental multi-frame parser, expected-response matching, bounded frame count. |
| Malformed stream | Memory/CPU growth or later state corruption. | Buffer/frame limits, typed error, atomic reset; publish no partial invalid state. |
| Disconnect timer race | An idle task closes an actively used writer. | No independent disconnect task; only worker closes. |
| Duplicate non-idempotent command | Timeout retry changes volume twice/skips tracks. | Retry classification based on whether write could have reached speaker. |
| Abandoned future | Worker raises `InvalidStateError` or sends a stale queued action. | Skip cancelled/expired queued jobs; check `future.done()` before completion. |
| Unload during I/O | Orphan worker/socket or command after unload. | Reject first, cancel timers, cancel/await worker, bounded close in `finally`, fail queue. |
| Swallowed cancellation | HA shutdown hangs. | Catch cancellation only for cleanup and re-raise it, per Python guidance. |
| Event-loop blocking | HA becomes unresponsive. | Async streams only, bounded parsing, no blocking sleeps/I/O, no large synchronous loops. |
| Listener reentrancy | Entity callback submits work while internals are half-mutated. | Commit a complete immutable snapshot first; notify outside transport mutation. |

## Testable acceptance properties

1. With a fake server instrumenting active exchanges, the maximum is one across
   simultaneous poll, volume, and service calls; maximum simultaneous connection
   attempts is also one.
2. Dropping one core-poll reply leaves cached state unchanged, sets stale and one
   failure, and leaves availability true.
3. Three failures at 30-second intervals retain state and availability; after at
   least four failures and 120 seconds without success, availability is false.
4. One valid response after sustained failure makes availability true and failure
   count zero immediately, without reload.
5. Packet capture/fake-server order for `turn_on` shows the first command is the
   configured wake/source SET, with no preceding GET.
6. If a poll is awaiting a dropped reply and `turn_on` is submitted, the poll
   times out once, the connection closes, a new connection is made, and wake SET
   is the next exchange. No second poll precedes it.
7. Under repeated coordinator refresh requests, only one poll is pending/running;
   a control completes within the residual 2.5-second current-attempt bound plus
   its own bounded execution.
8. Connect hang, drain hang, delayed response, and never-completing close each
   finish/fail within their documented absolute budgets; a routine poll consumes
   far less than 30 seconds.
9. The second attempt of an idempotent command occurs only after the first writer
   is closed and buffer cleared. A relative command with a dropped post-write
   reply is sent exactly once and returns an ambiguous result.
10. A late response delivered after a timeout on the old connection cannot
    satisfy the next exchange.
11. Two or more valid `R` frames delivered in one TCP write are all framed
    correctly; the expected one resolves the job and recognized extras update
    only their matching cache fields.
12. Oversized, malformed, truncated, and unexpected frames raise their typed
    errors, publish no partial values, reset state, and allow a later valid
    connection to recover.
13. While off, repeated polls contain no volume or DSP commands. DSP requests are
    sequential, and a queued user command runs between DSP fields.
14. Cancelling a queued caller prevents its command from being sent. Cancelling a
    caller after bytes are written never causes `InvalidStateError`; unload still
    stops the in-flight worker.
15. Unload during connect, read, retry delay, and idle wait leaves no worker,
    timer, reader, writer, pending connection, or unresolved future. A second
    unload succeeds as a no-op.
16. Tests monkeypatch/blocking-detector guard all command paths to prove they use
    async stream operations and do not call blocking socket or sleep functions on
    HA's event loop.
17. Queue saturation is deterministic: the seventeenth queued control fails
    promptly with `QueueBusyError`, while already accepted FIFO jobs retain order.
18. Verification fails independently: `last_command.attempted` remains true,
    verification becomes failed after three spaced reads, no verification backlog
    remains, and an intervening user command takes priority.

## Implementation review checklist

- Search the integration for every access to `_reader`, `_writer`, receive
  buffer, `open_connection`, `write`, and `close`; all must be reachable only from
  the worker/client execution path.
- Search for `create_task` and timer registration; each task/handle must have an
  owner and an unload cancellation path.
- Search for retry loops; only the scheduler's bounded attempt loop should retry
  transport exchanges.
- Ensure exception ordering never catches `BaseException`/`CancelledError` as a
  communication failure.
- Verify coordinator/entity availability does not directly mirror one failed
  update and that transient failures publish health snapshots.
- Verify service methods await scheduler futures, not client methods, and that
  direct wake contains no read dependency.
- Run failure-injection tests under asyncio debug mode and fail on pending-task,
  unclosed-transport, or never-awaited-coroutine warnings.

## Primary references

- [Home Assistant: fetching data and request parallelism](https://developers.home-assistant.io/docs/integration_fetching_data/)
- [Home Assistant: mark entity unavailable if appropriate](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/entity-unavailable/)
- [Home Assistant: async dependencies](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/async-dependency/)
- [Python: asyncio synchronization primitives](https://docs.python.org/3/library/asyncio-sync.html)
- [Python: asyncio queues](https://docs.python.org/3/library/asyncio-queue.html)
- [Python: asyncio streams](https://docs.python.org/3/library/asyncio-stream.html)
- [Python: asyncio tasks, cancellation, and timeouts](https://docs.python.org/3/library/asyncio-task.html)
