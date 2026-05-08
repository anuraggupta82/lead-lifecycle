"""
AI Campaign Optimizer — daily evaluation and optimization of Google Ads.

Runs daily (7 AM) after fresh data from google_ads_sync.
Pulls keyword performance, joins with lead/production data, then:
  1. Pauses keywords with high spend + zero leads
  2. Increases bids on proven production keywords
  3. Harvests new exact-match keywords from search terms report
  4. Adds negative keywords for irrelevant search terms
  5. Generates a daily optimization report

Phase 1 changes:
  - Every optimizer run creates a gads_optimizer_runs record.
  - Every recommendation creates a gads_audit_log row with execution_result='pending_approval'.
  - Each recommendation row includes an 'action_id' field for the Apply button.
  - Stale pending rows (>48h) are expired at the start of each run.
  - _execute_pause uses partial_failure=True, logs per-keyword, checks kill switch.
  - dry_run parameter is deprecated — optimizer always produces pending rows.
    Use the Apply button in the admin UI to execute individual actions.

Uses Claude API for analysis when ANTHROPIC_API_KEY is set.
Falls back to rule-based optimization otherwise.

Manual trigger: POST /api/admin/optimize
"""

import logging
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from google.ads.googleads.client import GoogleAdsClient
from config import get_settings
from database import get_all_leads

logger = logging.getLogger(__name__)


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


# ── Data Collection ──────────────────────────────────────────────────────────

def _get_keyword_performance(client, customer_id: str, days: int = 30) -> list:
    """Pull keyword-level performance metrics for the last N days."""
    service = client.get_service("GoogleAdsService")
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=days)

    query = f"""
        SELECT
            ad_group_criterion.keyword.text,
            ad_group_criterion.keyword.match_type,
            ad_group_criterion.status,
            ad_group_criterion.resource_name,
            ad_group_criterion.effective_cpc_bid_micros,
            ad_group_criterion.cpc_bid_micros,
            ad_group.name,
            ad_group.resource_name,
            campaign.name,
            campaign.resource_name,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.conversions_value,
            metrics.average_cpc
        FROM keyword_view
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
            AND campaign.status = 'ENABLED'
            AND ad_group.status = 'ENABLED'
    """

    results = []
    try:
        response = service.search(customer_id=customer_id, query=query)
        for row in response:
            cost = (row.metrics.cost_micros or 0) / 1_000_000.0
            clicks = row.metrics.clicks or 0
            # Use cpc_bid_micros (manual CPC) if set; fall back to effective_cpc
            current_bid = (row.ad_group_criterion.cpc_bid_micros or
                           row.ad_group_criterion.effective_cpc_bid_micros or 0)
            results.append({
                "keyword": row.ad_group_criterion.keyword.text,
                "match_type": str(row.ad_group_criterion.keyword.match_type),
                "status": str(row.ad_group_criterion.status),
                "resource_name": row.ad_group_criterion.resource_name,
                "current_bid_micros": current_bid,
                "ad_group": row.ad_group.name,
                "ad_group_resource": row.ad_group.resource_name,
                "campaign": row.campaign.name,
                "campaign_resource": row.campaign.resource_name,
                "impressions": row.metrics.impressions or 0,
                "clicks": clicks,
                "cost": cost,
                "cpc": cost / clicks if clicks > 0 else 0,
                "conversions": row.metrics.conversions or 0,
                "conversion_value": row.metrics.conversions_value or 0,
            })
    except Exception as e:
        logger.error(f"Failed to get keyword performance: {e}")

    return results


def _get_search_terms(client, customer_id: str, days: int = 30) -> list:
    """Pull search terms report to find new keywords and negatives."""
    service = client.get_service("GoogleAdsService")
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=days)

    query = f"""
        SELECT
            search_term_view.search_term,
            search_term_view.status,
            ad_group.resource_name,
            ad_group.name,
            campaign.resource_name,
            campaign.name,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions
        FROM search_term_view
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
            AND metrics.impressions > 0
            AND campaign.status = 'ENABLED'
            AND ad_group.status = 'ENABLED'
    """

    results = []
    try:
        response = service.search(customer_id=customer_id, query=query)
        for row in response:
            cost = (row.metrics.cost_micros or 0) / 1_000_000.0
            results.append({
                "search_term": row.search_term_view.search_term,
                "status": str(row.search_term_view.status),
                "ad_group_resource": row.ad_group.resource_name,
                "ad_group": row.ad_group.name,
                "campaign_resource": row.campaign.resource_name,
                "campaign": row.campaign.name,
                "impressions": row.metrics.impressions or 0,
                "clicks": row.metrics.clicks or 0,
                "cost": cost,
                "conversions": row.metrics.conversions or 0,
            })
    except Exception as e:
        logger.error(f"Failed to get search terms: {e}")

    return results


def _get_google_recommendations(client, customer_id: str) -> list:
    """
    Pull Google's own recommendations via RecommendationService.
    Returns list of dicts with type, resource_name, title, description, impact, details.

    IMPORTANT: Only select top-level GAQL-selectable fields in the query.
    Nested sub-fields (impact.base_metrics.*, keyword_recommendation.keyword.text, etc.)
    are NOT selectable via GAQL — they are returned automatically on the proto object
    once the parent message is in the SELECT list. We read them via getattr after fetch.
    """
    service = client.get_service("GoogleAdsService")
    query = """
        SELECT
            recommendation.resource_name,
            recommendation.type,
            recommendation.campaign,
            recommendation.ad_group,
            recommendation.dismissed,
            campaign.name,
            campaign.resource_name
        FROM recommendation
        WHERE recommendation.dismissed = FALSE
    """
    results = []
    try:
        response = service.search(customer_id=customer_id, query=query)
        for row in response:
            rec = row.recommendation
            # rec_type: use .name for enum, fall back to int string
            try:
                rec_type = rec.type_.name
            except AttributeError:
                rec_type = str(int(rec.type_))

            # Impact — proto fields are populated on the object even though we
            # didn't SELECT the sub-fields in GAQL (they come as part of the message)
            try:
                base = rec.impact.base_metrics
                potential = rec.impact.potential_metrics
                impact = {
                    "base_impressions": float(getattr(base, 'impressions', 0) or 0),
                    "base_clicks": float(getattr(base, 'clicks', 0) or 0),
                    "base_cost": (int(getattr(base, 'cost_micros', 0) or 0)) / 1_000_000,
                    "base_conversions": float(getattr(base, 'conversions', 0) or 0),
                    "potential_impressions": float(getattr(potential, 'impressions', 0) or 0),
                    "potential_clicks": float(getattr(potential, 'clicks', 0) or 0),
                    "potential_cost": (int(getattr(potential, 'cost_micros', 0) or 0)) / 1_000_000,
                    "potential_conversions": float(getattr(potential, 'conversions', 0) or 0),
                }
            except Exception:
                impact = {
                    "base_impressions": 0, "base_clicks": 0, "base_cost": 0, "base_conversions": 0,
                    "potential_impressions": 0, "potential_clicks": 0, "potential_cost": 0, "potential_conversions": 0,
                }

            # Extract type-specific details — use try/except per type since
            # proto oneof fields throw AttributeError if the wrong variant is accessed
            details = {}
            title = rec_type.replace("_", " ").title()
            description = ""

            try:
                if rec_type == "KEYWORD":
                    kw = rec.keyword_recommendation
                    kw_text = kw.keyword.text or ""
                    try:
                        kw_match = kw.keyword.match_type.name
                    except AttributeError:
                        kw_match = str(kw.keyword.match_type)
                    bid = (kw.recommended_cpc_bid_micros or 0) / 1_000_000
                    details = {"keyword_text": kw_text, "match_type": kw_match, "recommended_cpc_bid": bid}
                    title = f"Add Keyword: {kw_text}"
                    description = f"Add '{kw_text}' [{kw_match}] at ${bid:.2f} CPC"

                elif rec_type == "KEYWORD_MATCH_TYPE":
                    km = rec.keyword_match_type_recommendation
                    kw_text = km.keyword.text or ""
                    try:
                        from_type = km.keyword.match_type.name
                        to_type = km.recommended_match_type.name
                    except AttributeError:
                        from_type = str(km.keyword.match_type)
                        to_type = str(km.recommended_match_type)
                    details = {"keyword_text": kw_text, "from_match_type": from_type, "to_match_type": to_type}
                    title = f"Change Match Type: {kw_text}"
                    description = f"Change '{kw_text}' from {from_type} → {to_type}"

                elif rec_type == "MAXIMIZE_CONVERSIONS_OPT_IN":
                    budget = (rec.maximize_conversions_opt_in_recommendation.recommended_budget_amount_micros or 0) / 1_000_000
                    details = {"recommended_budget": budget}
                    title = "Switch to Maximize Conversions"
                    description = f"Switch bid strategy to Maximize Conversions (recommended budget: ${budget:.0f}/day)"

                elif rec_type == "TARGET_CPA_OPT_IN":
                    r = rec.target_cpa_opt_in_recommendation
                    cpa = (r.recommended_target_cpa_micros or 0) / 1_000_000
                    req_budget = (r.required_campaign_budget_amount_micros or 0) / 1_000_000
                    details = {"recommended_target_cpa": cpa, "required_budget": req_budget}
                    title = "Switch to Target CPA Bidding"
                    description = f"Switch to Target CPA at ${cpa:.2f} (requires ${req_budget:.0f}/day budget)"

                elif rec_type == "TARGET_ROAS_OPT_IN":
                    roas = rec.target_roas_opt_in_recommendation.recommended_target_roas or 0
                    details = {"recommended_target_roas": roas}
                    title = "Switch to Target ROAS Bidding"
                    description = f"Switch to Target ROAS at {roas:.1%}"

                elif rec_type in ("MARGINAL_ROI_CAMPAIGN_BUDGET", "CAMPAIGN_BUDGET"):
                    budget_rec = (rec.marginal_roi_campaign_budget_recommendation
                                  if rec_type == "MARGINAL_ROI_CAMPAIGN_BUDGET"
                                  else rec.campaign_budget_recommendation)
                    rec_budget = (budget_rec.recommended_budget_amount_micros or 0) / 1_000_000
                    cur_budget = (budget_rec.current_budget_amount_micros or 0) / 1_000_000
                    details = {"current_budget": cur_budget, "recommended_budget": rec_budget,
                               "campaign_resource": rec.campaign or ""}
                    title = "Increase Campaign Budget"
                    description = f"Increase daily budget from ${cur_budget:.0f} to ${rec_budget:.0f}"

                elif rec_type == "MOVE_UNUSED_BUDGET":
                    mu = rec.move_unused_budget_recommendation
                    # budget_recommendation is a CampaignBudgetRecommendation sub-message
                    rec_amount = (mu.budget_recommendation.recommended_budget_amount_micros or 0) / 1_000_000
                    details = {"recommended_budget_amount": rec_amount,
                               "excess_campaign_budget": mu.excess_campaign_budget or ""}
                    title = "Move Unused Budget"
                    description = f"Reallocate unused budget (${rec_amount:.0f}) to this campaign"

                elif rec_type in ("RESPONSIVE_SEARCH_AD", "RESPONSIVE_SEARCH_AD_IMPROVE_AD_STRENGTH"):
                    details = {"has_rsa_suggestion": True}
                    title = "Improve Responsive Search Ad"
                    description = "Google recommends updating ad copy for better Ad Strength"

                elif rec_type in ("SITELINK_ASSET", "SITELINK_EXTENSION"):
                    title = "Add Sitelink Assets"
                    description = "Add sitelink assets to improve ad visibility (+10-20% CTR)"

                elif rec_type in ("CALLOUT_ASSET", "CALLOUT_EXTENSION"):
                    title = "Add Callout Assets"
                    description = "Add callout assets to highlight practice features"

                elif rec_type in ("CALL_ASSET", "CALL_EXTENSION"):
                    title = "Add Call Asset"
                    description = "Add call asset to enable direct calling from ads"

                elif rec_type == "MAXIMIZE_CLICKS_OPT_IN":
                    title = "Switch to Maximize Clicks"
                    description = "Switch bid strategy to Maximize Clicks"

                elif rec_type == "ENHANCED_CPC_OPT_IN":
                    title = "Enable Enhanced CPC"
                    description = "Enable Enhanced CPC to optimize manual bids with AI"

                elif rec_type == "USE_BROAD_MATCH_KEYWORD":
                    r = rec.use_broad_match_keyword_recommendation
                    kw_count = r.suggested_keywords_count or 0
                    details = {"suggested_keywords_count": kw_count,
                               "required_budget": (r.required_campaign_budget_amount_micros or 0) / 1_000_000}
                    title = "Use Broad Match Keywords"
                    description = f"Switch {kw_count} keywords to broad match for wider reach"

                elif rec_type == "RAISE_TARGET_CPA":
                    r = rec.raise_target_cpa_recommendation
                    details = {"target_adjustment": str(getattr(r, 'target_adjustment', ''))}
                    title = "Raise Target CPA"
                    description = "Google recommends raising Target CPA to get more conversions"

            except Exception as detail_err:
                logger.debug(f"Could not extract details for {rec_type}: {detail_err}")

            results.append({
                "resource_name": rec.resource_name,
                "rec_type": rec_type,
                "campaign_resource": row.campaign.resource_name if row.campaign.resource_name else "",
                "campaign_name": row.campaign.name if row.campaign.name else "",
                "ad_group_resource": rec.ad_group if rec.ad_group else "",
                "title": title,
                "description": description,
                "impact": impact,
                "details": details,
            })
    except Exception as e:
        logger.error(f"Failed to get Google recommendations: {e}")
    logger.info(f"Fetched {len(results)} Google recommendations")
    return results


def _apply_google_recommendation(client, customer_id: str, resource_name: str) -> bool:
    """
    Apply a Google recommendation directly via ApplyRecommendation.
    This is the simplest path for most rec types — Google applies it server-side.
    Returns True on success.
    """
    service = client.get_service("RecommendationService")
    operation = client.get_type("ApplyRecommendationOperation")
    operation.resource_name = resource_name
    try:
        response = service.apply_recommendation(
            customer_id=customer_id,
            operations=[operation],
            partial_failure=True,
        )
        logger.info(f"Applied Google rec {resource_name}: {response}")
        return True
    except Exception as e:
        logger.error(f"Failed to apply Google rec {resource_name}: {e}")
        raise


# ── Lead-Level Attribution ───────────────────────────────────────────────────

def _get_keyword_attribution() -> dict:
    """
    Build keyword → lead/revenue attribution from SQLite.
    Returns: {keyword_text: {leads, booked, treated, production}}
    """
    leads = get_all_leads(limit=1000)
    attribution = {}

    for lead in leads:
        keyword = (lead.get("keyword_text") or "").strip()
        if not keyword:
            continue

        if keyword not in attribution:
            attribution[keyword] = {
                "leads": 0,
                "booked": 0,
                "treated": 0,
                "production": 0.0,
            }

        attribution[keyword]["leads"] += 1

        stage = lead.get("stage", "")
        if stage in ("scheduled", "no_show", "showed", "treatment_presented",
                      "treatment_accepted", "treatment_completed"):
            attribution[keyword]["booked"] += 1

        if stage in ("treatment_accepted", "treatment_completed"):
            attribution[keyword]["treated"] += 1

        attribution[keyword]["production"] += float(lead.get("attributed_production", 0))

    return attribution


