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
    upsert_gads_call_view, upsert_gads_clicks, upsert_call_search_terms,
    save_gads_keyword_bid_estimates,
    save_gads_conversion_actions, save_gads_keyword_click_share,
    save_gads_device_performance, save_gads_geo_performance,
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


def _micros_to_usd(val):
    """Convert micros (int) to dollars. None-safe — treats 0 as valid (not None)."""
    return round(val / 1_000_000.0, 4) if val is not None else None


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

        # NOTE: click_view only allows a restricted set of resources in SELECT.
        # AD_GROUP_AD is incompatible with click_view — both ad.id and ad.name
        # cause INVALID_ARGUMENT errors. Only select: click_view, ad_group,
        # campaign, and segments. ad_id stored as "" since it's not queryable here.
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

                click_data = {
                    "gclid": gclid,
                    "keyword_text": row.click_view.keyword_info.text or "",
                    "match_type": str(row.click_view.keyword_info.match_type) if row.click_view.keyword_info.match_type else "",
                    "ad_group_name": row.ad_group.name or "",
                    "ad_group_id": str(row.ad_group.id) if row.ad_group.id else "",
                    "ad_id": "",    # ad_group_ad not selectable with click_view resource
                    "ad_name": "",  # ad_group_ad not selectable with click_view resource
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
            # Log full error — a silent exception here means ALL days return 0
            # which is indistinguishable from "no clicks", masking real query bugs.
            error_str = str(e)
            if "EXPECTED_FILTER" not in error_str:
                logger.error(f"click_view query failed for {date_str}: {error_str[:500]}")

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


def _fetch_account_intelligence(client, customer_id: str) -> dict:
    """
    Fetch account-level health signals from Google Ads API and persist to gads_account_stats.

    Three independent passes — each wrapped in its own try/except so a failure in one
    doesn't blank the others:

    Pass 1 — customer.optimization_score (no date segment, single row)
    Pass 2 — top/abs-top impression share via `customer` + `segments.date`
              + invalid clicks aggregated from `campaign` level (invalid_clicks is NOT
              available on the `customer` resource in v24 — must use campaign aggregate)
              Both impression-share metrics are impression-weighted (not a daily mean)
              to match how Google calculates account-level IS.
    Pass 3 — Search Partners spend share via `segments.ad_network_type` on `campaign`.
              Uses proto enum .name per project convention (str() returns integer).
              search_partners_pct is None when total_cost=0 (no sync data yet) so the
              optimizer can distinguish "disabled" (0.0) from "not synced yet" (None).

    Returns a dict ready for save_gads_account_stats().
    """
    from database import save_gads_account_stats

    service = client.get_service("GoogleAdsService")
    result: dict = {}

    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=30)

    # Pass 1: customer-level optimization_score (no date segment needed)
    try:
        cust_query = "SELECT customer.optimization_score FROM customer"
        for row in service.search(customer_id=customer_id, query=cust_query):
            result["optimization_score"] = float(row.customer.optimization_score or 0.0)
        logger.info(f"Account optimization_score: {result.get('optimization_score')}")
    except Exception as e:
        logger.warning(f"_fetch_account_intelligence pass 1 (opt_score) failed: {e}")

    # Pass 2a: Top / abs-top impression share from customer resource (date-segmented).
    # Weighted by daily impressions — NOT a simple daily mean — to faithfully represent
    # the 30-day account-level IS (a day with 1k imps at 20% outweighs a day with 10 imps at 90%).
    try:
        is_query = f"""
            SELECT
                metrics.impressions,
                metrics.search_top_impression_share,
                metrics.search_absolute_top_impression_share
            FROM customer
            WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
        """
        imp_total = 0
        top_is_weighted = 0.0
        abs_top_weighted = 0.0
        for row in service.search(customer_id=customer_id, query=is_query):
            imps = int(row.metrics.impressions or 0)
            top_is_weighted += float(row.metrics.search_top_impression_share or 0.0) * imps
            abs_top_weighted += float(row.metrics.search_absolute_top_impression_share or 0.0) * imps
            imp_total += imps
        result["top_impression_pct"] = round(top_is_weighted / imp_total, 4) if imp_total else 0.0
        result["abs_top_impression_pct"] = round(abs_top_weighted / imp_total, 4) if imp_total else 0.0
        logger.info(f"Account top_IS: {result.get('top_impression_pct'):.1%}, "
                    f"abs_top_IS: {result.get('abs_top_impression_pct'):.1%}")
    except Exception as e:
        logger.warning(f"_fetch_account_intelligence pass 2a (impression share) failed: {e}")

    # Pass 2b: Invalid clicks — aggregated from campaign level (NOT available on customer resource in v24).
    # invalid_click_rate computed from totals (click-weighted) rather than averaging daily rates.
    try:
        inv_query = f"""
            SELECT
                metrics.invalid_clicks,
                metrics.clicks
            FROM campaign
            WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
        """
        inv_click_total = 0
        click_total = 0
        for row in service.search(customer_id=customer_id, query=inv_query):
            inv_click_total += int(row.metrics.invalid_clicks or 0)
            click_total += int(row.metrics.clicks or 0)
        result["invalid_clicks"] = inv_click_total
        result["invalid_click_rate"] = round(inv_click_total / click_total, 4) if click_total else 0.0
        logger.info(f"Account invalid_clicks: {inv_click_total}, rate: {result.get('invalid_click_rate'):.2%}")
    except Exception as e:
        logger.warning(f"_fetch_account_intelligence pass 2b (invalid clicks) failed: {e}")

    # Pass 3: Search Partners spend share (campaign-level, segmented by network type).
    # Uses .name on proto enum per project convention — str() returns the integer value.
    # search_partners_pct = None when total_cost=0 (no spend data) so the optimizer can
    # distinguish "Search Partners genuinely disabled (0.0)" from "not yet synced (None)".
    try:
        network_query = f"""
            SELECT
                segments.ad_network_type,
                metrics.cost_micros
            FROM campaign
            WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
        """
        search_cost = 0
        partners_cost = 0
        for row in service.search(customer_id=customer_id, query=network_query):
            net = row.segments.ad_network_type.name  # "SEARCH_PARTNERS", "GOOGLE_SEARCH", etc.
            cost = int(row.metrics.cost_micros or 0)
            if net == "SEARCH_PARTNERS":
                partners_cost += cost
            elif net in ("GOOGLE_SEARCH", "SEARCH"):
                search_cost += cost
        total_cost = search_cost + partners_cost
        if total_cost > 0:
            result["search_partners_pct"] = round(partners_cost / total_cost, 4)
        else:
            result["search_partners_pct"] = None  # not synced / no spend data yet
        logger.info(f"Search Partners spend share: {result.get('search_partners_pct')}")
    except Exception as e:
        logger.warning(f"_fetch_account_intelligence pass 3 (search partners) failed: {e}")

    if result:
        try:
            save_gads_account_stats(result)
            logger.info("Account intelligence saved to gads_account_stats")
        except Exception as e:
            logger.warning(f"save_gads_account_stats failed: {e}")

    return result


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


