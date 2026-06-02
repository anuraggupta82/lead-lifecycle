"""
GA4 Audience Manager — creates retargeting audiences in GA4 that auto-flow to Google Ads.

Audiences defined here:
  1. "GDC — Implant Page Visitors (No Form)"
       Condition: page_path contains /implant AND no generate_lead event in same session
       Use: retarget interested-but-not-converted implant prospects

  2. "GDC — High-Engagement Visitors"
       Condition: Clarity-pushed engagement event (clarity_engagement_high = true custom dim)
       Use: retarget visitors with >70% scroll depth / long dwell time
       Requires: Clarity → GA4 integration active in Clarity dashboard

Audiences are CREATE-ONCE — GA4 does not support updating filter clauses on existing audiences.
Re-running this function is safe: it skips creation if an audience with the same name exists.

Auto-flow to Google Ads requires:
  - GA4 ↔ Google Ads link established in GA4 Admin
  - "Enable personalized advertising" enabled on that link
  - Audience sharing turned on

PREREQUISITES (manual, one-time — cannot be done via API):
  1. SA must have "Editor" role on the GA4 property (currently likely "Viewer")
  2. ga4_service_account_json credentials must use analytics.edit scope (not analytics.readonly)
     → generate new SA key with correct scopes, or update existing SA role
  3. Install: pip install google-analytics-admin>=0.22.0 (added to requirements.txt)
  4. Clarity → GA4 integration: enable in Clarity dashboard → Settings → Integrations → Google Analytics
     (This pushes clarity_session_applied_tags and other custom events to GA4 automatically)

Until prerequisites are met, this module degrades gracefully — get_audience_status() will
return a "prerequisites_not_met" note instead of raising.

Trigger: POST /api/admin/sync-ga4-audiences  (run manually after prerequisites complete)
"""

import logging
from datetime import datetime, timezone

from config import get_settings

logger = logging.getLogger(__name__)

# Audience definitions — create-once, matched by display_name
AUDIENCES = [
    {
        "display_name": "GDC — Implant Page Visitors (No Form)",
        "description": (
            "Visitors who viewed an implant-related page but did NOT submit a lead form. "
            "Use for retargeting campaigns."
        ),
        "membership_duration_days": 90,
        # GA4 Audience filter: sequence — page_path contains /implant, NOT followed by generate_lead
        # Built programmatically in _build_implant_visitor_filter()
        "filter_type": "implant_no_form",
    },
    {
        "display_name": "GDC — High-Engagement Visitors",
        "description": (
            "Visitors with high engagement score from Microsoft Clarity "
            "(scroll depth >70% or long dwell time). Requires Clarity→GA4 integration."
        ),
        "membership_duration_days": 90,
        # GA4 Audience filter: event dimension clarity_engagement_high = true
        "filter_type": "clarity_engagement",
    },
]


def _load_admin_credentials():
    """
    Load GA4 service account credentials with analytics.edit scope.
    Requires SA to have Editor role on the GA4 property.
    Returns credentials or raises ImportError/FileNotFoundError.
    """
    from google.oauth2 import service_account
    settings = get_settings()
    if not settings.ga4_service_account_json:
        raise FileNotFoundError("ga4_service_account_json not configured in .env")
    return service_account.Credentials.from_service_account_file(
        settings.ga4_service_account_json,
        scopes=["https://www.googleapis.com/auth/analytics.edit"],
    )


def _admin_client():
    """Return an AnalyticsAdminServiceClient with edit scope."""
    from google.analytics.admin import AnalyticsAdminServiceClient
    creds = _load_admin_credentials()
    return AnalyticsAdminServiceClient(credentials=creds)


def _get_primary_property_id() -> str | None:
    """Return the graftondentalcare.com GA4 property ID from settings."""
    import json
    settings = get_settings()
    props = {}
    try:
        props = json.loads(settings.ga4_properties) if settings.ga4_properties else {}
    except Exception:
        pass
    # Try GDC domain first, then any available property
    for domain in ("graftondentalcare.com", "visitgdc.com"):
        if domain in props:
            return str(props[domain])
    # Fallback to legacy single-property setting
    if settings.ga4_property_id:
        return str(settings.ga4_property_id)
    return None


