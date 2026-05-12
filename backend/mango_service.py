"""
mango_service.py — Mango Voice integration for the Lead-Lifecycle dashboard.

Provides:
  - MangoTokenManager   JWT auth + auto-refresh (ported from mango_desktop.py)
  - fetch_calls_since() paginated call puller
  - normalize_call()    raw API dict → DB-ready dict
  - sync_mango_calls()  main sync entry point (called by APScheduler every 5 min)
  - reconcile_attribution() match Mango calls against GAds call_view + leads

Extension → staff mapping (update if team changes):
  '101' → Olivia
  '103' → Ivette
"""

import json
import logging
import re
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

from database import (
    upsert_mango_call,
    upsert_mango_calls_batch,
    get_mango_calls_unmatched,
    get_gads_call_view,
    get_all_leads,
    update_mango_call_attribution,
    _conn as _db_conn,
)

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────
_API_BASE = "https://api.mangovoice.com"

# Extension number → staff name mapping
EXTENSION_MAP = {
    "101": "Olivia",
    "103": "Ivette",
}

# ── JWT Authentication ────────────────────────────────────────────────────────

def _login_mango(username: str, password: str, api_base: str = _API_BASE) -> dict:
    """Authenticate against Mango JWT endpoint. Returns {access_token, refresh_token}."""
    r = requests.post(
        f"{api_base.rstrip('/')}/api/token/",
        json={"username": username, "password": password},
        timeout=15,
    )
    if r.status_code in (400, 401):
        detail = r.json()
        msg = detail.get("message") or detail.get("detail") or detail.get("error") or str(detail)
        raise ValueError(f"Mango login failed: {msg}")
    r.raise_for_status()
    data = r.json()
    return {"access_token": data["access"], "refresh_token": data["refresh"]}


def _refresh_access_token(refresh_tok: str, api_base: str = _API_BASE) -> str:
    """Get a new access token using the stored refresh token."""
    r = requests.post(
        f"{api_base.rstrip('/')}/api/token/refresh/",
        json={"refresh": refresh_tok},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["access"]


class MangoTokenManager:
    """
    Thread-safe JWT token manager for the Mango Voice API.
    Logs in on construction and auto-refreshes every 50 minutes.
    Falls back to full re-login if refresh fails.
    Singleton per FastAPI process — instantiate once in main.py lifespan().
    """

    def __init__(self, username: str, password: str, api_base: str = _API_BASE):
        self.username = username
        self.password = password
        self.api_base = api_base
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._lock = threading.Lock()
        self._do_login()

    def _do_login(self) -> None:
        tokens = _login_mango(self.username, self.password, self.api_base)
        with self._lock:
            self._access_token = tokens["access_token"]
            self._refresh_token = tokens["refresh_token"]
        logger.info("Mango: logged in successfully")
        self._schedule_refresh()

    def _schedule_refresh(self) -> None:
        t = threading.Timer(50 * 60, self._refresh)
        t.daemon = True
        t.start()

    def _refresh(self) -> None:
        try:
            with self._lock:
                rt = self._refresh_token
            new_token = _refresh_access_token(rt, self.api_base)
            with self._lock:
                self._access_token = new_token
            logger.debug("Mango: token refreshed")
        except Exception as e:
            logger.warning(f"Mango: token refresh failed ({e}), falling back to re-login")
            try:
                tokens = _login_mango(self.username, self.password, self.api_base)
                with self._lock:
                    self._access_token = tokens["access_token"]
                    self._refresh_token = tokens["refresh_token"]
                logger.info("Mango: re-login successful after refresh failure")
            except Exception as e2:
                logger.error(f"Mango: re-login also failed: {e2}")
        self._schedule_refresh()

    def get_token(self) -> Optional[str]:
        with self._lock:
            return self._access_token


# ── Phone normalization ───────────────────────────────────────────────────────

def _extract_area_code(phone: Optional[str]) -> str:
    """Extract 3-digit area code from E.164 or local number string."""
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    # E.164 US: +1XXXXXXXXXX → 11 digits starting with 1
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:4]
    if len(digits) == 10:
        return digits[:3]
    return ""


def _normalize_phone(phone) -> str:
    """Normalize a Mango phone field (may be dict or string) to E.164-ish string."""
    if phone is None:
        return ""
    if isinstance(phone, dict):
        # Mango returns {"formatted": "(508) 318-4222", "caller_id": "VASQUEZ", ...}
        phone = (phone.get("formatted")
                 or phone.get("number")
                 or phone.get("e164")
                 or "")
    return str(phone).strip()


