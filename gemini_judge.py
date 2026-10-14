"""Layer 2 — Gemini judgment (spec 6.4) via LiteLLM, plus TEST MODE fallback.

GEMINI_ENABLED=False (default): ZERO API calls; rules fallback assigns
EXACT/STRONG/RELATED/NO MATCH from candidate scores (borderline downgraded,
mirroring the 'if unsure, downgrade' rule). Every call logs
"TEST MODE — Gemini disabled".
GEMINI_ENABLED=True: real Gemini 2.5 Flash judgment with downgrade instruction.
"""
import json
import logging

import config
from candidates import Candidate

logger = logging.getLogger(__name__)

JUDGE_PROMPT = """You match grocery flyer offers to a target list item.
Private-label rule: Food Basics 'Selection' == Loblaw 'No Name' -> EXACT when size fits.
Size law: the target size is authoritative. Compare strictly within tolerance.
A sizeless offer can NEVER be EXACT — at best STRONG. A stated size outside
tolerance (or a multipack vs single, or a wrong count) is NO MATCH / RELATED, never EXACT.
If unsure, DOWNGRADE (EXACT->STRONG, STRONG->RELATED). A wrong EXACT is worse than a cautious STRONG.
Reply JSON only: {"match": "EXACT|STRONG|RELATED|NO MATCH", "offer_index": <int|null>, "reason": "<one line>", "confidence": 0.0-1.0}
Match classes: EXACT = same product+brand(+equiv), size in tolerance. STRONG = very close, small difference. RELATED = similar, not same. NO MATCH = nothing comparable.
Target item: {target}
Candidates:
{cands}"""


def _fallback(spec_branch: str, cands: list[Candidate]) -> dict:
    logger.warning("TEST MODE — Gemini disabled (fallback judgment for %r)", spec_branch[:50])
    if not cands:
        return {"match": "NO MATCH", "offer_index": None, "reason": "no candidates", "confidence": 0.4}
    top = cands[0]
    sig = "+".join(top.signals)
    if top.score >= 0.8:
        return {"match": "EXACT", "offer_index": 0, "reason": f"rules: {sig}", "confidence": 0.8}
    if top.score >= 0.55:
        return {"match": "STRONG", "offer_index": 0, "reason": f"rules (downgraded): {sig}", "confidence": 0.6}
    if top.score >= 0.35:
        return {"match": "RELATED", "offer_index": 0, "reason": f"rules: {sig}", "confidence": 0.45}
    return {"match": "NO MATCH", "offer_index": None, "reason": f"rules: weak ({sig})", "confidence": 0.4}


def _live_rules_text() -> str:
    """Live Stage-6 rules injected into every judgment (cap 20 lines)."""
    try:
        from rules_table import load_rules

        lines = []
        for m in load_rules().matching:
            if getattr(m, "status", "") != "live":
                continue
            if getattr(m, "kind", "") == "brand_equiv" and m.brand_a and m.brand_b:
                lines.append(f"- {m.brand_a} == {m.brand_b} (equivalent)")
            elif getattr(m, "kind", "") == "forbid_pair" and m.list_item:
                lines.append(f"- For '{m.list_item}': reject offers containing {', '.join(m.tokens or [])}")
        for r in load_rules().normalization:
            if getattr(r, "golden_passed", False):
                lines.append(f"- {r.pattern} -> {r.action}")
        return "\n".join(lines[:20])
    except Exception:
        return ""


def judge(target_text: str, cands: list[Candidate]) -> dict:
    """Return {match, offer_index, reason, confidence}."""
    if not config.GEMINI_ENABLED:
        return _fallback(target_text, cands)
    from openai import OpenAI

    client = OpenAI(base_url=config.LITELLM_BASE_URL, api_key=config.LITELLM_API_KEY)
    lines = []
    for i, c in enumerate(cands[: MAX_CANDS]):
        it = c.item
        lines.append(
            f"[{i}] {it.store} | {it.title} | ${it.price} | {it.category} | "
            f"pack={it.pack_count} size={it.unit_size}{it.unit_type} | signals={'+'.join(c.signals)}"
        )
    prompt = JUDGE_PROMPT.format(target=target_text, cands="\n".join(lines) or "(none)")
    live = _live_rules_text()
    if live:
        prompt += "\nLive rules from prior audits (must follow):\n" + live
    last: Exception | None = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            resp = client.chat.completions.create(
                model=config.LITELLM_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=300,
                timeout=90,
            )
            text = (resp.choices[0].message.content or "").strip()
            s, e = text.find("{"), text.rfind("}")
            data = json.loads(text[s : e + 1] if s >= 0 else text)
            if data.get("match") not in ("EXACT", "STRONG", "RELATED", "NO MATCH"):
                raise ValueError(f"bad class {data.get('match')}")
            data["confidence"] = float(data.get("confidence", 0.5))
            return data
        except Exception as exc:  # noqa: BLE001 — one bad judgment must not kill the run
            last = exc
            logger.warning("Gemini judge failed attempt %d: %s", attempt, exc)
            import time

            time.sleep(config.RETRY_BACKOFF_SECONDS * attempt)
    logger.error("Gemini judge gave up, downgrading to NO MATCH: %s", last)
    return {"match": "NO MATCH", "offer_index": None, "reason": f"judge error: {last}", "confidence": 0.3}


MAX_CANDS = 15
