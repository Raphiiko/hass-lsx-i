# Current Home Assistant and HACS architecture for KEF LSX

Research snapshot: 2026-07-13. This report covers the Home Assistant/HACS-facing architecture only. Protocol framing, command mappings, and connection-worker details belong to the other research workstreams.

## Executive recommendation

Use the domain **`kef_lsx`** and the integration name **KEF LSX**. It is distinct from Core's built-in `kef` domain, accurately describes the initially supported product, matches this repository, and does not imply support for every product that happens to use a legacy KEF protocol. `kef_legacy` is broader and would create expectations around LS50W and other unverified models.

Implement one config entry per speaker as a local-polling `device` integration. Keep the protocol client and its single-owner command worker below a small Home Assistant runtime object stored in `ConfigEntry.runtime_data`. A `DataUpdateCoordinator` is useful for poll cadence and entity fan-out, but it must not own the socket and its default one-failed-update availability behavior must be adapted to the required hysteresis.

Target and test the version actually declared in `hacs.json`. At this snapshot, the current stable release is **Home Assistant 2026.7.2**, tag commit [`f9122fb`](https://github.com/home-assistant/core/tree/f9122fb28dd30d3833b3b313924befbc82157f97), published 2026-07-10. Its [`pyproject.toml`](https://github.com/home-assistant/core/blob/2026.7.2/pyproject.toml) requires **Python >=3.14.2** and advertises Python 3.14. The current out-of-tree test harness is `pytest-homeassistant-custom-component==0.13.346`, which pins `homeassistant==2026.7.2`. Do not claim compatibility with older Home Assistant releases unless CI actually tests them. If the live investigator finds an older installed release, either test that exact generation and lower the minimum deliberately or require an HA update before deployment.

## Recommended repository and integration shape

```text
custom_components/kef_lsx/
  __init__.py
  binary_sensor.py             # degraded communication, if exposed as an entity
  brand/icon.png               # current custom-integration branding convention
  client.py                    # vendored typed legacy protocol client
  config_flow.py
  const.py
  coordinator.py               # HA poll/health adapter, not socket owner
  diagnostics.py
  entity.py                    # shared device metadata
  manifest.json
  media_player.py
  sensor.py                    # sparse diagnostic sensors
  translations/en.json
  worker.py                    # single serialized protocol owner
hacs.json
tests/
```

Current Home Assistant documentation says a custom integration lives at `custom_components/<domain>` and recommends a separate `coordinator.py` for a coordinator subclass. Since Home Assistant 2026.3, custom integrations can ship local brand images under `brand/`; these take precedence over the brands CDN ([file structure](https://developers.home-assistant.io/docs/creating_integration_file_structure/), [brand images](https://developers.home-assistant.io/docs/core/integration/brand_images/)).

Recommended manifest content:

```json
{
  "codeowners": ["@Raphiiko"],
  "config_flow": true,
  "dependencies": [],
  "documentation": "https://github.com/Raphiiko/hass-lsx-i#readme",
  "domain": "kef_lsx",
  "integration_type": "device",
  "iot_class": "local_polling",
  "issue_tracker": "https://github.com/Raphiiko/hass-lsx-i/issues",
  "name": "KEF LSX",
  "requirements": [],
  "version": "0.1.0b1"
}
```

`version` is required for custom integrations and must be valid AwesomeVersion-compatible SemVer or CalVer. A vendored client means there is no extra `requirements` dependency. Do not add speculative discovery keys. Do not set `single_config_entry`: users may own more than one speaker pair. See the current [manifest reference](https://developers.home-assistant.io/docs/creating_integration_manifest/).

## Config entry and config flow

Follow the current [config-flow contract](https://developers.home-assistant.io/docs/core/integration/config_flow/) and [config-entry lifecycle](https://developers.home-assistant.io/docs/config_entries_index/):

- Store connection-critical data in `entry.data`: normalized host and, only if genuinely variable and supported, port/model. Port 50001 and LSX generation 1 should remain constants for this focused integration.
- Store user preferences in `entry.options`: preferred wake source, maximum volume, volume step, and inverse orientation. A changed host is connection data and belongs in an `async_step_reconfigure`, not an options flow.
- Validate the host with a short, read-only protocol exchange before creating the entry. Map expected connection/protocol errors to translated flow errors; log an unexpected exception and show `unknown`. Do not wake or otherwise control the speaker during validation.
- Prevent duplicates. If the protocol exposes a serial number or MAC address, use that stable immutable value with `await self.async_set_unique_id(...)` followed by `_abort_if_unique_id_configured()`. On future discovery, it can safely update `CONF_HOST`.
- **Do not use an IP address as a config-entry unique ID.** Current HA rules explicitly list IP address, user-changeable hostname, device name, and URL as unacceptable. If legacy LSX exposes no immutable identity, use `_async_abort_entries_match({CONF_HOST: normalized_host})` for duplicate prevention and use the config entry ID as the last-resort entity unique ID. This is an explicit unresolved tension with the requested “stable config-entry unique ID”: inventing a UUID or hashing the IP would fill the field but would not identify the physical speaker or prevent duplicates. Protocol research must first confirm whether a stable device identifier is queryable ([unique ID rules](https://developers.home-assistant.io/docs/core/integration/config_flow/#unique-ids), [duplicate-entry rule](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/unique-config-entry/), [entity unique IDs](https://developers.home-assistant.io/docs/entity_registry_index/#unique-id)).
- Implement reconfigure with `_get_reconfigure_entry()` and `async_update_reload_and_abort(...)`; verify the new host and check it does not collide with another entry.
- Give `config_flow.py` full branch coverage, including unreachable host, timeout, malformed reply, duplicate, unknown exception, successful setup, options, and reconfigure.

For current HA, `ConfigFlow.async_get_options_flow` returns an `OptionsFlow`. `OptionsFlow.config_entry` is available after initialization, so do not pass/store the entry in a custom `__init__`; [`OptionsFlowWithConfigEntry` is explicitly being phased out](https://github.com/home-assistant/core/blob/2026.7.2/homeassistant/config_entries.py). Use a normal `OptionsFlow` and read `self.config_entry` in the step.

## Runtime data, setup, reload, and unload

Define a typed config entry alias and runtime container, for example `type KefLsxConfigEntry = ConfigEntry[KefLsxRuntime]`. Store the client/worker, coordinator, and health snapshot in `entry.runtime_data`; do not use a domain-global `hass.data` dictionary. This follows the current [`runtime_data` rule](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/runtime-data/).

Recommended lifecycle:

1. `async_setup_entry` constructs the runtime and performs one short initial read. A temporary failure raises `ConfigEntryNotReady`; malformed/incompatible configuration raises `ConfigEntryError`. This satisfies the current [test-before-setup rule](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/test-before-setup/).
2. Assign `entry.runtime_data`, register cleanup/update listeners with `entry.async_on_unload(...)`, then `await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)`.
3. An options/reconfigure update listener calls `hass.config_entries.async_reload(entry.entry_id)`.
4. `async_unload_entry` calls `async_unload_platforms`. On successful unload, stop/cancel the worker and verification tasks and await client close. `entry.async_on_unload` accepts async callbacks in 2026.7, and config-entry-created background tasks are cancelled on unload.
5. Return the actual unload result. Merely registering `async_on_unload` is not sufficient under the [unloading rule](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/config-entry-unloading/).

Use the public `entry.async_create_background_task(hass, coroutine, name)` for post-command verification tasks on the current target: its documented source contract ties tasks to the entry lifecycle and cancels them on unload ([2026.7.2 source](https://github.com/home-assistant/core/blob/2026.7.2/homeassistant/config_entries.py)). Avoid private task APIs.

## Coordinator, polling, and availability

The current [data-fetching guidance](https://developers.home-assistant.io/docs/integration_fetching_data/) makes `DataUpdateCoordinator` appropriate when one poll feeds multiple entities. It also states that entity properties must be memory-only and that integrations which manage their own serialization can set `PARALLEL_UPDATES = 0`.

Recommended split:

- The worker is the only socket/client owner and serializes user commands and polls, with command priority.
- The coordinator schedules one low-priority poll and publishes an immutable state/health snapshot. Polls do not call the protocol client directly.
- Set `always_update=False` only if snapshot equality includes health fields; a failed poll changes failure count/stale state and therefore still notifies diagnostic entities.
- In `media_player.py`, set `PARALLEL_UPDATES = 0` because the worker, not Home Assistant's platform semaphore, is the authoritative concurrency boundary.
- Command methods submit to the high-priority worker queue. After a successful SET/ack, publish the updated health snapshot immediately. Schedule bounded verification separately; do not block the action on a full DSP refresh.

Important availability caveat: current [`CoordinatorEntity.available`](https://github.com/home-assistant/core/blob/2026.7.2/homeassistant/helpers/update_coordinator.py) is exactly `coordinator.last_update_success`. If `_async_update_data` raises `UpdateFailed` for every timeout, one failed poll makes the entity unavailable—the behavior this project exists to fix.

Preserve the coordinator convenience while implementing hysteresis as follows:

- On a transient poll failure below the sustained-failure threshold, update the runtime health (`degraded=True`, failure count/time, last known device state retained) and return the snapshot rather than raising `UpdateFailed`.
- Only raise `UpdateFailed` when the sustained availability threshold is crossed. This then makes `CoordinatorEntity.available` false through the standard path and gives standard unavailable/recovery logging behavior.
- On the next successful poll **or successful command exchange**, call `coordinator.async_set_updated_data(snapshot)` so availability and listeners recover immediately.
- Do not override `available` to ignore the coordinator unless tests demonstrate the soft-failure approach is insufficient. The quality rule advises custom availability logic to incorporate `super().available` ([entity unavailable](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/entity-unavailable/)).

This interpretation is consistent with the base entity contract: `available` means Home Assistant can read **or control** the device, not that the most recent background read was perfect ([Entity](https://developers.home-assistant.io/docs/core/entity/)). HA's media-player guidance also says a player that HA can wake should be represented as off rather than unavailable while in standby ([Media player](https://developers.home-assistant.io/docs/core/entity/media-player/)).

## Media player and device/entity metadata

Subclass `MediaPlayerEntity` (and normally `CoordinatorEntity`). Properties return only cached snapshot values. Use current enums: `MediaPlayerState`, `MediaPlayerDeviceClass.SPEAKER`, and bitwise `MediaPlayerEntityFeature` flags. Initial flags should include only implemented behavior: `TURN_ON`, `TURN_OFF`, `SELECT_SOURCE`, `VOLUME_SET`, `VOLUME_STEP`, and `VOLUME_MUTE`. Add `PLAY`, `PAUSE`, `NEXT_TRACK`, and `PREVIOUS_TRACK` only when protocol behavior is verified and tests cover it.

Set `_attr_has_entity_name = True` and `_attr_name = None` for the primary media player. Give every entity a stable unique ID. Prefer a protocol-derived device ID; otherwise the config entry ID is HA's documented last resort. Register one device with `DeviceInfo(identifiers={(DOMAIN, device_id)}, manufacturer="KEF", model="LSX", name=entry.title)`. Do not fabricate firmware, serial, MAC, or model details. Current device/entity registry requirements are documented in [Device registry](https://developers.home-assistant.io/docs/device_registry_index/) and [Entity](https://developers.home-assistant.io/docs/core/entity/).

Avoid rapidly changing media-player extra attributes because each state write can increase recorder volume. Prefer these diagnostic entities:

- a low-churn `BinarySensorEntity` with `EntityCategory.DIAGNOSTIC` for degraded/stale communication (reasonable to enable by default);
- timestamp sensor for last successful communication, disabled by default because it changes every poll;
- consecutive-failure sensor, disabled by default;
- last-command-result sensor only if its small, stable vocabulary is useful, disabled by default.

All diagnostic entity names should use translation keys. Less popular/noisy diagnostic entities should set `entity_registry_enabled_default = False`, matching current entity guidance.

## Diagnostics and repairs

Implement `async_get_config_entry_diagnostics(hass, entry)` in `diagnostics.py`. Use `homeassistant.components.diagnostics.async_redact_data` and redact at least host/IP, config-entry/device identifiers, and any future credentials. Return options, non-sensitive protocol/client version information, health counters/timestamps, queue/worker state, and a typed last error category. Do not dump raw packets, exception strings containing host addresses, reader/writer objects, or live HA state. See the current [diagnostics rule](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/diagnostics/).

Repairs are only for actionable user intervention. A transient timeout, degraded status, or ordinary offline speaker is not a repair issue. Use a translated repair only if a user can take a concrete action, such as reconfiguring an invalid host after a migration or resolving an unsupported config schema. Do not create/delete issues on every poll transition. See [repair issues](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/repair-issues/).

## Translations: current custom-integration rule

There is a material 2026 change/conflict to resolve. The current custom-integration-specific documentation, updated 2026-06-15, says:

- custom integrations load files from `custom_components/<domain>/translations/<language>.json`;
- ship `translations/en.json` containing full English text;
- **do not use `strings.json` or Core build-time `[%key:...]` placeholders**, because custom integrations do not run Core's translation build and may show raw keys.

Source: [Custom integration localization](https://developers.home-assistant.io/docs/internationalization/custom_integration/).

That custom-specific rule is more applicable than generic Core examples that still show `strings.json`. Therefore the implementation recommendation is `translations/en.json` as the runtime authority and no placeholder references. The project brief lists both `strings.json` and `translations/en.json`; blindly adding Core-style `strings.json` would contradict the current official custom-component guidance. If the lead keeps `strings.json` solely to satisfy an external validator/template, it must be redundant, contain no placeholder dependency, and tests must prove that a clean custom-component install renders translations from `translations/en.json`.

Include translations for config/reconfigure/options steps, field descriptions, errors/aborts, entity names, exceptions, and any repairs. Run hassfest against the custom component.

## HACS packaging and validation

Current [HACS integration requirements](https://hacs.xyz/docs/publish/integration/) require:

- one integration subdirectory beneath root `custom_components/`;
- all runtime files inside that integration directory;
- a manifest with at least domain, documentation, issue tracker, codeowners, name, and version;
- brand assets (current HA/HACS convention permits `custom_components/kef_lsx/brand/icon.png`);
- public GitHub hosting for normal HACS distribution.

Root `hacs.json` needs at least `name`. Add `homeassistant` equal to the oldest HA version actually tested, for example initially:

```json
{
  "homeassistant": "2026.7.2",
  "name": "KEF LSX"
}
```

Do not set `content_in_root`; this repository uses the standard layout. Do not set `zip_release` unless CI actually creates the named release asset. HACS can install the default branch without releases; if releases are used, HACS derives the remote version from a GitHub **release**, not a bare tag ([general publishing requirements](https://hacs.xyz/docs/publish/start/)). This fits the safety requirement: merge/test without publishing a stable release, then create a pre-release after hardware validation planning.

The GitHub repository must have a description, topics, README, and enabled issues. Run `hacs/action` with `category: integration`; do not ignore checks. HACS says the action runs the same validator as HACS itself ([HACS action](https://hacs.xyz/docs/publish/action/)). For eventual inclusion as a HACS default repository, both HACS validation and hassfest must pass without ignores and a full GitHub release is required ([default inclusion](https://hacs.xyz/docs/publish/include/)). Default inclusion is not required for users to add this as a custom repository.

## Testing and CI conventions

Use pytest and exercise Home Assistant through public interfaces, following current [HA testing guidance](https://developers.home-assistant.io/docs/development_testing/):

- set up via `hass.config_entries.async_setup`, assert config-entry state, entity state via `hass.states`, and actions via `hass.services.async_call(..., blocking=True)`;
- use `MockConfigEntry` from `pytest_homeassistant_custom_component.common` for the out-of-tree harness and enable the custom integration with its fixture;
- assert device/entity registry results through their registries;
- test setup, reload, and unload in `test_init.py`-style coverage;
- use snapshots for broad diagnostics/config-flow structures only alongside explicit behavioral assertions; a snapshot is not a substitute for asserting availability transitions or redaction;
- use the real deterministic fake TCP server for command-after-failed-poll and recovery tests. Mock only Home Assistant boundaries where necessary.

At this snapshot, [PyPI metadata](https://pypi.org/project/pytest-homeassistant-custom-component/) reports `pytest-homeassistant-custom-component==0.13.346`, Python >=3.14, and `homeassistant==2026.7.2`. Pin it for reproducibility, set pytest asyncio mode as required by that package, and update deliberately.

Minimum CI jobs:

1. Python 3.14.2+ test job: pytest with coverage, deterministic fake server, setup/unload tests.
2. Ruff format/check and configured type checker.
3. HACS validator with `category: integration`.
4. `home-assistant/actions/hassfest` custom-component validator.
5. Optional scheduled jobs against moving HACS/hassfest validators to catch future incompatibility; keep PR jobs reproducible by pinning reviewed action SHAs or tags and use dependency automation.

Home Assistant's current quality scale is written for Core but is a useful quality target for a HACS integration. At minimum apply Bronze and Silver rules relevant to a local unauthenticated device; target Gold diagnostics/docs/entity metadata. Reauthentication is not applicable because this protocol has no credentials. Discovery/dynamic/stale-device rules can be exempt when the protocol offers no safe discovery. Do not claim a formal Core quality tier for an out-of-tree custom integration ([quality scale rules](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/)).

## Decisions and open uncertainties

| Topic | Recommendation | Remaining uncertainty |
|---|---|---|
| Domain | `kef_lsx` | None; reserve `kef_legacy` for a genuinely multi-product integration. |
| HA baseline | Develop/test on 2026.7.2 / Python 3.14.2 | Live investigator must report installed HA; lower only with an explicit test matrix. |
| Config unique ID | Protocol serial/MAC if available | Protocol research must establish whether LSX gen 1 exposes one. Never use IP as `unique_id`. |
| Duplicate fallback | Normalized host match | A host change requires explicit reconfigure and cannot automatically prove physical identity. |
| Polling abstraction | Coordinator above a priority worker | Reliability workstream chooses exact hysteresis/timeouts. |
| Availability | Soft poll failures update health without `UpdateFailed`; sustained failures raise; any successful exchange restores | Confirm coordinator callbacks publish the final failure count at threshold. |
| Translation files | `translations/en.json` is runtime authority | Brief requests `strings.json`, but current official custom docs say not to use it. |
| Repairs | None for ordinary communication failures | Add only when an actionable migration/incompatibility case exists. |
| Brand | Local `brand/icon.png` | Logo must be properly licensed; do not copy KEF marks without permission. |
| Media transport | Expose only verified feature flags | Real hardware validation remains required. |

## Research checks actually performed

These were research/source checks, not implementation validation:

```text
git ls-remote --tags https://github.com/home-assistant/core.git refs/tags/2026.7.2
f9122fb28dd30d3833b3b313924befbc82157f97  refs/tags/2026.7.2

GET https://api.github.com/repos/home-assistant/core/releases/tags/2026.7.2
tag=2026.7.2, published=2026-07-10T21:15:27Z, prerelease=false

GET https://raw.githubusercontent.com/home-assistant/core/2026.7.2/pyproject.toml
requires-python = ">=3.14.2"
classifier: Programming Language :: Python :: 3.14

GET https://pypi.org/pypi/pytest-homeassistant-custom-component/json
version=0.13.346
requires_python=>=3.14
requires_dist includes homeassistant==2026.7.2

git ls-remote https://github.com/hacs/action.git refs/heads/main
1ebf01c408f29afcb6406bd431bc98fd8cbb15aa  refs/heads/main

git ls-remote https://github.com/home-assistant/actions.git refs/heads/master
f4ca6f671bd429efb108c0f2fa0ae8af0215986c  refs/heads/master
```

Official documentation pages were accessed on 2026-07-13. They are rolling documents, so the commit/tag links above are the reproducible reference for exact 2026.7.2 API behavior. No Home Assistant instance was modified, no speaker command was sent, and no repository file other than this report was changed by this workstream.
