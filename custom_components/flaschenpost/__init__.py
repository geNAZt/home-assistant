"""The Flaschenpost Home Assistant integration."""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store

from .api import FlaschenpostApiClient, FlaschenpostError
from .const import (
    ALL_PAYMENT_METHODS,
    CONF_DEFAULT_PAYMENT,
    CONF_EMAIL,
    CONF_IBAN_LAST4,
    CONF_INITIAL_SHOPPING_LISTS,
    CONF_PASSWORD,
    CONF_STORED_PAYMENT_ID,
    DOMAIN,
    ORIGIN_REORDER,
    ORIGIN_SEARCH,
    PAYMENT_CREDIT_CARD,
    SERVICE_ADD_COUPON,
    SERVICE_ADD_LISTS_TO_CART,
    SERVICE_ADD_TO_CART,
    SERVICE_CREATE_NEW_CART,
    SERVICE_DELETE_COUPON,
    SERVICE_DELETE_SHOPPING_LIST,
    SERVICE_GET_CATEGORIES,
    SERVICE_GET_COUPONS,
    SERVICE_GET_FAVORITES,
    SERVICE_GET_PAYMENT_METHODS,
    SERVICE_GET_SHOPPING_LISTS,
    SERVICE_GET_STORED_PAYMENT_ID,
    SERVICE_IMPORT_CART_TO_LIST,
    SERVICE_PLACE_ORDER,
    SERVICE_REORDER_LAST_ORDER,
    SERVICE_SAVE_SHOPPING_LIST,
    SERVICE_SEARCH_PRODUCTS,
    SERVICE_SELECT_DELIVERY_SLOT,
    SERVICE_SET_LIST_CONFIG,
    SERVICE_SYNC_LIST_TO_CART,
    SORT_OPTIONS,
    SORT_RELEVANCE,
    STORAGE_KEY_PREFIX,
    STORAGE_VERSION,
    UNIT_TYPE_UNIT,
)
from .coordinator import FlaschenpostCoordinator
from .shopping_list import (
    FlaschenpostShoppingListManager,
    parse_shopping_item_spec,
    resolve_sort_alias,
)
from .storage import FlaschenpostSqliteStorage
from .websocket_api import async_register_websocket_commands

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.TODO,
    Platform.BUTTON,
    Platform.EVENT,
]

ALL_SERVICES = (
    SERVICE_SEARCH_PRODUCTS,
    SERVICE_GET_CATEGORIES,
    SERVICE_GET_FAVORITES,
    SERVICE_CREATE_NEW_CART,
    SERVICE_ADD_TO_CART,
    SERVICE_SAVE_SHOPPING_LIST,
    SERVICE_DELETE_SHOPPING_LIST,
    SERVICE_GET_SHOPPING_LISTS,
    SERVICE_ADD_LISTS_TO_CART,
    SERVICE_SYNC_LIST_TO_CART,
    SERVICE_REORDER_LAST_ORDER,
    SERVICE_SELECT_DELIVERY_SLOT,
    SERVICE_PLACE_ORDER,
    SERVICE_IMPORT_CART_TO_LIST,
    SERVICE_SET_LIST_CONFIG,
    SERVICE_GET_STORED_PAYMENT_ID,
    SERVICE_GET_PAYMENT_METHODS,
)


def _resolve_coordinator(
    hass: HomeAssistant, config_entry_id: str | None = None
) -> FlaschenpostCoordinator:
    """Resolve target FlaschenpostCoordinator for a service call."""
    entries: dict[str, FlaschenpostCoordinator] = hass.data.get(DOMAIN, {})
    if not entries:
        raise ServiceValidationError("No Flaschenpost account is configured")
    if config_entry_id:
        coord = entries.get(config_entry_id)
        if not coord:
            raise ServiceValidationError(
                f"Flaschenpost config_entry_id '{config_entry_id}' not found"
            )
        return coord
    return next(iter(entries.values()))


def _coerce_sort_by(value: Any) -> str:
    """Validate sort_by accepting both canonical keys and aliases like `'€/kg'`."""
    raw = str(value or SORT_RELEVANCE)
    resolved = resolve_sort_alias(raw)
    if resolved in SORT_OPTIONS:
        return resolved
    raise vol.Invalid(
        f"Unsupported sort_by '{raw}'. Expected one of {SORT_OPTIONS} or aliases like '€/kg', '€/unit', '€/L'."
    )


