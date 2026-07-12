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
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

# No concurrency for auto-process — calls are processed one-by-one sequentially
# to avoid file-descriptor exhaustion (each process_call downloads from S3,
# calls OpenAI Whisper, Vertex AI, etc.).

from database import (
    upsert_mango_call,
    upsert_mango_calls_batch,
    get_mango_calls_unmatched,
    get_gads_call_view,
    get_all_leads,
    update_mango_call_attribution,
    backfill_call_keyword_attribution,
    upsert_lead,
    add_lead_event,
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
    Lazy initialization: credentials are stored at construction but the first
    actual login happens on the first get_token() call.  This prevents a Mango
    API 500 at startup from blocking the entire lifespan and leaving
    app.state.mango_token_mgr as None.
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
        # Lazy: do NOT call _do_login() here — first get_token() will trigger it.

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
            token = self._access_token
        if token is None:
            # First call after a failed startup — attempt login now
            try:
                self._do_login()
                with self._lock:
                    token = self._access_token
            except Exception as e:
                logger.error(f"Mango: deferred login failed: {e}")
                return None
        return token


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

    # After every sync, attempt to link any CallRail rows that arrived before
    # their Mango call was ingested (race condition: webhook fires ~8 min before sync).
    try:
        linked = _link_unmatched_callrail_to_mango()
        if linked:
            logger.info(f"Mango sync: linked {linked} CallRail row(s) to Mango calls (post-sync)")
    except Exception as _le:
        logger.warning(f"Mango sync: CallRail post-link failed (non-fatal): {_le}")

    return count


def _link_unmatched_callrail_to_mango(window_minutes: int = 3, days: int = 7) -> int:
    """
    For CallRail rows with mango_call_id='', try to match by phone + time to a
    mango_calls row within ±window_minutes. Runs after every Mango sync to fix
    the race where the CallRail webhook fires before the Mango call is ingested.
    Returns count of rows linked.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    linked = 0
    with _db_conn() as conn:
        unlinked = conn.execute("""
            SELECT id, caller_number, called_at, keyword, campaign, source
            FROM callrail_calls
            WHERE (mango_call_id IS NULL OR mango_call_id = '')
              AND called_at >= ?
            ORDER BY called_at DESC
        """, (cutoff,)).fetchall()

        for row in unlinked:
            caller = row["caller_number"] or ""
            called_at_str = row["called_at"] or ""
            cr_keyword = (row["keyword"] or "").strip()
            cr_campaign = (row["campaign"] or "").strip()
            cr_source = (row["source"] or "").strip()
            if not caller or not called_at_str:
                continue
            try:
                called_dt = datetime.fromisoformat(called_at_str.replace("Z", "+00:00"))
                if called_dt.tzinfo is None:
                    called_dt = called_dt.replace(tzinfo=timezone.utc)
                called_utc = called_dt.astimezone(timezone.utc)
            except Exception:
                continue

            window_start = (called_utc - timedelta(minutes=window_minutes)).isoformat()
            window_end   = (called_utc + timedelta(minutes=window_minutes)).isoformat()

            digits = re.sub(r"\D", "", caller)
            # Use last-10-digits comparison to handle format mismatches:
            # CallRail stores "+17744524631", Mango stores "(774) 452-4631".
            # REPLACE strips common formatting chars from from_number for comparison.
            last10 = digits[-10:] if len(digits) >= 10 else digits

            mango_row = conn.execute("""
                SELECT uuid FROM mango_calls
                WHERE direction = 'inbound'
                  AND REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
                      from_number, '(', ''), ')', ''), '-', ''), ' ', ''), '+', '')
                      LIKE '%' || ?
                  AND started_at BETWEEN ? AND ?
                ORDER BY ABS(strftime('%s', started_at) - strftime('%s', ?))
                LIMIT 1
            """, (last10, window_start, window_end, called_utc.isoformat())).fetchone()

            if mango_row:
                mango_uuid = mango_row["uuid"]
                conn.execute("""
                    UPDATE callrail_calls
                    SET mango_call_id = ?
                    WHERE id = ? AND (mango_call_id IS NULL OR mango_call_id = '')
                """, (mango_uuid, row["id"]))
                linked += 1

                # Write CallRail keyword attribution to the Mango call if:
                # 1. This CallRail row is a google_ads call with a keyword
                # 2. The Mango call doesn't already have a higher-quality keyword method
                if cr_keyword and (cr_source or "").lower().replace(" ", "_") == "google_ads":
                    ag_display = f"{cr_campaign} > " if cr_campaign else ""
                    conn.execute("""
                        UPDATE mango_calls SET
                            attributed_keyword = ?,
                            attributed_keyword_method = 'callrail_keyword',
                            attributed_keyword_confidence = 0.95,
                            attributed_ad_group = CASE
                                WHEN ? != '' AND (attributed_ad_group IS NULL OR attributed_ad_group = '')
                                THEN ?
                                ELSE attributed_ad_group
                            END,
                            updated_at = ?
                        WHERE uuid = ?
                          AND (attributed_keyword_method NOT IN ('skag_direct', 'callrail_keyword')
                               OR attributed_keyword_method IS NULL
                               OR attributed_keyword_method = '')
                    """, (cr_keyword, ag_display, ag_display,
                          datetime.now(timezone.utc).isoformat(), mango_uuid))

    return linked


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


def finalize_call_lead(uuid: str) -> dict:
    """Copy attribution + OD match results from mango_calls (and joined callrail_calls)
    back to the linked lead. Idempotent — only fills empty fields, never overwrites.

    Called at:
      1. End of process_call() — after OD match and booked_outcome are written
      2. After backfill_call_keyword_attribution() in reconcile_attribution()

    Returns dict with {"updated": [list of fields written]} or {} if no lead linked.
    """
    # High-confidence attribution methods that carry real keyword data.
    # callrail_keyword = actual Google search query from user's browser (DNI) — highest fidelity.
    _KEYWORD_METHODS = {"callrail_keyword", "skag_direct", "call_search_term", "ad_group_best_keyword"}

    try:
        with _db_conn() as conn:
            # Fetch mango_call row + joined callrail_call (left join — may not exist)
            row = conn.execute("""
                SELECT
                    mc.lead_id,
                    mc.attributed_keyword,
                    mc.attributed_ad_group,
                    mc.attributed_keyword_method,
                    mc.gads_call_id,
                    mc.od_patient_num       AS mc_od_patient_num,
                    mc.od_patient_status    AS mc_od_patient_status,
                    mc.od_matched_at        AS mc_od_matched_at,
                    cc.source               AS cr_source,
                    cc.gclid                AS cr_gclid,
                    cc.campaign             AS cr_campaign,
                    cc.landing_page         AS cr_landing_page,
                    -- ATTR-FIX3: join gads_call_view for campaign_id/name
                    gcv.campaign_id         AS gcv_campaign_id,
                    gcv.campaign_name       AS gcv_campaign_name
                FROM mango_calls mc
                -- Deterministic subquery: multiple callrail_calls rows can exist per
                -- mango_call (call.created + call.completed). Prefer the row with gclid,
                -- then most recent event, so attribution fields are consistent.
                LEFT JOIN callrail_calls cc ON cc.id = (
                    SELECT id FROM callrail_calls
                    WHERE mango_call_id = mc.uuid
                    ORDER BY (COALESCE(gclid,'') != '') DESC, called_at DESC
                    LIMIT 1
                )
                LEFT JOIN gads_call_view gcv ON gcv.call_id = mc.gads_call_id
                WHERE mc.uuid = ?
            """, (uuid,)).fetchone()

            if not row or not row["lead_id"]:
                logger.debug(f"finalize_call_lead: no lead linked for {uuid}")
                return {}

            lead_id = row["lead_id"]

            # Fetch current lead state (only the fields we may update)
            lead = conn.execute("""
                SELECT keyword_text, ad_group_name, campaign_name, campaign_id, gclid,
                       od_patient_num, existing_patient, paid_source, landing_url
                FROM leads WHERE id = ?
            """, (lead_id,)).fetchone()

            if not lead:
                logger.warning(f"finalize_call_lead: lead {lead_id} not found")
                return {}

            updates: dict = {"id": lead_id}

            # ── 1. OD patient linkage (biggest ROI impact) ───────────────────
            if row["mc_od_patient_num"] and not lead["od_patient_num"]:
                updates["od_patient_num"] = row["mc_od_patient_num"]
                if row["mc_od_matched_at"]:
                    updates["od_matched_at"] = row["mc_od_matched_at"]
                # existing_patient: sticky upgrade via upsert_lead (database.py:3237).
                # upsert_lead only writes existing_patient=1, never demotes to 0.
                # Note: if od_patient_status is not existing_* (e.g. new patient),
                # we intentionally do not set existing_patient=1 here.
                if row["mc_od_patient_status"] in ("existing_active", "existing_inactive"):
                    updates["existing_patient"] = 1

            # ── 2. Keyword + ad group (only high-confidence methods) ─────────
            kw_method = row["attributed_keyword_method"] or ""
            if (row["attributed_keyword"]
                    and kw_method in _KEYWORD_METHODS
                    and not lead["keyword_text"]):
                updates["keyword_text"] = row["attributed_keyword"]

            if row["attributed_ad_group"] and not lead["ad_group_name"]:
                ag_raw = row["attributed_ad_group"]
                # Format is "Campaign Name > Ad Group Name"
                if " > " in ag_raw:
                    camp_part, ag_part = ag_raw.split(" > ", 1)
                    updates["ad_group_name"] = ag_part.strip()
                    if not lead["campaign_name"]:
                        updates["campaign_name"] = camp_part.strip()
                    # ATTR-FIX3: also set campaign_id from gads_call_view
                    if not lead["campaign_id"] and row.get("gcv_campaign_id"):
                        updates["campaign_id"] = str(row["gcv_campaign_id"])
                else:
                    updates["ad_group_name"] = ag_raw.strip()
            # ATTR-FIX3: campaign_id/name even if ad_group_name already set
            if not lead.get("campaign_id") and row.get("gcv_campaign_id") and "campaign_id" not in updates:
                updates["campaign_id"] = str(row["gcv_campaign_id"])
            if not lead.get("campaign_name") and row.get("gcv_campaign_name") and "campaign_name" not in updates:
                updates["campaign_name"] = row["gcv_campaign_name"]

            # ── 3. gclid backstop (webhook-miss recovery) ────────────────────
            if row["cr_gclid"] and not lead["gclid"]:
                updates["gclid"] = row["cr_gclid"]

            # ── 4. paid_source (google_ads | organic | direct | '') ──────────
            if row["cr_source"] and not lead["paid_source"]:
                updates["paid_source"] = row["cr_source"]

            # ── 5. landing_url fallback ───────────────────────────────────────
            if row["cr_landing_page"] and not lead["landing_url"]:
                updates["landing_url"] = row["cr_landing_page"]

            # Nothing to write
            written = [k for k in updates if k != "id"]
            if not written:
                logger.debug(f"finalize_call_lead: nothing to update for lead {lead_id}")
                return {}

            upsert_lead(updates)
            add_lead_event(
                lead_id,
                "call_lead_finalized",
                detail=json.dumps({
                    "mango_call_uuid": uuid,
                    "backfilled": written,
                }),
                source="mango_pipeline",
            )
            logger.info(f"finalize_call_lead: wrote {written} to lead {lead_id} (call {uuid})")
            return {"updated": written}

    except Exception as exc:
        logger.warning(f"finalize_call_lead: failed for {uuid}: {exc}")
        return {}


# ── Gemini transcript campaign inference ─────────────────────────────────────

_CAMPAIGN_INFERENCE_PROMPT = """\
You are an expert dental marketing analyst. Given a phone call transcript and a list of \
currently-running Google Ads campaigns, determine which campaign (if any) most likely \
generated this call.

