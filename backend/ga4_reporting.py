"""
GA4 Data API — pulls analytics reports for the marketing dashboard.

Multi-property architecture:
  Every report queries ALL properties in GA4_PROPERTIES and merges results.
  Adding a new domain to GA4_PROPERTIES in .env is the ONLY step needed to
  include it end-to-end across every report (overview, devices, pages, traffic,
  events, trends, campaign metrics, leads by campaign).

  .env config:
    GA4_PROPERTIES={"nxtsmile.com": "531016678", "graftondentalcare.com": "536128204"}
    GA4_SERVICE_ACCOUNT_JSON=/path/to/service-account-key.json
    # The SA must have Viewer access on every property in GA4_PROPERTIES.

  To add a new property:
    1. Add it to GA4_PROPERTIES in .env: {"newdomain.com": "NUMERIC_PROPERTY_ID", ...}
    2. Grant the service account Viewer access on that GA4 property
    3. Restart the backend — no code changes required

Legacy single-property fallback (GA4_PROPERTY_ID) is still supported but
GA4_PROPERTIES takes priority.
"""

import logging
import json
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import urlparse
from config import get_settings

logger = logging.getLogger(__name__)


# ── Credentials & client ───────────────────────────────────────────────────────

def _get_credentials():
    """Load GA4 service account credentials, or None if not configured."""
    settings = get_settings()
    if not settings.ga4_service_account_json:
        return None
    try:
        from google.oauth2 import service_account
        return service_account.Credentials.from_service_account_file(
            settings.ga4_service_account_json,
            scopes=["https://www.googleapis.com/auth/analytics.readonly"],
        )
    except FileNotFoundError:
        logger.warning(f"GA4 service account JSON not found: {settings.ga4_service_account_json}")
        return None
    except Exception as e:
        logger.warning(f"GA4 credentials load failed: {e}")
        return None


def _make_client(credentials):
    """Create a BetaAnalyticsDataClient from credentials."""
    try:
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        return BetaAnalyticsDataClient(credentials=credentials)
    except ImportError:
        logger.warning("google-analytics-data not installed — pip install google-analytics-data")
        return None


def _get_property_map() -> dict:
    """
    Return {domain: numeric_property_id} from GA4_PROPERTIES env var.
    Falls back to {"_default": GA4_PROPERTY_ID} for single-property legacy mode.
    Example: {"nxtsmile.com": "531016678", "graftondentalcare.com": "536128204"}
    """
    settings = get_settings()
    try:
        props = json.loads(settings.ga4_properties) if settings.ga4_properties else {}
        if props:
            return props
    except (json.JSONDecodeError, AttributeError):
        pass
    if settings.ga4_property_id:
        return {"_default": settings.ga4_property_id}
    return {}


def _get_all_clients() -> list:
    """
    Return list of (domain, client, property_path) for every configured property.
    This is the central entry point — all multi-property reports use this.
    """
    prop_map = _get_property_map()
    if not prop_map:
        return []
    credentials = _get_credentials()
    if not credentials:
        return []
    client = _make_client(credentials)
    if not client:
        return []
    return [
        (domain if domain != "_default" else "default", client, f"properties/{pid}")
        for domain, pid in prop_map.items()
    ]


# ── Generic report runner ──────────────────────────────────────────────────────

def _run_report(client, property_path: str, dimensions: list, metrics: list,
                date_range_days: int = 30, dimension_filter=None, limit: int = 0) -> list:
    """Run a single GA4 Data API report and return rows as list of dicts."""
    from google.analytics.data_v1beta.types import (
        RunReportRequest, DateRange, Dimension, Metric,
    )
    start_date = (datetime.now() - timedelta(days=date_range_days)).strftime("%Y-%m-%d")
    request = RunReportRequest(
        property=property_path,
        date_ranges=[DateRange(start_date=start_date, end_date="today")],
        dimensions=[Dimension(name=d) for d in dimensions],
        metrics=[Metric(name=m) for m in metrics],
    )
    if dimension_filter:
        request.dimension_filter = dimension_filter
    if limit:
        request.limit = limit

    response = client.run_report(request)
    rows = []
    for row in response.rows:
        entry = {}
        for i, dim in enumerate(dimensions):
            entry[dim] = row.dimension_values[i].value
        for i, met in enumerate(metrics):
            val = row.metric_values[i].value
            try:
                entry[met] = float(val)
            except (ValueError, TypeError):
                entry[met] = val
        rows.append(entry)
    return rows