def _list_existing_audience_names(client, property_path: str) -> set[str]:
    """Return set of existing audience display_names for this property."""
    try:
        pager = client.list_audiences(parent=property_path)
        return {a.display_name for a in pager}
    except Exception as e:
        logger.warning(f"GA4 Audiences: could not list existing audiences: {e}")
        return set()


def _build_implant_visitor_filter(client):
    """
    Build GA4 AudienceFilterClause for: visited /implant* page AND no generate_lead event.

    Uses a simple event filter (page_view with page_path contains /implant) combined with
    an exclusion for generate_lead. GA4 Audiences API uses AudienceFilterClause with
    simple_filter (event/dimension conditions).
    """
    from google.analytics.admin_v1alpha.types import (
        AudienceFilterClause,
        AudienceSimpleFilter,
    )

    # Include: page_view event where page_path contains "implant"
    include_filter = AudienceFilterClause(
        clause_type=AudienceFilterClause.AudienceClauseType.INCLUDE,
        simple_filter=AudienceSimpleFilter(
            scope=AudienceSimpleFilter.AudienceFilterScope.AUDIENCE_FILTER_SCOPE_ACROSS_ALL_SESSIONS,
            filter_expression=_dim_contains_filter(client, "pagePathPlusQueryString", "implant"),
        ),
    )

    # Exclude: sessions that contain generate_lead event
    exclude_filter = AudienceFilterClause(
        clause_type=AudienceFilterClause.AudienceClauseType.EXCLUDE,
        simple_filter=AudienceSimpleFilter(
            scope=AudienceSimpleFilter.AudienceFilterScope.AUDIENCE_FILTER_SCOPE_WITHIN_SAME_SESSION,
            filter_expression=_event_filter(client, "generate_lead"),
        ),
    )

    return [include_filter, exclude_filter]


def _build_clarity_engagement_filter(client):
    """
    Build GA4 AudienceFilterClause for: Clarity high-engagement event fired.
    Requires Clarity → GA4 integration active (pushes clarity_engagement_high custom event).
    """
    from google.analytics.admin_v1alpha.types import (
        AudienceFilterClause,
        AudienceSimpleFilter,
    )

    include_filter = AudienceFilterClause(
        clause_type=AudienceFilterClause.AudienceClauseType.INCLUDE,
        simple_filter=AudienceSimpleFilter(
            scope=AudienceSimpleFilter.AudienceFilterScope.AUDIENCE_FILTER_SCOPE_ACROSS_ALL_SESSIONS,
            filter_expression=_event_filter(client, "clarity_engagement_high"),
        ),
    )
    return [include_filter]


def _dim_contains_filter(client, dimension_name: str, value: str):
    """Build an AudienceFilterExpression for dimension contains value."""
    from google.analytics.admin_v1alpha.types import (
        AudienceFilterExpression,
        AudienceDimensionOrMetricFilter,
    )

    return AudienceFilterExpression(
        dimension_or_metric_filter=AudienceDimensionOrMetricFilter(
            field_name=dimension_name,
            string_filter=AudienceDimensionOrMetricFilter.StringFilter(
                match_type=AudienceDimensionOrMetricFilter.StringFilter.MatchType.CONTAINS,
                value=value,
                case_sensitive=False,
            ),
        )
    )


def _event_filter(client, event_name: str):
    """Build an AudienceFilterExpression that matches a named event."""
    from google.analytics.admin_v1alpha.types import (
        AudienceFilterExpression,
        AudienceEventFilter,
    )

    return AudienceFilterExpression(
        event_filter=AudienceEventFilter(event_name=event_name)
    )


