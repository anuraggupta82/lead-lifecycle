"""
Firestore sync — pulls existing nxtsmile leads from Firestore into SQLite on startup.
Also callable via admin API to re-sync at any time.
"""
import hashlib
import logging
import urllib.request
import ssl
import json
from database import upsert_lead, get_lead, enqueue_follow_ups, add_event, is_deleted_lead, _conn
from database import unsubscribe as db_unsubscribe
from config import get_settings

# macOS Python doesn't use the system keychain for SSL by default.
# This context uses certifi if available, otherwise falls back to unverified
# (acceptable since we're calling our own Cloud Run service over HTTPS).
def _ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

logger = logging.getLogger(__name__)


def _normalize_firestore_lead(doc: dict) -> dict:
    """Map Firestore lead fields → our lead schema."""
    # Firestore leads have varied field names depending on which form submitted
    raw = doc.get("data", doc)  # some docs nest under 'data'

    lead_id = (
        raw.get("id") or
        raw.get("lead_id") or
        None
    )

    # Firestore leads may lack a proper ID field — generate deterministic one
    # from email (matching save_lead logic: lead_<sha256[:16]>)
    if not lead_id:
        email = raw.get("email", "").strip().lower()
        if email:
            lead_id = "lead_" + hashlib.sha256(email.encode()).hexdigest()[:16]
        else:
            phone = (raw.get("phone") or raw.get("phone_number") or "").strip()
            if phone:
                lead_id = "lead_" + hashlib.sha256(phone.encode()).hexdigest()[:16]
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
        "stage": "new",
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
        "utm_content": tracking.get("utm_content") or raw.get("utm_content") or "",
        "landing_url": tracking.get("landing_url") or raw.get("landing_url") or "",
        "smile_image_url": raw.get("smile_url") or raw.get("smile_image_url") or "",
        "smile_blob_name": raw.get("smile_blob_name") or "",
        "smile_composite_blob_name": raw.get("smile_composite_blob_name") or "",
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
        resp = urllib.request.urlopen(req, timeout=20, context=_ssl_context())
        payload = json.loads(resp.read())
    except Exception as e:
        logger.error(f"Firestore sync failed: {e}")
        return {"synced": 0, "skipped": 0, "errors": 1, "error": str(e)}

    # Payload may be a list or {"leads": [...], "source": "firestore"}
    all_docs = payload if isinstance(payload, list) else payload.get("leads", [])
    logger.info(f"Firestore returned {len(all_docs)} leads")

    # Deduplicate by email — keep only the newest doc per email address.
    # Old test submissions create many legacy docs with auto-generated IDs;
    # we only want one record per person in the local DB.
    seen_emails: dict = {}
    no_email_docs = []
    for doc in all_docs:
        email = (doc.get("email") or "").strip().lower()
        ts = doc.get("timestamp") or ""
        if not email:
            no_email_docs.append(doc)
            continue
        if email not in seen_emails or ts > seen_emails[email].get("timestamp", ""):
            seen_emails[email] = doc
    docs = list(seen_emails.values()) + no_email_docs
    logger.info(f"After dedup by email: {len(docs)} unique leads (skipped {len(all_docs) - len(docs)} duplicates)")

    synced = skipped = errors = 0

    for doc in docs:
        try:
            normalized = _normalize_firestore_lead(doc)
            if not normalized or not normalized.get("id"):
                skipped += 1
                continue

            # Tombstone check — never re-import a lead that admin deleted
            if is_deleted_lead(normalized["id"], normalized.get("email") or ""):
                logger.info(
                    f"Skipping deleted lead {normalized['id']} "
                    f"({normalized.get('email','')}) — tombstoned"
                )
                skipped += 1
                continue

            existing = get_lead(normalized["id"])
            old_stage = (existing or {}).get("stage", "")

            upsert_lead(normalized)

            if not existing:
                # New lead — enqueue follow-ups from their original created_at
                lead_row = get_lead(normalized["id"]) or normalized
                enqueue_follow_ups(lead_row, normalized.get("created_at") or "")
                add_event(normalized["id"], "lead_created", source="firestore_sync",
                          detail=json.dumps({"source": normalized["source"]}))
                synced += 1
            else:
                # Detect booked transition — only fire stop engine on actual transition
                new_stage = (get_lead(normalized["id"]) or {}).get("stage", "")
                BOOKED_STAGES = {"scheduled", "showed", "no_show",
                                 "treatment_presented", "treatment_accepted",
                                 "treatment_completed"}
                was_booked = old_stage in BOOKED_STAGES
                now_booked = new_stage in BOOKED_STAGES
                if not was_booked and now_booked:
                    try:
                        from stop_engine import handle_event
                        handle_event(normalized["id"], "booked", reason=f"stage_transition:{old_stage}->{new_stage}")
                        logger.info(
                            f"Firestore sync: fired stop_engine 'booked' for lead {normalized['id']} "
                            f"({old_stage} → {new_stage})"
                        )
                    except Exception as _se:
                        logger.warning(f"stop_engine.handle_event(booked) failed (non-fatal): {_se}")
                skipped += 1

        except Exception as e:
            logger.error(f"Error syncing lead {doc}: {e}")
            errors += 1

    # --- Dedup: remove duplicate leads that have a canonical lead_ version ----
    # Previous syncs may have created entries under fs_<hash>, random Firestore
    # doc IDs (e.g. hpSVJzZBOX1SrT3MmF8X), or other non-canonical IDs.
    # Keep the lead_ version, purge any other ID for the same email.
    deduped = 0
    try:
        from database import get_all_leads, _conn
        local_leads = get_all_leads(limit=5000)
        # Build email→lead_ canonical ID map
        canonical_by_email = {}
        for lead in local_leads:
            lid = lead.get("id", "")
            if lid.startswith("lead_"):
                em = (lead.get("email") or "").strip().lower()
                if em:
                    canonical_by_email[em] = lid
        # Purge any non-canonical lead whose email has a lead_ version
        for lead in local_leads:
            lid = lead.get("id", "")
            if lid.startswith("lead_"):
                continue  # this IS the canonical version — keep it
            em = (lead.get("email") or "").strip().lower()
            if em and em in canonical_by_email:
                with _conn() as conn:
                    conn.execute("DELETE FROM lifecycle_events WHERE lead_id = ?", (lid,))
                    conn.execute("DELETE FROM follow_up_queue WHERE lead_id = ?", (lid,))
                    conn.execute("DELETE FROM lead_notes WHERE lead_id = ?", (lid,))
                    conn.execute("DELETE FROM leads WHERE id = ?", (lid,))
                deduped += 1
                logger.info(f"Deduped non-canonical lead {lid} ({em}) — canonical is {canonical_by_email[em]}")
    except Exception as e:
        logger.warning(f"Dedup step failed (non-fatal): {e}")

    # --- Purge: remove local leads deleted from Firestore --------------------
    purged = 0
    firestore_ids = {(d.get("id") or "").strip() for d in docs if d.get("id")}
    firestore_emails = {(d.get("email") or "").strip().lower() for d in docs if d.get("email")}
    try:
        # Re-fetch after dedup to avoid stale references
        local_leads = get_all_leads(limit=5000)
        for lead in local_leads:
            lid = lead.get("id", "")
            if not (lid.startswith("fs_") or lid.startswith("lead_")):
                continue
            # Keep if the lead ID matches a current Firestore record
            if lid in firestore_ids:
                continue
            # Keep if the email matches a current Firestore record
            local_email = (lead.get("email") or "").strip().lower()
            if local_email and local_email in firestore_emails:
                continue
            # This lead is gone from Firestore — purge it locally
            with _conn() as conn:
                conn.execute("DELETE FROM lifecycle_events WHERE lead_id = ?", (lid,))
                conn.execute("DELETE FROM follow_up_queue WHERE lead_id = ?", (lid,))
                conn.execute("DELETE FROM lead_notes WHERE lead_id = ?", (lid,))
                conn.execute("DELETE FROM leads WHERE id = ?", (lid,))
            purged += 1
            logger.info(f"Purged stale local lead {lid} ({local_email}) — no longer in Firestore")
    except Exception as e:
        logger.warning(f"Purge step failed (non-fatal): {e}")

    logger.info(f"Firestore sync complete: synced={synced} skipped={skipped} errors={errors} deduped={deduped} purged={purged}")
    return {"synced": synced, "skipped": skipped, "errors": errors, "deduped": deduped, "purged": purged}


