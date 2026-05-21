"""
CallRail nightly number sync — PR 1.

sync_callrail_numbers()
  Pulls all trackers from CallRail API and upserts into the local
  callrail_numbers table.  Safe to call multiple times (idempotent).

HIPAA note (Path B — no BAA):
  Recording is DISABLED on all trackers. This function logs a warning
  if it finds any tracker with recording_enabled=True so the operator
  can take corrective action.
"""
import json
import logging
from datetime import datetime, timezone

import callrail_client as cr
from database import _conn

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sync_callrail_numbers() -> dict:
    """
    Fetch all CallRail trackers and upsert into callrail_numbers.

    Returns a summary dict:
      {
        "total": <int>,        # trackers returned by API
        "inserted": <int>,     # new rows created
        "updated": <int>,      # existing rows updated
        "recording_warnings": [<tracker_id>, ...]  # trackers with recording ON
      }
    """
    logger.info("[callrail_sync] starting number sync")
    trackers = cr.get_trackers()
    now = _now_iso()

    inserted = 0
    updated = 0
    recording_warnings: list[str] = []

    with _conn() as conn:
        for t in trackers:
            tracker_id = t.get("id", "")
            if not tracker_id:
                continue

            # Extract the first tracking number (trackers can have multiple but
            # GDC uses one number per tracker)
            tracking_numbers = t.get("tracking_numbers", [])
            phone = tracking_numbers[0] if tracking_numbers else ""

            # Recording flag lives inside call_flow
            call_flow = t.get("call_flow") or {}
            recording_enabled = int(bool(call_flow.get("recording_enabled", False)))
            if recording_enabled:
                recording_warnings.append(tracker_id)
                logger.warning(
                    "[callrail_sync] HIPAA PATH B VIOLATION: recording is ON "
                    "for tracker %s (%s). Disable immediately.",
                    tracker_id, t.get("name", "")
                )

            # Determine source_type from the tracker type field
            source_type = t.get("type", "")

            # Check if row exists
            existing = conn.execute(
                "SELECT id FROM callrail_numbers WHERE callrail_tracker_id = ?",
                (tracker_id,)
            ).fetchone()

            payload = json.dumps(t)

            if existing:
                conn.execute("""
                    UPDATE callrail_numbers SET
                        phone_number        = ?,
                        friendly_name       = ?,
                        forward_to          = ?,
                        whisper_message     = ?,
                        recording_enabled   = ?,
                        status              = ?,
                        source_type         = ?,
                        raw_payload         = ?,
                        updated_at          = ?,
                        last_synced_at      = ?
                    WHERE callrail_tracker_id = ?
                """, (
                    phone,
                    t.get("name", ""),
                    t.get("destination_number", ""),
                    t.get("whisper_message", ""),
                    recording_enabled,
                    t.get("status", "active"),
                    source_type,
                    payload,
                    now,
                    now,
                    tracker_id,
                ))
                updated += 1
            else:
                conn.execute("""
                    INSERT INTO callrail_numbers (
                        callrail_tracker_id,
                        phone_number,
                        friendly_name,
                        forward_to,
                        whisper_message,
                        recording_enabled,
                        status,
                        source_type,
                        raw_payload,
                        created_at,
                        updated_at,
                        last_synced_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    tracker_id,
                    phone,
                    t.get("name", ""),
                    t.get("destination_number", ""),
                    t.get("whisper_message", ""),
                    recording_enabled,
                    t.get("status", "active"),
                    source_type,
                    payload,
                    now,
                    now,
                    now,
                ))
                inserted += 1

    summary = {
        "total": len(trackers),
        "inserted": inserted,
        "updated": updated,
        "recording_warnings": recording_warnings,
        "synced_at": now,
    }
    logger.info("[callrail_sync] sync complete: %s", summary)
    return summary