def _fetch_campaign_phone_stats(client, customer_id: str, days: int = 30) -> int:
    """
    Fetch Google Ads native phone-call metrics per campaign per day.
    These come from call extensions and call-only ads, NOT from CallRail.

    Fields fetched:
      - metrics.phone_calls        — calls initiated via Google Ads call extensions
      - metrics.phone_impressions  — impressions of call extensions (shows)
      - metrics.phone_through_rate — phone_calls / phone_impressions

    Note: phone_through_rate is derived (phone_calls / phone_impressions) so we
    compute it here rather than trusting the API value which may differ from the
    per-row detail. The API value is included as-is for reference.

    Upserts into gads_campaign_phone_stats via save_gads_campaign_phone_stats().
    Returns count of rows upserted.
    """
    from database import save_gads_campaign_phone_stats

    service = client.get_service("GoogleAdsService")
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=days)

    query = f"""
        SELECT
            campaign.id,
            campaign.name,
            segments.date,
            metrics.phone_calls,
            metrics.phone_impressions,
            metrics.phone_through_rate
        FROM campaign
        WHERE segments.date BETWEEN '{start_date.strftime("%Y-%m-%d")}' AND '{end_date.strftime("%Y-%m-%d")}'
          AND metrics.phone_impressions > 0
    """

    rows = []
    try:
        response = service.search(customer_id=customer_id, query=query)
        for row in response:
            campaign_id = str(row.campaign.id) if row.campaign.id else ""
            if not campaign_id:
                continue
            phone_calls = int(row.metrics.phone_calls or 0)
            phone_impressions = int(row.metrics.phone_impressions or 0)
            # Compute phone_through_rate from raw counts (more accurate than API value)
            ptr = round(phone_calls / phone_impressions, 4) if phone_impressions > 0 else 0.0
            rows.append({
                "campaign_id": campaign_id,
                "campaign_name": row.campaign.name or "",
                "date": row.segments.date,
                "phone_calls": phone_calls,
                "phone_impressions": phone_impressions,
                "phone_through_rate": ptr,
            })
        logger.info(f"Campaign phone stats: {len(rows)} rows fetched")
    except Exception as e:
        logger.warning(f"_fetch_campaign_phone_stats failed: {e}")
        return 0

    if rows:
        count = save_gads_campaign_phone_stats(rows)
        logger.info(f"Upserted {count} rows into gads_campaign_phone_stats")
        return count
    return 0


