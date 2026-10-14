"""Stage 3 — ETL (spec 5). Raw table -> ONE clean table.

Order (spec): category filter FIRST on complete items -> split complete /
incomplete -> clean complete (dedupe AFTER normalization, normalize,
category re-check) -> Gemini fill incomplete -> re-check filled -> merge.

Complete = has title + price + image. Incomplete = missing >= 1 (spec 5.2).
NEVER drop for missing category; NEVER drop no-image items; missing price
after fill -> drop. Page: 1-based counted pages from the API payload
(index + countTowardsPageCount); note exposed for the Warnings sheet.
"""
import csv
import logging
from dataclasses import dataclass, field
from datetime import date

from models import CleanItem, RawItem
from normalize import (
    brand_from_title,
    detect_language,
    fix_mojibake,
    normalize_brand,
    normalize_title,
    parse_price,
    parse_quantity,
)

logger = logging.getLogger(__name__)

# Category filter: the real 69-item list spans grocery + household + baby
# (Tide, Gain, Purex, Bounty, Cashmere, Royale, Ziploc, Huggies), so those
# stay IN scope. Only clear non-grocery is dropped. "Other" is KEPT
# (ambiguous — could be miscategorized grocery).
OUT_OF_SCOPE = {
    "electronics",
    "home electronics",
    "clothing",
    "apparel",
    "pets",
    "pet supplies",
    "toys",
    "games",
    "furniture",
    "automotive",
    "office supplies",
    "health & beauty",
    "pharmacy",
}

# Flavor variants only make sense on food. Gating stops 'light' on toys,
# 'fresh' as flavor on non-food, etc. Unknown (None) categories still scan —
# the matcher needs the signal and the reviewer sees the flag.
FOOD_CATEGORIES = {
    "dairy & eggs", "dairy", "dairy and cheese", "meat & poultry", "meat",
    "meat and deli", "deli", "fish & seafood", "fish and seafood", "seafood",
    "produce", "fruit and vegetables", "fruits & vegetables", "fruits",
    "vegetables", "bakery", "bread & bakery", "bread and bakery products",
    "pantry", "dry goods & pasta", "frozen foods", "frozen food", "frozen",
    "beverages", "snacks", "snacks & candy", "candy", "condiments & sauces",
    "condiments", "breakfast", "cereal", "canned goods", "grocery",
    "international foods", "prepared meals", "vegan & vegetarian food",
    "beer & wine",
}

PAGE_NOTE = (
    "Page numbers are 1-based counted flyer pages from the Raddar payload "
    "(index + countTowardsPageCount). Cover/non-counted pages are excluded."
)

VARIANT_KEYWORDS = [
    "salted", "unsalted", "light", "2%", "1%", "skim", "whole", "white",
    "whole wheat", "large", "medium", "small", "frozen", "fresh",
]


@dataclass
class ETLStats:
    items_in: int = 0
    complete_n: int = 0
    incomplete_n: int = 0
    dropped_category_first: int = 0
    dropped_category_recheck: int = 0
    deduped: int = 0
    fill_stats: dict = field(default_factory=dict)
    enriched_complete: int = 0
    save_ghosts: int = 0  # raw SAVE-only offers (savings, no price) — live count
    save_ghosts_cat: int = 0  # ... of which excluded by the category filter
    save_ghosts_noprice: int = 0  # ... of which dropped for missing price
    dropped_no_price: int = 0
    final_n: int = 0


def load_target_list(path: str = "data/target_list.csv") -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def in_scope(category: str | None) -> bool:
    if not (category or "").strip():
        return True  # unknown -> never drop on category (spec)
    return category.strip().lower() not in OUT_OF_SCOPE


def _is_complete(r: RawItem) -> bool:
    return bool((r.title or "").strip()) and r.price is not None and str(r.price).strip() != "" and bool((r.image_url or "").strip())


