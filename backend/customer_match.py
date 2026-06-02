"""
Google Ads Customer Match — upload hashed patient lists as GAds user list audiences.

Three lists are maintained and synced weekly (Sunday 11 PM):
  GDC — Existing Patients   : OD-matched patients with completed appointments (exclude from new-patient campaigns)
  GDC — Unconverted Leads   : Leads from ads that never converted (retargeting)
  GDC — High-Value Patients : Leads with attributed production > $5,000 (lookalike seed)

Flow per list:
  1. get_or_create_user_list()  → returns/creates the GAds CRM-based UserList resource name
  2. Build UserData operations with hashed email + phone (no name/address — incomplete
     address data silently drops members)
  3. create_offline_user_data_job() → add_offline_user_data_job_operations() → run_offline_user_data_job()
     The job runs async; member_count updates in GAds UI within a few hours.

Identifiers: email (lowercase + SHA-256) and phone (E.164 → SHA-256).
PHI policy: no raw email/phone in logs — only hash prefix or counts.

Prerequisites (manual, one-time):
  - Accept Customer Match terms in Google Ads UI (account-level)
  - Account must be policy-eligible for Customer Match
  - Lists need ~100 matched members before usable for targeting

Trigger:
  POST /api/admin/sync-customer-match
  Weekly cron: Sunday 23:00
"""

import logging
import sqlite3
import os
from datetime import datetime, timezone

from google.ads.googleads.client import GoogleAdsClient
from config import get_settings
from google_ads_conversions import _build_client, _normalize_and_hash

logger = logging.getLogger(__name__)

# List definitions: (name, description, segment_label)
LISTS = [
    (
        "GDC — Existing Patients",
        "GDC patients who have completed treatment. Use to exclude from new-patient campaigns.",
        "existing_patients",
    ),
    (
        "GDC — Unconverted Leads",
        "GDC ad leads (have gclid) who never booked. Use for retargeting campaigns.",
        "unconverted_leads",
    ),
    (
        "GDC — High-Value Patients",
        "GDC patients with attributed production > $5,000. Use as lookalike seed.",
        "high_value",
    ),
]

# Membership life span in days (540 = max allowed by Google for Customer Match)
MEMBERSHIP_LIFE_SPAN_DAYS = 540


def _get_db():
    settings = get_settings()
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _get_or_create_user_list(client, customer_id: str, list_name: str, description: str) -> str:
    """
    Return resource_name of the named CRM-based UserList, creating it if absent.
    """
    ga_service = client.get_service("GoogleAdsService")
    query = f"""
        SELECT user_list.resource_name
        FROM user_list
        WHERE user_list.name = '{list_name.replace("'", "''")}'
          AND user_list.type = 'CRM_BASED'
    """
    try:
        response = ga_service.search(customer_id=customer_id, query=query)
        for row in response:
            rn = row.user_list.resource_name
            logger.info(f"Customer Match: found existing list '{list_name}' → {rn}")
            return rn
    except Exception as e:
        logger.warning(f"Customer Match: list lookup failed, will create. Error: {e}")

    # Create new list
    ul_service = client.get_service("UserListService")
    op = client.get_type("UserListOperation")
    ul = op.create
    ul.name = list_name
    ul.description = description
    ul.membership_life_span = MEMBERSHIP_LIFE_SPAN_DAYS
    ul.crm_based_user_list.upload_key_type = (
        client.enums.CustomerMatchUploadKeyTypeEnum.CONTACT_INFO
    )
    response = ul_service.mutate_user_lists(
        customer_id=customer_id,
        operations=[op],
    )
    rn = response.results[0].resource_name
    logger.info(f"Customer Match: created new list '{list_name}' → {rn}")
    return rn


def _build_operations(client, leads: list) -> list:
    """
    Build OfflineUserDataJobOperation objects from lead rows.
    Each lead contributes one UserData with up to 2 identifiers (email + phone).
    Leads without either identifier are skipped.
    """
    ops = []
    for lead in leads:
        identifiers = []

        email_hash = _normalize_and_hash(lead["email"], is_phone=False)
        if email_hash:
            ui = client.get_type("UserIdentifier")
            ui.hashed_email = email_hash
            identifiers.append(ui)

        phone_hash = _normalize_and_hash(lead["phone"], is_phone=True)
        if phone_hash:
            ui = client.get_type("UserIdentifier")
            ui.hashed_phone_number = phone_hash
            identifiers.append(ui)

        if not identifiers:
            continue

        op = client.get_type("OfflineUserDataJobOperation")
        ud = op.create
        for ui in identifiers:
            ud.user_identifiers.append(ui)
        ops.append(op)

    return ops


