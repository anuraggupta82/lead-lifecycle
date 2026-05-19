"""
OpenDental patient matcher — matches leads to OD patients by phone/email hash.
Also syncs treatment plan stages for matched patients.

Runs as a nightly job. Never stores raw PHI — uses SHA-256 hashes for comparison.
Only accessible on the office LAN (GraftonServer).

Five functions:
  1. match_leads_to_od()              — match unmatched leads to OD patients
  2. sync_treatment_stages()          — update stages for already-matched leads
  3. sync_scheduler_direct_leads()    — auto-create leads for scheduler (visitgdc.com) bookings
  4. run_full_od_sync()               — runs 1-3 (called by nightly scheduler)
  5. match_calls_to_od_appointments() — link booked Mango calls → OD appointments
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


def _parse_attr_marker(note_text: str) -> dict:
    """
    Parse the ATTR: attribution marker written by the scheduler backend into
    an OD appointment Note field.

    Format written by stripe_router.py:
        ATTR:gclid=abc123;utm_source=google;utm_medium=cpc;utm_campaign=implants;
             utm_term=dental+implants;utm_content=ad1;fbclid=;msclkid=;
             landing_url=https://visitgdc.com/?gclid=abc123;ga4_client_id=GA1.1.xxx

    Returns a dict with string values for all recognised keys, or {} if no
    ATTR: block is found.
    """
    if not note_text:
        return {}
    # Find the ATTR: block — it may appear after a patient message
    idx = note_text.find("ATTR:")
    if idx == -1:
        return {}
    attr_str = note_text[idx + 5:]  # everything after "ATTR:"
    result = {}
    for part in attr_str.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            continue
        key, _, val = part.partition("=")
        key = key.strip()
        val = val.strip()
        # Only capture known attribution keys; skip empty values
        if key in ("gclid", "fbclid", "msclkid", "gbraid", "wbraid",
                   "utm_source", "utm_medium", "utm_campaign",
                   "utm_term", "utm_content", "landing_url", "ga4_client_id"):
            if val:
                # Reverse the percent-encoding applied by _build_attr_marker in stripe_router.py
                val = val.replace("%3B", ";").replace("%3D", "=")
                result[key] = val
    return result


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

    M3 fix: also refreshes production for already-matched leads that have a gclid,
    so cumulative OD production (crown work added 60 days post-consult) is captured.
    Uses INSERT OR REPLACE in append_keyword_production_log so re-runs are idempotent.
    """
    conn = _get_db()
    if not conn:
        return {"matched": 0, "errors": 1, "error": "OpenDental MySQL unavailable (office network required)"}

    all_leads = get_all_leads()
    unmatched = [
        l for l in all_leads
        if not l.get("od_patient_num") and (l.get("phone_hash") or l.get("email_hash"))
    ]
    # Already-matched leads with gclid — refresh production amounts nightly
    already_matched_with_gclid = [
        l for l in all_leads
        if l.get("od_patient_num") and l.get("gclid") and l.get("keyword_text")
    ]
    logger.info(f"OD matcher: {len(unmatched)} leads to match, "
                f"{len(already_matched_with_gclid)} existing matches to refresh")

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

                # Phase A: append to keyword_production_log when lead has a gclid + keyword
                if lead.get("gclid") and lead.get("keyword_text"):
                    try:
                        from database import append_keyword_production_log
                        # Look up match_type from gads_keywords_cache for this keyword+campaign
                        _match_type = ""
                        try:
                            _lconn2 = _get_sqlite()
                            _kw_row = _lconn2.execute(
                                "SELECT match_type FROM gads_keywords_cache "
                                "WHERE LOWER(keyword_text)=? AND campaign_name=? LIMIT 1",
                                (lead["keyword_text"].lower(), lead.get("campaign_name", ""))
                            ).fetchone()
                            _lconn2.close()
                            if _kw_row:
                                _match_type = dict(_kw_row).get("match_type", "")
                        except Exception:
                            pass
                        # Determine appointment date from apt_info
                        _apt_date = apt_info.get("next_apt_date", "")
                        if not _apt_date and apt_info.get("last_complete_date"):
                            _apt_date = str(apt_info["last_complete_date"])[:10]
                        append_keyword_production_log(
                            lead_id=lead["id"],
                            keyword_text=lead["keyword_text"],
                            match_type=_match_type,
                            campaign_id=lead.get("campaign_id", ""),
                            campaign_name=lead.get("campaign_name", ""),
                            ad_group_name=lead.get("ad_group_name", ""),
                            gclid=lead["gclid"],
                            od_patient_num=pat_num,
                            production_amount=production["total"],
                            procedure_codes=production.get("codes", []),
                            match_method=match_method,
                            appointment_date=_apt_date,
                        )
                        logger.info(
                            f"[phase_a] keyword_production_log: lead={lead['id'][:8]} "
                            f"kw='{lead['keyword_text']}' prod=${production['total']:.2f}"
                        )
                    except Exception as _kpl_err:
                        logger.warning(f"[phase_a] keyword_production_log append failed (non-fatal): {_kpl_err}")

                logger.info(f"Lead {lead['id']} matched to OD PatNum {pat_num} via {match_method}, "
                            f"production=${production['total']:.2f}, relationship={od_rel}")
                matched += 1

            except Exception as e:
                logger.error(f"Error matching lead {lead['id']}: {e}")
                errors += 1

    finally:
        conn.close()

    # ── M3 fix: refresh production for already-matched leads with gclid ───────
    # Opens a fresh OD connection for the production refresh pass.
    production_refreshed = 0
    if already_matched_with_gclid:
        refresh_conn = _get_db()
        if refresh_conn:
            try:
                for lead in already_matched_with_gclid:
                    try:
                        pat_num = lead["od_patient_num"]
                        production = _get_patient_production(refresh_conn, pat_num)
                        apt_info = _get_appointment_info(refresh_conn, pat_num)
                        # Look up match_type from cache
                        _match_type = ""
                        try:
                            _lconn = _get_sqlite()
                            _kw_row = _lconn.execute(
                                "SELECT match_type FROM gads_keywords_cache "
                                "WHERE LOWER(keyword_text)=? AND campaign_name=? LIMIT 1",
                                (lead["keyword_text"].lower(), lead.get("campaign_name", ""))
                            ).fetchone()
                            _lconn.close()
                            if _kw_row:
                                _match_type = dict(_kw_row).get("match_type", "")
                        except Exception:
                            pass
                        _apt_date = apt_info.get("next_apt_date", "")
                        if not _apt_date and apt_info.get("last_complete_date"):
                            _apt_date = str(apt_info["last_complete_date"])[:10]
                        from database import append_keyword_production_log
                        append_keyword_production_log(
                            lead_id=lead["id"],
                            keyword_text=lead["keyword_text"],
                            match_type=_match_type,
                            campaign_id=lead.get("campaign_id", ""),
                            campaign_name=lead.get("campaign_name", ""),
                            ad_group_name=lead.get("ad_group_name", ""),
                            gclid=lead["gclid"],
                            od_patient_num=pat_num,
                            production_amount=production["total"],
                            procedure_codes=production.get("codes", []),
                            match_method=lead.get("od_matched_at", "refresh"),
                            appointment_date=_apt_date,
                        )
                        production_refreshed += 1
                    except Exception as _ref_err:
                        logger.warning(f"[phase_a] Production refresh failed for lead {lead['id'][:8]}: {_ref_err}")
            finally:
                refresh_conn.close()
            if production_refreshed:
                logger.info(f"[phase_a] Refreshed production for {production_refreshed} already-matched leads")

    return {
        "matched": matched,
        "unmatched": len(unmatched) - matched,
        "errors": errors,
        "production_refreshed": production_refreshed,
    }


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
    latest_scheduled_note: Note field from the most recent Scheduled appointment —
        used by sync_scheduler_direct_leads() to recover attribution from the ATTR: marker.
    """
    result = {
        "has_scheduled": False,
        "has_showed": False,       # Complete appointment exists
        "has_broken": False,
        "broken_count": 0,
        "next_apt_date": "",
        "next_apt_datetime": "",   # full ISO datetime of next scheduled appointment
        "status": "",
        "last_complete_date": None,  # datetime | None — used by _compute_od_relationship
        "latest_scheduled_note": "",  # Note text of most recent Scheduled apt (for ATTR: marker)
    }
    try:
        with conn.cursor() as cur:
            # Rows ordered DESC by AptDateTime — first complete row is the most recent.
            # LIMIT 50: covers all clinically relevant appointments without full scan.
            # Note column added to recover ATTR: attribution marker written by the scheduler.
            cur.execute("""
                SELECT AptStatus, AptDateTime, Note
                FROM appointment
                WHERE PatNum = %s
                ORDER BY AptDateTime DESC
                LIMIT 50
            """, [int(pat_num)])
            rows = cur.fetchall()

            for row in rows:
                status = int(row[0])
                apt_date = row[1]  # datetime object from pymysql
                note_text = row[2] or ""

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
                        result["next_apt_datetime"] = str(apt_date)   # full datetime string
                    # Capture Note from the FIRST (most recent) scheduled apt only
                    if not result["latest_scheduled_note"] and note_text:
                        result["latest_scheduled_note"] = note_text

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


# ── Part 3: Auto-create leads for scheduler bookings ────────────────────────

def sync_scheduler_direct_leads(lookback_days: int = 30) -> dict:
    """
    Scan OpenDental appointments for recent bookings that were created via the
    visitgdc.com online scheduler (identified by an ATTR: marker in the Note
    field), and auto-create a lead in the marketing pipeline for each one that
    doesn't already have a matching lead.

    Why OD directly (not the scheduler DB):
      The scheduler backend runs on Cloud Run with Cloud SQL (Postgres). That DB
      is not directly accessible from the lead lifecycle service, which runs
      locally on the Mac. OpenDental IS accessible on the office LAN and is
      already the integration substrate — the scheduler writes an ATTR: marker
      into every appointment's Note field. The nightly OD sync is the bridge.

    The function is idempotent: it checks email and phone against existing leads
    before creating anything, so re-runs are safe.

    Args:
        lookback_days: How far back to look for OD appointments. Defaults to 30 days.

    Returns:
        dict with created/skipped/errors counts.
    """
    import uuid

    od_conn = _get_db()
    if not od_conn:
        return {"created": 0, "skipped": 0, "errors": 0,
                "error": "OpenDental MySQL unavailable (office network required)"}

    # ── Query recent OD appointments whose Note contains ATTR: ───────────────
    # Filter by DateTStamp (record creation time) not AptDateTime so we catch
    # appointments created recently even if scheduled in the past or far future.
    cutoff = (datetime.now(_EASTERN) - timedelta(days=lookback_days)).replace(tzinfo=None)
    try:
        with od_conn.cursor() as cur:
            cur.execute("""
                SELECT a.AptNum, a.PatNum, a.AptDateTime, a.Note,
                       p.FName, p.LName,
                       LOWER(p.Email) AS email,
                       p.HmPhone, p.WirelessPhone
                FROM appointment a
                JOIN patient p ON a.PatNum = p.PatNum
                WHERE a.DateTStamp >= %s
                  AND a.Note LIKE %s
                ORDER BY a.DateTStamp DESC
            """, (cutoff, "%ATTR:%"))
            rows = cur.fetchall()
    except Exception as e:
        od_conn.close()
        logger.warning(f"[sched_leads] OD query failed: {e}")
        return {"created": 0, "skipped": 0, "errors": 0, "error": str(e)}

    if not rows:
        od_conn.close()
        logger.info("[sched_leads] No scheduler-sourced OD appointments found")
        return {"created": 0, "skipped": 0, "errors": 0}

    logger.info(f"[sched_leads] Found {len(rows)} scheduler-sourced OD apts (last {lookback_days}d)")

    from database import find_lead_by_identifiers, upsert_lead, is_deleted_lead

    created = skipped = errors = 0

    try:
        for row in rows:
            try:
                apt_num    = str(row[0])
                pat_num    = str(row[1])
                apt_dt     = row[2]   # datetime from pymysql
                note_text  = row[3] or ""
                first_name = row[4] or ""
                last_name  = row[5] or ""
                email      = (row[6] or "").strip().lower()
                hm_phone   = row[7] or ""
                cell_phone = row[8] or ""

                # Use cell phone preferentially, fall back to home
                raw_phone = cell_phone.strip() or hm_phone.strip()
                phone_digits = "".join(c for c in raw_phone if c.isdigit())
                phone = phone_digits[-10:] if len(phone_digits) >= 10 else phone_digits

                if not email and not phone:
                    logger.debug(f"[sched_leads] AptNum={apt_num} missing email+phone — skipping")
                    skipped += 1
                    continue

                # Skip tombstoned leads
                if email and is_deleted_lead("", email=email):
                    logger.debug(f"[sched_leads] {email} is tombstoned — skipping")
                    skipped += 1
                    continue

                # Check for existing lead
                existing = find_lead_by_identifiers(email=email, phone=phone)
                if existing:
                    logger.debug(
                        f"[sched_leads] Lead already exists for {email or phone} "
                        f"(id={existing['id'][:8]}) — skipping"
                    )
                    skipped += 1
                    continue

                # ── Parse ATTR: attribution from OD appointment Note ──────────
                attr = _parse_attr_marker(note_text)

                # ── Derive appointment type from Note (first non-ATTR line) ───
                apt_type = ""
                for line in note_text.splitlines():
                    stripped = line.strip()
                    if stripped and not stripped.startswith("ATTR:") and "=" not in stripped:
                        apt_type = stripped
                        break

                apt_datetime_str = str(apt_dt) if apt_dt else ""

                # ── Build lead payload ────────────────────────────────────────
                new_id = str(uuid.uuid4())
                created_at = datetime.now(timezone.utc).isoformat()

                lead_data = {
                    "id": new_id,
                    "created_at": created_at,
                    "source": "scheduler",
                    "stage": "scheduled",
                    "first_name": first_name,
                    "last_name": last_name,
                    "email": email,
                    "phone": phone,
                    "goals": [apt_type] if apt_type else [],
                    "notes": (
                        f"Auto-created from visitgdc.com scheduler booking. "
                        f"OD AptNum: {apt_num}. "
                        f"Appointment type: {apt_type or 'unknown'}. "
                        f"Scheduled: {apt_datetime_str[:10] if apt_datetime_str else 'unknown'}."
                    ),
                    # Attribution from ATTR: marker
                    "gclid":         attr.get("gclid", ""),
                    "fbclid":        attr.get("fbclid", ""),
                    "msclkid":       attr.get("msclkid", ""),
                    "utm_source":    attr.get("utm_source", ""),
                    "utm_medium":    attr.get("utm_medium", ""),
                    "utm_campaign":  attr.get("utm_campaign", ""),
                    "utm_term":      attr.get("utm_term", ""),
                    "utm_content":   attr.get("utm_content", ""),
                    "landing_url":   attr.get("landing_url", ""),
                    "ga4_client_id": attr.get("ga4_client_id", ""),
                    # Appointment metadata
                    "appointment_date":   apt_datetime_str[:10] if apt_datetime_str else "",
                    "appointment_status": "scheduled",
                    "od_patient_num":     pat_num,
                }

                upsert_lead(lead_data)
                add_event(new_id, "lead_created", source="od_matcher",
                          detail=json.dumps({
                              "source": "scheduler",
                              "od_apt_num": apt_num,
                              "od_pat_num": pat_num,
                              "appointment_type": apt_type,
                              "apt_datetime": apt_datetime_str,
                              "has_attribution": bool(attr.get("gclid") or attr.get("utm_campaign")),
                          }))

                logger.info(
                    f"[sched_leads] Created lead {new_id[:8]} for {email or phone} "
                    f"AptNum={apt_num} apt_type={apt_type!r} "
                    f"gclid={'yes' if attr.get('gclid') else 'no'}"
                )
                created += 1

            except Exception as e:
                logger.error(f"[sched_leads] Error processing AptNum row: {e}", exc_info=True)
                errors += 1

    finally:
        od_conn.close()

    logger.info(f"[sched_leads] Done: created={created} skipped={skipped} errors={errors}")
    return {"created": created, "skipped": skipped, "errors": errors, "total": len(rows)}


# ── Combined runner ──────────────────────────────────────────────────────────

def run_full_od_sync() -> dict:
    """Run both matching and treatment stage sync. Called by nightly scheduler."""
    match_result = match_leads_to_od()
    stage_result = sync_treatment_stages()
    direct_result = sync_scheduler_direct_leads(lookback_days=30)
    return {
        "match": match_result,
        "treatment_stages": stage_result,
        "scheduler_leads": direct_result,
    }


# ── Part 4: Match booked Mango calls → OD appointments ──────────────────────

def _normalize_phone(raw: str) -> str:
    """Strip non-digits, return last 10 digits."""
    digits = "".join(c for c in (raw or "") if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def _get_od_patient_by_phone(conn, phone_10: str) -> str | None:
    """
    Look up OD PatNum by phone number (home or wireless).
    Uses nested REPLACE chains (MySQL 5.x compatible, no REGEXP_REPLACE needed).
    Returns PatNum as string, or None if not found.
    """
    if not phone_10 or len(phone_10) < 10:
        return None

    # Normalize digits using nested REPLACE (works on all MySQL versions)
    # Strips: spaces, dashes, dots, parens, plus signs
    normalize_sql = (
        "REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE({col},' ',''),'-',''),'(',''),')',''),'.',''),'+','')"
    )
    hm_norm = normalize_sql.format(col="HmPhone")
    cell_norm = normalize_sql.format(col="WirelessPhone")

    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT PatNum FROM patient
                   WHERE PatStatus = 0
                     AND (
                         RIGHT({hm_norm}, 10) = %s
                      OR RIGHT({cell_norm}, 10) = %s
                     )
                   LIMIT 1""",
                (phone_10, phone_10),
            )
            row = cur.fetchone()
            return str(row[0]) if row else None
    except Exception as e:
        # Last-resort Python-side fallback — no row limit so we get all patients
        logger.warning(f"SQL phone lookup failed ({e}), falling back to Python normalization")
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT PatNum, HmPhone, WirelessPhone FROM patient
                       WHERE PatStatus = 0
                         AND (HmPhone != '' OR WirelessPhone != '')""",
                )
                for row in cur.fetchall():
                    for raw in [row[1] or "", row[2] or ""]:
                        if _normalize_phone(raw) == phone_10:
                            return str(row[0])
        except Exception as e2:
            logger.warning(f"Phone lookup fallback also failed: {e2}")
        return None


def _find_od_appointment_near_call(conn, pat_num: str, call_dt: datetime,
                                    forward_days: int = 14, back_days: int = 1) -> str | None:
    """
    Find the OD appointment (AptNum) for pat_num that was CREATED on or after the call
    and scheduled within a forward/back window of the call date.

    Strategy:
    - Prefer appointments created (DateTStamp) within 24h of the call — high confidence
    - Fall back to any Scheduled/ASAP appointment in the forward window
    - Last resort: recently Completed appointment (patient already came in same day)
    - AptStatus: 1=Scheduled, 2=Complete, 4=ASAP (skip 3=UnschedList, 5=Broken, 6=Planned)

    Returns AptNum as string, or None.
    """
    if not pat_num:
        return None
    if call_dt.tzinfo is None:
        call_dt = call_dt.replace(tzinfo=_EASTERN)
    call_local = call_dt.astimezone(_EASTERN).replace(tzinfo=None)
    # Window: look forward more than back (booked appointments are usually future)
    start = call_local - timedelta(days=back_days)
    end = call_local + timedelta(days=forward_days)
    # Appointments created within 24h of call = strong match signal
    created_cutoff = call_local - timedelta(hours=1)  # 1h before call (buffer for creation timing)
    created_end = call_local + timedelta(hours=24)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT AptNum, AptDateTime, AptStatus, DateTStamp
                   FROM appointment
                   WHERE PatNum = %s
                     AND AptStatus IN (1, 2, 4)
                     AND AptDateTime BETWEEN %s AND %s
                   ORDER BY DateTStamp DESC""",
                (int(pat_num), start, end),
            )
            rows = cur.fetchall()
            if not rows:
                return None

            # Phase 1: prefer appointments CREATED near the call time (DateTStamp)
            created_near = [
                r for r in rows
                if r[3] and created_cutoff <= r[3] <= created_end and int(r[2]) in (1, 4)
            ]
            if created_near:
                # Among these, pick the one scheduled soonest after the call
                best = min(created_near, key=lambda r: abs((r[1] - call_local).total_seconds()))
                logger.debug(f"[call_od_match] PatNum={pat_num} — matched via DateTStamp, AptNum={best[0]}")
                return str(best[0])

            # Phase 2: any Scheduled/ASAP appointment in window (no DateTStamp filter)
            scheduled = [(r[0], r[1]) for r in rows if int(r[2]) in (1, 4)]
            if scheduled:
                best = min(scheduled, key=lambda r: abs((r[1] - call_local).total_seconds()))
                logger.debug(f"[call_od_match] PatNum={pat_num} — matched via window (no DateTStamp), AptNum={best[0]}")
                return str(best[0])

            # Phase 3: completed appointment same day (patient came in quickly)
            completed = [(r[0], r[1]) for r in rows if int(r[2]) == 2
                         and abs((r[1] - call_local).total_seconds()) < 86400]
            if completed:
                best = min(completed, key=lambda r: abs((r[1] - call_local).total_seconds()))
                logger.debug(f"[call_od_match] PatNum={pat_num} — matched via same-day Complete, AptNum={best[0]}")
                return str(best[0])

            return None
    except Exception as e:
        logger.warning(f"OD appointment lookup failed for PatNum {pat_num}: {e}")
        return None


