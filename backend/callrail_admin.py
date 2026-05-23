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
# UI exposes "unassigned", "gads_campaign", "static_source", "gbp" today.
# "gads_call_extension", "pool" are reserved for future PRs — kept for forward compatibility.
# "gbp" = static number placed on Google Business Profile listing (no GAds push).
_VALID_ASSIGNMENT_TYPES = {"unassigned", "gads_campaign", "gads_call_extension", "static_source", "pool", "gbp"}

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

    # PR 3: push call extension to Google Ads (non-fatal — errors stored in DB row)
    gads_result = _push_to_gads(number_id)
    detail = get_number_detail(number_id)
    detail["gads_push"] = gads_result
    return detail


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

    # PR 3: update Google Ads call extension (remove on pause, restore on activate)
    gads_result = _push_to_gads(number_id)
    detail = get_number_detail(number_id)
    detail["gads_push"] = gads_result
    return detail


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

    # PR 3: GAds drift — numbers marked 'pushed' but missing from GAds
    gads_drift = []
    try:
        from google_ads_extensions import find_call_assets_on_campaign
        pushed_rows = [r for r in db.values()
                       if r.get("gads_push_status") == "pushed"
                       and r.get("assigned_campaign_id")]
        if pushed_rows:
            # Load campaign resources for the pushed rows
            campaign_ids = list({r["assigned_campaign_id"] for r in pushed_rows})
            with _conn() as conn:
                camps = conn.execute(
                    f"SELECT id, gads_campaign_resource, campaign_name FROM campaigns "
                    f"WHERE id IN ({','.join('?' * len(campaign_ids))})",
                    campaign_ids
                ).fetchall()
            camp_map = {c["id"]: dict(c) for c in camps}

            for r in pushed_rows:
                camp = camp_map.get(r["assigned_campaign_id"])
                if not camp or not camp.get("gads_campaign_resource"):
                    continue
                live_assets = find_call_assets_on_campaign(camp["gads_campaign_resource"])
                phone_digits = "".join(ch for ch in (r.get("phone_number") or "") if ch.isdigit())
                stripped = phone_digits[1:] if phone_digits.startswith("1") else phone_digits
                found = any(
                    "".join(ch for ch in a["phone_number"] if ch.isdigit()) in (phone_digits, stripped)
                    for a in live_assets
                )
                if not found:
                    gads_drift.append({
                        "db_id": r["id"],
                        "phone_number": r["phone_number"],
                        "campaign_name": camp["campaign_name"],
                        "expected": "pushed",
                        "actual": "missing_from_gads",
                    })
    except Exception as e:
        logger.warning("[callrail_admin] reconcile gads_drift check failed (non-fatal): %s", e)

    clean = not (missing_in_callrail or missing_in_db or field_drift or gads_drift)
    logger.info(
        "[callrail_admin] reconcile done — missing_in_cr=%d missing_in_db=%d drift=%d gads_drift=%d",
        len(missing_in_callrail), len(missing_in_db), len(field_drift), len(gads_drift)
    )
    return {
        "missing_in_callrail": missing_in_callrail,
        "missing_in_db": missing_in_db,
        "field_drift": field_drift,
        "gads_drift": gads_drift,
        "clean": clean,
    }


# ── Google Ads push helpers (PR 3) ────────────────────────────────────────────

