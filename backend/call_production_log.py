"""
call_production_log.py — Close the phone-only attribution revenue blackhole.

Problem (G1):
  Patients who click a Google Ad, call directly (no web form), book, show, and
  produce revenue are INVISIBLE to keyword attribution. od_matcher.py only writes
  keyword_production_log for leads in the `leads` table — Mango calls with no
  lead_id never get production logged, even when they have a clear keyword
  attribution (Methods A–C) and a confirmed OD appointment.

This module fixes that by:
  1. Walking mango_calls rows that are fully resolved:
       - od_appointment_id IS NOT NULL        (matched to an actual OD appointment;
                                               this is the booking signal — booked_outcome
                                               was never populated by the pipeline)
       - od_patient_num IS NOT NULL           (matched to an OD patient)
       - attributed_keyword_method != ''      (any GAds attribution exists)
       - attributed_keyword_method != 'no_signal'
  2. Pulling the patient's OD production (implant CDT codes) via od_matcher helpers.
  3. Writing a keyword_production_log row with source='call' using a synthetic
     lead_id of 'call::{mango_call_uuid}' so the UNIQUE(lead_id, od_patient_num)
     constraint doesn't collide with existing lead-sourced rows.

Guard against double-counting:
  - If a lead-sourced keyword_production_log row already exists for the same
    od_patient_num, we skip the call path — lead attribution (gclid, higher
    confidence) always wins.
  - Within the call path, an upsert on UNIQUE(lead_id, od_patient_num) refreshes
    production_amount + procedure_codes on each nightly run (patients may have
    more procedures completed since last log) while preserving the row's id PK.

Backfill:
  Pass days=60 (default) to process calls going back 60 days, covering patients
  from recently closed campaigns. The daily scheduler run uses days=7 for
  incremental updates.

Run:
  - Nightly at 22:30 ET (registered in main.py scheduler)
  - On-demand: POST /api/admin/sync-call-production
  - Backfill: call link_calls_to_keyword_production(days=60) once after deploy
"""

import logging
import json
from datetime import datetime, timezone, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

# Only attribute calls with a method that has at least campaign-level signal.
# 'no_signal' and '' provide nothing useful.
_SKIP_METHODS = {"no_signal", "", None}

# Minimum call confidence to write production. Weak (campaign_only) calls get
# campaign-level attribution in the dashboard but no production log entry —
# we don't want to pollute keyword ROAS with low-confidence attributions.
# 'campaign_only' is 0.30; we require at least 0.55 (moderate tier).
_MIN_CONFIDENCE_FOR_PRODUCTION = 0.55


def _get_od_conn():
    """Open OpenDental MySQL connection. Returns None if unavailable."""
    try:
        import pymysql
        from database import get_od_settings
        s = get_od_settings()
        return pymysql.connect(
            host=s["od_db_host"],
            port=s["od_db_port"],
            user=s["od_db_user"],
            password=s["od_db_password"],
            database=s["od_db_name"],
            connect_timeout=5,
            charset="utf8mb4",
        )
    except Exception as e:
        logger.warning(f"[call_prod] OpenDental MySQL unavailable: {e}")
        return None


