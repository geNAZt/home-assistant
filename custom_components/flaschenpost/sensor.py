"""Sensor entities for active order tracking, live delivery, history, cart & slots."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CURRENCY_EURO
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .api import cents_to_eur
from .const import DOMAIN
from .coordinator import FlaschenpostCoordinator
from .entity import FlaschenpostEntity


def _freshness_attrs(coordinator: FlaschenpostCoordinator) -> dict[str, Any]:
    """Return common data freshness and next update timestamps."""
    last_up = coordinator.last_update_success_time
    next_up = coordinator.next_update_time
    return {
        "last_updated": last_up.isoformat() if last_up else None,
        "next_update": next_up.isoformat() if next_up else None,
    }


def _filter_and_aggregate_orders(
    orders: list[dict[str, Any]],
    since: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate total money spent and breakdown for valid (non-cancelled) orders since a given datetime."""
    valid_orders: list[dict[str, Any]] = []
    total_eur = 0.0
    goods_eur = 0.0
    deposit_eur = 0.0
    delivery_fee_eur = 0.0

    for o in orders:
        if not o or not isinstance(o, dict):
            continue

        # Exclude cancelled orders
        if o.get("orderState") == "Cancelled":
            continue
        c_fields = (o.get("custom") or {}).get("fields") or {}
        st = str(c_fields.get("OfficialStatusIconState") or "").lower()
        if st in ("cancelled", "storniert"):
            continue

        created_str = o.get("createdAt")
        if not created_str:
            continue

        dt = dt_util.parse_datetime(str(created_str))
        if dt is None:
            continue

        dt_local = dt_util.as_local(dt)
        if since is not None and dt_local < since:
            continue

        t_eur = cents_to_eur(o.get("totalPrice"))
        g_eur = cents_to_eur(c_fields.get("TotalGoods"))
        d_eur = cents_to_eur(c_fields.get("TotalDeposit"))
        f_eur = cents_to_eur(c_fields.get("TotalDelivery"))

        total_eur += t_eur
        goods_eur += g_eur
        deposit_eur += d_eur
        delivery_fee_eur += f_eur

        valid_orders.append(
            {
                "order_id": o.get("id"),
                "order_number": o.get("orderNumber"),
                "created_at": created_str,
                "total_eur": t_eur,
            }
        )

    valid_orders.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)

    return {
        "order_count": len(valid_orders),
        "total_eur": round(total_eur, 2),
        "goods_eur": round(goods_eur, 2),
        "deposit_eur": round(deposit_eur, 2),
        "delivery_fee_eur": round(delivery_fee_eur, 2),
        "first_order_date": valid_orders[-1]["created_at"] if valid_orders else None,
        "last_order_date": valid_orders[0]["created_at"] if valid_orders else None,
        "orders": valid_orders[:15],
    }


