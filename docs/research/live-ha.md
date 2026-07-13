# Live Home Assistant investigation (read-only)

## Safety and scope

Snapshot taken 2026-07-13 around 11:55 Europe/Amsterdam using only read-only
Home Assistant MCP calls. No service was called; no entity, automation,
integration, dashboard, YAML, or registry entry was changed; nothing was
reloaded, restarted, enabled, disabled, installed, or removed. The speakers were
not controlled.

The MCP responses contained a private speaker address, a hardware-derived unique
identifier, and a webhook identifier. They are deliberately excluded from this
report. The private address is represented as `<speaker-host>`. Unrelated network
and Home Assistant configuration returned incidentally by system health is also
excluded.

Before inspecting automation configuration, the
`home-assistant-best-practices` MCP skill and only its applicable
`references/safe-refactoring.md` reference were read. Its relevant rule is to
find every consumer before changing an entity ID and verify no stale references
afterward.

## Executive findings

- The live system runs Home Assistant Core **2026.7.2** on Home Assistant OS,
  Python **3.14.6**, architecture `aarch64`, timezone Europe/Amsterdam.
- `media_player.kef` existed and was **unavailable** at inspection time. Its
  friendly name is **Desk Speakers** and its advertised sources are `Wifi`,
  `Bluetooth`, `Aux`, and `Opt`.
- Registry metadata identifies platform `kef`, with **no config entry** and **no
  HA device**. This is consistent with the stated legacy YAML platform. The MCP
  config-entry lookup for domain `kef` returned zero entries.
- The live entity metadata does not expose a model name or installed `aiokef`
  version. Therefore the exact LSX-generation and `aiokef==0.2.16` claims were
  **not independently confirmed through live MCP metadata**. The KEF source set
  is compatible with the stated setup but is not sufficient model proof.
- KEF-specific live logs contain `<speaker-host>: Timeout in waiting for reply`.
  The redacted address matched the host supplied by the user, confirming that
  the inspected integration is pointed at the expected speaker without storing
  that address here.
- Ten-day state history confirms frequent short unavailability, with a median of
  **28.783 seconds**. This independently reproduces the supplied approximately
  28.9-second median.
- Live logs confirm both halves of the operational failure: reply timeouts make
  polling overrun, and HA reports service targets as missing/not currently
  available. A recorded automation exception also shows the legacy `turn_off`
  path calling `get_state()` before attempting the SET.
- Both named automations directly target `media_player.kef`. Their wake sequence
  is `media_player.turn_on`, a two-second delay, then source `Opt`. Neither KEF
  sequence continues to source selection if the preceding turn-on action raises.

## Entity and registry snapshot

| Property | Observed value |
|---|---|
| Entity ID | `media_player.kef` |
| Friendly/original name | `Desk Speakers` |
| State | `unavailable` |
| State changed | 2026-07-13 11:53:38.881678 +02:00 |
| Sources | `Wifi`, `Bluetooth`, `Aux`, `Opt` |
| Platform | `kef` |
| Registry enabled/hidden | enabled; not hidden |
| Config entry | none |
| Associated HA device | none |
| Area | none |
| Unique ID | present, but hardware-derived and redacted |

Volume, current source, and mute attributes were absent while the entity was
unavailable. The entity has no device page or config-entry options to migrate.
The new integration must create its own device/config entry and stable unique ID.

## Ten-day availability history

The recorder history endpoint was queried chronologically with significant state
changes only and paginated at 1,000 records. The two pages contained 1,518 state
records and 745 `unavailable` episodes over the rolling ten-day window ending at
approximately 11:55 local time. The final episode was still open at the end of
the sample.

Durations were computed from each transition into `unavailable` to the next
state transition (or the query end for the open final episode):

| Metric | Result |
|---|---:|
| Unavailable episodes | 745 |
| Median | 28.783 s |
| 90th percentile | 118.255 s |
| 95th percentile | 178.273 s |
| Longest | 718.389 s (about 12 minutes) |
| Episodes between 20 and 40 seconds | 547 (73.423%) |
| Episodes at least 120 seconds | 63 |
| Episodes at least 300 seconds | 10 |

This pattern strongly supports a 30-second polling interaction: most failures
clear around one polling period, with a smaller but material long tail. It also
supports hysteresis longer than a single failed poll and a separate stale health
signal.

The logbook endpoint returned only 19 entries for the same nominal ten-day
window, despite reporting no next page. It was not used for duration statistics;
the paginated recorder-history result is the complete source used above.