def _fetch_call_production_data(days: int = 60) -> list:
    """
    Return mango_calls rows that are fully resolved and ready for production logging.
    Joins gads_call_view to recover campaign_id/campaign_name when the call was
    attributed via Method C (time-window/campaign-only path).

    Patient status gate (mirrors google_ads_conversions.py logic):
      new_patient      → include — no prior OD history at time of match
      unknown / ''     → include — not yet classified, give benefit of the doubt
      existing_active  → exclude — pre-existing patient; their implant revenue
                         is not attributable to the ad that drove the call
      existing_inactive→ exclude — lapsed patient; same reasoning
    """
    from database import _conn
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    with _conn() as conn:
        rows = conn.execute("""
            SELECT
                mc.uuid,
                mc.started_at,
                mc.from_number,
                mc.od_patient_num,
                mc.od_appointment_id,
                mc.lead_id,
                mc.attributed_keyword,
                mc.attributed_match_type,
                mc.attributed_ad_group,
                mc.attributed_keyword_method,
                mc.attributed_keyword_confidence,
                mc.od_patient_status,
                -- Campaign info: prefer from lead (Method A/B), fall back to gads_call_view (C)
                COALESCE(l.campaign_id,   gcv.campaign_id,   '') AS campaign_id,
                COALESCE(l.campaign_name, gcv.campaign_name, '') AS campaign_name,
                COALESCE(l.ad_group_name, mc.attributed_ad_group, '') AS ad_group_name,
                -- gclid: only available if the call was tied to a web-form lead
                COALESCE(l.gclid, '') AS gclid
            FROM mango_calls mc
            LEFT JOIN leads l             ON l.id = mc.lead_id
                                         AND mc.lead_id IS NOT NULL
                                         AND mc.lead_id != ''
            LEFT JOIN gads_call_view gcv  ON gcv.call_id = mc.gads_call_id
            WHERE mc.started_at >= ?
              AND mc.direction = 'inbound'
              AND mc.od_appointment_id IS NOT NULL
              AND mc.od_appointment_id != ''
              AND mc.od_patient_num IS NOT NULL
              AND mc.od_patient_num != ''
              AND mc.attributed_keyword_method IS NOT NULL
              AND mc.attributed_keyword_method != ''
              AND mc.attributed_keyword_method != 'no_signal'
              AND mc.attributed_keyword_method != 'campaign_only'
              AND COALESCE(mc.attributed_keyword_confidence, 0) >= ?
              -- Exclude pre-existing patients — their production is not new-patient acquisition
              AND COALESCE(mc.od_patient_status, '') NOT IN ('existing_active', 'existing_inactive')
            ORDER BY mc.started_at DESC
        """, (cutoff, _MIN_CONFIDENCE_FOR_PRODUCTION)).fetchall()

    return [dict(r) for r in rows]


def _existing_lead_production_patient_nums() -> set:
    """
    Return the set of od_patient_num values already in keyword_production_log
    via the lead (web-form) path. Used to avoid double-counting when a patient
    submitted a form AND called.

    We identify lead-sourced rows by lead_id NOT starting with 'call::'.
    """
    from database import _conn
    with _conn() as conn:
        rows = conn.execute("""
            SELECT DISTINCT od_patient_num
            FROM keyword_production_log
            WHERE od_patient_num != ''
              AND lead_id NOT LIKE 'call::%'
        """).fetchall()
    return {r["od_patient_num"] for r in rows}


def _get_od_production(od_conn, pat_num: str) -> dict:
    """
    Pull completed implant CDT production for a patient from OpenDental.
    Reuses the same query pattern as od_matcher._get_patient_production().
    Returns {"total": float, "codes": [str]}.
    """
    from od_matcher import CDT_IMPLANT_CODES
    if not pat_num or not od_conn:
        return {"total": 0.0, "codes": []}
    # Validate pat_num is numeric before the int() cast — legacy data or
    # accidental strings would raise ValueError and be silently swallowed
    # as $0 production, permanently misclassifying the patient.
    if not str(pat_num).strip().isdigit():
        logger.warning(f"[call_prod] PatNum '{pat_num}' is not numeric — skipping OD lookup")
        return {"total": 0.0, "codes": []}
    try:
        # Reconnect if connection dropped mid-batch (connect_timeout only covers connect)
        try:
            od_conn.ping(reconnect=True)
        except Exception:
            pass
        placeholders = ",".join(["%s"] * len(CDT_IMPLANT_CODES))
        with od_conn.cursor() as cur:
            cur.execute(f"""
                SELECT pc.ProcCode, SUM(pl.ProcFee) AS total
                FROM procedurelog pl
                JOIN procedurecode pc ON pl.CodeNum = pc.CodeNum
                WHERE pl.PatNum = %s
                  AND pl.ProcStatus = 2
                  AND pc.ProcCode IN ({placeholders})
                GROUP BY pc.ProcCode
            """, [int(pat_num)] + list(CDT_IMPLANT_CODES))
            rows = cur.fetchall()
            total = sum(float(r[1]) for r in rows if r[1])
            codes = [r[0] for r in rows if r[1]]
            return {"total": total, "codes": codes}
    except Exception as e:
        logger.warning(f"[call_prod] OD production fetch failed for PatNum {pat_num}: {e}")
        return {"total": 0.0, "codes": []}


