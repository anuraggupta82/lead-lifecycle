"""
OpenDental patient matcher — matches leads to OD patients by phone/email hash.
Also syncs treatment plan stages for matched patients.

Runs as a nightly job. Never stores raw PHI — uses SHA-256 hashes for comparison.
Only accessible on the office LAN (GraftonServer).

Six functions:
  1. match_leads_to_od()                  — match unmatched leads to OD patients
  2. sync_treatment_stages()              — update stages for already-matched leads
  3. sync_scheduler_direct_leads()        — DEPRECATED (OD-note grep, broken since May 19 2026)
  4. sync_scheduler_bookings_via_api()    — PR 2: replaces #3; pulls posted_appointments via scheduler API
  5. run_full_od_sync()                   — runs 1+2+4 (called by nightly scheduler)
  6. match_calls_to_od_appointments()     — link booked Mango calls → OD appointments
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
                       LOWER(Email)         AS email,
                       LOWER(WkPhone)       AS work_phone,
                       LOWER(FName)         AS first_name,
                       LOWER(LName)         AS last_name
                FROM patient
                WHERE PatStatus = 0
                  AND (HmPhone != '' OR WirelessPhone != '' OR Email != ''
                       OR WkPhone != '' OR FName != '' OR LName != '')
            """)
            od_patients = cur.fetchall()

        # Build lookup maps: {hash: PatNum} and name maps
        phone_map = {}
        email_map = {}
        # name_map: {(first, last): [PatNum, ...]} — list to detect ambiguity
        name_map  = {}
        # pat_data: {PatNum: {phone_hashes, email_hash, first, last}}
        pat_data  = {}

        for row in od_patients:
            pat_num    = str(row[0])
            home_phone = row[1] or ""
            cell_phone = row[2] or ""
            email_raw  = row[3] or ""
            work_phone = row[4] or ""
            first_name = (row[5] or "").strip().lower()
            last_name  = (row[6] or "").strip().lower()

            phone_hashes = set()
            for phone_raw in [home_phone, cell_phone, work_phone]:
                digits = "".join(c for c in phone_raw if c.isdigit())
                if len(digits) >= 10:
                    h1 = _hash(digits[-10:])
                    h2 = _hash("1" + digits[-10:])
                    phone_map[h1] = pat_num
                    phone_map[h2] = pat_num
                    phone_hashes.update([h1, h2])

            email_h = None
            if email_raw:
                email_h = _hash(email_raw.strip().lower())
                email_map[email_h] = pat_num

            if first_name and last_name:
                key = (first_name, last_name)
                name_map.setdefault(key, []).append(pat_num)

            pat_data[pat_num] = {
                "phone_hashes": phone_hashes,
                "email_hash":   email_h,
                "first":        first_name,
                "last":         last_name,
            }

        def _secondary_match(pat_num, lead):
            """Return True if lead's phone or email matches this OD patient."""
            pd = pat_data.get(pat_num, {})
            if lead.get("phone_hash") and lead["phone_hash"] in pd.get("phone_hashes", set()):
                return True
            if lead.get("email_hash") and lead["email_hash"] == pd.get("email_hash"):
                return True
            return False

        for lead in unmatched:
            try:
                pat_num = None
                match_method = None

                lead_first = (lead.get("first_name") or "").strip().lower()
                lead_last  = (lead.get("last_name")  or "").strip().lower()

                # ── Tier 1: Full name match + secondary verification ──────────
                # Only attempt if both first and last are present and look like
                # real names (skip caller-ID values like "WESTBOROUGH MA")
                if (lead_first and lead_last
                        and not any(c.isdigit() for c in lead_first + lead_last)
                        and len(lead_last) > 1):
                    name_key = (lead_first, lead_last)
                    candidates = name_map.get(name_key, [])
                    if len(candidates) == 1:
                        # Unique full name — verify with phone or email
                        if _secondary_match(candidates[0], lead):
                            pat_num      = candidates[0]
                            match_method = "full_name+secondary"
                        else:
                            # Unique name but no secondary match — still use it
                            # (manually scheduled patients may lack phone/email in OD)
                            pat_num      = candidates[0]
                            match_method = "full_name_only"
                    elif len(candidates) > 1:
                        # Ambiguous name — require secondary to disambiguate
                        for c in candidates:
                            if _secondary_match(c, lead):
                                pat_num      = c
                                match_method = "full_name+secondary"
                                break

                # ── Tier 2: Phone match ───────────────────────────────────────
                if not pat_num and lead.get("phone_hash") and lead["phone_hash"] in phone_map:
                    pat_num      = phone_map[lead["phone_hash"]]
                    match_method = "phone"

                # ── Tier 3: Email match ───────────────────────────────────────
                if not pat_num and lead.get("email_hash") and lead["email_hash"] in email_map:
                    pat_num      = email_map[lead["email_hash"]]
                    match_method = "email"

                # ── Tier 4: Last name only (unique + secondary) ───────────────
                if not pat_num and lead_last and len(lead_last) > 2:
                    last_candidates = [
                        pn for (fn, ln), pns in name_map.items()
                        if ln == lead_last for pn in pns
                    ]
                    if len(last_candidates) == 1 and _secondary_match(last_candidates[0], lead):
                        pat_num      = last_candidates[0]
                        match_method = "last_name+secondary"

                if not pat_num:
                    continue

                # Get production, treatment plan, and appointment info
                production = _get_patient_production(conn, pat_num)
                tp_status  = _get_treatment_plan_status(conn, pat_num)
                apt_info   = _get_appointment_info(conn, pat_num)
                od_rel     = _compute_od_relationship(tp_status, apt_info)

                # PR 5: "existing patient" = has ≥1 completed (AptStatus=2) appointment in OD.
                # has_showed is set by _get_appointment_info when any AptStatus=2 row exists.
                # Sticky-upgrade: CASE WHEN prevents overwriting 1→0 on subsequent runs.
                existing_patient = 1 if apt_info.get("has_showed") else 0

                # Update lead in SQLite
                lconn = _get_sqlite()
                now = datetime.now(timezone.utc).isoformat()
                lconn.execute("""
                    UPDATE leads
                    SET od_patient_num=?, od_matched_at=?, attributed_production=?,
                        od_relationship=?,
                        existing_patient = CASE WHEN ? = 1 THEN 1 ELSE existing_patient END,
                        updated_at=?
                    WHERE id=?
                """, (pat_num, now, production["total"], od_rel,
                      existing_patient, now, lead["id"]))
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

                # ── PR 4: Self-Booked flag ────────────────────────────────────
                # If the lead has GAds attribution AND the matched campaign in
                # the pipeline DB has auto_enter_pipeline_rule='always', mark
                # self_booked=1. Indicates the patient self-scheduled online
                # (positive signal vs. calling in).
                self_booked = 0
                utm_campaign_attr = (attr.get("utm_campaign") or "").strip()
                if utm_campaign_attr:
                    try:
                        from contextlib import closing
                        from database import _conn as _local_conn
                        with closing(_local_conn()) as _lc:
                            _row = _lc.execute(
                                "SELECT auto_enter_pipeline_rule FROM campaigns "
                                "WHERE LOWER(campaign_name)=LOWER(?) LIMIT 1",
                                (utm_campaign_attr,)
                            ).fetchone()
                        if _row and (_row[0] == 'always'):
                            self_booked = 1
                    except Exception as _sb_e:
                        logger.debug(f"[sched_leads] self_booked lookup failed for "
                                     f"utm_campaign={utm_campaign_attr!r}: {_sb_e}")

                # ── Derive appointment type from Note (first non-ATTR line) ───
                apt_type = ""
                for line in note_text.splitlines():
                    stripped = line.strip()
                    if stripped and not stripped.startswith("ATTR:") and "=" not in stripped:
                        apt_type = stripped
                        break

                apt_datetime_str = str(apt_dt) if apt_dt else ""

                # ── PR 5: existing_patient detection ──────────────────────────
                # A patient is "existing" if they have ≥1 completed appointment (AptStatus=2)
                # already in OD. The appointment we're processing here is the NEW one the
                # scheduler just booked (AptStatus=1 Scheduled), so has_showed only catches
                # prior visits. Existing-patient leads stay in DB but are hidden from the
                # default pipeline view via _pipeline_visibility_clause().
                existing_patient = 0
                try:
                    _apt_info = _get_appointment_info(od_conn, pat_num)
                    if _apt_info.get("has_showed"):
                        existing_patient = 1
                except Exception as _ep_err:
                    logger.debug(f"[sched_leads] existing_patient check failed for "
                                 f"PatNum={pat_num}: {_ep_err}")

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
                    "self_booked":        self_booked,
                    "existing_patient":   existing_patient,
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
                              "self_booked": bool(self_booked),
                              "existing_patient": bool(existing_patient),
                          }))

                logger.info(
                    f"[sched_leads] Created lead {new_id[:8]} for {email or phone} "
                    f"AptNum={apt_num} apt_type={apt_type!r} "
                    f"gclid={'yes' if attr.get('gclid') else 'no'} "
                    f"self_booked={self_booked} existing_patient={existing_patient}"
                )
                created += 1

            except Exception as e:
                logger.error(f"[sched_leads] Error processing AptNum row: {e}", exc_info=True)
                errors += 1

    finally:
        od_conn.close()

    logger.info(f"[sched_leads] Done: created={created} skipped={skipped} errors={errors}")
    return {"created": created, "skipped": skipped, "errors": errors, "total": len(rows)}


