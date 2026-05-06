"""
AI cost tracking helpers.

Thin service layer on top of database.insert_ai_usage / get_ai_cost_summary.
Import this module — not database directly — for anything cost-related.

Pricing constants mirror the standalone Mango Call Analysis app (core.py)
and are updated here as provider rates change.

APIs tracked
------------
openai   – Whisper transcription (audio → text)
gemini   – Gemini summarisation + grading
claude   – Anthropic Claude (campaign strategy, ad copy, optimizer, etc.)
"""

from __future__ import annotations

# ── Pricing constants (USD) ───────────────────────────────────────────────────

# OpenAI Whisper: $0.006 / minute of audio
WHISPER_COST_PER_MIN: float = 0.006

# Google Gemini 2.5 Flash  (input / output per 1 M tokens — standard tier)
GEMINI_IN_PER_M_TOKENS: float = 0.075
GEMINI_OUT_PER_M_TOKENS: float = 0.300

# Anthropic Claude Sonnet 4.x  ($3 / 1 M input tokens, $15 / 1 M output tokens)
CLAUDE_IN_PER_M_TOKENS: float = 3.00
CLAUDE_OUT_PER_M_TOKENS: float = 15.00

# ── Cost calculators ──────────────────────────────────────────────────────────


def whisper_cost(duration_seconds: float) -> float:
    """Return USD cost for transcribing `duration_seconds` of audio."""
    return round((duration_seconds / 60.0) * WHISPER_COST_PER_MIN, 6)


def gemini_cost(input_tokens: int, output_tokens: int) -> float:
    """Return USD cost for a Gemini API call (standard tier, no cache)."""
    cost = (input_tokens / 1_000_000) * GEMINI_IN_PER_M_TOKENS
    cost += (output_tokens / 1_000_000) * GEMINI_OUT_PER_M_TOKENS
    return round(cost, 6)


def claude_cost(input_tokens: int, output_tokens: int) -> float:
    """Return USD cost for a Claude API call."""
    cost = (input_tokens / 1_000_000) * CLAUDE_IN_PER_M_TOKENS
    cost += (output_tokens / 1_000_000) * CLAUDE_OUT_PER_M_TOKENS
    return round(cost, 6)


# ── Logging wrappers ──────────────────────────────────────────────────────────

def log_ai_usage(
    *,
    api: str,
    model: str,
    purpose: str,
    cost_usd: float,
    input_tokens: int = 0,
    output_tokens: int = 0,
    minutes: float = 0.0,
    call_id: str = "",
    lead_id: str = "",
    campaign_id: str = "",
    optimizer_run_id: str = "",
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    request_ms: int = 0,
    error: str = "",
) -> None:
    """
    Persist one AI usage record to the database.

    Parameters
    ----------
    api              : 'openai' | 'gemini' | 'claude'
    model            : model string, e.g. 'whisper-1', 'gemini-2.5-flash', 'claude-sonnet-4-5'
    purpose          : free-form tag — e.g. 'transcription', 'call_summary', 'call_grade',
                       'optimizer_run', 'campaign_strategy', 'campaign_build', 'ad_copy'
    cost_usd         : pre-calculated cost (use helpers above)
    input_tokens     : token count (0 for Whisper)
    output_tokens    : token count (0 for Whisper)
    minutes          : audio duration in minutes (Whisper only; 0 for token-based APIs)
    call_id          : mango call_id if triggered by a specific call
    lead_id          : pipeline lead ID if linked (use "" not None)
    campaign_id      : Google Ads campaign ID if linked (use "" not None)
    optimizer_run_id : optimizer run ID if applicable
    cache_read_tokens : Gemini cached-input tokens (cheaper tier)
    cache_write_tokens: Gemini cache-write tokens
    request_ms       : round-trip latency in milliseconds
    error            : error message if the call failed (still log the attempt)
    """
    from database import insert_ai_usage  # local import to avoid circular deps

    insert_ai_usage(
        api=api,
        model=model,
        purpose=purpose,
        cost_usd=cost_usd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        minutes=minutes,
        call_id=call_id,
        lead_id=lead_id,
        campaign_id=campaign_id,
        optimizer_run_id=optimizer_run_id,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        request_ms=request_ms,
        error=error,
    )


def log_whisper(
    *,
    duration_seconds: float,
    model: str = "whisper-1",
    call_id: str = "",
    lead_id: str = "",
) -> float:
    """Log a Whisper transcription and return the cost."""
    minutes = duration_seconds / 60.0
    cost = whisper_cost(duration_seconds)
    log_ai_usage(
        api="openai",
        model=model,
        purpose="transcription",
        cost_usd=cost,
        minutes=minutes,
        call_id=call_id,
        lead_id=lead_id,
    )
    return cost


def log_gemini(
    *,
    purpose: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    call_id: str = "",
    lead_id: str = "",
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    request_ms: int = 0,
) -> float:
    """Log a Gemini API call and return the cost."""
    cost = gemini_cost(input_tokens, output_tokens)
    log_ai_usage(
        api="gemini",
        model=model,
        purpose=purpose,
        cost_usd=cost,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        call_id=call_id,
        lead_id=lead_id,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        request_ms=request_ms,
    )
    return cost


def log_claude(
    *,
    purpose: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    campaign_id: str = "",
    lead_id: str = "",
    optimizer_run_id: str = "",
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    request_ms: int = 0,
) -> float:
    """Log a Claude API call and return the cost."""
    cost = claude_cost(input_tokens, output_tokens)
    log_ai_usage(
        api="claude",
        model=model,
        purpose=purpose,
        cost_usd=cost,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        campaign_id=campaign_id,
        lead_id=lead_id,
        optimizer_run_id=optimizer_run_id,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        request_ms=request_ms,
    )
    return cost


# ── Summary accessor ──────────────────────────────────────────────────────────

def cost_summary(days: int = 30) -> dict:
    """
    Return aggregated AI spend for the last `days` days plus lifetime totals.

    Returns the dict produced by database.get_ai_cost_summary:
    {
        "window_days": int,
        "window": {
            "total_cost": float,
            "total_minutes": float,
            "total_tokens": int,
            "by_api":     { api: {"cost": float, "mins": float, "tokens": int, "calls": int} },
            "by_model":   [ {"model", "api", "cost", "calls"} ],
            "by_purpose": [ {"purpose", "api", "cost", "calls"} ],
            "daily":      [ {"date", "api", "cost"} ],
        },
        "lifetime": { ... same shape ... }
    }
    """
    from database import get_ai_cost_summary  # local import to avoid circular deps

    return get_ai_cost_summary(days=days)
