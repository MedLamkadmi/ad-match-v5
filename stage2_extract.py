"""Stage 2 — Extraction (spec 4) with real Raddar routing.

Routing (correction):
  - Most banners -> ADF gateway (RADDAR_ADF_KEY + RADDAR_ADF_BASE_URL).
  - Metro -> metrodigital gateway (RADDAR_METRO_KEY + RADDAR_METRO_BASE_URL).
  - Food Basics -> metrodigital gateway (RADDAR_FOODBASICS_KEY + RADDAR_FOODBASICS_BASE_URL).

Discovery (public, no key): raddar.ca retailers + publications, filtered by
postal code / lang / radius from config. Item fetch (keyed): ADF pages API.
HTTP logic reused from the user's proven backup extractor — keys/bases now
come from .env, never hardcoded, never printed/logged.

Completeness check (spec 4.1): Case A vs Case B, warn and continue, never silent.
"""
import gzip
import json
import logging
import re
import time
import urllib.parse
import urllib.request
import zlib
from collections.abc import Callable
from datetime import date, datetime
from zoneinfo import ZoneInfo

import config
from config import (
    MAX_RETRIES,
    RETRY_BACKOFF_SECONDS,
    base_for_gateway,
    gateway_for_store,
    key_for_gateway,
)
from models import ExtractionResult, Flyer, RawItem, StoreWarning, TargetSelection

logger = logging.getLogger(__name__)

FetchFn = Callable[[Flyer], list[RawItem]]
TZ = ZoneInfo("America/Toronto")

COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120",
    "Accept": "application/json, text/plain, */*",
}

# Banner GUIDs + fixed site_ids from the user's proven backup config.
# Missing banners fall back to the discovered bannerId from the API.
STORE_PROFILES: dict[str, dict] = {
    "walmart": {"match": ["walmart"], "banner": "9d14a64b-4846-4b62-b62c-40b35df5648a", "site_id": None},
    "costco": {"match": ["costco"], "banner": "", "site_id": None},
    "metro": {"match": ["metro"], "banner": "62e3eddbffe0e6f10778a56d", "site_id": 135},
    "sobeys": {"match": ["sobeys"], "banner": "9a9935d3-c4e8-441e-816a-7c98d12dab43", "site_id": None},
    "freshco": {"match": ["freshco", "freshco on"], "banner": "73b24611-cb81-4dd1-9d3d-d50b99f0441c", "site_id": None},
    "food basics": {"match": ["food basics", "foodbasics"], "banner": "62015981ed29a2a604a206b4", "site_id": 320},
    "no frills": {"match": ["no frills", "nofrills"], "banner": "e9f987f3-808d-4e8f-81bb-1a395b84c9db", "site_id": None},
    "superstore": {"match": ["superstore", "real canadian superstore", "rcss"], "banner": "", "site_id": None},
}


def _fetch_raw(url: str, headers: dict | None = None, timeout: int = 40) -> bytes:
    hdr = dict(COMMON_HEADERS)
    if headers:
        hdr.update(headers)
    req = urllib.request.Request(url, headers=hdr)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        enc = (resp.headers.get("Content-Encoding") or "").lower()
    if enc == "gzip":
        raw = gzip.decompress(raw)
    elif enc == "deflate":
        try:
            raw = zlib.decompress(raw)
        except Exception:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw


def _fetch_json(url: str, headers: dict | None = None) -> object:
    return json.loads(_fetch_raw(url, headers).decode("utf-8", "replace"))


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def get_retailers() -> list[dict]:
    url = "https://raddar.ca/api/v2/retailers/?lang={}&postal_code={}&radius={}".format(
        urllib.parse.quote(config.RADDAR_LANG or "en"),
        urllib.parse.quote(config.RADDAR_POSTAL_CODE),
        urllib.parse.quote(config.RADDAR_RADIUS_KM or "100"),
    )
    data = _fetch_json(url)
    if isinstance(data, list):
        return data
    return data.get("retailers", [])


