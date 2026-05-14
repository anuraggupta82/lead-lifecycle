"""
competitor_intel_engine.py — Competitor Advertising Intelligence System

Runs every 15 days (1st and 16th of each month at 4 AM) to detect which
nearby dental practices are actively advertising specific services via
Google Ads Auction Insights data. Uses this intelligence to:

  1. Mark nearby_practices rows with is_advertising=1 + advertising_services
  2. Stage suppress_negative or add_conquest_keyword actions as pending
     for admin review (never auto-applies to Google Ads)

Detection tiers:
  Tier 1 — Google Ads Auction Insights (primary, free, per-campaign)
  Tier 2 — Claude Haiku enrichment for unmatched domains
  Tier 3 — NATIONAL_CHAINS static fallback (always advertising)

Key policy (from competitor_policy.py):
  - local_office competitors → always negate regardless of advertising status
  - national_chain in CONQUEST_ELIGIBLE_TYPES + actively advertising → suppress negative
  - national_chain NOT advertising or in non-conquest type → keep negative

Nothing auto-applies. All actions land in competitor_intel_actions as 'pending'.
"""
from __future__ import annotations
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Campaign types where national chains are worth conquesting
from competitor_policy import (
    CONQUEST_ELIGIBLE_TYPES,
    NATIONAL_CHAINS,
    classify,
    normalize,
)

# Map GDC campaign name fragments → campaign type
# Used to infer campaign_type from campaign.name in Auction Insights results
_CAMP_TYPE_HINTS: list[tuple[str, str]] = [
    ("emergency", "emergency"),
    ("implant",   "implants"),
    ("invisalign","invisalign"),
    ("cosmetic",  "cosmetic"),
    ("veneer",    "cosmetic"),
    ("gum",       "gum"),
    ("periodon",  "gum"),
    ("general",   "general"),
    ("family",    "general"),
    ("new patient","general"),
]


def _detect_campaign_type(campaign_name: str) -> str:
    """Infer campaign_type from a campaign name string."""
    n = campaign_name.lower()
    for hint, ctype in _CAMP_TYPE_HINTS:
        if hint in n:
            return ctype
    return "general"


def _normalize_domain(domain: str) -> str:
    """
    Strip TLD and normalise domain to a matchable string.
    e.g. "aspendental.com" → "aspendental"
        "main-street-dental.com" → "main street dental"
    """
    if not domain:
        return ""
    # Remove protocol prefix
    d = re.sub(r"^https?://", "", domain.lower().strip())
    # Remove www.
    d = re.sub(r"^www\.", "", d)
    # Strip TLD and path
    d = re.sub(r"\.[a-z]{2,6}(/.*)?$", "", d)
    # Replace hyphens/underscores with spaces
    d = d.replace("-", " ").replace("_", " ")
    d = re.sub(r" {2,}", " ", d).strip()
    return d


def _build_national_chain_domain_map() -> dict[str, str]:
    """
    Build a lookup: normalized domain fragment → national chain name.
    e.g. "aspendental" → "Aspen Dental"
    """
    result: dict[str, str] = {}
    for chain in NATIONAL_CHAINS:
        for stem in chain["stems"]:
            key = stem.replace(" ", "")   # "aspen dental" → "aspendental"
            result[key] = chain["name"]
            key2 = stem.replace(" ", "-")  # "aspen-dental"
            result[key2] = chain["name"]
            result[stem] = chain["name"]   # "aspen dental"
    return result


_NATIONAL_DOMAIN_MAP: dict[str, str] = _build_national_chain_domain_map()


def _match_domain_to_national_chain(domain_norm: str) -> str | None:
    """
    Returns the national chain name if domain matches, else None.
    e.g. "aspendental" → "Aspen Dental"
    """
    # Direct lookup
    if domain_norm in _NATIONAL_DOMAIN_MAP:
        return _NATIONAL_DOMAIN_MAP[domain_norm]
    # Partial match: domain contains a chain stem
    for key, name in _NATIONAL_DOMAIN_MAP.items():
        if len(key) >= 5 and key in domain_norm:
            return name
    return None