# ── PR 2 (Option B): API-based scheduler booking sync ────────────────────────

def sync_scheduler_bookings_via_api(lookback_days: int = 60) -> dict:
    """Pull posted_appointments from the scheduler's Cloud Run API and reconcile
    them into the marketing pipeline.

    Replaces sync_scheduler_direct_leads (OD-note ATTR: grep), which has been
    permanently broken since scheduler commit 6e4d671 removed _build_attr_marker
    on May 19 2026. The scheduler's posted_appointments table still stores full
    attribution (gclid/UTM) via _sanitize_attribution(); this function reads it
    via the new GET /api/admin/internal/bookings endpoint.

    Match order (idempotent — safe to re-run):
      1. od_patient_num match (exact, strongest — scheduler resolved OD PatNum)
      2. Email match via find_lead_by_identifiers
      3. No match → create new 'scheduled' lead

    Attribution backfill rule: only fill empty fields — never overwrite an
    existing gclid/utm that the lead already carries from its web-form origin.
    """
    import os
    import uuid
    import requests as _requests

    from database import (
        find_lead_by_identifiers, get_lead_by_od_pat_num,
        upsert_lead, is_deleted_lead,
    )
    from contextlib import closing
    from database import _conn as _local_conn

    base = (os.environ.get("SCHEDULER_API") or "").rstrip("/")
    key  = (
        os.environ.get("SCHEDULER_INTERNAL_KEY")
        or os.environ.get("SCHEDULER_ADMIN_PASSWORD")
        or ""
    )
    if not base:
        logger.warning("[sched_api] SCHEDULER_API not configured — skipping")
        return {"created": 0, "updated": 0, "skipped": 0, "errors": 0,
                "error": "SCHEDULER_API not set"}

    # Use naive UTC to match the scheduler's stored created_at format (datetime.utcnow()).
    # Aware ISO (+00:00 suffix) sorts lexicographically wrong against naive strings.
    since = (datetime.utcnow() - timedelta(days=lookback_days)).isoformat()
    try:
        resp = _requests.get(
            f"{base}/api/admin/internal/bookings",
            params={"since": since, "limit": 1000},
            headers={"X-Internal-Key": key},
            timeout=30,
        )
        resp.raise_for_status()
        bookings = resp.json().get("bookings", [])
    except Exception as e:
        logger.warning(f"[sched_api] Failed to fetch bookings from scheduler: {e}")
        return {"created": 0, "updated": 0, "skipped": 0, "errors": 0, "error": str(e)}

    logger.info(f"[sched_api] Fetched {len(bookings)} bookings since {since[:10]}")

    created = updated = skipped = errors = 0

    for b in bookings:
        try:
            od_pat_num = str(b.get("od_pat_num") or "").strip()
            email      = (b.get("patient_email") or "").strip().lower()
            name_parts = (b.get("patient_name") or "").strip().split()
            first_name = name_parts[0] if name_parts else ""
            last_name  = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""
            apt_dt     = b.get("apt_datetime") or ""
            apt_type   = b.get("appointment_type") or ""

            attr = {k: (b.get(k) or "") for k in (
                "gclid", "utm_source", "utm_medium", "utm_campaign",
                "utm_term", "utm_content", "landing_referrer", "ga4_client_id",
            )}

            if not email and not od_pat_num:
                logger.debug(f"[sched_api] Booking {b.get('stripe_session_id')} has no email/od_pat_num — skipping")
                skipped += 1
                continue

            if email and is_deleted_lead("", email=email):
                logger.debug(f"[sched_api] {email} is tombstoned — skipping")
                skipped += 1
                continue

            # ── Tier 1: match by od_patient_num ──────────────────────────────
            lead = get_lead_by_od_pat_num(od_pat_num) if od_pat_num else None

            # ── Tier 2: match by email ────────────────────────────────────────
            if not lead and email:
                lead = find_lead_by_identifiers(email=email, phone="")

            if lead:
                # Backfill empty attribution fields only — never overwrite
                now = datetime.now(timezone.utc).isoformat()
                sets, vals = [], []
                for col in ("gclid", "utm_source", "utm_medium", "utm_campaign",
                             "utm_term", "utm_content", "ga4_client_id"):
                    if attr.get(col) and not (lead.get(col) or ""):
                        sets.append(f"{col}=?"); vals.append(attr[col])
                if attr.get("landing_referrer") and not (lead.get("landing_url") or ""):
                    sets.append("landing_url=?"); vals.append(attr["landing_referrer"])
                if od_pat_num and not (lead.get("od_patient_num") or ""):
                    sets.append("od_patient_num=?"); vals.append(od_pat_num)
                # Always refresh appointment metadata
                sets += ["appointment_date=?", "appointment_status=?", "updated_at=?"]
                vals += [apt_dt[:10] if apt_dt else "", "scheduled", now]
                vals.append(lead["id"])

                with closing(_local_conn()) as lc:
                    lc.execute(f"UPDATE leads SET {', '.join(sets)} WHERE id=?", vals)

                add_event(
                    lead["id"], "scheduler_booking_reconciled",
                    source="od_matcher",
                    detail=json.dumps({
                        "stripe_session_id": b.get("stripe_session_id"),
                        "od_apt_num": b.get("od_apt_num"),
                        "had_attribution": bool(attr.get("gclid") or attr.get("utm_campaign")),
                    }),
                )
                logger.debug(f"[sched_api] Updated lead {lead['id'][:8]} for {email or od_pat_num}")
                updated += 1

            else:
                # ── Create new lead ───────────────────────────────────────────
                self_booked = 0
                if attr.get("utm_campaign"):
                    try:
                        with closing(_local_conn()) as lc:
                            _row = lc.execute(
                                "SELECT auto_enter_pipeline_rule FROM campaigns "
                                "WHERE LOWER(campaign_name)=LOWER(?) LIMIT 1",
                                (attr["utm_campaign"],),
                            ).fetchone()
                        if _row and _row[0] == "always":
                            self_booked = 1
                    except Exception as _e:
                        logger.debug(f"[sched_api] self_booked lookup failed: {_e}")

                new_id = str(uuid.uuid4())
                now = datetime.now(timezone.utc).isoformat()
                lead_data = {
                    "id": new_id,
                    "created_at": now,
                    "source": "scheduler",
                    "stage": "scheduled",
                    "first_name": first_name,
                    "last_name": last_name,
                    "email": email,
                    "phone": "",
                    "goals": [apt_type] if apt_type else [],
                    "notes": (
                        f"Auto-created from visitgdc.com scheduler booking via API sync. "
                        f"Stripe session: {b.get('stripe_session_id') or 'unknown'}. "
                        f"Appointment type: {apt_type or 'unknown'}. "
                        f"Scheduled: {apt_dt[:10] if apt_dt else 'unknown'}."
                    ),
                    "gclid":              attr.get("gclid", ""),
                    "utm_source":         attr.get("utm_source", ""),
                    "utm_medium":         attr.get("utm_medium", ""),
                    "utm_campaign":       attr.get("utm_campaign", ""),
                    "utm_term":           attr.get("utm_term", ""),
                    "utm_content":        attr.get("utm_content", ""),
                    "landing_url":        attr.get("landing_referrer", ""),
                    "ga4_client_id":      attr.get("ga4_client_id", ""),
                    "appointment_date":   apt_dt[:10] if apt_dt else "",
                    "appointment_status": "scheduled",
                    "od_patient_num":     od_pat_num,
                    "self_booked":        self_booked,
                    "existing_patient":   0,  # unknown without OD; sync_treatment_stages corrects next run
                }
                upsert_lead(lead_data)
                add_event(
                    new_id, "lead_created",
                    source="od_matcher",
                    detail=json.dumps({
                        "source": "scheduler_api",
                        "stripe_session_id": b.get("stripe_session_id"),
                        "od_pat_num": od_pat_num,
                        "appointment_type": apt_type,
                        "apt_datetime": apt_dt,
                        "has_attribution": bool(attr.get("gclid") or attr.get("utm_campaign")),
                        "self_booked": bool(self_booked),
                    }),
                )
                logger.info(
                    f"[sched_api] Created lead {new_id[:8]} for {email or od_pat_num} "
                    f"apt_type={apt_type!r} gclid={'yes' if attr.get('gclid') else 'no'} "
                    f"self_booked={self_booked}"
                )
                created += 1

        except Exception as e:
            logger.error(f"[sched_api] Error on booking {b.get('stripe_session_id')}: {e}", exc_info=True)
            errors += 1

    logger.info(f"[sched_api] Done — created={created} updated={updated} skipped={skipped} errors={errors}")
    return {"created": created, "updated": updated, "skipped": skipped,
            "errors": errors, "total": len(bookings)}