def _fetch_keyword_bid_estimates(client, customer_id: str) -> int:
    """
    PR-A: Fetch keyword-level bid estimates and serving status from Google Ads API.

    Fields fetched per keyword (ad_group_criterion resource — no date segment):
      - position_estimates.first_page_cpc_micros       — min CPC to reach page 1
      - position_estimates.top_of_page_cpc_micros      — min CPC to reach top 3
      - position_estimates.first_position_cpc_micros   — min CPC to reach #1
      - position_estimates.estimated_add_clicks_at_first_position_cpc
      - system_serving_status                          — ELIGIBLE / RARELY_SERVED / etc.
      - primary_status                                 — ELIGIBLE / PAUSED / REMOVED / etc.
      - effective_cpc_bid_micros                       — actual bid currently applied
      - quality_info.quality_score                     — 1–10

    Strategy: fetch from ad_group_criterion (no date segment) to get latest estimates.
    These are point-in-time estimates — re-fetched on every sync.

    Upserts into gads_keyword_bid_estimates via save_gads_keyword_bid_estimates().
    Returns count of rows upserted.
    """
    service = client.get_service("GoogleAdsService")

    query = """
        SELECT
            ad_group_criterion.keyword.text,
            ad_group_criterion.keyword.match_type,
            ad_group_criterion.status,
            ad_group_criterion.system_serving_status,
            ad_group_criterion.primary_status,
            ad_group_criterion.effective_cpc_bid_micros,
            ad_group_criterion.quality_info.quality_score,
            ad_group_criterion.position_estimates.first_page_cpc_micros,
            ad_group_criterion.position_estimates.top_of_page_cpc_micros,
            ad_group_criterion.position_estimates.first_position_cpc_micros,
            ad_group_criterion.position_estimates.estimated_add_clicks_at_first_position_cpc,
            ad_group.name,
            ad_group.id,
            campaign.name,
            campaign.id,
            campaign.status
        FROM ad_group_criterion
        WHERE ad_group_criterion.type = 'KEYWORD'
          AND ad_group_criterion.status != 'REMOVED'
          AND campaign.status = 'ENABLED'
          AND ad_group.status = 'ENABLED'
    """

    rows = []
    try:
        response = service.search(customer_id=customer_id, query=query)
        for row in response:
            kw = row.ad_group_criterion
            keyword_text = kw.keyword.text
            if not keyword_text:
                continue

            pe = kw.position_estimates
            # quality_score=0 means "no data yet" (new keyword); store None to distinguish from genuinely low QS
            qs_raw = kw.quality_info.quality_score
            qs = qs_raw if qs_raw > 0 else None

            rows.append({
                "keyword_text": keyword_text,
                "match_type": kw.keyword.match_type.name,
                "ad_group_name": row.ad_group.name or "",
                "ad_group_id": str(row.ad_group.id) if row.ad_group.id else "",
                "campaign_name": row.campaign.name or "",
                "campaign_id": str(row.campaign.id) if row.campaign.id else "",
                "criterion_status": kw.status.name,
                "system_serving_status": kw.system_serving_status.name,
                "primary_status": kw.primary_status.name,
                # Store raw micros (int) AND dollar values for prompt/reporting use
                "effective_cpc_bid": _micros_to_usd(kw.effective_cpc_bid_micros),
                "effective_cpc_bid_micros": int(kw.effective_cpc_bid_micros) if kw.effective_cpc_bid_micros is not None else None,
                "quality_score": qs,
                "first_page_cpc": _micros_to_usd(pe.first_page_cpc_micros),
                "first_page_cpc_micros": int(pe.first_page_cpc_micros) if pe.first_page_cpc_micros else None,
                "top_of_page_cpc": _micros_to_usd(pe.top_of_page_cpc_micros),
                "top_of_page_cpc_micros": int(pe.top_of_page_cpc_micros) if pe.top_of_page_cpc_micros else None,
                "first_position_cpc": _micros_to_usd(pe.first_position_cpc_micros),
                "first_position_cpc_micros": int(pe.first_position_cpc_micros) if pe.first_position_cpc_micros else None,
                "est_add_clicks_first_pos": pe.estimated_add_clicks_at_first_position_cpc or 0,
            })

        logger.info(f"Keyword bid estimates: {len(rows)} rows fetched")
    except Exception as e:
        logger.warning(f"_fetch_keyword_bid_estimates failed: {e}")
        return 0

    if rows:
        count = save_gads_keyword_bid_estimates(rows)
        logger.info(f"Upserted {count} rows into gads_keyword_bid_estimates")
        return count
    return 0


def _fetch_geo_target_names(client, customer_id: str, criterion_ids: list) -> dict:
    """
    PR-E helper: Look up human-readable names for a list of geo criterion IDs.
    Uses the geo_target_constant resource which is accessible from any customer.
    Returns dict: criterion_id → "Name, Country" (e.g. "Grafton, US")
    Falls back to empty dict on any error (geo name enrichment is best-effort).
    """
    if not criterion_ids:
        return {}
    service = client.get_service("GoogleAdsService")
    name_map: dict = {}
    try:
        # geo_target_constant is a global resource — no customer scope needed for read
        # Query in batches of 50 to stay within request limits
        unique_ids = list(set(str(cid) for cid in criterion_ids if cid))
        for i in range(0, len(unique_ids), 50):
            batch = unique_ids[i:i + 50]
            id_list = ", ".join(f"'{cid}'" for cid in batch)
            q = f"""
                SELECT geo_target_constant.id, geo_target_constant.name,
                       geo_target_constant.country_code, geo_target_constant.target_type
                FROM geo_target_constant
                WHERE geo_target_constant.id IN ({id_list})
            """
            for row in service.search(customer_id=customer_id, query=q):
                cid = str(row.geo_target_constant.id)
                name = row.geo_target_constant.name or ""
                country = row.geo_target_constant.country_code or ""
                ttype = row.geo_target_constant.target_type or ""
                name_map[cid] = f"{name}, {country}" if country else name
    except Exception as e:
        logger.warning(f"_fetch_geo_target_names failed (non-fatal): {e}")
    return name_map