def _run_report_all_properties(dimensions: list, metrics: list,
                                date_range_days: int = 30,
                                dimension_filter=None, limit: int = 0) -> list:
    """
    Run the same report across ALL configured properties and return merged rows,
    each annotated with 'property_domain'.
    """
    all_rows = []
    for domain, client, property_path in _get_all_clients():
        try:
            rows = _run_report(client, property_path, dimensions, metrics,
                               date_range_days, dimension_filter, limit)
            for row in rows:
                row["property_domain"] = domain
            all_rows.extend(rows)
            logger.debug(f"GA4 [{domain}] report OK — {len(rows)} rows")
        except Exception as e:
            logger.error(f"GA4 [{domain}] report failed: {e}")
    return all_rows


# ── Numeric aggregation helpers ────────────────────────────────────────────────

def _sum_metric(rows: list, key: str) -> float:
    return sum(r.get(key, 0) or 0 for r in rows)

def _weighted_avg(rows: list, value_key: str, weight_key: str) -> float:
    total_weight = _sum_metric(rows, weight_key)
    if not total_weight:
        return 0.0
    return sum((r.get(value_key, 0) or 0) * (r.get(weight_key, 0) or 0) for r in rows) / total_weight


# ── Per-report fetchers ────────────────────────────────────────────────────────

def fetch_site_overview(days: int = 30) -> dict:
    """
    High-level website metrics merged across all properties.
    Sessions, engagement, bounce rate, etc.
    """
    clients = _get_all_clients()
    if not clients:
        return {"error": "GA4 Data API not configured", "configured": False}

    try:
        from google.analytics.data_v1beta.types import RunReportRequest, DateRange, Metric

        metric_names = [
            "sessions", "engagedSessions", "averageSessionDuration",
            "bounceRate", "screenPageViewsPerSession", "newUsers",
            "totalUsers", "conversions", "userEngagementDuration",
        ]

        # Fetch from each property and aggregate
        totals = {m: 0.0 for m in metric_names}
        property_count = 0

        for domain, client, property_path in clients:
            try:
                start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
                request = RunReportRequest(
                    property=property_path,
                    date_ranges=[DateRange(start_date=start_date, end_date="today")],
                    metrics=[Metric(name=m) for m in metric_names],
                )
                response = client.run_report(request)
                if not response.rows:
                    continue
                row = response.rows[0]
                sessions = float(row.metric_values[0].value or 0)
                for i, name in enumerate(metric_names):
                    val = float(row.metric_values[i].value or 0)
                    # averageSessionDuration and bounceRate are weighted averages — accumulate weighted sum
                    if name in ("averageSessionDuration", "bounceRate", "screenPageViewsPerSession"):
                        totals[name] += val * sessions  # weighted by sessions; divide at end
                    else:
                        totals[name] += val
                property_count += 1
                logger.debug(f"GA4 overview [{domain}]: {sessions:.0f} sessions")
            except Exception as e:
                logger.error(f"GA4 overview [{domain}] failed: {e}")

        if property_count == 0:
            return {"configured": True, "no_data": True}

        # Finalise weighted averages
        total_sessions = totals.get("sessions", 0) or 1
        for key in ("averageSessionDuration", "bounceRate", "screenPageViewsPerSession"):
            totals[key] = totals[key] / total_sessions

        result = {"configured": True}
        for name in metric_names:
            result[name] = totals[name]

        sessions = result.get("sessions", 0)
        engaged = result.get("engagedSessions", 0)
        result["engagement_rate"] = round((engaged / sessions * 100) if sessions > 0 else 0, 1)
        result["avg_session_duration_formatted"] = _format_duration(result.get("averageSessionDuration", 0))
        result["bounce_rate_pct"] = round(result.get("bounceRate", 0) * 100, 1)
        result["pages_per_session"] = round(result.get("screenPageViewsPerSession", 0), 1)
        result["properties_queried"] = property_count

        return result

    except Exception as e:
        logger.error(f"GA4 site overview failed: {e}")
        return {"error": str(e), "configured": True}