def _get_call_attribution(days: int = 30) -> dict:
    """
    Return per-campaign inbound call attribution directly from mango_calls.
    Bypasses v_campaign_call_stats (which requires gads_campaign_numeric_id on campaigns
    rows — often unpopulated — causing false-zero call counts).

    Resolution order for campaign name:
      1. gads_call_view.campaign_name  (most authoritative — came from GAds API)
      2. leads.campaign_name           (form-fill attribution)
      3. campaigns.campaign_name via gads_campaign_numeric_id (ID-based lookup)

    Returns: {campaign_name_lower: {campaign_name, calls, booked_calls, confirmed_appts,
                                    avg_duration_sec, gcv_campaign_id}}
    """
    from database import _conn
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    out = {}
    try:
        with _conn() as conn:
            rows = conn.execute("""
                WITH resolved AS (
                  SELECT
                    mc.uuid,
                    mc.duration_sec,
                    mc.booked_outcome,
                    mc.od_appointment_id,
                    gcv.campaign_id   AS gcv_campaign_id,
                    COALESCE(
                      NULLIF(TRIM(gcv.campaign_name), ''),
                      NULLIF(TRIM(l.campaign_name),   ''),
                      (SELECT campaign_name FROM campaigns
                         WHERE gads_campaign_numeric_id = gcv.campaign_id LIMIT 1)
                    ) AS campaign_name
                  FROM mango_calls mc
                  LEFT JOIN gads_call_view gcv ON gcv.call_id = mc.gads_call_id
                  LEFT JOIN leads l            ON l.id = mc.lead_id
                  WHERE mc.started_at >= ?
                    AND mc.direction = 'inbound'
                )
                SELECT
                  LOWER(TRIM(campaign_name))  AS key,
                  campaign_name,
                  gcv_campaign_id,
                  COUNT(DISTINCT uuid)         AS calls,
                  SUM(CASE WHEN booked_outcome = 'booked' THEN 1 ELSE 0 END) AS booked_calls,
                  SUM(CASE WHEN od_appointment_id IS NOT NULL
                            AND od_appointment_id != '' THEN 1 ELSE 0 END)   AS confirmed_appts,
                  AVG(duration_sec)            AS avg_duration_sec
                FROM resolved
                WHERE campaign_name IS NOT NULL AND TRIM(campaign_name) != ''
                GROUP BY LOWER(TRIM(campaign_name)), campaign_name, gcv_campaign_id
            """, (cutoff,)).fetchall()

            for r in rows:
                key = r["key"]
                if not key:
                    continue
                if key not in out:
                    out[key] = {
                        "campaign_name": r["campaign_name"],
                        "gcv_campaign_id": r["gcv_campaign_id"] or "",
                        "calls": 0, "booked_calls": 0,
                        "confirmed_appts": 0, "avg_duration_sec": 0.0,
                    }
                # Merge rows (multiple gcv_campaign_id values can share a campaign name)
                out[key]["calls"]          += int(r["calls"] or 0)
                out[key]["booked_calls"]   += int(r["booked_calls"] or 0)
                out[key]["confirmed_appts"] += int(r["confirmed_appts"] or 0)
                # Weighted average for duration
                prev_avg = out[key]["avg_duration_sec"]
                prev_n   = out[key]["calls"] - int(r["calls"] or 0)
                new_n    = int(r["calls"] or 0)
                if out[key]["calls"] > 0:
                    out[key]["avg_duration_sec"] = (
                        (prev_avg * prev_n + float(r["avg_duration_sec"] or 0.0) * new_n)
                        / out[key]["calls"]
                    )

            # Log unresolved calls for diagnostics
            unresolved = conn.execute("""
                SELECT COUNT(*) FROM mango_calls mc
                LEFT JOIN gads_call_view gcv ON gcv.call_id = mc.gads_call_id
                LEFT JOIN leads l ON l.id = mc.lead_id
                WHERE mc.started_at >= ? AND mc.direction = 'inbound'
                  AND COALESCE(NULLIF(TRIM(gcv.campaign_name),''),
                               NULLIF(TRIM(l.campaign_name),'')) IS NULL
            """, (cutoff,)).fetchone()[0]
            if unresolved > 0:
                logger.warning(f"Call attribution: {unresolved} inbound calls have no resolvable campaign name "
                               f"(no gads_call_view match AND no lead campaign). Run Sync Now to fix.")

    except Exception as e:
        logger.error(f"Failed to build call attribution: {e}", exc_info=True)
    return out


def _get_keyword_call_attribution(days: int = 30) -> dict:
    """
    Return per-keyword inbound call attribution using the attributed_keyword column
    set by call_keyword_attribution.py (Methods A/B/C only — excludes campaign_only).

    Returns: {keyword_lower: {keyword, match_type, ad_group, calls, booked_calls,
                               confirmed_appts, avg_duration_sec, campaigns: [str]}}
    """
    from database import _conn
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    out = {}
    try:
        with _conn() as conn:
            # Pre-aggregate per call in a CTE to prevent JOIN row multiplication.
            # gads_call_view or leads may match multiple rows per call; the CTE
            # collapses mango_calls first, then joins for campaign label only.
            rows = conn.execute("""
                WITH call_agg AS (
                  SELECT
                    mc.uuid,
                    LOWER(TRIM(mc.attributed_keyword))  AS kw_lower,
                    mc.attributed_keyword               AS keyword,
                    mc.attributed_match_type            AS match_type,
                    mc.attributed_ad_group              AS ad_group,
                    mc.gads_call_id,
                    mc.lead_id,
                    CASE WHEN mc.booked_outcome = 'booked' THEN 1 ELSE 0 END AS is_booked,
                    CASE WHEN mc.od_appointment_id IS NOT NULL
                          AND mc.od_appointment_id != '' THEN 1 ELSE 0 END   AS is_confirmed,
                    COALESCE(mc.duration_sec, 0)        AS duration_sec
                  FROM mango_calls mc
                  WHERE mc.started_at >= ?
                    AND mc.direction = 'inbound'
                    AND mc.attributed_keyword IS NOT NULL
                    AND mc.attributed_keyword != ''
                    AND mc.attributed_keyword_method NOT IN ('campaign_only', 'no_signal')
                )
                SELECT
                  ca.kw_lower,
                  ca.keyword,
                  MAX(ca.match_type)   AS match_type,
                  MAX(ca.ad_group)     AS ad_group,
                  COALESCE(NULLIF(gcv.campaign_name,''),
                           NULLIF(l.campaign_name,''), '')  AS campaign_name,
                  COUNT(DISTINCT ca.uuid)                   AS calls,
                  SUM(ca.is_booked)                         AS booked_calls,
                  SUM(ca.is_confirmed)                      AS confirmed_appts,
                  SUM(ca.duration_sec)                      AS total_duration_sec
                FROM call_agg ca
                LEFT JOIN gads_call_view gcv ON gcv.call_id = ca.gads_call_id
                LEFT JOIN leads l            ON l.id        = ca.lead_id
                GROUP BY ca.kw_lower, ca.keyword,
                         COALESCE(NULLIF(gcv.campaign_name,''), NULLIF(l.campaign_name,''), '')
            """, (cutoff,)).fetchall()

            # Merge rows with the same keyword across campaigns
            for r in rows:
                kw_lower = r["kw_lower"] or ""
                if not kw_lower:
                    continue
                if kw_lower not in out:
                    out[kw_lower] = {
                        "keyword": r["keyword"],
                        "match_type": r["match_type"] or "",
                        "ad_group": r["ad_group"] or "",
                        "calls": 0,
                        "booked_calls": 0,
                        "confirmed_appts": 0,
                        "_total_duration_sec": 0.0,   # internal accumulator, removed before return
                        "campaigns": [],
                    }
                entry = out[kw_lower]
                entry["calls"] += int(r["calls"] or 0)
                entry["booked_calls"] += int(r["booked_calls"] or 0)
                entry["confirmed_appts"] += int(r["confirmed_appts"] or 0)
                entry["_total_duration_sec"] += float(r["total_duration_sec"] or 0.0)
                if r["campaign_name"] and r["campaign_name"] not in entry["campaigns"]:
                    entry["campaigns"].append(r["campaign_name"])

        # Compute avg_duration_sec and remove internal accumulator
        for entry in out.values():
            entry["avg_duration_sec"] = (
                round(entry["_total_duration_sec"] / entry["calls"], 1)
                if entry["calls"] > 0 else 0.0
            )
            del entry["_total_duration_sec"]

    except Exception as e:
        logger.error(f"Failed to build keyword call attribution: {e}")
    return out


def _get_od_production_summary(days: int = 30) -> dict:
    """
    Roll up attributed_production from leads over the last N days.
    Returns: {"total_attributed": float, "by_campaign": {campaign_name: float}}
    """
    from database import _conn
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    total = 0.0
    by_camp: dict = {}
    try:
        with _conn() as conn:
            rows = conn.execute("""
                SELECT COALESCE(NULLIF(campaign_name,''),'(unknown)') AS campaign_name,
                       SUM(COALESCE(attributed_production,0))         AS prod
                  FROM leads
                 WHERE created_at >= ?
                 GROUP BY campaign_name
            """, (cutoff,)).fetchall()
            for r in rows:
                amt = float(r["prod"] or 0.0)
                if amt <= 0:
                    continue
                total += amt
                by_camp[r["campaign_name"]] = round(amt, 2)
    except Exception as e:
        logger.error(f"OD production summary failed: {e}")
    return {"total_attributed": round(total, 2), "by_campaign": by_camp}