def _push_to_gads(number_id: int) -> dict:
    """
    Push (or remove) the Google Ads call extension based on the current DB
    state of callrail_numbers[number_id].

    Decision matrix:
      released or paused                  → remove call extension from GAds
      unassigned + active                 → remove call extension from GAds
      gads_campaign + active:
        campaign has gads_campaign_resource → push (create or reuse)
        campaign missing resource           → set pending (not yet on GAds)
      other assignment types              → not_applicable (no GAds push)

    Returns status dict and writes result back to DB.
    Never raises — errors are captured in the dict and stored in the DB row.
    """
    result = {
        "status": "not_applicable",
        "asset_resource": "",
        "campaign_asset_resource": "",
        "error": "",
    }

    with _conn() as conn:
        row = conn.execute("SELECT * FROM callrail_numbers WHERE id = ?", (number_id,)).fetchone()
    if not row:
        result["error"] = f"callrail_numbers id={number_id} not found"
        return result
    row = dict(row)

    assignment_type = row.get("assignment_type", "unassigned")
    status          = row.get("status", "active")
    phone           = row.get("phone_number", "")
    prev_camp_asset = row.get("gads_campaign_asset_resource", "")

    # ── Determine action ─────────────────────────────────────────────────────
    should_remove = (
        status in ("released", "paused") or
        assignment_type == "unassigned"
    )
    should_push = (assignment_type == "gads_campaign" and status == "active")

    if not should_remove and not should_push:
        # static_source, pool, gads_call_extension — no GAds push,
        # but if this number was previously pushed we must remove the stale extension.
        if prev_camp_asset:
            assigned_campaign_id = row.get("assigned_campaign_id")
            campaign_resource = ""
            if assigned_campaign_id:
                with _conn() as conn:
                    camp = conn.execute(
                        "SELECT gads_campaign_resource FROM campaigns WHERE id = ?",
                        (assigned_campaign_id,)
                    ).fetchone()
                if camp:
                    campaign_resource = camp["gads_campaign_resource"] or ""
            from google_ads_extensions import remove_call_extension_from_campaign as _rcef
            rm = _rcef(campaign_resource or prev_camp_asset, prev_camp_asset)
            err = "" if rm["ok"] else "; ".join(rm.get("errors") or ["remove failed"])
            _write_gads_status(number_id, "not_applicable", "", "", err)
        else:
            _write_gads_status(number_id, "not_applicable", "", "", "")
        result["status"] = "not_applicable"
        return result

    from google_ads_extensions import push_call_extension_to_campaign, remove_call_extension_from_campaign

    if should_remove:
        # Fetch the campaign resource if we have a stored campaign_asset to remove
        campaign_resource = ""
        if prev_camp_asset:
            # Extract campaign resource from stored campaign_asset_resource if possible,
            # else fall back to looking up from the campaign record
            assigned_campaign_id = row.get("assigned_campaign_id")
            if assigned_campaign_id:
                with _conn() as conn:
                    camp = conn.execute(
                        "SELECT gads_campaign_resource FROM campaigns WHERE id = ?",
                        (assigned_campaign_id,)
                    ).fetchone()
                if camp:
                    campaign_resource = camp["gads_campaign_resource"] or ""

        if campaign_resource or prev_camp_asset:
            # Use prev_camp_asset as fallback for customer_id extraction
            # (campaign_asset resource "customers/N/campaignAssets/..." embeds the ID)
            rm = remove_call_extension_from_campaign(
                campaign_resource or prev_camp_asset,
                prev_camp_asset,
            )
            if rm["ok"]:
                result["status"] = "removed"
                _write_gads_status(number_id, "removed", "", "", "")
                logger.info("[callrail_admin] _push_to_gads: removed call extension for number %d", number_id)
            else:
                err = "; ".join(rm.get("errors") or ["remove failed"])
                result.update({"status": "failed", "error": err})
                _write_gads_status(number_id, "failed", "", "", err)
        else:
            # Nothing stored — nothing to remove
            result["status"] = "removed"
            _write_gads_status(number_id, "removed", "", "", "")
        return result

    # should_push — look up campaign
    assigned_campaign_id = row.get("assigned_campaign_id")
    if not assigned_campaign_id:
        msg = "No campaign assigned — cannot push call extension"
        result.update({"status": "pending", "error": msg})
        _write_gads_status(number_id, "pending", "", "", msg)
        return result

    with _conn() as conn:
        camp = conn.execute(
            "SELECT id, campaign_name, gads_campaign_resource FROM campaigns WHERE id = ?",
            (assigned_campaign_id,)
        ).fetchone()

    if not camp:
        msg = f"Campaign id={assigned_campaign_id} not found in DB"
        result.update({"status": "pending", "error": msg})
        _write_gads_status(number_id, "pending", "", "", msg)
        return result

    campaign_resource = camp["gads_campaign_resource"] or ""
    if not campaign_resource:
        msg = f"Campaign '{camp['campaign_name']}' not yet launched on Google Ads — no campaign resource"
        result.update({"status": "pending", "error": msg})
        _write_gads_status(number_id, "pending", "", "", msg)
        logger.info("[callrail_admin] _push_to_gads pending: %s", msg)
        return result

    # Remove any other numbers on same campaign first (handles campaign re-assignment)
    # Then push this number
    _write_gads_status(number_id, "pending", "", "", "")
    push = push_call_extension_to_campaign(
        campaign_resource=campaign_resource,
        phone_number_e164=phone,
        friendly_name=f"CallRail {phone}",
    )

    if push["ok"]:
        result.update({
            "status": "pushed",
            "asset_resource":          push["asset_resource"],
            "campaign_asset_resource": push["campaign_asset_resource"],
        })
        _write_gads_status(
            number_id, "pushed",
            push["asset_resource"],
            push["campaign_asset_resource"],
            "",
        )
        # Demote any other DB rows that were displaced (pushed same campaign, different number)
        if push.get("removed_old"):
            _demote_displaced_numbers(number_id, assigned_campaign_id, push["removed_old"])

        logger.info("[callrail_admin] _push_to_gads pushed for number %d: %s",
                    number_id, push["campaign_asset_resource"])
    else:
        err = "; ".join(push.get("errors") or ["push failed"])
        result.update({"status": "failed", "error": err})
        _write_gads_status(number_id, "failed", "", "", err)
        logger.error("[callrail_admin] _push_to_gads failed for number %d: %s", number_id, err)

    return result


