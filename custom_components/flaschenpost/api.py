"""Async API client for Flaschenpost B2C Webshop & Delivery services."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import aiohttp

from .const import (
    ADYEN_CLIENT_KEY,
    ALL_PAYMENT_METHODS,
    APP_USER_AGENT,
    BASE_URL,
    FRONTEND_VERSION,
    ORIGIN_UNKNOWN,
    PAYMENT_CREDIT_CARD,
    PAYMENT_INVOICE,
    PAYMENT_PAYPAL,
    PAYMENT_SEPA,
    RECOMMENDATIONS_APIM_KEY,
    SLOTS_APIM_KEY,
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

_PPU_REGEX = re.compile(
    r"(?P<val>\d+(?:[.,]\d+)?)\s*€\s*/\s*(?P<scale>\d+\s*)?(?P<unit>kg|g|l|ml|stk|stck|stück)",
    re.IGNORECASE,
)
_MULTIPACK_REGEX = re.compile(r"^\s*(?P<count>\d+)\s*[x×]\s*", re.IGNORECASE)
_DESC_AMOUNT_REGEX = re.compile(
    r"^\s*(?:(?P<count>\d+)\s*[x×]\s*)?(?P<val>\d+(?:[.,]\d+)?)\s*(?P<unit>kg|g|l|ml)\b",
    re.IGNORECASE,
)
_QUERY_FILLER_WORDS = {"fett", "fettgehalt", "i.tr.", "ca."}
_QUALIFIER_TOKEN_REGEX = re.compile(r"\d+(?:[.,]\d+)?\s*%")
_DOUBLE_CONSONANT_REGEX = re.compile(r"([bcdfghjklmnpqrstvwxz])\1+")
_CATEGORY_SPLIT_REGEX = re.compile(r"\s*(?:->|-->|::|>|/)\s*")

_SYNONYM_GROUPS: list[tuple[str, ...]] = [
    ("zero", "ohne zucker", "zuckerfrei", "sugarfree"),
    ("karotte", "karotten", "möhre", "möhren", "mohrrübe", "mohrrüben"),
    ("kartoffel", "kartoffeln", "erdapfel", "erdäpfel"),
    ("brötchen", "semmel", "semmeln", "schrippe", "schrippen"),
]


class FlaschenpostError(Exception):
    """Base exception for Flaschenpost API errors."""


class FlaschenpostAuthError(FlaschenpostError):
    """Authentication or authorization error."""


class FlaschenpostOrderError(FlaschenpostError):
    """Checkout or ordering validation error."""


def cents_to_eur(money: dict[str, Any] | int | float | None) -> float:
    """Convert Commercetools centPrecision money object or cent integer to EUR float."""
    if money is None:
        return 0.0
    if isinstance(money, (int, float)):
        return round(float(money) / 100.0, 2)
    if not isinstance(money, dict):
        return 0.0
    cents = money.get("centAmount", 0)
    digits = money.get("fractionDigits", 2)
    return round(float(cents) / (10**digits), digits)


def compute_basket_class(total_goods_cents: int) -> str:
    """Compute Flaschenpost slot basket class (`FP` in webshop bundle)."""
    if total_goods_cents > 8000:
        return "ABOVETHRESHOLD"
    if total_goods_cents > 5000:
        return "ABOVEMOV"
    return "BELOWMOV"


def parse_price_per_unit_text(
    text: str | None,
) -> tuple[float | None, str | None]:
    """Parse Flaschenpost `PricePerUnitText` such as `(1,48 €/kg)` or `12 x 1L (0,71 €/l)`."""
    if not text:
        return None, None
    match = _PPU_REGEX.search(text)
    if not match:
        return None, None

    val = float(match.group("val").replace(",", "."))
    scale_raw = (match.group("scale") or "").strip()
    scale = float(scale_raw) if scale_raw else 1.0
    unit = match.group("unit").lower()

    if unit == "g":
        return round(val * (1000.0 / scale), 4), "kg"
    if unit == "kg":
        return round(val / scale, 4), "kg"
    if unit == "ml":
        return round(val * (1000.0 / scale), 4), "l"
    if unit == "l":
        return round(val / scale, 4), "l"
    return round(val / scale, 4), "unit"


def parse_package_amount_from_description(
    desc: str | None,
) -> tuple[float | None, str | None]:
    """Parse total package weight (g) or volume (ml) from `ArticleDescription` like `'4 x 150g'`."""
    if not desc:
        return None, None
    match = _DESC_AMOUNT_REGEX.search(desc)
    if not match:
        return None, None
    count = int(match.group("count")) if match.group("count") else 1
    val = float(match.group("val").replace(",", "."))
    unit = match.group("unit").lower()
    total = count * val
    if unit == "kg":
        return round(total * 1000.0, 2), "g"
    if unit == "g":
        return round(total, 2), "g"
    if unit == "l":
        return round(total * 1000.0, 2), "ml"
    if unit == "ml":
        return round(total, 2), "ml"
    return None, None


def _collapse_double_consonants(text: str) -> str:
    return _DOUBLE_CONSONANT_REGEX.sub(r"\1", text.lower())


def _normalize_category_token(text: str) -> str:
    """Normalize category name/slug for resilient matching (`'Reis & Getreide'` == `'reis-getreide'`)."""
    lowered = text.strip().lower()
    lowered = (
        lowered.replace("ä", "ae")
        .replace("ö", "oe")
        .replace("ü", "ue")
        .replace("ß", "ss")
    )
    lowered = re.sub(r"\s*(?:&|\+|und)\s*", " ", lowered)
    lowered = re.sub(r"[^a-z0-9]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def _category_segment_matches(seg: str, cand: str) -> bool:
    """Return True if category token `cand` matches filter segment `seg`."""
    if not seg or not cand:
        return False
    if seg == cand:
        return True
    cand_words = cand.split()
    seg_words = seg.split()
    if seg_words == cand_words:
        return True
    cand_parts = [
        _normalize_category_token(p)
        for p in re.split(r",|\s+(?:und|&|\+)\s+", cand)
        if _normalize_category_token(p)
    ]
    return seg in cand_parts


def extract_product_categories(
    product: dict[str, Any],
) -> tuple[list[str], list[str], str | None]:
    """Extract `(category_names, category_slugs, brand)` from Commercetools `categories`."""
    cat_names: list[str] = []
    cat_slugs: list[str] = []
    brand: str | None = None

    def _visit_cat_obj(obj: dict[str, Any] | None) -> None:
        nonlocal brand
        if not isinstance(obj, dict):
            return
        for anc in obj.get("ancestors") or []:
            if isinstance(anc, dict) and isinstance(anc.get("obj"), dict):
                _visit_cat_obj(anc["obj"])

        ctype = (((obj.get("custom") or {}).get("type") or {}).get("key") or "").lower()
        name_map = obj.get("name") or {}
        cname = (
            name_map.get("de-DE") or next(iter(name_map.values()), "")
            if isinstance(name_map, dict)
            else str(name_map)
        ).strip()
        slug_map = obj.get("slug") or {}
        cslug = (
            slug_map.get("de-DE") or next(iter(slug_map.values()), "")
            if isinstance(slug_map, dict)
            else str(slug_map)
        ).strip()

        if ctype == "fp-category-brand":
            if cname and not brand:
                brand = cname
            return

        if cname and cname not in cat_names:
            cat_names.append(cname)
        if cslug and cslug not in cat_slugs:
            cat_slugs.append(cslug)

    for entry in product.get("categories") or []:
        if isinstance(entry, dict):
            _visit_cat_obj(entry.get("obj") or entry)

    return cat_names, cat_slugs, brand


def matches_category_filter(item: dict[str, Any], category_filter: str | None) -> bool:
    """Return True if candidate's category hierarchy satisfies `category_filter`.

    Supports single categories (`'Reis & Getreide'`), slugs (`'reis-getreide'`),
    and hierarchical paths (`'Fleisch & Fisch -> Fleisch'` or `'Kochen & Vorrat::Reis & Getreide'`).
    """
    if not category_filter or not category_filter.strip():
        return True

    segments = [
        _normalize_category_token(seg)
        for seg in _CATEGORY_SPLIT_REGEX.split(category_filter)
        if _normalize_category_token(seg)
    ]
    if not segments:
        return True

    item_cats = item.get("categories") or []
    item_slugs = item.get("category_slugs") or []
    if not item_cats and not item_slugs:
        return False

    if len(segments) > 1:
        curr_level = 0
        for seg in segments:
            matched = False
            for idx in range(curr_level, max(len(item_cats), len(item_slugs))):
                cname = _normalize_category_token(str(item_cats[idx])) if idx < len(item_cats) else ""
                cslug = _normalize_category_token(str(item_slugs[idx])) if idx < len(item_slugs) else ""
                if _category_segment_matches(seg, cname) or _category_segment_matches(seg, cslug):
                    curr_level = idx + 1
                    matched = True
                    break
            if not matched:
                return False
        return True

    seg = segments[0]
    for idx in range(max(len(item_cats), len(item_slugs))):
        cname = _normalize_category_token(str(item_cats[idx])) if idx < len(item_cats) else ""
        cslug = _normalize_category_token(str(item_slugs[idx])) if idx < len(item_slugs) else ""
        if _category_segment_matches(seg, cname) or _category_segment_matches(seg, cslug):
            return True
    return False


def extract_price_tiers(
    variant: dict[str, Any],
    price_eur: float,
    attrs: dict[str, Any],
    price_obj: dict[str, Any],
) -> list[dict[str, Any]]:
    """Extract normalized quantity discount tiers (Mengenrabatte / Staffelpreise).

    Returns a list of dicts sorted by min_quantity ascending:
        [{"min_quantity": int, "unit_price_eur": float, "discount_eur": float}, ...]
    """
    raw_tiers: list[Any] = []

    if isinstance(price_obj.get("tiers"), list):
        raw_tiers.extend(price_obj["tiers"])

    for p in variant.get("prices") or []:
        if isinstance(p, dict) and isinstance(p.get("tiers"), list):
            raw_tiers.extend(p["tiers"])

    if isinstance(variant.get("price_tiers"), list):
        raw_tiers.extend(variant["price_tiers"])

    for attr_key in (
        "TieredPrices",
        "PriceTiers",
        "TierPrices",
        "VolumePrices",
        "Mengenrabatt",
        "Staffelpreise",
        "Gradelpreise",
    ):
        attr_val = attrs.get(attr_key)
        if isinstance(attr_val, list):
            raw_tiers.extend(attr_val)
        elif isinstance(attr_val, str) and (
            attr_val.strip().startswith("[") or attr_val.strip().startswith("{")
        ):
            try:
                parsed = json.loads(attr_val)
                if isinstance(parsed, list):
                    raw_tiers.extend(parsed)
                elif isinstance(parsed, dict):
                    raw_tiers.append(parsed)
            except Exception:
                pass

    out: list[dict[str, Any]] = []
    seen_qtys: set[int] = set()

    for item in raw_tiers:
        if not isinstance(item, dict):
            continue

        min_q = None
        for q_key in (
            "minimumQuantity",
            "min_quantity",
            "minQuantity",
            "minQty",
            "quantity",
            "from",
            "ab",
            "threshold",
        ):
            if item.get(q_key) is not None:
                try:
                    min_q = int(item[q_key])
                    break
                except (ValueError, TypeError):
                    pass
        if min_q is None or min_q < 2 or min_q in seen_qtys:
            continue

        unit_price = None
        val_obj = item.get("value")
        if isinstance(val_obj, dict) and "centAmount" in val_obj:
            unit_price = cents_to_eur(val_obj)
        elif isinstance(val_obj, (int, float)):
            unit_price = float(val_obj)

        if unit_price is None:
            for p_key in (
                "price_eur",
                "priceEur",
                "unit_price",
                "unit_price_eur",
                "price",
            ):
                if item.get(p_key) is not None:
                    try:
                        unit_price = float(item[p_key])
                        break
                    except (ValueError, TypeError):
                        pass

        discount_val = None
        for d_key in (
            "discount_eur",
            "discountEur",
            "discount",
            "discount_per_unit",
        ):
            if item.get(d_key) is not None:
                try:
                    discount_val = float(item[d_key])
                    break
                except (ValueError, TypeError):
                    pass
        if discount_val is None:
            d_cent = item.get("discountCentAmount") or item.get("discount_cent_amount")
            if isinstance(d_cent, (int, float)) or (isinstance(d_cent, str) and d_cent.replace(".", "", 1).isdigit()):
                discount_val = float(d_cent) / 100.0

        if unit_price is None and discount_val is not None:
            unit_price = round(price_eur - discount_val, 4)
        elif unit_price is not None and discount_val is None:
            discount_val = round(price_eur - unit_price, 4)

        if unit_price is not None and 0 < unit_price < price_eur:
            seen_qtys.add(min_q)
            out.append(
                {
                    "min_quantity": min_q,
                    "unit_price_eur": round(unit_price, 4),
                    "discount_eur": round(
                        discount_val if discount_val is not None else (price_eur - unit_price), 4
                    ),
                }
            )

    out.sort(key=lambda t: t["min_quantity"])
    return out


def compute_optimal_pack_plan(
    product: dict[str, Any], min_packs: int
) -> tuple[int, float, float, bool]:
    """Find the pack quantity >= min_packs that minimizes total line price considering price tiers (Mengenrabatte).

    Returns:
        (optimal_packs, effective_unit_price_eur, line_total_eur, tier_applied)
    """
    regular_price = float(product.get("price_eur") or 0.0)
    tiers = product.get("price_tiers") or []

    def _unit_price(q: int) -> float:
        price = regular_price
        for t in tiers:
            min_q = int(t.get("min_quantity") or 0)
            t_price = float(t.get("unit_price_eur") or 0.0)
            if q >= min_q and 0 < t_price < price:
                price = t_price
        return price

    best_packs = max(1, min_packs)
    best_unit_price = _unit_price(best_packs)
    best_total = round(best_packs * best_unit_price, 2)
    tier_hit = best_unit_price < regular_price

    for t in tiers:
        t_min_q = int(t.get("min_quantity") or 0)
        if t_min_q > min_packs:
            t_unit_price = _unit_price(t_min_q)
            t_total = round(t_min_q * t_unit_price, 2)
            if t_total < best_total:
                best_packs = t_min_q
                best_unit_price = t_unit_price
                best_total = t_total
                tier_hit = True

    return best_packs, round(best_unit_price, 4), best_total, tier_hit


def extract_variant_summary(
    product: dict[str, Any], variant: dict[str, Any]
) -> dict[str, Any]:
    """Normalize a single Commercetools product variant with categories, €/unit, €/kg, €/L, weight, and crate info."""
    sku = variant.get("sku") or product.get("key") or ""
    try:
        article_id: int | None = int(sku)
    except (TypeError, ValueError):
        article_id = None

    name_obj = product.get("name") or {}
    name = (
        name_obj.get("de-DE")
        or next(iter(name_obj.values()), "")
        if isinstance(name_obj, dict)
        else str(name_obj)
    ).strip()
    name = re.sub(r"\s{2,}", " ", name)

    cat_names, cat_slugs, brand = extract_product_categories(product)

    attrs = {
        a.get("name"): a.get("value")
        for a in (variant.get("attributes") or [])
        if isinstance(a, dict) and a.get("name")
    }

    price_obj = variant.get("price") or {}
    regular_price_eur = cents_to_eur(price_obj.get("value"))
    disc_money = attrs.get("DiscountedPrice")
    price_eur = (
        cents_to_eur(disc_money)
        if isinstance(disc_money, dict) and disc_money.get("centAmount") is not None
        else regular_price_eur
    )

    price_fields = (price_obj.get("custom") or {}).get("fields") or {}
    ppu_text = (
        attrs.get("DiscountedPricePerUnitText")
        or price_fields.get("PricePerUnitText")
    )
    price_tiers = extract_price_tiers(variant, price_eur, attrs, price_obj)
    base_price_eur, base_unit = parse_price_per_unit_text(ppu_text)

    article_desc = str(attrs.get("ArticleDescription") or "").strip()
    items_in_frame = int(attrs.get("ItemsInFrame") or 1)
    mp_match = _MULTIPACK_REGEX.match(article_desc)
    piece_count = (
        int(mp_match.group("count"))
        if mp_match and items_in_frame == 1
        else max(1, items_in_frame)
    )
    price_per_unit_eur = (
        round(price_eur / piece_count, 4) if price_eur > 0 else 0.0
    )

    # Compute exact package weight (g) or volume (ml)
    vol = attrs.get("Volume")
    vol_unit_raw = attrs.get("VolumeUnit")
    vol_unit = (
        vol_unit_raw.get("key")
        if isinstance(vol_unit_raw, dict)
        else str(vol_unit_raw or "")
    ).lower()

    package_weight_g: float | None = None
    package_volume_ml: float | None = None
    single_item_volume_ml: float = 0.0

    if isinstance(vol, (int, float)) and vol > 0:
        total_raw = float(vol) * (max(1, items_in_frame))
        if vol_unit == "g":
            package_weight_g = round(total_raw, 2)
        elif vol_unit == "kg":
            package_weight_g = round(total_raw * 1000.0, 2)
        elif vol_unit == "ml":
            package_volume_ml = round(total_raw, 2)
            single_item_volume_ml = float(vol)
        elif vol_unit == "l":
            package_volume_ml = round(total_raw * 1000.0, 2)
            single_item_volume_ml = float(vol) * 1000.0

    desc_amount, desc_kind = parse_package_amount_from_description(article_desc)
    if package_weight_g is None and desc_kind == "g":
        package_weight_g = desc_amount
    if package_volume_ml is None and desc_kind == "ml":
        package_volume_ml = desc_amount
        if piece_count > 0 and desc_amount:
            single_item_volume_ml = desc_amount / piece_count

    if base_price_eur is None and price_eur > 0:
        if package_weight_g and package_weight_g > 0:
            base_price_eur = round(price_eur / (package_weight_g / 1000.0), 4)
            base_unit = "kg"
        elif package_volume_ml and package_volume_ml > 0:
            base_price_eur = round(price_eur / (package_volume_ml / 1000.0), 4)
            base_unit = "l"

    price_per_kg_eur = base_price_eur if base_unit == "kg" else None
    price_per_liter_eur = base_price_eur if base_unit == "l" else None

    if package_weight_g is None and price_per_kg_eur and price_per_kg_eur > 0 and price_eur > 0:
        package_weight_g = round((price_eur / price_per_kg_eur) * 1000.0, 1)
    if package_volume_ml is None and price_per_liter_eur and price_per_liter_eur > 0 and price_eur > 0:
        package_volume_ml = round((price_eur / price_per_liter_eur) * 1000.0, 1)

    container_raw = attrs.get("ContainerType")
    container_type = (
        container_raw.get("key")
        if isinstance(container_raw, dict)
        else (str(container_raw) if container_raw else None)
    )
    container_lower = (container_type or "").lower()
    desc_lower = article_desc.lower()
    is_can_or_grocery = (
        "can" in container_lower
        or "none" in container_lower
        or "(dose)" in desc_lower
    )
    is_crate = (
        not is_can_or_grocery
        and (
            items_in_frame >= 11
            or (items_in_frame >= 6 and single_item_volume_ml >= 700.0)
        )
    )

    is_available = attrs.get("IsAvailable")
    if is_available is None:
        is_available = price_fields.get("ArticleStatus", "A") == "A"

    images = variant.get("images") or []
    image_url = images[0].get("url") if images else None

    slug_map = product.get("slug") or {}
    slug = (
        slug_map.get("de-DE") or next(iter(slug_map.values()), "")
        if isinstance(slug_map, dict)
        else str(slug_map or "")
    ).strip()
    if not slug and name:
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    product_url = f"https://www.flaschenpost.de/p/{slug}" if slug else "https://www.flaschenpost.de"

    return {
        "article_id": article_id,
        "slug": slug,
        "product_url": product_url,
        "product_id": product.get("id"),
        "name": name,
        "brand": brand,
        "categories": cat_names,
        "category_slugs": cat_slugs,
        "description": article_desc or None,
        "is_available": bool(is_available),
        "price_eur": price_eur,
        "regular_price_eur": regular_price_eur,
        "price_tiers": price_tiers,
        "items_in_frame": items_in_frame,
        "piece_count": piece_count,
        "package_weight_g": package_weight_g,
        "package_volume_ml": package_volume_ml,
        "container_type": container_type,
        "is_crate": is_crate,
        "price_per_unit_eur": price_per_unit_eur,
        "price_per_kg_eur": price_per_kg_eur,
        "price_per_liter_eur": price_per_liter_eur,
        "base_price_eur": base_price_eur,
        "base_unit": base_unit,
        "price_per_unit": ppu_text,
        "article_status": price_fields.get("ArticleStatus"),
        "ingredients": attrs.get("Ingredients"),
        "image_url": image_url,
    }


def extract_product_summary(product: dict[str, Any]) -> dict[str, Any]:
    """Normalize the masterVariant of a Commercetools product card."""
    master = product.get("masterVariant") or {}
    return extract_variant_summary(product, master)


def extract_all_product_variants(product: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract normalized summaries for `masterVariant` and any additional `variants`."""
    variants = []
    if product.get("masterVariant"):
        variants.append(product["masterVariant"])
    variants.extend(product.get("variants") or [])
    out = [extract_variant_summary(product, v) for v in variants]
    return [s for s in out if s.get("article_id") is not None]


