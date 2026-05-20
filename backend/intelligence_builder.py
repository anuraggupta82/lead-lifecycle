"""
intelligence_builder.py — Phase A Feedback Loop

Nightly job that joins GAds keyword stats + OD production + GA4 sessions
into the keyword_intelligence denormalized table. Runs at 6:30 AM after
GA4 pull (5:30) and GAds sync (6:00) have refreshed source data.

GA4 source: ga4_cache stores a single JSON blob per report in the 'data' column.
The blob contains 'leads_by_campaign' (list of {keyword, campaign, sessions, leads})
which is the best per-keyword signal we have. We parse this in Python.

Session Quality Score (SQS) formula — 0–100 composite:
  - Lead event component (0–50 pts): leads/sessions ≥ 5% = 50, 2% = 25, 0% = 0
    (heaviest weight — only directly-causal signal; dental callers skip form submit)
  - Duration component (0–30 pts): avg ≥ 120s = 30, 60s = 15, <30s = 3
  - Bounce component  (0–20 pts): bounce_rate ≤ 20% = 20, 50% = 10, ≥ 80% = 0

Confidence tiers (for optimizer to weight recommendations):
  - low:    < 14 days of keyword exposure (use CTR only, ignore cost signals)
  - medium: 14–90 days
  - high:   90+ days (ROAS is reliable, full scoring enabled)

Keyword exposure age is derived from gads_keywords_cache.synced_at (earliest sync),
NOT from OD production first_seen (which defaults to today for non-converting keywords).
"""

import json
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)


def _compute_sqs(avg_duration_sec: float, bounce_rate: float, lead_events: int, sessions: int) -> float:
    """
    Compute Session Quality Score (0–100).
    Lead events weighted heaviest (50pts) — form submissions are the most direct signal.
    Duration (30pts) and bounce (20pts) are supporting signals.
    bounce_rate expected as 0.0–1.0 decimal (GA4 API format).
    """
    # Lead event component (0–50) — primary signal
    lead_rate = (lead_events / sessions) if sessions > 0 else 0.0
    if lead_rate >= 0.05:
        lead_score = 50.0
    elif lead_rate >= 0.02:
        lead_score = 25.0 + (lead_rate - 0.02) / 0.03 * 25.0
    elif lead_rate > 0:
        lead_score = lead_rate / 0.02 * 25.0
    else:
        lead_score = 0.0

    # Duration component (0–30)
    if avg_duration_sec >= 120:
        dur_score = 30.0
    elif avg_duration_sec >= 60:
        dur_score = 15.0 + (avg_duration_sec - 60) / 60 * 15.0
    elif avg_duration_sec >= 30:
        dur_score = 3.0 + (avg_duration_sec - 30) / 30 * 12.0
    else:
        dur_score = max(0.0, avg_duration_sec / 30 * 3.0)

    # Bounce component (0–20); bounce_rate is 0.0–1.0
    bounce_pct = bounce_rate * 100
    if bounce_pct <= 20:
        bounce_score = 20.0
    elif bounce_pct <= 50:
        bounce_score = 20.0 - (bounce_pct - 20) / 30 * 10.0
    elif bounce_pct <= 80:
        bounce_score = 10.0 - (bounce_pct - 50) / 30 * 10.0
    else:
        bounce_score = 0.0

    return round(min(100.0, lead_score + dur_score + bounce_score), 2)


def _confidence_tier(synced_at_iso: str, today: str) -> tuple:
    """
    Return (data_age_days, confidence_tier) based on keyword's earliest GAds sync date.
    Uses synced_at from gads_keywords_cache — NOT OD production first_seen.
    Keywords with zero OD matches still get correct age from GAds exposure history.
    """
    try:
        # synced_at is ISO timestamp like "2026-04-01T06:00:00+00:00"
        d0 = datetime.strptime(synced_at_iso[:10], "%Y-%m-%d")
        d1 = datetime.strptime(today, "%Y-%m-%d")
        age = max(0, (d1 - d0).days)
    except Exception:
        age = 0
    if age >= 90:
        tier = "high"
    elif age >= 14:
        tier = "medium"
    else:
        tier = "low"
    return age, tier


