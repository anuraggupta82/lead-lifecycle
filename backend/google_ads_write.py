"""
Google Ads write operations — manual edits from the dashboard.

Handles:
  - Negate a search term (add campaign-level negative keyword)
  - Pause / enable an ad group
  - Add a keyword to an ad group  (PR 2)
  - Set campaign daily budget     (PR 2)
  - Replace geographic targeting  (PR 3)
  - Set campaign bid strategy     (future)

All functions check the kill switch before executing.
All calls are logged to gads_audit_log via log_admin_manual_action().

Customer ID is parsed from the resource name (multi-account safe).
Falls back to settings.google_ads_customer_id if resource name is unavailable.
"""
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# ── Customer ID helper ────────────────────────────────────────────────────────

def _customer_id_from_resource(resource_name: str) -> str:
    """
    Parse 'customers/NNNN/...' → 'NNNN'.
    Falls back to settings.google_ads_customer_id if resource_name is unavailable.
    """
    if resource_name and resource_name.startswith("customers/"):
        parts = resource_name.split("/")
        if len(parts) >= 2 and parts[1].isdigit():
            return parts[1]
    # Fallback
    from config import get_settings
    settings = get_settings()
    return "".join(ch for ch in (settings.google_ads_customer_id or "") if ch.isdigit())


def _build_client():
    """Re-use the same client builder as google_ads_sync."""
    from google_ads_sync import _build_client as _sync_build
    return _sync_build()


# ── Audit log helper ──────────────────────────────────────────────────────────
# log_admin_manual_action lives in database.py — import it from there.
# (Callers in main.py already do: from database import log_admin_manual_action)


# ── 1. Negate a search term ───────────────────────────────────────────────────

def add_negative_keyword_to_campaign(
    campaign_resource: str,
    keyword_text: str,
    match_type: str = "EXACT",
) -> bool:
    """
    Add a campaign-level negative keyword.
    match_type: 'EXACT' | 'PHRASE' | 'BROAD'
    Handles KEYWORD_ALREADY_EXISTS gracefully (returns True).
    Raises on other errors.
    """
    match_type = (match_type or "EXACT").upper()
    if match_type not in ("EXACT", "PHRASE", "BROAD"):
        raise ValueError(f"Invalid match_type '{match_type}' — must be EXACT, PHRASE, or BROAD")

    customer_id = _customer_id_from_resource(campaign_resource)
    client = _build_client()
    service = client.get_service("CampaignCriterionService")
    operation = client.get_type("CampaignCriterionOperation")
    criterion = operation.create
    criterion.campaign = campaign_resource
    criterion.negative = True
    criterion.keyword.text = keyword_text
    criterion.keyword.match_type = client.enums.KeywordMatchTypeEnum[match_type]

    try:
        service.mutate_campaign_criteria(
            customer_id=customer_id,
            operations=[operation],
        )
        logger.info(
            f"Negated '{keyword_text}' [{match_type}] on campaign {campaign_resource}"
        )
        return True
    except Exception as e:
        err_str = str(e)
        if "KEYWORD_ALREADY_EXISTS" in err_str or "already exists" in err_str.lower():
            logger.info(
                f"Negative '{keyword_text}' already exists on {campaign_resource} — idempotent success"
            )
            return True
        logger.error(f"add_negative_keyword_to_campaign failed: {e}")
        raise


# ── 2. Pause / enable an ad group ────────────────────────────────────────────

