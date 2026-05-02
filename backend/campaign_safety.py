"""
Campaign Safety — guards all Google Ads write operations.

The kill switch is two-layered (per Opus review):
  1. Env-var floor: CAMPAIGN_WRITE_OPS_ENABLED=true must be set in .env
     (default False — safe to deploy without enabling writes)
  2. Runtime DB toggle: settings['gads_writes_enabled'] = 'true'
     Controlled via POST /api/admin/gads/writes-enabled without redeploying.

Both must be True for any write to proceed.

Spend guardrails: max budget change = 25%, never decrease from 0.
Never-automate list: operations that require human eyes, always.
"""

import logging

logger = logging.getLogger(__name__)


class WriteBlockedError(Exception):
    """Raised when a write is attempted while blocked by kill switch or guardrail."""


def check_writes_enabled():
    """
    Raise WriteBlockedError if writes are blocked.
    Checks env-var floor first, then DB runtime toggle.
    """
    from config import get_settings
    from database import get_setting

    settings = get_settings()

    # Layer 1: env-var floor — if False, kills everything regardless of DB toggle
    if not settings.campaign_write_ops_enabled:
        raise WriteBlockedError(
            "CAMPAIGN_WRITE_OPS_ENABLED=false in .env — all Google Ads writes blocked. "
            "Set CAMPAIGN_WRITE_OPS_ENABLED=true to enable."
        )

    # Layer 2: runtime DB toggle — admin can flip this without redeploy
    db_toggle = get_setting("gads_writes_enabled", "false").lower()
    if db_toggle != "true":
        raise WriteBlockedError(
            "Google Ads writes disabled at runtime (gads_writes_enabled=false). "
            "Enable via Admin → Google Ads → Write Controls."
        )


def check_budget_change_safe(current_budget_micros: int, new_budget_micros: int) -> bool:
    """
    Reject budget increases > 25% in a single operation.
    Increases from 0 are always blocked (must be set by human).
    Decreases are always allowed.
    """
    if current_budget_micros == 0:
        # Increasing from zero requires human judgment — block automation
        return False
    if new_budget_micros <= current_budget_micros:
        return True  # decreases always safe
    pct_increase = (new_budget_micros - current_budget_micros) / current_budget_micros
    return pct_increase <= 0.25


def check_proposed_spend_under_cap(campaign_id: str, proposed_daily_budget_micros: int) -> tuple:
    """
    Check proposed daily budget against per-campaign spend cap.
    Returns (allowed: bool, cap_usd: float | None).
    'proposed_daily_budget_micros' is in micros (1 USD = 1,000,000 micros).
    Note: this checks a proposed budget change, not cumulative daily spend.
    """
    from database import get_spend_guardrail
    cap_usd = get_spend_guardrail(campaign_id)
    if cap_usd is None:
        return True, None  # no cap set — allow (audit log captures it)
    cap_micros = int(cap_usd * 1_000_000)
    return proposed_daily_budget_micros <= cap_micros, cap_usd


# Operations that are NEVER automated — require a human to do them manually
NEVER_AUTOMATE = {
    "delete_campaign",
    "delete_ad_group",
    "delete_ad",
    "pause_campaign",      # pausing an entire campaign loses quality score momentum
    "pause_ad_group",      # same reasoning
    "enable_campaign",     # re-enabling a paused campaign requires human judgment
    "change_match_type",   # must delete + recreate; match type changes are structural
}


def check_operation_allowed(operation: str) -> bool:
    """Return True if the operation is allowed to be automated."""
    return operation not in NEVER_AUTOMATE


def get_writes_status() -> dict:
    """Return the current state of the kill switch for display in the admin UI."""
    from config import get_settings
    from database import get_setting

    settings = get_settings()
    env_enabled = settings.campaign_write_ops_enabled
    db_toggle = get_setting("gads_writes_enabled", "false").lower() == "true"

    return {
        "env_floor_enabled": env_enabled,
        "db_runtime_enabled": db_toggle,
        "writes_enabled": env_enabled and db_toggle,
        "blocked_reason": (
            None if (env_enabled and db_toggle) else
            "env_var_disabled" if not env_enabled else
            "runtime_disabled"
        ),
    }
