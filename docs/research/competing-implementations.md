# Competing KEF implementations and maintainability review

Research snapshot: 2026-07-13. This report evaluates current Home Assistant/KEF repositories as implementation references for a first-generation LSX integration. Protocol truth and `aiokef` behavior are covered in the dedicated protocol report; this document focuses on product-family fit, repository conventions, licensing, and the client packaging decision.

## Recommendation

Build a focused **`kef_lsx`** HACS integration with a small, typed, async legacy TCP client vendored inside `custom_components/kef_lsx`. Keep that client free of Home Assistant imports and give it its own protocol/fake-server tests, so it can be extracted later without designing a second package today.

Do not depend on or copy the architecture of current KEF Connect integrations for protocol behavior. Their `/api/getData`, `/api/setData`, and event-queue APIs target the newer W2/LSX II family, not the first-generation LSX TCP service on port 50001. Do not use the `kef` domain: both Home Assistant Core and one competing HACS integration use it, and a custom component with that domain overrides Core.

Useful ideas to adopt independently are UI config entry setup, a maximum-volume guard, a volume-step option, typed snapshots/exceptions, coordinator fan-out, diagnostic redaction, local brand assets, and disabled-by-default low-value diagnostic entities. Do not copy their polling, wake, socket, identity, or failure handling.

## Family split: KEF Connect is not LSX generation 1

There are two materially different local API families:

| Family | Representative devices | Transport observed in current projects | Relevant here? |
|---|---|---|---|
| Legacy / first generation | LSX gen 1, LS50 Wireless gen 1 | Compact binary TCP protocol, normally port 50001 | **Yes** |
| KEF Connect / W2 generation | LSX II, LSX II LT, LS50 Wireless II, LS60, XIO | Local HTTP endpoints such as `/api/getData`, `/api/setData`, and `/api/event/*` | No, except for UX/repository ideas |

