"""Stage 6 — Learning loop (spec 8). Corrections -> memory -> rules.

Reads Ellie's decisions back from the SAME output Excel (Ellie's decision +
Ellie's reason columns) — no separate file. Each Reject/Change + reason
becomes a candidate rule; Approve on a flagged STRONG becomes a confirmed
STRONG record (stored directly — it changes no matching, only reviewer notes).

GUARDRAIL: a candidate goes live ONLY if golden precision AND recall do not
drop. Otherwise it stays a one-off note. Stored: rule + reason + example +
date. Never the full weekly output.
"""
import logging
from datetime import date

import config
from candidates import meaningful_tokens

logger = logging.getLogger(__name__)

SIZE_WORDS = {"size", "pack", "weight", "gram", "oz", "ml", "count", "unit", "volume", "kg", "lb"}
BRAND_WORDS = {"brand", "equivalent", "label", "selection", "name"}


def _find_col(ws, keys: set[str]) -> int | None:
    for j in range(1, ws.max_column + 1):
        v = str(ws.cell(row=2, column=j).value or "").strip().lower()
        if v in keys or any(k in v for k in keys):
            return j
    return None


def read_decisions(path: str) -> list[dict]:
    """Scrape decision rows from Action List (+ Review Queue sheets)."""
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=True)
    out = []
    seen = set()  # Review Queue repeats Action List rows — collect each once
    for sheet in ("Action List", "Review Queue"):
        if sheet not in wb.sheetnames:
            continue
        ws = wb[sheet]
        cu = _find_col(ws, {"ellie's decision", "decision"})
        cr = _find_col(ws, {"ellie's reason", "reason"})
        if cu is None:
            continue
        for i in range(3, ws.max_row + 1):
            dec = str(ws.cell(row=i, column=cu).value or "").strip()
            if not dec:
                continue
            key = (str(ws.cell(row=i, column=2).value or "").strip(),
                   str(ws.cell(row=i, column=6).value or "").strip())
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "sheet": sheet,
                "list_item": ws.cell(row=i, column=2).value,
                "match_class": ws.cell(row=i, column=18).value,
                "competitor": ws.cell(row=i, column=6).value,
                "decision": dec,
                "reason": str(ws.cell(row=i, column=cr).value or "").strip() if cr else "",
                "needs_review": bool(ws.cell(row=i, column=20).value),
            })
    return out


def classify_correction(d: dict) -> dict:
    """Approve-on-flagged-STRONG -> strong_record. Else candidate rule."""
    dec = d["decision"].strip().lower()
    if dec.startswith("approv") and (d["needs_review"] or (d["match_class"] or "").upper() == "STRONG"):
        return {"kind": "strong_record", "decision": d}
    rl = (d["reason"] or "").lower()
    if any(w in rl for w in SIZE_WORDS):
        return {"kind": "normalization", "decision": d}
    if any(w in rl for w in BRAND_WORDS):
        return {"kind": "brand_equiv", "decision": d}
    # Default: forbid the wrong pair (safe, list-scoped). Forbid tokens =
    # distinctive competitor tokens absent from the list text.
    ltoks = meaningful_tokens(d["list_item"] or "")
    ctoks = meaningful_tokens(d["competitor"] or "")
    forbid = sorted(t for t in (ctoks - ltoks) if len(t) > 1)[:3]
    return {"kind": "forbid_pair", "decision": d, "tokens": forbid}


def _metrics(results: list, gold: list[dict]) -> dict:
    by_item = {r.list_item: r for r in results}
    n = correct = claimed = claimed_ok = tp = exp_pos = 0
    for g in gold:
        r = by_item.get(g["list_item"])
        if r is None:
            continue
        n += 1
        exp = (g.get("expected_match") or "").upper()
        if r.match_class == exp:
            correct += 1
        if exp in ("EXACT", "STRONG"):
            exp_pos += 1
            if r.match_class in ("EXACT", "STRONG"):
                tp += 1
        if r.match_class in ("EXACT", "STRONG"):
            claimed += 1
            if exp in ("EXACT", "STRONG"):
                claimed_ok += 1
    return {
        "accuracy": round(correct / n, 3) if n else 0.0,
        "precision": round(claimed_ok / claimed, 3) if claimed else 0.0,
        "recall": round(tp / exp_pos, 3) if exp_pos else 0.0,
        "n": n,
    }


def guardrail(rule_apply, rule_revert, run_match, gold_path: str = "data/golden_test.csv") -> dict:
    """Run golden with/without the candidate. Live iff nothing drops."""
    from golden_test import load_golden

    gold = load_golden(gold_path)
    base = _metrics(run_match(), gold)
    rule_apply()
    try:
        with_ = _metrics(run_match(), gold)
    finally:
        rule_revert()
    live = (with_["precision"] >= base["precision"] and with_["recall"] >= base["recall"]
            and with_["accuracy"] >= base["accuracy"])
    return {"baseline": base, "with_rule": with_, "live": live}