## Active Campaigns
{campaign_context}

## Call Transcript
{transcript}

## Instructions
Analyze the transcript to identify the dental service the caller is inquiring about, then \
match it to the most likely campaign from the list above.

Confidence levels:
- "high" — caller explicitly mentioned a service or concern that maps clearly to one campaign
- "medium" — a service is discussed but could plausibly match multiple campaigns
- "low" — the call is vague, off-topic, or the service discussed has only a weak link to any campaign

Output JSON only:
{{"campaign_name": "<exact campaign name from list, or \\"none\\">", "confidence": "high|medium|low", "reasoning": "<one sentence explaining the match>"}}
"""


def _build_campaign_context_for_inference(call_time: str = "") -> str:
    """Build a structured text block describing campaigns that were ACTIVE at call_time.

    Date-aware filtering: uses campaign_status_history to determine which campaigns
    were ACTIVE when the call was made, not just which are active NOW. This prevents
    both stale attribution (paused campaigns leaking in) and lost attribution
    (historical calls not matching campaigns that were active at the time).

    Fallback: if no call_time or no history data, uses current status == ACTIVE.
    """
    with _db_conn() as conn:
        # Get campaigns with recent keyword impressions (last 30 days)
        kw_rows = conn.execute("""
            SELECT campaign_name, keyword_text, match_type, impressions
            FROM gads_keywords_cache
            WHERE days = 30 AND campaign_name != '' AND impressions > 0
            ORDER BY campaign_name, impressions DESC
        """).fetchall()

        # Get campaign metadata
        all_campaigns = conn.execute("""
            SELECT campaign_id, campaign_name, service_focus, target_audience,
                   promo_offer, status, activated_at, paused_at
            FROM campaigns
            WHERE campaign_name != ''
        """).fetchall()

        # Build name→campaign_id mapping for history lookup
        name_to_cid = {c["campaign_name"]: c["campaign_id"] for c in all_campaigns}

        # Determine which campaigns were active at call_time using history
        allowed_names: set[str] = set()
        if call_time:
            # For each campaign, find its status at call_time from history
            for camp in all_campaigns:
                cid = camp["campaign_id"]
                cname = camp["campaign_name"]
                cstatus = (camp["status"] or "").upper()

                # Quick check: if activated_at <= call_time and
                # (paused_at is empty OR paused_at > call_time) → was active
                act_at = camp["activated_at"] or ""
                pau_at = camp["paused_at"] or ""

                if act_at and act_at <= call_time:
                    if not pau_at or pau_at > call_time:
                        allowed_names.add(cname)
                        continue

                # Fall back to history table for more complex cases
                # (multiple on/off cycles)
                last_before = conn.execute("""
                    SELECT status FROM campaign_status_history
                    WHERE campaign_id = ? AND changed_at <= ?
                    ORDER BY changed_at DESC, id DESC
                    LIMIT 1
                """, (cid, call_time)).fetchone()

                if last_before:
                    if last_before["status"] == "ACTIVE":
                        allowed_names.add(cname)
                elif cstatus == "ACTIVE":
                    # No history before call_time — if currently active,
                    # assume it was active (conservative fallback)
                    allowed_names.add(cname)
        else:
            # No call_time — fall back to current status
            allowed_names = {c["campaign_name"] for c in all_campaigns
                            if (c["status"] or "").upper() == "ACTIVE"}

    # Group keywords by campaign, top 10 each
    kw_by_campaign: dict[str, list[str]] = {}
    kw_count: dict[str, int] = {}
    active_campaign_names: set[str] = set()
    for row in kw_rows:
        cname = row["campaign_name"]
        active_campaign_names.add(cname)
        if kw_count.get(cname, 0) >= 10:
            continue
        kw_count[cname] = kw_count.get(cname, 0) + 1
        kw_by_campaign.setdefault(cname, []).append(
            f'{row["keyword_text"]} ({row["match_type"]}, {row["impressions"]} imp)'
        )

    # Intersect: must have both impressions AND been active at call_time
    meta_by_name = {c["campaign_name"]: c for c in all_campaigns}
    excluded = active_campaign_names - allowed_names
    if excluded:
        logger.info(f"[gemini_inference] excluding {len(excluded)} campaign(s) not active at "
                    f"{call_time or 'now'}: {', '.join(sorted(excluded))}")
    active_campaign_names &= allowed_names

    lines = []
    for cname in sorted(active_campaign_names):
        block = f"Campaign: {cname}"
        meta = meta_by_name.get(cname)
        if meta:
            if meta["service_focus"]:
                block += f"\n  Service Focus: {meta['service_focus']}"
            if meta["target_audience"]:
                block += f"\n  Target Audience: {meta['target_audience']}"
            if meta["promo_offer"]:
                block += f"\n  Promo: {meta['promo_offer']}"
        kws = kw_by_campaign.get(cname, [])
        if kws:
            block += f"\n  Top Keywords: {', '.join(kws)}"
        lines.append(block)

    logger.info(f"[gemini_inference] campaign context: {len(active_campaign_names)} campaigns "
                f"active at {call_time or 'now'} (filtered by 30-day impressions)")
    return "\n\n".join(lines) if lines else "(no active campaigns found)"


def infer_campaign_from_transcript(mc: dict) -> Optional[dict]:
    """
    Use Gemini to infer which campaign a call came from based on transcript content.

    Args:
        mc: A mango_calls row dict with at least call_transcript or call_summary.

    Returns:
        {"campaign_name": str, "confidence": str, "reasoning": str} or None on failure.
    """
    transcript = (mc.get("call_transcript") or mc.get("call_summary") or "").strip()
    if len(transcript) < 50:
        return None

    try:
        call_time = mc.get("started_at") or ""
        campaign_context = _build_campaign_context_for_inference(call_time=call_time)
        prompt = _CAMPAIGN_INFERENCE_PROMPT.format(
            campaign_context=campaign_context,
            transcript=transcript[:6000],  # cap transcript to avoid token bloat
        )

        from mango_pipeline import _call_vertex
        from config import get_settings
        cfg = get_settings()

        text, in_tok, out_tok = _call_vertex(
            prompt=prompt,
            model="gemini-2.5-flash",
            project_id=cfg.vertex_project_id,
            location=cfg.vertex_location,
            credentials_path=cfg.vertex_credentials_path,
            temperature=0.1,
            max_tokens=1200,
        )

        if not text:
            logger.warning("infer_campaign_from_transcript: empty Vertex response")
            return None

        logger.info("infer_campaign_from_transcript: raw response (%d chars, in=%d out=%d): %s", len(text), in_tok, out_tok, text[:500])

        # Gemini sometimes wraps JSON in markdown fences despite response_mime_type
        clean = text.strip()
        if clean.startswith("```"):
            # Strip ```json ... ``` or ``` ... ```
            lines = clean.split("\n")
            # Remove first line (```json) and last line (```)
            lines = [l for l in lines if not l.strip().startswith("```")]
            clean = "\n".join(lines).strip()
        # Also strip any preamble text before the JSON object
        if "{" in clean and "}" in clean:
            clean = clean[clean.index("{"):clean.rindex("}") + 1]

        if not clean or clean[0] != "{":
            logger.warning("infer_campaign_from_transcript: no JSON object in response: %s", text[:300])
            return None

        result = json.loads(clean)
        # Validate expected keys
        if "campaign_name" not in result:
            logger.warning("infer_campaign_from_transcript: missing campaign_name in response: %s", text[:200])
            return None

        result.setdefault("confidence", "low")
        result.setdefault("reasoning", "")
        logger.info(
            "infer_campaign_from_transcript: campaign=%s confidence=%s tokens=%d/%d",
            result["campaign_name"], result["confidence"], in_tok, out_tok,
        )
        return result

    except json.JSONDecodeError as e:
        logger.warning("infer_campaign_from_transcript: JSON parse error: %s (raw: %s)", e, text[:200] if 'text' in dir() else "N/A")
        return None
    except Exception as e:
        logger.warning("infer_campaign_from_transcript: failed: %s", e)
        return None


def reconcile_attribution(days: int = 7, target_gads_call_id: str = "", mango_token: Optional[str] = None) -> int:
    """
    Match unattributed inbound Mango calls against:
      1. CallRail-confirmed Google Ads calls (authoritative — CallRail is ground truth)
      2. Lead phone numbers (existing leads who called in)

    Updates mango_calls.lead_id / gads_call_id / match_confidence / match_method.
    Safe to run repeatedly — only updates NULL attribution rows.

    Attribution model (CallRail-first, no time-window guessing):
      callrail_confirmed 0.95 — CallRail row linked to this Mango call with source='google_ads'.
                                 Finds closest gads_call_view row within ±60s for reporting.
                                 Writes callrail_keyword if CallRail captured the search query.
      phone_exact        0.90 — Caller phone matches an existing lead record.

    After any CallRail-confirmed GAds match: creates a call_flag if the call was missed or short.
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

    attributed = 0

    # ── Main attribution loop ─────────────────────────────────────────────────
    # Track which GAds call_ids have already been claimed this pass so we never
    # assign the same GAds call to two different Mango records (can happen when
    # two back-to-back ad calls arrive within the callrail_confirmed ±60s window).
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

        # Pre-check: is this caller a confirmed existing OD patient?
        # Existing patients did NOT come from an ad — block CallRail GAds attribution
        # to prevent billing calls from showing as ad conversions.
        _od_status = (mc.get("od_patient_status") or "").strip()
        _mc_is_existing_patient = _od_status in ("existing_active", "existing_inactive")

        # ── CallRail-confirmed GAds attribution (highest priority) ────────────
        # CallRail is the ground truth: if a linked CallRail row has source='google_ads',
        # this call came from an ad. Find the matching gads_call_view entry by time (±60s)
        # for reporting purposes, and write the CallRail keyword if captured.
        _cr_gads_confirmed = False
        with _db_conn() as _cr_conn:
            _cr_row = _cr_conn.execute("""
                SELECT cr.source, cr.called_at, cr.keyword, cr.campaign
                FROM callrail_calls cr
                WHERE cr.mango_call_id = ? AND LOWER(REPLACE(cr.source, ' ', '_')) = 'google_ads'
                ORDER BY CASE WHEN cr.gclid != '' AND cr.gclid IS NOT NULL THEN 0 ELSE 1 END,
                         cr.called_at DESC
                LIMIT 1
            """, (mc_uuid,)).fetchone()
        if _cr_row and not _mc_is_existing_patient:
            _cr_keyword = (_cr_row["keyword"] or "").strip()
            _cr_campaign = (_cr_row["campaign"] or "").strip()

            # Find best gads_call_view match by time only (±60s) — CallRail already
            # confirmed it's Google Ads so we just need the call_id for reporting.
            _cr_best_id   = None
            _cr_best_gc   = None
            _cr_best_delta = float("inf")
            for gc, gc_dt in gads_parsed:
                if gc["call_id"] in _used_gads_ids:
                    continue
                _delta = abs((mc_dt - gc_dt).total_seconds())
                if _delta <= 60 and _delta < _cr_best_delta:
                    _cr_best_delta = _delta
                    _cr_best_id    = gc["call_id"]
                    _cr_best_gc    = gc

            # Determine attributed_ad_group from gads_call_view if matched,
            # or from CallRail campaign name as fallback (for DNI-pool calls).
            _cr_ag_display = ""
            if _cr_best_gc:
                _gcv_campaign = _cr_best_gc.get("campaign_name", "") or ""
                _gcv_ag       = _cr_best_gc.get("ad_group_name", "") or ""
                _cr_ag_display = f"{_gcv_campaign} > {_gcv_ag}" if _gcv_ag else _gcv_campaign
            elif _cr_campaign:
                _cr_ag_display = f"{_cr_campaign} > "

            # Build keyword kwargs — only pass when keyword is present and current
            # method is not already authoritative (skag_direct or callrail_keyword).
            _cur_kw_method = (mc.get("attributed_keyword_method") or "").strip()
            _write_kw = _cr_keyword and _cur_kw_method not in ("skag_direct", "callrail_keyword")
            _kw_kwargs: dict = {}
            if _write_kw:
                _kw_kwargs = dict(
                    attributed_keyword=_cr_keyword,
                    attributed_keyword_method="callrail_keyword",
                    attributed_keyword_confidence=0.95,
                    attributed_ad_group=_cr_ag_display or None,
                )
            # ATTR-FIX 2026-07-06: call-extension (tap-to-call) calls are CallRail-
            # confirmed google_ads but usually have NO search keyword (no web session /
            # DNI swap happened — the number is dialed straight off the ad). Previously
            # _kw_kwargs stayed empty in that case, so attributed_ad_group (the field the
            # calls-list and campaign rollups read for "campaign") was never written even
            # though CallRail's `campaign` field (_cr_campaign) was known. Fix: when there's
            # no keyword but we do have a CallRail campaign and _kw_kwargs is still empty,
            # write attributed_ad_group alone (method stays distinguishable via
            # 'callrail_campaign_only' so we don't overwrite a real keyword-level method).
            elif _cr_ag_display and _cur_kw_method not in ("skag_direct", "callrail_keyword", "callrail_campaign_only"):
                _kw_kwargs = dict(
                    attributed_keyword_method="callrail_campaign_only",
                    attributed_keyword_confidence=0.60,
                    attributed_ad_group=_cr_ag_display,
                )

            if _cr_best_id:
                _used_gads_ids.add(_cr_best_id)
                update_mango_call_attribution(
                    uuid=mc_uuid,
                    gads_call_id=_cr_best_id,
                    match_confidence=0.95,
                    match_method="callrail_confirmed",
                    **_kw_kwargs,
                )
                attributed += 1
                _queue_process_if_needed(mc, mango_token=mango_token)
                try:
                    with _db_conn() as _fc_conn:
                        mc_fresh = dict(mc)
                        mc_fresh["match_confidence"] = 0.95
                        _create_call_flag_if_needed(_fc_conn, mc_fresh, _cr_best_gc)
                except Exception as _fe:
                    logger.warning(f"call_flag creation failed for {mc_uuid}: {_fe}")
                logger.info(
                    "reconcile[callrail] Mango=%s -> GAds=%s (time_delta=%.0fs) kw=%s",
                    mc_uuid, _cr_best_id, _cr_best_delta, _cr_keyword or "(none)",
                )
            else:
                # CallRail confirms GAds but no gads_call_view row found yet (DNI pool
                # call or delayed gads_call_view sync). Write keyword attribution now
                # so the call shows correct keyword even without a gads_call_id.
                if _kw_kwargs:
                    update_mango_call_attribution(uuid=mc_uuid, **_kw_kwargs)
                logger.info(
                    "reconcile[callrail] Mango=%s confirmed google_ads by CallRail "
                    "but no gads_call_view row within 60s — kw=%s",
                    mc_uuid, _cr_keyword or "(none)",
                )
            _cr_gads_confirmed = True

        if _cr_gads_confirmed:
            # ATTR-FIX3 2026-07-11: link lead_id on GAds-attributed calls.
            # Previously, callrail_confirmed path set gads_call_id but skipped
            # phone→lead matching, so finalize_call_lead() never ran and the
            # lead's campaign_name was never backfilled from the call's attribution.
            _from_digits = re.sub(r"\D", "", mc.get("from_number", "") or "")
            if len(_from_digits) >= 10:
                _matched_lead_id = phone_to_lead.get(_from_digits[-10:])
                if _matched_lead_id and not (mc.get("lead_id") or "").strip():
                    update_mango_call_attribution(uuid=mc_uuid, lead_id=_matched_lead_id)
                    logger.info("reconcile[lead_link] Mango=%s -> lead=%s (phone match after GAds attr)", mc_uuid, _matched_lead_id)
            continue

        # ATTR-FIX2 2026-07-07: direct gads_call_view time-match (no CallRail bridge).
        # The CallRail source='google_ads' bridge row (matched above) stopped being
        # produced after the DNI pool broke ~May 22 (call-extension number
        # recategorized), so every ad call since has gone unattributed even though
        # gads_call_view itself is fully populated. Google redacts the caller's area
        # code on call-extension rows, so time (±60s) is the only signal available —
        # match directly here instead of depending on the broken bridge.
        # Only consider RECEIVED (answered) gads rows; MISSED/0-duration rows have
        # no reliable time anchor and would produce false matches. Attribute the ad
        # touch regardless of new/existing patient status — the existing-patient
        # *conversion* exclusion is a separate downstream rule, not applied here.
        _dm_best_id, _dm_best_gc, _dm_best_delta = None, None, float("inf")
        for gc, gc_dt in gads_parsed:
            if gc["call_id"] in _used_gads_ids:
                continue
            if (gc.get("call_status") or "") != "RECEIVED":
                continue
            if int(gc.get("call_duration_sec") or 0) <= 0:
                continue
            _delta = abs((mc_dt - gc_dt).total_seconds())
            if _delta <= 60 and _delta < _dm_best_delta:
                _dm_best_delta, _dm_best_id, _dm_best_gc = _delta, gc["call_id"], gc

        # Don't clobber a call that already has campaign attribution from some
        # other path (defensive — get_mango_calls_unmatched already filters on
        # gads_call_id IS NULL, but attributed_ad_group could theoretically be
        # set without a gads_call_id in edge cases, so guard on it too).
        if _dm_best_id and not (mc.get("attributed_ad_group") or "").strip():
            _dm_campaign = _dm_best_gc.get("campaign_name", "") or ""
            _dm_ag       = _dm_best_gc.get("ad_group_name", "") or ""
            _dm_ag_display = f"{_dm_campaign} > {_dm_ag}" if _dm_ag else _dm_campaign

            _used_gads_ids.add(_dm_best_id)
            update_mango_call_attribution(
                uuid=mc_uuid,
                gads_call_id=_dm_best_id,
                match_confidence=0.85,
                match_method="gads_time_match",
                attributed_ad_group=_dm_ag_display,
                # No keyword: call-extension (tap-to-call) calls have no search term.
            )
            attributed += 1
            _queue_process_if_needed(mc, mango_token=mango_token)
            try:
                with _db_conn() as _fc_conn:
                    mc_fresh = dict(mc)
                    mc_fresh["match_confidence"] = 0.85
                    _create_call_flag_if_needed(_fc_conn, mc_fresh, _dm_best_gc)
            except Exception as _fe:
                logger.warning(f"call_flag creation failed for {mc_uuid}: {_fe}")
            logger.info(
                "reconcile[gads_time_match] Mango=%s -> GAds=%s (time_delta=%.0fs)",
                mc_uuid, _dm_best_id, _dm_best_delta,
            )
            # ATTR-FIX3: link lead_id on gads_time_match calls (same as callrail_confirmed above)
            _from_digits = re.sub(r"\D", "", mc.get("from_number", "") or "")
            if len(_from_digits) >= 10:
                _matched_lead_id = phone_to_lead.get(_from_digits[-10:])
                if _matched_lead_id and not (mc.get("lead_id") or "").strip():
                    update_mango_call_attribution(uuid=mc_uuid, lead_id=_matched_lead_id)
                    logger.info("reconcile[lead_link] Mango=%s -> lead=%s (phone match after GAds attr)", mc_uuid, _matched_lead_id)
            continue

        # ── Gemini transcript campaign inference ──────────────────────────────
        # For GAds calls (callrail_source='google_ads') with no gclid and no
        # gads_call_view match, try to infer campaign from the call transcript.
        if not _cr_gads_confirmed and not _dm_best_id:
            _is_gads_no_gclid = False
            with _db_conn() as _gi_conn:
                _gi_row = _gi_conn.execute("""
                    SELECT 1 FROM callrail_calls
                    WHERE mango_call_id = ? AND LOWER(REPLACE(source, ' ', '_')) = 'google_ads'
                    LIMIT 1
                """, (mc_uuid,)).fetchone()
                _is_gads_no_gclid = bool(_gi_row)

            if _is_gads_no_gclid and mc.get("call_transcript"):
                result = infer_campaign_from_transcript(mc)
                if result and result.get("campaign_name") and result["campaign_name"] != "none":
                    _conf_map = {"high": 0.80, "medium": 0.65, "low": 0.45}
                    _num_conf = _conf_map.get(result.get("confidence", "low"), 0.45)
                    _method = "gemini_inferred" if _num_conf >= 0.65 else "gemini_low_confidence"

                    _gi_ag_display = f"{result['campaign_name']} > (gemini-inferred)"

                    update_mango_call_attribution(
                        uuid=mc_uuid,
                        match_confidence=_num_conf,
                        match_method=_method,
                        attributed_ad_group=_gi_ag_display,
                        attributed_keyword_method="gemini_inferred",
                        attributed_keyword_confidence=_num_conf,
                    )
                    attributed += 1
                    logger.info(
                        "reconcile[gemini_inferred] Mango=%s -> campaign=%s (confidence=%s, reason=%s)",
                        mc_uuid, result["campaign_name"], result["confidence"], result.get("reasoning", "")[:80],
                    )
                    # ATTR-FIX3: link lead_id on gemini-inferred calls too
                    _from_digits = re.sub(r"\D", "", mc.get("from_number", "") or "")
                    if len(_from_digits) >= 10:
                        _matched_lead_id = phone_to_lead.get(_from_digits[-10:])
                        if _matched_lead_id and not (mc.get("lead_id") or "").strip():
                            update_mango_call_attribution(uuid=mc_uuid, lead_id=_matched_lead_id)
                            logger.info("reconcile[lead_link] Mango=%s -> lead=%s (phone match after Gemini attr)", mc_uuid, _matched_lead_id)
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
                _queue_process_if_needed(mc, mango_token=mango_token)

    logger.info(f"Mango reconcile: attributed {attributed}/{len(unmatched)} calls")

    # Backfill keyword attribution on any calls matched to a GAds call_view row
    # (call_view doesn't expose the triggering keyword directly — we derive it from
    # the best keyword in the matched ad group using gads_keyword_perf cache)
    try:
        kw_backfilled = backfill_call_keyword_attribution()
        if kw_backfilled:
            logger.info(f"Backfilled keyword attribution on {kw_backfilled} call(s)")
    except Exception as _kwe:
        logger.warning(f"backfill_call_keyword_attribution failed (non-fatal): {_kwe}")

    # Copy attribution + OD match back to lead records for all recently-linked calls.
    # Runs after keyword backfill so attributed_keyword is already populated.
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with _db_conn() as _fc_conn:
            linked_uuids = [
                r[0] for r in _fc_conn.execute("""
                    SELECT uuid FROM mango_calls
                    WHERE lead_id IS NOT NULL AND lead_id != ''
                      AND started_at >= ?
                """, (cutoff,)).fetchall()
            ]
        fc_count = 0
        for _uuid in linked_uuids:
            result = finalize_call_lead(_uuid)
            if result.get("updated"):
                fc_count += 1
        if fc_count:
            logger.info(f"finalize_call_lead: enriched {fc_count} lead(s) during reconcile")
    except Exception as _fce:
        logger.warning(f"finalize_call_lead reconcile pass failed (non-fatal): {_fce}")

    # §2.3a: Auto-create leads for GAds-attributed calls with no lead
    try:
        auto_leads = _auto_create_leads_for_gads_calls(days=days)
        if auto_leads:
            logger.info(f"auto_create_leads: created {auto_leads} lead(s) during reconcile")
    except Exception as _ale:
        logger.warning(f"auto_create_leads failed (non-fatal): {_ale}")

    # Backfill flags for previously-attributed GAds calls that may have missed flag creation
    _backfill_call_flags_for_attributed(days=days)

    # Flag unattributed missed new-patient calls (organic/GMB/direct-dial)
    _flag_unattributed_missed_new_patients(days=days)

    return attributed


