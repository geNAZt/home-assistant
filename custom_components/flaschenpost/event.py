"""Event platform for Flaschenpost shopping list item consumption."""

from __future__ import annotations

import contextlib
import re
from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import FlaschenpostCoordinator
from .entity import FlaschenpostEntity


def _slugify_list_name(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", name.strip().lower()).strip("_")
    return slug or "default"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Flaschenpost event entities based on a config entry."""
    coordinator: FlaschenpostCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities([Flaschenpost3dsRequiredEventEntity(coordinator)])

    added_list_keys: set[str] = set()

    def _add_event_entities() -> None:
        if not hasattr(coordinator, "shopping_list") or not coordinator.shopping_list:
            return
        list_names = list(getattr(coordinator.shopping_list, "list_names", []))
        new_entities: list[EventEntity] = []
        for name in list_names:
            norm_name = str(name).strip()
            if norm_name in added_list_keys:
                continue
            added_list_keys.add(norm_name)
            new_entities.append(
                FlaschenpostListItemConsumedEventEntity(coordinator, norm_name)
            )
        if new_entities:
            async_add_entities(new_entities)

    _add_event_entities()

    if hasattr(coordinator, "shopping_list") and hasattr(
        coordinator.shopping_list, "register_list_added_callback"
    ):
        coordinator.shopping_list.register_list_added_callback(lambda _: _add_event_entities())

    if hasattr(coordinator, "async_add_listener"):
        unsub = coordinator.async_add_listener(_add_event_entities)
        if hasattr(entry, "async_on_unload"):
            entry.async_on_unload(unsub)


class FlaschenpostListItemConsumedEventEntity(FlaschenpostEntity, EventEntity):
    """Event entity triggered when a shopping list item is consumed upon order placement."""

    _attr_translation_key = "list_item_consumed"
    _attr_icon = "mdi:cart-check"
    _attr_event_types = ["item_consumed"]

    def __init__(self, coordinator: FlaschenpostCoordinator, list_name: str) -> None:
        """Initialize the list item consumed event entity."""
        self._list_name = list_name
        slug = _slugify_list_name(list_name)
        super().__init__(coordinator, f"list_{slug}_item_consumed")
        self._attr_translation_placeholders = {"list_name": list_name}
        self._unsub_listener: Any | None = None

    async def async_added_to_hass(self) -> None:
        """Register consumption listener when entity is added to hass."""
        with contextlib.suppress(AttributeError):
            await super().async_added_to_hass()
        if hasattr(self.coordinator, "register_item_consumed_listener"):
            self._unsub_listener = self.coordinator.register_item_consumed_listener(
                self._handle_consumed_event
            )

    async def async_will_remove_from_hass(self) -> None:
        """Unregister consumption listener on removal."""
        if self._unsub_listener:
            self._unsub_listener()
            self._unsub_listener = None
        with contextlib.suppress(AttributeError):
            await super().async_will_remove_from_hass()

    @callback
    def _handle_consumed_event(self, event_data: dict[str, Any]) -> None:
        """Receive consumption event and trigger HA EventEntity if it matches this list."""
        if str(event_data.get("list_name", "")).strip().lower() != self._list_name.lower():
            return
        self._trigger_event("item_consumed", event_data)


class Flaschenpost3dsRequiredEventEntity(FlaschenpostEntity, EventEntity):
    """Event entity triggered when a credit card payment requires 3D-Secure approval."""

    _attr_translation_key = "3ds_required"
    _attr_icon = "mdi:shield-alert"
    _attr_event_types = ["3ds_required"]

    def __init__(self, coordinator: FlaschenpostCoordinator) -> None:
        """Initialize the 3DS required event entity."""
        super().__init__(coordinator, "3ds_required")
        self._unsub_listener: Any | None = None

    async def async_added_to_hass(self) -> None:
        """Register 3DS listener when entity is added to hass."""
        with contextlib.suppress(AttributeError):
            await super().async_added_to_hass()
        if hasattr(self.coordinator, "register_3ds_required_listener"):
            self._unsub_listener = self.coordinator.register_3ds_required_listener(
                self._handle_3ds_event
            )

    async def async_will_remove_from_hass(self) -> None:
        """Unregister 3DS listener on removal."""
        if self._unsub_listener:
            self._unsub_listener()
            self._unsub_listener = None
        with contextlib.suppress(AttributeError):
            await super().async_will_remove_from_hass()

    @callback
    def _handle_3ds_event(self, event_data: dict[str, Any]) -> None:
        """Trigger HA EventEntity when 3DS approval is needed."""
        self._trigger_event("3ds_required", event_data)
        self.async_write_ha_state()
