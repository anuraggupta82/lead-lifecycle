"""
Follow-up engine — APScheduler runs every 15 minutes,
processes due items from follow_up_queue.

Step 9: dynamic dispatch via workflow_steps table.
LEGACY_TEMPLATES: in-flight queue rows created before Step 9 still use
old hardcoded template names — those are handled by the legacy block.
New rows have workflow_step_id set and use dynamic body from DB.
"""
import logging
import json
import re
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from database import (
    get_due_follow_ups, mark_follow_up_sent, update_stage,
    add_event, get_lead, unsubscribe,
    already_sent, record_send,
    get_workflow_step,
)
from datetime import timezone as _tz
from email_service import (
    send_day1_email, send_day7_email, send_day14_email,
    send_day30_cold_email, _send, send_workflow_step_email,
)
from sms_service import send_day3_sms, send_day21_sms, _send_sms
from config import get_settings

logger = logging.getLogger(__name__)
_scheduler: BackgroundScheduler = None

# Templates that existed before Step 9 — dispatched via legacy hardcoded functions
LEGACY_TEMPLATES = {
    "day1_email", "day3_sms", "day7_email",
    "day14_email", "day21_sms", "day30_cold",
}


def _unsubscribe_url(lead_id: str, channel: str) -> str:
    settings = get_settings()
    base = settings.base_url.rstrip("/")
    return f"{base}/unsubscribe/{lead_id}/{channel}"


def _render_template(template: str, lead: dict, unsub_url: str) -> str:
    """Simple {key} substitution for workflow step body text."""
    replacements = {
        "first_name": lead.get("first_name") or "there",
        "last_name": lead.get("last_name") or "",
        "email": lead.get("email") or "",
        "phone": lead.get("phone") or "",
        "unsub_url": unsub_url,
    }
    result = template
    for key, val in replacements.items():
        result = result.replace(f"{{{key}}}", val)
    return result


def _dispatch_dynamic(item: dict, step: dict, unsub_url: str) -> bool:
    """Send a message described by a workflow_steps row."""
    channel = step["channel"]
    attachment = (step.get("image_attachment") or "none").strip()

    if channel == "sms":
        body_template = step.get("body") or ""
        rendered_body = _render_template(body_template, item, unsub_url)
        return _send_sms(item.get("phone", ""), rendered_body)
    elif channel == "email":
        # Route through send_workflow_step_email for all email steps — it handles
        # both plain (attachment="none") and image-embedded cases in one place.
        return send_workflow_step_email(item, step, unsub_url)
    else:
        logger.warning(f"Unknown channel '{channel}' for step {step.get('id')}")
        return False


# Send window: 9 AM – 6 PM America/New_York
_SEND_WINDOW_START = 9   # 9:00 AM ET
_SEND_WINDOW_END   = 18  # 6:00 PM ET (exclusive)


def _in_send_window() -> bool:
    """Return True if current Eastern time is within the allowed send window."""
    try:
        import zoneinfo
        et = zoneinfo.ZoneInfo("America/New_York")
    except ImportError:
        import pytz
        et = pytz.timezone("America/New_York")
    from datetime import datetime
    now_et = datetime.now(et)
    return _SEND_WINDOW_START <= now_et.hour < _SEND_WINDOW_END