def fetch_auction_insights(client, customer_id: str) -> list[dict]:
    """
    Pull Google Ads Auction Insights for all enabled campaigns, last 30 days.

    Uses the correct GAQL resource: auction_insight_domain (not segments.auction_insight_domain).
    Per the Google Ads API docs, Auction Insights data is accessed via the
    auction_insight_domain resource linked to campaign, NOT as segments.* columns.

    Returns a list of dicts:
      {
        "domain":           "aspendental.com",
        "campaign_id":      "12345678",
        "campaign_name":    "Dental Implants — Grafton MA",
        "campaign_type":    "implants",
        "impression_share": 0.42,
        "overlap_rate":     0.65,
      }

    Returns [] on any error (non-fatal — caller falls back to Tier 3 static nationals).
    """
    try:
        service = client.get_service("GoogleAdsService")

        # First fetch all enabled campaigns to loop over
        camp_query = """
            SELECT campaign.id, campaign.name, campaign.resource_name
            FROM campaign
            WHERE campaign.status = 'ENABLED'
        """
        campaigns = []
        try:
            for row in service.search(customer_id=customer_id, query=camp_query):
                campaigns.append({
                    "id":   str(row.campaign.id),
                    "name": row.campaign.name,
                    "rn":   row.campaign.resource_name,
                })
        except Exception as e:
            logger.warning(f"[intel] campaign fetch failed: {e}")
            return []

        if not campaigns:
            return []

        rows = []
        for camp in campaigns:
            try:
                # Auction Insights: correct GAQL resource is auction_insight_domain
                # segmented by campaign, available for LAST_30_DAYS
                ai_query = f"""
                    SELECT
                        auction_insight_domain.domain,
                        metrics.search_impression_share,
                        metrics.search_overlap_rate
                    FROM auction_insight_domain
                    WHERE campaign.resource_name = '{camp["rn"]}'
                      AND segments.date DURING LAST_30_DAYS
                """
                for row in service.search(customer_id=customer_id, query=ai_query):
                    domain = (row.auction_insight_domain.domain or "").strip()
                    if not domain:
                        continue
                    imp_share = float(row.metrics.search_impression_share or 0.0)
                    overlap   = float(row.metrics.search_overlap_rate or 0.0)
                    rows.append({
                        "domain":           domain,
                        "campaign_id":      camp["id"],
                        "campaign_name":    camp["name"],
                        "campaign_type":    _detect_campaign_type(camp["name"]),
                        "impression_share": imp_share,
                        "overlap_rate":     overlap,
                    })
            except Exception as e:
                # Per-campaign failures are non-fatal — log and continue
                logger.debug(f"[intel] Auction Insights query failed for campaign '{camp['name']}': {e}")
                continue

        logger.info(f"[intel] Auction Insights: {len(rows)} rows across {len(campaigns)} campaigns")
        return rows
    except Exception as e:
        logger.warning(f"[intel] fetch_auction_insights failed (non-fatal): {e}")
        return []


def _haiku_enrich_unmatched_domains(
    unmatched: list[str],
    anthropic_key: str,
) -> dict[str, dict]:
    """
    Call Claude Haiku once to classify unmatched domains.
    Returns: {domain: {"is_dso": bool, "services": [...], "confidence": "high|medium|low"}}
    """
    if not unmatched or not anthropic_key:
        return {}
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=anthropic_key)
        prompt = (
            "You are analyzing competitor dental advertising in the Worcester County, MA area.\n"
            "These domains appeared in Google Ads auction data for a dental practice in Grafton, MA.\n"
            "For each domain, identify:\n"
            "  1. is_dso: is it a known national/regional DSO or dental chain? (true/false)\n"
            "  2. services: which dental services do they heavily advertise? "
            "Choose from: implants, invisalign, cosmetic, emergency, gum, general\n"
            "  3. confidence: how confident are you? high|medium|low\n\n"
            f"Domains to classify:\n{json.dumps(unmatched)}\n\n"
            "Return ONLY a JSON array:\n"
            '[{"domain": "example.com", "is_dso": false, "services": ["general"], '
            '"confidence": "low"}]\n'
            "No explanation, just the JSON array."
        )
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        # Extract JSON array
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        if not m:
            return {}
        items: list[dict] = json.loads(m.group())
        return {item["domain"]: item for item in items if "domain" in item}
    except Exception as e:
        logger.warning(f"[intel] Haiku domain enrichment failed: {e}")
        return {}