# ── Combined runner ──────────────────────────────────────────────────────────

def run_full_od_sync() -> dict:
    """Run both matching and treatment stage sync. Called by nightly scheduler."""
    match_result = match_leads_to_od()
    stage_result = sync_treatment_stages()
    # PR 2 (Jul 2026): replaced sync_scheduler_direct_leads (OD-note ATTR: grep, broken
    # since commit 6e4d671) with sync_scheduler_bookings_via_api (reads posted_appointments
    # via scheduler's new /api/admin/internal/bookings endpoint).
    direct_result = sync_scheduler_bookings_via_api(lookback_days=60)
    callrail_result = enrich_callrail_calls_with_od(limit=500)
    return {
        "match": match_result,
        "treatment_stages": stage_result,
        "scheduler_leads": direct_result,
        "callrail_od_enrich": callrail_result,
    }


# ── Part 4: Match booked Mango calls → OD appointments ──────────────────────

def _normalize_phone(raw: str) -> str:
    """Strip non-digits, return last 10 digits."""
    digits = "".join(c for c in (raw or "") if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def _get_od_patient_by_name(conn, full_name: str) -> tuple[str | None, str]:
    """
    Look up OD PatNum by patient name from Gemini ai_patient_name.
    Accepts full name ("Matthew Cornwell") or first name only ("Matthew").
    Returns (PatNum | None, confidence) where confidence is one of:
      "full_name"   — first + last matched exactly → highest confidence
      "last_name"   — last name only, single unambiguous match
      "first_name"  — first name only, single unambiguous match → lowest
      "none"        — no match or ambiguous

    Tiered strategy (stops at first success):
      Tier 1: LOWER(FName)=first AND LOWER(LName)=last  → unambiguous regardless of count
      Tier 2: LOWER(LName)=last                         → only if exactly 1 active match
      Tier 3: LOWER(FName)=first                        → only if exactly 1 active match

    Caller's identity is confirmed by Gemini reading the transcript.
    This is just to find the right OD record to link.
    """
    if not full_name or len(full_name.strip()) < 2:
        return None, "none"

    tokens = full_name.strip().split()
    first = tokens[0].lower() if tokens else ""
    last  = tokens[-1].lower() if len(tokens) >= 2 else ""

    try:
        with conn.cursor() as cur:
            # ── Tier 1: full name match (first + last) ────────────────────────
            if first and last:
                cur.execute(
                    """SELECT PatNum FROM patient
                       WHERE PatStatus = 0
                         AND LOWER(FName) = %s
                         AND LOWER(LName) = %s
                       ORDER BY PatNum DESC LIMIT 5""",
                    (first, last),
                )
                rows = cur.fetchall()
                if len(rows) == 1:
                    logger.info(f"[call_od_match] Name lookup '{full_name}' → Tier 1 full match PatNum={rows[0][0]}")
                    return str(rows[0][0]), "full_name"
                if len(rows) > 1:
                    # Multiple patients share same first+last — refuse to guess.
                    # Linking the wrong family member would corrupt income attribution.
                    logger.info(f"[call_od_match] Name lookup '{full_name}' → Tier 1 {len(rows)} matches (ambiguous) — skipping")
                    return None, "none"

            # ── Tier 2: last name only ────────────────────────────────────────
            # Skip Tier 2 when Gemini gave us both first AND last name but Tier 1
            # failed — using last name alone would risk matching a different family
            # member. Only use Tier 2 when we have last name but no first name.
            if last and not first:
                cur.execute(
                    """SELECT PatNum FROM patient
                       WHERE PatStatus = 0
                         AND LOWER(LName) = %s
                       ORDER BY PatNum DESC LIMIT 5""",
                    (last,),
                )
                rows = cur.fetchall()
                if len(rows) == 1:
                    logger.info(f"[call_od_match] Name lookup '{full_name}' → Tier 2 last-name match PatNum={rows[0][0]}")
                    return str(rows[0][0]), "last_name"
                if len(rows) > 1:
                    logger.info(f"[call_od_match] Name lookup last='{last}' → {len(rows)} matches, ambiguous — skipping")

            # ── Tier 3: first name only ───────────────────────────────────────
            if first:
                cur.execute(
                    """SELECT PatNum FROM patient
                       WHERE PatStatus = 0
                         AND LOWER(FName) = %s
                       ORDER BY PatNum DESC LIMIT 5""",
                    (first,),
                )
                rows = cur.fetchall()
                if len(rows) == 1:
                    logger.info(f"[call_od_match] Name lookup '{full_name}' → Tier 3 first-name-only match PatNum={rows[0][0]}")
                    return str(rows[0][0]), "first_name"
                if len(rows) > 1:
                    logger.info(f"[call_od_match] Name lookup first='{first}' → {len(rows)} matches, ambiguous — skipping")

        return None, "none"

    except Exception as e:
        logger.warning(f"OD name lookup failed for '{full_name}': {e}")
        return None, "none"


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


def _get_family_members(conn, guarantor_patnum: str) -> list[dict]:
    """
    Given a guarantor's PatNum, return all family members (including the guarantor).
    Each dict has: pat_num, first_name, last_name, pat_status.
    Only returns active (0) and inactive (2) patients.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT PatNum, FName, LName, PatStatus
                   FROM patient
                   WHERE Guarantor = %s
                     AND PatStatus IN (0, 2)
                   ORDER BY PatNum ASC""",
                (int(guarantor_patnum),),
            )
            return [
                {
                    "pat_num": str(r[0]),
                    "first_name": (r[1] or "").strip(),
                    "last_name": (r[2] or "").strip(),
                    "pat_status": int(r[3]),
                }
                for r in cur.fetchall()
            ]
    except Exception as e:
        logger.warning(f"[guarantor] Family lookup failed for guarantor {guarantor_patnum}: {e}")
        return []


