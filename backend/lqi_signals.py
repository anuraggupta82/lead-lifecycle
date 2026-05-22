"""
Lead Quality Intelligence (LQI) — signal collector for the AI optimizer.

Runs once per optimizer cycle, queries existing tables (leads, mango_calls,
gads_search_terms_cache, gads_keywords_cache, gads_call_view, communication_log,
call_flags, lifecycle_events) and returns a compact dict that gets injected into
both Claude prompts.

No schema changes, no background jobs, no UI. All collectors are read-only.

Entry point:
    from lqi_signals import collect_all
    lqi = collect_all(days=30)
    # → {"sources": {...}, "calls": {...}, "search_terms": {...},
    #    "schedule": {...}, "cold_leads": {...}, "no_shows": {...}}
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ── Tunables ──────────────────────────────────────────────────────────────────
SHORT_CALL_SEC = 60           # call <60s is "short/hangup"
MIN_SPEND_FOR_BAD_TERM = 1.0  # flag terms with $1+ spend (lowered from $5 — low-cost terms still matter)
TOP_N_SHORT_CALLS = 10
TOP_N_BAD_TERMS = 25
TOP_N_HOTSPOTS = 12

# Spanish/wrong-intent regex (case-insensitive)
_SPANISH_RX = re.compile(
    r"\b(muela|dolor|precio|sin\s+seguro|baratos?|economico|dentista|"
    r"clinica|emergencia|seguro|costo|gratis|cuanto)\b",
    re.IGNORECASE,
)
# Competitor practice patterns — named chains AND generic "X dental group/care/office" patterns
_COMPETITOR_RX = re.compile(
    r"\b(aspen\s+dental|gentle\s+dental|grace\s+dental|simply\s+orthodontics|"
    r"smile\s*direct|byte|invisalign\s+doctor|nibblers|kool\s+smiles|"
    r"family\s+dentistry\s+of|metrowest\s+oral|new\s+england\s+oral|"
    # Generic: any "[name] dental group/care/office/associates/center/clinic/studio"
    r"\w+\s+dental\s+(group|care|office|associates|center|clinic|studio)|"
    # "[name] dentistry" — other practices
    r"\w+\s+dentistry\b|"
    # "[town] dental" patterns that aren't us (we are "grafton dental care")
    r"(gardner|worcester|shrewsbury|northborough|westborough|marlborough|"
    r"clinton|fitchburg|leominster|milford|hopedale|upton|uxbridge|"
    r"medway|holliston|hopkinton|millis|medfield|norwood|framingham)\s+dental)\b",
    re.IGNORECASE,
)
# Wrong-intent: education/research/DIY/home-remedy, not booking
_WRONG_INTENT_RX = re.compile(
    r"\b(meaning|definition|wikipedia|reddit|youtube|images|salary|"
    r"how\s+to\s+become|school|job|career|insurance\s+coverage|"
    # DIY / home-remedy / informational patterns
    r"how\s+to\s+(fix|treat|cure|prevent|reduce|remove|stop|get\s+rid|heal|whiten|clean|brush|floss)|"
    r"home\s+(remedy|remedies|treatment|cure)|"
    r"natural\s+(remedy|remedies|treatment|cure)|"
    r"diy\s+dental|at\s+home\s+dental|"
    r"what\s+(is|are|causes?)|why\s+(do|does|is|are)|"
    r"symptoms?\s+of|signs?\s+of|stages?\s+of|"
    r"dental\s+(school|student|degree|program|salary|license)|"
    r"free\s+dental|low\s+cost\s+dental|affordable\s+dental\s+clinic)\b",
    re.IGNORECASE,
)

_DOW_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


# ─── Source quality ───────────────────────────────────────────────────────────
def collect_source_quality(conn, days: int = 30) -> dict:
    """
    Per-source funnel quality. Sources: smile_tool, contact_form, pearly, gads_call.

    Returns:
      {
        "<source>": {
            "leads": int, "booked": int, "showed": int, "cold": int,
            "od_matched": int,
            "booked_rate": float, "showed_rate": float,
            "cold_rate": float, "od_match_rate": float,
            "quality_score": int   # 0–100
        }, ...
      }
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    sql = """
      SELECT
        COALESCE(NULLIF(source,''),'unknown') AS source,
        COUNT(*)                                                          AS leads,
        SUM(CASE WHEN stage IN ('booked','scheduled','showed','tx_presented',
                                'tx_accepted','tx_completed') THEN 1 ELSE 0 END)
                                                                          AS booked,
        SUM(CASE WHEN stage IN ('showed','tx_presented','tx_accepted',
                                'tx_completed') THEN 1 ELSE 0 END)        AS showed,
        SUM(CASE WHEN stage='cold' OR cold_at != '' THEN 1 ELSE 0 END)    AS cold,
        SUM(CASE WHEN od_patient_num != '' THEN 1 ELSE 0 END)             AS od_matched
      FROM leads
      WHERE created_at >= ?
      GROUP BY source
    """
    out: dict = {}
    for r in conn.execute(sql, (cutoff,)):
        leads = int(r["leads"] or 0)
        if leads == 0:
            continue
        booked = int(r["booked"] or 0)
        showed = int(r["showed"] or 0)
        cold   = int(r["cold"] or 0)
        odm    = int(r["od_matched"] or 0)
        booked_rate = round(booked / leads, 3)
        showed_rate = round(showed / leads, 3)
        cold_rate   = round(cold / leads, 3)
        od_rate     = round(odm / leads, 3)
        # Quality score (0–100):
        #   40% booked_rate, 30% showed_rate, 20% od_match_rate, 10% (1 - cold_rate)
        qs = int(round(100 * (
            0.40 * booked_rate +
            0.30 * showed_rate +
            0.20 * od_rate +
            0.10 * (1.0 - cold_rate)
        )))
        out[r["source"]] = {
            "leads":          leads,
            "booked":         booked,
            "showed":         showed,
            "cold":           cold,
            "od_matched":     odm,
            "booked_rate":    booked_rate,
            "showed_rate":    showed_rate,
            "cold_rate":      cold_rate,
            "od_match_rate":  od_rate,
            "quality_score":  qs,
        }

    # Add gads_call as a synthetic source from mango_calls matched to a GAds click
    try:
        row = conn.execute("""
          SELECT
            COUNT(*) AS calls,
            SUM(CASE WHEN duration_sec >= ? THEN 1 ELSE 0 END)               AS qualified,
            SUM(CASE WHEN booked_outcome='booked' THEN 1 ELSE 0 END)          AS booked,
            SUM(CASE WHEN od_appointment_id IS NOT NULL
                      AND od_appointment_id != '' THEN 1 ELSE 0 END)         AS od_matched
          FROM mango_calls
          WHERE started_at >= ?
            AND gads_call_id IS NOT NULL
            AND gads_call_id != ''
        """, (SHORT_CALL_SEC, cutoff)).fetchone()
        calls = int(row["calls"] or 0) if row else 0
        if calls > 0:
            booked = int(row["booked"] or 0)
            odm    = int(row["od_matched"] or 0)
            quali  = int(row["qualified"] or 0)
            br  = round(booked / calls, 3)
            qr  = round(quali / calls, 3)
            odr = round(odm / calls, 3)
            qs  = int(round(100 * (0.40 * br + 0.30 * qr + 0.20 * odr + 0.10 * 1.0)))
            out["gads_call"] = {
                "leads":         calls,
                "booked":        booked,
                "showed":        quali,
                "cold":          calls - quali,
                "od_matched":    odm,
                "booked_rate":   br,
                "showed_rate":   qr,
                "cold_rate":     round(1.0 - qr, 3),
                "od_match_rate": odr,
                "quality_score": qs,
            }
    except Exception as e:
        logger.debug(f"LQI: gads_call source build failed (non-fatal): {e}")
    return out


