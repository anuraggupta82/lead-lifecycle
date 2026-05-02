"""
AI Campaign Optimizer — daily evaluation and optimization of Google Ads.

Runs daily (7 AM) after fresh data from google_ads_sync.
Pulls keyword performance, joins with lead/production data, then:
  1. Pauses keywords with high spend + zero leads
  2. Increases bids on proven production keywords
  3. Harvests new exact-match keywords from search terms report
  4. Adds negative keywords for irrelevant search terms
  5. Generates a daily optimization report

Phase 1 changes:
  - Every optimizer run creates a gads_optimizer_runs record.
  - Every recommendation creates a gads_audit_log row with execution_result='pending_approval'.
  - Each recommendation row includes an 'action_id' field for the Apply button.
  - Stale pending rows (>48h) are expired at the start of each run.
  - _execute_pause uses partial_failure=True, logs per-keyword, checks kill switch.
  - dry_run parameter is deprecated — optimizer always produces pending rows.
    Use the Apply button in the admin UI to execute individual actions.

Uses Claude API for analysis when ANTHROPIC_API_KEY is set.
Falls back to rule-based optimization otherwise.

Manual trigger: POST /api/admin/optimize
"""

import logging
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from google.ads.googleads.client import GoogleAdsClient
from config import get_settings
from database import get_all_leads

logger = logging.getLogger(__name__)


def _build_client():
    settings = get_settings()
    return GoogleAdsClient.load_from_dict({
        "developer_token": settings.google_ads_developer_token,
        "client_id": settings.google_ads_client_id,
        "client_secret": settings.google_ads_client_secret,
        "refresh_token": settings.google_ads_refresh_token,
        "login_customer_id": settings.google_ads_login_customer_id,
        "use_proto_plus": True,
    })


# ── Data Collection ──────────────────────────────────────────────────────────

def _get_keyword_performance(client, customer_id: str, days: int = 30) -> list:
    """Pull keyword-level performance metrics for the last N days."""
    service = client.get_service("GoogleAdsService")
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=days)

    query = f"""
        SELECT
            ad_group_criterion.keyword.text,
            ad_group_criterion.keyword.match_type,
            ad_group_criterion.status,
            ad_group_criterion.resource_name,
            ad_group.name,
            campaign.name,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.conversions_value,
            metrics.average_cpc
        FROM keyword_view
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
            AND campaign.status = 'ENABLED'
            AND ad_group.status = 'ENABLED'
    """

    results = []
    try:
        response = service.search(customer_id=customer_id, query=query)
        for row in response:
            cost = (row.metrics.cost_micros or 0) / 1_000_000.0
            clicks = row.metrics.clicks or 0
            results.append({
                "keyword": row.ad_group_criterion.keyword.text,
                "match_type": str(row.ad_group_criterion.keyword.match_type),
                "status": str(row.ad_group_criterion.status),
                "resource_name": row.ad_group_criterion.resource_name,
                "ad_group": row.ad_group.name,
                "campaign": row.campaign.name,
                "impressions": row.metrics.impressions or 0,
                "clicks": clicks,
                "cost": cost,
                "cpc": cost / clicks if clicks > 0 else 0,
                "conversions": row.metrics.conversions or 0,
                "conversion_value": row.metrics.conversions_value or 0,
            })
    except Exception as e:
        logger.error(f"Failed to get keyword performance: {e}")

    return results


def _get_search_terms(client, customer_id: str, days: int = 30) -> list:
    """Pull search terms report to find new keywords and negatives."""
    service = client.get_service("GoogleAdsService")
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=days)

    query = f"""
        SELECT
            search_term_view.search_term,
            search_term_view.status,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions
        FROM search_term_view
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
            AND metrics.impressions > 0
    """

    results = []
    try:
        response = service.search(customer_id=customer_id, query=query)
        for row in response:
            cost = (row.metrics.cost_micros or 0) / 1_000_000.0
            results.append({
                "search_term": row.search_term_view.search_term,
                "status": str(row.search_term_view.status),
                "impressions": row.metrics.impressions or 0,
                "clicks": row.metrics.clicks or 0,
                "cost": cost,
                "conversions": row.metrics.conversions or 0,
            })
    except Exception as e:
        logger.error(f"Failed to get search terms: {e}")

    return results


