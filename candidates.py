"""Layer 1 — rule-based candidate generation (spec 6.2), generous for recall.

Signals (any one can qualify): token overlap OR brand match OR
in-scope-category + loose token match. Up to 25 candidates per list item.

Quantity: weight/volume ±2% (or ±5 base units, larger wins); count/pack exact
when BOTH sides state it explicitly; multipack != single; missing size info
passes (never filter on unknown). Target overrides applied (broccoli-28,
oikos-multi, eggs-18). OR-branches keep their own sizes; output records WHICH
branch matched (spec 6.3).
"""
import json
import os
import re
from dataclasses import dataclass, field

from config import RETRY_TOLERANCE_PCT, TOLERANCE_DEFAULT_PCT, TOLERANCE_MIN_ABS_G, TOLERANCE_MIN_ABS_ML
from models import CleanItem
from normalize import (
    BRAND_MAP,
    brand_from_title,
    normalize_title,
    parse_quantity,
    strip_accents,
)

MAX_CANDIDATES = 25

STOPWORDS = {
    "or", "and", "the", "with", "for", "pack", "pk", "ct", "count", "ea", "each",
    "x", "g", "kg", "lb", "lbs", "oz", "ml", "l", "kg", "un", "en", "de", "du",
    "des", "au", "aux", "a", "an", "of", "in", "on", "per", "par", "12", "24",
    "no", "name",  # brand words, not product words (audit: milk matched butter)
}

# Generic container/descriptor/color tokens carry no product identity.
# Excluded from overlap on BOTH sides (audit: 'bag' matched a milk pitcher).
GENERIC_TOKENS = {
    "bag", "bags", "pack", "packs", "box", "boxes", "bottle", "bottles",
    "jar", "jars", "can", "cans", "tin", "tins", "package", "packages",
    "bundle", "bundles", "container", "containers", "carton", "cartons",
    "case", "cases", "tub", "tubs", "tub", "pouch", "pouches", "roll", "rolls",
    "red", "green", "yellow", "orange", "purple", "black", "blue", "white",
    "assorted", "variety", "varieties", "mixed", "mix", "flavour", "flavours",
    "flavor", "flavors", "choice", "value",
}
UNIT_TOKENS = {"g", "kg", "lb", "lbs", "oz", "ml", "l", "ct", "pk", "ea", "pint", "kg"}

OVERRIDES_PATH = os.path.join("data", "target_overrides.json")
PINT_ML = 473.176


def load_overrides(path: str = OVERRIDES_PATH) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f).get("interpretations", {})


def override_note(spec: "TargetSpec") -> str:
    """Human-readable override for the judge prompt (same info Layer 1 uses)."""
    ov = spec.override or {}
    bits = []
    if ov.get("match_as"):
        ign = ", ".join(f"'{t}'" for t in ov.get("ignore_tokens", []))
        bits.append(f"ignore {ign} — it has no meaning; match as '{ov['match_as']}'")
    if ov.get("quantity_count"):
        bits.append(f"quantity is {ov['quantity_count']} eggs; match {ov.get('match_size', '')} packs".strip())
    if ov.get("accept_total_sizes"):
        bits.append(f"accept {' or '.join(ov['accept_total_sizes'])}")
    if ov.get("match_unit") == "pint":
        bits.append("pint container only, never 6 oz")
    return ("Interpretation: " + "; ".join(bits) + ".") if bits else ""


def meaningful_tokens(text: str) -> set[str]:
    toks = set()
    for w in normalize_title(text).split():
        if not w or w in STOPWORDS or w in UNIT_TOKENS or w in GENERIC_TOKENS:
            continue
        if re.fullmatch(r"\d+(?:[.,]\d+)?", w):
            continue  # bare numbers ignored (spec 6.2)
        if re.fullmatch(r"\d+\s*=\s*\d+", w):
            continue
        toks.add(w)
    return toks


@dataclass
class TargetSpec:
    raw_name: str
    raw_size: str
    branch: str  # which OR-branch this spec is ("" if none)
    tokens: set[str] = field(default_factory=set)
    brand: str = ""
    kind: str = "unknown"  # weight | volume | count | each | unknown
    low: float | None = None  # base units (g / mL / count)
    high: float | None = None
    pack: int = 1
    override: dict | None = None
    pint_item: bool = False  # explicit pint exception (pint-12oz)


