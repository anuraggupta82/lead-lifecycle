"""
OD Payment Sync — PR 2
======================
Pulls actual collected payments from OpenDental's payment/paysplit tables and
writes them into two buckets on leads and keyword_production_log:

  paid_amount_365d — payments within 365 days of the lead/call anchor date
  paid_amount_ltv  — all payments on or after the anchor date (no upper bound)

Scope: Google Ads-attributed patients only.
  - Leads: gclid present OR the lead's CallRail row has source='google_ads'
    (call-extension calls carry no gclid)
  - Calls: keyword_production_log rows with lead_id LIKE 'call::%%' and
    attributed_keyword_confidence >= 0.55 (enforced upstream by
    call_production_log.py; we trust the KPL row implies >= 0.55)

Existing patients (od_patient_status IN ('existing_active','existing_inactive'))
are skipped — their pre-existing payments would inflate Google Ads ROI.

Connection pattern mirrors call_production_log._get_od_conn() exactly.
"""
import logging
import time
from datetime import datetime, date, timezone, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_EASTERN = ZoneInfo("America/New_York")

# Payment query chunk size — avoids excessively large IN (...) clauses.
_CHUNK_SIZE = 500

# Minimum payment delta to emit a lifecycle_events row.
_MIN_EVENT_DELTA = 50.0