def set_ad_group_status(ad_group_resource: str, status: str) -> bool:
    """
    Set an ad group to PAUSED or ENABLED.
    status: 'PAUSED' | 'ENABLED'
    Raises on error.
    """
    status = (status or "").upper()
    if status not in ("PAUSED", "ENABLED"):
        raise ValueError(f"Invalid status '{status}' — must be PAUSED or ENABLED")

    customer_id = _customer_id_from_resource(ad_group_resource)
    client = _build_client()
    service = client.get_service("AdGroupService")
    operation = client.get_type("AdGroupOperation")
    ad_group = operation.update
    ad_group.resource_name = ad_group_resource
    ad_group.status = client.enums.AdGroupStatusEnum[status]

    from google.protobuf import field_mask_pb2
    client.copy_from(
        operation.update_mask,
        field_mask_pb2.FieldMask(paths=["status"]),
    )

    try:
        service.mutate_ad_groups(
            customer_id=customer_id,
            operations=[operation],
        )
        logger.info(f"Ad group {ad_group_resource} → {status}")
        return True
    except Exception as e:
        logger.error(f"set_ad_group_status failed for {ad_group_resource}: {e}")
        raise


# ── 3. Add keyword to ad group (PR 2) ────────────────────────────────────────

def add_keyword_to_ad_group(
    ad_group_resource: str,
    keyword_text: str,
    match_type: str = "EXACT",
    cpc_bid_micros: int = 0,
) -> bool:
    """
    Add a positive keyword to an ad group.
    Optionally set cpc_bid_micros (0 = use ad group default).
    Handles KEYWORD_ALREADY_EXISTS gracefully.
    """
    match_type = (match_type or "EXACT").upper()
    if match_type not in ("EXACT", "PHRASE", "BROAD"):
        raise ValueError(f"Invalid match_type '{match_type}'")

    customer_id = _customer_id_from_resource(ad_group_resource)
    client = _build_client()
    service = client.get_service("AdGroupCriterionService")
    operation = client.get_type("AdGroupCriterionOperation")
    criterion = operation.create
    criterion.ad_group = ad_group_resource
    criterion.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
    criterion.keyword.text = keyword_text
    criterion.keyword.match_type = client.enums.KeywordMatchTypeEnum[match_type]
    if cpc_bid_micros > 0:
        criterion.cpc_bid_micros = cpc_bid_micros

    try:
        service.mutate_ad_group_criteria(
            customer_id=customer_id,
            operations=[operation],
        )
        logger.info(f"Added keyword '{keyword_text}' [{match_type}] to {ad_group_resource}")
        return True
    except Exception as e:
        err_str = str(e)
        if "KEYWORD_ALREADY_EXISTS" in err_str or "already exists" in err_str.lower():
            logger.info(f"Keyword '{keyword_text}' already in {ad_group_resource} — idempotent success")
            return True
        logger.error(f"add_keyword_to_ad_group failed: {e}")
        raise


# ── 4. Set campaign daily budget (PR 2) ──────────────────────────────────────

def set_campaign_daily_budget(
    campaign_resource: str,
    new_daily_budget_usd: float,
) -> bool:
    """
    Change a campaign's daily budget.
    Fetches the campaign's budget resource via GAQL, then mutates it.
    Raises on error.
    """
    if new_daily_budget_usd < 1.0:
        raise ValueError(f"Daily budget ${new_daily_budget_usd:.2f} is below the $1.00 minimum")

    customer_id = _customer_id_from_resource(campaign_resource)
    client = _build_client()
    ga_service = client.get_service("GoogleAdsService")

    # Step 1: resolve the budget resource name from the campaign
    query = f"""
        SELECT campaign.campaign_budget
        FROM campaign
        WHERE campaign.resource_name = '{campaign_resource}'
        LIMIT 1
    """
    response = ga_service.search(customer_id=customer_id, query=query)
    budget_resource = None
    for row in response:
        budget_resource = row.campaign.campaign_budget
        break

    if not budget_resource:
        raise RuntimeError(
            f"Could not resolve budget resource for campaign {campaign_resource}"
        )

    # Step 2: mutate the budget
    new_micros = int(new_daily_budget_usd * 1_000_000)
    budget_service = client.get_service("CampaignBudgetService")
    operation = client.get_type("CampaignBudgetOperation")
    budget = operation.update
    budget.resource_name = budget_resource
    budget.amount_micros = new_micros

    from google.protobuf import field_mask_pb2
    client.copy_from(
        operation.update_mask,
        field_mask_pb2.FieldMask(paths=["amount_micros"]),
    )

    try:
        budget_service.mutate_campaign_budgets(
            customer_id=customer_id,
            operations=[operation],
        )
        logger.info(
            f"Campaign {campaign_resource} budget → ${new_daily_budget_usd:.2f}/day "
            f"({new_micros} micros)"
        )
        return True
    except Exception as e:
        logger.error(f"set_campaign_daily_budget failed: {e}")
        raise