def _to_base(val: float, unit: str) -> tuple[float, str] | None:
    unit = unit.lower()
    if unit in ("g",):
        return val, "weight"
    if unit == "kg":
        return val * 1000.0, "weight"
    if unit in ("lb", "lbs"):
        return val * 453.592, "weight"
    if unit == "oz":
        return val * 28.3495, "weight"
    if unit == "ml":
        return val, "volume"
    if unit == "l":
        return val * 1000.0, "volume"
    if unit == "pint":
        return PINT_ML, "volume"
    return None


def parse_target_size(size: str) -> dict:
    """Parse target size text (ranges, multis, counts kept exactly as written)."""
    s = (size or "").strip().lower().replace(",", " ")
    if not s:
        return {"kind": "unknown"}
    # Tissue "12=24": 12 packs = 24 rolls
    m = re.fullmatch(r"(\d+)\s*=\s*(\d+)", s)
    if m:
        return {"kind": "count", "low": float(m.group(1)), "high": float(m.group(1)),
                "pack": int(m.group(1)), "equiv": float(m.group(2))}
    # Multipack "6x710 ml", "12x355 ml"
    m = re.fullmatch(r"(\d+)\s*x\s*(\d+(?:[.,]\d+)?)\s*(g|kg|lb|oz|ml|l)", s)
    if m:
        base = _to_base(float(m.group(2).replace(",", ".")), m.group(3))
        if base:
            return {"kind": base[1], "low": base[0], "high": base[0],
                    "pack": int(m.group(1)), "unit": float(m.group(2).replace(",", "."))}
    # Range "177-255 g", "750 g - 1 kg", "800 - 930 g", "60-208 ct", "3.46-3.90 l"
    m = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*(g|kg|lb|oz|ml|l|ct|count|pk)?\s*-\s*(\d+(?:[.,]\d+)?)\s*(g|kg|lb|oz|ml|l|ct|count|pk)", s)
    if m:
        u1, u2 = (m.group(2) or m.group(4)), m.group(4)
        if u2 in ("ct", "count", "pk"):
            return {"kind": "count", "low": float(m.group(1).replace(",", ".")),
                    "high": float(m.group(3).replace(",", ".")), "pack": -1}
        b1, b2 = _to_base(float(m.group(1).replace(",", ".")), u1), _to_base(float(m.group(3).replace(",", ".")), u2)
        if b1 and b2:
            return {"kind": b1[1], "low": min(b1[0], b2[0]), "high": max(b1[0], b2[0]), "pack": 1}
    # Single "454 g", "4 l", "10kg", "10 lb", "12 ct", "24 pk", "30 pk", "4 ct", "1 ea"
    m = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*(g|kg|lb|oz|ml|l|pint|ct|count|pk|ea|each|pk)?", s)
    if m:
        u = m.group(2) or ""
        if u in ("ct", "count", "pk"):
            return {"kind": "count", "low": float(m.group(1).replace(",", ".")),
                    "high": float(m.group(1).replace(",", ".")), "pack": int(float(m.group(1).replace(",", ".")))}
        if u in ("ea", "each", ""):
            return {"kind": "each", "low": 1.0, "high": 1.0, "pack": 1}
        base = _to_base(float(m.group(1).replace(",", ".")), u)
        if base:
            return {"kind": base[1], "low": base[0], "high": base[0], "pack": 1}
    if "pint" in s:
        return {"kind": "volume", "low": PINT_ML, "high": PINT_ML, "pack": 1}
    if "brick" in s:
        n = re.search(r"(\d+)\s*pk", s)
        return {"kind": "unknown", "pack": int(n.group(1)) if n else 1}
    return {"kind": "unknown"}


def split_or_branches(name: str) -> list[str]:
    parts = re.split(r"\s+or\s+", name, flags=re.IGNORECASE)
    return [p.strip(" ,") for p in parts if p.strip(" ,")]


