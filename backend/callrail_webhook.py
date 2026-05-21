"""
CallRail PR 4 — Webhook ingestion for inbound call events.

Handles: call.created, call.completed, call.recording_completed, post_call

Flow for each inbound call:
  1. Verify signature (HMAC-SHA256 or query-param secret)
  2. Parse the call object from the webhook envelope
  3. Normalize caller phone → E.164
  4. Look up tracking_number_id by tracking_phone_number
  5. Resolve campaign by name → our campaigns table
  6. Try to match to mango_calls by phone + time ±2 min
  7. Lead resolution: find existing by phone_hash → backfill attrs
                    OR create new if answered/voicemail + call.completed
  8. Upsert callrail_calls row (idempotent on callrail_call_id)

HIPAA Path B: recording_url is NEVER stored (no BAA signed).
"""
import hashlib
import hmac
import json
import logging
import re
import uuid
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

_E164_RE = re.compile(r"^\+1\d{10}$")
_MANGO_MATCH_WINDOW_MINUTES = 2

# Events we accept — others get a 200 + "ignored_event_type"
_VALID_EVENT_TYPES = {
    "call.created",
    "call.completed",
    "call.recording_completed",
    "post_call",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Auth ──────────────────────────────────────────────────────────────────────

def verify_signature(raw_body: bytes, signature_header: str, secret: str) -> bool:
    """
    HMAC-SHA256 verification of the raw request body.
    Accepts bare hex digest or 'sha256=<hex>' from the header.
    Returns True if secret is empty (dev mode — callers must log a warning).
    """
    if not secret:
        return True
    if not signature_header:
        return False
    # Strip optional sha256= prefix
    sig = signature_header
    if sig.startswith("sha256="):
        sig = sig[7:]
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig.lower(), expected.lower())


def verify_query_secret(provided: str, expected: str) -> bool:
    """Fallback query-param secret check. Both empty → reject."""
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided, expected)


# ── Phone normalization ───────────────────────────────────────────────────────

def normalize_phone_e164(raw: str) -> str:
    """
    Normalize a raw phone string to E.164 (+1XXXXXXXXXX).
    Returns "" if unable to normalize.
    """
    if not raw:
        return ""
    # Strip all non-digits
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    # Already E.164 check (came in with +)
    if raw.startswith("+") and _E164_RE.match(raw.strip()):
        return raw.strip()
    return ""


# ── DB helpers (take an open conn) ───────────────────────────────────────────

def _find_tracking_number_id(conn, tracking_phone: str):
    """Return callrail_numbers.id or None. Tries E.164 then digits-only LIKE."""
    if not tracking_phone:
        return None
    normed = normalize_phone_e164(tracking_phone)
    if normed:
        row = conn.execute(
            "SELECT id FROM callrail_numbers WHERE phone_number = ? LIMIT 1", (normed,)
        ).fetchone()
        if row:
            return row[0]
    # Fallback: last 10 digits LIKE match
    digits = re.sub(r"\D", "", tracking_phone)[-10:]
    if digits:
        row = conn.execute(
            "SELECT id FROM callrail_numbers WHERE phone_number LIKE ? LIMIT 1",
            (f"%{digits}",)
        ).fetchone()
        if row:
            return row[0]
    logger.warning("[callrail_webhook] tracking number %r not in callrail_numbers", tracking_phone)
    return None


def _find_recording_enabled(conn, tracking_number_id) -> bool:
    """Return recording_enabled flag for the tracking number row."""
    if not tracking_number_id:
        return False
    row = conn.execute(
        "SELECT recording_enabled FROM callrail_numbers WHERE id = ? LIMIT 1",
        (tracking_number_id,)
    ).fetchone()
    return bool(row and row[0])


def _find_campaign_by_name(conn, campaign_name: str) -> tuple:
    """
    Return (campaign_id_str, campaign_name_str) from campaigns table by name.
    campaign_id here is campaigns.campaign_id (the GAds campaign ID string),
    not the PK. Returns ("", campaign_name) if no match.
    """
    if not campaign_name:
        return ("", "")
    row = conn.execute(
        "SELECT campaign_id, campaign_name FROM campaigns "
        "WHERE campaign_name = ? COLLATE NOCASE LIMIT 1",
        (campaign_name,)
    ).fetchone()
    if row:
        return (row[0] or "", row[1] or campaign_name)
    return ("", campaign_name)