def _appointment_date_from_uuid(call_row: dict) -> str:
    """
    Derive appointment date from the call's started_at (Eastern Time),
    used as the appointment_date field in keyword_production_log.
    """
    started_at = call_row.get("started_at") or ""
    if not started_at:
        return ""
    try:
        s = started_at.replace("Z", "+00:00")
        if "+" not in s and "T" in s:
            s += "+00:00"
        dt = datetime.fromisoformat(s).astimezone(_ET)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return started_at[:10]


def _write_call_production_row(call_row: dict, production: dict) -> bool:
    """
    Write one keyword_production_log row sourced from a Mango call.

    Uses a synthetic lead_id = 'call::{uuid}' so it never collides with
    real lead rows under the UNIQUE(lead_id, od_patient_num) constraint.

    On conflict (same call already logged): refreshes production_amount,
    procedure_codes, and logged_at so nightly runs pick up newly completed
    procedures without churning the row's id PK or audit trail.

    Returns True if a row was inserted or updated, False if unchanged.
    """
    from database import _conn

    synthetic_lead_id = f"call::{call_row['uuid']}"
    od_patient_num = call_row["od_patient_num"]
    keyword = (call_row.get("attributed_keyword") or "").lower().strip()
    match_type = call_row.get("attributed_match_type") or ""
    campaign_id = call_row.get("campaign_id") or ""
    campaign_name = call_row.get("campaign_name") or ""
    ad_group_name = call_row.get("ad_group_name") or ""
    gclid = call_row.get("gclid") or ""
    apt_date = _appointment_date_from_uuid(call_row)
    method = call_row.get("attributed_keyword_method") or ""
    now = datetime.now(timezone.utc).isoformat()
    codes_json = json.dumps(production["codes"])

    with _conn() as conn:
        cur = conn.execute("""
            INSERT INTO keyword_production_log
               (logged_at, lead_id, keyword_text, match_type, campaign_id, campaign_name,
                ad_group_name, gclid, od_patient_num, production_amount, procedure_codes,
                match_method, appointment_date)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(lead_id, od_patient_num) DO UPDATE SET
                production_amount = excluded.production_amount,
                procedure_codes   = excluded.procedure_codes,
                logged_at         = excluded.logged_at
            WHERE excluded.production_amount != keyword_production_log.production_amount
               OR excluded.procedure_codes   != keyword_production_log.procedure_codes
        """, (
            now,
            synthetic_lead_id,
            keyword,
            match_type,
            campaign_id,
            campaign_name,
            ad_group_name,
            gclid,
            od_patient_num,
            production["total"],
            codes_json,
            f"call_{method}",   # prefix so it's distinguishable from lead-path match methods
            apt_date,
        ))
        return cur.rowcount > 0