def _call_claude_advisories(keyword_perf: list, attribution: dict, search_terms: list,
                             call_attribution: dict, od_production: dict,
                             summary: dict, campaign: str,
                             keyword_call_attribution: dict | None = None,
                             feedback: str = "",
                             rsa_resources: list | None = None,
                             geo_resolutions: dict | None = None,
                             google_recs: list | None = None) -> list:
    """
    Ask Claude (Opus) for structured, actionable recommendations for this campaign.
    Each recommendation is a dict with operation + exact parameters ready to execute via API.

    Supported operations Claude may return:
      add_negative_keyword  — {keyword_text, match_type, reason}
      pause_keyword         — {keyword_text, resource_name, reason}
      increase_bid          — {keyword_text, resource_name, new_bid_micros, reason}
      decrease_bid          — {keyword_text, resource_name, new_bid_micros, reason}
      add_exact_keyword     — {keyword_text, ad_group_resource, reason}
      ad_copy_suggestion    — {headline, description, reason}  (informational — no API call)
      geo_exclusion         — {location_name, reason}          (informational — logged for review)

    Returns list of dicts. Never raises — failure returns [].
    """
    import os, re as _re
    try:
        from database import get_setting as _get_setting
    except Exception:
        _get_setting = lambda k: None
    _api_key = _get_setting("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    if not _api_key:
        return []
    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=_api_key)

        call_summary = {
            v["campaign_name"]: {
                "calls": v["calls"],
                "booked": v["booked_calls"],
                "confirmed_appts": v["confirmed_appts"],
                "avg_duration_sec": round(v.get("avg_duration_sec") or 0),
            }
            for v in call_attribution.values()
        }
        kw_call_summary = {
            kw: {
                "calls": v["calls"],
                "booked_calls": v["booked_calls"],
                "confirmed_appts": v["confirmed_appts"],
                "avg_duration_sec": round(v.get("avg_duration_sec") or 0),
            }
            for kw, v in sorted(
                (keyword_call_attribution or {}).items(),
                key=lambda x: -(x[1].get("confirmed_appts", 0) * 10 + x[1].get("calls", 0))
            )[:20]
        }

        # Enrich search terms with resource names from keyword_perf for API execution
        kw_resource_map = {
            kw.get("keyword", "").strip().lower(): {
                "resource_name": kw.get("resource_name", ""),
                "ad_group_resource": kw.get("ad_group_resource", ""),
                "campaign_resource": kw.get("campaign_resource", ""),
                "current_bid_micros": kw.get("cpc_bid_micros", 0),
                "match_type": kw.get("match_type", "BROAD"),
            }
            for kw in keyword_perf if kw.get("keyword")
        }
        # Campaign resource name from keyword_perf (first match for this campaign)
        campaign_resource = ""
        for kw in keyword_perf:
            if kw.get("campaign", "").strip().lower() == campaign.strip().lower():
                campaign_resource = kw.get("campaign_resource", "")
                if campaign_resource:
                    break

        context = {
            "campaign_name": campaign,
            "campaign_resource": campaign_resource,
            "summary": summary,
            "keyword_performance": [
                {**k, **kw_resource_map.get(k.get("keyword","").lower(), {})}
                for k in keyword_perf[:50]
            ],
            "search_terms_top": sorted(search_terms, key=lambda s: -s.get("cost", 0))[:30],
            "form_attribution": attribution,
            "call_summary": call_summary,
            "keyword_call_summary": kw_call_summary,
            "od_production_summary": od_production,
            "keyword_resource_map": kw_resource_map,
            "rsa_resources": (rsa_resources or [])[:10],
            "geo_resolutions": geo_resolutions or {},
            "google_recommendations": (google_recs or [])[:20],
        }

        feedback_block = f"\n\nUSER FEEDBACK (incorporate this):\n{feedback}" if feedback else ""

        rsa_note = ""
        if rsa_resources:
            rsa_note = (
                "\n\nRSA RESOURCES (use ad_group_ad_resource for ad_copy_suggestion):\n"
                + json.dumps(rsa_resources[:5], default=str)
            )
        geo_note = ""
        if geo_resolutions:
            geo_note = (
                "\n\nPRE-RESOLVED GEO TARGETS (use geo_target_resource for geo_exclusion):\n"
                + json.dumps(geo_resolutions, default=str)
            )

        prompt = """You are a Google Ads specialist optimizing a dental practice's campaigns.
Analyze the data and return up to 7 SPECIFIC, EXECUTABLE recommendations.

Each recommendation MUST be a JSON object with these fields:
- "operation": one of: add_negative_keyword | pause_keyword | increase_bid | decrease_bid | add_exact_keyword | ad_copy_suggestion | geo_exclusion | enable_keyword | change_budget | change_bid_strategy | change_match_type | add_asset
- "reason": 1-2 sentence explanation with specific numbers from the data
- Operation-specific fields:

For add_negative_keyword:
  "keyword_text": exact term to block, "match_type": "EXACT"|"PHRASE"|"BROAD",
  "campaign_resource": the campaign resource name from the data

For pause_keyword:
  "keyword_text": keyword, "resource_name": the keyword resource_name from data

For enable_keyword:
  "keyword_text": keyword, "resource_name": the keyword resource_name from data (must be a PAUSED keyword)

For increase_bid / decrease_bid:
  "keyword_text": keyword, "resource_name": resource_name,
  "new_bid_micros": integer (current bid ± 10-20%)

For add_exact_keyword:
  "keyword_text": search term, "ad_group_resource": ad_group_resource from data

For ad_copy_suggestion:
  "headline": new headline (STRICT MAX 30 chars — count carefully),
  "description": new description (STRICT MAX 90 chars — count carefully),
  "ad_resource": the ad_group_ad_resource from rsa_resources data (required for API execution)

For geo_exclusion:
  "location_name": city/region name to exclude,
  "geo_target_resource": resource_name from geo_resolutions data (required for API execution)

For change_budget:
  "new_daily_budget_usd": float (e.g. 35.0), max 25% increase from current,
  "campaign_resource": the campaign resource name from the data

For change_bid_strategy:
  "bid_strategy": "MAXIMIZE_CONVERSIONS"|"TARGET_CPA"|"TARGET_ROAS"|"MAXIMIZE_CLICKS",
  "target_cpa_micros": integer (only for TARGET_CPA),
  "target_roas": float (only for TARGET_ROAS),
  "campaign_resource": campaign resource name

For change_match_type:
  "keyword_text": keyword text,
  "resource_name": keyword resource_name,
  "new_match_type": "EXACT"|"PHRASE"|"BROAD"

For add_asset:
  "asset_type": "SITELINK"|"CALLOUT"|"CALL",
  "campaign_resource": campaign resource name,
  "description": what to add (advisory — no API call)

GOOGLE'S OWN RECOMMENDATIONS (pulled live from Google Ads API):
Google has flagged the following recommendations for this account. Evaluate each one against the campaign data and lead/call attribution above. For each Google rec:
- If the data supports it → include it as your recommendation with operation matching the rec type, add "google_rec_resource_name": the resource_name field
- If the data contradicts it (e.g. Google says add keyword X but our data shows it converts poorly) → explicitly reject it in your reasoning but do NOT include it
- If neutral/unknown → include it as advisory

When endorsing a Google recommendation, use these operation mappings:
- KEYWORD rec → "add_exact_keyword" operation (or add_negative if it looks like a negative)
- KEYWORD_MATCH_TYPE → "change_match_type" operation
- MARGINAL_ROI_CAMPAIGN_BUDGET / CAMPAIGN_BUDGET → "change_budget" operation
- MAXIMIZE_CONVERSIONS_OPT_IN → "change_bid_strategy" operation
- TARGET_CPA_OPT_IN → "change_bid_strategy" operation
- SITELINK_EXTENSION / CALLOUT_EXTENSION / CALL_EXTENSION → "add_asset" operation (advisory)
- RESPONSIVE_SEARCH_AD → "ad_copy_suggestion" operation

Always include "google_rec_resource_name" field in any rec that came from Google.

Rules:
- Only use resource_names that appear in the data — never invent them
- If resource_name is unavailable for a keyword operation, skip that recommendation
- For ad_copy_suggestion: ALWAYS include ad_resource from rsa_resources — skip if none available
- For geo_exclusion: ONLY suggest if geo_target_resource is available in geo_resolutions
- Prioritize recommendations that stop wasted spend first
- COMPETITOR SEARCHES: Any search term containing a competitor practice name (e.g. "grace dental", "simply orthodontics", "aspen dental", "gentle dental", any "[name] dental [city]" that isn't Grafton Dental Care) MUST be flagged as add_negative_keyword. These waste budget showing our ads to people searching for a competitor.
- Return ONLY a valid JSON array, no markdown, no explanation outside the array""" + rsa_note + geo_note + feedback_block

        msg = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=2000,
            messages=[{
                "role": "user",
                "content": prompt + "\n\nCAMPAIGN DATA:\n" + json.dumps(context, default=str)[:70000],
            }],
        )
        text = msg.content[0].text if msg.content else "[]"
        m = _re.search(r"\[[\s\S]*\]", text)
        if m:
            arr = json.loads(m.group(0))
            # Build sets of valid resource names from actual keyword_perf data
            valid_kw_resources   = {kw.get("resource_name","")      for kw in keyword_perf if kw.get("resource_name")}
            valid_ag_resources   = {kw.get("ad_group_resource","")  for kw in keyword_perf if kw.get("ad_group_resource")}
            valid_camp_resources = {kw.get("campaign_resource","")  for kw in keyword_perf if kw.get("campaign_resource")}

            validated = []
            for item in arr:
                if not isinstance(item, dict) or not item.get("operation"):
                    continue
                op = item["operation"]
                # Validate resource names Claude returned are real — drop hallucinated ones
                if op in ("pause_keyword", "increase_bid", "decrease_bid"):
                    rn = item.get("resource_name","")
                    if rn and rn not in valid_kw_resources:
                        logger.warning(f"Dropping Claude rec — unknown resource_name '{rn}' for op={op}")
                        continue
                elif op == "add_exact_keyword":
                    ag = item.get("ad_group_resource","")
                    if ag and ag not in valid_ag_resources:
                        logger.warning(f"Dropping Claude rec — unknown ad_group_resource '{ag}' for op={op}")
                        continue
                elif op == "add_negative_keyword":
                    cr = item.get("campaign_resource","")
                    if cr and cr not in valid_camp_resources:
                        logger.warning(f"Dropping Claude rec — unknown campaign_resource '{cr}' for op={op}")
                        # Fall back: use the campaign_resource we derived from keyword_perf
                        if campaign_resource:
                            item["campaign_resource"] = campaign_resource
                            logger.info(f"  Fixed campaign_resource for '{item.get('keyword_text','?')}' → {campaign_resource}")
                        else:
                            continue
                elif op == "ad_copy_suggestion":
                    # Validate character limits — drop silently if over limit
                    headline = item.get("headline", "")
                    description = item.get("description", "")
                    if len(headline) > 30:
                        logger.warning(f"Dropping ad_copy_suggestion — headline too long: '{headline}' ({len(headline)} chars)")
                        continue
                    if len(description) > 90:
                        logger.warning(f"Dropping ad_copy_suggestion — description too long ({len(description)} chars)")
                        continue
                    # ad_resource is required for API execution; warn if missing but keep rec
                    if not item.get("ad_resource"):
                        logger.warning(f"ad_copy_suggestion missing ad_resource — will be acknowledged-only")
                elif op == "geo_exclusion":
                    # Drop if no pre-resolved geo_target_resource
                    if not item.get("geo_target_resource"):
                        logger.warning(f"Dropping geo_exclusion '{item.get('location_name','')}' — no geo_target_resource resolved")
                        continue
                elif op == "enable_keyword":
                    rn = item.get("resource_name","")
                    if rn and rn not in valid_kw_resources:
                        logger.warning(f"Dropping Claude rec — unknown resource_name '{rn}' for op={op}")
                        continue
                elif op == "change_budget":
                    cr = item.get("campaign_resource","")
                    if cr and cr not in valid_camp_resources:
                        if campaign_resource:
                            item["campaign_resource"] = campaign_resource
                        else:
                            logger.warning(f"Dropping change_budget — no valid campaign_resource")
                            continue
                    if not item.get("new_daily_budget_usd"):
                        logger.warning("Dropping change_budget — missing new_daily_budget_usd")
                        continue
                validated.append(item)
            logger.info(f"Claude returned {len(arr)} recs, {len(validated)} passed validation")
            return validated
    except Exception as e:
        logger.warning(f"Claude advisory call failed (non-fatal): {e}")


def _call_claude_account_level(
    all_keyword_perf: list,
    all_search_terms: list,
    call_attribution: dict,
    od_production: dict,
    summary: dict,
    campaign_spend: dict,
    google_recs: list | None = None,
) -> list:
    """
    Account-level Claude pass: runs once after all per-campaign passes.
    Focuses on cross-campaign patterns and whole-account recommendations.
    Returns recs with campaign_name="" (shown in Account Level section).

    Account-level rec types:
      - add_negative_keyword with campaign_resource set (competitor names seen across campaigns)
      - change_bid_strategy  (account-wide strategy change)
      - change_budget        (rebalance budget across campaigns)
      - add_asset            (sitelinks / callouts that should apply broadly)
      - claude_advisory      (account-level insight, no API action)
    """
    import os, re as _re
    try:
        from database import get_setting as _get_setting
    except Exception:
        _get_setting = lambda k: None
    _api_key = _get_setting("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    if not _api_key:
        return []

    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=_api_key)

        # Build cross-campaign summary
        camp_resources = {}  # campaign_name -> campaign_resource
        for kw in all_keyword_perf:
            cn = kw.get("campaign", "")
            cr = kw.get("campaign_resource", "")
            if cn and cr and cn not in camp_resources:
                camp_resources[cn] = cr

        # Find competitor terms appearing across multiple campaigns
        from collections import defaultdict
        term_campaigns = defaultdict(set)
        for st in all_search_terms:
            t = st.get("search_term", "").strip().lower()
            c = st.get("campaign", "").strip()
            if t and c:
                term_campaigns[t].add(c)
        cross_camp_terms = {t: list(camps) for t, camps in term_campaigns.items() if len(camps) > 1}

        # Build per-campaign budget/performance summary
        camp_perf = {}
        for cn, cr in camp_resources.items():
            kws = [k for k in all_keyword_perf if k.get("campaign","") == cn]
            spend = sum(k.get("cost", 0) for k in kws)
            clicks = sum(k.get("clicks", 0) for k in kws)
            calls = call_attribution.get(cn.lower(), {}).get("calls", 0)
            booked = call_attribution.get(cn.lower(), {}).get("booked_calls", 0)
            prod = 0.0
            if isinstance(od_production, dict):
                by_camp = od_production.get("by_campaign", {})
                prod = float(by_camp.get(cn, 0))
            camp_perf[cn] = {
                "campaign_resource": cr,
                "spend_30d": round(spend, 2),
                "clicks": clicks,
                "calls": calls,
                "booked_calls": booked,
                "production": prod,
                "daily_budget": campaign_spend.get(cn, {}).get("daily_budget_usd") if isinstance(campaign_spend.get(cn), dict) else None,
            }

        context = {
            "account_summary": summary,
            "campaign_performance": camp_perf,
            "campaign_resources": camp_resources,
            "cross_campaign_search_terms": dict(list(cross_camp_terms.items())[:30]),
            "top_search_terms_by_cost": sorted(all_search_terms, key=lambda s: -s.get("cost", 0))[:40],
            "call_attribution": {
                v["campaign_name"]: {"calls": v["calls"], "booked": v["booked_calls"], "confirmed_appts": v["confirmed_appts"]}
                for v in call_attribution.values()
            },
            "od_production_summary": od_production,
            "google_recommendations": (google_recs or [])[:20],
        }

        prompt = """You are a Google Ads specialist performing an ACCOUNT-LEVEL review for a dental practice (Grafton Dental Care, Grafton MA).

You have already reviewed individual campaigns. Now identify issues and opportunities that span the whole account or cannot be attributed to one campaign.

Return up to 6 ACCOUNT-LEVEL recommendations as a JSON array. Each must have:
- "operation": one of: add_negative_keyword | change_bid_strategy | change_budget | add_asset | claude_advisory
- "reason": 1-2 sentences with specific numbers. For cross-campaign negatives, cite which campaigns the term appeared in.
- "campaign_name": MUST be "" (empty string) — these are account-level recs

Operation-specific fields (same spec as campaign-level):
- add_negative_keyword: "keyword_text", "match_type" ("EXACT"|"PHRASE"|"BROAD"), "campaign_resource" (use the campaign_resource for the campaign where this term appeared most — or the highest-spend campaign if cross-campaign)
- change_bid_strategy: "bid_strategy", "target_cpa_micros" (optional), "target_roas" (optional), "campaign_resource"
- change_budget: "new_daily_budget_usd", "campaign_resource"
- add_asset: "asset_type" ("SITELINK"|"CALLOUT"|"CALL"), "campaign_resource", "description"
- claude_advisory: "insight" (account-level observation, no API action needed)

Focus areas for account-level recs:
1. COMPETITOR NAMES appearing across multiple campaigns → add_negative_keyword (highest-spend campaign's resource)
2. BUDGET REBALANCING — if one campaign has 0 conversions/calls but high spend vs another with conversions → change_budget
3. BID STRATEGY — if a campaign has enough conversion data to switch strategies → change_bid_strategy
4. MISSING ASSETS — sitelinks/callouts that should exist on all campaigns but don't → add_asset
5. CROSS-CAMPAIGN WASTE — identical wasteful terms appearing in multiple campaigns
6. ACCOUNT HEALTH — any account-wide pattern not captured by individual campaign reviews

IMPORTANT:
- Only flag competitor negatives here if they appear in multiple campaigns (single-campaign terms were already handled per-campaign)
- Use only campaign_resource values from the "campaign_resources" field in the data
- Return ONLY a valid JSON array, no markdown, no explanation outside the array"""

        msg = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1500,
            messages=[{
                "role": "user",
                "content": prompt + "\n\nACCOUNT DATA:\n" + json.dumps(context, default=str)[:60000],
            }],
        )
        text = msg.content[0].text if msg.content else "[]"
        m = _re.search(r"\[[\s\S]*\]", text)
        if m:
            arr = json.loads(m.group(0))
            valid_camp_resources = set(camp_resources.values())
            validated = []
            for item in arr:
                if not isinstance(item, dict) or not item.get("operation"):
                    continue
                # Force campaign_name to empty — these are account-level
                item["campaign_name"] = ""
                op = item["operation"]
                cr = item.get("campaign_resource", "")
                if op in ("add_negative_keyword", "change_bid_strategy", "change_budget", "add_asset"):
                    if cr and cr not in valid_camp_resources:
                        logger.warning(f"Account-level: dropping '{op}' — unknown campaign_resource '{cr}'")
                        continue
                    if not cr and op != "add_asset" and op != "claude_advisory":
                        # Try to pick the highest-spend campaign's resource
                        if camp_resources:
                            top_camp = max(camp_perf, key=lambda c: camp_perf[c]["spend_30d"])
                            item["campaign_resource"] = camp_resources[top_camp]
                            logger.info(f"Account-level: assigned campaign_resource for '{op}' → {camp_resources[top_camp][:30]}")
                if op == "change_budget" and not item.get("new_daily_budget_usd"):
                    logger.warning("Account-level: dropping change_budget — missing new_daily_budget_usd")
                    continue
                validated.append(item)
            logger.info(f"Account-level Claude returned {len(arr)} recs, {len(validated)} passed validation")
            return validated
    except Exception as e:
        logger.warning(f"Account-level Claude call failed (non-fatal): {e}")
    return []


