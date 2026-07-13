"""Media-player controls for first-generation KEF LSX speakers."""

from __future__ import annotations

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import KefLsxConfigEntry
from .const import DOMAIN
from .entity import KefLsxEntity
from .errors import LsxError
from .protocol import Source

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KefLsxConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the speaker entity."""
    async_add_entities([KefLsxMediaPlayer(entry)])


class KefLsxMediaPlayer(KefLsxEntity, MediaPlayerEntity):
    """Reliable local LSX media player."""

    _attr_name = None
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_supported_features = (
        MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.TURN_OFF
        | MediaPlayerEntityFeature.SELECT_SOURCE
        | MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.VOLUME_MUTE
    )

    def __init__(self, entry: KefLsxConfigEntry) -> None:
        super().__init__(entry)
        self._attr_source_list = [source.value for source in Source]
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_media_player"

    @property
    def state(self) -> MediaPlayerState | None:
        power = self.coordinator.data.speaker.power_on
        if power is None:
            return None
        return MediaPlayerState.ON if power else MediaPlayerState.OFF

    @property
    def source(self) -> str | None:
        source = self.coordinator.data.speaker.source
        return source.value if source else None

    @property
    def volume_level(self) -> float | None:
        volume = self.coordinator.data.speaker.volume
        return volume / 100 if volume is not None else None

    @property
    def is_volume_muted(self) -> bool | None:
        return self.coordinator.data.speaker.muted

    async def async_turn_on(self) -> None:
        await self._call(self.runtime.async_turn_on())

    async def async_turn_off(self) -> None:
        await self._call(self.runtime.async_turn_off())

    async def async_select_source(self, source: str) -> None:
        try:
            parsed = Source(source)
        except ValueError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unsupported_source",
                translation_placeholders={"source": source},
            ) from err
        await self._call(self.runtime.async_select_source(parsed))

    async def async_set_volume_level(self, volume: float) -> None:
        await self._call(self.runtime.async_set_volume(volume))

    async def async_volume_up(self) -> None:
        await self._call(self.runtime.async_volume_up())

    async def async_volume_down(self) -> None:
        await self._call(self.runtime.async_volume_down())

    async def async_mute_volume(self, mute: bool) -> None:
        await self._call(self.runtime.async_mute(mute))

    @staticmethod
    async def _call(operation: object) -> None:
        try:
            await operation  # type: ignore[misc]
        except (LsxError, TimeoutError, RuntimeError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err
