# Legacy KEF protocol and `aiokef` audit

Research date: 2026-07-13

## Scope and conclusion

This audit covers the legacy TCP protocol as implemented by `basnijholt/aiokef` 0.2.16/0.2.17, Home Assistant Core's built-in `kef` integration, and the five requested issue threads. It is a source audit, not a packet capture from the target LSX. Protocol facts below are therefore classified as either code-confirmed or field-observed.

The decisive findings are:

1. `aiokef` 0.2.16 and 0.2.17 contain identical protocol/client code. Version 0.2.17 only changes packaging and CI. Home Assistant still pins 0.2.16.
2. `AsyncKefSpeaker.turn_on()` always performs `get_state()` before any SET, even when the caller supplies a source. If that read exhausts its retries, no wake/source SET is written.
3. The legacy speaker is fragile under parallel access. Home Assistant PR #41700 removed a seven-way concurrent DSP read after an LSX's port-50001 server became unresponsive; both the PR and field report say the speaker permits only one control connection/exchange at a time.
4. `aiokef` has a per-exchange lock, but not one owner for a complete high-level operation. Poll and control sequences can interleave, its one-second delayed disconnect can race queued work, and retry decorators are nested deeply enough to exceed Home Assistant's 30-second poll interval.
5. A response timeout does not become a useful timeout exception. The timeout is logged, then `finally: return data` evaluates an unassigned local and raises `UnboundLocalError`. Connection, timeout, reset, malformed-response, and rejection failures are consequently not represented by a stable typed error model.
6. A replacement should retain the proven command/register and source-encoding facts, but replace the connection, framing, retry, and wake flows.

