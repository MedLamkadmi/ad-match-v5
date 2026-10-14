"""Rules memory — structure only (Stage 3). Content grows in Stage 6.

Three kinds of memory (spec 8):
  - Normalization rules  e.g. pack pattern N x SIZE = pack, never single.
  - Matching rules        e.g. Brand A = Brand B as equivalent.
  - Confirmed STRONG pair records (seen + confirmed, never auto-promoted).

Stored: rule + reason + example + date. Golden-test guardrail decides go-live.
Full weekly output is NOT stored.
"""
from __future__ import annotations
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date


@dataclass
class NormalizationRule:
    rule_id: str
    pattern: str  # human-readable IF condition
    action: str  # THEN action
    reason: str  # Ellie's "this is wrong because..."
    example: str = ""
    date_added: str = ""
    golden_passed: bool = False


@dataclass
class MatchingRule:
    brand_a: str = ""
    brand_b: str = ""
    equivalent: bool = True
    reason: str = ""
    date_added: str = ""
    # Stage 6 candidate-rule fields (defaults keep old files loadable)
    kind: str = "note"  # note | forbid_pair | brand_equiv
    list_item: str = ""  # parsed target name the rule applies to
    tokens: list = None  # forbid tokens (candidate must not contain)
    status: str = "note"  # note (one-off) | live (enforced)
    example: str = ""

    def __post_init__(self):
        if self.tokens is None:
            self.tokens = []


@dataclass
class StrongRecord:
    list_item: str
    offer_key: str  # store|normalized_title|price
    store: str
    date_confirmed: str = ""


@dataclass
class RulesTable:
    normalization: list[NormalizationRule] = field(default_factory=list)
    matching: list[MatchingRule] = field(default_factory=list)
    strong_pairs: list[StrongRecord] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "normalization": [asdict(r) for r in self.normalization],
            "matching": [asdict(r) for r in self.matching],
            "strong_pairs": [asdict(r) for r in self.strong_pairs],
        }

    @classmethod
    def from_dict(cls, d: dict) -> RulesTable:
        return cls(
            normalization=[NormalizationRule(**r) for r in d.get("normalization", [])],
            matching=[MatchingRule(**r) for r in d.get("matching", [])],
            strong_pairs=[StrongRecord(**r) for r in d.get("strong_pairs", [])],
        )


MEMORY_DIR = os.path.join("data", "memory")
RULES_PATH = os.path.join(MEMORY_DIR, "rules.json")


def load_rules(path: str = RULES_PATH) -> RulesTable:
    if not os.path.exists(path):
        return RulesTable()
    with open(path, encoding="utf-8") as f:
        return RulesTable.from_dict(json.load(f))


def save_rules(table: RulesTable, path: str = RULES_PATH) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(table.to_dict(), f, indent=2)


def today() -> str:
    return date.today().isoformat()


def add_normalization_rule(
    table: RulesTable, rule_id: str, pattern: str, action: str, reason: str, example: str = ""
) -> None:
    table.normalization.append(
        NormalizationRule(
            rule_id=rule_id, pattern=pattern, action=action,
            reason=reason, example=example, date_added=today(),
        )
    )


def is_known_strong(table: RulesTable, list_item: str, offer_key: str) -> bool:
    return any(
        s.list_item == list_item and s.offer_key == offer_key for s in table.strong_pairs
    )
