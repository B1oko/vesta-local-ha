"""Image platform for Vesta/Climax Local: last photo of each PIR camera."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import VestaDeviceEntity

if TYPE_CHECKING:
    from . import VestaConfigEntry
    from .client import DeviceStatus
    from .coordinator import VestaDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VestaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one image entity per camera device."""
    coordinator = entry.runtime_data
    entities = [
        VestaCaptureImage(coordinator, device, entry.entry_id)
        for device in (coordinator.data.devices if coordinator.data else [])
        if device.is_camera
    ]
    async_add_entities(entities)
    _LOGGER.debug("Added %d image entities", len(entities))


class VestaCaptureImage(VestaDeviceEntity, ImageEntity):
    """Last photo taken by a PIR camera when the alarm triggered."""

    _attr_translation_key = "last_capture"
    _attr_content_type = "image/jpeg"

    def __init__(
        self,
        coordinator: VestaDataUpdateCoordinator,
        device: DeviceStatus,
        entry_id: str,
    ) -> None:
        """Initialize the image entity."""
        VestaDeviceEntity.__init__(self, coordinator, device, entry_id)
        ImageEntity.__init__(self, coordinator.hass)
        self._attr_unique_id = f"{entry_id}_{device.device_id}_last_capture"
        self._photo_key: tuple[str, str] | None = None
        self._update_from_photo()

    @property
    def available(self) -> bool:
        """Stay available while the panel answers, even without photos."""
        return self.coordinator.last_update_success and self.device_data is not None

    async def async_added_to_hass(self) -> None:
        """Register the entity id so capture events can reference it."""
        await super().async_added_to_hass()
        self.coordinator.captures.camera_entity_ids[self._device_id] = self.entity_id

    def _update_from_photo(self) -> None:
        photo = self.coordinator.captures.photos.get(self._device_id)
        key = (photo.capture.key, photo.panel_path) if photo else None
        if key != self._photo_key:
            self._photo_key = key
            self._attr_image_last_updated = photo.updated if photo else None
            # New image: rotate the access token so cached URLs refresh.
            self._cached_image = None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Pick up a new photo after each poll."""
        self._update_from_photo()
        super()._handle_coordinator_update()

    @property
    def image_last_updated(self) -> datetime | None:
        """Return when the current photo was taken."""
        return self._attr_image_last_updated

    async def async_image(self) -> bytes | None:
        """Return the bytes of the last photo."""
        photo = self.coordinator.captures.photos.get(self._device_id)
        return photo.content if photo else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Describe the capture the photo belongs to."""
        photo = self.coordinator.captures.photos.get(self._device_id)
        if photo is None:
            return None
        capture = photo.capture
        return {
            "time": capture.time,
            "type": capture.type,
            "status": capture.status,
            "zone": capture.zone,
            "area": capture.area,
            "photo_count": len(capture.image_files),
            "files": capture.image_files,
            "photo": photo.panel_path,
            "saved_files": photo.saved_files,
        }