## KEF-specific log evidence

The current structured system-log window contained eight aggregated
`aiokef.aiokef` timeout records, split by Core `kef` call site. Their occurrence
counts totalled **135** occurrences of `Timeout in waiting for reply` from the
earliest recorded occurrence at 2026-07-13 01:17 local through 11:53 local.
These are aggregated occurrences, not 135 distinct system-log rows.

The broader `kef` filter also showed:

- `Updating kef media_player took longer than the scheduled update interval
  0:00:30`, aggregated count 16 in the current log window;
- `Update of media_player.kef is taking over 10 seconds`; its system-log row was
  grouped with a warning for an unrelated entity, so the row's aggregate count
  cannot safely be attributed solely to KEF;
- `Referenced entities media_player.kef are missing or not currently available`,
  aggregated count 4;
- one `PC audio mode applied` automation failure represented by two adjacent
  aggregated script-error messages.

The automation exception chain is especially relevant. It shows:

1. an `aiokef` connection open timeout being translated to
   `ConnectionRefusedError("Speaker is offline.")`;
2. nested Tenacity `RetryError` wrappers;
3. HA's built-in `kef` `async_turn_off` calling `aiokef.turn_off()`;
4. `aiokef.turn_off()` calling `get_state()` before completing the requested
   control.

Thus the live evidence is not limited to inaccurate status: an explicit action
failed through a prerequisite read/retry chain. A separate HA service warning
confirms actions are also refused when the entity is unavailable.

The raw `home-assistant.log` text search for the timeout phrase returned zero
lines, while structured system-log search returned the aggregated errors above.
That is a source-retention/representation difference, not evidence that the
timeouts did not occur.

## Automation consumers

### `automation.pc_audio_mode_applied`

Observed configuration and state:

- enabled, `mode: restart`, idle during inspection;
- last triggered 2026-07-13 11:23:39.953007 +02:00;
- locally triggered by a webhook (identifier intentionally omitted);
- when the selected audio mode is `speaker`, calls
  `media_player.turn_on` on `media_player.kef`, waits two seconds, then calls
  `media_player.select_source` with `Opt`;
- for other modes, calls `media_player.turn_off` on `media_player.kef`.

There is no `continue_on_error` around the KEF turn-on action. If HA refuses the
unavailable target or the integration raises, the two-second delay and `Opt`
selection do not run.

### `automation.pc_power_controls_monitors_and_kef_speakers`

Observed configuration and state:

- enabled, `mode: restart`, idle during inspection;
- last triggered 2026-07-02 10:05:43.757167 +02:00;
- on its PC-on branch, starts monitor control and a KEF sequence in parallel;
- the KEF sequence calls `media_player.turn_on` on `media_player.kef`, waits two
  seconds, then selects `Opt`;
- its PC-off branch turns monitors and `media_player.kef` off in parallel.

Again, the KEF sequence has no continuation behavior after a failed turn-on, so
source selection depends on successful completion of the first service action.
The monitor entity IDs are unrelated to this integration and are intentionally
not reproduced here.

### Reference-search coverage

A cross-configuration search for the literal `media_player.kef` found exactly
the two automations above among the configurations it could inspect. It found no
matching scripts, scenes, helpers, or dashboards among those scanned. However,
the MCP explicitly marked the result **partial**: 16 YAML-defined automations
could not be scanned through the per-ID config endpoint, and one dashboard could
not be fetched. Therefore this is not proof that there are only two consumers.

Before any entity rename or removal, deployment must repeat an exhaustive search
across all automations, scripts, scenes, dashboards, groups, config-entry data,
and any external automation systems. The two confirmed automations form the
minimum migration set.

## Migration constraints and safety implications

1. **Never run both integrations against the speaker.** The old YAML platform
   must stop owning its TCP connection before the custom integration performs a
   setup probe or control.
2. **Preserve `media_player.kef` if practical.** Because both confirmed
   automations use that entity ID, assigning the new entity that ID after the old
   entity is fully removed minimizes the atomic migration. A new integration
   domain does not prevent the media-player entity from using this object ID.
3. **Account for the old registry entry.** The legacy entity has a registry
   unique ID even though it has no config entry/device. Back up the relevant
   configuration and record registry state before removing YAML; verify the old
   entity no longer occupies `media_player.kef` before renaming the new entity.
