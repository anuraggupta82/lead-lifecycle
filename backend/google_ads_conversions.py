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

# Map lead stages to conversion action names.
# NOTE: "Appointment Booked" is NOT determined by stage alone — see _resolve_conversion() below.
# Only stages that have passed the appointment confirmation gate qualify for that conversion.
STAGE_TO_CONVERSION = {
    "new":                  "Qualified Lead",
    "auto_nurture":         "Qualified Lead",
    "scheduled":            None,               # not enough — requires OD confirmation (showed_at)
    "no_show":              None,               # patient did not show — no appointment conversion
    "showed":               "Appointment Booked",
    "treatment_presented":  "Treatment Accepted",
    "treatment_accepted":   "Treatment Accepted",
    "treatment_completed":  "Treatment Completed",
}

# Stage → timestamp column in the leads table (used for accurate conversion_date_time)
STAGE_TO_TIMESTAMP_COL = {
    "Qualified Lead":     "created_at",      # lead creation time
    "Appointment Booked": "showed_at",       # when they actually came in (confirmed by OD)
    "Treatment Accepted": "tx_accepted_at",  # when treatment plan was accepted
    "Treatment Completed": "tx_completed_at", # when treatment was completed
}


def _resolve_conversion(lead: sqlite3.Row) -> tuple[str | None, str | None]:
    """
    Determine which conversion action (if any) applies to this lead, and the
    correct timestamp to use for conversion_date_time.

    Returns: (conversion_name, iso_timestamp) or (None, None) if not eligible.

    Rules:
      - "Qualified Lead"     → stage in (new, auto_nurture); timestamp = created_at
      - "Appointment Booked" → showed_at IS NOT NULL (confirmed they came in); timestamp = showed_at
      - "Treatment Accepted" → stage in (treatment_presented, treatment_accepted); timestamp = tx_accepted_at
      - "Treatment Completed"→ stage = treatment_completed; timestamp = tx_completed_at

    "scheduled" and "no_show" intentionally do NOT generate an "Appointment Booked" conversion —
    a scheduled appointment that was never kept does not confirm patient acquisition.
    """
    stage = lead["stage"]
    conversion_name = STAGE_TO_CONVERSION.get(stage)

    # Stages mapped to None are explicitly excluded
    if conversion_name is None:
        return None, None

    # "Appointment Booked" requires showed_at — even if stage is "showed", verify the field exists
    if conversion_name == "Appointment Booked":
        showed_at = lead["showed_at"] or ""
        if not showed_at:
            return None, None
        return conversion_name, showed_at

    # For all other conversions, look up the stage timestamp column
    ts_col = STAGE_TO_TIMESTAMP_COL.get(conversion_name, "created_at")
    ts = lead[ts_col] if ts_col in lead.keys() else ""
    # Fall back to created_at if the specific timestamp is missing (legacy rows)
    if not ts:
        ts = lead["created_at"]
    return conversion_name, ts

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

    # Get all leads with a gclid — include stage-transition timestamps for accurate conversion times
    db = _get_db()
    leads = db.execute("""
        SELECT id, gclid, stage, created_at, attributed_production, email, first_name,
               od_patient_num, od_relationship, od_matched_at,
               showed_at, tx_accepted_at, tx_completed_at,
               appointment_date, appointment_status
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

        # Skip pre-existing OD patients only — their gclid came from a recall/existing-patient
        # search, not a new patient acquisition. Uploading their conversion would inflate our
        # "new patient from ads" metrics and waste conversion attribution budget.
        #
        # PR 1 fix: previously gated on od_patient_num != '' which incorrectly skipped
        # brand-new patients who received a chart number after their first appointment.
        # Now we gate on od_relationship, which reflects the patient's status at first OD match:
        #   active_patient   → had completed appointments before this lead existed → skip*
        #   reactivation     → lapsed patient returning via ad                    → skip*
        #   new_patient      → no prior OD history at time of match               → allow
        #   implant_prospect → matched, has implant TP, no completed apts yet     → allow
        #   cold             → schema default; not yet matched to OD              → allow
        #   '' / NULL        → legacy rows pre-migration                          → allow
        #
        # *Carve-out (Option B): if od_matched_at > created_at, the lead existed before
        # OD matching ran, meaning the patient was newly acquired via this ad click and
        # subsequently got a chart number. Allow their treatment-stage conversions through
        # even if od_relationship has since evolved to active_patient.
        od_rel = lead["od_relationship"] or ""
        if od_rel in ("active_patient", "reactivation"):
            # Carve-out: new acquisition whose OD relationship evolved after the lead was created
            od_matched_at = lead["od_matched_at"] or ""
            lead_created_at = lead["created_at"] or ""
            if od_matched_at and lead_created_at and od_matched_at > lead_created_at:
                # OD match happened after lead creation → patient was new at acquisition time.
                # Allow treatment-stage conversions to upload so Smart Bidding sees revenue.
                logger.debug(
                    f"Lead {lead_id} allowed: od_relationship={od_rel} but matched after "
                    f"lead creation (matched={od_matched_at[:10]}, created={lead_created_at[:10]})"
                )
            else:
                logger.debug(
                    f"Lead {lead_id} skipped: pre-existing OD patient "
                    f"(od_relationship={od_rel}, PatNum={lead['od_patient_num']})"
                )
                skipped += 1
                continue

        # Determine which conversion to upload — requires OD confirmation for appointment conversions
        conversion_name, conversion_ts = _resolve_conversion(lead)
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

            # Conversion time = actual stage-transition timestamp (not upload time).
            # Using the real event time teaches Smart Bidding the correct conversion lag.
            # conversion_ts comes from _resolve_conversion() — showed_at, tx_accepted_at, etc.
            try:
                # Normalize to Google Ads format: "YYYY-MM-DD HH:MM:SS+HH:MM"
                ts = datetime.fromisoformat(conversion_ts.replace("Z", "+00:00"))
                ts_str = ts.strftime("%Y-%m-%d %H:%M:%S+00:00")
            except Exception:
                # Fallback if timestamp is malformed
                ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00:00")
            click_conversion.conversion_date_time = ts_str

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
                """, (lead_id, conversion_name, gclid, ts_str, value, now, "failed", error_msg[:500]))
                db.commit()
                errors += 1
            else:
                # Success
                db.execute("""
                    INSERT INTO conversion_uploads
                        (lead_id, conversion_action, gclid, conversion_time, conversion_value, uploaded_at, status, google_response)
                    VALUES (?,?,?,?,?,?,?,?)
                """, (lead_id, conversion_name, gclid, ts_str, value, now, "uploaded", "success"))
                db.commit()
                uploaded += 1

                logger.info(
                    f"Uploaded: lead {lead_id} → '{conversion_name}' (${value:.2f}) "
                    f"[{lead['first_name'] or 'unknown'}]"
                )

        except Exception as e:
            logger.error(f"Error uploading conversion for lead {lead_id}: {e}")
            _ts = locals().get("ts_str", now)
            db.execute("""
                INSERT INTO conversion_uploads
                    (lead_id, conversion_action, gclid, conversion_time, conversion_value, uploaded_at, status, google_response)
                VALUES (?,?,?,?,?,?,?,?)
            """, (lead_id, conversion_name, gclid, _ts, value, now, "failed", str(e)[:500]))
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
