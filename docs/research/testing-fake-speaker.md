# Testing and deterministic fake-speaker design

Research date: 2026-07-13

## Recommendation

Build a small stateful `asyncio` TCP server in `tests/fake_lsx.py` and use it for almost every protocol, scheduler, and Home Assistant behaviour test. It should listen only on `127.0.0.1` and an OS-assigned port, parse the real three-/four-byte LSX requests, and emit real protocol frames over real TCP streams. Mocks remain appropriate at narrow framework boundaries (for example, asserting config-entry forwarding), but they must not replace the socket in the reliability tests.

The high-value acceptance scenario is one continuous integration test:

1. complete a successful state poll and cache an off/Opt state;
2. drop every reply attempt belonging to the next poll;
3. assert the entity retains its state, becomes degraded/stale, and remains available;
4. invoke `media_player.turn_on` through Home Assistant's service registry;
5. assert the next protocol command is the Opt source/power SET, not a source GET;
6. ACK that SET and allow bounded asynchronous verification to succeed.

That scenario would fail against the old `aiokef` behaviour for the operational reason in scope: old `turn_on` sends `G0\x80` before attempting a wake SET.

Current testing authority: Home Assistant's [official testing guide](https://developers.home-assistant.io/docs/development_testing/) says integration tests should set up through config entries, inspect the HA state machine/registries, and invoke actions through the service registry instead of reaching through integration internals. The current [`pytest-homeassistant-custom-component` README](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component/blob/4fecaf44c8852e4c2bbde09f8ec89f85bdf1b4ad/README.md) tracks HA 2026.7.2, requires the `enable_custom_integrations` fixture, uses its own `MockConfigEntry` import, and requires `asyncio_mode = auto`.

## Fake server API and ownership

Use one fixture-created instance per test:

```python
async with FakeLsxServer() as speaker:
    client = LsxClient("127.0.0.1", speaker.port, ...)
    ...
```