def _coerce_string_list(value: Any) -> list[str]:
    """Accept either a YAML list of strings or a newline/comma-separated string."""
    if isinstance(value, str):
        if "\n" in value:
            parts = [line.strip().lstrip("-*•").strip() for line in value.splitlines()]
        else:
            parts = [p.strip() for p in value.split(",")]
        return [p for p in parts if p]
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    raise vol.Invalid("Expected a list of strings or newline-separated text")


def _coerce_item_lines(value: Any) -> list[str]:
    """Accept a YAML list of item lines or multiline text block."""
    if isinstance(value, str):
        lines = [line.strip().lstrip("-*•").strip() for line in value.splitlines()]
        return [ln for ln in lines if ln]
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    raise vol.Invalid("Expected a list of item strings or multiline text")


async def _async_fetch_external_ha_todo_items(
    hass: HomeAssistant,
    coord: FlaschenpostCoordinator,
    requested_names: list[str],
) -> dict[str, list[str]]:
    """Fetch open items from external Home Assistant `todo.*` entities matched by name or entity_id."""
    external: dict[str, list[str]] = {}
    if not hass.services.has_service("todo", "get_items"):
        return external

    internal_lower = {n.lower() for n in coord.shopping_list.list_names}
    todo_states = hass.states.async_all("todo")

    for req_name in requested_names:
        if req_name.strip().lower() in internal_lower:
            continue

        needle = req_name.strip().lower()
        matched_entity_id: str | None = None
        for st in todo_states:
            eid = st.entity_id
            fname = str(st.attributes.get("friendly_name") or "").strip().lower()
            if eid.lower() == needle or fname == needle:
                matched_entity_id = eid
                break

        if not matched_entity_id:
            continue

        resp = await hass.services.async_call(
            "todo",
            "get_items",
            {"entity_id": matched_entity_id, "status": ["needs_action"]},
            blocking=True,
            return_response=True,
        )
        entity_data = (resp or {}).get(matched_entity_id) or {}
        raw_items = entity_data.get("items") or []
        summaries = [
            str(it.get("summary")).strip()
            for it in raw_items
            if isinstance(it, dict) and str(it.get("summary") or "").strip()
        ]
        external[req_name] = summaries

    return external


PANEL_URL_PATH = "flaschenpost"
FRONTEND_URL = "/flaschenpost_static"
PANEL_JS_VERSION = "2.1.0"


async def _async_register_frontend_panel(hass: HomeAssistant) -> None:
    """Register Flaschenpost sidebar panel and Lovelace card resource."""
    frontend_dir = Path(__file__).parent / "frontend"

    # 1. Register static path for frontend assets
    if hasattr(hass.http, "async_register_static_paths"):
        try:
            from homeassistant.components.http import StaticPathConfig

            await hass.http.async_register_static_paths(
                [StaticPathConfig(FRONTEND_URL, str(frontend_dir), cache_headers=False)]
            )
        except Exception as err:
            _LOGGER.debug("Could not use async_register_static_paths: %s", err)
    elif hasattr(hass.http, "register_static_path"):
        try:
            hass.http.register_static_path(
                FRONTEND_URL, str(frontend_dir), cache_headers=False
            )
        except Exception as err:
            _LOGGER.debug("Could not use register_static_path: %s", err)

    # 2. Register extra JS url for Lovelace Card picker
    try:
        from homeassistant.components import frontend

        if hasattr(frontend, "add_extra_js_url"):
            frontend.add_extra_js_url(hass, f"{FRONTEND_URL}/flaschenpost-panel.js?v={PANEL_JS_VERSION}")
    except Exception as err:
        _LOGGER.debug("Could not register extra js url: %s", err)

    # 3. Register custom sidebar panel
    try:
        from homeassistant.components import panel_custom

        current_panels = hass.data.get("frontend_panels", {})
        if PANEL_URL_PATH not in current_panels:
            await panel_custom.async_register_panel(
                hass=hass,
                frontend_url_path=PANEL_URL_PATH,
                webcomponent_name="flaschenpost-panel",
                sidebar_title="Flaschenpost",
                sidebar_icon="mdi:cart-outline",
                module_url=f"{FRONTEND_URL}/flaschenpost-panel.js?v={PANEL_JS_VERSION}",
                require_admin=False,
            )
    except Exception as err:
        _LOGGER.debug("Could not register Flaschenpost sidebar panel: %s", err)