def fetch_device_split(days: int = 30) -> dict:
    """Traffic by device across all properties — sums sessions per device type."""
    clients = _get_all_clients()
    if not clients:
        return {"error": "GA4 not configured"}

    try:
        all_rows = _run_report_all_properties(
            dimensions=["deviceCategory"],
            metrics=["sessions", "engagedSessions", "conversions"],
            date_range_days=days,
        )

        # Merge by device
        merged: dict = {}
        for row in all_rows:
            device = row["deviceCategory"].lower()
            if device not in merged:
                merged[device] = {"sessions": 0, "engaged": 0, "conversions": 0}
            merged[device]["sessions"]    += row.get("sessions", 0)
            merged[device]["engaged"]     += row.get("engagedSessions", 0)
            merged[device]["conversions"] += row.get("conversions", 0)

        total = sum(v["sessions"] for v in merged.values())
        for device, v in merged.items():
            v["pct"] = round((v["sessions"] / total * 100) if total > 0 else 0, 1)
            v["sessions"] = int(v["sessions"])
            v["engaged"] = int(v["engaged"])
            v["conversions"] = int(v["conversions"])

        return merged

    except Exception as e:
        logger.error(f"GA4 device split failed: {e}")
        return {"error": str(e)}


def fetch_top_landing_pages(days: int = 30, limit: int = 10) -> list:
    """Top landing pages by sessions across all properties."""
    try:
        all_rows = _run_report_all_properties(
            dimensions=["landingPagePlusQueryString"],
            metrics=["sessions", "engagedSessions", "averageSessionDuration",
                     "bounceRate", "conversions"],
            date_range_days=days,
        )

        # Merge by (domain, page) so pages from different properties stay distinguishable
        merged: dict = {}
        for row in all_rows:
            domain = row.get("property_domain", "")
            page   = row.get("landingPagePlusQueryString", "")
            key    = f"{domain}::{page}"
            sessions = row.get("sessions", 0)
            if key not in merged:
                merged[key] = {
                    "property_domain": domain,
                    "page": page,
                    "sessions": 0,
                    "_duration_sum": 0.0,
                    "_bounce_sum": 0.0,
                    "conversions": 0,
                    "engaged": 0,
                }
            merged[key]["sessions"]      += sessions
            merged[key]["engaged"]       += row.get("engagedSessions", 0)
            merged[key]["conversions"]   += row.get("conversions", 0)
            merged[key]["_duration_sum"] += row.get("averageSessionDuration", 0) * sessions
            merged[key]["_bounce_sum"]   += row.get("bounceRate", 0) * sessions

        result = []
        for v in merged.values():
            sessions = v["sessions"]
            engaged  = v["engaged"]
            result.append({
                "property_domain":  v["property_domain"],
                "page":             v["page"],
                "sessions":         int(sessions),
                "engagement_rate":  round((engaged / sessions * 100) if sessions > 0 else 0, 1),
                "avg_duration":     _format_duration(v["_duration_sum"] / sessions if sessions else 0),
                "bounce_rate":      round((v["_bounce_sum"] / sessions * 100) if sessions > 0 else 0, 1),
                "conversions":      int(v["conversions"]),
            })

        result.sort(key=lambda r: r["sessions"], reverse=True)
        return result[:limit]

    except Exception as e:
        logger.error(f"GA4 landing pages failed: {e}")
        return []


