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

def _resolve_geo_target_names(client, customer_id: str, resource_names: set) -> dict:
    """
    Batch-resolve geoTargetConstants/XXXXXX resource names → human-readable city/region names.
    Returns {resource_name: display_name} dict.
    Resource names that cannot be resolved are omitted.
    """
    if not resource_names:
        return {}
    service = client.get_service("GoogleAdsService")
    # Filter to actual resource-name strings (skip already-resolved names)
    to_resolve = {r for r in resource_names if r and r.startswith("geoTargetConstants/")}
    if not to_resolve:
        return {}
    # Build quoted list for GAQL IN clause
    quoted = ", ".join(f"'{r}'" for r in to_resolve)
    query = f"""
        SELECT
            geo_target_constant.resource_name,
            geo_target_constant.name,
            geo_target_constant.country_code,
            geo_target_constant.target_type
        FROM geo_target_constant
        WHERE geo_target_constant.resource_name IN ({quoted})
    """
    name_map: dict = {}
    try:
        response = service.search(customer_id=customer_id, query=query)
        for row in response:
            rn = row.geo_target_constant.resource_name
            name = row.geo_target_constant.name
            if rn and name:
                name_map[rn] = name
    except Exception as e:
        logger.warning(f"geo_target_constant name resolution failed (non-fatal): {e}")
    return name_map


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
            campaign.status,
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

    raw_rows = []
    resource_names_to_resolve: set = set()
    try:
        response = service.search(customer_id=customer_id, query=query)
        for row in response:
            cost = (row.metrics.cost_micros or 0) / 1_000_000.0
            clicks = row.metrics.clicks or 0
            conversions = row.metrics.conversions or 0.0

            # segments.geo_target_city and geo_target_region return resource names
            # like "geoTargetConstants/1025471" — resolve to human-readable names below
            location_resource = ""
            location_type = str(row.geographic_view.location_type) if row.geographic_view.location_type else "UNKNOWN"
            if hasattr(row.segments, 'geo_target_city') and row.segments.geo_target_city:
                location_resource = row.segments.geo_target_city
                location_type = "CITY"
            elif hasattr(row.segments, 'geo_target_region') and row.segments.geo_target_region:
                location_resource = row.segments.geo_target_region
                location_type = "REGION"

            if location_resource:
                resource_names_to_resolve.add(location_resource)

            raw_rows.append({
                "location_resource": location_resource,
                "location_type": location_type,
                "country_criterion_id": row.geographic_view.country_criterion_id,
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
        return []

    # Batch-resolve resource IDs → human-readable names
    name_map = _resolve_geo_target_names(client, customer_id, resource_names_to_resolve)

    results = []
    for r in raw_rows:
        res = r["location_resource"]
        # Use resolved name; fall back to resource name (strip prefix) if unresolved
        if res in name_map:
            location_name = name_map[res]
        elif res.startswith("geoTargetConstants/"):
            # Keep as resource name stripped of prefix as last resort
            location_name = res  # will be filtered by caller if needed
        else:
            location_name = res or f"geo:{r['country_criterion_id']}"

        if not location_name:
            continue  # skip unresolvable rows

        results.append({
            "location_type": r["location_type"],
            "location_name": location_name,
            "campaign_name": r["campaign_name"],
            "impressions": r["impressions"],
            "clicks": r["clicks"],
            "cost": r["cost"],
            "conversions": r["conversions"],
            "cpc": r["cpc"],
            "conversion_rate": r["conversion_rate"],
        })

    return results


# ── User Location (Physical) Performance ─────────────────────────────────────

def fetch_user_location_performance(days: int = 30) -> list:
    """
    Pull campaign performance from user_location_view — where users PHYSICALLY were
    when they clicked (not just targeted location). This reveals demand leaks: towns
    that generate clicks/conversions but are NOT in the campaign's current targeting.

    Returns list of dicts (same shape as fetch_geo_performance but view_type='physical').
    """
    try:
        client = _build_client()
    except Exception as e:
        logger.error(f"Failed to build client for user location performance: {e}")
        return []

    settings = get_settings()
    customer_id = settings.google_ads_customer_id
    service = client.get_service("GoogleAdsService")

    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=days)

    query = f"""
        SELECT
            campaign.name,
            campaign.resource_name,
            segments.geo_target_city,
            segments.geo_target_region,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions
        FROM user_location_view
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
            AND campaign.status = 'ENABLED'
            AND metrics.impressions > 0
        ORDER BY metrics.conversions DESC, metrics.clicks DESC
        LIMIT 300
    """

    raw_rows = []
    resource_names_to_resolve: set = set()
    try:
        response = service.search(customer_id=customer_id, query=query)
        for row in response:
            cost = (row.metrics.cost_micros or 0) / 1_000_000.0
            clicks = row.metrics.clicks or 0
            conversions = row.metrics.conversions or 0.0

            # segments.geo_target_city/region return resource names like geoTargetConstants/XXXXXX
            location_resource = ""
            location_type = "CITY"
            if hasattr(row.segments, 'geo_target_city') and row.segments.geo_target_city:
                location_resource = row.segments.geo_target_city
            elif hasattr(row.segments, 'geo_target_region') and row.segments.geo_target_region:
                location_resource = row.segments.geo_target_region
                location_type = "REGION"

            if not location_resource:
                continue  # skip unattributed rows

            resource_names_to_resolve.add(location_resource)
            raw_rows.append({
                "location_resource": location_resource,
                "location_type": location_type,
                "zip_code": "",  # user_location_view does not support geo_target_postal_code segment
                "campaign_name": row.campaign.name or "",
                "campaign_resource": row.campaign.resource_name or "",
                "impressions": row.metrics.impressions or 0,
                "clicks": clicks,
                "cost": round(cost, 2),
                "conversions": round(conversions, 2),
                "cpc": round(cost / clicks, 2) if clicks > 0 else 0.0,
                "conversion_rate": round(conversions / clicks * 100, 1) if clicks > 0 else 0.0,
                "view_type": "physical",
            })
    except Exception as e:
        logger.error(f"User location view query failed: {e}")
        return []

    # Batch-resolve resource IDs → human-readable names
    name_map = _resolve_geo_target_names(client, customer_id, resource_names_to_resolve)

    results = []
    for r in raw_rows:
        res = r["location_resource"]
        location_name = name_map.get(res, "")
        if not location_name:
            continue  # skip rows whose resource ID couldn't be resolved

        results.append({
            "location_type": r["location_type"],
            "location_name": location_name,
            "zip_code": r["zip_code"],
            "campaign_name": r["campaign_name"],
            "campaign_resource": r["campaign_resource"],
            "impressions": r["impressions"],
            "clicks": r["clicks"],
            "cost": r["cost"],
            "conversions": r["conversions"],
            "cpc": r["cpc"],
            "conversion_rate": r["conversion_rate"],
            "view_type": r["view_type"],
        })

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

    # Hour of day — use segments.hour (not hour_of_day) with ad_group resource
    hour_query = f"""
        SELECT
            segments.hour,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions
        FROM ad_group
        WHERE {date_filter}
            AND campaign.status = 'ENABLED'
        ORDER BY segments.hour
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

    by_hour = _agg_rows(by_hour_raw, "hour")
    by_hour.sort(key=lambda x: int(x.get("hour", 0)))
    for h in by_hour:
        h["hour"] = int(h.get("hour", 0))

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
                "status": (getattr(row.search_term_view.status, 'name', None) or str(row.search_term_view.status) or "NONE"),
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