def _resolve_guarantor_family(
    conn,
    matched_patnum: str,
    caller_name: str = "",
    ai_patient_name: str = "",
    call_dt: datetime | None = None,
) -> tuple[str, str]:
    """
    When a phone match returns a patient, check if they are a guarantor with
    dependents. If the caller/patient name doesn't match the guarantor, try to
    find the correct family member.

    Resolution priority:
      1. ai_patient_name (from Gemini transcript) — most reliable
      2. caller_name (caller ID / CNAM) — less reliable but available early
      3. Appointment proximity — if a dependent has an appointment near call_dt

    Returns (resolved_patnum, resolution_method):
      - (original, "self")         — matched patient IS the right person
      - (dependent, "family_name") — resolved via name match to a dependent
      - (dependent, "family_appt") — resolved via appointment proximity
      - (original, "family_ambiguous") — has dependents but can't disambiguate
    """
    # Check if matched patient is a guarantor (Guarantor == self)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT Guarantor, FName, LName FROM patient WHERE PatNum = %s",
                (int(matched_patnum),),
            )
            row = cur.fetchone()
            if not row:
                return matched_patnum, "self"
            guarantor_id, matched_fname, matched_lname = (
                str(row[0]),
                (row[1] or "").strip(),
                (row[2] or "").strip(),
            )
    except Exception as e:
        logger.warning(f"[guarantor] Guarantor check failed for {matched_patnum}: {e}")
        return matched_patnum, "self"

    # Only proceed if matched patient IS the guarantor (head of family)
    if guarantor_id != matched_patnum:
        return matched_patnum, "self"  # matched patient is a dependent, not the guarantor

    # Get family members (including the guarantor)
    family = _get_family_members(conn, matched_patnum)
    dependents = [m for m in family if m["pat_num"] != matched_patnum]

    if not dependents:
        return matched_patnum, "self"  # no dependents, guarantor IS the patient

    # ── Resolution 1: Match by ai_patient_name (Gemini transcript) ──────────
    name_to_check = ai_patient_name.strip() if ai_patient_name else ""
    if not name_to_check and caller_name:
        # Fall back to caller_name only if it looks like a real person name
        if _cnam_is_person(caller_name):
            name_to_check = caller_name.strip()

    if name_to_check:
        name_tokens = set(t.lower() for t in name_to_check.split() if len(t) >= 3)
        # Extract first name token specifically for disambiguation
        _name_parts = name_to_check.strip().split()
        _check_first = _name_parts[0].lower() if _name_parts else ""

        if name_tokens:
            # First check: does the FIRST NAME match the guarantor themselves?
            # Can't just use token intersection — family members share last names,
            # so "Emily Grondin" would match Christine Grondin on "grondin" alone.
            _guar_first = matched_fname.lower().strip()
            if _check_first and _guar_first and len(_check_first) >= 3 and _check_first == _guar_first:
                logger.debug(
                    f"[guarantor] First name '{_check_first}' matches guarantor "
                    f"{matched_fname} {matched_lname} (PatNum={matched_patnum})"
                )
                return matched_patnum, "self"

            # Check each dependent — require first name match, not just any token
            name_matches = []
            for dep in dependents:
                _dep_first = dep["first_name"].lower().strip()
                if _check_first and _dep_first and len(_dep_first) >= 3 and _check_first == _dep_first:
                    name_matches.append(dep)
                elif not _check_first or len(_check_first) < 3:
                    # No usable first name — fall back to any-token match
                    dep_tokens = set(
                        t.lower()
                        for t in f"{dep['first_name']} {dep['last_name']}".split()
                        if len(t) >= 3
                    )
                    if name_tokens & dep_tokens:
                        name_matches.append(dep)

            if len(name_matches) == 1:
                dep = name_matches[0]
                logger.info(
                    f"[guarantor] Resolved guarantor {matched_patnum} → "
                    f"dependent {dep['pat_num']} ({dep['first_name']} {dep['last_name']}) "
                    f"via name match '{name_to_check}'"
                )
                return dep["pat_num"], "family_name"
            elif len(name_matches) > 1:
                logger.info(
                    f"[guarantor] Name '{name_to_check}' matched {len(name_matches)} "
                    f"dependents of guarantor {matched_patnum} — ambiguous"
                )
                # Fall through to appointment check

    # ── Resolution 2: Match by appointment proximity ────────────────────────
    if call_dt:
        try:
            # Check which family member has an appointment near the call date
            apt_matches = []
            for member in family:
                apt = _find_od_appointment_near_call(conn, member["pat_num"], call_dt)
                if apt:
                    apt_matches.append((member, apt))

            if len(apt_matches) == 1:
                member, apt = apt_matches[0]
                logger.info(
                    f"[guarantor] Resolved guarantor {matched_patnum} → "
                    f"{member['pat_num']} ({member['first_name']} {member['last_name']}) "
                    f"via appointment proximity (AptNum={apt})"
                )
                return member["pat_num"], "family_appt"
            elif len(apt_matches) > 1:
                # Multiple family members have appointments near the call — can't disambiguate
                logger.info(
                    f"[guarantor] {len(apt_matches)} family members of guarantor "
                    f"{matched_patnum} have appointments near call — ambiguous"
                )
        except Exception as e:
            logger.warning(f"[guarantor] Appointment proximity check failed: {e}")

    # Can't disambiguate — return original with a flag
    logger.info(
        f"[guarantor] Guarantor {matched_patnum} ({matched_fname} {matched_lname}) "
        f"has {len(dependents)} dependent(s) but can't disambiguate caller"
    )
    return matched_patnum, "family_ambiguous"


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