def _filter_and_aggregate_list_spends(
    spends: list[dict[str, Any]],
    since: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate per-list spending records filtering out cancelled orders and out-of-range dates."""
    valid_spends: list[dict[str, Any]] = []
    total_cents = 0
    goods_cents = 0
    deposit_cents = 0
    delivery_fee_cents = 0
    item_count = 0
    all_items: list[dict[str, Any]] = []

    for s in spends:
        if not isinstance(s, dict):
            continue
        order_state = str(s.get("order_state") or "")
        order_status = str(s.get("order_status") or "")
        if order_state.lower() == "cancelled" or order_status.lower() in ("cancelled", "storniert"):
            continue

        created_str = s.get("created_at")
        if created_str:
            try:
                created = dt_util.parse_datetime(created_str)
                if created and since and dt_util.as_local(created) < since:
                    continue
            except Exception:
                pass
        elif since:
            continue

        valid_spends.append(s)
        total_cents += int(s.get("total_cents") or 0)
        goods_cents += int(s.get("goods_cents") or 0)
        deposit_cents += int(s.get("deposit_cents") or 0)
        delivery_fee_cents += int(s.get("delivery_fee_cents") or 0)
        item_count += int(s.get("items_count") or 0)
        for it in s.get("items") or []:
            all_items.append(it)

    return {
        "order_count": len(valid_spends),
        "item_count": item_count,
        "total_eur": round(total_cents / 100.0, 2),
        "goods_eur": round(goods_cents / 100.0, 2),
        "deposit_eur": round(deposit_cents / 100.0, 2),
        "delivery_fee_eur": round(delivery_fee_cents / 100.0, 2),
        "first_order_date": valid_spends[-1]["created_at"] if valid_spends else None,
        "last_order_date": valid_spends[0]["created_at"] if valid_spends else None,
        "items": all_items[:15],
    }


def _summarize_order(order: dict[str, Any]) -> dict[str, Any]:
    """Build compact HA attribute summary for an order object."""
    custom_fields = (order.get("custom") or {}).get("fields") or {}
    ship_fields = (order.get("shippingCustomFields") or {}).get("fields") or {}
    deliveries = (order.get("shippingInfo") or {}).get("deliveries") or []
    deliv_fields = (
        ((deliveries[0].get("custom") or {}).get("fields") or {})
        if deliveries
        else {}
    )
    items = []
    for li in order.get("lineItems") or []:
        name_obj = li.get("name") or {}
        name = (
            name_obj.get("de-DE") or next(iter(name_obj.values()), "")
            if isinstance(name_obj, dict)
            else str(name_obj)
        )
        items.append(
            {
                "article_id": (li.get("variant") or {}).get("sku"),
                "name": name,
                "quantity": li.get("quantity"),
                "total_eur": cents_to_eur(li.get("totalPrice")),
            }
        )

    return {
        "order_id": order.get("id"),
        "order_number": order.get("orderNumber"),
        "created_at": order.get("createdAt"),
        "status": custom_fields.get("OfficialStatusIconState"),
        "status_message": custom_fields.get("OfficialStatusMessage"),
        "eta_from": custom_fields.get("OfficialStatusETAFrom")
        or ship_fields.get("SelectedDeliveryFrom"),
        "eta_until": custom_fields.get("OfficialStatusETAUntil")
        or ship_fields.get("SelectedDeliveryUntil"),
        "delivery_type": ship_fields.get("SelectedDeliveryType"),
        "stops_remaining": deliv_fields.get("CurrentStopsBeforeArrival"),
        "actual_delivery_time": deliv_fields.get("ActualTimeOfDelivery"),
        "total_eur": cents_to_eur(order.get("totalPrice")),
        "goods_eur": cents_to_eur(custom_fields.get("TotalGoods")),
        "deposit_eur": cents_to_eur(custom_fields.get("TotalDeposit")),
        "delivery_fee_eur": cents_to_eur(custom_fields.get("TotalDelivery")),
        "items": items,
    }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Flaschenpost sensor entities."""
    coordinator: FlaschenpostCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            FlaschenpostOrderStatusSensor(coordinator),
            FlaschenpostStopsRemainingSensor(coordinator),
            FlaschenpostDeliveryEtaSensor(coordinator),
            FlaschenpostOrderHistorySensor(coordinator),
            FlaschenpostCartTotalSensor(coordinator),
            FlaschenpostNextSlotSensor(coordinator),
            FlaschenpostNextUpdateSensor(coordinator),
            FlaschenpostLastUpdateSensor(coordinator),
            FlaschenpostWeeklySpentSensor(coordinator),
            FlaschenpostMonthlySpentSensor(coordinator),
            FlaschenpostYearlySpentSensor(coordinator),
            FlaschenpostTotalSpentSensor(coordinator),
        ]
    )

    added_list_keys: set[str] = set()

    def _add_list_sensors() -> None:
        if not hasattr(coordinator, "shopping_list") or not coordinator.shopping_list:
            return
        new_entities: list[SensorEntity] = []
        for list_name in coordinator.shopping_list.list_names:
            norm_name = list_name.strip()
            if not norm_name or norm_name in added_list_keys:
                continue
            added_list_keys.add(norm_name)
            new_entities.extend(
                [
                    FlaschenpostListWeeklySpentSensor(coordinator, norm_name),
                    FlaschenpostListMonthlySpentSensor(coordinator, norm_name),
                    FlaschenpostListYearlySpentSensor(coordinator, norm_name),
                    FlaschenpostListTotalSpentSensor(coordinator, norm_name),
                ]
            )
        if new_entities:
            async_add_entities(new_entities)

    _add_list_sensors()
    if hasattr(coordinator, "shopping_list") and hasattr(coordinator.shopping_list, "register_list_added_callback"):
        coordinator.shopping_list.register_list_added_callback(lambda _: _add_list_sensors())
    if hasattr(coordinator, "async_add_listener"):
        unsub = coordinator.async_add_listener(_add_list_sensors)
        if hasattr(entry, "async_on_unload"):
            entry.async_on_unload(unsub)


