"""
N-gram waste analysis for the GDC AI Optimizer (GI-3).

Scans non-converting search terms and identifies 1-gram and 2-gram
tokens that appear repeatedly across multiple terms with no conversions.
These are candidates for broad-match negative keywords.

Thresholds (tunable at top of file):
  MIN_TERMS   – N-gram must appear in at least this many distinct search terms
  MIN_WASTE   – Total spend across those terms must exceed this amount (USD)
  MIN_DAYS    – Cache window to use (default 30 days)

Public API
----------
  compute_ngram_waste(days=30) -> dict
      Returns structured waste signal dict suitable for injection into
      the account-level optimizer context.

  get_ngram_waste_for_prompt(days=30, top_n=20) -> list[dict]
      Returns top_n ngram candidates sorted by waste, ready for the
      Claude prompt.
"""

from __future__ import annotations

import re
import logging
from collections import defaultdict
from typing import Optional

logger = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────
MIN_TERMS: int = 3          # N-gram must appear in ≥ N distinct search terms
MIN_WASTE: float = 15.0     # Total wasted spend threshold (USD)
MIN_DAYS: int = 30          # Cache window

# ── Stopwords ─────────────────────────────────────────────────────────────────
# Common dental search connectives that aren't meaningful negatives on their own
_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "and", "or", "of", "in", "for", "to", "with",
    "near", "me", "my", "i", "is", "are", "do", "can", "how", "what",
    "best", "top", "good", "great", "local", "new",
    # too generic in dental context
    "dental", "dentist", "dentistry", "teeth", "tooth",
})

# Tokens that are almost always relevant — never flag as waste unigrams
_NEVER_NEGATIVE_UNIGRAMS: frozenset[str] = frozenset({
    "implant", "implants", "invisalign", "crown", "crowns", "veneer",
    "veneers", "whitening", "cleaning", "cleanings", "emergency",
    "pediatric", "orthodontic", "braces", "filling", "extraction",
    "denture", "dentures", "grafton",  # our location — never negate alone
})

# ── Tokeniser ─────────────────────────────────────────────────────────────────