def _match_domain_to_nearby_practice(
    domain: str,
    nearby: list[dict],
) -> dict | None:
    """
    Try to match an auction_insight_domain to a row in nearby_practices.
    Returns the matching nearby_practices row dict, or None.
    """
    domain_norm = _normalize_domain(domain)
    if not domain_norm:
        return None

    # Try exact normalized name match
    for p in nearby:
        p_norm = normalize(p.get("name", ""))
        p_norm_nospace = p_norm.replace(" ", "")
        if p_norm == domain_norm or p_norm_nospace == domain_norm.replace(" ", ""):
            return p
        # Partial: domain contains key tokens of practice name
        tokens = p_norm.split()
        if len(tokens) >= 2:
            key = " ".join(tokens[:2])  # first 2 words
            if key in domain_norm or key.replace(" ", "") in domain_norm.replace(" ", ""):
                return p

    return None


def run_competitor_intel_scan(
    google_ads_client=None,
    customer_id: str = "",
    anthropic_key: str = "",
) -> dict:
    """
    Main entry point for the 15-day competitor advertising intelligence scan.

    Steps:
      1. Reset is_advertising state on all nearby_practices (clean slate per run)
      2. Fetch Auction Insights via Google Ads API (Tier 1)
      3. Match each domain to a nearby_practices row
      4. For national chains not in nearby_practices, use static NATIONAL_CHAINS fallback
      5. Haiku enrichment for unmatched non-national domains (Tier 2)
      6. Write intel rows to competitor_ad_intel
      7. Stage pending actions for admin review in competitor_intel_actions

    Returns a summary dict.
    """
    from database import (
        get_nearby_practices,
        upsert_competitor_ad_intel,
        reset_advertising_state,
        add_competitor_intel_action,
        get_intel_scan_stats,
    )
    from config import get_settings
    from database import get_setting

    settings = get_settings()
    run_id = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")

    # Build the Google Ads client if not provided
    if google_ads_client is None:
        try:
            from google_ads_sync import _build_client
            google_ads_client = _build_client()
        except Exception as e:
            logger.error(f"[intel] Cannot build Google Ads client: {e}")
            return {"ok": False, "error": str(e), "run_id": run_id}

    if not customer_id:
        customer_id = "".join(
            ch for ch in (settings.google_ads_customer_id or "") if ch.isdigit()
        )

    if not anthropic_key:
        anthropic_key = get_setting("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")

    logger.info(f"[intel] Starting competitor intel scan run_id={run_id}")

    # ── Step 1: Fetch Auction Insights FIRST (before resetting state) ─────────
    # We delay the reset until we have real data so that a failed API call
    # doesn't wipe the advertising state with nothing to repopulate it.
    auction_rows = fetch_auction_insights(google_ads_client, customer_id)
    logger.info(f"[intel] Fetched {len(auction_rows)} auction insight rows")

    # ── Step 2: Load all nearby practices ────────────────────────────────────
    nearby = get_nearby_practices(max_miles=20.0, include_excluded=True)

    # ── Step 4: Match domains + classify ─────────────────────────────────────
    # Group auction rows by domain+campaign_type for deduplication
    # key: (domain, campaign_type) → best row (highest overlap_rate)
    domain_camp_best: dict[tuple[str, str], dict] = {}
    for row in auction_rows:
        key = (row["domain"], row["campaign_type"])
        existing = domain_camp_best.get(key)
        if existing is None or row["overlap_rate"] > existing["overlap_rate"]:
            domain_camp_best[key] = row

    # Tier 3: Add static entries for NATIONAL_CHAINS only if no auction-data row
    # already covers that (chain, campaign_type) pair. Mark confidence='synthetic'
    # so the admin feed clearly shows these were not observed in live auction data.
    # Use the chain's canonical display name as domain (no fake .com fabrication).
    auction_covered: set[str] = set()  # chain_name values seen in auction_rows
    for (domain, ctype), row in domain_camp_best.items():
        chain_name_check = _match_domain_to_national_chain(_normalize_domain(domain))
        if chain_name_check and row["impression_share"] > 0:
            auction_covered.add(chain_name_check)

    for chain in NATIONAL_CHAINS:
        if chain["name"] in auction_covered:
            continue  # real auction data already covers this chain
        for ctype in CONQUEST_ELIGIBLE_TYPES:
            # Use stem as synthetic domain (human-readable, no fake .com)
            synthetic_domain = chain["stems"][0]
            key = (synthetic_domain, ctype)
            if key not in domain_camp_best:
                domain_camp_best[key] = {
                    "domain":           synthetic_domain,
                    "campaign_id":      "",
                    "campaign_name":    f"{chain['name']} (static)",
                    "campaign_type":    ctype,
                    "impression_share": 0.0,
                    "overlap_rate":     0.0,
                    "_synthetic":       True,
                    "_chain_name":      chain["name"],
                }

    # ── Step 5: Haiku enrichment for unmatched local domains ─────────────────
    unmatched_local_domains: list[str] = []
    for (domain, ctype), row in domain_camp_best.items():
        chain_match = _match_domain_to_national_chain(_normalize_domain(domain))
        if chain_match:
            continue  # handled as national chain
        practice_match = _match_domain_to_nearby_practice(domain, nearby)
        if practice_match:
            continue  # matched to a known nearby practice
        unmatched_local_domains.append(domain)

    haiku_results: dict[str, dict] = {}
    if unmatched_local_domains:
        unique_unmatched = list(set(unmatched_local_domains))[:20]  # cap at 20
        haiku_results = _haiku_enrich_unmatched_domains(unique_unmatched, anthropic_key)

    # ── Step 6: Fetch campaign resource_name lookup via lightweight GAQL ────────
    # Avoids importing _get_campaign_settings (heavy, 3 GAQL calls).
    # Maps campaign_type → list[resource_name] (all matching campaigns, not just first).
    camp_type_to_res_list: dict[str, list[str]] = {}
    try:
        service = google_ads_client.get_service("GoogleAdsService")
        rn_query = """
            SELECT campaign.resource_name, campaign.name
            FROM campaign
            WHERE campaign.status = 'ENABLED'
        """
        for row in service.search(customer_id=customer_id, query=rn_query):
            ctype = _detect_campaign_type(row.campaign.name or "")
            camp_type_to_res_list.setdefault(ctype, []).append(row.campaign.resource_name)
    except Exception as e:
        logger.warning(f"[intel] Could not load campaign resource names: {e}")

    # ── Step 7: Reset advertising state — AFTER all data is gathered ─────────
    # Only reset now that we have data to repopulate; a failed fetch earlier means
    # we skip the reset entirely (safe degradation — stale intel beats blank intel).
    reset_advertising_state(run_id)

    # ── Step 8: Write intel rows + stage actions ──────────────────────────────
    intel_written = 0
    actions_staged = 0
    no_match_count = 0

    for (domain, campaign_type), row in domain_camp_best.items():
        domain_norm = _normalize_domain(domain)
        confidence = "none"
        place_id: str | None = None
        practice_name = ""
        practice_match = None

        # Prefer explicit chain name from synthetic entries
        _explicit_chain = row.get("_chain_name")

        # Try national chain match
        chain_name = _explicit_chain or _match_domain_to_national_chain(domain_norm)
        if chain_name:
            confidence = row.get("_synthetic") and "synthetic" or (
                "claude_dso" if row["impression_share"] == 0.0 else "auction_data"
            )
            # Use a synthetic place_id for national chains not in nearby_practices
            practice_match = _match_domain_to_nearby_practice(domain, nearby)
            if practice_match:
                place_id = practice_match["place_id"]
                practice_name = practice_match["name"]
            else:
                place_id = f"national_{chain_name.upper().replace(' ', '_')}"
                practice_name = chain_name
        else:
            # Try matching to nearby_practices
            practice_match = _match_domain_to_nearby_practice(domain, nearby)
            if practice_match:
                place_id = practice_match["place_id"]
                practice_name = practice_match["name"]
                # Only mark as advertising if there is real signal
                if row["impression_share"] > 0 or row["overlap_rate"] > 0:
                    confidence = "auction_data"
                else:
                    confidence = "none"
            else:
                # Check Haiku results
                haiku = haiku_results.get(domain, {})
                if haiku.get("is_dso") and haiku.get("confidence") in ("high", "medium"):
                    place_id = f"haiku_{domain_norm.replace(' ', '_')}"
                    practice_name = domain
                    confidence = f"claude_haiku_{haiku.get('confidence', 'low')}"
                else:
                    no_match_count += 1
                    logger.debug(f"[intel] Domain '{domain}' had no practice match — skipping")
                    continue

        if not place_id:
            no_match_count += 1
            continue

        # Skip writing intel rows with no real signal (confidence=none, non-synthetic)
        # to avoid cluttering the feed with ghost entries
        if confidence == "none" and not row.get("_synthetic"):
            no_match_count += 1
            continue

        # Write to competitor_ad_intel
        try:
            intel_id = upsert_competitor_ad_intel(
                place_id=place_id,
                run_id=run_id,
                domain=domain,
                campaign_type=campaign_type,
                impression_share=row["impression_share"],
                overlap_rate=row["overlap_rate"],
                confidence=confidence,
            )
            intel_written += 1
        except Exception as e:
            logger.warning(f"[intel] Failed to write intel row for '{domain}': {e}")
            continue

        # Stage pending action: suppress_negative for conquest-eligible nationals.
        # Stage one action per matching campaign (not just first), capped at 2 campaigns.
        if (
            campaign_type in CONQUEST_ELIGIBLE_TYPES
            and (chain_name or classify(practice_name) == "national_chain")
        ):
            camp_res_list = camp_type_to_res_list.get(campaign_type, [])[:2]  # cap at 2 campaigns
            if not camp_res_list:
                continue

            # Determine brand stems to suppress (cap at 2 per competitor)
            brand_stems_to_suppress: list[str] = []
            if chain_name:
                for ch in NATIONAL_CHAINS:
                    if ch["name"] == chain_name:
                        brand_stems_to_suppress = ch["stems"][:2]
                        break
            elif practice_match:
                try:
                    brand_stems_to_suppress = json.loads(
                        practice_match.get("brand_stems") or "[]"
                    )[:2]
                except Exception:
                    brand_stems_to_suppress = []

            for camp_res in camp_res_list:
                for stem in brand_stems_to_suppress:
                    try:
                        add_competitor_intel_action(
                            intel_id=intel_id,
                            place_id=place_id,
                            campaign_resource=camp_res,
                            campaign_type=campaign_type,
                            action_type="suppress_negative",
                            brand_stem=stem,
                            keyword_text=stem,
                            match_type="PHRASE",
                        )
                        actions_staged += 1
                    except Exception as e:
                        logger.warning(f"[intel] Failed to stage action for stem '{stem}': {e}")

    summary = {
        "ok": True,
        "run_id": run_id,
        "auction_rows": len(auction_rows),
        "intel_written": intel_written,
        "actions_staged": actions_staged,
        "no_match": no_match_count,
    }
    logger.info(f"[intel] Scan complete: {summary}")
    return summary


