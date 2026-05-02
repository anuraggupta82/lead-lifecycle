"""
Stop Conditions Engine — Step 10 TCPA compliance.

Handles cancellation of queued follow-up messages when a lead-level event
triggers a stop condition.  All rules are hardcoded here (YAGNI — no DB table).

STOP_RULES maps event_type → frozenset of channels to cancel.
  None  = cancel ALL channels (sms + email)
  set() = cancel nothing (log only)
  {'sms'} / {'email'} = cancel that channel only

Usage
-----
    from stop_engine import handle_event

    handle_event(lead_id, 'sms_stop')       # inbound STOP keyword
    handle_event(lead_id, 'email_unsub')     # unsubscribe link
    handle_event(lead_id, 'booked')          # appointment confirmed
    handle_event(lead_id, 'replied')         # lead replied to a message (log only)
    handle_event(lead_id, 'manual_pause')    # admin paused the lead
    handle_event(lead_id, 'dnd_set')         # admin set DND on all channels
"""
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# channel frozensets
_ALL = None        # cancel all channels
_SMS = frozenset({"sms"})
_EMAIL = frozenset({"email"})
_NONE = frozenset()   # log only — no cancellation

STOP_RULES: dict = {
    "sms_stop":     _SMS,    # Twilio inbound STOP → cancel sms queue rows only
    "email_unsub":  _EMAIL,  # unsubscribe link → cancel email queue rows only
    "booked":       _ALL,    # appointment confirmed → cancel everything
    "manual_pause": _ALL,    # admin manually paused → cancel everything
    "dnd_set":      _ALL,    # admin set DND → cancel everything
    "replied":      _NONE,   # lead replied (interesting signal) → log only
}


def cancel_for_event(lead_id: str, event_type: str, reason: str = "") -> int:
    """
    Cancel queued follow-up rows for lead_id based on the event type.

    Returns the number of queue rows cancelled (0 if log-only event or no
    pending rows existed).
    """
    from database import cancel_queue_rows

    channels_rule = STOP_RULES.get(event_type)

    if channels_rule is _NONE:
        # Log-only event — nothing to cancel
        logger.info(f"stop_engine: event={event_type} lead={lead_id} → log only (no cancellation)")
        return 0

    cancel_reason = reason or event_type

    if channels_rule is _ALL:
        count = cancel_queue_rows(lead_id, channels=None, reason=cancel_reason)
        logger.info(
            f"stop_engine: event={event_type} lead={lead_id} → cancelled {count} queue rows (all channels)"
        )
    else:
        channels_list = list(channels_rule)
        count = cancel_queue_rows(lead_id, channels=channels_list, reason=cancel_reason)
        logger.info(
            f"stop_engine: event={event_type} lead={lead_id} → cancelled {count} queue rows "
            f"(channels={channels_list})"
        )

    return count


def handle_event(lead_id: str, event_type: str, reason: str = "") -> dict:
    """
    Main entry point.  Logs the event to lifecycle_events and cancels queue rows
    per STOP_RULES.

    Returns {"lead_id": ..., "event_type": ..., "cancelled": N}
    """
    from database import add_lead_event

    if event_type not in STOP_RULES:
        logger.warning(f"stop_engine.handle_event: unknown event_type={event_type!r} — ignoring")
        return {"lead_id": lead_id, "event_type": event_type, "cancelled": 0}

    # Log the event into lifecycle_events
    add_lead_event(
        lead_id,
        event_type,
        detail=json.dumps({"reason": reason}) if reason else "",
        source="stop_engine",
    )

    cancelled = cancel_for_event(lead_id, event_type, reason=reason)

    return {
        "lead_id": lead_id,
        "event_type": event_type,
        "cancelled": cancelled,
    }