def fetch_traffic_sources(days: int = 30, limit: int = 10) -> list:
    """Traffic sources (source/medium) merged across all properties."""
    try:
        all_rows = _run_report_all_properties(
            dimensions=["sessionSourceMedium"],
            metrics=["sessions", "engagedSessions", "averageSessionDuration",
                     "conversions", "newUsers"],
            date_range_days=days,
        )

        # Merge by source/medium across properties
        merged: dict = {}
        for row in all_rows:
            key = row.get("sessionSourceMedium", "")
            sessions = row.get("sessions", 0)
            if key not in merged:
                merged[key] = {"sessions": 0, "_dur_sum": 0.0, "conversions": 0,
                               "new_users": 0, "engaged": 0}
            merged[key]["sessions"]   += sessions
            merged[key]["engaged"]    += row.get("engagedSessions", 0)
            merged[key]["conversions"]+= row.get("conversions", 0)
            merged[key]["new_users"]  += row.get("newUsers", 0)
            merged[key]["_dur_sum"]   += row.get("averageSessionDuration", 0) * sessions

        result = []
        for source, v in merged.items():
            sessions = v["sessions"]
            engaged  = v["engaged"]
            result.append({
                "source_medium":    source,
                "sessions":         int(sessions),
                "new_users":        int(v["new_users"]),
                "engagement_rate":  round((engaged / sessions * 100) if sessions > 0 else 0, 1),
                "avg_duration":     _format_duration(v["_dur_sum"] / sessions if sessions else 0),
                "conversions":      int(v["conversions"]),
            })

        result.sort(key=lambda r: r["sessions"], reverse=True)
        return result[:limit]

    except Exception as e:
        logger.error(f"GA4 traffic sources failed: {e}")
        return []


def fetch_event_counts(days: int = 30) -> dict:
    """Key event counts summed across all properties."""
    try:
        all_rows = _run_report_all_properties(
            dimensions=["eventName"],
            metrics=["eventCount"],
            date_range_days=days,
        )

        # Sum event counts across all properties
        events: dict = {}
        for row in all_rows:
            name = row["eventName"]
            events[name] = events.get(name, 0) + int(row.get("eventCount", 0))

        result = {
            "page_view":             events.get("page_view", 0),
            "smile_widget_click":    events.get("smile_widget_click", 0),
            "smile_started":         events.get("smile_started", 0) + events.get("smile_start", 0),
            "smile_submitted":       events.get("smile_submitted", 0) + events.get("smile_completed", 0) + events.get("smile_complete", 0),
            "form_submit":           events.get("form_submit", 0) + events.get("generate_lead", 0),
            "chat_opened":           events.get("chat_opened", 0),
            "chat_lead_captured":    events.get("chat_lead_captured", 0),
            "phone_click":           events.get("phone_click", 0) + events.get("click_to_call", 0),
            "scroll":                events.get("scroll", 0),
            "first_visit":           events.get("first_visit", 0),
            "session_start":         events.get("session_start", 0),
        }

        sessions = result.get("session_start", 0) or 1
        result["smile_click_rate"]    = round(result["smile_widget_click"] / sessions * 100, 1)
        result["smile_start_rate"]    = round(result["smile_started"]      / sessions * 100, 1)
        result["smile_complete_rate"] = round(result["smile_submitted"]     / sessions * 100, 1)
        result["form_submit_rate"]    = round(result["form_submit"]         / sessions * 100, 1)
        result["chat_open_rate"]      = round(result["chat_opened"]         / sessions * 100, 1)

        return result

    except Exception as e:
        logger.error(f"GA4 event counts failed: {e}")
        return {}


def fetch_daily_trend(days: int = 30) -> list:
    """Daily sessions and engagement merged across all properties."""
    try:
        all_rows = _run_report_all_properties(
            dimensions=["date"],
            metrics=["sessions", "engagedSessions", "newUsers", "conversions"],
            date_range_days=days,
        )

        # Sum by date across properties
        merged: dict = {}
        for row in all_rows:
            date = row.get("date", "")
            if date not in merged:
                merged[date] = {"sessions": 0, "engaged": 0, "new_users": 0, "conversions": 0}
            merged[date]["sessions"]    += row.get("sessions", 0)
            merged[date]["engaged"]     += row.get("engagedSessions", 0)
            merged[date]["new_users"]   += row.get("newUsers", 0)
            merged[date]["conversions"] += row.get("conversions", 0)

        result = [
            {
                "date":        date,
                "sessions":    int(v["sessions"]),
                "engaged":     int(v["engaged"]),
                "new_users":   int(v["new_users"]),
                "conversions": int(v["conversions"]),
            }
            for date, v in sorted(merged.items())
        ]
        return result

    except Exception as e:
        logger.error(f"GA4 daily trend failed: {e}")
        return []


