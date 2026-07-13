"""Tests for the KEF LSX configuration flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant import config_entries, data_entry_flow
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kef_lsx.config_flow import CannotConnect
from custom_components.kef_lsx.const import (
    CONF_INVERSE_ORIENTATION,
    CONF_MAX_VOLUME,
    CONF_PREFERRED_WAKE_SOURCE,
    CONF_STANDBY_TIME,
    CONF_VOLUME_STEP,
    DEFAULT_PORT,
    DOMAIN,
)

VALID_ENDPOINT = {CONF_HOST: "speaker.local", CONF_PORT: DEFAULT_PORT}


async def test_user_can_create_entry_with_normalized_endpoint(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> None:
    """A reachable speaker creates one normalized config entry."""
    with (
        patch(
            "custom_components.kef_lsx.config_flow._async_validate_endpoint",
            new=AsyncMock(),
        ) as validate,
        patch(
            "custom_components.kef_lsx.async_setup_entry",
            new=AsyncMock(return_value=True),
        ) as setup_entry,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_HOST: "  SPEAKER.Local. ", CONF_PORT: DEFAULT_PORT},
        )
        await hass.async_block_till_done()

    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["title"] == "speaker.local"
    assert result["data"] == {
        CONF_HOST: "speaker.local",
        CONF_PORT: DEFAULT_PORT,
    }
    assert result["result"].unique_id
    assert result["result"].unique_id != "speaker.local"
    validate.assert_awaited_once_with("speaker.local", DEFAULT_PORT)
    setup_entry.assert_awaited_once()


async def test_user_flow_rejects_invalid_host_without_network_access(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> None:
    """URLs and other non-host input are rejected before probing."""
    with patch(
        "custom_components.kef_lsx.config_flow._async_validate_endpoint",
        new=AsyncMock(),
    ) as validate:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={CONF_HOST: "https://speaker.local", CONF_PORT: DEFAULT_PORT},
        )

    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_host"}
    validate.assert_not_awaited()


async def test_user_flow_reports_connection_failure(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> None:
    """A typed client failure keeps the user on the form."""
    with patch(
        "custom_components.kef_lsx.config_flow._async_validate_endpoint",
        new=AsyncMock(side_effect=CannotConnect),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data=VALID_ENDPOINT,
        )

    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_rejects_normalized_duplicate_before_probe(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> None:
    """Endpoint matching prevents duplicate entries without an IP unique ID."""
    entry = MockConfigEntry(domain=DOMAIN, data=VALID_ENDPOINT)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.kef_lsx.config_flow._async_validate_endpoint",
        new=AsyncMock(),
    ) as validate:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={CONF_HOST: "SPEAKER.LOCAL.", CONF_PORT: DEFAULT_PORT},
        )

    assert result["type"] is data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    validate.assert_not_awaited()


async def test_options_flow_stores_user_facing_settings(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> None:
    """The options flow stores only the useful supported settings."""
    entry = MockConfigEntry(domain=DOMAIN, data=VALID_ENDPOINT)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_PREFERRED_WAKE_SOURCE: "opt",
            CONF_MAX_VOLUME: 0.4,
            CONF_VOLUME_STEP: 0.04,
            CONF_INVERSE_ORIENTATION: True,
            CONF_STANDBY_TIME: "60",
        },
    )

    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    assert entry.options == {
        CONF_PREFERRED_WAKE_SOURCE: "opt",
        CONF_MAX_VOLUME: 0.4,
        CONF_VOLUME_STEP: 0.04,
        CONF_INVERSE_ORIENTATION: True,
        CONF_STANDBY_TIME: 60,
    }


async def test_reconfigure_skips_probe_for_unchanged_endpoint(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> None:
    """Opening and submitting unchanged reconfigure data causes no TCP probe."""
    entry = MockConfigEntry(domain=DOMAIN, title="speaker.local", data=VALID_ENDPOINT)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.kef_lsx.config_flow._async_validate_endpoint",
        new=AsyncMock(),
    ) as validate:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": entry.entry_id,
            },
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=VALID_ENDPOINT
        )

    assert result["type"] is data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    validate.assert_not_awaited()


async def test_reconfigure_validates_and_updates_changed_endpoint(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> None:
    """A changed endpoint is validated before replacing config data."""
    entry = MockConfigEntry(domain=DOMAIN, title="speaker.local", data=VALID_ENDPOINT)
    entry.add_to_hass(hass)
    changed = {CONF_HOST: "new-speaker.local", CONF_PORT: 50002}

    with (
        patch(
            "custom_components.kef_lsx.config_flow._async_validate_endpoint",
            new=AsyncMock(),
        ) as validate,
        patch.object(
            hass.config_entries,
            "async_reload",
            new=AsyncMock(return_value=True),
        ) as reload_entry,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": entry.entry_id,
            },
            data=changed,
        )
        await hass.async_block_till_done()

    assert result["type"] is data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == changed
    assert entry.title == "new-speaker.local"
    validate.assert_awaited_once_with("new-speaker.local", 50002)
    reload_entry.assert_awaited_once_with(entry.entry_id)
