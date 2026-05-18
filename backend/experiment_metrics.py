"""
Google Ads experiment metrics fetcher.

Fetches campaign-level performance metrics for both arms of an A/B experiment.
The experiment is set up manually in Google Ads UI; this module just reads data.

Usage:
    from experiment_metrics import get_gads_experiment_metrics
    metrics = get_gads_experiment_metrics(
        base_campaign_resource="customers/123/campaigns/456",
        trial_campaign_resource="customers/123/campaigns/789",
        start_date="2026-05-01",
        end_date="2026-05-31",
    )
"""
from __future__ import annotations
import logging
from datetime import date, timedelta

logger = logging.getLogger(__name__)

# Minimum thresholds for winner signal (from experiment_policy)
MIN_CLICKS_PER_ARM    = 200
MIN_CONVERSIONS_PER_ARM = 20
MIN_DAYS_RUNNING      = 14
MIN_RELATIVE_LIFT     = 0.30   # 30% relative improvement to flag a winner


def get_gads_experiment_metrics(
    base_campaign_resource: str,
    trial_campaign_resource: str,
    start_date: str = "",
    end_date: str = "",
    customer_id: str = "",
) -> dict:
    """
    Fetch campaign metrics for both arms from the Google Ads API.

    Returns:
    {
        "base":  { "clicks", "impressions", "ctr", "conversions", "cost_usd", "cpa" },
        "trial": { same },
        "date_range": { "start", "end" },
        "error": str or None,
    }
    """
    try:
        from google.ads.googleads.client import GoogleAdsClient
        from config import get_settings
        settings = get_settings()

        if not customer_id:
            customer_id = "".join(
                ch for ch in (settings.google_ads_customer_id or "") if ch.isdigit()
            )
        if not customer_id:
            return _empty_metrics("google_ads_customer_id not configured")

        client = GoogleAdsClient.load_from_dict({
            "developer_token":  settings.google_ads_developer_token,
            "client_id":        settings.google_ads_client_id,
            "client_secret":    settings.google_ads_client_secret,
            "refresh_token":    settings.google_ads_refresh_token,
            "login_customer_id": settings.google_ads_login_customer_id or customer_id,
            "use_proto_plus":   True,
        })

        # Default date range: last 30 days
        if not end_date:
            end_date = date.today().isoformat()
        if not start_date:
            start_date = (date.today() - timedelta(days=30)).isoformat()

        ga_service = client.get_service("GoogleAdsService")

        def _fetch_campaign_metrics(campaign_resource: str) -> dict:
            if not campaign_resource:
                return _empty_arm()
            query = f"""
                SELECT
                    campaign.resource_name,
                    metrics.clicks,
                    metrics.impressions,
                    metrics.ctr,
                    metrics.conversions,
                    metrics.cost_micros
                FROM campaign
                WHERE campaign.resource_name = '{campaign_resource}'
                  AND segments.date BETWEEN '{start_date}' AND '{end_date}'
            """
            try:
                rows = list(ga_service.search(customer_id=customer_id, query=query))
            except Exception as e:
                logger.warning(f"experiment_metrics: query failed for {campaign_resource}: {e}")
                return _empty_arm(str(e))

            # Aggregate across all date segments
            clicks = impressions = conversions = cost_micros = 0
            ctr_sum = 0.0
            n = 0
            for row in rows:
                clicks       += row.metrics.clicks or 0
                impressions  += row.metrics.impressions or 0
                conversions  += row.metrics.conversions or 0
                cost_micros  += row.metrics.cost_micros or 0
                ctr_sum      += float(row.metrics.ctr or 0)
                n            += 1

            cost_usd = cost_micros / 1_000_000
            cpa      = round(cost_usd / conversions, 2) if conversions > 0 else None
            ctr      = round(clicks / impressions, 4) if impressions > 0 else 0.0

            return {
                "clicks":      clicks,
                "impressions": impressions,
                "ctr":         ctr,
                "conversions": round(conversions, 2),
                "cost_usd":    round(cost_usd, 2),
                "cpa":         cpa,
                "error":       None,
            }

        base_metrics  = _fetch_campaign_metrics(base_campaign_resource)
        trial_metrics = _fetch_campaign_metrics(trial_campaign_resource)

        return {
            "base":       base_metrics,
            "trial":      trial_metrics,
            "date_range": {"start": start_date, "end": end_date},
            "error":      None,
        }

    except Exception as e:
        logger.error(f"experiment_metrics: top-level error: {e}")
        return _empty_metrics(str(e))