# ─── Short/hangup calls ───────────────────────────────────────────────────────
def collect_short_calls(conn, days: int = 30) -> dict:
    """
    Per-campaign Google Ads call quality.

    Returns:
      {
        "by_campaign": {
            "<campaign_name>": {
                "total_calls": int, "short_calls": int, "short_pct": float,
                "missed_calls": int, "avg_duration_sec": int,
                "shortest": [
                    {"uuid": str, "campaign": str, "duration_sec": int,
                     "started_at": str, "status": str,
                     "transcript_snippet": str}
                ]
            }, ...
        },
        "shortest_overall": [...]
      }
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    # Per-campaign aggregates — campaign_name lives on gads_call_view, not mango_calls
    agg_sql = """
      SELECT
        COALESCE(NULLIF(gcv.campaign_name,''),'(unattributed)') AS campaign,
        COUNT(*)                                                  AS total_calls,
        SUM(CASE WHEN mc.duration_sec < ? THEN 1 ELSE 0 END)     AS short_calls,
        SUM(CASE WHEN mc.status IN ('missed','no-answer','no_answer','abandoned','voicemail')
                  OR mc.duration_sec = 0 THEN 1 ELSE 0 END)      AS missed_calls,
        AVG(mc.duration_sec)                                      AS avg_duration
      FROM mango_calls mc
      LEFT JOIN gads_call_view gcv ON gcv.call_id = mc.gads_call_id
      WHERE mc.started_at >= ?
        AND mc.gads_call_id IS NOT NULL
        AND mc.gads_call_id != ''
      GROUP BY campaign
    """
    by_camp: dict = {}
    for r in conn.execute(agg_sql, (SHORT_CALL_SEC, cutoff)):
        total = int(r["total_calls"] or 0)
        if total == 0:
            continue
        short = int(r["short_calls"] or 0)
        by_camp[r["campaign"]] = {
            "total_calls":      total,
            "short_calls":      short,
            "short_pct":        round(short / total, 3),
            "missed_calls":     int(r["missed_calls"] or 0),
            "avg_duration_sec": int(round(r["avg_duration"] or 0)),
            "shortest":         [],
        }

    # Per-campaign top-N shortest with transcript snippet
    short_sql = """
      SELECT
        mc.uuid, mc.duration_sec, mc.started_at, mc.status,
        SUBSTR(COALESCE(mc.call_transcript, mc.call_summary, ''), 1, 240) AS snippet,
        COALESCE(NULLIF(gcv.campaign_name,''),'(unattributed)') AS campaign
      FROM mango_calls mc
      LEFT JOIN gads_call_view gcv ON gcv.call_id = mc.gads_call_id
      WHERE mc.started_at >= ?
        AND mc.gads_call_id IS NOT NULL
        AND mc.gads_call_id != ''
        AND mc.duration_sec < ?
      ORDER BY mc.duration_sec ASC, mc.started_at DESC
    """
    overall: list = []
    for r in conn.execute(short_sql, (cutoff, SHORT_CALL_SEC)):
        item = {
            "uuid":               r["uuid"],
            "campaign":           r["campaign"],
            "duration_sec":       int(r["duration_sec"] or 0),
            "started_at":         r["started_at"],
            "status":             r["status"] or "",
            "transcript_snippet": (r["snippet"] or "").strip(),
        }
        camp = r["campaign"]
        if camp in by_camp and len(by_camp[camp]["shortest"]) < TOP_N_SHORT_CALLS:
            by_camp[camp]["shortest"].append(item)
        if len(overall) < TOP_N_SHORT_CALLS:
            overall.append(item)

    return {"by_campaign": by_camp, "shortest_overall": overall}


# ─── Bad search terms ─────────────────────────────────────────────────────────
def collect_bad_search_terms(conn, days: int = 30) -> dict:
    """
    Search terms with $5+ spend that produced 0 attributed leads, classified by reason.

    Returns:
      {
        "by_campaign": {
            "<campaign>": [
                {"term": str, "cost": float, "clicks": int, "impressions": int,
                 "reason": "spanish"|"competitor"|"wrong_intent"|"zero_lead",
                 "flag": str}
            ]
        },
        "totals": {"terms_flagged": int, "wasted_spend_usd": float}
      }
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    # Terms that actually produced leads in this window
    lead_terms = {
        row[0] for row in conn.execute("""
          SELECT DISTINCT LOWER(TRIM(search_term))
          FROM leads
          WHERE created_at >= ?
            AND search_term IS NOT NULL
            AND search_term != ''
        """, (cutoff,))
    }

    # Pull from gads_search_terms_cache (already populated by sync)
    # Try days-based lookup first, fall back to date range
    st_rows = list(conn.execute("""
        SELECT search_term, campaign_name, cost, clicks, impressions, conversions
        FROM gads_search_terms_cache
        WHERE days = ?
          AND cost >= ?
    """, (days, MIN_SPEND_FOR_BAD_TERM)))

    if not st_rows:
        # Fallback: try directly from any cached rows (aggregate to avoid dupes across days partitions)
        st_rows = list(conn.execute("""
            SELECT search_term,
                   campaign_name,
                   SUM(cost)        AS cost,
                   SUM(clicks)      AS clicks,
                   SUM(impressions) AS impressions,
                   0                AS conversions
            FROM gads_search_terms_cache
            GROUP BY search_term, campaign_name
            HAVING SUM(cost) >= ?
        """, (MIN_SPEND_FOR_BAD_TERM,)))

    by_camp: dict = {}
    flagged_total = 0
    wasted = 0.0

    for r in st_rows:
        term = (r["search_term"] or "").strip()
        if not term:
            continue
        if term.lower() in lead_terms:
            continue  # produced a lead → not bad
        # Classify
        if _SPANISH_RX.search(term):
            reason, flag = "spanish", "ES-language intent — not our service area"
        elif _COMPETITOR_RX.search(term):
            reason, flag = "competitor", "Competitor brand search"
        elif _WRONG_INTENT_RX.search(term):
            reason, flag = "wrong_intent", "Info/education intent, not booking"
        else:
            reason, flag = "zero_lead", "Has spend but 0 attributed leads"
        camp = (r["campaign_name"] or "(unknown)").strip()
        by_camp.setdefault(camp, []).append({
            "term":        term,
            "cost":        round(float(r["cost"] or 0), 2),
            "clicks":      int(r["clicks"] or 0),
            "impressions": int(r["impressions"] or 0),
            "reason":      reason,
            "flag":        flag,
        })
        flagged_total += 1
        wasted += float(r["cost"] or 0)

    # Trim each campaign to top N by cost
    for camp in by_camp:
        by_camp[camp] = sorted(by_camp[camp], key=lambda x: -x["cost"])[:TOP_N_BAD_TERMS]

    return {
        "by_campaign": by_camp,
        "totals": {
            "terms_flagged":    flagged_total,
            "wasted_spend_usd": round(wasted, 2),
        },
    }