def _fetch_geo_performance(client, customer_id: str, days: int = 30) -> int:
    """
    PR-E: Fetch geographic performance breakdown per campaign.
    Uses 'geographic_view' resource (v24).

    Key v24 schema facts:
      - geographic_view.resource_name encodes location criterion:
          customers/{cid}/geographicViews/{country_criterion_id}~{location_criterion_id}
        Parse the location criterion_id from the part after '~'.
        If no '~', the whole numeric suffix is the criterion_id (country-level).
      - geographic_view.location_type returns LOCATION_OF_PRESENCE or AREA_OF_INTEREST
        (NOT city/region granularity — that comes from geo_target_constant.target_type).
      - segments.geo_target does NOT exist in v24 — use geographic_view.resource_name.
      - Include impressions > 0 (not just clicks > 0) to capture high-impression / zero-click
        waste locations (candidates for negative geo targeting).

    After fetching performance rows, does a secondary lookup of geo_target_constant
    to resolve criterion_ids to human-readable "City, Country" names.

    Returns count of rows upserted.
    """
    from database import save_gads_geo_performance as _save
    service = client.get_service("GoogleAdsService")
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=days)

    rows = []
    try:
        query = f"""
            SELECT
                campaign.id,
                campaign.name,
                campaign.status,
                geographic_view.resource_name,
                geographic_view.location_type,
                metrics.impressions,
                metrics.clicks,
                metrics.cost_micros,
                metrics.conversions,
                metrics.ctr,
                metrics.average_cpc
            FROM geographic_view
            WHERE segments.date BETWEEN '{start_date.strftime("%Y-%m-%d")}' AND '{end_date.strftime("%Y-%m-%d")}'
              AND campaign.status = 'ENABLED'
              AND metrics.impressions > 0
        """
        for row in service.search(customer_id=customer_id, query=query):
            cost = _micros_to_usd(row.metrics.cost_micros) or 0.0
            clicks = int(row.metrics.clicks or 0)
            impressions = int(row.metrics.impressions or 0)
            conversions = float(row.metrics.conversions or 0.0)

            # Parse location criterion_id from resource_name:
            # Format: customers/{cid}/geographicViews/{country_id}~{location_id}
            # or just: customers/{cid}/geographicViews/{country_id} for country-level rows
            resource_name = row.geographic_view.resource_name or ""
            view_suffix = resource_name.split("/geographicViews/")[-1] if "/geographicViews/" in resource_name else ""
            if "~" in view_suffix:
                criterion_id = view_suffix.split("~")[-1]
            else:
                criterion_id = view_suffix  # country-level row

            # location_type: LOCATION_OF_PRESENCE (physical location) or AREA_OF_INTEREST (search interest)
            loc_type = row.geographic_view.location_type.name if row.geographic_view.location_type else "UNKNOWN"

            rows.append({
                "campaign_id": str(row.campaign.id),
                "campaign_name": row.campaign.name or "",
                "location_type": loc_type,
                "location_name": criterion_id,  # resolved to human name after batch lookup
                "criterion_id": criterion_id,
                "impressions": impressions,
                "clicks": clicks,
                "cost": cost,
                "conversions": conversions,
                "ctr": float(row.metrics.ctr or 0.0),
                "avg_cpc": _micros_to_usd(int(row.metrics.average_cpc or 0)) or 0.0,
                "cost_per_conv": round(cost / conversions, 4) if conversions > 0 else 0.0,
            })
        logger.info(f"Geo performance: {len(rows)} rows fetched")
    except Exception as e:
        logger.warning(f"_fetch_geo_performance failed: {e}")
        return 0

    if not rows:
        return 0

    # Enrich location_name: resolve criterion_ids to human-readable "City, Country"
    all_criterion_ids = [r["criterion_id"] for r in rows if r["criterion_id"]]
    name_map = _fetch_geo_target_names(client, customer_id, all_criterion_ids)
    for r in rows:
        cid = r["criterion_id"]
        if cid and cid in name_map:
            r["location_name"] = name_map[cid]
        else:
            r["location_name"] = f"geo:{cid}" if cid else "Unknown"

    count = _save(rows, days=days)
    logger.info(f"Upserted {count} rows into gads_geo_performance")
    return count


