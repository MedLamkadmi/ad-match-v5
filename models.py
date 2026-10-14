"""Shared dataclasses for Stage 1 + Stage 2."""
from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class Flyer:
    flyer_id: str
    store: str
    region: str
    valid_from: date
    valid_to: date


@dataclass(frozen=True)
class RawItem:
    """Stage 2 output row — everything pulled, no filtering (spec 4)."""

    title: str | None
    price: float | str | None
    image_url: str | None
    page: int | None
    valid_from: date | None
    valid_to: date | None
    category_raw: str | None
    store: str
    flyer_id: str | None = None
    save_only: bool = False  # offer had savings info but no sale/regular price


@dataclass(frozen=True)
class TargetSelection:
    flyer: Flyer
    is_carryover: bool = False  # True -> mark CHECK WITH ELLIE in Warnings sheet
    reason: str = ""


@dataclass(frozen=True)
class StoreWarning:
    store: str
    case: str  # "A-no-flyer" | "B-extraction-failed" | "C-out-of-region"
    message: str


@dataclass
class ExtractionResult:
    items: list[RawItem] = field(default_factory=list)
    warnings: list[StoreWarning] = field(default_factory=list)
    extracted_stores: list[str] = field(default_factory=list)
    missing_stores: list[str] = field(default_factory=list)


@dataclass
class MatchResult:
    """Stage 4 output per target-list item (spec 6.4 + 6.6/6.7)."""

    list_item: str
    match_class: str  # EXACT | STRONG | RELATED | NO MATCH
    competitor_store: str | None = None
    competitor_title: str | None = None
    competitor_price: float | None = None
    competitor_page: int | None = None
    competitor_flyer_id: str | None = None
    superstore_title: str | None = None
    superstore_price: float | None = None
    superstore_page: int | None = None
    matched_branch: str = ""  # WHICH OR-branch matched (spec 6.3)
    reason: str = ""
    confidence: float = 0.0
    needs_review: bool = False
    retried: bool = False
    size_unverified: bool = False  # flyer states no size: capped at STRONG


@dataclass
class CleanItem:
    """Stage 3 output row — ONE clean table, same columns (spec 5.9)."""

    title: str
    normalized_title: str
    brand: str
    product_type: str
    flavor_variant: str
    pack_count: int
    unit_size: float | None
    unit_type: str
    total_size: float | None
    language: str
    price: float | None
    price_flags: list[str] = field(default_factory=list)
    price_per_unit: float | None = None
    image_url: str | None = None
    page: int | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    store: str = ""
    flyer_id: str | None = None
    category: str | None = None
    based_on_image: list[str] = field(default_factory=list)  # filled fields
    description: str = ""