Primary code references: [`aiokef` 0.2.16 source](https://github.com/basnijholt/aiokef/blob/efd51ba17ba9bd661e276cd6f255d1788f0b2811/aiokef/aiokef.py), [`aiokef` 0.2.17 source](https://github.com/basnijholt/aiokef/blob/215be752350761fcded71e64b97c5cb67dd8b3a6/aiokef/aiokef.py), and [current Home Assistant `kef` source](https://github.com/home-assistant/core/blob/b2aa1c1b8dc157d9cbff0529cac7cb12cea3b702/homeassistant/components/kef/media_player.py).

## Version audit

The tag `v0.2.16` resolves to commit `efd51ba17ba9bd661e276cd6f255d1788f0b2811`; `v0.2.17` resolves to `215be752350761fcded71e64b97c5cb67dd8b3a6`. The [complete tag diff](https://github.com/basnijholt/aiokef/compare/v0.2.16...v0.2.17) changes only `setup.py` (exclude the top-level tests package) and a GitHub Actions version. There is no change under `aiokef/`. Therefore all protocol, retry, lifecycle, and `turn_on` findings apply equally to both versions.

Home Assistant's [current manifest](https://github.com/home-assistant/core/blob/b2aa1c1b8dc157d9cbff0529cac7cb12cea3b702/homeassistant/components/kef/manifest.json) pins `aiokef==0.2.16`, marks the integration `local_polling`, and gives it the `legacy` quality scale. Git history shows that HA moved from 0.2.13 to 0.2.16 in [PR #41700](https://github.com/home-assistant/core/pull/41700) and has never adopted 0.2.17.

## Wire format and command map

### Requests

The following is code-confirmed by [`_get`, `_set`, and `COMMANDS`](https://github.com/basnijholt/aiokef/blob/efd51ba17ba9bd661e276cd6f255d1788f0b2811/aiokef/aiokef.py#L63-L114):

- GET is exactly three bytes: `0x47 ('G'), register, 0x80`.
- SET is exactly four bytes: `0x53 ('S'), register, 0x81, value`.
- There is no request delimiter or length prefix in this implementation.

| Function | Register | GET | SET value |
|---|---:|---|---|
| Volume/mute | `0x25` (`%`) | `47 25 80` | raw volume/mute byte |
| Source/power/standby/orientation | `0x30` (`0`) | `47 30 80` | encoded source byte |
| Play/pause/track | `0x31` (`1`) | `47 31 80` | play/pause `0x81` (comment says `0x80` also works), next `0x82`, previous `0x83` |
| DSP mode | `0x27` | `47 27 80` | packed mode byte |
| Desk dB | `0x28` | `47 28 80` | option index + `0x80` |
| Wall dB | `0x29` | `47 29 80` | option index + `0x80` |
| Treble dB | `0x2a` | `47 2a 80` | option index + `0x80` |
| High-pass Hz | `0x2b` | `47 2b 80` | option index + `0x80` |
| Low-pass Hz | `0x2c` | `47 2c 80` | option index + `0x80` |
| Sub dB | `0x2d` | `47 2d 80` | option index + `0x80` |

Volume values are 0-100. Mute is encoded by adding bit `0x80` to the current volume; unmute clears it. `aiokef` treats a GET value >=128 as muted and applies `% 128` while constructing mute/unmute SETs ([volume implementation](https://github.com/basnijholt/aiokef/blob/efd51ba17ba9bd661e276cd6f255d1788f0b2811/aiokef/aiokef.py#L468-L485), [mute implementation](https://github.com/basnijholt/aiokef/blob/efd51ba17ba9bd661e276cd6f255d1788f0b2811/aiokef/aiokef.py#L635-L672)).

### Responses and framing

Code and issue logs support two response forms:

- A GET reply is expected to be five bytes: `0x52 ('R'), register, 0x81, value, trailer`. For example, issue #15 records `R%\x81<D`; `aiokef` takes the second-to-last byte (`0x3c`, volume 60) as the value.
- A successful SET acknowledgement is exactly `52 11 ff`; `send_message()` consequently returns `0x11` as the SET result.

The fifth GET byte is opaque to `aiokef`. It may be a checksum, but neither its meaning nor validation algorithm is established by the audited sources, so a new client must not claim checksum validation without packet/spec evidence.

The existing parser is not a safe stream framer. It calls `reader.read(100)` once, splits the returned chunk at every literal `R`, then chooses the first GET segment whose second byte equals the requested register or an exact three-byte SET ACK ([parser and read path](https://github.com/basnijholt/aiokef/blob/efd51ba17ba9bd661e276cd6f255d1788f0b2811/aiokef/aiokef.py#L209-L227), [I/O](https://github.com/basnijholt/aiokef/blob/efd51ba17ba9bd661e276cd6f255d1788f0b2811/aiokef/aiokef.py#L292-L319)). This was intended to cope with combined replies, but it:

- does not accumulate a partial TCP frame;
- does not validate a GET frame's length, `0x81` marker, or trailer;
- mistakes value/trailer byte `0x52` for a frame boundary (source value 82 is itself valid);
- can raise `IndexError` on a one-byte segment;
- emits generic `Exception` for wrong-register replies and SET rejection; and
- can leave an unrelated delayed frame in the stream for the next exchange.

A maintained parser should buffer bytes, recognize fixed three-byte SET ACK and five-byte GET frames, tolerate multiple complete frames in one read, retain incomplete suffixes, match the requested register, impose a small maximum buffer, and reset the connection after malformed/unmatchable input. Whether unsolicited replies occur is unverified.

## Source, standby, orientation, and power encoding

The encoding table is generated in [`INPUT_SOURCES`](https://github.com/basnijholt/aiokef/blob/efd51ba17ba9bd661e276cd6f255d1788f0b2811/aiokef/aiokef.py#L34-L61). The table below lists the **on** value. Add `0x80` for off/standby. `L/R` is normal orientation; `R/L` is inverse orientation.

| Source | Standby | L/R | R/L |
|---|---:|---:|---:|
| Wifi | 20 min | 2 (`0x02`) | 66 (`0x42`) |
| Wifi | 60 min | 18 (`0x12`) | 82 (`0x52`) |
| Wifi | never (`None`) | 34 (`0x22`) | 98 (`0x62`) |
| Bluetooth | 20 min | 9 (`0x09`) | 73 (`0x49`) |
| Bluetooth | 60 min | 25 (`0x19`) | 89 (`0x59`) |
| Bluetooth | never | 41 (`0x29`) | 105 (`0x69`) |
| Aux | 20 min | 10 (`0x0a`) | 74 (`0x4a`) |
| Aux | 60 min | 26 (`0x1a`) | 90 (`0x5a`) |
| Aux | never | 42 (`0x2a`) | 106 (`0x6a`) |
| Opt | 20 min | 11 (`0x0b`) | 75 (`0x4b`) |
| Opt | 60 min | 27 (`0x1b`) | 91 (`0x5b`) |
| Opt | never | 43 (`0x2b`) | 107 (`0x6b`) |
| Usb | 20 min | 12 (`0x0c`) | 76 (`0x4c`) |
| Usb | 60 min | 28 (`0x1c`) | 92 (`0x5c`) |
| Usb | never | 44 (`0x2c`) | 108 (`0x6c`) |

The response-only `Bluetooth_paired` variants are 15/79 (20), 31/95 (60), and 47/111 (never); `aiokef`'s comment says they cannot be used to set. The built-in HA integration exposes Wifi, Bluetooth, Aux, and Opt for LSX, adding Usb only for LS50 ([HA source list](https://github.com/home-assistant/core/blob/b2aa1c1b8dc157d9cbff0529cac7cb12cea3b702/homeassistant/components/kef/media_player.py#L41-L42)).

Power is not a separate register. `set_source()` clears bit 7 for on and adds `0x80` for off. `get_state()` treats values above 128 as off, reduces the response modulo 128, and decodes source/standby/orientation ([state and source implementation](https://github.com/basnijholt/aiokef/blob/efd51ba17ba9bd661e276cd6f255d1788f0b2811/aiokef/aiokef.py#L416-L466)). There is a hard-coded anomaly: response 48 is decoded as response 82, i.e. Wifi / 60 minutes / R/L; the source comment says both LSX and LS50W require this, but gives no protocol explanation.

For the primary target, direct Opt wake with default never-standby and normal orientation is `53 30 81 2b`; inverse orientation is `53 30 81 6b`. With 20-minute standby the corresponding values are `0b`/`4b`, and with 60-minute standby `1b`/`5b`. These bytes are a direct consequence of the audited mapping, but still require real-LSX validation before a stable release.

## Exact `turn_on` read-before-write behaviour

[`turn_on`](https://github.com/basnijholt/aiokef/blob/efd51ba17ba9bd661e276cd6f255d1788f0b2811/aiokef/aiokef.py#L682-L700) performs this sequence:

1. `state = await get_state()`; this sends source GET `47 30 80` and waits/retries.
2. If `state.is_on`, return without writing.
3. Otherwise call `set_source(source or state.source, state="on")`; only here is `53 30 81 <encoded-source>` written.
4. `set_source()` itself polls `get_state()` to verify the source.
5. `turn_on()` then polls `is_on()` (which is another `get_state()`) up to 20 times with one-second sleeps. If all checks report off, it falls through without raising a final failure.

Supplying `turn_on("Opt")` does **not** bypass step 1. `turn_on` itself has no retry decorator, although `get_state` has nested retries. If the initial read ultimately raises, control exits before step 3 and no SET is attempted. Home Assistant's [`async_turn_on`](https://github.com/home-assistant/core/blob/b2aa1c1b8dc157d9cbff0529cac7cb12cea3b702/homeassistant/components/kef/media_player.py#L273-L278) calls `turn_on()` with no source, cementing this dependency on a successful pre-read.

The new integration should encode its configured preferred wake source (or a safe cached source) and submit the SET directly. Verification belongs after the attempted write and must have its own bounded budget; inability to verify must not be reported as “command not attempted.”

## Retry and connection lifecycle audit

`aiokef` uses a two-second connect/response timeout, one-second keep-alive, ten connection-refusal attempts, five `send_message` attempts, and ten high-level command attempts ([constants and retry policy](https://github.com/basnijholt/aiokef/blob/efd51ba17ba9bd661e276cd6f255d1788f0b2811/aiokef/aiokef.py#L25-L32), [Tenacity configuration](https://github.com/basnijholt/aiokef/blob/efd51ba17ba9bd661e276cd6f255d1788f0b2811/aiokef/aiokef.py#L151-L162)). The retry layers are nested: for example `get_state` (10) calls `send_message` (5), while `open_connection` can try ten times inside each send. `set_source` adds its own ten-attempt verification loop, and turn-on adds 20 checks. Exponential waits occur at both decorated levels. A single HA update can therefore last far beyond the 30-second polling interval; issue #64588 records the corresponding HA warning and an approximately ten-minute dead period.

Failure behaviour in 0.2.16/0.2.17 is materially ambiguous:

| Event | Existing observable exception/result |
|---|---|
| TCP connect refused ten times | `ConnectionRefusedError("Connection tries exceeded.")` after ten 0.5 s sleeps |
| Connect timeout or other `OSError` | normalized to `ConnectionRefusedError("Speaker is offline.")` |
| Response timeout | logs “Timeout in waiting for reply”, then normally raises `UnboundLocalError` because `data` was never assigned before `finally: return data` |
| Peer reset during drain | disconnects, then re-raises `ConnectionResetError` |
| EOF / missing requested response | generic parser `Exception` |
| Truncated segment | potentially `IndexError` |
| SET without exact OK frame | generic parser `Exception` |

The `return` in `_send_message`'s `finally` is also cancellation-hostile because a `return` in `finally` can suppress an in-flight exception when a value exists. [`is_online`](https://github.com/basnijholt/aiokef/blob/efd51ba17ba9bd661e276cd6f255d1788f0b2811/aiokef/aiokef.py#L674-L680) likewise returns from `finally`, potentially suppressing unexpected exceptions/cancellation.

Connection ownership is incomplete. The October 2020 fix moved the connected check inside the lock ([commit `efd51ba`](https://github.com/basnijholt/aiokef/commit/efd51ba17ba9bd661e276cd6f255d1788f0b2811)), preventing simultaneous `asyncio.open_connection` calls. However, `send_message()` releases that lock after `open_connection()` and reacquires it for write/read. A one-second disconnect is scheduled after connect and after each reply. The disconnect task sleeps, then shields `_disconnect()` from cancellation; canceling the outer task after shielding has begun does not cancel the inner disconnect. It can therefore wait for the same lock and close a connection that newer queued work intended to use. This specific “knocking on the door” race was identified in [issue #15's follow-up](https://github.com/basnijholt/aiokef/issues/15#issuecomment-712668739). `writer.wait_closed()` is unbounded, and the speaker class exposes no explicit close method for HA unload.

A new client needs one serialized owner for connection creation, reader/writer mutation, framing, and exchange. It should use explicit typed errors (connect refused, connect timeout, response timeout, reset/EOF, malformed response, rejection), one bounded retry policy, reset before retry, and a bounded close. `CancelledError` must propagate after cleanup. Poll and control priority belongs above the protocol client; a lock around individual exchanges alone cannot stop a long poll/verification chain from interleaving with or starving commands.

## Built-in Home Assistant behaviour

The built-in integration remains a legacy YAML media-player platform. It polls every 30 seconds and refreshes DSP every hour. On each [`async_update`](https://github.com/home-assistant/core/blob/b2aa1c1b8dc157d9cbff0529cac7cb12cea3b702/homeassistant/components/kef/media_player.py#L240-L267), it:

1. assigns entity availability directly from `speaker.is_online()` with no hysteresis;
2. if online, reads volume/mute and state, then performs the initial seven DSP reads sequentially;
3. if the connection check is false, clears volume/source, sets state off, and makes the entity unavailable;
4. catches only `ConnectionError` and `TimeoutError` around later reads and sets state to `None` without a structured degraded state.

Thus a single failed connection check can make services unavailable for that poll, while a later read failure has inconsistent semantics. The response-timeout `UnboundLocalError` is not in the integration's catch tuple. There is no last-success age, consecutive-failure count, or separation between stale state and reasonable ability to attempt control.

PR [#41700](https://github.com/home-assistant/core/pull/41700) changed DSP refresh from `asyncio.gather()` to sequential awaits and says the speaker allows only one concurrent connection. This mitigated the observed seven-way burst but did not create command priority, bound nested retries, change availability semantics, or fix direct wake.

## Required issue review

### `home-assistant/core#141568`

[Issue #141568](https://github.com/home-assistant/core/issues/141568) reports both pairs of speakers being slow under HA 2025.3.4, with entity updates exceeding ten seconds. It received no technical diagnosis or fix and was closed by stale automation. Evidence value: the latency problem persisted years after #41700 and with the same 0.2.16 pin; it does not establish a root cause.

### `home-assistant/core#64588`

[Issue #64588](https://github.com/home-assistant/core/issues/64588) reports quick source-select then volume-set sometimes not taking effect, repeated “Timeout in waiting for reply,” HA updates exceeding the 30-second interval, and a roughly ten-minute period in which control/state appeared dead. The reporter later said it still occurred; no fix was merged in the thread. Evidence value: retry duration and high-level command interleaving are operational failures, not merely inaccurate state.

### `basnijholt/aiokef#15`

[Issue #15](https://github.com/basnijholt/aiokef/issues/15) is the strongest LSX reliability evidence. During simultaneous mode plus six DSP requests, the LSX control server and KEF Control app became unreachable while Wi-Fi, Spotify playback, and HTTP remained functional; recovery required a power cycle. [The reporter tied recurrence to seven simultaneous operations](https://github.com/basnijholt/aiokef/issues/15#issuecomment-706912083). HA [PR #41700](https://github.com/home-assistant/core/pull/41700) serialized DSP reads; after a week the reporter saw no recurrence, but later documented remaining state/disconnect races. Evidence value: never overlap protocol/DSP exchanges, and minimize traffic to this fragile server.

### `basnijholt/aiokef#7`

[Issue #7](https://github.com/basnijholt/aiokef/issues/7) says the 20-minute standby setting could make an LS50W's port-50001 server shut down; a field workaround required changing standby to 60 in a separate request before power-off, not merely embedding 60 in the off request. Another comment notes older LS50W controllers may not keep the TCP server alive while off. This is LS50W evidence, not proof of LSX behaviour. It does establish that standby encoding and wake capability can vary by model/controller and must not be generalized.

### `basnijholt/aiokef#35`

[Issue #35](https://github.com/basnijholt/aiokef/issues/35) is a single LSX gen-1 firmware `p20.5202205110.105243422` field report: power, inputs, status, volume, and mute work, while setting standby always reads back as `None` and does not cause automatic standby. It has no maintainer confirmation or comments as of this audit, and its method names do not match the 0.2.16/0.2.17 API. Treat it as hardware-test guidance, not a confirmed protocol amendment.

## Reuse and attribution

`aiokef` is [MIT licensed](https://github.com/basnijholt/aiokef/blob/efd51ba17ba9bd661e276cd6f255d1788f0b2811/LICENSE). The license notice is, verbatim, headed “MIT License” and credits `Copyright (c) 2019 Bas Nijholt and -2018 Robin Grönberg`. If source, tables, mappings, or other substantial portions are copied or adapted, include that exact copyright notice and the full MIT permission/warranty text in the distribution, preferably in `THIRD_PARTY_NOTICES.md`, and add a concise derived-from note near the vendored mapping/client. Record the source URL, tag/hash (`v0.2.16`, `efd51ba...`), and modifications. The README also credits Robin Grönberg/`pykef`, Bastian Beggel/`hasskef`, and `chimpy`; retaining those provenance credits is prudent when carrying forward the reverse-engineered map.

Home Assistant Core is Apache-2.0, not MIT. Do not copy its integration implementation and describe the result as solely MIT-derived. Reimplement the observed behaviour using current HA APIs. If any HA code is copied, preserve Apache-2.0 notices and comply with its NOTICE/change-marking terms. Protocol facts and independently written compatibility code can be documented without importing HA's implementation text.

## Implementation constraints derived from this audit

- One connection owner and at most one request/reply exchange per speaker at all times.
- A direct wake SET must be enqueued without a prerequisite GET.
- User commands take priority over routine polls; high-level poll/verification sequences must not monopolize the owner.
- One retry layer, an explicit total budget shorter than the poll interval, and a clean connection reset before retry.
- Fixed-length buffered framing with support for partial and combined replies; no `split(b"R")` parser.
- Retain last known state across transient failures; track staleness/failure counters independently from availability.
- Sequential, sparse DSP access; no gather, and preferably no DSP in the normal off-state poll.
- Explicit async close/unload; cancel queued work, propagate cancellation, bound `wait_closed`, and leave reader/writer references cleared.
- Validate source/standby/orientation bytes with the deterministic fake server, then with real LSX hardware before release claims.

## Verification commands and uncertainties

Research was performed from fresh read-only clones under the OS temporary directory. Representative commands actually run:

```text
git clone https://github.com/basnijholt/aiokef.git <temp>/aiokef
git clone --filter=blob:none --sparse https://github.com/home-assistant/core.git <temp>/core
git -C <temp>/core sparse-checkout set homeassistant/components/kef
git -C <temp>/aiokef show-ref --tags
git -C <temp>/aiokef diff v0.2.16 v0.2.17
git -C <temp>/aiokef show v0.2.16:aiokef/aiokef.py
git -C <temp>/aiokef log --oneline v0.2.15..v0.2.16
git -C <temp>/core log -- homeassistant/components/kef
git -C <temp>/core show 42c9776c97ad8649a59ebd457ab70ef9b50ab275
Invoke-RestMethod https://api.github.com/repos/{owner}/{repo}/issues/{number}
Invoke-RestMethod <issue comments_url>
```

Verified outputs included the two tag hashes, a two-file non-client diff between 0.2.16/0.2.17, current HA's `aiokef==0.2.16` manifest pin, and PR #41700's gather-to-sequential diff. No command was sent to a live speaker and no live HA state was modified.

Remaining uncertainties requiring hardware/packet validation are the GET trailer/check algorithm; whether unsolicited frames occur; how the LSX behaves when TCP splits a frame; play/pause/track behaviour per source; firmware-specific standby behaviour; the response-code-48 anomaly; and whether all listed LS50-derived USB/DSP behaviours apply to LSX gen 1. These uncertainties should become explicit fake-server cases where possible and a real-hardware prerelease checklist otherwise.