def get_publications(store_ids: list) -> list[dict]:
    url = "https://raddar.ca/api/v2/publications/stores?lang={}&stores={}".format(
        urllib.parse.quote(config.RADDAR_LANG or "en"),
        ",".join(str(i) for i in store_ids),
    )
    data = _fetch_json(url)
    if isinstance(data, list):
        return data
    return data.get("publications", [])


def _adf_headers(store_key: str) -> dict:
    gateway = gateway_for_store(store_key)
    key = key_for_gateway(gateway)
    if not key:
        raise RuntimeError(f"Missing API key for gateway={gateway} (store={store_key}). Fill .env.")
    base = base_for_gateway(gateway)
    origin = "https://flyer.foodbasics.ca" if "metrodigital" in base else base
    h: dict = {
        "Ocp-Apim-Subscription-Key": key,
        "x-api-version": "3.0",
        "Origin": origin,
    }
    # Discovered bannerId first (fix 1a), hardcoded fallback second. Never invented.
    banner = _DISCOVERED_BANNERS.get(store_key) or STORE_PROFILES[store_key]["banner"]
    if banner:
        h["Banner"] = banner
    return h


def _adf_base(store_key: str) -> str:
    base = base_for_gateway(gateway_for_store(store_key))
    if not base:
        raise RuntimeError(f"Missing base URL for gateway={gateway_for_store(store_key)}. Fill .env.")
    return base.rstrip("/")


def get_adf_pages(store_key: str, code: str, site_id) -> list[dict]:
    base = _adf_base(store_key)
    # .env base may already end with /api (or /adf/api) — avoid doubling /api/api.
    if base.lower().endswith("/api"):
        url = "{}/pages/{}/{}/en/".format(
            base, urllib.parse.quote(code), site_id
        )
    else:
        url = "{}/api/pages/{}/{}/en/".format(
            base, urllib.parse.quote(code), site_id
        )
    data = _fetch_json(url, headers=_adf_headers(store_key))
    return data if isinstance(data, list) else data.get("pages", [])


def _real_date(v: object) -> str:
    if not v or (isinstance(v, str) and v.startswith("0001-01-01")):
        return ""
    return str(v)


def _price_display(pr: dict) -> str:
    # PRICE AUDIT FIX: never fabricate a price from savings alone. An offer
    # with only SAVE info and no sale/regular/alternate price has NO price
    # (missing -> dropped in ETL), not the save amount as price.
    sale = pr.get("salePrice")
    reg = pr.get("regularPrice")
    alt = pr.get("alternatePrice")
    prefix = pr.get("salePricePrefixEn") or ""
    unit = pr.get("promoUnitEn") or pr.get("priceQuantity") or ""
    parts: list[str] = []
    if sale:
        parts.append(f"{prefix}{sale}" if prefix.endswith("$") or not prefix else f"{prefix} ${sale}")
    elif reg:
        parts.append(f"${reg}")
    if alt:
        parts.append(str(alt) if str(alt).startswith("$") else f"${alt}")
    if unit and parts:
        parts.append(str(unit))
    return " ".join(parts)


def _to_date(s: str) -> date | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except Exception:
        return None


def discover_stores_and_pubs() -> tuple[dict, list[dict]]:
    """Map expected stores to raddar retailer ids + fetch all publications."""
    retailers = get_retailers()
    mapped: dict[str, dict] = {}
    for store in config.ALL_STORES:
        key = store.lower()
        matchers = STORE_PROFILES[key]["match"]
        hit = None
        for ret in retailers:
            hay = _norm(
                str(ret.get("name", ""))
                + " "
                + str(ret.get("bannerName") or "")
                + " "
                + str(ret.get("slug") or "")
            )
            if any(_norm(m) in hay for m in matchers):
                hit = ret
                break
        if hit is None:
            mapped[store] = {"retailer_id": None, "store_ids": [], "banner_id": ""}
            continue
        sids = [s.get("id") for s in (hit.get("stores") or []) if s.get("id") is not None]
        # Fix 1a: discovered bannerId from API (not invented) — used as Banner header,
        # preferred over the hardcoded fallback in STORE_PROFILES.
        mapped[store] = {
            "retailer_id": hit.get("id"),
            "store_ids": sids,
            "banner_id": str(hit.get("bannerId") or ""),
        }
    all_ids = [sid for store, m in mapped.items() for sid in m["store_ids"]
               if store not in config.OUT_OF_REGION]
    pubs = get_publications(all_ids) if all_ids else []
    _DISCOVERED_BANNERS.clear()
    for store, m in mapped.items():
        if m.get("banner_id"):
            _DISCOVERED_BANNERS[store.lower()] = m["banner_id"]
    return mapped, pubs


