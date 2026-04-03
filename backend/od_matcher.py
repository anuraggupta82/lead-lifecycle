"""
OpenDental patient matcher — matches leads to OD patients by phone/email hash.
Runs as a nightly job. Never stores raw PHI — uses SHA-256 hashes for comparison.
Only accessible on the office LAN (GraftonServer).
"""
import hashlib
import logging
import json
from datetime import datetime, timezone
from database import get_all_leads, get_lead, add_event
from config import get_settings

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
        settings = get_settings()
        return pymysql.connect(
            host=settings.od_db_host,
            port=settings.od_db_port,
            user=settings.od_db_user,
            password=settings.od_db_password,
            database=settings.od_db_name,
            connect_timeout=5,
            charset="utf8mb4",
        )
    except Exception as e:
        logger.warning(f"OpenDental MySQL unavailable: {e}")
        return None


def match_leads_to_od() -> dict:
    """
    Match unmatched leads to OpenDental patients using phone/email hashes.
    Pulls production attributed to implant CDT codes.
    """
    conn = _get_db()
    if not conn:
        return {"matched": 0, "errors": 1, "error": "OpenDental MySQL unavailable (office network required)"}

    # Get leads that don't yet have an OD match and have contact info
    unmatched = [
        l for l in get_all_leads()
        if not l.get("od_patient_num") and (l.get("phone_hash") or l.get("email_hash"))
    ]
    logger.info(f"OD matcher: {len(unmatched)} leads to check")

    matched = errors = 0

    try:
        with conn.cursor() as cur:
            # Pull all patients with phone + email hashes (SELECT only — read-only access)
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

                # Get production for this patient (implant codes only)
                production = _get_patient_production(conn, pat_num)

                # Update lead
                import sqlite3 as _sqlite
                from config import get_settings as _gs
                import os as _os
                settings = _gs()
                _os.makedirs(_os.path.dirname(settings.db_path), exist_ok=True)
                lconn = _sqlite.connect(settings.db_path)
                lconn.row_factory = _sqlite.Row
                now = datetime.now(timezone.utc).isoformat()
                lconn.execute("""
                    UPDATE leads SET od_patient_num=?, od_matched_at=?, attributed_production=?, updated_at=?
                    WHERE id=?
                """, (pat_num, now, production["total"], now, lead["id"]))
                lconn.commit()
                lconn.close()

                add_event(lead["id"], "od_matched", source="od_matcher",
                          detail=json.dumps({
                              "pat_num": pat_num,
                              "match_method": match_method,
                              "production": production["total"],
                              "codes": production["codes"],
                          }))

                logger.info(f"Lead {lead['id']} matched to OD PatNum {pat_num} via {match_method}, production=${production['total']:.2f}")
                matched += 1

            except Exception as e:
                logger.error(f"Error matching lead {lead['id']}: {e}")
                errors += 1

    finally:
        conn.close()

    return {"matched": matched, "unmatched": len(unmatched) - matched, "errors": errors}


def _get_patient_production(conn, pat_num: str) -> dict:
    """Get total implant production for a patient."""
    try:
        with conn.cursor() as cur:
            placeholders = ",".join(["%s"] * len(CDT_IMPLANT_CODES))
            cur.execute(f"""
                SELECT pl.ProcCode, SUM(pl.ProcFee) as total
                FROM procedurelog pl
                JOIN procedurecode pc ON pl.CodeNum = pc.CodeNum
                WHERE pl.PatNum = %s
                  AND pl.ProcStatus = 2
                  AND pc.ProcCode IN ({placeholders})
                GROUP BY pl.ProcCode
            """, [int(pat_num)] + list(CDT_IMPLANT_CODES))
            rows = cur.fetchall()
            total = sum(float(r[1]) for r in rows)
            codes = [r[0] for r in rows]
            return {"total": total, "codes": codes}
    except Exception:
        return {"total": 0.0, "codes": []}