def _refine_claude_action(action_row: dict, feedback: str) -> dict | None:
    """
    Re-run Claude on a single existing action with user feedback.
    Returns a revised action dict, or None on failure.
    """
    import os, re as _re
    try:
        from database import get_setting as _get_setting
    except Exception:
        _get_setting = lambda k: None
    _api_key = _get_setting("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    if not _api_key:
        return None
    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=_api_key)

        after_state = json.loads(action_row.get("after_state_json") or "{}")
        prompt = f"""You are refining a Google Ads recommendation based on user feedback.

Original recommendation:
Operation: {action_row.get('operation')}
Entity: {action_row.get('entity_name')}
Reason: {action_row.get('reason')}
Parameters: {json.dumps(after_state)}

User feedback: {feedback}

Return a SINGLE revised recommendation as a JSON object with the same operation type and all required fields updated per the feedback. Return ONLY the JSON object, no markdown."""

        msg = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text if msg.content else "{}"
        m = _re.search(r"\{[\s\S]*\}", text)
        if m:
            revised = json.loads(m.group(0))
            if isinstance(revised, dict) and revised.get("operation"):
                return revised
    except Exception as e:
        logger.warning(f"Claude refine call failed: {e}")
    return None


# ── Outcome History (AI Learning Loop) ───────────────────────────────────────

def _load_outcome_history(days_back: int = 90) -> dict:
    """
    Load the history of applied actions and their measured outcomes.
    Returns: {(entity_id, operation): {improved, degraded, neutral, last_applied_at}}

    Used by _analyze_keywords to skip or downgrade recommendations for entities
    where previous identical actions degraded performance.
    Guards against noise:
      - Only counts outcomes with pre_clicks_7d >= 5 (minimum sample)
      - 90-day window prevents stale data from blocking valid current recommendations
    """
    from database import _conn
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
    out = {}
    try:
        with _conn() as conn:
            rows = conn.execute("""
                SELECT entity_id, entity_name, operation,
                       SUM(CASE WHEN verdict='improved' THEN 1 ELSE 0 END) AS n_improved,
                       SUM(CASE WHEN verdict='degraded' THEN 1 ELSE 0 END) AS n_degraded,
                       SUM(CASE WHEN verdict='neutral'  THEN 1 ELSE 0 END) AS n_neutral,
                       MAX(applied_at) AS last_applied_at
                  FROM applied_outcomes
                 WHERE applied_at >= ?
                   AND pre_clicks_7d >= 5
                 GROUP BY entity_id, operation
            """, (cutoff,)).fetchall()
        for r in rows:
            out[(r["entity_id"], r["operation"])] = {
                "improved": int(r["n_improved"] or 0),
                "degraded": int(r["n_degraded"] or 0),
                "neutral":  int(r["n_neutral"] or 0),
                "last_applied_at": r["last_applied_at"] or "",
            }
    except Exception as e:
        logger.warning(f"Could not load outcome history (non-fatal): {e}")
    return out


# ── Rule-Based Optimization ──────────────────────────────────────────────────

# Stop words for harvest-exact token-overlap check (Rule 4).
# Single-word match on these alone is too generic for a dental practice
# and would cause false-positive exact-match harvesting.
_HARVEST_STOP_WORDS = frozenset({
    "a", "an", "the", "of", "and", "or", "in", "near", "my", "me", "i",
    "for", "to", "at", "on", "with", "is", "are",
    "dental", "dentist", "dentistry", "tooth", "teeth", "oral",
    "care", "office", "clinic", "practice",
})


def _analyze_keywords(keyword_perf: list, attribution: dict, search_terms: list,
                      call_attribution: dict | None = None,
                      keyword_call_attribution: dict | None = None,
                      campaign: str = "",
                      outcome_history: dict | None = None) -> dict:
    """
    Apply optimization rules. Returns recommended actions.
    campaign: name of the campaign being evaluated — used to scope memory lookups.
              Empty string = global memory only.
    outcome_history: pre-loaded from _load_outcome_history(); if None, loaded here.
    """
    # Load persistent memory — what the optimizer has been taught
    try:
        from database import get_optimizer_memory_dict
        mem = get_optimizer_memory_dict(campaign=campaign)
    except Exception as e:
        logger.warning(f"Could not load optimizer memory: {e}")
        mem = {'term_classifications': {}, 'keyword_overrides': {}, 'campaign_rules': {}, 'general': {}}

    term_classifications = mem.get('term_classifications', {})
    keyword_overrides = mem.get('keyword_overrides', {})
    campaign_rules = mem.get('campaign_rules', {})

    min_spend_before_pause = float(campaign_rules.get('min_spend_before_pause', 40))
    min_clicks_before_pause = int(campaign_rules.get('min_clicks_before_pause', 20))

    # Load outcome history (AI learning loop)
    if outcome_history is None:
        outcome_history = _load_outcome_history(days_back=90)

    logger.info(f"Optimizer memory loaded: {len(term_classifications)} term classifications, "
                f"{len(keyword_overrides)} keyword overrides, {len(campaign_rules)} campaign rules, "
                f"{len(outcome_history)} outcome history entries")

    actions = {
        "pause": [],            # Keywords to pause (high spend, no results)
        "increase_bid": [],     # Keywords to bid up (proven production)
        "decrease_bid": [],     # Keywords to bid down (high cost, low conversion)
        "new_exact": [],        # Search terms to add as exact match keywords
        "new_negatives": [],    # Search terms to add as negatives
        "tighten_match": [],    # Broad keywords to convert to exact match
        "summary": {},
        "memory_applied": [],   # Log of memory overrides that changed the outcome
    }

    call_attribution = call_attribution or {}
    keyword_call_attribution = keyword_call_attribution or {}

    def _calls_for(camp_name: str) -> dict:
        """Return call attribution for a campaign name (case-insensitive)."""
        return call_attribution.get((camp_name or "").lower(), {
            "calls": 0, "booked_calls": 0, "confirmed_appts": 0, "avg_duration_sec": 0,
        })

    def _kw_calls_for(kw_text: str) -> dict:
        """Return keyword-level call attribution (case-insensitive)."""
        return keyword_call_attribution.get((kw_text or "").lower().strip(), {
            "calls": 0, "booked_calls": 0, "confirmed_appts": 0, "avg_duration_sec": 0.0,
        })

    total_spend = sum(k["cost"] for k in keyword_perf)
    total_clicks = sum(k["clicks"] for k in keyword_perf)
    total_leads = sum(a["leads"] for a in attribution.values())
    total_production = sum(a["production"] for a in attribution.values())
    total_calls = sum(c["calls"] for c in call_attribution.values())
    total_booked_calls = sum(c["booked_calls"] for c in call_attribution.values())
    total_confirmed_appts = sum(c["confirmed_appts"] for c in call_attribution.values())

    # Rule 1: Pause keywords with spend > threshold and zero leads/calls
    for kw in keyword_perf:
        keyword = kw["keyword"]
        keyword_lower = keyword.lower()
        attr = attribution.get(keyword, {"leads": 0, "booked": 0, "production": 0})

        # Check memory override first
        override = keyword_overrides.get(keyword_lower)
        if override == 'never_pause':
            actions["memory_applied"].append(f"SKIP PAUSE '{keyword}': memory says '{override}'")
            continue

        if kw["cost"] > min_spend_before_pause and attr["leads"] == 0 and kw["clicks"] > min_clicks_before_pause:
            kw_calls = _kw_calls_for(keyword)
            camp_calls = _calls_for(kw.get("campaign", ""))

            # Learning loop guard: if previous pause of this keyword DEGRADED performance, skip
            hist = outcome_history.get((kw["resource_name"], "pause_keyword"))
            if hist and hist["degraded"] >= 1 and hist["improved"] == 0:
                actions["memory_applied"].append(
                    f"SKIP PAUSE '{keyword}': previous pause degraded campaign performance — learning loop override"
                )
                continue

            # Guard 1: this specific keyword drove a call or confirmed appt — protect it
            if kw_calls["confirmed_appts"] > 0 or kw_calls["booked_calls"] > 0:
                actions["memory_applied"].append(
                    f"SKIP PAUSE '{keyword}': drove {kw_calls['calls']} calls / "
                    f"{kw_calls['confirmed_appts']} confirmed appts directly"
                )
                continue
            if kw_calls["calls"] >= 2:
                actions["memory_applied"].append(
                    f"SKIP PAUSE '{keyword}': drove {kw_calls['calls']} inbound calls "
                    f"(keyword-level attribution)"
                )
                continue

            # Guard 2 (legacy fallback): this keyword has no keyword-level call data yet,
            # but the campaign is generating calls or confirmed appts — protect it.
            # Fires when: no keyword-level attribution for this kw AND campaign has any calls
            # (not just confirmed appts) so brand-new campaigns with unattributed calls are safe.
            camp_has_signal = (camp_calls["confirmed_appts"] > 0 or camp_calls["calls"] >= 3)
            if not kw_calls["calls"] and camp_has_signal:
                actions["memory_applied"].append(
                    f"SKIP PAUSE '{keyword}': campaign '{kw.get('campaign','')}' has "
                    f"{camp_calls['calls']} call(s) / {camp_calls['confirmed_appts']} confirmed OD appt(s) — "
                    f"no keyword-level call data yet for this keyword"
                )
                continue

            actions["pause"].append({
                "keyword": keyword,
                "match_type": kw["match_type"],
                "resource_name": kw["resource_name"],
                "reason": (
                    f"${kw['cost']:.2f} spent, {kw['clicks']} clicks, "
                    f"0 form leads, 0 calls attributed"
                ),
                "cost": kw["cost"],
            })

    # Rule 2: Increase bids on keywords with production or strong call conversions
    for kw in keyword_perf:
        keyword = kw["keyword"]
        attr = attribution.get(keyword, {"leads": 0, "booked": 0, "production": 0})
        kw_calls = _kw_calls_for(keyword)
        camp_calls = _calls_for(kw.get("campaign", ""))

        # Learning loop guard: skip bid increase if previous increases degraded performance
        bid_hist = outcome_history.get((kw["resource_name"], "increase_bid"))
        if bid_hist and bid_hist["degraded"] >= 1 and bid_hist["improved"] == 0:
            actions["memory_applied"].append(
                f"SKIP BID UP '{keyword}': previous bid increase degraded performance — learning loop override"
            )
            continue

        if attr["production"] > 0:
            # Gold standard: keyword has OD-attributed production revenue
            roas = attr["production"] / kw["cost"] if kw["cost"] > 0 else float("inf")
            actions["increase_bid"].append({
                "keyword": keyword,
                "match_type": kw["match_type"],
                "resource_name": kw["resource_name"],
                "current_bid_micros": kw.get("current_bid_micros", 0),
                "reason": f"ROAS {roas:.1f}x — ${attr['production']:.0f} production from ${kw['cost']:.2f} spend",
                "roas": roas,
            })
        elif kw_calls["confirmed_appts"] > 0 and kw["cost"] > 0:
            # This specific keyword drove confirmed OD appointments via inbound calls
            cost_per_appt = kw["cost"] / kw_calls["confirmed_appts"]
            if cost_per_appt < 300:
                actions["increase_bid"].append({
                    "keyword": keyword,
                    "match_type": kw["match_type"],
                    "resource_name": kw["resource_name"],
                    "current_bid_micros": kw.get("current_bid_micros", 0),
                    "reason": (
                        f"Keyword drove {kw_calls['confirmed_appts']} confirmed OD appt(s) "
                        f"via {kw_calls['calls']} inbound calls (${cost_per_appt:.0f}/appt)"
                    ),
                    "roas": 0,
                })
        elif kw_calls["booked_calls"] > 0 and kw["cost"] > 0:
            # Keyword drove booked calls (no OD match yet but call outcome = booked)
            cost_per_booking = kw["cost"] / kw_calls["booked_calls"]
            if cost_per_booking < 80:
                actions["increase_bid"].append({
                    "keyword": keyword,
                    "match_type": kw["match_type"],
                    "resource_name": kw["resource_name"],
                    "current_bid_micros": kw.get("current_bid_micros", 0),
                    "reason": (
                        f"Keyword drove {kw_calls['booked_calls']} booked call(s) "
                        f"at ${cost_per_booking:.2f}/booking"
                    ),
                    "roas": 0,
                })
        elif attr["booked"] > 0 and kw["cost"] > 0:
            # Form booking signal (checked after call signals — call data is stronger)
            cost_per_booking = kw["cost"] / attr["booked"]
            if cost_per_booking < 50:
                actions["increase_bid"].append({
                    "keyword": keyword,
                    "match_type": kw["match_type"],
                    "resource_name": kw["resource_name"],
                    "current_bid_micros": kw.get("current_bid_micros", 0),
                    "reason": f"${cost_per_booking:.2f}/booking — {attr['booked']} form bookings",
                    "roas": 0,
                })
        elif not kw_calls["calls"] and camp_calls["confirmed_appts"] > 0 and kw["cost"] > 0:
            # Legacy fallback: this keyword has no call attribution data, use campaign-level signal
            cost_per_appt = kw["cost"] / camp_calls["confirmed_appts"]
            if cost_per_appt < 300:
                actions["increase_bid"].append({
                    "keyword": keyword,
                    "match_type": kw["match_type"],
                    "resource_name": kw["resource_name"],
                    "current_bid_micros": kw.get("current_bid_micros", 0),
                    "reason": (
                        f"Campaign has {camp_calls['confirmed_appts']} confirmed OD appt(s) "
                        f"from inbound calls (${cost_per_appt:.0f}/appt — campaign-level only)"
                    ),
                    "roas": 0,
                })

    # Rule 3: Decrease bids on high-cost, low-conversion keywords
    for kw in keyword_perf:
        keyword = kw["keyword"]
        attr = attribution.get(keyword, {"leads": 0, "booked": 0, "production": 0})

        if kw["cost"] > 10 and kw["clicks"] > 5 and attr["leads"] > 0 and attr["booked"] == 0:
            actions["decrease_bid"].append({
                "keyword": keyword,
                "match_type": kw["match_type"],
                "resource_name": kw["resource_name"],
                "current_bid_micros": kw.get("current_bid_micros", 0),
                "reason": f"{attr['leads']} leads but 0 bookings from ${kw['cost']:.2f} spend",
            })

    # Rule 6: Tighten match type — broad keywords with call/lead signal but poor CPA
    # Do Add EXACT first, then Pause BROAD (so no impression gap between the two GAds calls).
    for kw in keyword_perf:
        keyword = kw["keyword"]
        match_type = (kw.get("match_type") or "").upper()
        if "BROAD" not in match_type:
            continue  # Only targets broad match keywords

        kw_calls = _kw_calls_for(keyword)
        attr = attribution.get(keyword, {"leads": 0, "booked": 0, "production": 0})
        acquisitions = kw_calls["calls"] + attr["leads"]

        # Skip if keyword has no signal at all (nothing worth keeping in exact)
        if acquisitions == 0:
            continue

        cpa = kw["cost"] / acquisitions if acquisitions else 0
        # Tighten if: has signal (calls or leads) but high CPA from broad waste
        if kw["cost"] > 20 and cpa > 40:
            # Learning loop guard: skip if previous tighten degraded
            tighten_hist = outcome_history.get((kw["resource_name"], "tighten_match_type"))
            if tighten_hist and tighten_hist["degraded"] >= 1 and tighten_hist["improved"] == 0:
                actions["memory_applied"].append(
                    f"SKIP TIGHTEN '{keyword}': previous match-type change degraded performance"
                )
                continue

            actions["tighten_match"].append({
                "keyword": keyword,
                "current_match_type": match_type,
                "proposed_match_type": "EXACT",
                "resource_name": kw["resource_name"],
                "ad_group_resource": kw.get("ad_group_resource", ""),
                "campaign": kw.get("campaign", ""),
                "campaign_resource": kw.get("campaign_resource", ""),
                "cost": kw["cost"],
                "calls": kw_calls["calls"],
                "leads": attr["leads"],
                "reason": (
                    f"Broad match '{keyword}' spent ${kw['cost']:.2f} with {acquisitions} acquisition(s) "
                    f"(${cpa:.0f}/acq). Tighten to exact match to eliminate irrelevant search terms "
                    f"while keeping proven converting queries."
                ),
            })

    # ── Negative keyword signals ──────────────────────────────────────
    # Search terms that indicate the person can't/won't pay for treatment.
    # These waste ad budget because they'll never convert to a $15k+ case.
    # Hard negatives — genuinely not a dental patient searching for treatment
    _HARD_NEGATIVES = [
        "dental school", "dental schools",        # looking for student-rate work
        "diy", "home remed",                      # not seeking professional care
        "complaint", "lawsuit", "malpractice",    # legal research
        "salary", "job", "career", "how to become",  # career searches
    ]
    # Soft negatives — empty for now, let the pipeline data decide
    _SOFT_NEGATIVES = []
    # EVERYTHING ELSE gets tracked and judged by real pipeline data:
    # cheap, low cost, affordable, discount, free — price-sensitive buyers
    # cost, price, how much, payment plan, financing — research/buying intent
    # review — evaluating the practice
    # clinical trial, medicaid, medicare — let data prove they don't convert
    # can't afford — might convert with financing options

    # ── Competitor names — any search containing these is a competitor search
    # and should ALWAYS be a negative keyword (no spend on competitor brand terms)
    _COMPETITOR_NAMES = [
        # Direct local competitors
        "grace dental", "grace smiles",
        "simply orthodontics", "simply ortho",
        "grafton smiles",                         # different practice
        "aspen dental",
        "western mass dental",
        "westborough dental",
        "shrewsbury dental",
        "worcester dental",
        "millbury dental",
        "auburn dental",
        "northborough dental",
        "framingham dental",
        "gentle dental",
        "comfort dental",
        "perfect teeth",
        "castle dental",
        "bright now dental",
        "affordable dentures",
        "small smiles",
        # Generic competitor signals — another named practice
        # (catches "[name] dental [city]" patterns where name isn't ours)
    ]
    # Our own practice names — do NOT negative these
    _OUR_NAMES = ["grafton dental", "grafton dental care", "gdc", "dr gupta", "dr. gupta"]

    def _is_competitor_term(term: str) -> str:
        """Return reason string if this search term is a competitor brand search, else empty."""
        t = term.lower()
        # Skip if it's our own practice name
        for own in _OUR_NAMES:
            if own in t:
                return ""
        for comp in _COMPETITOR_NAMES:
            if comp in t:
                return f"Competitor brand search: '{comp}' — should not show our ads"
        return ""

    def _is_negative_intent(term: str) -> str:
        """Check if a search term has negative intent. Returns reason or empty string."""
        t = term.lower()
        # Check competitor first — highest priority negative
        comp_reason = _is_competitor_term(t)
        if comp_reason:
            return comp_reason
        for signal in _HARD_NEGATIVES:
            if signal in t:
                return f"Negative intent: '{signal}'"
        for signal in _SOFT_NEGATIVES:
            if signal in t:
                # Make sure it's not a clinical term (e.g. "free gingival graft")
                if "free gingival" in t or "free connective" in t:
                    return ""
                return f"Likely negative: '{signal}'"
        return ""

    # Rule 4: Harvest search terms that converted AND have buying intent
    existing_keywords = {kw["keyword"].lower() for kw in keyword_perf}
    for st in search_terms:
        term = st["search_term"].lower()

        # Check memory classification first — overrides all heuristics
        mem_classification = None
        for mem_term, mem_val in term_classifications.items():
            if mem_term in term or term in mem_term:
                mem_classification = mem_val
                break

        if mem_classification == 'negative':
            actions["new_negatives"].append({
                "search_term": st["search_term"],
                "clicks": st.get("clicks", 0),
                "impressions": st.get("impressions", 0),
                "cost": st["cost"],
                "campaign_resource": st.get("campaign_resource", ""),
                "campaign": st.get("campaign", ""),
                "ad_group_resource": st.get("ad_group_resource", ""),
                "reason": f"Memory: classified as negative",
            })
            actions["memory_applied"].append(f"NEGATIVE '{st['search_term']}': memory classification")
            continue
        elif mem_classification in ('good_keyword', 'irrelevant'):
            # Skip — don't add as negative, don't add as exact match candidate
            actions["memory_applied"].append(f"SKIP '{st['search_term']}': memory says '{mem_classification}'")
            continue

        neg_reason = _is_negative_intent(term)

        if neg_reason:
            # Even if Google says it "converted", the intent is wrong — add as negative
            actions["new_negatives"].append({
                "search_term": st["search_term"],
                "clicks": st.get("clicks", 0),
                "impressions": st.get("impressions", 0),
                "cost": st["cost"],
                "campaign_resource": st.get("campaign_resource", ""),
                "campaign": st.get("campaign", ""),
                "ad_group_resource": st.get("ad_group_resource", ""),
                "reason": neg_reason,
            })
        elif st["conversions"] > 0 and term not in existing_keywords:
            # Count as real acquisition if: form lead attribution OR keyword-level call attribution
            term_has_real_leads = any(
                term in a_kw.lower() or a_kw.lower() in term
                for a_kw in attribution.keys()
            ) if attribution else False

            # Word-boundary check: split both into tokens and look for *significant* overlap.
            # Filter stop words so generic dental terms don't trigger false-positive harvesting.
            term_tokens = set(term.split()) - _HARVEST_STOP_WORDS
            term_has_call_attr = bool(term_tokens) and any(
                term_tokens & (set(kw_lower.split()) - _HARVEST_STOP_WORDS)
                for kw_lower in keyword_call_attribution.keys()
            ) if keyword_call_attribution else False

            if term_has_real_leads or term_has_call_attr:
                signal = "form lead + call attribution" if (term_has_real_leads and term_has_call_attr) \
                    else ("call attribution" if term_has_call_attr else "form lead attribution")
                actions["new_exact"].append({
                    "search_term": st["search_term"],
                    "clicks": st["clicks"],
                    "conversions": st["conversions"],
                    "cost": st["cost"],
                    "ad_group_resource": st.get("ad_group_resource", ""),
                    "ad_group": st.get("ad_group", ""),
                    "campaign_resource": st.get("campaign_resource", ""),
                    "reason": f"Has real {signal} + Google conversion",
                })
            else:
                # Conversion in Google but no signal in our system — flag for review
                actions["new_exact"].append({
                    "search_term": st["search_term"],
                    "clicks": st["clicks"],
                    "conversions": st["conversions"],
                    "cost": st["cost"],
                    "ad_group_resource": st.get("ad_group_resource", ""),
                    "ad_group": st.get("ad_group", ""),
                    "campaign_resource": st.get("campaign_resource", ""),
                    "reason": "Google conversion but NO lead/call in pipeline — verify before adding",
                })

    # Rule 5: Negative keywords — high spend with no results
    for st in search_terms:
        term = st["search_term"].lower()
        # Skip if already added as negative in Rule 4
        already_negative = any(n["search_term"].lower() == term for n in actions["new_negatives"])
        if already_negative:
            continue

        if st["cost"] > 5 and st.get("clicks", 0) == 0 and st.get("impressions", 0) > 20:
            actions["new_negatives"].append({
                "search_term": st["search_term"],
                "impressions": st["impressions"],
                "cost": st["cost"],
                "campaign_resource": st.get("campaign_resource", ""),
                "campaign": st.get("campaign", ""),
                "ad_group_resource": st.get("ad_group_resource", ""),
                "reason": "High impressions, zero clicks — likely irrelevant",
            })
        elif st.get("clicks", 0) > 5 and st["cost"] > 15 and st["conversions"] == 0:
            term_lower = st["search_term"].lower()
            # Guard A: term matches a form-attributed keyword
            has_leads = any(
                term_lower in a_kw.lower()
                for a_kw in attribution.keys()
            )
            # Guard B: term has significant token overlap with a call-attributed keyword.
            # This prevents negativing search terms that drove inbound calls even when
            # Google's conversion column shows 0 (calls tracked via Mango, not GAds).
            has_call_signal = False
            if keyword_call_attribution and not has_leads:
                sig_term = set(term_lower.split()) - _HARVEST_STOP_WORDS
                if sig_term:
                    for kw_lower_key, kw_data in keyword_call_attribution.items():
                        sig_kw = set(kw_lower_key.split()) - _HARVEST_STOP_WORDS
                        if sig_term & sig_kw and kw_data.get("calls", 0) > 0:
                            has_call_signal = True
                            break
            # Guard C: if we have ZERO keyword-level call data (gads_clicks sync hasn't
            # run yet), we can't distinguish converting vs junk terms — hold off on all
            # negatives for campaigns that are generating calls.
            # Once keyword attribution is populated, Guard B handles per-term protection.
            no_kw_data = not keyword_call_attribution
            camp_lower = st.get("campaign", "").lower()
            camp_has_calls = False
            if no_kw_data and call_attribution:
                camp_calls = call_attribution.get(camp_lower, {})
                camp_has_calls = camp_calls.get("calls", 0) >= 3

            if not has_leads and not has_call_signal and not camp_has_calls:
                actions["new_negatives"].append({
                    "search_term": st["search_term"],
                    "clicks": st.get("clicks", 0),
                    "cost": st["cost"],
                    "campaign_resource": st.get("campaign_resource", ""),
                    "campaign": st.get("campaign", ""),
                    "ad_group_resource": st.get("ad_group_resource", ""),
                    "reason": f"${st['cost']:.2f} spent, {st.get('clicks',0)} clicks, 0 conversions/leads/calls",
                })

    # Summary
    combined_acq = total_leads + total_booked_calls
    actions["summary"] = {
        "total_spend": round(total_spend, 2),
        "total_clicks": total_clicks,
        "total_leads": total_leads,
        "total_production": round(total_production, 2),
        "overall_roas": round(total_production / total_spend, 1) if total_spend > 0 else 0,
        "cost_per_lead": round(total_spend / total_leads, 2) if total_leads > 0 else 0,
        # Call enrichment
        "total_calls": total_calls,
        "total_booked_calls": total_booked_calls,
        "total_confirmed_appts": total_confirmed_appts,
        "cost_per_acquisition": round(total_spend / combined_acq, 2) if combined_acq > 0 else 0,
        # Actions
        "keywords_to_pause": len(actions["pause"]),
        "keywords_to_bid_up": len(actions["increase_bid"]),
        "keywords_to_bid_down": len(actions["decrease_bid"]),
        "new_exact_match": len(actions["new_exact"]),
        "new_negatives": len(actions["new_negatives"]),
        "memory_overrides_applied": len(actions["memory_applied"]),
    }

    if actions["memory_applied"]:
        logger.info(f"  Memory overrides applied ({len(actions['memory_applied'])}):")
        for m in actions["memory_applied"]:
            logger.info(f"    {m}")

    return actions