def _process_queue():
    """Called every 15 minutes by APScheduler."""
    if not _in_send_window():
        logger.debug("Follow-up engine: outside send window (9 AM–6 PM ET), skipping")
        return

    due = get_due_follow_ups()
    if not due:
        return

    logger.info(f"Follow-up engine: {len(due)} items due")

    for item in due:
        lead_id = item["lead_id"]
        queue_id = item["id"]
        template = item["template"]
        channel = item["channel"]
        workflow_step_id = item.get("workflow_step_id")

        # ── Pre-dispatch status re-read ──────────────────────────────────────
        # Re-read the queue row status — it may have been cancelled by the stop
        # engine between the time get_due_follow_ups() ran and now.
        fresh_lead = get_lead(lead_id)
        if not fresh_lead:
            mark_follow_up_sent(queue_id, "skipped", "lead_not_found")
            continue

        # Skip if queue row was cancelled while we were iterating
        from database import _conn as _db_conn
        with _db_conn() as _check_conn:
            _qrow = _check_conn.execute(
                "SELECT status FROM follow_up_queue WHERE id=?", (queue_id,)
            ).fetchone()
        if _qrow and _qrow["status"] == "cancelled":
            logger.info(f"Queue item {queue_id} was cancelled mid-batch; skipping")
            continue

        # Check paused_until — skip if pause is still in effect
        paused_until = fresh_lead.get("paused_until") or ""
        if paused_until:
            try:
                from datetime import datetime
                import dateutil.parser
                pu = dateutil.parser.parse(paused_until)
                if pu.tzinfo is None:
                    pu = pu.replace(tzinfo=_tz.utc)
                if datetime.now(_tz.utc) < pu:
                    mark_follow_up_sent(queue_id, "skipped", f"lead_paused_until={paused_until}")
                    continue
            except Exception:
                pass  # malformed paused_until — don't block

        # Check indefinite pause (paused_at set but paused_until empty)
        if fresh_lead.get("paused_at") and not paused_until:
            mark_follow_up_sent(queue_id, "skipped", "lead_paused_indefinitely")
            continue

        # Use fresh lead data for the rest of the checks
        channel = item["channel"]

        # Skip if unsubscribed (re-check from fresh DB read)
        if channel == "email" and fresh_lead.get("unsubscribed_email"):
            mark_follow_up_sent(queue_id, "skipped", "unsubscribed")
            continue
        if channel == "sms" and fresh_lead.get("unsubscribed_sms"):
            mark_follow_up_sent(queue_id, "skipped", "unsubscribed")
            continue

        # Skip if lead already booked or beyond nurturing
        stage = fresh_lead.get("stage", "new")
        if stage in ("scheduled", "no_show", "showed", "treatment_presented",
                     "treatment_accepted", "treatment_completed", "cold"):
            mark_follow_up_sent(queue_id, "skipped", f"stage={stage}")
            continue

        # Skip if no contact info
        if channel == "email" and not fresh_lead.get("email"):
            mark_follow_up_sent(queue_id, "skipped", "no_email")
            continue
        if channel == "sms" and not fresh_lead.get("phone"):
            mark_follow_up_sent(queue_id, "skipped", "no_phone")
            continue

        # Dedupe guard — restart-safe
        if already_sent(lead_id, template):
            mark_follow_up_sent(queue_id, "sent", "already_sent_dedupe")
            logger.info(f"Skipped {template} for lead {lead_id} (already in communication_log)")
            continue

        try:
            success = False
            # Merge fresh lead data into item for downstream dispatch functions
            item = {**item, **{k: v for k, v in fresh_lead.items() if v not in (None, "")}}
            unsub_url = _unsubscribe_url(lead_id, channel)
            is_terminal = False

            if template in LEGACY_TEMPLATES:
                # ── Legacy dispatch (pre-Step-9 queue rows) ──────────────────
                if template == "day1_email":
                    success = send_day1_email(item, unsub_url)
                    if success:
                        update_stage(lead_id, "auto_nurture", source="follow_up_engine")

                elif template == "day3_sms":
                    success = send_day3_sms(item)

                elif template == "day7_email":
                    success = send_day7_email(item, unsub_url)

                elif template == "day14_email":
                    success = send_day14_email(item, unsub_url)

                elif template == "day21_sms":
                    success = send_day21_sms(item)

                elif template == "day30_cold":
                    success = send_day30_cold_email(item, unsub_url)
                    is_terminal = True  # side-effects below
            else:
                # ── Dynamic dispatch (Step-9 workflow rows) ───────────────────
                step = None
                if workflow_step_id:
                    step = get_workflow_step(workflow_step_id)
                if not step:
                    # Fallback: look up by template_name
                    from database import get_workflow_step_by_template
                    step = get_workflow_step_by_template(template)
                if not step:
                    logger.warning(f"No workflow step found for template '{template}' (queue_id={queue_id})")
                    mark_follow_up_sent(queue_id, "skipped", "no_step_found")
                    continue

                success = _dispatch_dynamic(item, step, unsub_url)
                is_terminal = bool(step.get("terminal"))

                if success and (template.endswith("day1_email") or (step.get("sequence_day") == 1 and step.get("channel") == "email")):
                    update_stage(lead_id, "auto_nurture", source="follow_up_engine")

            # ── Terminal side-effects ─────────────────────────────────────────
            if is_terminal:
                update_stage(lead_id, "cold", source="follow_up_engine", detail="30-day nurture complete")
                _delete_smile_image(item)

            status = "sent" if success else "failed"
            mark_follow_up_sent(queue_id, status)

            if success:
                record_send(lead_id, template, channel, queue_id=queue_id)
                add_event(
                    lead_id,
                    f"{channel}_sent",
                    detail=json.dumps({"template": template, "queue_id": queue_id}),
                    source="follow_up_engine"
                )
                logger.info(f"Sent {template} to lead {lead_id}")
            else:
                logger.warning(f"Failed {template} for lead {lead_id}")

        except Exception as e:
            logger.error(f"Error processing queue item {queue_id}: {e}")
            mark_follow_up_sent(queue_id, "failed", str(e))


def _delete_smile_image(lead: dict):
    """Delete both after and composite smile images from GCS when lead goes cold."""
    from config import get_settings
    settings = get_settings()
    lead_id = lead.get("lead_id") or lead.get("id", "")

    blobs_to_delete = []

    # After-only blob (preferred) or parsed from legacy URL
    after_blob = lead.get("smile_blob_name", "")
    if not after_blob:
        url = lead.get("smile_image_url", "")
        if url and "storage.googleapis.com" in url:
            try:
                path = url.split("storage.googleapis.com/")[1].split("?")[0]
                _, after_blob = path.split("/", 1)
            except Exception:
                pass
    if after_blob:
        blobs_to_delete.append(after_blob)

    # Composite blob
    composite_blob = lead.get("smile_composite_blob_name", "")
    if composite_blob:
        blobs_to_delete.append(composite_blob)

    if not blobs_to_delete:
        return

    try:
        from google.cloud import storage
        client = storage.Client()
        bucket = client.bucket(settings.gcs_bucket)
        for bname in blobs_to_delete:
            try:
                bucket.blob(bname).delete()
                logger.info(f"Deleted GCS blob for cold lead {lead_id}: {bname}")
            except Exception as e:
                logger.warning(f"Could not delete GCS blob {bname} for lead {lead_id}: {e}")
    except Exception as e:
        logger.warning(f"GCS client init failed during smile image delete (lead {lead_id}): {e}")


def start_scheduler():
    global _scheduler
    if _scheduler and _scheduler.running:
        return
    _scheduler = BackgroundScheduler(timezone="America/New_York")
    _scheduler.add_job(
        _process_queue,
        trigger=IntervalTrigger(minutes=15),
        id="follow_up_engine",
        name="Follow-up Queue Processor",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
        next_run_time=None,
    )
    _scheduler.start()
    logger.info("Follow-up engine started (runs every 15 min)")


def stop_scheduler():
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Follow-up engine stopped")


def run_now():
    """Manually trigger the queue processor (used by admin API)."""
    _process_queue()