def expand_targets(rows: list[dict], overrides: dict) -> list[TargetSpec]:
    """One TargetSpec per OR-branch (spec 6.3: match ANY branch, show which)."""
    specs: list[TargetSpec] = []
    for row in rows:
        name = (row.get("name") or "").strip()
        size = (row.get("target_size") or "").strip()
        branches = split_or_branches(name)
        multi = len(branches) > 1
        for b in branches:
            # Branch keeps its own size if stated, else inherits item size
            bq = parse_quantity(b)
            b_has_size = bq["total_size"] is not None or (bq["pack_count"] or 1) > 1
            spec_size = size if not (multi and b_has_size) else ""
            parsed = parse_target_size(spec_size or size)
            ov = None
            for key, o in overrides.items():
                want = (o.get("list_text") or "").strip().lower()
                if want and want in (name.lower(), f"{name} {size}".strip().lower()):
                    ov = o
                    break
            toks = meaningful_tokens(b)
            if ov and "broccoli" in (ov.get("match_as") or "").lower():
                toks = {t for t in toks if t not in set(ov.get("ignore_tokens", []))}
                toks |= meaningful_tokens(ov.get("match_as", ""))
            pint = bool(ov and ov.get("match_unit") == "pint")
            if pint:
                parsed = {"kind": "pint", "low": None, "high": None, "pack": 1}
            specs.append(TargetSpec(
                raw_name=name, raw_size=size, branch=b if multi else "",
                tokens=toks, brand=brand_from_title(b), kind=parsed.get("kind", "unknown"),
                low=parsed.get("low"), high=parsed.get("high"),
                pack=parsed.get("pack", 1), override=ov, pint_item=pint,
            ))
    return specs


def _parse_accept_size(s: str) -> tuple | None:
    """'4x95g' -> (pack 4, unit 95.0, 'g'). None when unparsable."""
    m = re.fullmatch(r"\s*(\d+)\s*x\s*(\d+(?:[.,]\d+)?)\s*(g|kg|lb|oz|ml|l)\s*", (s or "").lower())
    if not m:
        return None
    return int(m.group(1)), float(m.group(2).replace(",", ".")), m.group(3)


def vision_size_ok(spec: TargetSpec, pack: int, unit_size: float | None, unit_type: str | None) -> bool:
    """Strict compare of a vision-filled size against the spec (all kinds).

    Shared by the Stage 4 vision-restore path so overrides, pint, count and
    each all behave exactly like the Layer 1 gate — no special cases rot here.
    """
    pack = int(pack or 1)
    ut = (unit_type or "").strip()
    if spec.override and spec.override.get("match_as"):
        return True
    if spec.pint_item:
        return ut == "pint" and pack == 1
    if spec.override and spec.override.get("quantity_count") == 18:
        return pack == 18
    accepts = [a for a in (_parse_accept_size(x) for x in (spec.override or {}).get("accept_total_sizes", [])) if a]
    if accepts:
        if unit_size is None or ut not in ("g", "kg", "lb", "lbs", "oz", "ml", "l"):
            return False
        base = {"g": 1.0, "kg": 1000.0, "lb": 453.592, "lbs": 453.592, "oz": 28.3495,
                "ml": 1.0, "l": 1000.0}[ut]
        total = pack * float(unit_size) * base
        for apack, asize, aunit in accepts:
            abase = {"g": 1.0, "kg": 1000.0, "lb": 453.592, "lbs": 453.592, "oz": 28.3495,
                     "ml": 1.0, "l": 1000.0}[aunit]
            atotal = apack * asize * abase
            if pack == apack and abs(total - atotal) <= max(atotal * 0.02, 5.0):
                return True
        return False
    if spec.kind == "unknown" or spec.low is None:
        return True
    if spec.kind == "each":
        return pack == 1 and ut in ("", "each", "ea", None)
    if spec.kind == "count":
        if spec.pack == -1:
            return (spec.low or 0) <= pack <= (spec.high or 0)
        return pack == (spec.pack or 1)
    if unit_size is None or ut not in ("g", "kg", "lb", "lbs", "oz", "ml", "l"):
        return False
    base = {"g": 1.0, "kg": 1000.0, "lb": 453.592, "lbs": 453.592, "oz": 28.3495,
            "ml": 1.0, "l": 1000.0}[ut]
    total = pack * float(unit_size) * base
    tol = max((spec.low or 0) * 0.02, 5.0)
    if not ((spec.low or 0) - tol <= total <= (spec.high or 0) + tol):
        return False
    if spec.pack > 1 and pack == 1:
        return False
    if spec.pack == 1 and pack > 1:
        return False
    return True


