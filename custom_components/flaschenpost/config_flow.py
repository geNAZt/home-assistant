"""Config flow, location verification map, and interactive shopping list management for Flaschenpost."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .api import FlaschenpostApiClient, FlaschenpostAuthError, FlaschenpostError
from .const import (
    ALL_PAYMENT_METHODS,
    CONF_DEFAULT_PAYMENT,
    CONF_DELIVERY_LOCATION,
    CONF_EMAIL,
    CONF_IBAN_LAST4,
    CONF_INITIAL_SHOPPING_LISTS,
    CONF_PASSWORD,
    CONF_STORED_PAYMENT_ID,
    CONF_USER_GUID,
    CONF_WAREHOUSE_ID,
    DEFAULT_SHOPPING_LIST_NAME,
    DOMAIN,
    PAYMENT_CREDIT_CARD,
    PAYMENT_INVOICE,
    PAYMENT_PAYPAL,
    PAYMENT_SEPA,
    SORT_PRICE,
    SORT_PRICE_PER_KG,
    SORT_PRICE_PER_LITER,
    SORT_PRICE_PER_UNIT,
    SORT_RELEVANCE,
)
from .coordinator import FlaschenpostCoordinator
from .shopping_list import FlaschenpostShoppingListManager

_LOGGER = logging.getLogger(__name__)

# Menu actions for managing shopping lists
MENU_PREVIEW_SEARCH = "preview_search"
MENU_ADD_ITEM = "add_item"
MENU_EDIT_LIST = "edit_list"
MENU_DELETE_LIST = "delete_list"
MENU_VIEW_SUMMARY = "view_summary"
MENU_LOCATION_MAP = "location_map"
MENU_ORDER_PREFS = "order_prefs"
MENU_DONE = "done"

_SORT_SELECT_OPTIONS = [
    {"value": SORT_PRICE, "label": "Cheapest total price (€)"},
    {"value": SORT_PRICE_PER_KG, "label": "Cheapest per kg (€/kg)"},
    {"value": SORT_PRICE_PER_LITER, "label": "Cheapest per liter (€/L)"},
    {"value": SORT_PRICE_PER_UNIT, "label": "Cheapest per unit (€/unit)"},
    {"value": SORT_RELEVANCE, "label": "Relevance"},
]


class FlaschenpostConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Flaschenpost."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize flow state."""
        self._reauth_entry: ConfigEntry | None = None
        self._email: str = ""
        self._password: str = ""
        self._user_guid: str = ""
        self._default_payment: str = PAYMENT_CREDIT_CARD
        self._stored_payment_id: str = ""
        self._iban_last4: str = ""
        self._manage_lists_now: bool = False
        self._client: FlaschenpostApiClient | None = None
        self._temp_mgr: FlaschenpostShoppingListManager | None = None
        self._account_data: dict[str, Any] = {}
        self._warehouse_id: int | None = None
        self._delivery_location: dict[str, float] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return options flow handler."""
        return FlaschenpostOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle initial setup via Flaschenpost email & password."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            password = user_input[CONF_PASSWORD]
            client = FlaschenpostApiClient(email=email, password=password)
            try:
                login_data = await client.async_login()
                account_data = await client.async_get_my_account()
                cart = await client.async_get_cart()
            except FlaschenpostAuthError:
                errors["base"] = "invalid_auth"
            except FlaschenpostError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during Flaschenpost login")
                errors["base"] = "unknown"
            else:
                user_guid = str(
                    login_data.get("webUserExternalGuid") or email.lower()
                )
                await self.async_set_unique_id(user_guid)
                self._abort_if_unique_id_configured()

                self._email = email
                self._password = password
                self._user_guid = user_guid
                self._default_payment = user_input.get(
                    CONF_DEFAULT_PAYMENT, PAYMENT_CREDIT_CARD
                )
                self._iban_last4 = user_input.get(CONF_IBAN_LAST4, "").strip()
                self._manage_lists_now = bool(
                    user_input.get("manage_lists_now", False)
                )
                self._client = client
                self._account_data = account_data
                self._temp_mgr = FlaschenpostShoppingListManager(client=client)

                try:
                    saved_methods = await client.async_get_saved_payment_methods()
                    for sm in saved_methods:
                        if sm.get("stored_payment_id"):
                            self._stored_payment_id = sm["stored_payment_id"]
                            if CONF_DEFAULT_PAYMENT not in user_input:
                                self._default_payment = sm["payment_method"]
                            break
                except Exception as sm_err:
                    _LOGGER.debug("Could not query saved payment methods during setup: %s", sm_err)

                # Extract discovered warehouse ID
                wh_cart = ((cart.get("custom") or {}).get("fields") or {}).get(
                    "RecentlyUsedWarehouseId"
                )
                self._warehouse_id = int(wh_cart) if wh_cart else client.warehouse_id

                # Advance to interactive map step to verify location & warehouse
                return await self.async_step_location()
            finally:
                if self._client is None or errors:
                    await client.async_close()

        schema = vol.Schema(
            {
                vol.Required(CONF_EMAIL): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Optional(CONF_DEFAULT_PAYMENT, default=PAYMENT_CREDIT_CARD): vol.In(
                    ALL_PAYMENT_METHODS
                ),
                vol.Optional(CONF_IBAN_LAST4, default=""): str,
                vol.Optional("manage_lists_now", default=False): bool,
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )

    async def async_step_location(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Interactive map step to confirm location and verify warehouse assignment."""
        assert self._client is not None
        errors: dict[str, str] = {}

        # Default to Home Assistant home zone coordinates
        default_lat = float(self.hass.config.latitude)
        default_lon = float(self.hass.config.longitude)

        # If customer profile already has GeoCoordinates, prefer profile coords
        for addr in self._account_data.get("addresses") or []:
            geo = ((addr.get("custom") or {}).get("fields") or {}).get("GeoCoordinates")
            if geo and "," in geo:
                try:
                    parts = geo.split(",")
                    default_lat = float(parts[0].strip())
                    default_lon = float(parts[1].strip())
                    break
                except (TypeError, ValueError):
                    pass

        if user_input is not None:
            loc = user_input.get(CONF_DELIVERY_LOCATION) or {}
            lat = float(loc.get("latitude", default_lat))
            lon = float(loc.get("longitude", default_lon))
            self._delivery_location = {"latitude": lat, "longitude": lon}

            # Check delivery area on Flaschenpost backend
            try:
                is_area = await self._client.async_is_in_polygon_delivery_area(lat, lon)
                if is_area is False:
                    errors["base"] = "outside_delivery_area"
            except Exception:
                _LOGGER.warning("Could not verify delivery area with Flaschenpost API")

            if not errors:
                # Sync cart polygon properties to guarantee active warehouse matches
                try:
                    cart_res = await self._client.async_refresh_polygon_properties()
                    wh_id = (
                        ((cart_res.get("custom") or {}).get("fields") or {}).get(
                            "RecentlyUsedWarehouseId"
                        )
                    )
                    if wh_id:
                        self._warehouse_id = int(wh_id)
                except Exception:
                    pass

                if self._manage_lists_now:
                    return await self.async_step_manage_menu()

                return self._create_flaschenpost_entry()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_DELIVERY_LOCATION,
                    default={"latitude": default_lat, "longitude": default_lon},
                ): selector.LocationSelector(
                    selector.LocationSelectorConfig(radius=False)
                ),
            }
        )
        return self.async_show_form(
            step_id="location",
            data_schema=schema,
            description_placeholders={
                "warehouse_id": str(self._warehouse_id or "Auto"),
                "email": self._email,
            },
            errors=errors,
        )

    def _create_flaschenpost_entry(self) -> ConfigFlowResult:
        """Finalize and persist the config entry."""
        initial_lists = (
            self._temp_mgr.export_raw_lists() if self._temp_mgr else {}
        )
        data = {
            CONF_EMAIL: self._email,
            CONF_PASSWORD: self._password,
            CONF_USER_GUID: self._user_guid,
        }
        if self._warehouse_id:
            data[CONF_WAREHOUSE_ID] = self._warehouse_id
        if self._delivery_location:
            data[CONF_DELIVERY_LOCATION] = self._delivery_location
        if initial_lists:
            data[CONF_INITIAL_SHOPPING_LISTS] = initial_lists

        return self.async_create_entry(
            title=self._email,
            data=data,
            options={
                CONF_DEFAULT_PAYMENT: self._default_payment,
                CONF_STORED_PAYMENT_ID: self._stored_payment_id,
                CONF_IBAN_LAST4: self._iban_last4,
            },
        )

    async def async_step_manage_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the shopping list management menu during setup."""
        if user_input is not None:
            action = user_input.get("action")
            if action == MENU_PREVIEW_SEARCH:
                return await self.async_step_preview_search()
            if action == MENU_EDIT_LIST:
                return await self.async_step_edit_list()
            if action == MENU_VIEW_SUMMARY:
                return await self.async_step_view_summary()
            if action == MENU_DONE:
                if self._client:
                    await self._client.async_close()
                return self._create_flaschenpost_entry()

        schema = vol.Schema(
            {
                vol.Required("action", default=MENU_PREVIEW_SEARCH): vol.In(
                    {
                        MENU_PREVIEW_SEARCH: "Interactive preview search (lookup warehouse products & simulate cheapest pack counts)",
                        MENU_EDIT_LIST: "Edit or create a shopping list (multiline editor)",
                        MENU_VIEW_SUMMARY: "View shopping lists & live resolution summary",
                        MENU_DONE: "Done — Finish setting up integration",
                    }
                )
            }
        )
        return self.async_show_form(step_id="manage_menu", data_schema=schema)

    async def async_step_preview_search(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Run an interactive live search and display simulated pack plans."""
        assert self._temp_mgr is not None
        errors: dict[str, str] = {}
        category_options = await self._temp_mgr.async_get_category_options()
        cat_choices = {"": "All categories"}
        for opt in category_options:
            cat_choices[opt["value"]] = opt["label"]

        if user_input is not None:
            q = user_input.get("query", "").strip()
            if not q:
                errors["base"] = "empty_query"
            else:
                cat = user_input.get("category") or None
                amt = user_input.get("amount_prefix") or None
                sort_val = user_input.get("sort_by") or SORT_PRICE
                preview = await self._temp_mgr.async_preview_search(
                    q,
                    amount_prefix=amt,
                    category=cat,
                    sort_by=sort_val,
                )
                self.context["preview_query_params"] = {
                    "raw_query": q,
                    "amount_prefix": amt,
                    "category": cat,
                    "sort_by": sort_val,
                }
                self.context["preview_result"] = preview
                return await self.async_step_preview_result()

        prev_params = self.context.get("preview_query_params") or {}
        schema = vol.Schema(
            {
                vol.Required("query", default=prev_params.get("raw_query", "")): str,
                vol.Optional("amount_prefix", default=prev_params.get("amount_prefix") or ""): str,
                vol.Optional("category", default=prev_params.get("category") or ""): vol.In(cat_choices),
                vol.Optional("sort_by", default=prev_params.get("sort_by") or SORT_PRICE): vol.In(
                    {opt["value"]: opt["label"] for opt in _SORT_SELECT_OPTIONS}
                ),
            }
        )
        return self.async_show_form(
            step_id="preview_search",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_preview_result(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Display interactive search preview and let user add the item to a shopping list."""
        assert self._temp_mgr is not None
        preview = self.context.get("preview_result") or {}
        spec = preview.get("spec")
        lazy_summary = preview.get("lazy_summary", "")
        params = self.context.get("preview_query_params") or {}
        current_sort = (
            (spec.sort_by if spec else None)
            or params.get("sort_by")
            or SORT_PRICE
        )

        candidates = preview.get("candidates") or []
        add_options: dict[str, str] = {
            "lazy": f"Add lazy requirement: '{lazy_summary}'",
        }
        for cand in candidates[:6]:
            aid = cand.get("article_id")
            add_options[str(aid)] = (
                f"Pin #{aid} {cand.get('name')} ({cand.get('description') or '1x'}) "
                f"[{cand['packs']}x = {cand['line_total_eur']:.2f} €]"
            )
        add_options["refresh_sort"] = "↻ Update preview with selected sort order"
        add_options["back_edit"] = "← Back (edit search query & filters)"
        add_options["skip"] = "Do not add (back to menu)"

        existing_lists = list(self._temp_mgr.list_names)
        list_choices = {name: name for name in existing_lists}
        list_choices["__new__"] = "+ Create new list..."
        sort_choices = {opt["value"]: opt["label"] for opt in _SORT_SELECT_OPTIONS}

        if user_input is not None:
            choice = user_input.get("chosen_option")
            if choice == "back_edit":
                return await self.async_step_preview_search()

            new_sort = user_input.get("sort_by") or SORT_PRICE

            if choice == "refresh_sort" or new_sort != current_sort:
                raw_q = params.get("raw_query") or (spec.query if spec else "")
                amt = params.get("amount_prefix")
                cat = params.get("category") or (spec.category if spec else None)
                updated_preview = await self._temp_mgr.async_preview_search(
                    raw_q,
                    amount_prefix=amt,
                    category=cat,
                    sort_by=new_sort,
                )
                self.context["preview_query_params"] = {
                    "raw_query": raw_q,
                    "amount_prefix": amt,
                    "category": cat,
                    "sort_by": new_sort,
                }
                self.context["preview_result"] = updated_preview
                return await self.async_step_preview_result()

            target_list = user_input.get("target_list")
            new_list_name = user_input.get("new_list_name", "").strip()
            final_list_name = (
                new_list_name
                if target_list == "__new__" and new_list_name
                else (target_list if target_list != "__new__" else DEFAULT_SHOPPING_LIST_NAME)
            )

            if choice != "skip" and choice:
                if choice == "lazy":
                    line_to_add = lazy_summary
                else:
                    matched_cand = next(
                        (c for c in candidates if str(c.get("article_id")) == str(choice)),
                        None,
                    )
                    if matched_cand and spec:
                        from .shopping_list import build_pinned_spec_summary
                        line_to_add = build_pinned_spec_summary(spec, matched_cand)
                    else:
                        line_to_add = lazy_summary

                await self._temp_mgr.async_add_item(
                    line_to_add, list_name=final_list_name
                )

            return await self.async_step_manage_menu()

        schema = vol.Schema(
            {
                vol.Required("chosen_option", default="lazy"): vol.In(add_options),
                vol.Required("sort_by", default=current_sort): vol.In(sort_choices),
                vol.Required(
                    "target_list",
                    default=existing_lists[0] if existing_lists else DEFAULT_SHOPPING_LIST_NAME,
                ): vol.In(list_choices),
                vol.Optional("new_list_name", default=""): str,
            }
        )
        return self.async_show_form(
            step_id="preview_result",
            data_schema=schema,
            description_placeholders={
                "preview_markdown": preview.get("markdown", "")
            },
        )

    async def async_step_edit_list(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit or create a shopping list using a multiline text block."""
        assert self._temp_mgr is not None
        existing_lists = list(self._temp_mgr.list_names)

        if user_input is not None:
            lname = user_input.get("list_name", "").strip() or DEFAULT_SHOPPING_LIST_NAME
            content = user_input.get("items_text", "")
            lines = [line.strip().lstrip("-*•").strip() for line in content.splitlines()]
            cleaned_lines = [line_text for line_text in lines if line_text]
            await self._temp_mgr.async_save_named_list(
                lname, cleaned_lines, replace=True
            )
            return await self.async_step_manage_menu()

        current_items = "\n".join(
            it.summary for it in self._temp_mgr.get_list_items(existing_lists[0])
        )
        schema = vol.Schema(
            {
                vol.Required("list_name", default=existing_lists[0]): str,
                vol.Optional("items_text", default=current_items): str,
            }
        )
        return self.async_show_form(step_id="edit_list", data_schema=schema)

    async def async_step_view_summary(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Display live resolution preview across all managed lists."""
        assert self._temp_mgr is not None
        if user_input is not None:
            return await self.async_step_manage_menu()

        preview = await self._temp_mgr.async_preview_lists(
            list_names=self._temp_mgr.list_names
        )
        return self.async_show_form(
            step_id="view_summary",
            data_schema=vol.Schema({}),
            description_placeholders={
                "summary_markdown": preview.get("markdown", "")
            },
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle reauthentication when credentials expire or change."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm updated password for reauthentication."""
        errors: dict[str, str] = {}
        assert self._reauth_entry is not None
        email = self._reauth_entry.data[CONF_EMAIL]

        if user_input is not None:
            password = user_input[CONF_PASSWORD]
            client = FlaschenpostApiClient(email=email, password=password)
            try:
                await client.async_login()
            except FlaschenpostAuthError:
                errors["base"] = "invalid_auth"
            except FlaschenpostError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    self._reauth_entry,
                    data_updates={CONF_PASSWORD: password},
                )
            finally:
                await client.async_close()

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"email": email},
            errors=errors,
        )


