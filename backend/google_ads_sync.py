"""
Google Ads Sync — resolves gclid to keyword/ad/cost data.
Pulls click data from Google Ads API and enriches leads in SQLite.

Run as a scheduled job (daily, 6 AM).
Or trigger manually via: POST /api/admin/gads-sync

Two-pass approach:
  1. click_view → maps gclid to keyword, ad group, campaign
  2. keyword_view → gets cost data per keyword (used as fallback for CPC)
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from google.ads.googleads.client import GoogleAdsClient
from config import get_settings
from database import get_all_leads, upsert_lead, save_gads_keywords_cache

logger = logging.getLogger(__name__)


def _build_client():
    """Create authenticated Google Ads API client."""
    settings = get_settings()
    return GoogleAdsClient.load_from_dict({
        "developer_token": settings.google_ads_developer_token,
        "client_id": settings.google_ads_client_id,
        "client_secret": settings.google_ads_client_secret,
        "refresh_token": settings.google_ads_refresh_token,
        "login_customer_id": settings.google_ads_login_customer_id,
        "use_proto_plus": True,
    })


def _fetch_click_data(client, customer_id: str, days_back: int = 90) -> dict:
    """
    Query click_view for the last N days.
    Returns dict: {gclid: {keyword_text, ad_group_name, campaign_name, ...}}

    Note: click_view requires a single-day filter — we query day by day.
    Daily scheduled runs should use days_back=7; first run uses 90.
    """
    service = client.get_service("GoogleAdsService")
    gclid_map = {}

    # click_view REQUIRES a single-day filter — query day by day
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=days_back)

    total_days = (end_date - start_date).days + 1
    logger.info(f"Querying click_view day by day from {start_date} to {end_date} ({total_days} days)...")

    days_with_data = 0
    days_queried = 0

    current_date = start_date
    while current_date <= end_date:
        date_str = current_date.strftime("%Y-%m-%d")

        query = f"""
            SELECT
                click_view.gclid,
                click_view.keyword_info.text,
                click_view.keyword_info.match_type,
                ad_group.name,
                ad_group.id,
                campaign.name,
                campaign.id,
                segments.date
            FROM click_view
            WHERE segments.date = '{date_str}'
        """

        try:
            response = service.search(customer_id=customer_id, query=query)
            day_count = 0

            for row in response:
                gclid = row.click_view.gclid
                if not gclid:
                    continue

                gclid_map[gclid] = {
                    "keyword_text": row.click_view.keyword_info.text or "",
                    "match_type": str(row.click_view.keyword_info.match_type) if row.click_view.keyword_info.match_type else "",
                    "ad_group_name": row.ad_group.name or "",
                    "ad_group_id": str(row.ad_group.id) if row.ad_group.id else "",
                    "campaign_name": row.campaign.name or "",
                    "campaign_id": str(row.campaign.id) if row.campaign.id else "",
                    "click_date": row.segments.date or date_str,
                }
                day_count += 1

            if day_count > 0:
                days_with_data += 1
                logger.debug(f"  {date_str}: {day_count} clicks")

        except Exception as e:
            # Skip days with errors (e.g. no data)
            error_str = str(e)
            if "EXPECTED_FILTER" not in error_str:
                logger.warning(f"  {date_str}: error — {error_str[:100]}")

        days_queried += 1
        current_date += timedelta(days=1)

    logger.info(f"click_view complete: {len(gclid_map)} gclids from {days_with_data}/{days_queried} days")

    return gclid_map


def _fetch_keyword_costs(client, customer_id: str) -> dict:
    """
    Query keyword_view for average CPC by keyword text.
    Returns dict: {keyword_text: avg_cpc_dollars}
    """
    perf = _fetch_all_keyword_perf(client, customer_id, days=30)
    return {kw["keyword_text"]: kw["avg_cpc"] for kw in perf}


def _fetch_all_keyword_perf(client, customer_id: str, days: int = 30) -> list:
    """
    Query keyword_view for full performance data — impressions, clicks, cost, conversions.
    Returns list of dicts. Also saves to gads_keywords_cache for the reports table.
    """
    service = client.get_service("GoogleAdsService")
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=days)

    query = f"""
        SELECT
            ad_group_criterion.keyword.text,
            ad_group_criterion.keyword.match_type,
            ad_group_criterion.status,
            ad_group.name,
            campaign.name,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.average_cpc
        FROM keyword_view
        WHERE segments.date BETWEEN '{start_date.strftime("%Y-%m-%d")}' AND '{end_date.strftime("%Y-%m-%d")}'
            AND campaign.status = 'ENABLED'
            AND ad_group.status = 'ENABLED'
            AND ad_group_criterion.status != 'REMOVED'
    """

    logger.info(f"Querying keyword_view for full performance (last {days}d)...")

    results = []
    # Aggregate across days — same keyword may appear multiple times
    agg = {}

    try:
        response = service.search(customer_id=customer_id, query=query)
        for row in response:
            keyword = row.ad_group_criterion.keyword.text
            if not keyword:
                continue
            cost = (row.metrics.cost_micros or 0) / 1_000_000.0
            clicks = row.metrics.clicks or 0
            impressions = row.metrics.impressions or 0
            conversions = row.metrics.conversions or 0.0

            if keyword not in agg:
                agg[keyword] = {
                    "keyword_text": keyword,
                    "match_type": str(row.ad_group_criterion.keyword.match_type),
                    "ad_group_name": row.ad_group.name or "",
                    "campaign_name": row.campaign.name or "",
                    "impressions": 0,
                    "clicks": 0,
                    "cost": 0.0,
                    "conversions": 0.0,
                }
            agg[keyword]["impressions"] += impressions
            agg[keyword]["clicks"] += clicks
            agg[keyword]["cost"] += cost
            agg[keyword]["conversions"] += conversions

        for kw_data in agg.values():
            clicks = kw_data["clicks"] or 1
            kw_data["avg_cpc"] = round(kw_data["cost"] / clicks, 4) if kw_data["cost"] > 0 else 0.0
            results.append(kw_data)

        logger.info(f"Found {len(results)} unique keywords in keyword_view")

        # Save to cache so reports can show all keywords even with zero leads
        if results:
            save_gads_keywords_cache(results, days=days)
            logger.info(f"Saved {len(results)} keywords to gads_keywords_cache")

    except Exception as e:
        logger.error(f"keyword_view full perf query failed: {e}")

    return results


def sync_gclids_to_keywords(days_back: int = 7) -> dict:
    """
    Main sync function. Resolves gclids on leads to keyword/ad data.
    Use days_back=90 for first run, days_back=7 for daily scheduled runs.
    Returns: {"synced": N, "skipped": N, "no_gclid": N, "errors": N}
    """
    settings = get_settings()

    try:
        client = _build_client()
    except Exception as e:
        logger.error(f"Failed to create Google Ads client: {e}")
        return {"synced": 0, "skipped": 0, "no_gclid": 0, "errors": 1, "error": str(e)}

    customer_id = settings.google_ads_customer_id

    # Pass 1: Get gclid → keyword/ad mapping
    gclid_map = _fetch_click_data(client, customer_id, days_back=days_back)

    # Pass 2: Get full keyword performance + cache it for reports
    keyword_perf_list = _fetch_all_keyword_perf(client, customer_id, days=30)
    keyword_costs = {kw["keyword_text"]: kw["avg_cpc"] for kw in keyword_perf_list}

    # Match against leads
    leads = get_all_leads(limit=1000)
    synced = 0
    skipped = 0
    no_gclid = 0
    errors = 0

    now = datetime.now(timezone.utc).isoformat()

    for lead in leads:
        try:
            gclid = (lead.get("gclid") or "").strip()

            if not gclid:
                no_gclid += 1
                continue

            # Already synced and no new data? Skip.
            if lead.get("gads_synced_at") and gclid not in gclid_map:
                skipped += 1
                continue

            if gclid not in gclid_map:
                skipped += 1
                continue

            click_data = gclid_map[gclid]
            keyword = click_data["keyword_text"]
            click_cost = keyword_costs.get(keyword, 0.0)

            update_data = {
                "id": lead["id"],
                "keyword_text": keyword,
                "search_term": keyword,  # click_view doesn't have search_term; keyword is closest
                "ad_group_name": click_data["ad_group_name"],
                "ad_id": click_data.get("ad_group_id", ""),
                "campaign_name": click_data.get("campaign_name", ""),
                "campaign_id": click_data.get("campaign_id", ""),
                "click_cost": click_cost,
                "gads_synced_at": now,
            }

            upsert_lead(update_data)
            synced += 1

            logger.info(
                f"Lead {lead['id']}: gclid → keyword='{keyword}', "
                f"ad_group='{click_data['ad_group_name']}', "
                f"campaign='{click_data['campaign_name']}', "
                f"CPC=${click_cost:.2f}"
            )

        except Exception as e:
            logger.error(f"Error syncing lead {lead.get('id', '?')}: {e}")
            errors += 1

    result = {
        "synced": synced,
        "skipped": skipped,
        "no_gclid": no_gclid,
        "errors": errors,
        "gclids_in_google": len(gclid_map),
        "keywords_with_cost": len(keyword_costs),
    }
    logger.info(f"Google Ads sync complete: {result}")
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = sync_gclids_to_keywords()
    print(result)
