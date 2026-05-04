"""
Google Ads campaign creation and lifecycle management.

Handles:
  - Campaign status changes (pause / resume / remove)
  - Fetching existing campaigns from Google Ads account (read-only)
  - Full campaign creation: budget → campaign → geo → ad groups → keywords → RSAs → extensions

All write operations check the kill switch before executing.
New campaigns are always created as PAUSED first, then ENABLED at the end.
"""
import logging
import json
from config import get_settings

logger = logging.getLogger(__name__)

# GAds sentinel dates that mean "not set" — strip these when importing
_GADS_DATE_SENTINELS = {"2037-12-30", "1970-01-01", "0001-01-01", ""}


def create_campaign_in_gads(campaign: dict, build: dict) -> dict:
    """
    Create a full Google Search campaign in Google Ads from dashboard data.

    Steps:
      1. Budget resource (daily budget)
      2. Campaign (PAUSED, Search, manual CPC)
      3. Geographic targeting (from geographic_targeting JSON)
      4. Ad groups (from build.ad_groups)
      5. Keywords — exact, phrase, broad per ad group (from build.keywords)
      6. Negative keywords (campaign-level)
      7. RSA ads (from build.ad_copy, one per ad group)
      8. Call extension (from call_extension_phone or practice phone)
      9. Enable campaign (ENABLED) — only after all assets created

    Args:
        campaign: dict from campaigns DB row (campaign_name, monthly_budget,
                  landing_page, call_extension_phone, geographic_targeting, etc.)
        build:    dict from campaign_build_json (keywords, ad_copy, ad_groups keys)

    Returns:
        {
            "ok": bool,
            "campaign_resource_name": str,
            "campaign_numeric_id": str,
            "ad_group_resources": [...],
            "keywords_added": int,
            "ads_created": int,
            "error": str | None,
            "log": [str, ...]   # step-by-step progress log
        }
    """
    from campaign_safety import check_writes_enabled, WriteBlockedError
    try:
        check_writes_enabled()
    except WriteBlockedError as e:
        return {"ok": False, "error": str(e), "log": [f"Blocked: {e}"]}

    settings = get_settings()
    customer_id = "".join(ch for ch in (settings.google_ads_customer_id or "") if ch.isdigit())
    if not customer_id:
        return {"ok": False, "error": "google_ads_customer_id not configured", "log": []}

    log = []

    try:
        client = _build_client()

        campaign_name    = campaign.get("campaign_name", "New Campaign")
        monthly_budget   = float(campaign.get("monthly_budget") or 0)
        daily_budget_usd = round(monthly_budget / 30.4, 2) if monthly_budget else 10.0
        landing_page     = campaign.get("landing_page") or settings.practice_website or "https://graftondentalcare.com"
        call_phone       = campaign.get("call_extension_phone") or getattr(settings, "practice_phone", "") or ""
        geo_json         = campaign.get("geographic_targeting") or ""
        start_date       = campaign.get("start_date") or ""

        kw_data    = build.get("keywords") or {}
        ac_data    = build.get("ad_copy") or {}
        ag_data    = build.get("ad_groups") or {}

        # ── Step 1: Budget resource ───────────────────────────────────────────
        log.append(f"Step 1: Creating daily budget ${daily_budget_usd}/day")
        budget_service = client.get_service("CampaignBudgetService")
        budget_op      = client.get_type("CampaignBudgetOperation")
        budget         = budget_op.create
        budget.name    = f"{campaign_name} Budget"
        budget.amount_micros = int(daily_budget_usd * 1_000_000)
        budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
        budget.explicitly_shared = False

        budget_response = budget_service.mutate_campaign_budgets(
            customer_id=customer_id, operations=[budget_op]
        )
        budget_resource = budget_response.results[0].resource_name
        log.append(f"  ✓ Budget created: {budget_resource}")

        # ── Step 2: Campaign (PAUSED) ─────────────────────────────────────────
        log.append(f"Step 2: Creating campaign '{campaign_name}' (PAUSED)")
        camp_service = client.get_service("CampaignService")
        camp_op      = client.get_type("CampaignOperation")
        camp         = camp_op.create
        camp.name    = campaign_name
        camp.status  = client.enums.CampaignStatusEnum.PAUSED
        camp.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.SEARCH
        camp.campaign_budget = budget_resource

        # Manual CPC bidding — use copy_from to activate the oneof so Google
        # recognises this as the selected bidding strategy (proto-plus oneof
        # requires a non-default field assignment or copy_from to be "set").
        client.copy_from(camp.manual_cpc, client.get_type("ManualCpc")())

        # Network settings — search only, no display, no partners
        camp.network_settings.target_google_search     = True
        camp.network_settings.target_search_network    = True
        camp.network_settings.target_content_network   = False
        camp.network_settings.target_partner_search_network = False

        # Start date
        if start_date:
            try:
                from datetime import datetime
                dt = datetime.strptime(start_date[:10], "%Y-%m-%d")
                camp.start_date = dt.strftime("%Y%m%d")
            except Exception:
                pass

        camp_response = camp_service.mutate_campaigns(
            customer_id=customer_id, operations=[camp_op]
        )
        camp_resource = camp_response.results[0].resource_name
        camp_numeric  = camp_resource.split("/campaigns/")[-1]
        log.append(f"  ✓ Campaign created: {camp_resource} (id={camp_numeric})")

        # ── Step 3: Geographic targeting ──────────────────────────────────────
        log.append("Step 3: Setting geographic targeting")
        geo_criterion_service = client.get_service("CampaignCriterionService")
        geo_ops = []

        # Parse geo_json — list of {type, value, radius, include}
        geo_locs = []
        if geo_json:
            try:
                parsed = json.loads(geo_json) if isinstance(geo_json, str) else geo_json
                geo_locs = parsed.get("locations", []) if isinstance(parsed, dict) else []
            except Exception:
                pass

        if geo_locs:
            # Use GeoTargetConstant lookup for postal codes and named places
            geo_target_service = client.get_service("GeoTargetConstantService")
            for loc in geo_locs:
                loc_type  = loc.get("type", "postal")
                loc_value = str(loc.get("value", "")).strip()
                include   = loc.get("include", True)
                if not loc_value:
                    continue
                try:
                    # Suggest geo targets by name/postal code
                    suggest_req = client.get_type("SuggestGeoTargetConstantsRequest")
                    suggest_req.locale = "en"
                    suggest_req.country_code = "US"
                    # SuggestGeoTargetConstantsRequest uses location_names.names
                    # (not geo_targets.names — that proto path does not exist)
                    suggest_req.location_names.names.append(loc_value)

                    suggest_resp = geo_target_service.suggest_geo_target_constants(request=suggest_req)
                    for suggestion in (suggest_resp.geo_target_constant_suggestions or []):
                        geo_const = suggestion.geo_target_constant
                        crit_op   = client.get_type("CampaignCriterionOperation")
                        crit      = crit_op.create
                        crit.campaign = camp_resource
                        crit.location.geo_target_constant = geo_const.resource_name
                        if not include:
                            crit.negative = True
                        geo_ops.append(crit_op)
                        break  # take first match only
                except Exception as ge:
                    log.append(f"  ⚠ Geo lookup failed for '{loc_value}': {ge}")
        else:
            # Default: 15-mile radius around Grafton MA (42.2012° N, 71.6870° W)
            log.append("  No geo targets set — defaulting to 15-mile radius around Grafton MA")
            crit_op = client.get_type("CampaignCriterionOperation")
            crit    = crit_op.create
            crit.campaign = camp_resource
            crit.proximity.address.city_name     = "Grafton"
            crit.proximity.address.province_code = "MA"
            crit.proximity.address.country_code  = "US"
            crit.proximity.radius               = 15
            crit.proximity.radius_units         = client.enums.ProximityRadiusUnitsEnum.MILES
            geo_ops.append(crit_op)

        if geo_ops:
            geo_response = geo_criterion_service.mutate_campaign_criteria(
                customer_id=customer_id, operations=geo_ops
            )
            log.append(f"  ✓ {len(geo_response.results)} geo target(s) applied")

        # ── Step 4 & 5: Ad Groups + Keywords ─────────────────────────────────
        log.append("Step 4: Creating ad groups and keywords")
        ag_service  = client.get_service("AdGroupService")
        kw_service  = client.get_service("AdGroupCriterionService")

        # Build ad group name → keywords map from build data
        # build.keywords has: exact_match[], phrase_match[], broad_match_modifier[], negative_keywords[]
        # build.ad_groups has: ad_groups[{name, keywords[], cpc_bid}]
        # Strategy: one ad group per build.ad_groups entry; assign keywords to it

        ag_entries = ag_data.get("ad_groups", [])
        if not ag_entries:
            # Fallback: single ad group with all keywords
            ag_entries = [{"name": f"{campaign.get('service_focus','General')} - Search", "cpc_bid": 3.0}]

        ad_group_resources = []
        keywords_added     = 0

        # All keywords from build (flat lists)
        exact_kws  = [k if isinstance(k, str) else k.get("keyword","") for k in kw_data.get("exact_match", [])]
        phrase_kws = [k if isinstance(k, str) else k.get("keyword","") for k in kw_data.get("phrase_match", [])]
        broad_kws  = [k if isinstance(k, str) else k.get("keyword","") for k in kw_data.get("broad_match_modifier", [])]
        neg_kws    = [k if isinstance(k, str) else k.get("keyword","") for k in kw_data.get("negative_keywords", [])]

        # Split keywords evenly across ad groups if multiple groups
        # (Simple approach: all groups get all keywords — Google doesn't mind)
        for ag_entry in ag_entries:
            ag_name    = ag_entry.get("name") or ag_entry.get("ad_group_name") or "Ad Group 1"
            cpc_bid    = float(ag_entry.get("cpc_bid") or ag_entry.get("cpc_bid_usd") or 3.0)

            # Create ad group
            ag_op      = client.get_type("AdGroupOperation")
            ag         = ag_op.create
            ag.name    = ag_name
            ag.campaign = camp_resource
            ag.status  = client.enums.AdGroupStatusEnum.ENABLED
            ag.type_   = client.enums.AdGroupTypeEnum.SEARCH_STANDARD
            ag.cpc_bid_micros = int(cpc_bid * 1_000_000)

            ag_response = ag_service.mutate_ad_groups(
                customer_id=customer_id, operations=[ag_op]
            )
            ag_resource = ag_response.results[0].resource_name
            ad_group_resources.append(ag_resource)
            log.append(f"  ✓ Ad group '{ag_name}': {ag_resource}")

            # Add keywords to this ad group
            kw_ops = []
            match_enum = client.enums.KeywordMatchTypeEnum

            for kw in exact_kws:
                if not kw.strip(): continue
                op = client.get_type("AdGroupCriterionOperation")
                c  = op.create
                c.ad_group = ag_resource
                c.status   = client.enums.AdGroupCriterionStatusEnum.ENABLED
                c.keyword.text       = kw.strip()
                c.keyword.match_type = match_enum.EXACT
                kw_ops.append(op)

            for kw in phrase_kws:
                if not kw.strip(): continue
                op = client.get_type("AdGroupCriterionOperation")
                c  = op.create
                c.ad_group = ag_resource
                c.status   = client.enums.AdGroupCriterionStatusEnum.ENABLED
                c.keyword.text       = kw.strip()
                c.keyword.match_type = match_enum.PHRASE
                kw_ops.append(op)

            for kw in broad_kws:
                if not kw.strip(): continue
                op = client.get_type("AdGroupCriterionOperation")
                c  = op.create
                c.ad_group = ag_resource
                c.status   = client.enums.AdGroupCriterionStatusEnum.ENABLED
                c.keyword.text       = kw.strip()
                c.keyword.match_type = match_enum.BROAD
                kw_ops.append(op)

            if kw_ops:
                kw_response = kw_service.mutate_ad_group_criteria(
                    customer_id=customer_id, operations=kw_ops
                )
                count = len(kw_response.results)
                keywords_added += count
                log.append(f"    ✓ {count} keywords added to '{ag_name}'")

        # ── Step 6: Campaign-level negative keywords ──────────────────────────
        log.append("Step 6: Adding campaign-level negative keywords")
        camp_crit_service = client.get_service("CampaignCriterionService")
        neg_ops = []
        for kw in neg_kws:
            kw = kw.strip()
            if not kw: continue
            op = client.get_type("CampaignCriterionOperation")
            c  = op.create
            c.campaign = camp_resource
            c.negative = True
            c.keyword.text       = kw
            c.keyword.match_type = client.enums.KeywordMatchTypeEnum.BROAD
            neg_ops.append(op)

        if neg_ops:
            neg_response = camp_crit_service.mutate_campaign_criteria(
                customer_id=customer_id, operations=neg_ops
            )
            log.append(f"  ✓ {len(neg_response.results)} negative keywords added")
        else:
            log.append("  (no negative keywords)")

        # ── Step 7: RSA ads ───────────────────────────────────────────────────
        log.append("Step 7: Creating RSA ads")
        ad_service  = client.get_service("AdGroupAdService")
        ads_created = 0

        ac_groups = ac_data.get("ad_groups", [])

        for i, ag_resource in enumerate(ad_group_resources):
            # Match ad copy group by index (or use first if only one)
            ac_group = ac_groups[i] if i < len(ac_groups) else (ac_groups[0] if ac_groups else {})

            headlines_raw    = ac_group.get("headlines", [])
            descriptions_raw = ac_group.get("descriptions", [])

            # Normalise: each item may be str or {text:...}
            headlines    = [h if isinstance(h, str) else h.get("text","") for h in headlines_raw]
            descriptions = [d if isinstance(d, str) else d.get("text","") for d in descriptions_raw]

            # Filter empty, cap at 15 headlines / 4 descriptions (API limits)
            headlines    = [h.strip() for h in headlines    if h.strip()][:15]
            descriptions = [d.strip() for d in descriptions if d.strip()][:4]

            if not headlines:
                log.append(f"  ⚠ Ad group {i+1}: no headlines — skipping RSA creation")
                continue
            if not descriptions:
                log.append(f"  ⚠ Ad group {i+1}: no descriptions — skipping RSA creation")
                continue

            # Need at least 3 headlines
            if len(headlines) < 3:
                log.append(f"  ⚠ Ad group {i+1}: only {len(headlines)} headline(s) — need 3+ for RSA")
                continue

            ad_op = client.get_type("AdGroupAdOperation")
            ad    = ad_op.create
            ad.ad_group = ag_resource
            ad.status   = client.enums.AdGroupAdStatusEnum.ENABLED

            rsa = ad.ad.responsive_search_ad
            # Parse display path from landing page
            from urllib.parse import urlparse
            parsed_url = urlparse(landing_page)
            path_parts = [p for p in parsed_url.path.strip("/").split("/") if p]
            # Only assign path1/path2 when non-empty — Google rejects empty string
            if path_parts:
                rsa.path1 = path_parts[0][:15]
            if len(path_parts) > 1:
                rsa.path2 = path_parts[1][:15]

            # Add headlines
            for h_text in headlines:
                asset = client.get_type("AdTextAsset")
                asset.text = h_text[:30]  # Google max 30 chars
                rsa.headlines.append(asset)

            # Add descriptions
            for d_text in descriptions:
                asset = client.get_type("AdTextAsset")
                asset.text = d_text[:90]  # Google max 90 chars
                rsa.descriptions.append(asset)

            ad.ad.final_urls.append(landing_page)

            ad_response = ad_service.mutate_ad_group_ads(
                customer_id=customer_id, operations=[ad_op]
            )
            ads_created += 1
            log.append(f"  ✓ RSA created for ad group {i+1}: {len(headlines)} headlines, {len(descriptions)} descriptions")

        # ── Step 8: Call extension ────────────────────────────────────────────
        if call_phone:
            log.append(f"Step 8: Adding call extension ({call_phone})")
            try:
                asset_service = client.get_service("AssetService")
                asset_op      = client.get_type("AssetOperation")
                asset         = asset_op.create
                # Strip non-digits for the phone_number field
                phone_digits  = "".join(ch for ch in call_phone if ch.isdigit() or ch == "+")
                asset.call_asset.phone_number    = phone_digits  # E.164 digits only
                asset.call_asset.country_code    = "US"
                asset.name = f"{campaign_name} Call"

                asset_response   = asset_service.mutate_assets(
                    customer_id=customer_id, operations=[asset_op]
                )
                asset_resource = asset_response.results[0].resource_name

                # Link asset to campaign
                camp_asset_service = client.get_service("CampaignAssetService")
                link_op  = client.get_type("CampaignAssetOperation")
                link     = link_op.create
                link.campaign      = camp_resource
                link.asset         = asset_resource
                link.field_type    = client.enums.AssetFieldTypeEnum.CALL
                camp_asset_service.mutate_campaign_assets(
                    customer_id=customer_id, operations=[link_op]
                )
                log.append(f"  ✓ Call extension linked to campaign")
            except Exception as ce:
                log.append(f"  ⚠ Call extension failed (non-fatal): {ce}")
        else:
            log.append("Step 8: No phone configured — skipping call extension")

        # ── Step 9: Enable campaign ───────────────────────────────────────────
        log.append("Step 9: Enabling campaign (PAUSED → ENABLED)")
        enable_result = set_campaign_status(camp_resource, "ENABLED")
        if enable_result["ok"]:
            log.append(f"  ✓ Campaign ENABLED and live in Google Ads")
        else:
            log.append(f"  ⚠ Enable failed: {enable_result['error']} — campaign remains PAUSED in Google Ads. Enable manually.")

        logger.info(
            f"create_campaign_in_gads: '{campaign_name}' created. "
            f"resource={camp_resource} kw={keywords_added} ads={ads_created}"
        )

        return {
            "ok":                     True,
            "campaign_resource_name": camp_resource,
            "campaign_numeric_id":    camp_numeric,
            "ad_group_resources":     ad_group_resources,
            "keywords_added":         keywords_added,
            "ads_created":            ads_created,
            "enabled":                enable_result["ok"],
            "error":                  None if enable_result["ok"] else f"Created but not enabled: {enable_result['error']}",
            "log":                    log,
        }

    except Exception as e:
        logger.error(f"create_campaign_in_gads failed: {e}", exc_info=True)
        log.append(f"FATAL ERROR: {e}")
        return {"ok": False, "error": str(e), "log": log}


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
            # Only surface explicit per-keyword max-CPC overrides.
            # effective_cpc_bid_micros falls back to $0.01 floor for paused
            # campaigns — that value is real but meaningless to display.
            cpc_usd = round(cpc_micros / 1_000_000.0, 2) if cpc_micros else None
            eff_cpc_usd = round(eff_micros / 1_000_000.0, 2) if eff_micros else None

            entry = {
                "keyword":       kw_text,
                "match_type":    match_type_clean,
                "cpc_bid":       cpc_usd,       # explicit override only (None if inherited)
                "effective_cpc": eff_cpc_usd,   # Google's effective floor (informational)
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