def fetch_campaign_ga4_metrics(days: int = 30) -> list:
    """Campaign-level GA4 metrics (sessions, engagement) merged across all properties."""
    try:
        all_rows = _run_report_all_properties(
            dimensions=["sessionCampaignName"],
            metrics=["sessions", "engagedSessions", "averageSessionDuration",
                     "bounceRate", "conversions", "newUsers"],
            date_range_days=days,
        )

        # Merge by campaign name across properties
        merged: dict = {}
        for row in all_rows:
            campaign = row.get("sessionCampaignName", "")
            if not campaign or campaign == "(not set)":
                continue
            sessions = row.get("sessions", 0)
            if campaign not in merged:
                merged[campaign] = {"sessions": 0, "engaged": 0, "new_users": 0,
                                    "conversions": 0, "_dur_sum": 0.0, "_bounce_sum": 0.0}
            merged[campaign]["sessions"]    += sessions
            merged[campaign]["engaged"]     += row.get("engagedSessions", 0)
            merged[campaign]["new_users"]   += row.get("newUsers", 0)
            merged[campaign]["conversions"] += row.get("conversions", 0)
            merged[campaign]["_dur_sum"]    += row.get("averageSessionDuration", 0) * sessions
            merged[campaign]["_bounce_sum"] += row.get("bounceRate", 0) * sessions

        result = []
        for campaign, v in merged.items():
            sessions = v["sessions"]
            engaged  = v["engaged"]
            result.append({
                "campaign":        campaign,
                "sessions":        int(sessions),
                "new_users":       int(v["new_users"]),
                "engagement_rate": round((engaged / sessions * 100) if sessions > 0 else 0, 1),
                "avg_duration":    _format_duration(v["_dur_sum"] / sessions if sessions else 0),
                "bounce_rate":     round((v["_bounce_sum"] / sessions * 100) if sessions > 0 else 0, 1),
                "conversions":     int(v["conversions"]),
            })

        result.sort(key=lambda r: r["sessions"], reverse=True)
        return result

    except Exception as e:
        logger.error(f"GA4 campaign metrics failed: {e}")
        return []


def fetch_leads_by_campaign(days: int = 30) -> list:
    """
    generate_lead events broken down by campaign/ad group/keyword/landing page,
    queried across ALL configured GA4 properties.

    Each row includes 'property_domain' so you know which site the lead came from.
    Only rows with at least one generate_lead event are returned.
    """
    try:
        from google.analytics.data_v1beta.types import (
            FilterExpression, Filter,
        )
    except ImportError:
        logger.warning("google-analytics-data not installed")
        return []

    try:
        all_rows = _run_report_all_properties(
            dimensions=[
                "sessionGoogleAdsCampaignName",
                "sessionGoogleAdsAdGroupName",
                "sessionGoogleAdsKeyword",
                "landingPagePlusQueryString",
            ],
            metrics=["eventCount", "sessions"],
            date_range_days=days,
            dimension_filter=FilterExpression(
                filter=Filter(
                    field_name="eventName",
                    string_filter=Filter.StringFilter(value="generate_lead"),
                )
            ),
            limit=200,
        )

        result = []
        for row in all_rows:
            campaign = row.get("sessionGoogleAdsCampaignName", "")
            if not campaign or campaign in ("(not set)", ""):
                continue
            lead_count = int(row.get("eventCount", 0))
            sessions   = int(row.get("sessions", 0))
            ad_group   = row.get("sessionGoogleAdsAdGroupName", "")
            keyword    = row.get("sessionGoogleAdsKeyword", "")
            landing    = row.get("landingPagePlusQueryString", "")
            result.append({
                "property_domain": row.get("property_domain", ""),
                "campaign":        campaign,
                "ad_group":        ad_group  if ad_group  not in ("(not set)", "") else "",
                "keyword":         keyword   if keyword   not in ("(not set)", "") else "",
                "landing_page":    landing   if landing   not in ("(not set)", "") else "/",
                "leads":           lead_count,
                "sessions":        sessions,
                "lead_rate":       round((lead_count / sessions * 100) if sessions > 0 else 0, 1),
            })

        result.sort(key=lambda r: r["leads"], reverse=True)
        return result

    except Exception as e:
        logger.error(f"GA4 fetch_leads_by_campaign failed: {e}")
        return []