def pubs_to_flyers(pubs: list[dict], mapped: dict, region: str) -> list[Flyer]:
    inv = {m["retailer_id"]: store for store, m in mapped.items() if m["retailer_id"]}
    flyers: list[Flyer] = []
    for p in pubs:
        store = inv.get(p.get("retailerId"))
        if not store or store in config.OUT_OF_REGION:
            continue
        vf = _to_date(str(p.get("validFrom", "")))
        vt = _to_date(str(p.get("validTo", "")))
        if vf is None or vt is None:
            continue
        flyers.append(
            Flyer(
                flyer_id=str(p.get("id", "")),
                store=store,
                region=region,
                valid_from=vf,
                valid_to=vt,
            )
        )
        # stash raw pub for page fetch
        _PUB_CACHE[str(p.get("id", ""))] = p
    return flyers


_PUB_CACHE: dict[str, dict] = {}

# Discovered bannerIds per store key (populated by discover_stores_and_pubs).
_DISCOVERED_BANNERS: dict[str, str] = {}


def fetch_flyer_items(flyer: Flyer) -> list[RawItem]:
    """Real fetch for one flyer: ADF pages -> RawItem list. No filtering."""
    key = flyer.store.lower()
    pub = _PUB_CACHE.get(flyer.flyer_id, {})
    site_id = STORE_PROFILES[key]["site_id"] or pub.get("siteId")
    code = pub.get("name", "")
    if site_id is None or not code:
        raise RuntimeError(f"No site_id/code for {flyer.store} flyer {flyer.flyer_id}")
    pages = get_adf_pages(key, code, site_id)
    flyer_vf = _real_date(pub.get("validFrom"))
    flyer_vt = _real_date(pub.get("validTo"))
    # Real page numbers: 1-based count of counted flyer pages (payload has
    # 0-based `index` + `countTowardsPageCount`). Non-counted pages get 0.
    page_no_by_pos: list[int] = []
    running = 0
    for pg in pages:
        if pg.get("countTowardsPageCount", True):
            running += 1
            page_no_by_pos.append(running)
        else:
            page_no_by_pos.append(0)
    items: list[RawItem] = []
    for pos, pg in enumerate(pages):
        page = page_no_by_pos[pos]
        for blk in pg.get("blocks") or []:
            for pr in blk.get("products") or []:
                name = pr.get("productEn") or pr.get("productFr") or pr.get("productDescription") or ""
                if not name:
                    continue
                img = (pr.get("productImage") or "") or (
                    (pr.get("productImagePaths") or [None])[0]
                    if pr.get("productImagePaths")
                    else ""
                )
                has_price = bool(pr.get("salePrice") or pr.get("regularPrice") or pr.get("alternatePrice"))
                has_save = bool(pr.get("savingsEn") or pr.get("save"))
                items.append(
                    RawItem(
                        title=name,
                        price=str(pr.get("salePrice") or pr.get("regularPrice") or _price_display(pr) or ""),
                        image_url=img or None,
                        page=page,
                        valid_from=_to_date(_real_date(pr.get("validFrom")) or flyer_vf) or flyer.valid_from,
                        valid_to=_to_date(_real_date(pr.get("validTo")) or flyer_vt) or flyer.valid_to,
                        category_raw=pr.get("mainCategoryEn") or None,
                        store=flyer.store,
                        flyer_id=flyer.flyer_id,
                        save_only=(has_save and not has_price),
                    )
                )
    return items


