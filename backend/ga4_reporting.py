"""
GA4 Data API — pulls analytics reports for the marketing dashboard.

Fetches website engagement metrics that help optimize ad spend:
  - Session duration, bounce rate, pages per session
  - Device split (mobile vs desktop)
  - Top landing pages with conversion rates
  - Smile tool engagement funnel
  - Traffic by source/medium
  - Daily/weekly trends
  - Lead events (generate_lead) broken down by campaign/ad group/keyword

Multi-property setup:
  Each landing page domain has its own GA4 property. Configure in .env:
    GA4_PROPERTIES={"nxtsmile.com": "531016678", "graftondentalcare.com": "536128204"}
  Add new domains here as new landing pages/domains are created.
  GA4_SERVICE_ACCOUNT_JSON must be a service account with Viewer access
  on ALL configured properties.

Single-property setup (legacy fallback):
  GA4_PROPERTY_ID=531016678
  GA4_SERVICE_ACCOUNT_JSON=/path/to/key.json
"""

import logging
import json
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import urlparse
from config import get_settings

logger = logging.getLogger(__name__)


def _get_credentials():
    """Load GA4 service account credentials object, or None if not configured."""
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


def _get_property_map() -> dict:
    """
    Return {domain: property_id} from GA4_PROPERTIES env var.
    Falls back to {None: GA4_PROPERTY_ID} for single-property legacy mode.
    Example: {"nxtsmile.com": "531016678", "graftondentalcare.com": "536128204"}
    """
    settings = get_settings()
    try:
        props = json.loads(settings.ga4_properties) if settings.ga4_properties else {}
        if props:
            return props
    except (json.JSONDecodeError, AttributeError):
        pass
    # Legacy single-property fallback
    if settings.ga4_property_id:
        return {"_default": settings.ga4_property_id}
    return {}


