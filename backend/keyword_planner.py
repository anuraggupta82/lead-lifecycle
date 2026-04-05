"""
Keyword Planner & Campaign Intelligence — Google Ads API
Provides pre-launch research (search volume, competition, CPC forecasts)
and ongoing performance segmentation (geo, device, time-of-day, demographics).

Used by:
  - Admin "Keyword Research" tool (pre-launch campaign planning)
  - AI optimizer decision packet (context for recommendations)
  - Daily sync (saves geo/schedule data to DB for trend analysis)
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from config import get_settings

logger = logging.getLogger(__name__)


def _build_client():
    """Create authenticated Google Ads API client."""
    from google.ads.googleads.client import GoogleAdsClient
    settings = get_settings()
    return GoogleAdsClient.load_from_dict({
        "developer_token": settings.google_ads_developer_token,
        "client_id": settings.google_ads_client_id,
        "client_secret": settings.google_ads_client_secret,
        "refresh_token": settings.google_ads_refresh_token,
        "login_customer_id": settings.google_ads_login_customer_id,
        "use_proto_plus": True,
    })


# ── Keyword Planner ───────────────────────────────────────────────────────────

def get_keyword_ideas(
    seed_keywords: list,
    geo_target_ids: list = None,
    language_id: str = "1000",  # 1000 = English
    include_adult_keywords: bool = False,
) -> list:
    """
    Use Google Keyword Planner to get keyword ideas from seed terms.
    Returns list of dicts with search volume, competition, and CPC range.

    seed_keywords: list of strings, e.g. ["dental implants near me", "all on 4"]
    geo_target_ids: list of Google geo target constant resource names
                    e.g. ["geoTargetConstants/1020615"] for a metro area
                    Defaults to settings.google_ads_geo_target_ids (comma-separated)
    language_id: Google language ID. 1000 = English.

    Returns list of:
      {
        "keyword": str,
        "avg_monthly_searches": int,
        "competition": str,       # "LOW" / "MEDIUM" / "HIGH" / "UNSPECIFIED"
        "competition_index": int, # 0-100
        "low_cpc": float,         # low top-of-page bid (dollars)
        "high_cpc": float,        # high top-of-page bid (dollars)
        "trend_monthly": list,    # last 12 months search volumes (newest first)
      }
    """
    settings = get_settings()

    # Build geo target list
    if not geo_target_ids:
        raw = getattr(settings, 'google_ads_geo_target_ids', '')
        if raw:
            geo_target_ids = [g.strip() for g in raw.split(',') if g.strip()]
        else:
            geo_target_ids = []

    try:
        client = _build_client()
    except Exception as e:
        logger.error(f"Failed to build Google Ads client: {e}")
        return []

    customer_id = settings.google_ads_customer_id
    keyword_plan_idea_service = client.get_service("KeywordPlanIdeaService")

    request = client.get_type("GenerateKeywordIdeasRequest")
    request.customer_id = customer_id
    request.language = f"languageConstants/{language_id}"
    request.include_adult_keywords = include_adult_keywords

    if geo_target_ids:
        request.geo_target_constants.extend(geo_target_ids)

    # Use keyword seed
    request.keyword_seed.keywords.extend(seed_keywords)

    results = []
    try:
        response = keyword_plan_idea_service.generate_keyword_ideas(request=request)
        for idea in response:
            metrics = idea.keyword_idea_metrics
            competition_map = {0: "UNSPECIFIED", 1: "UNKNOWN", 2: "LOW", 3: "MEDIUM", 4: "HIGH"}
            comp_val = int(metrics.competition) if metrics.competition else 0

            # Monthly search volume trend (last 12 months)
            trend = []
            if hasattr(metrics, 'monthly_search_volumes'):
                for m in metrics.monthly_search_volumes:
                    trend.append({
                        "year": m.year,
                        "month": m.month,
                        "searches": m.monthly_searches or 0,
                    })
                # Sort newest first
                trend.sort(key=lambda x: (x["year"], x["month"]), reverse=True)
                trend = trend[:12]

            results.append({
                "keyword": idea.text,
                "avg_monthly_searches": metrics.avg_monthly_searches or 0,
                "competition": competition_map.get(comp_val, "UNSPECIFIED"),
                "competition_index": metrics.competition_index or 0,
                "low_cpc": round((metrics.low_top_of_page_bid_micros or 0) / 1_000_000, 2),
                "high_cpc": round((metrics.high_top_of_page_bid_micros or 0) / 1_000_000, 2),
                "trend_monthly": trend,
            })

        # Sort by search volume descending
        results.sort(key=lambda x: x["avg_monthly_searches"], reverse=True)
        logger.info(f"Keyword Planner: {len(results)} ideas for seeds {seed_keywords}")

    except Exception as e:
        logger.error(f"Keyword Planner API error: {e}")

    return results


# ── Geographic Performance ────────────────────────────────────────────────────

def fetch_geo_performance(days: int = 30) -> list:
    """
    Pull campaign performance segmented by city/region from geographic_view.
    Returns list of dicts sorted by conversions desc.

    Returns:
      {
        "location_type": str,   # "CITY" / "REGION" / "COUNTRY"
        "location_name": str,   # city or region name (from criteria)
        "campaign_name": str,
        "impressions": int,
        "clicks": int,
        "cost": float,
        "conversions": float,
        "cpc": float,
        "conversion_rate": float,
      }
    """
    try:
        client = _build_client()
    except Exception as e:
        logger.error(f"Failed to build client for geo performance: {e}")
        return []

    settings = get_settings()
    customer_id = settings.google_ads_customer_id
    service = client.get_service("GoogleAdsService")

    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=days)

    query = f"""
        SELECT
            geographic_view.location_type,
            geographic_view.country_criterion_id,
            campaign.name,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            segments.geo_target_city,
            segments.geo_target_region
        FROM geographic_view
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
            AND campaign.status = 'ENABLED'
            AND metrics.impressions > 0
        ORDER BY metrics.conversions DESC
        LIMIT 200
    """

    results = []
    try:
        response = service.search(customer_id=customer_id, query=query)
        for row in response:
            cost = (row.metrics.cost_micros or 0) / 1_000_000.0
            clicks = row.metrics.clicks or 0
            conversions = row.metrics.conversions or 0.0

            # Use geo_target_city if available, fall back to region
            location_name = ""
            location_type = str(row.geographic_view.location_type) if row.geographic_view.location_type else "UNKNOWN"
            if hasattr(row.segments, 'geo_target_city') and row.segments.geo_target_city:
                location_name = row.segments.geo_target_city
                location_type = "CITY"
            elif hasattr(row.segments, 'geo_target_region') and row.segments.geo_target_region:
                location_name = row.segments.geo_target_region
                location_type = "REGION"

            results.append({
                "location_type": location_type,
                "location_name": location_name or f"geo:{row.geographic_view.country_criterion_id}",
                "campaign_name": row.campaign.name or "",
                "impressions": row.metrics.impressions or 0,
                "clicks": clicks,
                "cost": round(cost, 2),
                "conversions": round(conversions, 2),
                "cpc": round(cost / clicks, 2) if clicks > 0 else 0.0,
                "conversion_rate": round(conversions / clicks * 100, 1) if clicks > 0 else 0.0,
            })

    except Exception as e:
        logger.error(f"Geographic view query failed: {e}")

    return results


# ── Hour-of-Day & Day-of-Week Performance ─────────────────────────────────────

def fetch_schedule_performance(days: int = 30) -> dict:
    """
    Pull campaign performance segmented by hour of day and day of week.
    Used to identify best/worst times to show ads.

    Returns:
      {
        "by_hour": [{"hour": 0-23, "impressions": N, "clicks": N, "cost": F, "conversions": F, "cpc": F}],
        "by_day":  [{"day": "MONDAY"..., "impressions": N, ...}],
        "by_device": [{"device": "MOBILE"/"DESKTOP"/"TABLET", ...}],
      }
    """
    try:
        client = _build_client()
    except Exception as e:
        logger.error(f"Failed to build client for schedule performance: {e}")
        return {"by_hour": [], "by_day": [], "by_device": []}

    settings = get_settings()
    customer_id = settings.google_ads_customer_id
    service = client.get_service("GoogleAdsService")

    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=days)
    date_filter = f"segments.date BETWEEN '{start_date}' AND '{end_date}'"

    def _run_query(query):
        try:
            return list(service.search(customer_id=customer_id, query=query))
        except Exception as e:
            logger.error(f"Schedule query failed: {e}")
            return []

    # Hour of day
    hour_query = f"""
        SELECT
            segments.hour_of_day,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions
        FROM campaign
        WHERE {date_filter}
            AND campaign.status = 'ENABLED'
        ORDER BY segments.hour_of_day
    """

    # Day of week
    dow_query = f"""
        SELECT
            segments.day_of_week,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions
        FROM campaign
        WHERE {date_filter}
            AND campaign.status = 'ENABLED'
        ORDER BY segments.day_of_week
    """

    # Device breakdown
    device_query = f"""
        SELECT
            segments.device,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions
        FROM campaign
        WHERE {date_filter}
            AND campaign.status = 'ENABLED'
        ORDER BY metrics.clicks DESC
    """

    def _agg_rows(rows, key_attr):
        agg = {}
        for row in rows:
            key = str(getattr(row.segments, key_attr, ""))
            cost = (row.metrics.cost_micros or 0) / 1_000_000.0
            clicks = row.metrics.clicks or 0
            if key not in agg:
                agg[key] = {"impressions": 0, "clicks": 0, "cost": 0.0, "conversions": 0.0}
            agg[key]["impressions"] += row.metrics.impressions or 0
            agg[key]["clicks"] += clicks
            agg[key]["cost"] += cost
            agg[key]["conversions"] += row.metrics.conversions or 0.0
        result = []
        for k, v in agg.items():
            v["cpc"] = round(v["cost"] / v["clicks"], 2) if v["clicks"] > 0 else 0.0
            v["conversion_rate"] = round(v["conversions"] / v["clicks"] * 100, 1) if v["clicks"] > 0 else 0.0
            v["cost"] = round(v["cost"], 2)
            result.append({key_attr: k, **v})
        return result

    by_hour_raw = _run_query(hour_query)
    by_day_raw = _run_query(dow_query)
    by_device_raw = _run_query(device_query)

    by_hour = _agg_rows(by_hour_raw, "hour_of_day")
    by_hour.sort(key=lambda x: int(x.get("hour_of_day", 0)))
    # Rename key for clarity
    for h in by_hour:
        h["hour"] = int(h.pop("hour_of_day", 0))

    by_day = _agg_rows(by_day_raw, "day_of_week")
    DAY_ORDER = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]
    by_day.sort(key=lambda x: DAY_ORDER.index(x.get("day_of_week", "MONDAY")) if x.get("day_of_week") in DAY_ORDER else 99)
    for d in by_day:
        d["day"] = d.pop("day_of_week", "")

    by_device = _agg_rows(by_device_raw, "device")
    for d in by_device:
        d["device"] = d.pop("device", "")

    logger.info(f"Schedule performance: {len(by_hour)} hours, {len(by_day)} days, {len(by_device)} devices")

    return {
        "by_hour": by_hour,
        "by_day": by_day,
        "by_device": by_device,
    }


# ── Age & Gender Demographics ─────────────────────────────────────────────────

def fetch_demographic_performance(days: int = 30) -> dict:
    """
    Pull performance segmented by age range and gender.
    Uses age_range_view and gender_view resources.

    Returns:
      {
        "by_age":    [{"age_range": "AGE_RANGE_18_24", "impressions": N, ...}],
        "by_gender": [{"gender": "MALE"/"FEMALE"/"UNDETERMINED", ...}],
      }

    NOTE: Household income is NOT available via Google Ads API.
    """
    try:
        client = _build_client()
    except Exception as e:
        logger.error(f"Failed to build client for demographics: {e}")
        return {"by_age": [], "by_gender": []}

    settings = get_settings()
    customer_id = settings.google_ads_customer_id
    service = client.get_service("GoogleAdsService")

    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=days)
    date_filter = f"segments.date BETWEEN '{start_date}' AND '{end_date}'"

    age_query = f"""
        SELECT
            ad_group_criterion.age_range.type,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions
        FROM age_range_view
        WHERE {date_filter}
            AND campaign.status = 'ENABLED'
    """

    gender_query = f"""
        SELECT
            ad_group_criterion.gender.type,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions
        FROM gender_view
        WHERE {date_filter}
            AND campaign.status = 'ENABLED'
    """

    def _run(q):
        try:
            return list(service.search(customer_id=customer_id, query=q))
        except Exception as e:
            logger.error(f"Demo query failed: {e}")
            return []

    def _agg(rows, attr_path):
        agg = {}
        for row in rows:
            # Navigate nested attr (e.g. ad_group_criterion.age_range.type)
            obj = row
            for part in attr_path.split('.'):
                obj = getattr(obj, part, None)
                if obj is None:
                    break
            key = str(obj) if obj is not None else "UNKNOWN"
            cost = (row.metrics.cost_micros or 0) / 1_000_000.0
            if key not in agg:
                agg[key] = {"impressions": 0, "clicks": 0, "cost": 0.0, "conversions": 0.0}
            agg[key]["impressions"] += row.metrics.impressions or 0
            agg[key]["clicks"] += row.metrics.clicks or 0
            agg[key]["cost"] += cost
            agg[key]["conversions"] += row.metrics.conversions or 0.0

        result = []
        for k, v in agg.items():
            v["cpc"] = round(v["cost"] / v["clicks"], 2) if v["clicks"] > 0 else 0.0
            v["conversion_rate"] = round(v["conversions"] / v["clicks"] * 100, 1) if v["clicks"] > 0 else 0.0
            v["cost"] = round(v["cost"], 2)
            result.append({"segment": k, **v})
        result.sort(key=lambda x: x["impressions"], reverse=True)
        return result

    age_rows = _run(age_query)
    gender_rows = _run(gender_query)

    by_age = _agg(age_rows, "ad_group_criterion.age_range.type")
    by_gender = _agg(gender_rows, "ad_group_criterion.gender.type")

    # Rename 'segment' key for clarity
    by_age_out = [{"age_range": r.pop("segment"), **r} for r in by_age]
    by_gender_out = [{"gender": r.pop("segment"), **r} for r in by_gender]

    logger.info(f"Demographics: {len(by_age_out)} age bands, {len(by_gender_out)} gender bands")

    return {
        "by_age": by_age_out,
        "by_gender": by_gender_out,
    }


# ── Search Term Performance (with persistence) ────────────────────────────────

def fetch_search_terms(days: int = 30) -> list:
    """
    Pull search_term_view — actual queries users typed that triggered ads.
    Saves to gads_search_terms_cache table.

    Returns list of:
      {
        "search_term": str,
        "status": str,          # ADDED / EXCLUDED / NONE
        "campaign_name": str,
        "ad_group_name": str,
        "impressions": int,
        "clicks": int,
        "cost": float,
        "conversions": float,
        "cpc": float,
      }
    """
    try:
        client = _build_client()
    except Exception as e:
        logger.error(f"Failed to build client for search terms: {e}")
        return []

    settings = get_settings()
    customer_id = settings.google_ads_customer_id
    service = client.get_service("GoogleAdsService")

    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=days)

    query = f"""
        SELECT
            search_term_view.search_term,
            search_term_view.status,
            campaign.name,
            ad_group.name,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions
        FROM search_term_view
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
            AND metrics.impressions > 0
            AND campaign.status = 'ENABLED'
        ORDER BY metrics.cost_micros DESC
        LIMIT 500
    """

    results = []
    try:
        response = service.search(customer_id=customer_id, query=query)
        for row in response:
            cost = (row.metrics.cost_micros or 0) / 1_000_000.0
            clicks = row.metrics.clicks or 0
            results.append({
                "search_term": row.search_term_view.search_term or "",
                "status": str(row.search_term_view.status) if row.search_term_view.status else "NONE",
                "campaign_name": row.campaign.name or "",
                "ad_group_name": row.ad_group.name or "",
                "impressions": row.metrics.impressions or 0,
                "clicks": clicks,
                "cost": round(cost, 4),
                "conversions": round(row.metrics.conversions or 0.0, 2),
                "cpc": round(cost / clicks, 2) if clicks > 0 else 0.0,
            })

        logger.info(f"Search terms: {len(results)} terms fetched")

        # Persist to cache
        if results:
            from database import save_gads_search_terms_cache
            save_gads_search_terms_cache(results, days=days)

    except Exception as e:
        logger.error(f"Search term view query failed: {e}")

    return results
