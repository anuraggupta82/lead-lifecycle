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


def _get_campaign_channel_type(campaign_resource_name: str) -> str:
    """
    Fetch the advertising_channel_type for a campaign resource name.
    Returns the string value e.g. "SEARCH", "PERFORMANCE_MAX", "DISPLAY", or ""
    on failure.
    """
    settings = get_settings()
    customer_id = "".join(ch for ch in (settings.google_ads_customer_id or "") if ch.isdigit())
    try:
        client = _build_client()
        service = client.get_service("GoogleAdsService")
        query = f"""
            SELECT campaign.advertising_channel_type
            FROM campaign
            WHERE campaign.resource_name = '{campaign_resource_name}'
            LIMIT 1
        """
        rows = list(service.search(customer_id=customer_id, query=query))
        if rows:
            return str(rows[0].campaign.advertising_channel_type).upper()
    except Exception as e:
        logger.warning(f"_get_campaign_channel_type failed: {e}")
    return ""


def enable_ai_max(campaign_resource_name: str) -> dict:
    """
    Enable AI Max on an existing Google Search campaign.

    AI Max expands reach beyond the keyword list using Google's AI for search
    term matching. Only valid for SEARCH channel type campaigns.

    Does NOT enable Final URL expansion — that is left OFF by default because
    it breaks landing_url attribution. Implement separately when needed.

    Respects the global kill switch.

    Returns:
        { "ok": bool, "resource_name": str, "error": str | None }
    """
    from campaign_safety import check_writes_enabled, WriteBlockedError
    try:
        check_writes_enabled()
    except WriteBlockedError as e:
        logger.warning(f"enable_ai_max blocked by kill switch: {e}")
        return {"ok": False, "resource_name": campaign_resource_name, "error": str(e)}

    # AI Max only works on Search campaigns
    channel = _get_campaign_channel_type(campaign_resource_name)
    if channel and channel != "SEARCH":
        return {
            "ok": False,
            "resource_name": campaign_resource_name,
            "error": f"AI Max only applies to Search campaigns (this is {channel})",
        }

    settings = get_settings()
    customer_id = "".join(ch for ch in (settings.google_ads_customer_id or "") if ch.isdigit())

    try:
        client = _build_client()
        campaign_service = client.get_service("CampaignService")

        campaign_operation = client.get_type("CampaignOperation")
        campaign = campaign_operation.update
        campaign.resource_name = campaign_resource_name

        # Set the single AI Max master switch
        campaign.ai_max_setting.enable_ai_max = True

        client.copy_from(
            campaign_operation.update_mask,
            client.get_type("FieldMask")(paths=["ai_max_setting.enable_ai_max"]),
        )

        response = campaign_service.mutate_campaigns(
            customer_id=customer_id,
            operations=[campaign_operation],
        )

        updated = response.results[0].resource_name if response.results else campaign_resource_name
        logger.info(f"AI Max ENABLED on campaign: {updated}")
        return {"ok": True, "resource_name": updated, "error": None}

    except Exception as e:
        logger.error(f"enable_ai_max failed: {e}")
        return {"ok": False, "resource_name": campaign_resource_name, "error": str(e)}


def disable_ai_max(campaign_resource_name: str) -> dict:
    """
    Disable AI Max on a Google Search campaign.

    Historical lead attribution (search_term_type='ai_max') is preserved as-is —
    disabling AI Max only stops new AI-expanded queries, it does not retroactively
    change historical data.

    Respects the global kill switch.

    Returns:
        { "ok": bool, "resource_name": str, "error": str | None }
    """
    from campaign_safety import check_writes_enabled, WriteBlockedError
    try:
        check_writes_enabled()
    except WriteBlockedError as e:
        logger.warning(f"disable_ai_max blocked by kill switch: {e}")
        return {"ok": False, "resource_name": campaign_resource_name, "error": str(e)}

    settings = get_settings()
    customer_id = "".join(ch for ch in (settings.google_ads_customer_id or "") if ch.isdigit())

    try:
        client = _build_client()
        campaign_service = client.get_service("CampaignService")

        campaign_operation = client.get_type("CampaignOperation")
        campaign = campaign_operation.update
        campaign.resource_name = campaign_resource_name
        campaign.ai_max_setting.enable_ai_max = False

        client.copy_from(
            campaign_operation.update_mask,
            client.get_type("FieldMask")(paths=["ai_max_setting.enable_ai_max"]),
        )

        response = campaign_service.mutate_campaigns(
            customer_id=customer_id,
            operations=[campaign_operation],
        )

        updated = response.results[0].resource_name if response.results else campaign_resource_name
        logger.info(f"AI Max DISABLED on campaign: {updated}")
        return {"ok": True, "resource_name": updated, "error": None}

    except Exception as e:
        logger.error(f"disable_ai_max failed: {e}")
        return {"ok": False, "resource_name": campaign_resource_name, "error": str(e)}