def _fetch_device_performance(client, customer_id: str, days: int = 30) -> int:
    """
    PR-C: Fetch device-segmented performance (MOBILE / DESKTOP / TABLET / CONNECTED_TV).
    Uses 'campaign' resource with segments.device — valid without incompatibility issues.
    Stores one row per (campaign_id, device, date) so trends can be tracked.
    Returns count of rows upserted.
    """
    from database import save_gads_device_performance as _save
    service = client.get_service("GoogleAdsService")
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=days)

    rows = []
    try:
        query = f"""
            SELECT
                campaign.id,
                campaign.name,
                segments.device,
                segments.date,
                metrics.impressions,
                metrics.clicks,
                metrics.cost_micros,
                metrics.conversions,
                metrics.ctr,
                metrics.average_cpc
            FROM campaign
            WHERE segments.date BETWEEN '{start_date.strftime("%Y-%m-%d")}' AND '{end_date.strftime("%Y-%m-%d")}'
              AND campaign.status = 'ENABLED'
              AND metrics.impressions > 0
        """
        for row in service.search(customer_id=customer_id, query=query):
            cost = _micros_to_usd(row.metrics.cost_micros) or 0.0
            clicks = int(row.metrics.clicks or 0)
            impressions = int(row.metrics.impressions or 0)
            conversions = float(row.metrics.conversions or 0.0)
            rows.append({
                "campaign_id": str(row.campaign.id),
                "campaign_name": row.campaign.name or "",
                # Always call .name — don't guard on truthiness since UNSPECIFIED=0 is falsy
                "device": row.segments.device.name,
                "date": str(row.segments.date) if row.segments.date else "",
                "impressions": impressions,
                "clicks": clicks,
                "cost": cost,
                "conversions": conversions,
                "ctr": float(row.metrics.ctr or 0.0),
                "avg_cpc": _micros_to_usd(int(row.metrics.average_cpc or 0)) or 0.0,
                "cost_per_conv": round(cost / conversions, 4) if conversions > 0 else 0.0,
            })
        logger.info(f"Device performance: {len(rows)} rows fetched")
    except Exception as e:
        logger.warning(f"_fetch_device_performance failed: {e}")
        return 0

    if rows:
        count = _save(rows)
        logger.info(f"Upserted {count} rows into gads_device_performance")
        return count
    return 0


def _fetch_conversion_actions(client, customer_id: str, days: int = 30) -> int:
    """
    PR-B Pass 1: Fetch conversions segmented by conversion_action_name per campaign per day.

    Uses campaign resource with segments.conversion_action_name and segments.date.
    This breaks down what TYPE of conversion is happening (e.g. "Appointment Booked",
    "Phone Call", "Form Submit") vs the aggregate conversions count.

    Upserts into gads_conversion_actions via save_gads_conversion_actions().
    Returns count of rows upserted.
    """
    service = client.get_service("GoogleAdsService")
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=days)

    query = f"""
        SELECT
            campaign.id,
            campaign.name,
            segments.date,
            segments.conversion_action_name,
            segments.conversion_action_category,
            metrics.conversions,
            metrics.conversions_value,
            metrics.all_conversions
        FROM campaign
        WHERE segments.date BETWEEN '{start_date.strftime("%Y-%m-%d")}' AND '{end_date.strftime("%Y-%m-%d")}'
          AND metrics.all_conversions > 0
    """

    rows = []
    try:
        response = service.search(customer_id=customer_id, query=query)
        for row in response:
            campaign_id = str(row.campaign.id) if row.campaign.id else ""
            if not campaign_id:
                continue
            rows.append({
                "campaign_id": campaign_id,
                "campaign_name": row.campaign.name or "",
                "date": row.segments.date,
                "conversion_action_name": row.segments.conversion_action_name or "",
                "conversion_action_category": row.segments.conversion_action_category.name if row.segments.conversion_action_category else "",
                "conversions": float(row.metrics.conversions or 0),
                "conversions_value": float(row.metrics.conversions_value or 0),
                "all_conversions": float(row.metrics.all_conversions or 0),
            })
        logger.info(f"Conversion action segments: {len(rows)} rows fetched")
    except Exception as e:
        logger.warning(f"_fetch_conversion_actions failed: {e}")
        return 0

    if rows:
        count = save_gads_conversion_actions(rows)
        logger.info(f"Upserted {count} rows into gads_conversion_actions")
        return count
    return 0


