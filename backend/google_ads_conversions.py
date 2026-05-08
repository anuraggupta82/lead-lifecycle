"""
Google Ads Offline Conversion Uploader.
Uploads lead stage milestones as conversions back to Google Ads.

This teaches Google's bidding algorithm which keywords produce actual patients
and revenue, not just clicks. Highest-ROI component of the tracking system.

Conversion actions (created in Step 2):
  - "Qualified Lead"       ($200)  — lead submitted contact info
  - "Appointment Booked"   ($500)  — lead booked via scheduler
  - "Treatment Accepted"   ($15k)  — patient accepted treatment plan in OD
  - "Treatment Completed"  (dynamic) — actual production from OpenDental

Run nightly via APScheduler (11 PM) or manually:
  POST /api/admin/upload-conversions
"""

import logging
import sqlite3
import os
from datetime import datetime, timezone

from google.ads.googleads.client import GoogleAdsClient
from config import get_settings

logger = logging.getLogger(__name__)

# Map lead stages to conversion action names
STAGE_TO_CONVERSION = {
    "new":                  "Qualified Lead",
    "auto_nurture":         "Qualified Lead",
    "scheduled":            "Appointment Booked",
    "no_show":              "Appointment Booked",
    "showed":               "Appointment Booked",
    "treatment_presented":  "Treatment Accepted",
    "treatment_accepted":   "Treatment Accepted",
    "treatment_completed":  "Treatment Completed",
}

# Default values for each conversion action (used when no production data)
DEFAULT_VALUES = {
    "Qualified Lead":     200.0,
    "Appointment Booked": 500.0,
    "Treatment Accepted": 15000.0,
    "Treatment Completed": 25000.0,
}


def _build_client():
    settings = get_settings()
    return GoogleAdsClient.load_from_dict({
        "developer_token": settings.google_ads_developer_token,
        "client_id": settings.google_ads_client_id,
        "client_secret": settings.google_ads_client_secret,
        "refresh_token": settings.google_ads_refresh_token,
        "login_customer_id": settings.google_ads_login_customer_id,
        "use_proto_plus": True,
    })


def _get_db():
    """Direct SQLite connection for conversion_uploads table."""
    settings = get_settings()
    os.makedirs(os.path.dirname(settings.db_path), exist_ok=True)
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _get_conversion_action_id(client, customer_id: str, action_name: str) -> str:
    """Look up the conversion action resource name by name."""
    service = client.get_service("GoogleAdsService")
    query = f"""
        SELECT conversion_action.resource_name, conversion_action.id
        FROM conversion_action
        WHERE conversion_action.name = '{action_name}'
            AND conversion_action.status = 'ENABLED'
    """
    response = service.search(customer_id=customer_id, query=query)
    for row in response:
        return row.conversion_action.resource_name
    return None


def _already_uploaded(db, lead_id: str, conversion_action: str) -> bool:
    """Check if this conversion was already uploaded."""
    row = db.execute(
        "SELECT id FROM conversion_uploads WHERE lead_id=? AND conversion_action=? AND status='uploaded'",
        (lead_id, conversion_action)
    ).fetchone()
    return row is not None


