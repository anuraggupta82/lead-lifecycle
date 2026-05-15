"""
Campaign Lifecycle Awareness — single source of truth for age/stage classification.

Stages:
  new      (0–30 days)   — Google learning period; only negatives + advisory allowed
  ramping  (31–90 days)  — building history; tactical changes OK, no strategy switches
  mature   (91+ days)    — full optimization allowed
  unknown               — no date available; treated as 'new' (conservative default)

Learning period (stricter than just age):
  Triggered if stage == new OR (clicks < 100 AND conversions < 15).
  Used by the rule-based engine to suppress bid bumps and keyword pauses.
"""

from datetime import date, datetime
from typing import Optional
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

_ET = ZoneInfo("America/New_York")

# ── Stage constants ────────────────────────────────────────────────────────────
STAGE_NEW     = "new"
STAGE_RAMPING = "ramping"
STAGE_MATURE  = "mature"
STAGE_UNKNOWN = "unknown"

NEW_MAX_DAYS     = 30
RAMPING_MAX_DAYS = 90

# Thresholds for "still in learning" even if age > 30d
LEARNING_MIN_CLICKS = 100
LEARNING_MIN_CONV   = 15


def today_et() -> date:
    """Return today's date in America/New_York (matches how the rest of the system thinks about days)."""
    return datetime.now(tz=_ET).date()


def compute_days_since_launch(launch_date, today: Optional[date] = None) -> Optional[int]:
    """
    Compute days since launch from any reasonable input:
      - None / empty string → None
      - date object → used directly
      - datetime object → .date() extracted
      - str 'YYYY-MM-DD' or ISO-8601 with T/Z suffix → parsed to date

    Returns max(delta, 0) to clamp future-dated launches to 0.
    Returns None when input cannot be parsed.
    """
    if not launch_date:
        return None
    if isinstance(launch_date, str):
        raw = launch_date.strip()
        if not raw:
            return None
        # Accept 'YYYY-MM-DD', 'YYYY-MM-DDThh:mm:ss', 'YYYY-MM-DDThh:mm:ssZ', etc.
        try:
            launch_date = datetime.strptime(raw[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    elif isinstance(launch_date, datetime):
        launch_date = launch_date.date()
    elif not isinstance(launch_date, date):
        return None

    ref = today or today_et()
    return max((ref - launch_date).days, 0)


def classify_stage(days: Optional[int]) -> str:
    """Classify a campaign's lifecycle stage from its age in days."""
    if days is None:
        return STAGE_UNKNOWN
    if days <= NEW_MAX_DAYS:
        return STAGE_NEW
    if days <= RAMPING_MAX_DAYS:
        return STAGE_RAMPING
    return STAGE_MATURE


def build_lifecycle_block(
    launch_date,
    first_impression_date=None,
    conversions_30d: float = 0,
    clicks_30d: int = 0,
) -> dict:
    """
    Build the lifecycle context dict that is injected into the per-campaign
    Claude context and used by the rule-based engine.

    launch_date            — from campaigns.launch_date (DB); can be None
    first_impression_date  — fallback from GAds first segment date; can be None
    conversions_30d        — from campaign_stats (used for in_learning_period heuristic)
    clicks_30d             — from campaign_stats (used for in_learning_period heuristic)

    Returns:
    {
        "days_since_launch": int or None,
        "stage": "new"|"ramping"|"mature"|"unknown",
        "source": "db_launch_date"|"gads_first_impression"|"none",
        "in_learning_period": bool,
        "thresholds": { ... }
    }
    """
    days = compute_days_since_launch(launch_date)
    source = "db_launch_date"

    if days is None:
        days = compute_days_since_launch(first_impression_date)
        source = "gads_first_impression" if days is not None else "none"

    stage = classify_stage(days)

    # in_learning_period is True when:
    #   - stage is new (age ≤ 30d), OR
    #   - stage is unknown (no date — conservative), OR
    #   - still in ramping AND thin on BOTH clicks AND conversions
    #   NOTE: volume check is intentionally restricted to non-mature stages.
    #   Mature campaigns (91d+) have made their choices; low volume at maturity
    #   means the campaign itself is underperforming, not still learning.
    #   Also: OR between click/conv (not AND) — either thin signal is enough.
    _thin_signal = (
        int(clicks_30d or 0) < LEARNING_MIN_CLICKS
        or float(conversions_30d or 0) < LEARNING_MIN_CONV
    )
    in_learning = (
        stage in (STAGE_NEW, STAGE_UNKNOWN)
        or (stage == STAGE_RAMPING and _thin_signal)
    )

    return {
        "days_since_launch": days,
        "stage": stage,
        "source": source,
        "in_learning_period": in_learning,
        "thresholds": {
            "new_max_days": NEW_MAX_DAYS,
            "ramping_max_days": RAMPING_MAX_DAYS,
            "learning_min_clicks": LEARNING_MIN_CLICKS,
            "learning_min_conv": LEARNING_MIN_CONV,
        },
    }


# ── Quick self-test (python lifecycle.py) ────────────────────────────────────
if __name__ == "__main__":
    from datetime import timedelta

    ref = date(2026, 5, 15)

    cases = [
        ("5 days ago",   ref - timedelta(days=5),   STAGE_NEW),
        ("30 days ago",  ref - timedelta(days=30),  STAGE_NEW),
        ("31 days ago",  ref - timedelta(days=31),  STAGE_RAMPING),
        ("90 days ago",  ref - timedelta(days=90),  STAGE_RAMPING),
        ("91 days ago",  ref - timedelta(days=91),  STAGE_MATURE),
        ("None",         None,                       STAGE_UNKNOWN),
        ("empty str",    "",                         STAGE_UNKNOWN),
        ("future",       ref + timedelta(days=3),    STAGE_NEW),    # clamped to 0 → new
        ("ISO string",   "2026-04-01T00:00:00Z",     STAGE_RAMPING),
    ]

    all_ok = True
    for label, ld, expected in cases:
        days = compute_days_since_launch(ld, today=ref)
        stage = classify_stage(days)
        ok = stage == expected
        all_ok = all_ok and ok
        print(f"{'✅' if ok else '❌'} {label:20s} → days={days}, stage={stage} (expected {expected})")

    block = build_lifecycle_block(
        launch_date="2026-04-20",
        conversions_30d=3,
        clicks_30d=50,
    )
    print(f"\nbuild_lifecycle_block('2026-04-20', conv=3, clicks=50):")
    for k, v in block.items():
        print(f"  {k}: {v}")

    print(f"\nAll tests {'PASSED' if all_ok else 'FAILED'}")