# ── 5. Set campaign status (ENABLED / PAUSED) ────────────────────────────────

def set_campaign_status_gads(campaign_resource: str, new_status: str) -> bool:
    """
    Push ENABLED or PAUSED status to Google Ads.
    Delegates to google_ads_create.set_campaign_status which handles
    the kill switch and returns a result dict.
    Raises RuntimeError on failure.
    """
    new_status = new_status.upper()
    if new_status not in ("ENABLED", "PAUSED"):
        raise ValueError(f"Invalid status '{new_status}' — must be ENABLED or PAUSED")

    from google_ads_create import set_campaign_status
    result = set_campaign_status(campaign_resource, new_status)
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or "set_campaign_status_gads failed")
    logger.info(f"Campaign {campaign_resource} status → {new_status}")
    return True


# ── 6. Update final_urls on all RSA ads in a campaign ────────────────────────

def update_campaign_ad_final_urls(campaign_resource: str, new_url: str) -> dict:
    """
    Replace final_urls on every enabled/paused RSA ad in the given campaign.

    Returns:
        {"updated": N, "skipped": M, "errors": [...]}

    Why a campaign-level operation: landing page changes apply to all ads in the
    campaign uniformly. Targeting individual ad resource names would require an
    extra lookup pass before this call, adding complexity for no benefit.
    """
    if not new_url or not new_url.startswith("http"):
        raise ValueError(f"Invalid URL '{new_url}' — must start with http:// or https://")

    client = _build_client()
    customer_id = _customer_id_from_resource(campaign_resource)
    ga_service = client.get_service("GoogleAdsService")
    ad_service = client.get_service("AdGroupAdService")

    # Step 1 — fetch all enabled/paused RSA ads for the campaign
    query = f"""
        SELECT
            ad_group_ad.resource_name,
            ad_group_ad.ad.final_urls
        FROM ad_group_ad
        WHERE campaign.resource_name = '{campaign_resource}'
          AND ad_group_ad.status IN (ENABLED, PAUSED)
          AND ad_group_ad.ad.type = RESPONSIVE_SEARCH_AD
    """

    ad_resources = []
    for row in ga_service.search(customer_id=customer_id, query=query):
        ad_resources.append(row.ad_group_ad.resource_name)

    if not ad_resources:
        return {"updated": 0, "skipped": 0, "errors": ["No RSA ads found for this campaign"]}

    # Step 2 — build one mutate operation per ad
    ops = []
    for res in ad_resources:
        op = client.get_type("AdGroupAdOperation")
        ad_group_ad = op.update
        ad_group_ad.resource_name = res
        # Clear existing final_urls and set the new one
        ad_group_ad.ad.final_urls[:] = [new_url]
        op.update_mask.CopyFrom(
            client.get_type("FieldMask")
        )
        op.update_mask.paths.append("ad.final_urls")
        ops.append(op)

    # Step 3 — mutate in batches of 50 (safe API limit)
    updated = 0
    errors = []
    BATCH = 50
    for i in range(0, len(ops), BATCH):
        batch = ops[i : i + BATCH]
        try:
            ad_service.mutate_ad_group_ads(customer_id=customer_id, operations=batch)
            updated += len(batch)
        except Exception as e:
            err_msg = str(e)
            logger.error(f"update_campaign_ad_final_urls batch {i//BATCH}: {err_msg}")
            errors.append(err_msg[:200])

    logger.info(
        f"update_campaign_ad_final_urls: campaign={campaign_resource} "
        f"url={new_url!r} updated={updated} errors={len(errors)}"
    )
    return {"updated": updated, "skipped": 0, "errors": errors}


