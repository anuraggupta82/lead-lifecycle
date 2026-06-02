"""
Google Ads Offline Conversion Uploader.
Uploads lead stage milestones as conversions back to Google Ads.

This teaches Google's bidding algorithm which keywords produce actual patients
and revenue, not just clicks. Highest-ROI component of the tracking system.

Conversion actions (created in Step 2):
  - "Qualified Lead"       ($200)  — PRIMARY   — lead submitted contact info
  - "Appointment Booked"   ($500)  — SECONDARY — appointment scheduled in OD (fast signal)
  - "Treatment Accepted"   ($15k)  — SECONDARY — patient accepted treatment plan in OD
  - "Treatment Completed"  (dynamic)— SECONDARY — actual production from OpenDental

Primary vs Secondary (Gemini recommendation, implemented May 30 2026):
  Google's algorithm trains ONLY on Primary conversions. Secondary conversions appear
  in reports but do not influence Smart Bidding. Qualified Lead is Primary because it
  fires immediately (same day as click) giving Google a fast, high-volume signal.
  Appointment Booked / Treatment stages are Secondary (observation only) so Google
  reports ROAS without waiting weeks for downstream events to train the algorithm.

Appointment Booked trigger (updated May 30 2026):
  Previously fired on showed_at (physical arrival — up to 10 days delay).
  Now fires on scheduled_at (moment OD puts patient on calendar — same day or next day).
  This cuts the algorithm's feedback loop from 10 days to <24 hours.
  No-shows: Appointment Booked uploads even for no-shows because it is a Secondary
  conversion (does not train Smart Bidding). Suppressing it adds complexity with
  minimal algorithmic benefit. No-show leads appear in ROAS reports but do not
  influence bid strategy.

Run nightly via APScheduler (11 PM) or manually:
  POST /api/admin/upload-conversions
"""

import hashlib
import logging
import re
import sqlite3
import os
from datetime import datetime, timezone

from google.ads.googleads.client import GoogleAdsClient
from google.protobuf import field_mask_pb2
from config import get_settings

logger = logging.getLogger(__name__)

# Conversion action names — must match exactly what's in Google Ads account
CONV_QUALIFIED_LEAD    = "Qualified Lead"
CONV_APPOINTMENT_BOOKED = "Appointment Booked"
CONV_TREATMENT_ACCEPTED = "Treatment Accepted"
CONV_TREATMENT_COMPLETED = "Treatment Completed"

# Which conversion actions are PRIMARY (train Google's algorithm) vs SECONDARY (observation only).
# Change these via POST /api/admin/set-conversion-categories to sync with Google Ads.
PRIMARY_CONVERSIONS = {CONV_QUALIFIED_LEAD}
SECONDARY_CONVERSIONS = {CONV_APPOINTMENT_BOOKED, CONV_TREATMENT_ACCEPTED, CONV_TREATMENT_COMPLETED}

# Default values for each conversion action (used when no production data)
DEFAULT_VALUES = {
    CONV_QUALIFIED_LEAD:      200.0,
    CONV_APPOINTMENT_BOOKED:  500.0,
    CONV_TREATMENT_ACCEPTED:  15000.0,
    CONV_TREATMENT_COMPLETED: 25000.0,
}


