"""Normalization — structured fields (spec 5.7 + decided price/mojibake rules)."""
import re
import unicodedata

# --- Brand map / private-label equivalence (spec: Selection = No Name) ---
# "Butcher's Selection" is a NATIONAL brand (Erie Meats) — NOT the private
# label. Standalone "selection" = No Name; "butcher's selection" keeps its own
# brand (R13 fix). Verified against foodbasics.ca + eriemeats.com.
BRAND_MAP = {
    "selection": "no name",
    "compliments": "compliments",
    "sensations": "compliments",
    "no name": "no name",
    "sans nom": "no name",
    "pc": "pc",
    "president's choice": "pc",
    "le choix du president": "pc",
    "blue menu": "blue menu",
    "great value": "great value",
    "irresistibles": "irresistibles",
    "metro": "metro",
    "butcher's selection": "butcher's selection",
}

# Major grocery brands, longest-first, word-boundary matched. No first-token
# fallback: unknown brand stays "" rather than a wrong guess.
BRAND_TOKENS = [
    "butcher's selection",
    "president's choice",
    "le choix du president",
    "black diamond",
    "cracker barrel",
    "great value",
    "red rose",
    "maxwell house",
    "green giant",
    "clover leaf",
    "old dutch",
    "miss vickie's",
    "orville redenbacher",
    "dr. oetker",
    "lean cuisine",
    "hungry-man",
    "boston pizza",
    "maple leaf",
    "schneiders",
    "piller's",
    "marc angelo",
    "la petite bretonne",
    "chapman's",
    "haagen-dazs",
    "ben & jerry's",
    "earth's own",
    "almond breeze",
    "minute maid",
    "canada dry",
    "dr pepper",
    "coca-cola",
    "kellogg's",
    "general mills",
    "hellmann's",
    "french's",
    "bull's-eye",
    "club house",
    "robin hood",
    "five roses",
    "stouffer's",
    "dempster's",
    "silver hills",
    "country harvest",
    "promise",
    "ozery",
    "pita break",
    "ace bakery",
    "villaggio",
    "compliments",
    "sensations",
    "selection",
    "irresistibles",
    "sans nom",
    "no name",
    "blue menu",
    "nestle",
    "kellogg",
    "quaker",
    "post",
    "kraft",
    "heinz",
    "danone",
    "activia",
    "oikos",
    "yoplait",
    "astro",
    "iogo",
    "natrel",
    "sealtest",
    "beatrice",
    "lactantia",
    "neilson",
    "gay lea",
    "armstrong",
    "saputo",
    "bothwell",
    "oka",
    "leclerc",
    "vachon",
    "hostess",
    "dare",
    "lays",
    "lay's",
    "doritos",
    "ruffles",
    "cheetos",
    "fritos",
    "tostitos",
    "smartfood",
    "ruffles",
    "pringles",
    "pepsi",
    "coke",
    "sprite",
    "fanta",
    "crush",
    "schweppes",
    "gatorade",
    "powerade",
    "tropicana",
    "oasis",
    "allen's",
    "ocean spray",
    "sunkist",
    "dole",
    "del monte",
    "driscoll",
    "campbell's",
    "habitant",
    "aylmer",
    "primo",
    "unico",
    "aurora",
    "mastro",
    "san daniele",
    "grimm's",
    "freybe",
    "lyoner",
    "olymel",
    "lafleur",
    "butterball",
    "janes",
    "pillsbury",
    "betty crocker",
    "lipton",
    "knorr",
    "tetley",
    "nabob",
    "folgers",
    "nescafe",
    "starbucks",
    "tim hortons",
    "catelli",
    "olivieri",
    "barilla",
    "mccain",
    "cavendish",
    "breyers",
    "parlour",
    "pampers",
    "huggies",
    "pull-ups",
    "tide",
    "downy",
    "bounce",
    "gain",
    "arm & hammer",
    "cascade",
    "finish",
    "sunlight",
    "palmolive",
    "dawn",
    "lysol",
    "clorox",
    "febreze",
    "glade",
    "bounty",
    "charmin",
    "scott",
    "kleenex",
    "colgate",
    "crest",
    "gillette",
    "old spice",
    "dove",
    "olay",
    "pantene",
    "tresemme",
    "head & shoulders",
    "kraft dinner",
    "mr. noodle",
    "divya",
    "heluva",
    "vh",
    "diana",
    "hp",
    "whiskas",
    "temptations",
    "friskies",
    "fancy feast",
    "pedigree",
    "cesar",
    "pc",
]

# --- Unit conversion to base units (grams / mL) ---
G_PER_UNIT = {"g": 1.0, "kg": 1000.0, "lb": 453.592, "lbs": 453.592, "oz": 28.3495}
ML_PER_UNIT = {"ml": 1.0, "l": 1000.0}
COUNT_UNITS = {"count", "ct", "pack", "pk", "ea", "each", "un", "unite", "unites"}


def fix_mojibake(text: str) -> str:
    """Attempt UTF-8 repair (I�GO stays if � remains — info is lost, keep as-is)."""
    if not text or "\ufffd" in text:
        return text
    try:
        fixed = text.encode("latin1").decode("utf-8")
        # Accept only if it removes high-byte garbage without introducing �
        if "\ufffd" not in fixed and fixed != text:
            # sanity: fixed must still be mostly printable
            if sum(ch.isprintable() for ch in fixed) >= len(fixed) * 0.9:
                return fixed
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return text


def strip_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    )


def strip_symbols(text: str) -> str:
    """Strip ®/™/© (and similar) — normalization, not content."""
    return re.sub(r"[®™©℠]", "", text or "")