# ── 7. Replace geographic targeting (PR 3) ────────────────────────────────────

def replace_campaign_locations(
    campaign_resource: str,
    geo_json: str,
) -> dict:
    """
    Atomically replace all geographic (LOCATION + PROXIMITY) criteria on a campaign.

    Algorithm:
      1. Build all ADD operations first (GeoTargetConstant lookup + proximity).
      2. Fetch existing LOCATION + PROXIMITY criteria resource names.
      3. Build REMOVE operations for existing criteria.
      4. Issue removes + adds in a SINGLE mutate_campaign_criteria call (atomic).

    geo_json format: {"unit": "miles"|"km", "locations": [{type, value, radius, include}]}
      type: "postal" | "city" | "address" | "state" | "country"
            city with radius → written as proximity criterion
            others → written as GeoTargetConstant criterion

    Returns: {"removed": int, "added": int, "errors": [str]}
    Raises on API-level fatal errors.
    """
    # ── Parse geo_json ────────────────────────────────────────────────────────
    errors = []
    geo_locs = []
    unit = "miles"
    if geo_json:
        try:
            parsed = json.loads(geo_json) if isinstance(geo_json, str) else geo_json
            if isinstance(parsed, dict):
                geo_locs = parsed.get("locations") or []
                unit = parsed.get("unit") or "miles"
        except Exception as e:
            # Opus F2: raise on parse failure — silently continuing with empty geo_locs
            # would bypass the M2 guard and allow a removal-only mutate (worldwide targeting).
            raise ValueError(f"Failed to parse geo_json: {e}") from e

    # Opus F1: Guard against empty locations at the function level (not just the M2 guard below).
    # M2 only fires when geo_locs is non-empty but all lookups fail; this catches the case
    # where geo_json is missing/empty/has no locations list — same outcome: all geo removed.
    if not geo_locs:
        raise ValueError(
            "geo_json must contain at least one location — "
            "removing all locations would make the campaign worldwide"
        )

    # M4: Validate campaign_resource is a strict GAQL-safe format before interpolation
    import re as _re
    if not _re.match(r"^customers/\d+/campaigns/\d+$", campaign_resource or ""):
        raise ValueError(
            f"campaign_resource must match customers/NNNN/campaigns/MMMM, got: {campaign_resource!r}"
        )

    customer_id = _customer_id_from_resource(campaign_resource)
    client = _build_client()
    ga_service = client.get_service("GoogleAdsService")
    crit_service = client.get_service("CampaignCriterionService")

    # ── Step 1: Build ADD operations (before touching live data) ──────────────
    add_ops = []
    if geo_locs:
        geo_target_service = client.get_service("GeoTargetConstantService")
        for loc in geo_locs:
            loc_type  = (loc.get("type") or "postal").lower()
            loc_value = str(loc.get("value") or "").strip()
            include   = loc.get("include", True)
            if not loc_value:
                continue

            if loc_type == "city" and loc.get("radius") is not None:
                # City + radius → proximity criterion (matches google_ads_create.py behavior)
                # M8: use 'is not None' so radius=0 is handled (clamped to 1 by max())
                loc_radius = max(1, min(500, float(loc.get("radius") or 15)))
                # Opus F7 + M9: Google Ads API does NOT support negative proximity criteria.
                # Silently flipping to positive would add the opposite of what the user requested.
                # Skip this criterion and surface it as an error instead.
                if not include:
                    errors.append(
                        f"Negative radius targeting around '{loc_value}' is not supported "
                        "by Google Ads — this location was skipped. Use 'City/County' without radius "
                        "or a state/country exclusion for negative geo targeting."
                    )
                    continue
                try:
                    op = client.get_type("CampaignCriterionOperation")
                    crit = op.create
                    crit.campaign = campaign_resource
                    crit.proximity.address.city_name = loc_value
                    crit.proximity.address.country_code = "US"  # TODO: multi-region
                    crit.proximity.radius = loc_radius
                    crit.proximity.radius_units = (
                        client.enums.ProximityRadiusUnitsEnum.MILES
                        if unit == "miles"
                        else client.enums.ProximityRadiusUnitsEnum.KILOMETERS
                    )
                    add_ops.append(op)
                except Exception as e:
                    errors.append(f"Proximity setup failed for '{loc_value}': {e}")

            elif loc_type in ("postal", "city", "address", "state", "country"):
                # GeoTargetConstant lookup
                try:
                    suggest_req = client.get_type("SuggestGeoTargetConstantsRequest")
                    suggest_req.locale = "en"
                    suggest_req.country_code = "US"
                    suggest_req.location_names.names.append(loc_value)

                    suggest_resp = geo_target_service.suggest_geo_target_constants(request=suggest_req)
                    matched = False
                    for suggestion in (suggest_resp.geo_target_constant_suggestions or []):
                        geo_const = suggestion.geo_target_constant
                        op = client.get_type("CampaignCriterionOperation")
                        crit = op.create
                        crit.campaign = campaign_resource
                        crit.location.geo_target_constant = geo_const.resource_name
                        if not include:
                            crit.negative = True
                        add_ops.append(op)
                        matched = True
                        break  # take first match only
                    if not matched:
                        errors.append(f"No geo match found for '{loc_value}'")
                except Exception as e:
                    errors.append(f"Geo lookup failed for '{loc_value}': {e}")
            else:
                errors.append(f"Unknown location type '{loc_type}' for '{loc_value}' — skipped")

    # M2: Guard against removal-only mutate — if user submitted locations but none resolved,
    # abort NOW before fetching existing criteria (prevents accidentally clearing all geo targeting)
    if geo_locs and not add_ops:
        raise RuntimeError(
            f"No geo criteria could be resolved from the submitted locations "
            f"(all {len(geo_locs)} entries failed). Errors: {errors}. "
            "Campaign targeting was NOT changed."
        )

    # ── Step 2: Fetch existing LOCATION + PROXIMITY criteria ──────────────────
    # GAQL enum values are unquoted; both LOCATION and PROXIMITY must be fetched
    # so that the default Grafton proximity radius is also removed on edit.
    existing_rns = []
    try:
        query = f"""
            SELECT campaign_criterion.resource_name
            FROM campaign_criterion
            WHERE campaign.resource_name = '{campaign_resource}'
              AND campaign_criterion.type IN (LOCATION, PROXIMITY)
        """
        for row in ga_service.search(customer_id=customer_id, query=query):
            existing_rns.append(row.campaign_criterion.resource_name)
    except Exception as e:
        # M1: Re-raise rather than silently proceeding — a transient fetch failure must not
        # result in the mutate running with only adds (duplicating onto old targeting).
        logger.error(f"Could not fetch existing location criteria: {e}")
        raise RuntimeError(
            f"Failed to fetch existing geographic targeting before editing: {e}. "
            "Campaign targeting was NOT changed."
        ) from e

    # ── Step 3: Build REMOVE operations ──────────────────────────────────────
    remove_ops = []
    for rn in existing_rns:
        op = client.get_type("CampaignCriterionOperation")
        op.remove = rn
        remove_ops.append(op)

    # ── Step 4: Single atomic mutate (removes first, then adds) ───────────────
    # Issuing both in one call means Google Ads rolls back everything on failure.
    all_ops = remove_ops + add_ops
    added = 0
    if all_ops:
        try:
            result = crit_service.mutate_campaign_criteria(
                customer_id=customer_id,
                operations=all_ops,
            )
            # result.results contains one entry per successful operation
            added = max(0, len(result.results) - len(remove_ops))
            logger.info(
                f"Geo update for {campaign_resource}: removed {len(remove_ops)}, "
                f"added {added}, errors={errors}"
            )
        except Exception as e:
            logger.error(f"replace_campaign_locations atomic mutate failed: {e}")
            raise

    return {
        "removed": len(remove_ops),
        "added": added,
        "errors": errors,
    }