def to_clean(r: RawItem) -> CleanItem:
    from normalize import strip_symbols

    title = strip_symbols(fix_mojibake((r.title or "").strip()))
    norm = normalize_title(title)
    qty = parse_quantity(title)
    price, flags = parse_price(r.price)
    # price_per_unit: Stage 3 (here). Price per 100 base units (g or mL)
    # for sized items; None when no size or no price.
    ppu = None
    if price is not None and qty["total_size"]:
        ppu = round(price / qty["total_size"] * 100, 4)
    category = (r.category_raw or "").strip() or None
    variant = ""
    if category is None or category.lower() in FOOD_CATEGORIES:
        variant = next((w for w in VARIANT_KEYWORDS if w in norm), "")
    prod = norm
    prod = __import__("re").sub(r"\d+\s*x\s*\d+(?:[.,]\d+)?\s*(?:g|kg|lb|oz|ml|l)\b", "", prod)
    prod = __import__("re").sub(r"\d+(?:[.,]\d+)?\s*(?:g|kg|lb|oz|ml|l|count|ct|pack)\b", "", prod).strip()
    return CleanItem(
        title=title,
        normalized_title=norm,
        brand=brand_from_title(title),
        product_type=prod,
        flavor_variant=variant,
        pack_count=qty["pack_count"],
        unit_size=qty["unit_size"],
        unit_type=qty["unit_type"],
        total_size=qty["total_size"],
        language=detect_language(title),
        price=price,
        price_flags=flags,
        price_per_unit=ppu,
        image_url=r.image_url,
        page=r.page,
        valid_from=r.valid_from,
        valid_to=r.valid_to,
        store=r.store,
        flyer_id=r.flyer_id,
        category=(r.category_raw or "").strip() or None,
    )


def run_etl(raw_items: list[RawItem], fill: bool = True) -> tuple[list[CleanItem], ETLStats]:
    stats = ETLStats(items_in=len(raw_items))
    stats.save_ghosts = sum(1 for r in raw_items if r.save_only)
    complete = [r for r in raw_items if _is_complete(r)]
    incomplete_raw = [r for r in raw_items if not _is_complete(r)]
    stats.complete_n, stats.incomplete_n = len(complete), len(incomplete_raw)

    # Filter FIRST on complete items (spec 5.1)
    kept = [r for r in complete if in_scope(r.category_raw)]
    stats.dropped_category_first = len(complete) - len(kept)

    # Normalize, then dedupe AFTER normalization (spec 5.3)
    cleaned = [to_clean(r) for r in kept]
    seen: dict[tuple, CleanItem] = {}
    for c in cleaned:
        key = (c.store, c.normalized_title, round(c.price, 2) if c.price is not None else None)
        if key not in seen:
            seen[key] = c
    deduped = list(seen.values())
    stats.deduped = len(cleaned) - len(deduped)

    # Category re-check
    final_complete = [c for c in deduped if in_scope(c.category)]
    stats.dropped_category_recheck = len(deduped) - len(final_complete)

    # Queued fix: complete-but-uncategorized rows must NOT bypass enrichment.
    # They keep their place (never dropped for missing category) but go through
    # category-only Gemini fill; a newly assigned out-of-scope category drops them.
    stats.enriched_complete = 0
    to_enrich = [c for c in final_complete if not (c.category or "").strip()]
    if fill and to_enrich:
        from fill_missing import fill_items

        fstats = fill_items(to_enrich)
        stats.enriched_complete = fstats.get("category_filled", 0)
        stats.fill_stats = {
            k: stats.fill_stats.get(k, 0) + fstats.get(k, 0) for k in set(stats.fill_stats) | set(fstats)
        } if stats.fill_stats else dict(fstats)
        kept_enriched = [c for c in final_complete if in_scope(c.category)]
        stats.dropped_category_recheck += len(final_complete) - len(kept_enriched)
        final_complete = kept_enriched

    # Incomplete path: normalize -> Gemini fill -> re-check -> drop if no price
    incomplete = [to_clean(r) for r in incomplete_raw]
    if fill and incomplete:
        from fill_missing import fill_items

        fstats2 = fill_items(incomplete)
        stats.fill_stats = {
            k: stats.fill_stats.get(k, 0) + fstats2.get(k, 0)
            for k in set(stats.fill_stats) | set(fstats2)
        }
        # re-derive normalized fields for filled titles
        for c in incomplete:
            if "title" in c.based_on_image:
                c.normalized_title = normalize_title(c.title)
                c.language = detect_language(c.title)
                q = parse_quantity(c.title)
                c.pack_count, c.unit_size, c.unit_type, c.total_size = (
                    q["pack_count"], q["unit_size"], q["unit_type"], q["total_size"],
                )
    filled = [c for c in incomplete if in_scope(c.category)]
    kept_after_cat = len(filled)
    stats.dropped_category_recheck += len(incomplete) - kept_after_cat
    stats.save_ghosts_cat = sum(1 for r in incomplete_raw if r.save_only and not in_scope((r.category_raw or "").strip() or None))
    filled = [c for c in filled if c.price is not None]
    # Exclusive bucket: only items that survived the category check can be
    # counted as price drops (no double-counting).
    stats.dropped_no_price = kept_after_cat - len(filled)
    stats.save_ghosts_noprice = stats.save_ghosts - stats.save_ghosts_cat

    merged = final_complete + filled
    stats.final_n = len(merged)
    return merged, stats