# ── Execute Actions ──────────────────────────────────────────────────────────

def _execute_pause(client, customer_id: str, keywords: list) -> int:
    """
    Pause keywords via Google Ads API.

    Phase 1: each keyword in the list must have an 'action_id' field (UUID).
    Checks the kill switch first — if blocked, marks all action rows as 'blocked'.
    Uses partial_failure=True so one bad keyword doesn't fail the whole batch.
    Returns count of successfully paused keywords.
    """
    if not keywords:
        return 0

    from campaign_safety import check_writes_enabled, WriteBlockedError
    from campaign_audit import mark_executed

    # Kill switch check
    try:
        check_writes_enabled()
    except WriteBlockedError as e:
        for kw in keywords:
            aid = kw.get("action_id")
            if aid:
                from database import update_gads_action_result
                update_gads_action_result(
                    action_id=aid,
                    executed=False,
                    execution_result="blocked",
                    error_detail=str(e),
                )
        logger.warning(f"Keyword pause BLOCKED by kill switch: {e}")
        return 0

    service = client.get_service("AdGroupCriterionService")
    operations = []

    for kw in keywords:
        operation = client.get_type("AdGroupCriterionOperation")
        criterion = operation.update
        criterion.resource_name = kw["resource_name"]
        criterion.status = client.enums.AdGroupCriterionStatusEnum.PAUSED
        client.copy_from(
            operation.update_mask,
            client.get_type("FieldMask")(paths=["status"])
        )
        operations.append(operation)

    try:
        response = service.mutate_ad_group_criteria(
            customer_id=customer_id,
            operations=operations,
            partial_failure=True,   # don't fail-all on one bad op
        )
        success_count = 0
        # Walk results — one entry per operation in order
        results = list(response.results) if response.results else []
        for i, kw in enumerate(keywords):
            aid = kw.get("action_id")
            if i < len(results) and results[i].resource_name:
                if aid:
                    mark_executed(aid, success=True)
                success_count += 1
            else:
                if aid:
                    mark_executed(aid, success=False, error_detail="partial_failure or no result")
        logger.info(f"Paused {success_count}/{len(keywords)} keywords")
        return success_count
    except Exception as e:
        logger.error(f"Failed to pause keywords: {e}")
        for kw in keywords:
            aid = kw.get("action_id")
            if aid:
                mark_executed(aid, success=False, error_detail=str(e))
        return 0


def _execute_single_pause(client, customer_id: str, resource_name: str) -> bool:
    """
    Pause a single keyword by resource_name.
    Used by the /approve endpoint for individual Apply-button execution.
    Does NOT check kill switch — caller must check first.
    Returns True on success.
    """
    service = client.get_service("AdGroupCriterionService")
    operation = client.get_type("AdGroupCriterionOperation")
    criterion = operation.update
    criterion.resource_name = resource_name
    criterion.status = client.enums.AdGroupCriterionStatusEnum.PAUSED
    client.copy_from(
        operation.update_mask,
        client.get_type("FieldMask")(paths=["status"])
    )
    try:
        service.mutate_ad_group_criteria(
            customer_id=customer_id,
            operations=[operation],
        )
        return True
    except Exception as e:
        logger.error(f"Single pause failed for {resource_name}: {e}")
        raise


# Bid guardrails — hard limits enforced before any bid write
_MIN_BID_MICROS = 10_000       # $0.01 — Google rejects sub-cent bids
_MAX_BID_MICROS = 50_000_000   # $50.00 — hard ceiling for GDC ad spend