def _tokenise(text: str) -> list[str]:
    """Lower-case, strip punctuation, return word tokens."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return [t for t in text.split() if len(t) > 1]


def _ngrams_from_tokens(tokens: list[str], n: int) -> list[str]:
    """Produce N-grams of size `n` from a token list."""
    if len(tokens) < n:
        return []
    return [" ".join(tokens[i:i + n]) for i in range(len(tokens) - n + 1)]


# ── Core computation ──────────────────────────────────────────────────────────

def compute_ngram_waste(days: int = MIN_DAYS) -> dict:
    """
    Query non-converting search terms from gads_search_terms_cache,
    extract 1-gram and 2-gram waste signals, and return a structured dict.

    Returns
    -------
    {
        "generated_at": ISO timestamp,
        "days_window": int,
        "total_waste_analyzed": float,   # total spend of zero-conversion terms
        "unigrams": [
            {
                "token": str,
                "total_waste": float,
                "distinct_terms": int,
                "total_clicks": int,
                "example_terms": [str, ...],   # up to 3
            },
            ...
        ],
        "bigrams": [ same shape ],
    }
    """
    from database import _conn  # local import

    try:
        with _conn() as conn:
            rows = conn.execute("""
                SELECT search_term, SUM(cost) AS total_cost,
                       SUM(clicks) AS total_clicks,
                       SUM(conversions) AS total_conversions,
                       GROUP_CONCAT(DISTINCT campaign_name) AS campaigns
                FROM gads_search_terms_cache
                WHERE days = ?
                GROUP BY search_term
                HAVING total_conversions = 0
                   AND total_cost > 0
                ORDER BY total_cost DESC
            """, (days,)).fetchall()
    except Exception as e:
        logger.error(f"ngram_analysis: DB query failed: {e}")
        return _empty_result(days)

    if not rows:
        logger.info("ngram_analysis: no non-converting search terms found")
        return _empty_result(days)

    total_waste = sum(r[1] for r in rows)

    # Aggregate waste/clicks per token
    uni_waste:   defaultdict[str, float] = defaultdict(float)
    uni_clicks:  defaultdict[str, int]   = defaultdict(int)
    uni_terms:   defaultdict[str, set]   = defaultdict(set)

    bi_waste:    defaultdict[str, float] = defaultdict(float)
    bi_clicks:   defaultdict[str, int]   = defaultdict(int)
    bi_terms:    defaultdict[str, set]   = defaultdict(set)

    for row in rows:
        search_term, cost, clicks, _conv, _camps = row
        tokens = _tokenise(search_term)
        # filtered tokens for unigrams (remove stopwords)
        filtered = [t for t in tokens if t not in _STOPWORDS]

        cost   = float(cost or 0)
        clicks = int(clicks or 0)

        for tok in filtered:
            uni_waste[tok]  += cost
            uni_clicks[tok] += clicks
            uni_terms[tok].add(search_term)

        # bigrams use all tokens (not filtered) for phrase coherence
        for bg in _ngrams_from_tokens(tokens, 2):
            bi_waste[bg]  += cost
            bi_clicks[bg] += clicks
            bi_terms[bg].add(search_term)

    # Build candidate lists
    unigrams = []
    for tok, waste in sorted(uni_waste.items(), key=lambda x: -x[1]):
        if tok in _NEVER_NEGATIVE_UNIGRAMS:
            continue
        distinct = len(uni_terms[tok])
        if distinct < MIN_TERMS or waste < MIN_WASTE:
            continue
        unigrams.append({
            "token":        tok,
            "total_waste":  round(waste, 2),
            "distinct_terms": distinct,
            "total_clicks": uni_clicks[tok],
            "example_terms": sorted(uni_terms[tok], key=lambda t: -uni_waste.get(t, 0))[:3],
        })

    bigrams = []
    for bg, waste in sorted(bi_waste.items(), key=lambda x: -x[1]):
        # Skip bigrams whose both tokens are stopwords
        parts = bg.split()
        if all(p in _STOPWORDS for p in parts):
            continue
        distinct = len(bi_terms[bg])
        if distinct < MIN_TERMS or waste < MIN_WASTE:
            continue
        bigrams.append({
            "token":        bg,
            "total_waste":  round(waste, 2),
            "distinct_terms": distinct,
            "total_clicks": bi_clicks[bg],
            "example_terms": sorted(bi_terms[bg], key=lambda t: -bi_waste.get(t, 0))[:3],
        })

    from datetime import datetime, timezone
    return {
        "generated_at":        datetime.now(timezone.utc).isoformat(),
        "days_window":         days,
        "total_waste_analyzed": round(total_waste, 2),
        "total_non_converting_terms": len(rows),
        "unigrams": unigrams,
        "bigrams":  bigrams,
    }


def get_ngram_waste_for_prompt(days: int = MIN_DAYS, top_n: int = 20) -> list[dict]:
    """
    Return top_n combined N-gram candidates (unigrams + bigrams),
    sorted by waste descending.  Deduplicated: if a unigram is already
    covered by a higher-waste bigram containing it, the unigram is
    deprioritised (but not removed — Claude decides action).

    Each entry: {"token", "n", "total_waste", "distinct_terms", "example_terms"}
    """
    result = compute_ngram_waste(days=days)
    combined = []
    for item in result.get("unigrams", []):
        combined.append({**item, "n": 1})
    for item in result.get("bigrams", []):
        combined.append({**item, "n": 2})

    combined.sort(key=lambda x: -x["total_waste"])
    return combined[:top_n]


# ── Convenience: summary text for prompt ──────────────────────────────────────

def ngram_waste_summary_text(days: int = MIN_DAYS, top_n: int = 15) -> str:
    """
    Return a compact plain-text summary suitable for embedding directly
    in a Claude prompt section.  Format:

      N-GRAM WASTE SIGNALS (30-day window, $XXX total non-converting spend):
      Token           | Type    | Waste  | Terms | Examples
      ...
    """
    result = compute_ngram_waste(days=days)
    total_waste = result.get("total_waste_analyzed", 0)
    total_terms = result.get("total_non_converting_terms", 0)

    candidates = get_ngram_waste_for_prompt(days=days, top_n=top_n)
    if not candidates:
        return (
            f"N-GRAM WASTE SIGNALS ({days}-day window): "
            f"No significant patterns found (${total_waste:.2f} across {total_terms} terms)."
        )

    lines = [
        f"N-GRAM WASTE SIGNALS ({days}-day window, ${total_waste:.2f} total non-converting spend, "
        f"{total_terms} zero-conversion terms):",
        f"{'Token':<30} | {'Type':<8} | {'Waste':>7} | {'Terms':>5} | Examples",
        "-" * 80,
    ]
    for c in candidates:
        gram_type = f"{c['n']}-gram"
        examples  = ", ".join(c.get("example_terms", [])[:2])
        lines.append(
            f"{c['token']:<30} | {gram_type:<8} | ${c['total_waste']:>6.2f} | "
            f"{c['distinct_terms']:>5} | {examples}"
        )

    lines.append("")
    lines.append(
        "INTERPRETATION: Each row shows a token or phrase that appears across multiple "
        "non-converting search terms. High waste + many distinct terms = strong broad-match "
        "negative candidate. Review 'example_terms' to confirm intent before adding negatives."
    )
    return "\n".join(lines)


# ── Empty result helper ───────────────────────────────────────────────────────

def _empty_result(days: int) -> dict:
    from datetime import datetime, timezone
    return {
        "generated_at":        datetime.now(timezone.utc).isoformat(),
        "days_window":         days,
        "total_waste_analyzed": 0.0,
        "total_non_converting_terms": 0,
        "unigrams": [],
        "bigrams":  [],
    }


# ── CLI test runner ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys, os
    sys.path.insert(0, os.path.dirname(__file__))
    print(ngram_waste_summary_text())