def _size_status(spec: TargetSpec, item: CleanItem, tol_pct: float) -> str:
    """ok | mismatch | unverified.

    CORRECTED RULE: target size is law. Flyer states a size -> strict compare,
    mismatch rejects. Flyer states NO size -> UNVERIFIED (never EXACT, max
    STRONG + flag). Overrides (broccoli-28, oikos-multi, eggs-18) are explicit
    exceptions ONLY and are never generalized.
    """
    # Explicit exception: Broccoli ignores 28 (no size check at all)
    if spec.override and spec.override.get("match_as"):
        return "ok"
    # Explicit exception: pint item matches pint containers ONLY
    # (pint-to-pint unit matching — no gram conversion, no weight equivalence).
    if spec.pint_item:
        if item.unit_type == "pint":
            return "ok" if (item.pack_count or 1) == 1 else "mismatch"
        if item.total_size is None:
            return "unverified"
        return "mismatch"
    # Separation, both directions: a pint pack never matches a non-pint spec.
    if item.unit_type == "pint" and spec.kind in ("weight", "volume", "count", "each"):
        return "mismatch"
    # Explicit exception: Oikos accepts any size in accept_total_sizes.
    accepts = [a for a in (_parse_accept_size(x) for x in (spec.override or {}).get("accept_total_sizes", [])) if a]
    if accepts:
        if item.total_size is None and (item.pack_count or 1) == 1 and item.unit_type in ("", None):
            return "unverified"
        for apack, asize, aunit in accepts:
            abase = {"g": 1.0, "kg": 1000.0, "lb": 453.592, "lbs": 453.592, "oz": 28.3495,
                     "ml": 1.0, "l": 1000.0}.get(aunit, 1.0)
            atotal = apack * asize * abase
            if (item.pack_count or 1) == apack and item.total_size is not None and abs(item.total_size - atotal) <= max(atotal * tol_pct, 5.0):
                return "ok"
        return "mismatch"
    # Explicit exception: Prestige Eggs = 18 count exact
    if spec.override and spec.override.get("quantity_count") == 18:
        if (item.pack_count or 1) == 18:
            return "ok"
        if item.unit_type == "count" or (item.pack_count or 1) > 1:
            return "mismatch"
        return "unverified"
    if spec.kind == "unknown" or spec.low is None:
        return "ok"  # target itself states no usable size (incl. overrides)
    if spec.kind == "each":
        if item.total_size is not None:
            return "mismatch"  # weighed offer vs each target
        if item.unit_type == "count" and (item.pack_count or 1) != 1:
            return "mismatch"
        if item.unit_type in ("", None) and (item.pack_count or 1) == 1:
            return "unverified"
        return "ok"
    if spec.kind == "count":
        if item.unit_type == "count" or (item.pack_count or 1) > 1:
            if spec.pack == -1:  # target range e.g. 60-208 ct
                if (spec.low or 0) <= (item.pack_count or 0) <= (spec.high or 0):
                    return "ok"
                return "mismatch"
            return "ok" if (item.pack_count or 1) == (spec.pack or 1) else "mismatch"
        return "unverified"  # candidate states no count
    # weight / volume: candidate total within [low, high] expanded by tolerance
    if item.total_size is None:
        return "unverified"  # candidate states no size
    tol = max((spec.low or 0) * tol_pct, TOLERANCE_MIN_ABS_G if spec.kind == "weight" else TOLERANCE_MIN_ABS_ML)
    if not ((spec.low or 0) - tol <= (item.total_size or 0) <= (spec.high or 0) + tol):
        return "mismatch"
    # Multipack != single (both sides explicit)
    if spec.pack > 1 and (item.pack_count or 1) == 1:
        return "mismatch"
    if spec.pack == 1 and (item.pack_count or 1) > 1 and item.unit_type in ("g", "kg", "lb", "oz", "ml", "l"):
        return "mismatch"
    return "ok"


def _size_compatible(spec: TargetSpec, item: CleanItem, tol_pct: float) -> bool:
    return _size_status(spec, item, tol_pct) != "mismatch"