def _extract_extension(raw: dict) -> str:
    """
    Extract the answering extension number from a raw Mango call dict.
    Mango may put it under different keys depending on API version:
      - top-level 'extension' (dict or string)
      - 'endpoint' dict
      - 'user' dict
      - 'answered_by' dict
      - 'legs[]' array — look in the leg where answered=True
    """
    candidates = [
        raw.get("extension"),
        raw.get("endpoint"),
        raw.get("user"),
        raw.get("answered_by"),
        raw.get("dispositioned_by"),
    ]
    for c in candidates:
        if isinstance(c, dict):
            num = c.get("number") or c.get("extension") or c.get("ext")
            if num:
                return str(num).strip()
        elif c and str(c).strip():
            return str(c).strip()
    # Try call legs — Mango often puts the answering ext inside the answered leg
    for leg in (raw.get("legs") or []):
        if isinstance(leg, dict) and leg.get("answered"):
            ep = leg.get("endpoint") or {}
            num = ep.get("number") or ep.get("extension")
            if num:
                return str(num).strip()
    return ""


def _extract_caller_id_name(raw: dict, direction: str) -> str:
    """Extract caller name from top-level or nested from/to dict."""
    # Top-level fields first
    name = raw.get("caller_id_name") or raw.get("cnam") or ""
    if name:
        return str(name).strip()
    # Mango embeds it in the from/to dict as 'caller_id'
    if direction == "inbound":
        src = raw.get("from") or raw.get("from_number") or {}
    else:
        src = raw.get("to") or raw.get("to_number") or {}
    if isinstance(src, dict):
        name = src.get("caller_id") or src.get("name") or src.get("cnam") or ""
    return str(name).strip()


# ── Call normalization ────────────────────────────────────────────────────────

_ANSWERED_STATUSES  = {"answered", "received", "completed", "connected", "success"}
_MISSED_STATUSES    = {"missed", "no_answer", "noanswer", "unanswered", "abandoned",
                       "not_answered", "no-answer", "declined", "failed", "rejected",
                       "busy_missed"}
_VOICEMAIL_STATUSES = {"voicemail", "vm", "left_voicemail", "to_voicemail"}
_BUSY_STATUSES      = {"busy"}


def normalize_call(raw: dict) -> dict:
    """
    Convert a raw Mango /calls/ API result dict into a DB-ready dict for upsert_mango_call().
    Stores raw_payload JSON for field-name debugging without re-syncing.
    """
    # Direction normalization (do first — needed by _extract_caller_id_name)
    direction = (raw.get("direction") or "inbound").lower()
    if direction not in ("inbound", "outbound"):
        direction = "inbound"

    # Extension → staff name (broadened extraction)
    ext_number = _extract_extension(raw)
    answered_by = EXTENSION_MAP.get(ext_number) if ext_number else None

    # Duration — Mango uses 'duration' or 'duration_in_seconds'
    duration_sec = int(raw.get("duration") or raw.get("duration_in_seconds") or 0)

    # Status normalization — Mango uses 'unanswered'/'abandoned' not 'missed'/'no_answer'
    raw_status = (raw.get("status") or raw.get("call_status") or "").lower().strip()
    if raw_status in _ANSWERED_STATUSES:
        status = "answered"
    elif raw_status in _MISSED_STATUSES:
        status = "missed"
    elif raw_status in _VOICEMAIL_STATUSES:
        status = "voicemail"
    elif raw_status in _BUSY_STATUSES:
        status = "busy"
    else:
        status = raw_status or "unknown"

    # Phone fields — Mango returns dicts with 'formatted' key, not 'number'
    from_number = _normalize_phone(raw.get("from_number") or raw.get("from") or raw.get("caller_id_number"))
    to_number   = _normalize_phone(raw.get("to_number")   or raw.get("to")   or raw.get("destination_number"))

    # Caller name — may be in top-level or nested in from/to dict
    caller_id_name = _extract_caller_id_name(raw, direction)

    return {
        "uuid": str(raw.get("uuid") or raw.get("id") or ""),
        "started_at": raw.get("started_at") or raw.get("start_time") or "",
        "ended_at": raw.get("ended_at") or raw.get("end_time"),
        "direction": direction,
        "from_number": from_number,
        "to_number": to_number,
        "caller_id_name": caller_id_name,
        "caller_id_number": from_number if direction == "inbound" else to_number,
        "destination_number": to_number if direction == "inbound" else from_number,
        "extension_number": ext_number,
        "answered_by": answered_by,
        "duration_sec": duration_sec,
        "status": status,
        "recording_url": raw.get("recording_url") or raw.get("recording"),
        "recording_local_path": None,
        "raw_payload": json.dumps(raw, separators=(",", ":"), default=str),
    }


