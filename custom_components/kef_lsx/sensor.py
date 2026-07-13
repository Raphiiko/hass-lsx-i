"""Small diagnostic surface for KEF LSX communication health."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from . import KefLsxConfigEntry
from .entity import KefLsxEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class KefLsxSensorDescription(SensorEntityDescription):
    """Describe one intentionally small diagnostic sensor."""

    kind: str


DESCRIPTIONS = (
    KefLsxSensorDescription(
        key="communication_status",
        translation_key="communication_status",
        kind="status",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    KefLsxSensorDescription(
        key="last_successful_communication",
        translation_key="last_successful_communication",
        kind="last_success",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    KefLsxSensorDescription(
        key="consecutive_failures",
        translation_key="consecutive_failures",
        kind="failures",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    KefLsxSensorDescription(
        key="last_command_result",
        translation_key="last_command_result",
        kind="command",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KefLsxConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(KefLsxSensor(entry, description) for description in DESCRIPTIONS)


class KefLsxSensor(KefLsxEntity, SensorEntity):
    """A derived view of the atomic health snapshot."""

    entity_description: KefLsxSensorDescription

    def __init__(
        self, entry: KefLsxConfigEntry, description: KefLsxSensorDescription
    ) -> None:
        super().__init__(entry)
        self.entity_description = description
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_{description.key}"

    @property
    def native_value(self) -> StateType | datetime:
        snapshot = self.coordinator.data
        health = snapshot.health
        match self.entity_description.kind:
            case "status":
                return (
                    "unavailable"
                    if not health.available
                    else "degraded"
                    if health.degraded
                    else "healthy"
                )
            case "last_success":
                return health.last_channel_success
            case "failures":
                return health.consecutive_failures
            case "command":
                result = snapshot.last_command
                if result is None:
                    return None
                return (
                    "acknowledged" if result.acknowledged else result.error or "pending"
                )
        return None

    @property
    def available(self) -> bool:
        """Remain visible so communication outages can be explained."""
        return self.coordinator.data is not None