def link_calls_to_keyword_production(days: int = 60) -> dict:
    """
    Main entry point. Walk resolved Mango calls and write production attribution.

    Args:
        days: How many days back to look for unlogged calls.
              Default 60 covers patients from recently closed campaigns.
              The daily incremental run uses days=7.

    Returns:
        Summary dict: {processed, written, unchanged, skipped_lead_wins,
                       skipped_no_production, skipped_od_unavailable, errors, total_production}
    """
    counts = {
        "processed": 0,
        "written": 0,          # rows inserted or updated (production changed)
        "unchanged": 0,        # rows where production hasn't changed since last run
        "skipped_lead_wins": 0,       # lead-path row already exists for this patient
        "skipped_no_production": 0,   # OD returned $0 — nothing to log yet
        "skipped_od_unavailable": 0,  # OD MySQL not reachable
        "errors": 0,
        "total_production": 0.0,
    }

    # Fetch calls ready for logging
    try:
        call_rows = _fetch_call_production_data(days=days)
    except Exception as e:
        logger.error(f"[call_prod] Failed to fetch call rows: {e}")
        counts["errors"] += 1
        return counts

    if not call_rows:
        logger.info("[call_prod] No resolved calls pending production logging")
        return counts

    logger.info(f"[call_prod] {len(call_rows)} resolved calls to check (days={days})")

    # Build set of od_patient_nums already attributed via the lead (web-form) path.
    # Lead-path wins when both exist — the gclid is a stronger signal.
    try:
        lead_attributed_patients = _existing_lead_production_patient_nums()
    except Exception as e:
        logger.warning(f"[call_prod] Could not build lead-patient set (non-fatal): {e}")
        lead_attributed_patients = set()

    # Open OD connection once for the full batch
    od_conn = _get_od_conn()
    if od_conn is None:
        logger.warning("[call_prod] OpenDental unavailable — cannot fetch production amounts")
        counts["skipped_od_unavailable"] = len(call_rows)
        return counts

    try:
        for call_row in call_rows:
            counts["processed"] += 1
            uuid = call_row["uuid"]
            od_patient_num = call_row["od_patient_num"]

            try:
                # Guard 1: Lead-path wins — skip if the same patient is already
                # attributed via a web-form lead (avoids double-counting)
                if od_patient_num in lead_attributed_patients:
                    logger.debug(
                        f"[call_prod] uuid={uuid} od_patient_num={od_patient_num} "
                        f"skipped: lead-path row exists (lead wins)"
                    )
                    counts["skipped_lead_wins"] += 1
                    continue

                # Fetch OD production for this patient
                production = _get_od_production(od_conn, od_patient_num)

                # Guard 3: No production yet — patient may not have had procedures
                # completed. We still write a $0 row so the appointment is tracked,
                # and it will be refreshed on the next nightly run via INSERT OR REPLACE.
                # If production is truly $0 and we have no codes, skip for now.
                if production["total"] == 0.0 and not production["codes"]:
                    logger.debug(
                        f"[call_prod] uuid={uuid} PatNum={od_patient_num}: "
                        f"no completed production yet — skipping"
                    )
                    counts["skipped_no_production"] += 1
                    continue

                # Write/refresh the production log row (upsert only updates if changed)
                written = _write_call_production_row(call_row, production)
                if written:
                    counts["written"] += 1
                    counts["total_production"] += production["total"]
                    logger.info(
                        f"[call_prod] Logged: uuid={uuid[:8]} "
                        f"keyword='{call_row.get('attributed_keyword', '')}' "
                        f"method={call_row.get('attributed_keyword_method', '')} "
                        f"PatNum={od_patient_num} "
                        f"production=${production['total']:.2f}"
                    )
                else:
                    counts["unchanged"] += 1

            except Exception as e:
                logger.error(f"[call_prod] Error processing call uuid={uuid}: {e}")
                counts["errors"] += 1

    finally:
        try:
            od_conn.close()
        except Exception:
            pass

    logger.info(
        f"[call_prod] Done: processed={counts['processed']} written={counts['written']} "
        f"unchanged={counts['unchanged']} lead_wins={counts['skipped_lead_wins']} "
        f"no_prod={counts['skipped_no_production']} errors={counts['errors']} "
        f"total_production=${counts['total_production']:.2f}"
    )
    return counts


def backfill_call_production(days: int = 60) -> dict:
    """
    One-shot backfill for calls going back `days` days.
    Covers patients from recently closed campaigns.
    Safe to call multiple times — idempotent via upsert (only refreshes rows
    where production_amount or procedure_codes changed).
    """
    logger.info(f"[call_prod] Starting backfill (days={days})")
    result = link_calls_to_keyword_production(days=days)
    logger.info(f"[call_prod] Backfill complete: {result}")
    return result