def _write_gads_status(
    number_id: int,
    status: str,
    asset_resource: str,
    campaign_asset_resource: str,
    error: str,
) -> None:
    """Write GAds push status columns back to the callrail_numbers row."""
    now = _now_iso()
    with _conn() as conn:
        conn.execute("""
            UPDATE callrail_numbers SET
                gads_push_status             = ?,
                gads_call_asset_resource     = ?,
                gads_campaign_asset_resource = ?,
                gads_push_error              = ?,
                gads_push_attempted_at       = ?
            WHERE id = ?
        """, (status, asset_resource, campaign_asset_resource, error, now, number_id))


def _demote_displaced_numbers(
    winner_id: int,
    campaign_id: int,
    removed_campaign_asset_resources: list[str],
) -> None:
    """
    When we removed existing CampaignAssets to make room for the new number,
    mark any DB rows that referenced those resources as 'removed' so they
    don't show stale 'pushed' status.
    """
    if not removed_campaign_asset_resources:
        return
    with _conn() as conn:
        for resource in removed_campaign_asset_resources:
            conn.execute("""
                UPDATE callrail_numbers
                SET gads_push_status = 'removed',
                    gads_push_error  = 'displaced by number_id=' || ?,
                    gads_campaign_asset_resource = ''
                WHERE id != ?
                  AND assigned_campaign_id = ?
                  AND gads_campaign_asset_resource = ?
                  AND gads_push_status = 'pushed'
            """, (winner_id, winner_id, campaign_id, resource))


def retry_gads_push(number_id: int) -> dict:
    """
    Re-attempt the Google Ads call extension push for a number.
    Does NOT touch CallRail or the DB assignment — only re-pushes
    based on current DB state.
    """
    with _conn() as conn:
        row = conn.execute(
            "SELECT id FROM callrail_numbers WHERE id = ?", (number_id,)
        ).fetchone()
    if not row:
        raise ValueError(f"callrail_numbers id={number_id} not found")
    return _push_to_gads(number_id)
