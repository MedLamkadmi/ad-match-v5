"""Stage 5 — Output (spec 7), Ellie-readable redesign.

Sheets: Summary / Action List (ALL items, 5-group sort) / Review Queue /
Warnings. TEST MODE banner on every sheet + in filename when Gemini is off.
Layout: separate image columns, split Price/Page/From/To, plain-English why,
numeric Gap % (sortable), 60pt rows, auto-filter + freeze everywhere.
"""
import io
import logging
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import config
from models import CleanItem, MatchResult

logger = logging.getLogger(__name__)

COLUMNS = ["Action", "List item", "Gap $", "Gap %", "Competitor store",
           "Competitor item", "Competitor image", "Competitor price",
           "Page", "Valid from", "Valid to",
           "Superstore item", "Superstore image", "Superstore price",
           "Page", "Valid from", "Valid to",
           "Match class", "Why this match", "Flags", "Ellie's decision", "Ellie's reason"]
N_COLS = len(COLUMNS)

GROUP_ORDER = ["SUPERSTORE HIGHER", "NOT IN SUPERSTORE", "ONLY IN SUPERSTORE",
               "NOT FOUND ANYWHERE", "EQUAL / OK"]

FILL_G1 = "F8CBAD"
FILL_G2 = "FCE4B2"
FILL_G345 = "D9D9D9"
FILL_REVIEW = "FFFF00"
FILL_HEADER = "4472C4"
FILL_BANNER = "FF0000"


def classify(res: MatchResult) -> tuple[str, float | None, float | None]:
    pc = res.competitor_price if res.match_class in ("EXACT", "STRONG") else None
    ps = res.superstore_price
    if pc is not None and ps is not None:
        gap = round(ps - pc, 2)
        pct = round(gap / pc * 100, 1) if pc else 0.0
        if ps > pc:
            return "SUPERSTORE HIGHER", gap, pct
        return "EQUAL / OK", gap, pct
    if pc is not None:
        return "NOT IN SUPERSTORE", None, None
    if ps is not None:
        return "ONLY IN SUPERSTORE", None, None
    return "NOT FOUND ANYWHERE", None, None


def plain_reason(res: MatchResult) -> str:
    """Rule-debug text -> one plain-English line (no debug syntax).

    Live Gemini reasons pass through verbatim (only the branch suffix is
    added) — never parsed or replaced.
    """
    raw = res.reason or ""
    is_fallback = raw.startswith("rules") or "tokens:[" in raw
    if not is_fallback:
        text = raw.strip()
        if res.matched_branch:
            text += f" — matched '{res.matched_branch}'"
        return text
    toks = re.search(r"tokens:\[([^\]]*)\]", raw)
    words = [w.strip("' \"") for w in toks.group(1).split(",")] if toks else []
    words = [w for w in words if w]
    brand = re.search(r"brand:([a-z0-9'& -]+)", raw)
    brand_words = set(brand.group(1).strip().split()) if brand else set()
    parts = []
    if brand:
        parts.append(f"Same brand ({brand.group(1).strip().title()})")
    prod_words = [w for w in words if w not in brand_words]
    if prod_words:
        parts.append(f"same product ({', '.join(prod_words[:3])})")
    if "embedding" in raw:
        parts.append("similar meaning")
    if not parts:
        parts.append("No comparable offer" if res.match_class == "NO MATCH" else "Close match, needs a look")
    text = " + ".join(parts)
    if res.matched_branch:
        text += f" — matched '{res.matched_branch}'"
    return text


def _index_clean(clean: list[CleanItem]) -> dict:
    idx = {}
    for c in clean:
        idx.setdefault((c.store, c.title), c)
    return idx


def _fetch_thumb(url: str) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read()
        if not data:
            return None
        from PIL import Image

        im = Image.open(io.BytesIO(data)).convert("RGB")
        im.thumbnail((100, 100))
        buf = io.BytesIO()
        im.save(buf, format="JPEG")
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001 — dead image never fails the run
        logger.warning("Image fetch failed %.60s: %s", url, exc)
        return None


def _dstr(d) -> str:
    if d is None or d == "":
        return ""
    try:
        return d.strftime("%b %d")
    except AttributeError:
        return str(d)


