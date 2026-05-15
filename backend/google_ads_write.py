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


# ── 6. Landing page / final_urls — NOT supported via API update ───────────────
#
# Google Ads RSA ad.final_urls is IMMUTABLE after creation (IMMUTABLE_FIELD).
# AdGroup does not have a final_urls field in the API.
# To change a landing page, ads must be paused and recreated.
# There is intentionally no function here — call sites must handle this explicitly.

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
                # Use 'is not None' guard so radius=0 passes the check (then clamped to 1 by max()).
                # Note: `or 15` means radius=0 becomes 15 (a safe default, since radius=0 is meaningless).
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
                    # Strip state suffix if present: "Grafton, MA" → city="Grafton", state="MA"
                    city_part  = loc_value.split(",")[0].strip()
                    # Guard against "Grafton," (trailing comma) → empty state → default MA
                    raw_state  = loc_value.split(",")[1].strip() if "," in loc_value else ""
                    state_part = raw_state or "MA"
                    if not raw_state:
                        logger.info(f"  Geo: no state in '{loc_value}', defaulting to MA")

                    # ProximityCriterion REQUIRES geo_point — address-only is display-only
                    # and will cause the criterion to fail or target the wrong location.
                    from google_ads_create import _resolve_city_latlng
                    lat, lng = _resolve_city_latlng(city_part, state_part, errors)
                    if lat is None or lng is None:
                        # error already appended to errors list by helper
                        continue

                    op = client.get_type("CampaignCriterionOperation")
                    crit = op.create
                    crit.campaign = campaign_resource
                    # geo_point drives the radius circle in Google Ads
                    crit.proximity.geo_point.latitude_in_micro_degrees  = int(round(lat  * 1_000_000))
                    crit.proximity.geo_point.longitude_in_micro_degrees = int(round(lng  * 1_000_000))
                    # address is display-only but still useful for the UI
                    crit.proximity.address.city_name     = city_part
                    crit.proximity.address.province_code = state_part
                    crit.proximity.address.country_code  = "US"
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
    # Both types must be fetched so the default Grafton proximity is also removed on edit.
    # Enum values in GAQL IN-lists are quoted strings (GAQL accepts both forms; quoted is safer).
    existing_rns = []
    try:
        query = f"""
            SELECT campaign_criterion.resource_name,
                   campaign_criterion.type
            FROM campaign_criterion
            WHERE campaign_criterion.campaign = '{campaign_resource}'
              AND campaign_criterion.type IN ('LOCATION', 'PROXIMITY')
        """
        for row in ga_service.search(customer_id=customer_id, query=query):
            existing_rns.append(row.campaign_criterion.resource_name)
            logger.info(
                f"  existing geo crit to remove: {row.campaign_criterion.resource_name} "
                f"type={row.campaign_criterion.type.name}"
            )
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


# ── 6. Remove a campaign-level negative keyword ───────────────────────────────

def remove_negative_keyword_from_campaign(
    campaign_resource: str,
    keyword_text: str,
    match_type: str = "PHRASE",
) -> bool:
    """
    Remove a campaign-level negative keyword if it exists.

    First fetches the campaign_criterion resource_name via GAQL, then issues
    a remove mutation. Returns True if removed or if the keyword did not exist
    (idempotent). Raises on API-level errors.

    Used by the competitor intel engine to suppress brand negatives for national
    chains that are actively advertising the same service (conquest intent).
    """
    match_type = (match_type or "PHRASE").upper()
    if match_type not in ("EXACT", "PHRASE", "BROAD"):
        raise ValueError(f"Invalid match_type '{match_type}'")

    customer_id = _customer_id_from_resource(campaign_resource)
    client = _build_client()
    ga_service = client.get_service("GoogleAdsService")

    # Step 1: find the criterion resource_name
    # Escape single quotes for GAQL by doubling them
    _kw_escaped = keyword_text.replace("'", "''")
    # Note: campaign_criterion.type is an enum — must NOT be quoted in GAQL.
    # Filter by match_type to avoid removing the wrong criterion when the same
    # text exists as both PHRASE and EXACT negatives.
    query = f"""
        SELECT campaign_criterion.resource_name
        FROM campaign_criterion
        WHERE campaign_criterion.campaign = '{campaign_resource}'
          AND campaign_criterion.negative = TRUE
          AND campaign_criterion.keyword.text = '{_kw_escaped}'
          AND campaign_criterion.keyword.match_type = {match_type}
          AND campaign_criterion.type = KEYWORD
        LIMIT 1
    """
    try:
        response = ga_service.search(customer_id=customer_id, query=query)
        criterion_rn = None
        for row in response:
            criterion_rn = row.campaign_criterion.resource_name
            break
    except Exception as e:
        logger.error(f"remove_negative_keyword_from_campaign GAQL failed: {e}")
        raise

    if not criterion_rn:
        logger.info(
            f"remove_negative_keyword_from_campaign: '{keyword_text}' not found on "
            f"{campaign_resource} — idempotent success"
        )
        return True  # already absent — idempotent

    # Step 2: remove the criterion
    service = client.get_service("CampaignCriterionService")
    operation = client.get_type("CampaignCriterionOperation")
    operation.remove = criterion_rn

    try:
        service.mutate_campaign_criteria(
            customer_id=customer_id,
            operations=[operation],
        )
        logger.info(
            f"Removed negative '{keyword_text}' [{match_type}] from {campaign_resource}"
        )
        return True
    except Exception as e:
        logger.error(f"remove_negative_keyword_from_campaign mutate failed: {e}")
        raise


