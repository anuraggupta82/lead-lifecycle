"""
OpenDental patient matcher — matches leads to OD patients by phone/email hash.
Also syncs treatment plan stages for matched patients.

Runs as a nightly job. Never stores raw PHI — uses SHA-256 hashes for comparison.
Only accessible on the office LAN (GraftonServer).

Three functions:
  1. match_leads_to_od()         — match unmatched leads to OD patients
  2. sync_treatment_stages()     — update stages for already-matched leads
  3. run_full_od_sync()          — runs both (called by scheduler)
"""
import hashlib
import logging
import json
from datetime import datetime, timezone, timedelta
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo
from database import get_all_leads, get_lead, add_event, update_stage, enqueue_follow_ups, get_od_settings
from config import get_settings
from ga4_events import (
    track_treatment_presented, track_treatment_accepted, track_treatment_completed,
)

_REACTIVATION_DAYS = 548  # 18 months
_EASTERN = ZoneInfo("America/New_York")

logger = logging.getLogger(__name__)

CDT_IMPLANT_CODES = {
    "D6010", "D6011", "D6012", "D6013", "D6040", "D6041", "D6050",
    "D6051", "D6052", "D6053", "D6054", "D6055", "D6056", "D6057",
    "D6058", "D6059", "D6060", "D6061", "D6062", "D6063", "D6064",
    "D6065", "D6066", "D6067", "D6068", "D6069", "D6070", "D6071",
    "D6072", "D6073", "D6074", "D6075", "D6076", "D6077", "D6078",
    "D6079", "D6080", "D6081", "D6082", "D6083", "D6084", "D6085",
    "D6086", "D6087", "D6088", "D6089", "D6090", "D6091", "D6092",
    "D6093", "D6094", "D6095", "D6096", "D6097", "D6098", "D6099",
    "D6194",  # All-on-4 / full arch
}


