"""Layer 3 — confidence-triggered retry (spec 6.5): widen + embeddings.

Trigger: confidence < 0.70 OR <3 candidates OR NO MATCH.
(a) Widen: tolerance 2%->10%, category gate dropped (implemented as re-score
    with tol_pct=RETRY_TOLERANCE_PCT and cat_ok forced true).
(b) Meaning net: real embedding similarity via LiteLLM when GEMINI_ENABLED,
    else character-bigram similarity fallback (TEST MODE, zero API calls).
    Top-10 by similarity join the pool. Gemini re-judges the expanded set.
"""
import logging
import math
import re

import config
from candidates import Candidate, TargetSpec, meaningful_tokens
from models import CleanItem

logger = logging.getLogger(__name__)
EMBED_MODEL = __import__("os").environ.get("LITELLM_EMBED_MODEL", "gemini-embedding-001").strip()


def _bigrams(text: str) -> set[str]:
    t = re.sub(r"\s+", " ", text.lower()).strip()
    return {t[i : i + 2] for i in range(max(0, len(t) - 1))} or {t}


def _bigram_sim(a: str, b: str) -> float:
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return 0.0
    return len(ba & bb) / math.sqrt(len(ba) * len(bb))


def embedding_hits(spec: TargetSpec, pool: list[CleanItem], have_ids: set[int], k: int = 10) -> list[CleanItem]:
    """Meaning similarity top-k not already in the pool. TEST MODE: bigrams."""
    query = (spec.branch or spec.raw_name) + " " + spec.raw_size
    if not config.GEMINI_ENABLED:
        logger.warning("TEST MODE — Gemini disabled (bigram similarity fallback)")
        ranked = sorted(pool, key=lambda it: -_bigram_sim(query, f"{it.title} {it.category or ''}"))
        return [it for it in ranked if id(it) not in have_ids][:k]
    from openai import OpenAI

    client = OpenAI(base_url=config.LITELLM_BASE_URL, api_key=config.LITELLM_API_KEY)
    texts = [f"{it.title} {it.category or ''}" for it in pool]
    try:
        emb = client.embeddings.create(model=EMBED_MODEL, input=[query] + texts)
        qv = emb.data[0].embedding
        sims = []
        for i, it in enumerate(pool):
            v = emb.data[i + 1].embedding
            dot = sum(a * b for a, b in zip(qv, v))
            nq = math.sqrt(sum(a * a for a in qv)) or 1.0
            nv = math.sqrt(sum(b * b for b in v)) or 1.0
            sims.append((dot / (nq * nv), it))
        sims.sort(key=lambda x: -x[0])
        return [it for _, it in sims if id(it) not in have_ids][:k]
    except Exception as exc:  # noqa: BLE001 — retry must not kill the run
        logger.error("Embedding search failed, bigram fallback: %s", exc)
        ranked = sorted(pool, key=lambda it: -_bigram_sim(query, f"{it.title} {it.category or ''}"))
        return [it for it in ranked if id(it) not in have_ids][:k]


def should_retry(judgment: dict, n_cands: int) -> bool:
    return (
        judgment.get("confidence", 0) < config.CONFIDENCE_THRESHOLD
        or n_cands < 3
        or judgment.get("match") == "NO MATCH"
    )


def widened(spec: TargetSpec, pool: list[CleanItem]) -> list[Candidate]:
    """Re-score with 10% tolerance and no category gate (spec 6.5a)."""
    from candidates import _score, _size_status

    out: list[Candidate] = []
    for item in pool:
        if item.price is None:
            continue
        r = _score(spec, item, config.RETRY_TOLERANCE_PCT)
        if r:
            sig = list(r[1]) + (["cat-gate-dropped"] if "cat+price-band" not in r[1] else [])
            out.append(Candidate(item=item, score=round(r[0], 3), signals=sig, spec=spec,
                                 size_verified=(_size_status(spec, item, config.RETRY_TOLERANCE_PCT) == "ok")))
    out.sort(key=lambda c: (-c.score, c.item.price or 0))
    return out