# ── Lead-Level Attribution ───────────────────────────────────────────────────

def _get_keyword_attribution() -> dict:
    """
    Build keyword → lead/revenue attribution from SQLite.
    Returns: {keyword_text: {leads, booked, treated, production}}
    """
    leads = get_all_leads(limit=1000)
    attribution = {}

    for lead in leads:
        keyword = (lead.get("keyword_text") or "").strip()
        if not keyword:
            continue

        if keyword not in attribution:
            attribution[keyword] = {
                "leads": 0,
                "booked": 0,
                "treated": 0,
                "production": 0.0,
            }

        attribution[keyword]["leads"] += 1

        stage = lead.get("stage", "")
        if stage in ("scheduled", "no_show", "showed", "treatment_presented",
                      "treatment_accepted", "treatment_completed"):
            attribution[keyword]["booked"] += 1

        if stage in ("treatment_accepted", "treatment_completed"):
            attribution[keyword]["treated"] += 1

        attribution[keyword]["production"] += float(lead.get("attributed_production", 0))

    return attribution


# ── Rule-Based Optimization ──────────────────────────────────────────────────

def _analyze_keywords(keyword_perf: list, attribution: dict, search_terms: list, campaign: str = "") -> dict:
    """
    Apply optimization rules. Returns recommended actions.
    campaign: name of the campaign being evaluated — used to scope memory lookups.
              Empty string = global memory only.
    """
    # Load persistent memory — what the optimizer has been taught
    # Two-pass: global entries first, campaign-specific overrides second
    try:
        from database import get_optimizer_memory_dict
        mem = get_optimizer_memory_dict(campaign=campaign)
    except Exception as e:
        logger.warning(f"Could not load optimizer memory: {e}")
        mem = {'term_classifications': {}, 'keyword_overrides': {}, 'campaign_rules': {}, 'general': {}}

    term_classifications = mem.get('term_classifications', {})   # search term → 'negative'/'good_keyword'/'irrelevant'
    keyword_overrides = mem.get('keyword_overrides', {})         # keyword → 'never_pause'/'always_bid_up' etc.
    campaign_rules = mem.get('campaign_rules', {})               # 'min_spend_before_pause' → value

    # Pull configurable thresholds from memory (with sensible defaults)
    min_spend_before_pause = float(campaign_rules.get('min_spend_before_pause', 20))
    min_clicks_before_pause = int(campaign_rules.get('min_clicks_before_pause', 10))

    logger.info(f"Optimizer memory loaded: {len(term_classifications)} term classifications, "
                f"{len(keyword_overrides)} keyword overrides, {len(campaign_rules)} campaign rules")

    actions = {
        "pause": [],           # Keywords to pause (high spend, no results)
        "increase_bid": [],    # Keywords to bid up (proven production)
        "decrease_bid": [],    # Keywords to bid down (high cost, low conversion)
        "new_exact": [],       # Search terms to add as exact match keywords
        "new_negatives": [],   # Search terms to add as negatives
        "summary": {},
        "memory_applied": [],  # Log of memory overrides that changed the outcome
    }

    total_spend = sum(k["cost"] for k in keyword_perf)
    total_clicks = sum(k["clicks"] for k in keyword_perf)
    total_leads = sum(a["leads"] for a in attribution.values())
    total_production = sum(a["production"] for a in attribution.values())

    # Rule 1: Pause keywords with spend > threshold and zero leads
    for kw in keyword_perf:
        keyword = kw["keyword"]
        keyword_lower = keyword.lower()
        attr = attribution.get(keyword, {"leads": 0, "booked": 0, "production": 0})

        # Check memory override first
        override = keyword_overrides.get(keyword_lower)
        if override == 'never_pause':
            actions["memory_applied"].append(f"SKIP PAUSE '{keyword}': memory says '{override}'")
            continue

        if kw["cost"] > min_spend_before_pause and attr["leads"] == 0 and kw["clicks"] > min_clicks_before_pause:
            actions["pause"].append({
                "keyword": keyword,
                "match_type": kw["match_type"],
                "resource_name": kw["resource_name"],
                "reason": f"${kw['cost']:.2f} spent, {kw['clicks']} clicks, 0 leads",
                "cost": kw["cost"],
            })

    # Rule 2: Increase bids on keywords with production
    for kw in keyword_perf:
        keyword = kw["keyword"]
        attr = attribution.get(keyword, {"leads": 0, "booked": 0, "production": 0})

        if attr["production"] > 0:
            roas = attr["production"] / kw["cost"] if kw["cost"] > 0 else float("inf")
            actions["increase_bid"].append({
                "keyword": keyword,
                "match_type": kw["match_type"],
                "resource_name": kw["resource_name"],
                "reason": f"ROAS {roas:.1f}x — ${attr['production']:.0f} production from ${kw['cost']:.2f} spend",
                "roas": roas,
            })
        elif attr["booked"] > 0 and kw["cost"] > 0:
            cost_per_booking = kw["cost"] / attr["booked"]
            if cost_per_booking < 50:  # Good cost per booking
                actions["increase_bid"].append({
                    "keyword": keyword,
                    "match_type": kw["match_type"],
                    "resource_name": kw["resource_name"],
                    "reason": f"${cost_per_booking:.2f}/booking — {attr['booked']} bookings",
                    "roas": 0,
                })

    # Rule 3: Decrease bids on high-cost, low-conversion keywords
    for kw in keyword_perf:
        keyword = kw["keyword"]
        attr = attribution.get(keyword, {"leads": 0, "booked": 0, "production": 0})

        if kw["cost"] > 10 and kw["clicks"] > 5 and attr["leads"] > 0 and attr["booked"] == 0:
            actions["decrease_bid"].append({
                "keyword": keyword,
                "match_type": kw["match_type"],
                "resource_name": kw["resource_name"],
                "reason": f"{attr['leads']} leads but 0 bookings from ${kw['cost']:.2f} spend",
            })

    # ── Negative keyword signals ──────────────────────────────────────
    # Search terms that indicate the person can't/won't pay for treatment.
    # These waste ad budget because they'll never convert to a $15k+ case.
    # Hard negatives — genuinely not a dental patient searching for treatment
    _HARD_NEGATIVES = [
        "dental school", "dental schools",        # looking for student-rate work
        "diy", "home remed",                      # not seeking professional care
        "complaint", "lawsuit", "malpractice",    # legal research
        "salary", "job", "career", "how to become",  # career searches
    ]
    # Soft negatives — empty for now, let the pipeline data decide
    _SOFT_NEGATIVES = []
    # EVERYTHING ELSE gets tracked and judged by real pipeline data:
    # cheap, low cost, affordable, discount, free — price-sensitive buyers
    # cost, price, how much, payment plan, financing — research/buying intent
    # review — evaluating the practice
    # clinical trial, medicaid, medicare — let data prove they don't convert
    # can't afford — might convert with financing options

    def _is_negative_intent(term: str) -> str:
        """Check if a search term has negative intent. Returns reason or empty string."""
        t = term.lower()
        for signal in _HARD_NEGATIVES:
            if signal in t:
                return f"Negative intent: '{signal}'"
        for signal in _SOFT_NEGATIVES:
            if signal in t:
                # Make sure it's not a clinical term (e.g. "free gingival graft")
                if "free gingival" in t or "free connective" in t:
                    return ""
                return f"Likely negative: '{signal}'"
        return ""

    # Rule 4: Harvest search terms that converted AND have buying intent
    existing_keywords = {kw["keyword"].lower() for kw in keyword_perf}
    for st in search_terms:
        term = st["search_term"].lower()

        # Check memory classification first — overrides all heuristics
        mem_classification = None
        for mem_term, mem_val in term_classifications.items():
            if mem_term in term or term in mem_term:
                mem_classification = mem_val
                break

        if mem_classification == 'negative':
            actions["new_negatives"].append({
                "search_term": st["search_term"],
                "clicks": st.get("clicks", 0),
                "impressions": st.get("impressions", 0),
                "cost": st["cost"],
                "reason": f"Memory: classified as negative",
            })
            actions["memory_applied"].append(f"NEGATIVE '{st['search_term']}': memory classification")
            continue
        elif mem_classification in ('good_keyword', 'irrelevant'):
            # Skip — don't add as negative, don't add as exact match candidate
            actions["memory_applied"].append(f"SKIP '{st['search_term']}': memory says '{mem_classification}'")
            continue

        neg_reason = _is_negative_intent(term)

        if neg_reason:
            # Even if Google says it "converted", the intent is wrong — add as negative
            actions["new_negatives"].append({
                "search_term": st["search_term"],
                "clicks": st.get("clicks", 0),
                "impressions": st.get("impressions", 0),
                "cost": st["cost"],
                "reason": neg_reason,
            })
        elif st["conversions"] > 0 and term not in existing_keywords:
            # Only add as exact match if it has real lead attribution
            # (not just a Google Ads "conversion" which could be just a form view)
            term_has_real_leads = any(
                term in a_kw.lower() or a_kw.lower() in term
                for a_kw in attribution.keys()
            ) if attribution else False

            if term_has_real_leads:
                actions["new_exact"].append({
                    "search_term": st["search_term"],
                    "clicks": st["clicks"],
                    "conversions": st["conversions"],
                    "cost": st["cost"],
                    "reason": "Has real lead attribution + Google conversion",
                })
            else:
                # Conversion in Google but no lead in our system — flag for review
                actions["new_exact"].append({
                    "search_term": st["search_term"],
                    "clicks": st["clicks"],
                    "conversions": st["conversions"],
                    "cost": st["cost"],
                    "reason": "Google conversion but NO lead in pipeline — verify before adding",
                })

    # Rule 5: Negative keywords — high spend with no results
    for st in search_terms:
        term = st["search_term"].lower()
        # Skip if already added as negative in Rule 4
        already_negative = any(n["search_term"].lower() == term for n in actions["new_negatives"])
        if already_negative:
            continue

        if st["cost"] > 5 and st.get("clicks", 0) == 0 and st.get("impressions", 0) > 20:
            actions["new_negatives"].append({
                "search_term": st["search_term"],
                "impressions": st["impressions"],
                "cost": st["cost"],
                "reason": "High impressions, zero clicks — likely irrelevant",
            })
        elif st.get("clicks", 0) > 5 and st["cost"] > 15 and st["conversions"] == 0:
            term_lower = st["search_term"].lower()
            has_leads = any(
                term_lower in (a_kw.lower())
                for a_kw in attribution.keys()
            )
            if not has_leads:
                actions["new_negatives"].append({
                    "search_term": st["search_term"],
                    "clicks": st.get("clicks", 0),
                    "cost": st["cost"],
                    "reason": f"${st['cost']:.2f} spent, {st.get('clicks',0)} clicks, 0 conversions/leads",
                })

    # Summary
    actions["summary"] = {
        "total_spend": round(total_spend, 2),
        "total_clicks": total_clicks,
        "total_leads": total_leads,
        "total_production": round(total_production, 2),
        "overall_roas": round(total_production / total_spend, 1) if total_spend > 0 else 0,
        "cost_per_lead": round(total_spend / total_leads, 2) if total_leads > 0 else 0,
        "keywords_to_pause": len(actions["pause"]),
        "keywords_to_bid_up": len(actions["increase_bid"]),
        "keywords_to_bid_down": len(actions["decrease_bid"]),
        "new_exact_match": len(actions["new_exact"]),
        "new_negatives": len(actions["new_negatives"]),
        "memory_overrides_applied": len(actions["memory_applied"]),
    }

    if actions["memory_applied"]:
        logger.info(f"  Memory overrides applied ({len(actions['memory_applied'])}):")
        for m in actions["memory_applied"]:
            logger.info(f"    {m}")

    return actions