def _fetch_keyword_click_share(client, customer_id: str, days: int = 30) -> int:
    """
    PR-B Pass 2: Fetch click share (ad group level) + historical Quality Score per keyword.

    search_click_share: metrics.search_click_share is available on the 'ad_group' resource
    (NOT keyword_view — same segment incompatibility as impression share metrics).
    We pull it at ad group granularity, then map to all keywords in that ad group.

    historical_quality_score / historical_creative_quality_score /
    historical_landing_page_quality_score / historical_search_predicted_ctr:
    Available on keyword_view WITH segments.date — lets us see QS trends over time.
    Component scores are tracked by date so we use the most recent day's values.

    Two sub-passes:
      a) ad_group resource without date: click share at ad group level → mapped to keywords
      b) keyword_view with date: historical QS per keyword per day

    Upserts into gads_keyword_click_share via save_gads_keyword_click_share().
    Returns count of rows upserted.
    """
    from database import save_gads_keyword_click_share as _save
    service = client.get_service("GoogleAdsService")
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=days)

    # qs_map_labels: 0=no data, 1=unknown (treat as empty), 2=BELOW_AVERAGE, 3=AVERAGE, 4=ABOVE_AVERAGE
    qs_map_labels = {0: "", 1: "", 2: "BELOW_AVERAGE", 3: "AVERAGE", 4: "ABOVE_AVERAGE"}

    # Pass 2a: Click share at ad group level (metrics.search_click_share valid on ad_group resource)
    # Result: ad_group_id → click_share so we can map to all keywords in that ad group.
    ag_click_share_map: dict = {}  # ad_group_id → float or None
    ag_info_map: dict = {}         # ad_group_id → {ad_group_name, campaign_name}
    try:
        query_cs = """
            SELECT
                ad_group.id,
                ad_group.name,
                campaign.name,
                metrics.search_click_share
            FROM ad_group
            WHERE campaign.status = 'ENABLED'
              AND ad_group.status = 'ENABLED'
        """
        for row in service.search(customer_id=customer_id, query=query_cs):
            ag_id = str(row.ad_group.id)
            cs = row.metrics.search_click_share
            ag_click_share_map[ag_id] = float(cs) if cs else None
            ag_info_map[ag_id] = {
                "ad_group_name": row.ad_group.name or "",
                "campaign_name": row.campaign.name or "",
            }
        logger.info(f"Click share fetched for {len(ag_click_share_map)} ad groups")
    except Exception as e:
        logger.warning(f"_fetch_keyword_click_share pass 2a (ad_group click share) failed: {e}")

    # Pass 2b: Historical QS per keyword per day (with date segment)
    # Track latest_date per key so component scores use the most recent day, not iteration order.
    hist_qs_map: dict = {}  # key: (keyword_text, match_type, ag_name, camp_name) → agg dict
    kw_ag_id_map: dict = {}  # key → ad_group_id (for mapping click share)
    try:
        query_hqs = f"""
            SELECT
                ad_group_criterion.keyword.text,
                ad_group_criterion.keyword.match_type,
                ad_group.id,
                ad_group.name,
                campaign.name,
                segments.date,
                metrics.historical_quality_score,
                metrics.historical_creative_quality_score,
                metrics.historical_landing_page_quality_score,
                metrics.historical_search_predicted_ctr
            FROM keyword_view
            WHERE segments.date BETWEEN '{start_date.strftime("%Y-%m-%d")}' AND '{end_date.strftime("%Y-%m-%d")}'
              AND campaign.status = 'ENABLED'
              AND ad_group.status = 'ENABLED'
              AND ad_group_criterion.status != 'REMOVED'
        """
        for row in service.search(customer_id=customer_id, query=query_hqs):
            kw_text = row.ad_group_criterion.keyword.text
            if not kw_text:
                continue
            ag_id = str(row.ad_group.id)
            key = (kw_text, row.ad_group_criterion.keyword.match_type.name,
                   row.ad_group.name or "", row.campaign.name or "")
            kw_ag_id_map[key] = ag_id
            row_date = str(row.segments.date) if row.segments.date else ""
            qs = int(row.metrics.historical_quality_score or 0)
            if key not in hist_qs_map:
                hist_qs_map[key] = {
                    "qs_sum": 0, "qs_count": 0,
                    "qs_min": None, "qs_max": None,
                    "latest_date": "",
                    "creative_qs_latest": "",
                    "landing_page_qs_latest": "",
                    "search_predicted_ctr_latest": "",
                }
            if qs > 0:
                agg = hist_qs_map[key]
                agg["qs_sum"] += qs
                agg["qs_count"] += 1
                agg["qs_min"] = min(agg["qs_min"], qs) if agg["qs_min"] is not None else qs
                agg["qs_max"] = max(agg["qs_max"], qs) if agg["qs_max"] is not None else qs
                # Only update component scores if this row is more recent than what we have
                if row_date >= agg["latest_date"]:
                    agg["latest_date"] = row_date
                    cqs = int(row.metrics.historical_creative_quality_score or 0)
                    lpqs = int(row.metrics.historical_landing_page_quality_score or 0)
                    spctr = int(row.metrics.historical_search_predicted_ctr or 0)
                    if cqs: agg["creative_qs_latest"] = qs_map_labels.get(cqs, "")
                    if lpqs: agg["landing_page_qs_latest"] = qs_map_labels.get(lpqs, "")
                    if spctr: agg["search_predicted_ctr_latest"] = qs_map_labels.get(spctr, "")
        logger.info(f"Historical QS aggregated for {len(hist_qs_map)} keywords")
    except Exception as e:
        logger.warning(f"_fetch_keyword_click_share pass 2b (hist QS) failed: {e}")

    # Merge into rows for DB.
    # Click share is at ad group level → look up by ad_group_id mapped from keywords.
    # For keywords seen in hist_qs_map but not ag_click_share_map, click_share=None.
    all_keys = set(kw_ag_id_map.keys()) | set(hist_qs_map.keys())
    rows = []
    for key in all_keys:
        kw_text, match_type, ag_name, camp_name = key
        agg = hist_qs_map.get(key, {})
        qs_count = agg.get("qs_count", 0)
        # Map click share via ad_group_id → ag_click_share_map
        ag_id = kw_ag_id_map.get(key)
        click_share = ag_click_share_map.get(ag_id) if ag_id else None
        rows.append({
            "keyword_text": kw_text,
            "match_type": match_type,
            "ad_group_name": ag_name,
            "campaign_name": camp_name,
            "search_click_share": click_share,
            "historical_qs_avg": round(agg["qs_sum"] / qs_count, 2) if qs_count > 0 else None,
            "historical_qs_min": agg.get("qs_min"),
            "historical_qs_max": agg.get("qs_max"),
            "creative_quality_score": agg.get("creative_qs_latest", ""),
            "landing_page_quality_score": agg.get("landing_page_qs_latest", ""),
            "search_predicted_ctr": agg.get("search_predicted_ctr_latest", ""),
        })

    if rows:
        count = _save(rows)
        logger.info(f"Upserted {count} rows into gads_keyword_click_share")
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

    # Pass 4b: User location view (physical location of user — reveals demand leaks)
    logger.info("Fetching user location (physical) performance...")
    try:
        from keyword_planner import fetch_user_location_performance
        phys_data = fetch_user_location_performance(days=30)
        if phys_data:
            save_gads_geo_cache(phys_data, days=30, view_type="physical")
            logger.info(f"User location (physical) synced: {len(phys_data)} rows cached")
    except Exception as e:
        logger.warning(f"User location fetch failed (non-fatal): {e}")

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

    # Pass 7a: Phone stats (call extensions / call-only ads) per campaign per day
    logger.info("Fetching campaign phone stats (call extensions)...")
    try:
        phone_rows = _fetch_campaign_phone_stats(client, customer_id, days=30)
        logger.info(f"Phone stats: {phone_rows} rows upserted")
    except Exception as e:
        logger.warning(f"Phone stats fetch failed (non-fatal): {e}")
        phone_rows = 0

    # Pass 7b: Account-level intelligence (optimization_score, invalid clicks, top IS, partners)
    logger.info("Fetching account-level intelligence...")
    try:
        account_intel = _fetch_account_intelligence(client, customer_id)
        logger.info(f"Account intelligence fetched: {list(account_intel.keys())}")
    except Exception as e:
        logger.warning(f"Account intelligence fetch failed (non-fatal): {e}")
        account_intel = {}

    # Pass 7c: Keyword bid estimates (first_page_cpc, top_of_page_cpc, first_position_cpc, serving status)
    logger.info("Fetching keyword bid estimates and serving status...")
    try:
        bid_estimate_rows = _fetch_keyword_bid_estimates(client, customer_id)
        logger.info(f"Keyword bid estimates: {bid_estimate_rows} rows upserted")
    except Exception as e:
        logger.warning(f"Keyword bid estimates fetch failed (non-fatal): {e}")
        bid_estimate_rows = 0

    # Pass 8a: Conversion action segmentation (what TYPE of conversion per campaign)
    logger.info("Fetching conversion action segments...")
    try:
        conv_action_rows = _fetch_conversion_actions(client, customer_id, days=30)
        logger.info(f"Conversion actions: {conv_action_rows} rows upserted")
    except Exception as e:
        logger.warning(f"Conversion action fetch failed (non-fatal): {e}")
        conv_action_rows = 0

    # Pass 8b: Click share + historical QS per keyword
    logger.info("Fetching keyword click share and historical QS...")
    try:
        click_share_rows = _fetch_keyword_click_share(client, customer_id, days=30)
        logger.info(f"Keyword click share/hist QS: {click_share_rows} rows upserted")
    except Exception as e:
        logger.warning(f"Keyword click share fetch failed (non-fatal): {e}")
        click_share_rows = 0

    # Pass 8c: Device segmentation (MOBILE / DESKTOP / TABLET performance breakdown)
    logger.info("Fetching device performance breakdown...")
    try:
        device_perf_rows = _fetch_device_performance(client, customer_id, days=30)
        logger.info(f"Device performance: {device_perf_rows} rows upserted")
    except Exception as e:
        logger.warning(f"Device performance fetch failed (non-fatal): {e}")
        device_perf_rows = 0

    # Pass 8d: Geographic performance (city/region breakdown per campaign)
    logger.info("Fetching geographic performance breakdown...")
    try:
        geo_perf_rows = _fetch_geo_performance(client, customer_id, days=30)
        logger.info(f"Geo performance: {geo_perf_rows} rows upserted")
    except Exception as e:
        logger.warning(f"Geo performance fetch failed (non-fatal): {e}")
        geo_perf_rows = 0

    # Pass 8e: Auction Insights (competitor impression share + overlap rates)
    logger.info("Fetching auction insights...")
    try:
        auction_insight_rows = _fetch_auction_insights(client, customer_id, days=30)
        logger.info(f"Auction insights: {auction_insight_rows} rows upserted")
    except Exception as e:
        logger.warning(f"Auction insights fetch failed (non-fatal): {e}")
        auction_insight_rows = 0

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
        "account_intel_fetched": bool(account_intel),
        "phone_stats_rows": phone_rows,
        "keyword_bid_estimate_rows": bid_estimate_rows,
        "conversion_action_rows": conv_action_rows,
        "keyword_click_share_rows": click_share_rows,
        "device_performance_rows": device_perf_rows,
        "geo_performance_rows": geo_perf_rows,
        "auction_insight_rows": auction_insight_rows,
    }
    logger.info(f"Google Ads sync complete: {result}")
    return result


