"""Shared KEF LSX entity metadata."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import KefLsxConfigEntry
from .const import DOMAIN
from .coordinator import KefLsxCoordinator


class KefLsxEntity(CoordinatorEntity[KefLsxCoordinator]):
    """Base entity backed by an atomic runtime snapshot."""

    _attr_has_entity_name = True

    def __init__(self, entry: KefLsxConfigEntry) -> None:
        super().__init__(entry.runtime_data.coordinator)
        self.entry = entry
        self.runtime = entry.runtime_data
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
            name=entry.title or "KEF LSX",
            manufacturer="KEF",
            model="LSX (first generation)",
        )

    @property
    def available(self) -> bool:
        """Use runtime hysteresis, not the coordinator's last-poll flag."""
        return super().available and self.coordinator.data.health.available
