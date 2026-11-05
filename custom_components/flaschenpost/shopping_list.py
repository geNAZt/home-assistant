"""Shopping list item parsing, category filtering, interactive preview, and lazy cart resolution."""

from __future__ import annotations

import logging
import math
import random
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from .api import (
    FlaschenpostApiClient,
    compute_optimal_pack_plan,
    extract_negative_keywords,
)
from .const import (
    DEFAULT_ITEM_WEIGHT,
    DEFAULT_SHOPPING_LIST_NAME,
    ORIGIN_FAVORITES,
    ORIGIN_SEARCH,
    SORT_OPTIONS,
    SORT_PRICE,
    SORT_PRICE_PER_KG,
    SORT_PRICE_PER_LITER,
    SORT_PRICE_PER_UNIT,
    SORT_RELEVANCE,
    UNIT_TYPE_CRATE,
    UNIT_TYPE_UNIT,
    UNIT_TYPE_VOLUME_ML,
    UNIT_TYPE_WEIGHT_G,
)

_LOGGER = logging.getLogger(__name__)

_INLINE_SORT_TAG_REGEX = re.compile(
    r"\s*\[(?P<tag>[^\]]+)\]\s*",
    re.IGNORECASE,
)

_TRAILING_AID_REGEX = re.compile(r"(?:\s+#(?P<aid>\d+))\s*$")

# Matches leading amount + explicit unit:
#   "500g Reis & Getreide::Milchreis", "1kg Naturjoghurt",
#   "1,5kg Puten Schnitzel", "1 Kiste Pepsi Zero", "2x Gerolsteiner Medium"
_AMOUNT_WITH_UNIT_REGEX = re.compile(
    r"^\s*(?P<amt>\d+(?:[.,]\d+)?)\s*(?P<unit>"
    r"kilogramm|kilo|kg|gramm|gr|g|"
    r"milliliter|ml|liter|ltr|l|"
    r"kisten|kiste|kästen|kaesten|kasten|crates|crate|"
    r"stück|stueck|stck|stk|packungen|packung|pack|pkg|becher|bund|flaschen|flasche|dosen|dose|"
    r"x|×|\*"
    r")(?:\s+|\b\s*)(?P<name>\S.*?)\s*$",
    re.IGNORECASE,
)

# Matches bare integer count + whitespace + product name: "2 Salatgurken"
_BARE_COUNT_REGEX = re.compile(
    r"^\s*(?P<amt>\d+)\s+(?P<name>\S.*?)\s*$",
)

_SORT_TAG_ALIASES: dict[str, str] = {
    "relevance": SORT_RELEVANCE,
    "price": SORT_PRICE,
    "cheapest": SORT_PRICE,
    "total": SORT_PRICE,
    "total_price": SORT_PRICE,
    "auto": SORT_PRICE,
    "€": SORT_PRICE,
    "eur": SORT_PRICE,
    "price_per_unit": SORT_PRICE_PER_UNIT,
    "€/unit": SORT_PRICE_PER_UNIT,
    "eur/unit": SORT_PRICE_PER_UNIT,
    "€/stk": SORT_PRICE_PER_UNIT,
    "€/st": SORT_PRICE_PER_UNIT,
    "€/stück": SORT_PRICE_PER_UNIT,
    "price_per_kg": SORT_PRICE_PER_KG,
    "€/kg": SORT_PRICE_PER_KG,
    "eur/kg": SORT_PRICE_PER_KG,
    "kg": SORT_PRICE_PER_KG,
    "price_per_liter": SORT_PRICE_PER_LITER,
    "€/l": SORT_PRICE_PER_LITER,
    "eur/l": SORT_PRICE_PER_LITER,
    "€/liter": SORT_PRICE_PER_LITER,
}


@dataclass
class ParsedShoppingSpec:
    """Normalized representation of a shopping list line item."""

    query: str
    category: str | None = None
    unit_type: str = UNIT_TYPE_UNIT
    target_amount_base: float = 1.0
    display_amount: float = 1.0
    display_unit: str = "x"
    explicit_article_id: int | None = None
    sort_by: str | None = None
    negative_keywords: list[str] = field(default_factory=list)


def resolve_sort_alias(raw: str | None) -> str | None:
    """Resolve user-friendly sort aliases like `'€/kg'` or `'€/unit'` to canonical sort keys."""
    if not raw:
        return None
    normalized = re.sub(r"\s+", "", raw.strip().lower())
    if normalized in _SORT_TAG_ALIASES:
        return _SORT_TAG_ALIASES[normalized]
    if raw.strip().lower() in SORT_OPTIONS:
        return raw.strip().lower()
    return None


def extract_inline_sort_hint(text: str) -> tuple[str, str | None]:
    """Extract optional inline sort tag like `[€/kg]` or `[€/unit]` from text."""
    detected_sort: str | None = None

    def _replace(match: re.Match[str]) -> str:
        nonlocal detected_sort
        resolved = resolve_sort_alias(match.group("tag"))
        if resolved is not None:
            detected_sort = resolved
            return " "
        return match.group(0)

    cleaned = _INLINE_SORT_TAG_REGEX.sub(_replace, text)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned, detected_sort


def _split_category_and_query(raw_name: str) -> tuple[str | None, str]:
    """Split optional `'Category::Product'` or `'Parent::Subcategory::Product'` syntax."""
    if "::" not in raw_name:
        return None, raw_name.strip()
    cat_part, prod_part = raw_name.rsplit("::", 1)
    cat_clean = re.sub(r"\s*::\s*", "::", cat_part.strip())
    prod_clean = prod_part.strip()
    if not cat_clean or not prod_clean:
        return None, raw_name.strip()
    return cat_clean, prod_clean


def _format_number_de(val: float) -> str:
    if abs(val - round(val)) < 1e-6:
        return str(round(val))
    formatted = f"{val:.3f}".rstrip("0").rstrip(".")
    return formatted.replace(".", ",")


def format_base_amount(unit_type: str, amount_base: float) -> str:
    """Format a base amount (`weight_g`, `volume_ml`, `crate`, `unit`) for human display."""
    if unit_type == UNIT_TYPE_WEIGHT_G:
        if amount_base >= 1000.0:
            kg_val = amount_base / 1000.0
            if abs(kg_val * 100 - round(kg_val * 100)) < 1e-4:
                return f"{_format_number_de(kg_val)}kg"
        return f"{_format_number_de(amount_base)}g"
    if unit_type == UNIT_TYPE_VOLUME_ML:
        if amount_base >= 1000.0:
            l_val = amount_base / 1000.0
            if abs(l_val * 100 - round(l_val * 100)) < 1e-4:
                return f"{_format_number_de(l_val)}L"
        return f"{_format_number_de(amount_base)}ml"
    if unit_type == UNIT_TYPE_CRATE:
        cnt = max(1, round(amount_base))
        label = "Kiste" if cnt == 1 else "Kisten"
        return f"{cnt} {label}"
    cnt = max(1, round(amount_base))
    return f"{cnt}x"


def format_shopping_spec_summary(spec: ParsedShoppingSpec) -> str:
    """Format a `ParsedShoppingSpec` into a clean canonical summary."""
    suffix = f" #{spec.explicit_article_id}" if spec.explicit_article_id is not None else ""
    neg_suffix = "".join(f" !{kw}" for kw in spec.negative_keywords)
    sort_tag = ""
    if spec.sort_by:
        reverse_tag = {
            SORT_PRICE_PER_KG: "€/kg",
            SORT_PRICE_PER_UNIT: "€/unit",
            SORT_PRICE_PER_LITER: "€/L",
            SORT_PRICE: "price",
            SORT_RELEVANCE: "relevance",
        }.get(spec.sort_by)
        if reverse_tag and spec.sort_by != SORT_PRICE:
            sort_tag = f" [{reverse_tag}]"

    core_name = f"{spec.category}::{spec.query}" if spec.category else spec.query

    if spec.unit_type == UNIT_TYPE_UNIT:
        qty = max(1, round(spec.target_amount_base))
        prefix = f"{qty}x " if qty > 1 else ""
        return f"{prefix}{core_name}{neg_suffix}{sort_tag}{suffix}"

    amt_prefix = format_base_amount(spec.unit_type, spec.target_amount_base)
    return f"{amt_prefix} {core_name}{neg_suffix}{sort_tag}{suffix}"