def collect(path: str, run_match=None, gold_path: str = "data/golden_test.csv") -> dict:
    """Full Stage 6 pass over an annotated workbook. Returns summary."""
    from rules_table import load_rules, save_rules, today
    from rules_table import MatchingRule, NormalizationRule, StrongRecord

    table = load_rules()
    summary = {"decisions": 0, "live": [], "notes": [], "strong_records": 0}
    # parsed-name lookup: memory keys on parsed names (what Layer 1 sees),
    # display uses raw lines. Both live in target_list.csv.
    try:
        from stage3_etl import load_target_list

        parsed_by_raw = {(r.get("raw") or "").strip(): (r.get("name") or "").strip()
                         for r in load_target_list("data/target_list.csv")}
    except Exception:
        parsed_by_raw = {}
    for d in read_decisions(path):
        if not d["decision"].lower().startswith(("approv", "reject", "change")):
            continue
        summary["decisions"] += 1
        kind = classify_correction(d)
        if kind["kind"] == "strong_record":
            table.strong_pairs.append(StrongRecord(
                list_item=str(d["list_item"] or ""), offer_key=f"{d['competitor']}",
                store="", date_confirmed=today()))
            summary["strong_records"] += 1
            continue
        if kind["kind"] == "normalization":
            cand = NormalizationRule(rule_id=f"audit-{date.today().isoformat()}-{summary['decisions']}",
                                     pattern=d["reason"], action="pending review",
                                     reason=d["reason"], example=f"{d['list_item']} -> {d['competitor']}",
                                     date_added=today())
            if run_match is None:
                summary["notes"].append(f"normalization note (no golden run): {d['reason'][:80]}")
                continue
            table.normalization.append(cand)
            save_rules(table)
            summary["notes"].append(f"normalization note (golden run needs remainder): {d['reason'][:80]}")
            continue
        # matching candidates: brand_equiv or forbid_pair
        if kind["kind"] == "brand_equiv":
            rule = MatchingRule(reason=d["reason"], date_added=today(), kind="brand_equiv",
                                list_item="", tokens=[], status="note",
                                example=f"{d['list_item']} -> {d['competitor']}")
            table.matching.append(rule)
            save_rules(table)
            summary["notes"].append(f"brand_equiv note (manual scoping needed): {d['reason'][:80]}")
            continue
        rule = MatchingRule(reason=d["reason"], date_added=today(), kind="forbid_pair",
                            list_item=parsed_by_raw.get(str(d["list_item"] or "").strip(),
                                                        str(d["list_item"] or "")),
                            tokens=kind.get("tokens", []),
                            status="note",
                            example=f"{d['list_item']} -> {d['competitor']}")
        if run_match is None:
            table.matching.append(rule)
            save_rules(table)
            summary["notes"].append(f"forbid note (no golden run): {rule.list_item[:50]}")
            continue

        def _apply(rr=rule):
            import candidates

            rr.status = "live"
            table.matching.append(rr)
            save_rules(table)
            candidates._FORBID_CACHE = None

        def _revert(rr=rule):
            import candidates

            table.matching = [m for m in table.matching if m is not rr]
            save_rules(table)
            candidates._FORBID_CACHE = None

        g = guardrail(_apply, _revert, run_match, gold_path)
        if g["live"]:
            _apply()
            summary["live"].append(f"forbid_pair LIVE: {rule.list_item[:50]} -{rule.tokens} "
                                   f"(acc {g['baseline']['accuracy']}->{g['with_rule']['accuracy']}, "
                                   f"p {g['baseline']['precision']}->{g['with_rule']['precision']}, "
                                   f"r {g['baseline']['recall']}->{g['with_rule']['recall']})")
        else:
            rule.status = "note"
            table.matching.append(rule)
            save_rules(table)
            summary["notes"].append(f"forbid REJECTED by golden (stays note): {rule.list_item[:50]}")
    save_rules(table)
    return summary



---------
RADDAR_ADF_KEY=8881f61684654723ba3fc7df4d283a18
RADDAR_METRO_KEY=0a112db32b2f42588b54063b05dfbc90
RADDAR_FOODBASICS_KEY=0defd42b9de9412488327864774fbfca
RADDAR_SSL_VERIFY=0
RADDAR_POSTAL_CODE=K1C2N6
RADDAR_DISCOVER=1
RADDAR_RADIUS_KM=100
RADDAR_LANG=en
RADDAR_ADF_BASE_URL=https://apim-raddar-adf.azure-api.net/api
RADDAR_METRO_BASE_URL=https://metrodigital-apim.azure-api.net/api
RADDAR_FOODBASICS_BASE_URL=https://metrodigital-apim.azure-api.net/api
LITELLM_API_KEY=sk-mTaGgx9xaxZbKDSzC3V3hg
LITELLM_BASE_URL=https://genai-litellm-dev.nane1-ml-1wr.1blw.cloud/
LITELLM_MODEL=gemini-2.5-flash
---------