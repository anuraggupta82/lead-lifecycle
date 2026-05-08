"""
Campaign Safety — guardrails for Google Ads write operations.

Kill switch removed (May 2026) — writes are always enabled.
Spend guardrails and never-automate list are kept as-is.
"""

import logging

logger = logging.getLogger(__name__)


class WriteBlockedError(Exception):
    """Raised when a write is blocked by a guardrail (not a kill switch)."""


def check_writes_enabled():
    """No-op — kill switch removed. Writes are always permitted."""
    pass


def check_budget_change_safe(current_budget_micros: int, new_budget_micros: int) -> bool:
    """
    Reject budget increases > 25% in a single operation.
    Increases from 0 are always blocked (must be set by human).
    Decreases are always allowed.
    """
    if current_budget_micros == 0:
        return False
    if new_budget_micros <= current_budget_micros:
        return True
    pct_increase = (new_budget_micros - current_budget_micros) / current_budget_micros
    return pct_increase <= 0.25


def check_proposed_spend_under_cap(campaign_id: str, proposed_daily_budget_micros: int) -> tuple:
    """
    Check proposed daily budget against per-campaign spend cap.
    Returns (allowed: bool, cap_usd: float | None).
    """
    from database import get_spend_guardrail
    cap_usd = get_spend_guardrail(campaign_id)
    if cap_usd is None:
        return True, None
    cap_micros = int(cap_usd * 1_000_000)
    return proposed_daily_budget_micros <= cap_micros, cap_usd


# Budget absolute limits — hard floor/ceiling for any automated budget change
_MIN_DAILY_BUDGET_MICROS = 5_000_000      # $5.00/day — Google minimum
_MAX_DAILY_BUDGET_MICROS = 500_000_000    # $500.00/day — GDC hard ceiling


def check_budget_absolute_limits(new_budget_micros: int) -> None:
    """
    Raise WriteBlockedError if the proposed budget is outside absolute limits.
    Call this before check_budget_change_safe.
    """
    if new_budget_micros < _MIN_DAILY_BUDGET_MICROS:
        raise WriteBlockedError(
            f"Proposed budget ${new_budget_micros/1_000_000:.2f}/day is below "
            f"minimum ${_MIN_DAILY_BUDGET_MICROS/1_000_000:.2f}/day"
        )
    if new_budget_micros > _MAX_DAILY_BUDGET_MICROS:
        raise WriteBlockedError(
            f"Proposed budget ${new_budget_micros/1_000_000:.2f}/day exceeds "
            f"maximum ${_MAX_DAILY_BUDGET_MICROS/1_000_000:.2f}/day"
        )


# Operations that are NEVER automated — require a human to do them manually
NEVER_AUTOMATE = {
    "delete_campaign",
    "delete_ad_group",
    "delete_ad",
    "pause_campaign",
    "pause_ad_group",
    "enable_campaign",
    "change_match_type",
    "ad_schedule",           # Complex time-of-day bidding — human judgment required
}


def check_operation_allowed(operation: str) -> bool:
    """Return True if the operation is allowed to be automated."""
    return operation not in NEVER_AUTOMATE


def get_writes_status() -> dict:
    """Always enabled — kill switch removed."""
    return {
        "env_floor_enabled": True,
        "db_runtime_enabled": True,
        "writes_enabled": True,
        "blocked_reason": None,
    }