This distinction is explicit in the current `pykefcontrol` README: it says the library supports LS50 Wireless II, LSX II, and LS60 only and directs first-generation LS50W/LSX users to `aiokef` ([permalink](https://github.com/N0ciple/pykefcontrol/blob/f5d530c227976d527f2288d29409e42978853bba/README.md)). Its source builds HTTP requests to the speaker's `/api/*` endpoints ([client permalink](https://github.com/N0ciple/pykefcontrol/blob/f5d530c227976d527f2288d29409e42978853bba/pykefcontrol/kef_connector.py)). This is authoritative evidence that a `pykefcontrol`-based integration is not a transport implementation for the target LSX.

## Current HACS-visible KEF integrations

The current HACS default repository list contains both `N0ciple/hass-kef-connector` and `EvotecIT/homeassistant-kef` ([HACS default list at `bf57844`](https://github.com/hacs/default/blob/bf578449efc26204cb15c9b7df0b05e84b9b1758/integration)). Being listed does not establish first-generation hardware compatibility; the source must still be evaluated.

### N0ciple/hass-kef-connector

Snapshot inspected: default branch commit [`dc7218e`](https://github.com/N0ciple/hass-kef-connector/tree/dc7218e6c09278c627da8ae315eaea570e4d2e0f), pushed 2026-05-02.

What it is:

- The README lists LSX II/II LT, LS50 Wireless II, LS60, and XIO, not LSX gen 1 ([README](https://github.com/N0ciple/hass-kef-connector/blob/dc7218e6c09278c627da8ae315eaea570e4d2e0f/README.md)).
- Its manifest depends on `pykefcontrol==0.9.3`, declares `config_flow: false`, and is a YAML media-player platform ([manifest](https://github.com/N0ciple/hass-kef-connector/blob/dc7218e6c09278c627da8ae315eaea570e4d2e0f/custom_components/kef_connector/manifest.json)).
- The entity polls a series of independent HTTP properties, sleeps inside command methods before forcing refresh, and fetches MAC/name during entity update ([media player](https://github.com/N0ciple/hass-kef-connector/blob/dc7218e6c09278c627da8ae315eaea570e4d2e0f/custom_components/kef_connector/media_player.py)).
- The repository has HACS and hassfest workflows but no tests at the inspected commit ([tree](https://github.com/N0ciple/hass-kef-connector/tree/dc7218e6c09278c627da8ae315eaea570e4d2e0f)).

Useful conventions:

- maximum volume and volume step are understandable user settings;
- source lists are model-specific;
- HACS and hassfest workflows are present;
- a speaker MAC, when returned by the device API itself, is a good device identity.

Do not reuse:

- the transport or endpoint mappings: they are for the newer HTTP API;
- YAML-only setup, delayed sleeps in service calls, or multiple I/O property reads per update;
- broad feature flags without capability/hardware verification;
- connection/session subclassing that reaches into a dependency's private `_session` state.

Licence: the integration is Apache-2.0 ([LICENSE](https://github.com/N0ciple/hass-kef-connector/blob/dc7218e6c09278c627da8ae315eaea570e4d2e0f/LICENSE)); `pykefcontrol` is MIT, copyright 2020 rodupont ([LICENSE](https://github.com/N0ciple/pykefcontrol/blob/f5d530c227976d527f2288d29409e42978853bba/LICENSE)). Concepts may be reimplemented, but copied integration code would bring Apache-2.0 notice/modified-file obligations into this MIT repository. There is no need to take that burden for incompatible transport code.

### danielpetrovic/hass-kef-connector

Snapshot inspected: fork commit [`19e8cbe`](https://github.com/danielpetrovic/hass-kef-connector/tree/19e8cbe9e33158fb7fea573996cbb0c20ac2f450), pushed 2026-05-04.

This fork modernizes the N0ciple integration with UI config flow, discovery, options, a `DataUpdateCoordinator`, translations, and diagnostic sensors. Its README still explicitly limits compatibility to the W2 platform: LSX II/II LT, LS50 Wireless II, LS60, and XIO ([README](https://github.com/danielpetrovic/hass-kef-connector/blob/19e8cbe9e33158fb7fea573996cbb0c20ac2f450/README.md)); its manifest still depends on `pykefcontrol` ([manifest](https://github.com/danielpetrovic/hass-kef-connector/blob/19e8cbe9e33158fb7fea573996cbb0c20ac2f450/custom_components/kef_connector/manifest.json)).

The coordinator has a useful high-level idea: retain cached data for two failures, then raise `UpdateFailed` after three and slow the offline poll interval ([coordinator](https://github.com/danielpetrovic/hass-kef-connector/blob/19e8cbe9e33158fb7fea573996cbb0c20ac2f450/custom_components/kef_connector/coordinator.py)). That is evidence that availability hysteresis is practical with a coordinator. It is not a reliability implementation to copy:

- one refresh performs many sequential HTTP calls;
- there is no single communication owner or command/poll serialization;
- broad `Exception` handling collapses error types;
- user commands call the speaker directly and can overlap coordinator polling;
- scan and offline retry intervals are exposed as expert options;
- runtime state is stored in `hass.data` rather than current typed `ConfigEntry.runtime_data`;
- the inspected repository has no test directory.

Its Apache-2.0 licence is inherited from the upstream project ([LICENSE](https://github.com/danielpetrovic/hass-kef-connector/blob/19e8cbe9e33158fb7fea573996cbb0c20ac2f450/LICENSE)). Reimplement the small UX ideas; do not copy code.

### EvotecIT/homeassistant-kef

Snapshot inspected: commit [`0479463`](https://github.com/EvotecIT/homeassistant-kef/tree/0479463c4ee0fabb7a7b85d4a62dffcf84e3f861), pushed 2026-06-21, version 0.8.2.

This is the most substantial current competitor. It is MIT-licensed ([LICENSE](https://github.com/EvotecIT/homeassistant-kef/blob/0479463c4ee0fabb7a7b85d4a62dffcf84e3f861/LICENSE)), uses config entries, typed snapshot models, diagnostics, a test suite, local brand assets, and modern/legacy backend classes. The README is candid that real-device validation is strongest on LSX II and that older-device validation remains a roadmap priority ([README](https://github.com/EvotecIT/homeassistant-kef/blob/0479463c4ee0fabb7a7b85d4a62dffcf84e3f861/README.md)).

Useful conventions:

- protocol models and typed exception classes are separated from Home Assistant entity code;
- `ConfigEntry.runtime_data` holds the coordinator;
- diagnostics use `async_redact_data` and redact host, network, MAC, serial, KEF ID, and password ([diagnostics](https://github.com/EvotecIT/homeassistant-kef/blob/0479463c4ee0fabb7a7b85d4a62dffcf84e3f861/custom_components/kef/diagnostics.py));
- less useful diagnostic entities can be disabled by default;
- config flow, reconfigure, reauth, options, translations, CI, and integration tests are represented;
- modern and legacy capabilities are separated behind a backend interface;
- its LSX II investigation clearly documents that the modern HTTP API and legacy TCP API are distinct ([investigation](https://github.com/EvotecIT/homeassistant-kef/blob/0479463c4ee0fabb7a7b85d4a62dffcf84e3f861/docs/kef-lsx2-investigation.md)).

Why its legacy path is not suitable as-is:

- It uses the domain `kef` ([manifest](https://github.com/EvotecIT/homeassistant-kef/blob/0479463c4ee0fabb7a7b85d4a62dffcf84e3f861/custom_components/kef/manifest.json)), so installation overrides the built-in integration. This directly conflicts with this project's distinct-domain requirement.
- `LegacyBinaryClient.async_turn_on()` performs `async_refresh()` before sending the source SET, preserving the exact read-before-wake failure mode that must be removed ([legacy client, lines 2096-2240](https://github.com/EvotecIT/homeassistant-kef/blob/0479463c4ee0fabb7a7b85d4a62dffcf84e3f861/custom_components/kef/kef_client/client.py#L2096-L2240)).
- Each exchange opens and closes a new TCP connection. There is no single long-lived owner, lock, or priority queue; a user command can overlap a coordinator refresh ([exchange, lines 2533-2560](https://github.com/EvotecIT/homeassistant-kef/blob/0479463c4ee0fabb7a7b85d4a62dffcf84e3f861/custom_components/kef/kef_client/client.py#L2533-L2560)).
- `reader.read(100)` is not a complete framing strategy: a TCP read may return a partial frame or several frames. Splitting the one read on `R` does not buffer incomplete replies ([parser, lines 2562-2577](https://github.com/EvotecIT/homeassistant-kef/blob/0479463c4ee0fabb7a7b85d4a62dffcf84e3f861/custom_components/kef/kef_client/client.py#L2562-L2577)).
- Connection timeout and connection OS errors are not classified separately; the initial `asyncio.wait_for(open_connection(...))` catches `OSError` but not its timeout at that point.
- Legacy `async_identify()` manufactures a host-based `unique_id`, so an IP change changes identity ([lines 2116-2126](https://github.com/EvotecIT/homeassistant-kef/blob/0479463c4ee0fabb7a7b85d4a62dffcf84e3f861/custom_components/kef/kef_client/client.py#L2116-L2126)).
- The client is duplicated in root `kef_client/` and `custom_components/kef/kef_client/`, creating two copies that must stay synchronized ([repository tree](https://github.com/EvotecIT/homeassistant-kef/tree/0479463c4ee0fabb7a7b85d4a62dffcf84e3f861)).
- At the inspected commit, protocol tests cover the modern client with monkeypatched methods, while the only `LegacyBinaryClient` reference in `test_api.py` is an autodetection/auth mock; there is no deterministic legacy TCP fake-server suite ([test API](https://github.com/EvotecIT/homeassistant-kef/blob/0479463c4ee0fabb7a7b85d4a62dffcf84e3f861/tests/components/kef/test_api.py)).
- The broad dual-family feature surface introduces dependencies and failure paths irrelevant to an LSX-gen1 reliability fix, including modern HTTP event queues, authentication, firmware upload, EQ/settings entities, and `cryptography` ([project metadata](https://github.com/EvotecIT/homeassistant-kef/blob/0479463c4ee0fabb7a7b85d4a62dffcf84e3f861/pyproject.toml)).

This repository is a valuable adversarial reference and a source of maintainable HA conventions. Its legacy implementation is not evidence that the required failure behavior is solved.

## Other repositories found

These are not stronger bases:

- [`basnijholt/media_player.kef` at `0e61e7e`](https://github.com/basnijholt/media_player.kef/tree/0e61e7e17740fa7357952d10878046a1f71a9beb) is the old MIT custom integration for LS50W/LSX and now directs users to the Core integration. It vendors the historical client style and is not a current config-entry/HACS architecture.
- [`JesalR/kef_control` at `6a877b0`](https://github.com/JesalR/kef_control/tree/6a877b01e41cc1de081f128cc007659db4c434a8) targets modern LSX II/LS50WII/LS60, depends on an older `pykefcontrol`, and its own README recommends N0ciple instead. It is MIT, but adds no relevant legacy solution.
- [`m-lange/kef_speaker` at `f3ef1df`](https://github.com/m-lange/kef_speaker/tree/f3ef1dfe7b720c7c0858c294f913e4266b756a50) is an LSX II HTTP integration, not gen 1, and the inspected tree has no licence file.
- [`pravin-sivabalan/ha-kef` at `80098c4`](https://github.com/pravin-sivabalan/ha-kef/tree/80098c46813ef5adc67cfa54f0508d2f0e15462b) depends on `pykefcontrol==0.7.1`, targets the newer API, is not in standard HACS layout, and the inspected tree has no licence file.
- [`dnch-chernov/ha-kef-speaker` at `cc2ce8a`](https://github.com/dnch-chernov/ha-kef-speaker/tree/cc2ce8a629c694f61bc98aa61bd51b8133f86a0f) is still the generic `integration_blueprint` template internally, not a KEF implementation.

Do not copy from repositories without an explicit licence. GitHub's own licensing guidance states that without a licence, default copyright applies and others do not receive permission to reproduce, distribute, or create derivative works ([GitHub Docs](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository)).

## Built-in Home Assistant integration as a competitor/reference

Home Assistant 2026.7.2 still ships `kef` as a legacy-quality, YAML-only local-polling integration with `aiokef==0.2.16` and `getmac==0.9.5` ([manifest](https://github.com/home-assistant/core/blob/2026.7.2/homeassistant/components/kef/manifest.json)). It is Apache-2.0 as part of Home Assistant Core. Its value here is behavioral compatibility and migration context, not code architecture.

The old `basnijholt/media_player.kef`, current Core integration, and Evotec HACS integration all use the `kef` domain. This reinforces the need for `kef_lsx`; an overriding custom `kef` component could silently replace Core files for all users and complicate rollback.

## What to adopt and what to reject

| Area | Adopt | Reject |
|---|---|---|
| Setup | UI config flow, short connection validation, reconfigure, duplicate prevention | YAML-only setup; broad AirPlay/Google Cast discovery before device identity is trustworthy |
| UX options | Preferred wake source, maximum volume, volume step, inverse orientation | User-facing poll, timeout, retry, and offline-interval tuning knobs |
| Entity model | Standard media-player features, cached memory-only properties, typed snapshots | I/O in properties; always advertising unverified play/track features |
| Diagnostics | Redacted host/identifiers, typed last error, health counters; noisy entities disabled | Raw packets, SSID/BSSID/IP, exception strings containing network data |
| Availability | Cached state with hysteresis and immediate recovery on any successful exchange | `UpdateFailed` on one miss; treating status-read health as control capability |
| Commands | Direct known wake/source SET, then bounded asynchronous verification | Read-before-wake; fixed sleeps inside user actions |
| Transport | One serialized owner, priority for controls, bounded typed failures, incremental framing | One uncoordinated socket per call; overlapping poll/command connections; one-shot `read(100)` parsing |
| Scope | LSX generation 1 and its verified sources/standby/orientation behavior | Modern HTTP API, auth, firmware uploads, wide EQ/entity surface in the MVP |
| Tests | Deterministic TCP fake with drops/delays/combined frames/reset/recovery | Monkeypatch-only “was called” tests that cannot reproduce control loss after failed poll |

## Vendored client versus a separate PyPI package

### Option A: vendored, HA-independent module inside the integration

Recommended initial layout:

```text
custom_components/kef_lsx/
  protocol.py       # messages, parser, typed models/errors
  client.py         # connection lifecycle; no HA imports
  worker.py         # command/poll ownership and priority
  coordinator.py    # HA adapter
tests/
  fake_speaker.py
  test_protocol.py
  test_client.py
  test_worker.py
  test_integration.py
```

Advantages:

- one repository, PR, version, issue tracker, and release contains protocol and HA changes;
- failure-injection tests can exercise the exact code HACS installs;
- no PyPI publishing credentials, package metadata, release ordering, or dependency-resolution failure during HA setup;
- no external runtime dependency beyond Home Assistant/Python standard async facilities;
- HACS simply copies `custom_components/kef_lsx`, so all required code is present offline after download;
- protocol changes during real-speaker validation can be made atomically with coordinator/entity changes;
- the small target-specific interface is easier to review than a premature multi-model public API.

Costs:

- the client is not directly `pip install`-reusable;
- the integration repository owns protocol maintenance and security updates;
- a future Core contribution would normally require extracting a library.

Mitigate those costs by enforcing a one-way dependency: client/protocol modules import no `homeassistant.*`, have full type annotations, and are tested without HA. Extraction later is then mostly packaging, not redesign.

Manifest/HACS impact: set `"requirements": []`; no dependency logger is needed. Bump only the integration version. Third-party MIT attribution for any derived `aiokef` mappings or code ships in this repository and therefore always accompanies HACS installs.

### Option B: separate PyPI package now

Advantages:

- reusable by scripts and potentially a future Core integration;
- protocol releases can have a distinct public API and changelog;
- HA integration code becomes a thinner adapter.

Costs now:

- two repositories/packages, release processes, version numbers, issue-routing decisions, and CI matrices;
- every protocol fix must be published first, then pinned and released in the integration;
- package publication credentials and supply-chain controls become part of the project;
- users can fail setup because a requirement cannot be downloaded or resolved, even though HACS installed the integration files;
- real-hardware discovery is still changing the desired API, so a public package interface would be stabilized before its behavior is understood;
- a tiny LSX-only client has no demonstrated second consumer yet.

Manifest/HACS impact: `manifest.json` would need an exact requirement such as `"requirements": ["kef-legacy-client==0.x.y"]` and normally `loggers` for that package. HACS does not bundle the PyPI wheel; Home Assistant installs manifest requirements. Protocol and integration compatibility must be kept in lockstep, and attribution/licence metadata must exist in both distributions where applicable.

Home Assistant's current guidance highlights an external Python library for integrations intended for Core ([contributing an integration](https://developers.home-assistant.io/docs/core/integration/contributing_to_core/)), but this project is initially a HACS custom integration. That possible future upstream requirement does not justify a second release surface before reliable protocol behavior and a second consumer exist.

### Decision trigger for later extraction

Revisit a PyPI split only when at least one is true:

1. a non-Home-Assistant consumer is being maintained;
2. the integration is being prepared for Home Assistant Core submission;
3. LS50W or another model is added and the protocol package gains a stable, genuinely reusable public API;
4. protocol maintainers need a release cadence independent of the integration.

Before extraction, require stable typed API/models, fake-server coverage, semver policy, independent package CI, an owner, and a migration plan that does not leave both vendored and installed copies active.

## Licensing and attribution decision

This repository is MIT. Recommended rules:

- Implement the reliability architecture independently from observed behavior and protocol facts.
- If source/mappings are derived from `basnijholt/aiokef`, retain its MIT copyright and permission notice in `THIRD_PARTY_NOTICES` (and a concise source header where substantial code is copied). MIT permits reuse but requires the notice in copies or substantial portions.
- Evotec's MIT code is legally reusable with its notice, but its fragile legacy implementation should be treated as a test oracle/adversarial reference, not copied.
- Do not copy N0ciple/Daniel integration source unless intentionally complying with Apache-2.0 redistribution, retained notices, and prominent modified-file notices. There is no technical reason to do so.
- Home Assistant Core source is Apache-2.0; use its public API conventions, but avoid copying implementation bodies into an MIT-only file without preserving Apache obligations.
- Do not copy code/assets from unlicensed repositories. Do not assume a public GitHub repository or a KEF logo grants reuse rights.

This is a source-provenance recommendation, not legal advice.

## Concrete maintainability decision

For the initial release:

1. Vendor a small LSX-gen1-only async client under `custom_components/kef_lsx`.
2. Keep its public surface minimal: connect/close, state/source, direct set source/wake, power off, volume, mute, and verified transport controls.
3. Use typed errors for connection refused/timeout, response timeout, reset, malformed frame, and rejected SET.
4. Put all socket mutation behind one worker/ownership boundary; do not model both modern and legacy backends yet.
5. Test the client against a deterministic TCP server, not method mocks.
6. Derive UX conventions but not source from current W2 integrations.
7. Ship MIT attribution for any `aiokef`-derived protocol mapping.
8. Leave modern KEF Connect support explicitly out of scope. Its API, dependencies, discovery, authentication, and firmware behavior belong in a separate future decision.

This is the smallest dependency and failure surface that still permits production-quality reliability work.

## Checks actually performed

These are research checks, not validation of this repository's implementation:

```text
git ls-remote N0ciple/hass-kef-connector main
dc7218e6c09278c627da8ae315eaea570e4d2e0f

git ls-remote N0ciple/pykefcontrol main
f5d530c227976d527f2288d29409e42978853bba

git ls-remote danielpetrovic/hass-kef-connector main
19e8cbe9e33158fb7fea573996cbb0c20ac2f450

git ls-remote EvotecIT/homeassistant-kef main
0479463c4ee0fabb7a7b85d4a62dffcf84e3f861

GitHub repository metadata at the snapshot:
N0ciple/hass-kef-connector: Apache-2.0, not archived
N0ciple/pykefcontrol: MIT, not archived
danielpetrovic/hass-kef-connector: Apache-2.0, not archived
EvotecIT/homeassistant-kef: MIT, not archived

HACS default list search:
EvotecIT/homeassistant-kef
N0ciple/hass-kef-connector

Repository file/test scan:
N0ciple/hass-kef-connector: 0 test files
danielpetrovic/hass-kef-connector: 0 test files
EvotecIT/homeassistant-kef: 8 test files; no asyncio.start_server/fake legacy TCP server found
```

No competing source was copied into the implementation, no live Home Assistant or speaker was accessed, and this workstream changed only this report.
