"""
CallRail sync — PR 1 (number sync) + PR 6 (call polling).

sync_callrail_numbers()
  Pulls all trackers from CallRail API and upserts into callrail_numbers.
  Runs nightly at 1 AM. Safe to call multiple times (idempotent).

sync_callrail_calls()
  Polls CallRail for new completed calls and runs each through
  process_webhook() for lead creation / attribution.  Runs every 15 min.
  Uses a cursor in the settings table so each run only fetches new calls.
  Idempotent — the _upsert_callrail_call layer ignores duplicates.

HIPAA note (Path B — no BAA):
  Recording is DISABLED on all trackers. Do NOT re-enable without signing
  the HIPAA BAA first (support.callrail.com).
"""
import json
import logging
import time
from datetime import datetime, timezone, timedelta

import callrail_client as cr
from database import _conn, get_setting, save_setting

logger = logging.getLogger(__name__)

# ── PR 6 — call-polling constants ─────────────────────────────────────────────
_CURSOR_KEY            = "callrail_calls_last_sync"   # ISO 8601, UTC
_OVERLAP_MINUTES       = 5      # re-pull this many minutes before cursor to catch late records
_INITIAL_LOOKBACK_HOURS = 168   # 7 days on first run (account just went live)


def _parse_iso(s: str) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, normalising Z → +00:00."""
    if not s:
        return None
    try:
        s2 = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s2)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _fmt_callrail(dt: datetime) -> str:
    """
    Format a datetime for the CallRail API.
    CallRail expects: 2026-05-01T00:00:00-04:00  (no microseconds, named offset).
    We send UTC so the offset is always -00:00, but CallRail is happier with
    the numeric form without microseconds.
    """
    # Strip microseconds, keep the UTC offset as +00:00
    return dt.replace(microsecond=0).isoformat()


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


# ── PR 6 — Call polling ───────────────────────────────────────────────────────

def sync_callrail_calls() -> dict:
    """
    Poll CallRail API for completed inbound calls and ingest each one via
    process_webhook() for lead creation / attribution tracking.

    Cursor: settings table key 'callrail_calls_last_sync' stores the ISO
    timestamp of the newest call start_time successfully processed.  Each
    run fetches from (cursor - 5 min) to now, so late-arriving call.completed
    events are caught on the next pass.

    On first run (no cursor): fetches the last 7 days (account just went live).

    The ingestion pipeline (process_webhook) is fully idempotent — duplicate
    calls are silently skipped at the DB upsert layer.

    Note: raw_payload stored in callrail_calls will include the synthetic
    event_type="call.completed" field added by this poller, which correctly
    identifies the row as originating from the poll path rather than a webhook.

    Returns a summary dict suitable for logging and the admin API response.
    """
    from callrail_webhook import process_webhook

    t0 = time.time()
    now_utc = datetime.now(timezone.utc)

    cursor_before = (get_setting(_CURSOR_KEY, "") or "").strip()
    cursor_before_dt: datetime | None = _parse_iso(cursor_before)

    # ── Compute fetch window ──────────────────────────────────────────────────
    if cursor_before:
        if cursor_before_dt:
            window_start_dt = cursor_before_dt - timedelta(minutes=_OVERLAP_MINUTES)
        else:
            logger.warning("[callrail_sync] bad cursor %r — falling back to 7d lookback",
                           cursor_before)
            window_start_dt = now_utc - timedelta(hours=_INITIAL_LOOKBACK_HOURS)
    else:
        window_start_dt = now_utc - timedelta(hours=_INITIAL_LOOKBACK_HOURS)

    window_start = _fmt_callrail(window_start_dt)
    window_end   = _fmt_callrail(now_utc)

    summary = {
        "ok":               True,
        "total":            0,
        "created":          0,
        "linked":           0,
        "skipped_existing": 0,
        "skipped_outbound": 0,
        "ingested_no_lead": 0,
        "errors":           0,
        "error_details":    [],
        "window_start":     window_start,
        "window_end":       window_end,
        "cursor_before":    cursor_before,
        "cursor_after":     cursor_before,
        "duration_ms":      0,
    }

    # ── Fetch from API ────────────────────────────────────────────────────────
    try:
        calls = cr.get_calls(
            date_range_start=window_start,
            date_range_end=window_end,
        )
    except Exception as e:
        logger.error("[callrail_sync] get_calls API error: %s", e, exc_info=True)
        summary.update({"ok": False, "errors": 1,
                        "error_details": [f"get_calls: {e}"],
                        "duration_ms": int((time.time() - t0) * 1000)})
        # Do NOT advance cursor — retry on next run
        return summary

    summary["total"] = len(calls)

    # Warn if we hit the page cap — some calls may have been silently dropped
    if len(calls) >= 1000:
        logger.warning(
            "[callrail_sync] fetched %d calls — hit max_pages cap (window %s → %s). "
            "Some calls may be missing. Consider increasing max_pages in get_calls() "
            "or reducing the polling interval.",
            len(calls), window_start, window_end,
        )

    logger.info("[callrail_sync] fetched %d calls (window %s → %s)",
                len(calls), window_start, window_end)

    # ── Process each call ─────────────────────────────────────────────────────
    _action_map = {
        "created":                  "created",
        "linked":                   "linked",
        "skipped_existing_patient": "skipped_existing",
        "skipped_outbound":         "skipped_outbound",
        "ingested_no_lead":         "ingested_no_lead",
    }

    max_seen_dt: datetime | None = cursor_before_dt

    for c in calls:
        try:
            payload = dict(c)   # shallow copy — don't mutate the fetched dict
            payload["event_type"] = "call.completed"
            result = process_webhook(payload, b"")

            if not result.get("ok"):
                summary["errors"] += 1
                if len(summary["error_details"]) < 10:
                    summary["error_details"].append(
                        f"{c.get('id', '?')}: {result.get('errors') or result.get('action')}"
                    )
                continue

            counter_key = _action_map.get(result.get("action", ""))
            if counter_key:
                summary[counter_key] += 1

            # Track newest successfully-processed start_time
            st_dt = _parse_iso(c.get("start_time") or "")
            if st_dt and (max_seen_dt is None or st_dt > max_seen_dt):
                max_seen_dt = st_dt

        except Exception as e:
            summary["errors"] += 1
            if len(summary["error_details"]) < 10:
                summary["error_details"].append(f"{c.get('id', '?')}: {e}")
            logger.error("[callrail_sync] per-call failure id=%s: %s",
                         c.get("id"), e, exc_info=True)

    # ── Advance cursor ────────────────────────────────────────────────────────
    if summary["errors"] == 0:
        if max_seen_dt and (cursor_before_dt is None or max_seen_dt > cursor_before_dt):
            new_cursor = max_seen_dt.isoformat()
            save_setting(_CURSOR_KEY, new_cursor)
            summary["cursor_after"] = new_cursor
        elif not calls:
            # Quiet window, no errors → advance to window_end so next run
            # doesn't re-pull the same empty range.
            save_setting(_CURSOR_KEY, window_end)
            summary["cursor_after"] = window_end
    # If any per-call errors → leave cursor unchanged so the window replays.

    summary["duration_ms"] = int((time.time() - t0) * 1000)
    logger.info("[callrail_sync] sync_callrail_calls done: %s", {
        k: v for k, v in summary.items() if k != "error_details"
    })
    return summary