def upload_offline_conversions() -> dict:
    """
    Scan leads with a gclid that reached a trackable stage.
    Upload conversions that haven't been sent yet.
    """
    settings = get_settings()

    try:
        client = _build_client()
    except Exception as e:
        logger.error(f"Failed to create Google Ads client: {e}")
        return {"uploaded": 0, "skipped": 0, "errors": 1, "error": str(e)}

    customer_id = settings.google_ads_customer_id
    now = datetime.now(timezone.utc).isoformat()

    # Cache conversion action resource names
    action_cache = {}
    for action_name in DEFAULT_VALUES.keys():
        resource_name = _get_conversion_action_id(client, customer_id, action_name)
        if resource_name:
            action_cache[action_name] = resource_name
            logger.info(f"Conversion action '{action_name}' → {resource_name}")
        else:
            logger.warning(f"Conversion action '{action_name}' not found in Google Ads!")

    if not action_cache:
        return {"uploaded": 0, "skipped": 0, "errors": 1, "error": "No conversion actions found"}

    # Get all leads with a gclid
    db = _get_db()
    leads = db.execute("""
        SELECT id, gclid, stage, created_at, attributed_production, email, first_name, od_patient_num
        FROM leads
        WHERE gclid != '' AND gclid IS NOT NULL
        ORDER BY updated_at DESC
    """).fetchall()

    logger.info(f"Found {len(leads)} leads with gclids to check")

    uploaded = 0
    skipped = 0
    errors = 0

    conversion_upload_service = client.get_service("ConversionUploadService")

    for lead in leads:
        lead_id = lead["id"]
        gclid = lead["gclid"]
        stage = lead["stage"]
        created_at = lead["created_at"]

        # Skip existing OD patients — their gclid came from a recall/existing-patient search,
        # not a new patient acquisition. Uploading their conversion would inflate our
        # "new patient from ads" metrics and waste our conversion attribution budget.
        if lead.get("od_patient_num"):
            logger.debug(
                f"Lead {lead_id} skipped: existing OD patient (PatNum={lead['od_patient_num']})"
            )
            skipped += 1
            continue

        # Determine which conversion to upload based on current stage
        conversion_name = STAGE_TO_CONVERSION.get(stage)
        if not conversion_name:
            skipped += 1
            continue

        # Check if already uploaded
        if _already_uploaded(db, lead_id, conversion_name):
            skipped += 1
            continue

        # Get conversion action resource name
        action_resource = action_cache.get(conversion_name)
        if not action_resource:
            skipped += 1
            continue

        # Determine conversion value
        if conversion_name == "Treatment Completed" and lead["attributed_production"]:
            value = float(lead["attributed_production"])
        else:
            value = DEFAULT_VALUES[conversion_name]

        # Build the click conversion
        try:
            click_conversion = client.get_type("ClickConversion")
            click_conversion.gclid = gclid
            click_conversion.conversion_action = action_resource
            click_conversion.conversion_value = value
            click_conversion.currency_code = "USD"

            # Conversion time = when the lead was created (for Qualified Lead)
            # or current time for later stages
            click_conversion.conversion_date_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00:00")

            # Upload
            response = conversion_upload_service.upload_click_conversions(
                customer_id=customer_id,
                conversions=[click_conversion],
                partial_failure=True,
            )

            # Check for partial failure
            if response.partial_failure_error:
                error_msg = response.partial_failure_error.message
                logger.error(f"Partial failure for lead {lead_id}: {error_msg}")

                db.execute("""
                    INSERT INTO conversion_uploads
                        (lead_id, conversion_action, gclid, conversion_time, conversion_value, uploaded_at, status, google_response)
                    VALUES (?,?,?,?,?,?,?,?)
                """, (lead_id, conversion_name, gclid, now, value, now, "failed", error_msg[:500]))
                db.commit()
                errors += 1
            else:
                # Success
                db.execute("""
                    INSERT INTO conversion_uploads
                        (lead_id, conversion_action, gclid, conversion_time, conversion_value, uploaded_at, status, google_response)
                    VALUES (?,?,?,?,?,?,?,?)
                """, (lead_id, conversion_name, gclid, now, value, now, "uploaded", "success"))
                db.commit()
                uploaded += 1

                logger.info(
                    f"Uploaded: lead {lead_id} → '{conversion_name}' (${value:.2f}) "
                    f"[{lead['first_name'] or 'unknown'}]"
                )

        except Exception as e:
            logger.error(f"Error uploading conversion for lead {lead_id}: {e}")
            db.execute("""
                INSERT INTO conversion_uploads
                    (lead_id, conversion_action, gclid, conversion_time, conversion_value, uploaded_at, status, google_response)
                VALUES (?,?,?,?,?,?,?,?)
            """, (lead_id, conversion_name, gclid, now, value, now, "failed", str(e)[:500]))
            db.commit()
            errors += 1

    db.close()

    result = {
        "uploaded": uploaded,
        "skipped": skipped,
        "errors": errors,
        "leads_checked": len(leads),
    }
    logger.info(f"Conversion upload complete: {result}")
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = upload_offline_conversions()
    print(result)