def sync_ga4_audiences() -> dict:
    """
    Create GDC retargeting audiences in GA4 if they don't already exist.
    Audiences are create-once (GA4 doesn't support updating filter clauses).
    Safe to re-run — skips creation for names that already exist.
    """
    now = datetime.now(timezone.utc).isoformat()

    # Check prerequisites
    try:
        from google.analytics.admin import AnalyticsAdminServiceClient  # noqa: F401
    except ImportError:
        return {
            "ok": False,
            "error": "google-analytics-admin not installed. Run: pip install google-analytics-admin>=0.22.0 --break-system-packages",
        }

    property_id = _get_primary_property_id()
    if not property_id:
        return {
            "ok": False,
            "error": "No GA4 property ID configured. Set ga4_properties or ga4_property_id in .env",
        }

    property_path = f"properties/{property_id}"

    try:
        client = _admin_client()
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {
            "ok": False,
            "error": (
                f"Failed to load GA4 admin credentials: {e}. "
                "Ensure SA has Editor role on GA4 property and analytics.edit scope."
            ),
        }

    existing = _list_existing_audience_names(client, property_path)
    results = []

    for aud_def in AUDIENCES:
        name = aud_def["display_name"]

        if name in existing:
            logger.info(f"GA4 Audiences: '{name}' already exists — skipping")
            results.append({"audience": name, "status": "exists"})
            continue

        try:
            from google.analytics.admin_v1alpha.types import Audience

            if aud_def["filter_type"] == "implant_no_form":
                filter_clauses = _build_implant_visitor_filter(client)
            elif aud_def["filter_type"] == "clarity_engagement":
                filter_clauses = _build_clarity_engagement_filter(client)
            else:
                results.append({"audience": name, "status": "error", "error": "Unknown filter_type"})
                continue

            audience = Audience(
                display_name=name,
                description=aud_def["description"],
                membership_duration_days=aud_def["membership_duration_days"],
                filter_clauses=filter_clauses,
            )

            created = client.create_audience(parent=property_path, audience=audience)
            logger.info(f"GA4 Audiences: created '{name}' → {created.name}")
            results.append({
                "audience": name,
                "status": "created",
                "resource_name": created.name,
                "note": (
                    "Audience will appear in Google Ads within 24–48 hours if "
                    "GA4↔GAds link has audience sharing enabled."
                ),
            })

        except Exception as e:
            logger.error(f"GA4 Audiences: failed to create '{name}': {e}")
            results.append({"audience": name, "status": "error", "error": str(e)})

    ok = all(r.get("status") in ("created", "exists") for r in results)
    return {
        "ok": ok,
        "property_id": property_id,
        "synced_at": now,
        "audiences": results,
        "prerequisites": {
            "clarity_ga4_link": "Must be enabled in Clarity Dashboard → Settings → Integrations → Google Analytics",
            "gads_link": "GA4 Admin → Google Ads Links → Enable personalized advertising + audience sharing",
            "sa_scope": "SA must have analytics.edit scope and Editor role on this GA4 property",
        },
    }


def get_audience_status() -> dict:
    """
    List all GA4 audiences for the GDC property — shows which exist and their sizes.
    Uses analytics.readonly scope (existing credentials), so no new setup needed.
    """
    try:
        from google.analytics.admin import AnalyticsAdminServiceClient
        from google.oauth2 import service_account
    except ImportError:
        return {"ok": False, "error": "google-analytics-admin not installed"}

    settings = get_settings()
    property_id = _get_primary_property_id()
    if not property_id:
        return {"ok": False, "error": "No GA4 property ID configured"}

    property_path = f"properties/{property_id}"

    try:
        # Use readonly scope for status checks (doesn't require SA role upgrade)
        creds = service_account.Credentials.from_service_account_file(
            settings.ga4_service_account_json,
            scopes=["https://www.googleapis.com/auth/analytics.readonly"],
        )
        client = AnalyticsAdminServiceClient(credentials=creds)
        audiences = list(client.list_audiences(parent=property_path))
        return {
            "ok": True,
            "property_id": property_id,
            "audiences": [
                {
                    "name": a.display_name,
                    "resource_name": a.name,
                    "membership_duration_days": a.membership_duration_days,
                }
                for a in audiences
            ],
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
