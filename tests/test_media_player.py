"""Media player state projection tests."""

from __future__ import annotations

from types import SimpleNamespace

from fake_lsx import DropReply, Step
from homeassistant.const import ATTR_ENTITY_ID, CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kef_lsx.const import DOMAIN
from custom_components.kef_lsx.media_player import KefLsxMediaPlayer
from custom_components.kef_lsx.models import RuntimeSnapshot, SpeakerState
from custom_components.kef_lsx.protocol import Source

GET_SOURCE = bytes((0x47, 0x30, 0x80))
GET_VOLUME = bytes((0x47, 0x25, 0x80))
WAKE_OPT = bytes((0x53, 0x30, 0x81, 0x2B))


def test_media_player_projects_cached_state_while_degraded() -> None:
    coordinator = SimpleNamespace(
        data=RuntimeSnapshot(speaker=SpeakerState(True, Source.OPT, 25, False))
    )
    entity = object.__new__(KefLsxMediaPlayer)
    entity.coordinator = coordinator
    assert entity.source == "Opt"
    assert entity.volume_level == 0.25
    assert not entity.is_volume_muted


async def test_full_ha_service_direct_wake_after_dropped_poll(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    socket_enabled: None,
    fake_lsx_server,
) -> None:
    """Set up through HA, fail a poll, control through the service, then unload."""
    server = fake_lsx_server
    server.queue(
        Step(GET_SOURCE, label="initial setup"),
        Step(GET_VOLUME, label="initial sparse volume maintenance"),
        Step(GET_SOURCE, DropReply(), "dropped coordinator poll"),
        Step(WAKE_OPT, label="direct wake service"),
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Desk Speakers",
        data={CONF_HOST: server.host, CONF_PORT: server.port},
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await server.wait_for_commands(2)
    registry_entries = er.async_entries_for_config_entry(
        er.async_get(hass), entry.entry_id
    )
    media_entry = next(
        item for item in registry_entries if item.domain == "media_player"
    )
    entity_id = media_entry.entity_id
    assert (state := hass.states.get(entity_id)) is not None
    assert state.state == "on"

    await entry.runtime_data.coordinator.async_refresh()
    assert (state := hass.states.get(entity_id)) is not None
    assert state.state != "unavailable"
    assert entry.runtime_data.snapshot.health.consecutive_failures == 1

    await hass.services.async_call(
        "media_player",
        "turn_on",
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    assert server.commands[-1].raw == WAKE_OPT
    assert entry.runtime_data.snapshot.last_command is not None
    assert entry.runtime_data.snapshot.last_command.acknowledged

    runtime = entry.runtime_data
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert runtime._worker is None
    assert runtime.queue_size == 0
    assert server._open_connection_count == 0
    server.assert_clean()