def _execute_bid_change(client, customer_id: str, resource_name: str,
                         new_bid_micros: int) -> bool:
    """
    Update the manual CPC bid (cpc_bid_micros) on a single keyword.
    Uses the same FieldMask pattern as _execute_single_pause.
    Does NOT check kill switch — caller must check first.
    Raises ValueError if bid is outside guardrail limits.
    Returns True on success.
    """
    if new_bid_micros < _MIN_BID_MICROS:
        raise ValueError(
            f"Bid {new_bid_micros} micros (${new_bid_micros/1_000_000:.4f}) "
            f"is below minimum ${_MIN_BID_MICROS/1_000_000:.2f}"
        )
    if new_bid_micros > _MAX_BID_MICROS:
        raise ValueError(
            f"Bid {new_bid_micros} micros (${new_bid_micros/1_000_000:.2f}) "
            f"exceeds maximum ${_MAX_BID_MICROS/1_000_000:.2f}"
        )

    service = client.get_service("AdGroupCriterionService")
    operation = client.get_type("AdGroupCriterionOperation")
    criterion = operation.update
    criterion.resource_name = resource_name
    criterion.cpc_bid_micros = new_bid_micros
    client.copy_from(
        operation.update_mask,
        client.get_type("FieldMask")(paths=["cpc_bid_micros"])
    )
    try:
        service.mutate_ad_group_criteria(
            customer_id=customer_id,
            operations=[operation],
        )
        return True
    except Exception as e:
        logger.error(f"Bid change failed for {resource_name} → {new_bid_micros}: {e}")
        raise


def _execute_add_keyword(client, customer_id: str, ad_group_resource: str,
                          keyword_text: str, match_type: str = "EXACT") -> bool:
    """
    Add a new keyword to an ad group.
    Does NOT check kill switch — caller must check first.
    Handles ALREADY_EXISTS gracefully (returns True, caller marks as duplicate).
    Returns True on success or duplicate.
    """
    match_type = (match_type or "EXACT").upper()
    if match_type not in ("EXACT", "PHRASE", "BROAD"):
        raise ValueError(f"Invalid match_type '{match_type}' — must be EXACT, PHRASE, or BROAD")
    service = client.get_service("AdGroupCriterionService")
    operation = client.get_type("AdGroupCriterionOperation")
    criterion = operation.create
    criterion.ad_group = ad_group_resource
    criterion.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
    criterion.keyword.text = keyword_text
    criterion.keyword.match_type = client.enums.KeywordMatchTypeEnum[match_type]
    try:
        service.mutate_ad_group_criteria(
            customer_id=customer_id,
            operations=[operation],
        )
        logger.info(f"Added keyword '{keyword_text}' [{match_type}] to {ad_group_resource}")
        return True
    except Exception as e:
        err_str = str(e)
        # Idempotent: keyword already exists is not a hard failure
        if "KEYWORD_ALREADY_EXISTS" in err_str or "already exists" in err_str.lower():
            logger.info(f"Keyword '{keyword_text}' already exists in {ad_group_resource} — treating as success")
            return True
        logger.error(f"Add keyword failed '{keyword_text}' → {ad_group_resource}: {e}")
        raise


def _execute_add_negative(client, customer_id: str, campaign_resource: str,
                           keyword_text: str, match_type: str = "BROAD") -> bool:
    """
    Add a campaign-level negative keyword.
    Does NOT check kill switch — caller must check first.
    Handles ALREADY_EXISTS gracefully (returns True).
    Returns True on success or duplicate.
    """
    match_type = (match_type or "BROAD").upper()
    if match_type not in ("EXACT", "PHRASE", "BROAD"):
        raise ValueError(f"Invalid match_type '{match_type}' — must be EXACT, PHRASE, or BROAD")
    service = client.get_service("CampaignCriterionService")
    operation = client.get_type("CampaignCriterionOperation")
    criterion = operation.create
    criterion.campaign = campaign_resource
    criterion.negative = True
    criterion.keyword.text = keyword_text
    criterion.keyword.match_type = client.enums.KeywordMatchTypeEnum[match_type]
    try:
        service.mutate_campaign_criteria(
            customer_id=customer_id,
            operations=[operation],
        )
        logger.info(f"Added negative '{keyword_text}' [{match_type}] to campaign {campaign_resource}")
        return True
    except Exception as e:
        err_str = str(e)
        if "KEYWORD_ALREADY_EXISTS" in err_str or "already exists" in err_str.lower():
            logger.info(f"Negative '{keyword_text}' already in {campaign_resource} — treating as success")
            return True
        logger.error(f"Add negative failed '{keyword_text}' → {campaign_resource}: {e}")
        raise


# ── New Execute Functions (Opus Plan — May 2026) ──────────────────────────────

def _execute_enable_keyword(client, customer_id: str, resource_name: str) -> bool:
    """
    Enable a paused keyword by resource_name.
    Does NOT check kill switch — caller must check first.
    Returns True on success.
    """
    service = client.get_service("AdGroupCriterionService")
    operation = client.get_type("AdGroupCriterionOperation")
    criterion = operation.update
    criterion.resource_name = resource_name
    criterion.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
    client.copy_from(
        operation.update_mask,
        client.get_type("FieldMask")(paths=["status"])
    )
    try:
        service.mutate_ad_group_criteria(
            customer_id=customer_id,
            operations=[operation],
        )
        logger.info(f"Enabled keyword: {resource_name}")
        return True
    except Exception as e:
        logger.error(f"Enable keyword failed for {resource_name}: {e}")
        raise


def _get_campaign_budget_resource(client, customer_id: str, campaign_resource: str) -> tuple:
    """
    Return (budget_resource_name, current_amount_micros) for a campaign.
    Raises ValueError if campaign not found.
    """
    service = client.get_service("GoogleAdsService")
    query = f"""
        SELECT campaign.campaign_budget, campaign_budget.amount_micros
        FROM campaign
        WHERE campaign.resource_name = '{campaign_resource}'
        LIMIT 1
    """
    try:
        response = service.search(customer_id=customer_id, query=query)
        for row in response:
            return row.campaign.campaign_budget, row.campaign_budget.amount_micros
    except Exception as e:
        logger.error(f"Failed to get budget for {campaign_resource}: {e}")
        raise
    raise ValueError(f"Campaign not found: {campaign_resource}")


def _execute_budget_change(client, customer_id: str, campaign_resource: str,
                            new_daily_budget_usd: float) -> bool:
    """
    Update the daily budget for a campaign.
    Performs safety checks (absolute limits + 25% increase guard) before writing.
    Does NOT check kill switch — caller must check first.
    Returns True on success.
    """
    from campaign_safety import check_budget_absolute_limits, check_budget_change_safe, WriteBlockedError

    new_micros = int(new_daily_budget_usd * 1_000_000)

    # Absolute limits first
    check_budget_absolute_limits(new_micros)

    # Get current budget
    budget_resource, current_micros = _get_campaign_budget_resource(client, customer_id, campaign_resource)

    # 25% increase guard
    if not check_budget_change_safe(current_micros, new_micros):
        raise WriteBlockedError(
            f"Budget increase from ${current_micros/1_000_000:.2f} to ${new_daily_budget_usd:.2f} "
            f"exceeds 25% limit. Increase manually if needed."
        )

    service = client.get_service("CampaignBudgetService")
    operation = client.get_type("CampaignBudgetOperation")
    budget = operation.update
    budget.resource_name = budget_resource
    budget.amount_micros = new_micros
    client.copy_from(
        operation.update_mask,
        client.get_type("FieldMask")(paths=["amount_micros"])
    )
    try:
        service.mutate_campaign_budgets(
            customer_id=customer_id,
            operations=[operation],
        )
        logger.info(f"Budget updated for {campaign_resource}: "
                    f"${current_micros/1_000_000:.2f} → ${new_daily_budget_usd:.2f}/day")
        return True
    except Exception as e:
        logger.error(f"Budget change failed for {campaign_resource}: {e}")
        raise


def _execute_change_bid_strategy(client, customer_id: str, campaign_resource: str,
                                   bid_strategy: str, target_cpa_micros: int = 0,
                                   target_roas: float = 0.0) -> bool:
    """
    Change a campaign's bid strategy.
    Supports: MAXIMIZE_CONVERSIONS, TARGET_CPA, TARGET_ROAS, MAXIMIZE_CLICKS
    """
    campaign_service = client.get_service("CampaignService")
    campaign = client.get_type("Campaign")
    campaign.resource_name = campaign_resource

    strategy = bid_strategy.upper()
    if strategy == "MAXIMIZE_CONVERSIONS":
        campaign.maximize_conversions.CopyFrom(client.get_type("MaximizeConversions"))
    elif strategy == "TARGET_CPA":
        tc = client.get_type("TargetCpa")
        tc.target_cpa_micros = int(target_cpa_micros)
        campaign.target_cpa.CopyFrom(tc)
    elif strategy == "TARGET_ROAS":
        tr = client.get_type("TargetRoas")
        tr.target_roas = float(target_roas)
        campaign.target_roas.CopyFrom(tr)
    elif strategy == "MAXIMIZE_CLICKS":
        campaign.maximize_clicks.CopyFrom(client.get_type("MaximizeClicks"))
    else:
        raise ValueError(f"Unknown bid strategy: {bid_strategy}")

    from google.protobuf import field_mask_pb2
    field_mask = field_mask_pb2.FieldMask()
    if strategy == "MAXIMIZE_CONVERSIONS":
        field_mask.paths.append("maximize_conversions")
    elif strategy == "TARGET_CPA":
        field_mask.paths.extend(["target_cpa", "target_cpa.target_cpa_micros"])
    elif strategy == "TARGET_ROAS":
        field_mask.paths.extend(["target_roas", "target_roas.target_roas"])
    elif strategy == "MAXIMIZE_CLICKS":
        field_mask.paths.append("maximize_clicks")

    operation = client.get_type("CampaignOperation")
    operation.update.CopyFrom(campaign)
    operation.update_mask.CopyFrom(field_mask)

    try:
        response = campaign_service.mutate_campaigns(
            customer_id=customer_id,
            operations=[operation],
        )
        logger.info(f"Changed bid strategy to {bid_strategy} for {campaign_resource}")
        return True
    except Exception as e:
        logger.error(f"Failed to change bid strategy: {e}")
        raise


def _execute_change_match_type(client, customer_id: str, resource_name: str,
                                new_match_type: str) -> bool:
    """
    Change a keyword's match type.
    """
    service = client.get_service("AdGroupCriterionService")
    criterion = client.get_type("AdGroupCriterion")
    criterion.resource_name = resource_name
    match_type_enum = client.enums.KeywordMatchTypeEnum[new_match_type.upper()]
    criterion.keyword.match_type = match_type_enum

    from google.protobuf import field_mask_pb2
    field_mask = field_mask_pb2.FieldMask(paths=["keyword.match_type"])

    operation = client.get_type("AdGroupCriterionOperation")
    operation.update.CopyFrom(criterion)
    operation.update_mask.CopyFrom(field_mask)

    try:
        service.mutate_ad_group_criteria(
            customer_id=customer_id,
            operations=[operation],
        )
        logger.info(f"Changed match type to {new_match_type} for {resource_name}")
        return True
    except Exception as e:
        logger.error(f"Failed to change match type: {e}")
        raise


def _get_rsa_current_assets(client, customer_id: str, ad_group_ad_resource: str) -> dict:
    """
    Fetch the current headlines and descriptions for a Responsive Search Ad.
    Returns: {
        "headlines": [{"text": str, "pinned_field": str|None}],
        "descriptions": [{"text": str, "pinned_field": str|None}],
        "ad_group_ad_resource": str
    }
    Returns empty dict if not found.
    """
    service = client.get_service("GoogleAdsService")
    query = f"""
        SELECT
            ad_group_ad.resource_name,
            ad_group_ad.ad.responsive_search_ad.headlines,
            ad_group_ad.ad.responsive_search_ad.descriptions
        FROM ad_group_ad
        WHERE ad_group_ad.resource_name = '{ad_group_ad_resource}'
        LIMIT 1
    """
    try:
        response = service.search(customer_id=customer_id, query=query)
        for row in response:
            rsa = row.ad_group_ad.ad.responsive_search_ad
            headlines = []
            for h in rsa.headlines:
                headlines.append({
                    "text": h.text,
                    "pinned_field": str(h.pinned_field) if h.pinned_field else None,
                })
            descriptions = []
            for d in rsa.descriptions:
                descriptions.append({
                    "text": d.text,
                    "pinned_field": str(d.pinned_field) if d.pinned_field else None,
                })
            return {
                "headlines": headlines,
                "descriptions": descriptions,
                "ad_group_ad_resource": ad_group_ad_resource,
            }
    except Exception as e:
        logger.error(f"Failed to get RSA assets for {ad_group_ad_resource}: {e}")
    return {}


def _execute_update_rsa(client, customer_id: str, ad_group_ad_resource: str,
                         new_headlines: list, new_descriptions: list) -> bool:
    """
    Update a Responsive Search Ad by reading current assets, merging new ones,
    and writing back the full set.

    RSA constraint: max 15 headlines, max 4 descriptions.
    Character limits: headline ≤ 30 chars, description ≤ 90 chars.

    new_headlines: list of str (text only, no pinning — appended after existing unique ones)
    new_descriptions: list of str

    Does NOT check kill switch — caller must check first.
    Returns True on success.
    """
    # Validate character limits
    for h in new_headlines:
        if len(h) > 30:
            raise ValueError(f"Headline '{h}' exceeds 30-char limit ({len(h)} chars)")
    for d in new_descriptions:
        if len(d) > 90:
            raise ValueError(f"Description '{d}' exceeds 90-char limit ({len(d)} chars)")

    # Fetch current assets
    current = _get_rsa_current_assets(client, customer_id, ad_group_ad_resource)
    if not current:
        raise ValueError(f"RSA not found: {ad_group_ad_resource}")

    existing_headline_texts = {h["text"].lower() for h in current.get("headlines", [])}
    existing_desc_texts = {d["text"].lower() for d in current.get("descriptions", [])}

    # Merge: keep existing assets, append unique new ones up to max
    merged_headlines = list(current.get("headlines", []))
    for text in new_headlines:
        if text.lower() not in existing_headline_texts and len(merged_headlines) < 15:
            merged_headlines.append({"text": text, "pinned_field": None})

    merged_descriptions = list(current.get("descriptions", []))
    for text in new_descriptions:
        if text.lower() not in existing_desc_texts and len(merged_descriptions) < 4:
            merged_descriptions.append({"text": text, "pinned_field": None})

    # Build the update operation
    service = client.get_service("AdGroupAdService")
    operation = client.get_type("AdGroupAdOperation")
    ad_group_ad = operation.update
    ad_group_ad.resource_name = ad_group_ad_resource

    rsa = ad_group_ad.ad.responsive_search_ad
    rsa.headlines.clear()
    for h in merged_headlines:
        asset = client.get_type("AdTextAsset")
        asset.text = h["text"]
        rsa.headlines.append(asset)

    rsa.descriptions.clear()
    for d in merged_descriptions:
        asset = client.get_type("AdTextAsset")
        asset.text = d["text"]
        rsa.descriptions.append(asset)

    client.copy_from(
        operation.update_mask,
        client.get_type("FieldMask")(
            paths=["ad.responsive_search_ad.headlines",
                   "ad.responsive_search_ad.descriptions"]
        )
    )

    try:
        response = service.mutate_ad_group_ads(
            customer_id=customer_id,
            operations=[operation],
        )
        logger.info(f"RSA updated: {ad_group_ad_resource} — "
                    f"{len(merged_headlines)} headlines, {len(merged_descriptions)} descriptions")
        return True
    except Exception as e:
        logger.error(f"RSA update failed for {ad_group_ad_resource}: {e}")
        raise


