"""Stage 4 — Matching orchestration, Strategy B (spec 6).

Per target item: OR-branch specs -> Layer 1 (competitors + Superstore pools)
-> Layer 2 judge -> Layer 3 retry if triggered -> lowest-price EXACT select
-> Superstore second pass. STRONG recorded in output, NEVER promoted or
favored (spec 6.8); known-strong pairs only add a reviewer note.
"""
import logging

import config
from candidates import Candidate, expand_targets, load_overrides, candidates_for_spec
from gemini_judge import judge
from models import CleanItem, MatchResult
from retry_embeddings import embedding_hits, should_retry, widened

logger = logging.getLogger(__name__)

REVIEW_CLASSES = ("STRONG",)


def _pick_lowest_exact(cands: list[Candidate], cls: str) -> Candidate | None:
    if cls != "EXACT":
        return None
    # Equivalent = same normalized title, or same brand+product with compatible pack
    top = cands[0]
    equiv = [c for c in cands[:8]
             if c.item.normalized_title == top.item.normalized_title
             or (c.item.brand == top.item.brand and c.item.product_type == top.item.product_type
                 and c.item.pack_count == top.item.pack_count)]
    equiv = equiv or [top]
    # Verified size wins over unverified at equal standing (size rule)
    verified = [c for c in equiv if c.size_verified]
    pool = verified or equiv
    pool.sort(key=lambda c: (c.item.price or 0, -c.score))
    best = pool[0]
    if len(pool) > 1 and pool[0].item.price == pool[1].item.price:
        best = max(pool[:2], key=lambda c: c.score)
    return best


def _match_pool(branch_text: str, spec, pool: list[CleanItem], store_tag: str) -> tuple[dict, list[Candidate], bool]:
    from candidates import override_note

    note = override_note(spec)
    prompt_text = branch_text + (f" [{note}]" if note else "")
    cands = candidates_for_spec(spec, pool)
    j = judge(prompt_text, cands)
    # Retry triggers: low confidence, thin pool, NO MATCH — or an unverified
    # top pick (a wider net may surface a size-stated offer for strict compare).
    top_unverified = bool(cands and j.get("offer_index") is not None
                          and j.get("offer_index", 0) < len(cands)
                          and not cands[j["offer_index"]].size_verified
                          and j.get("match") == "EXACT")
    retried = False
    if should_retry(j, len(cands)) or top_unverified:
        retried = True
        wide = widened(spec, pool)
        have = {id(c.item) for c in wide}
        from candidates import _size_status

        for it in embedding_hits(spec, pool, have):
            if it.price is not None:
                wide.append(Candidate(item=it, score=0.35, signals=["embedding"], spec=spec,
                                      size_verified=(_size_status(spec, it, config.RETRY_TOLERANCE_PCT) == "ok")))
        wide.sort(key=lambda c: (-c.score, c.item.price or 0))
        cands = wide[:25]
        j = judge(prompt_text + " (retry)", cands)
    return j, cands, retried