def _auto_create_leads_for_gads_calls(days: int = 7) -> int:
    """Auto-create lead records for GAds-attributed calls with no linked lead.

    Runs as part of reconcile_attribution (after finalize_call_lead).
    Only creates leads for calls that pass the qualifying filter:
      - gads_call_id present (confirmed in Google's call_view)
      - inbound, >= 60s duration
      - not existing patient
      - not spam/wrong_number/not_qualified
      - no existing lead by phone match

    Returns count of leads created.
    """
    import uuid as _uuid
    from database import upsert_lead, get_lead_by_phone, update_mango_call_attribution

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    created = 0

    with _db_conn() as conn:
        rows = conn.execute("""
            SELECT
                mc.uuid, mc.from_number, mc.caller_id_name, mc.duration_sec,
                mc.started_at, mc.lead_id,
                mc.gads_call_id, mc.attributed_ad_group,
                mc.od_patient_status,
                mc.ai_patient_name, mc.ai_appointment_scheduled,
                mc.booked_outcome, mc.call_category, mc.lead_quality,
                mc.answered_by, mc.status AS mc_status,
                mc.call_transcript,
                gcv.campaign_id   AS gcv_campaign_id,
                gcv.campaign_name AS gcv_campaign_name
            FROM mango_calls mc
            LEFT JOIN gads_call_view gcv ON gcv.call_id = mc.gads_call_id
            WHERE mc.direction = 'inbound'
              AND mc.gads_call_id IS NOT NULL AND mc.gads_call_id != ''
              AND mc.duration_sec >= 60
              AND (mc.lead_id IS NULL OR mc.lead_id = '')
              AND COALESCE(mc.od_patient_status, '') NOT IN ('existing_active', 'existing_inactive')
              AND mc.started_at >= ?
        """, (cutoff,)).fetchall()

    import json as _json
    _seen_phones: set = set()  # Within-batch dedup

    for r in rows:
        from_number = r["from_number"] or ""
        from_digits = re.sub(r"\D", "", from_number)
        if len(from_digits) < 10:
            continue

        phone_last10 = from_digits[-10:]
        if phone_last10 in _seen_phones:
            continue

        # Skip spam/junk
        cat = (r["call_category"] or "").lower()
        qual = (r["lead_quality"] or "").lower()
        if cat in ("spam", "wrong_number") or qual == "not_qualified":
            continue

        # Check for existing lead by phone
        existing_lead = get_lead_by_phone(from_number)
        if existing_lead:
            # Link call to existing lead (backfill campaign if empty)
            update_mango_call_attribution(uuid=r["uuid"], lead_id=existing_lead["id"])
            if not existing_lead.get("campaign_id") and r["gcv_campaign_id"]:
                upsert_lead({
                    "id": existing_lead["id"],
                    "campaign_id": r["gcv_campaign_id"],
                    "campaign_name": r["gcv_campaign_name"] or "",
                })
            finalize_call_lead(r["uuid"])
            _seen_phones.add(phone_last10)
            logger.info(f"auto_create_leads: linked Mango={r['uuid']} -> existing lead={existing_lead['id']}")
            continue

        # Determine best name (title-case all-caps caller IDs, swap LAST FIRST → First Last)
        ai_name = (r["ai_patient_name"] or "").strip()
        caller_id = (r["caller_id_name"] or "").strip()
        if ai_name:
            name_parts = ai_name.split(None, 1)
        elif caller_id:
            _name = caller_id.title() if caller_id.isupper() else caller_id
            if caller_id.isupper() and len(_name.split()) == 2:
                parts = _name.split()
                name_parts = [parts[1], parts[0]]
            else:
                name_parts = _name.split(None, 1)
        else:
            name_parts = ["Unknown"]

        first_name = name_parts[0] if name_parts else "Unknown"
        last_name = name_parts[1] if len(name_parts) > 1 else ""

        # Determine tier for tagging
        mc_status = (r["mc_status"] or "").lower()
        is_missed = mc_status in ("missed", "voicemail") or not r["answered_by"]
        booked = (r["booked_outcome"] or "").lower() == "booked" or r["ai_appointment_scheduled"] == 1

        if booked:
            tier = "A"
        elif is_missed and r["duration_sec"] >= 15:
            tier = "C"
        elif is_missed:
            tier = "B"
        elif r["answered_by"] or (r["call_transcript"] and len(r["call_transcript"] or "") > 50):
            tier = "A"
        else:
            tier = "D"

        lead_id = str(_uuid.uuid4())
        lead_data = {
            "id": lead_id,
            "created_at": r["started_at"],
            "source": "google_ads_call",
            "stage": "new",
            "first_name": first_name,
            "last_name": last_name,
            "phone": from_number,
            "campaign_name": r["gcv_campaign_name"] or "",
            "campaign_id": r["gcv_campaign_id"] or "",
            "utm_source": "google",
            "utm_medium": "cpc",
            "tags": _json.dumps(["auto_created", f"call_tier_{tier}"]),
            "notes": f"Auto-created from GAds call {r['uuid']}. Tier {tier}.",
        }
        upsert_lead(lead_data)
        update_mango_call_attribution(uuid=r["uuid"], lead_id=lead_id)
        finalize_call_lead(r["uuid"])
        _seen_phones.add(phone_last10)
        created += 1
        logger.info(
            "auto_create_leads: created lead=%s for Mango=%s (%s %s, campaign=%s, tier=%s)",
            lead_id, r["uuid"], first_name, last_name, r["gcv_campaign_name"] or "unknown", tier,
        )

    return created


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