def _hash(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()


def _get_db():
    try:
        import pymysql
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
        logger.warning(f"OpenDental MySQL unavailable: {e}")
        return None


def _get_sqlite():
    """Direct SQLite connection for batch updates."""
    import sqlite3
    import os
    settings = get_settings()
    os.makedirs(os.path.dirname(settings.db_path), exist_ok=True)
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ── od_relationship classifier ───────────────────────────────────────────────

def _compute_od_relationship(tp_status: dict, apt_info: dict) -> str:
    """
    Return one of: 'implant_prospect', 'reactivation', 'active_patient',
                   'new_patient', 'cold'
    Priority order matters — check implant first, then reactivation, then active.
    """
    # 1. Implant prospect — treatment plan with implant CDT codes exists
    if tp_status.get("has_treatment_plan"):
        return "implant_prospect"

    # 2. Reactivation — previously active but no visit in 18+ months.
    #    MySQL returns naive datetimes in office-local time (America/New_York).
    #    Compare against an Eastern-aware cutoff — never stamp as UTC.
    last_complete = apt_info.get("last_complete_date")
    if apt_info.get("has_showed") and last_complete:
        cutoff = datetime.now(_EASTERN) - timedelta(days=_REACTIVATION_DAYS)
        if isinstance(last_complete, str):
            try:
                last_complete = datetime.fromisoformat(last_complete)
            except ValueError:
                last_complete = None
        if last_complete:
            # Treat naive datetimes as Eastern (office-local)
            if last_complete.tzinfo is None:
                last_complete = last_complete.replace(tzinfo=_EASTERN)
            if last_complete < cutoff:
                return "reactivation"

    # 3. Active patient — has at least one completed appointment, no implant plan
    if apt_info.get("has_showed"):
        return "active_patient"

    # 4. New patient — matched to OD but no completed appointments yet
    #    (may or may not have a future appointment scheduled)
    return "new_patient"


# ── Part 1: Match unmatched leads to OD patients ────────────────────────────

def match_leads_to_od() -> dict:
    """
    Match unmatched leads to OpenDental patients using phone/email hashes.
    Pulls production attributed to implant CDT codes.
    """
    conn = _get_db()
    if not conn:
        return {"matched": 0, "errors": 1, "error": "OpenDental MySQL unavailable (office network required)"}

    unmatched = [
        l for l in get_all_leads()
        if not l.get("od_patient_num") and (l.get("phone_hash") or l.get("email_hash"))
    ]
    logger.info(f"OD matcher: {len(unmatched)} leads to check")

    matched = errors = 0

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT PatNum,
                       LOWER(HmPhone)       AS home_phone,
                       LOWER(WirelessPhone) AS cell_phone,
                       LOWER(Email)         AS email
                FROM patient
                WHERE PatStatus = 0
                  AND (HmPhone != '' OR WirelessPhone != '' OR Email != '')
            """)
            od_patients = cur.fetchall()

        # Build hash lookup: {hash: PatNum}
        phone_map = {}
        email_map = {}
        for row in od_patients:
            pat_num = str(row[0])
            for phone_raw in [row[1] or "", row[2] or ""]:
                digits = "".join(c for c in phone_raw if c.isdigit())
                if len(digits) >= 10:
                    phone_map[_hash(digits[-10:])] = pat_num
                    phone_map[_hash("1" + digits[-10:])] = pat_num
            email_raw = row[3] or ""
            if email_raw:
                email_map[_hash(email_raw)] = pat_num

        for lead in unmatched:
            try:
                pat_num = None
                match_method = None

                if lead.get("phone_hash") and lead["phone_hash"] in phone_map:
                    pat_num = phone_map[lead["phone_hash"]]
                    match_method = "phone"
                elif lead.get("email_hash") and lead["email_hash"] in email_map:
                    pat_num = email_map[lead["email_hash"]]
                    match_method = "email"

                if not pat_num:
                    continue

                # Get production, treatment plan, and appointment info
                production = _get_patient_production(conn, pat_num)
                tp_status  = _get_treatment_plan_status(conn, pat_num)
                apt_info   = _get_appointment_info(conn, pat_num)
                od_rel     = _compute_od_relationship(tp_status, apt_info)

                # Update lead in SQLite
                lconn = _get_sqlite()
                now = datetime.now(timezone.utc).isoformat()
                lconn.execute("""
                    UPDATE leads
                    SET od_patient_num=?, od_matched_at=?, attributed_production=?,
                        od_relationship=?, updated_at=?
                    WHERE id=?
                """, (pat_num, now, production["total"], od_rel, now, lead["id"]))
                lconn.commit()
                lconn.close()

                add_event(lead["id"], "od_matched", source="od_matcher",
                          detail=json.dumps({
                              "pat_num": pat_num,
                              "match_method": match_method,
                              "production": production["total"],
                              "codes": production["codes"],
                              "od_relationship": od_rel,
                          }))

                logger.info(f"Lead {lead['id']} matched to OD PatNum {pat_num} via {match_method}, "
                            f"production=${production['total']:.2f}, relationship={od_rel}")
                matched += 1

            except Exception as e:
                logger.error(f"Error matching lead {lead['id']}: {e}")
                errors += 1

    finally:
        conn.close()

    return {"matched": matched, "unmatched": len(unmatched) - matched, "errors": errors}


# ── Part 2: Sync treatment stages for already-matched leads ─────────────────

def sync_treatment_stages() -> dict:
    """
    For leads already matched to OD patients, check their treatment status:
      - Has a treatment plan with implant codes → "treatment_presented"
      - Has scheduled procedures (ProcStatus=1 with future AptNum) → "treatment_accepted"
      - Has completed procedures (ProcStatus=2) → "treatment_completed"
      - Has broken appointment (AptStatus=5) → "no_show"
    Also refreshes attributed_production, income, and appointment info.
    """
    conn = _get_db()
    if not conn:
        return {"updated": 0, "errors": 1, "error": "OpenDental MySQL unavailable (office network required)"}

    # Get all leads that have an OD match
    matched_leads = [
        l for l in get_all_leads()
        if l.get("od_patient_num")
    ]
    logger.info(f"Treatment stage sync: checking {len(matched_leads)} matched leads")

    updated = 0
    errors = 0

    try:
        for lead in matched_leads:
            try:
                pat_num = lead["od_patient_num"]
                current_stage = lead["stage"]

                # Check treatment plan status
                tp_status = _get_treatment_plan_status(conn, pat_num)
                production = _get_patient_production(conn, pat_num)
                income = _get_patient_income(conn, pat_num)
                apt_info = _get_appointment_info(conn, pat_num)

                # Determine what stage the lead should be in based on OD data
                new_stage = None

                if production["total"] > 0:
                    new_stage = "treatment_completed"
                elif tp_status["has_scheduled_procedures"]:
                    new_stage = "treatment_accepted"
                elif tp_status["has_treatment_plan"]:
                    new_stage = "treatment_presented"
                elif apt_info["has_showed"]:
                    new_stage = "showed"
                elif apt_info["has_broken"] and current_stage in ("scheduled", "new", "auto_nurture"):
                    new_stage = "no_show"
                elif apt_info["has_scheduled"]:
                    new_stage = "scheduled"

                # Re-evaluate od_relationship on every sync pass (relationship evolves)
                od_rel = _compute_od_relationship(tp_status, apt_info)

                # Update production, income, appointment info, treatment_plan_value, and od_relationship
                lconn = _get_sqlite()
                now = datetime.now(timezone.utc).isoformat()
                lconn.execute("""
                    UPDATE leads SET
                        attributed_production=?,
                        attributed_income=?,
                        treatment_plan_value=?,
                        appointment_date=?,
                        appointment_status=?,
                        no_show_count=?,
                        od_relationship=?,
                        updated_at=?
                    WHERE id=?
                """, (
                    production["total"],
                    income,
                    tp_status["plan_value"],
                    apt_info.get("next_apt_date", ""),
                    apt_info.get("status", ""),
                    apt_info.get("broken_count", 0),
                    od_rel,
                    now,
                    lead["id"],
                ))
                lconn.commit()
                lconn.close()

                # Advance stage if appropriate
                if new_stage and new_stage != current_stage:
                    update_stage(lead["id"], new_stage, source="od_matcher",
                                 detail=json.dumps({
                                     "production": production["total"],
                                     "income": income,
                                     "tp_value": tp_status["plan_value"],
                                     "codes": production["codes"],
                                 }))
                    add_event(lead["id"], f"od_stage_{new_stage}", source="od_matcher",
                              detail=json.dumps({
                                  "from_stage": current_stage,
                                  "to_stage": new_stage,
                                  "production": production["total"],
                                  "income": income,
                                  "tp_value": tp_status["plan_value"],
                              }))

                    # Fire GA4 events for treatment milestones
                    try:
                        if new_stage == "treatment_presented":
                            track_treatment_presented(lead["id"], plan_value=tp_status["plan_value"])
                        elif new_stage == "treatment_accepted":
                            track_treatment_accepted(lead["id"], plan_value=tp_status["plan_value"])
                        elif new_stage == "treatment_completed":
                            track_treatment_completed(lead["id"], production=production["total"])
                    except Exception as e:
                        logger.debug(f"GA4 event failed for {lead['id']} (non-fatal): {e}")

                    # Trigger no-show follow-up sequence (email + SMS)
                    if new_stage == "no_show":
                        try:
                            _send_no_show_follow_ups(lead)
                        except Exception as e:
                            logger.warning(f"No-show follow-up failed for {lead['id']}: {e}")

                    updated += 1
                    logger.info(
                        f"Lead {lead['id']} (PatNum {pat_num}): "
                        f"{current_stage} → {new_stage}, "
                        f"production=${production['total']:.2f}, "
                        f"income=${income:.2f}, "
                        f"TP value=${tp_status['plan_value']:.2f}"
                    )

            except Exception as e:
                logger.error(f"Error syncing treatment stage for lead {lead['id']}: {e}")
                errors += 1

    finally:
        conn.close()

    return {"updated": updated, "checked": len(matched_leads), "errors": errors}


def _get_treatment_plan_status(conn, pat_num: str) -> dict:
    """
    Check if patient has treatment plans with implant codes.
    Returns: {has_treatment_plan, has_scheduled_procedures, plan_value}
    """
    result = {
        "has_treatment_plan": False,
        "has_scheduled_procedures": False,
        "plan_value": 0.0,
    }

    try:
        placeholders = ",".join(["%s"] * len(CDT_IMPLANT_CODES))

        with conn.cursor() as cur:
            # Check for treatment plan entries with implant codes
            # proctp has ProcCode directly (no CodeNum in this OD version)
            cur.execute(f"""
                SELECT SUM(pt.FeeAmt) as plan_total, COUNT(*) as plan_count
                FROM proctp pt
                JOIN treatplan tp ON pt.TreatPlanNum = tp.TreatPlanNum
                WHERE tp.PatNum = %s
                  AND tp.TPStatus IN (0, 1)
                  AND pt.ProcCode IN ({placeholders})
            """, [int(pat_num)] + list(CDT_IMPLANT_CODES))
            row = cur.fetchone()

            if row and row[1] and int(row[1]) > 0:
                result["has_treatment_plan"] = True
                result["plan_value"] = float(row[0] or 0)

            # Check for scheduled procedures (ProcStatus=1 = treatment planned,
            # with a future appointment)
            cur.execute(f"""
                SELECT COUNT(*) as scheduled_count
                FROM procedurelog pl
                JOIN procedurecode pc ON pl.CodeNum = pc.CodeNum
                WHERE pl.PatNum = %s
                  AND pl.ProcStatus = 1
                  AND pl.AptNum > 0
                  AND pc.ProcCode IN ({placeholders})
            """, [int(pat_num)] + list(CDT_IMPLANT_CODES))
            sched_row = cur.fetchone()

            if sched_row and int(sched_row[0]) > 0:
                result["has_scheduled_procedures"] = True

    except Exception as e:
        logger.warning(f"Error checking treatment plan for PatNum {pat_num}: {e}")

    return result


def _get_patient_production(conn, pat_num: str) -> dict:
    """Get total completed implant production for a patient."""
    try:
        placeholders = ",".join(["%s"] * len(CDT_IMPLANT_CODES))
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT pc.ProcCode, SUM(pl.ProcFee) as total
                FROM procedurelog pl
                JOIN procedurecode pc ON pl.CodeNum = pc.CodeNum
                WHERE pl.PatNum = %s
                  AND pl.ProcStatus = 2
                  AND pc.ProcCode IN ({placeholders})
                GROUP BY pc.ProcCode
            """, [int(pat_num)] + list(CDT_IMPLANT_CODES))
            rows = cur.fetchall()
            total = sum(float(r[1]) for r in rows)
            codes = [r[0] for r in rows]
            return {"total": total, "codes": codes}
    except Exception:
        return {"total": 0.0, "codes": []}


