"""DataUpdateCoordinator for the Flaschenpost integration."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import FlaschenpostApiClient, FlaschenpostAuthError, FlaschenpostError
from .const import (
    CONF_DEFAULT_PAYMENT,
    CONF_IBAN_LAST4,
    CONF_STORED_PAYMENT_ID,
    DOMAIN,
    PAYMENT_CREDIT_CARD,
    SCAN_INTERVAL_ACTIVE_DELIVERY,
    SCAN_INTERVAL_IDLE,
)
from .shopping_list import FlaschenpostShoppingListManager

_LOGGER = logging.getLogger(__name__)


@dataclass
class FlaschenpostSnapshot:
    """Aggregated state snapshot from Flaschenpost backend."""

    account: dict[str, Any] = field(default_factory=dict)
    cart: dict[str, Any] = field(default_factory=dict)
    active_orders: list[dict[str, Any]] = field(default_factory=list)
    tracked_order: dict[str, Any] | None = None
    past_orders: list[dict[str, Any]] = field(default_factory=list)
    past_orders_total: int = 0
    all_orders: list[dict[str, Any]] = field(default_factory=list)
    list_spends: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    slots_response: dict[str, Any] = field(default_factory=dict)
    available_slots: list[dict[str, Any]] = field(default_factory=list)


class FlaschenpostCoordinator(DataUpdateCoordinator[FlaschenpostSnapshot]):
    """Coordinator managing polling and adaptive delivery tracking updates."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: FlaschenpostApiClient,
        shopping_list: FlaschenpostShoppingListManager,
    ) -> None:
        """Initialize the Flaschenpost coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.title}",
            update_interval=SCAN_INTERVAL_IDLE,
        )
        self.config_entry = entry
        self.client = client
        self.shopping_list = shopping_list
        self.last_update_success_time: datetime | None = None
        self._item_consumed_listeners: list[Callable[[dict[str, Any]], None]] = []
        self._3ds_required_listeners: list[Callable[[dict[str, Any]], None]] = []
        if self.shopping_list and hasattr(self.shopping_list, "register_item_consumed_callback"):
            self.shopping_list.register_item_consumed_callback(self.async_on_item_consumed)

    def register_3ds_required_listener(
        self, listener: Callable[[dict[str, Any]], None]
    ) -> Callable[[], None]:
        """Register a callback for 3D-Secure required events."""
        self._3ds_required_listeners.append(listener)

        def _unsubscribe() -> None:
            if listener in self._3ds_required_listeners:
                self._3ds_required_listeners.remove(listener)

        return _unsubscribe

    def async_on_3ds_required(self, event_data: dict[str, Any]) -> None:
        """Handle 3D-Secure challenge requirement: fire HA bus event and notify event entities."""
        if self.hass and hasattr(self.hass, "bus") and hasattr(self.hass.bus, "async_fire"):
            self.hass.bus.async_fire("flaschenpost_3ds_required", event_data)
        for listener in list(self._3ds_required_listeners):
            try:
                listener(event_data)
            except Exception as err:
                _LOGGER.warning("Error in 3D-Secure listener: %s", err)

    def async_on_item_consumed(self, event_data: dict[str, Any]) -> None:
        """Handle shopping list item consumption: fire HA bus event and notify event entities."""
        if self.hass and hasattr(self.hass, "bus") and hasattr(self.hass.bus, "async_fire"):
            self.hass.bus.async_fire("flaschenpost_item_consumed", event_data)
        for listener in list(self._item_consumed_listeners):
            try:
                listener(event_data)
            except Exception as err:
                _LOGGER.warning("Error in item consumed listener: %s", err)

    async def async_resolve_payment_parameters(
        self,
        payment_method: str | None = None,
        stored_payment_id: str | None = None,
        iban_last4: str | None = None,
    ) -> tuple[str, str | None, str | None]:
        """Resolve payment parameters using explicit params, options, or data."""
        return await async_resolve_payment_parameters(
            self,
            payment_method=payment_method,
            stored_payment_id=stored_payment_id,
            iban_last4=iban_last4,
        )

    def register_item_consumed_listener(
        self, listener: Callable[[dict[str, Any]], None]
    ) -> Callable[[], None]:
        """Register a listener for consumed item events (used by Event entities)."""
        self._item_consumed_listeners.append(listener)

        def _remove() -> None:
            if listener in self._item_consumed_listeners:
                self._item_consumed_listeners.remove(listener)

        return _remove

    @property
    def next_update_time(self) -> datetime | None:
        """Return estimated timestamp of the next coordinator poll."""
        if self.last_update_success_time is None or self.update_interval is None:
            return None
        return self.last_update_success_time + self.update_interval

    async def _async_update_data(self) -> FlaschenpostSnapshot:
        """Fetch latest account, cart, active order tracking, history, and slots."""
        try:
            account = await self.client.async_get_my_account()
            cart = await self.client.async_get_cart()
            active_resp = await self.client.async_get_active_orders(offset=0)
            active_orders = active_resp.get("results") or []

            tracked_order: dict[str, Any] | None = None
            if active_orders:
                primary_id = active_orders[0].get("id")
                if primary_id:
                    try:
                        tracked_order = await self.client.async_get_order_tracking(
                            primary_id
                        )
                    except FlaschenpostError as err:
                        _LOGGER.debug(
                            "Live order tracking fallback to active order entry: %s",
                            err,
                        )
                        tracked_order = active_orders[0]

            if hasattr(self.client, "async_get_all_past_orders"):
                past_orders = await self.client.async_get_all_past_orders(days=365 * 3)
            else:
                past_resp = await self.client.async_get_past_orders(days=180, offset=0)
                past_orders = past_resp.get("results") or []
            past_total = len(past_orders)

            # Persist orders to SQLite for permanent historical and spending reporting
            if self.shopping_list and hasattr(self.shopping_list, "async_record_orders"):
                await self.shopping_list.async_record_orders(past_orders + active_orders)

            # Attribute spends and consume items for any active orders
            if active_orders and self.shopping_list and hasattr(self.shopping_list, "async_record_order_attributions"):
                for ao in active_orders:
                    with contextlib.suppress(Exception):
                        await self.shopping_list.async_record_order_attributions(ao)

            all_orders: list[dict[str, Any]] = []
            if self.shopping_list and hasattr(self.shopping_list, "async_get_all_recorded_orders"):
                all_orders = await self.shopping_list.async_get_all_recorded_orders()

            if not all_orders:
                seen = set()
                for o in active_orders + past_orders:
                    oid = o.get("id") or o.get("orderNumber")
                    if oid and oid not in seen:
                        seen.add(oid)
                        all_orders.append(o)

            list_spends: dict[str, list[dict[str, Any]]] = {}
            if self.shopping_list and hasattr(self.shopping_list, "async_get_all_list_spends"):
                list_spends = await self.shopping_list.async_get_all_list_spends()

            slots_resp = await self.client.async_get_delivery_slots(
                cart=cart, account=account
            )
            available_slots = self.client.pick_available_slots(slots_resp)

            # Speed up polling automatically while an active delivery is underway
            desired_interval = (
                SCAN_INTERVAL_ACTIVE_DELIVERY
                if active_orders
                else SCAN_INTERVAL_IDLE
            )
            if self.update_interval != desired_interval:
                self.update_interval = desired_interval

            self.last_update_success_time = dt_util.utcnow()

            return FlaschenpostSnapshot(
                account=account,
                cart=cart,
                active_orders=active_orders,
                tracked_order=tracked_order,
                past_orders=past_orders,
                past_orders_total=past_total,
                all_orders=all_orders,
                list_spends=list_spends,
                slots_response=slots_resp,
                available_slots=available_slots,
            )
        except FlaschenpostAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except FlaschenpostError as err:
            raise UpdateFailed(f"Error communicating with Flaschenpost: {err}") from err


async def async_resolve_payment_parameters(
    coord: Any,
    payment_method: str | None = None,
    stored_payment_id: str | None = None,
    iban_last4: str | None = None,
) -> tuple[str, str | None, str | None]:
    """Resolve payment method, stored payment id, and IBAN from explicit params or config entry."""
    entry = getattr(coord, "config_entry", None)
    opts = entry.options if entry and hasattr(entry, "options") else {}
    data = entry.data if entry and hasattr(entry, "data") else {}

    method = (
        payment_method
        or opts.get(CONF_DEFAULT_PAYMENT)
        or data.get(CONF_DEFAULT_PAYMENT)
    )
    stored_id = (
        stored_payment_id
        or opts.get(CONF_STORED_PAYMENT_ID)
        or data.get(CONF_STORED_PAYMENT_ID)
        or None
    )
    iban = (
        iban_last4
        or opts.get(CONF_IBAN_LAST4)
        or data.get(CONF_IBAN_LAST4)
        or None
    )

    if not method:
        method = PAYMENT_CREDIT_CARD

    client = getattr(coord, "client", None)
    if client and hasattr(client, "async_get_saved_payment_methods"):
        try:
            saved_methods = await client.async_get_saved_payment_methods()
            if method == PAYMENT_CREDIT_CARD:
                cc_methods = [
                    m for m in saved_methods if m.get("payment_method") == PAYMENT_CREDIT_CARD
                ]
                if stored_id:
                    matching = next(
                        (m for m in cc_methods if m.get("stored_payment_id") == stored_id),
                        None,
                    )
                    if matching:
                        stored_id = matching.get("stored_payment_id")
                else:
                    with_stored = next(
                        (m for m in cc_methods if m.get("stored_payment_id")),
                        None,
                    )
                    if with_stored:
                        stored_id = with_stored.get("stored_payment_id")
            elif stored_id:
                matching = next(
                    (
                        m
                        for m in saved_methods
                        if m.get("stored_payment_id") == stored_id
                        and m.get("payment_method") == method
                    ),
                    None,
                )
                if matching:
                    stored_id = matching.get("stored_payment_id")
            elif method:
                method_saved = [
                    m for m in saved_methods if m.get("payment_method") == method
                ]
                with_stored = next(
                    (m for m in method_saved if m.get("stored_payment_id")), None
                )
                if with_stored:
                    stored_id = with_stored.get("stored_payment_id")
        except Exception as err:
            _LOGGER.debug("Could not resolve saved payment methods: %s", err)

    if not method:
        method = PAYMENT_CREDIT_CARD

    return method, stored_id, iban
