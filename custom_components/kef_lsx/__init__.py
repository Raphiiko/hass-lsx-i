"""KEF LSX integration lifecycle."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import PLATFORMS

if TYPE_CHECKING:
    from .runtime import KefLsxRuntime

type KefLsxConfigEntry = ConfigEntry["KefLsxRuntime"]


async def _async_create_runtime(
    hass: HomeAssistant, entry: KefLsxConfigEntry
) -> KefLsxRuntime:
    """Create the fully started runtime through its owned module boundary."""
    from .runtime import async_create_runtime

    return await async_create_runtime(hass, entry)


async def async_setup_entry(hass: HomeAssistant, entry: KefLsxConfigEntry) -> bool:
    """Set up KEF LSX from a config entry."""
    runtime = await _async_create_runtime(hass, entry)
    entry.runtime_data = runtime

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await runtime.async_close()
        raise

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: KefLsxConfigEntry) -> bool:
    """Unload a KEF LSX config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False

    await entry.runtime_data.async_close()
    return True


async def _async_update_listener(hass: HomeAssistant, entry: KefLsxConfigEntry) -> None:
    """Reload the entry after options change."""
    await hass.config_entries.async_reload(entry.entry_id)
