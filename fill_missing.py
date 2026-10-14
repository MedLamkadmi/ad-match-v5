"""Fill incomplete items with Gemini 2.5 Flash from image VIA LiteLLM.

LiteLLM proxy is OpenAI-compatible: LITELLM_BASE_URL + LITELLM_API_KEY +
LITELLM_MODEL from .env. NEVER direct Gemini API, NEVER hardcoded keys.

Rules (spec 5.4): missing title -> read from image. Missing category ->
assign from image, NEVER drop for missing category. Missing image -> NOT
dropped (match on title/price). Missing price after fill -> drop. Every
filled field is marked 'based on image'.
"""
import json
import logging
import time

import config
from models import CleanItem

logger = logging.getLogger(__name__)

FILL_PROMPT = (    "You read grocery flyer product images. Given the product image, return JSON "
    '{"title": "<brand + product + size from visible text>", '
    '"category": "<one of: Dairy & Eggs, Meat & Poultry, Fish & Seafood, '
    "Produce, Bakery, Pantry, Frozen Foods, Beverages, Snacks, Household, "
    'Pets, Other>}. If the image shows no readable product, return {"title": null, '
    '"category": "Other"}. Reply with JSON only.'
)


def _client():
    from openai import OpenAI

    if not config.LITELLM_API_KEY or not config.LITELLM_BASE_URL:
        raise RuntimeError("LiteLLM not configured (LITELLM_API_KEY/BASE_URL). Fill .env.")
    return OpenAI(base_url=config.LITELLM_BASE_URL, api_key=config.LITELLM_API_KEY)


def _fill_one(client, item: CleanItem, need_title: bool, need_category: bool) -> None:
    if not item.image_url:
        return  # no image -> keep, never drop (spec)
    content: list[dict] = [{"type": "text", "text": FILL_PROMPT}]
    content.append({"type": "image_url", "image_url": {"url": item.image_url}})
    last: Exception | None = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            resp = client.chat.completions.create(
                model=config.LITELLM_MODEL,
                messages=[{"role": "user", "content": content}],
                max_tokens=200,
                timeout=60,
            )
            text = (resp.choices[0].message.content or "").strip()
            start, end = text.find("{"), text.rfind("}")
            data = json.loads(text[start : end + 1] if start >= 0 else text)
            if need_title and data.get("title"):
                item.title = str(data["title"])
                if "title" not in item.based_on_image:
                    item.based_on_image.append("title")
            if need_category and data.get("category"):
                item.category = str(data["category"])
                if "category" not in item.based_on_image:
                    item.based_on_image.append("category")
            return
        except Exception as exc:  # noqa: BLE001 — one bad image must not kill the run
            last = exc
            logger.warning("Gemini fill failed %s attempt %d: %s", item.store, attempt, exc)
            time.sleep(config.RETRY_BACKOFF_SECONDS * attempt)
    logger.error("Gemini fill gave up %s %r: %s", item.store, item.title, last)


def fill_items(items: list[CleanItem]) -> dict:
    """Fill all items missing title and/or category (with image). Returns counts."""
    stats = {"title_filled": 0, "category_filled": 0, "attempted": 0, "skipped_no_image": 0}
    needy = [
        it
        for it in items
        if (not (it.title or "").strip() or not (it.category or "").strip())
        and it.price is not None
    ]
    if not needy:
        return stats
    client = _client()
    for it in needy:
        need_t = not (it.title or "").strip()
        need_c = not (it.category or "").strip()
        if not it.image_url:
            stats["skipped_no_image"] += 1
            continue  # never drop — match on title/price
        stats["attempted"] += 1
        before_t, before_c = it.title, it.category
        _fill_one(client, it, need_t, need_c)
        if need_t and (it.title or "").strip() and it.title != before_t:
            stats["title_filled"] += 1
        if need_c and (it.category or "").strip() and it.category != before_c:
            stats["category_filled"] += 1
    return stats


SIZE_PROMPT = (
    "You read grocery flyer product images. What pack size/weight/volume does "
    "the offer show? Reply JSON only: "
    '{"pack_count": <int>, "unit_size": <number|null>, '
    '"unit_type": "<g|kg|lb|oz|ml|L|pint|count|each|null>"} '
    'Examples: a 4x100g multipack -> {"pack_count": 4, "unit_size": 100, "unit_type": "g"}. '
    'A single 454g pack -> {"pack_count": 1, "unit_size": 454, "unit_type": "g"}. '
    'A pint container -> {"pack_count": 1, "unit_size": 1, "unit_type": "pint"}. '
    'A 12-count egg pack -> {"pack_count": 12, "unit_size": null, "unit_type": "count"}. '
    'A single item sold each -> {"pack_count": 1, "unit_size": null, "unit_type": "each"}. '
    "If no size is visible, reply "
    '{"pack_count": 1, "unit_size": null, "unit_type": null}.'
)


def fill_size_from_image(image_url: str) -> dict | None:
    """Try to read pack size from the flyer image (Stage 4 size rule).

    Caller must check GEMINI_ENABLED first — never called in TEST MODE
    (zero-calls rule). Returns pack dict or None when unreadable/failed.
    """
    from openai import OpenAI

    client = OpenAI(base_url=config.LITELLM_BASE_URL, api_key=config.LITELLM_API_KEY)
    last: Exception | None = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            resp = client.chat.completions.create(
                model=config.LITELLM_MODEL,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": SIZE_PROMPT},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ]}],
                max_tokens=150,
                timeout=60,
            )
            text = (resp.choices[0].message.content or "").strip()
            s, e = text.find("{"), text.rfind("}")
            return json.loads(text[s : e + 1] if s >= 0 else text)
        except Exception as exc:  # noqa: BLE001
            last = exc
            logger.warning("Size fill failed attempt %d: %s", attempt, exc)
            time.sleep(config.RETRY_BACKOFF_SECONDS * attempt)
    logger.error("Size fill gave up: %s", last)
    return None