def _queue_process_if_needed(mc: dict, mango_token: Optional[str] = None) -> None:
    """
    If a newly-matched call hasn't been transcribed yet, queue it for full
    pipeline processing (transcription → summary → grading) in a background
    thread. Skips calls that are already done, in-progress, or too short to
    be worth transcribing (<15s).

    mango_token must be passed so process_call can fetch a fresh pre-signed
    recording URL from Mango. Without it, process_call immediately writes
    transcription_status='skipped_no_audio' and aborts.
    """
    status = mc.get("transcription_status") or ""
    duration = int(mc.get("duration_sec") or 0)
    uuid = mc.get("uuid") or ""

    if status in ("done", "in_progress"):
        return
    if duration < 15:
        logger.debug(f"[reconcile] Skipping auto-process for {uuid[:8]} — too short ({duration}s)")
        return

    if not mango_token:
        logger.warning(f"[reconcile] No mango_token available — call {uuid[:8]} will remain pending for pipeline tick")
        return

    logger.info(f"[reconcile] Processing call {uuid[:8]} ({duration}s) synchronously")

    try:
        from mango_pipeline import process_call
        from database import get_mango_call
        call_row = get_mango_call(uuid)
        if call_row:
            process_call(call_row, mango_token=mango_token)
    except Exception as e:
        logger.warning(f"[reconcile] Auto-process failed for {uuid[:8]}: {e}")