def _send_no_show_follow_ups(lead: dict):
    """Send immediate no-show email + SMS when broken appointment is detected."""
    from email_service import send_no_show_email
    from sms_service import send_no_show_sms

    lead_id = lead["id"]

    # Build unsub URL
    settings = get_settings()
    unsub_url = f"http://localhost:{settings.port}/unsubscribe/{lead_id}/email"

    # Send email if not unsubscribed
    if not lead.get("unsubscribed_email") and lead.get("email"):
        try:
            success = send_no_show_email(lead, unsub_url)
            if success:
                add_event(lead_id, "email_sent", source="od_matcher",
                          detail=json.dumps({"template": "no_show_email"}))
                logger.info(f"No-show email sent to lead {lead_id}")
        except Exception as e:
            logger.warning(f"No-show email failed for {lead_id}: {e}")

    # Send SMS if not unsubscribed
    if not lead.get("unsubscribed_sms") and lead.get("phone"):
        try:
            success = send_no_show_sms(lead)
            if success:
                add_event(lead_id, "sms_sent", source="od_matcher",
                          detail=json.dumps({"template": "no_show_sms"}))
                logger.info(f"No-show SMS sent to lead {lead_id}")
        except Exception as e:
            logger.warning(f"No-show SMS failed for {lead_id}: {e}")