def build_workbook(results: list[MatchResult], clean: list[CleanItem],
                   meta: dict, path: str | None = None) -> str:
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    test_mode = meta.get("test_mode", not config.GEMINI_ENABLED)
    run_date = meta.get("run_date") or date.today().isoformat()
    path = path or f"./output/ad_match_{run_date}{'_TESTMODE' if test_mode else ''}.xlsx"

    idx = _index_clean(clean)
    enriched = []
    for r in results:
        action, gap, pct = classify(r)
        comp = idx.get((r.competitor_store or "", r.competitor_title or "")) if r.match_class in ("EXACT", "STRONG") else None
        ss = None
        if r.superstore_title:
            ss = next((c for c in clean if c.store.lower() == "superstore" and c.title == r.superstore_title), None)
        flags = []
        if r.needs_review:
            flags.append("REVIEW")
        for c in (comp, ss):
            if c is not None and c.based_on_image:
                flags.append("based on image")
                break
        enriched.append({"res": r, "action": action, "gap": gap, "pct": pct,
                         "comp": comp, "ss": ss, "flags": "+".join(flags)})

    order = {g: i for i, g in enumerate(GROUP_ORDER)}

    def sort_key(e):
        r = e["res"]
        if e["action"] == "SUPERSTORE HIGHER":
            return (0, -(e["pct"] or 0))
        if e["action"] == "NOT IN SUPERSTORE":
            return (1, (r.competitor_price if r.competitor_price is not None else 1e9))
        return (order[e["action"]], r.list_item.lower())

    enriched.sort(key=sort_key)

    urls = []
    for e in enriched:
        for c in (e["comp"], e["ss"]):
            if c is not None and c.image_url:
                urls.append(c.image_url)
    urls = list(dict.fromkeys(urls))
    thumbs: dict[str, bytes] = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for u, b in zip(urls, ex.map(_fetch_thumb, urls)):
            if b:
                thumbs[u] = b

    wb = Workbook()
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor=FILL_HEADER)
    banner_fill = PatternFill("solid", fgColor=FILL_BANNER)
    banner_font = Font(bold=True, color="FFFFFF", size=12)
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    money_fmt = '"$"#,##0.00'
    pct_fmt = '0.0%'

    def sheet_banner(ws, text, ncols):
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncols)
        c = ws.cell(row=1, column=1, value=text)
        c.fill, c.font, c.alignment = banner_fill, banner_font, center
        ws.row_dimensions[1].height = 28

    banner_text = ("TEST MODE — Gemini OFF, rule-fallback reasons (VPN pending). DO NOT USE AS REAL OUTPUT."
                   if test_mode else "Ad Match — weekly price comparison")

    group_fill = {"SUPERSTORE HIGHER": PatternFill("solid", fgColor=FILL_G1),
                  "NOT IN SUPERSTORE": PatternFill("solid", fgColor=FILL_G2),
                  "ONLY IN SUPERSTORE": PatternFill("solid", fgColor=FILL_G345),
                  "NOT FOUND ANYWHERE": PatternFill("solid", fgColor=FILL_G345),
                  "EQUAL / OK": PatternFill("solid", fgColor=FILL_G345)}
    review_fill = PatternFill("solid", fgColor=FILL_REVIEW)

    IMG_W = 7
    SS_IMG_W = 13

    def paint_action_sheet(ws, rows, with_images: bool, title: str):
        sheet_banner(ws, banner_text, N_COLS)
        for j, h in enumerate(COLUMNS, 1):
            label = h + (" (Approve / Reject / Change)" if h == "Ellie's decision" else "")
            c = ws.cell(row=2, column=j, value=label)
            c.font, c.fill, c.alignment = header_font, header_fill, center
        widths = [18, 30, 9, 9, 13, 32, 14, 11, 7, 10, 10, 32, 14, 11, 7, 10, 10, 11, 42, 14, 14, 30]
        for j, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(j)].width = w
        for i, e in enumerate(rows, 3):
            r = e["res"]
            has_comp = e["action"] in ("SUPERSTORE HIGHER", "NOT IN SUPERSTORE", "EQUAL / OK")
            has_ss = e["action"] in ("SUPERSTORE HIGHER", "ONLY IN SUPERSTORE", "EQUAL / OK")
            vals = [
                e["action"], r.list_item,
                e["gap"] if e["gap"] is not None else "",
                (e["pct"] / 100 if e["pct"] is not None else ""),
                r.competitor_store if has_comp and r.competitor_store else ("--" if has_comp else "not found"),
                r.competitor_title if has_comp and r.competitor_title else "not found",
                "",  # image col filled below / "no image"
                r.competitor_price if has_comp and r.competitor_price is not None else "",
                r.competitor_page if has_comp and r.competitor_page else "",
                _dstr(e["comp"].valid_from) if has_comp and e["comp"] else "",
                _dstr(e["comp"].valid_to) if has_comp and e["comp"] else "",
                r.superstore_title if has_ss and r.superstore_title else "not found",
                "",
                r.superstore_price if has_ss and r.superstore_price is not None else "",
                r.superstore_page if has_ss and r.superstore_page else "",
                _dstr(e["ss"].valid_from) if has_ss and e["ss"] else "",
                _dstr(e["ss"].valid_to) if has_ss and e["ss"] else "",
                r.match_class, plain_reason(r), e["flags"], "", "",
            ]
            for j, v in enumerate(vals, 1):
                c = ws.cell(row=i, column=j, value=v)
                c.alignment = Alignment(vertical="center", wrap_text=True)
                if j == 1:
                    c.fill = group_fill[e["action"]]
                if j == 20 and r.needs_review:
                    c.fill, c.font = review_fill, Font(bold=True)
            ws.cell(row=i, column=3).number_format = money_fmt
            ws.cell(row=i, column=4).number_format = pct_fmt
            ws.cell(row=i, column=8).number_format = money_fmt
            ws.cell(row=i, column=14).number_format = money_fmt
            ws.row_dimensions[i].height = 60
            for side, col in (("comp", IMG_W), ("ss", SS_IMG_W)):
                c = e[side]
                cell = ws.cell(row=i, column=col)
                embedded = False
                if with_images and c is not None and c.image_url and c.image_url in thumbs:
                    try:
                        im = XLImage(io.BytesIO(thumbs[c.image_url]))
                        im.width, im.height = 70, 70
                        ws.add_image(im, cell.coordinate)
                        embedded = True
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Embed failed: %s", exc)
                if not embedded:
                    cell.value = "no image"
            ws.cell(row=i, column=7).alignment = center
            ws.cell(row=i, column=13).alignment = center
        ws.freeze_panes = "A3"
        ws.auto_filter.ref = f"A2:{get_column_letter(N_COLS)}{len(rows) + 2}"
        ws.sheet_view.showGridLines = True

    ws2 = wb.active
    ws2.title = "Action List"
    paint_action_sheet(ws2, enriched, with_images=True, title="Action List")

    ws1 = wb.create_sheet("Summary", 0)
    sheet_banner(ws1, banner_text, 5)
    ws1.cell(row=2, column=1, value="Ad Match — Summary").font = Font(bold=True, size=14)
    ws1.cell(row=3, column=1,
             value=f"Run {run_date} | Items checked: {len(enriched)} | Review needed: {sum(1 for e in enriched if e['res'].needs_review)}")
    counts = {g: sum(1 for e in enriched if e["action"] == g) for g in GROUP_ORDER}
    r0 = 5
    for i, g in enumerate(GROUP_ORDER):
        ws1.cell(row=r0 + i, column=1, value=g).fill = group_fill[g]
        ws1.cell(row=r0 + i, column=2, value=counts[g]).alignment = center
    ws1.cell(row=r0 + 5, column=1, value="Needs review").fill = review_fill
    ws1.cell(row=r0 + 5, column=2, value=sum(1 for e in enriched if e["res"].needs_review))
    t0 = r0 + 7
    ws1.cell(row=t0, column=1, value="Top 10 price gaps (Superstore HIGHER, gap % desc)").font = Font(bold=True)
    for j, h in enumerate(["List item", "Competitor $", "Superstore $", "Gap $", "Gap %"], 1):
        c = ws1.cell(row=t0 + 1, column=j, value=h)
        c.font, c.fill = header_font, header_fill
    for i, e in enumerate([x for x in enriched if x["action"] == "SUPERSTORE HIGHER"][:10], t0 + 2):
        r = e["res"]
        ws1.cell(row=i, column=1, value=r.list_item)
        ws1.cell(row=i, column=2, value=r.competitor_price).number_format = money_fmt
        ws1.cell(row=i, column=3, value=r.superstore_price).number_format = money_fmt
        ws1.cell(row=i, column=4, value=e["gap"]).number_format = money_fmt
        ws1.cell(row=i, column=5, value=(e["pct"] or 0) / 100).number_format = pct_fmt
    ws1.column_dimensions["A"].width = 42
    for col in ("B", "C", "D", "E"):
        ws1.column_dimensions[col].width = 16
    ws1.freeze_panes = "A4"
    ws1.auto_filter.ref = f"A{t0 + 1}:E{t0 + 11}"

    ws3 = wb.create_sheet("Review Queue")
    paint_action_sheet(ws3, [e for e in enriched if e["res"].needs_review], with_images=True, title="Review Queue")

    ws4 = wb.create_sheet("Warnings")
    sheet_banner(ws4, banner_text, 1)
    lines = [
        f"Run {run_date} | TEST MODE: {test_mode}",
        "",
        "Missing flyers:",
        *[f"- {w}" for w in meta.get("ext_warnings", [])],
        f"- Carried-over 2-week flyers this run: {meta.get('carryovers', 0)}",
        "",
        "Data notes:",
        f"- Image-fill fields this run: {meta.get('image_fills', 0)}",
        f"- SAVE-only offers found at extraction: {meta.get('save_ghosts', 0)} "
        f"({meta.get('save_ghosts_noprice', 0)} removed from clean table; "
        f"{meta.get('save_ghosts_cat', 0)} excluded by other filters)",
        f"- {meta.get('page_note', '')}",
        "- Match reasons are fallback rule reasons (TEST MODE), not Gemini reasons.",
    ]
    for i, line in enumerate(lines, 2):
        ws4.cell(row=i, column=1, value=line)
    ws4.column_dimensions["A"].width = 110
    ws4.freeze_panes = "A3"

    import os

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    base, ext = os.path.splitext(path)
    cand, v = path, 1
    while True:
        try:
            wb.save(cand)
            if cand != path:
                logger.warning("Target locked, saved versioned copy %s", cand)
            return cand
        except PermissionError:
            # File open in Excel (Ellie reviewing) — version up, never fail
            v += 1
            cand = f"{base}_v{v}{ext}"
            if v > 50:
                raise