# ─── Schedule waste ───────────────────────────────────────────────────────────
def collect_schedule_waste(conn, days: int = 30) -> dict:
    """
    Calls by hour-of-day and day-of-week with quality metrics.
    Marks slots outside practice_hours.

    Returns:
      {
        "by_hour": [{"hour": int, "calls": int, "short_calls": int,
                     "missed": int, "in_office_hours": bool}],
        "by_dow":  [{"dow": int, "dow_name": str, "calls": int,
                     "short_calls": int, "in_office_hours": bool}],
        "hotspots": [{"dow": int, "hour": int, "calls": int, "short_calls": int,
                      "short_pct": float, "in_office_hours": bool, "flag": str}],
        "practice_hours_raw": str
      }
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    ph_row = conn.execute(
        "SELECT value FROM settings WHERE key='practice_hours'"
    ).fetchone()
    ph_raw = ph_row[0] if ph_row else ""
    open_hours = _parse_practice_hours(ph_raw)

    # By hour
    hour_sql = """
      SELECT
        CAST(strftime('%H', started_at) AS INTEGER) AS hour,
        COUNT(*)                                    AS calls,
        SUM(CASE WHEN duration_sec < ? THEN 1 ELSE 0 END) AS short_calls,
        SUM(CASE WHEN status IN ('missed','no-answer','no_answer','abandoned','voicemail')
                  OR duration_sec = 0 THEN 1 ELSE 0 END) AS missed
      FROM mango_calls
      WHERE started_at >= ?
        AND gads_call_id IS NOT NULL AND gads_call_id != ''
      GROUP BY hour
      ORDER BY hour
    """
    by_hour = []
    for r in conn.execute(hour_sql, (SHORT_CALL_SEC, cutoff)):
        h = int(r["hour"] or 0)
        in_oh = _hour_in_any_dow_range(h, open_hours)
        by_hour.append({
            "hour":            h,
            "calls":           int(r["calls"] or 0),
            "short_calls":     int(r["short_calls"] or 0),
            "missed":          int(r["missed"] or 0),
            "in_office_hours": in_oh,
        })

    # By day-of-week
    dow_sql = """
      SELECT
        CAST(strftime('%w', started_at) AS INTEGER) AS dow,
        COUNT(*)                                    AS calls,
        SUM(CASE WHEN duration_sec < ? THEN 1 ELSE 0 END) AS short_calls
      FROM mango_calls
      WHERE started_at >= ?
        AND gads_call_id IS NOT NULL AND gads_call_id != ''
      GROUP BY dow
    """
    dow_map = {i: {"dow": i, "dow_name": _DOW_NAMES[i], "calls": 0,
                   "short_calls": 0, "in_office_hours": i in open_hours}
               for i in range(7)}
    for r in conn.execute(dow_sql, (SHORT_CALL_SEC, cutoff)):
        i = int(r["dow"] or 0)
        dow_map[i]["calls"]       = int(r["calls"] or 0)
        dow_map[i]["short_calls"] = int(r["short_calls"] or 0)
    by_dow = [dow_map[i] for i in range(7)]

    # Hotspots: (dow, hour) with high short_pct or outside office hours
    hotspot_sql = """
      SELECT
        CAST(strftime('%w', started_at) AS INTEGER) AS dow,
        CAST(strftime('%H', started_at) AS INTEGER) AS hour,
        COUNT(*) AS calls,
        SUM(CASE WHEN duration_sec < ? THEN 1 ELSE 0 END) AS short_calls
      FROM mango_calls
      WHERE started_at >= ?
        AND gads_call_id IS NOT NULL AND gads_call_id != ''
      GROUP BY dow, hour
      HAVING calls >= 3
    """
    hot_rows = []
    for r in conn.execute(hotspot_sql, (SHORT_CALL_SEC, cutoff)):
        dow_i = int(r["dow"] or 0)
        h     = int(r["hour"] or 0)
        calls = int(r["calls"] or 0)
        short = int(r["short_calls"] or 0)
        short_pct = round(short / calls, 3) if calls else 0
        in_oh = _hour_in_dow_range(h, open_hours.get(dow_i))
        flags = []
        if not in_oh:
            flags.append("outside_office_hours")
        if short_pct >= 0.5:
            flags.append("high_short_pct")
        hot_rows.append({
            "dow":             dow_i,
            "dow_name":        _DOW_NAMES[dow_i],
            "hour":            h,
            "calls":           calls,
            "short_calls":     short,
            "short_pct":       short_pct,
            "in_office_hours": in_oh,
            "flag":            ",".join(flags) or "ok",
        })
    hot_rows.sort(key=lambda x: (
        0 if not x["in_office_hours"] else 1,
        -x["short_pct"],
        -x["calls"],
    ))
    return {
        "by_hour":            by_hour,
        "by_dow":             by_dow,
        "hotspots":           hot_rows[:TOP_N_HOTSPOTS],
        "practice_hours_raw": ph_raw,
    }


# ─── Cold lead root causes ────────────────────────────────────────────────────
def collect_cold_lead_causes(conn, days: int = 30) -> dict:
    """
    Cold rate by utm_campaign / source / keyword + time-to-first-contact delta.

    Returns:
      {
        "by_utm_campaign": [{"utm_campaign": str, "leads": int, "cold": int, "cold_rate": float}],
        "by_source":       [...],
        "by_keyword":      [...],
        "time_to_first_contact_min": {"cold_median": float, "converted_median": float},
        "no_staff_contact_pct": float
      }
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    def _grouped(col_expr: str, label: str) -> list:
        sql = f"""
          SELECT
            COALESCE(NULLIF({col_expr},''),'(none)') AS bucket,
            COUNT(*) AS leads,
            SUM(CASE WHEN stage='cold' OR cold_at != '' THEN 1 ELSE 0 END) AS cold
          FROM leads
          WHERE created_at >= ?
          GROUP BY bucket
          HAVING leads >= 3
        """
        results = []
        for r in conn.execute(sql, (cutoff,)):
            leads = int(r["leads"] or 0)
            cold  = int(r["cold"] or 0)
            results.append({
                label:        r["bucket"],
                "leads":      leads,
                "cold":       cold,
                "cold_rate":  round(cold / leads, 3) if leads else 0,
            })
        return sorted(results, key=lambda x: -x["cold_rate"])[:15]

    out = {
        "by_utm_campaign": _grouped("utm_campaign", "utm_campaign"),
        "by_source":       _grouped("source",       "source"),
        "by_keyword":      _grouped("keyword_text", "keyword"),
    }

    # Time-to-first-staff-contact (minutes): cold vs converted
    tt_sql = """
      SELECT
        CASE WHEN stage='cold' OR cold_at != '' THEN 'cold'
             WHEN stage IN ('showed','tx_presented','tx_accepted','tx_completed')
                  THEN 'converted'
             ELSE 'other' END AS bucket,
        (julianday(last_staff_contact_at) - julianday(created_at)) * 24 * 60
          AS minutes_to_contact
      FROM leads
      WHERE created_at >= ?
        AND last_staff_contact_at IS NOT NULL
        AND last_staff_contact_at != ''
    """
    cold_mins, conv_mins = [], []
    for r in conn.execute(tt_sql, (cutoff,)):
        m = r["minutes_to_contact"]
        if m is None or m < 0:
            continue
        if r["bucket"] == "cold":
            cold_mins.append(m)
        elif r["bucket"] == "converted":
            conv_mins.append(m)

    out["time_to_first_contact_min"] = {
        "cold_median":      _median(cold_mins),
        "converted_median": _median(conv_mins),
    }

    # % of cold leads that never had staff contact
    nc_row = conn.execute("""
      SELECT
        SUM(CASE WHEN (stage='cold' OR cold_at != '')
                  AND (last_staff_contact_at IS NULL OR last_staff_contact_at = '')
                 THEN 1 ELSE 0 END) AS no_contact_cold,
        SUM(CASE WHEN stage='cold' OR cold_at != '' THEN 1 ELSE 0 END) AS cold_total
      FROM leads
      WHERE created_at >= ?
    """, (cutoff,)).fetchone()
    nc = int(nc_row["no_contact_cold"] or 0) if nc_row else 0
    ct = int(nc_row["cold_total"] or 0) if nc_row else 0
    out["no_staff_contact_pct"] = round(nc / ct, 3) if ct else 0.0
    return out