# ── Call fetcher ──────────────────────────────────────────────────────────────

def fetch_calls_since(
    token_manager: MangoTokenManager,
    pbx_id: str,
    since_dt: datetime,
    limit: int = 200,
    api_base: str = _API_BASE,
) -> list:
    """
    Fetch all calls since `since_dt` (UTC datetime) from Mango API.
    Handles pagination automatically. Returns list of raw call dicts.
    """
    base = api_base.rstrip("/")
    since_str = since_dt.strftime("%Y-%m-%dT%H:%M:%S")
    offset = 0
    results = []

    while True:
        token = token_manager.get_token()
        params = {
            "pbx_ids": pbx_id,
            "ordering": "-started_at",
            "limit": limit,
            "offset": offset,
            "started_at__gte": since_str,
        }
        r = requests.get(
            f"{base}/calls/",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=20,
        )
        if r.status_code == 401:
            logger.warning("Mango: 401 on calls fetch — token may have expired")
            raise PermissionError("Mango token expired")
        r.raise_for_status()
        data = r.json()
        page_results = data.get("results", [])
        results.extend(page_results)
        total = data.get("count", 0)
        offset += len(page_results)
        if offset >= total or not page_results:
            break

    logger.info(f"Mango: fetched {len(results)} calls since {since_str}")
    return results


def fetch_fresh_recording_url(
    token_manager: MangoTokenManager,
    call_uuid: str,
    pbx_id: str,
    api_base: str = _API_BASE,
) -> str:
    """
    Fetch a fresh pre-signed recording URL for a specific call from Mango API.

    Strategy:
    1. Try GET /calls/{uuid}/ (single-object endpoint — fastest, most accurate)
    2. Fall back to GET /calls/?pbx_ids=...&limit=200 and scan for matching uuid
       (Mango ignores most filter params, so uuid= filter doesn't work)

    Returns the fresh recording_url string, or "" if unavailable.
    """
    base = api_base.rstrip("/")
    token = token_manager.get_token()
    headers = {"Authorization": f"Bearer {token}"}

    # ── Strategy 1: single-object endpoint ────────────────────────────────────
    r = requests.get(f"{base}/calls/{call_uuid}/", headers=headers, timeout=20)
    if r.status_code == 200:
        raw = r.json()
        url = raw.get("recording_url") or raw.get("recording") or ""
        if url:
            logger.info("Mango: fresh recording URL for %s via detail endpoint: found", call_uuid)
            return url
        logger.info("Mango: detail endpoint returned call %s but no recording_url. Keys: %s",
                    call_uuid, list(raw.keys()))
        # Call exists but has no recording — return empty immediately
        return ""

    logger.info("Mango: detail endpoint returned %s for %s — falling back to list scan",
                r.status_code, call_uuid)

    # ── Strategy 2: scan recent calls list for matching uuid ─────────────────
    # Mango ignores most filter params; fetch a large recent page and find our call
    r2 = requests.get(
        f"{base}/calls/",
        headers=headers,
        params={"pbx_ids": pbx_id, "ordering": "-started_at", "limit": 200},
        timeout=20,
    )
    if not r2.ok:
        logger.warning("Mango: list scan returned HTTP %s for call %s", r2.status_code, call_uuid)
        return ""
    results = r2.json().get("results", [])
    for raw in results:
        if str(raw.get("uuid") or raw.get("id") or "") == str(call_uuid):
            url = raw.get("recording_url") or raw.get("recording") or ""
            logger.info("Mango: fresh recording URL for %s via list scan: %s",
                        call_uuid, "found" if url else "not available (no recording)")
            if not url:
                logger.info("Mango: call %s list keys: %s", call_uuid, list(raw.keys()))
            return url

    logger.warning("Mango: call %s not found in 200-call list scan", call_uuid)
    return ""


# ── Main sync entry point ─────────────────────────────────────────────────────

# Module-level cursor: tracks last successful sync time (populated from DB on first use)
_last_sync_cursor: Optional[datetime] = None

_SYNC_CURSOR_KEY = "mango_last_sync_cursor"


def _load_sync_cursor() -> Optional[datetime]:
    """Read the persisted sync cursor from the settings table. Returns None if not set."""
    try:
        from database import get_setting
        raw = get_setting(_SYNC_CURSOR_KEY, "")
        if raw:
            return datetime.fromisoformat(raw)
    except Exception as e:
        logger.warning("Mango sync: could not load cursor from DB: %s", e)
    return None