# ─────────────────────────────────────────────────────────────────────────────
# OD connection helper
# Copied verbatim from call_production_log._get_od_conn() — modules are kept
# independent so either can be imported without the other.
# ─────────────────────────────────────────────────────────────────────────────

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
        logger.warning(f"[od_payment_sync] OpenDental MySQL unavailable: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_anchor(anchor_str: str) -> Optional[date]:
    """Parse an ISO timestamp/date string to an Eastern (America/New_York) calendar
    date. OpenDental PayDate is a plain Eastern date, so a UTC anchor timestamp must
    be converted to Eastern before taking .date(), otherwise evening-ET leads (already
    past midnight UTC) get an anchor one day late and same-day payments are dropped.
    Date-only strings (no time component) are returned unshifted. Returns None on failure."""
    if not anchor_str:
        return None
    s = anchor_str.strip()
    # Date-only anchor (no time-of-day) — return as-is, no tz shift.
    if len(s) <= 10:
        try:
            return date.fromisoformat(s[:10])
        except Exception:
            return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        try:
            return date.fromisoformat(s[:10])
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # stored UTC
    return dt.astimezone(_EASTERN).date()


def _days_back_cutoff(days_back: int) -> str:
    """ISO timestamp for (now - days_back days)."""
    return (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()


def _collect_lead_targets(conn, full_resync: bool, cutoff_iso: str) -> list:
    """
    Collect leads to sync.

    Returns list of dicts:
      {od_patient_num, anchor_date, target_table='leads', target_id=<lead.id>,
       current_365d, current_ltv}

    Filters:
    - od_patient_num != ''
    - Google Ads leads only: gclid present OR the lead's CallRail row has
      source='google_ads' (call-extension calls carry no gclid)
    - Excludes existing patients: leads with existing_patient = 1 are filtered
      out at collection time via COALESCE(existing_patient, 0) = 0, so their
      pre-existing payments never enter the payment computation.
    - Stale check: payment_synced_at < cutoff OR payment_synced_at == '' (unless full_resync)
    """
    if full_resync:
        rows = conn.execute("""
            SELECT id, od_patient_num, created_at,
                   COALESCE(paid_amount_365d, 0.0) AS current_365d,
                   COALESCE(paid_amount_ltv,  0.0) AS current_ltv
            FROM leads
            WHERE od_patient_num != ''
              AND od_patient_num IS NOT NULL
              AND (
                  (gclid != '' AND gclid IS NOT NULL)
                  OR EXISTS (
                      SELECT 1 FROM callrail_calls cc
                      WHERE cc.lead_id = leads.id
                        AND cc.source = 'google_ads'
                  )
              )
              AND COALESCE(existing_patient, 0) = 0
        """).fetchall()
    else:
        rows = conn.execute("""
            SELECT id, od_patient_num, created_at,
                   COALESCE(paid_amount_365d, 0.0) AS current_365d,
                   COALESCE(paid_amount_ltv,  0.0) AS current_ltv
            FROM leads
            WHERE od_patient_num != ''
              AND od_patient_num IS NOT NULL
              AND (
                  (gclid != '' AND gclid IS NOT NULL)
                  OR EXISTS (
                      SELECT 1 FROM callrail_calls cc
                      WHERE cc.lead_id = leads.id
                        AND cc.source = 'google_ads'
                  )
              )
              AND COALESCE(existing_patient, 0) = 0
              AND (payment_synced_at IS NULL
                   OR payment_synced_at = ''
                   OR payment_synced_at < ?)
        """, (cutoff_iso,)).fetchall()

    targets = []
    for r in rows:
        targets.append({
            "od_patient_num": str(r["od_patient_num"]).strip(),
            "anchor_date":    r["created_at"] or "",
            "target_table":   "leads",
            "target_id":      r["id"],
            "current_365d":   float(r["current_365d"] or 0.0),
            "current_ltv":    float(r["current_ltv"] or 0.0),
        })
    return targets


def _collect_call_targets(conn, full_resync: bool, cutoff_iso: str) -> list:
    """
    Collect call-path KPL rows to sync.

    Returns list of dicts:
      {od_patient_num, anchor_date, target_table='kpl', target_id=<kpl.id>}

    anchor_date comes from mango_calls.started_at (joined via UUID in lead_id).
    If the join fails, falls back to kpl.logged_at.

    Existing patient exclusion: if mango_calls.od_patient_status is
    'existing_active' or 'existing_inactive', the row is skipped.
    """
    if full_resync:
        rows = conn.execute("""
            SELECT
                kpl.id              AS kpl_id,
                kpl.od_patient_num  AS od_patient_num,
                kpl.lead_id         AS lead_id,
                kpl.logged_at       AS logged_at,
                COALESCE(mc.started_at, kpl.logged_at) AS anchor_date,
                COALESCE(mc.od_patient_status, '')     AS od_patient_status,
                COALESCE(kpl.paid_amount_365d, 0.0)   AS current_365d,
                COALESCE(kpl.paid_amount_ltv,  0.0)   AS current_ltv
            FROM keyword_production_log kpl
            LEFT JOIN mango_calls mc
                ON mc.uuid = SUBSTR(kpl.lead_id, 7)   -- strip 'call::' prefix
            WHERE kpl.lead_id LIKE 'call::%'
              AND kpl.od_patient_num != ''
              AND kpl.od_patient_num IS NOT NULL
        """).fetchall()
    else:
        rows = conn.execute("""
            SELECT
                kpl.id              AS kpl_id,
                kpl.od_patient_num  AS od_patient_num,
                kpl.lead_id         AS lead_id,
                kpl.logged_at       AS logged_at,
                COALESCE(mc.started_at, kpl.logged_at) AS anchor_date,
                COALESCE(mc.od_patient_status, '')     AS od_patient_status,
                COALESCE(kpl.paid_amount_365d, 0.0)   AS current_365d,
                COALESCE(kpl.paid_amount_ltv,  0.0)   AS current_ltv
            FROM keyword_production_log kpl
            LEFT JOIN mango_calls mc
                ON mc.uuid = SUBSTR(kpl.lead_id, 7)
            WHERE kpl.lead_id LIKE 'call::%'
              AND kpl.od_patient_num != ''
              AND kpl.od_patient_num IS NOT NULL
              AND (kpl.payment_synced_at IS NULL
                   OR kpl.payment_synced_at = ''
                   OR kpl.payment_synced_at < ?)
        """, (cutoff_iso,)).fetchall()

    targets = []
    for r in rows:
        # Skip existing patients — their payments predate Google Ads attribution
        status = (r["od_patient_status"] or "").strip()
        if status in ("existing_active", "existing_inactive"):
            continue
        targets.append({
            "od_patient_num": str(r["od_patient_num"]).strip(),
            "anchor_date":    r["anchor_date"] or r["logged_at"] or "",
            "target_table":   "kpl",
            "target_id":      r["kpl_id"],
            "current_365d":   float(r["current_365d"] or 0.0),
            "current_ltv":    float(r["current_ltv"] or 0.0),
        })
    return targets


def _bulk_query_od_payments(od_conn, patient_nums: list) -> dict:
    """
    Query OD MySQL for payment totals per (patient_num, payment_date).
    Chunks the IN (...) list to at most _CHUNK_SIZE patient nums per query.

    Returns dict: { od_patient_num_str -> [(date_str, amount), ...] }
    sorted by date ascending.

    Uses paysplit.SplitAmt NOT payment.PayAmt to avoid family-split inflation.
    (paysplit.SplitAmt is the amount actually allocated to this patient's account.)
    """
    results: dict = {}

    # De-duplicate patient_nums (in case the same patient appears in both leads
    # and kpl — shouldn't happen but defensive)
    unique_nums = list({str(n) for n in patient_nums if n})
    if not unique_nums:
        return results

    # Chunk the list
    chunks = [unique_nums[i:i + _CHUNK_SIZE] for i in range(0, len(unique_nums), _CHUNK_SIZE)]

    with od_conn.cursor() as cur:
        for chunk in chunks:
            placeholders = ",".join(["%s"] * len(chunk))
            query = f"""
                SELECT
                    p.PatNum            AS od_patient_num,
                    DATE(p.PayDate)     AS payment_date,
                    SUM(ps.SplitAmt)    AS amount
                FROM payment p
                JOIN paysplit ps ON ps.PayNum = p.PayNum
                WHERE p.PatNum IN ({placeholders})
                  AND p.PayDate IS NOT NULL
                  AND p.PayDate != '0001-01-01'
                GROUP BY p.PatNum, DATE(p.PayDate)
                ORDER BY p.PatNum, p.PayDate
            """
            cur.execute(query, chunk)
            for row in cur.fetchall():
                pat = str(row["od_patient_num"]) if isinstance(row, dict) else str(row[0])
                pdate = str(row["payment_date"]) if isinstance(row, dict) else str(row[1])
                amt = float(row["amount"]) if isinstance(row, dict) else float(row[2])
                if pat not in results:
                    results[pat] = []
                results[pat].append((pdate, amt))

    return results


def _compute_buckets(payment_rows: list, anchor_date_str: str, window_days: int = 365):
    """
    Given sorted (date_str, amount) rows and an anchor_date string, compute:
      paid_365d, paid_ltv, first_payment_date, paid_through_date

    Payments BEFORE anchor_date are excluded from both buckets.
    """
    anchor = _parse_anchor(anchor_date_str)
    if anchor is None:
        return 0.0, 0.0, "", ""

    cutoff_365 = anchor + timedelta(days=window_days)

    paid_365d = 0.0
    paid_ltv = 0.0
    first_pdate = ""
    last_pdate = ""

    for pdate_str, amt in payment_rows:
        try:
            pdate = date.fromisoformat(str(pdate_str)[:10])
        except Exception:
            continue

        if pdate < anchor:
            # Pre-anchor payment — exclude from both buckets
            continue

        # In LTV (no upper bound, as long as >= anchor)
        paid_ltv += amt

        # In 365d window?
        if pdate <= cutoff_365:
            paid_365d += amt

        # Track first/last
        if not first_pdate or pdate_str[:10] < first_pdate:
            first_pdate = pdate_str[:10]
        if not last_pdate or pdate_str[:10] > last_pdate:
            last_pdate = pdate_str[:10]

    return paid_365d, paid_ltv, first_pdate, last_pdate


# ─────────────────────────────────────────────────────────────────────────────
# PR 4: Refresh call OD income
# ─────────────────────────────────────────────────────────────────────────────

def _try_date_ge(pdate_str: str, anchor_date) -> bool:
    """Return True if the payment date string is >= anchor_date. False on any parse error."""
    try:
        pdate = date.fromisoformat(str(pdate_str)[:10])
        return pdate >= anchor_date
    except Exception:
        return False


def refresh_call_od_income(days: int = 90) -> dict:
    """
    Refresh mango_calls.od_patient_income for every new-patient call matched
    to an OD patient in the last `days` days. Re-queries OD for the current
    paid total since the call's started_at and writes back.

    Uses the same SUM(paysplit.SplitAmt) query as sync_od_payments to handle
    family splits and accounting reallocations correctly (PayNum 9780 case
    with +$165/-$165 nets to $0).

    Anchor: each call's started_at. Payments before that date are excluded
    (pre-existing patient defense — won't fire for new_patient rows but
    defensive).

    Also updates keyword_production_log paid amounts for any KPL row keyed
    to the same call uuid (call::{uuid}), keeping PR 2 data in sync.

    Returns:
        {
            "calls_refreshed": int,
            "calls_updated": int,        # non-zero diff from prior value
            "total_income_synced": float,
            "kpl_updated": int,
            "errors": int,
            "duration_seconds": float,
        }
    """
    t0 = time.time()

    od_conn = _get_od_conn()
    if od_conn is None:
        logger.warning("[refresh_call_income] OpenDental unavailable — skipping")
        return {"status": "skipped", "reason": "od_unavailable"}

    calls_refreshed = 0
    calls_updated = 0
    kpl_updated = 0
    total_income_synced = 0.0
    errors = 0

    try:
        import sqlite3
        from config import get_settings
        settings = get_settings()
        db_conn = sqlite3.connect(settings.db_path, check_same_thread=False, timeout=15)
        db_conn.row_factory = sqlite3.Row
        db_conn.execute("PRAGMA foreign_keys=ON")
        db_conn.execute("PRAGMA busy_timeout=15000")

        try:
            # ── 1. Fetch target calls ───────────────────────────────────────
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            rows = db_conn.execute("""
                SELECT uuid, od_patient_num, started_at,
                       COALESCE(od_patient_income, 0.0) AS od_patient_income,
                       COALESCE(od_patient_production, 0.0) AS od_patient_production
                FROM mango_calls
                WHERE od_patient_status = 'new_patient'
                  AND od_patient_num IS NOT NULL AND od_patient_num != ''
                  AND od_appointment_id IS NOT NULL AND od_appointment_id != ''
                  AND started_at >= ?
                ORDER BY started_at DESC
            """, (cutoff,)).fetchall()

            if not rows:
                logger.info("[refresh_call_income] No eligible calls found")
                return {
                    "calls_refreshed": 0,
                    "calls_updated": 0,
                    "total_income_synced": 0.0,
                    "kpl_updated": 0,
                    "errors": 0,
                    "duration_seconds": round(time.time() - t0, 2),
                }

            call_list = [dict(r) for r in rows]
            patient_nums = list({r["od_patient_num"] for r in call_list if r["od_patient_num"]})

            # ── 2. Bulk-query OD payments for all patients ──────────────────
            od_payments = _bulk_query_od_payments(od_conn, patient_nums)

            # ── 3. Compute new income per call and collect updates ──────────
            mango_updates = []   # (new_income, existing_production, uuid)
            kpl_updates = []     # (paid_365d, paid_ltv, payment_synced_at, kpl_lead_id)

            now_iso = _now_iso()

            for call in call_list:
                try:
                    calls_refreshed += 1
                    uuid = call["uuid"]
                    pat = call["od_patient_num"]
                    prior_income = float(call["od_patient_income"] or 0.0)
                    existing_production = float(call["od_patient_production"] or 0.0)

                    anchor_date = _parse_anchor(call["started_at"])
                    if anchor_date is None:
                        logger.warning(f"[refresh_call_income] uuid={uuid}: unparseable started_at, skipping")
                        errors += 1
                        continue

                    payment_rows = od_payments.get(pat, [])
                    # Sum all SplitAmt values on/after anchor_date.
                    # CRITICAL: do NOT filter negative amounts — they net out correctly
                    # (PayNum 9780: +165, -165, +34, -34 = $0, not $199)
                    total_paid = 0.0
                    for pdate_str, amt in payment_rows:
                        if _try_date_ge(pdate_str, anchor_date):
                            total_paid += amt

                    if abs(total_paid - prior_income) < 0.01:
                        # No meaningful change — skip write
                        total_income_synced += total_paid
                        continue

                    mango_updates.append((total_paid, existing_production, now_iso, now_iso, uuid))
                    calls_updated += 1
                    total_income_synced += total_paid

                    # Also update KPL row if one exists for this call
                    synthetic_lead_id = f"call::{uuid}"
                    kpl_row = db_conn.execute(
                        "SELECT id FROM keyword_production_log WHERE lead_id = ?",
                        (synthetic_lead_id,)
                    ).fetchone()
                    if kpl_row:
                        # Compute 365d bucket from anchor_date
                        paid_365d, paid_ltv, _, _ = _compute_buckets(
                            payment_rows, call["started_at"], 365
                        )
                        kpl_updates.append((paid_365d, paid_ltv, now_iso, kpl_row["id"]))
                        kpl_updated += 1

                except Exception as e:
                    logger.error(f"[refresh_call_income] uuid={call.get('uuid','?')} error: {e}")
                    errors += 1

            # ── 4. Write back ────────────────────────────────────────────────
            if mango_updates:
                db_conn.executemany(
                    """UPDATE mango_calls
                       SET od_patient_income=?, od_patient_production=?,
                           od_income_synced_at=?, updated_at=?
                       WHERE uuid=?""",
                    mango_updates,
                )

            if kpl_updates:
                db_conn.executemany(
                    """UPDATE keyword_production_log
                       SET paid_amount_365d=?, paid_amount_ltv=?, payment_synced_at=?
                       WHERE id=?""",
                    kpl_updates,
                )

            db_conn.commit()
            logger.info(
                f"[refresh_call_income] Done: refreshed={calls_refreshed} "
                f"updated={calls_updated} kpl_updated={kpl_updated} "
                f"errors={errors} total=${total_income_synced:.2f}"
            )

        finally:
            db_conn.close()

    except Exception as e:
        logger.error(f"[refresh_call_income] Unexpected error: {e}")
        errors += 1
    finally:
        try:
            od_conn.close()
        except Exception:
            pass

    return {
        "calls_refreshed": calls_refreshed,
        "calls_updated": calls_updated,
        "total_income_synced": round(total_income_synced, 2),
        "kpl_updated": kpl_updated,
        "errors": errors,
        "duration_seconds": round(time.time() - t0, 2),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def sync_od_payments(days_back: int = 7, full_resync: bool = False) -> dict:
    """
    Pull payments from OpenDental for all leads + call-only patients tied to
    Google Ads attribution. Updates paid_amount_365d / paid_amount_ltv on
    leads and keyword_production_log.

    Args:
        days_back: only re-pull patients whose OD payments may have changed in
                   the last N days. Default 7. Use full_resync=True for backfill.
        full_resync: ignore days_back, rebuild paid amounts for every attributed
                     patient. Use sparingly (e.g., one-shot after PR 2 ships).

    Returns:
        {
            "leads_synced": int,
            "calls_synced": int,
            "total_paid_365d": float,
            "total_paid_ltv":  float,
            "errors": int,
            "duration_seconds": float,
        }
    """
    t0 = time.time()
    od_conn = _get_od_conn()
    if od_conn is None:
        return {"status": "skipped", "reason": "od_unavailable"}

    from config import get_settings
    settings = get_settings()
    window_days = getattr(settings, "gads_attribution_window_days", 365)

    leads_synced = 0
    calls_synced = 0
    total_paid_365d = 0.0
    total_paid_ltv = 0.0
    errors = 0

    try:
        import sqlite3
        db_path = settings.db_path
        db_conn = sqlite3.connect(db_path, check_same_thread=False, timeout=15)
        db_conn.row_factory = sqlite3.Row
        db_conn.execute("PRAGMA foreign_keys=ON")
        db_conn.execute("PRAGMA busy_timeout=15000")

        now_iso = _now_iso()
        cutoff_iso = _days_back_cutoff(days_back)

        try:
            # ── Collect all target patients ──────────────────────────────────
            lead_targets = _collect_lead_targets(db_conn, full_resync, cutoff_iso)
            call_targets = _collect_call_targets(db_conn, full_resync, cutoff_iso)

            all_targets = lead_targets + call_targets
            if not all_targets:
                return {
                    "leads_synced":    0,
                    "calls_synced":    0,
                    "total_paid_365d": 0.0,
                    "total_paid_ltv":  0.0,
                    "errors":          0,
                    "duration_seconds": round(time.time() - t0, 2),
                }

            all_patient_nums = list({t["od_patient_num"] for t in all_targets})

            # ── Bulk-query OD for all patient payment history ────────────────
            od_payments = _bulk_query_od_payments(od_conn, all_patient_nums)

            # ── Build lookup: od_patient_num -> payment rows ─────────────────
            # (already done — od_payments IS the lookup dict)

            # ── Process each target ──────────────────────────────────────────
            leads_updates = []    # (paid_365d, paid_ltv, first_pdate, through_pdate, synced_at, lead_id)
            kpl_updates = []      # (paid_365d, paid_ltv, synced_at, kpl_id)
            lifecycle_events = [] # (lead_id, delta_365d, delta_ltv)

            for target in all_targets:
                try:
                    pat = target["od_patient_num"]
                    payment_rows = od_payments.get(pat, [])
                    paid_365d, paid_ltv, first_pdate, through_pdate = _compute_buckets(
                        payment_rows, target["anchor_date"], window_days
                    )

                    total_paid_365d += paid_365d
                    total_paid_ltv  += paid_ltv

                    if target["target_table"] == "leads":
                        old_365d = target["current_365d"]
                        leads_updates.append((
                            paid_365d, paid_ltv, first_pdate, through_pdate, now_iso,
                            target["target_id"],
                        ))
                        leads_synced += 1
                        # Lifecycle event if 365d changed by >= $50
                        delta_365d = paid_365d - old_365d
                        if abs(delta_365d) >= _MIN_EVENT_DELTA:
                            delta_ltv = paid_ltv - target["current_ltv"]
                            lifecycle_events.append((
                                target["target_id"],
                                round(delta_365d, 2),
                                round(delta_ltv, 2),
                            ))
                    else:  # kpl
                        kpl_updates.append((
                            paid_365d, paid_ltv, now_iso,
                            target["target_id"],
                        ))
                        calls_synced += 1

                except Exception as e:
                    logger.warning(f"[od_payment_sync] patient {target.get('od_patient_num')} error: {e}")
                    errors += 1

            # ── Write back in single transactions ────────────────────────────
            if leads_updates:
                db_conn.executemany(
                    """UPDATE leads
                       SET paid_amount_365d=?, paid_amount_ltv=?,
                           first_payment_date=?, paid_through_date=?,
                           payment_synced_at=?
                       WHERE id=?""",
                    leads_updates,
                )

            if kpl_updates:
                db_conn.executemany(
                    """UPDATE keyword_production_log
                       SET paid_amount_365d=?, paid_amount_ltv=?,
                           payment_synced_at=?
                       WHERE id=?""",
                    kpl_updates,
                )

            # ── Lifecycle events ─────────────────────────────────────────────
            if lifecycle_events:
                import json as _json
                event_rows = [
                    (
                        lead_id,
                        "payment_pulled",
                        _json.dumps({"paid_365d_delta": d365, "paid_ltv_delta": dltv}),
                        now_iso,
                    )
                    for lead_id, d365, dltv in lifecycle_events
                ]
                db_conn.executemany(
                    """INSERT INTO lifecycle_events (lead_id, event_type, detail, created_at)
                       VALUES (?, ?, ?, ?)""",
                    event_rows,
                )

            db_conn.commit()

        finally:
            db_conn.close()

    except Exception as e:
        logger.error(f"[od_payment_sync] Unexpected error: {e}")
        errors += 1
    finally:
        try:
            od_conn.close()
        except Exception:
            pass

    return {
        "leads_synced":    leads_synced,
        "calls_synced":    calls_synced,
        "total_paid_365d": round(total_paid_365d, 2),
        "total_paid_ltv":  round(total_paid_ltv, 2),
        "errors":          errors,
        "duration_seconds": round(time.time() - t0, 2),
    }