# ── Execute Actions ──────────────────────────────────────────────────────────

def _execute_pause(client, customer_id: str, keywords: list) -> int:
    """
    Pause keywords via Google Ads API.

    Phase 1: each keyword in the list must have an 'action_id' field (UUID).
    Checks the kill switch first — if blocked, marks all action rows as 'blocked'.
    Uses partial_failure=True so one bad keyword doesn't fail the whole batch.
    Returns count of successfully paused keywords.
    """
    if not keywords:
        return 0

    from campaign_safety import check_writes_enabled, WriteBlockedError
    from campaign_audit import mark_executed

    # Kill switch check
    try:
        check_writes_enabled()
    except WriteBlockedError as e:
        for kw in keywords:
            aid = kw.get("action_id")
            if aid:
                from database import update_gads_action_result
                update_gads_action_result(
                    action_id=aid,
                    executed=False,
                    execution_result="blocked",
                    error_detail=str(e),
                )
        logger.warning(f"Keyword pause BLOCKED by kill switch: {e}")
        return 0

    service = client.get_service("AdGroupCriterionService")
    operations = []

    for kw in keywords:
        operation = client.get_type("AdGroupCriterionOperation")
        criterion = operation.update
        criterion.resource_name = kw["resource_name"]
        criterion.status = client.enums.AdGroupCriterionStatusEnum.PAUSED
        client.copy_from(
            operation.update_mask,
            client.get_type("FieldMask")(paths=["status"])
        )
        operations.append(operation)

    try:
        response = service.mutate_ad_group_criteria(
            customer_id=customer_id,
            operations=operations,
            partial_failure=True,   # don't fail-all on one bad op
        )
        success_count = 0
        # Walk results — one entry per operation in order
        results = list(response.results) if response.results else []
        for i, kw in enumerate(keywords):
            aid = kw.get("action_id")
            if i < len(results) and results[i].resource_name:
                if aid:
                    mark_executed(aid, success=True)
                success_count += 1
            else:
                if aid:
                    mark_executed(aid, success=False, error_detail="partial_failure or no result")
        logger.info(f"Paused {success_count}/{len(keywords)} keywords")
        return success_count
    except Exception as e:
        logger.error(f"Failed to pause keywords: {e}")
        for kw in keywords:
            aid = kw.get("action_id")
            if aid:
                mark_executed(aid, success=False, error_detail=str(e))
        return 0


