"""
Follow-up engine — APScheduler runs every 15 minutes,
processes due items from follow_up_queue.
"""
import logging
import json
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from database import (
    get_due_follow_ups, mark_follow_up_sent, update_stage,
    add_event, get_lead, unsubscribe
)
from email_service import (
    send_day1_email, send_day7_email, send_day14_email,
    send_day30_cold_email
)
from sms_service import send_day3_sms, send_day21_sms
from config import get_settings

logger = logging.getLogger(__name__)
_scheduler: BackgroundScheduler = None


def _unsubscribe_url(lead_id: str, channel: str) -> str:
    settings = get_settings()
    return f"http://localhost:{settings.port}/unsubscribe/{lead_id}/{channel}"


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

        # Skip if unsubscribed
        if channel == "email" and item.get("unsubscribed_email"):
            mark_follow_up_sent(queue_id, "skipped", "unsubscribed")
            continue
        if channel == "sms" and item.get("unsubscribed_sms"):
            mark_follow_up_sent(queue_id, "skipped", "unsubscribed")
            continue

        # Skip if lead already booked or beyond nurturing
        stage = item.get("stage", "new")
        if stage in ("scheduled", "confirmed", "showed", "treatment_presented",
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

        try:
            success = False
            unsub_url = _unsubscribe_url(lead_id, channel)

            if template == "day1_email":
                success = send_day1_email(item, unsub_url)
                if success:
                    update_stage(lead_id, "nurturing", source="follow_up_engine")

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
                # Mark cold regardless of email success
                update_stage(lead_id, "cold", source="follow_up_engine", detail="30-day nurture complete")
                _delete_smile_image(item)

            status = "sent" if success else "failed"
            mark_follow_up_sent(queue_id, status)

            if success:
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
        # Extract bucket and blob from signed URL
        # URL format: https://storage.googleapis.com/BUCKET/BLOB?X-Goog-...
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
        misfire_grace_time=60,
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
