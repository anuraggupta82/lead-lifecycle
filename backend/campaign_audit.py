"""
Campaign Audit — orchestration layer for all Google Ads write audit logging.

All SQL lives in database.py (consistent with codebase convention).
This module handles:
  - UUID generation
  - JSON serialization of before/after state
  - Two-step audit pattern: log_pending() → mark_executed()
  - Convenience wrappers for common op types

The two-step pattern:
  1. log_pending(...) → inserts execution_result='pending_approval', returns action_id
  2. mark_executed(action_id, success=True/False) → updates row after Google Ads call

This guarantees crash-safe audit: if the process dies between step 1 and 2,
a 'pending_approval' row older than 48h is auto-expired by the janitor job.
"""

import json
import uuid
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_pending(
    operation: str,
    entity_type: str,
    entity_id: str,
    entity_name: str,
    before_state: dict,
    after_state: dict,
    optimizer_run_id: str = "",
    actor: str = "ai_optimizer",
    reason: str = "",
    campaign_id: str = "",
    campaign_name: str = "",
    priority: int = 50,
    impact_estimate: dict | None = None,
) -> str:
    """
    Step 1: Log a recommended action as pending_approval.
    Returns the action_id (UUID) for the frontend Apply button.

    priority: 0=critical, 50=normal, 100=cosmetic (lower = shown first)
    impact_estimate: optional dict with keys like savings_30d_usd, leads_recovered
    """
    from database import log_gads_action
    import json as _json
    action_id = str(uuid.uuid4())
    log_gads_action(
        action_id=action_id,
        operation=operation,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_name=entity_name,
        before_state_json=json.dumps(before_state),
        after_state_json=json.dumps(after_state),
        executed=False,
        execution_result="pending_approval",
        actor=actor,
        reason=reason,
        error_detail="",
        optimizer_run_id=optimizer_run_id,
        campaign_id=campaign_id,
        campaign_name=campaign_name,
        priority=priority,
        impact_estimate_json=_json.dumps(impact_estimate or {}),
    )
    logger.debug(f"Audit pending: {operation} {entity_type} '{entity_name}' → action_id={action_id}")
    return action_id


def log_blocked(
    operation: str,
    entity_type: str,
    entity_id: str,
    entity_name: str,
    before_state: dict,
    after_state: dict,
    reason: str = "",
    error_detail: str = "",
    actor: str = "ai_optimizer",
    optimizer_run_id: str = "",
) -> str:
    """Log an action that was blocked by the kill switch or guardrail (no user action needed)."""
    from database import log_gads_action
    action_id = str(uuid.uuid4())
    log_gads_action(
        action_id=action_id,
        operation=operation,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_name=entity_name,
        before_state_json=json.dumps(before_state),
        after_state_json=json.dumps(after_state),
        executed=False,
        execution_result="blocked",
        actor=actor,
        reason=reason,
        error_detail=error_detail,
        optimizer_run_id=optimizer_run_id,
    )
    logger.info(f"Audit blocked: {operation} '{entity_name}' — {error_detail or reason}")
    return action_id


def mark_executed(action_id: str, success: bool, error_detail: str = "") -> None:
    """
    Step 2: Update audit row after the Google Ads API call returns.
    Called immediately after mutate_ad_group_criteria (or equivalent) returns.
    """
    from database import update_gads_action_result
    execution_result = "success" if success else "error"
    update_gads_action_result(
        action_id=action_id,
        executed=success,
        execution_result=execution_result,
        error_detail=error_detail,
    )
    if success:
        logger.info(f"Audit executed: action_id={action_id} → success")
    else:
        logger.warning(f"Audit executed: action_id={action_id} → error: {error_detail}")


def expire_stale_pending(max_age_hours: int = 48) -> int:
    """
    Janitor: mark pending_approval rows older than max_age_hours as 'expired'.
    Should be called from the 7AM optimizer job before each run.
    Returns count of rows expired.
    """
    from database import expire_stale_audit_rows
    expired = expire_stale_audit_rows(max_age_hours=max_age_hours)
    if expired > 0:
        logger.info(f"Expired {expired} stale pending audit rows (>{max_age_hours}h old)")
    return expired