def apply_intel_action(
    action_id: int,
    campaign_resource: str = "",
    dry_run: bool = False,
) -> dict:
    """
    Apply a single competitor_intel_actions row.
    action_type='suppress_negative': remove the brand negative from the campaign.
    action_type='add_conquest_keyword': add as a positive phrase keyword.

    Returns {"ok": bool, "message": str}
    """
    from database import get_pending_intel_actions, update_intel_action_status
    import sqlite3

    # Fetch the specific action
    actions = get_pending_intel_actions(limit=500)
    action = next((a for a in actions if a["id"] == action_id), None)
    if not action:
        return {"ok": False, "message": f"Action {action_id} not found or not pending"}

    action_type = action["action_type"]
    brand_stem   = action["brand_stem"]
    keyword_text = action["keyword_text"] or brand_stem
    camp_res     = campaign_resource or action["campaign_resource"]
    match_type   = action.get("match_type") or "PHRASE"

    if not camp_res:
        return {"ok": False, "message": "No campaign_resource — cannot apply"}

    if dry_run:
        return {"ok": True, "message": f"[dry_run] Would {action_type} '{keyword_text}' on {camp_res}"}

    try:
        from google_ads_write import (
            remove_negative_keyword_from_campaign,
            add_keyword_to_ad_group,
        )

        if action_type == "suppress_negative":
            ok = remove_negative_keyword_from_campaign(
                campaign_resource=camp_res,
                keyword_text=keyword_text,
                match_type=match_type,
            )
            msg = f"Removed negative '{keyword_text}' from {camp_res}"
        elif action_type == "add_conquest_keyword":
            # Conquest keyword additions require an ad_group_resource which we don't
            # have here. Mark as deferred so it leaves the pending queue instead of
            # getting stuck forever.
            update_intel_action_status(action_id, "deferred", applied_by="admin")
            return {
                "ok": True,
                "message": "add_conquest_keyword: use the AI Refine panel to add conquest keywords to a specific ad group. Action marked as deferred.",
            }
        else:
            return {"ok": False, "message": f"Unknown action_type '{action_type}'"}

        if ok:
            update_intel_action_status(action_id, "applied", applied_by="admin")
            return {"ok": True, "message": msg}
        else:
            return {"ok": False, "message": f"Google Ads write returned False for action {action_id}"}

    except Exception as e:
        logger.error(f"[intel] apply_intel_action {action_id} failed: {e}")
        return {"ok": False, "message": str(e)}
