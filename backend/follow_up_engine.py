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
import html as _html_module
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from database import (
    get_due_follow_ups, mark_follow_up_sent, update_stage,
    add_event, get_lead, unsubscribe,
    already_sent, record_send,
    get_workflow_step,
)
from email_service import (
    send_day1_email, send_day7_email, send_day14_email,
    send_day30_cold_email, _send,
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
    return f"http://localhost:{settings.port}/unsubscribe/{lead_id}/{channel}"


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
    lead_id = item["lead_id"]
    channel = step["channel"]
    body_template = step.get("body") or ""
    subject_template = step.get("subject") or ""

    rendered_body = _render_template(body_template, item, unsub_url)
    rendered_subject = _render_template(subject_template, item, unsub_url)

    if channel == "sms":
        return _send_sms(item.get("phone", ""), rendered_body)
    elif channel == "email":
        escaped = _html_module.escape(rendered_body).replace("\n", "<br>")
        html_body = (
            f"<html><body style='font-family:Arial,sans-serif;color:#333;max-width:600px;margin:0 auto'>"
            f"{escaped}"
            f"</body></html>"
        )
        return _send(item.get("email", ""), rendered_subject, html_body, plain=rendered_body)
    else:
        logger.warning(f"Unknown channel '{channel}' for step {step.get('id')}")
        return False


def _process_queue():
    """Called every 15 minutes by APScheduler."""
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

        # Skip if unsubscribed
        if channel == "email" and item.get("unsubscribed_email"):
            mark_follow_up_sent(queue_id, "skipped", "unsubscribed")
            continue
        if channel == "sms" and item.get("unsubscribed_sms"):
            mark_follow_up_sent(queue_id, "skipped", "unsubscribed")
            continue

        # Skip if lead already booked or beyond nurturing
        stage = item.get("stage", "new")
        if stage in ("scheduled", "no_show", "showed", "treatment_presented",
                     "treatment_accepted", "treatment_completed", "cold"):
            mark_follow_up_sent(queue_id, "skipped", f"stage={stage}")
            continue

        # Skip if no contact info
        if channel == "email" and not item.get("email"):
            mark_follow_up_sent(queue_id, "skipped", "no_email")
            continue
        if channel == "sms" and not item.get("phone"):
            mark_follow_up_sent(queue_id, "skipped", "no_phone")
            continue

        # Dedupe guard — restart-safe
        if already_sent(lead_id, template):
            mark_follow_up_sent(queue_id, "sent", "already_sent_dedupe")
            logger.info(f"Skipped {template} for lead {lead_id} (already in communication_log)")
            continue

        try:
            success = False
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

                if success and template.endswith("day1_email") or (step.get("sequence_day") == 1 and step.get("channel") == "email"):
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
    """Delete smile image from GCS when lead goes cold."""
    url = lead.get("smile_image_url", "")
    if not url or "storage.googleapis.com" not in url:
        return
    try:
        from google.cloud import storage
        path = url.split("storage.googleapis.com/")[1].split("?")[0]
        bucket_name, blob_name = path.split("/", 1)
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        blob.delete()
        logger.info(f"Deleted GCS smile image for lead {lead.get('lead_id')}")
    except Exception as e:
        logger.warning(f"Could not delete GCS smile image: {e}")


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