def fetch_campaign_build_data(campaign_resource_name: str) -> dict:
    """
    Pull existing keywords, ad copies, and ad groups from Google Ads for a
    specific campaign and return them formatted as campaign_build_json steps.

    READ-ONLY — no kill switch needed.

    Three separate GAQL queries (same two-pass pattern used elsewhere):
      Pass 1: keyword_view — all enabled/paused keywords for this campaign
      Pass 2: ad_group_ad   — all enabled/paused RSAs / ETAs for this campaign
      Pass 3: ad_group      — all enabled/paused ad groups for this campaign

    The returned dict is stored to `gads_campaign_snapshot` (NOT campaign_build_json)
    so it never clobbers user-edited wizard state.  The frontend seeds the wizard
    from the snapshot only when campaign_build_json is empty.

    Returns:
        {
            "keywords":          { ... }   # Keywords step format
            "ad_copy":           { ... }   # Ad Copy step format
            "ad_groups":         { ... }   # Ad Groups step format
            "campaign_settings": { ... }   # campaign-level meta pulled from Pass 3
            "synced_from_gads_at": "ISO timestamp"
        }

    On failure returns:
        { "error": str, "synced_from_gads_at": None }
    """
    from datetime import datetime, timezone

    settings = get_settings()
    customer_id = "".join(ch for ch in (settings.google_ads_customer_id or "") if ch.isdigit())
    if not customer_id:
        return {"error": "google_ads_customer_id is not configured", "synced_from_gads_at": None}

    try:
        client = _build_client()
        service = client.get_service("GoogleAdsService")

        # ── Pass 1: Keywords ──────────────────────────────────────────────────
        # keyword_view is the correct resource (ad_group_criterion includes all
        # criterion types; keyword_view is pre-filtered to keywords only).
        keyword_query = f"""
            SELECT
                ad_group_criterion.resource_name,
                ad_group_criterion.keyword.text,
                ad_group_criterion.keyword.match_type,
                ad_group_criterion.status,
                ad_group_criterion.negative,
                ad_group_criterion.cpc_bid_micros,
                ad_group_criterion.effective_cpc_bid_micros,
                ad_group.id,
                ad_group.name
            FROM keyword_view
            WHERE campaign.resource_name = '{campaign_resource_name}'
              AND ad_group_criterion.status IN (ENABLED, PAUSED)
            ORDER BY ad_group.name ASC, ad_group_criterion.keyword.text ASC
        """

        keywords_list = []
        neg_keywords_list = []
        for row in service.search(customer_id=customer_id, query=keyword_query):
            crit = row.ad_group_criterion
            kw_text = crit.keyword.text or ""
            match_type = str(crit.keyword.match_type).upper().replace("KEYWORD_MATCH_TYPE_", "")
            # Normalise match type strings from proto enum names
            # e.g. "EXACT" → "Exact", "PHRASE" → "Phrase", "BROAD" → "Broad"
            match_type_clean = match_type.capitalize() if match_type else "Broad"

            cpc_micros = crit.cpc_bid_micros or 0
            eff_micros  = crit.effective_cpc_bid_micros or 0
            cpc_usd = round((cpc_micros or eff_micros) / 1_000_000.0, 2) if (cpc_micros or eff_micros) else None

            entry = {
                "keyword":    kw_text,
                "match_type": match_type_clean,
                "cpc_bid":    cpc_usd,
                "ad_group":   row.ad_group.name or "",
                "ad_group_id": str(row.ad_group.id),
                "resource_name": crit.resource_name,
                "negative":   bool(crit.negative),
                "status":     str(crit.status).upper(),
            }
            if crit.negative:
                neg_keywords_list.append(entry)
            else:
                keywords_list.append(entry)

        keywords_step = {
            "keywords":          keywords_list,
            "negative_keywords": neg_keywords_list,
            "source":            "imported_from_google_ads",
        }

        # ── Pass 2: Ad Copies (RSA / ETA) ────────────────────────────────────
        ad_query = f"""
            SELECT
                ad_group_ad.resource_name,
                ad_group_ad.status,
                ad_group_ad.ad.id,
                ad_group_ad.ad.type,
                ad_group_ad.ad.responsive_search_ad.headlines,
                ad_group_ad.ad.responsive_search_ad.descriptions,
                ad_group_ad.ad.responsive_search_ad.path1,
                ad_group_ad.ad.responsive_search_ad.path2,
                ad_group_ad.ad.final_urls,
                ad_group_ad.ad_strength,
                ad_group_ad.policy_summary.approval_status,
                ad_group.id,
                ad_group.name
            FROM ad_group_ad
            WHERE campaign.resource_name = '{campaign_resource_name}'
              AND ad_group_ad.status IN (ENABLED, PAUSED)
        """

        ads_list = []
        for row in service.search(customer_id=customer_id, query=ad_query):
            ad = row.ad_group_ad.ad
            rsa = ad.responsive_search_ad

            # Extract headline texts (proto-plus list of AdTextAsset)
            headlines = []
            if rsa and rsa.headlines:
                for h in rsa.headlines:
                    text = h.text if hasattr(h, "text") else ""
                    if text:
                        headlines.append(text)

            # Extract description texts
            descriptions = []
            if rsa and rsa.descriptions:
                for d in rsa.descriptions:
                    text = d.text if hasattr(d, "text") else ""
                    if text:
                        descriptions.append(text)

            final_urls = list(ad.final_urls) if ad.final_urls else []

            ad_strength_raw = str(row.ad_group_ad.ad_strength).upper()
            # Normalise proto enum prefix (AD_STRENGTH_UNSPECIFIED → "UNSPECIFIED")
            ad_strength = ad_strength_raw.replace("AD_STRENGTH_", "")

            approval_raw = str(row.ad_group_ad.policy_summary.approval_status).upper()
            approval = approval_raw.replace("POLICY_APPROVAL_STATUS_", "")

            ads_list.append({
                "resource_name":  row.ad_group_ad.resource_name,
                "ad_id":          str(ad.id),
                "ad_type":        str(ad.type).upper(),
                "ad_group":       row.ad_group.name or "",
                "ad_group_id":    str(row.ad_group.id),
                "headlines":      headlines,
                "descriptions":   descriptions,
                "path1":          rsa.path1 if rsa else "",
                "path2":          rsa.path2 if rsa else "",
                "final_urls":     final_urls,
                "ad_strength":    ad_strength,
                "approval_status": approval,
                "status":         str(row.ad_group_ad.status).upper(),
                "source":         "imported_from_google_ads",
            })

        ad_copy_step = {
            "ads":    ads_list,
            "source": "imported_from_google_ads",
        }

        # ── Pass 3: Ad Groups ─────────────────────────────────────────────────
        ad_group_query = f"""
            SELECT
                ad_group.resource_name,
                ad_group.id,
                ad_group.name,
                ad_group.status,
                ad_group.type,
                ad_group.cpc_bid_micros,
                ad_group.target_cpa_micros
            FROM ad_group
            WHERE campaign.resource_name = '{campaign_resource_name}'
              AND ad_group.status IN (ENABLED, PAUSED)
            ORDER BY ad_group.name ASC
        """

        ad_groups_list = []
        for row in service.search(customer_id=customer_id, query=ad_group_query):
            ag = row.ad_group
            cpc_micros = ag.cpc_bid_micros or 0
            target_cpa_micros = ag.target_cpa_micros or 0

            ad_groups_list.append({
                "resource_name":  ag.resource_name,
                "ad_group_id":    str(ag.id),
                "ad_group_name":  ag.name or "",
                "status":         str(ag.status).upper(),
                "type":           str(ag.type).upper(),
                "cpc_bid_usd":    round(cpc_micros / 1_000_000.0, 2) if cpc_micros else None,
                "target_cpa_usd": round(target_cpa_micros / 1_000_000.0, 2) if target_cpa_micros else None,
                "source":         "imported_from_google_ads",
            })

        ad_groups_step = {
            "ad_groups": ad_groups_list,
            "source":    "imported_from_google_ads",
        }

        # campaign_settings: derive from ad group data (no separate query needed)
        # These are aggregated summaries useful for the Strategy tab seed
        unique_ag_types = list({ag["type"] for ag in ad_groups_list})
        campaign_settings = {
            "ad_group_count":  len(ad_groups_list),
            "keyword_count":   len(keywords_list),
            "neg_kw_count":    len(neg_keywords_list),
            "ad_count":        len(ads_list),
            "ad_group_types":  unique_ag_types,
            "source":          "imported_from_google_ads",
        }

        synced_at = datetime.now(timezone.utc).isoformat()
        logger.info(
            f"fetch_campaign_build_data: {len(keywords_list)} kw, "
            f"{len(ads_list)} ads, {len(ad_groups_list)} ad groups "
            f"for {campaign_resource_name}"
        )

        return {
            "keywords":          keywords_step,
            "ad_copy":           ad_copy_step,
            "ad_groups":         ad_groups_step,
            "campaign_settings": campaign_settings,
            "synced_from_gads_at": synced_at,
        }

    except Exception as e:
        logger.error(f"fetch_campaign_build_data failed: {e}")
        return {"error": str(e), "synced_from_gads_at": None}