def _fetch_with_retry(flyer: Flyer, fetch_fn: FetchFn) -> list[RawItem]:
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fetch_fn(flyer)
        except Exception as exc:  # noqa: BLE001 — must survive one bad store
            last_exc = exc
            logger.warning(
                "Fetch failed store=%s flyer=%s attempt=%d/%d: %s",
                flyer.store,
                flyer.flyer_id,
                attempt,
                MAX_RETRIES,
                exc,
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise last_exc  # type: ignore[misc]


def extract_all(
    selections: list[TargetSelection],
    fetch_fn: FetchFn,
    expected_stores: list[str],
) -> ExtractionResult:
    result = ExtractionResult()
    by_store_items: dict[str, list[RawItem]] = {s: [] for s in expected_stores}
    selected_stores = {s.flyer.store for s in selections}
    failed_stores: set[str] = set()

    for sel in selections:
        try:
            items = _fetch_with_retry(sel.flyer, fetch_fn)
            by_store_items.setdefault(sel.flyer.store, []).extend(items)
        except Exception as exc:  # noqa: BLE001 — warn and continue per spec
            failed_stores.add(sel.flyer.store)
            logger.error(
                "Extraction FAILED store=%s flyer=%s after %d retries: %s",
                sel.flyer.store,
                sel.flyer.flyer_id,
                MAX_RETRIES,
                exc,
            )

    for store in expected_stores:
        if store in config.OUT_OF_REGION:
            # Case C — out-of-region: never fetch QC prices into Ottawa comparison
            msg = (
                f"LOUD WARNING: '{store}' EXCLUDED as out-of-region "
                f"({config.OUT_OF_REGION[store]}). No {store} prices in this run."
            )
            logger.warning(msg)
            print(msg)
            result.warnings.append(
                StoreWarning(store=store, case="C-out-of-region", message=msg)
            )
            result.missing_stores.append(store)
            continue
        n = len(by_store_items.get(store, []))
        if n > 0:
            # Store yielded items — keep them. A sibling flyer failure still warns
            # (partial = incomplete) but must not discard the good flyers' items.
            result.extracted_stores.append(store)
            result.items.extend(by_store_items[store])
            if store in failed_stores:
                msg = (
                    f"LOUD WARNING: '{store}' partially extracted — one flyer failed "
                    f"(Case B — partial). Comparison is incomplete for this store."
                )
                logger.warning(msg)
                print(msg)
                result.warnings.append(
                    StoreWarning(store=store, case="B-extraction-failed", message=msg)
                )
            continue
        if store in failed_stores:
            msg = (
                f"LOUD WARNING: '{store}' data MISSING due to extraction failure "
                f"(NOT because the store has no flyer). Comparison is incomplete for this store."
            )
            logger.warning(msg)
            print(msg)
            result.warnings.append(
                StoreWarning(store=store, case="B-extraction-failed", message=msg)
            )
            result.missing_stores.append(store)
            result.items.extend(by_store_items.get(store, []))
        elif store not in selected_stores:
            msg = (
                f"LOUD WARNING: '{store}' has no active flyer this week "
                f"(Case A — OK). Continuing with remaining stores."
            )
            logger.warning(msg)
            print(msg)
            result.warnings.append(
                StoreWarning(store=store, case="A-no-flyer", message=msg)
            )
            result.missing_stores.append(store)
        else:
            msg = (
                f"LOUD WARNING: '{store}' flyer selected but 0 items extracted "
                f"(Case B — extraction missed/empty). Comparison is incomplete for this store."
            )
            logger.warning(msg)
            print(msg)
            result.warnings.append(
                StoreWarning(store=store, case="B-extraction-failed", message=msg)
            )
            result.missing_stores.append(store)

    return result
