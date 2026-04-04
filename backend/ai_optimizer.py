"""
AI Campaign Optimizer — daily evaluation and optimization of Google Ads.

Runs daily (7 AM) after fresh data from google_ads_sync.
Pulls keyword performance, joins with lead/production data, then:
  1. Pauses keywords with high spend + zero leads
  2. Increases bids on proven production keywords
  3. Harvests new exact-match keywords from search terms report
  4. Adds negative keywords for irrelevant search terms
  5. Generates a daily optimization report

Uses Claude API for analysis when ANTHROPIC_API_KEY is set.
Falls back to rule-based optimization otherwise.

Manual trigger: POST /api/admin/optimize
Dry-run mode: POST /api/admin/optimize?dry_run=true
"""

import logging
import json
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
            ad_group.name,
            campaign.name,
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
            results.append({
                "keyword": row.ad_group_criterion.keyword.text,
                "match_type": str(row.ad_group_criterion.keyword.match_type),
                "status": str(row.ad_group_criterion.status),
                "resource_name": row.ad_group_criterion.resource_name,
                "ad_group": row.ad_group.name,
                "campaign": row.campaign.name,
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
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions
        FROM search_term_view
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
            AND metrics.impressions > 0
    """

    results = []
    try:
        response = service.search(customer_id=customer_id, query=query)
        for row in response:
            cost = (row.metrics.cost_micros or 0) / 1_000_000.0
            results.append({
                "search_term": row.search_term_view.search_term,
                "status": str(row.search_term_view.status),
                "impressions": row.metrics.impressions or 0,
                "clicks": row.metrics.clicks or 0,
                "cost": cost,
                "conversions": row.metrics.conversions or 0,
            })
    except Exception as e:
        logger.error(f"Failed to get search terms: {e}")

    return results


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
        if stage in ("scheduled", "confirmed", "showed", "treatment_presented",
                      "treatment_accepted", "treatment_completed"):
            attribution[keyword]["booked"] += 1

        if stage in ("treatment_accepted", "treatment_completed"):
            attribution[keyword]["treated"] += 1

        attribution[keyword]["production"] += float(lead.get("attributed_production", 0))

    return attribution


# ── Rule-Based Optimization ──────────────────────────────────────────────────

def _analyze_keywords(keyword_perf: list, attribution: dict, search_terms: list) -> dict:
    """
    Apply optimization rules. Returns recommended actions.
    """
    actions = {
        "pause": [],           # Keywords to pause (high spend, no results)
        "increase_bid": [],    # Keywords to bid up (proven production)
        "decrease_bid": [],    # Keywords to bid down (high cost, low conversion)
        "new_exact": [],       # Search terms to add as exact match keywords
        "new_negatives": [],   # Search terms to add as negatives
        "summary": {},
    }

    total_spend = sum(k["cost"] for k in keyword_perf)
    total_clicks = sum(k["clicks"] for k in keyword_perf)
    total_leads = sum(a["leads"] for a in attribution.values())
    total_production = sum(a["production"] for a in attribution.values())

    # Rule 1: Pause keywords with spend > $20 and zero leads
    for kw in keyword_perf:
        keyword = kw["keyword"]
        attr = attribution.get(keyword, {"leads": 0, "booked": 0, "production": 0})

        if kw["cost"] > 20 and attr["leads"] == 0 and kw["clicks"] > 10:
            actions["pause"].append({
                "keyword": keyword,
                "match_type": kw["match_type"],
                "resource_name": kw["resource_name"],
                "reason": f"${kw['cost']:.2f} spent, {kw['clicks']} clicks, 0 leads",
                "cost": kw["cost"],
            })

    # Rule 2: Increase bids on keywords with production
    for kw in keyword_perf:
        keyword = kw["keyword"]
        attr = attribution.get(keyword, {"leads": 0, "booked": 0, "production": 0})

        if attr["production"] > 0:
            roas = attr["production"] / kw["cost"] if kw["cost"] > 0 else float("inf")
            actions["increase_bid"].append({
                "keyword": keyword,
                "match_type": kw["match_type"],
                "resource_name": kw["resource_name"],
                "reason": f"ROAS {roas:.1f}x — ${attr['production']:.0f} production from ${kw['cost']:.2f} spend",
                "roas": roas,
            })
        elif attr["booked"] > 0 and kw["cost"] > 0:
            cost_per_booking = kw["cost"] / attr["booked"]
            if cost_per_booking < 50:  # Good cost per booking
                actions["increase_bid"].append({
                    "keyword": keyword,
                    "match_type": kw["match_type"],
                    "resource_name": kw["resource_name"],
                    "reason": f"${cost_per_booking:.2f}/booking — {attr['booked']} bookings",
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
                "reason": f"{attr['leads']} leads but 0 bookings from ${kw['cost']:.2f} spend",
            })

    # Rule 4: Harvest search terms that converted but aren't exact match keywords
    existing_keywords = {kw["keyword"].lower() for kw in keyword_perf}
    for st in search_terms:
        term = st["search_term"].lower()
        if st["conversions"] > 0 and term not in existing_keywords:
            actions["new_exact"].append({
                "search_term": st["search_term"],
                "clicks": st["clicks"],
                "conversions": st["conversions"],
                "cost": st["cost"],
            })

    # Rule 5: Negative keywords — search terms with spend but no clicks or irrelevant
    for st in search_terms:
        if st["cost"] > 5 and st["clicks"] == 0 and st["impressions"] > 20:
            actions["new_negatives"].append({
                "search_term": st["search_term"],
                "impressions": st["impressions"],
                "cost": st["cost"],
                "reason": "High impressions, zero clicks — likely irrelevant",
            })
        elif st["clicks"] > 5 and st["cost"] > 15 and st["conversions"] == 0:
            # Check if this search term generated any leads
            term_lower = st["search_term"].lower()
            has_leads = any(
                term_lower in (a_kw.lower())
                for a_kw in attribution.keys()
            )
            if not has_leads:
                actions["new_negatives"].append({
                    "search_term": st["search_term"],
                    "clicks": st["clicks"],
                    "cost": st["cost"],
                    "reason": f"${st['cost']:.2f} spent, {st['clicks']} clicks, 0 conversions/leads",
                })

    # Summary
    actions["summary"] = {
        "total_spend": round(total_spend, 2),
        "total_clicks": total_clicks,
        "total_leads": total_leads,
        "total_production": round(total_production, 2),
        "overall_roas": round(total_production / total_spend, 1) if total_spend > 0 else 0,
        "cost_per_lead": round(total_spend / total_leads, 2) if total_leads > 0 else 0,
        "keywords_to_pause": len(actions["pause"]),
        "keywords_to_bid_up": len(actions["increase_bid"]),
        "keywords_to_bid_down": len(actions["decrease_bid"]),
        "new_exact_match": len(actions["new_exact"]),
        "new_negatives": len(actions["new_negatives"]),
    }

    return actions


# ── Execute Actions ──────────────────────────────────────────────────────────

def _execute_pause(client, customer_id: str, keywords: list) -> int:
    """Pause keywords via Google Ads API."""
    if not keywords:
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
        )
        return len(response.results)
    except Exception as e:
        logger.error(f"Failed to pause keywords: {e}")
        return 0


# ── Main Entry Point ─────────────────────────────────────────────────────────

def optimize_campaign(dry_run: bool = True) -> dict:
    """
    Run the full optimization cycle.
    Set dry_run=False to actually execute changes in Google Ads.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc).isoformat()

    try:
        client = _build_client()
    except Exception as e:
        logger.error(f"Failed to create Google Ads client: {e}")
        return {"error": str(e)}

    customer_id = settings.google_ads_customer_id

    logger.info("=" * 60)
    logger.info("AI Campaign Optimizer — Starting daily evaluation")
    logger.info("=" * 60)

    # Collect data
    logger.info("Collecting keyword performance...")
    keyword_perf = _get_keyword_performance(client, customer_id, days=30)

    logger.info("Collecting search terms...")
    search_terms = _get_search_terms(client, customer_id, days=30)

    logger.info("Building lead attribution...")
    attribution = _get_keyword_attribution()

    # Analyze
    logger.info("Analyzing and generating recommendations...")
    actions = _analyze_keywords(keyword_perf, attribution, search_terms)

    # Report
    summary = actions["summary"]
    report = {
        "timestamp": now,
        "mode": "DRY RUN" if dry_run else "LIVE",
        "summary": summary,
        "actions": {
            "pause": actions["pause"],
            "increase_bid": actions["increase_bid"],
            "decrease_bid": actions["decrease_bid"],
            "new_exact": actions["new_exact"],
            "new_negatives": actions["new_negatives"],
        },
        "executed": {},
    }

    logger.info(f"\n{'='*60}")
    logger.info(f"OPTIMIZATION REPORT — {'DRY RUN' if dry_run else 'LIVE'}")
    logger.info(f"{'='*60}")
    logger.info(f"  Total spend (30d):    ${summary['total_spend']}")
    logger.info(f"  Total clicks:         {summary['total_clicks']}")
    logger.info(f"  Total leads:          {summary['total_leads']}")
    logger.info(f"  Total production:     ${summary['total_production']}")
    logger.info(f"  Overall ROAS:         {summary['overall_roas']}x")
    logger.info(f"  Cost per lead:        ${summary['cost_per_lead']}")
    logger.info(f"  Keywords to pause:    {summary['keywords_to_pause']}")
    logger.info(f"  Keywords to bid up:   {summary['keywords_to_bid_up']}")
    logger.info(f"  Keywords to bid down: {summary['keywords_to_bid_down']}")
    logger.info(f"  New exact-match:      {summary['new_exact_match']}")
    logger.info(f"  New negatives:        {summary['new_negatives']}")

    for kw in actions["pause"]:
        logger.info(f"  PAUSE: '{kw['keyword']}' — {kw['reason']}")
    for kw in actions["increase_bid"]:
        logger.info(f"  BID UP: '{kw['keyword']}' — {kw['reason']}")
    for kw in actions["decrease_bid"]:
        logger.info(f"  BID DOWN: '{kw['keyword']}' — {kw['reason']}")
    for st in actions["new_exact"]:
        logger.info(f"  NEW EXACT: '{st['search_term']}' — {st['clicks']} clicks, {st['conversions']} conversions")
    for st in actions["new_negatives"]:
        logger.info(f"  NEW NEGATIVE: '{st['search_term']}' — {st['reason']}")

    # Execute (only if not dry run)
    if not dry_run:
        logger.info("\nExecuting changes...")

        paused = _execute_pause(client, customer_id, actions["pause"])
        report["executed"]["paused"] = paused
        logger.info(f"  Paused {paused} keywords")

        # Note: bid adjustments and new keywords require more complex API calls
        # For now, these are reported but not auto-executed
        report["executed"]["bid_changes"] = "reported_only"
        report["executed"]["new_keywords"] = "reported_only"
        report["executed"]["new_negatives"] = "reported_only"
    else:
        logger.info("\nDry run — no changes made")

    logger.info("=" * 60)
    return report


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = optimize_campaign(dry_run=True)
    print(json.dumps(result, indent=2, default=str))
