"""
CallRail API v3 client — thin wrapper for tracker/call reads and writes.

All functions return parsed dicts/lists. Errors are logged and re-raised
so callers can decide how to handle them.

Base URL: https://api.callrail.com/v3/a/{account_id}/
Auth:     Token auth — Authorization: Token token=<api_key>
Docs:     https://apidocs.callrail.com/

BAA posture (Path B — no recording):
  Recording is DISABLED on all CallRail trackers. Do NOT re-enable without
  signing the HIPAA BAA first (submit ticket at support.callrail.com).
"""
import logging
import requests
from typing import Optional
from config import get_settings

logger = logging.getLogger(__name__)

_BASE = "https://api.callrail.com/v3/a"


def _headers() -> dict:
    s = get_settings()
    return {
        "Authorization": f"Token token={s.callrail_api_key}",
        "Content-Type": "application/json",
    }


def _account_id() -> str:
    return get_settings().callrail_account_id


def _get(path: str, params: Optional[dict] = None) -> dict:
    """GET helper — raises on non-2xx."""
    url = f"{_BASE}/{_account_id()}/{path}"
    resp = requests.get(url, headers=_headers(), params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _put(path: str, body: dict) -> dict:
    """PUT helper — raises on non-2xx."""
    url = f"{_BASE}/{_account_id()}/{path}"
    resp = requests.put(url, headers=_headers(), json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _post(path: str, body: dict) -> dict:
    """POST helper — raises on non-2xx."""
    url = f"{_BASE}/{_account_id()}/{path}"
    resp = requests.post(url, headers=_headers(), json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()


# ── Trackers (tracking numbers) ──────────────────────────────────────────────

def get_trackers(per_page: int = 100) -> list[dict]:
    """
    Return all trackers for the account, auto-paginating.

    Each tracker dict includes: id, name, type, status, tracking_numbers,
    destination_number, whisper_message, source, call_flow, company.
    """
    trackers = []
    page = 1
    while True:
        data = _get("trackers.json", params={"per_page": per_page, "page": page})
        batch = data.get("trackers", [])
        trackers.extend(batch)
        if len(trackers) >= data.get("total_records", 0):
            break
        page += 1
    logger.debug(f"[callrail] fetched {len(trackers)} trackers")
    return trackers


def get_tracker(tracker_id: str) -> dict:
    """Return a single tracker by its resource ID."""
    return _get(f"trackers/{tracker_id}.json")


def update_tracker(tracker_id: str, **kwargs) -> dict:
    """
    Update tracker fields. Common kwargs:
      name, whisper_message, destination_number,
      call_flow (dict with recording_enabled, steps)
    Returns the updated tracker dict.
    """
    data = _put(f"trackers/{tracker_id}.json", kwargs)
    logger.info(f"[callrail] updated tracker {tracker_id}: {list(kwargs.keys())}")
    return data


def set_recording_enabled(tracker_id: str, enabled: bool,
                          destination_number: str = "") -> dict:
    """
    Toggle recording on a tracker's call flow.
    destination_number required to preserve the dial step when rebuilding the flow.
    """
    tracker = get_tracker(tracker_id)
    dest = destination_number or tracker.get("destination_number", "")
    return update_tracker(
        tracker_id,
        call_flow={
            "type": "advanced",
            "recording_enabled": enabled,
            "steps": [{"type": "dial", "destination_number": dest, "timeout": 60}],
        },
    )


# ── Companies ─────────────────────────────────────────────────────────────────

def get_company(company_resource_id: str) -> dict:
    """Return company details by resource ID (COM...) ."""
    return _get(f"companies/{company_resource_id}.json")


def update_company(company_resource_id: str, **kwargs) -> dict:
    """
    Update company fields. Common kwargs: name, time_zone, callscribe_enabled.
    Returns the updated company dict.
    """
    data = _put(f"companies/{company_resource_id}.json", kwargs)
    logger.info(f"[callrail] updated company {company_resource_id}: {list(kwargs.keys())}")
    return data


# ── Calls ─────────────────────────────────────────────────────────────────────

def get_calls(
    date_range_start: Optional[str] = None,
    date_range_end: Optional[str] = None,
    per_page: int = 100,
    max_pages: int = 10,
    fields: str = (
        "id,answered,direction,duration,start_time,tracking_phone_number,"
        "customer_phone_number,customer_name,customer_city,customer_state,"
        "voicemail,recording,source,campaign,keywords,gclid,landing_page_url,"
        "first_call,lead_score,utm_source,utm_medium,utm_campaign,utm_term"
    ),
) -> list[dict]:
    """
    Return calls, auto-paginating up to max_pages.

    date_range_start/end: ISO 8601 strings, e.g. "2026-05-01T00:00:00-04:00"
    max_pages: safety cap — set higher for large historical pulls.
    """
    calls = []
    page = 1
    params: dict = {"per_page": per_page, "fields": fields, "page": page}
    if date_range_start:
        params["date_range_start"] = date_range_start
    if date_range_end:
        params["date_range_end"] = date_range_end

    while page <= max_pages:
        params["page"] = page
        data = _get("calls.json", params=params)
        batch = data.get("calls", [])
        calls.extend(batch)
        total = data.get("total_records", 0)
        if len(calls) >= total or not batch:
            break
        page += 1

    logger.debug(f"[callrail] fetched {len(calls)} calls")
    return calls
