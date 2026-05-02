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
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, Request, Header
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

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
)
from email_service import send_office_new_lead
from follow_up_engine import start_scheduler, stop_scheduler, run_now
from ga4_events import (
    track_lead_created, track_smile_completed, track_appointment_booked,
)
from firestore_sync import sync_from_firestore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

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
            result = optimize_campaign(dry_run=True)  # Start in dry-run mode
            logger.info(f"Scheduled optimizer: {result.get('summary', {})}")
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

    from apscheduler.triggers.interval import IntervalTrigger
    ads_scheduler.add_job(_imap_poll_job, IntervalTrigger(minutes=5),
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
        result = optimize_campaign(dry_run=dry_run)
        return {"status": "ok", "result": result}
    except ImportError as e:
        raise HTTPException(status_code=503, detail=f"AI optimizer dependencies not installed: {e}")
    except Exception as e:
        logger.error(f"AI optimizer failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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

    # Tombstone first — even if Firestore delete or GCS delete fails below,
    # the next Firestore sync will refuse to re-import this lead.
    try:
        add_deleted_lead_tombstone(
            lead_id,
            email=lead.get("email", ""),
            deleted_by="admin",
            reason="admin_delete_lead",
        )
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

    logger.info(f"Lead {lead_id} ({lead.get('email','')}) permanently deleted by admin (firestore={'yes' if firestore_deleted else 'FAILED'})")
    return {"status": "deleted", "lead_id": lead_id, "firestore_deleted": firestore_deleted}


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
    msg_id = save_outbound_message(lead_id, "sms", "", msg, sent_by="admin")
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
    msg_id = save_outbound_message(lead_id, "email", subject, email_body, sent_by="admin")
    return {"ok": True, "message_id": msg_id}


# ─── Step 9: Workflow CRUD + AI Generate ─────────────────────────────────────

class WorkflowCreate(BaseModel):
    name: str
    campaign_tag: str = ""
    description: str = ""


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
    import re
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
        client = anthropic.Anthropic()
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
    json_text = raw_text.strip()
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", json_text, re.DOTALL)
    if match:
        json_text = match.group(1)
    # If still looks like it has outer text, try to find the first { ... }
    if not json_text.startswith("{"):
        brace_match = re.search(r"\{.*\}", json_text, re.DOTALL)
        if brace_match:
            json_text = brace_match.group(0)

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


if __name__ == "__main__":
    import uvicorn
    settings = get_settings()
    uvicorn.run("main:app", host=settings.host, port=settings.port, reload=False)