def normalize_search_queries(query: str) -> list[str]:
    """Generate primary, filler-stripped, and regional/brand synonym queries."""
    cleaned = query.strip()
    if not cleaned:
        return []

    out: list[str] = [cleaned]
    tokens = [
        t for t in re.split(r"\s+", cleaned) if t.lower() not in _QUERY_FILLER_WORDS
    ]
    stripped = " ".join(tokens).strip()
    if stripped and stripped.lower() not in {q.lower() for q in out}:
        out.append(stripped)

    lower_q = cleaned.lower()
    if "schwipp" in lower_q or "schwapp" in lower_q:
        fixed_brand = re.sub(
            r"schwipp", "Schwip", cleaned, flags=re.IGNORECASE
        )
        fixed_brand = re.sub(
            r"schwapp", "Schwap", fixed_brand, flags=re.IGNORECASE
        )
        if fixed_brand.lower() not in {q.lower() for q in out}:
            out.append(fixed_brand)

    if "karotte" in lower_q:
        moehren = re.sub(
            r"karotten?", "Möhren", cleaned, flags=re.IGNORECASE
        )
        if moehren.lower() not in {q.lower() for q in out}:
            out.append(moehren)

    return out


def _word_matches_haystack(
    word: str, haystack: str, haystack_compact: str, haystack_phonetic: str
) -> bool:
    """Check if a query word (or any of its German grocery/beverage synonyms) matches."""
    candidates = [word]
    for group in _SYNONYM_GROUPS:
        if word in group:
            candidates.extend(g for g in group if g != word)

    for cand in candidates:
        if cand in haystack:
            return True
        cand_compact = cand.replace(" ", "")
        if cand_compact in haystack_compact:
            return True
        cand_phonetic = _collapse_double_consonants(cand_compact)
        if len(cand_phonetic) >= 4 and cand_phonetic in haystack_phonetic:
            return True
    return False