def _resolve_conversions(lead: sqlite3.Row) -> list[tuple[str, str]]:
    """
    Determine ALL conversion actions that apply to this lead and their timestamps.

    Returns: list of (conversion_name, iso_timestamp) pairs — may be empty or multiple.

    Rules (ordered — a lead progresses through these stages over time):
      1. "Qualified Lead"      → stage in (new, auto_nurture)
                                 timestamp = created_at
      2. "Appointment Booked"  → scheduled_at IS NOT NULL (appointment created in OD)
                                 timestamp = scheduled_at  ← FAST SIGNAL (<24h after click)
                                 Guard: skip if appointment_status = 'broken' AND showed_at empty
                                 (cancelled before showing — still upload; no-show noise is
                                 less harmful than 10-day delay)
      3. "Treatment Accepted"  → stage in (treatment_presented, treatment_accepted)
                                 timestamp = tx_accepted_at
      4. "Treatment Completed" → stage = treatment_completed
                                 timestamp = tx_completed_at

    The caller checks _already_uploaded() per (lead_id, conversion_name) before uploading,
    so re-running nightly never double-uploads.
    """
    stage = lead["stage"]
    results: list[tuple[str, str]] = []

    # ── 1. Qualified Lead ─────────────────────────────────────────────────────
    if stage in ("new", "auto_nurture"):
        ts = lead["created_at"] or ""
        if ts:
            results.append((CONV_QUALIFIED_LEAD, ts))
        return results  # these stages don't have downstream events yet

    # ── 2. Appointment Booked — fires at scheduling, not arrival ─────────────
    # Preferred timestamp: scheduled_at (moment OD puts patient on calendar).
    # Fallback chain: showed_at → appointment_date → None.
    # Fallback is needed for leads that leapfrog the "scheduled" stage — e.g.
    # od_matcher sees a patient who already showed up on first OD sync and jumps
    # straight from new → showed, never writing scheduled_at.
    # No-show note: if appointment_status='broken' and showed_at is empty,
    # the lead is still at stage='no_show'. scheduled_at IS set (appointment was
    # created). We intentionally upload Appointment Booked even for no-shows
    # because (a) it is a Secondary conversion that does NOT train Smart Bidding,
    # and (b) suppressing it would require checking appointment_status, which adds
    # complexity with minimal algorithmic benefit.
    booked_ts = (
        lead["scheduled_at"]
        or lead["showed_at"]
        or lead["appointment_date"]
        or ""
    )
    if booked_ts:
        results.append((CONV_APPOINTMENT_BOOKED, booked_ts))

    # ── 3. Treatment Accepted ─────────────────────────────────────────────────
    if stage in ("treatment_presented", "treatment_accepted", "treatment_completed"):
        tx_accepted_at = lead["tx_accepted_at"] or ""
        if tx_accepted_at:
            results.append((CONV_TREATMENT_ACCEPTED, tx_accepted_at))

    # ── 4. Treatment Completed ────────────────────────────────────────────────
    if stage == "treatment_completed":
        tx_completed_at = lead["tx_completed_at"] or ""
        if tx_completed_at:
            results.append((CONV_TREATMENT_COMPLETED, tx_completed_at))

    return results


# ── Legacy single-return shim (used by tests / external callers) ──────────────
def _resolve_conversion(lead: sqlite3.Row) -> tuple[str | None, str | None]:
    """
    Compatibility shim — returns the HIGHEST-PRIORITY single conversion for this lead.
    Use _resolve_conversions() for full multi-stage upload logic.
    """
    pairs = _resolve_conversions(lead)
    if not pairs:
        return None, None
    return pairs[-1]  # last = highest stage reached

# (DEFAULT_VALUES defined above with CONV_* constants — do not redefine here)


