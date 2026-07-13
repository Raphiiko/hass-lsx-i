# Migration from Home Assistant's built-in `kef` platform

The old and new integrations must never control the same speaker simultaneously.
Even running the new config flow performs a read-only TCP probe, so do not begin
configuration while the YAML platform is still polling.

No live migration should be attempted without explicit approval and a maintenance
window. Repository installation alone is safe; configuration is not started
automatically.

## Before the maintenance window

1. Create a Home Assistant backup.
2. Save the existing `kef` YAML stanza and the entity-registry state for
   `media_player.kef` outside the repository.
3. Export or back up every automation, script, scene, dashboard, group, and
   external flow that references `media_player.kef`.
4. At minimum, verify these known consumers:
   - `automation.pc_audio_mode_applied`
   - `automation.pc_power_controls_monitors_and_kef_speakers`
5. Search all consumers, not only the two known automations. The live MCP search
   was partial and could not inspect every YAML automation/dashboard.
6. Record the old entity name and current automation behavior. Both known wake
   paths call turn-on, wait two seconds, then select `Opt`.

Do not copy private addresses, webhook IDs, tokens, or unrelated configuration
into an issue or repository file.

## Cutover

1. Install the custom integration files through HACS, but do not add/configure
   the integration yet.
2. Stop Home Assistant's old YAML `kef` platform by removing/commenting its
   backed-up configuration and performing the minimum supported reload/restart.
3. Confirm `media_player.kef` is no longer polling. Check logs for the absence of
   new built-in `kef`/`aiokef` activity before proceeding.
4. Add **KEF LSX** from Settings → Devices & services. Configure the preferred
   wake source as `Opt` for the known target setup.
5. Prefer renaming the new media-player entity to `media_player.kef` after the old
   registry entry no longer occupies that ID. If that is not possible, update all
   consumers to the new entity ID atomically.
6. Search for the old identifier again and expect zero stale references (unless
   the preserved ID makes old and new identical). Verify both known automations.

## Approved hardware validation checklist

After explicit approval and only while the old integration remains stopped:

1. Confirm one normal poll and healthy diagnostics.
2. Test direct `Opt` wake, including after a deliberately injected/transient
   failed poll.
3. Test source selection, power off, absolute volume, volume step, mute/unmute,
   and maximum-volume clamping.
4. Confirm the old and new integrations never open concurrent connections.
5. Inspect Home Assistant logs, entity history, degraded status, and immediate
   recovery after a successful exchange.
6. Verify both PC automations without changing unrelated actions or triggers.

## Rollback

1. Unload/remove the custom integration so its worker and socket are closed.
2. Restore the backed-up YAML `kef` stanza.
3. Restore `media_player.kef` naming and all changed references atomically.
4. Perform the minimum required HA reload/restart.
5. Confirm the built-in entity and automations are restored.

Do not leave the custom config entry active while restoring the built-in owner.