def _find_mango_match(conn, caller_e164: str, called_at_iso: str) -> str:
    """
    Try to match caller + time to a mango_calls row within ±2 minutes.
    Returns mango_calls.uuid or "".
    """
    if not caller_e164 or not called_at_iso:
        return ""
    try:
        called_dt = datetime.fromisoformat(called_at_iso)
        if called_dt.tzinfo is None:
            called_dt = called_dt.replace(tzinfo=timezone.utc)
        called_utc = called_dt.astimezone(timezone.utc)
        window_start = (called_utc - timedelta(minutes=_MANGO_MATCH_WINDOW_MINUTES)).isoformat()
        window_end   = (called_utc + timedelta(minutes=_MANGO_MATCH_WINDOW_MINUTES)).isoformat()

        # Build phone variant list (mango may store without +1)
        digits = re.sub(r"\D", "", caller_e164)
        variants = list({caller_e164, f"+{digits}", digits, digits[-10:] if len(digits) >= 10 else digits})

        placeholders = ",".join("?" * len(variants))
        row = conn.execute(
            f"""
            SELECT uuid FROM mango_calls
            WHERE direction = 'inbound'
              AND from_number IN ({placeholders})
              AND started_at BETWEEN ? AND ?
            ORDER BY ABS(strftime('%s', started_at) - strftime('%s', ?))
            LIMIT 1
            """,
            (*variants, window_start, window_end, called_utc.isoformat())
        ).fetchone()
        return row[0] if row else ""
    except Exception as e:
        logger.warning("[callrail_webhook] mango match failed (non-fatal): %s", e)
        return ""