def _extract_ga4_keyword_map(cached_blob: dict) -> dict:
    """
    Parse the ga4_cache JSON blob and extract per-keyword GA4 signals.

    Primary source: blob['leads_by_campaign'] — list of:
      {keyword, campaign, ad_group, sessions, leads, lead_rate, property_domain}

    Returns dict: {keyword_lower: {sessions, lead_events, avg_dur, bounce}}

    Note: the full GA4 blob doesn't include per-keyword avg_duration or bounce_rate
    (those are site-wide from fetch_site_overview). We use the overview for site-wide
    defaults and leads_by_campaign for keyword-specific lead counts + sessions.
    """
    ga4_map = {}
    if not cached_blob or not isinstance(cached_blob, dict):
        return ga4_map

    # Site-wide engagement signals (used as fallback per keyword)
    overview = cached_blob.get("overview") or {}
    site_avg_dur = float(overview.get("avg_session_duration_sec", 0) or 0)
    # GA4 engagement_rate = engaged/sessions; bounce ≈ 1 - engagement_rate
    engagement_rate = float(overview.get("engagement_rate", 0) or 0) / 100.0
    site_bounce = max(0.0, 1.0 - engagement_rate)

    # Per-keyword lead + session data from leads_by_campaign
    leads_by_campaign = cached_blob.get("leads_by_campaign") or []
    for row in leads_by_campaign:
        kw = (row.get("keyword") or "").lower().strip()
        if not kw or kw in ("(not set)", "(not provided)"):
            continue
        sessions = int(row.get("sessions") or 0)
        lead_count = int(row.get("leads") or 0)
        if kw in ga4_map:
            # Multiple ad groups / properties can report the same keyword — aggregate
            ga4_map[kw]["sessions"] += sessions
            ga4_map[kw]["lead_events"] += lead_count
        else:
            ga4_map[kw] = {
                "sessions": sessions,
                "lead_events": lead_count,
                # Use site-wide defaults for duration/bounce (per-keyword not available)
                "avg_dur": site_avg_dur,
                "bounce": site_bounce,
            }

    return ga4_map


