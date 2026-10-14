"""Weekly CLI: Stage 1 -> 2 -> 3 -> 4 -> 5 -> 6. TEST MODE unless GEMINI_ENABLED=1."""
import argparse
import logging
from datetime import date

import config

logging.basicConfig(level=logging.WARNING, format="%(levelname)s:%(name)s:%(message)s")
logger = logging.getLogger("main")


def parse_args():
    p = argparse.ArgumentParser(description="Ad Match weekly run")
    p.add_argument("--week-start", default="", help="YYYY-MM-DD override (default: system date)")
    p.add_argument("--collect-from", default="", help="annotated workbook for Stage 6 (default: just-built file)")
    p.add_argument("--no-stage6", action="store_true", help="skip Stage 6")
    return p.parse_args()


def main() -> str:
    args = parse_args()
    try:
        run_date = date.fromisoformat(args.week_start) if args.week_start else date.today()
    except ValueError:
        raise SystemExit(f"bad --week-start {args.week_start!r}, want YYYY-MM-DD")

    print("=" * 64)
    print(f"AD MATCH weekly run | {run_date} | REGION={config.REGION} {config.TARGET_CITY}")
    if not config.GEMINI_ENABLED:
        print("*** TEST MODE — Gemini disabled (zero API calls, fallback reasons) ***")
    else:
        print("LIVE MODE — Gemini enabled via LiteLLM")

    from stage1_target import select_target_flyers
    print("\n[1] Target...")
    from stage2_extract import discover_stores_and_pubs, pubs_to_flyers
    mapped, pubs = discover_stores_and_pubs()
    flyers = pubs_to_flyers(pubs, mapped, config.REGION)
    sel = select_target_flyers(flyers, run_date, config.REGION)
    print(f"    discovered={len(flyers)} selected={len(sel)} "
          f"carryover={sum(1 for s in sel if s.is_carryover)}")

    from stage2_extract import extract_all, fetch_flyer_items
    print("[2] Extract...")
    res = extract_all(sel, fetch_flyer_items, config.ALL_STORES)
    from collections import Counter
    print(f"    items={len(res.items)} per={dict(Counter(i.store for i in res.items))}")
    for w in res.warnings:
        print(f"    {w.message}")

    from stage3_etl import run_etl, load_target_list, PAGE_NOTE
    print("[3] ETL...")
    clean, stats = run_etl(res.items, fill=True)
    print(f"    in={stats.items_in} complete={stats.complete_n} incomplete={stats.incomplete_n} "
          f"catdrop={stats.dropped_category_first} deduped={stats.deduped} "
          f"recheck={stats.dropped_category_recheck} noprice={stats.dropped_no_price} "
          f"ghosts={stats.save_ghosts} final={stats.final_n}")

    from stage4_match import match_all
    print("[4] Match...")
    targets = load_target_list("data/target_list.csv")
    results, review = match_all(clean, targets)
    print(f"    targets={len(targets)} classes={dict(Counter(r.match_class for r in results))} "
          f"review={len(review)}")

    from stage5_output import build_workbook
    print("[5] Output...")
    fills = (stats.fill_stats.get("title_filled", 0) + stats.fill_stats.get("category_filled", 0)
             + stats.enriched_complete)
    meta = {"run_date": run_date.isoformat(), "test_mode": not config.GEMINI_ENABLED,
            "ext_warnings": [w.message for w in res.warnings],
            "carryovers": sum(1 for s in sel if s.is_carryover),
            "save_ghosts": stats.save_ghosts, "save_ghosts_noprice": stats.save_ghosts_noprice,
            "save_ghosts_cat": stats.save_ghosts_cat,
            "image_fills": fills, "page_note": PAGE_NOTE}
    path = build_workbook(results, clean, meta)
    print(f"    saved {path}")

    if not args.no_stage6:
        from stage6_learning import collect
        print("[6] Learning...")
        src = args.collect_from or path
        try:
            summary = collect(src)
            print(f"    decisions={summary['decisions']} live={len(summary['live'])} "
                  f"notes={len(summary['notes'])} strong={summary['strong_records']}")
            for line in summary["live"] + summary["notes"]:
                print(f"    - {line}")
        except Exception as exc:
            print(f"    collect skipped: {exc}")
    print("=" * 64)
    return path


if __name__ == "__main__":
    main()