class FlaschenpostOrderStatusSensor(FlaschenpostEntity, SensorEntity):
    """Current active order & live delivery tracking status sensor."""

    _attr_translation_key = "order_status"
    _attr_icon = "mdi:truck-delivery"

    def __init__(self, coordinator: FlaschenpostCoordinator) -> None:
        super().__init__(coordinator, "order_status")

    @property
    def native_value(self) -> str:
        order = self.coordinator.data.tracked_order
        if not order:
            return "idle"
        fields = (order.get("custom") or {}).get("fields") or {}
        return str(fields.get("OfficialStatusIconState") or "active")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = _freshness_attrs(self.coordinator)
        order = self.coordinator.data.tracked_order
        if not order:
            attrs["active_orders_count"] = 0
            return attrs
        attrs.update(_summarize_order(order))
        attrs["active_orders_count"] = len(self.coordinator.data.active_orders)
        return attrs


class FlaschenpostStopsRemainingSensor(FlaschenpostEntity, SensorEntity):
    """Number of stops remaining before the delivery driver arrives."""

    _attr_translation_key = "stops_remaining"
    _attr_icon = "mdi:map-marker-distance"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: FlaschenpostCoordinator) -> None:
        super().__init__(coordinator, "stops_remaining")

    @property
    def native_value(self) -> int | None:
        order = self.coordinator.data.tracked_order
        if not order:
            return None
        deliveries = (order.get("shippingInfo") or {}).get("deliveries") or []
        if not deliveries:
            return None
        stops = (
            (deliveries[0].get("custom") or {})
            .get("fields", {})
            .get("CurrentStopsBeforeArrival")
        )
        return int(stops) if stops is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return _freshness_attrs(self.coordinator)


class FlaschenpostDeliveryEtaSensor(FlaschenpostEntity, SensorEntity):
    """Live delivery ETA start timestamp sensor."""

    _attr_translation_key = "delivery_eta"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-fast"

    def __init__(self, coordinator: FlaschenpostCoordinator) -> None:
        super().__init__(coordinator, "delivery_eta")

    @property
    def native_value(self) -> datetime | None:
        order = self.coordinator.data.tracked_order
        if not order:
            return None
        c_fields = (order.get("custom") or {}).get("fields") or {}
        s_fields = (order.get("shippingCustomFields") or {}).get("fields") or {}
        eta_str = c_fields.get("OfficialStatusETAFrom") or s_fields.get(
            "SelectedDeliveryFrom"
        )
        if not eta_str:
            return None
        return dt_util.parse_datetime(str(eta_str))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = _freshness_attrs(self.coordinator)
        order = self.coordinator.data.tracked_order
        if not order:
            return attrs
        c_fields = (order.get("custom") or {}).get("fields") or {}
        s_fields = (order.get("shippingCustomFields") or {}).get("fields") or {}
        attrs.update(
            {
                "eta_until": c_fields.get("OfficialStatusETAUntil")
                or s_fields.get("SelectedDeliveryUntil"),
                "status_message": c_fields.get("OfficialStatusMessage"),
            }
        )
        return attrs