def match_all(clean: list[CleanItem], target_rows: list[dict]) -> tuple[list[MatchResult], list[MatchResult]]:
    """Returns (results, review_queue). TEST MODE banner logged prominently."""
    if not config.GEMINI_ENABLED:
        logger.warning("TEST MODE — Gemini disabled (full Stage 4 run, zero API calls)")
    overrides = load_overrides()
    specs = expand_targets(target_rows, overrides)
    competitors = [c for c in clean if c.store.lower() != "superstore"]
    superstore = [c for c in clean if c.store.lower() == "superstore"]

    try:
        from rules_table import load_rules, is_known_strong
        rules = load_rules()
    except Exception:
        rules, is_known_strong = None, lambda *a: False

    # group branch specs by list item
    by_item: dict[str, list] = {}
    for s in specs:
        by_item.setdefault(s.raw_name, []).append(s)

    results: list[MatchResult] = []
    for row in target_rows:
        name = (row.get("name") or "").strip()
        disp = (row.get("raw") or "").strip() or name  # view-only original line
        item_specs = by_item.get(name, [])
        best_overall: tuple | None = None  # (class_rank, price, -conf, MatchResult-ish)
        for spec in item_specs:
            branch_text = f"{spec.branch or name} [{spec.raw_size}]"
            j, cands, retried = _match_pool(branch_text, spec, competitors, "competitors")
            cls = j["match"]
            pick = _pick_lowest_exact(cands, cls) if cls == "EXACT" and cands else (cands[j["offer_index"]] if cls in ("EXACT", "STRONG", "RELATED") and j.get("offer_index") is not None and j["offer_index"] < len(cands) else None)
            if cls == "STRONG" and pick is None and cands:
                pick = cands[0]
            rank = {"EXACT": 0, "STRONG": 1, "RELATED": 2, "NO MATCH": 3}[cls]
            cand = (rank, (pick.item.price if pick and pick.item.price is not None else float("inf")), -j.get("confidence", 0), spec, j, pick, retried, cands)
            if best_overall is None or (cand[0], cand[1], cand[2]) < (best_overall[0], best_overall[1], best_overall[2]):
                best_overall = cand
        _, _, _, spec, j, pick, retried, _ = best_overall
        cls = j["match"]
        # CORRECTED SIZE RULE: flyer states no size -> NEVER EXACT.
        # Cap at STRONG + "size unverified" + Review Queue. With Gemini on,
        # try vision size fill first; if filled, compare strictly.
        size_unverified = bool(pick is not None and not pick.size_verified)
        if cls == "EXACT" and size_unverified and pick is not None:
            filled_ok = False
            if config.GEMINI_ENABLED and pick.item.image_url:
                from candidates import vision_size_ok
                from fill_missing import fill_size_from_image

                got = fill_size_from_image(pick.item.image_url)
                if got:
                    filled_ok = vision_size_ok(spec, int(got.get("pack_count") or 1),
                                               got.get("unit_size"), got.get("unit_type"))
                    if filled_ok:
                        pick.size_verified = True
            if not filled_ok:
                cls = "STRONG"
                size_unverified = True
            else:
                size_unverified = False  # vision-restored: strictly verified
        elif cls != "EXACT":
            size_unverified = False  # flag only gates EXACT claims
        res = MatchResult(
            list_item=disp,
            match_class=cls if cls in ("EXACT", "STRONG") else ("RELATED" if cls == "RELATED" else "NO MATCH"),
            competitor_store=pick.item.store if pick else None,
            competitor_title=pick.item.title if pick else None,
            competitor_price=pick.item.price if pick else None,
            competitor_page=pick.item.page if pick else None,
            competitor_flyer_id=pick.item.flyer_id if pick else None,
            matched_branch=spec.branch or "",
            reason=j.get("reason", "") + (" [size unverified]" if cls == "STRONG" and size_unverified else ""),
            confidence=min(float(j.get("confidence", 0) or 0), 0.69) if (cls == "STRONG" and size_unverified and float(j.get("confidence", 0) or 0) >= 0.70) else float(j.get("confidence", 0) or 0),
            needs_review=cls in REVIEW_CLASSES or (retried and float(j.get("confidence", 0) or 0) < config.CONFIDENCE_THRESHOLD) or size_unverified,
            retried=retried,
            size_unverified=size_unverified,
        )
        if cls not in ("EXACT", "STRONG"):
            res.match_class = "NO MATCH" if cls == "NO MATCH" else "RELATED"
            if cls == "RELATED":
                res.needs_review = False
        # Superstore second pass (same logic, Superstore offers only)
        sj, scands, sretried = _match_pool((spec.branch or name) + " [Superstore]", spec, superstore, "superstore")
        spick = None
        if sj["match"] in ("EXACT", "STRONG") and sj.get("offer_index") is not None and scands and sj["offer_index"] < len(scands):
            spick = scands[sj["offer_index"]]
        elif sj["match"] == "EXACT" and scands:
            spick = scands[0]
        if spick:
            res.superstore_title, res.superstore_price, res.superstore_page = spick.item.title, spick.item.price, spick.item.page
            if not spick.size_verified:
                res.reason += " [SS size unverified]"
        # known-strong note (never favor/promote — reviewer info only)
        if rules is not None and pick is not None:
            key = f"{pick.item.store}|{pick.item.normalized_title}|{pick.item.price}"
            if is_known_strong(rules, name, key):
                res.reason += " [seen-before STRONG]"
        results.append(res)

    review = [r for r in results if r.needs_review]
    return results, review