def _domain_for_url(url: str) -> str:
    """Extract bare domain from a URL string, e.g. 'https://nxtsmile.com/page' → 'nxtsmile.com'.
    Uses explicit prefix strip (not lstrip) to avoid character-set mangling."""
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
    Suffix match only goes one direction (domain ends with key), never reverse,
    to avoid wrong-property matches (e.g. 'dental.com' matching 'graftondental.com').
    """
    domain = _domain_for_url(url)
    prop_map = _get_property_map()
    # Exact match first
    if domain in prop_map:
        return prop_map[domain]
    # Suffix match — handles subdomains like sub.nxtsmile.com → nxtsmile.com
    for d, pid in prop_map.items():
        if d != "_default" and domain.endswith("." + d):
            return pid
    # Default fallback
    return prop_map.get("_default")


def _make_client(credentials):
    """Create a BetaAnalyticsDataClient from credentials."""
    try:
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        return BetaAnalyticsDataClient(credentials=credentials)
    except ImportError:
        logger.warning("google-analytics-data not installed — pip install google-analytics-data")
        return None


def _get_client():
    """Create GA4 Data API client from service account credentials (legacy single-property)."""
    settings = get_settings()
    if not settings.ga4_property_id or not settings.ga4_service_account_json:
        logger.debug("GA4 Data API not configured — skipping")
        return None, None

    try:
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        from google.oauth2 import service_account

        credentials = service_account.Credentials.from_service_account_file(
            settings.ga4_service_account_json,
            scopes=["https://www.googleapis.com/auth/analytics.readonly"],
        )
        client = BetaAnalyticsDataClient(credentials=credentials)
        property_id = f"properties/{settings.ga4_property_id}"
        return client, property_id

    except ImportError:
        logger.warning("google-analytics-data not installed — pip install google-analytics-data")
        return None, None
    except FileNotFoundError:
        logger.warning(f"GA4 service account JSON not found: {settings.ga4_service_account_json}")
        return None, None
    except Exception as e:
        logger.warning(f"GA4 Data API client init failed: {e}")
        return None, None


def _run_report(client, property_id: str, dimensions: list, metrics: list,
                date_range_days: int = 30, dimension_filter=None) -> list:
    """Generic helper to run a GA4 Data API report."""
    from google.analytics.data_v1beta.types import (
        RunReportRequest, DateRange, Dimension, Metric,
    )

    start_date = (datetime.now() - timedelta(days=date_range_days)).strftime("%Y-%m-%d")

    request = RunReportRequest(
        property=property_id,
        date_ranges=[DateRange(start_date=start_date, end_date="today")],
        dimensions=[Dimension(name=d) for d in dimensions],
        metrics=[Metric(name=m) for m in metrics],
    )
    if dimension_filter:
        request.dimension_filter = dimension_filter

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


def fetch_site_overview(days: int = 30) -> dict:
    """
    Pull high-level website metrics for the dashboard.
    Returns: {sessions, engaged_sessions, avg_session_duration, bounce_rate,
              pages_per_session, new_users, total_users}
    """
    client, property_id = _get_client()
    if not client:
        return {"error": "GA4 Data API not configured", "configured": False}

    try:
        from google.analytics.data_v1beta.types import (
            RunReportRequest, DateRange, Metric,
        )

        start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        request = RunReportRequest(
            property=property_id,
            date_ranges=[DateRange(start_date=start_date, end_date="today")],
            metrics=[
                Metric(name="sessions"),
                Metric(name="engagedSessions"),
                Metric(name="averageSessionDuration"),
                Metric(name="bounceRate"),
                Metric(name="screenPageViewsPerSession"),
                Metric(name="newUsers"),
                Metric(name="totalUsers"),
                Metric(name="conversions"),
                Metric(name="userEngagementDuration"),
            ],
        )
        response = client.run_report(request)

        if not response.rows:
            return {"configured": True, "no_data": True}

        row = response.rows[0]
        metrics_list = [
            "sessions", "engagedSessions", "averageSessionDuration", "bounceRate",
            "screenPageViewsPerSession", "newUsers", "totalUsers",
            "conversions", "userEngagementDuration",
        ]

        result = {"configured": True}
        for i, name in enumerate(metrics_list):
            try:
                result[name] = float(row.metric_values[i].value)
            except (ValueError, TypeError):
                result[name] = 0.0

        # Derived metrics
        sessions = result.get("sessions", 0)
        engaged = result.get("engagedSessions", 0)
        result["engagement_rate"] = round((engaged / sessions * 100) if sessions > 0 else 0, 1)
        result["avg_session_duration_formatted"] = _format_duration(result.get("averageSessionDuration", 0))
        result["bounce_rate_pct"] = round(result.get("bounceRate", 0) * 100, 1)
        result["pages_per_session"] = round(result.get("screenPageViewsPerSession", 0), 1)

        return result

    except Exception as e:
        logger.error(f"GA4 site overview failed: {e}")
        return {"error": str(e), "configured": True}


def fetch_device_split(days: int = 30) -> dict:
    """
    Returns traffic breakdown by device category.
    Result: {desktop: {sessions, pct}, mobile: {sessions, pct}, tablet: {sessions, pct}}
    """
    client, property_id = _get_client()
    if not client:
        return {"error": "GA4 not configured"}

    try:
        rows = _run_report(
            client, property_id,
            dimensions=["deviceCategory"],
            metrics=["sessions", "engagedSessions", "conversions"],
            date_range_days=days,
        )

        total = sum(r.get("sessions", 0) for r in rows)
        result = {}
        for row in rows:
            device = row["deviceCategory"].lower()
            sessions = row.get("sessions", 0)
            result[device] = {
                "sessions": int(sessions),
                "engaged": int(row.get("engagedSessions", 0)),
                "conversions": int(row.get("conversions", 0)),
                "pct": round((sessions / total * 100) if total > 0 else 0, 1),
            }

        return result

    except Exception as e:
        logger.error(f"GA4 device split failed: {e}")
        return {"error": str(e)}


def fetch_top_landing_pages(days: int = 30, limit: int = 10) -> list:
    """
    Top landing pages by sessions with engagement metrics.
    Useful for knowing which pages convert best.
    """
    client, property_id = _get_client()
    if not client:
        return []

    try:
        rows = _run_report(
            client, property_id,
            dimensions=["landingPagePlusQueryString"],
            metrics=["sessions", "engagedSessions", "averageSessionDuration",
                     "bounceRate", "conversions"],
            date_range_days=days,
        )

        # Sort by sessions desc
        rows.sort(key=lambda r: r.get("sessions", 0), reverse=True)

        result = []
        for row in rows[:limit]:
            sessions = row.get("sessions", 0)
            engaged = row.get("engagedSessions", 0)
            result.append({
                "page": row.get("landingPagePlusQueryString", ""),
                "sessions": int(sessions),
                "engagement_rate": round((engaged / sessions * 100) if sessions > 0 else 0, 1),
                "avg_duration": _format_duration(row.get("averageSessionDuration", 0)),
                "bounce_rate": round(row.get("bounceRate", 0) * 100, 1),
                "conversions": int(row.get("conversions", 0)),
            })

        return result

    except Exception as e:
        logger.error(f"GA4 landing pages failed: {e}")
        return []


def fetch_traffic_sources(days: int = 30, limit: int = 10) -> list:
    """
    Traffic sources by source/medium — correlates with ad spend.
    Shows which channels bring the most engaged visitors.
    """
    client, property_id = _get_client()
    if not client:
        return []

    try:
        rows = _run_report(
            client, property_id,
            dimensions=["sessionSourceMedium"],
            metrics=["sessions", "engagedSessions", "averageSessionDuration",
                     "conversions", "newUsers"],
            date_range_days=days,
        )

        rows.sort(key=lambda r: r.get("sessions", 0), reverse=True)

        result = []
        for row in rows[:limit]:
            sessions = row.get("sessions", 0)
            engaged = row.get("engagedSessions", 0)
            result.append({
                "source_medium": row.get("sessionSourceMedium", ""),
                "sessions": int(sessions),
                "new_users": int(row.get("newUsers", 0)),
                "engagement_rate": round((engaged / sessions * 100) if sessions > 0 else 0, 1),
                "avg_duration": _format_duration(row.get("averageSessionDuration", 0)),
                "conversions": int(row.get("conversions", 0)),
            })

        return result

    except Exception as e:
        logger.error(f"GA4 traffic sources failed: {e}")
        return []


def fetch_event_counts(days: int = 30) -> dict:
    """
    Count of key events — smile tool starts/completions, form submissions, phone clicks.
    This tells us the conversion funnel on the landing page itself.
    """
    client, property_id = _get_client()
    if not client:
        return {}

    try:
        rows = _run_report(
            client, property_id,
            dimensions=["eventName"],
            metrics=["eventCount"],
            date_range_days=days,
        )

        # Map all events to a dict
        events = {}
        for row in rows:
            events[row["eventName"]] = int(row.get("eventCount", 0))

        # Pull out the ones we care about
        result = {
            "page_view": events.get("page_view", 0),
            # Smile tool funnel — matches event names fired by smileTrack()
            "smile_widget_click": events.get("smile_widget_click", 0),
            "smile_started": events.get("smile_started", 0) + events.get("smile_start", 0),
            "smile_submitted": events.get("smile_submitted", 0) + events.get("smile_completed", 0) + events.get("smile_complete", 0),
            # Form and chat
            "form_submit": events.get("form_submit", 0) + events.get("generate_lead", 0),
            "chat_opened": events.get("chat_opened", 0),
            "chat_lead_captured": events.get("chat_lead_captured", 0),
            # Phone clicks
            "phone_click": events.get("phone_click", 0) + events.get("click_to_call", 0),
            "scroll": events.get("scroll", 0),
            "first_visit": events.get("first_visit", 0),
            "session_start": events.get("session_start", 0),
        }

        # Calculate funnel rates
        sessions = result.get("session_start", 0) or 1
        result["smile_click_rate"] = round(result["smile_widget_click"] / sessions * 100, 1)
        result["smile_start_rate"] = round(result["smile_started"] / sessions * 100, 1)
        result["smile_complete_rate"] = round(result["smile_submitted"] / sessions * 100, 1)
        result["form_submit_rate"] = round(result["form_submit"] / sessions * 100, 1)
        result["chat_open_rate"] = round(result["chat_opened"] / sessions * 100, 1)

        return result

    except Exception as e:
        logger.error(f"GA4 event counts failed: {e}")
        return {}


def fetch_daily_trend(days: int = 30) -> list:
    """
    Daily sessions and engagement trend — useful for spotting drops after ad changes.
    """
    client, property_id = _get_client()
    if not client:
        return []

    try:
        rows = _run_report(
            client, property_id,
            dimensions=["date"],
            metrics=["sessions", "engagedSessions", "newUsers", "conversions"],
            date_range_days=days,
        )

        rows.sort(key=lambda r: r.get("date", ""))

        return [{
            "date": row.get("date", ""),
            "sessions": int(row.get("sessions", 0)),
            "engaged": int(row.get("engagedSessions", 0)),
            "new_users": int(row.get("newUsers", 0)),
            "conversions": int(row.get("conversions", 0)),
        } for row in rows]

    except Exception as e:
        logger.error(f"GA4 daily trend failed: {e}")
        return []


def fetch_campaign_ga4_metrics(days: int = 30) -> list:
    """
    Campaign-level metrics from GA4 — complements Google Ads data with
    on-site engagement metrics per campaign.
    """
    client, property_id = _get_client()
    if not client:
        return []

    try:
        rows = _run_report(
            client, property_id,
            dimensions=["sessionCampaignName"],
            metrics=["sessions", "engagedSessions", "averageSessionDuration",
                     "bounceRate", "conversions", "newUsers"],
            date_range_days=days,
        )

        rows.sort(key=lambda r: r.get("sessions", 0), reverse=True)

        result = []
        for row in rows:
            campaign = row.get("sessionCampaignName", "")
            if not campaign or campaign == "(not set)":
                continue
            sessions = row.get("sessions", 0)
            engaged = row.get("engagedSessions", 0)
            result.append({
                "campaign": campaign,
                "sessions": int(sessions),
                "new_users": int(row.get("newUsers", 0)),
                "engagement_rate": round((engaged / sessions * 100) if sessions > 0 else 0, 1),
                "avg_duration": _format_duration(row.get("averageSessionDuration", 0)),
                "bounce_rate": round(row.get("bounceRate", 0) * 100, 1),
                "conversions": int(row.get("conversions", 0)),
            })

        return result

    except Exception as e:
        logger.error(f"GA4 campaign metrics failed: {e}")
        return []


def fetch_leads_by_campaign(days: int = 30) -> list:
    """
    Returns generate_lead event counts broken down by Google Ads campaign,
    ad group, keyword, and landing page — pulled across ALL configured GA4
    properties (one per landing page domain).

    Each property is queried independently and results are merged. The
    'property_domain' field tells you which site the lead came from.

    Returns a list of dicts like:
      {
        "property_domain": "nxtsmile.com",
        "campaign": "nXtSmile All-on-X Implants",
        "ad_group": "All on X",
        "keyword": "all on 4 implants near me",
        "landing_page": "/",
        "leads": 3,
        "sessions": 47,
        "lead_rate": 6.4   # leads / sessions %
      }
    Only rows where at least one generate_lead event fired are returned.
    Results are sorted by leads descending across all properties combined.
    """
    prop_map = _get_property_map()
    if not prop_map:
        logger.debug("GA4 multi-property: no properties configured — skipping fetch_leads_by_campaign")
        return []

    credentials = _get_credentials()
    if not credentials:
        return []

    client = _make_client(credentials)
    if not client:
        return []

    try:
        from google.analytics.data_v1beta.types import (
            RunReportRequest, DateRange, Dimension, Metric,
            FilterExpression, Filter,
        )
    except ImportError:
        logger.warning("google-analytics-data not installed")
        return []

    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    all_results = []

    for domain, prop_id in prop_map.items():
        property_path = f"properties/{prop_id}"
        display_domain = domain if domain != "_default" else "default"
        try:
            lead_request = RunReportRequest(
                property=property_path,
                date_ranges=[DateRange(start_date=start_date, end_date="today")],
                dimensions=[
                    Dimension(name="sessionGoogleAdsCampaignName"),
                    Dimension(name="sessionGoogleAdsAdGroupName"),
                    Dimension(name="sessionGoogleAdsKeyword"),
                    Dimension(name="landingPagePlusQueryString"),
                ],
                metrics=[
                    Metric(name="eventCount"),
                    Metric(name="sessions"),
                ],
                dimension_filter=FilterExpression(
                    filter=Filter(
                        field_name="eventName",
                        string_filter=Filter.StringFilter(value="generate_lead"),
                    )
                ),
                limit=200,
            )

            response = client.run_report(lead_request)

            for row in response.rows:
                dims = [d.value for d in row.dimension_values]
                campaign, ad_group, keyword, landing = dims[0], dims[1], dims[2], dims[3]

                # Skip fully unattributed rows
                if not campaign or campaign in ("(not set)", ""):
                    continue

                lead_count = int(row.metric_values[0].value or 0)
                sessions = int(row.metric_values[1].value or 0)

                all_results.append({
                    "property_domain": display_domain,
                    "campaign": campaign,
                    "ad_group": ad_group if ad_group not in ("(not set)", "") else "",
                    "keyword": keyword if keyword not in ("(not set)", "") else "",
                    "landing_page": landing if landing not in ("(not set)", "") else "/",
                    "leads": lead_count,
                    "sessions": sessions,
                    "lead_rate": round((lead_count / sessions * 100) if sessions > 0 else 0, 1),
                })

            logger.debug(f"GA4 [{display_domain}] lead query OK — {len(response.rows)} rows")

        except Exception as e:
            logger.error(f"GA4 fetch_leads_by_campaign failed for {display_domain} (property {prop_id}): {e}")

    # Sort combined results by lead count descending
    all_results.sort(key=lambda r: r["leads"], reverse=True)
    return all_results


def fetch_all_ga4_data(days: int = 30) -> dict:
    """
    Master function — pulls all GA4 reports and returns combined result.
    Called by the nightly scheduled job and the /api/admin/ga4 endpoint.
    """
    logger.info(f"Fetching GA4 analytics data ({days} days)...")

    result = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "days": days,
        "overview": fetch_site_overview(days),
        "device_split": fetch_device_split(days),
        "top_pages": fetch_top_landing_pages(days),
        "traffic_sources": fetch_traffic_sources(days),
        "events": fetch_event_counts(days),
        "daily_trend": fetch_daily_trend(days),
        "campaign_metrics": fetch_campaign_ga4_metrics(days),
        "leads_by_campaign": fetch_leads_by_campaign(days),
    }

    configured = result["overview"].get("configured", False)
    has_error = result["overview"].get("error")

    if configured and not has_error:
        logger.info(
            f"GA4 data fetched: {result['overview'].get('sessions', 0):.0f} sessions, "
            f"{result['overview'].get('engagement_rate', 0)}% engagement rate"
        )
    elif has_error:
        logger.warning(f"GA4 fetch completed with error: {has_error}")
    else:
        logger.info("GA4 Data API not configured — returning empty data")

    return result


def _format_duration(seconds: float) -> str:
    """Format seconds into human-readable duration like '2m 34s'."""
    try:
        s = int(float(seconds))
        if s < 60:
            return f"{s}s"
        m = s // 60
        s = s % 60
        return f"{m}m {s:02d}s"
    except (ValueError, TypeError):
        return "0s"


if __name__ == "__main__":
    """Quick test — run from command line to verify GA4 connection."""
    import logging
    logging.basicConfig(level=logging.INFO)

    data = fetch_all_ga4_data(days=7)
    print(json.dumps(data, indent=2, default=str))