# ─── No-show patterns ─────────────────────────────────────────────────────────
def collect_no_show_patterns(conn, days: int = 30) -> dict:
    """
    No-show rate by campaign/source + reminder stats + lead age at booking.

    Returns:
      {
        "by_campaign": [{"campaign": str, "booked": int, "no_shows": int, "no_show_rate": float}],
        "by_source":   [...],
        "reminders": {"no_show_no_reminders_pct": float, "showed_no_reminders_pct": float},
        "lead_age_at_booking_days": {"no_show_median": float, "showed_median": float}
      }
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    def _grouped(col_expr: str, label: str) -> list:
        sql = f"""
          SELECT
            COALESCE(NULLIF({col_expr},''),'(none)') AS bucket,
            SUM(CASE WHEN booking_id != '' OR scheduled_at != '' THEN 1 ELSE 0 END) AS booked,
            SUM(CASE WHEN no_show_count > 0 OR appointment_status='broken'
                          OR no_show_at != '' THEN 1 ELSE 0 END) AS no_shows
          FROM leads
          WHERE created_at >= ?
          GROUP BY bucket
          HAVING booked >= 3
        """
        rows = []
        for r in conn.execute(sql, (cutoff,)):
            b  = int(r["booked"] or 0)
            ns = int(r["no_shows"] or 0)
            rows.append({
                label:          r["bucket"],
                "booked":       b,
                "no_shows":     ns,
                "no_show_rate": round(ns / b, 3) if b else 0,
            })
        return sorted(rows, key=lambda x: -x["no_show_rate"])[:15]

    out = {
        "by_campaign": _grouped("campaign_name", "campaign"),
        "by_source":   _grouped("source",        "source"),
    }

    # Reminder counts (communication_log with reminder template)
    try:
        rem_sql = """
          SELECT
            CASE WHEN l.no_show_count > 0 OR l.appointment_status='broken' OR l.no_show_at != ''
                 THEN 'no_show' ELSE 'showed' END AS bucket,
            l.id AS lead_id,
            (SELECT COUNT(*) FROM communication_log cl
              WHERE cl.lead_id = l.id
                AND cl.template LIKE '%reminder%') AS reminder_count
          FROM leads l
          WHERE l.created_at >= ?
            AND (l.booking_id != '' OR l.scheduled_at != '')
            AND (l.no_show_count > 0 OR l.appointment_status='broken' OR l.no_show_at != ''
                 OR l.appointment_status='complete' OR l.showed_at != '')
        """
        ns_total, ns_zero, sh_total, sh_zero = 0, 0, 0, 0
        for r in conn.execute(rem_sql, (cutoff,)):
            if r["bucket"] == "no_show":
                ns_total += 1
                if (r["reminder_count"] or 0) == 0:
                    ns_zero += 1
            else:
                sh_total += 1
                if (r["reminder_count"] or 0) == 0:
                    sh_zero += 1
        out["reminders"] = {
            "no_show_no_reminders_pct": round(ns_zero / ns_total, 3) if ns_total else 0,
            "showed_no_reminders_pct":  round(sh_zero / sh_total, 3) if sh_total else 0,
        }
    except Exception as e:
        logger.debug(f"LQI: reminder query failed (non-fatal): {e}")
        out["reminders"] = {"no_show_no_reminders_pct": 0, "showed_no_reminders_pct": 0}

    # Lead age at booking
    age_sql = """
      SELECT
        CASE WHEN no_show_count > 0 OR appointment_status='broken' OR no_show_at != ''
             THEN 'no_show'
             WHEN appointment_status='complete' OR showed_at != '' THEN 'showed'
             ELSE 'other' END AS bucket,
        (julianday(scheduled_at) - julianday(created_at)) AS days_to_book
      FROM leads
      WHERE created_at >= ?
        AND scheduled_at != ''
    """
    ns_age, sh_age = [], []
    for r in conn.execute(age_sql, (cutoff,)):
        d = r["days_to_book"]
        if d is None or d < 0:
            continue
        if r["bucket"] == "no_show":
            ns_age.append(d)
        elif r["bucket"] == "showed":
            sh_age.append(d)

    out["lead_age_at_booking_days"] = {
        "no_show_median": _median(ns_age),
        "showed_median":  _median(sh_age),
    }
    return out


# ─── Geo signals ─────────────────────────────────────────────────────────────
def collect_geo_signals(conn, days: int = 30) -> dict:
    """
    Per-campaign geo intelligence from gads_geo_cache.

    Returns leakage alerts, low/high-perf locations, distance-band rollups.
    Never raises — returns {} on any failure.
    """
    try:
        rows = list(conn.execute("""
            SELECT location_name, location_type, campaign_name, campaign_resource,
                   impressions, clicks, cost, conversions, cpc, conversion_rate,
                   days, view_type, zip_code, distance_band, synced_at
            FROM gads_geo_cache
            WHERE days = ?
        """, (days,)))

        if not rows:
            # Fallback: use whatever is cached
            rows = list(conn.execute("""
                SELECT location_name, location_type, campaign_name, campaign_resource,
                       impressions, clicks, cost, conversions, cpc, conversion_rate,
                       days, view_type, zip_code, distance_band, synced_at
                FROM gads_geo_cache
            """))

        if not rows:
            return {}

        def _classify_camp_type(name: str) -> str:
            n = name.lower()
            if any(t in n for t in ["implant", "all-on", "all on"]):
                return "IMPLANTS"
            if any(t in n for t in ["emergency", "urgent", "same day", "toothache"]):
                return "EMERGENCY"
            if any(t in n for t in ["invisalign", "clear aligner", "ortho", "veneer",
                                     "cosmetic", "smile makeover", "whitening"]):
                return "ELECTIVE"
            if any(t in n for t in ["grafton dental", "brand", "branded"]):
                return "BRAND"
            return "GENERAL"

        # Group rows by campaign
        camps: dict = {}
        for r in rows:
            cn = (r["campaign_name"] or "(unknown)").strip()
            camps.setdefault(cn, {"targeted": [], "physical": []})
            view = (r["view_type"] or "").lower()
            clicks      = int(r["clicks"] or 0)
            cost        = float(r["cost"] or 0)
            convs       = float(r["conversions"] or 0)
            impr        = int(r["impressions"] or 0)
            loc         = (r["location_name"] or "").strip()
            band        = (r["distance_band"] or "").strip()
            total_clicks = clicks
            cvr_pct = round(convs / total_clicks * 100, 2) if total_clicks > 0 else 0.0
            cpl     = round(cost / convs, 2) if convs > 0 else 0.0
            entry = {
                "location_name": loc,
                "distance_band": band,
                "impressions":   impr,
                "clicks":        clicks,
                "cost":          round(cost, 2),
                "conversions":   convs,
                "cvr_pct":       cvr_pct,
                "cpl":           cpl,
            }
            if view == "targeted":
                camps[cn]["targeted"].append(entry)
            elif view == "physical":
                camps[cn]["physical"].append(entry)

        by_campaign: dict = {}
        total_targeted = 0
        total_leakage  = 0
        total_low_perf = 0

        for cn, data in camps.items():
            targeted = sorted(data["targeted"], key=lambda x: -x["cost"])
            physical = sorted(data["physical"], key=lambda x: -x["cost"])

            # Campaign-level CVR (across all targeted rows)
            t_clicks = sum(e["clicks"] for e in targeted)
            t_convs  = sum(e["conversions"] for e in targeted)
            t_cost   = sum(e["cost"] for e in targeted)
            camp_avg_cvr = round(t_convs / t_clicks * 100, 2) if t_clicks > 0 else 0.0

            # Leakage: physical rows with conversions >= 1 not matched in targeted names
            targeted_names = {e["location_name"].lower() for e in targeted}
            leakage_alerts = []
            for e in physical:
                if e["conversions"] >= 1 and e["location_name"].lower() not in targeted_names:
                    leakage_alerts.append({
                        "location_name": e["location_name"],
                        "distance_band": e["distance_band"],
                        "clicks":        e["clicks"],
                        "cost":          e["cost"],
                        "conversions":   e["conversions"],
                        "msg":           f"Converts from {e['location_name']} but not in targeted list",
                    })

            # Low / high perf (clicks >= 30 floor)
            low_perf_locs  = []
            high_perf_locs = []
            for e in targeted:
                if e["clicks"] < 30:
                    continue
                if camp_avg_cvr > 0 and e["cvr_pct"] < camp_avg_cvr * 0.5:
                    low_perf_locs.append({
                        "location_name": e["location_name"],
                        "distance_band": e["distance_band"],
                        "clicks":        e["clicks"],
                        "cvr_pct":       e["cvr_pct"],
                        "camp_avg_cvr":  camp_avg_cvr,
                        "cost":          e["cost"],
                    })
                elif camp_avg_cvr > 0 and e["cvr_pct"] >= camp_avg_cvr * 1.2:
                    high_perf_locs.append({
                        "location_name": e["location_name"],
                        "distance_band": e["distance_band"],
                        "clicks":        e["clicks"],
                        "cvr_pct":       e["cvr_pct"],
                        "camp_avg_cvr":  camp_avg_cvr,
                    })

            # Distance-band rollup (targeted only)
            band_map: dict = {}
            for e in targeted:
                b = e["distance_band"] or "unknown"
                if b not in band_map:
                    band_map[b] = {"band": b, "clicks": 0, "cost": 0.0,
                                   "conversions": 0.0}
                band_map[b]["clicks"]      += e["clicks"]
                band_map[b]["cost"]        += e["cost"]
                band_map[b]["conversions"] += e["conversions"]
            by_distance_band = []
            for bd in sorted(band_map.values(), key=lambda x: x["band"]):
                bc = bd["clicks"]
                bconv = bd["conversions"]
                by_distance_band.append({
                    "band":        bd["band"],
                    "clicks":      bc,
                    "cost":        round(bd["cost"], 2),
                    "conversions": bconv,
                    "cvr_pct":     round(bconv / bc * 100, 2) if bc > 0 else 0.0,
                    "cpl":         round(bd["cost"] / bconv, 2) if bconv > 0 else 0.0,
                })

            total_targeted += len(targeted)
            total_leakage  += len(leakage_alerts)
            total_low_perf += len(low_perf_locs)

            by_campaign[cn] = {
                "campaign_type":         _classify_camp_type(cn),
                "targeted":              targeted,
                "physical":              physical,
                "leakage_alerts":        leakage_alerts,
                "low_perf_locs":         low_perf_locs,
                "high_perf_locs":        high_perf_locs,
                "by_distance_band":      by_distance_band,
                "camp_avg_cvr":          camp_avg_cvr,
                "camp_total_cost":       round(t_cost, 2),
                "camp_total_conversions": t_convs,
            }

        return {
            "by_campaign": by_campaign,
            "account": {
                "total_locations_targeted": total_targeted,
                "leakage_alerts_count":     total_leakage,
                "low_perf_count":           total_low_perf,
            },
        }
    except Exception as e:
        logger.warning(f"LQI: collect_geo_signals failed (non-fatal): {e}")
        return {}


# ─── Helpers ──────────────────────────────────────────────────────────────────
def _median(xs: list) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    n = len(xs)
    return round(xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2, 1)


def _parse_practice_hours(raw: str) -> dict:
    """
    Best-effort parse of practice_hours setting. Accepts strings like:
      "Mon-Fri 8-5, Sat 9-2"
      "Mon-Thu 10am-6pm, Fri 10am-5pm, Sat 10am-2pm"
    Returns {dow_int: (open_hour, close_hour)} using 0=Sun .. 6=Sat.
    """
    if not raw:
        return {}
    name_to_dow = {
        "sun": 0, "mon": 1, "tue": 2, "tues": 2, "wed": 3,
        "thu": 4, "thur": 4, "thurs": 4, "fri": 5, "sat": 6,
    }
    out: dict = {}
    raw_l = raw.lower().replace("–", "-").replace("—", "-")
    for chunk in re.split(r"[;,]", raw_l):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = re.match(
            r"(\w+)(?:\s*-\s*(\w+))?\s+(\d{1,2})(?:a|am|:00)?\s*-\s*(\d{1,2})(?:p|pm|:00)?",
            chunk,
        )
        if not m:
            continue
        d1, d2, o, c = m.group(1), m.group(2), int(m.group(3)), int(m.group(4))
        if c <= o:
            c += 12  # "8-5" → 8:00–17:00
        start_d = name_to_dow.get(d1[:3])
        end_d   = name_to_dow.get((d2 or d1)[:3], start_d)
        if start_d is None or end_d is None:
            continue
        days = range(start_d, end_d + 1) if start_d <= end_d else (
            list(range(start_d, 7)) + list(range(0, end_d + 1))
        )
        for d in days:
            out[d] = (o, c)
    return out


def _hour_in_dow_range(hour: int, rng) -> bool:
    if not rng:
        return False
    o, c = rng
    return o <= hour < c


def _hour_in_any_dow_range(hour: int, open_hours: dict) -> bool:
    if not open_hours:
        return False
    return any(_hour_in_dow_range(hour, r) for r in open_hours.values())


# ─── Existing-patient call noise (PR 5) ──────────────────────────────────────
def collect_existing_patient_calls(conn, days: int = 30) -> dict:
    """
    Per-campaign count of GAds calls where the caller is already an active or
    inactive OD patient. These calls are not new-patient acquisitions — they're
    existing patients reaching the office through the ad number. High volume on
    a campaign is a signal that the search terms / match types may be attracting
    the current patient base rather than net-new prospects.

    Returns:
      {
        "by_campaign": {
            "<campaign_name>": {
                "existing_calls": int,   # od_patient_status in existing_active|existing_inactive
                "total_calls":    int,   # all GAds-attributed mango_calls in window
                "existing_pct":   float, # existing_calls / total_calls (0–1)
                "top_terms":      [str], # up to 3 search terms / keywords that produced these calls
            }, ...
        },
        "totals": {
            "existing_calls": int,
            "total_calls":    int,
        }
      }
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    agg_sql = """
      SELECT
        COALESCE(NULLIF(l.utm_campaign,''),'(unattributed)') AS campaign,
        COUNT(*)                                               AS total_calls,
        SUM(CASE WHEN mc.od_patient_status IN ('existing_active','existing_inactive')
                 THEN 1 ELSE 0 END)                           AS existing_calls
      FROM mango_calls mc
      LEFT JOIN leads l ON l.id = mc.lead_id
      WHERE mc.started_at >= ?
        AND (mc.gads_call_id IS NOT NULL AND mc.gads_call_id != '')
      GROUP BY campaign
    """
    by_camp: dict = {}
    tot_existing = 0
    tot_all = 0
    try:
        for r in conn.execute(agg_sql, (cutoff,)):
            total = int(r["total_calls"] or 0)
            existing = int(r["existing_calls"] or 0)
            if total == 0:
                continue
            tot_all += total
            tot_existing += existing
            by_camp[r["campaign"]] = {
                "existing_calls": existing,
                "total_calls":    total,
                "existing_pct":   round(existing / total, 3) if total else 0.0,
                "top_terms":      [],
            }
    except Exception as e:
        logger.debug(f"LQI existing_patient_calls agg failed: {e}")
        return {"by_campaign": {}, "totals": {"existing_calls": 0, "total_calls": 0}}

    # Top keywords producing existing-patient calls per campaign
    try:
        term_sql = """
          SELECT
            COALESCE(NULLIF(l.utm_campaign,''),'(unattributed)') AS campaign,
            COALESCE(NULLIF(l.keyword_text,''), NULLIF(l.utm_term,''), '(unknown)') AS term,
            COUNT(*) AS n
          FROM mango_calls mc
          LEFT JOIN leads l ON l.id = mc.lead_id
          WHERE mc.started_at >= ?
            AND mc.od_patient_status IN ('existing_active','existing_inactive')
          GROUP BY campaign, term
          ORDER BY n DESC
        """
        per_camp_terms: dict = {}
        for r in conn.execute(term_sql, (cutoff,)):
            per_camp_terms.setdefault(r["campaign"], []).append(r["term"])
        for camp, terms in per_camp_terms.items():
            if camp in by_camp:
                by_camp[camp]["top_terms"] = terms[:3]
    except Exception as e:
        logger.debug(f"LQI existing_patient_calls top_terms failed (non-fatal): {e}")

    return {
        "by_campaign": by_camp,
        "totals": {
            "existing_calls": tot_existing,
            "total_calls":    tot_all,
        },
    }