def set_conversion_categories() -> dict:
    """
    PR 2: Set Primary vs Secondary category on each conversion action in Google Ads.

    Primary   → include_in_conversions_metric = True  (trains Smart Bidding)
    Secondary → include_in_conversions_metric = False (observation/reporting only)

    GDC policy (Gemini recommendation, May 30 2026):
      PRIMARY:   Qualified Lead         — fires same day as click; high volume; fast signal
      SECONDARY: Appointment Booked     — fires at scheduling (<24h); ROAS reporting
                 Treatment Accepted     — fires weeks later; ROAS reporting
                 Treatment Completed    — fires months later; ROAS reporting

    Run via: POST /api/admin/set-conversion-categories
    """
    settings = get_settings()
    try:
        client = _build_client()
    except Exception as e:
        return {"ok": False, "error": f"Failed to build GAds client: {e}"}

    customer_id = settings.google_ads_customer_id
    service = client.get_service("ConversionActionService")
    ga_service = client.get_service("GoogleAdsService")

    # Fetch all enabled conversion actions with their resource names
    query = """
        SELECT conversion_action.resource_name,
               conversion_action.name,
               conversion_action.include_in_conversions_metric
        FROM conversion_action
        WHERE conversion_action.status = 'ENABLED'
    """
    response = ga_service.search(customer_id=customer_id, query=query)

    results = []
    operations = []

    for row in response:
        ca = row.conversion_action
        name = ca.name
        resource_name = ca.resource_name
        current_include = ca.include_in_conversions_metric

        # Determine desired setting
        if name in PRIMARY_CONVERSIONS:
            desired_include = True
        elif name in SECONDARY_CONVERSIONS:
            desired_include = False
        else:
            # Not one of our managed actions — skip
            results.append({"name": name, "action": "skipped", "reason": "not managed"})
            continue

        if current_include == desired_include:
            results.append({
                "name": name,
                "action": "no_change",
                "include_in_conversions_metric": desired_include,
            })
            continue

        # Build mutate operation
        op = client.get_type("ConversionActionOperation")
        ca_update = op.update
        ca_update.resource_name = resource_name
        ca_update.include_in_conversions_metric = desired_include

        op.update_mask.CopyFrom(
            field_mask_pb2.FieldMask(paths=["include_in_conversions_metric"])
        )

        operations.append(op)
        results.append({
            "name": name,
            "action": "updated",
            "from": current_include,
            "to": desired_include,
            "resource_name": resource_name,
        })

    if operations:
        try:
            mutate_response = service.mutate_conversion_actions(
                customer_id=customer_id,
                operations=operations,
            )
            logger.info(
                f"set_conversion_categories: {len(operations)} actions updated. "
                f"Results: {[r['name'] for r in results if r.get('action') == 'updated']}"
            )
        except Exception as e:
            logger.error(f"set_conversion_categories mutate failed: {e}")
            return {"ok": False, "error": str(e), "results": results}
    else:
        logger.info("set_conversion_categories: no changes needed — all actions already correct")

    return {
        "ok": True,
        "operations_sent": len(operations),
        "results": results,
        "policy": {
            "primary": sorted(PRIMARY_CONVERSIONS),
            "secondary": sorted(SECONDARY_CONVERSIONS),
        },
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


def _normalize_and_hash(value: str, is_phone: bool = False) -> str | None:
    """
    Normalize and SHA-256 hash a PII value for Enhanced Conversions / Customer Match.
    Email: lowercase + strip.
    Phone: normalize to E.164 (+1XXXXXXXXXX for US), then hash.
    Returns None if value is empty/None.
    Never log the input value — only log the hash prefix for debugging.
    """
    if not value:
        return None
    if is_phone:
        digits = re.sub(r"[^\d]", "", value)
        if len(digits) == 10:
            normalized = f"+1{digits}"
        elif len(digits) == 11 and digits.startswith("1"):
            normalized = f"+{digits}"
        else:
            normalized = f"+{digits}"  # best-effort for non-US
    else:
        normalized = value.strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


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

    # Get all leads with a gclid — include all stage-transition timestamps
    db = _get_db()
    leads = db.execute("""
        SELECT id, gclid, stage, created_at, attributed_production, email, first_name,
               phone, od_patient_num, od_relationship, od_matched_at,
               scheduled_at, showed_at, tx_accepted_at, tx_completed_at,
               appointment_date, appointment_status, source
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

        # ── Call duration filter ──────────────────────────────────────────────
        # CallRail is the source of truth for call attribution. Calls < 90s are
        # hang-ups / wrong numbers and should not count as conversions. This
        # mirrors the CallRail integration filter set in the Google Ads integration
        # (>90s threshold). Only applies to callrail-sourced leads.
        CALL_MIN_DURATION_SECONDS = 90
        lead_source = lead["source"] or ""
        if lead_source == "callrail":
            call_row = db.execute(
                "SELECT duration_seconds FROM callrail_calls WHERE lead_id = ? ORDER BY called_at DESC LIMIT 1",
                (lead_id,)
            ).fetchone()
            call_duration = call_row["duration_seconds"] if call_row else 0
            if call_duration < CALL_MIN_DURATION_SECONDS:
                logger.debug(
                    f"Lead {lead_id} skipped: callrail call too short "
                    f"({call_duration}s < {CALL_MIN_DURATION_SECONDS}s minimum)"
                )
                skipped += 1
                continue

        # Resolve ALL conversions that apply to this lead (may be multiple across stages)
        conversion_pairs = _resolve_conversions(lead)
        if not conversion_pairs:
            skipped += 1
            continue

        lead_had_any_upload = False

        for conversion_name, conversion_ts in conversion_pairs:

            # Skip if already uploaded for this (lead, action) pair
            if _already_uploaded(db, lead_id, conversion_name):
                continue

            # Get conversion action resource name
            action_resource = action_cache.get(conversion_name)
            if not action_resource:
                logger.warning(f"No resource name for '{conversion_name}' — skipping for lead {lead_id}")
                continue

            # Determine conversion value
            if conversion_name == CONV_TREATMENT_COMPLETED and lead["attributed_production"]:
                value = float(lead["attributed_production"])
            else:
                value = DEFAULT_VALUES[conversion_name]

            # Build and upload the click conversion
            ts_str = now  # fallback
            try:
                # Normalize to Google Ads format: "YYYY-MM-DD HH:MM:SS+HH:MM"
                ts = datetime.fromisoformat(conversion_ts.replace("Z", "+00:00"))
                ts_str = ts.strftime("%Y-%m-%d %H:%M:%S+00:00")
            except Exception:
                ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00:00")

            try:
                click_conversion = client.get_type("ClickConversion")
                click_conversion.gclid = gclid
                click_conversion.conversion_action = action_resource
                click_conversion.conversion_value = value
                click_conversion.currency_code = "USD"
                click_conversion.conversion_date_time = ts_str

                # Enhanced Conversions: attach hashed first-party identifiers so Google
                # can match conversions even when gclid is absent/expired (~15-30% lift).
                # Requires Customer Data Terms accepted in Google Ads UI (Settings → Conversions).
                for raw, is_phone in ((lead["email"], False), (lead["phone"], True)):
                    h = _normalize_and_hash(raw, is_phone=is_phone)
                    if not h:
                        continue
                    ui = client.get_type("UserIdentifier")
                    if is_phone:
                        ui.hashed_phone_number = h
                    else:
                        ui.hashed_email = h
                    click_conversion.user_identifiers.append(ui)

                response = conversion_upload_service.upload_click_conversions(
                    customer_id=customer_id,
                    conversions=[click_conversion],
                    partial_failure=True,
                )

                if response.partial_failure_error and response.partial_failure_error.code != 0:
                    error_msg = response.partial_failure_error.message
                    logger.error(f"Partial failure for lead {lead_id} / '{conversion_name}': {error_msg}")
                    db.execute("""
                        INSERT INTO conversion_uploads
                            (lead_id, conversion_action, gclid, conversion_time, conversion_value, uploaded_at, status, google_response)
                        VALUES (?,?,?,?,?,?,?,?)
                    """, (lead_id, conversion_name, gclid, ts_str, value, now, "failed", error_msg[:500]))
                    db.commit()
                    errors += 1
                else:
                    db.execute("""
                        INSERT INTO conversion_uploads
                            (lead_id, conversion_action, gclid, conversion_time, conversion_value, uploaded_at, status, google_response)
                        VALUES (?,?,?,?,?,?,?,?)
                    """, (lead_id, conversion_name, gclid, ts_str, value, now, "uploaded", "success"))
                    db.commit()
                    uploaded += 1
                    lead_had_any_upload = True
                    logger.info(
                        f"Uploaded: lead {lead_id} → '{conversion_name}' (${value:.2f}) "
                        f"ts={ts_str} [{lead['first_name'] or 'unknown'}]"
                    )

            except Exception as e:
                logger.error(f"Error uploading '{conversion_name}' for lead {lead_id}: {e}")
                db.execute("""
                    INSERT INTO conversion_uploads
                        (lead_id, conversion_action, gclid, conversion_time, conversion_value, uploaded_at, status, google_response)
                    VALUES (?,?,?,?,?,?,?,?)
                """, (lead_id, conversion_name, gclid, ts_str, value, now, "failed", str(e)[:500]))
                db.commit()
                errors += 1

        if not lead_had_any_upload:
            skipped += 1

    db.close()

    result = {
        "uploaded": uploaded,
        "skipped": skipped,
        "errors": errors,
        "leads_checked": len(leads),
    }
    logger.info(f"Conversion upload complete: {result}")
    return result


def enable_enhanced_conversions() -> dict:
    """
    Enable Enhanced Conversions for Leads at the customer level.

    Sets Customer.conversion_tracking_setting.enhanced_conversions_for_leads_enabled = True.
    This is a one-time account-level toggle — safe to re-run (idempotent).

    IMPORTANT: After running this, accept the Customer Data Terms in the Google Ads UI:
      Settings → Conversions → Enhanced Conversions for Leads → Accept terms
    Without accepting terms, hashed identifiers upload but won't be matched.

    The actual hashed email/phone attachment in upload_offline_conversions() is always
    active — this function only flips the account-level feature flag.
    """
    settings = get_settings()
    try:
        client = _build_client()
    except Exception as e:
        return {"ok": False, "error": f"Failed to build GAds client: {e}"}

    customer_id = settings.google_ads_customer_id
    svc = client.get_service("CustomerService")

    try:
        op = client.get_type("CustomerOperation")
        cust = op.update
        cust.resource_name = f"customers/{customer_id}"
        cust.conversion_tracking_setting.enhanced_conversions_for_leads_enabled = True
        op.update_mask.CopyFrom(
            field_mask_pb2.FieldMask(
                paths=["conversion_tracking_setting.enhanced_conversions_for_leads_enabled"]
            )
        )
        svc.mutate_customer(customer_id=customer_id, operation=op)
        logger.info(f"Enhanced Conversions for Leads enabled on customer {customer_id}")
        return {
            "ok": True,
            "customer_id": customer_id,
            "note": (
                "Feature flag enabled. To complete setup, accept Customer Data Terms in "
                "Google Ads UI: Settings → Conversions → Enhanced Conversions for Leads."
            ),
        }
    except Exception as e:
        logger.error(f"enable_enhanced_conversions failed: {e}")
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = upload_offline_conversions()
    print(result)