def sync_call_search_terms(days: int = 30) -> int:
    """
    Fetch all search terms from search_term_view for the last N days.

    Note: Google's GAQL does NOT allow segments.click_type on search_term_view
    (PROHIBITED_SEGMENT_IN_SELECT_OR_WHERE_CLAUSE), so we pull all search terms
    and rely on ad_group-level matching in backfill_call_keyword_attribution()
    to connect search terms to calls.

    Returns count of rows upserted.
    """
    try:
        client = _build_client()
    except Exception as e:
        logger.error(f"sync_call_search_terms: failed to build client: {e}")
        return 0

    settings = get_settings()
    customer_id = settings.google_ads_customer_id.replace("-", "")
    ga_service = client.get_service("GoogleAdsService")

    # Note: ad_group_criterion is INCOMPATIBLE with search_term_view FROM clause.
    # We only select from search_term_view, campaign, ad_group, and metrics.
    query = f"""
        SELECT
            search_term_view.search_term,
            campaign.id,
            campaign.name,
            ad_group.name,
            metrics.clicks,
            metrics.conversions
        FROM search_term_view
        WHERE
            segments.date DURING LAST_{days}_DAYS
            AND metrics.clicks > 0
        ORDER BY metrics.clicks DESC
    """

    rows_to_upsert = []
    try:
        response = ga_service.search(customer_id=customer_id, query=query)
        for row in response:
            rows_to_upsert.append({
                "search_term": row.search_term_view.search_term or "",
                "keyword_text": "",
                "keyword_match_type": "",
                "campaign_id": str(row.campaign.id),
                "campaign_name": row.campaign.name or "",
                "ad_group_name": row.ad_group.name or "",
                "conversions": float(row.metrics.conversions or 0),
                "days": days,
            })
    except Exception as e:
        logger.error(f"sync_call_search_terms: API error: {e}")
        return 0

    if rows_to_upsert:
        n = upsert_call_search_terms(rows_to_upsert)
        logger.info(f"sync_call_search_terms: upserted {n} rows ({len(rows_to_upsert)} fetched)")
        return n

    logger.info("sync_call_search_terms: no search terms found")
    return 0


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
            campaign.name,
            ad_group.id,
            ad_group.name
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
                "ad_group_id": str(row.ad_group.id) if row.ad_group.id else "",
                "ad_group_name": row.ad_group.name or "",
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


