"""Home Assistant polling facade over the single-owner runtime."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .models import RuntimeSnapshot
from .runtime import KefLsxRuntime


class KefLsxCoordinator(DataUpdateCoordinator[RuntimeSnapshot]):
    """Trigger coalesced polls and fan out runtime publications."""

    def __init__(self, hass: HomeAssistant, runtime: KefLsxRuntime) -> None:
        super().__init__(
            hass,
            logger=__import__("logging").getLogger(__name__),
            name="KEF LSX",
            update_interval=timedelta(seconds=30),
            always_update=False,
        )
        self.runtime = runtime

    async def _async_update_data(self) -> RuntimeSnapshot:
        """Request one coalesced, non-overlapping runtime poll."""
        return await self.runtime.async_poll()
