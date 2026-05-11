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
from database import (
    get_all_leads, upsert_lead, save_gads_keywords_cache,
    save_gads_geo_cache, save_gads_schedule_cache,
    save_gads_daily_stats, save_gads_ads, save_gads_ad_metrics,
    upsert_gads_call_view, upsert_gads_clicks,
)

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
    clicks_to_persist = []  # batch for gads_clicks upsert

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
                ad_group_ad.ad.id,
                ad_group_ad.ad.name,
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

                click_data = {
                    "gclid": gclid,
                    "keyword_text": row.click_view.keyword_info.text or "",
                    "match_type": str(row.click_view.keyword_info.match_type) if row.click_view.keyword_info.match_type else "",
                    "ad_group_name": row.ad_group.name or "",
                    "ad_group_id": str(row.ad_group.id) if row.ad_group.id else "",
                    "ad_id": str(row.ad_group_ad.ad.id) if row.ad_group_ad.ad.id else "",
                    "ad_name": row.ad_group_ad.ad.name or "",
                    "campaign_name": row.campaign.name or "",
                    "campaign_id": str(row.campaign.id) if row.campaign.id else "",
                    "click_date": row.segments.date or date_str,
                }
                gclid_map[gclid] = click_data
                clicks_to_persist.append(click_data)
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

    # Persist all clicks to gads_clicks for time-window call attribution
    if clicks_to_persist:
        try:
            persisted = upsert_gads_clicks(clicks_to_persist)
            logger.info(f"Persisted {persisted} click rows to gads_clicks")
        except Exception as e:
            logger.warning(f"Failed to persist clicks to gads_clicks: {e}")

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

    # Note: impression share metrics (search_impression_share, search_budget_lost_
    # impression_share, search_rank_lost_impression_share) are NOT compatible with
    # keyword_view when using date segmentation. Fetch them from campaign resource
    # separately if needed.
    query = f"""
        SELECT
            ad_group_criterion.keyword.text,
            ad_group_criterion.keyword.match_type,
            ad_group_criterion.status,
            ad_group_criterion.quality_info.quality_score,
            ad_group_criterion.quality_info.creative_quality_score,
            ad_group_criterion.quality_info.post_click_quality_score,
            ad_group_criterion.quality_info.search_predicted_ctr,
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
                # Quality info fields — not segmentable, take first non-zero value seen
                qi = row.ad_group_criterion.quality_info
                qs_map = {0: "", 1: "UNKNOWN", 2: "BELOW_AVERAGE", 3: "AVERAGE", 4: "ABOVE_AVERAGE"}
                agg[keyword] = {
                    "keyword_text": keyword,
                    "match_type": str(row.ad_group_criterion.keyword.match_type),
                    "ad_group_name": row.ad_group.name or "",
                    "campaign_name": row.campaign.name or "",
                    "impressions": 0,
                    "clicks": 0,
                    "cost": 0.0,
                    "conversions": 0.0,
                    # Quality score fields (set once from first row — not day-segmentable)
                    "quality_score": qi.quality_score or 0,
                    "creative_quality_score": qs_map.get(int(qi.creative_quality_score) if qi.creative_quality_score else 0, ""),
                    "post_click_quality": qs_map.get(int(qi.post_click_quality_score) if qi.post_click_quality_score else 0, ""),
                    "search_predicted_ctr": qs_map.get(int(qi.search_predicted_ctr) if qi.search_predicted_ctr else 0, ""),
                    "impression_share": 0.0,
                    "budget_lost_is": 0.0,
                    "rank_lost_is": 0.0,
                }
            agg[keyword]["impressions"] += impressions
            agg[keyword]["clicks"] += clicks
            agg[keyword]["cost"] += cost
            agg[keyword]["conversions"] += conversions
            # Impression share metrics are fetched separately (not available in keyword_view)

        for kw_data in agg.values():
            clicks = kw_data["clicks"] or 1
            kw_data["avg_cpc"] = round(kw_data["cost"] / clicks, 4) if kw_data["cost"] > 0 else 0.0
            # Impression share left at 0 — fetched separately if needed
            results.append(kw_data)

        logger.info(f"Found {len(results)} unique keywords in keyword_view")

        # Save to cache so reports can show all keywords even with zero leads
        if results:
            save_gads_keywords_cache(results, days=days)
            logger.info(f"Saved {len(results)} keywords to gads_keywords_cache")

    except Exception as e:
        logger.error(f"keyword_view full perf query failed: {e}")

    return results


def _fetch_campaign_daily_stats(client, customer_id: str, days: int = 30) -> int:
    """
    Query ad_group resource segmented by date for impressions/clicks/cost/conversions.
    Uses search_stream (handles >10k rows) with a BETWEEN date range.

    Incremental strategy: always re-fetch the last 3 days (for late-arriving data),
    skip dates already synced that are older than 3 days.

    Upserts into gads_daily_stats via save_gads_daily_stats().
    Returns count of rows upserted.

    Note: no campaign/ad_group status filter — we want historical data even if
    something was paused mid-window (silently dropping paused items would cause
    metrics to vanish retroactively).
    """
    service = client.get_service("GoogleAdsService")
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=days)

    query = f"""
        SELECT
            campaign.id,
            campaign.name,
            ad_group.id,
            ad_group.name,
            segments.date,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions
        FROM ad_group
        WHERE segments.date BETWEEN '{start_date.strftime("%Y-%m-%d")}' AND '{end_date.strftime("%Y-%m-%d")}'
    """

    logger.info(f"Querying daily ad-group stats ({start_date} to {end_date})...")

    rows = []
    # No inner try/except — let outer caller (Pass 6 in sync_gclids_to_keywords)
    # handle errors so auth/quota failures are not silently swallowed as "0 rows".
    stream = service.search_stream(customer_id=customer_id, query=query)
    for batch in stream:
        for row in batch.results:
            campaign_id = str(row.campaign.id) if row.campaign.id else ""
            ad_group_id = str(row.ad_group.id) if row.ad_group.id else ""
            if not campaign_id or not ad_group_id:
                continue
            rows.append({
                "date": row.segments.date,
                "campaign_id": campaign_id,
                "campaign_name": row.campaign.name or "",
                "ad_group_id": ad_group_id,
                "ad_group_name": row.ad_group.name or "",
                "impressions": int(row.metrics.impressions or 0),
                "clicks": int(row.metrics.clicks or 0),
                "cost_micros": int(row.metrics.cost_micros or 0),
                "conversions": float(row.metrics.conversions or 0.0),
            })

    logger.info(f"Daily ad-group stats: {len(rows)} rows fetched from API")
    if rows:
        count = save_gads_daily_stats(rows)
        logger.info(f"Upserted {count} rows into gads_daily_stats")
        return count
    return 0


def _fetch_ad_creatives(client, customer_id: str) -> list:
    """
    Fetch ad creative metadata from ad_group_ad resource.
    Includes all statuses (ENABLED, PAUSED, REMOVED) so historical leads
    joining on ad_id don't orphan when an ad is removed.
    Returns list of dicts ready for save_gads_ads().
    """
    import json as _json
    service = client.get_service("GoogleAdsService")

    query = """
        SELECT
            ad_group_ad.resource_name,
            ad_group_ad.ad.id,
            ad_group_ad.ad.name,
            ad_group_ad.ad.type,
            ad_group_ad.status,
            ad_group_ad.ad.expanded_text_ad.headline_part1,
            ad_group_ad.ad.expanded_text_ad.headline_part2,
            ad_group_ad.ad.expanded_text_ad.headline_part3,
            ad_group_ad.ad.expanded_text_ad.description,
            ad_group_ad.ad.expanded_text_ad.description2,
            ad_group_ad.ad.responsive_search_ad.headlines,
            ad_group_ad.ad.responsive_search_ad.descriptions,
            ad_group_ad.ad.final_urls,
            ad_group.resource_name,
            ad_group.id,
            ad_group.name,
            campaign.id,
            campaign.name
        FROM ad_group_ad
        WHERE campaign.advertising_channel_type = 'SEARCH'
    """

    ads = []
    try:
        response = service.search(customer_id=customer_id, query=query)
        for row in response:
            ad = row.ad_group_ad.ad
            ad_id = str(ad.id) if ad.id else ""
            if not ad_id:
                continue

            # Determine ad type string safely (use .name for proto enums, fallback to split)
            try:
                ad_type = ad.type_.name if hasattr(ad.type_, "name") else str(ad.type_).split(".")[-1]
            except Exception:
                ad_type = ""

            # ETA fields
            eta = ad.expanded_text_ad
            # RSA assets — store all as JSON, use first 3/2 for display columns
            rsa = ad.responsive_search_ad
            rsa_headlines = [a.text for a in rsa.headlines if a.text]
            rsa_descs     = [a.text for a in rsa.descriptions if a.text]

            if ad_type == "RESPONSIVE_SEARCH_AD":
                h1 = rsa_headlines[0] if len(rsa_headlines) > 0 else ""
                h2 = rsa_headlines[1] if len(rsa_headlines) > 1 else ""
                h3 = rsa_headlines[2] if len(rsa_headlines) > 2 else ""
                d1 = rsa_descs[0] if len(rsa_descs) > 0 else ""
                d2 = rsa_descs[1] if len(rsa_descs) > 1 else ""
                assets = {
                    "headlines":     [{"text": a.text, "pinned": str(a.pinned_field)} for a in rsa.headlines if a.text],
                    "descriptions":  [{"text": a.text, "pinned": str(a.pinned_field)} for a in rsa.descriptions if a.text],
                }
            else:
                # ETA — store same dict shape as RSA for consistent frontend handling
                h1 = eta.headline_part1 or ""
                h2 = eta.headline_part2 or ""
                h3 = eta.headline_part3 or ""
                d1 = eta.description  or ""
                d2 = eta.description2 or ""
                assets = {
                    "headlines":    [{"text": t, "pinned": ""} for t in [h1, h2, h3] if t],
                    "descriptions": [{"text": t, "pinned": ""} for t in [d1, d2] if t],
                }

            # Safe access to repeated final_urls field
            final_url = next(iter(ad.final_urls), "") if ad.final_urls else ""

            # Status string (use .name for proto enums, fallback to split)
            try:
                st = row.ad_group_ad.status
                status = st.name if hasattr(st, "name") else str(st).split(".")[-1]
            except Exception:
                status = ""

            ads.append({
                "ad_id":                ad_id,
                "customer_id":          customer_id,
                "ad_name":              ad.name or "",
                "ad_group_ad_resource": row.ad_group_ad.resource_name or "",
                "ad_group_resource":    row.ad_group.resource_name or "",
                "ad_group_id":          str(row.ad_group.id) if row.ad_group.id else "",
                "ad_group_name":        row.ad_group.name or "",
                "campaign_id":          str(row.campaign.id) if row.campaign.id else "",
                "campaign_name":        row.campaign.name or "",
                "status":               status,
                "ad_type":              ad_type,
                "headline_1":           h1,
                "headline_2":           h2,
                "headline_3":           h3,
                "description_1":        d1,
                "description_2":        d2,
                "final_url":            final_url,
                "assets_json":          assets,
            })

        logger.info(f"Ad creatives: {len(ads)} ads fetched")
    except Exception as e:
        logger.error(f"_fetch_ad_creatives failed: {e}")
        raise

    return ads


def _fetch_ad_daily_metrics(client, customer_id: str, days: int = 30) -> list:
    """
    Fetch daily metrics per ad creative.
    No campaign/ad_group status filter — matches Pass 6 convention so paused-mid-
    window ads don't silently vanish from historical data.
    Uses search_stream for large result sets.
    Returns list of dicts ready for save_gads_ad_metrics().
    """
    service = client.get_service("GoogleAdsService")
    end_date   = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=days)

    query = f"""
        SELECT
            ad_group_ad.ad.id,
            segments.date,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions
        FROM ad_group_ad
        WHERE segments.date BETWEEN '{start_date.strftime("%Y-%m-%d")}' AND '{end_date.strftime("%Y-%m-%d")}'
            AND campaign.advertising_channel_type = 'SEARCH'
    """

    rows = []
    stream = service.search_stream(customer_id=customer_id, query=query)
    for batch in stream:
        for row in batch.results:
            ad_id = str(row.ad_group_ad.ad.id) if row.ad_group_ad.ad.id else ""
            if not ad_id:
                continue
            rows.append({
                "ad_id":       ad_id,
                "date":        row.segments.date,
                "impressions": int(row.metrics.impressions or 0),
                "clicks":      int(row.metrics.clicks or 0),
                "cost_micros": int(row.metrics.cost_micros or 0),
                "conversions": float(row.metrics.conversions or 0.0),
            })

    logger.info(f"Ad daily metrics: {len(rows)} rows fetched")
    return rows


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

            # Map Google Ads match_type → our search_term_type.
            # "AI_MAX_KEYWORDLESS" and "AI_MAX_BROAD_MATCH" are returned by Google
            # when the click came from an AI Max expanded query. Standard values are
            # EXACT, PHRASE, BROAD (proto-plus returns these as enum names).
            raw_match = (click_data.get("match_type") or "").upper()
            if "AI_MAX" in raw_match or raw_match == "KEYWORDLESS":
                search_term_type = "ai_max"
            elif raw_match == "EXACT":
                search_term_type = "exact"
            elif raw_match == "PHRASE":
                search_term_type = "phrase"
            elif raw_match == "BROAD":
                search_term_type = "broad"
            else:
                search_term_type = ""

            update_data = {
                "id": lead["id"],
                "keyword_text": keyword,
                "search_term": keyword,  # click_view doesn't have search_term; keyword is closest
                "ad_group_name": click_data["ad_group_name"],
                "ad_group_id": click_data.get("ad_group_id", ""),
                "ad_id": click_data.get("ad_id", ""),           # actual ad creative ID
                "ad_name": click_data.get("ad_name", ""),       # ad creative name
                "campaign_name": click_data.get("campaign_name", ""),
                "campaign_id": click_data.get("campaign_id", ""),
                "click_cost": click_cost,
                "gads_synced_at": now,
                "search_term_type": search_term_type,
            }

            upsert_lead(update_data)
            synced += 1

            logger.info(
                f"Lead {lead['id']}: gclid → keyword='{keyword}', "
                f"ad_group='{click_data['ad_group_name']}' (id={click_data.get('ad_group_id', '')}), "
                f"ad_id={click_data.get('ad_id', '')}, ad_name='{click_data.get('ad_name', '')}', "
                f"campaign='{click_data['campaign_name']}', CPC=${click_cost:.2f}"
            )

        except Exception as e:
            logger.error(f"Error syncing lead {lead.get('id', '?')}: {e}")
            errors += 1

    # Pass 3: Search terms (actual user queries) — persist to cache for reports + AI
    logger.info("Fetching search terms report...")
    try:
        from keyword_planner import fetch_search_terms, fetch_geo_performance, fetch_schedule_performance
        search_terms = fetch_search_terms(days=30)
        logger.info(f"Search terms synced: {len(search_terms)} terms cached")
    except Exception as e:
        logger.warning(f"Search terms fetch failed (non-fatal): {e}")
        search_terms = []

    # Pass 4: Geographic performance — cache for analysis
    logger.info("Fetching geo performance...")
    try:
        geo_data = fetch_geo_performance(days=30)
        if geo_data:
            save_gads_geo_cache(geo_data, days=30)
            logger.info(f"Geo performance synced: {len(geo_data)} locations cached")
    except Exception as e:
        logger.warning(f"Geo performance fetch failed (non-fatal): {e}")
        geo_data = []

    # Pass 5: Schedule / device / hour-of-day performance
    logger.info("Fetching schedule performance...")
    try:
        schedule_data = fetch_schedule_performance(days=30)
        hours = len(schedule_data.get("by_hour", []))
        days_data = len(schedule_data.get("by_day", []))
        devices = len(schedule_data.get("by_device", []))
        if hours or days_data or devices:
            save_gads_schedule_cache(schedule_data, days=30)
            logger.info(f"Schedule performance synced: {hours}h, {days_data}d, {devices} devices")
    except Exception as e:
        logger.warning(f"Schedule performance fetch failed (non-fatal): {e}")
        schedule_data = {}

    # Pass 6: Daily campaign+ad_group stats for trend charts
    logger.info("Fetching daily ad-group stats for trend charts...")
    try:
        daily_rows = _fetch_campaign_daily_stats(client, customer_id, days=30)
        logger.info(f"Daily ad-group stats: {daily_rows} rows upserted")
    except Exception as e:
        logger.warning(f"Daily ad-group stats fetch failed (non-fatal): {e}")
        daily_rows = 0

    # Pass 7: Ad creative metadata + daily metrics
    logger.info("Fetching ad creative metadata and daily metrics...")
    ad_rows = 0
    ad_metric_rows = 0
    try:
        ad_list = _fetch_ad_creatives(client, customer_id)
        if ad_list:
            ad_rows = save_gads_ads(ad_list, customer_id=customer_id)
            logger.info(f"Ad creatives: {ad_rows} upserted")
        metric_list = _fetch_ad_daily_metrics(client, customer_id, days=30)
        if metric_list:
            ad_metric_rows = save_gads_ad_metrics(metric_list)
            logger.info(f"Ad daily metrics: {ad_metric_rows} rows upserted")
    except Exception as e:
        logger.warning(f"Ad creative sync failed (non-fatal): {e}")

    result = {
        "synced": synced,
        "skipped": skipped,
        "no_gclid": no_gclid,
        "errors": errors,
        "gclids_in_google": len(gclid_map),
        "keywords_with_cost": len(keyword_costs),
        "search_terms_cached": len(search_terms),
        "geo_locations_cached": len(geo_data),
        "schedule_hours_cached": len(schedule_data.get("by_hour", [])),
        "daily_stats_rows": daily_rows,
        "ad_creatives_synced": ad_rows,
        "ad_metric_rows": ad_metric_rows,
    }
    logger.info(f"Google Ads sync complete: {result}")
    return result


def sync_call_view(days_back: int = 14) -> int:
    """
    Pull Google Ads call_view data (calls made directly from ads) and upsert
    into the gads_call_view table for attribution matching.
    Returns count of rows upserted.
    """
    try:
        client = _build_client()
    except Exception as e:
        logger.error(f"sync_call_view: failed to build client: {e}")
        return 0

    settings = get_settings()
    customer_id = settings.google_ads_customer_id.replace("-", "")
    ga_service = client.get_service("GoogleAdsService")

    query = """
        SELECT
            call_view.resource_name,
            call_view.caller_area_code,
            call_view.caller_country_code,
            call_view.call_duration_seconds,
            call_view.call_status,
            call_view.type,
            call_view.start_call_date_time,
            call_view.end_call_date_time,
            campaign.id,
            campaign.name
        FROM call_view
        ORDER BY call_view.start_call_date_time DESC
        LIMIT 500
    """

    count = 0
    try:
        response = ga_service.search(customer_id=customer_id, query=query)
        for row in response:
            cv = row.call_view
            # Build a stable call_id from the resource_name tail segment
            resource_name = cv.resource_name or ""
            call_id = resource_name.split("/")[-1] if resource_name else ""
            if not call_id:
                continue

            record = {
                "call_id": call_id,
                "customer_id": customer_id,
                "campaign_id": str(row.campaign.id),
                "campaign_name": row.campaign.name,
                "caller_country_code": cv.caller_country_code or "",
                "caller_area_code": cv.caller_area_code or "",
                "call_duration_sec": int(cv.call_duration_seconds or 0),
                "call_status": cv.call_status.name if cv.call_status else "",
                "call_type": cv.type_.name if cv.type_ else "",
                "start_call_date_time": str(cv.start_call_date_time or ""),
            }
            upsert_gads_call_view(record)
            count += 1
    except Exception as e:
        logger.error(f"sync_call_view: API error: {e}")

    logger.info(f"sync_call_view: upserted {count} call_view rows")
    return count


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = sync_gclids_to_keywords()
    print(result)