def _fetch_auction_insights(client, customer_id: str, days: int = 30) -> int:
    """
    Pass 8e: Fetch Auction Insights — competitor impression share, overlap rate,
    outranking share, and top-of-page rate per campaign per date.

    Google Ads API resource: auction_insight (campaign level).
    Stores one row per (campaign_id, domain, date) so trends can be tracked.
    Returns count of rows upserted.
    """
    from database import save_gads_auction_insights as _save
    service = client.get_service("GoogleAdsService")
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=days)

    rows = []
    try:
        # NOTE: Auction Insights is queried FROM campaign (not from a separate resource).
        # The competitor domain is a SEGMENT: segments.auction_insight_domain.
        # This feature requires the account to be allowlisted by Google for Auction Insights API access.
        query = f"""
            SELECT
                campaign.id,
                campaign.name,
                segments.auction_insight_domain,
                segments.date,
                metrics.auction_insight_search_impression_share,
                metrics.auction_insight_search_overlap_rate,
                metrics.auction_insight_search_outranking_share,
                metrics.auction_insight_search_top_impression_percentage,
                metrics.auction_insight_search_absolute_top_impression_percentage,
                metrics.auction_insight_search_position_above_rate
            FROM campaign
            WHERE segments.date BETWEEN '{start_date.strftime("%Y-%m-%d")}' AND '{end_date.strftime("%Y-%m-%d")}'
              AND campaign.status = 'ENABLED'
        """
        for row in service.search(customer_id=customer_id, query=query):
            rows.append({
                "campaign_id":       str(row.campaign.id),
                "campaign_name":     row.campaign.name or "",
                "domain":            row.segments.auction_insight_domain or "",
                "date":              str(row.segments.date) if row.segments.date else "",
                "impression_share":  float(row.metrics.auction_insight_search_impression_share or 0.0),
                "overlap_rate":      float(row.metrics.auction_insight_search_overlap_rate or 0.0),
                "outranking_share":  float(row.metrics.auction_insight_search_outranking_share or 0.0),
                "top_impression_pct": float(row.metrics.auction_insight_search_top_impression_percentage or 0.0),
                "abs_top_impression_pct": float(row.metrics.auction_insight_search_absolute_top_impression_percentage or 0.0),
                "position_above_rate": float(row.metrics.auction_insight_search_position_above_rate or 0.0),
            })
        logger.info(f"Auction insights: {len(rows)} rows fetched")
    except Exception as e:
        logger.warning(f"_fetch_auction_insights failed: {e}")
        return 0

    if rows:
        count = _save(rows)
        logger.info(f"Upserted {count} rows into gads_auction_insights")
        return count
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = sync_gclids_to_keywords()
    print(result)