def _save_sync_cursor(dt: datetime) -> None:
    """Persist the sync cursor to the settings table so it survives restarts."""
    try:
        from database import save_setting
        save_setting(_SYNC_CURSOR_KEY, dt.isoformat())
    except Exception as e:
        logger.warning("Mango sync: could not save cursor to DB: %s", e)


def sync_mango_calls(
    token_manager: MangoTokenManager,
    pbx_id: str,
    api_base: str = _API_BASE,
    initial_days_back: int = 30,
) -> int:
    """
    Fetch new Mango calls since last sync and upsert into DB.
    Called by APScheduler every 5 minutes.
    Returns count of calls processed.
    """
    global _last_sync_cursor

    # On first call after a restart, try to restore the cursor from the DB
    if _last_sync_cursor is None:
        _last_sync_cursor = _load_sync_cursor()

    # Determine the sync window
    if _last_sync_cursor is None:
        # Genuine first run — do the full historical backfill
        since = datetime.now(timezone.utc) - timedelta(days=initial_days_back)
        logger.info(f"Mango sync: initial backfill, fetching {initial_days_back} days")
    else:
        # Incremental — overlap by 2 minutes to catch any calls that slipped through
        since = _last_sync_cursor - timedelta(minutes=2)
        logger.info(f"Mango sync: incremental from {since.isoformat()}")

    try:
        raw_calls = fetch_calls_since(token_manager, pbx_id, since, api_base=api_base)
    except PermissionError:
        logger.error("Mango sync: auth failure, skipping this cycle")
        return 0
    except Exception as e:
        logger.error(f"Mango sync: fetch error: {e}")
        return 0

    # Normalize all calls first, then batch-upsert in a single DB connection
    # to avoid [Errno 24] Too many open files (WAL mode = 3 fds per connection)
    normalized_calls = []
    skipped_internal = 0
    for raw in raw_calls:
        # Skip internal calls — Mango marks these with direction="internal"
        # This covers staff extensions dialing out (101→external) and true ext-to-ext
        if raw.get('direction') == 'internal':
            skipped_internal += 1
            continue
        normalized = normalize_call(raw)
        if normalized["uuid"]:
            normalized_calls.append(normalized)
    if skipped_internal:
        logger.info(f"Mango sync: skipped {skipped_internal} internal calls")

    count = 0
    if normalized_calls:
        try:
            count = upsert_mango_calls_batch(normalized_calls)
        except Exception as e:
            import traceback
            logger.error(f"Mango sync: batch upsert error: {e}\n{traceback.format_exc()}")

    _last_sync_cursor = datetime.now(timezone.utc)
    _save_sync_cursor(_last_sync_cursor)
    logger.info(f"Mango sync: upserted {count}/{len(normalized_calls)} calls")
    return count


# ── Attribution reconciler ────────────────────────────────────────────────────

def _parse_gads_dt(gc_start_str: str) -> Optional[datetime]:
    """Parse a GAds start_call_date_time string to UTC-aware datetime.
    GAds returns Eastern naive strings like '2026-04-19 10:16:00'."""
    try:
        gc_dt = datetime.fromisoformat(gc_start_str.replace("Z", "+00:00"))
        if gc_dt.tzinfo is None:
            try:
                from zoneinfo import ZoneInfo
                gc_dt = gc_dt.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
            except Exception:
                gc_dt = gc_dt.replace(tzinfo=timezone.utc) + timedelta(hours=5)
        return gc_dt
    except Exception:
        return None