def parse_shopping_item_spec(summary: str) -> ParsedShoppingSpec:
    """Parse shopping item summary including `Category::Product`, kg/g, L/ml, Kiste, `#id`, and `[sort]`.

    Examples:
      - `'500g Reis & Getreide::Milchreis'` -> weight_g=500, category='Reis & Getreide', query='Milchreis'
      - `'1kg Naturjoghurt'` -> weight_g=1000, query='Naturjoghurt'
      - `'1,5kg Puten Schnitzel'` -> weight_g=1500, query='Puten Schnitzel'
      - `'1 Kiste Pepsi Zero'` -> crate=1, query='Pepsi Zero'
      - `'Heidelbeeren !zucker'` -> query='Heidelbeeren', negative_keywords=['zucker']
    """
    cleaned, sort_hint = extract_inline_sort_hint(summary.strip())
    cleaned, neg_kws = extract_negative_keywords(cleaned)

    explicit_aid: int | None = None
    aid_match = _TRAILING_AID_REGEX.search(cleaned)
    if aid_match:
        explicit_aid = int(aid_match.group("aid"))
        cleaned = cleaned[: aid_match.start()].strip()

    unit_match = _AMOUNT_WITH_UNIT_REGEX.match(cleaned)
    if unit_match:
        amt = float(unit_match.group("amt").replace(",", "."))
        raw_unit = unit_match.group("unit").lower()
        cat, name = _split_category_and_query(unit_match.group("name"))

        if raw_unit in ("kg", "kilo", "kilogramm"):
            return ParsedShoppingSpec(
                query=name,
                category=cat,
                unit_type=UNIT_TYPE_WEIGHT_G,
                target_amount_base=round(amt * 1000.0, 2),
                display_amount=amt,
                display_unit="kg",
                explicit_article_id=explicit_aid,
                sort_by=sort_hint,
                negative_keywords=neg_kws,
            )
        if raw_unit in ("g", "gr", "gramm"):
            return ParsedShoppingSpec(
                query=name,
                category=cat,
                unit_type=UNIT_TYPE_WEIGHT_G,
                target_amount_base=round(amt, 2),
                display_amount=amt,
                display_unit="g",
                explicit_article_id=explicit_aid,
                sort_by=sort_hint,
                negative_keywords=neg_kws,
            )
        if raw_unit in ("l", "ltr", "liter"):
            return ParsedShoppingSpec(
                query=name,
                category=cat,
                unit_type=UNIT_TYPE_VOLUME_ML,
                target_amount_base=round(amt * 1000.0, 2),
                display_amount=amt,
                display_unit="L",
                explicit_article_id=explicit_aid,
                sort_by=sort_hint,
                negative_keywords=neg_kws,
            )
        if raw_unit in ("ml", "milliliter"):
            return ParsedShoppingSpec(
                query=name,
                category=cat,
                unit_type=UNIT_TYPE_VOLUME_ML,
                target_amount_base=round(amt, 2),
                display_amount=amt,
                display_unit="ml",
                explicit_article_id=explicit_aid,
                sort_by=sort_hint,
                negative_keywords=neg_kws,
            )
        if raw_unit in (
            "kiste",
            "kisten",
            "kasten",
            "kästen",
            "kaesten",
            "crate",
            "crates",
        ):
            return ParsedShoppingSpec(
                query=name,
                category=cat,
                unit_type=UNIT_TYPE_CRATE,
                target_amount_base=max(1.0, amt),
                display_amount=max(1.0, amt),
                display_unit="Kiste",
                explicit_article_id=explicit_aid,
                sort_by=sort_hint,
                negative_keywords=neg_kws,
            )

        return ParsedShoppingSpec(
            query=name,
            category=cat,
            unit_type=UNIT_TYPE_UNIT,
            target_amount_base=max(1.0, amt),
            display_amount=max(1.0, amt),
            display_unit="x",
            explicit_article_id=explicit_aid,
            sort_by=sort_hint,
            negative_keywords=neg_kws,
        )

    bare_match = _BARE_COUNT_REGEX.match(cleaned)
    if bare_match:
        amt = float(int(bare_match.group("amt")))
        cat, name = _split_category_and_query(bare_match.group("name"))
        return ParsedShoppingSpec(
            query=name,
            category=cat,
            unit_type=UNIT_TYPE_UNIT,
            target_amount_base=max(1.0, amt),
            display_amount=max(1.0, amt),
            display_unit="x",
            explicit_article_id=explicit_aid,
            sort_by=sort_hint,
            negative_keywords=neg_kws,
        )

    # Also support "Reis & Getreide::500g Milchreis" (category prefix before amount)
    cat, rhs = _split_category_and_query(cleaned)
    if cat is not None:
        inner = parse_shopping_item_spec(rhs)
        inner.category = f"{cat}::{inner.category}" if inner.category else cat
        if explicit_aid is not None and inner.explicit_article_id is None:
            inner.explicit_article_id = explicit_aid
        if sort_hint is not None and inner.sort_by is None:
            inner.sort_by = sort_hint
        if neg_kws:
            inner.negative_keywords = list(dict.fromkeys(inner.negative_keywords + neg_kws))
        return inner

    return ParsedShoppingSpec(
        query=cleaned,
        category=None,
        unit_type=UNIT_TYPE_UNIT,
        target_amount_base=1.0,
        display_amount=1.0,
        display_unit="x",
        explicit_article_id=explicit_aid,
        sort_by=sort_hint,
        negative_keywords=neg_kws,
    )


def build_preview_spec(
    raw_query: str,
    *,
    amount_prefix: str | None = None,
    category_override: str | None = None,
    sort_by: str | None = None,
) -> ParsedShoppingSpec:
    """Combine interactive search inputs (`query`, optional `amount_prefix`, `category`, `sort_by`) into a `ParsedShoppingSpec`."""
    base_spec = parse_shopping_item_spec(raw_query)

    if amount_prefix and amount_prefix.strip():
        amt_str = amount_prefix.strip()
        # Strip any existing amount from raw_query by applying amt_str to the extracted core name
        neg_suffix = "".join(f" !{kw}" for kw in base_spec.negative_keywords)
        core = (
            f"{base_spec.category}::{base_spec.query}"
            if base_spec.category
            else base_spec.query
        )
        combined = parse_shopping_item_spec(f"{amt_str} {core}{neg_suffix}")
        if base_spec.explicit_article_id is not None:
            combined.explicit_article_id = base_spec.explicit_article_id
        if base_spec.sort_by is not None:
            combined.sort_by = base_spec.sort_by
        combined.negative_keywords = list(
            dict.fromkeys(base_spec.negative_keywords + combined.negative_keywords)
        )
        base_spec = combined

    if category_override and category_override.strip():
        base_spec.category = re.sub(r"\s*::\s*", "::", category_override.strip())

    if sort_by and sort_by.strip().lower() not in ("", "auto"):
        resolved_sort = resolve_sort_alias(sort_by) or sort_by.strip().lower()
        if resolved_sort in SORT_OPTIONS:
            base_spec.sort_by = resolved_sort
    elif sort_by and sort_by.strip().lower() == "auto":
        base_spec.sort_by = SORT_PRICE

    return base_spec


def build_pinned_spec_summary(
    spec: ParsedShoppingSpec, product: dict[str, Any]
) -> str:
    """Build a shopping list line that pins a specific product `#article_id` while preserving amount/unit."""
    aid = product.get("article_id")
    pname = str(product.get("name") or spec.query).strip()
    pinned_spec = ParsedShoppingSpec(
        query=pname,
        category=spec.category,
        unit_type=spec.unit_type,
        target_amount_base=spec.target_amount_base,
        display_amount=spec.display_amount,
        display_unit=spec.display_unit,
        explicit_article_id=int(aid) if aid is not None else None,
        sort_by=None,
    )
    return format_shopping_spec_summary(pinned_spec)


def estimate_candidate_pack_plan(
    spec: ParsedShoppingSpec, product: dict[str, Any]
) -> dict[str, Any]:
    """Calculate how many packs of `product` are required to satisfy `spec` and at what cost, considering Mengenrabatte."""
    pkg_w = product.get("package_weight_g")
    pkg_v = product.get("package_volume_ml")

    if spec.unit_type == UNIT_TYPE_WEIGHT_G and pkg_w and float(pkg_w) > 0:
        min_packs = max(1, math.ceil(spec.target_amount_base / float(pkg_w) - 1e-9))
    elif spec.unit_type == UNIT_TYPE_VOLUME_ML and pkg_v and float(pkg_v) > 0:
        min_packs = max(1, math.ceil(spec.target_amount_base / float(pkg_v) - 1e-9))
    elif spec.unit_type == UNIT_TYPE_CRATE:
        min_packs = max(1, math.ceil(spec.target_amount_base - 1e-9))
    else:
        min_packs = max(1, math.ceil(spec.target_amount_base - 1e-9))

    packs, eff_unit_price, line_total, tier_applied = compute_optimal_pack_plan(
        product, min_packs
    )

    if spec.unit_type == UNIT_TYPE_WEIGHT_G and pkg_w and float(pkg_w) > 0:
        delivered = format_base_amount(UNIT_TYPE_WEIGHT_G, packs * float(pkg_w))
    elif spec.unit_type == UNIT_TYPE_VOLUME_ML and pkg_v and float(pkg_v) > 0:
        delivered = format_base_amount(UNIT_TYPE_VOLUME_ML, packs * float(pkg_v))
    elif spec.unit_type == UNIT_TYPE_CRATE:
        delivered = format_base_amount(UNIT_TYPE_CRATE, float(packs))
    else:
        delivered = f"{packs}x"

    if tier_applied and pkg_w and float(pkg_w) > 0:
        eff_kg = round(eff_unit_price / (float(pkg_w) / 1000.0), 2)
        base_metric = f"{eff_kg:.2f} EUR/kg (Mengenrabatt)"
    elif product.get("price_per_kg_eur") is not None:
        base_metric = f"{float(product['price_per_kg_eur']):.2f} EUR/kg"
    elif tier_applied and pkg_v and float(pkg_v) > 0:
        eff_l = round(eff_unit_price / (float(pkg_v) / 1000.0), 2)
        base_metric = f"{eff_l:.2f} EUR/L (Mengenrabatt)"
    elif product.get("price_per_liter_eur") is not None:
        base_metric = f"{float(product['price_per_liter_eur']):.2f} EUR/L"
    elif product.get("price_per_unit_eur") is not None:
        eff_u = eff_unit_price if tier_applied else float(product["price_per_unit_eur"])
        base_metric = f"{eff_u:.2f} EUR/unit" + (" (Mengenrabatt)" if tier_applied else "")
    else:
        base_metric = f"{eff_unit_price:.2f} EUR" + (" (Mengenrabatt)" if tier_applied else "")

    return {
        "packs": packs,
        "delivered_amount": delivered,
        "line_total_eur": line_total,
        "unit_price_eur": eff_unit_price,
        "base_metric": base_metric,
        "tier_applied": tier_applied,
        "pinned_summary": build_pinned_spec_summary(spec, product),
    }


