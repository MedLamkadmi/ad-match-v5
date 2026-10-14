"""Golden test — runner + template (Stage 4). User fills 50 hand-matched rows.

CSV: list_item, expected_match, expected_price, expected_store.
expected_match in EXACT|STRONG|RELATED|NO MATCH. Runner reports accuracy
+ precision/recall on Layer-1-style inclusion (approximated by non-NO MATCH).
"""
import csv


def load_golden(path: str = "data/golden_test.csv") -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if (r.get("list_item") or "").strip()]


def run_golden(results: list, path: str = "data/golden_test.csv") -> dict:
    gold = load_golden(path)
    by_item = {r.list_item: r for r in results}
    n = correct = 0
    claimed = claimed_ok = 0
    for g in gold:
        r = by_item.get(g["list_item"])
        if r is None:
            continue
        n += 1
        exp = (g.get("expected_match") or "").strip().upper()
        if r.match_class == exp:
            correct += 1
        if r.match_class in ("EXACT", "STRONG"):
            claimed += 1
            if exp in ("EXACT", "STRONG"):
                claimed_ok += 1
    return {
        "n": n,
        "accuracy": round(correct / n, 3) if n else 0.0,
        "precision": round(claimed_ok / claimed, 3) if claimed else 0.0,
    }