def _upsert_callrail_call(
    conn,
    callrail_call_id: str,
    tracking_number_id,
    caller_e164: str,
    call: dict,
    mango_uuid: str,
    lead_id,
    recording_enabled_on_tracker: bool,
    raw_payload: str,
    event_type: str,
    lead_match_method: str = "",
) -> tuple:
    """
    Idempotent upsert of a callrail_calls row.
    Returns (is_new: bool, row_id: int).

    On UPDATE: preserves lead_id and mango_call_id if new value is empty
    (so call.created won't wipe data from call.completed and vice versa).
    HIPAA: recording_url is always stored as "" (Path B, no BAA).
    """
    now = _now_iso()
    # HIPAA Path B — never store recording URL
    recording_url = ""

    duration = int(call.get("duration") or 0)
    answered  = int(bool(call.get("answered")))
    voicemail = int(bool(call.get("voicemail")))
    first_call = int(bool(call.get("first_call")))
    called_at  = call.get("start_time", "")
    direction  = call.get("direction", "inbound")
    source     = call.get("source", "")
    campaign   = call.get("campaign", "")
    keyword    = call.get("keyword", "")
    gclid      = call.get("gclid", "")
    landing    = call.get("landing_page", "")
    name       = call.get("customer_name", "")
    city       = call.get("customer_city", "")
    state      = call.get("customer_state", "")

    existing = conn.execute(
        "SELECT id, lead_id, mango_call_id FROM callrail_calls WHERE callrail_call_id = ? LIMIT 1",
        (callrail_call_id,)
    ).fetchone()

    if existing:
        row_id = existing[0]
        # Preserve existing lead_id / mango_call_id if we don't have a better value
        final_lead_id   = lead_id   or existing[1]
        final_mango_id  = mango_uuid or existing[2]
        conn.execute("""
            UPDATE callrail_calls SET
                tracking_number_id = COALESCE(?, tracking_number_id),
                caller_number      = CASE WHEN ? != '' THEN ? ELSE caller_number END,
                caller_name        = CASE WHEN ? != '' THEN ? ELSE caller_name END,
                caller_city        = CASE WHEN ? != '' THEN ? ELSE caller_city END,
                caller_state       = CASE WHEN ? != '' THEN ? ELSE caller_state END,
                called_at          = CASE WHEN ? != '' THEN ? ELSE called_at END,
                duration_seconds   = CASE WHEN ? > 0   THEN ? ELSE duration_seconds END,
                direction          = ?,
                answered           = CASE WHEN ? = 1 THEN 1 ELSE answered END,
                voicemail          = CASE WHEN ? = 1 THEN 1 ELSE voicemail END,
                first_call         = CASE WHEN ? = 1 THEN 1 ELSE first_call END,
                source             = CASE WHEN ? != '' THEN ? ELSE source END,
                campaign           = CASE WHEN ? != '' THEN ? ELSE campaign END,
                keyword            = CASE WHEN ? != '' THEN ? ELSE keyword END,
                gclid              = CASE WHEN ? != '' THEN ? ELSE gclid END,
                landing_page       = CASE WHEN ? != '' THEN ? ELSE landing_page END,
                mango_call_id      = CASE WHEN ? != '' THEN ? ELSE mango_call_id END,
                lead_id            = CASE WHEN ? IS NOT NULL THEN ? ELSE lead_id END,
                lead_match_method  = CASE WHEN ? != '' THEN ? ELSE lead_match_method END,
                event_type         = ?,
                webhook_received_at = ?,
                raw_payload        = ?
            WHERE id = ?
        """, (
            tracking_number_id,
            caller_e164, caller_e164,
            name, name,
            city, city,
            state, state,
            called_at, called_at,
            duration, duration,
            direction,
            answered,
            voicemail,
            first_call,
            source, source,
            campaign, campaign,
            keyword, keyword,
            gclid, gclid,
            landing, landing,
            final_mango_id, final_mango_id,
            final_lead_id, final_lead_id,
            lead_match_method, lead_match_method,
            event_type,
            now,
            raw_payload,
            row_id,
        ))
        return (False, row_id)
    else:
        cursor = conn.execute("""
            INSERT INTO callrail_calls (
                callrail_call_id, tracking_number_id,
                caller_number, caller_name, caller_city, caller_state,
                called_at, duration_seconds, direction, answered, voicemail, first_call,
                source, campaign, keyword, gclid, landing_page, recording_url,
                mango_call_id, lead_id, od_patient_num,
                event_type, webhook_received_at, lead_match_method,
                ingested_at, raw_payload
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            callrail_call_id, tracking_number_id,
            caller_e164, name, city, state,
            called_at, duration, direction, answered, voicemail, first_call,
            source, campaign, keyword, gclid, landing, recording_url,
            mango_uuid, lead_id, "",
            event_type, now, lead_match_method,
            now, raw_payload,
        ))
        return (True, cursor.lastrowid)


# ── Lead resolution helpers ───────────────────────────────────────────────────

def _split_name(full: str) -> tuple:
    """Split 'First Last' → ('First', 'Last'). Both empty if blank."""
    parts = (full or "").strip().split(None, 1)
    return (parts[0] if parts else "", parts[1] if len(parts) > 1 else "")


def _create_lead_from_call(call: dict, caller_e164: str, campaign_id_resolved: str) -> str:
    """
    Create a new lead from a CallRail call event.
    Returns the new lead's UUID string.
    """
    from database import upsert_lead, add_lead_event

    first, last = _split_name(call.get("customer_name", ""))
    new_id = str(uuid.uuid4())
    data = {
        "id": new_id,
        "source": "callrail",
        "stage": "new",
        "phone": caller_e164,
        "first_name": first,
        "last_name": last,
        "gclid": call.get("gclid") or "",
        "campaign_id": campaign_id_resolved or "",
        "campaign_name": call.get("campaign") or "",
        "keyword_text": call.get("keyword") or "",
        "landing_url": call.get("landing_page") or "",
        "notes": (
            f"Inbound call via CallRail\n"
            f"Source: {call.get('source', '')}\n"
            f"Campaign: {call.get('campaign', '')}\n"
            f"Keyword: {call.get('keyword', '')}"
        ).strip(),
    }
    upsert_lead(data)
    add_lead_event(
        new_id,
        "callrail_lead_created",
        detail=json.dumps({
            "callrail_call_id": call.get("id", ""),
            "source": call.get("source", ""),
            "campaign": call.get("campaign", ""),
            "keyword": call.get("keyword", ""),
            "answered": call.get("answered"),
            "voicemail": call.get("voicemail"),
        }),
        source="callrail_webhook",
    )
    logger.info("[callrail_webhook] created lead %s for caller %s", new_id, caller_e164)
    return new_id


def _link_existing_lead(lead: dict, call: dict, campaign_id_resolved: str) -> dict:
    """
    Backfill missing attribution on an existing matched lead.
    Only fills fields that are currently empty — never overwrites good data.
    Returns dict describing what was backfilled.
    """
    from database import upsert_lead, add_lead_event

    updates = {"id": lead["id"]}

    if call.get("gclid") and not lead.get("gclid"):
        updates["gclid"] = call["gclid"]
    if call.get("campaign") and not lead.get("campaign_name"):
        updates["campaign_name"] = call["campaign"]
        if campaign_id_resolved and not lead.get("campaign_id"):
            updates["campaign_id"] = campaign_id_resolved
    if call.get("keyword") and not lead.get("keyword_text"):
        updates["keyword_text"] = call["keyword"]
    if call.get("landing_page") and not lead.get("landing_url"):
        updates["landing_url"] = call["landing_page"]

    backfilled = [k for k in updates if k != "id"]
    if backfilled:
        upsert_lead(updates)
        add_lead_event(
            lead["id"],
            "callrail_call_linked",
            detail=json.dumps({
                "callrail_call_id": call.get("id", ""),
                "backfilled": backfilled,
            }),
            source="callrail_webhook",
        )
        logger.info("[callrail_webhook] linked call to lead %s, backfilled: %s",
                    lead["id"], backfilled)
    return {"linked": True, "backfilled": backfilled}


# ── Main entry point ──────────────────────────────────────────────────────────

def process_webhook(payload: dict, raw_body: bytes) -> dict:
    """
    Orchestrate webhook ingestion. Never raises — errors are captured in result.

    Returns:
      {
        "ok":                bool,
        "action":            str,   # "created" | "linked" | "ingested_no_lead" |
                                    # "ignored_event_type" | "skipped_outbound" |
                                    # "duplicate" | "error"
        "callrail_call_id":  str,
        "lead_id":           str | None,
        "was_new_call_row":  bool,
        "mango_matched":     bool,
        "errors":            [str],
      }
    """
    result = {
        "ok": False,
        "action": "",
        "callrail_call_id": "",
        "lead_id": None,
        "was_new_call_row": False,
        "mango_matched": False,
        "errors": [],
    }

    try:
        # ── 1. Validate envelope ─────────────────────────────────────────────
        event_type = payload.get("event_type", "")
        if event_type not in _VALID_EVENT_TYPES:
            result.update({"ok": True, "action": "ignored_event_type",
                           "event_type": event_type})
            return result

        # CallRail sends call data nested under "call" or at top level (post_call)
        call = payload.get("call") or {}
        if not call.get("id"):
            # Try top-level (some webhook configurations)
            if payload.get("id"):
                call = payload
            else:
                result.update({"ok": False, "action": "missing_call_id",
                               "errors": ["No call.id in payload"]})
                return result

        call_id = call["id"]
        result["callrail_call_id"] = call_id

        # Skip outbound calls — we only care about inbound attribution
        if call.get("direction", "inbound") != "inbound":
            result.update({"ok": True, "action": "skipped_outbound"})
            return result

        # ── 2. Normalize phones ──────────────────────────────────────────────
        # Try multiple possible field names for caller phone
        raw_caller = (
            call.get("customer_phone_number") or
            call.get("caller_id") or
            call.get("phone_number") or
            ""
        )
        caller_e164 = normalize_phone_e164(raw_caller)
        if not caller_e164:
            logger.warning("[callrail_webhook] could not normalize caller phone %r for call %s",
                           raw_caller, call_id)

        # ── 3-6. DB lookups (single conn) ────────────────────────────────────
        from database import _conn

        tracking_number_id = None
        recording_enabled  = False
        campaign_id_resolved = ""
        mango_uuid = ""

        with _conn() as conn:
            tracking_number_id = _find_tracking_number_id(
                conn, call.get("tracking_phone_number", "")
            )
            recording_enabled = _find_recording_enabled(conn, tracking_number_id)
            campaign_id_resolved, _ = _find_campaign_by_name(
                conn, call.get("campaign", "")
            )
            if caller_e164 and call.get("start_time"):
                mango_uuid = _find_mango_match(conn, caller_e164, call["start_time"])

        result["mango_matched"] = bool(mango_uuid)

        # ── 7. Lead resolution (uses its own _conn() internally) ─────────────
        lead_id = None
        action  = "ingested_no_lead"
        lead_match_method = "none"

        if caller_e164:
            from database import get_lead_by_phone

            existing_lead = get_lead_by_phone(caller_e164)
            if existing_lead:
                _link_existing_lead(existing_lead, call, campaign_id_resolved)
                lead_id = existing_lead["id"]
                action  = "linked"
                lead_match_method = "phone_hash"
            else:
                # Create a new lead only for call.completed / post_call
                # (call.created arrives at ring-start without answered/duration/voicemail data)
                # Voicemail → create (high-intent, don't drop)
                # Answered → create
                # call.created with no answered info → defer to call.completed
                should_create = (
                    event_type in ("call.completed", "post_call") and
                    (call.get("answered") or call.get("voicemail"))
                )
                if should_create:
                    lead_id = _create_lead_from_call(call, caller_e164, campaign_id_resolved)
                    action  = "created"
                    lead_match_method = "created"

        result["lead_id"] = lead_id

        # ── 8. Upsert call row ───────────────────────────────────────────────
        with _conn() as conn:
            is_new, row_id = _upsert_callrail_call(
                conn=conn,
                callrail_call_id=call_id,
                tracking_number_id=tracking_number_id,
                caller_e164=caller_e164,
                call=call,
                mango_uuid=mango_uuid,
                lead_id=lead_id,
                recording_enabled_on_tracker=recording_enabled,
                raw_payload=json.dumps(payload),
                event_type=event_type,
                lead_match_method=lead_match_method,
            )

        result["was_new_call_row"] = is_new
        result["lead_match_method"] = lead_match_method
        result.update({"ok": True, "action": action})

        logger.info(
            "[callrail_webhook] %s call %s | caller=%s | lead=%s | mango=%s | new_row=%s",
            event_type, call_id, caller_e164 or "unknown",
            lead_id or "none", mango_uuid or "none", is_new,
        )
        return result

    except Exception as e:
        logger.error("[callrail_webhook] process_webhook failed: %s", e, exc_info=True)
        result.update({"ok": False, "action": "error", "errors": [str(e)]})
        return result
