"""
Lead Lifecycle Service — FastAPI
Runs on Mac Mini (http://localhost:7070)

Endpoints:
  POST /api/events                    — receive lifecycle events from any source
  GET  /api/pipeline                  — all leads with stage summary (dashboard)
  GET  /api/lead/{id}                 — full lead + event timeline
  POST /api/unsubscribe/{id}/{channel} — opt-out handler
  GET  /unsubscribe/{id}/{channel}    — one-click unsubscribe (from email links)
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
)
from email_service import send_office_new_lead
from follow_up_engine import start_scheduler, stop_scheduler, run_now
from firestore_sync import sync_from_firestore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    init_db()
    logger.info("Database initialized")

    # Auto-sync Firestore leads on startup (non-blocking — ignore errors)
    try:
        result = sync_from_firestore()
        logger.info(f"Startup Firestore sync: {result}")
    except Exception as e:
        logger.warning(f"Startup Firestore sync failed (non-fatal): {e}")

    # Start follow-up scheduler
    start_scheduler()

    yield

    # Shutdown
    stop_scheduler()


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
            "stage": "engaged",
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
        }
        lead = upsert_lead(lead_data)
        enqueue_follow_ups(payload.lead_id, lead_data["created_at"])
        add_event(payload.lead_id, "lead_created", stage_to="engaged", source=payload.source,
                  detail=detail_str)

        # Notify office
        try:
            send_office_new_lead(lead)
        except Exception as e:
            logger.warning(f"Office notification failed: {e}")

        return {"status": "ok", "lead_id": payload.lead_id, "action": "created"}

    elif event_type == "smile_completed":
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        upsert_lead({"id": lead["id"], "smile_image_url": payload.smile_image_url or "",
                     "smile_generated_at": now})
        update_stage(lead["id"], "smile_completed", source=payload.source)
        add_event(lead["id"], "smile_completed", source=payload.source, detail=detail_str)
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
        return {"status": "ok", "lead_id": lead["id"], "action": "booking_noted"}

    elif event_type == "booking_cancelled":
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        update_stage(lead["id"], "nurturing", source="scheduler")
        add_event(lead["id"], "booking_cancelled", source="scheduler", detail=detail_str)
        return {"status": "ok", "lead_id": lead["id"], "action": "cancellation_noted"}

    elif event_type == "call_matched":
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        add_event(lead["id"], "call_matched", source="mango", detail=detail_str)
        # Advance to showed if they were scheduled
        if lead["stage"] in ("scheduled", "confirmed"):
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
    from od_matcher import match_leads_to_od
    result = match_leads_to_od()
    return {"status": "ok", "result": result}


@app.post("/api/admin/run-queue", dependencies=[Depends(_require_admin)])
def admin_run_queue():
    run_now()
    return {"status": "ok", "message": "Follow-up queue processed"}


@app.put("/api/admin/lead/{lead_id}/stage", dependencies=[Depends(_require_admin)])
def admin_update_stage(lead_id: str, request: dict):
    new_stage = request.get("stage")
    if not new_stage:
        raise HTTPException(status_code=400, detail="stage required")
    lead = update_stage(lead_id, new_stage, source="admin")
    return {"status": "ok", "lead": lead}


@app.put("/api/admin/lead/{lead_id}/stage")
async def admin_update_stage_body(lead_id: str, request: Request,
                                   x_admin_password: Optional[str] = Header(None)):
    settings = get_settings()
    if x_admin_password != settings.admin_password:
        raise HTTPException(status_code=401, detail="Unauthorized")
    body = await request.json()
    new_stage = body.get("stage")
    if not new_stage:
        raise HTTPException(status_code=400, detail="stage required")
    lead = update_stage(lead_id, new_stage, source="admin")
    return {"status": "ok", "lead": lead}


if __name__ == "__main__":
    import uvicorn
    settings = get_settings()
    uvicorn.run("main:app", host=settings.host, port=settings.port, reload=False)