class FlaschenpostOrderHistorySensor(FlaschenpostEntity, SensorEntity):
    """Customer order history count & recent orders summary."""

    _attr_translation_key = "order_history"
    _attr_icon = "mdi:history"
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator: FlaschenpostCoordinator) -> None:
        super().__init__(coordinator, "order_history")

    @property
    def native_value(self) -> int:
        acc_fields = (
            (self.coordinator.data.account.get("custom") or {}).get("fields") or {}
        )
        delivered = acc_fields.get("DeliveredOrders")
        if delivered is not None:
            return int(delivered)
        return self.coordinator.data.past_orders_total

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = _freshness_attrs(self.coordinator)
        past = self.coordinator.data.past_orders
        recent = [_summarize_order(o) for o in past[:5]]
        attrs.update(
            {
                "last_180d_count": self.coordinator.data.past_orders_total,
                "last_order": recent[0] if recent else None,
                "recent_orders": recent,
            }
        )
        return attrs


class FlaschenpostCartTotalSensor(FlaschenpostEntity, SensorEntity):
    """Current shopping cart total in EUR."""

    _attr_translation_key = "cart_total"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = CURRENCY_EURO
    _attr_icon = "mdi:cart"

    def __init__(self, coordinator: FlaschenpostCoordinator) -> None:
        super().__init__(coordinator, "cart_total")

    @property
    def native_value(self) -> float:
        cart = self.coordinator.data.cart or {}
        line_items = cart.get("lineItems") or []
        item_count = cart.get("totalLineItemQuantity", len(line_items))
        if not line_items or item_count == 0:
            return 0.0
        return cents_to_eur(cart.get("totalPrice"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = _freshness_attrs(self.coordinator)
        cart = self.coordinator.data.cart or {}
        c_fields = (cart.get("custom") or {}).get("fields") or {}
        s_fields = (cart.get("shippingCustomFields") or {}).get("fields") or {}
        line_items = cart.get("lineItems") or []
        item_count = cart.get("totalLineItemQuantity", len(line_items))
        is_empty = not line_items or item_count == 0
        items = []
        for li in line_items:
            name_obj = li.get("name") or {}
            name = (
                name_obj.get("de-DE") or next(iter(name_obj.values()), "")
                if isinstance(name_obj, dict)
                else str(name_obj)
            )
            items.append(
                {
                    "line_item_id": li.get("id"),
                    "article_id": (li.get("variant") or {}).get("sku"),
                    "name": name,
                    "quantity": li.get("quantity"),
                    "total_eur": cents_to_eur(li.get("totalPrice")),
                }
            )
        attrs.update(
            {
                "cart_id": cart.get("id"),
                "item_count": 0 if is_empty else item_count,
                "warehouse_id": c_fields.get("RecentlyUsedWarehouseId"),
                "goods_eur": 0.0 if is_empty else cents_to_eur(c_fields.get("TotalGoods")),
                "deposit_eur": 0.0 if is_empty else cents_to_eur(c_fields.get("TotalDeposit")),
                "delivery_fee_eur": 0.0 if is_empty else cents_to_eur(c_fields.get("TotalDelivery")),
                "selected_slot_type": s_fields.get("SelectedDeliveryType"),
                "selected_slot_from": s_fields.get("SelectedDeliveryFrom"),
                "selected_slot_until": s_fields.get("SelectedDeliveryUntil"),
                "items": items,
            }
        )
        return attrs


class FlaschenpostNextSlotSensor(FlaschenpostEntity, SensorEntity):
    """Earliest available delivery slot timestamp."""

    _attr_translation_key = "next_delivery_slot"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: FlaschenpostCoordinator) -> None:
        super().__init__(coordinator, "next_delivery_slot")

    @property
    def native_value(self) -> datetime | None:
        slots = self.coordinator.data.available_slots
        if not slots:
            return None
        first_from = slots[0].get("from")
        if not first_from:
            return None
        return dt_util.parse_datetime(str(first_from))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = _freshness_attrs(self.coordinator)
        slots = self.coordinator.data.available_slots
        resp = self.coordinator.data.slots_response
        comm = resp.get("shopCommunication") or {}
        attrs.update(
            {
                "shop_state": resp.get("shopState"),
                "communicated_delivery_time_min": comm.get("communicatedDeliveryTime"),
                "next_slot": slots[0] if slots else None,
                "available_slots": [
                    {
                        "type": s.get("type"),
                        "from": s.get("from"),
                        "until": s.get("until"),
                        "window": s.get("window"),
                    }
                    for s in slots[:10]
                ],
            }
        )
        return attrs


class FlaschenpostNextUpdateSensor(FlaschenpostEntity, SensorEntity):
    """Estimated timestamp / countdown for the next Flaschenpost update."""

    _attr_translation_key = "next_update"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:timer-sand"

    def __init__(self, coordinator: FlaschenpostCoordinator) -> None:
        super().__init__(coordinator, "next_update")

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.next_update_time

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        coord = self.coordinator
        interval_sec = (
            int(coord.update_interval.total_seconds())
            if coord.update_interval
            else None
        )
        return {
            "last_updated": (
                coord.last_update_success_time.isoformat()
                if coord.last_update_success_time
                else None
            ),
            "update_interval_seconds": interval_sec,
            "update_interval_human": (
                f"{interval_sec // 60}m"
                if interval_sec and interval_sec >= 60
                else f"{interval_sec}s" if interval_sec else None
            ),
            "active_delivery_mode": bool(
                coord.data.active_orders if coord.data else False
            ),
            "last_update_status": "success" if coord.last_update_success else "failed",
        }


class FlaschenpostLastUpdateSensor(FlaschenpostEntity, SensorEntity):
    """Timestamp of the most recent successful Flaschenpost data refresh."""

    _attr_translation_key = "last_update"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-check-outline"

    def __init__(self, coordinator: FlaschenpostCoordinator) -> None:
        super().__init__(coordinator, "last_update")

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.last_update_success_time

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        coord = self.coordinator
        interval_sec = (
            int(coord.update_interval.total_seconds())
            if coord.update_interval
            else None
        )
        return {
            "next_update": (
                coord.next_update_time.isoformat()
                if coord.next_update_time
                else None
            ),
            "update_interval_seconds": interval_sec,
            "active_delivery_mode": bool(
                coord.data.active_orders if coord.data else False
            ),
            "last_update_status": "success" if coord.last_update_success else "failed",
        }


class FlaschenpostWeeklySpentSensor(FlaschenpostEntity, SensorEntity):
    """Summary sensor for money spent in the current calendar week (EUR)."""

    _attr_translation_key = "weekly_spent"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = CURRENCY_EURO
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:calendar-week"

    def __init__(self, coordinator: FlaschenpostCoordinator) -> None:
        super().__init__(coordinator, "weekly_spent")

    def _get_orders(self) -> list[dict[str, Any]]:
        snap = self.coordinator.data
        if not snap:
            return []
        return snap.all_orders or (snap.past_orders + snap.active_orders)

    def _since_start_of_week(self) -> datetime:
        now = dt_util.now()
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start_of_day - timedelta(days=now.weekday())

    @property
    def native_value(self) -> float:
        orders = self._get_orders()
        since = self._since_start_of_week()
        agg = _filter_and_aggregate_orders(orders, since=since)
        return float(agg["total_eur"])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = _freshness_attrs(self.coordinator)
        orders = self._get_orders()
        since = self._since_start_of_week()
        agg = _filter_and_aggregate_orders(orders, since=since)
        attrs.update(
            {
                "week_start": since.isoformat(),
                "order_count": agg["order_count"],
                "goods_eur": agg["goods_eur"],
                "deposit_eur": agg["deposit_eur"],
                "delivery_fee_eur": agg["delivery_fee_eur"],
                "recent_orders": agg["orders"],
            }
        )
        return attrs


class FlaschenpostMonthlySpentSensor(FlaschenpostEntity, SensorEntity):
    """Summary sensor for money spent in the current calendar month (EUR)."""

    _attr_translation_key = "monthly_spent"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = CURRENCY_EURO
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:calendar-month"

    def __init__(self, coordinator: FlaschenpostCoordinator) -> None:
        super().__init__(coordinator, "monthly_spent")

    def _get_orders(self) -> list[dict[str, Any]]:
        snap = self.coordinator.data
        if not snap:
            return []
        return snap.all_orders or (snap.past_orders + snap.active_orders)

    def _since_start_of_month(self) -> datetime:
        now = dt_util.now()
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    @property
    def native_value(self) -> float:
        orders = self._get_orders()
        since = self._since_start_of_month()
        agg = _filter_and_aggregate_orders(orders, since=since)
        return float(agg["total_eur"])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = _freshness_attrs(self.coordinator)
        orders = self._get_orders()
        since = self._since_start_of_month()
        agg = _filter_and_aggregate_orders(orders, since=since)
        attrs.update(
            {
                "month_start": since.isoformat(),
                "order_count": agg["order_count"],
                "goods_eur": agg["goods_eur"],
                "deposit_eur": agg["deposit_eur"],
                "delivery_fee_eur": agg["delivery_fee_eur"],
                "recent_orders": agg["orders"],
            }
        )
        return attrs


class FlaschenpostYearlySpentSensor(FlaschenpostEntity, SensorEntity):
    """Summary sensor for money spent in the current calendar year (EUR)."""

    _attr_translation_key = "yearly_spent"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = CURRENCY_EURO
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:calendar-range"

    def __init__(self, coordinator: FlaschenpostCoordinator) -> None:
        super().__init__(coordinator, "yearly_spent")

    def _get_orders(self) -> list[dict[str, Any]]:
        snap = self.coordinator.data
        if not snap:
            return []
        return snap.all_orders or (snap.past_orders + snap.active_orders)

    def _since_start_of_year(self) -> datetime:
        now = dt_util.now()
        return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

    @property
    def native_value(self) -> float:
        orders = self._get_orders()
        since = self._since_start_of_year()
        agg = _filter_and_aggregate_orders(orders, since=since)
        return float(agg["total_eur"])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = _freshness_attrs(self.coordinator)
        orders = self._get_orders()
        since = self._since_start_of_year()
        agg = _filter_and_aggregate_orders(orders, since=since)
        attrs.update(
            {
                "year_start": since.isoformat(),
                "order_count": agg["order_count"],
                "goods_eur": agg["goods_eur"],
                "deposit_eur": agg["deposit_eur"],
                "delivery_fee_eur": agg["delivery_fee_eur"],
                "recent_orders": agg["orders"],
            }
        )
        return attrs


class FlaschenpostTotalSpentSensor(FlaschenpostEntity, SensorEntity):
    """Non-resetting all-time total money spent sensor (EUR)."""

    _attr_translation_key = "total_spent"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = CURRENCY_EURO
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:cash-multiple"

    def __init__(self, coordinator: FlaschenpostCoordinator) -> None:
        super().__init__(coordinator, "total_spent")

    def _get_orders(self) -> list[dict[str, Any]]:
        snap = self.coordinator.data
        if not snap:
            return []
        return snap.all_orders or (snap.past_orders + snap.active_orders)

    @property
    def native_value(self) -> float:
        orders = self._get_orders()
        agg = _filter_and_aggregate_orders(orders, since=None)
        return float(agg["total_eur"])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = _freshness_attrs(self.coordinator)
        orders = self._get_orders()
        agg = _filter_and_aggregate_orders(orders, since=None)
        attrs.update(
            {
                "order_count": agg["order_count"],
                "goods_eur": agg["goods_eur"],
                "deposit_eur": agg["deposit_eur"],
                "delivery_fee_eur": agg["delivery_fee_eur"],
                "first_order_date": agg["first_order_date"],
                "last_order_date": agg["last_order_date"],
                "recent_orders": agg["orders"],
            }
        )
        return attrs


class _FlaschenpostBaseListSpentSensor(FlaschenpostEntity, SensorEntity):
    """Base class for per-shopping-list spending summary sensors."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = CURRENCY_EURO
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(
        self,
        coordinator: FlaschenpostCoordinator,
        list_name: str,
        period_key: str,
        icon: str,
    ) -> None:
        self._list_name = list_name
        self._period_key = period_key
        self._attr_icon = icon
        slug = re.sub(r"[^a-zA-Z0-9_-]", "_", list_name.lower()).strip("_")
        super().__init__(coordinator, f"list_{slug}_{period_key}")
        self._attr_translation_key = f"list_{period_key}"
        self._attr_translation_placeholders = {"list_name": list_name}
        self._attr_name = f"{list_name} {period_key.replace('_', ' ').capitalize()}"

    def _get_list_spends(self) -> list[dict[str, Any]]:
        snap = self.coordinator.data
        if not snap or not hasattr(snap, "list_spends"):
            return []
        return snap.list_spends.get(self._list_name, [])

    def _get_since(self) -> datetime | None:
        now = dt_util.now()
        if self._period_key == "weekly_spent":
            start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            return start_of_day - timedelta(days=now.weekday())
        if self._period_key == "monthly_spent":
            return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if self._period_key == "yearly_spent":
            return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        return None

    @property
    def native_value(self) -> float:
        spends = self._get_list_spends()
        since = self._get_since()
        agg = _filter_and_aggregate_list_spends(spends, since=since)
        return float(agg["total_eur"])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = _freshness_attrs(self.coordinator)
        spends = self._get_list_spends()
        since = self._get_since()
        agg = _filter_and_aggregate_list_spends(spends, since=since)
        attrs.update(
            {
                "list_name": self._list_name,
                "order_count": agg["order_count"],
                "item_count": agg["item_count"],
                "goods_eur": agg["goods_eur"],
                "deposit_eur": agg["deposit_eur"],
                "delivery_fee_eur": agg["delivery_fee_eur"],
                "recent_items": agg["items"],
            }
        )
        if since:
            attrs[f"{self._period_key.split('_')[0]}_start"] = since.isoformat()
        if self._period_key == "total_spent":
            attrs["first_order_date"] = agg["first_order_date"]
            attrs["last_order_date"] = agg["last_order_date"]
        return attrs


class FlaschenpostListWeeklySpentSensor(_FlaschenpostBaseListSpentSensor):
    """Weekly spending summary sensor for a specific shopping list."""

    def __init__(self, coordinator: FlaschenpostCoordinator, list_name: str) -> None:
        super().__init__(coordinator, list_name, "weekly_spent", "mdi:calendar-week")


class FlaschenpostListMonthlySpentSensor(_FlaschenpostBaseListSpentSensor):
    """Monthly spending summary sensor for a specific shopping list."""

    def __init__(self, coordinator: FlaschenpostCoordinator, list_name: str) -> None:
        super().__init__(coordinator, list_name, "monthly_spent", "mdi:calendar-month")


class FlaschenpostListYearlySpentSensor(_FlaschenpostBaseListSpentSensor):
    """Yearly spending summary sensor for a specific shopping list."""

    def __init__(self, coordinator: FlaschenpostCoordinator, list_name: str) -> None:
        super().__init__(coordinator, list_name, "yearly_spent", "mdi:calendar-range")


class FlaschenpostListTotalSpentSensor(_FlaschenpostBaseListSpentSensor):
    """All-time non-resetting total spending summary sensor for a specific shopping list."""

    def __init__(self, coordinator: FlaschenpostCoordinator, list_name: str) -> None:
        super().__init__(coordinator, list_name, "total_spent", "mdi:cash-multiple")