# ─── Entry point ──────────────────────────────────────────────────────────────
def collect_all(days: int = 30) -> dict:
    """
    Top-level entry point called once per optimizer run.

    Returns:
      {
        "sources":      {...},
        "calls":        {...},
        "search_terms": {...},
        "schedule":     {...},
        "cold_leads":   {...},
        "no_shows":     {...},
        "meta":         {"days": int, "generated_at": str}
      }

    Never raises. Each collector falls back to {} on failure.
    """
    import sqlite3 as _sqlite3
    from config import get_settings as _get_settings

    out: dict = {
        "meta": {
            "days":         days,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    }
    collectors = [
        ("sources",                collect_source_quality),
        ("calls",                  collect_short_calls),
        ("search_terms",           collect_bad_search_terms),
        ("schedule",               collect_schedule_waste),
        ("cold_leads",             collect_cold_lead_causes),
        ("no_shows",               collect_no_show_patterns),
        ("geo",                    collect_geo_signals),
        ("existing_patient_calls", collect_existing_patient_calls),  # PR 5
    ]

    try:
        settings = _get_settings()
        conn = _sqlite3.connect(settings.db_path)
        conn.row_factory = _sqlite3.Row
        try:
            for key, fn in collectors:
                try:
                    out[key] = fn(conn, days=days)
                except Exception as e:
                    logger.warning(f"LQI: collector '{key}' failed (non-fatal): {e}")
                    out[key] = {}
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"LQI: collect_all failed to open DB (non-fatal): {e}")
        for key, _ in collectors:
            out.setdefault(key, {})

    return out
