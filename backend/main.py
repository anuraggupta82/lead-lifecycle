"""
Lead Lifecycle Service — FastAPI
Runs on Mac Mini (http://localhost:7070)

Endpoints:
  POST /api/events                    — receive lifecycle events from any source
  GET  /api/pipeline                  — all leads with stage summary (dashboard)
  GET  /api/lead/{id}                 — full lead + event timeline
  POST /api/unsubscribe/{id}/{channel} — opt-out handler
  GET  /unsubscribe/{id}/{channel}    — one-click unsubscribe (from email links)
  GET  /delete-image/{id}              — one-click smile image deletion (from email links)
  GET  /api/admin/stats               — pipeline funnel stats
  GET  /api/admin/queue               — pending follow-up queue
  POST /api/admin/sync                — trigger Firestore sync
  POST /api/admin/match               — trigger OD patient matching
  POST /api/admin/run-queue           — manually trigger follow-up engine
  PUT  /api/admin/lead/{id}/stage     — manually advance lead stage
  GET  /health                        — health check
  GET  /                              — pipeline dashboard (React SPA)
"""
import logging
import os
import json
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, Request, Header, Body, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, validator

from config import get_settings, Settings
from database import (
    init_db, upsert_lead, get_lead, get_lead_by_email, update_stage,
    get_all_leads, get_events, get_pipeline_stats, enqueue_follow_ups,
    add_event, unsubscribe, get_follow_up_queue, get_due_follow_ups,
    add_note, get_notes, delete_note, force_stage,
    get_campaign_stats, get_google_ads_campaigns, get_distinct_sources, get_keyword_stats,
    get_search_term_stats, get_geo_stats, get_schedule_stats,
    add_deleted_lead_tombstone, backfill_communication_log,
    get_or_create_conversation, get_conversation, get_messages, get_all_conversations,
    get_daily_stats, get_ad_group_stats,
    save_outbound_message, get_lead_messages,
    # Step 9: workflows
    get_all_workflows, get_workflow, get_workflow_steps, get_workflow_step,
    upsert_workflow, upsert_workflow_step, delete_workflow_step, delete_workflow,
    # OD settings
    get_setting, save_setting, get_od_settings,
    # Step 10: stop conditions helpers
    add_lead_event,
    # Inbox / call log / next action helpers
    get_unread_sms_count, get_unread_sms_leads, mark_sms_read,
    get_unread_email_count, get_unread_email_leads, mark_email_read,
    log_call, get_calls, set_next_action, clear_next_action,
    # Lead tags
    get_lead_tags, set_lead_tags,
)
from email_service import send_office_new_lead
from follow_up_engine import start_scheduler, stop_scheduler, run_now
from ga4_events import (
    track_lead_created, track_smile_completed, track_appointment_booked,
)
from firestore_sync import sync_from_firestore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import logging.handlers as _lh
_LOG_FILE = os.path.join(os.path.dirname(__file__), "logs", "app.log")
os.makedirs(os.path.dirname(_LOG_FILE), exist_ok=True)
_file_handler = _lh.RotatingFileHandler(
    _LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s — %(message)s"))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(), _file_handler],
)
logger = logging.getLogger(__name__)
logger.info(f"Log file: {_LOG_FILE}")

# Module-level scheduler reference so endpoints can inspect job state
ads_scheduler = None
# Tracks last successful run time per job id
_job_last_run: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global ads_scheduler
    # Startup
    init_db()
    logger.info("Database initialized")

    # One-time-safe backfill: ensure communication_log has a row for every
    # follow_up_queue entry already marked 'sent', so a restart will never
    # replay an already-delivered template. Idempotent on subsequent boots.
    try:
        n = backfill_communication_log()
        logger.info(f"communication_log backfill: inserted {n} row(s)")
    except Exception as e:
        logger.warning(f"communication_log backfill failed (non-fatal): {e}")

    # Auto-sync Firestore leads on startup (non-blocking — ignore errors)
    try:
        result = sync_from_firestore()
        logger.info(f"Startup Firestore sync: {result}")
    except Exception as e:
        logger.warning(f"Startup Firestore sync failed (non-fatal): {e}")

    # Start follow-up scheduler (every 15 min)
    start_scheduler()

    # Start Google Ads scheduled jobs
    ads_scheduler = BackgroundScheduler(timezone="America/New_York")  # also stored at module level above

    from datetime import datetime as _dt

    def _stamp(job_id):
        _job_last_run[job_id] = _dt.now().isoformat()

    # 5:30 AM — Pull GA4 analytics data
    def _ga4_pull_job():
        _stamp("ga4_pull")
        try:
            from ga4_reporting import fetch_all_ga4_data
            from database import save_ga4_cache
            data = fetch_all_ga4_data(days=30)
            if not data.get("overview", {}).get("error"):
                save_ga4_cache("full_report", 30, data)
                logger.info(f"GA4 data cached: {data['overview'].get('sessions', 0):.0f} sessions")
            else:
                logger.warning(f"GA4 pull returned error: {data['overview'].get('error')}")
        except Exception as e:
            logger.error(f"Scheduled GA4 pull failed: {e}")

    # 6 AM — Resolve gclids to keywords
    def _gads_sync_job():
        _stamp("gads_sync")
        try:
            from google_ads_sync import sync_gclids_to_keywords
            result = sync_gclids_to_keywords(days_back=7)
            logger.info(f"Scheduled Google Ads sync: {result}")
        except Exception as e:
            logger.error(f"Scheduled Google Ads sync failed: {e}")

    # 7 AM — AI optimizer (after fresh data)
    def _optimizer_job():
        _stamp("ai_optimizer")
        try:
            from ai_optimizer import optimize_campaign
            result = optimize_campaign(trigger="scheduler_7am")
            logger.info(f"Scheduled optimizer: run_id={result.get('run_id','?')} "
                        f"pending={result.get('summary', {}).get('keywords_to_pause', 0)} pauses")
        except Exception as e:
            logger.error(f"Scheduled optimizer failed: {e}")

    # 10 PM — OpenDental matcher + treatment stages
    def _od_sync_job():
        _stamp("od_sync")
        try:
            from od_matcher import run_full_od_sync
            result = run_full_od_sync()
            logger.info(f"Scheduled OD sync: {result}")
        except Exception as e:
            logger.error(f"Scheduled OD sync failed: {e}")

    # Every 5 min — Poll IMAP inbox for inbound emails
    def _imap_poll_job():
        _stamp("imap_poll")
        try:
            from imap_service import poll_once
            result = poll_once()
            if result.get("fetched", 0) > 0 or result.get("errors", 0) > 0:
                logger.info(f"IMAP poll: {result}")
        except Exception as e:
            logger.error(f"IMAP poll failed: {e}")

    # 11 PM — Upload offline conversions
    def _conversion_upload_job():
        _stamp("conversion_upload")
        try:
            from google_ads_conversions import upload_offline_conversions
            result = upload_offline_conversions()
            logger.info(f"Scheduled conversion upload: {result}")
        except Exception as e:
            logger.error(f"Scheduled conversion upload failed: {e}")

    ads_scheduler.add_job(_imap_poll_job, CronTrigger(minute="0,5,10,15,20,25,30,35,40,45,50,55"),
                          id="imap_poll", name="IMAP Inbox Poll",
                          max_instances=1, coalesce=True, replace_existing=True)
    ads_scheduler.add_job(_ga4_pull_job, CronTrigger(hour=5, minute=30),
                          id="ga4_pull", name="GA4 Analytics Data Pull", replace_existing=True)
    ads_scheduler.add_job(_gads_sync_job, CronTrigger(hour=6, minute=0),
                          id="gads_sync", name="Google Ads GCLID Sync", replace_existing=True)
    ads_scheduler.add_job(_optimizer_job, CronTrigger(hour=7, minute=0),
                          id="ai_optimizer", name="AI Campaign Optimizer", replace_existing=True)
    ads_scheduler.add_job(_od_sync_job, CronTrigger(hour=22, minute=0),
                          id="od_sync", name="OpenDental Patient Match + Treatment Stages", replace_existing=True)
    ads_scheduler.add_job(_conversion_upload_job, CronTrigger(hour=23, minute=0),
                          id="conversion_upload", name="Google Ads Conversion Upload", replace_existing=True)

    ads_scheduler.start()
    logger.info("Scheduled jobs started (5:30AM GA4, 6AM gads sync, 7AM optimizer, 10PM OD, 11PM conversions)")

    yield

    # Shutdown
    stop_scheduler()
    ads_scheduler.shutdown(wait=False)


app = FastAPI(
    title="Lead Lifecycle Service",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve frontend
_frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(_frontend_dir):
    app.mount("/static", StaticFiles(directory=_frontend_dir), name="static")


# ─── Auth ─────────────────────────────────────────────────────────────────────

def _require_admin(x_admin_password: Optional[str] = Header(None)):
    settings = get_settings()
    if x_admin_password != settings.admin_password:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ─── Health ───────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "lead-lifecycle", "time": datetime.now(timezone.utc).isoformat()}


# ─── Public: Dashboard SPA ────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def dashboard():
    frontend_path = os.path.join(os.path.dirname(__file__), "..", "frontend", "index.html")
    if os.path.exists(frontend_path):
        with open(frontend_path) as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>Pipeline Dashboard</h1><p>Frontend not found. Run from project root.</p>")


# ─── Events endpoint (called by landing page, scheduler, Mango) ───────────────

class EventPayload(BaseModel):
    event_type: str                    # 'lead_created','smile_completed','booking_confirmed',
                                       #   'call_matched','stage_update'
    lead_id: Optional[str] = None
    email: Optional[str] = None        # fallback lookup if no lead_id
    source: str = "external"
    detail: Optional[dict] = None

    # Lead fields (populated on lead_created)
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = None
    goals: Optional[list] = None
    gclid: Optional[str] = None
    fbclid: Optional[str] = None
    msclkid: Optional[str] = None
    utm_source: Optional[str] = None
    utm_medium: Optional[str] = None
    utm_campaign: Optional[str] = None
    utm_term: Optional[str] = None
    utm_content: Optional[str] = None
    landing_url: Optional[str] = None
    smile_image_url: Optional[str] = None
    booking_id: Optional[str] = None
    created_at: Optional[str] = None


@app.post("/api/events", status_code=201)
async def receive_event(payload: EventPayload):
    """
    Central event receiver. Called by:
    - nxtsmile.com backend (lead_created, smile_completed)
    - Appointment scheduler (booking_confirmed, booking_cancelled)
    - Mango call analysis (call_matched)
    """
    event_type = payload.event_type
    detail_str = json.dumps(payload.detail or {})

    # Resolve lead
    lead = None
    if payload.lead_id:
        lead = get_lead(payload.lead_id)
    if not lead and payload.email:
        lead = get_lead_by_email(payload.email)

    now = datetime.now(timezone.utc).isoformat()

    if event_type == "lead_created":
        if not payload.lead_id:
            raise HTTPException(status_code=400, detail="lead_id required for lead_created")

        lead_data = {
            "id": payload.lead_id,
            "created_at": payload.created_at or now,
            "source": payload.source,
            "stage": "new",
            "first_name": payload.first_name or "",
            "last_name": payload.last_name or "",
            "email": payload.email or "",
            "phone": payload.phone or "",
            "goals": payload.goals or [],
            "gclid": payload.gclid or "",
            "fbclid": payload.fbclid or "",
            "msclkid": payload.msclkid or "",
            "utm_source": payload.utm_source or "",
            "utm_medium": payload.utm_medium or "",
            "utm_campaign": payload.utm_campaign or "",
            "utm_term": payload.utm_term or "",
            "utm_content": payload.utm_content or "",
            "landing_url": payload.landing_url or "",
        }
        lead = upsert_lead(lead_data)
        enqueue_follow_ups(lead, lead_data["created_at"])
        add_event(payload.lead_id, "lead_created", stage_to="new", source=payload.source,
                  detail=detail_str)

        # Notify office
        try:
            send_office_new_lead(lead)
        except Exception as e:
            logger.warning(f"Office notification failed: {e}")

        # Fire GA4 event
        try:
            track_lead_created(payload.lead_id, source=payload.source, gclid=payload.gclid or "")
        except Exception as e:
            logger.debug(f"GA4 lead_created event failed (non-fatal): {e}")

        return {"status": "ok", "lead_id": payload.lead_id, "action": "created"}

    elif event_type == "smile_completed":
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        upsert_lead({"id": lead["id"], "smile_image_url": payload.smile_image_url or "",
                     "smile_generated_at": now})
        # Smile completion is just an event — no stage change (stays as 'new' until first email)
        add_event(lead["id"], "smile_completed", source=payload.source, detail=detail_str)

        # Fire GA4 event
        try:
            track_smile_completed(lead["id"])
        except Exception as e:
            logger.debug(f"GA4 smile_completed event failed (non-fatal): {e}")

        return {"status": "ok", "lead_id": lead["id"], "action": "smile_noted"}

    elif event_type == "booking_confirmed":
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        if payload.booking_id:
            upsert_lead({"id": lead["id"], "booking_id": payload.booking_id})
        update_stage(lead["id"], "scheduled", source="scheduler",
                     detail=f"booking_id={payload.booking_id}")
        add_event(lead["id"], "booking_confirmed", stage_to="scheduled", source="scheduler",
                  detail=detail_str)

        # Fire GA4 event
        try:
            track_appointment_booked(lead["id"])
        except Exception as e:
            logger.debug(f"GA4 appointment_booked event failed (non-fatal): {e}")

        return {"status": "ok", "lead_id": lead["id"], "action": "booking_noted"}

    elif event_type == "booking_cancelled":
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        update_stage(lead["id"], "auto_nurture", source="scheduler")
        add_event(lead["id"], "booking_cancelled", source="scheduler", detail=detail_str)
        return {"status": "ok", "lead_id": lead["id"], "action": "cancellation_noted"}

    elif event_type == "call_matched":
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        add_event(lead["id"], "call_matched", source="mango", detail=detail_str)
        # Advance to showed if they were scheduled
        if lead["stage"] in ("scheduled",):
            update_stage(lead["id"], "showed", source="mango")
        return {"status": "ok", "lead_id": lead["id"], "action": "call_noted"}

    elif event_type == "stage_update":
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        new_stage = (payload.detail or {}).get("stage")
        if not new_stage:
            raise HTTPException(status_code=400, detail="detail.stage required for stage_update")
        update_stage(lead["id"], new_stage, source=payload.source, detail=detail_str)
        return {"status": "ok", "lead_id": lead["id"], "action": "stage_updated"}

    else:
        raise HTTPException(status_code=400, detail=f"Unknown event_type: {event_type}")


# ─── Pipeline API ─────────────────────────────────────────────────────────────

@app.get("/api/pipeline")
def get_pipeline(stage: Optional[str] = None, limit: int = 200):
    """Return all leads with their current stage — feeds the dashboard."""
    leads = get_all_leads(stage=stage, limit=limit)

    # Enrich each lead with last event
    result = []
    for lead in leads:
        events = get_events(lead["id"])
        last_event = events[-1] if events else None
        queue = get_follow_up_queue(lead["id"])
        next_action = next(
            (q for q in queue if q["status"] == "pending"),
            None
        )
        result.append({
            **lead,
            "event_count": len(events),
            "last_event": last_event,
            "next_follow_up": next_action,
        })

    return {"leads": result, "total": len(result)}


@app.get("/api/lead/{lead_id}")
def get_lead_detail(lead_id: str):
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    events = get_events(lead_id)
    queue = get_follow_up_queue(lead_id)
    return {"lead": lead, "events": events, "follow_up_queue": queue}


# ─── Unsubscribe ──────────────────────────────────────────────────────────────

@app.get("/unsubscribe/{lead_id}/{channel}", response_class=HTMLResponse)
def one_click_unsubscribe(lead_id: str, channel: str):
    """One-click unsubscribe link from email footer."""
    if channel not in ("email", "sms"):
        return HTMLResponse("<h2>Invalid unsubscribe link.</h2>", status_code=400)
    lead = get_lead(lead_id)
    if not lead:
        return HTMLResponse("<h2>Already removed or link expired.</h2>")
    unsubscribe(lead_id, channel, reason="one-click")
    label = "email" if channel == "email" else "text messages"
    return HTMLResponse(f"""
    <!DOCTYPE html><html><head><meta charset="utf-8">
    <style>body{{font-family:sans-serif;text-align:center;padding:60px;color:#333}}
    .card{{max-width:400px;margin:0 auto;background:#f9f9f9;border-radius:12px;padding:32px}}
    h2{{color:#0d7a7f}}</style></head>
    <body><div class="card">
    <h2>You've been unsubscribed</h2>
    <p>You'll no longer receive {label} from Grafton Dental Care.</p>
    <p>If you change your mind, call us at <strong>508-318-4477</strong>.</p>
    </div></body></html>
    """)


# ─── Delete Smile Image (public — linked from emails) ────────────────────────

@app.get("/delete-image/{lead_id}", response_class=HTMLResponse)
def delete_smile_image(lead_id: str):
    """
    One-click image deletion link from follow-up emails.
    Deletes the smile preview from GCS and clears the URL in the database.
    """
    lead = get_lead(lead_id)
    if not lead:
        return HTMLResponse("""
        <!DOCTYPE html><html><head><meta charset="utf-8">
        <style>body{font-family:sans-serif;text-align:center;padding:60px;color:#333}
        .card{max-width:450px;margin:0 auto;background:#f9f9f9;border-radius:12px;padding:32px}
        h2{color:#0d7a7f}</style></head>
        <body><div class="card">
        <h2>Image Already Removed</h2>
        <p>This image has already been deleted or the link has expired.</p>
        <p>If you have questions, call us at <strong>508-318-4477</strong>.</p>
        </div></body></html>
        """)

    blob_name = lead.get("smile_blob_name", "")
    image_url = lead.get("smile_image_url", "")
    deleted_from_gcs = False

    # Delete from GCS using blob name (preferred) or parse from URL (legacy)
    gcs_blob_name = blob_name
    if not gcs_blob_name and image_url and "storage.googleapis.com" in image_url:
        try:
            path = image_url.split("storage.googleapis.com/")[1].split("?")[0]
            _, gcs_blob_name = path.split("/", 1)
        except Exception:
            pass

    if gcs_blob_name:
        try:
            from google.cloud import storage as gcs_storage
            from config import get_settings as _gs
            client = gcs_storage.Client()
            client.bucket(_gs().gcs_bucket).blob(gcs_blob_name).delete()
            deleted_from_gcs = True
            logger.info(f"Deleted GCS smile image for lead {lead_id}: {gcs_blob_name}")
        except Exception as e:
            logger.warning(f"Could not delete GCS image for lead {lead_id}: {e}")

    # Clear both URL and blob name from the database
    from database import _conn
    with _conn() as conn:
        conn.execute(
            "UPDATE leads SET smile_image_url = '', smile_blob_name = '', updated_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), lead_id)
        )

    # Log the event
    add_event(lead_id, "image_deleted", source="patient_request",
              detail=json.dumps({"gcs_deleted": deleted_from_gcs}))

    name = lead.get("first_name") or "there"
    return HTMLResponse(f"""
    <!DOCTYPE html><html><head><meta charset="utf-8">
    <style>body{{font-family:sans-serif;text-align:center;padding:60px;color:#333}}
    .card{{max-width:450px;margin:0 auto;background:#f9f9f9;border-radius:12px;padding:32px}}
    h2{{color:#0d7a7f}} .check{{font-size:48px;margin-bottom:16px}}</style></head>
    <body><div class="card">
    <div class="check">✅</div>
    <h2>Image Deleted</h2>
    <p>Hi {name}, your smile preview image has been permanently deleted from our servers.</p>
    <p>If you'd like to start fresh or have any questions, call us at <strong>508-318-4477</strong>
    or visit <a href="https://nxtsmile.com" style="color:#0d7a7f;">nxtsmile.com</a>.</p>
    </div></body></html>
    """)


