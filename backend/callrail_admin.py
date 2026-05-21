"""
CallRail PR 2 — admin helpers for the Tracking Numbers UI.

Functions:
  list_numbers_with_stats()  → table data including 30-day call counts
  assign_number(id, payload) → validate + DB write + CallRail API push
  reconcile_with_callrail()  → drift report between local DB and CallRail API

HIPAA note: recording_enabled is never writable via this module (Path B).
"""
import json
import logging
import re
from datetime import datetime, timezone

import callrail_client as cr
from database import _conn

logger = logging.getLogger(__name__)

_E164_RE = re.compile(r"^\+1\d{10}$")
_VALID_ASSIGNMENT_TYPES = {"unassigned", "gads_campaign", "gads_call_extension", "static_source", "pool"}

# CallRail status vocab → our DB vocab and back
_CR_STATUS_TO_DB = {"active": "active", "disabled": "paused", "paused": "paused"}
_DB_STATUS_TO_CR = {"active": "active", "paused": "disabled"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── List ──────────────────────────────────────────────────────────────────────

def list_numbers_with_stats() -> list[dict]:
    """
    Return all callrail_numbers rows enriched with:
      - assigned_campaign_name (from campaigns JOIN)
      - calls_30d (count from callrail_calls)
    """
    sql = """
        SELECT
            cn.id,
            cn.callrail_tracker_id,
            cn.phone_number,
            cn.friendly_name,
            cn.assignment_type,
            cn.assigned_campaign_id,
            c.campaign_name        AS assigned_campaign_name,
            cn.assigned_call_extension_id,
            cn.static_source_label,
            cn.forward_to,
            cn.whisper_message,
            cn.recording_enabled,
            cn.status,
            cn.source_type,
            cn.last_synced_at,
            cn.updated_at,
            (
                SELECT COUNT(*)
                FROM callrail_calls cc
                WHERE cc.tracking_number_id = cn.id
                  AND cc.called_at >= datetime('now', '-30 days')
            ) AS calls_30d
        FROM callrail_numbers cn
        LEFT JOIN campaigns c ON c.id = cn.assigned_campaign_id
        ORDER BY cn.status ASC, cn.friendly_name ASC
    """
    with _conn() as conn:
        rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def get_number_detail(number_id: int) -> dict | None:
    """Return a single callrail_numbers row by primary key (no call stats needed for modal)."""
    sql = """
        SELECT
            cn.*,
            c.campaign_name AS assigned_campaign_name
        FROM callrail_numbers cn
        LEFT JOIN campaigns c ON c.id = cn.assigned_campaign_id
        WHERE cn.id = ?
    """
    with _conn() as conn:
        row = conn.execute(sql, (number_id,)).fetchone()
    return dict(row) if row else None


def list_campaigns_for_assignment() -> list[dict]:
    """Return campaigns eligible for tracking number assignment (non-archived)."""
    sql = """
        SELECT id, campaign_id, campaign_name, status, gads_campaign_resource
        FROM campaigns
        WHERE status NOT IN ('ARCHIVED', 'COMPLETED')
        ORDER BY
            CASE status WHEN 'ACTIVE' THEN 0 WHEN 'PAUSED' THEN 1 ELSE 2 END,
            campaign_name
    """
    with _conn() as conn:
        rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


# ── Assign ────────────────────────────────────────────────────────────────────

def assign_number(number_id: int, payload: dict) -> dict:
    """
    Validate payload, update DB, and push destination_number + whisper to CallRail.

    Payload fields:
      assignment_type      str  required — one of _VALID_ASSIGNMENT_TYPES
      assigned_campaign_id int  required when assignment_type == 'gads_campaign'
      assigned_call_extension_id str  optional, stored for PR 3 use
      static_source_label  str  required when assignment_type == 'static_source'
      forward_to           str  required — E.164 (+1XXXXXXXXXX)
      whisper_message      str  optional

    Returns the updated row dict.
    Raises ValueError on validation failure.
    Raises RuntimeError if CallRail API push fails (DB is NOT written in that case).
    """
    assignment_type = payload.get("assignment_type", "")
    if assignment_type not in _VALID_ASSIGNMENT_TYPES:
        raise ValueError(f"assignment_type must be one of {_VALID_ASSIGNMENT_TYPES}")

    forward_to = (payload.get("forward_to") or "").strip()
    if not _E164_RE.match(forward_to):
        raise ValueError(f"forward_to must be E.164 US format (+1XXXXXXXXXX), got: {forward_to!r}")

    whisper_message = (payload.get("whisper_message") or "").strip()
    word_count = len(whisper_message.split()) if whisper_message else 0
    if word_count > 25:
        logger.warning("[callrail_admin] whisper_message is %d words (>25); CallRail may truncate", word_count)

    # Type-specific validation — each branch explicitly resets slots that don't apply
    # (Bug #1 fix: always clear non-applicable slots regardless of payload content)
    assigned_campaign_id = None
    assigned_call_extension_id = ""
    static_source_label = ""

    if assignment_type == "gads_campaign":
        raw_id = payload.get("assigned_campaign_id")
        if not raw_id:
            raise ValueError("assigned_campaign_id required when assignment_type is gads_campaign")
        assigned_campaign_id = int(raw_id)
        # Verify campaign exists
        with _conn() as conn:
            camp = conn.execute("SELECT id FROM campaigns WHERE id = ?", (assigned_campaign_id,)).fetchone()
        if not camp:
            raise ValueError(f"Campaign id={assigned_campaign_id} not found")

    elif assignment_type == "gads_call_extension":
        assigned_call_extension_id = (payload.get("assigned_call_extension_id") or "").strip()

    elif assignment_type == "static_source":
        static_source_label = (payload.get("static_source_label") or "").strip()
        if not static_source_label:
            raise ValueError("static_source_label required when assignment_type is static_source")

    # pool and unassigned: all slots already cleared above

    # Fetch the tracker_id BEFORE writing anything
    with _conn() as conn:
        row = conn.execute(
            "SELECT callrail_tracker_id, forward_to AS old_forward_to FROM callrail_numbers WHERE id = ?",
            (number_id,)
        ).fetchone()
    if not row:
        raise ValueError(f"callrail_numbers id={number_id} not found")

    tracker_id = row["callrail_tracker_id"]
    old_forward_to = row["old_forward_to"]

    # Push to CallRail FIRST — if this fails we don't write DB
    try:
        cr.update_tracker(
            tracker_id,
            destination_number=forward_to,
            whisper_message=whisper_message,
        )
    except Exception as e:
        raise RuntimeError(f"CallRail API push failed: {e}") from e

    if old_forward_to != forward_to:
        logger.info("[callrail_admin] forward_to changed for tracker %s: %s → %s", tracker_id, old_forward_to, forward_to)

    # DB write after successful API push
    now = _now_iso()
    with _conn() as conn:
        conn.execute("""
            UPDATE callrail_numbers SET
                assignment_type             = ?,
                assigned_campaign_id        = ?,
                assigned_call_extension_id  = ?,
                static_source_label         = ?,
                forward_to                  = ?,
                whisper_message             = ?,
                updated_at                  = ?
            WHERE id = ?
        """, (
            assignment_type,
            assigned_campaign_id,
            assigned_call_extension_id,
            static_source_label,
            forward_to,
            whisper_message,
            now,
            number_id,
        ))

    logger.info("[callrail_admin] assigned number id=%d tracker=%s type=%s", number_id, tracker_id, assignment_type)
    return get_number_detail(number_id)


# ── Status toggle ─────────────────────────────────────────────────────────────

def set_number_status(number_id: int, new_status: str) -> dict:
    """
    Pause or activate a tracking number. Updates DB + pushes to CallRail.
    new_status must be 'active' or 'paused'.
    """
    if new_status not in ("active", "paused"):
        raise ValueError("status must be 'active' or 'paused'")

    with _conn() as conn:
        row = conn.execute(
            "SELECT callrail_tracker_id, status FROM callrail_numbers WHERE id = ?",
            (number_id,)
        ).fetchone()
    if not row:
        raise ValueError(f"callrail_numbers id={number_id} not found")
    # Bug #2 fix: released numbers cannot be toggled (tracker is gone from CallRail)
    if row["status"] == "released":
        raise ValueError("Cannot change status of a released tracking number")

    tracker_id = row["callrail_tracker_id"]
    # Bug #3 fix: CallRail v3 tracker update uses 'disabled' boolean field, not 'status' string
    # disabled=True → paused in our DB; disabled=False → active
    disabled_flag = (new_status == "paused")

    # Push to CallRail first
    try:
        cr.update_tracker(tracker_id, disabled=disabled_flag)
    except Exception as e:
        raise RuntimeError(f"CallRail status push failed: {e}") from e

    now = _now_iso()
    with _conn() as conn:
        conn.execute(
            "UPDATE callrail_numbers SET status = ?, updated_at = ? WHERE id = ?",
            (new_status, now, number_id)
        )

    logger.info("[callrail_admin] status set to %s for tracker %s", new_status, tracker_id)
    return get_number_detail(number_id)


# ── Reconcile ─────────────────────────────────────────────────────────────────

def reconcile_with_callrail() -> dict:
    """
    Compare local DB against live CallRail tracker list.

    Returns:
      {
        "missing_in_callrail": [...],   # DB rows whose tracker_id is gone from CallRail
        "missing_in_db":        [...],  # CallRail trackers not in our DB
        "field_drift":          [...],  # rows present in both but with mismatched fields
        "clean":                bool,
      }

    Fields compared for drift: forward_to, whisper_message, recording_enabled.
    """
    live_trackers = cr.get_trackers()
    live = {t["id"]: t for t in live_trackers}

    with _conn() as conn:
        db_rows = conn.execute("SELECT * FROM callrail_numbers").fetchall()
    db = {r["callrail_tracker_id"]: dict(r) for r in db_rows}

    # Numbers in DB that aren't in CallRail (and not already released)
    missing_in_callrail = [
        {"db_id": r["id"], "tracker_id": tid, "phone_number": r["phone_number"], "friendly_name": r["friendly_name"]}
        for tid, r in db.items()
        if tid not in live and r["status"] != "released"
    ]

    # Numbers in CallRail that aren't in our DB
    missing_in_db = [
        {"tracker_id": tid, "phone_number": (t.get("tracking_numbers") or [""])[0], "friendly_name": t.get("name", "")}
        for tid, t in live.items()
        if tid not in db
    ]

    def _norm_str(v) -> str:
        """Normalize None → '' so null vs empty-string never produces false drift."""
        return "" if v is None else str(v).strip()

    # Field drift for numbers present in both
    field_drift = []
    for tid in set(db) & set(live):
        db_row = db[tid]
        live_t = live[tid]
        live_cf = live_t.get("call_flow") or {}

        # Bug #4 fix: normalize None vs '' before comparison to avoid false positives
        db_rec = int(db_row.get("recording_enabled") or 0)
        cr_rec = int(bool(live_cf.get("recording_enabled", False)))
        checks = [
            ("forward_to",        _norm_str(db_row.get("forward_to")),     _norm_str(live_t.get("destination_number"))),
            ("whisper_message",   _norm_str(db_row.get("whisper_message")), _norm_str(live_t.get("whisper_message"))),
            ("recording_enabled", db_rec,                                   cr_rec),
        ]
        for field, db_val, cr_val in checks:
            if db_val != cr_val:
                field_drift.append({
                    "tracker_id": tid,
                    "phone_number": db_row.get("phone_number", ""),
                    "friendly_name": db_row.get("friendly_name", ""),
                    "field": field,
                    "db_value": db_val,
                    "callrail_value": cr_val,
                })
                # HIPAA check: flag if recording was silently enabled on CallRail
                if field == "recording_enabled" and cr_val == 1:
                    logger.warning(
                        "[callrail_admin] HIPAA PATH B VIOLATION: recording is ON in CallRail "
                        "for tracker %s but OFF in DB. Disable immediately.", tid
                    )

    clean = not (missing_in_callrail or missing_in_db or field_drift)
    logger.info(
        "[callrail_admin] reconcile done — missing_in_cr=%d missing_in_db=%d drift=%d",
        len(missing_in_callrail), len(missing_in_db), len(field_drift)
    )
    return {
        "missing_in_callrail": missing_in_callrail,
        "missing_in_db": missing_in_db,
        "field_drift": field_drift,
        "clean": clean,
    }