def match_calls_to_od_appointments(days: int = 90, target_uuid: str = None) -> dict:
    """
    For Mango calls graded as booked but not yet linked to an OD appointment:
    1. Normalize caller phone number
    2. Look up OD patient by phone
    3. Find the closest OD appointment within ±7 days of the call
    4. Store AptNum in mango_calls.od_appointment_id

    Args:
        days:         How many days back to look for unmatched booked calls
        target_uuid:  If set, only process this specific call (for on-demand triggering)

    Returns dict with matched/skipped/errors counts.
    """
    from database import get_booked_calls_needing_od_match, update_mango_call_od_appointment

    od_conn = _get_db()
    if not od_conn:
        return {"matched": 0, "skipped": 0, "errors": 1,
                "error": "OpenDental MySQL unavailable (office network required)"}

    calls = get_booked_calls_needing_od_match(days=days)
    if target_uuid:
        calls = [c for c in calls if c["uuid"] == target_uuid]

    logger.info(f"[call_od_match] Processing {len(calls)} booked calls for OD appointment match")

    matched = skipped = errors = 0

    try:
        for call in calls:
            try:
                # Guard: started_at must be a valid string
                if not call.get("started_at"):
                    logger.debug(f"[call_od_match] uuid={call.get('uuid')} — no started_at, skipping")
                    skipped += 1
                    continue

                # Get caller phone — prefer from_number, fall back to caller_id_number
                raw_phone = call.get("from_number") or call.get("caller_id_number") or ""
                phone_10 = _normalize_phone(raw_phone)
                if not phone_10:
                    logger.debug(f"[call_od_match] uuid={call['uuid']} — no phone, skipping")
                    skipped += 1
                    continue

                # Parse call datetime (stored as ISO string in UTC)
                started_str = call["started_at"].replace("Z", "+00:00")
                call_dt = datetime.fromisoformat(started_str)

                # Look up OD patient
                pat_num = _get_od_patient_by_phone(od_conn, phone_10)
                if not pat_num:
                    logger.debug(f"[call_od_match] uuid={call['uuid']} phone={phone_10} — no OD patient found")
                    skipped += 1
                    continue

                # Find appointment (forward-biased window: 14 days forward, 1 day back)
                apt_num = _find_od_appointment_near_call(od_conn, pat_num, call_dt)
                if not apt_num:
                    logger.debug(f"[call_od_match] uuid={call['uuid']} PatNum={pat_num} — no appointment in window")
                    skipped += 1
                    continue

                # Store the match
                update_mango_call_od_appointment(call["uuid"], apt_num)
                logger.info(
                    f"[call_od_match] uuid={call['uuid']} phone={phone_10} "
                    f"PatNum={pat_num} → AptNum={apt_num}"
                )
                matched += 1

            except Exception as e:
                logger.error(f"[call_od_match] Error processing call {call.get('uuid')}: {e}")
                errors += 1
    finally:
        od_conn.close()

    logger.info(f"[call_od_match] Done: matched={matched} skipped={skipped} errors={errors}")
    return {"matched": matched, "skipped": skipped, "errors": errors, "total": len(calls)}