def parse_shopping_item_details(
    summary: str,
) -> tuple[int, str, int | None, str | None]:
    """Backward-compatible tuple parser `(quantity, query, explicit_article_id, sort_by)`."""
    spec = parse_shopping_item_spec(summary)
    qty = (
        max(1, round(spec.target_amount_base))
        if spec.unit_type in (UNIT_TYPE_UNIT, UNIT_TYPE_CRATE)
        else 1
    )
    return qty, spec.query, spec.explicit_article_id, spec.sort_by


def parse_shopping_item_summary(summary: str) -> tuple[int, str, int | None]:
    """Parse summary like `2x Gerolsteiner Medium #410` -> `(2, 'Gerolsteiner Medium', 410)`."""
    quantity, name, article_id, _sort_hint = parse_shopping_item_details(summary)
    return quantity, name, article_id


def format_shopping_item_summary(
    quantity: int, name: str, article_id: int | None
) -> str:
    """Format canonical shopping list item summary for count-based items."""
    prefix = f"{quantity}x " if quantity > 1 else ""
    suffix = f" #{article_id}" if article_id is not None else ""
    return f"{prefix}{name}{suffix}"



@dataclass
class ShoppingListConfig:
    """Per-list configuration options, such as weighted randomizer settings and order consumption."""

    random_enabled: bool = False
    random_count: int = 1
    consume_on_order: bool = False
    consume_action: str = "complete"

    def to_dict(self) -> dict[str, Any]:
        return {
            "random_enabled": self.random_enabled,
            "random_count": self.random_count,
            "consume_on_order": self.consume_on_order,
            "consume_action": self.consume_action,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ShoppingListConfig:
        if not isinstance(data, dict):
            return cls()
        return cls(
            random_enabled=bool(data.get("random_enabled", False)),
            random_count=max(1, int(data.get("random_count", 1))),
            consume_on_order=bool(data.get("consume_on_order", False)),
            consume_action=str(data.get("consume_action", "complete") or "complete"),
        )

@dataclass
class ShoppingListItem:
    """Persistent Flaschenpost shopping list item (resolved lazily at cart-add time)."""

    uid: str
    summary: str
    query: str = ""
    category: str | None = None
    unit_type: str = UNIT_TYPE_UNIT
    target_amount_base: float = 1.0
    quantity: int = 1
    article_id: int | None = None
    sort_by: str | None = None
    negative_keywords: list[str] = field(default_factory=list)
    last_resolved_note: str | None = None
    completed: bool = False
    weight: float = DEFAULT_ITEM_WEIGHT

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ShoppingListItem:
        """Deserialize from stored v1 or v2 dict."""
        summary = str(data.get("summary") or "")
        spec = parse_shopping_item_spec(summary)
        unit_type = str(data.get("unit_type") or spec.unit_type)
        if data.get("target_amount_base") is not None:
            target_base = float(data["target_amount_base"])
        elif unit_type == UNIT_TYPE_UNIT and data.get("quantity") is not None:
            target_base = float(data["quantity"])
        else:
            target_base = spec.target_amount_base

        raw_negs = data.get("negative_keywords")
        if isinstance(raw_negs, list):
            negs = [str(k) for k in raw_negs]
        else:
            negs = list(spec.negative_keywords)

        explicit_aid = spec.explicit_article_id
        raw_w = data.get("weight")
        try:
            item_weight = float(raw_w) if raw_w is not None else DEFAULT_ITEM_WEIGHT
            if item_weight <= 0:
                item_weight = DEFAULT_ITEM_WEIGHT
        except (ValueError, TypeError):
            item_weight = DEFAULT_ITEM_WEIGHT

        return cls(
            uid=str(data.get("uid") or uuid.uuid4()),
            summary=summary,
            query=str(data.get("query") or spec.query),
            category=data.get("category") if data.get("category") is not None else spec.category,
            unit_type=unit_type,
            target_amount_base=target_base,
            quantity=int(data.get("quantity") or max(1, round(target_base))),
            article_id=explicit_aid,
            sort_by=data.get("sort_by") or spec.sort_by,
            negative_keywords=negs,
            last_resolved_note=data.get("last_resolved_note"),
            completed=bool(data.get("completed", False)),
            weight=item_weight,
        )


@dataclass
class _MergedRequirement:
    """Aggregated requirement merged across N shopping lists before catalog lookup."""

    query: str
    category: str | None
    unit_type: str
    target_amount_base: float
    explicit_article_id: int | None
    sort_by: str | None
    negative_keywords: list[str] = field(default_factory=list)
    source_lists: list[str] = field(default_factory=list)
    source_items: list[ShoppingListItem] = field(default_factory=list)
    source_items_by_list: list[tuple[str, ShoppingListItem]] = field(default_factory=list)
    list_amounts: dict[str, float] = field(default_factory=dict)


class FlaschenpostShoppingListManager:
    """Manages multiple named Flaschenpost shopping lists, interactive previews, and lazy cart resolution."""

    def __init__(
        self,
        client: FlaschenpostApiClient,
        storage: Any | None = None,
        store: Any | None = None,
    ) -> None:
        """Initialize multi-list shopping list manager with SQLite storage."""
        self._client = client
        self._storage = storage
        self._store = store
        self._lists: dict[str, list[ShoppingListItem]] = {
            DEFAULT_SHOPPING_LIST_NAME: []
        }
        self._list_configs: dict[str, ShoppingListConfig] = {
            DEFAULT_SHOPPING_LIST_NAME: ShoppingListConfig()
        }
        self._list_added_callbacks: list[Callable[[str], None]] = []
        self._list_deleted_callbacks: list[Callable[[str], None]] = []
        self._in_memory_coupons: list[dict[str, Any]] = []
        self._in_memory_recorded_orders: list[dict[str, Any]] = []
        self._in_memory_cart_attributions: dict[int, dict[str, float]] = {}
        self._in_memory_list_spends: dict[str, list[dict[str, Any]]] = {}
        self._in_memory_cart_list_items: list[dict[str, Any]] = []
        self._item_consumed_callbacks: list[Callable[[dict[str, Any]], None]] = []
        self._consumed_order_ids: set[str] = set()

    @property
    def storage(self) -> Any | None:
        """Return the underlying SQLite storage instance."""
        return self._storage

    async def async_get_coupons(self, status: str | None = None) -> list[dict[str, Any]]:
        """Return coupons from SQLite storage or in-memory fallback."""
        if self._storage and hasattr(self._storage, "async_get_coupons"):
            return await self._storage.async_get_coupons(status=status)
        if status:
            return [c for c in self._in_memory_coupons if c["status"] == status]
        return list(self._in_memory_coupons)

    async def async_get_active_coupons(self) -> list[dict[str, Any]]:
        """Return all active coupons from SQLite storage."""
        return await self.async_get_coupons(status="active")

    async def async_add_coupon(self, code: str, description: str = "") -> dict[str, Any]:
        """Add coupon to SQLite storage or in-memory fallback."""
        if self._storage and hasattr(self._storage, "async_add_coupon"):
            return await self._storage.async_add_coupon(code=code, description=description)
        cleaned = code.strip().upper()
        existing = next((c for c in self._in_memory_coupons if c["code"] == cleaned), None)
        if existing:
            existing["status"] = "active"
            existing["description"] = description.strip() or existing["description"]
            return existing
        coupon = {
            "id": len(self._in_memory_coupons) + 1,
            "code": cleaned,
            "description": description.strip(),
            "status": "active",
            "used_at": None,
            "order_id": None,
            "created_at": "2026-11-05T00:00:00",
            "updated_at": "2026-11-05T00:00:00",
        }
        self._in_memory_coupons.append(coupon)
        return coupon

    async def async_delete_coupon(self, code_or_id: str | int) -> bool:
        """Delete coupon from SQLite storage or in-memory fallback."""
        if self._storage and hasattr(self._storage, "async_delete_coupon"):
            return await self._storage.async_delete_coupon(code_or_id)
        before = len(self._in_memory_coupons)
        if isinstance(code_or_id, int) or (isinstance(code_or_id, str) and str(code_or_id).isdigit()):
            self._in_memory_coupons = [c for c in self._in_memory_coupons if c["id"] != int(code_or_id)]
        else:
            needle = str(code_or_id).strip().upper()
            self._in_memory_coupons = [c for c in self._in_memory_coupons if c["code"] != needle]
        return len(self._in_memory_coupons) < before

    async def async_mark_coupon_used(self, code: str, order_id: str | None = None) -> bool:
        """Mark coupon as used in SQLite storage or in-memory fallback."""
        if self._storage and hasattr(self._storage, "async_mark_coupon_used"):
            return await self._storage.async_mark_coupon_used(code=code, order_id=order_id)
        needle = code.strip().upper()
        for c in self._in_memory_coupons:
            if c["code"] == needle:
                c["status"] = "used"
                c["order_id"] = str(order_id) if order_id else None
                return True
        return False

    async def async_apply_next_coupon_to_cart(self) -> dict[str, Any] | None:
        """Apply the oldest active coupon from DB to the current cart, if available."""
        active = await self.async_get_active_coupons()
        if not active:
            return None
        candidate = active[0]
        code = candidate["code"]
        try:
            if hasattr(self._client, "async_add_discount_code"):
                await self._client.async_add_discount_code(code)
            _LOGGER.info("Applied active coupon '%s' to cart", code)
            return candidate
        except Exception as err:
            _LOGGER.warning("Could not apply coupon '%s' to cart: %s", code, err)
            return candidate

    async def async_record_orders(self, orders: list[dict[str, Any]]) -> None:
        """Upsert orders into SQLite storage (or in-memory cache)."""
        if self._storage and hasattr(self._storage, "async_record_orders"):
            await self._storage.async_record_orders(orders)
        else:
            seen = {o.get("id") or o.get("orderNumber") for o in self._in_memory_recorded_orders}
            for o in orders:
                oid = o.get("id") or o.get("orderNumber")
                if oid and oid not in seen:
                    seen.add(oid)
                    self._in_memory_recorded_orders.append(o)

    async def async_get_all_recorded_orders(self) -> list[dict[str, Any]]:
        """Return all historical orders from SQLite (or in-memory cache)."""
        if self._storage and hasattr(self._storage, "async_get_all_recorded_orders"):
            return await self._storage.async_get_all_recorded_orders()
        return list(self._in_memory_recorded_orders)

    async def async_set_cart_attributions(
        self, attributions: dict[int, dict[str, float]], clear_existing: bool = False
    ) -> None:
        """Store or update cart item attributions ({article_id: {list_name: quantity}})."""
        if self._storage and hasattr(self._storage, "async_set_cart_attributions"):
            await self._storage.async_set_cart_attributions(
                attributions, clear_existing=clear_existing
            )
        if clear_existing:
            self._in_memory_cart_attributions.clear()
        for aid, list_map in attributions.items():
            for lname, q in list_map.items():
                if clear_existing:
                    self._in_memory_cart_attributions.setdefault(aid, {})[lname] = q
                else:
                    self._in_memory_cart_attributions.setdefault(aid, {})[lname] = (
                        self._in_memory_cart_attributions.setdefault(aid, {}).get(lname, 0.0) + q
                    )

    async def async_get_cart_attributions(self) -> dict[int, dict[str, float]]:
        """Return all active cart item attributions."""
        if self._storage and hasattr(self._storage, "async_get_cart_attributions"):
            return await self._storage.async_get_cart_attributions()
        return dict(self._in_memory_cart_attributions)

    def register_item_consumed_callback(
        self, callback: Callable[[dict[str, Any]], None]
    ) -> Callable[[], None]:
        """Register a callback invoked when a shopping list item is consumed on order placement."""
        self._item_consumed_callbacks.append(callback)

        def _remove() -> None:
            if callback in self._item_consumed_callbacks:
                self._item_consumed_callbacks.remove(callback)

        return _remove

    async def async_set_cart_list_items(
        self, items: list[dict[str, Any]], clear_existing: bool = False
    ) -> None:
        """Record which shopping list items (by list_name and item_uid) are active in the cart."""
        if self._storage and hasattr(self._storage, "async_set_cart_list_items"):
            await self._storage.async_set_cart_list_items(items, clear_existing=clear_existing)
            return
        if clear_existing:
            self._in_memory_cart_list_items.clear()
        for it in items:
            existing = next(
                (
                    x
                    for x in self._in_memory_cart_list_items
                    if x["list_name"] == it["list_name"] and x["item_uid"] == it["item_uid"]
                ),
                None,
            )
            if existing:
                existing["article_id"] = it.get("article_id")
            else:
                self._in_memory_cart_list_items.append(dict(it))

    async def async_get_cart_list_items(self) -> list[dict[str, Any]]:
        """Return all tracked cart list items."""
        if self._storage and hasattr(self._storage, "async_get_cart_list_items"):
            return await self._storage.async_get_cart_list_items()
        return list(self._in_memory_cart_list_items)

    async def async_clear_cart_list_items(self) -> None:
        """Clear all tracked cart list items."""
        if self._storage and hasattr(self._storage, "async_clear_cart_list_items"):
            await self._storage.async_clear_cart_list_items()
        self._in_memory_cart_list_items.clear()

    async def async_consume_order_items(
        self, order: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Consume items from configured shopping lists when an order is placed."""
        order_id = str(order.get("id") or order.get("orderNumber") or "")
        if order_id and order_id in self._consumed_order_ids:
            return []
        if order_id:
            self._consumed_order_ids.add(order_id)

        order_number = str(order.get("orderNumber") or order_id)
        order_created_at = str(order.get("createdAt") or datetime.now(UTC).isoformat())

        line_items = order.get("lineItems") or []
        ordered_article_ids: set[int] = set()
        for li in line_items:
            sku = (li.get("variant") or {}).get("sku")
            if sku and str(sku).isdigit():
                ordered_article_ids.add(int(sku))

        tracked_cart_items = await self.async_get_cart_list_items()
        consumed_events: list[dict[str, Any]] = []
        consumed_keys: set[tuple[str, str]] = set()
        mutated_lists: set[str] = set()

        # Phase 1: Match from tracked cart items
        for tracked in tracked_cart_items:
            lname = str(tracked["list_name"])
            uid = str(tracked["item_uid"])
            aid = tracked.get("article_id")

            cfg = self.get_list_config(lname)
            if not cfg.consume_on_order:
                continue

            if ordered_article_ids and aid is not None and aid not in ordered_article_ids:
                continue

            target_list = self._lists.get(lname)
            if not target_list:
                continue

            item = next((it for it in target_list if it.uid == uid), None)
            if not item:
                continue

            action = cfg.consume_action or "complete"
            if action == "delete":
                if item in target_list:
                    target_list.remove(item)
                    mutated_lists.add(lname)
            else:
                if not item.completed:
                    item.completed = True
                    mutated_lists.add(lname)

            consumed_keys.add((lname, uid))
            ev_data = {
                "list_name": lname,
                "item_uid": item.uid,
                "summary": item.summary,
                "query": item.query,
                "category": item.category,
                "unit_type": item.unit_type,
                "target_amount_base": item.target_amount_base,
                "quantity": item.quantity,
                "article_id": item.article_id or aid,
                "action": action,
                "order_id": order_id,
                "order_number": order_number,
                "order_created_at": order_created_at,
            }
            consumed_events.append(ev_data)

        # Phase 2: Fallback for lists with consume_on_order matching by article_id
        if ordered_article_ids:
            for lname, items in self._lists.items():
                cfg = self.get_list_config(lname)
                if not cfg.consume_on_order:
                    continue
                to_remove: list[ShoppingListItem] = []
                for item in list(items):
                    if (lname, item.uid) in consumed_keys:
                        continue
                    if item.article_id is not None and item.article_id in ordered_article_ids:
                        action = cfg.consume_action or "complete"
                        if action == "delete":
                            to_remove.append(item)
                            mutated_lists.add(lname)
                        else:
                            if not item.completed:
                                item.completed = True
                                mutated_lists.add(lname)

                        consumed_keys.add((lname, item.uid))
                        ev_data = {
                            "list_name": lname,
                            "item_uid": item.uid,
                            "summary": item.summary,
                            "query": item.query,
                            "category": item.category,
                            "unit_type": item.unit_type,
                            "target_amount_base": item.target_amount_base,
                            "quantity": item.quantity,
                            "article_id": item.article_id,
                            "action": action,
                            "order_id": order_id,
                            "order_number": order_number,
                            "order_created_at": order_created_at,
                        }
                        consumed_events.append(ev_data)

                for item in to_remove:
                    if item in items:
                        items.remove(item)

        await self.async_clear_cart_list_items()

        if mutated_lists:
            await self.async_save()

        for ev in consumed_events:
            for cb in list(self._item_consumed_callbacks):
                try:
                    cb(ev)
                except Exception as err:
                    _LOGGER.warning("Error invoking item_consumed callback: %s", err)

        return consumed_events

    async def async_clear_cart_attributions(self) -> None:
        """Clear all active cart item attributions."""
        if self._storage and hasattr(self._storage, "async_clear_cart_attributions"):
            await self._storage.async_clear_cart_attributions()
        self._in_memory_cart_attributions.clear()

    async def async_record_order_attributions(self, order: dict[str, Any]) -> None:
        """Record per-list item provenance and monetary breakdown when an order is placed."""
        order_id = str(order.get("id") or "")
        if not order_id:
            return
        created_at = str(order.get("createdAt") or datetime.now(UTC).isoformat())

        cart_attrs = await self.async_get_cart_attributions()
        line_items = order.get("lineItems") or []

        list_items: list[dict[str, Any]] = []
        list_goods_cents: dict[str, int] = {}
        list_item_counts: dict[str, int] = {}

        for li in line_items:
            sku = (li.get("variant") or {}).get("sku")
            aid = int(sku) if sku and str(sku).isdigit() else None
            tot_price = li.get("totalPrice")
            li_cents = (
                tot_price.get("centAmount", 0)
                if isinstance(tot_price, dict)
                else int(tot_price or 0)
            )
            li_qty = int(li.get("quantity") or 1)
            name_obj = li.get("name") or {}
            name = (
                name_obj.get("de-DE") or next(iter(name_obj.values()), "")
                if isinstance(name_obj, dict)
                else str(name_obj)
            )

            attr_map = cart_attrs.get(aid) if aid is not None else None
            if not attr_map:
                attr_map = {DEFAULT_SHOPPING_LIST_NAME: float(li_qty)}

            tot_attr_q = sum(attr_map.values())
            running_cents = 0
            items_list = list(attr_map.items())

            for idx, (lname, lqty) in enumerate(items_list):
                if idx == len(items_list) - 1:
                    alloc_cents = li_cents - running_cents
                else:
                    ratio = lqty / tot_attr_q if tot_attr_q > 0 else 1.0 / len(items_list)
                    alloc_cents = round(li_cents * ratio)
                    running_cents += alloc_cents

                alloc_q = round(li_qty * (lqty / tot_attr_q if tot_attr_q > 0 else 1.0 / len(items_list)), 2)
                list_goods_cents[lname] = list_goods_cents.get(lname, 0) + alloc_cents
                list_item_counts[lname] = list_item_counts.get(lname, 0) + 1
                list_items.append(
                    {
                        "list_name": lname,
                        "article_id": aid,
                        "name": name,
                        "quantity": alloc_q,
                        "total_cents": alloc_cents,
                        "created_at": created_at,
                    }
                )

        cf = (order.get("custom") or {}).get("fields") or {}
        td = cf.get("TotalDeposit")
        deposit_cents = (td.get("centAmount", 0) if isinstance(td, dict) else int(td or 0))
        tfee = cf.get("TotalDelivery")
        delivery_fee_cents = (tfee.get("centAmount", 0) if isinstance(tfee, dict) else int(tfee or 0))

        total_goods_all = sum(list_goods_cents.values())
        list_spends: list[dict[str, Any]] = []

        if not list_goods_cents:
            tot_p = order.get("totalPrice")
            tot_c = (tot_p.get("centAmount", 0) if isinstance(tot_p, dict) else int(tot_p or 0))
            list_spends.append(
                {
                    "list_name": DEFAULT_SHOPPING_LIST_NAME,
                    "goods_cents": 0,
                    "deposit_cents": deposit_cents,
                    "delivery_fee_cents": delivery_fee_cents,
                    "total_cents": tot_c,
                    "items_count": 0,
                    "created_at": created_at,
                }
            )
        else:
            dep_running = 0
            deliv_running = 0
            sorted_lists = list(list_goods_cents.keys())
            for idx, lname in enumerate(sorted_lists):
                lgoods = list_goods_cents[lname]
                if idx == len(sorted_lists) - 1:
                    ldep = deposit_cents - dep_running
                    ldeliv = delivery_fee_cents - deliv_running
                else:
                    share = lgoods / total_goods_all if total_goods_all > 0 else 1.0 / len(sorted_lists)
                    ldep = round(deposit_cents * share)
                    ldeliv = round(delivery_fee_cents * share)
                    dep_running += ldep
                    deliv_running += ldeliv

                ltotal = lgoods + ldep + ldeliv
                list_spends.append(
                    {
                        "list_name": lname,
                        "goods_cents": lgoods,
                        "deposit_cents": ldep,
                        "delivery_fee_cents": ldeliv,
                        "total_cents": ltotal,
                        "items_count": list_item_counts.get(lname, 0),
                        "created_at": created_at,
                    }
                )

        if self._storage and hasattr(self._storage, "async_record_order_list_spends"):
            await self._storage.async_record_order_list_spends(
                order_id=order_id,
                spends=list_spends,
                items=list_items,
            )
        else:
            for s in list_spends:
                self._in_memory_list_spends.setdefault(s["list_name"], []).append(
                    {
                        **s,
                        "order_id": order_id,
                        "items": [it for it in list_items if it["list_name"] == s["list_name"]],
                    }
                )

        # Consume shopping list items for lists configured to consume on order
        await self.async_consume_order_items(order)

        await self.async_clear_cart_attributions()

    async def async_get_all_list_spends(self) -> dict[str, list[dict[str, Any]]]:
        """Return all historical order list spends grouped by list_name."""
        if self._storage and hasattr(self._storage, "async_get_all_list_spends"):
            return await self._storage.async_get_all_list_spends()
        return dict(self._in_memory_list_spends)

    @property
    def list_names(self) -> list[str]:
        """Return all managed shopping list names."""
        return list(self._lists.keys())

    @property
    def items(self) -> list[ShoppingListItem]:
        """Return items of the default shopping list."""
        return self.get_list_items(DEFAULT_SHOPPING_LIST_NAME)

    def register_list_added_callback(self, callback: Callable[[str], None]) -> None:
        """Register callback invoked whenever a brand-new named list is created."""
        self._list_added_callbacks.append(callback)

    def register_list_deleted_callback(self, callback: Callable[[str], None]) -> None:
        """Register callback invoked whenever a named list is deleted."""
        self._list_deleted_callbacks.append(callback)

    def _resolve_existing_list_key(self, list_name: str) -> str | None:
        needle = list_name.strip().lower()
        for existing in self._lists:
            if existing.lower() == needle:
                return existing
        return None

    def _ensure_list(self, list_name: str) -> str:
        clean_name = list_name.strip() or DEFAULT_SHOPPING_LIST_NAME
        existing = self._resolve_existing_list_key(clean_name)
        if existing is not None:
            return existing
        self._lists[clean_name] = []
        for cb in self._list_added_callbacks:
            cb(clean_name)
        return clean_name

    def get_list_items(
        self, list_name: str = DEFAULT_SHOPPING_LIST_NAME
    ) -> list[ShoppingListItem]:
        """Return items for a named shopping list (case-insensitive)."""
        key = self._resolve_existing_list_key(list_name)
        if key is None:
            return []
        return list(self._lists[key])

    def get_all_lists_dict(self) -> dict[str, list[dict[str, Any]]]:
        """Return serialized view of all named shopping lists."""
        return {
            name: [item.to_dict() for item in items]
            for name, items in self._lists.items()
        }

    def export_raw_lists(self) -> dict[str, list[str]]:
        """Export `{list_name: [summary, ...]}` for config-flow seeding or UI editors."""
        exported: dict[str, list[str]] = {}
        for name, items in self._lists.items():
            if items or name != DEFAULT_SHOPPING_LIST_NAME:
                exported[name] = [it.summary for it in items]
        return exported

    async def async_import_raw_lists(
        self, raw_lists: Mapping[str, list[str]], *, replace: bool = False
    ) -> None:
        """Import `{list_name: [summary, ...]}` lazily and persist to storage."""
        for lname, lines in raw_lists.items():
            if isinstance(lines, list):
                await self.async_save_named_list(
                    str(lname),
                    [str(x) for x in lines if str(x).strip()],
                    replace=replace,
                )

    async def async_load(self) -> None:
        """Load persisted shopping lists and configs from SQLite storage or legacy Store."""
        if self._storage is not None:
            await self._storage.async_init()
            await self._storage.async_migrate_legacy_data(
                legacy_store=self._store,
                initial_lists=self._lists,
                initial_configs=self._list_configs,
            )
            lists, configs = await self._storage.async_get_all_data()
            if DEFAULT_SHOPPING_LIST_NAME not in lists:
                lists = {DEFAULT_SHOPPING_LIST_NAME: [], **lists}
                configs[DEFAULT_SHOPPING_LIST_NAME] = ShoppingListConfig()
            self._lists = lists
            self._list_configs = configs
            return

        if self._store is None:
            return
        raw = await self._store.async_load()
        if not isinstance(raw, dict):
            return
        if isinstance(raw.get("lists"), dict):
            loaded: dict[str, list[ShoppingListItem]] = {}
            for lname, raw_items in raw["lists"].items():
                if isinstance(raw_items, list):
                    loaded[str(lname)] = [
                        ShoppingListItem.from_dict(x) for x in raw_items
                    ]
            if DEFAULT_SHOPPING_LIST_NAME not in loaded:
                loaded = {DEFAULT_SHOPPING_LIST_NAME: [], **loaded}
            self._lists = loaded
            if isinstance(raw.get("list_configs"), dict):
                self._list_configs = {
                    str(k): ShoppingListConfig.from_dict(v)
                    for k, v in raw["list_configs"].items()
                }
        elif isinstance(raw.get("items"), list):
            self._lists = {
                DEFAULT_SHOPPING_LIST_NAME: [
                    ShoppingListItem.from_dict(x) for x in raw["items"]
                ]
            }

    def get_list_config(self, list_name: str) -> ShoppingListConfig:
        """Return configuration for a named shopping list."""
        key = self._resolve_existing_list_key(list_name) or list_name.strip()
        if key not in self._list_configs:
            self._list_configs[key] = ShoppingListConfig()
        return self._list_configs[key]

    async def async_set_list_config(
        self,
        list_name: str,
        *,
        random_enabled: bool | None = None,
        random_count: int | None = None,
        consume_on_order: bool | None = None,
        consume_action: str | None = None,
    ) -> ShoppingListConfig:
        """Update and persist configuration for a named shopping list."""
        key = self._ensure_list(list_name)
        cfg = self.get_list_config(key)
        if random_enabled is not None:
            cfg.random_enabled = bool(random_enabled)
        if random_count is not None:
            cfg.random_count = max(1, int(random_count))
        if consume_on_order is not None:
            cfg.consume_on_order = bool(consume_on_order)
        if consume_action is not None:
            cfg.consume_action = str(consume_action).lower().strip() or "complete"
        await self.async_save()
        return cfg

    def get_all_list_configs(self) -> dict[str, dict[str, Any]]:
        """Return serialized configuration dictionary for all managed lists."""
        return {
            name: self.get_list_config(name).to_dict()
            for name in self._lists
        }

    async def async_reset_list_weights(self, list_name: str) -> None:
        """Reset weights of all items in a named list to default weight."""
        key = self._resolve_existing_list_key(list_name)
        if key is None:
            return
        for it in self._lists[key]:
            it.weight = DEFAULT_ITEM_WEIGHT
        await self.async_save()

    async def async_save(self) -> None:
        """Persist all named lists and list configurations to SQLite storage (or legacy Store)."""
        if self._storage is not None:
            await self._storage.async_save_all(self._lists, self._list_configs)
            return
        if self._store is None:
            return
        await self._store.async_save({
            "lists": self.get_all_lists_dict(),
            "list_configs": self.get_all_list_configs(),
        })

    def _build_lazy_item(
        self,
        summary: str,
        description: str | None = None,
        sort_by: str | None = None,
    ) -> ShoppingListItem:
        """Parse item summary into a `ShoppingListItem` WITHOUT querying the catalog."""
        spec = parse_shopping_item_spec(summary)
        desc_sort: str | None = None
        if description:
            stripped_desc = description.strip()
            if spec.explicit_article_id is None and stripped_desc.isdigit():
                spec.explicit_article_id = int(stripped_desc)
            else:
                desc_sort = resolve_sort_alias(stripped_desc)

        effective_sort = (
            resolve_sort_alias(sort_by) or sort_by or spec.sort_by or desc_sort
        )
        spec.sort_by = effective_sort
        canonical = format_shopping_spec_summary(spec)
        qty_display = (
            max(1, round(spec.target_amount_base))
            if spec.unit_type in (UNIT_TYPE_UNIT, UNIT_TYPE_CRATE)
            else 1
        )

        return ShoppingListItem(
            uid=str(uuid.uuid4()),
            summary=canonical,
            query=spec.query,
            category=spec.category,
            unit_type=spec.unit_type,
            target_amount_base=spec.target_amount_base,
            quantity=qty_display,
            article_id=spec.explicit_article_id,
            sort_by=effective_sort,
            negative_keywords=spec.negative_keywords,
            completed=False,
            weight=DEFAULT_ITEM_WEIGHT,
        )

    async def async_save_named_list(
        self,
        list_name: str,
        items: list[str],
        *,
        replace: bool = True,
        sort_by: str | None = None,
    ) -> list[ShoppingListItem]:
        """Create, replace, or append a named shopping list lazily (no API lookup on save)."""
        key = self._ensure_list(list_name)
        built = [
            self._build_lazy_item(line, sort_by=sort_by)
            for line in items
            if line and line.strip()
        ]
        if replace:
            self._lists[key] = built
        else:
            self._lists[key].extend(built)
        await self.async_save()
        return list(self._lists[key])

    async def async_delete_named_list(self, list_name: str) -> bool:
        """Delete a named shopping list (resets default list if default is specified)."""
        key = self._resolve_existing_list_key(list_name)
        if key is None:
            return False
        if key == DEFAULT_SHOPPING_LIST_NAME:
            self._lists[key] = []
        else:
            self._lists.pop(key, None)
            self._list_configs.pop(key, None)
            if self._storage is not None:
                await self._storage.async_delete_list(key)
            for cb in self._list_deleted_callbacks:
                cb(key)
        await self.async_save()
        return True

    async def async_add_item(
        self,
        summary: str,
        description: str | None = None,
        sort_by: str | None = None,
        list_name: str = DEFAULT_SHOPPING_LIST_NAME,
    ) -> ShoppingListItem:
        """Append a shopping item lazily without resolving against the warehouse yet."""
        key = self._ensure_list(list_name)
        item = self._build_lazy_item(summary, description=description, sort_by=sort_by)
        self._lists[key].append(item)
        await self.async_save()
        return item

    async def async_update_item(
        self,
        uid: str,
        *,
        summary: str | None = None,
        completed: bool | None = None,
        list_name: str = DEFAULT_SHOPPING_LIST_NAME,
    ) -> ShoppingListItem | None:
        """Update an item's summary/amount or completion status lazily."""
        key = self._resolve_existing_list_key(list_name)
        if key is None:
            return None
        target = next((x for x in self._lists[key] if x.uid == uid), None)
        if target is None:
            return None

        if summary is not None and summary != target.summary:
            rebuilt = self._build_lazy_item(summary, sort_by=target.sort_by)
            target.summary = rebuilt.summary
            target.query = rebuilt.query
            target.category = rebuilt.category
            target.unit_type = rebuilt.unit_type
            target.target_amount_base = rebuilt.target_amount_base
            target.quantity = rebuilt.quantity
            target.article_id = rebuilt.article_id
            target.sort_by = rebuilt.sort_by
            target.negative_keywords = rebuilt.negative_keywords
            target.last_resolved_note = None

        if completed is not None:
            target.completed = completed

        await self.async_save()
        return target

    async def async_remove_items(
        self, uids: list[str], list_name: str = DEFAULT_SHOPPING_LIST_NAME
    ) -> None:
        """Remove items by UID from a named shopping list."""
        key = self._resolve_existing_list_key(list_name)
        if key is None:
            return
        uid_set = set(uids)
        self._lists[key] = [x for x in self._lists[key] if x.uid not in uid_set]
        await self.async_save()

    async def async_get_category_options(self) -> list[dict[str, str]]:
        """Build `{value, label}` category & subcategory options from the live warehouse catalog for limitless category depth."""
        if not hasattr(self._client, "async_get_categories"):
            return []
        tree = await self._client.async_get_categories()

        options: list[dict[str, str]] = []
        seen_values: set[str] = set()

        def _traverse(node: dict[str, Any], path: list[str]) -> None:
            name = str(node.get("name") or "").strip()
            if not name:
                return
            cur_path = [*path, name]
            val = "::".join(cur_path)
            subs = node.get("subcategories") or []

            if val not in seen_values:
                seen_values.add(val)
                if subs:
                    label = f"{' -> '.join(cur_path)} (All)" if len(cur_path) > 1 else f"{name} (All)"
                else:
                    label = " -> ".join(cur_path) if len(cur_path) > 1 else name
                options.append({"value": val, "label": label})

            for child in subs:
                _traverse(child, cur_path)

        for top in tree:
            _traverse(top, [])

        return options


    @staticmethod
    def _pick_weighted_random_items(
        items: list[ShoppingListItem],
        count: int,
    ) -> tuple[list[ShoppingListItem], list[ShoppingListItem]]:
        """Select count items from pool without replacement using weighted random selection.

        Returns (picked, unpicked).
        """
        if len(items) <= count:
            return list(items), []

        pool = list(items)
        picked: list[ShoppingListItem] = []
        target_count = min(count, len(items))
        for _ in range(target_count):
            weights = [
                max(0.001, float(getattr(it, "weight", DEFAULT_ITEM_WEIGHT) or DEFAULT_ITEM_WEIGHT))
                for it in pool
            ]
            chosen = random.choices(pool, weights=weights, k=1)[0]
            picked.append(chosen)
            pool.remove(chosen)
        return picked, pool

    def _apply_random_pick(
        self,
        candidates: list[ShoppingListItem],
        count: int,
        *,
        dry_run: bool = False,
    ) -> list[ShoppingListItem]:
        """Pick items with weighted randomizer and update weights if not dry_run."""
        picked, unpicked = self._pick_weighted_random_items(candidates, count)
        if not dry_run:
            for it in picked:
                it.weight = DEFAULT_ITEM_WEIGHT
            for it in unpicked:
                curr = float(getattr(it, "weight", DEFAULT_ITEM_WEIGHT) or DEFAULT_ITEM_WEIGHT)
                it.weight = round(curr + 1.0, 2)
        return picked

    async def async_import_cart_to_new_list(
        self,
        list_name: str,
    ) -> list[ShoppingListItem]:
        """Import all current cart line items by SKU into a new or replaced shopping list."""
        clean_name = list_name.strip()
        if not clean_name:
            raise ValueError("Name der Einkaufsliste darf nicht leer sein.")

        cart = await self._client.async_get_cart()
        line_items = cart.get("lineItems") or []
        if not line_items:
            raise ValueError("Der Flaschenpost-Warenkorb ist leer.")

        raw_entries: list[tuple[int, int, str]] = []
        missing_name_aids: list[int] = []

        for li in line_items:
            qty = max(1, int(li.get("quantity") or 1))
            var = li.get("variant") or {}
            sku = (
                var.get("sku")
                or var.get("key")
                or li.get("articleId")
                or li.get("article_id")
            )
            aid: int | None = None
            if sku is not None:
                try:
                    aid = int(sku)
                except (ValueError, TypeError):
                    aid = None

            name_val = li.get("name")
            if not name_val and isinstance(li.get("product"), dict):
                name_val = li["product"].get("name")

            name_str = ""
            if isinstance(name_val, dict):
                name_str = str(
                    name_val.get("de-DE")
                    or name_val.get("de")
                    or next(iter(name_val.values()), "")
                ).strip()
            elif isinstance(name_val, str):
                name_str = name_val.strip()

            if aid is not None:
                if not name_str:
                    missing_name_aids.append(aid)
                raw_entries.append((aid, qty, name_str))

        if not raw_entries:
            raise ValueError("Keine Artikel mit gültiger SKU im Warenkorb gefunden.")

        if missing_name_aids and hasattr(
            self._client, "async_get_products_by_article_ids"
        ):
            try:
                hydrated = await self._client.async_get_products_by_article_ids(
                    missing_name_aids
                )
                aid_to_name = {
                    p["article_id"]: p.get("name")
                    for p in hydrated
                    if p.get("article_id")
                }
                raw_entries = [
                    (aid, qty, name or aid_to_name.get(aid) or f"Artikel #{aid}")
                    for aid, qty, name in raw_entries
                ]
            except Exception:
                pass

        merged_entries: dict[int, tuple[int, str]] = {}
        for aid, qty, name in raw_entries:
            if aid in merged_entries:
                prev_qty, prev_name = merged_entries[aid]
                merged_entries[aid] = (prev_qty + qty, prev_name or name)
            else:
                merged_entries[aid] = (qty, name)

        formatted_lines = [
            f"{qty}x {name or f'Artikel #{aid}'} #{aid}"
            if qty > 1
            else f"{name or f'Artikel #{aid}'} #{aid}"
            for aid, (qty, name) in merged_entries.items()
        ]

        key = self._ensure_list(clean_name)
        built = [self._build_lazy_item(line) for line in formatted_lines]
        self._lists[key] = built
        await self.async_save()
        return list(self._lists[key])

    async def async_preview_search(
        self,
        raw_query: str,
        *,
        amount_prefix: str | None = None,
        category: str | None = None,
        sort_by: str | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        """Run an interactive live preview search for a shopping item spec without modifying any cart."""
        spec = build_preview_spec(
            raw_query,
            amount_prefix=amount_prefix,
            category_override=category,
            sort_by=sort_by,
        )
        lazy_summary = format_shopping_spec_summary(spec)

        winner = await self._client.async_resolve_item_for_cart(
            spec.query,
            category=spec.category,
            unit_type=spec.unit_type,
            target_amount_base=spec.target_amount_base,
            article_id=spec.explicit_article_id,
            sort_by=spec.sort_by,
            negative_keywords=spec.negative_keywords,
        )

        effective_sort = spec.sort_by or SORT_PRICE
        search_sort = (
            effective_sort
            if effective_sort in (SORT_PRICE_PER_KG, SORT_PRICE_PER_LITER, SORT_PRICE_PER_UNIT, SORT_RELEVANCE)
            else SORT_PRICE
        )

        candidates_raw: list[dict[str, Any]] = []
        if hasattr(self._client, "async_search_products"):
            candidates_raw = await self._client.async_search_products(
                spec.query,
                limit=max(limit * 3, 24),
                category=spec.category,
                sort_by=search_sort,
                only_available=False,
                only_crates=(spec.unit_type == UNIT_TYPE_CRATE),
                negative_keywords=spec.negative_keywords,
            )

        if spec.category and spec.category.strip():
            from .api import matches_category_filter

            candidates_raw = [
                c for c in candidates_raw if matches_category_filter(c, spec.category)
            ]

        enriched_candidates: list[dict[str, Any]] = []
        for cand in candidates_raw:
            plan = estimate_candidate_pack_plan(spec, cand)
            enriched_candidates.append(
                {
                    **cand,
                    **plan,
                }
            )

        if effective_sort == SORT_PRICE:
            enriched_candidates.sort(
                key=lambda c: (
                    not bool(c.get("is_available", True)),
                    float(c.get("line_total_eur") or 1e9),
                    float(
                        c.get("price_per_kg_eur")
                        or c.get("price_per_liter_eur")
                        or c.get("price_per_unit_eur")
                        or 1e9
                    ),
                    int(c.get("article_id") or 0),
                )
            )
        elif effective_sort == SORT_PRICE_PER_KG:
            enriched_candidates.sort(
                key=lambda c: (
                    not bool(c.get("is_available", True)),
                    c.get("price_per_kg_eur") is None,
                    float(c.get("price_per_kg_eur") or 1e9),
                    float(c.get("line_total_eur") or 1e9),
                )
            )
        elif effective_sort == SORT_PRICE_PER_LITER:
            enriched_candidates.sort(
                key=lambda c: (
                    not bool(c.get("is_available", True)),
                    c.get("price_per_liter_eur") is None,
                    float(c.get("price_per_liter_eur") or 1e9),
                    float(c.get("line_total_eur") or 1e9),
                )
            )
        elif effective_sort == SORT_PRICE_PER_UNIT:
            enriched_candidates.sort(
                key=lambda c: (
                    not bool(c.get("is_available", True)),
                    c.get("price_per_unit_eur") is None,
                    float(c.get("price_per_unit_eur") or 1e9),
                    float(c.get("line_total_eur") or 1e9),
                )
            )
        elif effective_sort == SORT_RELEVANCE:
            enriched_candidates.sort(
                key=lambda c: (not bool(c.get("is_available", True)),)
            )

        enriched_candidates = enriched_candidates[:limit]

        md_lines: list[str] = [f"### Live Preview for `{lazy_summary}`"]
        if winner and winner.get("article_id") is not None:
            wprod = winner.get("product") or {}
            w_deliv = format_base_amount(
                spec.unit_type, float(winner["delivered_amount_base"])
            )
            wcats = " > ".join(wprod.get("categories") or []) or "—"
            wplan = estimate_candidate_pack_plan(spec, wprod)
            w_url = winner.get("product_url") or wprod.get("product_url")
            w_aid_txt = f"[`#{winner['article_id']}`]({w_url})" if w_url else f"`#{winner['article_id']}`"
            md_lines.append(
                f"**Lazy Order-Time Winner:** `{winner['quantity']}x` **{wprod.get('name')}** "
                f"(`{wprod.get('description') or w_deliv}`, {w_aid_txt}) "
                f"-> **{w_deliv}** for **{winner['line_total_eur']:.2f} EUR** "
                f"({wplan['base_metric']}, Category: *{wcats}*)"
            )
        else:
            md_lines.append(
                "**Lazy Order-Time Winner:** *No in-stock product currently matches this query/category.*"
            )

        if enriched_candidates:
            md_lines.append("")
            md_lines.append("| #SKU | Product | Category | Pack | Unit Price | Needed -> Total | Stock |")
            md_lines.append("|---|---|---|---|---|---|---|")
            winner_aid = winner.get("article_id") if winner else None
            for cand in enriched_candidates:
                aid = cand.get("article_id")
                cats_txt = " > ".join(cand.get("categories") or []) or "—"
                desc_txt = cand.get("description") or "1x"
                stock_txt = (
                    "Winner"
                    if aid == winner_aid and cand.get("is_available", True)
                    else ("In stock" if cand.get("is_available", True) else "Out of stock")
                )
                cand_url = cand.get("product_url")
                sku_md = f"[`#{aid}`]({cand_url})" if cand_url else f"`#{aid}`"
                md_lines.append(
                    f"| {sku_md} | {cand.get('name')} | {cats_txt} | {desc_txt} "
                    f"({float(cand.get('price_eur') or 0):.2f} EUR) | {cand['base_metric']} | "
                    f"`{cand['packs']}x` ({cand['delivered_amount']}) = **{cand['line_total_eur']:.2f} EUR** | {stock_txt} |"
                )

        return {
            "spec": spec,
            "lazy_summary": lazy_summary,
            "winner": winner,
            "candidates": enriched_candidates,
            "markdown": "\n".join(md_lines),
        }

    async def async_preview_lists(
        self,
        list_names: list[str] | None = None,
        *,
        default_sort_by: str | None = None,
    ) -> dict[str, Any]:
        """Dry-run merge and live warehouse resolution for 1..N shopping lists without modifying the cart."""
        res = await self.async_add_lists_to_cart(
            list_names=list_names,
            mark_completed=False,
            clear_cart_first=False,
            default_sort_by=default_sort_by,
            dry_run=True,
        )
        est_total = round(
            sum(float(r.get("line_total_eur") or 0.0) for r in res["resolved_items"]),
            2,
        )
        res["estimated_goods_eur"] = est_total

        merged_title = ", ".join(res["lists_merged"]) or "None"
        md_lines: list[str] = [
            f"### Live Resolution Preview ({merged_title})",
        ]
        if res["resolved_items"]:
            md_lines.append(
                "| Requested | Selected Product | Category | Packs (Delivered) | Base Price | Line Total |"
            )
            md_lines.append("|---|---|---|---|---|---|")
            for item in res["resolved_items"]:
                cats_short = " > ".join(item.get("categories") or []) or "—"
                if item.get("price_per_kg_eur") is not None:
                    base_str = f"{float(item['price_per_kg_eur']):.2f} EUR/kg"
                elif item.get("price_per_liter_eur") is not None:
                    base_str = f"{float(item['price_per_liter_eur']):.2f} EUR/L"
                else:
                    base_str = f"{float(item.get('unit_price_eur') or 0.0):.2f} EUR/pack"
                md_lines.append(
                    f"| `{item['requested']}` | **{item['name']}** (`#{item['article_id']}`, "
                    f"{item.get('pack_description') or '1x'}) | {cats_short} | "
                    f"`{item['quantity_added']}x` ({item['delivered_amount']}) | "
                    f"{base_str} | **{float(item['line_total_eur']):.2f} EUR** |"
                )
            md_lines.append("")
            md_lines.append(
                f"**Estimated Goods Total (excl. crate deposit):** **{est_total:.2f} EUR**"
            )
        else:
            md_lines.append("*No open items resolved.*")

        if res["unresolved"]:
            unresolved_joined = ", ".join(f"`{u}`" for u in res["unresolved"])
            md_lines.append("")
            md_lines.append(
                f"**Unresolved / Out of Stock ({len(res['unresolved'])}):** {unresolved_joined}"
            )

        res["markdown"] = "\n".join(md_lines)
        return res

    async def async_add_lists_to_cart(
        self,
        list_names: list[str] | None = None,
        *,
        external_list_items: dict[str, list[str]] | None = None,
        mark_completed: bool = False,
        clear_cart_first: bool = False,
        default_sort_by: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Merge N named shopping lists, lazily resolve cheapest products now, and add to cart (unless `dry_run=True`)."""
        target_names = list_names or [DEFAULT_SHOPPING_LIST_NAME]
        merged_map: dict[
            tuple[str, str, str, int | None, str | None], _MergedRequirement
        ] = {}
        matched_lists: list[str] = []
        missing_lists: list[str] = []

        norm_default_sort = resolve_sort_alias(default_sort_by) or default_sort_by

        random_mutated: bool = False
        for requested_name in target_names:
            internal_key = self._resolve_existing_list_key(requested_name)
            if internal_key is not None:
                matched_lists.append(internal_key)
                cfg = self.get_list_config(internal_key)
                uncompleted = [item for item in self._lists[internal_key] if not item.completed]

                if cfg.random_enabled and uncompleted:
                    items_to_process = self._apply_random_pick(
                        uncompleted, cfg.random_count, dry_run=dry_run
                    )
                    if not dry_run:
                        random_mutated = True
                else:
                    items_to_process = uncompleted

                for item in items_to_process:
                    eff_sort = item.sort_by or norm_default_sort
                    mkey = (
                        item.query.strip().lower(),
                        (item.category or "").strip().lower(),
                        item.unit_type,
                        item.article_id,
                        eff_sort,
                        tuple(sorted([k.lower() for k in item.negative_keywords])),
                    )
                    if mkey not in merged_map:
                        merged_map[mkey] = _MergedRequirement(
                            query=item.query,
                            category=item.category,
                            unit_type=item.unit_type,
                            target_amount_base=item.target_amount_base,
                            explicit_article_id=item.article_id,
                            sort_by=eff_sort,
                            negative_keywords=list(item.negative_keywords),
                            source_lists=[internal_key],
                            source_items=[item],
                            source_items_by_list=[(internal_key, item)],
                            list_amounts={internal_key: item.target_amount_base},
                        )
                    else:
                        req = merged_map[mkey]
                        req.target_amount_base = round(
                            req.target_amount_base + item.target_amount_base, 2
                        )
                        if internal_key not in req.source_lists:
                            req.source_lists.append(internal_key)
                        req.source_items.append(item)
                        req.source_items_by_list.append((internal_key, item))
                        req.list_amounts[internal_key] = round(
                            req.list_amounts.get(internal_key, 0.0) + item.target_amount_base, 2
                        )
            elif external_list_items and requested_name in external_list_items:
                matched_lists.append(requested_name)
                for raw_line in external_list_items[requested_name]:
                    temp_item = self._build_lazy_item(
                        raw_line, sort_by=norm_default_sort
                    )
                    eff_sort = temp_item.sort_by or norm_default_sort
                    mkey = (
                        temp_item.query.strip().lower(),
                        (temp_item.category or "").strip().lower(),
                        temp_item.unit_type,
                        temp_item.article_id,
                        eff_sort,
                        tuple(sorted([k.lower() for k in temp_item.negative_keywords])),
                    )
                    if mkey not in merged_map:
                        merged_map[mkey] = _MergedRequirement(
                            query=temp_item.query,
                            category=temp_item.category,
                            unit_type=temp_item.unit_type,
                            target_amount_base=temp_item.target_amount_base,
                            explicit_article_id=temp_item.article_id,
                            sort_by=eff_sort,
                            negative_keywords=list(temp_item.negative_keywords),
                            source_lists=[requested_name],
                            source_items=[],
                            list_amounts={requested_name: temp_item.target_amount_base},
                        )
                    else:
                        req = merged_map[mkey]
                        req.target_amount_base = round(
                            req.target_amount_base + temp_item.target_amount_base, 2
                        )
                        if requested_name not in req.source_lists:
                            req.source_lists.append(requested_name)
                        req.list_amounts[requested_name] = round(
                            req.list_amounts.get(requested_name, 0.0) + temp_item.target_amount_base, 2
                        )
            else:
                missing_lists.append(requested_name)

        if not merged_map:
            cart_id: str | None = None
            if clear_cart_first and not dry_run:
                cart = await self._client.async_create_new_cart()
                cart_id = cart.get("id")
                await self.async_clear_cart_attributions()
                await self.async_clear_cart_list_items()
            return {
                "lists_merged": matched_lists,
                "missing_lists": missing_lists,
                "synced_count": 0,
                "resolved_items": [],
                "unresolved": [],
                "cart_id": cart_id,
            }

        cart: dict[str, Any] = {}
        existing_qty: dict[int, int] = {}
        if not dry_run:
            if clear_cart_first:
                cart = await self._client.async_create_new_cart()
            else:
                cart = await self._client.async_get_cart()

            for li in cart.get("lineItems") or []:
                sku = (li.get("variant") or {}).get("sku")
                if sku and str(sku).isdigit():
                    existing_qty[int(sku)] = int(li.get("quantity") or 0)

        desired_deltas: dict[int, int] = {}
        resolved_reports: list[dict[str, Any]] = []
        unresolved: list[str] = []
        completed_candidates: list[ShoppingListItem] = []
        current_sync_attrs: dict[int, dict[str, float]] = {}
        cart_items_tracked: list[dict[str, Any]] = []

        for req in merged_map.values():
            core_label = (
                f"{req.category}::{req.query}" if req.category else req.query
            )
            neg_suffix = "".join(f" !{kw}" for kw in req.negative_keywords)
            req_label = (
                f"{format_base_amount(req.unit_type, req.target_amount_base)} "
                f"{core_label}{neg_suffix}"
            )
            resolution = await self._client.async_resolve_item_for_cart(
                req.query,
                category=req.category,
                unit_type=req.unit_type,
                target_amount_base=req.target_amount_base,
                article_id=req.explicit_article_id,
                sort_by=req.sort_by,
                negative_keywords=req.negative_keywords,
            )
            if not resolution or resolution.get("article_id") is None:
                unresolved.append(req_label)
                continue

            aid = int(resolution["article_id"])
            qty = int(resolution["quantity"])
            prod = resolution.get("product") or {}
            desired_deltas[aid] = desired_deltas.get(aid, 0) + qty

            tot_target = sum(req.list_amounts.values())
            for lname, lamt in req.list_amounts.items():
                ratio = lamt / tot_target if tot_target > 0 else 1.0 / len(req.list_amounts)
                alloc_q = round(qty * ratio, 4)
                current_sync_attrs.setdefault(aid, {})[lname] = (
                    current_sync_attrs.setdefault(aid, {}).get(lname, 0.0) + alloc_q
                )

            for lname, src_item in req.source_items_by_list:
                cart_items_tracked.append(
                    {
                        "list_name": lname,
                        "item_uid": src_item.uid,
                        "article_id": aid,
                    }
                )

            delivered_str = format_base_amount(
                req.unit_type, float(resolution["delivered_amount_base"])
            )
            requested_str = format_base_amount(req.unit_type, req.target_amount_base)
            note = (
                f"{qty}x {prod.get('name')} ({prod.get('description') or delivered_str}) "
                f"— {resolution['line_total_eur']:.2f} EUR"
            )
            for src_item in req.source_items:
                src_item.last_resolved_note = note
                completed_candidates.append(src_item)

            resolved_reports.append(
                {
                    "requested": req_label,
                    "category_filter": req.category,
                    "source_lists": req.source_lists,
                    "article_id": aid,
                    "slug": prod.get("slug"),
                    "product_url": prod.get("product_url"),
                    "name": prod.get("name"),
                    "categories": prod.get("categories"),
                    "pack_description": prod.get("description"),
                    "quantity_added": qty,
                    "requested_amount": requested_str,
                    "delivered_amount": delivered_str,
                    "unit_price_eur": resolution.get("unit_price_eur", prod.get("price_eur")),
                    "tier_applied": resolution.get("tier_applied", False),
                    "price_per_kg_eur": prod.get("price_per_kg_eur"),
                    "price_per_liter_eur": prod.get("price_per_liter_eur"),
                    "line_total_eur": resolution["line_total_eur"],
                }
            )

        if desired_deltas and not dry_run:
            positions = [
                {
                    "articleId": aid,
                    "origin": ORIGIN_FAVORITES if aid in existing_qty else ORIGIN_SEARCH,
                    "quantityTotal": existing_qty.get(aid, 0) + delta,
                }
                for aid, delta in desired_deltas.items()
            ]
            cart = await self._client.async_set_cart_positions(positions)
            if current_sync_attrs:
                await self.async_set_cart_attributions(
                    current_sync_attrs, clear_existing=clear_cart_first
                )
            if cart_items_tracked:
                await self.async_set_cart_list_items(
                    cart_items_tracked, clear_existing=clear_cart_first
                )
            if mark_completed:
                for item in completed_candidates:
                    item.completed = True
            await self.async_save()
        elif not dry_run and (completed_candidates or random_mutated):
            if mark_completed and completed_candidates:
                for item in completed_candidates:
                    item.completed = True
            await self.async_save()

        return {
            "lists_merged": matched_lists,
            "missing_lists": missing_lists,
            "synced_count": len(resolved_reports),
            "resolved_items": resolved_reports,
            "unresolved": unresolved,
            "cart_id": cart.get("id") if not dry_run else None,
        }

    async def async_sync_to_cart(
        self,
        *,
        mark_completed: bool = True,
        clear_cart_first: bool = False,
        list_names: list[str] | None = None,
    ) -> dict[str, Any]:
        """Backward-compatible wrapper pushing default or specified lists into cart."""
        return await self.async_add_lists_to_cart(
            list_names=list_names or [DEFAULT_SHOPPING_LIST_NAME],
            mark_completed=mark_completed,
            clear_cart_first=clear_cart_first,
        )