# ── Master fetcher ─────────────────────────────────────────────────────────────

def fetch_all_ga4_data(days: int = 30) -> dict:
    """
    Pull all GA4 reports across every configured property and return merged result.
    Called by the /api/admin/ga4 endpoint and the nightly scheduled job.

    To add a new property: update GA4_PROPERTIES in .env and restart — no code changes needed.
    """
    prop_map = _get_property_map()
    prop_domains = [d for d in prop_map.keys() if d != "_default"]
    logger.info(f"Fetching GA4 analytics data ({days} days) across {len(prop_map)} property/properties: {prop_domains}")

    result = {
        "fetched_at":       datetime.now(timezone.utc).isoformat(),
        "days":             days,
        "properties":       prop_domains,   # surfaced in UI so you know which sites are included
        "overview":         fetch_site_overview(days),
        "device_split":     fetch_device_split(days),
        "top_pages":        fetch_top_landing_pages(days),
        "traffic_sources":  fetch_traffic_sources(days),
        "events":           fetch_event_counts(days),
        "daily_trend":      fetch_daily_trend(days),
        "campaign_metrics": fetch_campaign_ga4_metrics(days),
        "leads_by_campaign":fetch_leads_by_campaign(days),
    }

    configured = result["overview"].get("configured", False)
    has_error  = result["overview"].get("error")

    if configured and not has_error:
        logger.info(
            f"GA4 data fetched across {len(prop_map)} property/properties: "
            f"{result['overview'].get('sessions', 0):.0f} sessions, "
            f"{result['overview'].get('engagement_rate', 0)}% engagement rate"
        )
    elif has_error:
        logger.warning(f"GA4 fetch completed with error: {has_error}")
    else:
        logger.info("GA4 Data API not configured — returning empty data")

    return result


# ── URL helpers (used by google_ads_create.py) ────────────────────────────────

def _domain_for_url(url: str) -> str:
    """Extract bare domain from a URL string, e.g. 'https://nxtsmile.com/page' → 'nxtsmile.com'."""
    if not url:
        return ""
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        netloc = parsed.netloc
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc
    except Exception:
        return ""


def _property_id_for_url(url: str) -> Optional[str]:
    """
    Given a landing page URL, return the GA4 numeric property ID for its domain.
    Returns None if the domain isn't in GA4_PROPERTIES.
    """
    domain   = _domain_for_url(url)
    prop_map = _get_property_map()
    if domain in prop_map:
        return prop_map[domain]
    for d, pid in prop_map.items():
        if d != "_default" and domain.endswith("." + d):
            return pid
    return prop_map.get("_default")


# ── Formatting ─────────────────────────────────────────────────────────────────

def _format_duration(seconds: float) -> str:
    """Format seconds into '2m 34s'."""
    try:
        s = int(float(seconds))
        if s < 60:
            return f"{s}s"
        m, s = divmod(s, 60)
        return f"{m}m {s:02d}s"
    except (ValueError, TypeError):
        return "0s"


if __name__ == "__main__":
    """Quick test — run from command line to verify GA4 connection."""
    import logging
    logging.basicConfig(level=logging.INFO)
    data = fetch_all_ga4_data(days=7)
    print(json.dumps(data, indent=2, default=str))