# ── Part 5: Match Mango calls → OD patient status ────────────────────────────

def _get_od_patient_info_by_phone(conn, phone_10: str) -> dict | None:
    """
    Look up OD patient name, PatNum and PatStatus by caller phone.
    Checks both active (PatStatus=0) and inactive (PatStatus=1) patients.
    Returns dict with keys: pat_num, first_name, last_name, pat_status
    or None if not found.
    PatStatus codes: 0=Patient, 1=NonPatient, 2=Inactive, 3=Archived, 4=Deceased, 5=Prospective
    """
    if not phone_10 or len(phone_10) < 7:
        return None

    normalize_sql = (
        "REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE({col},' ',''),'-',''),'(',''),')',''),'.',''),'+','')"
    )
    hm_norm = normalize_sql.format(col="HmPhone")
    cell_norm = normalize_sql.format(col="WirelessPhone")

    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT PatNum, FName, LName, PatStatus FROM patient
                   WHERE (
                       RIGHT({hm_norm}, 10) = %s
                    OR RIGHT({cell_norm}, 10) = %s
                   )
                   AND PatStatus IN (0, 2, 3)
                   ORDER BY PatStatus ASC, PatNum DESC
                   LIMIT 1""",
                (phone_10, phone_10),
            )
            row = cur.fetchone()
            if row:
                pat_num = row[0]
                # Fetch the earliest appointment entry timestamp for this patient.
                # SecDateTEntry is set when staff books the appointment — this is the
                # reliable signal for when the patient was actually entered into OD,
                # since SecDateEntry on the patient table can reflect system defaults.
                earliest_apt_entry = None
                try:
                    cur.execute(
                        "SELECT MIN(SecDateTEntry) FROM appointment WHERE PatNum = %s",
                        (pat_num,),
                    )
                    apt_row = cur.fetchone()
                    if apt_row and apt_row[0]:
                        earliest_apt_entry = apt_row[0]  # datetime or string
                except Exception as apt_err:
                    logger.warning(f"OD earliest-apt lookup failed for PatNum {pat_num}: {apt_err}")
                return {
                    "pat_num":             str(pat_num),
                    "first_name":          (row[1] or "").strip(),
                    "last_name":           (row[2] or "").strip(),
                    "pat_status":          int(row[3]),
                    "earliest_apt_entry":  earliest_apt_entry,
                }
    except Exception as e:
        logger.warning(f"OD patient info lookup failed for phone {phone_10}: {e}")
    return None


_CNAM_NON_PERSON_PATTERNS = (
    # US state abbreviations that appear in "CITY ST" CNAM values
    " MA", " RI", " NH", " CT", " VT", " ME", " NY", " NJ",
    # Generic carrier / location strings
    "WIRELESS CALLER", "UNKNOWN", "UNAVAILABLE", "TOLL FREE", "TOLLFREE",
    "NUMBER", "CALLER",
)


def _cnam_is_person(cnam: str) -> bool:
    """Return False if CNAM looks like a city/carrier string rather than a person name."""
    if not cnam:
        return False
    upper = cnam.upper()
    for pat in _CNAM_NON_PERSON_PATTERNS:
        if pat in upper:
            return False
    # If every token is Title-case or ALL-CAPS and contains no lowercase → likely a name or city.
    # Heuristic: a person name has at least 2 tokens, none of which are a bare state abbrev.
    tokens = upper.split()
    if len(tokens) < 2:
        return False  # single-token CNAM (e.g. just "BARNSTABLE") — not a usable person name
    return True


def _names_match(caller_id_name: str, od_first: str, od_last: str) -> bool:
    """
    Return True if the caller's CNAM looks like it could be the same person as the OD record.
    Strategy:
      1. If CNAM doesn't look like a real person name (city, carrier, etc.) → can't compare,
         return True (don't override — caller is unverifiable, leave for manual review).
      2. Tokenise both sides, check if any token from the OD name appears in the caller name
         (case-insensitive, min 3 chars to avoid false positives on initials).
      3. Returns True (assume match) when either name is blank.
    """
    if not caller_id_name or not od_last:
        return True  # can't compare → don't override
    if not _cnam_is_person(caller_id_name):
        return True  # city/carrier CNAM — can't confirm mismatch, leave as-is
    caller_tokens = set(t.lower() for t in caller_id_name.split() if len(t) >= 3)
    od_tokens = set(t.lower() for t in (od_last + " " + od_first).split() if len(t) >= 3)
    return bool(caller_tokens & od_tokens)


def _classify_od_status(pat_info: dict | None, call_time=None,
                         check_recent_visit: bool = False, od_conn=None) -> str:
    """
    Classify caller as: new_patient | existing_active | existing_inactive | unknown

    Core rule: if the patient's earliest appointment entry in OD is AFTER the call
    time, the record was created in response to this call → new_patient, regardless
    of PatStatus. SecDateEntry on the patient table is unreliable (can reflect system
    defaults); SecDateTEntry on the appointment table is the authoritative timestamp.

    PatStatus=0 with earliest_apt_entry <= call_time → existing_active
    PatStatus=0 with earliest_apt_entry > call_time  → new_patient
    PatStatus=0 with no appointments at all          → new_patient (never been seen)
    PatStatus=2 (Inactive) or 3 (Archived)           → existing_inactive
    No OD match                                      → new_patient
    """
    if pat_info is None:
        return "new_patient"
    ps = pat_info.get("pat_status", -1)
    if ps == 0:
        earliest = pat_info.get("earliest_apt_entry")
        if not earliest:
            # In OD but no appointments at all → new patient
            logger.debug(f"[classify_od] PatNum={pat_info.get('pat_num')} PatStatus=0, no appointments → new_patient")
            return "new_patient"
        if call_time:
            # Normalise both to naive datetimes for comparison
            from datetime import datetime
            def _to_dt(v):
                if isinstance(v, datetime):
                    return v.replace(tzinfo=None)
                if isinstance(v, str):
                    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                        try:
                            return datetime.strptime(v[:19], fmt)
                        except ValueError:
                            continue
                return None
            earliest_dt = _to_dt(earliest)
            call_dt     = _to_dt(call_time)
            if earliest_dt and call_dt:
                if earliest_dt > call_dt:
                    logger.info(
                        f"[classify_od] PatNum={pat_info.get('pat_num')} earliest_apt={earliest_dt} "
                        f"> call_time={call_dt} → new_patient (record created after call)"
                    )
                    return "new_patient"
        return "existing_active"
    elif ps in (2, 3):
        return "existing_inactive"
    return "unknown"


def match_mango_calls_to_od_patients(limit: int = 500) -> dict:
    """
    Match unmatched inbound Mango calls to OpenDental patients by phone number.
    Sets od_patient_num, od_patient_status, od_patient_name on each call.

    od_patient_status values:
      new_patient       — phone not found in OD (likely a new patient inquiry)
      existing_active   — PatStatus=0 active patient
      existing_inactive — PatStatus=2/3 inactive/archived patient
      unknown           — OD found but status unclear (shouldn't happen often)

    This is used to:
      1. Show New/Existing badge in the call inbox
      2. Gate offline conversion uploads (skip existing patients)
    """
    from database import get_mango_calls_needing_od_match, update_mango_call_od_status

    od_conn = _get_db()
    if od_conn is None:
        logger.warning("[mango_od_match] OpenDental unavailable — skipping patient match")
        return {"matched": 0, "new_patient": 0, "existing": 0, "errors": 0, "skipped": 0,
                "detail": "OpenDental unavailable"}

    calls = get_mango_calls_needing_od_match(limit=limit)
    logger.info(f"[mango_od_match] Matching {len(calls)} calls to OD patients")

    new_patient = existing = errors = skipped = 0

    try:
        for call in calls:
            try:
                raw_phone = call.get("from_number") or ""
                phone_10 = _normalize_phone(raw_phone)

                if not phone_10:
                    # Mark as attempted so we don't retry empty-phone calls forever
                    update_mango_call_od_status(call["uuid"], "", "unknown", "")
                    skipped += 1
                    continue

                pat_info = _get_od_patient_info_by_phone(od_conn, phone_10)
                caller_name = call.get("caller_id_name") or ""
                # Name-mismatch guard: if OD found a record but the caller's CNAM shares no
                # name token with the OD patient, treat as new_patient (phone reuse / family
                # member / recycled number).
                if pat_info and not _names_match(
                    caller_name,
                    pat_info.get("first_name", ""),
                    pat_info.get("last_name", ""),
                ):
                    logger.info(
                        f"[mango_od_match] uuid={call['uuid']} phone={phone_10} "
                        f"OD name='{pat_info.get('first_name','')} {pat_info.get('last_name','')}' "
                        f"caller='{caller_name}' → name mismatch, treating as new_patient"
                    )
                    pat_info = None  # force new_patient classification
                status = _classify_od_status(pat_info, call_time=call.get("started_at"))
                pat_num = pat_info["pat_num"] if pat_info else ""
                pat_name = ""
                if pat_info:
                    fn = pat_info.get("first_name", "")
                    ln = pat_info.get("last_name", "")
                    pat_name = f"{fn} {ln}".strip()

                update_mango_call_od_status(call["uuid"], pat_num, status, pat_name)

                if status == "new_patient":
                    new_patient += 1
                    logger.debug(f"[mango_od_match] uuid={call['uuid']} phone={phone_10} → new_patient")
                else:
                    existing += 1
                    logger.debug(
                        f"[mango_od_match] uuid={call['uuid']} phone={phone_10} "
                        f"→ {status} PatNum={pat_num} Name={pat_name}"
                    )

            except Exception as e:
                logger.error(f"[mango_od_match] Error for uuid={call.get('uuid')}: {e}")
                errors += 1
    finally:
        od_conn.close()

    total = len(calls)
    logger.info(
        f"[mango_od_match] Done: total={total} new={new_patient} "
        f"existing={existing} errors={errors} skipped={skipped}"
    )
    return {
        "total": total,
        "new_patient": new_patient,
        "existing": existing,
        "errors": errors,
        "skipped": skipped,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = run_full_od_sync()
    print(json.dumps(result, indent=2))
