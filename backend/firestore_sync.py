"""
Firestore sync — pulls existing nxtsmile leads from Firestore into SQLite on startup.
Also callable via admin API to re-sync at any time.
"""
import hashlib
import logging
import urllib.request
import json
from database import upsert_lead, get_lead, enqueue_follow_ups, add_event
from config import get_settings

logger = logging.getLogger(__name__)


def _normalize_firestore_lead(doc: dict) -> dict:
    """Map Firestore lead fields → our lead schema."""
    # Firestore leads have varied field names depending on which form submitted
    raw = doc.get("data", doc)  # some docs nest under 'data'

    lead_id = (
        raw.get("id") or
        raw.get("lead_id") or
        raw.get("firestore_id") or
        doc.get("firestore_id") or
        None
    )

    # Firestore leads often lack an ID field — generate one from email
    if not lead_id:
        email = raw.get("email", "").strip().lower()
        if email:
            lead_id = "fs_" + hashlib.sha256(email.encode()).hexdigest()[:12]
        else:
            return None

    # Parse name — may be combined "full_name" or split
    first = raw.get("first_name") or raw.get("firstName") or ""
    last = raw.get("last_name") or raw.get("lastName") or ""
    if not first and raw.get("name"):
        parts = raw["name"].strip().split(" ", 1)
        first = parts[0]
        last = parts[1] if len(parts) > 1 else ""

    # Determine source
    source = raw.get("source") or raw.get("form_type") or "nxtsmile_landing_page"
    if "smile" in source.lower():
        source = "smile_tool"
    elif "pearly" in source.lower() or "chat" in source.lower():
        source = "pearly"
    elif "contact" in source.lower() or "form" in source.lower():
        source = "contact_form"

    # Tracking
    tracking = raw.get("tracking") or raw.get("tracking_data") or {}
    if isinstance(tracking, str):
        try:
            tracking = json.loads(tracking)
        except Exception:
            tracking = {}

    return {
        "id": lead_id,
        "created_at": raw.get("timestamp") or raw.get("created_at") or "",
        "source": source,
        "stage": "nurturing" if raw.get("smile_url") else "engaged",
        "first_name": first,
        "last_name": last,
        "email": raw.get("email") or "",
        "phone": raw.get("phone") or raw.get("phone_number") or "",
        "goals": raw.get("goals") or [],
        "gclid": tracking.get("gclid") or raw.get("gclid") or "",
        "fbclid": tracking.get("fbclid") or raw.get("fbclid") or "",
        "msclkid": tracking.get("msclkid") or raw.get("msclkid") or "",
        "utm_source": tracking.get("utm_source") or raw.get("utm_source") or "",
        "utm_medium": tracking.get("utm_medium") or raw.get("utm_medium") or "",
        "utm_campaign": tracking.get("utm_campaign") or raw.get("utm_campaign") or "",
        "utm_term": tracking.get("utm_term") or raw.get("utm_term") or "",
        "smile_image_url": raw.get("smile_url") or raw.get("smile_image_url") or "",
        "notes": raw.get("message") or raw.get("notes") or "",
    }


def sync_from_firestore() -> dict:
    """
    Pull leads from the nxtsmile API (which reads Firestore).
    Returns {"synced": N, "skipped": N, "errors": N}
    """
    settings = get_settings()
    url = f"{settings.nxtsmile_api}/api/leads?secret={settings.firestore_secret}"

    logger.info(f"Syncing leads from Firestore via {url}")

    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        resp = urllib.request.urlopen(req, timeout=20)
        payload = json.loads(resp.read())
    except Exception as e:
        logger.error(f"Firestore sync failed: {e}")
        return {"synced": 0, "skipped": 0, "errors": 1, "error": str(e)}

    # Payload may be a list or {"leads": [...], "source": "firestore"}
    docs = payload if isinstance(payload, list) else payload.get("leads", [])
    logger.info(f"Firestore returned {len(docs)} leads")

    synced = skipped = errors = 0

    for doc in docs:
        try:
            normalized = _normalize_firestore_lead(doc)
            if not normalized or not normalized.get("id"):
                skipped += 1
                continue

            existing = get_lead(normalized["id"])
            upsert_lead(normalized)

            if not existing:
                # New lead — enqueue follow-ups from their original created_at
                enqueue_follow_ups(normalized["id"], normalized.get("created_at") or "")
                add_event(normalized["id"], "lead_created", source="firestore_sync",
                          detail=json.dumps({"source": normalized["source"]}))
                synced += 1
            else:
                skipped += 1

        except Exception as e:
            logger.error(f"Error syncing lead {doc}: {e}")
            errors += 1

    logger.info(f"Firestore sync complete: synced={synced} skipped={skipped} errors={errors}")
    return {"synced": synced, "skipped": skipped, "errors": errors}
