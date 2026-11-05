"""Constants for the Flaschenpost Home Assistant integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "flaschenpost"

BASE_URL = "https://www.flaschenpost.de"
FRONTEND_VERSION = "v-2026-10-29t08:40:30.097z"
APP_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36 "
    "flaschenpost App Android 4.2.4"
)

# Public client APIM subscription keys from Flaschenpost webshop bundle
SLOTS_APIM_KEY = "1a3c9dbd77134dc3ae9eebc67ad939d2"
RECOMMENDATIONS_APIM_KEY = (
    "a0c649b18a0642ad96216e0888727e81;product=webshop-b2c-clientside"
)

# Config / Options keys
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_USER_GUID = "web_user_guid"
CONF_DEFAULT_PAYMENT = "default_payment_method"
CONF_IBAN_LAST4 = "iban_last4"
CONF_INITIAL_SHOPPING_LISTS = "initial_shopping_lists"
CONF_WAREHOUSE_ID = "warehouse_id"
CONF_DELIVERY_LOCATION = "delivery_location"
CONF_STORED_PAYMENT_ID = "stored_payment_id"

PAYMENT_CREDIT_CARD = "credit_card"
PAYMENT_PAYPAL = "paypal"
PAYMENT_SEPA = "sepa"
PAYMENT_INVOICE = "invoice"
ALL_PAYMENT_METHODS = [
    PAYMENT_CREDIT_CARD,
    PAYMENT_PAYPAL,
    PAYMENT_SEPA,
    PAYMENT_INVOICE,
]

ADYEN_CLIENT_KEY = "live_5KSNWJ7X25HWHCXAUCLVBKVKVY3IFXJT"

# Product search sort modes
SORT_RELEVANCE = "relevance"
SORT_PRICE = "price"
SORT_PRICE_PER_UNIT = "price_per_unit"
SORT_PRICE_PER_KG = "price_per_kg"
SORT_PRICE_PER_LITER = "price_per_liter"
SORT_OPTIONS = [
    SORT_RELEVANCE,
    SORT_PRICE,
    SORT_PRICE_PER_UNIT,
    SORT_PRICE_PER_KG,
    SORT_PRICE_PER_LITER,
]

# Shopping list item amount/unit categories
UNIT_TYPE_UNIT = "unit"
UNIT_TYPE_WEIGHT_G = "weight_g"
UNIT_TYPE_VOLUME_ML = "volume_ml"
UNIT_TYPE_CRATE = "crate"

DEFAULT_SHOPPING_LIST_NAME = "Shopping list"

# Polling intervals
SCAN_INTERVAL_IDLE = timedelta(minutes=5)
SCAN_INTERVAL_ACTIVE_DELIVERY = timedelta(seconds=60)

# Article origin enum (_b in webshop bundle)
ORIGIN_UNKNOWN = 0
ORIGIN_SEARCH = 14
ORIGIN_FAVORITES = 15
ORIGIN_CART = 16
ORIGIN_REORDER = 20

# Storage for local HA Flaschenpost shopping lists
STORAGE_VERSION = 2
STORAGE_KEY_PREFIX = f"{DOMAIN}_shopping_list"

# Service names
SERVICE_SEARCH_PRODUCTS = "search_products"
SERVICE_GET_CATEGORIES = "get_categories"
SERVICE_GET_FAVORITES = "get_favorites"
SERVICE_CREATE_NEW_CART = "create_new_cart"
SERVICE_ADD_TO_CART = "add_to_cart"
SERVICE_SAVE_SHOPPING_LIST = "save_shopping_list"
SERVICE_DELETE_SHOPPING_LIST = "delete_shopping_list"
SERVICE_GET_SHOPPING_LISTS = "get_shopping_lists"
SERVICE_ADD_LISTS_TO_CART = "add_shopping_lists_to_cart"
SERVICE_SYNC_LIST_TO_CART = "sync_shopping_list_to_cart"
SERVICE_REORDER_LAST_ORDER = "reorder_last_order"
SERVICE_SELECT_DELIVERY_SLOT = "select_delivery_slot"
SERVICE_PLACE_ORDER = "place_order"
SERVICE_GET_STORED_PAYMENT_ID = "get_stored_payment_id"
SERVICE_GET_PAYMENT_METHODS = "get_payment_methods"
SERVICE_IMPORT_CART_TO_LIST = "import_cart_to_list"
SERVICE_SET_LIST_CONFIG = "set_shopping_list_config"
SERVICE_ADD_COUPON = "add_coupon"
SERVICE_GET_COUPONS = "get_coupons"
SERVICE_DELETE_COUPON = "delete_coupon"

DEFAULT_ITEM_WEIGHT: float = 1.0

