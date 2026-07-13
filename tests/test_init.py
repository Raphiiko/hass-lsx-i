"""Tests for KEF LSX config-entry lifecycle."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kef_lsx import async_setup_entry, async_unload_entry
from custom_components.kef_lsx.const import DEFAULT_PORT, DOMAIN, PLATFORMS


def _entry() -> MockConfigEntry:
    """Return a config entry for lifecycle tests."""
    return MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: "speaker.local", CONF_PORT: DEFAULT_PORT},
    )


async def test_setup_stores_started_runtime_and_forwards_platforms(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> None:
    """A fully started runtime is stored before entity setup."""
    entry = _entry()
    entry.add_to_hass(hass)
    runtime = MagicMock()
    runtime.async_close = AsyncMock()

    with (
        patch(
            "custom_components.kef_lsx._async_create_runtime",
            new=AsyncMock(return_value=runtime),
        ) as create_runtime,
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new=AsyncMock(return_value=True),
        ) as forward,
    ):
        assert await async_setup_entry(hass, entry)

    assert entry.runtime_data is runtime
    create_runtime.assert_awaited_once_with(hass, entry)
    forward.assert_awaited_once_with(entry, PLATFORMS)


async def test_setup_closes_runtime_if_platform_setup_fails(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> None:
    """A partial setup cannot leave the communication owner running."""
    entry = _entry()
    entry.add_to_hass(hass)
    runtime = MagicMock()
    runtime.async_close = AsyncMock()

    with (
        patch(
            "custom_components.kef_lsx._async_create_runtime",
            new=AsyncMock(return_value=runtime),
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new=AsyncMock(side_effect=RuntimeError("platform failed")),
        ),
        pytest.raises(RuntimeError, match="platform failed"),
    ):
        await async_setup_entry(hass, entry)

    runtime.async_close.assert_awaited_once_with()


async def test_successful_unload_closes_runtime_after_platforms(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> None:
    """Unload waits for platforms and then closes the sole runtime owner."""
    entry = _entry()
    entry.add_to_hass(hass)
    runtime = MagicMock()
    runtime.async_close = AsyncMock()
    entry.runtime_data = runtime

    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        new=AsyncMock(return_value=True),
    ) as unload_platforms:
        assert await async_unload_entry(hass, entry)

    unload_platforms.assert_awaited_once_with(entry, PLATFORMS)
    runtime.async_close.assert_awaited_once_with()


async def test_failed_platform_unload_keeps_runtime_running(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> None:
    """A runtime remains usable when HA refuses to unload its entities."""
    entry = _entry()
    entry.add_to_hass(hass)
    runtime = MagicMock()
    runtime.async_close = AsyncMock()
    entry.runtime_data = runtime

    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        new=AsyncMock(return_value=False),
    ):
        assert not await async_unload_entry(hass, entry)

    runtime.async_close.assert_not_awaited()
