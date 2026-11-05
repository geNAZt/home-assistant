"""Base entity for the Flaschenpost integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import FlaschenpostCoordinator


class FlaschenpostEntity(CoordinatorEntity[FlaschenpostCoordinator]):
    """Base entity linked to a Flaschenpost customer account device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: FlaschenpostCoordinator, key: str) -> None:
        """Initialize the base entity."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"Flaschenpost ({entry.title})",
            manufacturer="flaschenpost SE",
            model="B2C Webshop & Delivery",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://www.flaschenpost.de/konto",
        )