def _resolve_geo_target_id(client, location_name: str, country_code: str = "US") -> tuple:
    """
    Resolve a location name to a GeoTargetConstant resource name.
    Returns (resource_name, canonical_name) or ("", "") if not found.
    """
    service = client.get_service("GeoTargetConstantService")
    try:
        request = client.get_type("SuggestGeoTargetConstantsRequest")
        request.locale = "en"
        request.country_code = country_code
        request.location_names.names.append(location_name)
        response = service.suggest_geo_target_constants(request=request)
        for suggestion in response.geo_target_constant_suggestions:
            gtc = suggestion.geo_target_constant
            return gtc.resource_name, gtc.canonical_name
    except Exception as e:
        logger.error(f"Failed to resolve geo target '{location_name}': {e}")
    return "", ""


def _execute_geo_exclusion(client, customer_id: str, campaign_resource: str,
                            geo_target_resource: str) -> bool:
    """
    Add a campaign-level geo exclusion (negative location target).
    Handles ALREADY_EXISTS gracefully (returns True).
    Does NOT check kill switch — caller must check first.
    Returns True on success or duplicate.
    """
    service = client.get_service("CampaignCriterionService")
    operation = client.get_type("CampaignCriterionOperation")
    criterion = operation.create
    criterion.campaign = campaign_resource
    criterion.negative = True
    criterion.location.geo_target_constant = geo_target_resource

    try:
        service.mutate_campaign_criteria(
            customer_id=customer_id,
            operations=[operation],
        )
        logger.info(f"Geo exclusion added: {geo_target_resource} on {campaign_resource}")
        return True
    except Exception as e:
        err_str = str(e)
        if "DUPLICATE_CAMPAIGN_CRITERION" in err_str or "already exists" in err_str.lower():
            logger.info(f"Geo exclusion already exists: {geo_target_resource} — treating as success")
            return True
        logger.error(f"Geo exclusion failed: {geo_target_resource} on {campaign_resource}: {e}")
        raise


def _get_active_rsa_resources(client, customer_id: str, campaign_resource: str) -> list:
    """
    Fetch all ENABLED RSA ad resources for a campaign.
    Returns list of {ad_group_ad_resource, ad_group, ad_group_resource, headlines_count, descriptions_count}
    """
    service = client.get_service("GoogleAdsService")
    query = f"""
        SELECT
            ad_group_ad.resource_name,
            ad_group.name,
            ad_group.resource_name,
            ad_group_ad.ad.responsive_search_ad.headlines,
            ad_group_ad.ad.responsive_search_ad.descriptions
        FROM ad_group_ad
        WHERE campaign.resource_name = '{campaign_resource}'
            AND ad_group_ad.status = 'ENABLED'
            AND ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'
            AND ad_group.status = 'ENABLED'
    """
    results = []
    try:
        response = service.search(customer_id=customer_id, query=query)
        for row in response:
            rsa = row.ad_group_ad.ad.responsive_search_ad
            results.append({
                "ad_group_ad_resource": row.ad_group_ad.resource_name,
                "ad_group": row.ad_group.name,
                "ad_group_resource": row.ad_group.resource_name,
                "headlines_count": len(rsa.headlines),
                "descriptions_count": len(rsa.descriptions),
                "headline_texts": [h.text for h in rsa.headlines[:5]],  # preview for Claude context
            })
    except Exception as e:
        logger.warning(f"Could not fetch RSA resources for {campaign_resource}: {e}")
    return results


# ── Main Entry Point ─────────────────────────────────────────────────────────

