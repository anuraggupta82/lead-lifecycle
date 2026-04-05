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

    # 11 PM — Upload offline conversions
    def _conversion_upload_job():
        _stamp("conversion_upload")
        try:
            from google_ads_conversions import upload_offline_conversions
            result = upload_offline_conversions()
            logger.info(f"Scheduled conversion upload: {result}")
        except Exception as e:
            logger.error(f"Scheduled conversion upload failed: {e}")

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
        enqueue_follow_ups(payload.lead_id, lead_data["created_at"])
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

    image_url = lead.get("smile_image_url", "")
    deleted_from_gcs = False

    # Delete from GCS if URL exists
    if image_url and "storage.googleapis.com" in image_url:
        try:
            from google.cloud import storage as gcs_storage
            path = image_url.split("storage.googleapis.com/")[1].split("?")[0]
            bucket_name, blob_name = path.split("/", 1)
            client = gcs_storage.Client()
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(blob_name)
            blob.delete()
            deleted_from_gcs = True
            logger.info(f"Deleted GCS smile image for lead {lead_id}")
        except Exception as e:
            logger.warning(f"Could not delete GCS image for lead {lead_id}: {e}")
            # Still clear the URL even if GCS delete fails

    # Clear the URL from the database
    from database import _conn
    with _conn() as conn:
        conn.execute(
            "UPDATE leads SET smile_image_url = '', updated_at = ? WHERE id = ?",
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
            "has_smile_image": bool(lead.get("smile_image_url")),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Test email failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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


if __name__ == "__main__":
    import uvicorn
    settings = get_settings()
    uvicorn.run("main:app", host=settings.host, port=settings.port, reload=False)