`FakeLsxServer.start(port=0)` should use `asyncio.start_server(handler, "127.0.0.1", port, family=socket.AF_INET)`. Port `0` lets the OS select a collision-free ephemeral port; obtain the actual port from `server.sockets[0].getsockname()`. Python documents `start_server` as producing a `StreamReader`/`StreamWriter` pair per connection and documents that `StreamReader.read(n)` can return as soon as one byte is available, which is why both fake and client need buffering rather than assuming one read equals one frame ([Python asyncio streams](https://docs.python.org/3/library/asyncio-stream.html)).

The fake owns and cleans up:

- the `asyncio.Server`;
- every accepted `StreamWriter` and handler task;
- all delay gates;
- the scenario queue and transcript;
- a monotonic connection-id and event-id counter; and
- background exceptions, which the fixture finalizer must re-raise.

`close()` must stop accepting, release all gates, close/abort client transports, await handler completion with a short outer timeout, and call `server.close(); await server.wait_closed()`. Never let an exception in a server handler become an unobserved “Task exception was never retrieved”; append it to `server.errors` and make `assert_clean()` fail the test. `assert_clean()` should also require that all mandatory scripted steps were consumed and no concurrency violation occurred.

## Protocol parser and state model

The fake's receive buffer recognizes requests by their leading byte:

- `G` (`0x47`) requires three bytes: `47 <register> 80`;
- `S` (`0x53`) requires four bytes: `53 <register> 81 <value>`;
- an unknown leader or wrong fixed marker records a malformed request and closes the connection.

It must parse multiple requests already present in one receive buffer and retain incomplete suffixes. This prevents the fake from accidentally teaching the client the same one-read/one-frame bug being removed.

The minimal state is deliberately LSX-gen-1-specific:

```text
power: on | off
source: Wifi | Bluetooth | Aux | Opt
standby: 20 | 60 | None
orientation: L/R | R/L
volume: 0..100
muted: bool
play_state: paused | playing | stopped
```

Source GET returns the encoded source byte, adding `0x80` when off. Volume GET adds `0x80` when muted. A normal GET reply is `52 <register> 81 <value> <trailer>` and a successful SET reply is `52 11 ff`. The trailer should be configurable but opaque; no checksum algorithm was established by the protocol audit. The fake may default to a constant non-`R` byte. Automatic SET handling mutates state before ACK, while a scripted rejection does not.

Keep DSP registers available only as simple scripted values. The reliability MVP does not need a second DSP framework; tests need only prove DSP reads are sequential and cannot overlap control.

## Deterministic scenario language

Wall-clock sleeps should not drive ordinary sequencing. Model a scenario as an ordered deque of `Step(expect, action, label)` values. `expect` matches exact bytes or parsed fields (`kind`, register, value). Receiving a non-matching command records a detailed failure immediately, including expected step and transcript. This is how the direct-wake test proves absence of a pre-read: its next step expects exact SET bytes.

Required actions:

| Action | Behaviour |
|---|---|
| `AutoReply` | Apply the state model and return the normal GET/ACK frame. |
| `Reply(bytes)` | Write exactly the supplied frame once and drain. |
| `ReplyChunks(chunks, gates=...)` | Write a response in controlled pieces, waiting on explicit events between pieces. |
| `CombinedReply(frames)` | Concatenate all frames into one `writer.write()` followed by one `drain()`. TCP may still split it at the receiver, as a correct client must tolerate. |
| `GateThen(gate, action)` | Mark the step received and wait for the test to release an `asyncio.Event`, then perform another action. |
| `Delay(seconds, action)` | Use only for actual timeout-budget tests; delay, then act if the connection remains open. |
| `DropReply` | Consume and transcript the command but write nothing. Continue waiting for input/EOF so the client's response timeout and reset are real. |
| `CloseGracefully` | Close and await the stream without replying, producing EOF. |
| `AbortConnection` | Call the underlying transport's `abort()` to discard buffered data and close immediately. |
| `Reject` | Emit a deterministic non-OK SET response. |
| `Malformed(bytes, then=...)` | Emit truncated/bad-marker/wrong-register/oversized data, optionally followed by close/abort. |

Convenience methods such as `drop_next(expect, count)`, `delay_next`, `combine_next`, and `reset_next` should only compile to these explicit steps. Do not have a probabilistic “failure rate.” Tests must know precisely which exchange fails.

For a dropped operation, queue as many `DropReply` steps as the production operation's configured attempt count. This matters: dropping only the first low-level attempt may allow an internal retry to turn the overall poll into a success, which would not test availability hysteresis. Test timeout profiles should use the production retry structure with smaller test durations, not a different algorithm.

## Transcript and concurrency detection

Every event is an immutable record:

```text
sequence, loop_time, connection_id, exchange_id,
event (accepted | command | write | drop | fin | abort | eof | closed),
raw_bytes, parsed_command, scenario_label,
open_connection_count, in_flight_exchange_count
```

Use sequence numbers and explicit events for assertions; timestamps are diagnostic and for generous upper-bound checks only. Redact no test value into production diagnostics, and never use a live speaker address in fixtures.

An exchange becomes in-flight when a complete request is parsed and stops after the configured reply/drop/close action completes. Track:

- `max_open_connections`;
- `max_in_flight_exchanges`;
- `concurrent_exchange_violations` with both exchange ids; and
- whether bytes for a second request arrive while another exchange is gated.

For cross-connection overlap, the counter is sufficient. Same-connection pipelining is detected by the request buffer: after parsing one complete command, note whether another complete request is already buffered before the first action completes. The handler must not silently serialize that condition and make a broken client look correct.

The definitive non-overlap test gates a poll reply, starts a user command, yields the event loop, and asserts that the fake has received only the poll. After the poll times out/resets, the fake must receive the command on a clean connection. Assert `max_in_flight_exchanges == 1`. Merely asserting that the production code owns an `asyncio.Lock` is not sufficient.

## Refusal, reset, and recovery

`AbortConnection` provides immediate reset-like closure. Exact OS exceptions vary: loopback abort can surface as `ConnectionResetError`, another `OSError`, or EOF depending on event-loop/backend timing. Assert the integration's stable typed “connection lost/reset” category and clean recovery, not a platform-specific errno. A separate graceful-close case must exercise EOF classification.

For deterministic **connection refused then recovery on the same endpoint**, avoid the racy “ask for an unused port” helper:

1. create an IPv4 TCP socket, bind it to `127.0.0.1:0`, but do not call `listen()`;
2. retain that bound non-listening socket while the client attempts to connect to its port, which yields refusal without allowing another process to steal the port;
3. close the reservation and start `FakeLsxServer` on that same port;
4. retry through the normal next poll/command and assert success.

No accepted TCP connection exists during the refusal phase, so there is no server-side TIME_WAIT socket to impede rebinding. Keep an outer timeout and report a clear platform skip only if the OS demonstrably cannot provide bound-non-listening refusal; do not silently replace this mandatory test with a mock.

For server stop/recover tests after accepted connections, `stop_accepting()` should close the listener and all tracked streams before `start(port=same_port)`. Avoid `SO_REUSEPORT`; it is not portable to Windows and could allow two listeners, invalidating connection-ownership assertions.

## Pytest and Home Assistant fixture structure

Recommended files:

```text
tests/
  __init__.py
  conftest.py
  fake_lsx.py
  test_protocol.py
  test_scheduler.py
  test_init.py
  test_config_flow.py
  test_media_player.py
  test_diagnostics.py
```

Configure:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
testpaths = ["tests"]
```

In `conftest.py`, make `enable_custom_integrations` autouse, following the [upstream package example](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component/blob/4fecaf44c8852e4c2bbde09f8ec89f85bdf1b4ad/tests/conftest.py). Import `MockConfigEntry` from `pytest_homeassistant_custom_component.common`, not `tests.common`. The package's current socket guard explicitly allows `127.0.0.1` before disabling other sockets ([plugin source](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component/blob/4fecaf44c8852e4c2bbde09f8ec89f85bdf1b4ad/src/pytest_homeassistant_custom_component/plugins.py#L198-L216)), so bind and configure the fake with that numeric IPv4 address.

An entry fixture should create a real config entry pointed at `speaker.port`, add it to HA, call `await hass.config_entries.async_setup(entry.entry_id)`, and await `hass.async_block_till_done()`. Framework-level tests should:

- inspect `hass.states.get(entity_id)` and registries;
- call `hass.services.async_call(..., blocking=True)` for media-player actions;
- invoke refresh through coordinator/public entry behaviour rather than calling entity methods;
- unload with `await hass.config_entries.async_unload(entry.entry_id)`; and
- use `get_diagnostics_for_config_entry` from `pytest_homeassistant_custom_component.components.diagnostics`, as in the [upstream diagnostics example](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component/blob/4fecaf44c8852e4c2bbde09f8ec89f85bdf1b4ad/tests/test_diagnostics.py).

Use snapshots for broad metadata only. HA's official guide explicitly recommends a direct state assertion rather than a snapshot when testing unavailable behaviour. Availability, stale/degraded flags, failure counts, and last-command result should therefore have explicit assertions.

## Time control and anti-flake policy

The fake uses gates for order and small real delays only to test socket timeouts. Coordinator interval tests can advance HA time using `async_fire_time_changed`; the current helper accounts for DataUpdateCoordinator's randomized scheduling offset ([upstream helper](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component/blob/4fecaf44c8852e4c2bbde09f8ec89f85bdf1b4ad/src/pytest_homeassistant_custom_component/common.py#L489-L545)). If availability uses elapsed stale time, freeze the HA clock and advance it; if it uses consecutive failures, call explicit refreshes. Do not wait 30, 90, or 120 real seconds.

Use three timeout layers with distinct purposes:

- production operation timeout/retry parameters reduced through an explicit test profile or constructor arguments;
- a generous assertion bound based on `loop.time()` (for example, expected 100 ms budget must complete well below 1 s); and
- `pytest-timeout` as a final deadlock guard, following HA's documented `pytest --timeout` support.

Do not assert that a 50 ms timeout completed in 50-60 ms; shared and Windows CI cannot promise that. Assert exact attempt count and connection-reset sequence in the transcript, plus a broad total upper bound that is still below the intended update budget. Avoid unbounded `hass.async_block_till_done()` while a deliberately gated server task exists; wait on the specific production task/event, then release the gate during `finally`.

## Mandatory test mapping

| # | Test and layer | Deterministic stimulus and required assertions |
|---:|---|---|
| 1 | Successful state poll (`test_protocol`, `test_media_player`) | Fake returns a valid source frame (and volume if on). Assert decoded state and HA entity state/source; transcript has expected sequential GETs. |
| 2 | One dropped poll does not remove availability (`test_media_player`) | After initial success, script all low-level attempts of one refresh as drops. Assert prior state retained, available true, degraded/stale true, failures 1. |
| 3 | Several transient failures remain controllable (`test_media_player`) | Fail up to one below the sustained threshold. After each refresh assert cached state/availability; invoke a control service and ACK it. This must include the failed-poll/direct-wake scenario. |
| 4 | Sustained failure eventually unavailable (`test_media_player`) | Trigger exactly the documented consecutive/time threshold without real sleeping. Assert transition occurs at threshold, not before. |
| 5 | Immediate recovery (`test_media_player`) | Following unavailable state, next scripted exchange succeeds. Assert available true, stale false, failure count zero in that same update. |
| 6 | Direct wake has no pre-read (`test_protocol`, `test_media_player`) | First scenario step expects exact `53 30 81 <Opt-code>`. Any GET is an immediate fake error. ACK, then optionally answer asynchronous verification GET. |
| 7 | Wake attempted after failed poll (`test_media_player`) | Run the full high-value scenario from the recommendation. Assert transcript ordering: dropped GET attempts, connection reset(s), then source SET; last-command metadata says attempted even if verification is separately failed. |
| 8 | Poll and command never share connection concurrently (`test_scheduler`) | Gate an in-flight poll and start a command. Assert no second request before timeout/release, no same-stream pipelining, `max_in_flight_exchanges == 1`. |
| 9 | Command is not starved (`test_scheduler`) | While first poll is gated, request several refreshes and enqueue a command. After first poll resolves/fails, next command in transcript must be SET; coalesced polls may follow but no poll backlog may precede it. |
| 10 | Bounded timeouts (`test_protocol`, `test_scheduler`) | Drop replies with short test budget. Assert exact attempts, reset before retry, broad elapsed upper bound, and total less than configured update budget. No nested retry multiplication. |
| 11 | Reset/refusal recovery (`test_protocol`, `test_media_player`) | Abort one accepted exchange, then reply on new connection; separately use bound non-listening socket for refusal then start fake on same port. Assert typed errors, cleared old connection, and next success. |
| 12 | Delayed/combined replies (`test_protocol`) | Use `ReplyChunks` for every split boundary and `CombinedReply` with unrelated valid frame plus requested frame. Assert requested value selected, partial suffix buffered, no stale reply consumed by next command. |
| 13 | Malformed typed failure/no corruption (`test_protocol`) | Parameterize truncated frame, wrong marker/register, oversized junk, invalid source. Assert `MalformedResponseError` (or agreed type), connection reset, and a clean subsequent connection succeeds. |
| 14 | LSX source/standby encoding (`test_protocol`) | Parameterize all supported Wifi/Bluetooth/Aux/Opt × 20/60/None × L/R/R/L × on/off values from protocol notes. Assert exact bytes, including direct Opt wake. Keep USB/response-only Bluetooth cases explicitly scoped. |
| 15 | Config-flow validation/duplicates (`test_config_flow`) | Use real fake endpoint for success. Test syntactically invalid host, refused endpoint as `cannot_connect`, and existing normalized unique id as `already_configured`; assert no extra entry created. Cover options validation too. |
| 16 | Unload cancels/closes (`test_init`) | Gate an active exchange, unload entry under outer timeout, and assert worker/coordinator tasks done, fake observes EOF/abort, no reconnect, platforms unloaded, and fake cleanup clean. |
| 17 | Diagnostics redact (`test_diagnostics`) | Obtain diagnostics through HA helper. Recursively assert raw host/IP, config-entry id/unique id and other designated identifiers are absent or `**REDACTED**`; assert useful failure/stale counters remain. A snapshot alone is insufficient. |
| 18 | No event-loop blocking (`test_protocol`, `test_media_player`) | Delay a real socket reply while a loop heartbeat/ticker task runs. Start setup/poll/service and assert ticker advances before reply. Add final pytest timeout. No test should call sync socket methods from HA loop. |

### Additional tests that close likely gaps

- Cancellation during connect, read, retry backoff, command verification, and unload propagates after cleanup.
- A command rejection is not retried as a transport failure and is reflected in last-command result.
- Poll coalescing: ten refresh requests produce at most one pending poll.
- Off-state polling sends no volume or DSP requests.
- DSP refresh is sequential and yields to an already queued command between bounded units of work.
- A delayed reply from a timed-out connection cannot satisfy a request on the new connection.
- EOF immediately after a complete valid response does not erase that successful exchange.
- Volume clamp, step, mute-bit, and source validation never put invalid bytes on the wire.
- Setup failure leaves no handler/worker task or open stream; reload creates exactly one new owner.

## Windows and CI portability

- Force `127.0.0.1` and `AF_INET`; do not use `localhost`, which may resolve to IPv6 differently and conflicts with the package's explicit local-socket allowance.
- Use TCP, not Unix-domain sockets. Although pytest permits Unix sockets for asyncio internals, they are unavailable/non-equivalent on Windows.
- Do not use `reuse_port`, `fork`, signals, `/proc`, or POSIX-only `SO_LINGER` layouts.
- Treat `transport.abort()` as reset-like injection but assert the client's portable typed connection-loss result rather than Linux errno 104.
- Always `close()` and `await wait_closed()` where possible. Suppress platform-specific close errors only in fake cleanup, never in the production assertion path.
- Derive ports from `server.sockets`; never hard-code 50001 or use a global “free port” with a bind gap.
- Avoid sub-100-ms pass/fail timing margins. Windows timer granularity and loaded GitHub runners make event/order assertions substantially more reliable.
- Run the socket/recovery suite at least on Linux and Windows in CI. Do not run these stateful tests concurrently against one shared fake; function-scoped instances make them safe under `pytest-xdist`.

## Verification commands and unresolved points

Research commands actually run against fresh temporary clones/current sources included:

```text
git clone https://github.com/MatthewFlamm/pytest-homeassistant-custom-component.git <temp>
git -C <temp> rev-parse HEAD
git -C <temp> describe --tags --always
rg "enable_custom_integrations|MockConfigEntry|diagnostics|async_fire_time_changed" <temp>
rg "socket_allow_hosts|disable_socket" <temp>/src/pytest_homeassistant_custom_component
```

The checkout resolved to commit `4fecaf44c8852e4c2bbde09f8ec89f85bdf1b4ad`, release `0.13.346`, tracking Home Assistant 2026.7.2. The official HA testing, config-flow, and test-layout documentation and Python asyncio stream documentation were also reviewed. No live HA instance or speaker was accessed or modified for this report.

Implementation-time decisions still needing alignment with the architecture are the production hysteresis threshold, exact typed exception names, retry count/budgets, whether the scheduler is a priority queue or a command-aware lock, and whether diagnostics redact host only or host plus stable entry identifiers. Tests should import these production constants rather than duplicate magic numbers, while still asserting the externally documented thresholds.