_NEGATIVE_KEYWORD_REGEX = re.compile(
    r"(?:^|\s)(?:!|-(?=[A-Za-zÄÖÜäöüß]))\s*(?P<kw>[A-Za-zÄÖÜäöüß0-9_]+)(?=\s|$)"
)


def extract_negative_keywords(text: str) -> tuple[str, list[str]]:
    """Extract negative keywords starting with `!` from a query or line item.

    Examples:
      - 'Heidelbeeren !zucker' -> ('Heidelbeeren', ['zucker'])
      - '500g Heidelbeeren !zucker !bio' -> ('500g Heidelbeeren', ['zucker', 'bio'])
    """
    negatives: list[str] = []

    def _replace(m: re.Match[str]) -> str:
        kw = m.group("kw").strip()
        if kw and kw.lower() not in [k.lower() for k in negatives]:
            negatives.append(kw)
        return " "

    cleaned = _NEGATIVE_KEYWORD_REGEX.sub(_replace, text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned, negatives


def product_matches_negative_keywords(
    product: dict[str, Any], negative_keywords: list[str]
) -> bool:
    """Check if product text contains any of the negative keywords (case-insensitive)."""
    if not negative_keywords:
        return False

    haystack_parts = [
        str(product.get("name") or ""),
        str(product.get("description") or ""),
        str(product.get("ingredients") or ""),
    ]
    for cat in product.get("categories") or []:
        haystack_parts.append(str(cat))

    haystack = " ".join(haystack_parts).lower()

    for kw in negative_keywords:
        kw_clean = kw.strip().lower()
        if not kw_clean:
            continue
        if kw_clean in haystack:
            return True
    return False


def matches_query_qualifiers(item: dict[str, Any], query: str) -> bool:
    """Verify explicit percentage/word tokens in `query` match candidate text."""
    title_haystack = " ".join(
        str(x or "") for x in (item.get("name"), item.get("description"))
    ).lower()
    full_haystack = " ".join(
        str(x or "")
        for x in (item.get("name"), item.get("description"), item.get("ingredients"))
    ).lower()

    full_norm = full_haystack.replace(",", ".")
    full_compact = re.sub(r"\s+", "", full_norm)

    for q_match in _QUALIFIER_TOKEN_REGEX.finditer(query):
        qual_norm = re.sub(r"\s+", "", q_match.group(0)).replace(",", ".").lower()
        if qual_norm not in full_compact:
            return False

    query_wo_quals = _QUALIFIER_TOKEN_REGEX.sub(" ", query)
    words = [
        w.lower()
        for w in re.findall(r"[A-Za-zÄÖÜäöüß]{3,}", query_wo_quals)
        if w.lower() not in _QUERY_FILLER_WORDS
    ]
    if not words:
        return True

    title_compact = re.sub(r"\s+", "", title_haystack)
    title_phonetic = _collapse_double_consonants(title_compact)
    return all(
        _word_matches_haystack(w, title_haystack, title_compact, title_phonetic)
        for w in words
    )


class FlaschenpostApiClient:
    """Async client for communicating with the Flaschenpost backend."""

    def __init__(
        self,
        email: str,
        password: str,
        session: aiohttp.ClientSession | None = None,
        base_url: str = BASE_URL,
        default_payment_method: str | None = None,
        stored_payment_id: str | None = None,
    ) -> None:
        """Initialize the client with credentials and dedicated cookie session."""
        self._email = email
        self._password = password
        self._base_url = base_url.rstrip("/")
        self._owns_session = session is None
        self._session = session
        self._bearer_token: str | None = None
        self._web_user_guid: str | None = None
        self._session_guid: str = str(uuid.uuid4())
        self._cart_id: str | None = None
        self._warehouse_id: int | None = None
        self._customer_id: int | None = None
        self._categories_cache: dict[int, list[dict[str, Any]]] = {}
        self._auth_lock = asyncio.Lock()
        self.default_payment_method: str | None = default_payment_method
        self.stored_payment_id: str | None = stored_payment_id

    @property
    def web_user_guid(self) -> str | None:
        """Return customer external GUID discovered at login."""
        return self._web_user_guid

    @property
    def warehouse_id(self) -> int | None:
        """Return customer's local warehouse ID discovered from cart/account."""
        return self._warehouse_id

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            jar = aiohttp.CookieJar()
            timeout = aiohttp.ClientTimeout(total=20)
            self._session = aiohttp.ClientSession(cookie_jar=jar, timeout=timeout)
            self._owns_session = True
        return self._session

    async def async_close(self) -> None:
        """Close owned aiohttp session."""
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    async def async_login(self) -> dict[str, Any]:
        """Authenticate with email and password (`POST /api/v2/account/login`)."""
        async with self._auth_lock:
            session = await self._ensure_session()
            url = f"{self._base_url}/api/v2/account/login"
            headers = {
                "User-Agent": APP_USER_AGENT,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Correlation-Id": str(uuid.uuid4()),
                "Idempotency-Key": str(uuid.uuid4()),
            }
            payload = {
                "username": self._email,
                "password": self._password,
                "captcha": "",
                "redirectUrl": "is-not-needed",
            }
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status in (401, 403):
                    text = await resp.text()
                    raise FlaschenpostAuthError(
                        f"Login rejected (HTTP {resp.status}): {text[:200]}"
                    )
                if resp.status >= 400:
                    text = await resp.text()
                    raise FlaschenpostError(
                        f"Login failed (HTTP {resp.status}): {text[:200]}"
                    )
                data: dict[str, Any] = await resp.json()

            token = data.get("bearerToken")
            if not token:
                raise FlaschenpostAuthError("Login response did not contain bearerToken")
            if data.get("shopType", 0) != 0:
                raise FlaschenpostAuthError(
                    "Only B2C Flaschenpost accounts (shopType=0) are supported"
                )

            self._bearer_token = token
            self._web_user_guid = data.get("webUserExternalGuid")
            if data.get("userId"):
                self._customer_id = int(data["userId"])
            return data

    async def async_refresh_token(self) -> str:
        """Refresh JWT access token via cookies (`GET /api/v2/account/refresh`)."""
        async with self._auth_lock:
            session = await self._ensure_session()
            url = f"{self._base_url}/api/v2/account/refresh"
            headers = {
                "User-Agent": APP_USER_AGENT,
                "Accept": "application/json",
                "X-instance": "axiosAuthClient",
                "X-Correlation-Id": str(uuid.uuid4()),
            }
            async with session.get(url, headers=headers) as resp:
                if resp.status >= 400:
                    raise FlaschenpostAuthError(
                        f"Token refresh failed (HTTP {resp.status})"
                    )
                data = await resp.json()

            token = data.get("token")
            if not token:
                raise FlaschenpostAuthError("Refresh response missing token")
            self._bearer_token = token
            return token

    async def _request(
        self,
        method: str,
        path_or_url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        extra_headers: dict[str, str] | None = None,
        authenticated: bool = True,
        retry_on_conflict: bool = False,
    ) -> Any:
        """Execute an HTTP request with automatic token refresh & 409 retry."""
        if authenticated and not self._bearer_token:
            await self.async_login()

        session = await self._ensure_session()
        url = (
            path_or_url
            if path_or_url.startswith("http")
            else f"{self._base_url}{path_or_url}"
        )

        conflict_attempts = 3 if retry_on_conflict else 1
        auth_retried = False

        for attempt in range(conflict_attempts):
            headers: dict[str, str] = {
                "User-Agent": APP_USER_AGENT,
                "Accept": "application/json",
                "X-Correlation-Id": str(uuid.uuid4()),
                "X-fp-frontend-version": FRONTEND_VERSION,
            }
            if self._cart_id:
                headers["cart-id"] = self._cart_id
            if authenticated and self._bearer_token and not path_or_url.startswith("/webshop-checkout-api/api/v1/checkout/init"):
                headers["Authorization"] = f"Bearer {self._bearer_token}"
            if json_body is not None:
                headers["Content-Type"] = "application/json"
                if method.upper() in ("POST", "PUT"):
                    headers["Idempotency-Key"] = str(uuid.uuid4())
            if extra_headers:
                headers.update(extra_headers)

            async with session.request(
                method, url, params=params, json=json_body, headers=headers
            ) as resp:
                if authenticated and resp.status in (401, 403) and not auth_retried:
                    auth_retried = True
                    try:
                        await self.async_refresh_token()
                    except FlaschenpostError:
                        await self.async_login()
                    return await self._request(
                        method,
                        path_or_url,
                        params=params,
                        json_body=json_body,
                        extra_headers=extra_headers,
                        authenticated=authenticated,
                        retry_on_conflict=retry_on_conflict,
                    )

                if resp.status == 409 and attempt + 1 < conflict_attempts:
                    await asyncio.sleep(0.3 * (attempt + 1))
                    continue

                if resp.status == 204:
                    return None

                if resp.status >= 400:
                    body_text = await resp.text()
                    raise FlaschenpostError(
                        f"{method} {path_or_url} failed (HTTP {resp.status}): "
                        f"{body_text[:250]}"
                    )

                content_type = resp.headers.get("Content-Type", "")
                if "json" in content_type:
                    return await resp.json()
                text = await resp.text()
                return text

        raise FlaschenpostError(f"Exceeded conflict retries for {method} {path_or_url}")

    # -------------------------------------------------------------------------
    # Account & Orders
    # -------------------------------------------------------------------------

    async def async_get_my_account(self) -> dict[str, Any]:
        """Fetch customer profile and saved addresses (`GET /api/v2/myaccount`)."""
        data: dict[str, Any] = await self._request("GET", "/api/v2/myaccount")
        return data

    async def async_get_active_orders(self, offset: int = 0) -> dict[str, Any]:
        """Fetch active in-progress orders (`GET /api/v2/myaccount/order/active/{offset}`)."""
        return await self._request("GET", f"/api/v2/myaccount/order/active/{offset}")

    async def async_get_order_tracking(self, order_id: str) -> dict[str, Any]:
        """Fetch live delivery tracking details for an order."""
        return await self._request(
            "GET", f"/api/v2/myaccount/order/tracking/{order_id}"
        )

    async def async_get_past_orders(
        self, days: int = 180, offset: int = 0
    ) -> dict[str, Any]:
        """Fetch historical orders within the last `days` days."""
        now = datetime.now(UTC)
        start = now - timedelta(days=days)
        frm = quote(start.strftime("%a, %d %b %Y %H:%M:%S GMT"))
        unt = quote(now.strftime("%a, %d %b %Y %H:%M:%S GMT"))
        return await self._request(
            "GET", f"/api/v2/myaccount/order/history/{frm}/{unt}/{offset}"
        )

    async def async_get_all_past_orders(
        self, days: int = 365 * 3
    ) -> list[dict[str, Any]]:
        """Fetch all historical orders within the last `days` days, paginating if needed."""
        all_orders: list[dict[str, Any]] = []
        offset = 0
        limit_safety = 1000

        while len(all_orders) < limit_safety:
            resp = await self.async_get_past_orders(days=days, offset=offset)
            results = resp.get("results") or []
            if not results:
                break
            all_orders.extend(results)
            total = int(resp.get("total") or len(all_orders))
            if len(all_orders) >= total or len(results) == 0:
                break
            offset = len(all_orders)

        return all_orders

    # -------------------------------------------------------------------------
    # Cart & Delivery Slots
    # -------------------------------------------------------------------------

    async def async_get_cart(self) -> dict[str, Any]:
        """Fetch active shopping cart (`GET /webshop-cart-api/api/v4/cart`)."""
        cart: dict[str, Any] = await self._request("GET", "/webshop-cart-api/api/v4/cart")
        self._update_cart_metadata(cart)
        return cart

    async def async_create_new_cart(self) -> dict[str, Any]:
        """Reset/clear the customer's cart to a clean empty cart state."""
        self._cart_id = None
        cart = await self.async_get_cart()
        actions: list[dict[str, Any]] = []

        for li in cart.get("lineItems") or []:
            if li.get("id"):
                actions.append({"action": "removeLineItem", "lineItemId": li["id"]})

        for dc in cart.get("discountCodes") or []:
            dc_obj = dc.get("discountCode") or {}
            if dc_obj.get("id"):
                actions.append(
                    {
                        "action": "removeDiscountCode",
                        "discountCode": {
                            "id": dc_obj["id"],
                            "typeId": "discount-code",
                        },
                    }
                )

        ship_fields = (cart.get("shippingCustomFields") or {}).get("fields") or {}
        if any(
            ship_fields.get(k) is not None
            for k in (
                "SelectedDeliveryType",
                "SelectedDeliveryFrom",
                "SelectedDeliveryUntil",
                "SelectedDeliveryWindow",
            )
        ):
            for field_name in (
                "SelectedDeliveryFrom",
                "SelectedDeliveryUntil",
                "SelectedDeliveryWindow",
                "SelectedDeliveryType",
                "SelectedDeliveryValidation",
                "SelectedDeliveryExpiry",
                "SelectedDeliveryRegion",
                "SelectedDeliveryDisplay",
            ):
                actions.append(
                    {
                        "action": "setShippingCustomField",
                        "name": field_name,
                        "value": None,
                    }
                )
            actions.append(
                {
                    "action": "setCustomField",
                    "name": "GreenDeliveryWindowRequestId",
                    "value": None,
                }
            )

        if actions:
            cart = await self.async_apply_cart_actions(actions)
        return cart

    def _update_cart_metadata(self, cart: dict[str, Any]) -> None:
        if isinstance(cart, dict):
            if cart.get("id"):
                self._cart_id = cart["id"]
            fields = (cart.get("custom") or {}).get("fields") or {}
            if fields.get("RecentlyUsedWarehouseId"):
                self._warehouse_id = int(fields["RecentlyUsedWarehouseId"])
            if fields.get("CustomerId"):
                self._customer_id = int(fields["CustomerId"])

    async def async_is_in_polygon_delivery_area(
        self, latitude: float, longitude: float
    ) -> bool:
        """Check whether coordinates fall inside a Flaschenpost delivery area polygon."""
        coords = f"{latitude}, {longitude}"
        res = await self._request(
            "GET",
            f"/webshop-checkout-api/api/v2/address/isInPolygonDeliveryArea/{coords}",
            authenticated=False,
        )
        return bool(res)

    async def async_refresh_polygon_properties(self) -> dict[str, Any]:
        """Trigger backend recalculation of cart polygon properties and update warehouse id."""
        cart: dict[str, Any] = await self._request(
            "PATCH",
            "/webshop-cart-api/api/v4/refreshPolygonProperties",
            json_body={},
            retry_on_conflict=True,
        )
        self._update_cart_metadata(cart)
        return cart

    async def async_set_cart_positions(
        self, positions: list[dict[str, int]]
    ) -> dict[str, Any]:
        """Add or update article quantities in cart (`POST /addCartPositions`)."""
        normalized = [
            {
                "articleId": int(p["articleId"]),
                "origin": int(p.get("origin", ORIGIN_UNKNOWN)),
                "quantityTotal": int(p["quantityTotal"]),
            }
            for p in positions
        ]
        cart: dict[str, Any] = await self._request(
            "POST",
            "/webshop-cart-api/api/v4/addCartPositions",
            json_body={"positions": normalized},
            retry_on_conflict=True,
        )
        self._update_cart_metadata(cart)
        return cart

    async def async_apply_cart_actions(
        self, actions: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Execute Commercetools update actions on cart (`POST /cart`)."""
        cart: dict[str, Any] = await self._request(
            "POST",
            "/webshop-cart-api/api/v4/cart",
            json_body={"actions": actions},
            retry_on_conflict=True,
        )
        self._update_cart_metadata(cart)
        return cart

    async def async_remove_line_item(self, line_item_id: str) -> dict[str, Any]:
        """Remove a line item from the cart."""
        return await self.async_apply_cart_actions(
            [{"action": "removeLineItem", "lineItemId": line_item_id}]
        )

    async def async_add_discount_code(self, code: str) -> dict[str, Any]:
        """Apply a coupon or discount code to the cart (`POST /cart` action `addDiscountCode`)."""
        cleaned = code.strip()
        if not cleaned:
            raise FlaschenpostError("Discount code cannot be empty")
        return await self.async_apply_cart_actions(
            [{"action": "addDiscountCode", "code": cleaned}]
        )

    async def async_remove_discount_code(self, discount_code_id: str) -> dict[str, Any]:
        """Remove a discount code from the cart."""
        return await self.async_apply_cart_actions(
            [
                {
                    "action": "removeDiscountCode",
                    "discountCode": {
                        "id": discount_code_id,
                        "typeId": "discount-code",
                    },
                }
            ]
        )

    async def async_get_delivery_slots(
        self,
        cart: dict[str, Any] | None = None,
        account: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Fetch available delivery slots (`GET /webshop-slots-api/api/v3/Slot/...`)."""
        if cart is None:
            cart = await self.async_get_cart()
        cart_id = cart.get("id") or self._cart_id
        if not cart_id:
            return {"slots": [], "days": []}

        fields = (cart.get("custom") or {}).get("fields") or {}
        goods_cents = (fields.get("TotalGoods") or {}).get("centAmount", 0)
        basket_class = compute_basket_class(int(goods_cents))
        customer_id = fields.get("CustomerId") or self._customer_id or 0

        geo = (
            ((cart.get("shippingAddress") or {}).get("custom") or {})
            .get("fields", {})
            .get("GeoCoordinates")
        )
        if not geo and account:
            addrs = account.get("addresses") or []
            def_id = account.get("defaultShippingAddressId")
            addr = next(
                (a for a in addrs if a.get("id") == def_id),
                addrs[0] if addrs else {},
            )
            geo = ((addr.get("custom") or {}).get("fields") or {}).get(
                "GeoCoordinates"
            )

        if not geo:
            return {"slots": [], "days": []}

        return await self._request(
            "GET",
            f"/webshop-slots-api/api/v3/Slot/{cart_id}/{basket_class}",
            params={"customerId": customer_id, "geocoords": geo},
            extra_headers={"Ocp-Apim-Subscription-Key": SLOTS_APIM_KEY},
            authenticated=False,
        )

    @staticmethod
    def pick_available_slots(slots_response: dict[str, Any]) -> list[dict[str, Any]]:
        """Filter enabled delivery slots using `Z1e`/`Q1e` webshop rules."""
        days = slots_response.get("days") or []
        disabled_dates = {
            str(d.get("date"))[:10] for d in days if d.get("disabled") is True
        }
        today_str = datetime.now(UTC).strftime("%Y-%m-%d")

        valid: list[dict[str, Any]] = []
        for slot in slots_response.get("slots") or []:
            display = (slot.get("additionalData") or {}).get("display", "")
            if str(display).lower() == "disable":
                continue
            stype = slot.get("type")
            if stype == "DIRECT":
                if today_str not in disabled_dates:
                    valid.append(slot)
            else:
                sfrom = str(slot.get("from") or "")[:10]
                if sfrom and slot.get("until") and sfrom not in disabled_dates:
                    valid.append(slot)
        return valid

    async def async_select_delivery_slot(
        self,
        slot_type: str | None = None,
        slot_from: str | None = None,
    ) -> dict[str, Any]:
        """Select a delivery slot on the cart via `setShippingCustomField` actions."""
        cart = await self.async_get_cart()
        slots_resp = await self.async_get_delivery_slots(cart=cart)
        available = self.pick_available_slots(slots_resp)
        if not available:
            raise FlaschenpostOrderError("No enabled delivery slots currently available")

        chosen: dict[str, Any] | None = None
        if slot_from:
            chosen = next(
                (s for s in available if s.get("from") == slot_from), None
            )
        elif slot_type:
            chosen = next(
                (s for s in available if s.get("type") == slot_type), None
            )
        else:
            chosen = next(
                (s for s in available if s.get("type") == "DIRECT"), available[0]
            )

        if not chosen:
            raise FlaschenpostOrderError(
                f"Requested delivery slot (type={slot_type}, from={slot_from}) not found"
            )

        is_border = bool(slots_resp.get("isBorderRegion"))
        display_raw = (chosen.get("additionalData") or {}).get("display", "")
        display_mode = "GREEN" if str(display_raw).lower() == "green" else "NORMAL"

        actions = [
            {
                "action": "setShippingCustomField",
                "name": "SelectedDeliveryFrom",
                "value": chosen.get("from"),
            },
            {
                "action": "setShippingCustomField",
                "name": "SelectedDeliveryUntil",
                "value": chosen.get("until"),
            },
            {
                "action": "setShippingCustomField",
                "name": "SelectedDeliveryWindow",
                "value": chosen.get("window"),
            },
            {
                "action": "setShippingCustomField",
                "name": "SelectedDeliveryType",
                "value": chosen.get("type"),
            },
            {
                "action": "setShippingCustomField",
                "name": "SelectedDeliveryValidation",
                "value": chosen.get("validation"),
            },
            {
                "action": "setShippingCustomField",
                "name": "SelectedDeliveryExpiry",
                "value": slots_resp.get("expiry"),
            },
            {
                "action": "setShippingCustomField",
                "name": "SelectedDeliveryRegion",
                "value": "BORDER" if is_border else "CORE",
            },
            {
                "action": "setShippingCustomField",
                "name": "SelectedDeliveryDisplay",
                "value": display_mode,
            },
            {
                "action": "setCustomField",
                "name": "GreenDeliveryWindowRequestId",
                "value": slots_resp.get("request_id"),
            },
        ]
        return await self.async_apply_cart_actions(actions)

    # -------------------------------------------------------------------------
    # Categories, Product Search & Lazy Cart Resolution
    # -------------------------------------------------------------------------

    async def async_get_categories(
        self, warehouse_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Fetch and cache the warehouse category & subcategory tree (`GET /php-product-api/v1/category/warehouse/{wh}`)."""
        wh_id = warehouse_id or self._warehouse_id
        if not wh_id:
            await self.async_get_cart()
            wh_id = self._warehouse_id or 1

        if wh_id in self._categories_cache:
            return self._categories_cache[wh_id]

        raw = await self._request(
            "GET",
            f"/php-product-api/v1/category/warehouse/{wh_id}",
            authenticated=False,
        )

        def _normalize_category_node(node: dict[str, Any]) -> dict[str, Any]:
            nname_map = node.get("name") or {}
            nname = (
                nname_map.get("de-DE") or next(iter(nname_map.values()), "")
                if isinstance(nname_map, dict)
                else str(nname_map or "")
            ).strip()
            nslug_map = node.get("slug") or {}
            nslug = (
                nslug_map.get("de-DE") or next(iter(nslug_map.values()), "")
                if isinstance(nslug_map, dict)
                else str(nslug_map or "")
            ).strip()
            raw_children = node.get("children") or []
            subs = [
                _normalize_category_node(child)
                for child in raw_children
                if isinstance(child, dict)
            ]
            return {
                "id": node.get("id"),
                "external_id": node.get("externalId"),
                "name": nname,
                "slug": nslug,
                "subcategories": subs,
            }

        normalized: list[dict[str, Any]] = [
            _normalize_category_node(top)
            for top in (raw if isinstance(raw, list) else [])
            if isinstance(top, dict)
        ]

        self._categories_cache[wh_id] = normalized
        return normalized

    async def _resolve_matching_subcategory_ids(
        self, category_filter: str, warehouse_id: int
    ) -> list[str]:
        """Resolve `subcategory_id` (`externalId`) values matching `category_filter` for limitless category depth."""
        tree = await self.async_get_categories(warehouse_id=warehouse_id)
        segments = [
            _normalize_category_token(seg)
            for seg in _CATEGORY_SPLIT_REGEX.split(category_filter)
            if _normalize_category_token(seg)
        ]
        if not segments:
            return []

        def _collect_external_ids(node: dict[str, Any], acc: list[str], seen: set[str]) -> None:
            ext_id = node.get("external_id")
            if ext_id:
                s_id = str(ext_id)
                if s_id not in seen:
                    seen.add(s_id)
                    acc.append(s_id)
            for sub in node.get("subcategories") or []:
                _collect_external_ids(sub, acc, seen)

        def _node_matches_seg(node: dict[str, Any], seg: str) -> bool:
            n_name = _normalize_category_token(node.get("name") or "")
            n_slug = _normalize_category_token(node.get("slug") or "")
            return _category_segment_matches(seg, n_name) or _category_segment_matches(seg, n_slug)

        def _path_matches(path: list[dict[str, Any]], segs: list[str]) -> bool:
            """Check if segs match nodes in path in order, ending at the current node (path[-1])."""
            if len(path) < len(segs):
                return False
            if not _node_matches_seg(path[-1], segs[-1]):
                return False
            s_idx = len(segs) - 2
            p_idx = len(path) - 2
            while s_idx >= 0 and p_idx >= 0:
                if _node_matches_seg(path[p_idx], segs[s_idx]):
                    s_idx -= 1
                p_idx -= 1
            return s_idx < 0

        matched_nodes: list[dict[str, Any]] = []

        def _traverse(node: dict[str, Any], current_path: list[dict[str, Any]]) -> None:
            path = [*current_path, node]
            if len(segments) == 1:
                if _node_matches_seg(node, segments[0]):
                    matched_nodes.append(node)
            else:
                if _path_matches(path, segments):
                    matched_nodes.append(node)
            for child in node.get("subcategories") or []:
                _traverse(child, path)

        for top in tree:
            _traverse(top, [])

        matched_ids: list[str] = []
        seen_ids: set[str] = set()
        for m_node in matched_nodes:
            _collect_external_ids(m_node, matched_ids, seen_ids)

        return matched_ids

    async def async_search_products(
        self,
        query: str,
        *,
        limit: int = 10,
        warehouse_id: int | None = None,
        category: str | None = None,
        name_contains: str | None = None,
        sort_by: str = SORT_RELEVANCE,
        max_price_eur: float | None = None,
        max_price_per_unit_eur: float | None = None,
        max_price_per_kg_eur: float | None = None,
        max_price_per_liter_eur: float | None = None,
        only_available: bool = True,
        only_crates: bool = False,
        negative_keywords: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Search products in customer's local warehouse with category, unit/kg/L, and crate filtering."""
        clean_query, extracted_neg = extract_negative_keywords(query)
        effective_neg: list[str] = list(
            dict.fromkeys(
                [k.strip().lower() for k in (negative_keywords or []) + extracted_neg if k.strip()]
            )
        )

        wh_id = warehouse_id or self._warehouse_id
        if not wh_id:
            await self.async_get_cart()
            wh_id = self._warehouse_id or 1

        seen_ids: set[int] = set()
        candidates: list[dict[str, Any]] = []

        # When a category filter is specified and matches specific warehouse subcategories
        # (e.g. "Reis & Getreide" -> subcategory_id=595), load that subcategory's catalog
        # directly first so items are never drowned out by other categories in Elastic search
        if category and category.strip():
            sub_ids = await self._resolve_matching_subcategory_ids(category, wh_id)
            if sub_ids:
                for sub_id in sub_ids[:12]:
                    sub_prods = await self._request(
                        "GET",
                        f"/php-product-api/v1/products/prv/warehouse/{wh_id}",
                        params={"subcategory_id": sub_id},
                        authenticated=False,
                    )
                    if isinstance(sub_prods, list):
                        for prod in sub_prods:
                            for summary in extract_all_product_variants(prod):
                                aid = summary["article_id"]
                                if aid not in seen_ids:
                                    seen_ids.add(aid)
                                    candidates.append(summary)

        queries = normalize_search_queries(clean_query or query)
        fetch_size = (
            50
            if sort_by != SORT_RELEVANCE or name_contains or only_crates or category
            else max(25, limit * 2)
        )
        for q_str in queries:
            data = await self._request(
                "GET",
                f"/elastic-query-portal/{wh_id}/v2/search",
                params={"q": q_str, "pageSize": min(100, fetch_size)},
                authenticated=False,
            )
            for prod in data.get("results") or []:
                for summary in extract_all_product_variants(prod):
                    aid = summary["article_id"]
                    if aid not in seen_ids:
                        seen_ids.add(aid)
                        candidates.append(summary)

        # Negative keywords filter (e.g. "!zucker", "!bio")
        if effective_neg:
            candidates = [
                c
                for c in candidates
                if not product_matches_negative_keywords(c, effective_neg)
            ]

        # 1. Category filter (enforced strictly when specified)
        if category and category.strip():
            candidates = [
                c for c in candidates if matches_category_filter(c, category)
            ]

        # 2. Availability filter
        if only_available:
            candidates = [c for c in candidates if c.get("is_available", True)]

        # 3. Optional explicit substring filter
        if name_contains:
            needle = name_contains.strip().lower()
            candidates = [
                c
                for c in candidates
                if needle in str(c.get("name") or "").lower()
                or needle in str(c.get("description") or "").lower()
            ]

        # 4. Strict token/qualifier/synonym match
        if category and category.strip():
            # When an explicit category was given, require query words to match
            # instead of falling back to unrelated items in that category
            candidates = [
                c for c in candidates if matches_query_qualifiers(c, clean_query or query)
            ]
        else:
            strict = [c for c in candidates if matches_query_qualifiers(c, clean_query or query)]
            if strict:
                candidates = strict

        # 5. Crate filter (for "1 Kiste ...")
        if only_crates:
            crates = [c for c in candidates if c.get("is_crate")]
            if crates:
                candidates = crates
            else:
                multi = [c for c in candidates if (c.get("items_in_frame") or 1) >= 6]
                if multi:
                    candidates = multi

        # 6. Price / unit-price / base-price ceiling filters
        if max_price_eur is not None:
            candidates = [
                c for c in candidates if (c.get("price_eur") or 0) <= max_price_eur
            ]
        if max_price_per_unit_eur is not None:
            candidates = [
                c
                for c in candidates
                if c.get("price_per_unit_eur") is not None
                and c["price_per_unit_eur"] <= max_price_per_unit_eur
            ]
        if max_price_per_kg_eur is not None:
            candidates = [
                c
                for c in candidates
                if c.get("price_per_kg_eur") is not None
                and c["price_per_kg_eur"] <= max_price_per_kg_eur
            ]
        if max_price_per_liter_eur is not None:
            candidates = [
                c
                for c in candidates
                if c.get("price_per_liter_eur") is not None
                and c["price_per_liter_eur"] <= max_price_per_liter_eur
            ]

        # 7. Sort
        if sort_by == SORT_PRICE:
            candidates.sort(
                key=lambda c: (c.get("price_eur") is None, c.get("price_eur") or 1e9)
            )
        elif sort_by == SORT_PRICE_PER_UNIT:
            candidates.sort(
                key=lambda c: (
                    c.get("price_per_unit_eur") is None,
                    c.get("price_per_unit_eur") or 1e9,
                    c.get("price_eur") or 1e9,
                )
            )
        elif sort_by == SORT_PRICE_PER_KG:
            candidates = [
                c for c in candidates if c.get("price_per_kg_eur") is not None
            ]
            candidates.sort(
                key=lambda c: (c["price_per_kg_eur"], c.get("price_eur") or 1e9)
            )
        elif sort_by == SORT_PRICE_PER_LITER:
            candidates = [
                c for c in candidates if c.get("price_per_liter_eur") is not None
            ]
            candidates.sort(
                key=lambda c: (c["price_per_liter_eur"], c.get("price_eur") or 1e9)
            )

        return candidates[:limit]

    async def async_resolve_item_for_cart(
        self,
        query: str,
        *,
        category: str | None = None,
        unit_type: str = UNIT_TYPE_UNIT,
        target_amount_base: float = 1.0,
        article_id: int | None = None,
        sort_by: str | None = None,
        name_contains: str | None = None,
        negative_keywords: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Lazily resolve a shopping list requirement against live warehouse stock, category, and prices, supporting Mengenrabatte."""
        clean_query, extracted_neg = extract_negative_keywords(query)
        effective_neg: list[str] = list(
            dict.fromkeys(
                [k.strip().lower() for k in (negative_keywords or []) + extracted_neg if k.strip()]
            )
        )

        if article_id is not None:
            cards = await self.async_get_products_by_article_ids([article_id])
            chosen = cards[0] if cards else {"article_id": article_id, "name": query}
            pkg_w = chosen.get("package_weight_g")
            pkg_v = chosen.get("package_volume_ml")
            if unit_type == UNIT_TYPE_WEIGHT_G and pkg_w and pkg_w > 0:
                min_p = max(1, math.ceil(target_amount_base / pkg_w - 1e-9))
            elif unit_type == UNIT_TYPE_VOLUME_ML and pkg_v and pkg_v > 0:
                min_p = max(1, math.ceil(target_amount_base / pkg_v - 1e-9))
            else:
                min_p = max(1, math.ceil(target_amount_base - 1e-9))

            qty, eff_price, line_total, tier_applied = compute_optimal_pack_plan(
                chosen, min_p
            )
            if unit_type == UNIT_TYPE_WEIGHT_G and pkg_w and pkg_w > 0:
                delivered = round(qty * pkg_w, 1)
            elif unit_type == UNIT_TYPE_VOLUME_ML and pkg_v and pkg_v > 0:
                delivered = round(qty * pkg_v, 1)
            else:
                delivered = float(qty)

            return {
                "article_id": int(article_id),
                "slug": chosen.get("slug"),
                "product_url": chosen.get("product_url"),
                "quantity": qty,
                "unit_type": unit_type,
                "requested_amount_base": target_amount_base,
                "delivered_amount_base": delivered,
                "line_total_eur": line_total,
                "unit_price_eur": eff_price,
                "tier_applied": tier_applied,
                "product": chosen,
            }

        if unit_type == UNIT_TYPE_WEIGHT_G:
            effective_sort = sort_by or SORT_PRICE
            search_sort = (
                SORT_PRICE_PER_KG
                if effective_sort == SORT_PRICE_PER_KG
                else SORT_PRICE
            )
            pool = await self.async_search_products(
                clean_query or query,
                limit=50,
                category=category,
                name_contains=name_contains,
                sort_by=search_sort,
                only_available=True,
                negative_keywords=effective_neg,
            )
            weighted = [
                c
                for c in pool
                if c.get("package_weight_g") and float(c["package_weight_g"]) > 0
            ]
            if not weighted:
                return None

            def _plan_w(cand: dict[str, Any]) -> tuple[int, float, float, bool]:
                min_p = max(
                    1,
                    math.ceil(
                        target_amount_base / float(cand["package_weight_g"]) - 1e-9
                    ),
                )
                return compute_optimal_pack_plan(cand, min_p)

            def _actual_w(cand: dict[str, Any]) -> float:
                return _plan_w(cand)[0] * float(cand["package_weight_g"])

            max_reasonable_g = max(
                target_amount_base * 2.0, target_amount_base + 500.0
            )
            reasonable = [c for c in weighted if _actual_w(c) <= max_reasonable_g]
            if reasonable:
                weighted = reasonable

            if effective_sort == SORT_PRICE:
                weighted.sort(
                    key=lambda c: (
                        _plan_w(c)[2],  # line_total_eur
                        round(_plan_w(c)[1] / (float(c["package_weight_g"]) / 1000.0), 4),  # eff price per kg
                        abs(_actual_w(c) - target_amount_base),
                        int(c.get("article_id") or 0),
                    )
                )
            elif effective_sort == SORT_PRICE_PER_KG:
                weighted.sort(
                    key=lambda c: (
                        round(_plan_w(c)[1] / (float(c["package_weight_g"]) / 1000.0), 4),
                        _plan_w(c)[2],
                        _actual_w(c) - target_amount_base,
                    )
                )
            elif effective_sort == SORT_PRICE_PER_UNIT:
                weighted.sort(
                    key=lambda c: (
                        _plan_w(c)[1],
                        _plan_w(c)[2],
                    )
                )

            winner = weighted[0]
            qty, eff_price, line_total, tier_applied = _plan_w(winner)
            delivered = round(_actual_w(winner), 1)
            return {
                "article_id": int(winner["article_id"]),
                "slug": winner.get("slug"),
                "product_url": winner.get("product_url"),
                "quantity": qty,
                "unit_type": unit_type,
                "requested_amount_base": target_amount_base,
                "delivered_amount_base": delivered,
                "line_total_eur": line_total,
                "unit_price_eur": eff_price,
                "tier_applied": tier_applied,
                "product": winner,
            }

        if unit_type == UNIT_TYPE_VOLUME_ML:
            effective_sort = sort_by or SORT_PRICE
            search_sort = (
                SORT_PRICE_PER_LITER
                if effective_sort == SORT_PRICE_PER_LITER
                else SORT_PRICE
            )
            pool = await self.async_search_products(
                clean_query or query,
                limit=50,
                category=category,
                name_contains=name_contains,
                sort_by=search_sort,
                only_available=True,
                negative_keywords=effective_neg,
            )
            volumetric = [
                c
                for c in pool
                if c.get("package_volume_ml") and float(c["package_volume_ml"]) > 0
            ]
            if not volumetric:
                return None

            def _plan_v(cand: dict[str, Any]) -> tuple[int, float, float, bool]:
                min_p = max(
                    1,
                    math.ceil(
                        target_amount_base / float(cand["package_volume_ml"]) - 1e-9
                    ),
                )
                return compute_optimal_pack_plan(cand, min_p)

            def _actual_v(cand: dict[str, Any]) -> float:
                return _plan_v(cand)[0] * float(cand["package_volume_ml"])

            max_reasonable_ml = max(
                target_amount_base * 2.0, target_amount_base + 1500.0
            )
            reasonable_v = [
                c for c in volumetric if _actual_v(c) <= max_reasonable_ml
            ]
            if reasonable_v:
                volumetric = reasonable_v

            if effective_sort == SORT_PRICE:
                volumetric.sort(
                    key=lambda c: (
                        _plan_v(c)[2],  # line_total_eur
                        round(_plan_v(c)[1] / (float(c["package_volume_ml"]) / 1000.0), 4),  # eff price per liter
                        abs(_actual_v(c) - target_amount_base),
                        int(c.get("article_id") or 0),
                    )
                )
            elif effective_sort == SORT_PRICE_PER_LITER:
                volumetric.sort(
                    key=lambda c: (
                        round(_plan_v(c)[1] / (float(c["package_volume_ml"]) / 1000.0), 4),
                        _plan_v(c)[2],
                    )
                )
            elif effective_sort == SORT_PRICE_PER_UNIT:
                volumetric.sort(
                    key=lambda c: (
                        _plan_v(c)[1],
                        _plan_v(c)[2],
                    )
                )

            winner = volumetric[0]
            qty, eff_price, line_total, tier_applied = _plan_v(winner)
            delivered = round(_actual_v(winner), 1)
            return {
                "article_id": int(winner["article_id"]),
                "slug": winner.get("slug"),
                "product_url": winner.get("product_url"),
                "quantity": qty,
                "unit_type": unit_type,
                "requested_amount_base": target_amount_base,
                "delivered_amount_base": delivered,
                "line_total_eur": line_total,
                "unit_price_eur": eff_price,
                "tier_applied": tier_applied,
                "product": winner,
            }

        if unit_type == UNIT_TYPE_CRATE:
            effective_sort = sort_by or SORT_PRICE
            search_sort = (
                SORT_PRICE_PER_LITER
                if effective_sort == SORT_PRICE_PER_LITER
                else SORT_PRICE
            )
            pool = await self.async_search_products(
                clean_query or query,
                limit=25,
                category=category,
                name_contains=name_contains,
                sort_by=search_sort,
                only_available=True,
                only_crates=True,
                negative_keywords=effective_neg,
            )
            if not pool:
                return None
            min_crates = max(1, math.ceil(target_amount_base - 1e-9))

            def _plan_c(cand: dict[str, Any]) -> tuple[int, float, float, bool]:
                return compute_optimal_pack_plan(cand, min_crates)

            if effective_sort == SORT_PRICE:
                pool.sort(
                    key=lambda c: (
                        _plan_c(c)[2],  # line_total_eur
                        c.get("price_per_liter_eur") or 1e9,
                        int(c.get("article_id") or 0),
                    )
                )
            elif effective_sort == SORT_PRICE_PER_LITER:
                pool.sort(
                    key=lambda c: (
                        c.get("price_per_liter_eur") is None,
                        c.get("price_per_liter_eur") or 1e9,
                        _plan_c(c)[2],
                    )
                )
            winner = pool[0]
            qty, eff_price, line_total, tier_applied = _plan_c(winner)
            return {
                "article_id": int(winner["article_id"]),
                "slug": winner.get("slug"),
                "product_url": winner.get("product_url"),
                "quantity": qty,
                "unit_type": unit_type,
                "requested_amount_base": target_amount_base,
                "delivered_amount_base": float(qty),
                "line_total_eur": line_total,
                "unit_price_eur": eff_price,
                "tier_applied": tier_applied,
                "product": winner,
            }

        effective_sort = sort_by or SORT_PRICE
        pool = await self.async_search_products(
            clean_query or query,
            limit=25,
            category=category,
            name_contains=name_contains,
            sort_by=effective_sort,
            only_available=True,
            negative_keywords=effective_neg,
        )
        if not pool:
            return None
        min_u = max(1, math.ceil(target_amount_base - 1e-9))

        def _plan_u(cand: dict[str, Any]) -> tuple[int, float, float, bool]:
            return compute_optimal_pack_plan(cand, min_u)

        if effective_sort == SORT_PRICE:
            pool.sort(
                key=lambda c: (
                    _plan_u(c)[2],  # line_total_eur
                    _plan_u(c)[1],  # unit_price_eur
                    int(c.get("article_id") or 0),
                )
            )
        elif effective_sort == SORT_PRICE_PER_UNIT:
            pool.sort(
                key=lambda c: (
                    _plan_u(c)[1],  # unit_price_eur
                    _plan_u(c)[2],  # line_total_eur
                )
            )
        winner = pool[0]
        qty, eff_price, line_total, tier_applied = _plan_u(winner)
        return {
            "article_id": int(winner["article_id"]),
            "slug": winner.get("slug"),
            "product_url": winner.get("product_url"),
            "quantity": qty,
            "unit_type": unit_type,
            "requested_amount_base": target_amount_base,
            "delivered_amount_base": float(qty),
            "line_total_eur": line_total,
            "unit_price_eur": eff_price,
            "tier_applied": tier_applied,
            "product": winner,
        }

    async def async_get_products_by_article_ids(
        self, article_ids: list[int], warehouse_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Hydrate product cards by numeric `articleId`s."""
        if not article_ids:
            return []
        wh_id = warehouse_id or self._warehouse_id
        if not wh_id:
            await self.async_get_cart()
            wh_id = self._warehouse_id or 1

        joined = ",".join(str(int(a)) for a in article_ids[:50])
        products = await self._request(
            "GET",
            f"/php-product-api/v1/products/prv/warehouse/{wh_id}",
            params={"article_ids": joined},
            authenticated=False,
        )
        if not isinstance(products, list):
            return []
        return [extract_product_summary(p) for p in products]

    async def async_get_favorites(self, limit: int = 25) -> list[dict[str, Any]]:
        """Fetch customer's personal favorite products ("Meine Getränke")."""
        if not self._web_user_guid:
            await self.async_login()
        wh_id = self._warehouse_id
        if not wh_id:
            await self.async_get_cart()
            wh_id = self._warehouse_id or 1

        path = (
            f"/ds-recommendation-api/api/v2/recommendations/my_products/"
            f"{self._web_user_guid}/{self._session_guid}/{wh_id}"
        )
        data = await self._request(
            "POST",
            path,
            params={"sp": "1"},
            json_body={"basket": [], "cart": {}},
            extra_headers={"Ocp-Apim-Subscription-Key": RECOMMENDATIONS_APIM_KEY},
            authenticated=False,
        )
        recs = (data or {}).get("recommendations") or []
        article_ids = [
            int(r["articleId"])
            for r in recs[:limit]
            if r.get("articleId") is not None
        ]
        if not article_ids:
            return []
        return await self.async_get_products_by_article_ids(
            article_ids, warehouse_id=wh_id
        )

    # -------------------------------------------------------------------------
    # Saved Payment Instruments & Headless Ordering
    # -------------------------------------------------------------------------

    async def async_get_saved_payment_methods(self) -> list[dict[str, Any]]:
        """Fetch all saved payment instruments on the customer profile (Credit Cards, PayPal, SEPA, Invoice)."""
        cart = await self.async_get_cart()
        cart_id = cart.get("id")
        amount_cents = (cart.get("totalPrice") or {}).get("centAmount", 0)
        account = await self.async_get_my_account()
        custom = (account.get("custom") or {}).get("fields") or {}

        # Resolve shopperReference & ltkaid (address key)
        shopper_ref = ""
        addr_key = ""
        for a in account.get("addresses") or []:
            ref = ((a.get("custom") or {}).get("fields") or {}).get("AddressReference")
            if ref:
                shopper_ref = ref
                addr_key = a.get("key", "")
                break

        methods: list[dict[str, Any]] = []

        # 1. Saved Credit Cards via PayPal Card Vault (current Flaschenpost default)
        try:
            pp_card = await self._request(
                "GET",
                f"/webshop-checkout-api/api/v1/payment/paypal-card/init?cart={cart_id}&shopperReference={shopper_ref}",
                extra_headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"},
            )
            for vc in pp_card.get("vaultCards") or []:
                brand = str(vc.get("brand") or "Kreditkarte").upper()
                last4 = str(vc.get("last_digits") or "")
                exp = str(vc.get("expiry") or "")
                methods.append({
                    "id": f"credit_card:{vc['id']}",
                    "payment_method": PAYMENT_CREDIT_CARD,
                    "stored_payment_id": vc["id"],
                    "brand": brand,
                    "last4": last4,
                    "label": f"Kreditkarte {brand} •••• {last4} (Exp: {exp})",
                    "payment_id": pp_card.get("paymentId"),
                    "provider": "paypal_vault",
                })
        except Exception as err:
            _LOGGER.debug("Could not query PayPal stored cards: %s", err)

        # 2. Saved Credit Cards via Adyen
        try:
            cb = str(int(time.time() * 1000))
            adyen_init = await self._request(
                "GET",
                f"/webshop-checkout-api/api/v1/payment/adyen/init?amount={amount_cents}&cart={cart_id}&allowedPaymentMethods=scheme&shopperReferenceAdd={shopper_ref}&cacheBuster={cb}",
            )
            session_resp = adyen_init.get("adyenSessionResponse") or {}
            session_id = session_resp.get("id")
            session_data = session_resp.get("sessionData")
            if session_id and session_data:
                setup_url = f"https://checkoutshopper-live.adyen.com/checkoutshopper/v1/sessions/{session_id}/setup?clientKey={ADYEN_CLIENT_KEY}"
                async with self._session.post(setup_url, json={"sessionData": session_data}) as r:
                    if r.status == 200:
                        body = await r.json()
                        for sp in body.get("storedPaymentMethods") or []:
                            brand = str(sp.get("brand") or "Kreditkarte").upper()
                            last4 = str(sp.get("lastFour") or "")
                            exp = f"{sp.get('expiryMonth', '')}/{sp.get('expiryYear', '')}"
                            methods.append({
                                "id": f"credit_card:{sp['id']}",
                                "payment_method": PAYMENT_CREDIT_CARD,
                                "stored_payment_id": sp["id"],
                                "brand": sp.get("brand"),
                                "last4": last4,
                                "label": f"Kreditkarte {brand} •••• {last4} (Exp: {exp})",
                                "provider": "adyen",
                            })
        except Exception as err:
            _LOGGER.debug("Could not query Adyen stored cards: %s", err)

        # Always include generic Credit Card option
        if not any(m["payment_method"] == PAYMENT_CREDIT_CARD for m in methods):
            methods.append({
                "id": PAYMENT_CREDIT_CARD,
                "payment_method": PAYMENT_CREDIT_CARD,
                "label": "Kreditkarte (Credit Card)",
            })

        # 3. Saved PayPal Accounts
        paypal_entries = custom.get("PaypalCustomerIds") or []
        for pe in paypal_entries:
            try:
                parsed = json.loads(pe) if isinstance(pe, str) else pe
                email = parsed.get("customerEmailAddress") or self._email
                cid = parsed.get("customerId") or email
                methods.append({
                    "id": f"paypal:{cid}",
                    "payment_method": PAYMENT_PAYPAL,
                    "stored_payment_id": cid,
                    "label": f"PayPal ({email})",
                    "email": email,
                })
            except Exception:
                pass
        if not paypal_entries:
            methods.append({
                "id": PAYMENT_PAYPAL,
                "payment_method": PAYMENT_PAYPAL,
                "label": "PayPal",
            })

        # 3. Saved SEPA Direct Debit Accounts
        try:
            direct_init = await self._request(
                "GET",
                "/webshop-checkout-api/api/v1/payment/direct/init",
                params={
                    "cart": cart_id,
                    "amount": amount_cents,
                    "shopperReferenceAdd": shopper_ref,
                    "allowedPaymentMethods": "undefined",
                    "ltkaid": addr_key,
                    "cb": "0",
                },
            )
            for d in direct_init.get("directDebitAccounts") or []:
                if not d.get("isBlackListed") and d.get("ibanMasked"):
                    iban = d["ibanMasked"]
                    methods.append({
                        "id": f"sepa:{iban[-4:]}",
                        "payment_method": PAYMENT_SEPA,
                        "iban_last4": iban[-4:],
                        "label": f"SEPA Lastschrift ({iban})",
                    })
        except Exception as err:
            _LOGGER.debug("Could not query direct debit accounts: %s", err)

        if not any(m["payment_method"] == PAYMENT_SEPA for m in methods):
            methods.append({
                "id": PAYMENT_SEPA,
                "payment_method": PAYMENT_SEPA,
                "label": "SEPA Lastschrift (Direct Debit)",
            })

        # 4. Direct Invoice (if customer profile is eligible)
        if custom.get("PaymentInvoiceAllowed"):
            methods.append({
                "id": PAYMENT_INVOICE,
                "payment_method": PAYMENT_INVOICE,
                "label": "Rechnung (Direct Invoice)",
            })

        return methods

    async def async_place_order(
        self,
        *,
        payment_method: str | None = None,
        iban_last4: str | None = None,
        stored_payment_id: str | None = None,
        slot_type: str | None = None,
        slot_from: str | None = None,
        on_3ds_required: Callable[[dict[str, Any]], Any] | None = None,
        wait_3ds_timeout_sec: float = 120.0,
        poll_3ds_interval_sec: float = 3.0,
    ) -> dict[str, Any]:
        """Place the current cart order headlessly using saved Credit Card, PayPal, SEPA, or Invoice."""
        if not payment_method:
            payment_method = self.default_payment_method or PAYMENT_CREDIT_CARD
        if not stored_payment_id:
            stored_payment_id = self.stored_payment_id

        if payment_method == PAYMENT_CREDIT_CARD and not stored_payment_id:
            try:
                saved = await self.async_get_saved_payment_methods()
                cc_methods = [
                    m
                    for m in saved
                    if m.get("payment_method") == PAYMENT_CREDIT_CARD
                    and m.get("stored_payment_id")
                ]
                if cc_methods:
                    stored_payment_id = cc_methods[0]["stored_payment_id"]
            except Exception as err:
                _LOGGER.debug("Could not determine saved credit card: %s", err)

        if not payment_method:
            payment_method = PAYMENT_CREDIT_CARD

        if payment_method not in ALL_PAYMENT_METHODS:
            raise FlaschenpostOrderError(
                f"Unsupported payment_method '{payment_method}' (must be one of: {', '.join(ALL_PAYMENT_METHODS)})"
            )

        cart = await self.async_apply_cart_actions(
            [{"action": "recalculate", "updateProductData": True}]
        )
        line_items = cart.get("lineItems") or []
        if not line_items:
            raise FlaschenpostOrderError("Cannot place order: cart is empty")

        ship_fields = (cart.get("shippingCustomFields") or {}).get("fields") or {}
        expiry_str = ship_fields.get("SelectedDeliveryExpiry")
        is_expired = False
        if expiry_str:
            try:
                exp_dt = datetime.fromisoformat(str(expiry_str).replace("Z", "+00:00"))
                if datetime.now(UTC) >= exp_dt:
                    is_expired = True
            except Exception:
                pass

        if slot_type or slot_from or not ship_fields.get("SelectedDeliveryType") or is_expired:
            cart = await self.async_select_delivery_slot(
                slot_type=slot_type, slot_from=slot_from
            )

        cart_id = cart["id"]
        amount_cents = (cart.get("totalPrice") or {}).get("centAmount", 0)
        ship_addr = cart.get("shippingAddress") or {}
        addr_ref = ((ship_addr.get("custom") or {}).get("fields") or {}).get(
            "AddressReference", ""
        )
        addr_key = ship_addr.get("key", "")
        if not addr_ref:
            account = await self.async_get_my_account()
            for a in account.get("addresses") or []:
                ref = ((a.get("custom") or {}).get("fields") or {}).get(
                    "AddressReference"
                )
                if ref:
                    addr_ref = ref
                    if not addr_key:
                        addr_key = a.get("key", "")
                    break
        if not addr_ref:
            raise FlaschenpostOrderError(
                "Customer address is missing AddressReference for checkout initialization"
            )

        stock_res = await self._request(
            "POST",
            "/webshop-checkout-api/api/v2/checkout/validatestock",
            params={"cartId": cart_id},
            json_body={},
        )
        if isinstance(stock_res, dict) and stock_res.get("insufficentStockItems"):
            items_desc = ", ".join(
                f"SKU {it.get('sku')} (verfügbar: {it.get('availableQuantity', 0)})"
                for it in stock_res["insufficentStockItems"]
            )
            raise FlaschenpostOrderError(
                f"Artikel im Warenkorb nicht mehr ausreichend verfügbar: {items_desc}. "
                "Bitte passe die Menge im Warenkorb an."
            )

        if payment_method == PAYMENT_CREDIT_CARD:
            # 1. Try PayPal Card Vault (current Flaschenpost default)
            pp_card = None
            try:
                pp_card = await self._request(
                    "GET",
                    f"/webshop-checkout-api/api/v1/payment/paypal-card/init?cart={cart_id}&shopperReference={addr_ref}",
                    extra_headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"},
                )
            except Exception as err:
                _LOGGER.debug("PayPal card init check: %s", err)

            vault_cards = (pp_card.get("vaultCards") or []) if pp_card else []
            if vault_cards:
                payment_id = pp_card["paymentId"]
                chosen_card = None
                if stored_payment_id:
                    chosen_card = next(
                        (c for c in vault_cards if c.get("id") == stored_payment_id),
                        None,
                    )
                if not chosen_card and iban_last4:
                    chosen_card = next(
                        (c for c in vault_cards if str(c.get("last_digits")) == str(iban_last4)),
                        None,
                    )
                if not chosen_card:
                    chosen_card = vault_cards[0]

                card_id = chosen_card["id"]

                # 1. Initialize checkout with payment ID and during marker (HTTP 204)
                await self._request(
                    "POST",
                    "/webshop-checkout-api/api/v1/checkout/init",
                    json_body={
                        "cartId": cart_id,
                        "paymentId": payment_id,
                        "during": "saveCheckout:checkout/init",
                    },
                )

                # 2. Create PayPal Card order
                order_url = f"/webshop-paypalincontext-payment-method-api/api/v1/payment/{payment_id}/order?cardId={card_id}"
                auth_order_res = await self._request("POST", order_url)
                if not isinstance(auth_order_res, dict):
                    raise FlaschenpostOrderError("Failed to create order for saved credit card")

                redirect_url = auth_order_res.get("redirectUrl")
                order_id = auth_order_res.get("id")
                if not order_id:
                    raise FlaschenpostOrderError("Failed to obtain order ID for saved credit card")

                authorize_url = f"/webshop-paypalincontext-payment-method-api/api/v1/payment/{payment_id}/order/{order_id}/authorize"

                # If 3D-Secure 2FA challenge is required by issuing bank
                if redirect_url:
                    event_data = {
                        "url": redirect_url,
                        "order_id": order_id,
                        "payment_id": payment_id,
                        "cart_id": cart_id,
                        "total_eur": round(amount_cents / 100.0, 2),
                    }
                    if on_3ds_required:
                        cb_res = on_3ds_required(event_data)
                        if asyncio.iscoroutine(cb_res):
                            await cb_res

                    # Poll for bank authorization completion while user confirms 3DS in browser/app
                    authorized = False
                    poll_interval = max(0.01, poll_3ds_interval_sec)
                    max_attempts = max(1, int(wait_3ds_timeout_sec / poll_interval))
                    for _ in range(max_attempts):
                        await asyncio.sleep(poll_interval)
                        try:
                            auth_res = await self._request("POST", authorize_url)
                            if isinstance(auth_res, dict) and auth_res.get("state") == "Success":
                                authorized = True
                                break
                        except Exception as poll_err:
                            _LOGGER.debug("Waiting for 3DS confirmation: %s", poll_err)

                    if not authorized:
                        raise FlaschenpostOrderError(
                            f"3D-Secure-Bestätigung ausstehend oder abgelaufen. "
                            f"Bitte bestätige die Zahlung bei deiner Bank: {redirect_url}"
                        )
                else:
                    # 3. Authorize order directly if no 3DS challenge is needed
                    auth_res = await self._request("POST", authorize_url)
                    if isinstance(auth_res, dict) and auth_res.get("state") != "Success":
                        raise FlaschenpostOrderError(
                            f"Credit card authorization failed ({auth_res.get('state', 'Unknown')})"
                        )
            else:
                # 2. Fall back to Adyen Credit Card flow
                cb = str(int(time.time() * 1000))
                adyen_init = await self._request(
                    "GET",
                    f"/webshop-checkout-api/api/v1/payment/adyen/init?amount={amount_cents}&cart={cart_id}&allowedPaymentMethods=scheme&shopperReferenceAdd={addr_ref}&cacheBuster={cb}",
                )
                payment_id = adyen_init.get("paymentId")
                session_resp = adyen_init.get("adyenSessionResponse") or {}
                session_id = session_resp.get("id")
                session_data = session_resp.get("sessionData")
                if not payment_id or not session_id or not session_data:
                    raise FlaschenpostOrderError("Failed to initialize Adyen credit card session")

                setup_url = f"https://checkoutshopper-live.adyen.com/checkoutshopper/v1/sessions/{session_id}/setup?clientKey={ADYEN_CLIENT_KEY}"
                async with self._session.post(setup_url, json={"sessionData": session_data}) as setup_res:
                    setup_body = await setup_res.json()
                    stored_methods = setup_body.get("storedPaymentMethods") or []

                if not stored_methods:
                    raise FlaschenpostOrderError(
                        "No saved credit card found on your Flaschenpost account. "
                        "Please save a credit card during checkout on flaschenpost.de first."
                    )

                chosen_card = None
                if stored_payment_id:
                    chosen_card = next(
                        (c for c in stored_methods if c.get("id") == stored_payment_id),
                        None,
                    )
                if not chosen_card and iban_last4:
                    chosen_card = next(
                        (c for c in stored_methods if str(c.get("lastFour")) == str(iban_last4)),
                        None,
                    )
                if not chosen_card:
                    chosen_card = stored_methods[0]

                pay_url = f"https://checkoutshopper-live.adyen.com/checkoutshopper/v1/sessions/{session_id}/payments?clientKey={ADYEN_CLIENT_KEY}"
                pay_payload = {
                    "sessionData": session_data,
                    "paymentMethod": {
                        "type": "scheme",
                        "storedPaymentMethodId": chosen_card["id"],
                    },
                }
                async with self._session.post(pay_url, json=pay_payload) as pay_res:
                    pay_data = await pay_res.json()

                if pay_data.get("resultCode") != "Authorised":
                    raise FlaschenpostOrderError(
                        f"Credit card authorization failed ({pay_data.get('resultCode', 'Unknown')})"
                    )

                await self._request(
                    "POST",
                    "/webshop-checkout-api/api/v2/checkout/init",
                    json_body={"cartId": cart_id, "paymentId": payment_id},
                )

        elif payment_method == PAYMENT_PAYPAL:
            pay_init = await self._request(
                "GET",
                f"/webshop-checkout-api/api/v1/payment/paypal/init?cart={cart_id}&shopperReferenceAdd={addr_ref}",
                extra_headers={"PAYPAL-CLIENT-METADATA-ID": cart_id},
            )
            payment_id = pay_init.get("paymentId")
            auth_url = pay_init.get("userAuthorizeUrl")
            if not payment_id or not auth_url:
                raise FlaschenpostOrderError("Failed to initialize PayPal payment session")
            if not pay_init.get("baExists"):
                raise FlaschenpostOrderError(
                    "No active PayPal billing agreement found on Flaschenpost profile"
                )

            auth_res = await self._request(
                "POST",
                auth_url,
                json_body={"dummy": "data"},
                extra_headers={"PAYPAL-CLIENT-METADATA-ID": cart_id},
            )
            if isinstance(auth_res, dict) and auth_res.get("authorized") is False:
                raise FlaschenpostOrderError("PayPal payment authorization was rejected")

            await self._request(
                "POST",
                "/webshop-checkout-api/api/v2/checkout/init",
                json_body={"cartId": cart_id, "paymentId": payment_id},
            )

        else:
            allowed = "invoice" if payment_method == PAYMENT_INVOICE else "undefined"
            pay_init = await self._request(
                "GET",
                "/webshop-checkout-api/api/v1/payment/direct/init",
                params={
                    "cart": cart_id,
                    "amount": amount_cents,
                    "shopperReferenceAdd": addr_ref,
                    "allowedPaymentMethods": allowed,
                    "ltkaid": addr_key,
                    "cb": "0",
                },
            )
            payment_id = pay_init.get("paymentId")
            if not payment_id:
                raise FlaschenpostOrderError(
                    "Payment initialization did not return a paymentId"
                )

            await self._request(
                "POST",
                "/webshop-checkout-api/api/v2/checkout/init",
                json_body={"cartId": cart_id, "paymentId": payment_id},
            )

            if payment_method == PAYMENT_SEPA:
                accounts = [
                    a
                    for a in (pay_init.get("directDebitAccounts") or [])
                    if not a.get("isBlackListed") and a.get("ibanMasked")
                ]
                if not accounts:
                    raise FlaschenpostOrderError(
                        "No active saved SEPA Direct Debit account found on Flaschenpost profile"
                    )
                chosen_acc = accounts[0]
                if iban_last4:
                    matched = next(
                        (
                            a
                            for a in accounts
                            if str(a.get("ibanMasked", "")).endswith(str(iban_last4))
                        ),
                        None,
                    )
                    if not matched:
                        raise FlaschenpostOrderError(
                            f"No saved SEPA IBAN ending in '{iban_last4}' found"
                        )
                    chosen_acc = matched

                auth_url = pay_init.get("userAuthorizeUrl")
                session_id = pay_init.get("sessionId")
                if not auth_url or not session_id:
                    raise FlaschenpostOrderError(
                        "SEPA payment init missing userAuthorizeUrl or sessionId"
                    )
                auth_res = await self._request(
                    "POST",
                    auth_url,
                    json_body={
                        "debitAccount": chosen_acc["ibanMasked"],
                        "sessionId": session_id,
                    },
                )
                if not isinstance(auth_res, dict) or not auth_res.get("authorized"):
                    raise FlaschenpostOrderError(
                        f"SEPA Direct Debit authorization rejected: {auth_res}"
                    )

        try:
            result: dict[str, Any] = await self._request(
                "POST",
                "/webshop-checkout-api/api/v1/checkout/complete",
                json_body={"cartId": cart_id},
            )
        except Exception:
            result = await self._request(
                "POST",
                "/webshop-checkout-api/api/v2/checkout/complete",
                json_body={"cartId": cart_id},
            )
        return result.get("order") or result

    async def async_complete_3ds_order(
        self,
        *,
        payment_id: str,
        order_id: str,
        cart_id: str | None = None,
    ) -> dict[str, Any]:
        """Authorize and finalize an order following 3D-Secure bank approval."""
        if not cart_id:
            cart = await self.async_get_cart()
            cart_id = cart["id"]

        authorize_url = f"/webshop-paypalincontext-payment-method-api/api/v1/payment/{payment_id}/order/{order_id}/authorize"
        auth_res = await self._request("POST", authorize_url)
        if isinstance(auth_res, dict) and auth_res.get("state") != "Success":
            raise FlaschenpostOrderError(
                f"Credit card 3DS authorization failed ({auth_res.get('state', 'Unknown')})"
            )

        try:
            result: dict[str, Any] = await self._request(
                "POST",
                "/webshop-checkout-api/api/v1/checkout/complete",
                json_body={"cartId": cart_id},
            )
        except Exception:
            result = await self._request(
                "POST",
                "/webshop-checkout-api/api/v2/checkout/complete",
                json_body={"cartId": cart_id},
            )
        return result.get("order") or result