def optimize_campaign(dry_run: bool = True, trigger: str = "admin_manual") -> dict:
    """
    Run the full optimization cycle.

    Phase 1 behavior:
    - Creates a gads_optimizer_runs record at the start.
    - Expires stale pending_approval rows (>48h old) before generating new ones.
    - Every recommendation generates a gads_audit_log row with
      execution_result='pending_approval' and an 'action_id' embedded in the
      returned report dict. The frontend Apply button references this action_id.
    - dry_run parameter kept for backward compatibility but no longer changes
      behavior — use Apply buttons in admin UI to execute individual actions.
    """
    from campaign_audit import log_pending, expire_stale_pending
    from database import (
        create_optimizer_run, update_optimizer_run, get_setting
    )

    settings = get_settings()
    run_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    # Expire recommendations older than 48h before generating new ones
    expired = expire_stale_pending(max_age_hours=48)

    try:
        client = _build_client()
    except Exception as e:
        logger.error(f"Failed to create Google Ads client: {e}")
        create_optimizer_run(run_id, trigger=trigger)
        update_optimizer_run(run_id, mode="errored", error=str(e))
        return {"error": str(e), "run_id": run_id}

    customer_id = settings.google_ads_customer_id

    logger.info("=" * 60)
    logger.info(f"AI Campaign Optimizer — run_id={run_id}")
    logger.info("=" * 60)
    if expired:
        logger.info(f"Expired {expired} stale pending rows before this run")

    # Collect data
    logger.info("Collecting keyword performance...")
    keyword_perf = _get_keyword_performance(client, customer_id, days=30)

    logger.info("Collecting search terms...")
    search_terms = _get_search_terms(client, customer_id, days=30)

    logger.info("Building lead attribution...")
    attribution = _get_keyword_attribution()

    logger.info("Building call attribution...")
    call_attribution = _get_call_attribution(days=30)
    if call_attribution:
        logger.info(f"Call attribution: {len(call_attribution)} campaigns, "
                    f"{sum(c['calls'] for c in call_attribution.values())} total calls, "
                    f"{sum(c['confirmed_appts'] for c in call_attribution.values())} confirmed appts")

    logger.info("Building keyword-level call attribution...")
    keyword_call_attribution = _get_keyword_call_attribution(days=30)
    if keyword_call_attribution:
        kw_call_total = sum(e["calls"] for e in keyword_call_attribution.values())
        kw_conf_total = sum(e["confirmed_appts"] for e in keyword_call_attribution.values())
        logger.info(f"Keyword call attribution: {len(keyword_call_attribution)} keywords, "
                    f"{kw_call_total} calls, {kw_conf_total} confirmed appts")
    else:
        logger.info("Keyword call attribution: 0 keywords (run gads-sync + attribute-keywords first)")

    logger.info("Building OD production summary...")
    od_production = _get_od_production_summary(days=30)

    # Fetch Google's own recommendations
    google_recs = []
    try:
        google_recs = _get_google_recommendations(client, customer_id)
        # Persist to DB for UI display between runs
        from database import upsert_google_rec
        import json as _json
        fetched_at = datetime.now(timezone.utc).isoformat()
        for gr in google_recs:
            upsert_google_rec(
                resource_name=gr["resource_name"],
                rec_type=gr["rec_type"],
                campaign_resource=gr.get("campaign_resource", ""),
                campaign_name=gr.get("campaign_name", ""),
                ad_group_resource=gr.get("ad_group_resource", ""),
                title=gr["title"],
                description=gr["description"],
                impact_json=_json.dumps(gr.get("impact", {})),
                details_json=_json.dumps(gr.get("details", {})),
                fetched_at=fetched_at,
            )
        logger.info(f"Stored {len(google_recs)} Google recommendations")
    except Exception as e:
        logger.warning(f"Google recommendations fetch failed (non-fatal): {e}")

    # ── Capture account-wide totals before any filtering ──────────────────────
    total_spend_all_campaigns = round(sum(k.get("cost", 0) for k in keyword_perf), 2)
    total_clicks_all_campaigns = sum(k.get("clicks", 0) for k in keyword_perf)

    # ── Determine which campaigns to analyze ──────────────────────────────────
    # Analyze ALL campaigns that have at least some keyword data/spend.
    # The old ai_review_enabled allow-list is ignored — every active campaign
    # with impressions gets Claude analysis so recommendations appear per-campaign.
    # Paused campaigns are included if they have recent spend data (last 30d).
    active_campaigns_with_data = {
        k.get("campaign", "").strip()
        for k in keyword_perf
        if k.get("campaign", "").strip()
    }
    logger.info(f"Campaigns with keyword data: {active_campaigns_with_data}")

    # Determine the primary campaign name for memory scoping
    campaign_spend: dict = {}
    for kw in keyword_perf:
        camp = kw.get("campaign", "")
        if camp:
            campaign_spend[camp] = campaign_spend.get(camp, 0) + kw.get("cost", 0)
    primary_campaign = max(campaign_spend, key=campaign_spend.get) if campaign_spend else ""
    logger.info(f"Primary campaign for memory scoping: '{primary_campaign}'")

    # Create run record now that we have the primary campaign
    create_optimizer_run(run_id, trigger=trigger, primary_campaign=primary_campaign)

    # Load outcome history once (AI learning loop — shared across all rule passes)
    outcome_history = _load_outcome_history(days_back=90)
    if outcome_history:
        logger.info(f"Outcome history loaded: {len(outcome_history)} entity-operation pairs from last 90d")

    # Analyze
    logger.info("Analyzing and generating recommendations...")
    actions = _analyze_keywords(
        keyword_perf, attribution, search_terms,
        call_attribution=call_attribution,
        keyword_call_attribution=keyword_call_attribution,
        campaign=primary_campaign,
        outcome_history=outcome_history,
    )

    # ── Phase A: Suppress recently-rejected recommendations ───────────────────
    # M6 fix: key suppression by (entity_name_lower, operation) tuple — NOT entity_name
    # alone. A rejected "decrease_bid" on keyword X should NOT suppress "pause_keyword"
    # on the same keyword X. Each action type maps to a distinct operation string.
    try:
        from database import get_recent_rejections
        recent_rejections = get_recent_rejections(days=30)
        suppressed_count = 0

        # Build (entity_name_lower, operation) and (entity_id, operation) sets
        rejected_op_pairs = set()
        rejected_id_op_pairs = set()
        for r in recent_rejections:
            ename = (r.get("entity_name") or "").lower()
            op = r.get("operation") or ""
            eid = r.get("entity_id") or ""
            if ename and op:
                rejected_op_pairs.add((ename, op))
            if eid and op:
                rejected_id_op_pairs.add((eid, op))

        # Map each action_type to the exact operation string stored in gads_audit_log
        ACTION_TO_OPERATION = {
            "pause":         "pause_keyword",
            "increase_bid":  "increase_bid",
            "decrease_bid":  "decrease_bid",
            "new_exact":     "add_exact_keyword",
            "new_negative":  "add_negative_keyword",
        }

        def _is_rejected(item: dict, operation: str) -> bool:
            """Return True if this exact (entity, operation) was recently rejected."""
            eid = item.get("resource_name") or item.get("ad_group_resource") or item.get("campaign_resource") or ""
            ename = (item.get("keyword") or item.get("search_term") or "").lower()
            if eid and (eid, operation) in rejected_id_op_pairs:
                return True
            if ename and (ename, operation) in rejected_op_pairs:
                return True
            return False

        for action_type, operation in ACTION_TO_OPERATION.items():
            before = len(actions.get(action_type, []))
            actions[action_type] = [a for a in actions.get(action_type, []) if not _is_rejected(a, operation)]
            after = len(actions[action_type])
            suppressed_count += (before - after)

        if suppressed_count > 0:
            logger.info(f"[phase_a] Suppressed {suppressed_count} recommendation(s) recently rejected by admin")
    except Exception as _rej_err:
        logger.warning(f"[phase_a] Rejection suppression check failed (non-fatal): {_rej_err}")

    # ── Create pending_approval audit rows for each recommendation ────────────
    actions_pending = 0

    for kw in actions["pause"]:
        aid = log_pending(
            operation="pause_keyword",
            entity_type="keyword",
            entity_id=kw["resource_name"],
            entity_name=kw["keyword"],
            before_state={"status": "ENABLED", "match_type": kw.get("match_type", "")},
            after_state={"status": "PAUSED"},
            optimizer_run_id=run_id,
            reason=kw.get("reason", ""),
            campaign_name=kw.get("campaign", ""),
            priority=10,  # Pausing = high priority (stops waste)
            impact_estimate={"savings_30d_usd": round(kw.get("cost", 0), 2)},
        )
        kw["action_id"] = aid
        actions_pending += 1

    for kw in actions["increase_bid"]:
        current_bid = kw.get("current_bid_micros", 0)
        new_bid = int(current_bid * 1.10) if current_bid > 0 else 0
        aid = log_pending(
            operation="increase_bid",
            entity_type="keyword",
            entity_id=kw.get("resource_name", ""),
            entity_name=kw["keyword"],
            before_state={
                "match_type": kw.get("match_type", ""),
                "current_bid_micros": current_bid,
                "roas": kw.get("roas", 0),
            },
            after_state={
                "bid_change": "+10%",
                "new_bid_micros": new_bid,
            },
            optimizer_run_id=run_id,
            reason=kw.get("reason", ""),
            campaign_name=kw.get("campaign", ""),
            priority=30,
        )
        kw["action_id"] = aid
        actions_pending += 1

    for kw in actions["decrease_bid"]:
        current_bid = kw.get("current_bid_micros", 0)
        new_bid = int(current_bid * 0.90) if current_bid > 0 else 0
        aid = log_pending(
            operation="decrease_bid",
            entity_type="keyword",
            entity_id=kw.get("resource_name", ""),
            entity_name=kw["keyword"],
            before_state={
                "match_type": kw.get("match_type", ""),
                "current_bid_micros": current_bid,
            },
            after_state={
                "bid_change": "-10%",
                "new_bid_micros": new_bid,
            },
            optimizer_run_id=run_id,
            reason=kw.get("reason", ""),
            campaign_name=kw.get("campaign", ""),
            priority=40,
        )
        kw["action_id"] = aid
        actions_pending += 1

    for st in actions["new_exact"]:
        aid = log_pending(
            operation="add_exact_keyword",
            entity_type="keyword",
            entity_id=st.get("ad_group_resource", st["search_term"]),
            entity_name=st["search_term"],
            before_state={
                "type": "search_term",
                "clicks": st.get("clicks", 0),
                "conversions": st.get("conversions", 0),
            },
            after_state={
                "keyword_text": st["search_term"],
                "match_type": "EXACT",
                "ad_group_resource": st.get("ad_group_resource", ""),
                "ad_group": st.get("ad_group", ""),
            },
            optimizer_run_id=run_id,
            reason=st.get("reason", ""),
            campaign_name=st.get("campaign", ""),
            priority=20,
        )
        st["action_id"] = aid
        actions_pending += 1

    for st in actions["new_negatives"]:
        aid = log_pending(
            operation="add_negative_keyword",
            entity_type="keyword",
            entity_id=st.get("campaign_resource", st["search_term"]),
            entity_name=st["search_term"],
            before_state={
                "type": "search_term",
                "cost": st.get("cost", 0),
            },
            after_state={
                "keyword_text": st["search_term"],
                "match_type": "BROAD",
                "campaign_resource": st.get("campaign_resource", ""),
                "campaign": st.get("campaign", ""),
            },
            optimizer_run_id=run_id,
            reason=st.get("reason", ""),
            campaign_name=st.get("campaign", ""),
            priority=15,
            impact_estimate={"savings_30d_usd": round(st.get("cost", 0), 2)},
        )
        st["action_id"] = aid
        actions_pending += 1

    for kw in actions["tighten_match"]:
        aid = log_pending(
            operation="tighten_match_type",
            entity_type="keyword",
            entity_id=kw["resource_name"],
            entity_name=kw["keyword"],
            before_state={
                "match_type": kw["current_match_type"],
                "resource_name": kw["resource_name"],
            },
            after_state={
                "match_type": kw["proposed_match_type"],
                "ad_group_resource": kw.get("ad_group_resource", ""),
                "note": "Add EXACT first, then pause BROAD to avoid impression gap",
            },
            optimizer_run_id=run_id,
            reason=kw.get("reason", ""),
            campaign_name=kw.get("campaign", ""),
            priority=25,
        )
        kw["action_id"] = aid
        actions_pending += 1

    # Patch summary with account-wide spend/clicks (pre-filter values).
    # _analyze_keywords only sees allow-listed campaigns so its totals are partial.
    summary = actions["summary"]
    summary["total_spend"] = total_spend_all_campaigns
    summary["total_clicks"] = total_clicks_all_campaigns
    # Recompute derived metrics using corrected spend
    combined_acq = summary.get("total_leads", 0) + summary.get("total_booked_calls", 0)
    summary["overall_roas"] = round(summary["total_production"] / total_spend_all_campaigns, 1) if total_spend_all_campaigns > 0 else 0
    summary["cost_per_lead"] = round(total_spend_all_campaigns / summary["total_leads"], 2) if summary.get("total_leads", 0) > 0 else 0
    summary["cost_per_acquisition"] = round(total_spend_all_campaigns / combined_acq, 2) if combined_acq > 0 else 0
    # Keyword-level attribution quality metrics
    summary["keywords_with_call_attribution"] = len(keyword_call_attribution)
    summary["keyword_attributed_calls"] = sum(e["calls"] for e in keyword_call_attribution.values())

    # Claude structured recommendations — run once per active campaign.
    # Returns dicts with operation + exact API parameters, not plain text.
    logger.info("Calling Claude (Opus) for structured recommendations...")
    # Use ALL campaigns with keyword data — not just campaign_spend keys
    # (campaign_spend only covers the allow-listed set in legacy mode; now we use all)
    all_campaign_names = sorted(active_campaigns_with_data) or list(campaign_spend.keys()) or ([primary_campaign] if primary_campaign else [])
    priority_counter = 30
    advisories = []  # for report dashboard (human-readable reasons)

    # Operation → (entity_type, entity_id_field, entity_name_field)
    # entity_id_field: which key in the rec dict to use as entity_id in audit log
    # entity_name_field: which key to use as the human-readable entity_name
    _OP_MAP = {
        "add_negative_keyword": ("keyword",  "campaign_resource", "keyword_text"),  # id=campaign_resource, name=term
        "pause_keyword":        ("keyword",  "resource_name",     "keyword_text"),
        "enable_keyword":       ("keyword",  "resource_name",     "keyword_text"),
        "increase_bid":         ("keyword",  "resource_name",     "keyword_text"),
        "decrease_bid":         ("keyword",  "resource_name",     "keyword_text"),
        "add_exact_keyword":    ("keyword",  "ad_group_resource", "keyword_text"),
        "ad_copy_suggestion":   ("ad",       "ad_resource",       "headline"),
        "geo_exclusion":        ("campaign", "geo_target_resource", "location_name"),
        "change_budget":        ("campaign", "campaign_resource", "campaign_resource"),
        "change_bid_strategy":  ("campaign", "campaign_resource", "bid_strategy"),
        "change_match_type":    ("keyword",  "resource_name",     "keyword_text"),
        "add_asset":            ("campaign", "campaign_resource",  "asset_type"),
    }

    # Geo candidates for Grafton Dental Care (Worcester area, MA)
    _CANDIDATE_GEOS = [
        "Worcester, Massachusetts",
        "Shrewsbury, Massachusetts",
        "Northborough, Massachusetts",
        "Westborough, Massachusetts",
        "Grafton, Massachusetts",
        "Millbury, Massachusetts",
        "Auburn, Massachusetts",
    ]

    for camp_name in all_campaign_names:
        camp_lower = camp_name.strip().lower()

        camp_kw   = [k for k in keyword_perf  if k.get("campaign","").strip().lower() == camp_lower]
        camp_st   = [s for s in search_terms   if s.get("campaign","").strip().lower() == camp_lower]
        camp_kw_attr = {k: v for k, v in keyword_call_attribution.items()
                        if any(kw.get("keyword","").strip().lower() == k for kw in camp_kw)}

        # Get campaign resource_name for pre-fetches
        camp_resource = ""
        for kw in camp_kw:
            cr = kw.get("campaign_resource", "")
            if cr:
                camp_resource = cr
                break

        # Pre-fetch RSA resources for this campaign (gives Claude ad_group_ad_resource for ad copy API calls)
        camp_rsa_resources = []
        if camp_resource:
            try:
                camp_rsa_resources = _get_active_rsa_resources(client, customer_id, camp_resource)
                if camp_rsa_resources:
                    logger.info(f"  [{camp_name}] {len(camp_rsa_resources)} RSA(s) found for Claude context")
            except Exception as _rsa_e:
                logger.warning(f"  [{camp_name}] RSA pre-fetch failed (non-fatal): {_rsa_e}")

        # Pre-resolve geo targets for this campaign (gives Claude geo_target_resource for API execution)
        camp_geo_resolutions: dict = {}
        if camp_resource:
            try:
                for geo_name in _CANDIDATE_GEOS:
                    rn, canonical = _resolve_geo_target_id(client, geo_name)
                    if rn:
                        camp_geo_resolutions[geo_name] = {
                            "geo_target_resource": rn,
                            "canonical_name": canonical,
                        }
                if camp_geo_resolutions:
                    logger.info(f"  [{camp_name}] {len(camp_geo_resolutions)} geo targets resolved")
            except Exception as _geo_e:
                logger.warning(f"  [{camp_name}] Geo pre-resolve failed (non-fatal): {_geo_e}")

        structured = _call_claude_advisories(
            camp_kw, attribution, camp_st,
            call_attribution, od_production,
            summary=summary, campaign=camp_name,
            keyword_call_attribution=camp_kw_attr,
            rsa_resources=camp_rsa_resources,
            geo_resolutions=camp_geo_resolutions,
            google_recs=[r for r in google_recs if r.get('campaign_name','').lower() == camp_name.lower() or not r.get('campaign_name')],
        )
        if not structured:
            continue

        logger.info(f"Claude recommendations for '{camp_name}': {len(structured)}")

        for rec in structured:
            op = rec.get("operation", "claude_advisory")
            reason = rec.get("reason", "")
            advisories.append(f"[{camp_name}] {reason}")

            # Build before/after state from the structured fields
            before = {}
            after = {k: v for k, v in rec.items() if k != "operation"}

            # Determine entity fields
            op_meta = _OP_MAP.get(op)
            if op_meta:
                entity_type, id_field, name_field = op_meta
                entity_id   = str(rec.get(id_field, camp_lower.replace(" ", "_")))
                entity_name = str(rec.get(name_field, camp_name))
            else:
                entity_type = "campaign"
                entity_id   = camp_lower.replace(" ", "_")
                entity_name = camp_name

            aid = log_pending(
                operation=op,
                entity_type=entity_type,
                entity_id=entity_id,
                entity_name=entity_name,
                before_state=before,
                after_state=after,
                optimizer_run_id=run_id,
                reason=reason,
                campaign_name=camp_name,
                priority=priority_counter,
                impact_estimate={},
            )
            # Store google_rec_resource_name if this rec came from a Google recommendation
            google_rec_rn = rec.get("google_rec_resource_name", "")
            if google_rec_rn:
                try:
                    from database import _conn as _db_conn_opt
                    with _db_conn_opt() as _c:
                        _c.execute(
                            "UPDATE gads_audit_log SET google_rec_resource_name=? WHERE action_id=?",
                            (google_rec_rn, aid)
                        )
                except Exception as _grn_err:
                    logger.warning(f"Could not store google_rec_resource_name: {_grn_err}")
            actions_pending += 1
            priority_counter += 1
            logger.info(f"  [{op}] '{entity_name}' → {aid[:8]}")

        actions.setdefault("memory_applied", []).extend(
            [f"[claude:{camp_name}] {rec.get('reason','')}" for rec in structured]
        )

    # ── Account-level pass (cross-campaign patterns) ─────────────────────────
    logger.info("Calling Claude (Opus) for account-level recommendations...")
    # Build campaign_spend dict with resource info for the account-level function
    camp_spend_for_acct = {}
    for cn in all_campaign_names:
        cn_lower = cn.strip().lower()
        kws = [k for k in keyword_perf if k.get("campaign","").strip().lower() == cn_lower]
        camp_spend_for_acct[cn] = {
            "daily_budget_usd": next((k.get("daily_budget_micros", 0) / 1e6 for k in kws if k.get("daily_budget_micros")), None),
        }

    acct_structured = _call_claude_account_level(
        all_keyword_perf=keyword_perf,
        all_search_terms=search_terms,
        call_attribution=call_attribution,
        od_production=od_production,
        summary=summary,
        campaign_spend=camp_spend_for_acct,
        google_recs=[r for r in google_recs if not r.get("campaign_name")],
    )

    logger.info(f"Account-level recommendations: {len(acct_structured)}")
    for rec in acct_structured:
        op = rec.get("operation", "claude_advisory")
        reason = rec.get("reason", rec.get("insight", ""))
        advisories.append(f"[Account Level] {reason}")

        op_meta = _OP_MAP.get(op)
        if op_meta:
            entity_type, id_field, name_field = op_meta
            entity_id   = str(rec.get(id_field, "account"))
            entity_name = str(rec.get(name_field, "Account"))
        else:
            entity_type = "account"
            entity_id   = "account"
            entity_name = "Account"

        aid = log_pending(
            operation=op,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            before_state={},
            after_state={k: v for k, v in rec.items() if k not in ("operation", "campaign_name")},
            optimizer_run_id=run_id,
            reason=reason,
            campaign_name="",   # ← account-level: no campaign
            priority=priority_counter,
            impact_estimate={},
        )
        actions_pending += 1
        priority_counter += 1
        logger.info(f"  [ACCOUNT] [{op}] '{entity_name}' → {aid[:8]}")

        # Store google_rec_resource_name if applicable
        google_rec_rn = rec.get("google_rec_resource_name", "")
        if google_rec_rn:
            try:
                from database import _conn as _db_conn_acct
                with _db_conn_acct() as _c:
                    _c.execute(
                        "UPDATE gads_audit_log SET google_rec_resource_name=? WHERE action_id=?",
                        (google_rec_rn, aid)
                    )
            except Exception as _grn_err:
                logger.warning(f"Could not store google_rec_resource_name (account): {_grn_err}")

    # Report
    report = {
        "run_id": run_id,
        "timestamp": now,
        "mode": "pending_approval",
        "primary_campaign": primary_campaign,
        "summary": summary,
        "actions": {
            "pause": actions["pause"],
            "increase_bid": actions["increase_bid"],
            "decrease_bid": actions["decrease_bid"],
            "new_exact": actions["new_exact"],
            "new_negatives": actions["new_negatives"],
        },
        "memory_applied": actions.get("memory_applied", []),
        "call_summary": {
            v["campaign_name"]: {
                "calls": v["calls"],
                "booked": v["booked_calls"],
                "confirmed_appts": v["confirmed_appts"],
            }
            for v in call_attribution.values()
        },
        "od_production_summary": od_production,
        "advisories": advisories,
    }

    # Update run record with results
    update_optimizer_run(
        run_id,
        summary_json=json.dumps(summary, default=str),
        report_json=json.dumps(report, default=str),
        actions_pending=actions_pending,
        mode="pending_approval",
    )

    logger.info(f"\n{'='*60}")
    logger.info(f"OPTIMIZATION REPORT — run_id={run_id}")
    logger.info(f"{'='*60}")
    logger.info(f"  Total spend (30d):    ${summary['total_spend']}")
    logger.info(f"  Total clicks:         {summary['total_clicks']}")
    logger.info(f"  Total leads:          {summary['total_leads']}")
    logger.info(f"  Total calls:          {summary.get('total_calls', 0)}")
    logger.info(f"  Total booked calls:   {summary.get('total_booked_calls', 0)}")
    logger.info(f"  Total confirmed appts:{summary.get('total_confirmed_appts', 0)}")
    logger.info(f"  Total production:     ${summary['total_production']}")
    logger.info(f"  Overall ROAS:         {summary['overall_roas']}x")
    logger.info(f"  Cost per lead:        ${summary['cost_per_lead']}")
    logger.info(f"  Cost per acquisition: ${summary.get('cost_per_acquisition', 'N/A')}")
    logger.info(f"  Keywords to pause:    {summary['keywords_to_pause']}")
    logger.info(f"  Keywords to bid up:   {summary['keywords_to_bid_up']}")
    logger.info(f"  Keywords to bid down: {summary['keywords_to_bid_down']}")
    logger.info(f"  New exact-match:      {summary['new_exact_match']}")
    logger.info(f"  New negatives:        {summary['new_negatives']}")
    logger.info(f"  Claude advisories:    {len(advisories)}")
    logger.info(f"  Total pending actions: {actions_pending}")

    for kw in actions["pause"]:
        logger.info(f"  PAUSE [{kw.get('action_id','?')[:8]}]: '{kw['keyword']}' — {kw['reason']}")
    for kw in actions["increase_bid"]:
        logger.info(f"  BID UP: '{kw['keyword']}' — {kw['reason']}")
    for kw in actions["decrease_bid"]:
        logger.info(f"  BID DOWN: '{kw['keyword']}' — {kw['reason']}")
    for st in actions["new_exact"]:
        logger.info(f"  NEW EXACT: '{st['search_term']}' — {st.get('clicks',0)} clicks")
    for st in actions["new_negatives"]:
        logger.info(f"  NEW NEGATIVE: '{st['search_term']}' — {st['reason']}")

    logger.info("=" * 60)
    return report


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = optimize_campaign(dry_run=True)
    print(json.dumps(result, indent=2, default=str))