# ─── Admin endpoints ──────────────────────────────────────────────────────────

@app.get("/api/admin/stats", dependencies=[Depends(_require_admin)])
def admin_stats():
    return get_pipeline_stats()


@app.get("/api/admin/queue", dependencies=[Depends(_require_admin)])
def admin_queue():
    return {"items": get_due_follow_ups(), "total": len(get_due_follow_ups())}


@app.get("/api/admin/hot-leads", dependencies=[Depends(_require_admin)])
def admin_hot_leads():
    from database import get_hot_leads
    return {"leads": get_hot_leads()}


@app.post("/api/admin/sync", dependencies=[Depends(_require_admin)])
def admin_sync():
    result = sync_from_firestore()
    return {"status": "ok", "result": result}


@app.post("/api/admin/match", dependencies=[Depends(_require_admin)])
async def admin_od_match():
    from od_matcher import run_full_od_sync
    result = run_full_od_sync()
    return {"status": "ok", "result": result}


@app.post("/api/admin/gads-sync", dependencies=[Depends(_require_admin)])
def admin_gads_sync():
    try:
        from google_ads_sync import sync_gclids_to_keywords
        result = sync_gclids_to_keywords()
        return {"status": "ok", "result": result}
    except ImportError as e:
        raise HTTPException(status_code=503, detail=f"Google Ads library not installed: {e}")
    except Exception as e:
        logger.error(f"Google Ads sync failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/upload-conversions", dependencies=[Depends(_require_admin)])
def admin_upload_conversions():
    try:
        from google_ads_conversions import upload_offline_conversions
        result = upload_offline_conversions()
        return {"status": "ok", "result": result}
    except ImportError as e:
        raise HTTPException(status_code=503, detail=f"Google Ads library not installed: {e}")
    except Exception as e:
        logger.error(f"Conversion upload failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/optimize", dependencies=[Depends(_require_admin)])