def _get_patient_income(conn, pat_num: str) -> float:
    """Get total payments (income/collections) for a patient — actual money received."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT SUM(SplitAmt) as total
                FROM paysplit
                WHERE PatNum = %s
            """, [int(pat_num)])
            row = cur.fetchone()
            return float(row[0] or 0) if row else 0.0
    except Exception as e:
        logger.warning(f"Error getting income for PatNum {pat_num}: {e}")
        return 0.0


def _get_appointment_info(conn, pat_num: str) -> dict:
    """
    Get appointment status for a patient.
    AptStatus: 1=Scheduled, 2=Complete, 3=UnschedList, 4=ASAP, 5=Broken
    last_complete_date: datetime of most recent completed appointment (for reactivation check).
    """
    result = {
        "has_scheduled": False,
        "has_showed": False,       # Complete appointment exists
        "has_broken": False,
        "broken_count": 0,
        "next_apt_date": "",
        "status": "",
        "last_complete_date": None,  # datetime | None — used by _compute_od_relationship
    }
    try:
        with conn.cursor() as cur:
            # Rows ordered DESC by AptDateTime — first complete row is the most recent.
            # LIMIT 50: covers all clinically relevant appointments without full scan.
            cur.execute("""
                SELECT AptStatus, AptDateTime
                FROM appointment
                WHERE PatNum = %s
                ORDER BY AptDateTime DESC
                LIMIT 50
            """, [int(pat_num)])
            rows = cur.fetchall()

            for row in rows:
                status = int(row[0])
                apt_date = row[1]  # datetime object from pymysql

                if status == 5:  # Broken
                    result["has_broken"] = True
                    result["broken_count"] += 1
                elif status == 2:  # Complete
                    result["has_showed"] = True
                    # Capture only the MOST RECENT completed apt (rows are DESC so first wins)
                    if result["last_complete_date"] is None and apt_date:
                        result["last_complete_date"] = apt_date  # keep as datetime
                elif status == 1:  # Scheduled
                    result["has_scheduled"] = True
                    if apt_date:
                        result["next_apt_date"] = str(apt_date)[:10]  # YYYY-MM-DD

            # Determine overall status string
            if result["has_scheduled"]:
                result["status"] = "scheduled"
            elif result["has_showed"]:
                result["status"] = "complete"
            elif result["has_broken"]:
                result["status"] = "broken"

    except Exception as e:
        logger.warning(f"Error getting appointment info for PatNum {pat_num}: {e}")

    return result


# ── Combined runner ──────────────────────────────────────────────────────────

def run_full_od_sync() -> dict:
    """Run both matching and treatment stage sync. Called by nightly scheduler."""
    match_result = match_leads_to_od()
    stage_result = sync_treatment_stages()
    return {
        "match": match_result,
        "treatment_stages": stage_result,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = run_full_od_sync()
    print(json.dumps(result, indent=2))