def _execute_single_pause(client, customer_id: str, resource_name: str) -> bool:
    """
    Pause a single keyword by resource_name.
    Used by the /approve endpoint for individual Apply-button execution.
    Does NOT check kill switch — caller must check first.
    Returns True on success.
    """
    service = client.get_service("AdGroupCriterionService")
    operation = client.get_type("AdGroupCriterionOperation")
    criterion = operation.update
    criterion.resource_name = resource_name
    criterion.status = client.enums.AdGroupCriterionStatusEnum.PAUSED
    client.copy_from(
        operation.update_mask,
        client.get_type("FieldMask")(paths=["status"])
    )
    try:
        service.mutate_ad_group_criteria(
            customer_id=customer_id,
            operations=[operation],
        )
        return True
    except Exception as e:
        logger.error(f"Single pause failed for {resource_name}: {e}")
        raise


# ── Main Entry Point ─────────────────────────────────────────────────────────

def optimize_campaign(dry_run: bool = True, trigger: str = "admin_manual") -> dict:
    """
    Run the full optimization cycle.

    Phase 1 behavior:
    - Creates a gads_optimizer_runs record at the start.
    - Expires stale pending_approval rows (>48h old) before generating new ones.
    - Every recommendation generates a gads_audit_log row with
      execution_result='pending_approval' and an 'action_id' embedded in the
      returned report dict. The frontend Apply button references this action_id.
    - dry_run parameter kept for backward compatibility but no longer changes
      behavior — use Apply buttons in admin UI to execute individual actions.
    """
    from campaign_audit import log_pending, expire_stale_pending
    from database import (
        create_optimizer_run, update_optimizer_run, get_setting
    )

    settings = get_settings()
    run_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    # Expire recommendations older than 48h before generating new ones
    expired = expire_stale_pending(max_age_hours=48)

    try:
        client = _build_client()
    except Exception as e:
        logger.error(f"Failed to create Google Ads client: {e}")
        create_optimizer_run(run_id, trigger=trigger)
        update_optimizer_run(run_id, mode="errored", error=str(e))
        return {"error": str(e), "run_id": run_id}

    customer_id = settings.google_ads_customer_id

    logger.info("=" * 60)
    logger.info(f"AI Campaign Optimizer — run_id={run_id}")
    logger.info("=" * 60)
    if expired:
        logger.info(f"Expired {expired} stale pending rows before this run")

    # Collect data
    logger.info("Collecting keyword performance...")
    keyword_perf = _get_keyword_performance(client, customer_id, days=30)

    logger.info("Collecting search terms...")
    search_terms = _get_search_terms(client, customer_id, days=30)

    logger.info("Building lead attribution...")
    attribution = _get_keyword_attribution()

    # Determine the primary campaign name for memory scoping
    campaign_spend: dict = {}
    for kw in keyword_perf:
        camp = kw.get("campaign", "")
        if camp:
            campaign_spend[camp] = campaign_spend.get(camp, 0) + kw.get("cost", 0)
    primary_campaign = max(campaign_spend, key=campaign_spend.get) if campaign_spend else ""
    logger.info(f"Primary campaign for memory scoping: '{primary_campaign}'")

    # Create run record now that we have the primary campaign
    create_optimizer_run(run_id, trigger=trigger, primary_campaign=primary_campaign)

    # Analyze
    logger.info("Analyzing and generating recommendations...")
    actions = _analyze_keywords(keyword_perf, attribution, search_terms, campaign=primary_campaign)

    # ── Create pending_approval audit rows for each recommendation ────────────
    actions_pending = 0

    for kw in actions["pause"]:
        aid = log_pending(
            operation="pause_keyword",
            entity_type="keyword",
            entity_id=kw["resource_name"],
            entity_name=kw["keyword"],
            before_state={"status": "ENABLED", "match_type": kw.get("match_type", "")},
            after_state={"status": "PAUSED"},
            optimizer_run_id=run_id,
            reason=kw.get("reason", ""),
        )
        kw["action_id"] = aid
        actions_pending += 1

    for kw in actions["increase_bid"]:
        aid = log_pending(
            operation="increase_bid",
            entity_type="keyword",
            entity_id=kw.get("resource_name", ""),
            entity_name=kw["keyword"],
            before_state={"match_type": kw.get("match_type", ""), "roas": kw.get("roas", 0)},
            after_state={"bid_change": "+10%"},
            optimizer_run_id=run_id,
            reason=kw.get("reason", ""),
        )
        kw["action_id"] = aid
        actions_pending += 1

    for kw in actions["decrease_bid"]:
        aid = log_pending(
            operation="decrease_bid",
            entity_type="keyword",
            entity_id=kw.get("resource_name", ""),
            entity_name=kw["keyword"],
            before_state={"match_type": kw.get("match_type", "")},
            after_state={"bid_change": "-10%"},
            optimizer_run_id=run_id,
            reason=kw.get("reason", ""),
        )
        kw["action_id"] = aid
        actions_pending += 1

    for st in actions["new_exact"]:
        aid = log_pending(
            operation="add_exact_keyword",
            entity_type="keyword",
            entity_id=st["search_term"],
            entity_name=st["search_term"],
            before_state={"type": "search_term", "clicks": st.get("clicks", 0), "conversions": st.get("conversions", 0)},
            after_state={"match_type": "EXACT"},
            optimizer_run_id=run_id,
            reason=st.get("reason", ""),
        )
        st["action_id"] = aid
        actions_pending += 1

    for st in actions["new_negatives"]:
        aid = log_pending(
            operation="add_negative_keyword",
            entity_type="keyword",
            entity_id=st["search_term"],
            entity_name=st["search_term"],
            before_state={"type": "search_term", "cost": st.get("cost", 0)},
            after_state={"status": "NEGATIVE"},
            optimizer_run_id=run_id,
            reason=st.get("reason", ""),
        )
        st["action_id"] = aid
        actions_pending += 1

    # Report
    summary = actions["summary"]
    report = {
        "run_id": run_id,
        "timestamp": now,
        "mode": "pending_approval",
        "primary_campaign": primary_campaign,
        "summary": summary,
        "actions": {
            "pause": actions["pause"],
            "increase_bid": actions["increase_bid"],
            "decrease_bid": actions["decrease_bid"],
            "new_exact": actions["new_exact"],
            "new_negatives": actions["new_negatives"],
        },
        "memory_applied": actions.get("memory_applied", []),
    }

    # Update run record with results
    update_optimizer_run(
        run_id,
        summary_json=json.dumps(summary, default=str),
        report_json=json.dumps(report, default=str),
        actions_pending=actions_pending,
        mode="pending_approval",
    )

    logger.info(f"\n{'='*60}")
    logger.info(f"OPTIMIZATION REPORT — run_id={run_id}")
    logger.info(f"{'='*60}")
    logger.info(f"  Total spend (30d):    ${summary['total_spend']}")
    logger.info(f"  Total clicks:         {summary['total_clicks']}")
    logger.info(f"  Total leads:          {summary['total_leads']}")
    logger.info(f"  Total production:     ${summary['total_production']}")
    logger.info(f"  Overall ROAS:         {summary['overall_roas']}x")
    logger.info(f"  Cost per lead:        ${summary['cost_per_lead']}")
    logger.info(f"  Keywords to pause:    {summary['keywords_to_pause']}")
    logger.info(f"  Keywords to bid up:   {summary['keywords_to_bid_up']}")
    logger.info(f"  Keywords to bid down: {summary['keywords_to_bid_down']}")
    logger.info(f"  New exact-match:      {summary['new_exact_match']}")
    logger.info(f"  New negatives:        {summary['new_negatives']}")
    logger.info(f"  Total pending actions: {actions_pending}")

    for kw in actions["pause"]:
        logger.info(f"  PAUSE [{kw.get('action_id','?')[:8]}]: '{kw['keyword']}' — {kw['reason']}")
    for kw in actions["increase_bid"]:
        logger.info(f"  BID UP: '{kw['keyword']}' — {kw['reason']}")
    for kw in actions["decrease_bid"]:
        logger.info(f"  BID DOWN: '{kw['keyword']}' — {kw['reason']}")
    for st in actions["new_exact"]:
        logger.info(f"  NEW EXACT: '{st['search_term']}' — {st.get('clicks',0)} clicks")
    for st in actions["new_negatives"]:
        logger.info(f"  NEW NEGATIVE: '{st['search_term']}' — {st['reason']}")

    logger.info("=" * 60)
    return report


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = optimize_campaign(dry_run=True)
    print(json.dumps(result, indent=2, default=str))