def normalize_title(title: str) -> str:
    """lowercase, accents stripped, symbols stripped, whitespace collapsed."""
    title = strip_symbols(fix_mojibake(title or ""))
    title = strip_accents(title).lower()
    title = re.sub(r"\s+", " ", title).strip()
    return title


def normalize_brand(brand: str) -> str:
    b = strip_accents((brand or "").lower()).strip()
    return BRAND_MAP.get(b, b)


def brand_from_title(title: str) -> str:
    """Detect brand inside the title text (flyer API has no brand field).

    Rule (R13): standalone "selection" = No Name private label, but
    "butcher's selection" is its own national brand. List is longest-first
    so the two-word brand wins. Word-boundary only; unknown stays "".
    """
    t = strip_accents((title or "").lower()).replace("’", "'")
    for tok in BRAND_TOKENS:
        if re.search(r"\b" + re.escape(tok) + r"\b", t):
            return BRAND_MAP.get(tok, tok)
    return ""


def parse_quantity(title: str) -> dict:
    """pack_count / unit_size / unit_type / total_size from title text."""
    t = (title or "").lower().replace(",", " ")
    pack_count = 1
    unit_size: float | None = None
    unit_type = ""

    # Multipack: "4 x 100 g", "4x100g", "2 x 200 ml"
    m = re.search(r"(\d+)\s*x\s*(\d+(?:[.,]\d+)?)\s*(g|kg|lb|lbs|oz|ml|l)\b", t)
    if m:
        pack_count = int(m.group(1))
        unit_size = float(m.group(2).replace(",", "."))
        unit_type = m.group(3)
    else:
        # Single: "454 g", "4 l", "12 count"
        m2 = re.search(r"(\d+(?:[.,]\d+)?)\s*(g|kg|lb|lbs|oz|ml|l|count|ct|pack|pk|ea|each|un)\b", t)
        if m2:
            unit_size = float(m2.group(1).replace(",", "."))
            unit_type = m2.group(2)
            if unit_type in COUNT_UNITS:
                pack_count = int(unit_size)
                unit_size = None
                unit_type = "count"
        elif re.search(r"\bpints?\b", t):
            # Pint container ("Blueberries Pint"): counted in pints, NOT fluid mL
            n = re.search(r"(\d+)\s*pints?\b", t)
            if n:
                pack_count = int(n.group(1))
            unit_size, unit_type = 1.0, "pint"

    total_size = None
    if unit_size is not None and unit_type in G_PER_UNIT:
        total_size = pack_count * unit_size * G_PER_UNIT[unit_type]  # grams
    elif unit_size is not None and unit_type in ML_PER_UNIT:
        total_size = pack_count * unit_size * ML_PER_UNIT[unit_type]  # mL
    elif unit_type == "pint":
        total_size = float(pack_count)  # pints (container count, not fluid mL)

    return {
        "pack_count": pack_count,
        "unit_size": unit_size,
        "unit_type": unit_type,
        "total_size": total_size,
    }


def _to_float(num: str) -> float | None:
    try:
        return float(num.replace(",", ""))
    except ValueError:
        return None


def parse_price(raw: object) -> tuple[float | None, list[str]]:
    """Returns (unit_price, flags). None price = missing -> drop after fill.

    Rules: 2/$5 -> unit + multi-buy. BOGO -> half per 2 + flag. Ranges ->
    LOW end + 'price range'. SAVE strings -> numeric only. 'limit N' ignored.
    'per lb' -> price kept, unit noted. No numeric -> (None, []).
    """
    flags: list[str] = []
    if raw is None:
        return None, flags
    text = str(raw).strip()
    if not text:
        return None, flags
    low = text.lower()

    # "limit 4" -> ignore the limit, price stands
    low = re.sub(r"limit\s*\d+", " ", low).strip()

    # PRICE AUDIT FIX: SAVE-only text (no sale/regular price, e.g. "SAVE 0.50")
    # is NOT a price. Strip save-clauses; if nothing price-like remains -> None.
    low_nosave = re.sub(r"save\s*\$?\s*\d+(?:[.,]\d+)?\$?", " ", low).strip()
    if "save" in low and not re.search(r"\$", low_nosave):
        return None, ["save-only-no-price"]

    if "bogo" in low or "buy 1 get 1" in low or "buy one get one" in low:
        nums = re.findall(r"\d+(?:[.,]\d+)?", low)
        if nums:
            base = _to_float(nums[0])
            if base is not None:
                return round(base / 2, 4), ["bogo"]
        return None, ["bogo"]

    # "2/$5", "2 / $5", "2 for $5", "2 for 5.00"
    m = re.search(r"(\d+)\s*(?:/|for)\s*\$?\s*(\d+(?:[.,]\d+)?)", low)
    if m:
        qty = int(m.group(1))
        total = _to_float(m.group(2))
        if total is not None and qty > 0:
            return round(total / qty, 4), ["multi-buy"]

    # Range "4.00 - 15.00" -> LOW end + flag
    nums = re.findall(r"\$?\s*(\d+(?:[.,]\d+)?)", low)
    if "-" in low and len(nums) >= 2:
        first = _to_float(nums[0])
        if first is not None:
            return first, ["price range"]

    if "per lb" in low or "/lb" in low:
        flags.append("per-lb")
    if "save" in low:
        flags.append("save-noted")

    if nums:
        val = _to_float(nums[0])
        return (val, flags) if val is not None else (None, flags)
    return None, flags


def detect_language(title: str) -> str:
    t = (title or "").lower()
    fr_markers = ["beurre", "lait", "sans", "avec", "saveur", "paquet", "pièce", "doux", "demi"]
    if any(w in t for w in fr_markers):
        return "fr"
    return "en"