def _get_od_appointment_type(conn, apt_num: str) -> str:
    """
    Fetch appointment type label for a given AptNum.
    Priority:
      1. appointmenttype.AppointmentTypeName (when AppointmentTypeNum > 0)
      2. appointment.ProcDescript (procedure codes/names assigned to the slot)
      3. "" (empty — caller will fall back to Gemini's ai_appointment_type in UI)
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT a.AppointmentTypeNum, a.ProcDescript,
                          COALESCE(at.AppointmentTypeName, '') AS type_name
                   FROM appointment a
                   LEFT JOIN appointmenttype at
                          ON at.AppointmentTypeNum = a.AppointmentTypeNum
                   WHERE a.AptNum = %s
                   LIMIT 1""",
                (int(apt_num),),
            )
            row = cur.fetchone()
            if not row:
                return ""
            type_num, proc_descript, type_name = row
            if type_num and int(type_num) > 0 and type_name:
                return type_name.strip()
            if proc_descript and proc_descript.strip():
                return proc_descript.strip()
            return ""
    except Exception as e:
        logger.warning(f"[call_od_match] _get_od_appointment_type failed for AptNum={apt_num}: {e}")
        return ""


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
    from database import (
        get_booked_calls_needing_od_match,
        update_mango_call_od_appointment,
        update_mango_call_analysis,
        update_mango_call_od_income,
    )

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

                # Look up OD patient — phone first, then Gemini name fallback
                ai_patient_name = (call.get("ai_patient_name") or "").strip()
                caller_id_name  = (call.get("caller_id_name") or "").strip()
                match_method    = "phone"

                pat_num = _get_od_patient_by_phone(od_conn, phone_10)

                # §2.3k: Guarantor resolution — if phone matched a guarantor,
                # check family members using ai_patient_name or appointment proximity
                if pat_num:
                    resolved_pat, resolve_method = _resolve_guarantor_family(
                        od_conn, pat_num,
                        caller_name=caller_id_name,
                        ai_patient_name=ai_patient_name,
                        call_dt=call_dt,
                    )
                    if resolved_pat != pat_num:
                        logger.info(
                            f"[call_od_match] uuid={call['uuid']} guarantor {pat_num} → "
                            f"dependent {resolved_pat} (method={resolve_method})"
                        )
                        pat_num = resolved_pat
                        match_method = f"phone+{resolve_method}"

                if not pat_num and ai_patient_name:
                    # Phone lookup failed — try Gemini's patient name from transcript.
                    # Gemini read the actual conversation so it knows the real patient,
                    # even when the caller (phone account holder) is a different person.
                    pat_num, name_tier = _get_od_patient_by_name(od_conn, ai_patient_name)
                    if pat_num:
                        # Confidence:
                        #   phone+name agree → high (caller IS the patient, name confirmed)
                        #   phone failed, full name matched → gemini-wins (name-based)
                        #   phone failed, last-name only → name-last
                        #   phone failed, first-name only → name-first (weakest)
                        cnam_tokens = set(t.lower() for t in caller_id_name.split() if len(t) >= 3)
                        name_tokens = set(t.lower() for t in ai_patient_name.split() if len(t) >= 3)
                        cnam_matches = bool(cnam_tokens & name_tokens)
                        if cnam_matches:
                            conf = "high"
                        elif name_tier == "full_name":
                            conf = "gemini-wins"
                        elif name_tier == "last_name":
                            conf = "name-last"
                        else:
                            conf = "name-first"
                        match_method = f"ai_name({ai_patient_name}, tier={name_tier}, conf={conf})"
                        logger.info(
                            f"[call_od_match] uuid={call['uuid']} phone={phone_10} — "
                            f"phone miss, matched via Gemini name '{ai_patient_name}' "
                            f"tier={name_tier} conf={conf} → PatNum={pat_num}"
                        )

                if not pat_num:
                    logger.debug(
                        f"[call_od_match] uuid={call['uuid']} phone={phone_10} "
                        f"ai_name='{ai_patient_name}' — no OD patient found"
                    )
                    # Stamp attempt timestamp so throttle prevents infinite re-querying
                    _now_iso = datetime.now(timezone.utc).isoformat()
                    update_mango_call_analysis(call["uuid"], od_match_attempted_at=_now_iso)
                    skipped += 1
                    continue

                # Find appointment (forward-biased window: 14 days forward, 1 day back)
                apt_num = _find_od_appointment_near_call(od_conn, pat_num, call_dt)
                if not apt_num:
                    logger.debug(f"[call_od_match] uuid={call['uuid']} PatNum={pat_num} — no appointment in window")
                    # M1 fix: stamp attempt timestamp
                    _now_iso = datetime.now(timezone.utc).isoformat()
                    update_mango_call_analysis(call["uuid"], od_match_attempted_at=_now_iso)
                    skipped += 1
                    continue

                # Fetch OD appointment type: AppointmentTypeName → ProcDescript fallback
                od_appt_type = _get_od_appointment_type(od_conn, apt_num)

                # Store the match + stamp attempt timestamp
                # Also write od_patient_num if matched via name (phone-matched calls
                # already have od_patient_num set from the patient enrichment step)
                _now_iso = datetime.now(timezone.utc).isoformat()
                update_mango_call_od_appointment(call["uuid"], apt_num)
                _extra = {
                    "od_match_attempted_at": _now_iso,
                    "od_appointment_type": od_appt_type,
                    "od_match_method": match_method,  # PR8: store how OD patient was matched
                }
                existing_od_pat = call.get("od_patient_num") or ""
                if not existing_od_pat and pat_num and match_method.startswith("ai_name"):
                    _extra["od_patient_num"] = pat_num
                    _extra["od_patient_status"] = "new_patient"  # name-matched = new patient inquiry
                update_mango_call_analysis(call["uuid"], **_extra)

                # PR5: fetch income ONLY for new patients acquired via ads.
                # existing_active/inactive patients already have revenue in OD —
                # attributing it to this call would inflate ads acquisition ROI.
                # new_patient status = phone not in OD at call time (genuine acquisition).
                # Also covers Gemini name-matched calls (match_method='ai_name(...)') which
                # are always treated as new_patient acquisitions.
                _existing_status = call.get("od_patient_status") or ""
                _is_existing = _existing_status in ("existing_active", "existing_inactive")
                _needs_income = (call.get("od_patient_income") is None) and not _is_existing
                if pat_num and _needs_income:
                    try:
                        _income = _get_patient_income(od_conn, pat_num)
                        _prod   = _get_patient_production(od_conn, pat_num)
                        update_mango_call_od_income(
                            call["uuid"], _income, _prod.get("total", 0.0)
                        )
                    except Exception as _e:
                        logger.warning(
                            f"[call_od_match] income fetch failed uuid={call['uuid']} "
                            f"PatNum={pat_num}: {_e}"
                        )

                logger.info(
                    f"[call_od_match] uuid={call['uuid']} method={match_method} "
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


def _get_od_patient_info_by_patnum(conn, pat_num: str) -> dict | None:
    """
    Look up OD patient info directly by PatNum.
    Returns same dict shape as _get_od_patient_info_by_phone.
    Used after guarantor resolution to get the dependent's full info.
    """
    if not pat_num:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT PatNum, FName, LName, PatStatus FROM patient
                   WHERE PatNum = %s""",
                (int(pat_num),),
            )
            row = cur.fetchone()
            if row:
                earliest_apt_entry = None
                try:
                    cur.execute(
                        "SELECT MIN(SecDateTEntry) FROM appointment WHERE PatNum = %s",
                        (int(pat_num),),
                    )
                    apt_row = cur.fetchone()
                    if apt_row and apt_row[0]:
                        earliest_apt_entry = apt_row[0]
                except Exception as apt_err:
                    logger.warning(f"OD earliest-apt lookup failed for PatNum {pat_num}: {apt_err}")
                return {
                    "pat_num":            str(row[0]),
                    "first_name":         (row[1] or "").strip(),
                    "last_name":          (row[2] or "").strip(),
                    "pat_status":         int(row[3]),
                    "earliest_apt_entry": earliest_apt_entry,
                }
    except Exception as e:
        logger.warning(f"OD patient info lookup failed for PatNum {pat_num}: {e}")
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
    Classify caller's OD relationship. Return values:

      no_match          — phone not found in OD at all (true new patient inquiry)
      new_patient       — in OD (PatStatus=0) but all appointments were booked AFTER
                          this call (record created in response to the call), or
                          in OD with PatStatus=0 but no appointments ever.
      existing_active   — PatStatus=0, has at least one appointment entry ≤ call_time
      existing_inactive — PatStatus=2 (Inactive) or 3 (Archived)
      unknown           — OD record found but PatStatus not in 0/2/3 (NonPatient,
                          Deceased, Prospective — shouldn't block lead creation)

    Core rule: if the patient's earliest appointment entry in OD is AFTER the call
    time, the record was created in response to this call → new_patient, regardless
    of PatStatus. SecDateEntry on the patient table is unreliable (can reflect system
    defaults); SecDateTEntry on the appointment table is the authoritative timestamp.

    GUARD logic in process_webhook: skips lead creation only for
    existing_active | existing_inactive. All other statuses → create lead.
    """
    if pat_info is None:
        return "no_match"
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
    from database import (
        get_mango_calls_needing_od_match,
        update_mango_call_od_status,
        update_mango_call_od_income,
        update_mango_call_analysis,
    )

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
                ai_patient_name = call.get("ai_patient_name") or ""

                # §2.3k: Guarantor resolution — if phone matched a patient who is a
                # guarantor, check if the actual caller/patient is a dependent.
                _guarantor_resolved = False
                if pat_info:
                    resolved_pat, resolve_method = _resolve_guarantor_family(
                        od_conn,
                        pat_info["pat_num"],
                        caller_name=caller_name,
                        ai_patient_name=ai_patient_name,
                        call_dt=None,  # no call_dt needed for status matching
                    )
                    if resolved_pat != pat_info["pat_num"]:
                        logger.info(
                            f"[mango_od_match] uuid={call['uuid']} guarantor "
                            f"{pat_info['pat_num']} → dependent {resolved_pat} "
                            f"(method={resolve_method})"
                        )
                        # Re-fetch full patient info for the resolved dependent
                        pat_info = _get_od_patient_info_by_patnum(od_conn, resolved_pat)
                        _guarantor_resolved = True

                # Name-mismatch guard: if OD found a record but the caller's CNAM shares no
                # name token with the OD patient, treat as new_patient (phone reuse / family
                # member / recycled number). Skip this check if we already resolved via
                # guarantor family — the resolution already verified the name.
                if pat_info and not _guarantor_resolved and not _names_match(
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
                # PR8: record match method (phone = phone number lookup)
                _match_m = "phone" if pat_num else "phone-no-match"
                update_mango_call_analysis(call["uuid"], od_match_method=_match_m)

                # PR5: income is only meaningful for NEW patients from ads.
                # Existing patients (existing_active/inactive) already have revenue
                # in OD — attributing it to this call would inflate ads ROI.
                # new_patient = no OD record found → pat_num is empty → skip.
                # So: only fetch income when status is NOT new_patient AND pat_num exists,
                # which means an existing patient called — but we intentionally skip those too.
                # Income columns stay NULL for all cases here; appointment matcher handles
                # the new-patient income path via Gemini name match.

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


# ── CallRail OD enrichment ────────────────────────────────────────────────────

def enrich_callrail_calls_with_od(limit: int = 500) -> dict:
    """
    Two-pass enrichment for callrail_calls rows missing od_patient_num/status.

    Pass 1 (no OD round-trip):
      Copy od_patient_num + od_patient_status from the linked lead row via
      database.backfill_callrail_od_from_leads().  Fast — pure SQLite.

    Pass 2 (OD phone lookup):
      For rows that still have no od_patient_num after Pass 1, do a live
      phone lookup against OpenDental using _get_od_patient_info_by_phone +
      _classify_od_status.  Updates callrail_calls via
      database.update_callrail_call_od_match().

    Returns counts dict suitable for logging / API response.
    """
    from database import (
        backfill_callrail_od_from_leads,
        get_callrail_calls_needing_od_enrich,
        update_callrail_call_od_match,
    )

    result = {
        "scanned": 0,
        "filled_from_lead": 0,
        "filled_from_od": 0,
        "skipped_no_phone": 0,
        "errors": 0,
        "od_unavailable": False,
    }

    # ── Pass 1: backfill from linked leads (SQLite only) ─────────────────────
    try:
        p1 = backfill_callrail_od_from_leads()
        result["filled_from_lead"] = p1.get("updated", 0)
        logger.info(f"[callrail_od_enrich] Pass 1 done: {result['filled_from_lead']} rows filled from leads")
    except Exception as e:
        logger.error(f"[callrail_od_enrich] Pass 1 (backfill) failed: {e}")
        result["errors"] += 1

    # ── Pass 2: OD phone lookup for remaining unenriched rows ─────────────────
    calls = get_callrail_calls_needing_od_enrich(limit=limit)
    result["scanned"] = len(calls)

    if not calls:
        logger.info("[callrail_od_enrich] Pass 2: nothing to enrich")
        return result

    od_conn = _get_db()
    if od_conn is None:
        logger.warning("[callrail_od_enrich] Pass 2: OpenDental unavailable — skipping phone lookup")
        result["od_unavailable"] = True
        return result

    try:
        for row in calls:
            row_id       = row["id"]
            caller_phone = (row.get("caller_number") or "").strip()
            called_at    = row.get("called_at") or ""

            # Normalize to 10-digit for OD query
            digits = "".join(c for c in caller_phone if c.isdigit())
            phone_10 = digits[-10:] if len(digits) >= 10 else digits

            if not phone_10 or len(phone_10) < 7:
                result["skipped_no_phone"] += 1
                continue

            try:
                pat_info = _get_od_patient_info_by_phone(od_conn, phone_10)
                status   = _classify_od_status(pat_info, call_time=called_at)
                pat_num  = pat_info["pat_num"] if pat_info else ""
                update_callrail_call_od_match(row_id, pat_num, status)
                result["filled_from_od"] += 1
                logger.debug(
                    f"[callrail_od_enrich] row {row_id} phone={phone_10} "
                    f"pat_num={pat_num} status={status}"
                )
            except Exception as e:
                logger.error(f"[callrail_od_enrich] row {row_id} error: {e}")
                result["errors"] += 1
    finally:
        od_conn.close()

    logger.info(
        f"[callrail_od_enrich] Pass 2 done: scanned={result['scanned']} "
        f"filled={result['filled_from_od']} skipped={result['skipped_no_phone']} "
        f"errors={result['errors']}"
    )
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = run_full_od_sync()
    print(json.dumps(result, indent=2))
