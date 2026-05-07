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
    get_mango_calls_unmatched,
    get_gads_call_view,
    get_all_leads,
    update_mango_call_attribution,
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

# Module-level cursor: tracks last successful sync time
_last_sync_cursor: Optional[datetime] = None


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

    # On first run, backfill initial_days_back days; after that use cursor
    if _last_sync_cursor is None:
        since = datetime.now(timezone.utc) - timedelta(days=initial_days_back)
        logger.info(f"Mango sync: initial backfill, fetching {initial_days_back} days")
    else:
        # Overlap by 2 minutes to catch any calls that slipped through
        since = _last_sync_cursor - timedelta(minutes=2)

    try:
        raw_calls = fetch_calls_since(token_manager, pbx_id, since, api_base=api_base)
    except PermissionError:
        logger.error("Mango sync: auth failure, skipping this cycle")
        return 0
    except Exception as e:
        logger.error(f"Mango sync: fetch error: {e}")
        return 0

    count = 0
    for raw in raw_calls:
        normalized = normalize_call(raw)
        if not normalized["uuid"]:
            continue
        try:
            upsert_mango_call(normalized)
            count += 1
        except Exception as e:
            logger.error(f"Mango sync: upsert error for {normalized.get('uuid')}: {e}")

    _last_sync_cursor = datetime.now(timezone.utc)
    logger.info(f"Mango sync: upserted {count} calls")
    return count


# ── Attribution reconciler ────────────────────────────────────────────────────

def reconcile_attribution(days: int = 7) -> int:
    """
    Match unattributed inbound Mango calls against:
      1. Google Ads call_view rows (ad-driven calls by area code + time window)
      2. Lead phone numbers (existing leads who called in)

    Updates mango_calls.lead_id / gads_call_id / match_confidence / match_method.
    Safe to run repeatedly — only updates NULL attribution rows.
    Returns count of calls newly attributed.
    """
    unmatched = get_mango_calls_unmatched(days=days)
    gads_calls = get_gads_call_view(days=days)
    # Use a large limit to get all leads for phone matching — default 200 is too small.
    all_leads = get_all_leads(limit=10000)

    # Build phone → lead_id lookup (strip non-digits for comparison)
    phone_to_lead: dict[str, str] = {}
    for lead in all_leads:
        phone = re.sub(r"\D", "", lead.get("phone", "") or "")
        if len(phone) >= 10:
            phone_to_lead[phone[-10:]] = lead["id"]

    attributed = 0

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

        # ── Try GAds call_view match ──────────────────────────────────────────
        best_gads_id = None
        best_confidence = 0.0
        best_method = ""

        for gc in gads_calls:
            gc_area = gc.get("caller_area_code", "")
            # Skip if either area code is empty (call < 15s or unparseable number)
            if not gc_area or not mc_area or gc_area != mc_area:
                continue
            gc_dur = int(gc.get("call_duration_sec") or 0)
            gc_start_str = gc.get("start_call_date_time", "")
            try:
                gc_dt = datetime.fromisoformat(gc_start_str.replace("Z", "+00:00"))
                if gc_dt.tzinfo is None:
                    # Google Ads reports time in account timezone (Eastern).
                    # Use zoneinfo for correct EST/EDT offset — avoids 1-hour
                    # miss in Nov-Mar when EDT offset assumption (+4h) is wrong.
                    try:
                        from zoneinfo import ZoneInfo
                        gc_dt = gc_dt.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
                    except Exception:
                        # zoneinfo not available — fall back to EST (-5h, conservative)
                        gc_dt = gc_dt.replace(tzinfo=timezone.utc) + timedelta(hours=5)
            except Exception:
                continue

            time_delta = abs((mc_dt - gc_dt).total_seconds())
            dur_delta = abs(mc_dur - gc_dur)

            if time_delta <= 90 and dur_delta <= 5:
                if 0.95 > best_confidence:
                    best_gads_id = gc["call_id"]
                    best_confidence = 0.95
                    best_method = "gads_window"
            elif time_delta <= 300 and dur_delta <= 30:
                if 0.75 > best_confidence:
                    best_gads_id = gc["call_id"]
                    best_confidence = 0.75
                    best_method = "gads_window_loose"

        if best_gads_id:
            update_mango_call_attribution(
                uuid=mc_uuid,
                gads_call_id=best_gads_id,
                match_confidence=best_confidence,
                match_method=best_method,
            )
            attributed += 1
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

    logger.info(f"Mango reconcile: attributed {attributed}/{len(unmatched)} calls")
    return attributed