class FlaschenpostOptionsFlow(OptionsFlow):
    """Interactive options flow for managing shopping lists, map location, & ordering preferences."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._entry = config_entry

    def _get_coordinator(self) -> FlaschenpostCoordinator | None:
        if DOMAIN in self.hass.data and self._entry.entry_id in self.hass.data[DOMAIN]:
            return self.hass.data[DOMAIN][self._entry.entry_id]
        return None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Main navigation hub for integration options & shopping list management."""
        coord = self._get_coordinator()
        list_count = (
            len(coord.shopping_list.list_names) if coord else 0
        )
        wh_label = str(coord.client.warehouse_id if coord else "Auto")

        if user_input is not None:
            action = user_input.get("action")
            if action == MENU_LOCATION_MAP:
                return await self.async_step_location_map()
            if action == MENU_PREVIEW_SEARCH:
                return await self.async_step_preview_search()
            if action == MENU_ADD_ITEM:
                return await self.async_step_add_item()
            if action == MENU_EDIT_LIST:
                return await self.async_step_select_list_to_edit()
            if action == MENU_DELETE_LIST:
                return await self.async_step_delete_list()
            if action == MENU_VIEW_SUMMARY:
                return await self.async_step_view_summary()
            if action == MENU_ORDER_PREFS:
                return await self.async_step_order_prefs()

        schema = vol.Schema(
            {
                vol.Required("action", default=MENU_PREVIEW_SEARCH): vol.In(
                    {
                        MENU_LOCATION_MAP: f"Verify delivery location & warehouse (Warehouse: {wh_label})",
                        MENU_PREVIEW_SEARCH: "Interactive preview search (lookup warehouse items, live prices, and simulated pack plans)",
                        MENU_ADD_ITEM: "Quick add item to a shopping list",
                        MENU_EDIT_LIST: f"Manage / edit shopping lists ({list_count} lists active)",
                        MENU_DELETE_LIST: "Delete a named shopping list",
                        MENU_VIEW_SUMMARY: "View all lists & dry-run cart resolution preview",
                        MENU_ORDER_PREFS: "Payment & ordering preferences (SEPA IBAN / Invoice)",
                    }
                )
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_location_map(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Interactive map step to adjust delivery location and re-verify warehouse assignment."""
        coord = self._get_coordinator()
        if not coord:
            return self.async_abort(reason="cannot_connect")

        stored_loc = self._entry.data.get(CONF_DELIVERY_LOCATION) or {}
        default_lat = float(
            stored_loc.get("latitude")
            or getattr(self.hass.config, "latitude", 52.3554)
        )
        default_lon = float(
            stored_loc.get("longitude")
            or getattr(self.hass.config, "longitude", 9.3862)
        )

        errors: dict[str, str] = {}
        if user_input is not None:
            loc = user_input.get(CONF_DELIVERY_LOCATION) or {}
            lat = float(loc.get("latitude", default_lat))
            lon = float(loc.get("longitude", default_lon))

            try:
                is_area = await coord.client.async_is_in_polygon_delivery_area(lat, lon)
                if is_area is False:
                    errors["base"] = "outside_delivery_area"
            except Exception:
                pass

            if not errors:
                new_wh_id = coord.client.warehouse_id
                try:
                    cart_res = await coord.client.async_refresh_polygon_properties()
                    wh_cart = (
                        ((cart_res.get("custom") or {}).get("fields") or {}).get(
                            "RecentlyUsedWarehouseId"
                        )
                    )
                    if wh_cart:
                        new_wh_id = int(wh_cart)
                except Exception:
                    pass

                self.hass.config_entries.async_update_entry(
                    self._entry,
                    data={
                        **self._entry.data,
                        CONF_DELIVERY_LOCATION: {"latitude": lat, "longitude": lon},
                        CONF_WAREHOUSE_ID: new_wh_id,
                    },
                )
                await coord.async_request_refresh()
                return await self.async_step_init()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_DELIVERY_LOCATION,
                    default={"latitude": default_lat, "longitude": default_lon},
                ): selector.LocationSelector(
                    selector.LocationSelectorConfig(radius=False)
                ),
            }
        )
        return self.async_show_form(
            step_id="location_map",
            data_schema=schema,
            description_placeholders={
                "warehouse_id": str(coord.client.warehouse_id or "Auto"),
            },
            errors=errors,
        )

    async def async_step_preview_search(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Run interactive live catalog search in options flow."""
        coord = self._get_coordinator()
        if not coord:
            return self.async_abort(reason="cannot_connect")

        category_options = await coord.shopping_list.async_get_category_options()
        cat_choices = {"": "All categories"}
        for opt in category_options:
            cat_choices[opt["value"]] = opt["label"]

        errors: dict[str, str] = {}
        if user_input is not None:
            q = user_input.get("query", "").strip()
            if not q:
                errors["base"] = "empty_query"
            else:
                cat = user_input.get("category") or None
                amt = user_input.get("amount_prefix") or None
                sort_val = user_input.get("sort_by") or SORT_PRICE
                preview = await coord.shopping_list.async_preview_search(
                    q,
                    amount_prefix=amt,
                    category=cat,
                    sort_by=sort_val,
                )
                self.context["options_preview_query_params"] = {
                    "raw_query": q,
                    "amount_prefix": amt,
                    "category": cat,
                    "sort_by": sort_val,
                }
                self.context["options_preview_result"] = preview
                return await self.async_step_preview_result()

        prev_params = self.context.get("options_preview_query_params") or {}
        schema = vol.Schema(
            {
                vol.Required("query", default=prev_params.get("raw_query", "")): str,
                vol.Optional("amount_prefix", default=prev_params.get("amount_prefix") or ""): str,
                vol.Optional("category", default=prev_params.get("category") or ""): vol.In(cat_choices),
                vol.Optional("sort_by", default=prev_params.get("sort_by") or SORT_PRICE): vol.In(
                    {opt["value"]: opt["label"] for opt in _SORT_SELECT_OPTIONS}
                ),
            }
        )
        return self.async_show_form(
            step_id="preview_search", data_schema=schema, errors=errors
        )

    async def async_step_preview_result(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Display search results with simulated pack counts and allow adding to a list."""
        coord = self._get_coordinator()
        if not coord:
            return self.async_abort(reason="cannot_connect")

        preview = self.context.get("options_preview_result") or {}
        spec = preview.get("spec")
        lazy_summary = preview.get("lazy_summary", "")
        params = self.context.get("options_preview_query_params") or {}
        current_sort = (
            (spec.sort_by if spec else None)
            or params.get("sort_by")
            or SORT_PRICE
        )

        candidates = preview.get("candidates") or []
        add_options: dict[str, str] = {
            "lazy": f"Add lazy requirement: '{lazy_summary}'",
        }
        for cand in candidates[:6]:
            aid = cand.get("article_id")
            add_options[str(aid)] = (
                f"Pin #{aid} {cand.get('name')} ({cand.get('description') or '1x'}) "
                f"[{cand['packs']}x = {cand['line_total_eur']:.2f} €]"
            )
        add_options["refresh_sort"] = "↻ Update preview with selected sort order"
        add_options["back_edit"] = "← Back (edit search query & filters)"
        add_options["skip"] = "Do not add (back to menu)"

        existing_lists = list(coord.shopping_list.list_names)
        list_choices = {name: name for name in existing_lists}
        list_choices["__new__"] = "+ Create new list..."
        sort_choices = {opt["value"]: opt["label"] for opt in _SORT_SELECT_OPTIONS}

        if user_input is not None:
            choice = user_input.get("chosen_option")
            if choice == "back_edit":
                return await self.async_step_preview_search()

            new_sort = user_input.get("sort_by") or SORT_PRICE

            if choice == "refresh_sort" or new_sort != current_sort:
                raw_q = params.get("raw_query") or (spec.query if spec else "")
                amt = params.get("amount_prefix")
                cat = params.get("category") or (spec.category if spec else None)
                updated_preview = await coord.shopping_list.async_preview_search(
                    raw_q,
                    amount_prefix=amt,
                    category=cat,
                    sort_by=new_sort,
                )
                self.context["options_preview_query_params"] = {
                    "raw_query": raw_q,
                    "amount_prefix": amt,
                    "category": cat,
                    "sort_by": new_sort,
                }
                self.context["options_preview_result"] = updated_preview
                return await self.async_step_preview_result()

            target_list = user_input.get("target_list")
            new_list_name = user_input.get("new_list_name", "").strip()
            final_list_name = (
                new_list_name
                if target_list == "__new__" and new_list_name
                else (target_list if target_list != "__new__" else DEFAULT_SHOPPING_LIST_NAME)
            )

            if choice != "skip" and choice:
                if choice == "lazy":
                    line_to_add = lazy_summary
                else:
                    matched_cand = next(
                        (c for c in candidates if str(c.get("article_id")) == str(choice)),
                        None,
                    )
                    if matched_cand and spec:
                        from .shopping_list import build_pinned_spec_summary
                        line_to_add = build_pinned_spec_summary(spec, matched_cand)
                    else:
                        line_to_add = lazy_summary

                await coord.shopping_list.async_add_item(
                    line_to_add, list_name=final_list_name
                )
                coord.async_update_listeners()

            return await self.async_step_init()

        schema = vol.Schema(
            {
                vol.Required("chosen_option", default="lazy"): vol.In(add_options),
                vol.Required("sort_by", default=current_sort): vol.In(sort_choices),
                vol.Required(
                    "target_list",
                    default=existing_lists[0] if existing_lists else DEFAULT_SHOPPING_LIST_NAME,
                ): vol.In(list_choices),
                vol.Optional("new_list_name", default=""): str,
            }
        )
        return self.async_show_form(
            step_id="preview_result",
            data_schema=schema,
            description_placeholders={
                "preview_markdown": preview.get("markdown", "")
            },
        )

    async def async_step_add_item(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Quickly add an item spec directly to a shopping list."""
        coord = self._get_coordinator()
        if not coord:
            return self.async_abort(reason="cannot_connect")

        existing_lists = list(coord.shopping_list.list_names)
        list_choices = {name: name for name in existing_lists}
        list_choices["__new__"] = "+ Create new list..."

        if user_input is not None:
            raw = user_input.get("item_spec", "").strip()
            if raw:
                target_list = user_input.get("target_list")
                new_list_name = user_input.get("new_list_name", "").strip()
                final_name = (
                    new_list_name
                    if target_list == "__new__" and new_list_name
                    else (target_list if target_list != "__new__" else DEFAULT_SHOPPING_LIST_NAME)
                )
                await coord.shopping_list.async_add_item(raw, list_name=final_name)
                coord.async_update_listeners()
            return await self.async_step_init()

        schema = vol.Schema(
            {
                vol.Required("item_spec"): str,
                vol.Required(
                    "target_list",
                    default=existing_lists[0] if existing_lists else DEFAULT_SHOPPING_LIST_NAME,
                ): vol.In(list_choices),
                vol.Optional("new_list_name", default=""): str,
            }
        )
        return self.async_show_form(step_id="add_item", data_schema=schema)

    async def async_step_select_list_to_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select which shopping list to edit or choose to create a new one."""
        coord = self._get_coordinator()
        if not coord:
            return self.async_abort(reason="cannot_connect")

        existing_lists = list(coord.shopping_list.list_names)
        choices = {name: f"{name} ({len(coord.shopping_list.get_list_items(name))} items)" for name in existing_lists}
        choices["__new__"] = "+ Create brand-new list"

        if user_input is not None:
            sel = user_input.get("list_to_edit")
            if sel == "__new__":
                self.context["edit_list_name"] = "Custom list"
                self.context["edit_list_content"] = ""
            else:
                self.context["edit_list_name"] = sel
                items = coord.shopping_list.get_list_items(sel)
                self.context["edit_list_content"] = "\n".join(it.summary for it in items)
            return await self.async_step_edit_list_content()

        schema = vol.Schema(
            {
                vol.Required(
                    "list_to_edit",
                    default=existing_lists[0] if existing_lists else "__new__",
                ): vol.In(choices)
            }
        )
        return self.async_show_form(step_id="select_list_to_edit", data_schema=schema)

    async def async_step_edit_list_content(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Multiline text editor for a selected shopping list."""
        coord = self._get_coordinator()
        if not coord:
            return self.async_abort(reason="cannot_connect")

        default_name = self.context.get("edit_list_name", DEFAULT_SHOPPING_LIST_NAME)
        default_content = self.context.get("edit_list_content", "")

        if user_input is not None:
            lname = user_input.get("list_name", "").strip() or DEFAULT_SHOPPING_LIST_NAME
            content = user_input.get("items_text", "")
            lines = [line.strip().lstrip("-*•").strip() for line in content.splitlines()]
            cleaned_lines = [line_text for line_text in lines if line_text]
            await coord.shopping_list.async_save_named_list(
                lname, cleaned_lines, replace=True
            )
            coord.async_update_listeners()
            return await self.async_step_init()

        schema = vol.Schema(
            {
                vol.Required("list_name", default=default_name): str,
                vol.Optional("items_text", default=default_content): str,
            }
        )
        return self.async_show_form(step_id="edit_list_content", data_schema=schema)

    async def async_step_delete_list(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Delete a named shopping list."""
        coord = self._get_coordinator()
        if not coord:
            return self.async_abort(reason="cannot_connect")

        existing_lists = [n for n in coord.shopping_list.list_names if n != DEFAULT_SHOPPING_LIST_NAME]
        if not existing_lists:
            return self.async_show_form(
                step_id="delete_list",
                data_schema=vol.Schema({}),
                errors={"base": "no_custom_lists"},
            )

        choices = {name: name for name in existing_lists}
        if user_input is not None:
            target = user_input.get("list_name")
            if target:
                await coord.shopping_list.async_delete_named_list(target)
                coord.async_update_listeners()
            return await self.async_step_init()

        schema = vol.Schema({vol.Required("list_name"): vol.In(choices)})
        return self.async_show_form(step_id="delete_list", data_schema=schema)

    async def async_step_view_summary(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Display live resolution preview across all shopping lists."""
        coord = self._get_coordinator()
        if not coord:
            return self.async_abort(reason="cannot_connect")

        if user_input is not None:
            return await self.async_step_init()

        preview = await coord.shopping_list.async_preview_lists(
            list_names=coord.shopping_list.list_names
        )
        return self.async_show_form(
            step_id="view_summary",
            data_schema=vol.Schema({}),
            description_placeholders={
                "summary_markdown": preview.get("markdown", "")
            },
        )

    async def async_step_order_prefs(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure checkout payment method, fetching saved instruments from Flaschenpost."""
        coord = self._get_coordinator()
        saved_methods: list[dict[str, Any]] = []
        if coord and coord.client:
            try:
                saved_methods = await coord.client.async_get_saved_payment_methods()
            except Exception as err:
                _LOGGER.warning("Could not fetch saved Flaschenpost payment methods: %s", err)

        payment_choices: dict[str, str] = {}
        for sm in saved_methods:
            payment_choices[sm["id"]] = sm["label"]

        if not payment_choices:
            payment_choices = {
                PAYMENT_CREDIT_CARD: "Kreditkarte (Credit Card via Adyen)",
                PAYMENT_PAYPAL: "PayPal",
                PAYMENT_SEPA: "SEPA Lastschrift",
                PAYMENT_INVOICE: "Rechnung (Direct Invoice)",
            }

        cur_pay = self._entry.options.get(
            CONF_DEFAULT_PAYMENT,
            self._entry.data.get(CONF_DEFAULT_PAYMENT, PAYMENT_CREDIT_CARD),
        )
        cur_stored = self._entry.options.get(
            CONF_STORED_PAYMENT_ID,
            self._entry.data.get(CONF_STORED_PAYMENT_ID, ""),
        )
        cur_iban = self._entry.options.get(
            CONF_IBAN_LAST4,
            self._entry.data.get(CONF_IBAN_LAST4, ""),
        )

        default_choice = cur_pay
        if cur_stored and f"{cur_pay}:{cur_stored}" in payment_choices:
            default_choice = f"{cur_pay}:{cur_stored}"
        elif cur_pay == PAYMENT_SEPA and cur_iban and f"sepa:{cur_iban}" in payment_choices:
            default_choice = f"sepa:{cur_iban}"
        elif cur_pay not in payment_choices:
            default_choice = next(iter(payment_choices))

        if user_input is not None:
            chosen_key = user_input.get("chosen_payment_method") or default_choice
            chosen_method = chosen_key
            chosen_stored_id = ""
            chosen_iban = user_input.get(CONF_IBAN_LAST4, "").strip()

            if ":" in chosen_key:
                prefix, suffix = chosen_key.split(":", 1)
                chosen_method = prefix
                if prefix == PAYMENT_SEPA:
                    chosen_iban = suffix
                else:
                    chosen_stored_id = suffix

            return self.async_create_entry(
                title="",
                data={
                    CONF_DEFAULT_PAYMENT: chosen_method,
                    CONF_STORED_PAYMENT_ID: chosen_stored_id,
                    CONF_IBAN_LAST4: chosen_iban,
                },
            )

        schema = vol.Schema(
            {
                vol.Required(
                    "chosen_payment_method",
                    default=default_choice,
                ): vol.In(payment_choices),
                vol.Optional(
                    CONF_IBAN_LAST4,
                    default=cur_iban,
                ): str,
            }
        )
        return self.async_show_form(
            step_id="order_prefs",
            data_schema=schema,
            description_placeholders={
                "saved_count": str(len(saved_methods)),
            },
        )
