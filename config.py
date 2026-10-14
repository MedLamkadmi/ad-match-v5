"""Stage 1 + Stage 2 config — correction applied.

- NO single RADDAR_API_KEY. Three gateways: ADF / METRO / FOODBASICS.
- NO direct GEMINI_API_KEY. Gemini goes through LiteLLM.
- REGION = "ON".
- Secrets ONLY from local .env. Never print or log any key (only presence booleans).
"""
import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# --- Stores (spec 3.1: 9 approved banners) ---
OUR_BANNER = "Superstore"

COMPETITORS = [
    "Walmart",
    "No Frills",
    "Sobeys",
    "Metro",
    "FreshCo",
    "Food Basics",
    "Costco",
]

ALL_STORES = [OUR_BANNER] + COMPETITORS

# --- Region (correction: ON) ---
REGION = _env("REGION", "ON")

# --- Matching thresholds (decision 5, 6) ---
CONFIDENCE_THRESHOLD = 0.70  # below triggers Layer 3 retry
TOLERANCE_DEFAULT_PCT = 0.02  # +/-2% starting default
RETRY_TOLERANCE_PCT = 0.10  # widened to 10% on retry
TOLERANCE_MIN_ABS_G = 5.0  # decision 9: 5g-equivalent for weight
TOLERANCE_MIN_ABS_ML = 5.0  # decision 9: 5mL-equivalent for volume
EMBEDDING_TOP_K = 10  # decision 6: top-10 embedding candidates on retry

# --- Output (decision 11) ---
OUTPUT_PATTERN = "./output/ad_match_{date}.xlsx"  # date = YYYY-MM-DD

# --- Raddar: three gateways (correction — no single key) ---
RADDAR_ADF_KEY = _env("RADDAR_ADF_KEY")
RADDAR_ADF_BASE_URL = _env("RADDAR_ADF_BASE_URL")
RADDAR_METRO_KEY = _env("RADDAR_METRO_KEY")
RADDAR_METRO_BASE_URL = _env("RADDAR_METRO_BASE_URL")
RADDAR_FOODBASICS_KEY = _env("RADDAR_FOODBASICS_KEY")
RADDAR_FOODBASICS_BASE_URL = _env("RADDAR_FOODBASICS_BASE_URL")

# --- Raddar: other settings (correction) ---
RADDAR_SSL_VERIFY = _env("RADDAR_SSL_VERIFY", "1") != "0"
# Spec Q3 CLOSED: target = Ottawa, Ontario. Default postal is downtown Ottawa
# (non-secret). User may override via .env RADDAR_POSTAL_CODE.
TARGET_CITY = _env("TARGET_CITY", "Ottawa")
RADDAR_POSTAL_CODE = _env("RADDAR_POSTAL_CODE", _env("POSTAL_CODE", "K1C2N6"))
RADDAR_DISCOVER = _env("RADDAR_DISCOVER", "")
RADDAR_RADIUS_KM = _env("RADDAR_RADIUS_KM", _env("RADIUS", "100"))
RADDAR_LANG = _env("RADDAR_LANG", _env("LANG", "en"))

# --- Gemini on/off (Stage 4: proxy currently unreachable) ---
# False (default): ZERO Gemini calls — test-only rules fallback, run logs
# "TEST MODE — Gemini disabled". True: real LiteLLM calls. Env override, no code change.
GEMINI_ENABLED = _env("GEMINI_ENABLED", "0") not in ("", "0", "false", "False", "no", "No")
LITELLM_API_KEY = _env("LITELLM_API_KEY")
LITELLM_BASE_URL = _env("LITELLM_BASE_URL", "http://localhost:4000")
LITELLM_MODEL = _env("LITELLM_MODEL", "gemini-2.5-flash")

# --- App ---
DB_PATH = _env("DB_PATH", "data/price_match.db")
WEEK_ID = _env("WEEK_ID", "")

# --- Out-of-region exclusion (region call: never fetch QC prices) ---
# Metro's nearest store is Gatineau QC from every tested postal (K1P1J1,
# K1C2N6) — no Ontario resolution. Excluded with a loud kind-C warning,
# distinct from Case A (no flyer) / Case B (extraction failed).
OUT_OF_REGION = {
    "Metro": "nearest store is Gatineau QC, no Ontario resolution from any tested postal",
}


# --- Store -> gateway routing (correction) ---
# Most banners -> ADF. Metro -> metrodigital/METRO. Food Basics -> metrodigital/FOODBASICS.
STORE_GATEWAY = {
    "superstore": "adf",
    "walmart": "adf",
    "no frills": "adf",
    "sobeys": "adf",
    "freshco": "adf",
    "costco": "adf",
    "metro": "metro",
    "food basics": "foodbasics",
}


def gateway_for_store(store: str) -> str:
    return STORE_GATEWAY.get(store.strip().lower(), "adf")


def key_for_gateway(gateway: str) -> str:
    if gateway == "metro":
        return RADDAR_METRO_KEY
    if gateway == "foodbasics":
        return RADDAR_FOODBASICS_KEY
    return RADDAR_ADF_KEY


def base_for_gateway(gateway: str) -> str:
    if gateway == "metro":
        return RADDAR_METRO_BASE_URL
    if gateway == "foodbasics":
        return RADDAR_FOODBASICS_BASE_URL
    return RADDAR_ADF_BASE_URL


def has_key_for_gateway(gateway: str) -> bool:
    return bool(key_for_gateway(gateway))


def raddar_env_status() -> dict:
    """Presence booleans ONLY — never values."""
    return {
        "RADDAR_ADF_KEY_set": bool(RADDAR_ADF_KEY),
        "RADDAR_ADF_BASE_URL_set": bool(RADDAR_ADF_BASE_URL),
        "RADDAR_METRO_KEY_set": bool(RADDAR_METRO_KEY),
        "RADDAR_METRO_BASE_URL_set": bool(RADDAR_METRO_BASE_URL),
        "RADDAR_FOODBASICS_KEY_set": bool(RADDAR_FOODBASICS_KEY),
        "RADDAR_FOODBASICS_BASE_URL_set": bool(RADDAR_FOODBASICS_BASE_URL),
        "LITELLM_API_KEY_set": bool(LITELLM_API_KEY),
        "RADDAR_POSTAL_CODE_set": bool(RADDAR_POSTAL_CODE),
    }


# --- Reliability (decision 15) ---
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2.0


@dataclass(frozen=True)
class Settings:
    region: str = REGION
    confidence_threshold: float = CONFIDENCE_THRESHOLD
    tolerance_pct: float = TOLERANCE_DEFAULT_PCT
    retry_tolerance_pct: float = RETRY_TOLERANCE_PCT
    output_pattern: str = OUTPUT_PATTERN
    max_retries: int = MAX_RETRIES


DEFAULT_SETTINGS = Settings()