def _run_job(client, customer_id: str, user_list_rn: str, operations: list) -> dict:
    """
    Create, populate, and run an OfflineUserDataJob for Customer Match.
    Job runs async — member_count updates in GAds UI within a few hours.
    """
    svc = client.get_service("OfflineUserDataJobService")

    job = client.get_type("OfflineUserDataJob")
    job.type_ = client.enums.OfflineUserDataJobTypeEnum.CUSTOMER_MATCH_USER_LIST
    job.customer_match_user_list_metadata.user_list = user_list_rn

    create_resp = svc.create_offline_user_data_job(
        customer_id=customer_id,
        job=job,
    )
    job_rn = create_resp.resource_name
    logger.info(f"Customer Match: created job {job_rn} for list {user_list_rn}")

    # Upload in batches of 100,000 (API limit per request)
    batch_size = 100_000
    batches_sent = 0
    for i in range(0, len(operations), batch_size):
        batch = operations[i : i + batch_size]
        svc.add_offline_user_data_job_operations(
            resource_name=job_rn,
            operations=batch,
            enable_partial_failure=True,
        )
        batches_sent += 1

    # Kick off async processing
    svc.run_offline_user_data_job(resource_name=job_rn)
    logger.info(
        f"Customer Match: job {job_rn} running — {len(operations)} operations in {batches_sent} batch(es)"
    )

    return {"job_resource_name": job_rn, "op_count": len(operations)}


def _query_segment(db, segment: str) -> list:
    """Return lead rows for the given segment label."""
    if segment == "existing_patients":
        return db.execute("""
            SELECT id, email, phone FROM leads
            WHERE (stage = 'treatment_completed'
                   OR od_relationship IN ('active_patient', 'reactivation'))
              AND (email != '' OR phone != '')
        """).fetchall()

    elif segment == "unconverted_leads":
        return db.execute("""
            SELECT id, email, phone FROM leads
            WHERE stage IN ('auto_nurture', 'no_show')
              AND gclid IS NOT NULL AND gclid != ''
              AND (email != '' OR phone != '')
        """).fetchall()

    elif segment == "high_value":
        return db.execute("""
            SELECT id, email, phone FROM leads
            WHERE attributed_production > 5000
              AND (email != '' OR phone != '')
        """).fetchall()

    return []


def sync_customer_match() -> dict:
    """
    Sync all three Customer Match lists from the lead database to Google Ads.
    Called by the weekly cron and the manual admin endpoint.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc).isoformat()

    try:
        client = _build_client()
    except Exception as e:
        logger.error(f"Customer Match: failed to build GAds client: {e}")
        return {"ok": False, "error": str(e)}

    customer_id = settings.google_ads_customer_id
    db = _get_db()
    results = []

    for list_name, description, segment in LISTS:
        try:
            leads = _query_segment(db, segment)
            ops = _build_operations(client, leads)

            if not ops:
                logger.info(f"Customer Match: no eligible leads for '{list_name}' — skipping job")
                results.append({
                    "list_name": list_name,
                    "status": "skipped",
                    "reason": "no eligible leads with email/phone",
                    "lead_count": len(leads),
                })
                continue

            user_list_rn = _get_or_create_user_list(client, customer_id, list_name, description)
            job_result = _run_job(client, customer_id, user_list_rn, ops)

            # Persist list metadata
            db.execute("""
                INSERT INTO customer_match_lists
                    (list_name, gads_resource_name, last_sync_at, last_op_count, last_status, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(list_name) DO UPDATE SET
                    gads_resource_name = excluded.gads_resource_name,
                    last_sync_at       = excluded.last_sync_at,
                    last_op_count      = excluded.last_op_count,
                    last_status        = excluded.last_status
            """, (
                list_name,
                user_list_rn,
                now,
                job_result["op_count"],
                "running",
                now,
            ))
            db.commit()

            results.append({
                "list_name": list_name,
                "status": "running",
                "lead_count": len(leads),
                "op_count": job_result["op_count"],
                "job_resource_name": job_result["job_resource_name"],
                "note": "Job async — member count updates in Google Ads UI within a few hours.",
            })
            logger.info(
                f"Customer Match: '{list_name}' — {job_result['op_count']} ops queued "
                f"(leads={len(leads)})"
            )

        except Exception as e:
            logger.error(f"Customer Match: error syncing '{list_name}': {e}")
            results.append({"list_name": list_name, "status": "error", "error": str(e)})

    db.close()

    ok = all(r.get("status") in ("running", "skipped") for r in results)
    return {
        "ok": ok,
        "synced_at": now,
        "lists": results,
        "min_size_note": (
            "Lists need ~100 matched members before usable for targeting/exclusion in Google Ads."
        ),
    }


def get_customer_match_status() -> dict:
    """Return current state of all Customer Match lists from local DB."""
    db = _get_db()
    rows = db.execute("""
        SELECT list_name, gads_resource_name, last_sync_at, member_count,
               last_op_count, last_status
        FROM customer_match_lists
        ORDER BY list_name
    """).fetchall()
    db.close()
    return {
        "lists": [dict(r) for r in rows],
        "note": "member_count reflects last known value; Google Ads updates it async after each job.",
    }