@dataclass
class Candidate:
    item: CleanItem
    score: float
    signals: list[str]
    spec: TargetSpec
    size_verified: bool = True  # False = flyer states no size (max STRONG)


def _active_forbids() -> dict:
    """Live forbid_pair rules from memory: {parsed list name -> [tokens]}."""
    global _FORBID_CACHE
    if _FORBID_CACHE is None:
        _FORBID_CACHE = {}
        try:
            from rules_table import load_rules

            for m in load_rules().matching:
                if getattr(m, "status", "") == "live" and getattr(m, "kind", "") == "forbid_pair" and m.list_item:
                    _FORBID_CACHE[m.list_item.strip().lower()] = [t.lower() for t in (m.tokens or [])]
        except Exception:
            pass
    return _FORBID_CACHE


_FORBID_CACHE = None
_EQUIV_CACHE = None


def _brand_equiv() -> set:
    """Live brand_equiv pairs from Stage 6 memory (both directions)."""
    global _EQUIV_CACHE
    if _EQUIV_CACHE is None:
        _EQUIV_CACHE = set()
        try:
            from rules_table import load_rules

            for m in load_rules().matching:
                if getattr(m, "status", "") == "live" and getattr(m, "kind", "") == "brand_equiv" and m.brand_a and m.brand_b:
                    a, b = m.brand_a.strip().lower(), m.brand_b.strip().lower()
                    _EQUIV_CACHE.add((a, b))
                    _EQUIV_CACHE.add((b, a))
        except Exception:
            pass
    return _EQUIV_CACHE


def _score(spec: TargetSpec, item: CleanItem, tol_pct: float) -> tuple[float, list[str]] | None:
    # Live forbid_pair rules from Stage 6 memory (never auto-promoted pairs)
    forb = _active_forbids().get((spec.raw_name or "").strip().lower(), [])
    if forb:
        itoks0 = meaningful_tokens(item.normalized_title or item.title)
        if any(t in itoks0 for t in forb):
            return None
    itoks = meaningful_tokens(item.normalized_title or item.title)
    inter = spec.tokens & itoks
    token_score = len(inter) / max(1, len(spec.tokens))
    signals: list[str] = []
    brand_hit = bool(spec.brand) and spec.brand != "" and (
        item.brand == spec.brand or (spec.brand, item.brand) in _brand_equiv())
    # Brand alone never qualifies (audit: milk matched butter on brand only).
    # Non-brand product tokens must also overlap.
    brand_words = set(spec.brand.split()) if spec.brand else set()
    product_inter = {t for t in inter if t not in brand_words}
    if len(inter) >= 2 and len(product_inter) >= 1:
        signals.append(f"tokens:{sorted(inter)[:4]}")
    elif brand_hit and len(product_inter) >= 1:
        signals.append(f"tokens:{sorted(inter)[:4]}")
    elif len(spec.tokens) <= 1 and len(inter) >= 1:
        signals.append(f"tokens:{sorted(inter)[:4]}")
    if brand_hit and signals:
        signals.append(f"brand:{spec.brand}")
    cat_ok = (item.category or "").strip() != ""
    if signals and cat_ok and len(inter) >= 1:
        signals.append("cat+price-band")
    if not signals:
        return None
    if not _size_compatible(spec, item, tol_pct):
        return None
    score = 0.5 * min(1.0, token_score * 1.5) + 0.3 * (1.0 if brand_hit else 0.0) + 0.2 * (1.0 if cat_ok else 0.0)
    return score, signals


def candidates_for_spec(spec: TargetSpec, pool: list[CleanItem], tol_pct: float = TOLERANCE_DEFAULT_PCT) -> list[Candidate]:
    scored: list[Candidate] = []
    for item in pool:
        if item.price is None:
            continue
        r = _score(spec, item, tol_pct)
        if r:
            status = _size_status(spec, item, tol_pct)
            scored.append(Candidate(item=item, score=round(r[0], 3), signals=r[1], spec=spec,
                                    size_verified=(status == "ok")))
    scored.sort(key=lambda c: (-c.score, c.item.price or 0))
    return scored[:MAX_CANDIDATES]
