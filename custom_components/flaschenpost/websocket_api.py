"""WebSocket API commands for Flaschenpost interactive shopping lists and warehouse search."""

from __future__ import annotations

import contextlib
import logging
from dataclasses import asdict, is_dataclass
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from .api import cents_to_eur
from .const import (
    CONF_DEFAULT_PAYMENT,
    CONF_STORED_PAYMENT_ID,
    DOMAIN,
    PAYMENT_CREDIT_CARD,
)
from .coordinator import async_resolve_payment_parameters

_LOGGER = logging.getLogger(__name__)


def _serialize_obj(obj: Any) -> Any:
    """Recursively serialize dataclasses, sets, or nested objects for JSON WebSocket responses."""
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, dict):
        return {k: _serialize_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize_obj(x) for x in obj]
    if isinstance(obj, set):
        return [_serialize_obj(x) for x in obj]
    return obj


def _resolve_coord(hass: HomeAssistant, config_entry_id: str | None = None) -> Any:
    """Resolve active Flaschenpost coordinator."""
    entries = hass.data.get(DOMAIN, {})
    if not entries:
        raise ValueError("No Flaschenpost integration is configured or loaded")
    if config_entry_id:
        coord = entries.get(config_entry_id)
        if not coord:
            raise ValueError(f"Flaschenpost config_entry_id '{config_entry_id}' not found")
        return coord
    return next(iter(entries.values()))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "flaschenpost/get_lists",
        vol.Optional("config_entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_get_lists(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Fetch all managed shopping lists and their items."""
    try:
        coord = _resolve_coord(hass, msg.get("config_entry_id"))
        lists = coord.shopping_list.get_all_lists_dict()
        names = coord.shopping_list.list_names
        configs = coord.shopping_list.get_all_list_configs()
        connection.send_result(msg["id"], {"lists": lists, "names": names, "configs": configs})
    except Exception as err:
        _LOGGER.exception("Error in flaschenpost/get_lists: %s", err)
        connection.send_error(msg["id"], "error", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "flaschenpost/save_list",
        vol.Required("name"): str,
        vol.Required("items"): list,
        vol.Optional("replace", default=True): bool,
        vol.Optional("sort_by"): vol.Any(None, str),
        vol.Optional("config_entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_save_list(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Save or replace a full named shopping list."""
    try:
        coord = _resolve_coord(hass, msg.get("config_entry_id"))
        name = str(msg["name"]).strip()
        lines = [str(x) for x in msg["items"]]
        saved = await coord.shopping_list.async_save_named_list(
            name, lines, replace=bool(msg.get("replace", True)), sort_by=msg.get("sort_by")
        )
        coord.async_update_listeners()
        connection.send_result(
            msg["id"],
            {
                "name": name,
                "item_count": len(saved),
                "items": [it.to_dict() for it in saved],
                "names": coord.shopping_list.list_names,
            },
        )
    except Exception as err:
        _LOGGER.exception("Error in flaschenpost/save_list: %s", err)
        connection.send_error(msg["id"], "error", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "flaschenpost/delete_list",
        vol.Required("name"): str,
        vol.Optional("config_entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_delete_list(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Delete a named shopping list."""
    try:
        coord = _resolve_coord(hass, msg.get("config_entry_id"))
        name = str(msg["name"]).strip()
        deleted = await coord.shopping_list.async_delete_named_list(name)
        coord.async_update_listeners()
        connection.send_result(
            msg["id"],
            {
                "name": name,
                "deleted": deleted,
                "names": coord.shopping_list.list_names,
            },
        )
    except Exception as err:
        _LOGGER.exception("Error in flaschenpost/delete_list: %s", err)
        connection.send_error(msg["id"], "error", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "flaschenpost/add_item",
        vol.Required("list_name"): str,
        vol.Required("summary"): str,
        vol.Optional("description"): str,
        vol.Optional("sort_by"): vol.Any(None, str),
        vol.Optional("config_entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_add_item(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Add a single item to a shopping list."""
    try:
        coord = _resolve_coord(hass, msg.get("config_entry_id"))
        lname = str(msg["list_name"]).strip()
        summary = str(msg["summary"]).strip()
        item = await coord.shopping_list.async_add_item(
            summary,
            description=msg.get("description"),
            sort_by=msg.get("sort_by"),
            list_name=lname,
        )
        coord.async_update_listeners()
        connection.send_result(
            msg["id"],
            {
                "item": item.to_dict(),
                "list_name": lname,
                "items": [it.to_dict() for it in coord.shopping_list.get_list_items(lname)],
            },
        )
    except Exception as err:
        _LOGGER.exception("Error in flaschenpost/add_item: %s", err)
        connection.send_error(msg["id"], "error", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "flaschenpost/update_item",
        vol.Required("list_name"): str,
        vol.Required("uid"): str,
        vol.Optional("summary"): str,
        vol.Optional("completed"): bool,
        vol.Optional("config_entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_update_item(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Update an item's text/amount or completed status."""
    try:
        coord = _resolve_coord(hass, msg.get("config_entry_id"))
        lname = str(msg["list_name"]).strip()
        uid = str(msg["uid"]).strip()
        item = await coord.shopping_list.async_update_item(
            uid,
            summary=msg.get("summary"),
            completed=msg.get("completed"),
            list_name=lname,
        )
        coord.async_update_listeners()
        connection.send_result(
            msg["id"],
            {
                "item": item.to_dict() if item else None,
                "list_name": lname,
                "items": [it.to_dict() for it in coord.shopping_list.get_list_items(lname)],
            },
        )
    except Exception as err:
        _LOGGER.exception("Error in flaschenpost/update_item: %s", err)
        connection.send_error(msg["id"], "error", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "flaschenpost/delete_item",
        vol.Required("list_name"): str,
        vol.Required("uids"): list,
        vol.Optional("config_entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_delete_item(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Remove items by UID from a named shopping list."""
    try:
        coord = _resolve_coord(hass, msg.get("config_entry_id"))
        lname = str(msg["list_name"]).strip()
        uids = [str(u) for u in msg["uids"]]
        await coord.shopping_list.async_remove_items(uids, list_name=lname)
        coord.async_update_listeners()
        connection.send_result(
            msg["id"],
            {
                "deleted_uids": uids,
                "list_name": lname,
                "items": [it.to_dict() for it in coord.shopping_list.get_list_items(lname)],
            },
        )
    except Exception as err:
        _LOGGER.exception("Error in flaschenpost/delete_item: %s", err)
        connection.send_error(msg["id"], "error", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "flaschenpost/preview_search",
        vol.Required("query"): str,
        vol.Optional("amount_prefix"): vol.Any(None, str),
        vol.Optional("category"): vol.Any(None, str),
        vol.Optional("sort_by"): vol.Any(None, str),
        vol.Optional("limit", default=10): vol.All(vol.Coerce(int), vol.Range(min=1, max=50)),
        vol.Optional("config_entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_preview_search(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Run an interactive live preview search with candidate comparison table and winner."""
    try:
        coord = _resolve_coord(hass, msg.get("config_entry_id"))
        query = str(msg["query"]).strip()
        if not query:
            connection.send_result(
                msg["id"],
                {
                    "candidates": [],
                    "winner": None,
                    "lazy_summary": "",
                    "markdown": "",
                },
            )
            return

        res = await coord.shopping_list.async_preview_search(
            query,
            amount_prefix=msg.get("amount_prefix"),
            category=msg.get("category"),
            sort_by=msg.get("sort_by"),
            limit=int(msg.get("limit", 10)),
        )
        connection.send_result(msg["id"], _serialize_obj(res))
    except Exception as err:
        _LOGGER.exception("Error in flaschenpost/preview_search: %s", err)
        connection.send_error(msg["id"], "error", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "flaschenpost/preview_lists",
        vol.Optional("shopping_lists"): list,
        vol.Optional("default_sort_by"): vol.Any(None, str),
        vol.Optional("config_entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_preview_lists(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Run a dry-run cart resolution preview across 1..N shopping lists."""
    try:
        coord = _resolve_coord(hass, msg.get("config_entry_id"))
        lists = [str(x) for x in msg["shopping_lists"]] if msg.get("shopping_lists") else None
        res = await coord.shopping_list.async_preview_lists(
            list_names=lists,
            default_sort_by=msg.get("default_sort_by"),
        )
        connection.send_result(msg["id"], _serialize_obj(res))
    except Exception as err:
        _LOGGER.exception("Error in flaschenpost/preview_lists: %s", err)
        connection.send_error(msg["id"], "error", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "flaschenpost/get_categories",
        vol.Optional("config_entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_get_categories(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Get category options for dropdown filtering."""
    try:
        coord = _resolve_coord(hass, msg.get("config_entry_id"))
        options = await coord.shopping_list.async_get_category_options()
        connection.send_result(msg["id"], {"options": options})
    except Exception as err:
        _LOGGER.exception("Error in flaschenpost/get_categories: %s", err)
        connection.send_error(msg["id"], "error", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "flaschenpost/get_cart",
        vol.Optional("config_entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_get_cart(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Get current active Flaschenpost cart status, line items, and minimum order progress."""
    try:
        coord = _resolve_coord(hass, msg.get("config_entry_id"))
        cart = await coord.client.async_get_cart()
        items = []
        for li in cart.get("lineItems") or []:
            prod = li.get("product") or {}
            variant = li.get("variant") or {}
            name_obj = li.get("name") or prod.get("name") or {}
            if isinstance(name_obj, dict):
                name = (
                    name_obj.get("de-DE")
                    or name_obj.get("de")
                    or next(iter(name_obj.values()), "")
                    or "Produkt"
                )
            else:
                name = str(name_obj or "Produkt")

            qty = li.get("quantity") or 1
            unit_price = cents_to_eur((li.get("price") or {}).get("value"))
            total_li_price = (
                cents_to_eur(li.get("totalPrice"))
                if li.get("totalPrice") is not None
                else round(unit_price * qty, 2)
            )
            if unit_price <= 0.0 and total_li_price > 0.0 and qty:
                unit_price = round(total_li_price / qty, 2)

            items.append(
                {
                    "article_id": variant.get("sku"),
                    "name": name,
                    "quantity": qty,
                    "unit_price_eur": unit_price,
                    "total_eur": total_li_price,
                }
            )

        custom_fields = (cart.get("custom") or {}).get("fields") or {}
        min_order = cents_to_eur(custom_fields.get("MinimumOrderValue")) or 25.0

        if not items:
            total_price = 0.0
            shipping_fee = 0.0
            min_order_reached = False
            remaining_to_min = round(min_order, 2)
        else:
            total_price = cents_to_eur(cart.get("totalPrice"))
            shipping_fee = cents_to_eur(
                (cart.get("shippingInfo") or {}).get("price")
            )
            goods_raw = custom_fields.get("TotalGoods")
            goods_eur = (
                cents_to_eur(goods_raw)
                if goods_raw is not None
                else round(sum(it["total_eur"] for it in items), 2)
            )
            if goods_eur <= 0.0 and items:
                goods_eur = round(sum(it["total_eur"] for it in items), 2)

            min_order_reached = goods_eur >= min_order
            remaining_to_min = round(max(0.0, min_order - goods_eur), 2)

        last_updated_iso = (
            coord.last_update_success_time.isoformat()
            if getattr(coord, "last_update_success_time", None)
            else None
        )
        next_update_iso = (
            coord.next_update_time.isoformat()
            if getattr(coord, "next_update_time", None)
            else None
        )
        update_interval_sec = (
            int(coord.update_interval.total_seconds())
            if getattr(coord, "update_interval", None)
            else 300
        )
        active_coupons = []
        if hasattr(coord, "shopping_list") and coord.shopping_list and hasattr(coord.shopping_list, "async_get_active_coupons"):
            with contextlib.suppress(Exception):
                active_coupons = await coord.shopping_list.async_get_active_coupons()

        connection.send_result(
            msg["id"],
            {
                "cart_id": cart.get("id"),
                "items": items,
                "total_eur": round(total_price, 2),
                "shipping_fee_eur": round(shipping_fee, 2),
                "min_order_value_eur": round(min_order, 2),
                "min_order_reached": min_order_reached,
                "remaining_to_min_eur": remaining_to_min,
                "active_coupons": active_coupons,
                "last_updated": last_updated_iso,
                "next_update": next_update_iso,
                "update_interval_seconds": update_interval_sec,
            },
        )
    except Exception as err:
        _LOGGER.exception("Error in flaschenpost/get_cart: %s", err)
        connection.send_error(msg["id"], "error", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "flaschenpost/sync_to_cart",
        vol.Optional("shopping_lists"): list,
        vol.Optional("clear_cart_first", default=False): bool,
        vol.Optional("mark_completed", default=False): bool,
        vol.Optional("default_sort_by"): vol.Any(None, str),
        vol.Optional("config_entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_sync_to_cart(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Sync specified shopping lists into live Flaschenpost cart."""
    try:
        coord = _resolve_coord(hass, msg.get("config_entry_id"))
        lists = [str(x) for x in msg["shopping_lists"]] if msg.get("shopping_lists") else None
        res = await coord.shopping_list.async_add_lists_to_cart(
            list_names=lists,
            clear_cart_first=bool(msg.get("clear_cart_first", False)),
            mark_completed=bool(msg.get("mark_completed", False)),
            default_sort_by=msg.get("default_sort_by"),
        )
        await coord.async_request_refresh()
        connection.send_result(msg["id"], _serialize_obj(res))
    except Exception as err:
        _LOGGER.exception("Error in flaschenpost/sync_to_cart: %s", err)
        connection.send_error(msg["id"], "error", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "flaschenpost/buy_cart",
        vol.Required("confirm"): bool,
        vol.Optional("payment_method"): str,
        vol.Optional("stored_payment_id"): str,
        vol.Optional("iban_last4"): str,
        vol.Optional("slot_type"): str,
        vol.Optional("slot_from"): str,
        vol.Optional("config_entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_buy_cart(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Submit purchase for current live cart."""
    try:
        if not msg.get("confirm"):
            connection.send_error(msg["id"], "unconfirmed", "Payment confirmation required")
            return
        coord = _resolve_coord(hass, msg.get("config_entry_id"))
        payment_method, stored_payment_id, iban_last4 = (
            await async_resolve_payment_parameters(
                coord,
                payment_method=msg.get("payment_method"),
                stored_payment_id=msg.get("stored_payment_id"),
                iban_last4=msg.get("iban_last4"),
            )
        )
        # Check and apply next active coupon code from DB
        applied_coupon = None
        if hasattr(coord, "shopping_list") and coord.shopping_list and hasattr(coord.shopping_list, "async_apply_next_coupon_to_cart"):
            applied_coupon = await coord.shopping_list.async_apply_next_coupon_to_cart()

        order = await coord.client.async_place_order(
            payment_method=payment_method,
            iban_last4=iban_last4,
            stored_payment_id=stored_payment_id,
            slot_type=msg.get("slot_type"),
            slot_from=msg.get("slot_from"),
        )

        if hasattr(coord, "shopping_list") and coord.shopping_list:
            with contextlib.suppress(Exception):
                await coord.shopping_list.async_record_order_attributions(order)

        if applied_coupon and hasattr(coord, "shopping_list") and coord.shopping_list:
            order_ref = str(order.get("id") or order.get("orderNumber") or "")
            with contextlib.suppress(Exception):
                await coord.shopping_list.async_mark_coupon_used(applied_coupon["code"], order_ref)

        await coord.async_request_refresh()
        connection.send_result(
            msg["id"],
            {
                "order_id": order.get("id"),
                "order_number": order.get("orderNumber"),
                "applied_coupon": applied_coupon["code"] if applied_coupon else None,
            },
        )
    except Exception as err:
        _LOGGER.exception("Error in flaschenpost/buy_cart: %s", err)
        connection.send_error(msg["id"], "error", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "flaschenpost/place_order",
        vol.Required("confirm"): bool,
        vol.Optional("payment_method"): str,
        vol.Optional("stored_payment_id"): str,
        vol.Optional("iban_last4"): str,
        vol.Optional("slot_type"): str,
        vol.Optional("slot_from"): str,
        vol.Optional("config_entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_place_order(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Submit purchase for current live cart (place order)."""
    await ws_buy_cart(hass, connection, msg)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "flaschenpost/get_stored_payment_id",
        vol.Optional("config_entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_get_stored_payment_id(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Fetch saved payment instruments and stored payment IDs."""
    try:
        coord = _resolve_coord(hass, msg.get("config_entry_id"))
        methods = await coord.client.async_get_saved_payment_methods()
        stored_payment_ids = [
            m["stored_payment_id"] for m in methods if m.get("stored_payment_id")
        ]
        primary_id = stored_payment_ids[0] if stored_payment_ids else None
        connection.send_result(
            msg["id"],
            {
                "stored_payment_id": primary_id,
                "stored_payment_ids": stored_payment_ids,
                "payment_methods": methods,
                "configured_payment_method": coord.config_entry.options.get(
                    CONF_DEFAULT_PAYMENT,
                    coord.config_entry.data.get(CONF_DEFAULT_PAYMENT, PAYMENT_CREDIT_CARD),
                ),
                "configured_stored_payment_id": coord.config_entry.options.get(
                    CONF_STORED_PAYMENT_ID,
                    coord.config_entry.data.get(CONF_STORED_PAYMENT_ID),
                ),
            },
        )
    except Exception as err:
        _LOGGER.exception("Error in flaschenpost/get_stored_payment_id: %s", err)
        connection.send_error(msg["id"], "error", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "flaschenpost/set_list_config",
        vol.Required("name"): str,
        vol.Optional("random_enabled"): bool,
        vol.Optional("random_count"): int,
        vol.Optional("consume_on_order"): bool,
        vol.Optional("consume_action"): vol.In(["complete", "delete"]),
        vol.Optional("config_entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_set_list_config(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Update per-list configuration (e.g. weighted randomizer settings and order consumption)."""
    try:
        coord = _resolve_coord(hass, msg.get("config_entry_id"))
        name = str(msg["name"]).strip()
        cfg = await coord.shopping_list.async_set_list_config(
            name,
            random_enabled=msg.get("random_enabled"),
            random_count=msg.get("random_count"),
            consume_on_order=msg.get("consume_on_order"),
            consume_action=msg.get("consume_action"),
        )
        coord.async_update_listeners()
        connection.send_result(msg["id"], {"name": name, "config": cfg.to_dict()})
    except Exception as err:
        _LOGGER.exception("Error in flaschenpost/set_list_config: %s", err)
        connection.send_error(msg["id"], "error", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "flaschenpost/reset_list_weights",
        vol.Required("name"): str,
        vol.Optional("config_entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_reset_list_weights(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Reset randomizer weights of all items in a shopping list back to 1.0."""
    try:
        coord = _resolve_coord(hass, msg.get("config_entry_id"))
        name = str(msg["name"]).strip()
        await coord.shopping_list.async_reset_list_weights(name)
        coord.async_update_listeners()
        connection.send_result(msg["id"], {"success": True, "name": name})
    except Exception as err:
        _LOGGER.exception("Error in flaschenpost/reset_list_weights: %s", err)
        connection.send_error(msg["id"], "error", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "flaschenpost/import_cart_to_list",
        vol.Required("name"): str,
        vol.Optional("config_entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_import_cart_to_list(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Import current Flaschenpost cart items by SKU into a new shopping list."""
    try:
        coord = _resolve_coord(hass, msg.get("config_entry_id"))
        name = str(msg["name"]).strip()
        items = await coord.shopping_list.async_import_cart_to_new_list(name)
        coord.async_update_listeners()
        connection.send_result(
            msg["id"],
            {
                "success": True,
                "name": name,
                "imported_count": len(items),
                "items": [it.to_dict() for it in items],
            },
        )
    except Exception as err:
        _LOGGER.exception("Error in flaschenpost/import_cart_to_list: %s", err)
        connection.send_error(msg["id"], "error", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "flaschenpost/refresh",
        vol.Optional("config_entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_refresh(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Trigger an immediate coordinator refresh."""
    try:
        coord = _resolve_coord(hass, msg.get("config_entry_id"))
        await coord.async_request_refresh()
        connection.send_result(
            msg["id"],
            {
                "success": True,
                "last_updated": (
                    coord.last_update_success_time.isoformat()
                    if getattr(coord, "last_update_success_time", None)
                    else None
                ),
                "next_update": (
                    coord.next_update_time.isoformat()
                    if getattr(coord, "next_update_time", None)
                    else None
                ),
                "update_interval_seconds": (
                    int(coord.update_interval.total_seconds())
                    if getattr(coord, "update_interval", None)
                    else 300
                ),
            },
        )
    except Exception as err:
        _LOGGER.exception("Error in flaschenpost/refresh: %s", err)
        connection.send_error(msg["id"], "error", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "flaschenpost/get_coupons",
        vol.Optional("status"): str,
        vol.Optional("config_entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_get_coupons(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Get all saved coupons from SQLite database."""
    try:
        coord = _resolve_coord(hass, msg.get("config_entry_id"))
        coupons = await coord.shopping_list.async_get_coupons(status=msg.get("status"))
        connection.send_result(msg["id"], {"coupons": coupons})
    except Exception as err:
        _LOGGER.exception("Error in flaschenpost/get_coupons: %s", err)
        connection.send_error(msg["id"], "error", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "flaschenpost/add_coupon",
        vol.Required("code"): str,
        vol.Optional("description"): str,
        vol.Optional("config_entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_add_coupon(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Add coupon to database and attempt immediate cart application."""
    try:
        coord = _resolve_coord(hass, msg.get("config_entry_id"))
        code = str(msg["code"]).strip()
        desc = str(msg.get("description") or "").strip()
        coupon = await coord.shopping_list.async_add_coupon(code=code, description=desc)
        try:
            if hasattr(coord.client, "async_add_discount_code"):
                await coord.client.async_add_discount_code(code)
                await coord.async_request_refresh()
        except Exception as app_err:
            _LOGGER.debug("Could not immediately apply coupon to cart: %s", app_err)
        all_coupons = await coord.shopping_list.async_get_coupons()
        connection.send_result(msg["id"], {"coupon": coupon, "coupons": all_coupons})
    except Exception as err:
        _LOGGER.exception("Error in flaschenpost/add_coupon: %s", err)
        connection.send_error(msg["id"], "error", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "flaschenpost/delete_coupon",
        vol.Required("code"): str,
        vol.Optional("config_entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_delete_coupon(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Delete coupon from database."""
    try:
        coord = _resolve_coord(hass, msg.get("config_entry_id"))
        code = str(msg["code"]).strip()
        deleted = await coord.shopping_list.async_delete_coupon(code)
        all_coupons = await coord.shopping_list.async_get_coupons()
        connection.send_result(msg["id"], {"code": code, "deleted": deleted, "coupons": all_coupons})
    except Exception as err:
        _LOGGER.exception("Error in flaschenpost/delete_coupon: %s", err)
        connection.send_error(msg["id"], "error", str(err))


def async_register_websocket_commands(hass: HomeAssistant) -> None:
    """Register all Flaschenpost WebSocket commands."""
    commands = [
        ws_get_lists,
        ws_save_list,
        ws_delete_list,
        ws_add_item,
        ws_update_item,
        ws_delete_item,
        ws_preview_search,
        ws_preview_lists,
        ws_get_categories,
        ws_get_cart,
        ws_sync_to_cart,
        ws_buy_cart,
        ws_place_order,
        ws_get_stored_payment_id,
        ws_set_list_config,
        ws_reset_list_weights,
        ws_import_cart_to_list,
        ws_refresh,
        ws_get_coupons,
        ws_add_coupon,
        ws_delete_coupon,
    ]
    for cmd in commands:
        websocket_api.async_register_command(hass, cmd)
