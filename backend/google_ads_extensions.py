"""
CallRail PR 3 — Google Ads Call Extension (CallAsset) management.

Functions:
  find_call_assets_on_campaign(campaign_resource)
      → list all active CALL CampaignAssets on a campaign

  push_call_extension_to_campaign(campaign_resource, phone_number_e164, friendly_name)
      → idempotent upsert: create CallAsset + CampaignAsset link, read-back verify

  remove_call_extension_from_campaign(campaign_resource, campaign_asset_resource)
      → remove the CampaignAsset link (orphaned CallAsset is harmless)

All write functions:
  - Check the kill switch (check_writes_enabled) before any API call
  - Verify via read-back after create
  - Log to gads_audit_log via log_admin_manual_action
  - Return a status dict — never raise to the caller (errors are captured in the dict)

Pattern mirrors google_ads_create.py Step 8 (lines 988-1018) which already
does this successfully at campaign-create time.
"""
import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_E164_RE = re.compile(r"^\+1\d{10}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_client():
    """Re-use the same client builder as google_ads_sync."""
    from google_ads_write import _build_client as _sync_build
    return _sync_build()


def _customer_id_from_resource(resource_name: str) -> str:
    """Parse 'customers/NNNN/...' → 'NNNN'."""
    from google_ads_write import _customer_id_from_resource as _base
    return _base(resource_name)


# ── Read ──────────────────────────────────────────────────────────────────────

def find_call_assets_on_campaign(campaign_resource: str) -> list[dict]:
    """
    Return all non-REMOVED CALL CampaignAssets currently on this campaign.

    Returns list of dicts:
      {
        "campaign_asset_resource": str,
        "asset_resource":           str,
        "phone_number":             str,   # digits-only as stored by GAds
        "country_code":             str,
        "status":                   str,   # "ENABLED" | "PAUSED"
      }
    """
    try:
        customer_id = _customer_id_from_resource(campaign_resource)
        client = _build_client()
        ga_service = client.get_service("GoogleAdsService")

        query = f"""
            SELECT
              campaign_asset.resource_name,
              campaign_asset.asset,
              campaign_asset.status,
              asset.call_asset.phone_number,
              asset.call_asset.country_code
            FROM campaign_asset
            WHERE campaign_asset.campaign = '{campaign_resource}'
              AND campaign_asset.field_type = CALL
              AND campaign_asset.status != REMOVED
        """
        stream = ga_service.search_stream(customer_id=customer_id, query=query)
        results = []
        for batch in stream:
            for row in batch.results:
                ca = row.campaign_asset
                a  = row.asset
                results.append({
                    "campaign_asset_resource": ca.resource_name,
                    "asset_resource":           ca.asset,
                    "phone_number":             a.call_asset.phone_number,
                    "country_code":             a.call_asset.country_code,
                    "status":                   ca.status.name,
                })
        logger.debug("[gads_ext] find_call_assets_on_campaign %s → %d results",
                     campaign_resource, len(results))
        return results
    except Exception as e:
        logger.error("[gads_ext] find_call_assets_on_campaign failed: %s", e)
        return []


# ── Write — push ──────────────────────────────────────────────────────────────

def push_call_extension_to_campaign(
    campaign_resource: str,
    phone_number_e164: str,
    friendly_name: str = "",
) -> dict:
    """
    Idempotent upsert of a CALL CampaignAsset on the given campaign.

    Algorithm:
      1. Validate inputs + check kill switch
      2. Query existing CALL CampaignAssets on the campaign
      3. If one already matches this phone → reuse it (no write), return 'reused'
      4. Remove any OTHER existing CALL CampaignAssets (keep 1:1 campaign:number)
      5. Create a new CallAsset via AssetService
      6. Link it via CampaignAssetService with field_type=CALL
      7. Read-back verify

    Returns:
      {
        "ok":                      bool,
        "action":                  "created" | "reused",
        "asset_resource":          str,
        "campaign_asset_resource": str,
        "phone_number":            str,
        "removed_old":             [str],   # campaign_asset resources removed
        "errors":                  [str],
      }
    """
    result = {
        "ok": False, "action": "", "asset_resource": "",
        "campaign_asset_resource": "", "phone_number": phone_number_e164,
        "removed_old": [], "errors": [],
    }

    # ── 1. Validate ──────────────────────────────────────────────────────────
    if not _E164_RE.match(phone_number_e164):
        result["errors"].append(f"phone_number_e164 must be +1XXXXXXXXXX, got: {phone_number_e164!r}")
        return result

    try:
        from campaign_safety import check_writes_enabled, WriteBlockedError
        check_writes_enabled()
    except Exception as e:
        result["errors"].append(f"Kill switch active: {e}")
        return result

    customer_id = _customer_id_from_resource(campaign_resource)
    if not customer_id:
        result["errors"].append(f"Cannot parse customer_id from {campaign_resource!r}")
        return result

    # Phone digits as GAds stores them (strip leading +)
    phone_digits = "".join(ch for ch in phone_number_e164 if ch.isdigit())  # "15085459356"

    # ── 2. Query existing ────────────────────────────────────────────────────
    existing = find_call_assets_on_campaign(campaign_resource)

    # ── 3. Reuse if phone already matches ────────────────────────────────────
    for ex in existing:
        # GAds stores without leading "1" sometimes — normalise both sides
        ex_digits = "".join(ch for ch in ex["phone_number"] if ch.isdigit())
        stripped = phone_digits[1:] if phone_digits.startswith("1") else phone_digits
        if ex_digits == phone_digits or ex_digits == stripped:
            logger.info("[gads_ext] reusing existing call asset %s on campaign %s",
                        ex["campaign_asset_resource"], campaign_resource)
            result.update({
                "ok": True, "action": "reused",
                "asset_resource":          ex["asset_resource"],
                "campaign_asset_resource": ex["campaign_asset_resource"],
            })
            _log_audit("callrail_push_call_extension", campaign_resource, customer_id,
                       before={"call_extension": phone_number_e164},
                       after={"call_extension": phone_number_e164, "action": "reused"})
            return result

    # ── 4. Remove other existing CALL CampaignAssets on this campaign ─────────
    to_remove = [ex["campaign_asset_resource"] for ex in existing]
    if to_remove:
        rm_result = _remove_campaign_asset_links(customer_id, to_remove)
        result["removed_old"] = rm_result["removed"]
        if rm_result["errors"]:
            result["errors"].extend(rm_result["errors"])
            logger.warning("[gads_ext] partial removal errors: %s", rm_result["errors"])

    # ── 5. Create CallAsset ───────────────────────────────────────────────────
    try:
        client = _build_client()
        asset_service = client.get_service("AssetService")
        asset_op = client.get_type("AssetOperation")
        asset = asset_op.create
        asset.call_asset.phone_number = phone_digits
        asset.call_asset.country_code = "US"
        asset.name = (friendly_name or f"CallRail {phone_number_e164}")[:128]

        asset_resp = asset_service.mutate_assets(
            customer_id=customer_id,
            operations=[asset_op],
        )
        new_asset_resource = asset_resp.results[0].resource_name
        logger.info("[gads_ext] created CallAsset %s", new_asset_resource)
    except Exception as e:
        result["errors"].append(f"AssetService.mutate_assets failed: {e}")
        logger.error("[gads_ext] create CallAsset failed: %s", e)
        _log_audit("callrail_push_call_extension_failed", campaign_resource, customer_id,
                   before={"call_extension": ""},
                   after={"call_extension": phone_number_e164, "error": str(e)})
        return result

    # ── 6. Link to campaign ───────────────────────────────────────────────────
    try:
        cas = client.get_service("CampaignAssetService")
        link_op = client.get_type("CampaignAssetOperation")
        link = link_op.create
        link.campaign   = campaign_resource
        link.asset      = new_asset_resource
        link.field_type = client.enums.AssetFieldTypeEnum.CALL

        link_resp = cas.mutate_campaign_assets(
            customer_id=customer_id,
            operations=[link_op],
        )
        new_campaign_asset_resource = link_resp.results[0].resource_name
        logger.info("[gads_ext] linked CampaignAsset %s", new_campaign_asset_resource)
    except Exception as e:
        result["errors"].append(f"CampaignAssetService.mutate_campaign_assets failed: {e}")
        logger.error("[gads_ext] link CampaignAsset failed: %s", e)
        _log_audit("callrail_push_call_extension_failed", campaign_resource, customer_id,
                   before={"call_extension": ""},
                   after={"call_extension": phone_number_e164,
                          "asset_resource": new_asset_resource, "error": str(e)})
        return result

    # ── 7. Read-back verify ───────────────────────────────────────────────────
    verify = find_call_assets_on_campaign(campaign_resource)
    confirmed = [v for v in verify if v["campaign_asset_resource"] == new_campaign_asset_resource]
    if not confirmed:
        msg = (f"Read-back failed: CampaignAsset {new_campaign_asset_resource} not visible "
               f"after create. CallRail+DB committed; manual GAds review may be needed.")
        result["errors"].append(msg)
        logger.error("[gads_ext] %s", msg)
        _log_audit("callrail_push_call_extension_failed", campaign_resource, customer_id,
                   before={"call_extension": ""},
                   after={"call_extension": phone_number_e164,
                          "campaign_asset_resource": new_campaign_asset_resource,
                          "error": "read-back failed"})
        return result

    result.update({
        "ok": True, "action": "created",
        "asset_resource":          new_asset_resource,
        "campaign_asset_resource": new_campaign_asset_resource,
    })
    _log_audit("callrail_push_call_extension", campaign_resource, customer_id,
               before={"call_extension": ""},
               after={"call_extension": phone_number_e164,
                      "asset_resource": new_asset_resource,
                      "campaign_asset_resource": new_campaign_asset_resource})
    logger.info("[gads_ext] push_call_extension_to_campaign OK: %s → %s",
                phone_number_e164, campaign_resource)
    return result


# ── Write — remove ────────────────────────────────────────────────────────────

def remove_call_extension_from_campaign(
    campaign_resource: str,
    campaign_asset_resource: str = "",
) -> dict:
    """
    Remove the CALL CampaignAsset link from the campaign.
    Does NOT delete the underlying CallAsset (orphaned assets are harmless).

    If campaign_asset_resource is provided, removes only that one.
    If empty, removes ALL CALL CampaignAssets on the campaign.

    Returns:
      { "ok": bool, "removed": int, "resources_removed": [str], "errors": [str] }
    """
    result = {"ok": False, "removed": 0, "resources_removed": [], "errors": []}

    try:
        from campaign_safety import check_writes_enabled, WriteBlockedError
        check_writes_enabled()
    except Exception as e:
        result["errors"].append(f"Kill switch active: {e}")
        return result

    customer_id = _customer_id_from_resource(campaign_resource)

    if campaign_asset_resource:
        to_remove = [campaign_asset_resource]
    else:
        existing = find_call_assets_on_campaign(campaign_resource)
        to_remove = [ex["campaign_asset_resource"] for ex in existing]

    if not to_remove:
        result["ok"] = True
        logger.debug("[gads_ext] remove_call_extension_from_campaign: nothing to remove")
        return result

    rm = _remove_campaign_asset_links(customer_id, to_remove)
    result.update({
        "ok":               not rm["errors"],
        "removed":          rm["removed_count"],
        "resources_removed": rm["removed"],
        "errors":           rm["errors"],
    })

    _log_audit("callrail_remove_call_extension", campaign_resource, customer_id,
               before={"campaign_asset_resources": to_remove},
               after={"removed_count": rm["removed_count"]})
    return result


# ── Internal helpers ──────────────────────────────────────────────────────────

def _remove_campaign_asset_links(customer_id: str, resource_names: list[str]) -> dict:
    """
    Low-level: remove a list of CampaignAsset resource names via operation.remove.
    Returns { "removed": [str], "removed_count": int, "errors": [str] }
    """
    removed = []
    errors = []
    try:
        client = _build_client()
        cas = client.get_service("CampaignAssetService")
        ops = []
        for rn in resource_names:
            op = client.get_type("CampaignAssetOperation")
            op.remove = rn
            ops.append(op)
        cas.mutate_campaign_assets(customer_id=customer_id, operations=ops)
        removed = resource_names
        logger.info("[gads_ext] removed %d CampaignAsset(s): %s", len(removed), removed)
    except Exception as e:
        errors.append(f"CampaignAssetService remove failed: {e}")
        logger.error("[gads_ext] _remove_campaign_asset_links failed: %s", e)
    return {"removed": removed, "removed_count": len(removed), "errors": errors}


def _log_audit(operation: str, campaign_resource: str, customer_id: str,
               before: dict, after: dict) -> None:
    """Fire-and-forget audit log entry — swallow exceptions so callers don't fail."""
    try:
        from database import log_admin_manual_action
        # Extract campaign numeric ID from resource "customers/N/campaigns/M"
        parts = campaign_resource.split("/")
        campaign_numeric_id = parts[-1] if parts else campaign_resource
        log_admin_manual_action(
            operation=operation,
            entity_type="campaign",
            entity_id=campaign_numeric_id,
            entity_name=campaign_resource,
            before=before,
            after=after,
            reason="callrail_pr3_auto_push",
        )
    except Exception as e:
        logger.warning("[gads_ext] audit log failed (non-fatal): %s", e)
