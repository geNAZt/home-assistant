"""Button entities for one-tap Flaschenpost actions."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
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
    """Set up Flaschenpost action buttons."""
    coordinator: FlaschenpostCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            FlaschenpostSyncListButton(coordinator),
            FlaschenpostRefreshButton(coordinator),
        ]
    )


class FlaschenpostSyncListButton(FlaschenpostEntity, ButtonEntity):
    """Push uncompleted Flaschenpost shopping list items into the live cart."""

    _attr_translation_key = "sync_shopping_list"
    _attr_icon = "mdi:cart-arrow-down"

    def __init__(self, coordinator: FlaschenpostCoordinator) -> None:
        super().__init__(coordinator, "sync_shopping_list")

    async def async_press(self) -> None:
        """Sync shopping list items to remote Flaschenpost cart and refresh."""
        await self.coordinator.shopping_list.async_sync_to_cart(mark_completed=True)
        await self.coordinator.async_request_refresh()


class FlaschenpostRefreshButton(FlaschenpostEntity, ButtonEntity):
    """Trigger an immediate Flaschenpost data refresh."""

    _attr_translation_key = "refresh"
    _attr_icon = "mdi:refresh"

    def __init__(self, coordinator: FlaschenpostCoordinator) -> None:
        super().__init__(coordinator, "refresh")

    async def async_press(self) -> None:
        """Fetch latest Flaschenpost data immediately."""
        await self.coordinator.async_request_refresh()