4. **If the entity ID changes, update every consumer atomically.** At minimum,
   update the two confirmed automations in the same maintenance window. The
   partial MCP search requires a broader old-ID search before the change and a
   zero-stale-reference check afterward.
5. **Keep the wake behavior compatible.** Both automations expect direct wake
   followed by `Opt` two seconds later. The custom integration's configured wake
   source should be `Opt`; direct wake must not depend on a successful read.
6. **Do not require an automation workaround for transient availability.** The
   live history and service warnings confirm that the integration must keep the
   entity controllable through a one-poll failure, rather than asking users to
   add retries around blocked HA actions.
7. **Plan a rollback.** Preserve the YAML stanza and automation configurations in
   a backup. Rollback is: stop/unload the custom integration, restore the YAML
   platform and original entity ID, restore any changed references, then perform
   only the minimum HA reload/restart required by that platform.
8. **Real-hardware testing still needs approval.** No live command was issued in
   this investigation. Direct wake, `Opt`, off, volume, post-failure recovery,
   and simultaneous-connection exclusion remain to be validated in a controlled
   deployment window.

## Claims not verified by the live connector

- The UI/registry did not expose the speaker model, so “original LSX / generation
  1” remains a user-supplied hardware fact to verify during approved hardware
  testing or from a non-sensitive device label.
- The connector did not expose installed Python package versions, so
  `aiokef==0.2.16` was not verified live here. It can be corroborated separately
  from the exact Core 2026.7.2 `kef` manifest/dependency resolution.
- Wi-Fi association and signal quality are not available through the Home
  Assistant connector. The UniFi claim was not independently checked.
- No attempt was made to inspect or reproduce private webhook contents, external
  PC-side software, or unrelated configuration.

## Read-only MCP call ledger

Every live-system call made in this investigation is listed below. Repeated
history/log calls were used to paginate or reduce large raw results into safe
aggregates; they are recorded rather than hidden.

| # | Read-only call | Result |
|---:|---|---|
| 1 | `ha_get_skill_guide(skill=home-assistant-best-practices, file=SKILL.md)` | Success. |
| 2 | `ha_get_skill_guide(..., file=references/safe-refactoring.md)` | Success. |
| 3 | `ha_get_system_health(include=system)` | Success, but warned that unknown include value `system` was ignored; default health still returned Core/OS/Python metadata. |
| 4 | `ha_get_state(media_player.kef, projected fields/attributes)` | Success. |
| 5 | `ha_get_entity(media_player.kef)` | Success; hardware unique ID redacted from this report. |
| 6 | `ha_get_device(entity_id=media_player.kef)` | Failed with `ENTITY_NOT_FOUND` / no associated device. This is expected for the observed YAML entity. |
| 7 | `ha_get_integration(domain=kef, exact_match=true)` | Success; zero config entries. |
| 8 | `ha_config_get_automation(automation.pc_audio_mode_applied)` | Success; webhook identifier omitted from this report. |
| 9 | `ha_config_get_automation(automation.pc_power_controls_monitors_and_kef_speakers)` | Success; unrelated entity IDs omitted. |
| 10 | `ha_get_history(media_player.kef, 10d, offset=0, limit=1000, asc)` | Success; first page, more data indicated. |
| 11 | `ha_get_logs(source=system, search=aiokef, limit=100)` | Success. |
| 12 | `ha_get_logs(source=system, search=kef, limit=100)` | Success. |
| 13 | `ha_get_logs(source=error_log, search="Timeout in waiting for reply", limit=100)` | Success; zero raw-text matches. |
| 14 | `ha_get_logs(source=logbook, entity_id=media_player.kef, hours_back=240)` | Success; 19 entries, no next page reported; not used for metrics. |
| 15 | `ha_search(query=media_player.kef, automation/script/scene/helper/dashboard)` | Success but explicitly partial; two automation matches, 16 automations and one dashboard unscanned. |
| 16 | Repeat history page `offset=0` | Success; used to inspect pagination and initial aggregate. |
| 17 | History page `offset=1000` | Success; 518 records, end of result. |
| 18 | Repeat history page `offset=0` | Success; paired with the next call for one final aggregate. |
| 19 | Repeat history page `offset=1000` | Success; paired final aggregate, no more pages. |
| 20 | Repeat system-log query `search=aiokef` | Success; reduced to redacted counts/times. |
| 21 | Repeat system-log query `search=kef` | Success; reduced to redacted counts/times. |
| 22 | `ha_get_state` for both named automation entities with projected attributes | Success. |

No write-capable MCP tool was called.