def sync_unsubscribes_from_firestore() -> dict:
    """
    Pull opt-outs from Firestore collection `unsubscribes` (written by the
    public nxtsmile-unsubscribe Cloud Run microservice) and apply them to the
    local SQLite DB.

    Idempotent: rows already present in the local `unsubscribes` table are
    skipped, so this can safely run on every scheduler tick.

    Returns {"applied": N, "skipped": N, "errors": N}.
    """
    settings = get_settings()
    project = getattr(settings, "gcp_project", "marketing-landing-page-491721")

    try:
        # Lazy import — google-cloud-firestore is heavy and only needed here
        from google.cloud import firestore as _fs
    except ImportError:
        logger.warning("google-cloud-firestore not installed — skipping unsubscribe sync")
        return {"applied": 0, "skipped": 0, "errors": 1, "error": "missing_dependency"}

    try:
        client = _fs.Client(project=project)
        docs = list(client.collection("unsubscribes").stream())
    except Exception as e:
        logger.error(f"Unsubscribe sync — Firestore read failed: {e}")
        return {"applied": 0, "skipped": 0, "errors": 1, "error": str(e)}

    # Build set of already-applied (lead_id, channel) pairs to avoid duplicate INSERTs
    already_applied: set = set()
    try:
        with _conn() as conn:
            for row in conn.execute("SELECT lead_id, channel FROM unsubscribes"):
                already_applied.add((row["lead_id"], row["channel"]))
    except Exception as e:
        logger.warning(f"Could not load existing unsubscribes (continuing): {e}")

    applied = skipped = errors = 0
    for doc in docs:
        try:
            data = doc.to_dict() or {}
            lead_id = (data.get("lead_id") or "").strip()
            channel = (data.get("channel") or "").strip().lower()
            if not lead_id or channel not in ("email", "sms"):
                skipped += 1
                continue
            if (lead_id, channel) in already_applied:
                skipped += 1
                continue
            # Confirm lead exists locally — if not, leave the Firestore doc in
            # place; the next lead sync may bring this lead in.
            if not get_lead(lead_id):
                skipped += 1
                continue
            db_unsubscribe(lead_id, channel, reason="firestore-sync")
            add_event(lead_id, "unsubscribed", source="firestore_sync",
                      detail=json.dumps({"channel": channel}))
            applied += 1
        except Exception as e:
            logger.error(f"Error applying unsubscribe doc {doc.id}: {e}")
            errors += 1

    logger.info(
        f"Unsubscribe sync complete: applied={applied} skipped={skipped} "
        f"errors={errors} total_docs={len(docs)}"
    )
    return {"applied": applied, "skipped": skipped, "errors": errors, "total_docs": len(docs)}