# ── SKAG: Create Single Keyword Ad Group ──────────────────────────────────────

def create_skag_ad_group(
    customer_id: str,
    campaign_resource: str,
    new_ad_group_name: str,
    keyword_text: str,
    source_ad_group_resource: str = "",
    cpc_bid_micros: int = 0,
) -> dict:
    """
    Create a SKAG (Single Keyword Ad Group) in Google Ads.

    Steps:
    1. Create a new ENABLED ad group under `campaign_resource`.
    2. Add the keyword as EXACT match (NEVER BROAD or PHRASE for a SKAG).
    3. Copy the first ENABLED RSA from the source ad group verbatim.
       RSA headlines/descriptions are copied as-is (per SKAG rule: no editing).

    Args:
        customer_id:            GAds customer ID string (digits only, no dashes)
        campaign_resource:      e.g. customers/NNN/campaigns/MMM
        new_ad_group_name:      Ad group name (max 255 chars)
        keyword_text:           The single keyword for this SKAG
        source_ad_group_resource: resource of the source ad group (for RSA copy)
        cpc_bid_micros:         CPC bid; 0 = inherit from campaign

    Returns:
        {
          "ad_group_resource": str,    # new ad group resource name
          "keyword_resource":  str,    # new keyword criterion resource name
          "ad_resource":       str,    # new RSA resource name (empty if none copied)
          "rsa_copied":        bool,   # True if an RSA was copied
        }

    Raises ValueError on bad inputs; raises GoogleAdsException on API failure.
    Does NOT check kill switch — caller must check first.
    """
    if not campaign_resource or not campaign_resource.startswith("customers/"):
        raise ValueError(f"Invalid campaign_resource: '{campaign_resource}'")
    if not new_ad_group_name or not new_ad_group_name.strip():
        raise ValueError("new_ad_group_name is required")
    if not keyword_text or not keyword_text.strip():
        raise ValueError("keyword_text is required")
    if len(new_ad_group_name) > 255:
        new_ad_group_name = new_ad_group_name[:255]

    client = _build_client()

    # ── Step 1: Create the ad group ──────────────────────────────────────────
    ag_service = client.get_service("AdGroupService")
    ag_op = client.get_type("AdGroupOperation")
    ag = ag_op.create
    ag.name = new_ad_group_name.strip()
    ag.campaign = campaign_resource
    ag.status = client.enums.AdGroupStatusEnum.ENABLED
    if cpc_bid_micros > 0:
        ag.cpc_bid_micros = cpc_bid_micros

    try:
        ag_response = ag_service.mutate_ad_groups(
            customer_id=customer_id,
            operations=[ag_op],
        )
        new_ag_resource = ag_response.results[0].resource_name
        logger.info(
            "SKAG: created ad group '%s' → %s", new_ad_group_name, new_ag_resource
        )
    except Exception as e:
        logger.error("SKAG: create_ad_group failed: %s", e)
        raise

    # ── Step 2: Add keyword as EXACT match ───────────────────────────────────
    crit_service = client.get_service("AdGroupCriterionService")
    crit_op = client.get_type("AdGroupCriterionOperation")
    crit = crit_op.create
    crit.ad_group = new_ag_resource
    crit.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
    crit.keyword.text = keyword_text.strip()
    crit.keyword.match_type = client.enums.KeywordMatchTypeEnum.EXACT

    try:
        crit_response = crit_service.mutate_ad_group_criteria(
            customer_id=customer_id,
            operations=[crit_op],
        )
        kw_resource = crit_response.results[0].resource_name
        logger.info(
            "SKAG: added keyword [EXACT] '%s' → %s", keyword_text, kw_resource
        )
    except Exception as e:
        logger.error("SKAG: add_keyword failed for '%s': %s", keyword_text, e)
        raise

    # ── Step 3: Copy first RSA from source ad group ──────────────────────────
    ad_resource = ""
    rsa_copied = False

    if source_ad_group_resource:
        try:
            _ag_id = source_ad_group_resource.split("/adGroups/")[-1]
            ga_service = client.get_service("GoogleAdsService")
            rsa_query = f"""
                SELECT
                    ad_group_ad.ad.responsive_search_ad.headlines,
                    ad_group_ad.ad.responsive_search_ad.descriptions,
                    ad_group_ad.ad.final_urls,
                    ad_group_ad.ad.display_url,
                    ad_group_ad.ad.responsive_search_ad.path1,
                    ad_group_ad.ad.responsive_search_ad.path2
                FROM ad_group_ad
                WHERE ad_group.resource_name = '{source_ad_group_resource}'
                  AND ad_group_ad.status = 'ENABLED'
                  AND ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'
                LIMIT 1
            """
            rsa_rows = list(ga_service.search(customer_id=customer_id, query=rsa_query))

            if rsa_rows:
                src = rsa_rows[0].ad_group_ad.ad
                src_rsa = src.responsive_search_ad
                final_urls = list(src.final_urls)

                if final_urls and src_rsa.headlines and src_rsa.descriptions:
                    ad_service = client.get_service("AdGroupAdService")
                    ad_op = client.get_type("AdGroupAdOperation")
                    new_ad = ad_op.create
                    new_ad.ad_group = new_ag_resource
                    new_ad.status = client.enums.AdGroupAdStatusEnum.ENABLED
                    new_rsa = new_ad.ad.responsive_search_ad

                    # Copy headlines verbatim (SKAG rule: do not edit RSAs)
                    for h in src_rsa.headlines:
                        new_h = new_rsa.headlines.add()
                        new_h.text = h.text
                        if h.HasField("pinned_field"):
                            new_h.pinned_field = h.pinned_field

                    # Copy descriptions verbatim
                    for d in src_rsa.descriptions:
                        new_d = new_rsa.descriptions.add()
                        new_d.text = d.text
                        if d.HasField("pinned_field"):
                            new_d.pinned_field = d.pinned_field

                    # Copy paths if present
                    if src_rsa.path1:
                        new_rsa.path1 = src_rsa.path1
                    if src_rsa.path2:
                        new_rsa.path2 = src_rsa.path2

                    # Copy final URLs (IMMUTABLE — must be set at creation)
                    new_ad.ad.final_urls.extend(final_urls)

                    try:
                        ad_response = ad_service.mutate_ad_group_ads(
                            customer_id=customer_id,
                            operations=[ad_op],
                        )
                        ad_resource = ad_response.results[0].resource_name
                        rsa_copied = True
                        logger.info(
                            "SKAG: copied RSA from '%s' → %s",
                            source_ad_group_resource, ad_resource
                        )
                    except Exception as e:
                        # Non-fatal — ad group + keyword already created; RSA copy failure
                        # is logged and handled gracefully (ad can be added manually).
                        logger.warning(
                            "SKAG: RSA copy failed (non-fatal) for '%s': %s",
                            new_ag_resource, e
                        )
                else:
                    logger.info(
                        "SKAG: source ad group has no complete RSA to copy from %s",
                        source_ad_group_resource
                    )
        except Exception as e:
            # Non-fatal — ad group + keyword already created
            logger.warning("SKAG: RSA fetch/copy failed (non-fatal): %s", e)

    return {
        "ad_group_resource": new_ag_resource,
        "keyword_resource":  kw_resource,
        "ad_resource":       ad_resource,
        "rsa_copied":        rsa_copied,
    }