def _async_unregister_frontend_panel(hass: HomeAssistant) -> None:
    """Unregister Flaschenpost sidebar panel."""
    try:
        from homeassistant.components import frontend

        if hasattr(frontend, "async_remove_panel"):
            frontend.async_remove_panel(hass, PANEL_URL_PATH)
    except Exception as err:
        _LOGGER.debug("Could not remove sidebar panel: %s", err)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Flaschenpost from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    client = FlaschenpostApiClient(
        email=entry.data[CONF_EMAIL],
        password=entry.data[CONF_PASSWORD],
    )
    db_path = hass.config.path("flaschenpost.db") if hasattr(hass, "config") and hasattr(hass.config, "path") else "flaschenpost.db"
    storage = FlaschenpostSqliteStorage(db_path=db_path, hass=hass)
    store: Store[dict[str, Any]] = Store(
        hass, STORAGE_VERSION, f"{STORAGE_KEY_PREFIX}_{entry.entry_id}"
    )
    shopping_list = FlaschenpostShoppingListManager(
        client=client, storage=storage, store=store
    )
    await shopping_list.async_load()

    # Clean up legacy shopping list To-Do entities (keep only live cart To-Do entity)
    try:
        from homeassistant.helpers import entity_registry as er

        ent_reg = er.async_get(hass)
        entries = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        for ent in entries:
            if ent.domain == "todo" and (
                ent.unique_id == "shopping_list"
                or ent.unique_id.startswith("shopping_list_")
            ):
                ent_reg.async_remove(ent.entity_id)
    except Exception as err:
        _LOGGER.debug("Could not clean up legacy shopping list todo entities: %s", err)

    # Seed initial shopping lists configured during setup flow if present
    initial_seed = entry.data.get(CONF_INITIAL_SHOPPING_LISTS)
    if isinstance(initial_seed, dict):
        for lname, lines in initial_seed.items():
            if not shopping_list.get_list_items(str(lname)) and isinstance(lines, list):
                await shopping_list.async_save_named_list(
                    str(lname),
                    [str(x) for x in lines if str(x).strip()],
                    replace=True,
                )

    coordinator = FlaschenpostCoordinator(
        hass=hass,
        entry=entry,
        client=client,
        shopping_list=shopping_list,
    )
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_register_services(hass)
    async_register_websocket_commands(hass)
    await _async_register_frontend_panel(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Flaschenpost config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: FlaschenpostCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.client.async_close()
        if not hass.data[DOMAIN]:
            for svc in ALL_SERVICES:
                hass.services.async_remove(DOMAIN, svc)
            _async_unregister_frontend_panel(hass)
    return unload_ok


def _async_register_services(hass: HomeAssistant) -> None:
    """Register domain services once."""
    if hass.services.has_service(DOMAIN, SERVICE_PLACE_ORDER):
        return

    async def handle_import_cart(call: ServiceCall) -> ServiceResponse:
        coord = _resolve_coordinator(hass, call.data.get("config_entry_id"))
        list_name = str(call.data["list_name"]).strip()
        items = await coord.shopping_list.async_import_cart_to_new_list(list_name)
        coord.async_update_listeners()
        return {
            "list_name": list_name,
            "imported_count": len(items),
            "items": [it.to_dict() for it in items],
        }

    async def handle_set_list_config(call: ServiceCall) -> ServiceResponse:
        coord = _resolve_coordinator(hass, call.data.get("config_entry_id"))
        list_name = str(call.data["list_name"]).strip()
        cfg = await coord.shopping_list.async_set_list_config(
            list_name,
            random_enabled=call.data.get("random_enabled"),
            random_count=call.data.get("random_count"),
            consume_on_order=call.data.get("consume_on_order"),
            consume_action=call.data.get("consume_action"),
        )
        coord.async_update_listeners()
        return {"list_name": list_name, **cfg.to_dict()}

    async def handle_search_products(call: ServiceCall) -> ServiceResponse:
        coord = _resolve_coordinator(hass, call.data.get("config_entry_id"))
        raw_query: str = call.data["query"]
        explicit_cat: str | None = call.data.get("category")
        spec = parse_shopping_item_spec(raw_query)
        eff_cat = explicit_cat or spec.category
        eff_query = spec.query if spec.category else raw_query

        results = await coord.client.async_search_products(
            query=eff_query,
            limit=call.data.get("limit", 10),
            category=eff_cat,
            name_contains=call.data.get("name_contains"),
            sort_by=call.data.get("sort_by", SORT_RELEVANCE),
            max_price_eur=call.data.get("max_price_eur"),
            max_price_per_unit_eur=call.data.get("max_price_per_unit_eur"),
            max_price_per_kg_eur=call.data.get("max_price_per_kg_eur"),
            max_price_per_liter_eur=call.data.get("max_price_per_liter_eur"),
            only_available=call.data.get("only_available", True),
            only_crates=call.data.get("only_crates", False),
        )
        return {"count": len(results), "results": results}

    async def handle_get_categories(call: ServiceCall) -> ServiceResponse:
        coord = _resolve_coordinator(hass, call.data.get("config_entry_id"))
        cats = await coord.client.async_get_categories()
        return {"warehouse_id": coord.client.warehouse_id, "categories": cats}

    async def handle_get_favorites(call: ServiceCall) -> ServiceResponse:
        coord = _resolve_coordinator(hass, call.data.get("config_entry_id"))
        favorites = await coord.client.async_get_favorites(
            limit=call.data.get("limit", 25)
        )
        return {"favorites": favorites}

    async def handle_create_new_cart(call: ServiceCall) -> ServiceResponse:
        coord = _resolve_coordinator(hass, call.data.get("config_entry_id"))
        try:
            cart = await coord.client.async_create_new_cart()
            if hasattr(coord, "shopping_list") and coord.shopping_list:
                with contextlib.suppress(Exception):
                    await coord.shopping_list.async_clear_cart_attributions()
        except FlaschenpostError as err:
            raise HomeAssistantError(str(err)) from err

        await coord.async_request_refresh()
        return {
            "cart_id": cart.get("id"),
            "line_items": len(cart.get("lineItems") or []),
        }

    async def handle_add_to_cart(call: ServiceCall) -> ServiceResponse:
        coord = _resolve_coordinator(hass, call.data.get("config_entry_id"))
        article_id: int | None = call.data.get("article_id")
        query: str | None = call.data.get("query")
        explicit_cat: str | None = call.data.get("category")
        name_contains: str | None = call.data.get("name_contains")
        explicit_sort: str | None = call.data.get("sort_by")
        quantity: int = call.data.get("quantity", 1)
        additive: bool = call.data.get("additive", True)
        clear_cart_first: bool = call.data.get("clear_cart_first", False)

        selected_product: dict[str, Any] | None = None
        resolved_qty = quantity

        if article_id is None:
            if not query:
                raise ServiceValidationError(
                    "Either 'article_id' or 'query' must be provided"
                )
            spec = parse_shopping_item_spec(query)
            eff_cat = explicit_cat or spec.category
            if (
                spec.unit_type != UNIT_TYPE_UNIT
                or spec.display_amount > 1.0
                or eff_cat is not None
            ):
                scaled_base = spec.target_amount_base * max(1, quantity)
                resolution = await coord.client.async_resolve_item_for_cart(
                    spec.query,
                    category=eff_cat,
                    unit_type=spec.unit_type,
                    target_amount_base=scaled_base,
                    article_id=spec.explicit_article_id,
                    sort_by=explicit_sort or spec.sort_by,
                    name_contains=name_contains,
                )
                if not resolution or resolution.get("article_id") is None:
                    raise HomeAssistantError(
                        f"No matching available Flaschenpost product found for '{query}'"
                    )
                article_id = int(resolution["article_id"])
                resolved_qty = int(resolution["quantity"])
                selected_product = resolution.get("product")
            else:
                sort_mode = explicit_sort or spec.sort_by or SORT_RELEVANCE
                matches = await coord.client.async_search_products(
                    spec.query,
                    limit=1,
                    category=eff_cat,
                    name_contains=name_contains,
                    sort_by=sort_mode,
                    only_available=True,
                )
                if not matches or matches[0].get("article_id") is None:
                    raise HomeAssistantError(
                        f"No matching available Flaschenpost product found for '{query}' (sort_by={sort_mode})"
                    )
                selected_product = matches[0]
                article_id = int(selected_product["article_id"])

        try:
            if clear_cart_first:
                base_cart = await coord.client.async_create_new_cart()
            else:
                base_cart = coord.data.cart

            target_qty = resolved_qty
            if additive and resolved_qty > 0 and not clear_cart_first:
                for li in base_cart.get("lineItems") or []:
                    if str((li.get("variant") or {}).get("sku")) == str(article_id):
                        target_qty += int(li.get("quantity") or 0)
                        break

            cart = await coord.client.async_set_cart_positions(
                [
                    {
                        "articleId": int(article_id),
                        "origin": ORIGIN_SEARCH,
                        "quantityTotal": max(0, target_qty),
                    }
                ]
            )
        except FlaschenpostError as err:
            raise HomeAssistantError(str(err)) from err

        await coord.async_request_refresh()
        resp: dict[str, Any] = {
            "article_id": int(article_id),
            "quantity_total": max(0, target_qty),
            "cart_id": cart.get("id"),
        }
        if selected_product:
            resp["product"] = selected_product
        return resp

    async def handle_save_shopping_list(call: ServiceCall) -> ServiceResponse:
        coord = _resolve_coordinator(hass, call.data.get("config_entry_id"))
        list_name: str = call.data["name"]
        items: list[str] = call.data["items"]
        replace: bool = call.data.get("replace", True)
        sort_by: str | None = call.data.get("sort_by")

        saved = await coord.shopping_list.async_save_named_list(
            list_name=list_name,
            items=items,
            replace=replace,
            sort_by=sort_by,
        )
        coord.async_update_listeners()
        return {
            "name": list_name,
            "item_count": len(saved),
            "items": [it.to_dict() for it in saved],
        }

    async def handle_delete_shopping_list(call: ServiceCall) -> ServiceResponse:
        coord = _resolve_coordinator(hass, call.data.get("config_entry_id"))
        list_name: str = call.data["name"]
        deleted = await coord.shopping_list.async_delete_named_list(list_name)
        coord.async_update_listeners()
        return {"name": list_name, "deleted": deleted}

    async def handle_get_shopping_lists(call: ServiceCall) -> ServiceResponse:
        coord = _resolve_coordinator(hass, call.data.get("config_entry_id"))
        return {"shopping_lists": coord.shopping_list.get_all_lists_dict()}

    async def handle_add_lists_to_cart(call: ServiceCall) -> ServiceResponse:
        coord = _resolve_coordinator(hass, call.data.get("config_entry_id"))
        list_names: list[str] = call.data["shopping_lists"]
        external = await _async_fetch_external_ha_todo_items(hass, coord, list_names)
        try:
            res = await coord.shopping_list.async_add_lists_to_cart(
                list_names=list_names,
                external_list_items=external,
                mark_completed=call.data.get("mark_completed", False),
                clear_cart_first=call.data.get("clear_cart_first", False),
                default_sort_by=call.data.get("default_sort_by"),
            )
        except FlaschenpostError as err:
            raise HomeAssistantError(str(err)) from err

        await coord.async_request_refresh()
        return res

    async def handle_sync_list_to_cart(call: ServiceCall) -> ServiceResponse:
        coord = _resolve_coordinator(hass, call.data.get("config_entry_id"))
        list_names: list[str] | None = call.data.get("shopping_lists")
        external = (
            await _async_fetch_external_ha_todo_items(hass, coord, list_names)
            if list_names
            else None
        )
        res = await coord.shopping_list.async_add_lists_to_cart(
            list_names=list_names,
            external_list_items=external,
            mark_completed=call.data.get("mark_completed", True),
            clear_cart_first=call.data.get("clear_cart_first", False),
        )
        await coord.async_request_refresh()
        return res

    async def handle_reorder_last_order(call: ServiceCall) -> ServiceResponse:
        coord = _resolve_coordinator(hass, call.data.get("config_entry_id"))
        order_id: str | None = call.data.get("order_id")
        past = coord.data.past_orders
        if not past:
            raise HomeAssistantError("No past orders found to reorder")

        target_order = (
            next((o for o in past if o.get("id") == order_id), None)
            if order_id
            else past[0]
        )
        if not target_order:
            raise HomeAssistantError(f"Order '{order_id}' not found in order history")

        positions: list[dict[str, int]] = []
        for li in target_order.get("lineItems") or []:
            sku = (li.get("variant") or {}).get("sku")
            qty = int(li.get("quantity") or 0)
            if sku and str(sku).isdigit() and qty > 0:
                positions.append(
                    {
                        "articleId": int(sku),
                        "origin": ORIGIN_REORDER,
                        "quantityTotal": qty,
                    }
                )
        if not positions:
            raise HomeAssistantError("Selected order has no reorderable line items")

        cart = await coord.client.async_set_cart_positions(positions)
        await coord.async_request_refresh()
        return {
            "reordered_order_number": target_order.get("orderNumber"),
            "added_positions": len(positions),
            "cart_id": cart.get("id"),
        }

    async def handle_select_slot(call: ServiceCall) -> ServiceResponse:
        coord = _resolve_coordinator(hass, call.data.get("config_entry_id"))
        try:
            cart = await coord.client.async_select_delivery_slot(
                slot_type=call.data.get("slot_type"),
                slot_from=call.data.get("slot_from"),
            )
        except FlaschenpostError as err:
            raise HomeAssistantError(str(err)) from err

        await coord.async_request_refresh()
        s_fields = (cart.get("shippingCustomFields") or {}).get("fields") or {}
        return {
            "selected_type": s_fields.get("SelectedDeliveryType"),
            "selected_from": s_fields.get("SelectedDeliveryFrom"),
            "selected_until": s_fields.get("SelectedDeliveryUntil"),
        }

    async def handle_get_stored_payment_id(call: ServiceCall) -> ServiceResponse:
        coord = _resolve_coordinator(hass, call.data.get("config_entry_id"))
        try:
            methods = await coord.client.async_get_saved_payment_methods()
        except FlaschenpostError as err:
            raise HomeAssistantError(str(err)) from err

        stored_payment_ids = [
            m["stored_payment_id"] for m in methods if m.get("stored_payment_id")
        ]
        primary_id = stored_payment_ids[0] if stored_payment_ids else None
        return {
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
        }

    async def handle_place_order(call: ServiceCall) -> ServiceResponse:
        if not call.data.get("confirm"):
            raise ServiceValidationError(
                "Refusing to place a paid order without 'confirm: true'"
            )
        coord = _resolve_coordinator(hass, call.data.get("config_entry_id"))
        opts = coord.config_entry.options
        payment_method = (
            call.data.get("payment_method")
            or opts.get(CONF_DEFAULT_PAYMENT)
            or coord.config_entry.data.get(CONF_DEFAULT_PAYMENT, PAYMENT_CREDIT_CARD)
        )
        stored_payment_id = (
            call.data.get("stored_payment_id")
            or opts.get(CONF_STORED_PAYMENT_ID)
            or coord.config_entry.data.get(CONF_STORED_PAYMENT_ID)
            or None
        )
        iban_last4 = (
            call.data.get("iban_last4")
            or opts.get(CONF_IBAN_LAST4)
            or coord.config_entry.data.get(CONF_IBAN_LAST4)
            or None
        )
        # Check and apply next active coupon code from DB
        applied_coupon = None
        if hasattr(coord, "shopping_list") and coord.shopping_list:
            applied_coupon = await coord.shopping_list.async_apply_next_coupon_to_cart()

        try:
            order = await coord.client.async_place_order(
                payment_method=payment_method,
                iban_last4=iban_last4,
                stored_payment_id=stored_payment_id,
                slot_type=call.data.get("slot_type"),
                slot_from=call.data.get("slot_from"),
            )
        except FlaschenpostError as err:
            raise HomeAssistantError(str(err)) from err

        if hasattr(coord, "shopping_list") and coord.shopping_list:
            with contextlib.suppress(Exception):
                await coord.shopping_list.async_record_order_attributions(order)

        if applied_coupon and hasattr(coord, "shopping_list") and coord.shopping_list:
            order_ref = str(order.get("id") or order.get("orderNumber") or "")
            await coord.shopping_list.async_mark_coupon_used(applied_coupon["code"], order_ref)

        await coord.async_request_refresh()
        return {
            "order_id": order.get("id"),
            "order_number": order.get("orderNumber"),
            "applied_coupon": applied_coupon["code"] if applied_coupon else None,
        }

    async def handle_add_coupon(call: ServiceCall) -> ServiceResponse:
        """Add or reactivate a coupon code in the database."""
        coord = _resolve_coordinator(hass, call.data.get("config_entry_id"))
        code = str(call.data["code"]).strip()
        desc = str(call.data.get("description") or "").strip()
        coupon = await coord.shopping_list.async_add_coupon(code=code, description=desc)
        with contextlib.suppress(Exception):
            await coord.client.async_add_discount_code(code)
            await coord.async_request_refresh()
        return coupon

    async def handle_get_coupons(call: ServiceCall) -> ServiceResponse:
        """Fetch saved coupons from database."""
        coord = _resolve_coordinator(hass, call.data.get("config_entry_id"))
        status = call.data.get("status")
        coupons = await coord.shopping_list.async_get_coupons(status=status)
        return {"coupons": coupons}

    async def handle_delete_coupon(call: ServiceCall) -> ServiceResponse:
        """Delete a saved coupon code from the database."""
        coord = _resolve_coordinator(hass, call.data.get("config_entry_id"))
        code = str(call.data["code"]).strip()
        deleted = await coord.shopping_list.async_delete_coupon(code)
        return {"code": code, "deleted": deleted}

    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_CART_TO_LIST,
        handle_import_cart,
        schema=vol.Schema(
            {
                vol.Optional("config_entry_id"): cv.string,
                vol.Required("list_name"): cv.string,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_LIST_CONFIG,
        handle_set_list_config,
        schema=vol.Schema(
            {
                vol.Optional("config_entry_id"): cv.string,
                vol.Required("list_name"): cv.string,
                vol.Optional("random_enabled"): cv.boolean,
                vol.Optional("random_count"): cv.positive_int,
                vol.Optional("consume_on_order"): cv.boolean,
                vol.Optional("consume_action"): vol.In(["complete", "delete"]),
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEARCH_PRODUCTS,
        handle_search_products,
        schema=vol.Schema(
            {
                vol.Optional("config_entry_id"): cv.string,
                vol.Required("query"): cv.string,
                vol.Optional("category"): cv.string,
                vol.Optional("name_contains"): cv.string,
                vol.Optional("sort_by", default=SORT_RELEVANCE): _coerce_sort_by,
                vol.Optional("max_price_eur"): vol.Coerce(float),
                vol.Optional("max_price_per_unit_eur"): vol.Coerce(float),
                vol.Optional("max_price_per_kg_eur"): vol.Coerce(float),
                vol.Optional("max_price_per_liter_eur"): vol.Coerce(float),
                vol.Optional("only_available", default=True): cv.boolean,
                vol.Optional("only_crates", default=False): cv.boolean,
                vol.Optional("limit", default=10): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=50)
                ),
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_CATEGORIES,
        handle_get_categories,
        schema=vol.Schema(
            {
                vol.Optional("config_entry_id"): cv.string,
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_FAVORITES,
        handle_get_favorites,
        schema=vol.Schema(
            {
                vol.Optional("config_entry_id"): cv.string,
                vol.Optional("limit", default=25): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=50)
                ),
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_CREATE_NEW_CART,
        handle_create_new_cart,
        schema=vol.Schema(
            {
                vol.Optional("config_entry_id"): cv.string,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_TO_CART,
        handle_add_to_cart,
        schema=vol.Schema(
            {
                vol.Optional("config_entry_id"): cv.string,
                vol.Optional("article_id"): vol.Coerce(int),
                vol.Optional("query"): cv.string,
                vol.Optional("category"): cv.string,
                vol.Optional("name_contains"): cv.string,
                vol.Optional("sort_by"): _coerce_sort_by,
                vol.Optional("quantity", default=1): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=99)
                ),
                vol.Optional("additive", default=True): cv.boolean,
                vol.Optional("clear_cart_first", default=False): cv.boolean,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SAVE_SHOPPING_LIST,
        handle_save_shopping_list,
        schema=vol.Schema(
            {
                vol.Optional("config_entry_id"): cv.string,
                vol.Required("name"): cv.string,
                vol.Required("items"): _coerce_item_lines,
                vol.Optional("replace", default=True): cv.boolean,
                vol.Optional("sort_by"): _coerce_sort_by,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_DELETE_SHOPPING_LIST,
        handle_delete_shopping_list,
        schema=vol.Schema(
            {
                vol.Optional("config_entry_id"): cv.string,
                vol.Required("name"): cv.string,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_SHOPPING_LISTS,
        handle_get_shopping_lists,
        schema=vol.Schema(
            {
                vol.Optional("config_entry_id"): cv.string,
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_LISTS_TO_CART,
        handle_add_lists_to_cart,
        schema=vol.Schema(
            {
                vol.Optional("config_entry_id"): cv.string,
                vol.Required("shopping_lists"): _coerce_string_list,
                vol.Optional("clear_cart_first", default=False): cv.boolean,
                vol.Optional("mark_completed", default=False): cv.boolean,
                vol.Optional("default_sort_by"): _coerce_sort_by,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SYNC_LIST_TO_CART,
        handle_sync_list_to_cart,
        schema=vol.Schema(
            {
                vol.Optional("config_entry_id"): cv.string,
                vol.Optional("shopping_lists"): _coerce_string_list,
                vol.Optional("mark_completed", default=True): cv.boolean,
                vol.Optional("clear_cart_first", default=False): cv.boolean,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_REORDER_LAST_ORDER,
        handle_reorder_last_order,
        schema=vol.Schema(
            {
                vol.Optional("config_entry_id"): cv.string,
                vol.Optional("order_id"): cv.string,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SELECT_DELIVERY_SLOT,
        handle_select_slot,
        schema=vol.Schema(
            {
                vol.Optional("config_entry_id"): cv.string,
                vol.Optional("slot_type"): cv.string,
                vol.Optional("slot_from"): cv.string,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )

    order_schema = vol.Schema(
        {
            vol.Optional("config_entry_id"): cv.string,
            vol.Required("confirm"): cv.boolean,
            vol.Optional("payment_method"): vol.In(ALL_PAYMENT_METHODS),
            vol.Optional("stored_payment_id"): cv.string,
            vol.Optional("iban_last4"): cv.string,
            vol.Optional("slot_type"): cv.string,
            vol.Optional("slot_from"): cv.string,
        }
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_STORED_PAYMENT_ID,
        handle_get_stored_payment_id,
        schema=vol.Schema({vol.Optional("config_entry_id"): cv.string}),
        supports_response=SupportsResponse.ONLY,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_PAYMENT_METHODS,
        handle_get_stored_payment_id,
        schema=vol.Schema({vol.Optional("config_entry_id"): cv.string}),
        supports_response=SupportsResponse.ONLY,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_PLACE_ORDER,
        handle_place_order,
        schema=order_schema,
        supports_response=SupportsResponse.OPTIONAL,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_COUPON,
        handle_add_coupon,
        schema=vol.Schema(
            {
                vol.Optional("config_entry_id"): cv.string,
                vol.Required("code"): cv.string,
                vol.Optional("description", default=""): cv.string,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_COUPONS,
        handle_get_coupons,
        schema=vol.Schema(
            {
                vol.Optional("config_entry_id"): cv.string,
                vol.Optional("status"): cv.string,
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_DELETE_COUPON,
        handle_delete_coupon,
        schema=vol.Schema(
            {
                vol.Optional("config_entry_id"): cv.string,
                vol.Required("code"): cv.string,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
