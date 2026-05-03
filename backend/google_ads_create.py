"""
Google Ads campaign creation and lifecycle management.

Handles:
  - Campaign status changes (pause / resume / remove)
  - Fetching existing campaigns from Google Ads account (read-only)
  - (Future) Campaign creation, ad group, keywords, RSA creation

All write operations check the kill switch before executing.
New campaigns are always created as PAUSED — never go live automatically.
"""
import logging
from config import get_settings

logger = logging.getLogger(__name__)

# GAds sentinel dates that mean "not set" — strip these when importing
_GADS_DATE_SENTINELS = {"2037-12-30", "1970-01-01", "0001-01-01", ""}


def _build_client():
    """Build authenticated Google Ads API client using shared credentials.

    Mirrors the exact same pattern used in google_ads_sync.py and ai_optimizer.py
    (confirmed working) — sub-account login_customer_id, not the manager ID.
    """
    from google.ads.googleads.client import GoogleAdsClient
    settings = get_settings()
    return GoogleAdsClient.load_from_dict({
        "developer_token":    settings.google_ads_developer_token,
        "client_id":          settings.google_ads_client_id,
        "client_secret":      settings.google_ads_client_secret,
        "refresh_token":      settings.google_ads_refresh_token,
        "login_customer_id":  settings.google_ads_login_customer_id,
        "use_proto_plus":     True,
    })


def fetch_campaigns_from_gads() -> list:
    """
    Pull all non-REMOVED campaigns from the Google Ads account.
    READ-ONLY — no kill switch needed.

    Returns list of dicts:
        resource_name, campaign_id (numeric str), campaign_name,
        gads_status ("ENABLED"|"PAUSED"), channel_type,
        start_date, end_date (sentinel dates stripped to ""),
        daily_budget_usd, monthly_budget_usd (daily × 30, approximate)

    Raises on auth/API failure so callers can surface a 502.
    """
    settings = get_settings()
    # Strip non-digits defensively (some envs set "249-804-9505")
    customer_id = "".join(ch for ch in (settings.google_ads_customer_id or "") if ch.isdigit())
    if not customer_id:
        raise ValueError("google_ads_customer_id is not configured")

    client = _build_client()
    service = client.get_service("GoogleAdsService")

    # Pass 1: campaigns — select campaign.campaign_budget (resource name pointer),
    # NOT campaign_budget.amount_micros. Cross-resource joins (FROM campaign +
    # campaign_budget.* field) produce a cryptic "501 GRPC target method can't be
    # resolved". We resolve budgets in a separate pass instead.
    # NOTE: campaign.start_date / campaign.end_date removed from API in v20+.
    campaign_query = """
        SELECT
            campaign.resource_name,
            campaign.id,
            campaign.name,
            campaign.status,
            campaign.advertising_channel_type,
            campaign.campaign_budget
        FROM campaign
        WHERE campaign.status IN (ENABLED, PAUSED)
        ORDER BY campaign.name ASC
    """

    campaigns_raw = []
    budget_resources = set()
    for row in service.search(customer_id=customer_id, query=campaign_query):
        budget_rn = row.campaign.campaign_budget or ""
        if budget_rn:
            budget_resources.add(budget_rn)
        campaigns_raw.append({
            "resource_name":    row.campaign.resource_name,
            "campaign_id":      str(row.campaign.id),
            "campaign_name":    row.campaign.name or "",
            "gads_status":      str(row.campaign.status),   # "ENABLED" or "PAUSED"
            "channel_type":     str(row.campaign.advertising_channel_type),
            "start_date":       "",
            "end_date":         "",
            "_budget_resource": budget_rn,
        })

    # Pass 2: fetch daily budget amounts by budget resource name
    budget_micros: dict = {}
    if budget_resources:
        in_list = ", ".join(f"'{rn}'" for rn in budget_resources)
        budget_query = f"""
            SELECT
                campaign_budget.resource_name,
                campaign_budget.amount_micros
            FROM campaign_budget
            WHERE campaign_budget.resource_name IN ({in_list})
        """
        for row in service.search(customer_id=customer_id, query=budget_query):
            budget_micros[row.campaign_budget.resource_name] = (
                row.campaign_budget.amount_micros or 0
            )

    # Stitch results and compute USD budgets
    results = []
    for c in campaigns_raw:
        budget_rn    = c.pop("_budget_resource", "")
        daily_micros = budget_micros.get(budget_rn, 0)
        daily_usd    = round(daily_micros / 1_000_000.0, 2)
        c["daily_budget_usd"]   = daily_usd
        c["monthly_budget_usd"] = round(daily_usd * 30, 2)  # approximate monthly
        results.append(c)

    logger.info(f"fetch_campaigns_from_gads: {len(results)} campaigns returned")
    return results


def set_campaign_status(campaign_resource_name: str, target_status: str) -> dict:
    """
    Change a Google Ads campaign's status via the API.

    Args:
        campaign_resource_name: Full resource name, e.g.
            "customers/1234567890/campaigns/9876543210"
        target_status: One of "PAUSED", "ENABLED", "REMOVED"
            REMOVED is irreversible — use only for Stop.

    Returns:
        { "ok": bool, "resource_name": str, "error": str | None }

    Raises nothing — all errors are returned in the dict so callers
    can decide whether to update local DB or surface to the user.
    """
    # ── Kill-switch check ──────────────────────────────────────────────────────
    from campaign_safety import check_writes_enabled, WriteBlockedError
    try:
        check_writes_enabled()
    except WriteBlockedError as e:
        logger.warning(f"set_campaign_status blocked by kill switch: {e}")
        return {"ok": False, "resource_name": campaign_resource_name, "error": str(e)}

    settings = get_settings()
    customer_id = settings.google_ads_customer_id

    valid_statuses = {"PAUSED", "ENABLED", "REMOVED"}
    if target_status not in valid_statuses:
        return {
            "ok": False,
            "resource_name": campaign_resource_name,
            "error": f"Invalid status '{target_status}'. Must be one of {valid_statuses}",
        }

    try:
        client = _build_client()
        campaign_service = client.get_service("CampaignService")

        # Build the update operation
        campaign_operation = client.get_type("CampaignOperation")
        campaign = campaign_operation.update
        campaign.resource_name = campaign_resource_name

        # Set status using the enum
        status_enum = client.enums.CampaignStatusEnum
        status_map = {
            "PAUSED":  status_enum.PAUSED,
            "ENABLED": status_enum.ENABLED,
            "REMOVED": status_enum.REMOVED,
        }
        campaign.status = status_map[target_status]

        # Explicit field mask — only update `status`, never touch other fields
        # Pattern from ai_optimizer.py (same codebase, confirmed working)
        client.copy_from(
            campaign_operation.update_mask,
            client.get_type("FieldMask")(paths=["status"]),
        )

        # Single-operation mutate — no partial_failure needed for one op
        # Errors surface as GoogleAdsException (caught below)
        response = campaign_service.mutate_campaigns(
            customer_id=customer_id,
            operations=[campaign_operation],
        )

        updated_resource = response.results[0].resource_name if response.results else campaign_resource_name
        logger.info(f"GAds campaign status → {target_status}: {updated_resource}")
        return {"ok": True, "resource_name": updated_resource, "error": None}

    except Exception as e:
        logger.error(f"GAds set_campaign_status failed ({target_status}): {e}")
        return {"ok": False, "resource_name": campaign_resource_name, "error": str(e)}