def _create_call_flag_if_needed(conn, mc: dict, gc: dict) -> None:
    """
    Create a call_flags row when a Google Ads-matched call warrants follow-up.

    Flag types (in order of priority):
      missed_new_patient       — GAds call was MISSED and caller is a new patient
      missed_existing_patient  — GAds call was MISSED and caller is an existing patient
      short_gads_call          — Call answered but <15s (misdial / accidental tap)
      unconverted_short_gads_call — 15–30s and not booked (IVR / call experience issue)

    Deferred (returns without inserting) if OD enrichment hasn't run yet for this call.
    Does not create duplicate open flags for the same (uuid, flag_type).
    """
    od_status     = mc.get("od_patient_status", "") or ""
    od_matched_at = mc.get("od_matched_at", "") or ""
    duration      = int(mc.get("duration_sec") or 0)
    gc_status     = (gc.get("call_status") or "").upper()
    mc_status     = (mc.get("status") or "").lower()
    booked        = mc.get("booked_outcome") == "booked"
    match_conf    = float(mc.get("match_confidence") or 0.0)

    # Defer flag creation if OD enrichment hasn't run yet (avoids wrong flag_type)
    if not od_status and not od_matched_at:
        return

    # Treat as missed if either GAds reports MISSED or Mango status is missed/voicemail
    is_missed = gc_status == "MISSED" or mc_status in ("missed", "voicemail")

    flag_type = None
    if is_missed:
        # GAds duration is 0 for MISSED — use Mango status instead
        if od_status == "new_patient" or (od_status in ("unknown", "") and od_matched_at):
            flag_type = "missed_new_patient"
        elif od_status.startswith("existing"):
            flag_type = "missed_existing_patient"
        else:
            flag_type = "missed_new_patient"  # default: treat unknown as potential new patient
    elif duration < 15:
        flag_type = "short_gads_call"
    elif duration < 30 and not booked:
        flag_type = "unconverted_short_gads_call"

    if not flag_type:
        return

    # Dedup: only one open flag per (uuid, flag_type)
    existing = conn.execute(
        "SELECT 1 FROM call_flags WHERE uuid=? AND flag_type=? AND resolved_at IS NULL LIMIT 1",
        (mc["uuid"], flag_type)
    ).fetchone()
    if existing:
        return

    reason = f"{'MISSED' if is_missed else gc_status or 'SHORT'} call, duration {duration}s, Mango status: {mc_status or 'unknown'}"
    conn.execute("""
        INSERT INTO call_flags
          (uuid, flag_type, reason, campaign_name, keyword,
           od_patient_status_at_flag, match_confidence_at_flag, created_at)
        VALUES (?,?,?,?,?,?,?,?)
    """, (
        mc["uuid"], flag_type, reason,
        gc.get("campaign_name", ""),
        mc.get("attributed_keyword", ""),
        od_status,
        match_conf,
        datetime.now(timezone.utc).isoformat(),
    ))
    logger.info(
        "call_flag created: uuid=%s type=%s duration=%ds campaign=%s",
        mc["uuid"], flag_type, duration, gc.get("campaign_name", "")
    )


def _flag_unattributed_missed_new_patients(days: int = 7) -> int:
    """
    Separate pass: flag missed new-patient calls with NO Google Ads attribution.
    These could be organic, GMB, or direct-dial — still worth following up.
    Runs after reconcile_attribution completes.
    Returns count of flags created.
    """
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    created = 0
    try:
        with _db_conn() as conn:
            rows = conn.execute("""
                SELECT uuid, od_patient_status, od_matched_at, match_confidence,
                       attributed_keyword, started_at
                FROM mango_calls
                WHERE direction = 'inbound'
                  AND status IN ('missed', 'voicemail')
                  AND (gads_call_id IS NULL OR gads_call_id = '')
                  AND od_patient_status = 'new_patient'
                  AND started_at >= ?
            """, (cutoff,)).fetchall()

            for row in rows:
                existing = conn.execute(
                    "SELECT 1 FROM call_flags WHERE uuid=? AND flag_type=? AND resolved_at IS NULL LIMIT 1",
                    (row["uuid"], "missed_new_patient_unattributed")
                ).fetchone()
                if existing:
                    continue
                conn.execute("""
                    INSERT INTO call_flags
                      (uuid, flag_type, reason, campaign_name, keyword,
                       od_patient_status_at_flag, match_confidence_at_flag, created_at)
                    VALUES (?,?,?,?,?,?,?,?)
                """, (
                    row["uuid"], "missed_new_patient_unattributed",
                    "Missed call from new patient (no GAds attribution)",
                    "", row["attributed_keyword"] or "",
                    "new_patient", 0.0,
                    datetime.now(timezone.utc).isoformat(),
                ))
                created += 1
    except Exception as e:
        logger.warning(f"_flag_unattributed_missed_new_patients failed: {e}")
    if created:
        logger.info(f"call_flags: {created} missed_new_patient_unattributed flags created")
    return created