def rebuild_keyword_intelligence() -> dict:
    """
    Full rebuild of keyword_intelligence table.
    Joins:
      1. gads_keywords_cache (30d GAds stats — impressions, clicks, cost, QS, IS)
      2. keyword_production_log (90d OD production events per keyword)
      3. ga4_cache JSON blob (leads_by_campaign for per-keyword GA4 signals)
      4. gads_audit_log (90d Apply/Reject decisions per entity, keyed by operation)

    Returns dict with counts for logging.
    """
    from database import upsert_keyword_intelligence, _conn, get_ga4_cache

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cutoff_90d = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")

    rows_built = 0
    errors = 0

    try:
        # ── 4. Load GA4 cache blob and extract keyword map ────────────────────
        # Done outside the conn context manager to avoid nesting issues
        ga4_blob = get_ga4_cache("full_report", 30, max_age_hours=26)
        ga4_map = _extract_ga4_keyword_map(ga4_blob)
        logger.info(f"[ki_builder] GA4 map: {len(ga4_map)} keywords with session data")

        with _conn() as conn:
            # ── 1. Load GAds keyword stats (primary source) ──────────────────
            kw_rows = conn.execute("""
                SELECT keyword_text, match_type, campaign_name, ad_group_name,
                       impressions, clicks, cost, avg_cpc, conversions,
                       quality_score, impression_share, synced_at
                FROM gads_keywords_cache
                WHERE days = 30
                ORDER BY clicks DESC
            """).fetchall()

            if not kw_rows:
                logger.info("[ki_builder] No keyword cache rows — skipping rebuild")
                return {"rebuilt": 0, "errors": 0, "skipped": True}

            # ── 2. Load campaign_id mapping from campaigns table ─────────────
            camp_rows = conn.execute(
                "SELECT campaign_id, campaign_name FROM campaigns WHERE campaign_id != ''"
            ).fetchall()
            name_to_id = {r["campaign_name"]: r["campaign_id"] for r in camp_rows}

            # ── 3. Aggregate OD production log (90d) by keyword+campaign ─────
            # PR 4: filter to 'high' tier rows only (>= 0.55 confidence) to keep
            # keyword_intelligence — and therefore the AI optimizer — protected from
            # low-confidence ('low'/'booked_override') attributions. NULL means a
            # pre-PR 4 row written under the old 0.55 floor; treat as 'high'.
            prod_rows = conn.execute("""
                SELECT keyword_text, campaign_id, campaign_name,
                       COUNT(*) AS appointments,
                       SUM(production_amount) AS total_production
                FROM keyword_production_log
                WHERE logged_at >= ?
                  AND (confidence_tier = 'high' OR confidence_tier IS NULL)
                GROUP BY LOWER(keyword_text), campaign_id
            """, (cutoff_90d + "T00:00:00+00:00",)).fetchall()

            prod_map = {}
            for p in prod_rows:
                key = (p["keyword_text"].lower(), p["campaign_id"])
                prod_map[key] = {
                    "appointments": p["appointments"],
                    "total_production": p["total_production"] or 0.0,
                }

            # ── 5. Aggregate decision history (90d) by (entity_name, operation) ──
            # Keyed by (keyword_lower, operation) so a rejected "decrease_bid"
            # does NOT suppress "pause_keyword" on the same term (M6 fix).
            decision_rows = conn.execute("""
                SELECT entity_name, operation, execution_result,
                       COUNT(*) AS cnt,
                       MAX(CASE WHEN execution_result='rejected' THEN created_at ELSE '' END) AS last_reject_at,
                       MAX(CASE WHEN execution_result='success'  THEN created_at ELSE '' END) AS last_apply_at
                FROM gads_audit_log
                WHERE entity_type = 'keyword'
                  AND created_at >= ?
                GROUP BY LOWER(entity_name), operation, execution_result
            """, (cutoff_90d + "T00:00:00+00:00",)).fetchall()

            # Build summary per keyword (aggregated across operations for intelligence table)
            dec_map = {}  # keyword_lower → {applied, rejected, last_decision, last_decision_at}
            for d in decision_rows:
                name_key = d["entity_name"].lower()
                if name_key not in dec_map:
                    dec_map[name_key] = {
                        "applied": 0, "rejected": 0,
                        "last_decision": "", "last_decision_at": ""
                    }
                if d["execution_result"] == "success":
                    dec_map[name_key]["applied"] += d["cnt"]
                    if d["last_apply_at"] > dec_map[name_key]["last_decision_at"]:
                        dec_map[name_key]["last_decision_at"] = d["last_apply_at"]
                        dec_map[name_key]["last_decision"] = "applied"
                elif d["execution_result"] == "rejected":
                    dec_map[name_key]["rejected"] += d["cnt"]
                    if d["last_reject_at"] > dec_map[name_key]["last_decision_at"]:
                        dec_map[name_key]["last_decision_at"] = d["last_reject_at"]
                        dec_map[name_key]["last_decision"] = "rejected"

            # ── 6. Build intelligence rows ────────────────────────────────────
            intel_rows = []
            for kw in kw_rows:
                kw_text = (kw["keyword_text"] or "").lower().strip()
                if not kw_text:
                    continue
                camp_name = kw["campaign_name"] or ""
                camp_id = name_to_id.get(camp_name, "")
                match_type = kw["match_type"] or ""

                cost_usd = float(kw["cost"] or 0.0)
                clicks = int(kw["clicks"] or 0)

                # OD production — try campaign-scoped first, then global
                prod = prod_map.get((kw_text, camp_id)) or prod_map.get((kw_text, "")) or {}
                od_apts = prod.get("appointments", 0)
                od_prod_total = prod.get("total_production", 0.0)
                od_prod_per_click = (od_prod_total / clicks) if clicks > 0 else 0.0

                # GA4 session quality (from parsed blob)
                ga4 = ga4_map.get(kw_text) or {}
                ga4_sessions = ga4.get("sessions", 0)
                ga4_avg_dur = ga4.get("avg_dur", 0.0)
                ga4_bounce = ga4.get("bounce", 0.0)
                ga4_leads = ga4.get("lead_events", 0)
                sqs = _compute_sqs(ga4_avg_dur, ga4_bounce, ga4_leads, ga4_sessions)

                # Decision history
                dec = dec_map.get(kw_text) or {}
                times_applied = dec.get("applied", 0)
                times_rejected = dec.get("rejected", 0)

                # True ROAS (OD production per dollar spent)
                true_roas = (od_prod_total / cost_usd) if cost_usd > 0 else 0.0

                # M7 fix: confidence tier from keyword's GAds sync date, NOT OD first_seen
                synced_at = kw["synced_at"] or today
                age_days, tier = _confidence_tier(synced_at, today)

                intel_rows.append({
                    "date": today,
                    "keyword_text": kw_text,
                    "match_type": match_type,
                    "campaign_id": camp_id,
                    "campaign_name": camp_name,
                    "ad_group_name": kw["ad_group_name"] or "",
                    "impressions": int(kw["impressions"] or 0),
                    "clicks": clicks,
                    "cost_usd": cost_usd,
                    "avg_cpc": float(kw["avg_cpc"] or 0.0),
                    "conversions": float(kw["conversions"] or 0.0),
                    "quality_score": int(kw["quality_score"] or 0),
                    "impression_share": float(kw["impression_share"] or 0.0),
                    "od_appointments": od_apts,
                    "od_production_total": od_prod_total,
                    "od_production_per_click": od_prod_per_click,
                    "ga4_sessions": ga4_sessions,
                    "ga4_avg_duration_sec": ga4_avg_dur,
                    "ga4_bounce_rate": ga4_bounce,
                    "ga4_lead_events": ga4_leads,
                    "session_quality_score": sqs,
                    "times_recommended": times_applied + times_rejected,
                    "times_applied": times_applied,
                    "times_rejected": times_rejected,
                    "last_decision_at": dec.get("last_decision_at", ""),
                    "last_decision": dec.get("last_decision", ""),
                    "true_roas": round(true_roas, 4),
                    "data_age_days": age_days,
                    "confidence_tier": tier,
                })

    except Exception as e:
        logger.error(f"[ki_builder] Error building intelligence rows: {e}")
        errors += 1
        return {"rebuilt": 0, "errors": errors}

    # Write to DB
    try:
        rows_built = upsert_keyword_intelligence(intel_rows)
        logger.info(f"[ki_builder] Rebuilt {rows_built} keyword intelligence rows for {today}")
    except Exception as e:
        logger.error(f"[ki_builder] Error writing keyword intelligence: {e}")
        errors += 1

    return {
        "rebuilt": rows_built,
        "errors": errors,
        "date": today,
        "keywords_processed": len(intel_rows),
        "od_matches": sum(1 for r in intel_rows if r["od_appointments"] > 0),
        "ga4_matched": sum(1 for r in intel_rows if r["ga4_sessions"] > 0),
    }
