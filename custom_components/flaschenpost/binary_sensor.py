"""Binary sensor platform for Flaschenpost active delivery detection."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import FlaschenpostCoordinator
from .entity import FlaschenpostEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Flaschenpost binary sensors."""
    coordinator: FlaschenpostCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([FlaschenpostActiveDeliveryBinarySensor(coordinator)])


class FlaschenpostActiveDeliveryBinarySensor(FlaschenpostEntity, BinarySensorEntity):
    """Indicates whether an active Flaschenpost order is currently in progress."""

    _attr_translation_key = "active_delivery"
    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_icon = "mdi:bottle-soda-classic-outline"

    def __init__(self, coordinator: FlaschenpostCoordinator) -> None:
        super().__init__(coordinator, "active_delivery")

    @property
    def is_on(self) -> bool:
        return len(self.coordinator.data.active_orders) > 0