def compute_winner_signal(
    gads_metrics: dict,
    lead_metrics: dict,
    days_running: int | None = 0,
) -> dict:
    """
    Compute a winner signal from combined GAds + local lead metrics.

    Returns:
    {
        "ready":         bool,   # enough data to make a call
        "winner":        "base" | "trial" | "inconclusive" | "insufficient_data",
        "confidence":    "high" | "medium" | "low",
        "primary_metric": str,   # which metric drove the decision
        "summary":       str,    # human-readable explanation
        "base_stats":    dict,
        "trial_stats":   dict,
    }
    """
    base_g  = gads_metrics.get("base", {})
    trial_g = gads_metrics.get("trial", {})
    base_l  = lead_metrics.get("base", {})
    trial_l = lead_metrics.get("trial", {})

    base_clicks  = int(base_g.get("clicks", 0))
    trial_clicks = int(trial_g.get("clicks", 0))
    base_conv    = float(base_g.get("conversions", 0))
    trial_conv   = float(trial_g.get("conversions", 0))
    base_leads   = int(base_l.get("total_leads", 0))
    trial_leads  = int(trial_l.get("total_leads", 0))
    base_rev     = float(base_l.get("revenue", 0))
    trial_rev    = float(trial_l.get("revenue", 0))

    base_stats = {
        "clicks": base_clicks, "conversions": base_conv,
        "leads": base_leads, "revenue": base_rev,
        "booked": base_l.get("booked", 0),
        "ctr": base_g.get("ctr", 0),
        "cost_usd": base_g.get("cost_usd", 0),
    }
    trial_stats = {
        "clicks": trial_clicks, "conversions": trial_conv,
        "leads": trial_leads, "revenue": trial_rev,
        "booked": trial_l.get("booked", 0),
        "ctr": trial_g.get("ctr", 0),
        "cost_usd": trial_g.get("cost_usd", 0),
    }

    # Check minimum data thresholds (days_running may be None when called at promotion time)
    _days = days_running if days_running is not None else 0
    if _days < MIN_DAYS_RUNNING:
        return _insufficient("Minimum 14 days required", base_stats, trial_stats)
    if base_clicks < MIN_CLICKS_PER_ARM or trial_clicks < MIN_CLICKS_PER_ARM:
        needed = MIN_CLICKS_PER_ARM - min(base_clicks, trial_clicks)
        return _insufficient(
            f"Need {MIN_CLICKS_PER_ARM} clicks per arm (need ~{needed} more on the smaller arm)",
            base_stats, trial_stats
        )

    # Primary metric: booked appointments per click (best proxy for revenue at dental scale)
    # Fall back to GAds conversions if no local leads matched
    if base_leads >= 5 and trial_leads >= 5:
        base_rate  = base_l.get("booked", 0) / base_clicks  if base_clicks > 0 else 0
        trial_rate = trial_l.get("booked", 0) / trial_clicks if trial_clicks > 0 else 0
        primary_metric = "booked_per_click"
    elif base_conv >= 5 and trial_conv >= 5:
        base_rate  = base_conv  / base_clicks  if base_clicks > 0 else 0
        trial_rate = trial_conv / trial_clicks if trial_clicks > 0 else 0
        primary_metric = "gads_conv_per_click"
    else:
        # Not enough conversions — use CTR as a weak signal
        base_rate  = float(base_g.get("ctr", 0))
        trial_rate = float(trial_g.get("ctr", 0))
        primary_metric = "ctr_only"

    if base_rate == 0 and trial_rate == 0:
        return _insufficient("Both arms have zero conversion rate", base_stats, trial_stats)

    # Compute relative lift
    better_arm  = "trial" if trial_rate >= base_rate else "base"
    worse_rate  = min(base_rate, trial_rate)
    better_rate = max(base_rate, trial_rate)
    relative_lift = ((better_rate - worse_rate) / worse_rate) if worse_rate > 0 else 0.0

    if relative_lift < MIN_RELATIVE_LIFT:
        summary = (
            f"{better_arm.title()} arm is {relative_lift:.0%} better on {primary_metric} "
            f"— below {MIN_RELATIVE_LIFT:.0%} threshold. No clear winner yet."
        )
        return {
            "ready": True, "winner": "inconclusive",
            "confidence": "low", "primary_metric": primary_metric,
            "summary": summary, "base_stats": base_stats, "trial_stats": trial_stats,
        }

    # Revenue sanity check: if revenue data available, don't promote if revenue contradicts
    confidence = "high"
    revenue_caveat = ""
    if base_rev > 50 and trial_rev > 50:
        rev_winner = "trial" if trial_rev >= base_rev else "base"
        if rev_winner != better_arm:
            confidence = "medium"
            revenue_caveat = (
                f" Note: revenue data favors {rev_winner} (${trial_rev:.0f} vs ${base_rev:.0f}) "
                f"— review before promoting."
            )

    summary = (
        f"{better_arm.title()} arm wins on {primary_metric}: "
        f"{better_rate:.2%} vs {worse_rate:.2%} ({relative_lift:.0%} lift). "
        f"Base: {base_clicks} clicks / {base_l.get('booked',0)} booked. "
        f"Trial: {trial_clicks} clicks / {trial_l.get('booked',0)} booked."
        + revenue_caveat
    )

    return {
        "ready": True, "winner": better_arm,
        "confidence": confidence, "primary_metric": primary_metric,
        "summary": summary, "base_stats": base_stats, "trial_stats": trial_stats,
    }


def _empty_arm(error: str = "") -> dict:
    return {"clicks": 0, "impressions": 0, "ctr": 0.0,
            "conversions": 0.0, "cost_usd": 0.0, "cpa": None, "error": error}

def _empty_metrics(error: str) -> dict:
    return {"base": _empty_arm(), "trial": _empty_arm(),
            "date_range": {}, "error": error}

def _insufficient(reason: str, base_stats: dict, trial_stats: dict) -> dict:
    return {
        "ready": False, "winner": "insufficient_data",
        "confidence": "low", "primary_metric": "",
        "summary": reason, "base_stats": base_stats, "trial_stats": trial_stats,
    }