def reconcile_attribution(days: int = 7, target_gads_call_id: str = "") -> int:
    """
    Match unattributed inbound Mango calls against:
      1. Google Ads call_view rows (ad-driven calls by area code + time window)
      2. Lead phone numbers (existing leads who called in)

    Updates mango_calls.lead_id / gads_call_id / match_confidence / match_method.
    Safe to run repeatedly — only updates NULL attribution rows.

    Matching tiers (highest confidence wins):
      gads_window           0.95 — area code + ±90s + ±5s duration
      gads_window_loose     0.75 — area code + ±300s + ±30s duration
      gads_window_time_only 0.60 — area code + ±600s (no duration check)
      gads_time_only_no_area 0.55 — NO area code on either side + ±120s + tight duration
                                    (mobile ad-extension taps where Google strips area code)

    After any GAds match: creates a call_flag if the call was missed or short.
    After all GAds matching: flags unattributed missed new-patient calls.

    target_gads_call_id: if set, only attempt to match this one GAds row (used by
    the match-and-transcribe endpoint for targeted matching with extra diagnostics).

    Returns count of calls newly attributed.
    """
    unmatched = get_mango_calls_unmatched(days=days)
    gads_calls = get_gads_call_view(days=days)
    # Use a large limit to get all leads for phone matching — default 200 is too small.
    all_leads = get_all_leads(limit=10000)

    # Filter to specific GAds call if targeted
    if target_gads_call_id:
        gads_calls = [g for g in gads_calls if g["call_id"] == target_gads_call_id]

    # Build phone → lead_id lookup (strip non-digits for comparison)
    phone_to_lead: dict[str, str] = {}
    for lead in all_leads:
        phone = re.sub(r"\D", "", lead.get("phone", "") or "")
        if len(phone) >= 10:
            phone_to_lead[phone[-10:]] = lead["id"]

    # Pre-parse GAds datetimes once (avoid re-parsing for every Mango call)
    gads_parsed = []
    for gc in gads_calls:
        gc_dt = _parse_gads_dt(gc.get("start_call_date_time", ""))
        if gc_dt:
            gads_parsed.append((gc, gc_dt))

    # Build a reverse map: gads_call_id → list of candidate Mango uuids at 0.55
    # Used to detect ambiguous time-only matches and skip them.
    _time_only_candidates: dict[str, list[str]] = {}   # gads_call_id → [mc_uuid, ...]
    _mc_time_only_match:   dict[str, list[str]] = {}   # mc_uuid → [gads_call_id, ...]

    attributed = 0

    # ── First pass: collect time-only candidates to detect ambiguity ─────────
    for mc in unmatched:
        mc_start_str = mc.get("started_at", "")
        mc_area = _extract_area_code(mc.get("from_number", ""))
        mc_dur = int(mc.get("duration_sec") or 0)
        mc_uuid = mc["uuid"]

        try:
            mc_dt = datetime.fromisoformat(mc_start_str.replace("Z", "+00:00"))
            if mc_dt.tzinfo is None:
                mc_dt = mc_dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue

        # Check if this call already has a strong phone-lead match
        from_digits = re.sub(r"\D", "", mc.get("from_number", "") or "")
        if len(from_digits) >= 10 and phone_to_lead.get(from_digits[-10:]):
            continue  # known patient — skip time-only GAds fallback

        for gc, gc_dt in gads_parsed:
            gc_area = gc.get("caller_area_code", "")
            gc_dur  = int(gc.get("call_duration_sec") or 0)
            gc_status = (gc.get("call_status") or "").upper()

            # Only consider GAds rows with no area code — rows with area codes are
            # handled by area-code branches and don't need ambiguity tracking here
            if gc_area:
                continue

            time_delta = abs((mc_dt - gc_dt).total_seconds())
            if time_delta > 120:
                continue

            # Duration check (skip for MISSED — GAds duration is 0 for missed calls)
            if gc_status != "MISSED":
                dur_delta = abs(mc_dur - gc_dur)
                max_dur = max(mc_dur, gc_dur)
                # Fallback A (no Mango area): looser tolerance
                # Fallback B (Mango has area): tighter tolerance
                tol = max(15, 0.25 * max_dur) if mc_area else max(20, 0.3 * max_dur)
                if dur_delta > tol:
                    continue

            gads_id = gc["call_id"]
            _time_only_candidates.setdefault(gads_id, []).append(mc_uuid)
            _mc_time_only_match.setdefault(mc_uuid, []).append(gads_id)

    # Remove ambiguous: any GAds row matched by >1 Mango call at 0.55
    _ambiguous_gads = {gid for gid, uuids in _time_only_candidates.items() if len(uuids) > 1}
    # Remove ambiguous: any Mango call that could match >1 GAds row at 0.55
    _ambiguous_mcs  = {uuid for uuid, gids in _mc_time_only_match.items() if len(gids) > 1}

    def _is_clean_time_only(mc_uuid: str, gads_id: str) -> bool:
        return gads_id not in _ambiguous_gads and mc_uuid not in _ambiguous_mcs

    # ── Main attribution loop ─────────────────────────────────────────────────
    # Track which GAds call_ids have already been claimed this pass so we never
    # assign the same GAds call to two different Mango records (can happen when
    # two callers ring in at the same second and both pass the area+time+dur check).
    _used_gads_ids: set = set()

    for mc in unmatched:
        mc_start_str = mc.get("started_at", "")
        mc_area = _extract_area_code(mc.get("from_number", ""))
        mc_dur = int(mc.get("duration_sec") or 0)
        mc_uuid = mc["uuid"]

        # Parse Mango started_at
        try:
            mc_dt = datetime.fromisoformat(mc_start_str.replace("Z", "+00:00"))
            if mc_dt.tzinfo is None:
                mc_dt = mc_dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue

        # Pre-check: is this caller a known lead? If so, skip time-only fallback.
        _from_digits_main = re.sub(r"\D", "", mc.get("from_number", "") or "")
        _mc_is_known_lead = (len(_from_digits_main) >= 10 and
                             phone_to_lead.get(_from_digits_main[-10:]) is not None)

        # ── Try GAds call_view match ──────────────────────────────────────────
        best_gads_id = None
        best_confidence = 0.0
        best_method = ""
        best_gc = None

        for gc, gc_dt in gads_parsed:
            # Skip GAds rows already claimed by an earlier Mango call this pass
            if gc["call_id"] in _used_gads_ids:
                continue

            gc_area   = gc.get("caller_area_code", "")
            gc_dur    = int(gc.get("call_duration_sec") or 0)
            gc_status = (gc.get("call_status") or "").upper()

            time_delta = abs((mc_dt - gc_dt).total_seconds())
            dur_delta  = abs(mc_dur - gc_dur)
            area_match = gc_area and mc_area and gc_area == mc_area

            # Tight match: area code + ±90s time + ±5s duration
            if area_match and time_delta <= 90 and dur_delta <= 5:
                if 0.95 > best_confidence:
                    best_gads_id   = gc["call_id"]
                    best_confidence = 0.95
                    best_method    = "gads_window"
                    best_gc        = gc
            # Loose match: area code + ±300s time + ±30s duration
            elif area_match and time_delta <= 300 and dur_delta <= 30:
                if 0.75 > best_confidence:
                    best_gads_id   = gc["call_id"]
                    best_confidence = 0.75
                    best_method    = "gads_window_loose"
                    best_gc        = gc
            # Very loose: area code + ±600s time (no duration check — GAds/Mango measure differently)
            elif area_match and time_delta <= 600:
                if 0.60 > best_confidence:
                    best_gads_id   = gc["call_id"]
                    best_confidence = 0.60
                    best_method    = "gads_window_time_only"
                    best_gc        = gc
            # Time-only fallback A: neither side has area code, ±120s, unambiguous,
            # not a known lead (known leads get phone_exact match instead).
            # Confidence 0.55 — lower than all area-code branches
            elif (not gc_area) and (not mc_area) and time_delta <= 120 and not _mc_is_known_lead:
                if gc_status != "MISSED":
                    max_dur  = max(mc_dur, gc_dur)
                    dur_ok   = dur_delta <= max(20, 0.3 * max_dur)
                    if not dur_ok:
                        continue
                if 0.55 > best_confidence and _is_clean_time_only(mc_uuid, gc["call_id"]):
                    best_gads_id   = gc["call_id"]
                    best_confidence = 0.55
                    best_method    = "gads_time_only_no_area"
                    best_gc        = gc
            # Time-only fallback B: GAds has no area code but Mango does (Google strips
            # area code for some mobile call extensions even when caller has one).
            # ±120s time, tight duration match. Confidence 0.60 — Mango area code is real
            # signal even if GAds doesn't have it, so slightly higher than fallback A.
            elif (not gc_area) and mc_area and time_delta <= 120 and not _mc_is_known_lead:
                if gc_status != "MISSED":
                    max_dur  = max(mc_dur, gc_dur)
                    dur_ok   = dur_delta <= max(15, 0.25 * max_dur)
                    if not dur_ok:
                        continue
                if 0.60 > best_confidence and _is_clean_time_only(mc_uuid, gc["call_id"]):
                    best_gads_id   = gc["call_id"]
                    best_confidence = 0.60
                    best_method    = "gads_time_only_gads_no_area"
                    best_gc        = gc
            # Last resort for targeted matching: time only (±120s), any area code combo
            elif target_gads_call_id and time_delta <= 120:
                if 0.50 > best_confidence:
                    best_gads_id   = gc["call_id"]
                    best_confidence = 0.50
                    best_method    = "gads_time_only"
                    best_gc        = gc

            if target_gads_call_id and gc["call_id"] == target_gads_call_id:
                logger.info(
                    "reconcile[targeted] GAds=%s Mango=%s | area: gc=%s mc=%s | "
                    "time_delta=%.0fs dur_delta=%ds | best_so_far=%.2f via %s",
                    gc["call_id"], mc_uuid, gc_area, mc_area,
                    time_delta, dur_delta, best_confidence, best_method or "none",
                )

        if best_gads_id and best_gc:
            # Claim this GAds call so no other Mango record can match it this pass
            _used_gads_ids.add(best_gads_id)
            update_mango_call_attribution(
                uuid=mc_uuid,
                gads_call_id=best_gads_id,
                match_confidence=best_confidence,
                match_method=best_method,
            )
            attributed += 1
            _queue_process_if_needed(mc)

            # Create call flag if this GAds call was missed or short
            try:
                with _db_conn() as conn:
                    # Refresh mc with updated match fields for flag creation
                    mc_fresh = dict(mc)
                    mc_fresh["match_confidence"] = best_confidence
                    _create_call_flag_if_needed(conn, mc_fresh, best_gc)
            except Exception as _fe:
                logger.warning(f"call_flag creation failed for {mc_uuid}: {_fe}")
            continue

        # ── Try lead phone match ──────────────────────────────────────────────
        from_digits = re.sub(r"\D", "", mc.get("from_number", "") or "")
        if len(from_digits) >= 10:
            lead_id = phone_to_lead.get(from_digits[-10:])
            if lead_id:
                update_mango_call_attribution(
                    uuid=mc_uuid,
                    lead_id=lead_id,
                    match_confidence=0.90,
                    match_method="phone_exact",
                )
                attributed += 1
                _queue_process_if_needed(mc)

    logger.info(f"Mango reconcile: attributed {attributed}/{len(unmatched)} calls")

    # Backfill flags for previously-attributed GAds calls that may have missed flag creation
    _backfill_call_flags_for_attributed(days=days)

    # Flag unattributed missed new-patient calls (organic/GMB/direct-dial)
    _flag_unattributed_missed_new_patients(days=days)

    return attributed