def admin_optimize(dry_run: bool = True):
    try:
        from ai_optimizer import optimize_campaign
        result = optimize_campaign(dry_run=dry_run, trigger="admin_manual")
        return {"status": "ok", "result": result}
    except ImportError as e:
        raise HTTPException(status_code=503, detail=f"AI optimizer dependencies not installed: {e}")
    except Exception as e:
        logger.error(f"AI optimizer failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─── Phase 1: Google Ads Campaign Management ───────────────────────────────��─

@app.get("/api/admin/gads/audit-log", dependencies=[Depends(_require_admin)])
def gads_audit_log(limit: int = 100, entity_id: str = "", operation: str = ""):
    """Return recent Google Ads audit log entries."""
    from database import get_audit_log
    entries = get_audit_log(limit=limit, entity_id=entity_id, operation=operation)
    return {"entries": entries, "total": len(entries)}


@app.get("/api/admin/gads/pending-approvals", dependencies=[Depends(_require_admin)])
def gads_pending_approvals():
    """Return all audit rows awaiting admin approval (Apply button)."""
    from database import get_pending_approvals
    rows = get_pending_approvals()
    return {"pending": rows, "total": len(rows)}


@app.post("/api/admin/gads/approve/{action_id}", dependencies=[Depends(_require_admin)])
async def gads_approve_action(action_id: str, request: Request):
    """
    Execute an approved recommendation against Google Ads.
    Idempotent: already-approved rows return 409.
    """
    from database import get_audit_row, update_gads_action_result, set_audit_approval
    from campaign_safety import check_writes_enabled, WriteBlockedError
    from ai_optimizer import (_build_client, _execute_single_pause,
                               _execute_bid_change, _execute_add_keyword,
                               _execute_add_negative)

    row = get_audit_row(action_id)
    if not row:
        raise HTTPException(status_code=404, detail="Action not found")
    if row["execution_result"] != "pending_approval":
        raise HTTPException(
            status_code=409,
            detail=f"Action already in state '{row['execution_result']}' — cannot re-apply"
        )

    # Kill switch check
    try:
        check_writes_enabled()
    except WriteBlockedError as e:
        update_gads_action_result(action_id, executed=False,
            execution_result="blocked", error_detail=str(e))
        raise HTTPException(status_code=403, detail=str(e))

    settings = get_settings()
    customer_id = settings.google_ads_customer_id

    operation = row["operation"]
    try:
        if operation == "pause_keyword":
            client = _build_client()
            _execute_single_pause(client, customer_id, resource_name=row["entity_id"])
            update_gads_action_result(action_id, executed=True, execution_result="success")
            set_audit_approval(action_id, approver="admin")
            logger.info(f"Approved + executed pause_keyword: {row['entity_name']} ({action_id[:8]})")

        elif operation in ("increase_bid", "decrease_bid"):
            after = json.loads(row["after_state_json"] or "{}")
            raw_bid = after.get("new_bid_micros")
            if not raw_bid:
                raise HTTPException(
                    status_code=422,
                    detail="after_state_json missing new_bid_micros — cannot execute bid change"
                )
            try:
                new_bid_micros = int(raw_bid)
            except (TypeError, ValueError):
                raise HTTPException(status_code=422, detail="new_bid_micros must be an integer")
            client = _build_client()
            try:
                _execute_bid_change(
                    client, customer_id,
                    resource_name=row["entity_id"],
                    new_bid_micros=new_bid_micros
                )
            except ValueError as e:
                # Bid guardrail violation — user error, not server error
                update_gads_action_result(action_id, executed=False,
                    execution_result=f"rejected: {str(e)[:200]}")
                raise HTTPException(status_code=422, detail=str(e))
            except Exception as e:
                update_gads_action_result(action_id, executed=True,
                    execution_result=f"failed: {str(e)[:200]}")
                raise
            update_gads_action_result(action_id, executed=True, execution_result="success")
            set_audit_approval(action_id, approver="admin")
            logger.info(f"Approved + executed {operation}: {row['entity_name']} "
                        f"new_bid={new_bid_micros} ({action_id[:8]})")

        elif operation == "add_exact_keyword":
            after = json.loads(row["after_state_json"] or "{}")
            keyword_text = after.get("keyword_text")
            match_type = after.get("match_type", "EXACT")
            ad_group_resource = after.get("ad_group_resource") or row["entity_id"]
            if not keyword_text:
                raise HTTPException(
                    status_code=422,
                    detail="after_state_json missing keyword_text"
                )
            client = _build_client()
            try:
                _execute_add_keyword(
                    client, customer_id,
                    ad_group_resource=ad_group_resource,
                    keyword_text=keyword_text,
                    match_type=match_type
                )
            except ValueError as e:
                update_gads_action_result(action_id, executed=False,
                    execution_result=f"rejected: {str(e)[:200]}")
                raise HTTPException(status_code=422, detail=str(e))
            except Exception as e:
                update_gads_action_result(action_id, executed=True,
                    execution_result=f"failed: {str(e)[:200]}")
                raise
            update_gads_action_result(action_id, executed=True, execution_result="success")
            set_audit_approval(action_id, approver="admin")
            logger.info(f"Approved + executed add_exact_keyword: '{keyword_text}' "
                        f"({action_id[:8]})")

        elif operation == "add_negative_keyword":
            after = json.loads(row["after_state_json"] or "{}")
            keyword_text = after.get("keyword_text")
            match_type = after.get("match_type", "BROAD")
            campaign_resource = after.get("campaign_resource") or row["entity_id"]
            if not keyword_text:
                raise HTTPException(
                    status_code=422,
                    detail="after_state_json missing keyword_text"
                )
            client = _build_client()
            try:
                _execute_add_negative(
                    client, customer_id,
                    campaign_resource=campaign_resource,
                    keyword_text=keyword_text,
                    match_type=match_type
                )
            except ValueError as e:
                update_gads_action_result(action_id, executed=False,
                    execution_result=f"rejected: {str(e)[:200]}")
                raise HTTPException(status_code=422, detail=str(e))
            except Exception as e:
                update_gads_action_result(action_id, executed=True,
                    execution_result=f"failed: {str(e)[:200]}")
                raise
            update_gads_action_result(action_id, executed=True, execution_result="success")
            set_audit_approval(action_id, approver="admin")
            logger.info(f"Approved + executed add_negative_keyword: '{keyword_text}' "
                        f"({action_id[:8]})")

        else:
            raise HTTPException(status_code=400, detail=f"Unknown operation: {operation}")

    except HTTPException:
        raise
    except Exception as e:
        update_gads_action_result(action_id, executed=False,
            execution_result="error", error_detail=str(e))
        logger.error(f"Approve action failed for {action_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    return {"status": "ok", "action_id": action_id, "operation": operation}


@app.post("/api/admin/gads/reject/{action_id}", dependencies=[Depends(_require_admin)])
async def gads_reject_action(action_id: str, request: Request):
    """Dismiss a recommendation without executing it."""
    from database import get_audit_row, update_gads_action_result, set_audit_approval
    row = get_audit_row(action_id)
    if not row:
        raise HTTPException(status_code=404, detail="Action not found")
    if row["execution_result"] != "pending_approval":
        raise HTTPException(
            status_code=409,
            detail=f"Action already in state '{row['execution_result']}'"
        )
    update_gads_action_result(action_id, executed=False, execution_result="rejected")
    set_audit_approval(action_id, approver="admin")
    return {"status": "ok", "action_id": action_id}


@app.get("/api/admin/gads/writes-status", dependencies=[Depends(_require_admin)])
def gads_writes_status():
    """Return the current state of the Google Ads write kill switch."""
    from campaign_safety import get_writes_status
    return get_writes_status()


@app.post("/api/admin/gads/writes-enabled", dependencies=[Depends(_require_admin)])
async def gads_set_writes_enabled(request: Request):
    """
    Toggle the runtime Google Ads write kill switch.
    Body: {"enabled": true|false}
    Note: env-var CAMPAIGN_WRITE_OPS_ENABLED must also be True for writes to work.
    """
    from database import save_setting
    from campaign_safety import get_writes_status
    body = await request.json()
    enabled = bool(body.get("enabled", False))
    save_setting("gads_writes_enabled", "true" if enabled else "false")
    logger.info(f"Google Ads writes {'ENABLED' if enabled else 'DISABLED'} via admin UI")
    return {"status": "ok", "writes_enabled": enabled, **get_writes_status()}


@app.get("/api/admin/gads/spend-guardrails", dependencies=[Depends(_require_admin)])
def gads_get_guardrails():
    """Return all spend guardrails."""
    from database import get_all_spend_guardrails
    rows = get_all_spend_guardrails()
    return {"guardrails": rows}


class SpendGuardrailBody(BaseModel):
    campaign_id: str
    campaign_name: str
    daily_cap_usd: float


@app.post("/api/admin/gads/spend-guardrails", dependencies=[Depends(_require_admin)])
def gads_upsert_guardrail(body: SpendGuardrailBody):
    """Create or update a spend guardrail for a campaign."""
    from database import upsert_spend_guardrail
    if body.daily_cap_usd <= 0:
        raise HTTPException(status_code=400, detail="daily_cap_usd must be > 0")
    row = upsert_spend_guardrail(body.campaign_id, body.campaign_name, body.daily_cap_usd)
    return {"status": "ok", "guardrail": row}


@app.get("/api/admin/gads/optimizer-runs", dependencies=[Depends(_require_admin)])
def gads_optimizer_runs(limit: int = 20):
    """Return recent optimizer run records."""
    from database import get_optimizer_runs
    runs = get_optimizer_runs(limit=limit)
    return {"runs": runs, "total": len(runs)}


# ─── Step 10: TCPA Stop Conditions ──────────────────────────────────────────

# STOP keyword normalization
_SMS_STOP_WORDS  = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit"}
_SMS_START_WORDS = {"start", "yes", "unstop"}
_SMS_HELP_WORDS  = {"help", "info"}

TWIML_STOP_REPLY  = "<?xml version='1.0' encoding='UTF-8'?><Response><Message>You have been unsubscribed. Reply START to resubscribe.</Message></Response>"
TWIML_START_REPLY = "<?xml version='1.0' encoding='UTF-8'?><Response><Message>You have been resubscribed. Reply STOP to unsubscribe.</Message></Response>"
TWIML_HELP_REPLY  = "<?xml version='1.0' encoding='UTF-8'?><Response><Message>Grafton Dental Care: Reply STOP to unsubscribe. Call 508-318-4477 for help.</Message></Response>"
TWIML_EMPTY       = "<?xml version='1.0' encoding='UTF-8'?><Response/>"


def _verify_twilio_signature(request_url: str, post_params: dict,
                              signature: str, auth_token: str) -> bool:
    """Validate Twilio request signature."""
    try:
        from twilio.request_validator import RequestValidator
        validator = RequestValidator(auth_token)
        return validator.validate(request_url, post_params, signature)
    except Exception:
        return False


@app.post("/webhooks/twilio/inbound")
async def twilio_inbound_webhook(request: Request):
    """
    Handle inbound SMS from Twilio.

    Signature verification has three modes (controlled by DB setting
    'twilio_sig_mode'):
      enforce   — reject invalid signatures (HTTP 403)
      log_only  — log mismatch but continue (default in dev)
      skip      — skip verification entirely (test/CI only)
    """
    from fastapi.responses import Response as _Resp
    from database import (
        get_lead_by_phone, set_lead_dnd, insert_sms_message,
        add_lead_event, cancel_queue_rows, get_setting as _get_setting,
    )
    from stop_engine import handle_event as _stop_handle

    settings = get_settings()

    # Parse form body
    form = await request.form()
    post_params = dict(form)

    from_number = post_params.get("From", "")
    to_number   = post_params.get("To", "")
    body_raw    = post_params.get("Body", "")
    twilio_sid  = post_params.get("MessageSid", "")
    body_clean  = body_raw.strip()

    # ── Twilio signature verification ────────────────────────────────────────
    # sig_valid tracks whether the signature check passed.
    # In log_only mode, bad-sig requests are logged but state mutations are blocked.
    sig_valid = True
    sig_mode = _get_setting("twilio_sig_mode", "log_only")
    if sig_mode != "skip" and settings.twilio_auth_token:
        x_sig = request.headers.get("X-Twilio-Signature", "")
        request_url = str(request.url)
        valid = _verify_twilio_signature(request_url, post_params,
                                         x_sig, settings.twilio_auth_token)
        if not valid:
            logger.warning(
                f"Twilio signature mismatch from={from_number} url={request_url}"
            )
            if sig_mode == "enforce":
                return _Resp(content=TWIML_EMPTY, media_type="application/xml",
                             status_code=403)
            # log_only: proceed for logging + SMS storage, but block state mutations
            sig_valid = False

    # ── Match lead by phone number ────────────────────────────────────────────
    lead = get_lead_by_phone(from_number)
    lead_id = lead["id"] if lead else None

    # ── Store inbound message (always — even on bad sig, for audit trail) ─────
    insert_sms_message(
        lead_id=lead_id,
        direction="inbound",
        from_number=from_number,
        to_number=to_number,
        body=body_clean,
        twilio_sid=twilio_sid,
    )

    # ── Parse first word for keyword handling ─────────────────────────────────
    words = body_clean.split()
    first_word = words[0].lower().strip(".,!?") if words else ""

    if first_word in _SMS_STOP_WORDS:
        if not sig_valid:
            # Bad sig in log_only mode — message is logged but don't mutate lead state
            logger.warning(
                f"STOP keyword received but signature invalid (log_only) — "
                f"skipping DND/cancellation for {from_number}"
            )
            return _Resp(content=TWIML_EMPTY, media_type="application/xml")
        if lead_id:
            # Set DND flag (reuses unsubscribed_sms column)
            set_lead_dnd(lead_id, "sms", reason="STOP keyword")
            # Cancel queued SMS rows + log sms_stop event via stop engine
            _stop_handle(lead_id, "sms_stop", reason="STOP keyword")
        else:
            # Unknown number — log but don't send confirmation (Twilio CTIA guidance)
            logger.info(f"STOP from unmatched number {from_number} — no confirmation sent")
            return _Resp(content=TWIML_EMPTY, media_type="application/xml")
        return _Resp(content=TWIML_STOP_REPLY, media_type="application/xml")

    elif first_word in _SMS_START_WORDS:
        if not sig_valid:
            logger.warning(
                f"START keyword received but signature invalid (log_only) — "
                f"skipping re-subscribe for {from_number}"
            )
            return _Resp(content=TWIML_EMPTY, media_type="application/xml")
        if lead_id:
            # Clear unsubscribed_sms flag
            from database import _conn as _dbc
            _now_ts = datetime.now(timezone.utc).isoformat()
            with _dbc() as _c:
                _c.execute(
                    "UPDATE leads SET unsubscribed_sms=0, dnd_reason='', dnd_set_at='', updated_at=? WHERE id=?",
                    (_now_ts, lead_id)
                )
            add_lead_event(lead_id, "sms_resubscribed", source="twilio_webhook")
        return _Resp(content=TWIML_START_REPLY, media_type="application/xml")

    elif first_word in _SMS_HELP_WORDS:
        # HELP is informational — no state mutation, safe to reply even on bad sig
        return _Resp(content=TWIML_HELP_REPLY, media_type="application/xml")

    else:
        # Regular reply — log the event only if sig is valid (stop_engine: log-only)
        if lead_id and sig_valid:
            _stop_handle(lead_id, "replied", reason=f"inbound_sms: {body_clean[:80]}")
        return _Resp(content=TWIML_EMPTY, media_type="application/xml")


# ── Admin stop-condition endpoints ────────────────────────────────────────────

class PauseLeadRequest(BaseModel):
    reason: str = "admin"
    until: str = ""   # ISO timestamp or '' for indefinite


class DndRequest(BaseModel):
    channel: str    # 'sms' | 'email' | 'all'
    reason: str = "admin"


@app.post("/api/admin/lead/{lead_id}/pause", dependencies=[Depends(_require_admin)])
def admin_pause_lead(lead_id: str, body: PauseLeadRequest):
    """Pause a lead's follow-up sequence (indefinitely or until a timestamp)."""
    from database import pause_lead
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    pause_lead(lead_id, reason=body.reason, until=body.until)
    from stop_engine import handle_event as _stop_handle
    _stop_handle(lead_id, "manual_pause", reason=body.reason)
    return {"status": "ok", "lead_id": lead_id, "paused": True}


@app.post("/api/admin/lead/{lead_id}/resume", dependencies=[Depends(_require_admin)])
def admin_resume_lead(lead_id: str):
    """Resume a paused lead."""
    from database import resume_lead
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    resume_lead(lead_id)
    return {"status": "ok", "lead_id": lead_id, "paused": False}


@app.post("/api/admin/lead/{lead_id}/dnd", dependencies=[Depends(_require_admin)])
def admin_set_dnd(lead_id: str, body: DndRequest):
    """Set DND (do-not-disturb) for a lead on one or all channels."""
    from database import set_lead_dnd
    from stop_engine import handle_event as _stop_handle
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    channels = ["sms", "email"] if body.channel == "all" else [body.channel]
    for ch in channels:
        set_lead_dnd(lead_id, ch, reason=body.reason)
    _stop_handle(lead_id, "dnd_set", reason=body.reason)
    return {"status": "ok", "lead_id": lead_id, "dnd_channels": channels}


@app.post("/api/admin/lead/{lead_id}/clear-dnd", dependencies=[Depends(_require_admin)])
async def admin_clear_dnd(lead_id: str, request: Request):
    """Clear DND flags for a lead (admin override — re-enables future messages)."""
    body = await request.json()
    channel = body.get("channel", "all")
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    _now_ts = datetime.now(timezone.utc).isoformat()
    from database import _conn as _dbc
    with _dbc() as _c:
        if channel in ("sms", "all"):
            _c.execute(
                "UPDATE leads SET unsubscribed_sms=0, dnd_reason='', dnd_set_at='', updated_at=? WHERE id=?",
                (_now_ts, lead_id)
            )
        if channel in ("email", "all"):
            _c.execute(
                "UPDATE leads SET unsubscribed_email=0, dnd_reason='', dnd_set_at='', updated_at=? WHERE id=?",
                (_now_ts, lead_id)
            )
    add_lead_event(lead_id, "dnd_cleared", detail=json.dumps({"channel": channel}), source="admin")
    return {"status": "ok", "lead_id": lead_id, "channel": channel}


# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/admin/run-queue", dependencies=[Depends(_require_admin)])
def admin_run_queue():
    run_now()
    return {"status": "ok", "message": "Follow-up queue processed"}


@app.put("/api/admin/lead/{lead_id}/stage", dependencies=[Depends(_require_admin)])
async def admin_update_stage(lead_id: str, request: Request):
    body = await request.json()
    new_stage = body.get("stage")
    if not new_stage:
        raise HTTPException(status_code=400, detail="stage required")
    lead = update_stage(lead_id, new_stage, source="admin")
    return {"status": "ok", "lead": lead}


# ─── Force stage (allows backward movement) ─────────────────────────────────

@app.put("/api/admin/lead/{lead_id}/force-stage", dependencies=[Depends(_require_admin)])
async def admin_force_stage(lead_id: str, request: Request):
    body = await request.json()
    new_stage = body.get("stage")
    if not new_stage:
        raise HTTPException(status_code=400, detail="stage required")
    from database import LIFECYCLE_STAGES
    if new_stage not in LIFECYCLE_STAGES:
        raise HTTPException(status_code=400, detail=f"Invalid stage: {new_stage}")
    lead = force_stage(lead_id, new_stage, source="admin",
                       detail=json.dumps({"reason": body.get("reason", "manual move")}))
    return {"status": "ok", "lead": lead}


# ─── Notes ───────────────────────────────────────────────────────────────────

@app.get("/api/admin/lead/{lead_id}/notes", dependencies=[Depends(_require_admin)])
def admin_get_notes(lead_id: str):
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    return {"notes": get_notes(lead_id)}


@app.post("/api/admin/lead/{lead_id}/notes", dependencies=[Depends(_require_admin)])
async def admin_add_note(lead_id: str, request: Request):
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    body = await request.json()
    text = body.get("note_text", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="note_text required")
    note = add_note(lead_id, text, author=body.get("author", "admin"))
    return {"status": "ok", "note": note}


@app.delete("/api/admin/note/{note_id}", dependencies=[Depends(_require_admin)])
def admin_delete_note(note_id: int):
    delete_note(note_id)
    return {"status": "ok"}


# ─── Lead Tags ────────────────────────────────────────────────────────────────

class TagsUpdateRequest(BaseModel):
    tags: list  # full replacement list

@app.get("/api/admin/lead/{lead_id}/tags", dependencies=[Depends(_require_admin)])
def admin_get_tags(lead_id: str):
    if not get_lead(lead_id):
        raise HTTPException(status_code=404, detail="Lead not found")
    return {"tags": get_lead_tags(lead_id)}


@app.put("/api/admin/lead/{lead_id}/tags", dependencies=[Depends(_require_admin)])
def admin_set_tags(lead_id: str, body: TagsUpdateRequest):
    if not get_lead(lead_id):
        raise HTTPException(status_code=404, detail="Lead not found")
    updated = set_lead_tags(lead_id, body.tags)
    return {"tags": updated}


# ─── Manual Lead Creation ────────────────────────────────────────────────────

class ManualLeadRequest(BaseModel):
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    source: str = "manual"
    notes: str = ""

    @validator("first_name", "last_name", "email", "phone", "source", "notes", pre=True)
    def coerce_str(cls, v):
        return str(v).strip() if v is not None else ""

    @validator("source")
    def source_not_empty(cls, v):
        return v if v.strip() else "manual"


@app.post("/api/admin/leads/create", dependencies=[Depends(_require_admin)])
def admin_create_lead(body: ManualLeadRequest):
    if not body.first_name and not body.email and not body.phone:
        raise HTTPException(status_code=400, detail="Provide at least a name, email, or phone")
    lead_id = str(uuid.uuid4())
    data = {
        "id": lead_id,
        "first_name": body.first_name,
        "last_name": body.last_name,
        "email": body.email,
        "phone": body.phone,
        "source": body.source,
        "notes": body.notes,
        "stage": "new",
    }
    lead = upsert_lead(data)
    # Log a lead_created event so the timeline isn't empty
    # Deliberately NOT enqueuing follow-ups — manual leads are staff-initiated contacts
    add_event(lead_id, "lead_created", stage_to="new",
              source=body.source, detail="manual entry")
    return {"status": "ok", "lead": lead}


@app.delete("/api/admin/lead/{lead_id}", dependencies=[Depends(_require_admin)])
def admin_delete_lead(lead_id: str):
    """
    Permanently delete a lead and all associated data (events, queue, notes).
    Also deletes smile image from GCS if present.
    Use with care — this is irreversible.
    """
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    settings = get_settings()

    # Tombstone first — even if Firestore delete or GCS delete fails below,
    # the next Firestore sync will refuse to re-import this lead.
    tombstone_written = False
    try:
        add_deleted_lead_tombstone(
            lead_id,
            email=lead.get("email", ""),
            deleted_by="admin",
            reason="admin_delete_lead",
        )
        tombstone_written = True
        logger.info(f"Tombstone written for lead {lead_id} ({lead.get('email','')})")
    except Exception as e:
        logger.warning(f"Could not write tombstone for lead {lead_id}: {e}")

    # Delete smile image from GCS using blob name (preferred) or parse from URL (legacy)
    gcs_blob_name = lead.get("smile_blob_name", "")
    image_url = lead.get("smile_image_url", "")
    if not gcs_blob_name and image_url and "storage.googleapis.com" in image_url:
        try:
            path = image_url.split("storage.googleapis.com/")[1].split("?")[0]
            _, gcs_blob_name = path.split("/", 1)
        except Exception:
            pass
    if gcs_blob_name:
        try:
            from google.cloud import storage as gcs_storage
            client = gcs_storage.Client()
            client.bucket(settings.gcs_bucket).blob(gcs_blob_name).delete()
            logger.info(f"Deleted GCS smile image for lead {lead_id}: {gcs_blob_name}")
        except Exception as e:
            logger.warning(f"Could not delete GCS image for lead {lead_id}: {e}")

    # Delete from Firestore via nxtsmile API
    # Pass email in X-Lead-Email header so old docs (no 'id' field) can be found by email
    firestore_deleted = False
    try:
        import requests as _req
        delete_url = f"{settings.nxtsmile_api}/api/leads/{lead_id}"
        logger.info(f"Calling Firestore delete: DELETE {delete_url}")
        resp = _req.delete(
            delete_url,
            headers={
                "X-Secret": settings.firestore_secret,
                "X-Lead-Email": lead.get("email", ""),
            },
            timeout=15,
        )
        result_body = resp.json() if resp.content else {}
        if resp.status_code in (200, 204, 404):
            docs_deleted = result_body.get("firestore_docs_deleted", "?")
            logger.info(f"Firestore delete for {lead_id}: {docs_deleted} doc(s) removed (status {resp.status_code})")
            firestore_deleted = True
        else:
            logger.warning(f"Firestore delete returned {resp.status_code} for lead {lead_id}: {result_body}")
    except Exception as e:
        logger.warning(f"Could not delete lead {lead_id} from Firestore: {e}")

    # Delete all associated local records then the lead itself
    from database import _conn
    with _conn() as conn:
        conn.execute("DELETE FROM lifecycle_events WHERE lead_id = ?", (lead_id,))
        conn.execute("DELETE FROM follow_up_queue WHERE lead_id = ?", (lead_id,))
        conn.execute("DELETE FROM lead_notes WHERE lead_id = ?", (lead_id,))
        conn.execute("DELETE FROM conversion_uploads WHERE lead_id = ?", (lead_id,))
        conn.execute("DELETE FROM leads WHERE id = ?", (lead_id,))

    logger.info(f"Lead {lead_id} ({lead.get('email','')}) permanently deleted by admin (tombstone={tombstone_written}, firestore={'yes' if firestore_deleted else 'FAILED'})")
    return {"status": "deleted", "lead_id": lead_id, "tombstone_written": tombstone_written, "firestore_deleted": firestore_deleted}


# ─── Test Email ──────────────────────────────────────────────────────────────

class TestEmailRequest(BaseModel):
    lead_id: str
    template: str          # day1, day7, day14, day30, noshow
    override_email: str = ""  # if set, send to this address instead of lead's email

@app.post("/api/admin/test-email", dependencies=[Depends(_require_admin)])
def admin_test_email(body: TestEmailRequest):
    """
    Fire a specific nurture email template to a lead immediately.
    Use override_email to redirect to your own inbox for testing.
    Templates: day1, day7, day14, day30, noshow
    """
    from database import get_lead
    from email_service import (
        send_day1_email, send_day7_email, send_day14_email,
        send_day30_cold_email, send_no_show_email,
    )
    from config import get_settings

    lead = get_lead(body.lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail=f"Lead {body.lead_id} not found")

    # Override email for testing without spamming real patients
    test_lead = dict(lead)
    if body.override_email:
        test_lead["email"] = body.override_email

    settings = get_settings()
    unsubscribe_url = f"http://localhost:{settings.port}/unsubscribe/{body.lead_id}"

    template = body.template.lower()
    try:
        if template == "day1":
            result = send_day1_email(test_lead, unsubscribe_url)
        elif template == "day7":
            result = send_day7_email(test_lead, unsubscribe_url)
        elif template == "day14":
            result = send_day14_email(test_lead, unsubscribe_url)
        elif template == "day30":
            result = send_day30_cold_email(test_lead, unsubscribe_url)
        elif template == "noshow":
            result = send_no_show_email(test_lead, unsubscribe_url)
        else:
            raise HTTPException(status_code=400, detail=f"Unknown template '{template}'. Use: day1, day7, day14, day30, noshow")

        return {
            "status": "sent" if result else "failed",
            "template": template,
            "to": test_lead["email"],
            "lead_id": body.lead_id,
            "has_smile_image": bool(lead.get("smile_blob_name") or lead.get("smile_image_url")),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Test email failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/debug-smile/{lead_id}", dependencies=[Depends(_require_admin)])
def debug_smile_resign(lead_id: str):
    """Debug endpoint: test GCS re-sign for a lead's smile blob."""
    from database import get_lead
    from email_service import _fetch_smile_image
    from datetime import timedelta
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    blob_name = lead.get("smile_blob_name", "")
    result = {"lead_id": lead_id, "blob_name": blob_name, "smile_image_url": lead.get("smile_image_url", "")}
    if blob_name:
        # Test re-sign step by step, capturing exact errors
        try:
            from google.cloud import storage as gcs_storage
            result["gcs_import"] = "ok"
        except Exception as e:
            result["gcs_import_error"] = str(e)
            return result
        try:
            import google.auth
            from google.auth.transport import requests as g_requests
            credentials, project = google.auth.default()
            result["auth_project"] = project
            result["credentials_type"] = type(credentials).__name__
        except Exception as e:
            result["auth_error"] = str(e)
            return result
        try:
            credentials.refresh(g_requests.Request())
            result["token_ok"] = bool(credentials.token)
            result["token_prefix"] = credentials.token[:20] if credentials.token else ""
        except Exception as e:
            result["refresh_error"] = str(e)
            return result
        # Test direct blob download (Strategy 1 — no signBlob needed)
        try:
            from config import get_settings
            settings = get_settings()
            result["gcs_bucket"] = settings.gcs_bucket
            client = gcs_storage.Client()
            blob = client.bucket(settings.gcs_bucket).blob(blob_name)
            data = blob.download_as_bytes()
            result["direct_download_bytes"] = len(data)
            result["direct_download_ok"] = len(data) > 1000
        except Exception as e:
            result["direct_download_error"] = str(e)

        # Test signed URL (Strategy 2 — needs signBlob permission)
        try:
            sa_email = getattr(settings, "gcs_sa_email", "1096868046685-compute@developer.gserviceaccount.com")
            result["sa_email"] = sa_email
            blob2 = client.bucket(settings.gcs_bucket).blob(blob_name)
            signed_url = blob2.generate_signed_url(
                expiration=timedelta(days=7), method="GET", version="v4",
                service_account_email=sa_email, access_token=credentials.token,
            )
            result["signed_url"] = signed_url[:80] if signed_url else ""
            result["resign_ok"] = bool(signed_url)
        except Exception as e:
            result["sign_error"] = str(e)
    return result


# ─── GA4 Analytics ───────────────────────────────────────────────────────────

@app.get("/api/admin/ga4", dependencies=[Depends(_require_admin)])
def admin_ga4(days: int = 30, force_refresh: bool = False):
    """Return GA4 analytics data (cached or fresh)."""
    from database import get_ga4_cache, save_ga4_cache

    # Try cache first (unless force refresh)
    if not force_refresh:
        cached = get_ga4_cache("full_report", days, max_age_hours=12)
        if cached:
            cached["from_cache"] = True
            return cached

    # Pull fresh data
    try:
        from ga4_reporting import fetch_all_ga4_data
        data = fetch_all_ga4_data(days=days)
        if not data.get("overview", {}).get("error"):
            save_ga4_cache("full_report", days, data)
        data["from_cache"] = False
        return data
    except ImportError:
        return {"error": "google-analytics-data not installed", "configured": False}
    except Exception as e:
        logger.error(f"GA4 data fetch failed: {e}")
        return {"error": str(e), "configured": True}


@app.post("/api/admin/ga4/refresh", dependencies=[Depends(_require_admin)])
def admin_ga4_refresh(days: int = 30):
    """Force refresh GA4 data."""
    return admin_ga4(days=days, force_refresh=True)


# ─── Campaign stats ──────────────────────────────────────────────────────────

@app.get("/api/admin/campaigns", dependencies=[Depends(_require_admin)])
def admin_campaigns():
    return {
        "campaigns": get_campaign_stats(),
        "google_ads_campaigns": get_google_ads_campaigns(),
        "sources": get_distinct_sources(),
        "keywords": get_keyword_stats(),
    }


class CampaignCreateRequest(BaseModel):
    campaign_name: str
    campaign_type: str = "MANUAL"          # MANUAL, GOOGLE_ADS, META, EMAIL
    campaign_id: Optional[str] = None      # Google Ads ID or custom; auto-generated if blank
    service_focus: Optional[str] = ""      # Implants, Invisalign, Whitening, Emergency, etc.
    promo_offer: Optional[str] = ""        # e.g. "$99 exam + X-ray"
    target_audience: Optional[str] = ""
    objective: Optional[str] = ""
    monthly_budget: Optional[float] = 0.0
    expected_cpl: Optional[float] = 0.0
    start_date: Optional[str] = ""        # YYYY-MM-DD
    end_date: Optional[str] = ""
    landing_page: Optional[str] = ""
    notes: Optional[str] = ""
    workflow_id: Optional[int] = None      # Attached follow-up workflow (NULL = use default)

    @validator("campaign_name")
    def name_not_empty(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("Campaign name is required")
        if len(v) > 200:
            raise ValueError("Campaign name too long (max 200 chars)")
        return v

    @validator("campaign_type")
    def type_valid(cls, v):
        allowed = {"MANUAL", "GOOGLE_ADS", "META", "EMAIL"}
        if v.upper() not in allowed:
            raise ValueError(f"campaign_type must be one of {allowed}")
        return v.upper()

    @validator("monthly_budget", "expected_cpl", pre=True)
    def budget_non_negative(cls, v):
        if v is not None and float(v) < 0:
            raise ValueError("Budget values must be >= 0")
        return v

    @validator("end_date")
    def end_after_start(cls, v, values):
        start = values.get("start_date") or ""
        if v and start and v < start:
            raise ValueError("end_date must be >= start_date")
        return v

    @validator("landing_page")
    def url_format(cls, v):
        if v and not v.startswith(("http://", "https://")):
            raise ValueError("landing_page must start with http:// or https://")
        return v

    @validator("workflow_id", pre=True)
    def coerce_workflow_id(cls, v):
        """Coerce empty string → None so frontend select can send '' for 'no workflow'."""
        if v in (None, "", "0", 0):
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None


class CampaignUpdateWorkflowRequest(BaseModel):
    workflow_id: Optional[int] = None

    @validator("workflow_id", pre=True)
    def coerce_workflow_id(cls, v):
        if v in (None, "", "0", 0):
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None


@app.get("/api/admin/campaigns/list", dependencies=[Depends(_require_admin)])
def admin_campaigns_list():
    """Return all managed campaign rows from the campaigns table."""
    from database import get_all_campaigns
    return {"campaigns": get_all_campaigns()}


@app.get("/api/admin/campaigns/list-with-workflows", dependencies=[Depends(_require_admin)])
def admin_campaigns_list_with_workflows():
    """Return all campaigns with their attached workflow name (single LEFT JOIN)."""
    from database import get_all_campaigns_with_workflows
    return {"campaigns": get_all_campaigns_with_workflows()}


@app.post("/api/admin/campaigns/create", dependencies=[Depends(_require_admin)])
def admin_create_campaign(body: CampaignCreateRequest):
    """Create a new managed campaign record."""
    from database import create_campaign
    try:
        row = create_campaign(body.dict())
        return {"ok": True, "campaign": row}
    except Exception as e:
        logger.error(f"create_campaign failed: {e}")
        # Don't leak SQL internals; surface a clean message
        detail = "A campaign with that name already exists" if "UNIQUE" in str(e) else "Failed to create campaign"
        raise HTTPException(status_code=500, detail=detail)


@app.patch("/api/admin/campaigns/{campaign_id}/workflow", dependencies=[Depends(_require_admin)])
def admin_campaign_set_workflow(campaign_id: str, body: CampaignUpdateWorkflowRequest):
    """Attach or detach a workflow from an existing campaign.
    Send {"workflow_id": 3} to attach, {"workflow_id": null} to detach.
    """
    from database import update_campaign_workflow
    found = update_campaign_workflow(campaign_id, body.workflow_id)
    if not found:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return {"ok": True}


class CampaignStrategyUpdateRequest(BaseModel):
    strategy: dict


@app.get("/api/admin/campaigns/unified", dependencies=[Depends(_require_admin)])
def admin_campaigns_unified(days: int = 30, include_inactive: bool = False):
    """
    Unified campaigns view — replaces the old split between Campaign Performance
    and Managed Campaigns. Returns each campaign with aggregated GAds metrics,
    lead counts, last_activity_date, and is_inactive_90d flag.
    Synthetic rows are emitted for GAds campaigns in gads_daily_stats that were
    never imported into the campaigns table.
    """
    from database import get_unified_campaigns
    if days < 1 or days > 365:
        raise HTTPException(status_code=422, detail="days must be between 1 and 365")
    rows = get_unified_campaigns(days=days)
    if not include_inactive:
        rows = [r for r in rows if not r.get("is_inactive_90d")]
    return {"campaigns": rows, "days": days, "include_inactive": include_inactive}


class CampaignUpdateFieldsRequest(BaseModel):
    campaign_name: str | None = None
    service_focus: str | None = None
    monthly_budget: float | None = None
    start_date: str | None = None
    end_date: str | None = None
    notes: str | None = None
    promo_offer: str | None = None
    landing_page: str | None = None
    objective: str | None = None
    target_audience: str | None = None
    expected_cpl: float | None = None


@app.patch("/api/admin/campaigns/{campaign_id}", dependencies=[Depends(_require_admin)])
def admin_campaign_update_fields(campaign_id: str, body: CampaignUpdateFieldsRequest):
    """Update editable fields on a campaign (name, budget, service focus, dates, etc.)."""
    from database import update_campaign_fields, get_campaign_by_id
    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    ok = update_campaign_fields(campaign_id, fields)
    if not ok:
        raise HTTPException(status_code=500, detail="Update failed")
    return {"ok": True, "campaign_id": campaign_id, "updated": list(fields.keys())}


class CampaignBuildStepRefineRequest(BaseModel):
    step: str          # "keywords" | "ad_copy" | "ad_groups"
    instruction: str   # Natural language instruction from user


@app.post("/api/admin/campaigns/{campaign_id}/build-step-refine", dependencies=[Depends(_require_admin)])
async def admin_campaign_build_step_refine(campaign_id: str, body: CampaignBuildStepRefineRequest):
    """
    Iteratively refine a build step using a user instruction.
    Reads current content for the step, applies the instruction via Sonnet,
    returns the refined content WITHOUT saving — caller decides to accept or discard.
    """
    from database import get_campaign_by_id, get_campaign_build
    import anthropic as _anthropic, json as _json, re as _re

    VALID_STEPS = {"keywords", "ad_copy", "ad_groups", "strategy"}
    if body.step not in VALID_STEPS:
        raise HTTPException(status_code=400, detail=f"Refinement only supported for: {VALID_STEPS}")

    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")

    # Strategy is stored in strategy_json on the campaign row; others are in campaign_build_json
    if body.step == "strategy":
        raw_strat = camp.get("strategy_json") or {}
        current = _json.loads(raw_strat) if isinstance(raw_strat, str) else raw_strat
        if not current:
            raise HTTPException(status_code=400, detail="No strategy to refine. Generate it first.")
    else:
        build = get_campaign_build(campaign_id)
        current = build.get(body.step)
        if not current:
            raise HTTPException(status_code=400, detail=f"No existing {body.step} to refine. Generate it first.")

    strategy = camp.get("strategy_json") or {}
    if isinstance(strategy, str):
        try:
            strategy = _json.loads(strategy)
        except Exception:
            strategy = {}

    _api_key = get_setting("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    ai_client = _anthropic.Anthropic(api_key=_api_key)

    step_guidance = ""
    if body.step == "strategy":
        step_guidance = """For the strategy step, the JSON schema is:
{objective, target_audience, key_messages (list), ad_headlines (list), ad_descriptions (list), implementation_instructions}
Keep all fields present. Modify only what the user instruction targets."""

    prompt = f"""You are a Google Ads specialist helping refine a campaign build step.

Campaign: {camp.get("campaign_name", "")}
Service Focus: {camp.get("service_focus", "")}
Objective: {strategy.get("objective", "")}

Current {body.step} content:
{_json.dumps(current, indent=2)}

User instruction: {body.instruction}

{step_guidance}
Apply the user's instruction to modify the {body.step} content. Return the complete updated {body.step} JSON structure — same format as the input, with the requested changes applied.

Rules:
- Keep all existing items unless the user asked to remove specific ones
- Add new items where instructed
- Maintain the exact same JSON structure/schema as the current content
- Return ONLY the JSON object, no explanation."""

    try:
        response = ai_client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        json_match = _re.search(r'\{[\s\S]*\}|\[[\s\S]*\]', raw)
        if not json_match:
            raise ValueError("No JSON found in AI response")
        refined = _json.loads(json_match.group())
    except Exception as e:
        logger.error(f"build-step-refine AI call failed ({body.step}): {e}")
        raise HTTPException(status_code=500, detail=f"AI refinement failed: {e}")

    logger.info(f"Campaign {campaign_id} step '{body.step}' refined (not yet saved)")
    return {"ok": True, "step": body.step, "data": refined}


class CampaignAiReviewRequest(BaseModel):
    enabled: bool


@app.patch("/api/admin/campaigns/{campaign_id}/ai-review", dependencies=[Depends(_require_admin)])
def admin_campaign_set_ai_review(campaign_id: str, body: CampaignAiReviewRequest):
    """
    Toggle the AI Review flag on a managed campaign.
    When enabled=true, the nightly ai_optimizer restricts keyword analysis
    to this campaign (plus any others also flagged on).
    """
    from database import set_campaign_ai_review, get_campaign_by_id
    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    ok = set_campaign_ai_review(campaign_id, body.enabled)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to update AI Review flag")
    logger.info(f"AI Review flag for {campaign_id} → {body.enabled}")
    return {"ok": True, "campaign_id": campaign_id, "ai_review_enabled": body.enabled}


class CampaignAiMaxRequest(BaseModel):
    enabled: bool


@app.patch("/api/admin/campaigns/{campaign_id}/ai-max", dependencies=[Depends(_require_admin)])
def admin_campaign_set_ai_max(campaign_id: str, body: CampaignAiMaxRequest):
    """
    Enable or disable Google Ads AI Max on a managed campaign.

    AI Max allows Google's AI to expand search term matching beyond the keyword
    list. Only works on Search campaigns linked to Google Ads.

    When enabled=true → calls enable_ai_max() on the GAds API, then updates local DB.
    When enabled=false → calls disable_ai_max(), then updates local DB.
    DB is only updated when the API call succeeds.

    Historical search_term_type='ai_max' data is never retroactively cleared
    when disabling — only future syncs are affected.
    """
    from database import get_campaign_by_id, set_campaign_ai_max
    from google_ads_create import enable_ai_max, disable_ai_max

    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")

    resource_name = camp.get("gads_campaign_resource") or ""
    if not resource_name:
        raise HTTPException(
            status_code=400,
            detail="Campaign is not yet linked to Google Ads. Launch it to Google Ads first."
        )

    if body.enabled:
        result = enable_ai_max(resource_name)
    else:
        result = disable_ai_max(resource_name)

    if not result.get("ok"):
        error_msg = result.get("error") or "Google Ads API call failed"
        logger.error(f"AI Max toggle failed for {campaign_id}: {error_msg}")
        raise HTTPException(status_code=502, detail=error_msg)

    # Only update local DB after confirmed API success
    ok = set_campaign_ai_max(campaign_id, body.enabled)
    if not ok:
        logger.warning(f"AI Max API succeeded but local DB update failed for {campaign_id}")

    action = "enabled" if body.enabled else "disabled"
    logger.info(f"AI Max {action} for campaign {campaign_id} ({resource_name})")
    return {"ok": True, "campaign_id": campaign_id, "ai_max_enabled": body.enabled}


@app.get("/api/admin/campaigns/{campaign_id}/search-term-types", dependencies=[Depends(_require_admin)])
def admin_campaign_search_term_types(campaign_id: str, days: int = 30):
    """
    Return a breakdown of leads by search_term_type for a campaign.
    Used by the Performance tab to show AI Max vs standard match type attribution.
    """
    from database import get_search_term_type_breakdown
    breakdown = get_search_term_type_breakdown(campaign_id, days)
    return {"campaign_id": campaign_id, "days": days, "breakdown": breakdown}


class CampaignBuildStepRequest(BaseModel):
    step: str  # "keywords" | "ad_copy" | "ad_groups" | "launch_checklist"


class CampaignBuildStepSaveRequest(BaseModel):
    step: str
    data: dict | list  # the accepted refined content to persist


@app.post("/api/admin/campaigns/{campaign_id}/build-step-save", dependencies=[Depends(_require_admin)])
def admin_campaign_build_step_save(campaign_id: str, body: CampaignBuildStepSaveRequest):
    """Save accepted refined build step data into campaign_build_json (or strategy_json for strategy step)."""
    from database import get_campaign_by_id, save_campaign_build_step, update_campaign_strategy
    import json as _json
    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    VALID_STEPS = {"keywords", "ad_copy", "ad_groups", "launch_checklist", "strategy"}
    if body.step not in VALID_STEPS:
        raise HTTPException(status_code=400, detail=f"Invalid step")
    if body.step == "strategy":
        # Strategy lives in strategy_json on the campaign row
        update_campaign_strategy(campaign_id, body.data)
    else:
        save_campaign_build_step(campaign_id, body.step, body.data)
    return {"ok": True, "step": body.step}


@app.post("/api/admin/campaigns/{campaign_id}/build-step", dependencies=[Depends(_require_admin)])
async def admin_campaign_build_step(campaign_id: str, body: CampaignBuildStepRequest):
    """
    AI-generate one stage of the campaign build pipeline using Claude Sonnet.

    Steps:
      keywords      — target keywords by match type + negatives
      ad_copy       — finalized RSA headlines (15) + descriptions (4) per ad group
      ad_groups     — keyword → ad group mapping with bid suggestions
      launch_checklist — readiness checklist (returns template, not AI-generated)

    Uses the campaign's strategy_json as context for all AI steps.
    Result is saved into campaign_build_json[step] and returned.
    """
    from database import get_campaign_by_id, get_campaign_build, save_campaign_build_step
    import anthropic as _anthropic

    VALID_STEPS = {"keywords", "ad_copy", "ad_groups", "launch_checklist"}
    if body.step not in VALID_STEPS:
        raise HTTPException(status_code=400, detail=f"Invalid step. Must be one of {VALID_STEPS}")

    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")

    step = body.step

    # Launch checklist is a static template — no AI needed
    if step == "launch_checklist":
        checklist = [
            {"item": "Campaign budget confirmed",               "done": False},
            {"item": "Geographic targeting set",                "done": False},
            {"item": "Ad schedule configured",                  "done": False},
            {"item": "Negative keywords added",                 "done": False},
            {"item": "Ad extensions added (call, location)",    "done": False},
            {"item": "Conversion tracking verified",            "done": False},
            {"item": "Landing page reviewed",                   "done": False},
            {"item": "Campaign reviewed and approved",          "done": False},
        ]
        save_campaign_build_step(campaign_id, step, checklist)
        return {"ok": True, "step": step, "data": checklist}

    # Build AI prompt using strategy context
    strategy = camp.get("strategy_json") or {}
    if isinstance(strategy, str):
        try:
            import json as _json
            strategy = _json.loads(strategy)
        except Exception:
            strategy = {}

    # Existing build data for context
    build = get_campaign_build(campaign_id)

    _api_key = get_setting("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    ai_client = _anthropic.Anthropic(api_key=_api_key)

    campaign_name = camp.get("campaign_name", "")
    service_focus = camp.get("service_focus", "")
    budget = camp.get("monthly_budget", 0)
    objective = strategy.get("objective", "")
    target_audience = strategy.get("target_audience", "")
    key_messages = strategy.get("key_messages", [])
    impl_notes = strategy.get("implementation_instructions", "")

    if step == "keywords":
        prompt = f"""You are a Google Ads specialist. Generate a comprehensive keyword list for this dental campaign.

Campaign: {campaign_name}
Service Focus: {service_focus}
Monthly Budget: ${budget}
Objective: {objective}
Target Audience: {target_audience}
Key Messages: {', '.join(key_messages)}
Implementation Notes: {impl_notes}

Return a JSON object with this exact structure:
{{
  "exact_match": ["keyword1", "keyword2", ...],
  "phrase_match": ["keyword1", "keyword2", ...],
  "broad_match_modifier": ["keyword1", "keyword2", ...],
  "negative_keywords": ["keyword1", "keyword2", ...]
}}

Rules:
- exact_match: 8-12 high-intent, specific keywords (e.g. "emergency dentist near me")
- phrase_match: 10-15 moderate-intent phrases
- broad_match_modifier: 5-8 broader terms to capture volume
- negative_keywords: 15-20 terms to exclude (jobs, DIY, insurance-only, etc.)
- All keywords should be relevant to the dental service and local search intent
- Return ONLY the JSON object, no explanation."""

    elif step == "ad_copy":
        keywords = build.get("keywords", {})
        kw_context = f"Target keywords: {', '.join(keywords.get('exact_match', [])[:8])}" if keywords else ""
        headlines_from_strategy = strategy.get("ad_headlines", [])
        descs_from_strategy = strategy.get("ad_descriptions", [])

        prompt = f"""You are a Google Ads copywriter. Generate complete RSA ad copy for this dental campaign.

Campaign: {campaign_name}
Service Focus: {service_focus}
Objective: {objective}
Target Audience: {target_audience}
{kw_context}
Strategy Headlines: {', '.join(headlines_from_strategy)}
Strategy Descriptions: {'; '.join(descs_from_strategy)}
Implementation Notes: {impl_notes}

Return a JSON object with this exact structure:
{{
  "ad_groups": [
    {{
      "name": "Ad Group Name",
      "theme": "What this group targets",
      "headlines": ["H1", "H2", "H3", "H4", "H5", "H6", "H7", "H8", "H9", "H10", "H11", "H12", "H13", "H14", "H15"],
      "descriptions": ["D1 (90 chars max)", "D2 (90 chars max)", "D3 (90 chars max)", "D4 (90 chars max)"]
    }}
  ]
}}

Rules:
- Create 2-3 ad groups based on keyword themes
- Headlines: exactly 15 per ad group, max 30 characters each
- Descriptions: exactly 4 per ad group, max 90 characters each
- Include the practice service, urgency, differentiators, and CTAs
- No punctuation at end of headlines
- Return ONLY the JSON object, no explanation."""

    elif step == "ad_groups":
        keywords = build.get("keywords", {})
        ad_copy = build.get("ad_copy", {})
        kw_context = f"Keywords: {keywords}" if keywords else "No keywords generated yet."
        groups_context = f"Ad groups from copy: {[g.get('name') for g in ad_copy.get('ad_groups', [])]}" if ad_copy else ""

        prompt = f"""You are a Google Ads account manager. Create the final ad group structure for this campaign.

Campaign: {campaign_name}
Service Focus: {service_focus}
Monthly Budget: ${budget}
{kw_context}
{groups_context}
Implementation Notes: {impl_notes}

Return a JSON object with this exact structure:
{{
  "ad_groups": [
    {{
      "name": "Ad Group Name",
      "theme": "Brief theme description",
      "match_types": ["exact", "phrase"],
      "keywords": ["keyword1", "keyword2", ...],
      "suggested_cpc_usd": 3.50,
      "daily_budget_pct": 40,
      "notes": "Why this group and bidding rationale"
    }}
  ],
  "bidding_strategy": "Recommended bidding strategy and rationale",
  "budget_allocation_notes": "How to split the monthly budget across groups"
}}

Rules:
- Create 2-3 ad groups that map to the keyword themes
- daily_budget_pct values should sum to 100
- suggested_cpc_usd should be realistic for dental keywords ($2-8 range)
- Return ONLY the JSON object, no explanation."""

    try:
        response = ai_client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()

        # Extract JSON
        import re as _re, json as _json
        json_match = _re.search(r'\{[\s\S]*\}', raw)
        if not json_match:
            raise ValueError("No JSON found in AI response")
        data = _json.loads(json_match.group())

    except Exception as e:
        logger.error(f"build-step AI call failed ({step}): {e}")
        raise HTTPException(status_code=500, detail=f"AI generation failed: {e}")

    save_campaign_build_step(campaign_id, step, data)
    logger.info(f"Campaign {campaign_id} build step '{step}' generated and saved")
    return {"ok": True, "step": step, "data": data}


@app.patch("/api/admin/campaigns/{campaign_id}/strategy", dependencies=[Depends(_require_admin)])
def admin_campaign_save_strategy(campaign_id: str, body: CampaignStrategyUpdateRequest):
    """Persist the Opus-generated strategy JSON to the campaign record."""
    from database import update_campaign_strategy
    found = update_campaign_strategy(campaign_id, body.strategy)
    if not found:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return {"ok": True}


@app.patch("/api/admin/campaigns/{campaign_id}/status", dependencies=[Depends(_require_admin)])
def admin_campaign_status(campaign_id: str, status: str = Body(..., embed=True)):
    """Update a campaign's status (ACTIVE, PAUSED, COMPLETED, ARCHIVED).
    Accepts JSON body: {"status": "PAUSED"}
    """
    from database import update_campaign_status
    # ACTIVE and PAUSED must go through the /pause and /resume endpoints
    # which also sync Google Ads. Only allow non-GAds transitions here.
    allowed = {"DRAFT", "COMPLETED", "ARCHIVED"}
    if status.upper() not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"Use /pause or /resume for ACTIVE/PAUSED transitions. This endpoint only accepts {sorted(allowed)}"
        )
    found = update_campaign_status(campaign_id, status.upper())
    if not found:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return {"ok": True}


# ─── Campaign lifecycle controls (Pause / Resume / Stop) ─────────────────────

@app.post("/api/admin/campaigns/{campaign_id}/pause", dependencies=[Depends(_require_admin)])
def admin_campaign_pause(campaign_id: str):
    """Pause a campaign — locally and in Google Ads if linked."""
    from database import get_campaign_by_id, update_campaign_status
    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")

    # Idempotency guard — already paused, nothing to do
    if camp["status"] == "PAUSED":
        return {"ok": True, "status": "PAUSED", "gads_updated": False, "note": "already paused"}

    gads_updated = False
    gads_error = None
    if camp.get("gads_campaign_resource"):
        from google_ads_create import set_campaign_status
        result = set_campaign_status(camp["gads_campaign_resource"], "PAUSED")
        gads_updated = result["ok"]
        gads_error = result.get("error")
        if not gads_updated:
            # Don't flip local status if the remote call failed for a linked campaign
            raise HTTPException(
                status_code=502,
                detail=f"Google Ads pause failed: {gads_error}. Local status unchanged."
            )

    update_campaign_status(campaign_id, "PAUSED")
    return {"ok": True, "status": "PAUSED", "gads_updated": gads_updated}


@app.post("/api/admin/campaigns/{campaign_id}/resume", dependencies=[Depends(_require_admin)])
def admin_campaign_resume(campaign_id: str):
    """Resume (enable) a campaign — locally and in Google Ads if linked."""
    from database import get_campaign_by_id, update_campaign_status
    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")

    # Idempotency guard — already active, nothing to do
    if camp["status"] == "ACTIVE":
        return {"ok": True, "status": "ACTIVE", "gads_updated": False, "note": "already active"}
    # Prevent resuming a stopped campaign
    if camp["status"] in ("ARCHIVED", "COMPLETED"):
        raise HTTPException(status_code=422, detail=f"Cannot resume a campaign with status {camp['status']}")

    gads_updated = False
    gads_error = None
    if camp.get("gads_campaign_resource"):
        from google_ads_create import set_campaign_status
        result = set_campaign_status(camp["gads_campaign_resource"], "ENABLED")
        gads_updated = result["ok"]
        gads_error = result.get("error")
        if not gads_updated:
            raise HTTPException(
                status_code=502,
                detail=f"Google Ads resume failed: {gads_error}. Local status unchanged."
            )

    update_campaign_status(campaign_id, "ACTIVE")
    return {"ok": True, "status": "ACTIVE", "gads_updated": gads_updated}


@app.post("/api/admin/campaigns/{campaign_id}/stop", dependencies=[Depends(_require_admin)])
def admin_campaign_stop(campaign_id: str):
    """Permanently stop a campaign. REMOVED in Google Ads (irreversible), ARCHIVED locally."""
    from database import get_campaign_by_id, update_campaign_status
    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")

    # Idempotency guard — already stopped, nothing to do (and GAds won't accept a REMOVED mutate)
    if camp["status"] == "ARCHIVED":
        return {"ok": True, "status": "ARCHIVED", "gads_updated": False, "note": "already stopped"}

    gads_updated = False
    gads_error = None
    if camp.get("gads_campaign_resource"):
        from google_ads_create import set_campaign_status
        result = set_campaign_status(camp["gads_campaign_resource"], "REMOVED")
        gads_updated = result["ok"]
        gads_error = result.get("error")
        if not gads_updated:
            raise HTTPException(
                status_code=502,
                detail=f"Google Ads stop failed: {gads_error}. Local status unchanged."
            )

    update_campaign_status(campaign_id, "ARCHIVED")
    return {"ok": True, "status": "ARCHIVED", "gads_updated": gads_updated}


@app.delete("/api/admin/campaigns/{campaign_id}", dependencies=[Depends(_require_admin)])
def admin_campaign_delete(campaign_id: str):
    """
    Permanently delete a campaign from the local dashboard.
    Does NOT touch Google Ads — only removes the local DB record.
    Use for cleaning up unlinked/test campaigns.
    """
    from database import get_campaign_by_id, delete_campaign
    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if camp.get("gads_campaign_resource"):
        raise HTTPException(
            status_code=400,
            detail="Cannot delete a campaign linked to Google Ads. Use Stop instead."
        )
    deleted = delete_campaign(campaign_id)
    return {"ok": deleted, "campaign_id": campaign_id}


# ─── Google Ads Campaign Import ──────────────────────────────────────────────

@app.get("/api/admin/gads/list-campaigns", dependencies=[Depends(_require_admin)])
def admin_gads_list_campaigns():
    """
    Fetch all non-REMOVED campaigns from the Google Ads account.
    Marks each one as already_imported if it exists in local campaigns table.
    READ-ONLY — no kill switch required.
    """
    from google_ads_create import fetch_campaigns_from_gads
    from database import get_all_campaigns_with_workflows

    try:
        gads_campaigns = fetch_campaigns_from_gads()
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Could not fetch campaigns from Google Ads: {e}"
        )

    # Build already-imported set from both gads_campaign_numeric_id AND campaign_id
    # (handles manual campaigns that used a numeric ID as campaign_id)
    local = get_all_campaigns_with_workflows()
    already_imported = set()
    for c in local:
        if c.get("gads_campaign_numeric_id"):
            already_imported.add(c["gads_campaign_numeric_id"])
        if c.get("campaign_id"):
            already_imported.add(c["campaign_id"])

    for c in gads_campaigns:
        c["already_imported"] = c["campaign_id"] in already_imported

    return {"campaigns": gads_campaigns, "total": len(gads_campaigns)}


class ImportCampaignsRequest(BaseModel):
    campaign_ids: list[str]   # GAds numeric campaign IDs to import


def _backfill_campaign_snapshot(campaign_id: str, resource_name: str) -> None:
    """
    Background task: fetch keywords/ads/ad-groups from Google Ads and store
    them in gads_campaign_snapshot.  Runs after import so the HTTP response
    is not blocked by 10-20s of API calls.
    """
    from google_ads_create import fetch_campaign_build_data
    from database import save_gads_campaign_snapshot
    try:
        snapshot = fetch_campaign_build_data(resource_name)
        if snapshot.get("error"):
            logger.warning(f"Snapshot backfill skipped for {campaign_id}: {snapshot['error']}")
            return
        save_gads_campaign_snapshot(campaign_id, snapshot)
        logger.info(f"Snapshot backfill complete for campaign {campaign_id}")
    except Exception as e:
        logger.error(f"Snapshot backfill failed for {campaign_id}: {e}")


@app.post("/api/admin/gads/import-campaigns", dependencies=[Depends(_require_admin)])
def admin_import_campaigns(body: ImportCampaignsRequest, background_tasks: BackgroundTasks):
    """
    Import selected Google Ads campaigns into the local managed campaigns table.
    Sets gads_campaign_resource + gads_campaign_numeric_id in one atomic INSERT.
    Skips already-imported campaigns silently.
    After each import, a background task fetches keywords/ads/ad-groups from
    Google Ads and stores them in gads_campaign_snapshot (non-blocking).
    """
    from google_ads_create import fetch_campaigns_from_gads
    from database import create_campaign, get_campaign_by_id

    if not body.campaign_ids:
        raise HTTPException(status_code=422, detail="No campaign IDs provided")

    try:
        gads_campaigns = fetch_campaigns_from_gads()
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Could not fetch campaigns from Google Ads: {e}"
        )

    gads_map = {c["campaign_id"]: c for c in gads_campaigns}

    imported = []
    skipped  = []
    errors   = []

    for cid in body.campaign_ids:
        if cid not in gads_map:
            errors.append({"campaign_id": cid, "error": "Not found in Google Ads"})
            continue

        gads = gads_map[cid]

        # Already imported? (use numeric ID as campaign_id, so get_campaign_by_id works)
        existing = get_campaign_by_id(cid)
        if existing:
            skipped.append(cid)
            continue

        # Map GAds status → local status
        local_status = "ACTIVE" if gads["gads_status"] == "ENABLED" else "PAUSED"

        data = {
            "campaign_id":               cid,              # GAds numeric ID = local logical key
            "campaign_name":             gads["campaign_name"],
            "status":                    local_status,
            "campaign_type":             "GOOGLE_ADS",
            "monthly_budget":            gads["monthly_budget_usd"],   # daily × 30 approx
            "start_date":                gads["start_date"],
            "end_date":                  gads["end_date"],
            "notes":                     f"Imported from Google Ads. Channel: {gads['channel_type']}. Daily budget: ${gads['daily_budget_usd']}/day.",
            "gads_campaign_resource":    gads["resource_name"],
            "gads_campaign_numeric_id":  cid,
        }

        try:
            create_campaign(data)
            logger.info(f"Imported GAds campaign: {cid} '{gads['campaign_name']}'")
            imported.append({"campaign_id": cid, "campaign_name": gads["campaign_name"]})
            # Non-blocking snapshot backfill — fetches keywords/ads/ad-groups in background
            background_tasks.add_task(_backfill_campaign_snapshot, cid, gads["resource_name"])
        except Exception as e:
            logger.error(f"Failed to import GAds campaign {cid}: {e}")
            errors.append({"campaign_id": cid, "error": str(e)})

    logger.info(f"GAds import complete: {len(imported)} imported, {len(skipped)} skipped, {len(errors)} errors")
    return {"imported": imported, "skipped": skipped, "errors": errors}


@app.post("/api/admin/campaigns/{campaign_id}/sync-from-gads", dependencies=[Depends(_require_admin)])
def admin_sync_campaign_from_gads(campaign_id: str):
    """
    On-demand sync: re-fetch keywords, ad copies, and ad groups from Google Ads
    for a campaign that was already imported.  Stores results in
    gads_campaign_snapshot (does NOT touch campaign_build_json, so user edits
    in the wizard are never clobbered).

    Returns the updated snapshot so the frontend can render it immediately.
    """
    from database import get_campaign_by_id, save_gads_campaign_snapshot, get_gads_campaign_snapshot
    from google_ads_create import fetch_campaign_build_data

    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")

    resource_name = camp.get("gads_campaign_resource") or ""
    if not resource_name:
        raise HTTPException(
            status_code=400,
            detail="Campaign is not linked to Google Ads — import it first"
        )

    snapshot = fetch_campaign_build_data(resource_name)
    if snapshot.get("error"):
        raise HTTPException(
            status_code=502,
            detail=f"Google Ads API error: {snapshot['error']}"
        )

    save_gads_campaign_snapshot(campaign_id, snapshot)
    logger.info(f"Manual GAds sync complete for campaign {campaign_id}")
    return {
        "ok": True,
        "campaign_id": campaign_id,
        "snapshot": snapshot,
        "synced_at": snapshot.get("synced_from_gads_at"),
    }


# ─── Campaign detail (click-through from Managed Campaigns table) ────────────

@app.get("/api/admin/campaigns/{campaign_id}/detail", dependencies=[Depends(_require_admin)])
def admin_campaign_detail(campaign_id: str, days: int = 30):
    """
    Full campaign detail: base info + strategy + GAds performance + ad groups + ad creatives.
    Returns everything needed for the campaign detail drawer in one request.
    """
    from database import (
        get_campaign_by_id, get_daily_stats, get_ad_group_stats, get_ads_with_metrics
    )
    import json as _json

    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")

    # Parse strategy_json if stored as string
    strategy = None
    if camp.get("strategy_json"):
        try:
            strategy = _json.loads(camp["strategy_json"]) if isinstance(camp["strategy_json"], str) else camp["strategy_json"]
        except Exception:
            strategy = None

    # Daily stats filtered to this campaign
    daily_stats = get_daily_stats(days=days, campaign_id=campaign_id)
    for row in daily_stats:
        row["cost"] = round((row.get("cost_micros") or 0) / 1_000_000.0, 2)

    # Aggregate summary from daily stats
    total_impressions = sum(r.get("impressions") or 0 for r in daily_stats)
    total_clicks      = sum(r.get("clicks") or 0 for r in daily_stats)
    total_cost        = sum(r.get("cost") or 0.0 for r in daily_stats)
    total_conversions = sum(r.get("conversions") or 0 for r in daily_stats)
    ctr  = round(total_clicks / total_impressions * 100, 2) if total_impressions > 0 else 0.0
    cpc  = round(total_cost / total_clicks, 2) if total_clicks > 0 else 0.0
    cpl  = round(total_cost / total_conversions, 2) if total_conversions > 0 else 0.0

    # Ad groups for this campaign (filter from all ad groups by campaign_id)
    all_ag = get_ad_group_stats(days=days)
    # gads_daily_stats uses campaign_id column; filter by numeric id or name match
    gads_num_id = camp.get("gads_campaign_numeric_id") or ""
    camp_name   = camp.get("campaign_name") or ""
    ad_groups = [
        ag for ag in all_ag
        if ag.get("campaign_id") == gads_num_id
        or ag.get("campaign_name", "").lower() == camp_name.lower()
    ]

    # Ad creatives for this campaign
    all_ads = get_ads_with_metrics(days=days)
    ads = [
        ad for ad in all_ads
        if ad.get("campaign_id") == gads_num_id
        or ad.get("campaign_name", "").lower() == camp_name.lower()
    ]
    for ad in ads:
        impressions = ad.get("impressions") or 0
        clicks      = ad.get("clicks") or 0
        cost_micros = ad.get("cost_micros") or 0
        leads       = ad.get("leads") or 0
        cost        = cost_micros / 1_000_000.0
        ad["cost"]  = round(cost, 2)
        ad["ctr"]   = round(clicks / impressions * 100, 2) if impressions > 0 else 0.0
        ad["cpc"]   = round(cost / clicks, 2) if clicks > 0 else 0.0
        ad["cpl"]   = round(cost / leads, 2)  if leads  > 0 else 0.0
        if isinstance(ad.get("assets_json"), str):
            try:
                ad["assets_json"] = _json.loads(ad["assets_json"])
            except Exception:
                ad["assets_json"] = {"headlines": [], "descriptions": []}
        if not isinstance(ad.get("assets_json"), dict):
            ad["assets_json"] = {"headlines": [], "descriptions": []}

    # Lead attribution: count leads linked to this campaign_id
    with __import__("database")._conn() as conn:
        lead_row = conn.execute(
            "SELECT COUNT(*) as cnt FROM leads WHERE campaign_id=?", (campaign_id,)
        ).fetchone()
        lead_count = lead_row["cnt"] if lead_row else 0

    # Parse campaign_build_json
    from database import get_campaign_build, get_gads_campaign_snapshot
    build_data = get_campaign_build(campaign_id)

    # Snapshot from Google Ads (raw imported state — separate from user-edited build)
    gads_snapshot = get_gads_campaign_snapshot(campaign_id)

    return {
        "campaign": {k: v for k, v in camp.items() if k not in ("strategy_json", "campaign_build_json", "gads_campaign_snapshot")},
        "strategy": strategy,
        "build": build_data,
        "gads_snapshot": gads_snapshot,
        "summary": {
            "days": days,
            "impressions": total_impressions,
            "clicks": total_clicks,
            "cost": round(total_cost, 2),
            "conversions": total_conversions,
            "ctr": ctr,
            "cpc": cpc,
            "cpl": cpl,
            "leads": lead_count,
        },
        "daily_stats": daily_stats,
        "ad_groups": ad_groups,
        "ads": ads,
        "has_gads_data": bool(camp.get("gads_campaign_resource")),
    }


# ─── Google Ads extended reporting ───────────────────────────────────────────

@app.get("/api/admin/gads/ad-groups", dependencies=[Depends(_require_admin)])
def admin_gads_ad_groups():
    """Ad-group level aggregated stats from gads_daily_stats + leads."""
    return {"ad_groups": get_ad_group_stats(days=30)}


@app.get("/api/admin/gads/daily-stats", dependencies=[Depends(_require_admin)])
def admin_gads_daily_stats(days: int = 30, campaign_id: Optional[str] = None):
    """Daily time-series stats per campaign (summed across ad groups). Max 90 days."""
    days = min(max(int(days), 1), 90)
    return {"rows": get_daily_stats(days=days, campaign_id=campaign_id or None), "days": days}


@app.get("/api/admin/gads/search-terms", dependencies=[Depends(_require_admin)])
def admin_gads_search_terms(days: int = 30, campaign: str = ""):
    """Search terms from gads_search_terms_cache, optionally filtered by campaign name."""
    return {"search_terms": get_search_term_stats(campaign_name=campaign, days=days)}


@app.get("/api/admin/gads/ads", dependencies=[Depends(_require_admin)])
def admin_gads_ads(days: int = 30):
    """
    List all ad creatives with aggregated metrics and lead counts for the last N days.
    Includes CTR and CPL computed server-side for convenience.
    """
    from database import get_ads_with_metrics
    ads = get_ads_with_metrics(days=days)
    for ad in ads:
        impressions = ad.get("impressions") or 0
        clicks      = ad.get("clicks") or 0
        cost_micros = ad.get("cost_micros") or 0
        leads       = ad.get("leads") or 0
        cost        = cost_micros / 1_000_000.0
        ad["cost"]  = round(cost, 2)
        ad["ctr"]   = round(clicks / impressions * 100, 2) if impressions > 0 else 0.0
        ad["cpc"]   = round(cost / clicks, 2) if clicks > 0 else 0.0
        ad["cpl"]   = round(cost / leads, 2)  if leads  > 0 else 0.0
        # Parse assets_json if stored as string
        if isinstance(ad.get("assets_json"), str):
            try:
                ad["assets_json"] = json.loads(ad["assets_json"])
            except Exception:
                ad["assets_json"] = {"headlines": [], "descriptions": []}
        # Ensure it's always a dict shape the frontend expects
        if not isinstance(ad.get("assets_json"), dict):
            ad["assets_json"] = {"headlines": [], "descriptions": []}
    return {"ads": ads, "days": days}


@app.get("/api/admin/gads/ads/{ad_id}/metrics", dependencies=[Depends(_require_admin)])
def admin_gads_ad_metrics(ad_id: str, days: int = 30):
    """Daily metrics time-series for a single ad creative."""
    from database import get_ad_metrics_series
    rows = get_ad_metrics_series(ad_id=ad_id, days=days)
    for row in rows:
        row["cost"] = round((row.get("cost_micros") or 0) / 1_000_000.0, 2)
    return {"ad_id": ad_id, "days": days, "metrics": rows}


# ─── Pipeline with enrichment ────────────────────────────────────────────────

@app.get("/api/pipeline/enriched")
def get_pipeline_enriched(stage: Optional[str] = None, campaign: Optional[str] = None, limit: int = 500):
    """Return all leads enriched with notes count, for Kanban board."""
    from database import _conn
    with _conn() as conn:
        query = "SELECT l.*, (SELECT COUNT(*) FROM lead_notes n WHERE n.lead_id = l.id) as notes_count FROM leads l"
        params = []
        conditions = []
        if stage:
            conditions.append("l.stage = ?")
            params.append(stage)
        if campaign:
            conditions.append(
                "(l.campaign_name = ? OR l.utm_campaign = ? OR (l.campaign_name = '' AND l.utm_campaign = '' AND l.source = ?))"
            )
            params.extend([campaign, campaign, campaign])
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY l.updated_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        leads = [dict(r) for r in rows]
    return {"leads": leads, "total": len(leads)}


# ─── Scheduled Jobs Status ───────────────────────────────────────────────────

@app.get("/api/admin/jobs", dependencies=[Depends(_require_admin)])
def get_job_status():
    """Return status of all scheduled jobs including last_run and next_run times."""

    def _fmt(dt):
        if dt is None:
            return None
        try:
            return dt.isoformat()
        except Exception:
            return str(dt)

    job_list = []

    # Ads + optimizer scheduler jobs (ga4_pull, gads_sync, ai_optimizer, od_sync, conversion_upload)
    if ads_scheduler:
        for job in ads_scheduler.get_jobs():
            job_list.append({
                "id": job.id,
                "name": job.name or job.id,
                "next_run": _fmt(job.next_run_time),
                "last_run": _job_last_run.get(job.id),
            })

    # Follow-up engine runs on its own internal scheduler; add as static entry
    job_list.insert(0, {
        "id": "follow_up_engine",
        "name": "Follow-up Engine",
        "next_run": None,
        "last_run": _job_last_run.get("follow_up_engine"),
        "schedule": "Every 15 min",
    })

    return {"jobs": job_list}


# ─── Optimizer Memory ────────────────────────────────────────────────────────

@app.get("/api/admin/optimizer/memory", dependencies=[Depends(_require_admin)])
def get_memory(category: Optional[str] = None, include_inactive: bool = False):
    """Return all optimizer memory entries."""
    from database import get_optimizer_memory
    entries = get_optimizer_memory(category=category, active_only=not include_inactive)
    return {"memory": entries, "total": len(entries)}


class MemoryCreate(BaseModel):
    category: str   # 'term_classification', 'keyword_override', 'campaign_rule', 'general'
    key: str
    value: str      # 'negative', 'good_keyword', 'irrelevant', 'never_pause', etc.
    reason: str
    author: str = "admin"
    campaign: str = ""  # empty = global, campaign name = scoped to that campaign


class MemoryUpdate(BaseModel):
    value: str
    reason: str


@app.post("/api/admin/optimizer/memory", dependencies=[Depends(_require_admin)])
def add_memory(body: MemoryCreate):
    """Add a new optimizer memory entry."""
    from database import add_optimizer_memory
    entry = add_optimizer_memory(
        category=body.category,
        key=body.key,
        value=body.value,
        reason=body.reason,
        author=body.author,
        campaign=body.campaign,
    )
    scope = f"campaign:{body.campaign}" if body.campaign else "global"
    logger.info(f"Optimizer memory added: [{body.category}] '{body.key}' = '{body.value}' (scope={scope})")
    return {"status": "ok", "entry": entry}


@app.put("/api/admin/optimizer/memory/{memory_id}", dependencies=[Depends(_require_admin)])
def update_memory(memory_id: int, body: MemoryUpdate):
    """Update value and reason for an existing memory entry."""
    from database import update_optimizer_memory
    entry = update_optimizer_memory(memory_id, body.value, body.reason)
    if not entry:
        raise HTTPException(status_code=404, detail="Memory entry not found or inactive")
    return {"status": "ok", "entry": entry}


@app.delete("/api/admin/optimizer/memory/{memory_id}", dependencies=[Depends(_require_admin)])
def delete_memory(memory_id: int):
    """Soft-delete (deactivate) a memory entry."""
    from database import deactivate_optimizer_memory
    deactivate_optimizer_memory(memory_id)
    logger.info(f"Optimizer memory deactivated: id={memory_id}")
    return {"status": "ok"}


# ── Google Ads Intelligence Endpoints ────────────────────────────────────────

@app.get("/api/admin/search-terms", dependencies=[Depends(_require_admin)])
def get_search_terms(campaign: str = "", days: int = 30):
    """Return cached search terms with lead attribution."""
    return {
        "search_terms": get_search_term_stats(campaign_name=campaign, days=days),
        "days": days,
        "campaign_filter": campaign,
    }


@app.get("/api/admin/geo-performance", dependencies=[Depends(_require_admin)])
def get_geo_performance(days: int = 30):
    """Return cached geographic performance data."""
    return {"geo": get_geo_stats(days=days), "days": days}


@app.get("/api/admin/schedule-performance", dependencies=[Depends(_require_admin)])
def get_schedule_performance(days: int = 30):
    """Return cached hour-of-day / day-of-week / device performance."""
    return {**get_schedule_stats(days=days), "days": days}


class KeywordResearchRequest(BaseModel):
    seed_keywords: list         # e.g. ["dental implants", "all on 4 near me"]
    budget: Optional[float] = None
    geo_target_ids: Optional[list] = None   # e.g. ["geoTargetConstants/1020615"]


@app.post("/api/admin/keyword-research", dependencies=[Depends(_require_admin)])
def keyword_research(body: KeywordResearchRequest):
    """
    Run Google Keyword Planner on seed keywords.
    Returns search volume, competition, CPC range, and 12-month trend.
    Use this before launching a new campaign to validate keyword demand.
    """
    try:
        from keyword_planner import get_keyword_ideas
        ideas = get_keyword_ideas(
            seed_keywords=body.seed_keywords,
            geo_target_ids=body.geo_target_ids or [],
        )
        logger.info(f"Keyword research: {len(ideas)} ideas for {body.seed_keywords}")
        return {
            "ideas": ideas,
            "seed_keywords": body.seed_keywords,
            "total": len(ideas),
        }
    except Exception as e:
        logger.error(f"Keyword research failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─── Email Inbox (Step 5 — bi-directional inbox) ─────────────────────────────

@app.post("/api/admin/email-inbox/poll", dependencies=[Depends(_require_admin)])
def admin_email_inbox_poll():
    """
    Manually trigger an IMAP poll of info@nxtsmile.com.
    Fetches UNSEEN messages, matches to leads, stores in conversations/messages.
    """
    try:
        from imap_service import poll_once
        result = poll_once()
        logger.info(f"Manual IMAP poll: {result}")
        return {"status": "ok", **result}
    except Exception as e:
        logger.error(f"Manual IMAP poll error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/conversations", dependencies=[Depends(_require_admin)])
def admin_get_conversations(limit: int = 100, unmatched_only: bool = False):
    """
    Return all email conversations, newest first.
    Each row includes lead name, contact email, last message preview, and message count.
    """
    convs = get_all_conversations(limit=limit, unmatched_only=unmatched_only)
    return {"conversations": convs, "total": len(convs)}


@app.get("/api/admin/conversations/{lead_id}", dependencies=[Depends(_require_admin)])
def admin_get_lead_conversation(lead_id: str):
    """Return full conversation thread for a specific lead."""
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    conv = get_conversation(lead_id)
    if not conv:
        return {"conversation": None, "messages": [], "lead": lead}
    messages = get_messages(conv["id"])
    # Do NOT auto-mark as read here — mark-read is triggered explicitly by
    # user action (send reply or "Mark as read" button) so the bell badge
    # stays visible until the user intentionally dismisses it.
    return {"conversation": conv, "messages": messages, "lead": lead}


class ReplyRequest(BaseModel):
    body: str
    subject: Optional[str] = None


@app.post("/api/admin/conversations/{lead_id}/reply", dependencies=[Depends(_require_admin)])
def admin_reply_to_lead(lead_id: str, body: ReplyRequest):
    """
    Send a staff reply email to a lead and store it in their conversation thread.
    In dev mode the email is redirected to test_redirect_email.
    """
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    try:
        from imap_service import send_reply
        result = send_reply(lead_id=lead_id, body=body.body, subject=body.subject)
        logger.info(f"Staff reply sent: lead={lead_id}, to={result['to']}")
        return {"status": "sent", **result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Reply failed for lead {lead_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─── Manual Messaging (Step 8) ────────────────────────────────────────────────

class ManualSmsRequest(BaseModel):
    message: str


class ManualEmailRequest(BaseModel):
    subject: str
    body: str


@app.get("/api/admin/lead/{lead_id}/messages", dependencies=[Depends(_require_admin)])
def admin_get_lead_messages(lead_id: str):
    """Return all messages (auto + manual) for a lead, ordered by timestamp."""
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    return {"messages": get_lead_messages(lead_id)}


@app.post("/api/admin/lead/{lead_id}/send-sms", dependencies=[Depends(_require_admin)])
def admin_send_manual_sms(lead_id: str, body: ManualSmsRequest):
    """Send a manual SMS to a lead. Respects kill switch and dev redirect."""
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    phone = (lead.get("phone") or "").strip()
    if not phone:
        raise HTTPException(status_code=422, detail="Lead has no phone number")
    msg = body.message.strip()
    if not msg:
        raise HTTPException(status_code=422, detail="Message cannot be empty")
    if len(msg) > 1600:
        raise HTTPException(status_code=422, detail="Message too long (max 1600 chars)")

    from sms_service import send_manual_sms
    ok = send_manual_sms(phone, msg)
    if not ok:
        raise HTTPException(status_code=502, detail="SMS send failed — check logs")
    msg_id = None
    try:
        msg_id = save_outbound_message(lead_id, "sms", "", msg, sent_by="admin")
    except Exception as e:
        logger.error(f"save_outbound_message failed after SMS send: {e}", exc_info=True)
    return {"ok": True, "message_id": msg_id}


@app.post("/api/admin/lead/{lead_id}/send-email", dependencies=[Depends(_require_admin)])
def admin_send_manual_email(lead_id: str, body: ManualEmailRequest):
    """Send a manual email to a lead. Respects kill switch and dev redirect."""
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    to_email = (lead.get("email") or "").strip()
    if not to_email:
        raise HTTPException(status_code=422, detail="Lead has no email address")
    subject = body.subject.strip()
    email_body = body.body.strip()
    if not subject:
        raise HTTPException(status_code=422, detail="Subject cannot be empty")
    if not email_body:
        raise HTTPException(status_code=422, detail="Email body cannot be empty")
    if len(email_body) > 100_000:
        raise HTTPException(status_code=422, detail="Email body too long (max 100KB)")

    from email_service import send_manual_email
    ok = send_manual_email(to_email, subject, email_body)
    if not ok:
        raise HTTPException(status_code=502, detail="Email send failed — check logs")
    msg_id = None
    try:
        msg_id = save_outbound_message(lead_id, "email", subject, email_body, sent_by="admin")
    except Exception as e:
        logger.error(f"save_outbound_message failed after email send: {e}", exc_info=True)
    return {"ok": True, "message_id": msg_id}


# ─── Inbox / Unread SMS ───────────────────────────────────────────────────────

@app.get("/api/admin/inbox/unread", dependencies=[Depends(_require_admin)])
def admin_inbox_unread():
    """Return unread inbound SMS + email count + combined list of leads with unread messages."""
    sms_count = get_unread_sms_count()
    email_count = get_unread_email_count()
    sms_leads = get_unread_sms_leads()
    email_leads = get_unread_email_leads()

    # Merge: combine leads from both channels, dedupe by lead_id
    # SMS leads use 'last_received_at', email leads use 'latest_at' — normalize to 'latest_at'
    merged = {}
    for l in sms_leads:
        row = {**l, "channel": "sms", "has_unread_email": False,
               "latest_at": l.get("latest_at") or l.get("last_received_at") or ""}
        merged[l["lead_id"]] = row

    for el in email_leads:
        lid = el["lead_id"]
        el_at = el.get("latest_at") or ""
        if lid in merged:
            # Lead has both unread SMS and email — keep most recent timestamp
            merged[lid]["unread_count"] = merged[lid].get("unread_count", 0) + el.get("unread_count", 0)
            merged[lid]["has_unread_email"] = True
            merged[lid]["channel"] = "both"
            if el_at > merged[lid].get("latest_at", ""):
                merged[lid]["latest_at"] = el_at
                merged[lid]["latest_body"] = el.get("latest_body", merged[lid].get("latest_body"))
        else:
            merged[lid] = {**el, "channel": "email", "has_unread_email": True, "latest_at": el_at}

    leads_list = sorted(merged.values(), key=lambda x: x.get("latest_at", ""), reverse=True)
    return {
        "count": sms_count + email_count,
        "sms_count": sms_count,
        "email_count": email_count,
        "leads": leads_list,
    }

@app.post("/api/admin/lead/{lead_id}/mark-read", dependencies=[Depends(_require_admin)])
def admin_mark_sms_read(lead_id: str):
    """Mark all inbound SMS and email messages for a lead as read."""
    if not get_lead(lead_id):
        raise HTTPException(status_code=404, detail="Lead not found")
    sms_updated = mark_sms_read(lead_id)
    email_updated = mark_email_read(lead_id)
    return {"ok": True, "updated": sms_updated + email_updated}


# ─── Call Log ─────────────────────────────────────────────────────────────────

class LogCallRequest(BaseModel):
    direction: str = "outbound"    # 'outbound' | 'inbound'
    outcome: str                   # 'spoke' | 'left_vm' | 'no_answer' | 'callback_scheduled'
    duration_sec: int = 0
    notes: str = ""

@app.post("/api/admin/lead/{lead_id}/log-call", dependencies=[Depends(_require_admin)])
def admin_log_call(lead_id: str, body: LogCallRequest):
    """Log a manual phone call attempt or received call."""
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    valid_outcomes = {"spoke", "left_vm", "no_answer", "callback_scheduled"}
    if body.outcome not in valid_outcomes:
        raise HTTPException(status_code=422, detail=f"outcome must be one of {sorted(valid_outcomes)}")
    call_id = log_call(
        lead_id=lead_id,
        direction=body.direction,
        outcome=body.outcome,
        duration_sec=body.duration_sec,
        notes=body.notes,
    )
    return {"ok": True, "call_id": call_id}

@app.get("/api/admin/lead/{lead_id}/calls", dependencies=[Depends(_require_admin)])
def admin_get_calls(lead_id: str):
    """Return call log for a lead."""
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    return {"calls": get_calls(lead_id)}


# ─── Next Action ──────────────────────────────────────────────────────────────

class NextActionRequest(BaseModel):
    next_action_at: str    # ISO date string e.g. '2026-05-05'
    next_action_note: str = ""

@app.put("/api/admin/lead/{lead_id}/next-action", dependencies=[Depends(_require_admin)])
def admin_set_next_action(lead_id: str, body: NextActionRequest):
    """Set a next follow-up date and note on a lead."""
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    date_str = body.next_action_at.strip()
    import re as _re
    if not _re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        raise HTTPException(status_code=422, detail="next_action_at must be YYYY-MM-DD")
    set_next_action(lead_id, date_str, body.next_action_note.strip())
    return {"ok": True}

@app.delete("/api/admin/lead/{lead_id}/next-action", dependencies=[Depends(_require_admin)])
def admin_clear_next_action(lead_id: str):
    """Clear the next action on a lead."""
    if not get_lead(lead_id):
        raise HTTPException(status_code=404, detail="Lead not found")
    clear_next_action(lead_id)
    return {"ok": True}


# ─── Step 9: Workflow CRUD + AI Generate ─────────────────────────────────────

class WorkflowCreate(BaseModel):
    name: str
    campaign_tag: Optional[str] = ""
    description: Optional[str] = ""


class WorkflowStepCreate(BaseModel):
    workflow_id: int
    sequence_day: int
    channel: str           # 'email' | 'sms'
    template_name: str
    subject: str = ""
    body: str
    terminal: bool = False


class WorkflowStepUpdate(BaseModel):
    workflow_id: Optional[int] = None
    sequence_day: Optional[int] = None
    channel: Optional[str] = None
    template_name: Optional[str] = None
    subject: Optional[str] = None
    body: Optional[str] = None
    terminal: Optional[bool] = None


class AIGenerateRequest(BaseModel):
    prompt: str              # Free-text description of the campaign / goals
    num_steps: int = 6       # How many steps to generate


# ─── Practice Information Settings ──────────────────────────────────────────

_PRACTICE_FIELDS = [
    "name", "phone", "email", "doctor_name", "address", "hours", "review_link",
    "booking_link_consult", "booking_link_exam", "booking_link_implant",
    "booking_link_ortho", "booking_link_general",
]

class PracticeSettingsRequest(BaseModel):
    name:                 str = ""
    phone:                str = ""
    email:                str = ""
    doctor_name:          str = ""
    address:              str = ""
    hours:                str = ""
    review_link:          str = ""
    booking_link_consult: str = ""
    booking_link_exam:    str = ""
    booking_link_implant: str = ""
    booking_link_ortho:   str = ""
    booking_link_general: str = ""


# ─── AI Generate single message ──────────────────────────────────────────────

_APPT_TYPE_FIELD_MAP = {
    "consult":  "booking_link_consult",
    "exam":     "booking_link_exam",
    "implant":  "booking_link_implant",
    "ortho":    "booking_link_ortho",
    "general":  "booking_link_general",
}

class AIGenerateMessageRequest(BaseModel):
    channel:          str        # "email" or "sms"
    appointment_type: str = "general"
    prompt:           str


def _extract_json_from_ai_response(raw_text: str) -> str:
    """Extract JSON object from AI response that may have code fences or surrounding text."""
    json_text = raw_text.strip()
    # Use [\s\S]+ (greedy, matches newlines) so nested JSON objects are captured correctly.
    match = re.search(r"```(?:json)?\s*(\{[\s\S]+\})\s*```", json_text)
    if match:
        return match.group(1)
    if not json_text.startswith("{"):
        brace_match = re.search(r"\{[\s\S]+\}", json_text)
        if brace_match:
            return brace_match.group(0)
    return json_text


@app.get("/api/admin/workflows", dependencies=[Depends(_require_admin)])
def admin_list_workflows():
    workflows = get_all_workflows()
    result = []
    for wf in workflows:
        steps = get_workflow_steps(wf["id"])
        result.append({**wf, "steps": steps})
    return {"workflows": result}


@app.get("/api/admin/workflows/{workflow_id}", dependencies=[Depends(_require_admin)])
def admin_get_workflow(workflow_id: int):
    wf = get_workflow(workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")
    steps = get_workflow_steps(workflow_id)
    return {**wf, "steps": steps}


@app.post("/api/admin/workflows", dependencies=[Depends(_require_admin)])
def admin_create_workflow(body: WorkflowCreate):
    wf = upsert_workflow(None, body.name, body.campaign_tag, body.description)
    return wf


@app.put("/api/admin/workflows/{workflow_id}", dependencies=[Depends(_require_admin)])
def admin_update_workflow(workflow_id: int, body: WorkflowCreate):
    wf = get_workflow(workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return upsert_workflow(workflow_id, body.name, body.campaign_tag, body.description)


@app.delete("/api/admin/workflows/{workflow_id}", dependencies=[Depends(_require_admin)])
def admin_delete_workflow(workflow_id: int):
    wf = get_workflow(workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")
    delete_workflow(workflow_id)
    return {"ok": True}


@app.post("/api/admin/workflows/{workflow_id}/copy", dependencies=[Depends(_require_admin)])
def admin_copy_workflow(workflow_id: int):
    """Duplicate a workflow and all its steps. Returns the new workflow with steps."""
    src = get_workflow(workflow_id)
    if not src:
        raise HTTPException(status_code=404, detail="Workflow not found")
    src_steps = get_workflow_steps(workflow_id)

    # Generate unique tag — append a short uuid suffix to avoid UNIQUE collision
    suffix = uuid.uuid4().hex[:6]
    orig_tag = (src.get("campaign_tag") or "").strip()
    new_tag = f"{orig_tag}_cp{suffix}" if orig_tag else f"copy_{suffix}"
    # Strip any trailing " (Copy)" before appending to avoid "Foo (Copy) (Copy)"
    base_name = re.sub(r'\s*\(Copy\)\s*$', '', src['name']).strip()
    new_name = f"{base_name} (Copy)"

    new_wf = upsert_workflow(None, new_name, new_tag, src.get("description", ""))
    new_wf_id = new_wf["id"]

    # Duplicate steps with unique template names using new wf id
    for step in src_steps:
        new_tname = f"wf{new_wf_id}_d{step['sequence_day']}_{step['channel']}_{suffix}"
        upsert_workflow_step(
            None, new_wf_id,
            step["sequence_day"], step["channel"],
            new_tname, step.get("subject", ""), step["body"],
            bool(step.get("terminal", False))
        )

    # Return new workflow with steps
    new_steps = get_workflow_steps(new_wf_id)
    return {**new_wf, "steps": new_steps}


@app.post("/api/admin/workflows/{workflow_id}/seed-nxtsmile", dependencies=[Depends(_require_admin)])
def admin_seed_nxtsmile(workflow_id: int):
    """Seed the nXtSmile 4-email follow-up sequence into an empty workflow."""
    wf = get_workflow(workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")
    existing_steps = get_workflow_steps(workflow_id)
    if existing_steps:
        raise HTTPException(status_code=409, detail="Workflow already has steps — clear them first")

    # Load practice booking link from DB settings
    booking_link = get_setting("practice_booking_link_consult") or get_setting("practice_booking_link_general") or "https://visitgdc.com"
    office_phone = get_setting("practice_phone") or "(508) 839-9900"

    seed_steps = [
        (1, "email",
         f"wf{workflow_id}_d1_email",
         "Your new smile is closer than you think, {first_name} :)",
         f"Hi {{first_name}},\n\nWe hope you loved your smile preview! Take another look — this could be you.\n\n"
         f"Every smile transformation starts with a single step. Hundreds of patients walked into Grafton Dental Care "
         f"feeling unsure — and walked out with a smile they couldn't stop showing off.\n\n"
         f"You deserve to eat the foods you love, laugh without thinking twice, and feel proud every time you look in the mirror. "
         f"Dr. Gupta and the nXtSmile team are here to make that happen.\n\n"
         f"Your free consultation is waiting — no pressure, no obligation. Just a conversation about what's possible.\n\n"
         f"📅 Book online: {booking_link}\n📞 Or call us: {office_phone}\n\n"
         f"— Dr. Gupta's Team at Grafton Dental Care\n\n"
         f"To unsubscribe: {{unsub_url}}",
         False),
        (7, "email",
         f"wf{workflow_id}_d7_email",
         "What's holding you back, {first_name}?",
         f"Hi {{first_name}},\n\nWe've had a lot of people tell us the same things before they finally came in:\n\n"
         f"\"I'm worried about the cost.\"\n"
         f"We work with CareCredit, Cherry, and in-house financing — many patients pay as little as $300 a month.\n\n"
         f"\"I'm not sure I'm a candidate.\"\n"
         f"That's exactly what the free consultation is for. There's no commitment — just answers.\n\n"
         f"\"I'm nervous.\"\n"
         f"Dr. Gupta has helped hundreds of patients just like you. The consultation is relaxed and pressure-free.\n\n"
         f"\"Would it hurt?\"\n"
         f"Dr. Gupta is an expert in painless dentistry. You will be provided comfortable sedation to make the procedure as painless as possible.\n\n"
         f"📅 Book your free consult: {booking_link}\n📞 Or call: {office_phone}\n\n"
         f"— Dr. Gupta's Team at Grafton Dental Care\n\n"
         f"To unsubscribe: {{unsub_url}}",
         False),
        (14, "email",
         f"wf{workflow_id}_d14_email",
         "Your new smile might cost less than you think, {first_name}",
         f"Hi {{first_name}},\n\n"
         f"We wanted to share something that surprises most people — All-on-X dental implants don't have to be a huge upfront expense. "
         f"With our financing options, many patients pay as little as $300 a month — and they eat what they want, smile with confidence, "
         f"and never worry about dentures slipping again.\n\n"
         f"Your financing options:\n"
         f"🏦 CareCredit — 0% interest available\n"
         f"🍒 Cherry — instant approval, flexible monthly plans\n"
         f"🏥 In-house financing — we'll work with your situation\n\n"
         f"We'll discuss your financing options at your free consultation — a full treatment plan personalized to your budget. No surprises.\n\n"
         f"📅 Book now: {booking_link}\n📞 Call: {office_phone}\n\n"
         f"— Dr. Gupta's Team at Grafton Dental Care\n\n"
         f"To unsubscribe: {{unsub_url}}",
         False),
        (30, "email",
         f"wf{workflow_id}_d30_email",
         "Still here whenever you're ready, {first_name}",
         f"Hi {{first_name}},\n\n"
         f"We know life gets busy and sometimes the timing just isn't right. That's completely okay.\n\n"
         f"Whether it's next week, next month, or next year — you deserve a smile you're proud of, "
         f"and we'd love to help make that happen. Whenever you're ready, reach out to us.\n\n"
         f"🔒 Your smile preview will be deleted today as part of our privacy policy. "
         f"If you'd like to start fresh in the future, we can always create a new one for you.\n\n"
         f"📅 Book anytime: {booking_link}\n📞 Call: {office_phone}\n\n"
         f"Wishing you a healthy, confident smile — whenever the time is right.\n\n"
         f"— Dr. Gupta's Team at Grafton Dental Care\n\n"
         f"To unsubscribe: {{unsub_url}}",
         True),
    ]

    for day, channel, tname, subject, body, terminal in seed_steps:
        upsert_workflow_step(None, workflow_id, day, channel, tname, subject, body, terminal)

    steps = get_workflow_steps(workflow_id)
    return {**wf, "steps": steps}


@app.post("/api/admin/workflow-steps", dependencies=[Depends(_require_admin)])
def admin_create_workflow_step(body: WorkflowStepCreate):
    wf = get_workflow(body.workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")
    step = upsert_workflow_step(
        None, body.workflow_id, body.sequence_day, body.channel,
        body.template_name, body.subject, body.body, body.terminal
    )
    return step


@app.put("/api/admin/workflow-steps/{step_id}", dependencies=[Depends(_require_admin)])
def admin_update_workflow_step(step_id: int, body: WorkflowStepUpdate):
    existing = get_workflow_step(step_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Workflow step not found")
    # Merge incoming fields with existing
    merged = {
        "workflow_id": body.workflow_id if body.workflow_id is not None else existing["workflow_id"],
        "sequence_day": body.sequence_day if body.sequence_day is not None else existing["sequence_day"],
        "channel": body.channel if body.channel is not None else existing["channel"],
        "template_name": body.template_name if body.template_name is not None else existing["template_name"],
        "subject": body.subject if body.subject is not None else existing["subject"],
        "body": body.body if body.body is not None else existing["body"],
        "terminal": body.terminal if body.terminal is not None else bool(existing["terminal"]),
    }
    return upsert_workflow_step(
        step_id, merged["workflow_id"], merged["sequence_day"], merged["channel"],
        merged["template_name"], merged["subject"], merged["body"], merged["terminal"]
    )


@app.delete("/api/admin/workflow-steps/{step_id}", dependencies=[Depends(_require_admin)])
def admin_delete_workflow_step(step_id: int):
    existing = get_workflow_step(step_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Workflow step not found")
    delete_workflow_step(step_id)
    return {"ok": True}


@app.post("/api/admin/workflow/ai-generate", dependencies=[Depends(_require_admin)])
def admin_ai_generate_workflow(body: AIGenerateRequest):
    """Use Claude to generate a workflow step sequence from a natural-language prompt."""
    prompt_text = (body.prompt or "").strip()
    if not prompt_text:
        raise HTTPException(status_code=422, detail="Prompt is required")
    if len(prompt_text) > 2000:
        raise HTTPException(status_code=422, detail="Prompt too long (max 2000 chars)")

    num_steps = max(1, min(body.num_steps, 12))

    system_prompt = (
        "You are a dental marketing expert helping design automated patient follow-up sequences. "
        "Generate a JSON object with a 'steps' array. Each step must have these exact keys: "
        "sequence_day (integer), channel ('email' or 'sms'), template_name (unique slug, e.g. 'aox_day1_email'), "
        "subject (string, blank for SMS), body (string with {first_name} and {unsub_url} placeholders). "
        "SMS bodies must end with '\\nReply STOP to opt out.' "
        "Email bodies must include {unsub_url} near the end as an unsubscribe link. "
        "Return ONLY the JSON object, no explanation."
    )

    user_prompt = (
        f"Campaign description: {prompt_text}\n\n"
        f"Generate exactly {num_steps} follow-up steps optimized for this campaign. "
        "Make the messaging specific to the campaign goals. "
        "Days should be spread naturally (e.g. 1, 3, 7, 14, 21, 30 for a 6-step sequence). "
        "Return a JSON object with a 'steps' array."
    )

    try:
        import anthropic
        api_key = get_setting("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise HTTPException(status_code=400, detail="Anthropic API key not configured. Add it in Admin → AI Settings.")
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4096,
            messages=[
                {"role": "user", "content": user_prompt}
            ],
            system=system_prompt,
        )
        raw_text = message.content[0].text if message.content else ""
    except Exception as e:
        logger.error(f"AI generate workflow failed: {e}")
        raise HTTPException(status_code=502, detail=f"AI generation failed: {e}")

    # Extract JSON — handle code fences or bare JSON
    json_text = _extract_json_from_ai_response(raw_text)

    try:
        result = json.loads(json_text)
    except json.JSONDecodeError as e:
        logger.error(f"AI generate — JSON parse failed: {e}\nRaw: {raw_text[:500]}")
        raise HTTPException(status_code=502, detail="AI returned invalid JSON — please try again")

    # Schema validation
    steps = result.get("steps")
    if not isinstance(steps, list) or not steps:
        raise HTTPException(status_code=502, detail="AI response missing 'steps' array")

    required_keys = {"sequence_day", "channel", "template_name", "body"}
    for i, step in enumerate(steps):
        missing = required_keys - set(step.keys())
        if missing:
            raise HTTPException(
                status_code=502,
                detail=f"Step {i+1} missing required fields: {missing}"
            )
        if step["channel"] not in ("email", "sms"):
            raise HTTPException(
                status_code=502,
                detail=f"Step {i+1} has invalid channel: {step['channel']}"
            )
        if not isinstance(step.get("sequence_day"), int):
            raise HTTPException(
                status_code=502,
                detail=f"Step {i+1} sequence_day must be an integer"
            )
        # Ensure subject key exists
        if "subject" not in step:
            step["subject"] = ""

    return {"steps": steps}


# ─── AI Campaign — Strategy + Implementation + Performance Analysis ─────────
#
# Two-tier model: Opus 4.6 acts as the strategist/analyst; Haiku 4.5 (default)
# or Sonnet 4.6 acts as the implementer. The strategist gathers practice +
# performance context and produces a structured plan with explicit
# implementation_instructions; the implementer executes those instructions to
# produce final ad copy, SMS/email sequences, or analysis writeups.

OPUS_MODEL   = "claude-opus-4-6"
HAIKU_MODEL  = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-4-6"


class CampaignStrategyRequest(BaseModel):
    campaign_goal:      str
    target_service:     str = "All-on-4 Implants"
    budget_hint:        str = ""
    additional_context: str = ""


class CampaignImplementRequest(BaseModel):
    strategy:    dict
    deliverable: str         # 'ad_copy' | 'sms_sequence' | 'email_sequence' | 'full_package'
    model:       str = "haiku"  # 'haiku' | 'sonnet'


class PerformanceAnalysisRequest(BaseModel):
    time_range_days: int = 30
    focus:           str = "overall"  # 'overall' | 'google_ads' | 'leads' | 'conversions'


def _get_anthropic_client():
    """Resolve API key from settings/env and return an Anthropic client."""
    import anthropic
    api_key = get_setting("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="Anthropic API key not configured. Add it in Admin → AI Settings.",
        )
    return anthropic.Anthropic(api_key=api_key)


def _build_practice_context() -> dict:
    """Read practice info from app_settings into a plain dict."""
    return {f: get_setting(f"practice_{f}") or "" for f in _PRACTICE_FIELDS}


def _format_practice_context(practice: dict) -> str:
    """Compact text block describing the practice for the AI."""
    lines = []
    if practice.get("name"):         lines.append(f"Practice: {practice['name']}")
    if practice.get("doctor_name"):  lines.append(f"Doctor: {practice['doctor_name']}")
    if practice.get("phone"):        lines.append(f"Phone: {practice['phone']}")
    if practice.get("address"):      lines.append(f"Address: {practice['address']}")
    if practice.get("hours"):        lines.append(f"Hours: {practice['hours']}")
    booking_links = []
    for k in ("booking_link_consult", "booking_link_implant", "booking_link_ortho",
              "booking_link_exam", "booking_link_general"):
        if practice.get(k):
            booking_links.append(f"  - {k.replace('booking_link_','')}: {practice[k]}")
    if booking_links:
        lines.append("Booking links:")
        lines.extend(booking_links)
    return "\n".join(lines) if lines else "(no practice info on file)"


def _gather_performance_context(days: int = 30) -> dict:
    """Pull a compact summary of pipeline + Google Ads performance."""
    ctx = {}
    try:
        ctx["pipeline_stats"] = get_pipeline_stats()
    except Exception as e:
        logger.warning(f"AI campaign — pipeline_stats failed: {e}")
        ctx["pipeline_stats"] = {}

    try:
        camps = get_campaign_stats() or []
        # keep top 8 by lead_count for brevity
        camps_sorted = sorted(camps, key=lambda c: c.get("lead_count", 0), reverse=True)[:8]
        ctx["top_campaigns"] = [
            {
                "campaign":      c.get("campaign"),
                "lead_count":    c.get("lead_count", 0),
                "total_cost":    c.get("total_cost", 0),
                "cpl":           c.get("cpl", 0),
                "revenue":       c.get("revenue", 0),
                "scheduled":     c.get("scheduled_count", 0),
                "treated":       c.get("treated_count", 0),
            }
            for c in camps_sorted
        ]
    except Exception as e:
        logger.warning(f"AI campaign — campaign_stats failed: {e}")
        ctx["top_campaigns"] = []

    try:
        kws = get_keyword_stats() or []
        # keep top 12 by lead_count
        kws_sorted = sorted(kws, key=lambda k: k.get("lead_count", 0), reverse=True)[:12]
        ctx["top_keywords"] = [
            {
                "keyword":     k.get("keyword"),
                "campaign":    k.get("campaign_name"),
                "impressions": k.get("impressions", 0),
                "clicks":      k.get("gads_clicks", 0),
                "cost":        k.get("total_cost", 0),
                "leads":       k.get("lead_count", 0),
                "cpl":         k.get("cpl", 0),
                "conv_rate":   k.get("conversion_rate", 0),
            }
            for k in kws_sorted
        ]
    except Exception as e:
        logger.warning(f"AI campaign — keyword_stats failed: {e}")
        ctx["top_keywords"] = []

    try:
        daily = get_daily_stats(days=days) or []
        # aggregate totals across the window
        agg = {"impressions": 0, "clicks": 0, "cost": 0.0, "conversions": 0.0}
        for d in daily:
            agg["impressions"] += int(d.get("impressions") or 0)
            agg["clicks"]      += int(d.get("clicks") or 0)
            agg["cost"]        += float(d.get("cost") or 0.0)
            agg["conversions"] += float(d.get("conversions") or 0.0)
        ctx["window_totals"] = {
            "days":        days,
            "impressions": agg["impressions"],
            "clicks":      agg["clicks"],
            "cost":        round(agg["cost"], 2),
            "conversions": round(agg["conversions"], 2),
        }
    except Exception as e:
        logger.warning(f"AI campaign — daily_stats failed: {e}")
        ctx["window_totals"] = {"days": days}

    return ctx


@app.post("/api/admin/ai/campaign-strategy", dependencies=[Depends(_require_admin)])
def admin_ai_campaign_strategy(body: CampaignStrategyRequest):
    """Opus researches the practice + performance data and produces a campaign plan."""
    goal = (body.campaign_goal or "").strip()
    if not goal:
        raise HTTPException(status_code=422, detail="campaign_goal is required")
    if len(goal) > 2000:
        raise HTTPException(status_code=422, detail="campaign_goal too long (max 2000 chars)")

    target_service = (body.target_service or "All-on-4 Implants").strip()
    budget_hint    = (body.budget_hint or "").strip()
    extra          = (body.additional_context or "").strip()

    practice = _build_practice_context()
    perf     = _gather_performance_context(days=30)

    practice_name = practice.get("name") or "this dental practice"
    practice_location = ""
    if practice.get("address"):
        # Extract city/region from address for local angle.
        # "123 Main St, Grafton, MA 01536" → 4 parts → take middle two → "Grafton, MA"
        # "Grafton, MA 01536" → 3 parts → take last two → "MA 01536" (acceptable)
        # "Grafton, MA" → 2 parts → take last two → "Grafton, MA" (ideal)
        parts = [p.strip() for p in practice["address"].split(",")]
        practice_location = ", ".join(parts[-3:-1]) if len(parts) >= 4 else ", ".join(parts[-2:])

    system_prompt = (
        f"You are a senior dental marketing strategist for {practice_name}. "
        f"Your job is to RESEARCH the practice's data and produce a tightly-scoped "
        f"campaign plan that a junior copywriter (Haiku) can execute without ambiguity. "
        f"Prefer practical, locally-resonant angles"
        + (f" ({practice_location})" if practice_location else "")
        + " — weekend availability, financing options, and specific clinical strengths — over "
        f"generic claims. Return ONLY a JSON object — no markdown, no commentary."
    )

    user_prompt = (
        f"Campaign goal: {goal}\n"
        f"Target service: {target_service}\n"
        + (f"Budget context: {budget_hint}\n" if budget_hint else "")
        + (f"Additional context: {extra}\n" if extra else "")
        + "\n=== Practice ===\n" + _format_practice_context(practice)
        + "\n\n=== Performance snapshot (last 30 days) ===\n"
        + json.dumps(perf, indent=2, default=str)
        + "\n\nReturn a JSON object with EXACTLY these keys:\n"
        + "  campaign_name (string)\n"
        + "  target_audience (string — concrete description)\n"
        + "  objective (string — 1-2 sentence goal)\n"
        + "  key_messages (array of 3-5 strings)\n"
        + "  ad_headlines (array of 6-10 strings, each <= 30 chars)\n"
        + "  ad_descriptions (array of 3-5 strings, each <= 90 chars)\n"
        + "  sms_sequence_brief (string — what the SMS sequence should accomplish, tone, cadence)\n"
        + "  email_sequence_brief (string — same, for email)\n"
        + "  implementation_instructions (string — explicit instructions for the implementer "
        + "    Haiku/Sonnet model, including voice, must-include details, and what to avoid)\n"
        + "Return ONLY the JSON object."
    )

    try:
        client = _get_anthropic_client()
        message = client.messages.create(
            model=OPUS_MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": user_prompt}],
            system=system_prompt,
        )
        raw_text = message.content[0].text if message.content else ""
        model_used = message.model
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"AI campaign-strategy failed: {e}")
        raise HTTPException(status_code=502, detail=f"AI strategy failed: {e}")

    json_text = _extract_json_from_ai_response(raw_text)
    try:
        result = json.loads(json_text)
    except json.JSONDecodeError as e:
        logger.error(f"AI campaign-strategy — JSON parse failed: {e}\nRaw: {raw_text[:500]}")
        raise HTTPException(status_code=502, detail="AI returned invalid JSON — please try again")

    required_keys = {
        "campaign_name", "target_audience", "objective", "key_messages",
        "ad_headlines", "ad_descriptions", "sms_sequence_brief",
        "email_sequence_brief", "implementation_instructions",
    }
    missing = required_keys - set(result.keys())
    if missing:
        raise HTTPException(
            status_code=502,
            detail=f"AI strategy missing required fields: {sorted(missing)}",
        )

    return {"strategy": result, "model_used": model_used}


@app.post("/api/admin/ai/campaign-implement", dependencies=[Depends(_require_admin)])
def admin_ai_campaign_implement(body: CampaignImplementRequest):
    """Haiku/Sonnet executes the Opus strategy to produce concrete deliverables."""
    strategy = body.strategy or {}
    if not isinstance(strategy, dict) or not strategy.get("implementation_instructions"):
        raise HTTPException(
            status_code=422,
            detail="strategy must include 'implementation_instructions' from a strategy run",
        )

    deliverable = (body.deliverable or "").strip().lower()
    if deliverable not in ("ad_copy", "sms_sequence", "email_sequence", "full_package"):
        raise HTTPException(
            status_code=422,
            detail="deliverable must be one of: ad_copy, sms_sequence, email_sequence, full_package",
        )

    model_choice = (body.model or "haiku").strip().lower()
    model_id = SONNET_MODEL if model_choice == "sonnet" else HAIKU_MODEL

    practice = _build_practice_context()
    practice_block = _format_practice_context(practice)

    # Format-specific schema instructions
    if deliverable == "ad_copy":
        schema_instructions = (
            "Return JSON with keys: "
            "headlines (array of 10 strings, each <= 30 chars), "
            "descriptions (array of 4 strings, each <= 90 chars), "
            "final_url (string — best booking link from the practice info), "
            "callouts (array of 4 short strings, each <= 25 chars)."
        )
    elif deliverable == "sms_sequence":
        schema_instructions = (
            "Return JSON with key 'steps' (array of objects). Each step has: "
            "sequence_day (integer), body (string ending with '\\nReply STOP to opt out.'). "
            "Keep each SMS body <= 280 chars before token expansion. "
            "Use {first_name} as the only runtime token."
        )
    elif deliverable == "email_sequence":
        schema_instructions = (
            "Return JSON with key 'steps' (array of objects). Each step has: "
            "sequence_day (integer), subject (string), body (string). "
            "Body must include {first_name} and end with {unsub_url}. "
            "2-4 short paragraphs each, conversational tone."
        )
    else:  # full_package
        schema_instructions = (
            "Return JSON with keys: "
            "ad_copy (object with headlines [10 strings <=30 chars], descriptions [4 strings <=90 chars], callouts [4 strings <=25 chars]), "
            "sms_sequence (object with 'steps' array — each step: sequence_day, body ending with '\\nReply STOP to opt out.'), "
            "email_sequence (object with 'steps' array — each step: sequence_day, subject, body with {first_name} and {unsub_url})."
        )

    system_prompt = (
        "You are a dental marketing copywriter executing instructions from a senior strategist. "
        "Follow the strategist's implementation_instructions exactly. Voice should be warm, "
        "specific, and locally grounded — use location, doctor name, and specific benefits "
        "from the practice context. Avoid generic phrases like "
        "'best dental care' — use specific benefits and proof points from the strategy. "
        "Return ONLY the JSON object — no markdown, no commentary."
    )

    user_prompt = (
        "=== Practice ===\n" + practice_block
        + "\n\n=== Strategist's plan (from Opus) ===\n"
        + json.dumps(strategy, indent=2, default=str)
        + f"\n\n=== Deliverable requested ===\n{deliverable}\n"
        + f"\n=== Output schema ===\n{schema_instructions}\n"
        + "\nReturn ONLY the JSON object."
    )

    try:
        client = _get_anthropic_client()
        message = client.messages.create(
            model=model_id,
            max_tokens=4096,
            messages=[{"role": "user", "content": user_prompt}],
            system=system_prompt,
        )
        raw_text = message.content[0].text if message.content else ""
        model_used = message.model
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"AI campaign-implement failed (model={model_id}): {e}")
        raise HTTPException(status_code=502, detail=f"AI implementation failed: {e}")

    json_text = _extract_json_from_ai_response(raw_text)
    try:
        content = json.loads(json_text)
    except json.JSONDecodeError as e:
        logger.error(f"AI campaign-implement — JSON parse failed: {e}\nRaw: {raw_text[:500]}")
        raise HTTPException(status_code=502, detail="AI returned invalid JSON — please try again")

    return {
        "deliverable": deliverable,
        "content":     content,
        "model_used":  model_used,
    }


@app.post("/api/admin/ai/performance-analysis", dependencies=[Depends(_require_admin)])
def admin_ai_performance_analysis(body: PerformanceAnalysisRequest):
    """Opus analyzes pipeline + Google Ads data and returns actionable insights."""
    days = max(1, min(int(body.time_range_days or 30), 90))
    focus = (body.focus or "overall").strip().lower()
    if focus not in ("overall", "google_ads", "leads", "conversions"):
        raise HTTPException(
            status_code=422,
            detail="focus must be one of: overall, google_ads, leads, conversions",
        )

    practice = _build_practice_context()
    perf     = _gather_performance_context(days=days)

    focus_hint = {
        "overall":     "the overall marketing pipeline health",
        "google_ads":  "Google Ads spend efficiency, top/bottom keywords, and wasted spend",
        "leads":       "lead volume by source and stage progression",
        "conversions": "conversion rates from lead → scheduled → treated and where leakage occurs",
    }[focus]

    practice_name_for_analysis = practice.get("name") or "this dental practice"
    system_prompt = (
        f"You are a senior dental marketing analyst reviewing data from {practice_name_for_analysis}. "
        "Be specific and quantitative — cite concrete numbers from the data. "
        "Flag wasted spend, conversion leakage, and underused keywords. "
        "Action items must be specific enough that a junior implementer (Haiku) could execute them. "
        "Return ONLY a JSON object."
    )

    user_prompt = (
        f"Analysis focus: {focus_hint}\n"
        f"Time window: last {days} days\n\n"
        + "=== Practice ===\n" + _format_practice_context(practice)
        + "\n\n=== Performance data ===\n"
        + json.dumps(perf, indent=2, default=str)
        + "\n\nReturn a JSON object with EXACTLY these keys:\n"
        + "  summary (string — 2-3 sentences citing concrete numbers)\n"
        + "  wins (array of strings — what's working, with numbers)\n"
        + "  concerns (array of strings — what's underperforming, with numbers)\n"
        + "  action_items (array of objects, each with: priority ['high'|'medium'|'low'], "
        + "    action [string], rationale [string])\n"
        + "  implementation_prompt (string — explicit instructions for Haiku to produce "
        + "    a follow-up implementation plan from these action items)\n"
        + "Return ONLY the JSON object."
    )

    try:
        client = _get_anthropic_client()
        message = client.messages.create(
            model=OPUS_MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": user_prompt}],
            system=system_prompt,
        )
        raw_text = message.content[0].text if message.content else ""
        model_used = message.model
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"AI performance-analysis failed: {e}")
        raise HTTPException(status_code=502, detail=f"AI analysis failed: {e}")

    json_text = _extract_json_from_ai_response(raw_text)
    try:
        result = json.loads(json_text)
    except json.JSONDecodeError as e:
        logger.error(f"AI performance-analysis — JSON parse failed: {e}\nRaw: {raw_text[:500]}")
        raise HTTPException(status_code=502, detail="AI returned invalid JSON — please try again")

    required_keys = {"summary", "wins", "concerns", "action_items", "implementation_prompt"}
    missing = required_keys - set(result.keys())
    if missing:
        raise HTTPException(
            status_code=502,
            detail=f"AI analysis missing required fields: {sorted(missing)}",
        )

    # Normalize action_items priority casing
    for item in result.get("action_items") or []:
        if isinstance(item, dict) and "priority" in item:
            item["priority"] = str(item["priority"]).lower()

    return {"analysis": result, "model_used": model_used, "time_range_days": days, "focus": focus}


# ─── OD Connection Settings ──────────────────────────────────────────────────

class ODSettingsRequest(BaseModel):
    od_db_host:       str = ""
    od_db_port:       int = 3306
    od_db_user:       str = ""
    od_db_password:   str = ""   # empty string = don't change existing saved password
    od_db_name:       str = "opendental"
    od_api_base:      str = ""
    od_developer_key: str = ""
    od_customer_key:  str = ""


@app.get("/api/admin/od-settings", dependencies=[Depends(_require_admin)])
def admin_get_od_settings():
    s = get_od_settings()
    return {
        "od_db_host":       s["od_db_host"],
        "od_db_port":       s["od_db_port"],
        "od_db_user":       s["od_db_user"],
        "od_db_password":   "••••••••" if s["od_db_password"] else "",
        "od_db_name":       s["od_db_name"],
        "od_api_base":      s["od_api_base"],
        "od_developer_key": s["od_developer_key"],
        "od_customer_key":  "••••••••" if s["od_customer_key"] else "",
    }


@app.get("/api/admin/ai-settings", dependencies=[Depends(_require_admin)])
def admin_get_ai_settings():
    key = get_setting("anthropic_api_key") or ""
    return {"anthropic_api_key": "••••••••" if key else ""}

@app.post("/api/admin/ai-settings", dependencies=[Depends(_require_admin)])
def admin_save_ai_settings(body: dict):
    key = (body.get("anthropic_api_key") or "").strip()
    if key and not key.startswith("•"):
        save_setting("anthropic_api_key", key)
    return {"ok": True}


# ─── Practice Information Settings ──────────────────────────────────────────

@app.get("/api/admin/practice-settings", dependencies=[Depends(_require_admin)])
def admin_get_practice_settings():
    return {f: get_setting(f"practice_{f}") or "" for f in _PRACTICE_FIELDS}

@app.post("/api/admin/practice-settings", dependencies=[Depends(_require_admin)])
def admin_save_practice_settings(body: PracticeSettingsRequest):
    for f in _PRACTICE_FIELDS:
        save_setting(f"practice_{f}", getattr(body, f).strip())
    return {"ok": True}


# ─── AI Generate Single Message ──────────────────────────────────────────────

@app.post("/api/admin/workflow/ai-generate-message", dependencies=[Depends(_require_admin)])
def admin_ai_generate_message(body: AIGenerateMessageRequest):
    """Generate a single email or SMS message with AI, pre-filled with practice context."""
    # Validate channel
    channel = (body.channel or "").strip().lower()
    if channel not in ("email", "sms"):
        raise HTTPException(status_code=422, detail="channel must be 'email' or 'sms'")

    # Validate appointment_type
    appt_type = (body.appointment_type or "general").strip().lower()
    if appt_type not in _APPT_TYPE_FIELD_MAP:
        raise HTTPException(status_code=422,
            detail=f"appointment_type must be one of: {list(_APPT_TYPE_FIELD_MAP.keys())}")

    # Validate prompt
    prompt_text = (body.prompt or "").strip()
    if not prompt_text:
        raise HTTPException(status_code=422, detail="prompt is required")
    if len(prompt_text) > 2000:
        raise HTTPException(status_code=422, detail="prompt too long (max 2000 chars)")

    # Load practice info from DB
    practice = {f: get_setting(f"practice_{f}") or "" for f in _PRACTICE_FIELDS}
    practice_name = practice.get("name") or "Grafton Dental Care"
    doctor_name   = practice.get("doctor_name") or "Dr. Gupta"
    practice_phone = practice.get("phone") or ""
    practice_address = practice.get("address") or ""
    practice_hours = practice.get("hours") or ""
    # Resolve booking link — fall back to general
    booking_link = (practice.get(_APPT_TYPE_FIELD_MAP[appt_type])
                    or practice.get("booking_link_general") or "")

    # Build prompts — bake practice values directly (no runtime tokens except {first_name} and {unsub_url})
    system_prompt = (
        f"You are a dental marketing expert writing patient communication for {practice_name}. "
        + (f"Practice phone: {practice_phone}. " if practice_phone else "")
        + (f"Location: {practice_address}. " if practice_address else "")
        + (f"Hours: {practice_hours}. " if practice_hours else "")
        + (f"Doctor: {doctor_name}. " if doctor_name else "")
        + (f"Booking link for this appointment type: {booking_link}. " if booking_link else "")
        + "Write warm, professional, conversational dental patient messages. "
        + "Use {{first_name}} as the ONLY runtime placeholder — this will be replaced with the patient's first name at send time. "
        + ("Use {{unsub_url}} as the ONLY other runtime placeholder — include it near the end of email bodies as an unsubscribe link. " if channel == "email" else "Do NOT include {{unsub_url}} in SMS messages. ")
        + "All other content (practice name, phone, URLs, doctor name) should be written as plain text, NOT as placeholders. "
        + "Return a JSON object with "
        + ("keys 'subject' (string) and 'body' (string)." if channel == "email"
           else "key 'body' (string) only. SMS body must end with '\\nReply STOP to opt out.'")
        + " Return ONLY the JSON object, no explanation."
    )

    user_prompt = (
        f"Channel: {channel}\n"
        f"Appointment type: {appt_type}\n"
        f"Goal: {prompt_text}\n\n"
        + ("Keep the SMS concise (ideally under 140 characters of content before token expansion). " if channel == "sms"
           else "Write 2-4 short paragraphs. ")
        + "Use the patient's first name at the start. "
        + (f"Include the booking link naturally: {booking_link}" if booking_link else "")
    )

    try:
        import anthropic
        api_key = get_setting("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise HTTPException(status_code=400,
                detail="Anthropic API key not configured. Add it in Admin → AI Settings.")
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1024,
            messages=[{"role": "user", "content": user_prompt}],
            system=system_prompt,
        )
        raw_text = message.content[0].text if message.content else ""
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"AI generate message failed: {e}")
        raise HTTPException(status_code=502, detail=f"AI generation failed: {e}")

    json_text = _extract_json_from_ai_response(raw_text)
    try:
        result = json.loads(json_text)
    except json.JSONDecodeError as e:
        logger.error(f"AI generate message — JSON parse failed: {e}\nRaw: {raw_text[:300]}")
        raise HTTPException(status_code=502, detail="AI returned invalid JSON — please try again")

    body_text = (result.get("body") or "").strip()
    if not body_text:
        raise HTTPException(status_code=502, detail="AI returned empty body — please try again")

    # Warn in logs if SMS is unusually long
    if channel == "sms" and len(body_text) > 320:
        logger.warning(f"AI generated SMS body is {len(body_text)} chars — may fragment on delivery")

    return {
        "channel":  channel,
        "subject":  result.get("subject", "") if channel == "email" else "",
        "body":     body_text,
    }


@app.post("/api/admin/od-settings", dependencies=[Depends(_require_admin)])
def admin_save_od_settings(body: ODSettingsRequest):
    save_setting("od_db_host",       body.od_db_host.strip())
    save_setting("od_db_port",       str(body.od_db_port))
    save_setting("od_db_user",       body.od_db_user.strip())
    save_setting("od_db_name",       body.od_db_name.strip())
    save_setting("od_api_base",      body.od_api_base.strip())
    save_setting("od_developer_key", body.od_developer_key.strip())
    # Only overwrite password/customer key if a real value was sent
    if body.od_db_password and not body.od_db_password.startswith("•"):
        save_setting("od_db_password", body.od_db_password)
    if body.od_customer_key and not body.od_customer_key.startswith("•"):
        save_setting("od_customer_key", body.od_customer_key)
    return {"ok": True}


@app.post("/api/admin/od-test", dependencies=[Depends(_require_admin)])
def admin_test_od_connection():
    s = get_od_settings()
    if not s["od_db_host"]:
        raise HTTPException(status_code=400, detail="No host configured")
    try:
        import pymysql
        conn = pymysql.connect(
            host=s["od_db_host"],
            port=s["od_db_port"],
            user=s["od_db_user"],
            password=s["od_db_password"],
            database=s["od_db_name"],
            connect_timeout=5,
            charset="utf8mb4",
        )
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION()")
            version = cur.fetchone()[0]
        conn.close()
        return {"ok": True, "message": f"Connected ✓  MySQL {version}"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    settings = get_settings()
    uvicorn.run("main:app", host=settings.host, port=settings.port, reload=False)