def _backfill_call_flags_for_attributed(days: int = 7) -> int:
    """
    Catch-up pass: create flags for any GAds-matched calls that don't yet have one.
    Handles the case where flag creation failed at match time (transient lock, etc.)
    and ensures flags are idempotent across reconcile runs.
    """
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    created = 0
    try:
        with _db_conn() as conn:
            rows = conn.execute("""
                SELECT mc.uuid, mc.duration_sec, mc.status, mc.od_patient_status,
                       mc.od_matched_at, mc.booked_outcome, mc.match_confidence,
                       mc.attributed_keyword, mc.gads_call_id,
                       gcv.campaign_name, gcv.call_status
                  FROM mango_calls mc
                  JOIN gads_call_view gcv ON gcv.call_id = mc.gads_call_id
                 WHERE mc.gads_call_id IS NOT NULL AND mc.gads_call_id != ''
                   AND mc.started_at >= ?
                   AND NOT EXISTS (
                     SELECT 1 FROM call_flags cf WHERE cf.uuid = mc.uuid
                   )
            """, (cutoff,)).fetchall()
            for r in rows:
                mc_dict = dict(r)
                gc_dict = {"call_status": r["call_status"], "campaign_name": r["campaign_name"]}
                _create_call_flag_if_needed(conn, mc_dict, gc_dict)
                created += 1
    except Exception as e:
        logger.warning(f"_backfill_call_flags_for_attributed failed: {e}")
    if created:
        logger.info(f"call_flags backfill: {created} flags created for previously-matched calls")
    return created


def _queue_process_if_needed(mc: dict) -> None:
    """
    If a newly-matched call hasn't been transcribed yet, queue it for full
    pipeline processing (transcription → summary → grading) in a background
    thread. Skips calls that are already done, in-progress, or too short to
    be worth transcribing (<15s).
    """
    status = mc.get("transcription_status") or ""
    duration = int(mc.get("duration_sec") or 0)
    uuid = mc.get("uuid") or ""

    if status in ("done", "in_progress"):
        return
    if duration < 15:
        logger.debug(f"[reconcile] Skipping auto-process for {uuid[:8]} — too short ({duration}s)")
        return

    logger.info(f"[reconcile] Queuing auto-process for newly matched call {uuid[:8]} ({duration}s)")

    def _run():
        try:
            from mango_pipeline import process_call
            from database import get_mango_call
            call_row = get_mango_call(uuid)
            if call_row:
                process_call(call_row)
        except Exception as e:
            logger.warning(f"[reconcile] Auto-process failed for {uuid[:8]}: {e}")

    t = threading.Thread(target=_run, daemon=True, name=f"auto-process-{uuid[:8]}")
    t.start()
