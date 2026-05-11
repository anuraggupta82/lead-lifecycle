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
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from google.ads.googleads.client import GoogleAdsClient
from google.protobuf import field_mask_pb2
from config import get_settings
from database import get_all_leads

logger = logging.getLogger(__name__)


# ── Negative keyword signals (module-level so all functions can use them) ─────

_HARD_NEGATIVES = [
    "dental school", "dental schools",        # looking for student-rate work
    "diy", "home remed",                      # not seeking professional care
    "complaint", "lawsuit", "malpractice",    # legal research
    "salary", "job", "career", "how to become",  # career searches
]
_SOFT_NEGATIVES = []

_COMPETITOR_NAMES = [
    # Direct local competitors
    "grace dental", "grace smiles",
    "simply orthodontics", "simply ortho",
    "grafton smiles",                         # different practice
    "aspen dental",
    "western mass dental",
    "westborough dental",
    "shrewsbury dental",
    "worcester dental",
    "millbury dental",
    "auburn dental",
    "northborough dental",
    "framingham dental",
    "gentle dental",
    "comfort dental",
    "perfect teeth",
    "castle dental",
    "bright now dental",
    "affordable dentures",
    "small smiles",
    # Specific competitor dentists by name
    "dr polasky", "polasky",
    "dr. polasky",
    "tina theroux",
    "dr cabrera", "dr. cabrera",
    "monica rao", "dr rao",
    "dr ryan harrington", "ryan harrington",
]
_OUR_NAMES = ["grafton dental", "grafton dental care", "gdc", "dr gupta", "dr. gupta"]


# ── Excellence Report — campaign-type classification & prompt injection ────────

# Tokens used to detect campaign type from campaign name.
# Order matters: more specific checked first.
_CAMPAIGN_TYPE_TOKENS = {
    "emergency":  ["emergency", "urgent", "same day", "toothache", "broken tooth"],
    "implants":   ["implant"],
    "invisalign": ["invisalign", "clear aligner", "ortho"],
    "cosmetic":   ["veneer", "cosmetic", "smile makeover", "whitening"],
    "brand":      ["grafton dental", "brand", "branded"],
    # default fallback = "general"
}

# Narrative guidance from Excellence Report — stays in code so it's git-tracked.
# Numeric targets live in the excellence_targets DB table (editable from UI).
_EXCELLENCE_NARRATIVE = {
    "emergency": (
        "Highest budget priority alongside Implants.\n"
        "Run 24/7 ONLY if after-hours answering service exists; otherwise pause 10 PM–7 AM to avoid unanswered calls.\n"
        "Emergency patients convert fast (same-day close rate high) — CPAs above band indicate budget or QS problem, not a conversion problem.\n"
        "Peak times: Sunday PM / Monday AM — ensure schedule covers these."
    ),
    "implants": (
        "Highest revenue-per-patient service ($5,000–$30,000/case) — CPA targets are wide for good reason.\n"
        "Patients travel further for implants (up to 20–30 miles) — geo targeting can be wider than other campaigns.\n"
        "High-consideration purchase: desktop converts better than mobile for research phase; expect lower mobile CVR.\n"
        "Keywords 'all-on-4', 'dental implants near me', 'single tooth implant' are proven converters — never pause without strong data."
    ),
    "invisalign": (
        "Case value $4,500–$8,000 — CPAs up to $300 are justifiable.\n"
        "High research phase: patients compare providers. Strong landing page with before/after and financing options critical.\n"
        "Financing messaging ('0% CareCredit') directly removes #1 conversion barrier."
    ),
    "cosmetic": (
        "Moderate priority — good margin but lower urgency than emergency/implants.\n"
        "Before/after photos and social proof (reviews) are especially important for cosmetic services.\n"
        "Keywords: teeth whitening, veneers, smile makeover, dental bonding."
    ),
    "brand": (
        "Never pause brand campaigns — branded keywords cost $0.50–$1.50 CPC and convert at 30–50% higher rate.\n"
        "If competitor appears above GDC in brand search results, this is a critical gap.\n"
        "Budget ~$5–$10/day is sufficient; should never be turned off."
    ),
    "general": (
        "General/new patient campaign: capture top-of-funnel searchers ('dentist near me', 'family dentist').\n"
        "Highest keyword volume, moderate CPC ($4–$12). Negative keywords critical here — most wasted spend comes from general terms.\n"
        "Landing page must be service-specific, NOT the homepage."
    ),
}

_BIDDING_PHASES = (
    "BIDDING PHASE GUIDANCE (conversions/month on this campaign):\n"
    "  Phase 1 (0–15 conv/mo):   Manual CPC — build data; review bids weekly\n"
    "  Phase 2 (15–30 conv/mo):  MAXIMIZE_CONVERSIONS — bridge strategy; do NOT suggest manual bid changes\n"
    "  Phase 3 (30–50 conv/mo):  TARGET_CPA — set initial target 20% above actual CPA; tighten over 4–8 weeks\n"
    "  Phase 4 (50+ conv/mo):    TARGET_ROAS — requires offline OD revenue flowing into Google Ads\n"
    "CRITICAL: Do NOT suggest switching to Smart Bidding before 30 conv/mo. Do NOT suggest manual bid changes when Smart Bidding is active."
)

_MATCH_TYPE_RULES = (
    "MATCH TYPE RULES (2025):\n"
    "  Exact Match = best CPA (top-performing in 70.79% of accounts) — use for proven winners\n"
    "  Phrase Match = default for new keywords\n"
    "  Broad Match = ONLY when Smart Bidding active AND 30+ conv/mo — Broad without Smart Bidding wastes 30–40% of budget\n"
    "  Pause keywords with Quality Score ≤ 3 (monthly review)"
)

_NEGATIVE_KEYWORD_CATEGORIES = (
    "NEGATIVE KEYWORD CATEGORIES TO FLAG (if seen in search terms):\n"
    "  Jobs/Careers:  dental assistant jobs, dentist hiring, dental hygienist salary, dental school, dentistry degree,\n"
    "                 how to become a dentist, dental residency, dental courses, CE credits, dental license\n"
    "  Free/Low-cost: free dental, free dental clinic, dental school free, charity dental, Medicaid dentist,\n"
    "                 low income dental, sliding scale dental, cheapest dentist\n"
    "  DIY/Products:  DIY teeth whitening, whitening strips, whitening toothpaste, Crest whitening,\n"
    "                 at-home veneers, snap-on veneers, dental bonding kit\n"
    "  Educational:   dental implant procedure explained, how does Invisalign work, what is a root canal,\n"
    "                 dental school curriculum\n"
    "  Competitors:   any named competing practice (already flagged by rule-based engine)"
)

_RSA_GUIDANCE = (
    "RSA / AD COPY GUIDANCE:\n"
    "  Fill ALL 15 headline slots and ALL 4 description slots (10–15% more clicks)\n"
    "  Shorter headlines (<20 chars) deliver CPA ~$9.35 vs ~$18.27 for longer headlines\n"
    "  'Average' Ad Strength delivers best CPA ($12.43) — do NOT chase 'Excellent' at expense of message clarity\n"
    "  Headline categories to cover: keyword-focused | value proposition | social proof | offer/urgency | trust/comfort\n"
    "  Key converting themes: same-day access, financing/payment plans, anxiety-free, new patient special, insurance accepted"
)

_GEO_GUIDANCE = (
    "GEO TARGETING GUIDANCE:\n"
    "  Suburban (like Grafton MA): 10–15 mile radius standard\n"
    "  Implants/specialty: 20–30 miles — patients travel further for the right provider\n"
    "  Use 'Presence only' (NOT 'Presence OR Interest') — default Google setting burns budget on out-of-area traffic\n"
    "  After 60+ days: run Geographic Report, apply +15–30% bid adjustment on high-converting zip codes"
)

_TOP_MISTAKES = (
    "TOP MISTAKES TO FLAG IF YOU SEE EVIDENCE IN THE DATA:\n"
    "  1. Traffic to homepage instead of service-specific landing page (costs 30–50% conv rate)\n"
    "  2. No negative keyword list (wastes 20–42% of budget)\n"
    "  3. Broad Match without Smart Bidding (wastes 30–40% of budget)\n"
    "  4. Tracking only form fills — missing call conversions (Smart Bidding starved of data)\n"
    "  5. No ad assets/extensions (leaving 10–25% CTR improvement on the table at zero cost)\n"
    "  6. Wide geo targeting with 'Presence OR Interest' setting\n"
    "  7. Smart Bidding enabled before 30 conv/mo (erratic bidding, insufficient data)"
)


def _classify_campaign(campaign_name: str) -> str:
    """Map a campaign name to its service type for excellence target scoping."""
    name = (campaign_name or "").lower()
    for ctype, tokens in _CAMPAIGN_TYPE_TOKENS.items():
        if any(tok in name for tok in tokens):
            return ctype
    return "general"


def _build_excellence_block(campaign_name: str, summary: dict, camp_settings: dict) -> str:
    """
    Build the campaign-type-aware excellence block injected into the Claude prompt.
    Pulls numeric targets from excellence_targets DB and computes a live gap analysis
    against the campaign's actual metrics. Returns empty string on error so the
    optimizer continues working even if the DB call fails.
    """
    try:
        from database import get_excellence_targets
        ctype = _classify_campaign(campaign_name)

        # Pull relevant targets: 'all' targets + service-specific targets
        targets_all = get_excellence_targets(applies_to='all')
        targets_service = get_excellence_targets(applies_to=ctype) if ctype != 'all' else []
        all_targets = targets_all + targets_service

        # Metric name → live value mapping (from summary + camp_settings)
        # summary keys match what _call_claude_advisories receives
        live_values = {
            'ctr':                    summary.get('ctr', 0),
            'conv_rate':              summary.get('conv_rate', summary.get('conversion_rate', 0)),
            'cpl':                    summary.get('cost_per_lead', 0),
            'cost_per_new_patient':   summary.get('cost_per_lead', 0),  # best proxy available
            'impression_share':       (camp_settings.get('search_impression_share') or 0) * 100,
            'roas':                   summary.get('roas', 0),
            'budget_lost_is_threshold': (camp_settings.get('search_budget_lost_is') or 0) * 100,
            'rank_lost_is_threshold':   (camp_settings.get('search_rank_lost_is') or 0) * 100,
            # CPA targets: actual CPA from summary
            'cpa_min':  summary.get('cost_per_lead', 0),
            'cpa_max':  summary.get('cost_per_lead', 0),
        }

        # Build gap analysis lines
        gap_lines = []
        for t in all_targets:
            metric = t['metric']
            target_val = t['target_value']
            direction = t['direction']  # 'above' or 'below'
            unit = t['unit']
            label = t['label']
            live = live_values.get(metric)

            # Pre-compute target display string regardless of whether live data exists
            if unit == '%':
                _target_display = f"{target_val:.0f}%"
            elif unit == '$':
                _target_display = f"${target_val:.0f}"
            elif unit == 'x':
                _target_display = f"{target_val:.1f}x"
            else:
                _target_display = f"{target_val:.1f}"

            if live is None or live == 0:
                status = "no data"
                gap_str = "—"
                live_str = None
            else:
                if unit == '%':
                    live_str = f"{live:.1f}%"
                elif unit == '$':
                    live_str = f"${live:.0f}"
                elif unit == 'x':
                    live_str = f"{live:.1f}x"
                else:
                    live_str = f"{live:.1f}"

                if direction == 'above':
                    ok = live >= target_val
                    gap = live - target_val
                    gap_str = f"+{abs(gap):.1f}{unit} above target" if ok else f"{abs(gap):.1f}{unit} BELOW target"
                else:  # below
                    ok = live <= target_val
                    gap = target_val - live
                    gap_str = f"{abs(gap):.1f}{unit} under limit" if ok else f"{abs(gap):.1f}{unit} ABOVE limit"

                status = "✓ OK" if ok else "⚠ UNDERPERFORMING"

            gap_lines.append(
                f"  {label}: target {'>' if direction=='above' else '<'}{_target_display}"
                + (f"  |  current {live_str}  →  {status} ({gap_str})" if live_str else "  |  no data yet")
            )
            if t.get('notes'):
                gap_lines.append(f"    ({t['notes']})")

        # Build service-specific narrative
        narrative = _EXCELLENCE_NARRATIVE.get(ctype, _EXCELLENCE_NARRATIVE["general"])

        lines = [
            f"=== GDC EXCELLENCE BENCHMARKS — GAP ANALYSIS (campaign type: {ctype}) ===",
        ] + gap_lines + [
            "",
            f"=== EXCELLENCE PLAYBOOK — {ctype.upper()} CAMPAIGN RULES ===",
            narrative,
            "",
            _BIDDING_PHASES,
            "",
            _MATCH_TYPE_RULES,
            "",
            _NEGATIVE_KEYWORD_CATEGORIES,
            "",
            _RSA_GUIDANCE,
            "",
            _GEO_GUIDANCE,
            "",
            _TOP_MISTAKES,
            "=== END EXCELLENCE BLOCK ===",
        ]
        return "\n".join(lines) + "\n"

    except Exception as e:
        logger.warning(f"[optimizer] _build_excellence_block failed (non-fatal): {e}")
        return (
            "DENTAL PPC BENCHMARKS: CTR target >7%, CPL <$100, Conv rate >10%, "
            "Impression Share >65%, ROAS >4x. CPA targets: emergency $75-125, "
            "general $100-175, Invisalign $150-300, implants $200-400.\n"
        )


def _is_competitor_term(term: str) -> str:
    """Return reason string if this search term is a competitor brand search, else empty."""
    t = term.lower()
    for own in _OUR_NAMES:
        if own in t:
            return ""
    for comp in _COMPETITOR_NAMES:
        if comp in t:
            return f"Competitor brand search: '{comp}' — should not show our ads"
    return ""


def _is_negative_intent(term: str) -> str:
    """Check if a search term has negative intent. Returns reason or empty string."""
    t = term.lower()
    comp_reason = _is_competitor_term(t)
    if comp_reason:
        return comp_reason
    for signal in _HARD_NEGATIVES:
        if signal in t:
            return f"Negative intent: '{signal}'"
    for signal in _SOFT_NEGATIVES:
        if signal in t:
            if "free gingival" in t or "free connective" in t:
                return ""
            return f"Likely negative: '{signal}'"
    return ""


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
            ad_group_criterion.effective_cpc_bid_micros,
            ad_group_criterion.cpc_bid_micros,
            ad_group.name,
            ad_group.resource_name,
            campaign.name,
            campaign.resource_name,
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
            # Use cpc_bid_micros (manual CPC) if set; fall back to effective_cpc
            current_bid = (row.ad_group_criterion.cpc_bid_micros or
                           row.ad_group_criterion.effective_cpc_bid_micros or 0)
            results.append({
                "keyword": row.ad_group_criterion.keyword.text,
                "match_type": str(row.ad_group_criterion.keyword.match_type),
                "status": str(row.ad_group_criterion.status),
                "resource_name": row.ad_group_criterion.resource_name,
                "current_bid_micros": current_bid,
                "ad_group": row.ad_group.name,
                "ad_group_resource": row.ad_group.resource_name,
                "campaign": row.campaign.name,
                "campaign_resource": row.campaign.resource_name,
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


def _get_campaign_settings(client, customer_id: str, days: int = 30) -> dict:
    """
    Pull campaign-level settings and impression share metrics for each active campaign.
    Returns dict keyed by campaign resource_name.

    Fields per campaign:
      campaign_name, daily_budget_usd, bidding_strategy_type,
      target_cpa_micros, target_roas,
      search_impression_share, search_budget_lost_impression_share,
      search_rank_lost_impression_share
    """
    service = client.get_service("GoogleAdsService")
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=days)

    # Pass 1: campaign settings (budget, bidding strategy)
    settings_query = """
        SELECT
            campaign.resource_name,
            campaign.name,
            campaign.status,
            campaign.bidding_strategy_type,
            campaign.target_cpa.target_cpa_micros,
            campaign.target_roas.target_roas,
            campaign.maximize_conversions.target_cpa_micros,
            campaign.campaign_budget
        FROM campaign
        WHERE campaign.status IN (ENABLED, PAUSED)
    """

    campaign_settings: dict = {}
    budget_resources: set = set()
    try:
        for row in service.search(customer_id=customer_id, query=settings_query):
            rn = row.campaign.resource_name
            budget_rn = row.campaign.campaign_budget or ""
            if budget_rn:
                budget_resources.add(budget_rn)

            # Bidding strategy
            strategy_type = str(row.campaign.bidding_strategy_type).replace("BiddingStrategyType.", "")
            target_cpa = (
                row.campaign.target_cpa.target_cpa_micros
                or row.campaign.maximize_conversions.target_cpa_micros
                or 0
            )
            target_roas = row.campaign.target_roas.target_roas or 0.0

            campaign_settings[rn] = {
                "campaign_name": row.campaign.name,
                "bidding_strategy_type": strategy_type,
                "target_cpa_usd": round(target_cpa / 1_000_000, 2) if target_cpa else None,
                "target_roas": round(target_roas, 3) if target_roas else None,
                "daily_budget_usd": 0.0,
                "_budget_resource": budget_rn,
                # impression share filled in pass 2
                "search_impression_share": None,
                "search_budget_lost_is": None,
                "search_rank_lost_is": None,
            }
    except Exception as e:
        logger.warning(f"_get_campaign_settings pass 1 failed: {e}")

    # Pass 2: resolve budget amounts
    if budget_resources:
        in_list = ", ".join(f"'{rn}'" for rn in budget_resources)
        try:
            budget_query = f"""
                SELECT campaign_budget.resource_name, campaign_budget.amount_micros
                FROM campaign_budget
                WHERE campaign_budget.resource_name IN ({in_list})
            """
            budget_map = {}
            for row in service.search(customer_id=customer_id, query=budget_query):
                budget_map[row.campaign_budget.resource_name] = row.campaign_budget.amount_micros or 0
            for s in campaign_settings.values():
                brn = s.pop("_budget_resource", "")
                s["daily_budget_usd"] = round(budget_map.get(brn, 0) / 1_000_000, 2)
        except Exception as e:
            logger.warning(f"_get_campaign_settings budget pass failed: {e}")
            for s in campaign_settings.values():
                s.pop("_budget_resource", None)

    # Pass 3: impression share (requires date range segment)
    try:
        is_query = f"""
            SELECT
                campaign.resource_name,
                metrics.search_impression_share,
                metrics.search_budget_lost_impression_share,
                metrics.search_rank_lost_impression_share
            FROM campaign
            WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
              AND campaign.status IN (ENABLED, PAUSED)
        """
        is_totals: dict = {}
        is_counts: dict = {}
        for row in service.search(customer_id=customer_id, query=is_query):
            rn = row.campaign.resource_name
            is_totals.setdefault(rn, {"is": 0.0, "budget_lost": 0.0, "rank_lost": 0.0})
            is_counts.setdefault(rn, 0)
            is_totals[rn]["is"] += row.metrics.search_impression_share or 0
            is_totals[rn]["budget_lost"] += row.metrics.search_budget_lost_impression_share or 0
            is_totals[rn]["rank_lost"] += row.metrics.search_rank_lost_impression_share or 0
            is_counts[rn] += 1
        for rn, totals in is_totals.items():
            n = is_counts[rn] or 1
            if rn in campaign_settings:
                campaign_settings[rn]["search_impression_share"] = round(totals["is"] / n, 3)
                campaign_settings[rn]["search_budget_lost_is"] = round(totals["budget_lost"] / n, 3)
                campaign_settings[rn]["search_rank_lost_is"] = round(totals["rank_lost"] / n, 3)
    except Exception as e:
        logger.warning(f"_get_campaign_settings impression share pass failed: {e}")

    return campaign_settings


def _get_search_terms(client, customer_id: str, start_date: date | None = None, days: int = 30) -> list:
    """Pull search terms report to find new keywords and negatives.

    If start_date is provided, fetches from that date to today (used by memory system).
    Otherwise falls back to 'days' lookback window (legacy / first-run fallback).
    """
    service = client.get_service("GoogleAdsService")
    end_date = datetime.now(timezone.utc).date()
    if start_date is None:
        start_date = end_date - timedelta(days=days)

    query = f"""
        SELECT
            search_term_view.search_term,
            search_term_view.status,
            ad_group.resource_name,
            ad_group.name,
            campaign.resource_name,
            campaign.name,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions
        FROM search_term_view
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
            AND metrics.impressions > 0
            AND campaign.status = 'ENABLED'
            AND ad_group.status = 'ENABLED'
            AND search_term_view.status != 'EXCLUDED'
    """

    results = []
    try:
        response = service.search(customer_id=customer_id, query=query)
        for row in response:
            cost = (row.metrics.cost_micros or 0) / 1_000_000.0
            results.append({
                "search_term": row.search_term_view.search_term,
                "status": str(row.search_term_view.status),
                "ad_group_resource": row.ad_group.resource_name,
                "ad_group": row.ad_group.name,
                "campaign_resource": row.campaign.resource_name,
                "campaign": row.campaign.name,
                "impressions": row.metrics.impressions or 0,
                "clicks": row.metrics.clicks or 0,
                "cost": cost,
                "conversions": row.metrics.conversions or 0,
            })
    except Exception as e:
        logger.error(f"Failed to get search terms: {e}")

    return results


def _get_google_recommendations(client, customer_id: str) -> list:
    """
    Pull Google's own recommendations via RecommendationService.
    Returns list of dicts with type, resource_name, title, description, impact, details.

    IMPORTANT: Only select top-level GAQL-selectable fields in the query.
    Nested sub-fields (impact.base_metrics.*, keyword_recommendation.keyword.text, etc.)
    are NOT selectable via GAQL — they are returned automatically on the proto object
    once the parent message is in the SELECT list. We read them via getattr after fetch.
    """
    service = client.get_service("GoogleAdsService")
    query = """
        SELECT
            recommendation.resource_name,
            recommendation.type,
            recommendation.campaign,
            recommendation.ad_group,
            recommendation.dismissed,
            campaign.name,
            campaign.resource_name
        FROM recommendation
        WHERE recommendation.dismissed = FALSE
    """
    results = []
    try:
        response = service.search(customer_id=customer_id, query=query)
        for row in response:
            rec = row.recommendation
            # rec_type: use .name for enum, fall back to int string
            try:
                rec_type = rec.type_.name
            except AttributeError:
                rec_type = str(int(rec.type_))

            # Impact — proto fields are populated on the object even though we
            # didn't SELECT the sub-fields in GAQL (they come as part of the message)
            try:
                base = rec.impact.base_metrics
                potential = rec.impact.potential_metrics
                impact = {
                    "base_impressions": float(getattr(base, 'impressions', 0) or 0),
                    "base_clicks": float(getattr(base, 'clicks', 0) or 0),
                    "base_cost": (int(getattr(base, 'cost_micros', 0) or 0)) / 1_000_000,
                    "base_conversions": float(getattr(base, 'conversions', 0) or 0),
                    "potential_impressions": float(getattr(potential, 'impressions', 0) or 0),
                    "potential_clicks": float(getattr(potential, 'clicks', 0) or 0),
                    "potential_cost": (int(getattr(potential, 'cost_micros', 0) or 0)) / 1_000_000,
                    "potential_conversions": float(getattr(potential, 'conversions', 0) or 0),
                }
            except Exception:
                impact = {
                    "base_impressions": 0, "base_clicks": 0, "base_cost": 0, "base_conversions": 0,
                    "potential_impressions": 0, "potential_clicks": 0, "potential_cost": 0, "potential_conversions": 0,
                }

            # Extract type-specific details — use try/except per type since
            # proto oneof fields throw AttributeError if the wrong variant is accessed
            details = {}
            title = rec_type.replace("_", " ").title()
            description = ""

            try:
                if rec_type == "KEYWORD":
                    kw = rec.keyword_recommendation
                    kw_text = kw.keyword.text or ""
                    try:
                        kw_match = kw.keyword.match_type.name
                    except AttributeError:
                        kw_match = str(kw.keyword.match_type)
                    bid = (kw.recommended_cpc_bid_micros or 0) / 1_000_000
                    details = {"keyword_text": kw_text, "match_type": kw_match, "recommended_cpc_bid": bid}
                    title = f"Add Keyword: {kw_text}"
                    description = f"Add '{kw_text}' [{kw_match}] at ${bid:.2f} CPC"

                elif rec_type == "KEYWORD_MATCH_TYPE":
                    km = rec.keyword_match_type_recommendation
                    kw_text = km.keyword.text or ""
                    try:
                        from_type = km.keyword.match_type.name
                        to_type = km.recommended_match_type.name
                    except AttributeError:
                        from_type = str(km.keyword.match_type)
                        to_type = str(km.recommended_match_type)
                    details = {"keyword_text": kw_text, "from_match_type": from_type, "to_match_type": to_type}
                    title = f"Change Match Type: {kw_text}"
                    description = f"Change '{kw_text}' from {from_type} → {to_type}"

                elif rec_type == "MAXIMIZE_CONVERSIONS_OPT_IN":
                    budget = (rec.maximize_conversions_opt_in_recommendation.recommended_budget_amount_micros or 0) / 1_000_000
                    details = {"recommended_budget": budget}
                    title = "Switch to Maximize Conversions"
                    description = f"Switch bid strategy to Maximize Conversions (recommended budget: ${budget:.0f}/day)"

                elif rec_type == "TARGET_CPA_OPT_IN":
                    r = rec.target_cpa_opt_in_recommendation
                    cpa = (r.recommended_target_cpa_micros or 0) / 1_000_000
                    req_budget = (r.required_campaign_budget_amount_micros or 0) / 1_000_000
                    details = {"recommended_target_cpa": cpa, "required_budget": req_budget}
                    title = "Switch to Target CPA Bidding"
                    description = f"Switch to Target CPA at ${cpa:.2f} (requires ${req_budget:.0f}/day budget)"

                elif rec_type == "TARGET_ROAS_OPT_IN":
                    roas = rec.target_roas_opt_in_recommendation.recommended_target_roas or 0
                    details = {"recommended_target_roas": roas}
                    title = "Switch to Target ROAS Bidding"
                    description = f"Switch to Target ROAS at {roas:.1%}"

                elif rec_type in ("MARGINAL_ROI_CAMPAIGN_BUDGET", "CAMPAIGN_BUDGET"):
                    budget_rec = (rec.marginal_roi_campaign_budget_recommendation
                                  if rec_type == "MARGINAL_ROI_CAMPAIGN_BUDGET"
                                  else rec.campaign_budget_recommendation)
                    rec_budget = (budget_rec.recommended_budget_amount_micros or 0) / 1_000_000
                    cur_budget = (budget_rec.current_budget_amount_micros or 0) / 1_000_000
                    details = {"current_budget": cur_budget, "recommended_budget": rec_budget,
                               "campaign_resource": rec.campaign or ""}
                    title = "Increase Campaign Budget"
                    description = f"Increase daily budget from ${cur_budget:.0f} to ${rec_budget:.0f}"

                elif rec_type == "MOVE_UNUSED_BUDGET":
                    mu = rec.move_unused_budget_recommendation
                    # budget_recommendation is a CampaignBudgetRecommendation sub-message
                    rec_amount = (mu.budget_recommendation.recommended_budget_amount_micros or 0) / 1_000_000
                    details = {"recommended_budget_amount": rec_amount,
                               "excess_campaign_budget": mu.excess_campaign_budget or ""}
                    title = "Move Unused Budget"
                    description = f"Reallocate unused budget (${rec_amount:.0f}) to this campaign"

                elif rec_type in ("RESPONSIVE_SEARCH_AD", "RESPONSIVE_SEARCH_AD_IMPROVE_AD_STRENGTH"):
                    details = {"has_rsa_suggestion": True}
                    title = "Improve Responsive Search Ad"
                    description = "Google recommends updating ad copy for better Ad Strength"

                elif rec_type in ("SITELINK_ASSET", "SITELINK_EXTENSION"):
                    title = "Add Sitelink Assets"
                    description = "Add sitelink assets to improve ad visibility (+10-20% CTR)"

                elif rec_type in ("CALLOUT_ASSET", "CALLOUT_EXTENSION"):
                    title = "Add Callout Assets"
                    description = "Add callout assets to highlight practice features"

                elif rec_type in ("CALL_ASSET", "CALL_EXTENSION"):
                    title = "Add Call Asset"
                    description = "Add call asset to enable direct calling from ads"

                elif rec_type == "MAXIMIZE_CLICKS_OPT_IN":
                    title = "Switch to Maximize Clicks"
                    description = "Switch bid strategy to Maximize Clicks"

                elif rec_type == "ENHANCED_CPC_OPT_IN":
                    title = "Enable Enhanced CPC"
                    description = "Enable Enhanced CPC to optimize manual bids with AI"

                elif rec_type == "USE_BROAD_MATCH_KEYWORD":
                    r = rec.use_broad_match_keyword_recommendation
                    kw_count = r.suggested_keywords_count or 0
                    details = {"suggested_keywords_count": kw_count,
                               "required_budget": (r.required_campaign_budget_amount_micros or 0) / 1_000_000}
                    title = "Use Broad Match Keywords"
                    description = f"Switch {kw_count} keywords to broad match for wider reach"

                elif rec_type == "RAISE_TARGET_CPA":
                    r = rec.raise_target_cpa_recommendation
                    details = {"target_adjustment": str(getattr(r, 'target_adjustment', ''))}
                    title = "Raise Target CPA"
                    description = "Google recommends raising Target CPA to get more conversions"

            except Exception as detail_err:
                logger.debug(f"Could not extract details for {rec_type}: {detail_err}")

            results.append({
                "resource_name": rec.resource_name,
                "rec_type": rec_type,
                "campaign_resource": row.campaign.resource_name if row.campaign.resource_name else "",
                "campaign_name": row.campaign.name if row.campaign.name else "",
                "ad_group_resource": rec.ad_group if rec.ad_group else "",
                "title": title,
                "description": description,
                "impact": impact,
                "details": details,
            })
    except Exception as e:
        logger.error(f"Failed to get Google recommendations: {e}")
    logger.info(f"Fetched {len(results)} Google recommendations")
    return results


def _apply_google_recommendation(client, customer_id: str, resource_name: str) -> bool:
    """
    Apply a Google recommendation directly via ApplyRecommendation.
    This is the simplest path for most rec types — Google applies it server-side.
    Returns True on success.
    """
    service = client.get_service("RecommendationService")
    operation = client.get_type("ApplyRecommendationOperation")
    operation.resource_name = resource_name
    try:
        response = service.apply_recommendation(
            customer_id=customer_id,
            operations=[operation],
            partial_failure=True,
        )
        logger.info(f"Applied Google rec {resource_name}: {response}")
        return True
    except Exception as e:
        logger.error(f"Failed to apply Google rec {resource_name}: {e}")
        raise


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


def _get_call_attribution(days: int = 30) -> dict:
    """
    Return per-campaign inbound call attribution directly from mango_calls.
    Bypasses v_campaign_call_stats (which requires gads_campaign_numeric_id on campaigns
    rows — often unpopulated — causing false-zero call counts).

    Resolution order for campaign name:
      1. gads_call_view.campaign_name  (most authoritative — came from GAds API)
      2. leads.campaign_name           (form-fill attribution)
      3. campaigns.campaign_name via gads_campaign_numeric_id (ID-based lookup)

    Returns: {campaign_name_lower: {campaign_name, calls, booked_calls, confirmed_appts,
                                    avg_duration_sec, gcv_campaign_id}}
    """
    from database import _conn
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    out = {}
    try:
        with _conn() as conn:
            rows = conn.execute("""
                WITH resolved AS (
                  SELECT
                    mc.uuid,
                    mc.duration_sec,
                    mc.booked_outcome,
                    mc.od_appointment_id,
                    gcv.campaign_id   AS gcv_campaign_id,
                    COALESCE(
                      NULLIF(TRIM(gcv.campaign_name), ''),
                      NULLIF(TRIM(l.campaign_name),   ''),
                      (SELECT campaign_name FROM campaigns
                         WHERE gads_campaign_numeric_id = gcv.campaign_id LIMIT 1)
                    ) AS campaign_name
                  FROM mango_calls mc
                  LEFT JOIN gads_call_view gcv ON gcv.call_id = mc.gads_call_id
                  LEFT JOIN leads l            ON l.id = mc.lead_id
                  WHERE mc.started_at >= ?
                    AND mc.direction = 'inbound'
                )
                SELECT
                  LOWER(TRIM(campaign_name))  AS key,
                  campaign_name,
                  gcv_campaign_id,
                  COUNT(DISTINCT uuid)         AS calls,
                  SUM(CASE WHEN booked_outcome = 'booked' THEN 1 ELSE 0 END) AS booked_calls,
                  SUM(CASE WHEN od_appointment_id IS NOT NULL
                            AND od_appointment_id != '' THEN 1 ELSE 0 END)   AS confirmed_appts,
                  AVG(duration_sec)            AS avg_duration_sec
                FROM resolved
                WHERE campaign_name IS NOT NULL AND TRIM(campaign_name) != ''
                GROUP BY LOWER(TRIM(campaign_name)), campaign_name, gcv_campaign_id
            """, (cutoff,)).fetchall()

            for r in rows:
                key = r["key"]
                if not key:
                    continue
                if key not in out:
                    out[key] = {
                        "campaign_name": r["campaign_name"],
                        "gcv_campaign_id": r["gcv_campaign_id"] or "",
                        "calls": 0, "booked_calls": 0,
                        "confirmed_appts": 0, "avg_duration_sec": 0.0,
                    }
                # Merge rows (multiple gcv_campaign_id values can share a campaign name)
                out[key]["calls"]          += int(r["calls"] or 0)
                out[key]["booked_calls"]   += int(r["booked_calls"] or 0)
                out[key]["confirmed_appts"] += int(r["confirmed_appts"] or 0)
                # Weighted average for duration
                prev_avg = out[key]["avg_duration_sec"]
                prev_n   = out[key]["calls"] - int(r["calls"] or 0)
                new_n    = int(r["calls"] or 0)
                if out[key]["calls"] > 0:
                    out[key]["avg_duration_sec"] = (
                        (prev_avg * prev_n + float(r["avg_duration_sec"] or 0.0) * new_n)
                        / out[key]["calls"]
                    )

            # Log unresolved calls for diagnostics
            unresolved = conn.execute("""
                SELECT COUNT(*) FROM mango_calls mc
                LEFT JOIN gads_call_view gcv ON gcv.call_id = mc.gads_call_id
                LEFT JOIN leads l ON l.id = mc.lead_id
                WHERE mc.started_at >= ? AND mc.direction = 'inbound'
                  AND COALESCE(NULLIF(TRIM(gcv.campaign_name),''),
                               NULLIF(TRIM(l.campaign_name),'')) IS NULL
            """, (cutoff,)).fetchone()[0]
            if unresolved > 0:
                logger.warning(f"Call attribution: {unresolved} inbound calls have no resolvable campaign name "
                               f"(no gads_call_view match AND no lead campaign). Run Sync Now to fix.")

    except Exception as e:
        logger.error(f"Failed to build call attribution: {e}", exc_info=True)
    return out


def _get_keyword_call_attribution(days: int = 30) -> dict:
    """
    Return per-keyword inbound call attribution using the attributed_keyword column
    set by call_keyword_attribution.py (Methods A/B/C only — excludes campaign_only).

    Returns: {keyword_lower: {keyword, match_type, ad_group, calls, booked_calls,
                               confirmed_appts, avg_duration_sec, campaigns: [str]}}
    """
    from database import _conn
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    out = {}
    try:
        with _conn() as conn:
            # Pre-aggregate per call in a CTE to prevent JOIN row multiplication.
            # gads_call_view or leads may match multiple rows per call; the CTE
            # collapses mango_calls first, then joins for campaign label only.
            rows = conn.execute("""
                WITH call_agg AS (
                  SELECT
                    mc.uuid,
                    LOWER(TRIM(mc.attributed_keyword))  AS kw_lower,
                    mc.attributed_keyword               AS keyword,
                    mc.attributed_match_type            AS match_type,
                    mc.attributed_ad_group              AS ad_group,
                    mc.gads_call_id,
                    mc.lead_id,
                    CASE WHEN mc.booked_outcome = 'booked' THEN 1 ELSE 0 END AS is_booked,
                    CASE WHEN mc.od_appointment_id IS NOT NULL
                          AND mc.od_appointment_id != '' THEN 1 ELSE 0 END   AS is_confirmed,
                    COALESCE(mc.duration_sec, 0)        AS duration_sec
                  FROM mango_calls mc
                  WHERE mc.started_at >= ?
                    AND mc.direction = 'inbound'
                    AND mc.attributed_keyword IS NOT NULL
                    AND mc.attributed_keyword != ''
                    AND mc.attributed_keyword_method NOT IN ('campaign_only', 'no_signal')
                )
                SELECT
                  ca.kw_lower,
                  ca.keyword,
                  MAX(ca.match_type)   AS match_type,
                  MAX(ca.ad_group)     AS ad_group,
                  COALESCE(NULLIF(gcv.campaign_name,''),
                           NULLIF(l.campaign_name,''), '')  AS campaign_name,
                  COUNT(DISTINCT ca.uuid)                   AS calls,
                  SUM(ca.is_booked)                         AS booked_calls,
                  SUM(ca.is_confirmed)                      AS confirmed_appts,
                  SUM(ca.duration_sec)                      AS total_duration_sec
                FROM call_agg ca
                LEFT JOIN gads_call_view gcv ON gcv.call_id = ca.gads_call_id
                LEFT JOIN leads l            ON l.id        = ca.lead_id
                GROUP BY ca.kw_lower, ca.keyword,
                         COALESCE(NULLIF(gcv.campaign_name,''), NULLIF(l.campaign_name,''), '')
            """, (cutoff,)).fetchall()

            # Merge rows with the same keyword across campaigns
            for r in rows:
                kw_lower = r["kw_lower"] or ""
                if not kw_lower:
                    continue
                if kw_lower not in out:
                    out[kw_lower] = {
                        "keyword": r["keyword"],
                        "match_type": r["match_type"] or "",
                        "ad_group": r["ad_group"] or "",
                        "calls": 0,
                        "booked_calls": 0,
                        "confirmed_appts": 0,
                        "_total_duration_sec": 0.0,   # internal accumulator, removed before return
                        "campaigns": [],
                    }
                entry = out[kw_lower]
                entry["calls"] += int(r["calls"] or 0)
                entry["booked_calls"] += int(r["booked_calls"] or 0)
                entry["confirmed_appts"] += int(r["confirmed_appts"] or 0)
                entry["_total_duration_sec"] += float(r["total_duration_sec"] or 0.0)
                if r["campaign_name"] and r["campaign_name"] not in entry["campaigns"]:
                    entry["campaigns"].append(r["campaign_name"])

        # Compute avg_duration_sec and remove internal accumulator
        for entry in out.values():
            entry["avg_duration_sec"] = (
                round(entry["_total_duration_sec"] / entry["calls"], 1)
                if entry["calls"] > 0 else 0.0
            )
            del entry["_total_duration_sec"]

    except Exception as e:
        logger.error(f"Failed to build keyword call attribution: {e}")
    return out


def _get_od_production_summary(days: int = 30) -> dict:
    """
    Roll up attributed_production from leads over the last N days.
    Returns: {"total_attributed": float, "by_campaign": {campaign_name: float}}
    """
    from database import _conn
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    total = 0.0
    by_camp: dict = {}
    try:
        with _conn() as conn:
            rows = conn.execute("""
                SELECT COALESCE(NULLIF(campaign_name,''),'(unknown)') AS campaign_name,
                       SUM(COALESCE(attributed_production,0))         AS prod
                  FROM leads
                 WHERE created_at >= ?
                 GROUP BY campaign_name
            """, (cutoff,)).fetchall()
            for r in rows:
                amt = float(r["prod"] or 0.0)
                if amt <= 0:
                    continue
                total += amt
                by_camp[r["campaign_name"]] = round(amt, 2)
    except Exception as e:
        logger.error(f"OD production summary failed: {e}")
    return {"total_attributed": round(total, 2), "by_campaign": by_camp}


def _score_tier(impr: int, clicks: int, cost_usd: float, conv: float, avg_ctr: float) -> str:
    """
    Shared tier scorer for ads and ad groups.
    Returns: "cold" | "weak" | "average" | "strong"

    Thresholds (aligned across both uses):
    - cold:   < 100 impressions — not enough data
    - weak:   zero conversions after ≥$30 spend (even if CTR looks ok)
    - strong: CTR ≥ 120% of campaign average
    - average: CTR ≥ 50% of campaign average
    - weak:   CTR < 50% of campaign average
    """
    if impr < 100:
        return "cold"
    if conv == 0 and cost_usd >= 30:
        return "weak"
    ctr = (clicks / impr) if impr > 0 else 0
    if avg_ctr > 0:
        if ctr >= avg_ctr * 1.2:
            return "strong"
        if ctr >= avg_ctr * 0.5:
            return "average"
        return "weak"
    # No campaign average yet — treat as average
    return "average"


def _call_claude_advisories(keyword_perf: list, attribution: dict, search_terms: list,
                             call_attribution: dict, od_production: dict,
                             summary: dict, campaign: str,
                             keyword_call_attribution: dict | None = None,
                             feedback: str = "",
                             rsa_resources: list | None = None,
                             geo_resolutions: dict | None = None,
                             google_recs: list | None = None,
                             optimizer_run_id: str = "",
                             existing_negatives: set | None = None,
                             memory_digest: dict | None = None,
                             camp_settings: dict | None = None,
                             ad_performance: list | None = None,
                             landing_page_intel: str = "",
                             ad_group_performance: list | None = None) -> list:
    """
    Ask Claude (Opus) for structured, actionable recommendations for this campaign.
    Each recommendation is a dict with operation + exact parameters ready to execute via API.

    Supported operations Claude may return:
      add_negative_keyword  — {keyword_text, match_type, reason}
      pause_keyword         — {keyword_text, resource_name, reason}
      increase_bid          — {keyword_text, resource_name, new_bid_micros, reason}
      decrease_bid          — {keyword_text, resource_name, new_bid_micros, reason}
      add_exact_keyword     — {keyword_text, ad_group_resource, reason}
      ad_copy_suggestion    — {headline, description, reason}  (informational — no API call)
      geo_exclusion         — {location_name, reason}          (informational — logged for review)

    Returns list of dicts. Never raises — failure returns [].
    """
    import os, re as _re
    try:
        from database import get_setting as _get_setting
    except Exception:
        _get_setting = lambda k: None
    _api_key = _get_setting("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    if not _api_key:
        return []
    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=_api_key)

        call_summary = {
            v["campaign_name"]: {
                "calls": v["calls"],
                "booked": v["booked_calls"],
                "confirmed_appts": v["confirmed_appts"],
                "avg_duration_sec": round(v.get("avg_duration_sec") or 0),
            }
            for v in call_attribution.values()
        }
        kw_call_summary = {
            kw: {
                "calls": v["calls"],
                "booked_calls": v["booked_calls"],
                "confirmed_appts": v["confirmed_appts"],
                "avg_duration_sec": round(v.get("avg_duration_sec") or 0),
            }
            for kw, v in sorted(
                (keyword_call_attribution or {}).items(),
                key=lambda x: -(x[1].get("confirmed_appts", 0) * 10 + x[1].get("calls", 0))
            )[:20]
        }

        # Enrich search terms with resource names from keyword_perf for API execution
        kw_resource_map = {
            kw.get("keyword", "").strip().lower(): {
                "resource_name": kw.get("resource_name", ""),
                "ad_group_resource": kw.get("ad_group_resource", ""),
                "campaign_resource": kw.get("campaign_resource", ""),
                "current_bid_micros": kw.get("cpc_bid_micros", 0),
                "match_type": kw.get("match_type", "BROAD"),
            }
            for kw in keyword_perf if kw.get("keyword")
        }
        # Campaign resource name from keyword_perf (first match for this campaign)
        campaign_resource = ""
        for kw in keyword_perf:
            if kw.get("campaign", "").strip().lower() == campaign.strip().lower():
                campaign_resource = kw.get("campaign_resource", "")
                if campaign_resource:
                    break

        context = {
            "campaign_name": campaign,
            "campaign_resource": campaign_resource,
            "campaign_settings": camp_settings or {},   # budget, bidding strategy, impression share
            "summary": summary,
            "keyword_performance": [
                {**k, **kw_resource_map.get(k.get("keyword","").lower(), {})}
                for k in keyword_perf[:50]
            ],
            "search_terms_top": sorted(search_terms, key=lambda s: -s.get("cost", 0))[:30],
            "form_attribution": attribution,
            "call_summary": call_summary,
            "keyword_call_summary": kw_call_summary,
            "od_production_summary": od_production,
            "keyword_resource_map": kw_resource_map,
            "rsa_resources": (rsa_resources or [])[:10],
            "geo_resolutions": geo_resolutions or {},
            "google_recommendations": (google_recs or [])[:20],
            "existing_negative_keywords": sorted(existing_negatives)[:200] if existing_negatives else [],
            "optimizer_memory": memory_digest or {},
            "ad_performance": (ad_performance or [])[:20],  # per-RSA performance + scored metrics
            "ad_group_performance": (ad_group_performance or [])[:10],  # per-ad-group scored metrics
        }

        feedback_block = f"\n\nUSER FEEDBACK (incorporate this):\n{feedback}" if feedback else ""

        rsa_note = ""
        if rsa_resources:
            rsa_note = (
                "\n\nRSA RESOURCES (use ad_group_ad_resource for ad_copy_suggestion or replace_ad):\n"
                + json.dumps(rsa_resources[:5], default=str)
            )
        geo_note = ""
        if geo_resolutions:
            geo_note = (
                "\n\nPRE-RESOLVED GEO TARGETS (use geo_target_resource for geo_exclusion):\n"
                + json.dumps(geo_resolutions, default=str)
            )
        ad_perf_note = ""
        if ad_performance:
            ad_perf_note = (
                "\n\nAD PERFORMANCE (30-day metrics per RSA — use for replace_ad decisions):\n"
                + json.dumps(ad_performance[:10], default=str)
            )
        page_intel_note = ""
        if landing_page_intel:
            page_intel_note = (
                "\n\nLANDING PAGE INTELLIGENCE (use to write grounded ad copy for replace_ad):\n"
                + landing_page_intel[:2000]
            )
        ag_perf_note = ""
        if ad_group_performance:
            ag_perf_note = (
                "\n\nAD GROUP PERFORMANCE (30-day metrics, pre-scored — use for pause_ad_group decisions):\n"
                + json.dumps(ad_group_performance[:10], default=str)
            )

        excellence_block = _build_excellence_block(campaign, summary, camp_settings or {})

        prompt = excellence_block + """
You are a Google Ads specialist optimizing a dental practice's campaigns.
Analyze the data and return up to 7 SPECIFIC, EXECUTABLE recommendations.

CAMPAIGN SETTINGS (use these to inform every recommendation):
The field "campaign_settings" in the data contains the live configuration from Google Ads:
- daily_budget_usd: the actual daily budget currently set in Google Ads
- bidding_strategy_type: e.g. MANUAL_CPC, MAXIMIZE_CONVERSIONS, TARGET_CPA, TARGET_ROAS
- target_cpa_usd: target cost-per-acquisition (for Target CPA campaigns; null if not set)
- target_roas: target return on ad spend (for Target ROAS campaigns; null if not set)
- search_impression_share: fraction of eligible impressions we're capturing (0–1.0)
- search_budget_lost_is: impression share lost due to budget being too low (0–1.0)
- search_rank_lost_is: impression share lost due to ad rank / bid too low (0–1.0)

Use this to:
- If search_budget_lost_is > 0.2: the campaign is budget-constrained — increasing bids will not help, suggest change_budget instead
- If search_rank_lost_is > 0.3: the campaign is losing due to low bids/quality — bid increases or quality improvements are warranted
- If bidding_strategy_type is MANUAL_CPC and the campaign has 30+ conversions in 30 days: consider recommending change_bid_strategy to MAXIMIZE_CONVERSIONS or TARGET_CPA
- If bidding_strategy_type is MAXIMIZE_CONVERSIONS or TARGET_CPA: do NOT suggest manual bid adjustments (increase_bid / decrease_bid) — the smart bidding algorithm manages bids automatically
- Always reference the current daily_budget_usd when recommending a change_budget — state the specific dollar increase and the % change

Each recommendation MUST be a JSON object with these fields:
- "operation": one of: add_negative_keyword | pause_keyword | increase_bid | decrease_bid | add_exact_keyword | ad_copy_suggestion | geo_exclusion | enable_keyword | change_budget | change_bid_strategy | change_match_type | add_asset | replace_ad | pause_ad_group
- "reason": 1-2 sentence explanation with specific numbers from the data
- "estimated_monthly_impact": object with keys:
    "savings_usd": estimated monthly dollar savings (0 if not applicable),
    "impact_type": one of "waste_reduction"|"conversion_lift"|"bid_efficiency"|"coverage_gain",
    "confidence": "high"|"medium"|"low",
    "benchmark_gap": brief string describing which benchmark this closes (e.g. "CTR below 7% target" or "negative keywords missing — est. 20-42% waste")
- Operation-specific fields:

For add_negative_keyword:
  "keyword_text": exact term to block, "match_type": "EXACT"|"PHRASE"|"BROAD",
  "campaign_resource": the campaign resource name from the data

For pause_keyword:
  "keyword_text": keyword, "resource_name": the keyword resource_name from data

For enable_keyword:
  "keyword_text": keyword, "resource_name": the keyword resource_name from data (must be a PAUSED keyword)

For increase_bid / decrease_bid:
  "keyword_text": keyword, "resource_name": resource_name,
  "new_bid_micros": integer (current bid ± 10-20%)

For add_exact_keyword:
  "keyword_text": search term, "ad_group_resource": ad_group_resource from data

For ad_copy_suggestion:
  "headline": new headline (STRICT MAX 30 chars — count carefully),
  "description": new description (STRICT MAX 90 chars — count carefully),
  "ad_resource": the ad_group_ad_resource from rsa_resources data (required for API execution)

For geo_exclusion:
  "location_name": city/region name to exclude,
  "geo_target_resource": resource_name from geo_resolutions data (required for API execution)

For change_budget:
  "new_daily_budget_usd": float (e.g. 35.0), max 25% increase from current,
  "campaign_resource": the campaign resource name from the data

For change_bid_strategy:
  "bid_strategy": "MAXIMIZE_CONVERSIONS"|"TARGET_CPA"|"TARGET_ROAS"|"MAXIMIZE_CLICKS",
  "target_cpa_micros": integer (only for TARGET_CPA),
  "target_roas": float (only for TARGET_ROAS),
  "campaign_resource": campaign resource name

For change_match_type:
  "keyword_text": keyword text,
  "resource_name": keyword resource_name,
  "new_match_type": "EXACT"|"PHRASE"|"BROAD"

For add_asset:
  "asset_type": "SITELINK"|"CALLOUT"|"CALL",
  "campaign_resource": campaign resource name,
  "description": what to add (advisory — no API call)

For replace_ad (A/B ad testing — pause underperformer, create improved version):
  "old_ad_group_ad_resource": EXACT ad_group_ad_resource from ad_performance or rsa_resources data (required)
  "new_headlines": array of 10-15 strings, each STRICTLY ≤30 chars — count every character
  "new_descriptions": array of 3-4 strings, each STRICTLY ≤90 chars
  "final_url": copy from old ad's final_url unless campaign context suggests a better page
  "path1": optional display-URL segment ≤15 chars (e.g. "dentures")
  "path2": optional display-URL segment ≤15 chars (e.g. "grafton-ma")
  "ad_group_resource": copy from the ad_performance row (required)
  Use replace_ad ONLY when: CTR < 50% of campaign average for 30+ days with ≥200 impressions,
  OR zero conversions after spending ≥$30, OR impressions < 100 in 30 days when budget allows.
  Ground new copy in landing_page_intel — use real service names, offers, CTAs from the page.
  Headlines must be specific and locally grounded. Avoid generic phrases like "Quality Care".
  CRITICAL — Google policy rules for ad text (violations cause PROHIBITED rejection):
    - NEVER include phone numbers in any headline or description (e.g. "508-123-4567" or "(508) 839-xxxx")
    - NEVER include URLs or domain names in headlines/descriptions
    - NEVER use excessive capitalization (e.g. "CALL NOW FREE CONSULTATION")
    - NEVER use punctuation in headlines (no periods, exclamation marks, etc.)
  A/B principle: only recommend ONE replace_ad per campaign per run. Keep changes focused.

For pause_ad_group (pause an underperforming ad group — does NOT pause the whole campaign):
  "ad_group_resource": EXACT ad_group_resource from ad_group_performance data (required — do not invent)
  "ad_group_name": human-readable name from ad_group_performance data
  Use pause_ad_group ONLY when ALL of these are true:
    - cost_30d_usd ≥ $30 AND conversions_30d = 0 AND lead_count_30d = 0 AND impressions_30d ≥ 100
    - performance_tier is "weak" or "cold"
    - The campaign has at least 2 active ad groups (never pause the campaign's ONLY ad group)
  This is a last resort — prefer replace_ad or keyword changes within the group first.
  Limit: only ONE pause_ad_group per campaign per optimizer run.

AD PERFORMANCE SCORING CONTEXT:
The "ad_performance" field contains per-RSA metrics. Key fields:
  - ctr: click-through rate (30-day)
  - avg_campaign_ctr: average CTR for all ads in this campaign (use as benchmark)
  - impressions_30d: total impressions in 30 days
  - cost_30d_usd: spend in 30 days
  - conversions_30d: attributed conversions
  - performance_tier: "strong"|"average"|"weak"|"cold" (pre-computed)
  - ad_group_ad_resource: use this EXACTLY for old_ad_group_ad_resource in replace_ad
  - ad_group_resource: use this EXACTLY for ad_group_resource in replace_ad
Prioritize "weak" or "cold" tier ads for replacement. Never replace "strong" tier ads.

AD GROUP PERFORMANCE SCORING CONTEXT:
The "ad_group_performance" field contains per-ad-group metrics. Key fields:
  - ad_group_resource: use this EXACTLY for ad_group_resource in pause_ad_group (never modify)
  - ad_group_name: human-readable name
  - impressions_30d, clicks_30d, cost_30d_usd, conversions_30d, lead_count_30d
  - ctr, avg_campaign_ctr: click-through rates (ad group vs. campaign average)
  - performance_tier: "strong"|"average"|"weak"|"cold" (pre-computed using same thresholds as ads)
  - cpl: cost per lead (0 if no leads)
  Lead count and revenue come from OD string-match attribution — use for directional guidance only.
  Only recommend pause_ad_group for "weak" or "cold" tier groups meeting the spend/conversion thresholds above.

GOOGLE'S OWN RECOMMENDATIONS (pulled live from Google Ads API):
Google has flagged the following recommendations for this account. Evaluate each one against the campaign data and lead/call attribution above. For each Google rec:
- If the data supports it → include it as your recommendation with operation matching the rec type, add "google_rec_resource_name": the resource_name field
- If the data contradicts it (e.g. Google says add keyword X but our data shows it converts poorly) → explicitly reject it in your reasoning but do NOT include it
- If neutral/unknown → include it as advisory

When endorsing a Google recommendation, use these operation mappings:
- KEYWORD rec → "add_exact_keyword" operation (or add_negative if it looks like a negative)
- KEYWORD_MATCH_TYPE → "change_match_type" operation
- MARGINAL_ROI_CAMPAIGN_BUDGET / CAMPAIGN_BUDGET → "change_budget" operation
- MAXIMIZE_CONVERSIONS_OPT_IN → "change_bid_strategy" operation
- TARGET_CPA_OPT_IN → "change_bid_strategy" operation
- SITELINK_EXTENSION / CALLOUT_EXTENSION / CALL_EXTENSION → "add_asset" operation (advisory)
- RESPONSIVE_SEARCH_AD → "ad_copy_suggestion" operation

Always include "google_rec_resource_name" field in any rec that came from Google.

Rules:
- Only use resource_names that appear in the data — never invent them
- If resource_name is unavailable for a keyword operation, skip that recommendation
- For ad_copy_suggestion: ALWAYS include ad_resource from rsa_resources — skip if none available
- For geo_exclusion: ONLY suggest if geo_target_resource is available in geo_resolutions
- Prioritize recommendations that stop wasted spend first
- COMPETITOR SEARCHES: Any search term containing a competitor practice name (e.g. "grace dental", "simply orthodontics", "aspen dental", "gentle dental", any "[name] dental [city]" that isn't Grafton Dental Care) MUST be flagged as add_negative_keyword. These waste budget showing our ads to people searching for a competitor.
- EXISTING NEGATIVES: The field "existing_negative_keywords" in the data lists keywords already added as negatives in Google Ads. Do NOT suggest add_negative_keyword for any term that already appears in that list (exact or near-match). Only flag NEW terms not yet blocked.
- OPTIMIZER MEMORY: The field "optimizer_memory" in the data contains historical run summaries. Use it to: (1) avoid repeating recommendations that were recently rejected, (2) build on patterns from past runs, (3) surface new issues not seen before. Do not re-suggest anything in "rejected_patterns".
- Return ONLY a valid JSON array, no markdown, no explanation outside the array
- For estimated_monthly_impact.savings_usd: use the keyword/search term cost data to estimate realistically. For waste_reduction ops (negatives, pauses): savings = the monthly spend being wasted. For conversion_lift ops (ad copy, landing page): savings = estimated CPL reduction × monthly lead volume. For bid_efficiency: savings = bid delta × monthly clicks. Use 0 if genuinely unknown.""" + rsa_note + geo_note + ad_perf_note + ag_perf_note + page_intel_note + feedback_block

        msg = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=2000,
            messages=[{
                "role": "user",
                "content": prompt + "\n\nCAMPAIGN DATA:\n" + json.dumps(context, default=str)[:70000],
            }],
        )
        try:
            from ai_costs import log_claude
            log_claude(
                purpose="ad_optimization",
                model="claude-opus-4-5",
                input_tokens=msg.usage.input_tokens,
                output_tokens=msg.usage.output_tokens,
                campaign_id=campaign,
                optimizer_run_id=optimizer_run_id if optimizer_run_id else "",
            )
        except Exception as _cost_err:
            logger.debug(f"Cost tracking failed (non-fatal): {_cost_err}")
        text = msg.content[0].text if msg.content else "[]"
        m = _re.search(r"\[[\s\S]*\]", text)
        if m:
            arr = json.loads(m.group(0))
            # Build sets of valid resource names from actual keyword_perf data
            valid_kw_resources   = {kw.get("resource_name","")      for kw in keyword_perf if kw.get("resource_name")}
            valid_ag_resources   = {kw.get("ad_group_resource","")  for kw in keyword_perf if kw.get("ad_group_resource")}
            valid_camp_resources = {kw.get("campaign_resource","")  for kw in keyword_perf if kw.get("campaign_resource")}

            validated = []
            for item in arr:
                if not isinstance(item, dict) or not item.get("operation"):
                    continue
                op = item["operation"]
                # Validate resource names Claude returned are real — drop hallucinated ones
                if op in ("pause_keyword", "increase_bid", "decrease_bid"):
                    rn = item.get("resource_name","")
                    if rn and rn not in valid_kw_resources:
                        logger.warning(f"Dropping Claude rec — unknown resource_name '{rn}' for op={op}")
                        continue
                elif op == "add_exact_keyword":
                    ag = item.get("ad_group_resource","")
                    if ag and ag not in valid_ag_resources:
                        logger.warning(f"Dropping Claude rec — unknown ad_group_resource '{ag}' for op={op}")
                        continue
                elif op == "add_negative_keyword":
                    cr = item.get("campaign_resource","")
                    if cr and cr not in valid_camp_resources:
                        logger.warning(f"Dropping Claude rec — unknown campaign_resource '{cr}' for op={op}")
                        # Fall back: use the campaign_resource we derived from keyword_perf
                        if campaign_resource:
                            item["campaign_resource"] = campaign_resource
                            logger.info(f"  Fixed campaign_resource for '{item.get('keyword_text','?')}' → {campaign_resource}")
                        else:
                            continue
                elif op == "ad_copy_suggestion":
                    # Validate character limits — drop silently if over limit
                    headline = item.get("headline", "")
                    description = item.get("description", "")
                    if len(headline) > 30:
                        logger.warning(f"Dropping ad_copy_suggestion — headline too long: '{headline}' ({len(headline)} chars)")
                        continue
                    if len(description) > 90:
                        logger.warning(f"Dropping ad_copy_suggestion — description too long ({len(description)} chars)")
                        continue
                    # ad_resource is required for API execution; warn if missing but keep rec
                    if not item.get("ad_resource"):
                        logger.warning(f"ad_copy_suggestion missing ad_resource — will be acknowledged-only")
                elif op == "geo_exclusion":
                    # Drop if no pre-resolved geo_target_resource
                    if not item.get("geo_target_resource"):
                        logger.warning(f"Dropping geo_exclusion '{item.get('location_name','')}' — no geo_target_resource resolved")
                        continue
                elif op == "enable_keyword":
                    rn = item.get("resource_name","")
                    if rn and rn not in valid_kw_resources:
                        logger.warning(f"Dropping Claude rec — unknown resource_name '{rn}' for op={op}")
                        continue
                elif op == "change_budget":
                    cr = item.get("campaign_resource","")
                    if cr and cr not in valid_camp_resources:
                        if campaign_resource:
                            item["campaign_resource"] = campaign_resource
                        else:
                            logger.warning(f"Dropping change_budget — no valid campaign_resource")
                            continue
                    if not item.get("new_daily_budget_usd"):
                        logger.warning("Dropping change_budget — missing new_daily_budget_usd")
                        continue
                elif op == "replace_ad":
                    # Validate resource names and character limits
                    old_rn = item.get("old_ad_group_ad_resource", "")
                    if not old_rn:
                        logger.warning("Dropping replace_ad — missing old_ad_group_ad_resource")
                        continue
                    # Build valid ad resources from ad_performance + rsa_resources
                    valid_ad_resources = {
                        a.get("ad_group_ad_resource", "") for a in (ad_performance or []) if a.get("ad_group_ad_resource")
                    } | {
                        r.get("ad_group_ad_resource", "") for r in (rsa_resources or []) if r.get("ad_group_ad_resource")
                    }
                    if valid_ad_resources and old_rn not in valid_ad_resources:
                        logger.warning(f"Dropping replace_ad — unknown ad resource '{old_rn}'")
                        continue
                    # Validate and clip headlines/descriptions
                    headlines = [h[:30].strip() for h in (item.get("new_headlines") or []) if (h or "").strip()]
                    descriptions = [d[:90].strip() for d in (item.get("new_descriptions") or []) if (d or "").strip()]
                    if len(headlines) < 3:
                        logger.warning(f"Dropping replace_ad — too few headlines ({len(headlines)})")
                        continue
                    if len(descriptions) < 2:
                        logger.warning(f"Dropping replace_ad — too few descriptions ({len(descriptions)})")
                        continue
                    if not item.get("final_url"):
                        logger.warning("Dropping replace_ad — missing final_url")
                        continue
                    item["new_headlines"] = headlines
                    item["new_descriptions"] = descriptions
                    # Ensure ad_group_resource is populated
                    if not item.get("ad_group_resource"):
                        # Derive from old_ad_group_ad_resource: .../adGroupAds/AGID~ADID → .../adGroups/AGID
                        try:
                            base = old_rn.split("/adGroupAds/")[0]
                            ag_id = old_rn.split("/adGroupAds/")[1].split("~")[0]
                            item["ad_group_resource"] = f"{base}/adGroups/{ag_id}"
                        except Exception:
                            pass
                elif op == "pause_ad_group":
                    ag_rn = item.get("ad_group_resource", "")
                    if not ag_rn:
                        logger.warning("Dropping pause_ad_group — missing ad_group_resource")
                        continue
                    # Validate against actual data from camp_ag_perf — reject hallucinated resources
                    valid_ag_group_resources = {
                        a.get("ad_group_resource", "")
                        for a in (ad_group_performance or [])
                        if a.get("ad_group_resource")
                    }
                    if valid_ag_group_resources and ag_rn not in valid_ag_group_resources:
                        logger.warning(f"Dropping pause_ad_group — unknown ad_group_resource '{ag_rn}'")
                        continue
                    # Safety: ensure ad_group_name is populated
                    if not item.get("ad_group_name"):
                        # Fallback: find name from ad_group_performance data
                        matched = next(
                            (a for a in (ad_group_performance or []) if a.get("ad_group_resource") == ag_rn),
                            None
                        )
                        item["ad_group_name"] = (matched or {}).get("ad_group_name", ag_rn.split("/")[-1])
                validated.append(item)
            logger.info(f"Claude returned {len(arr)} recs, {len(validated)} passed validation")
            return validated
    except Exception as e:
        logger.warning(f"Claude advisory call failed (non-fatal): {e}")


def _call_claude_account_level(
    all_keyword_perf: list,
    all_search_terms: list,
    call_attribution: dict,
    od_production: dict,
    summary: dict,
    campaign_spend: dict,
    google_recs: list | None = None,
    optimizer_run_id: str = "",
    existing_negatives: set | None = None,
    memory_digest: dict | None = None,
) -> list:
    """
    Account-level Claude pass: runs once after all per-campaign passes.
    Focuses on cross-campaign patterns and whole-account recommendations.
    Returns recs with campaign_name="" (shown in Account Level section).

    Account-level rec types:
      - add_negative_keyword with campaign_resource set (competitor names seen across campaigns)
      - change_bid_strategy  (account-wide strategy change)
      - change_budget        (rebalance budget across campaigns)
      - add_asset            (sitelinks / callouts that should apply broadly)
      - claude_advisory      (account-level insight, no API action)
    """
    import os, re as _re
    try:
        from database import get_setting as _get_setting
    except Exception:
        _get_setting = lambda k: None
    _api_key = _get_setting("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    if not _api_key:
        return []

    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=_api_key)

        # Build cross-campaign summary
        camp_resources = {}  # campaign_name -> campaign_resource
        for kw in all_keyword_perf:
            cn = kw.get("campaign", "")
            cr = kw.get("campaign_resource", "")
            if cn and cr and cn not in camp_resources:
                camp_resources[cn] = cr

        # Find competitor terms appearing across multiple campaigns
        from collections import defaultdict
        term_campaigns = defaultdict(set)
        for st in all_search_terms:
            t = st.get("search_term", "").strip().lower()
            c = st.get("campaign", "").strip()
            if t and c:
                term_campaigns[t].add(c)
        cross_camp_terms = {t: list(camps) for t, camps in term_campaigns.items() if len(camps) > 1}

        # Build per-campaign budget/performance summary
        camp_perf = {}
        for cn, cr in camp_resources.items():
            kws = [k for k in all_keyword_perf if k.get("campaign","") == cn]
            spend = sum(k.get("cost", 0) for k in kws)
            clicks = sum(k.get("clicks", 0) for k in kws)
            calls = call_attribution.get(cn.lower(), {}).get("calls", 0)
            booked = call_attribution.get(cn.lower(), {}).get("booked_calls", 0)
            prod = 0.0
            if isinstance(od_production, dict):
                by_camp = od_production.get("by_campaign", {})
                prod = float(by_camp.get(cn, 0))
            camp_perf[cn] = {
                "campaign_resource": cr,
                "spend_30d": round(spend, 2),
                "clicks": clicks,
                "calls": calls,
                "booked_calls": booked,
                "production": prod,
                "daily_budget": campaign_spend.get(cn, {}).get("daily_budget_usd") if isinstance(campaign_spend.get(cn), dict) else None,
            }

        # Fetch call quality flags for account-level signal
        _call_quality_flags: dict = {}
        try:
            from database import get_call_flag_summary
            _call_quality_flags = get_call_flag_summary(days=30)
        except Exception as _cqf_err:
            logger.debug(f"call_flag_summary fetch failed (non-fatal): {_cqf_err}")

        context = {
            "account_summary": summary,
            "campaign_performance": camp_perf,
            "campaign_resources": camp_resources,
            "cross_campaign_search_terms": dict(list(cross_camp_terms.items())[:30]),
            "top_search_terms_by_cost": sorted(all_search_terms, key=lambda s: -s.get("cost", 0))[:40],
            "call_attribution": {
                v["campaign_name"]: {"calls": v["calls"], "booked": v["booked_calls"], "confirmed_appts": v["confirmed_appts"]}
                for v in call_attribution.values()
            },
            "od_production_summary": od_production,
            "google_recommendations": (google_recs or [])[:20],
            "existing_negative_keywords": sorted(existing_negatives)[:200] if existing_negatives else [],
            "optimizer_memory": memory_digest or {},
            "call_quality_flags": _call_quality_flags,
        }

        # Account-level: use aggregate summary, no specific camp_settings
        acct_excellence_block = _build_excellence_block("", summary, {})

        prompt = acct_excellence_block + """
You are a Google Ads specialist performing an ACCOUNT-LEVEL review for a dental practice (Grafton Dental Care, Grafton MA).

You have already reviewed individual campaigns. Now identify issues and opportunities that span the whole account or cannot be attributed to one campaign.

Return up to 6 ACCOUNT-LEVEL recommendations as a JSON array. Each must have:
- "operation": one of: add_negative_keyword | change_bid_strategy | change_budget | add_asset | claude_advisory
- "reason": 1-2 sentences with specific numbers. For cross-campaign negatives, cite which campaigns the term appeared in.
- "estimated_monthly_impact": object with keys:
    "savings_usd": estimated monthly dollar savings (0 if not applicable),
    "impact_type": one of "waste_reduction"|"conversion_lift"|"bid_efficiency"|"coverage_gain",
    "confidence": "high"|"medium"|"low",
    "benchmark_gap": brief string describing which benchmark this closes (e.g. "CTR below 7% target" or "negative keywords missing — est. 20-42% waste")
- "campaign_name": MUST be "" (empty string) — these are account-level recs

Operation-specific fields (same spec as campaign-level):
- add_negative_keyword: "keyword_text", "match_type" ("EXACT"|"PHRASE"|"BROAD"), "campaign_resource" (use the campaign_resource for the campaign where this term appeared most — or the highest-spend campaign if cross-campaign)
- change_bid_strategy: "bid_strategy", "target_cpa_micros" (optional), "target_roas" (optional), "campaign_resource"
- change_budget: "new_daily_budget_usd", "campaign_resource"
- add_asset: "asset_type" ("SITELINK"|"CALLOUT"|"CALL"), "campaign_resource", "description"
- claude_advisory: "insight" (account-level observation, no API action needed)

Focus areas for account-level recs:
1. COMPETITOR NAMES appearing across multiple campaigns → add_negative_keyword (highest-spend campaign's resource)
2. BUDGET REBALANCING — if one campaign has 0 conversions/calls but high spend vs another with conversions → change_budget
3. BID STRATEGY — if a campaign has enough conversion data to switch strategies → change_bid_strategy
4. MISSING ASSETS — sitelinks/callouts that should exist on all campaigns but don't → add_asset
5. CROSS-CAMPAIGN WASTE — identical wasteful terms appearing in multiple campaigns
6. ACCOUNT HEALTH — any account-wide pattern not captured by individual campaign reviews
7. CALL EXPERIENCE: The field "call_quality_flags" in the data shows missed/short Google Ads calls
   flagged for follow-up. If missed_call_rate_pct > 15% OR any campaign has 3+ missed new-patient
   calls in 30d OR short_gads_calls > 5, return a claude_advisory. The advisory should name the
   specific campaigns bleeding qualified leads at the phone, cite exact counts, and suggest whether
   the issue is likely after-hours coverage, IVR routing, or call handling. Do NOT recommend pausing
   those campaigns — the spend is generating calls; the problem is downstream of the click.

IMPORTANT:
- Only flag competitor negatives here if they appear in multiple campaigns (single-campaign terms were already handled per-campaign)
- Use only campaign_resource values from the "campaign_resources" field in the data
- EXISTING NEGATIVES: The field "existing_negative_keywords" in the data lists keywords already live as negatives in Google Ads. Do NOT recommend add_negative_keyword for any term already in that list. Only suggest NEW terms not yet blocked.
- OPTIMIZER MEMORY: The field "optimizer_memory" in the data contains historical run summaries. Use it to: (1) avoid repeating rejected recommendations, (2) identify recurring patterns, (3) highlight new trends. Do not re-suggest anything in "rejected_patterns".
- Return ONLY a valid JSON array, no markdown, no explanation outside the array
- For estimated_monthly_impact.savings_usd: use the keyword/search term cost data to estimate realistically. For waste_reduction ops (negatives, pauses): savings = the monthly spend being wasted. For conversion_lift ops (ad copy, landing page): savings = estimated CPL reduction × monthly lead volume. For bid_efficiency: savings = bid delta × monthly clicks. Use 0 if genuinely unknown."""

        msg = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1500,
            messages=[{
                "role": "user",
                "content": prompt + "\n\nACCOUNT DATA:\n" + json.dumps(context, default=str)[:60000],
            }],
        )
        try:
            from ai_costs import log_claude
            log_claude(
                purpose="ad_optimization",
                model="claude-opus-4-5",
                input_tokens=msg.usage.input_tokens,
                output_tokens=msg.usage.output_tokens,
                optimizer_run_id=optimizer_run_id,
            )
        except Exception as _cost_err:
            logger.debug(f"Cost tracking failed (non-fatal): {_cost_err}")
        text = msg.content[0].text if msg.content else "[]"
        m = _re.search(r"\[[\s\S]*\]", text)
        if m:
            arr = json.loads(m.group(0))
            valid_camp_resources = set(camp_resources.values())
            validated = []
            for item in arr:
                if not isinstance(item, dict) or not item.get("operation"):
                    continue
                # Force campaign_name to empty — these are account-level
                item["campaign_name"] = ""
                op = item["operation"]
                cr = item.get("campaign_resource", "")
                if op in ("add_negative_keyword", "change_bid_strategy", "change_budget", "add_asset"):
                    if cr and cr not in valid_camp_resources:
                        logger.warning(f"Account-level: dropping '{op}' — unknown campaign_resource '{cr}'")
                        continue
                    if not cr and op != "add_asset" and op != "claude_advisory":
                        # Try to pick the highest-spend campaign's resource
                        if camp_resources:
                            top_camp = max(camp_perf, key=lambda c: camp_perf[c]["spend_30d"])
                            item["campaign_resource"] = camp_resources[top_camp]
                            logger.info(f"Account-level: assigned campaign_resource for '{op}' → {camp_resources[top_camp][:30]}")
                if op == "change_budget" and not item.get("new_daily_budget_usd"):
                    logger.warning("Account-level: dropping change_budget — missing new_daily_budget_usd")
                    continue
                validated.append(item)
            logger.info(f"Account-level Claude returned {len(arr)} recs, {len(validated)} passed validation")
            return validated
    except Exception as e:
        logger.warning(f"Account-level Claude call failed (non-fatal): {e}")
    return []


def _refine_claude_action(action_row: dict, feedback: str) -> dict | None:
    """
    Re-run Claude on a single existing action with user feedback.
    Returns a revised action dict, or None on failure.
    """
    import os, re as _re
    try:
        from database import get_setting as _get_setting
    except Exception:
        _get_setting = lambda k: None
    _api_key = _get_setting("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    if not _api_key:
        return None
    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=_api_key)

        after_state = json.loads(action_row.get("after_state_json") or "{}")
        prompt = f"""You are refining a Google Ads recommendation based on user feedback.

Original recommendation:
Operation: {action_row.get('operation')}
Entity: {action_row.get('entity_name')}
Reason: {action_row.get('reason')}
Parameters: {json.dumps(after_state)}

User feedback: {feedback}

Return a SINGLE revised recommendation as a JSON object with the same operation type and all required fields updated per the feedback. Return ONLY the JSON object, no markdown."""

        msg = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text if msg.content else "{}"
        m = _re.search(r"\{[\s\S]*\}", text)
        if m:
            revised = json.loads(m.group(0))
            if isinstance(revised, dict) and revised.get("operation"):
                return revised
    except Exception as e:
        logger.warning(f"Claude refine call failed: {e}")
    return None


# ── Outcome History (AI Learning Loop) ───────────────────────────────────────

def _load_outcome_history(days_back: int = 90) -> dict:
    """
    Load the history of applied actions and their measured outcomes.
    Returns: {(entity_id, operation): {improved, degraded, neutral, last_applied_at}}

    Used by _analyze_keywords to skip or downgrade recommendations for entities
    where previous identical actions degraded performance.
    Guards against noise:
      - Only counts outcomes with pre_clicks_7d >= 5 (minimum sample)
      - 90-day window prevents stale data from blocking valid current recommendations
    """
    from database import _conn
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
    out = {}
    try:
        with _conn() as conn:
            rows = conn.execute("""
                SELECT entity_id, entity_name, operation,
                       SUM(CASE WHEN verdict='improved' THEN 1 ELSE 0 END) AS n_improved,
                       SUM(CASE WHEN verdict='degraded' THEN 1 ELSE 0 END) AS n_degraded,
                       SUM(CASE WHEN verdict='neutral'  THEN 1 ELSE 0 END) AS n_neutral,
                       MAX(applied_at) AS last_applied_at
                  FROM applied_outcomes
                 WHERE applied_at >= ?
                   AND pre_clicks_7d >= 5
                 GROUP BY entity_id, operation
            """, (cutoff,)).fetchall()
        for r in rows:
            out[(r["entity_id"], r["operation"])] = {
                "improved": int(r["n_improved"] or 0),
                "degraded": int(r["n_degraded"] or 0),
                "neutral":  int(r["n_neutral"] or 0),
                "last_applied_at": r["last_applied_at"] or "",
            }
    except Exception as e:
        logger.warning(f"Could not load outcome history (non-fatal): {e}")
    return out


# ── Rule-Based Optimization ──────────────────────────────────────────────────

# Stop words for harvest-exact token-overlap check (Rule 4).
# Single-word match on these alone is too generic for a dental practice
# and would cause false-positive exact-match harvesting.
_HARVEST_STOP_WORDS = frozenset({
    "a", "an", "the", "of", "and", "or", "in", "near", "my", "me", "i",
    "for", "to", "at", "on", "with", "is", "are",
    "dental", "dentist", "dentistry", "tooth", "teeth", "oral",
    "care", "office", "clinic", "practice",
})


def _analyze_keywords(keyword_perf: list, attribution: dict, search_terms: list,
                      call_attribution: dict | None = None,
                      keyword_call_attribution: dict | None = None,
                      campaign: str = "",
                      outcome_history: dict | None = None) -> dict:
    """
    Apply optimization rules. Returns recommended actions.
    campaign: name of the campaign being evaluated — used to scope memory lookups.
              Empty string = global memory only.
    outcome_history: pre-loaded from _load_outcome_history(); if None, loaded here.
    """
    # Load persistent memory — what the optimizer has been taught
    try:
        from database import get_optimizer_memory_dict
        mem = get_optimizer_memory_dict(campaign=campaign)
    except Exception as e:
        logger.warning(f"Could not load optimizer memory: {e}")
        mem = {'term_classifications': {}, 'keyword_overrides': {}, 'campaign_rules': {}, 'general': {}}

    term_classifications = mem.get('term_classifications', {})
    keyword_overrides = mem.get('keyword_overrides', {})
    campaign_rules = mem.get('campaign_rules', {})

    min_spend_before_pause = float(campaign_rules.get('min_spend_before_pause', 40))
    min_clicks_before_pause = int(campaign_rules.get('min_clicks_before_pause', 20))

    # Load outcome history (AI learning loop)
    if outcome_history is None:
        outcome_history = _load_outcome_history(days_back=90)

    logger.info(f"Optimizer memory loaded: {len(term_classifications)} term classifications, "
                f"{len(keyword_overrides)} keyword overrides, {len(campaign_rules)} campaign rules, "
                f"{len(outcome_history)} outcome history entries")

    actions = {
        "pause": [],            # Keywords to pause (high spend, no results)
        "increase_bid": [],     # Keywords to bid up (proven production)
        "decrease_bid": [],     # Keywords to bid down (high cost, low conversion)
        "new_exact": [],        # Search terms to add as exact match keywords
        "new_negatives": [],    # Search terms to add as negatives
        "tighten_match": [],    # Broad keywords to convert to exact match
        "summary": {},
        "memory_applied": [],   # Log of memory overrides that changed the outcome
    }

    call_attribution = call_attribution or {}
    keyword_call_attribution = keyword_call_attribution or {}

    def _calls_for(camp_name: str) -> dict:
        """Return call attribution for a campaign name (case-insensitive)."""
        return call_attribution.get((camp_name or "").lower(), {
            "calls": 0, "booked_calls": 0, "confirmed_appts": 0, "avg_duration_sec": 0,
        })

    def _kw_calls_for(kw_text: str) -> dict:
        """Return keyword-level call attribution (case-insensitive)."""
        return keyword_call_attribution.get((kw_text or "").lower().strip(), {
            "calls": 0, "booked_calls": 0, "confirmed_appts": 0, "avg_duration_sec": 0.0,
        })

    total_spend = sum(k["cost"] for k in keyword_perf)
    total_clicks = sum(k["clicks"] for k in keyword_perf)
    total_leads = sum(a["leads"] for a in attribution.values())
    total_production = sum(a["production"] for a in attribution.values())
    total_calls = sum(c["calls"] for c in call_attribution.values())
    total_booked_calls = sum(c["booked_calls"] for c in call_attribution.values())
    total_confirmed_appts = sum(c["confirmed_appts"] for c in call_attribution.values())

    # Rule 1: Pause keywords with spend > threshold and zero leads/calls
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
            kw_calls = _kw_calls_for(keyword)
            camp_calls = _calls_for(kw.get("campaign", ""))

            # Learning loop guard: if previous pause of this keyword DEGRADED performance, skip
            hist = outcome_history.get((kw["resource_name"], "pause_keyword"))
            if hist and hist["degraded"] >= 1 and hist["improved"] == 0:
                actions["memory_applied"].append(
                    f"SKIP PAUSE '{keyword}': previous pause degraded campaign performance — learning loop override"
                )
                continue

            # Guard 1: this specific keyword drove a call or confirmed appt — protect it
            if kw_calls["confirmed_appts"] > 0 or kw_calls["booked_calls"] > 0:
                actions["memory_applied"].append(
                    f"SKIP PAUSE '{keyword}': drove {kw_calls['calls']} calls / "
                    f"{kw_calls['confirmed_appts']} confirmed appts directly"
                )
                continue
            if kw_calls["calls"] >= 2:
                actions["memory_applied"].append(
                    f"SKIP PAUSE '{keyword}': drove {kw_calls['calls']} inbound calls "
                    f"(keyword-level attribution)"
                )
                continue

            # Guard 2 (legacy fallback): this keyword has no keyword-level call data yet,
            # but the campaign is generating calls or confirmed appts — protect it.
            # Fires when: no keyword-level attribution for this kw AND campaign has any calls
            # (not just confirmed appts) so brand-new campaigns with unattributed calls are safe.
            camp_has_signal = (camp_calls["confirmed_appts"] > 0 or camp_calls["calls"] >= 3)
            if not kw_calls["calls"] and camp_has_signal:
                actions["memory_applied"].append(
                    f"SKIP PAUSE '{keyword}': campaign '{kw.get('campaign','')}' has "
                    f"{camp_calls['calls']} call(s) / {camp_calls['confirmed_appts']} confirmed OD appt(s) — "
                    f"no keyword-level call data yet for this keyword"
                )
                continue

            actions["pause"].append({
                "keyword": keyword,
                "match_type": kw["match_type"],
                "resource_name": kw["resource_name"],
                "reason": (
                    f"${kw['cost']:.2f} spent, {kw['clicks']} clicks, "
                    f"0 form leads, 0 calls attributed"
                ),
                "cost": kw["cost"],
            })

    # Rule 2: Increase bids on keywords with production or strong call conversions
    for kw in keyword_perf:
        keyword = kw["keyword"]
        attr = attribution.get(keyword, {"leads": 0, "booked": 0, "production": 0})
        kw_calls = _kw_calls_for(keyword)
        camp_calls = _calls_for(kw.get("campaign", ""))

        # Learning loop guard: skip bid increase if previous increases degraded performance
        bid_hist = outcome_history.get((kw["resource_name"], "increase_bid"))
        if bid_hist and bid_hist["degraded"] >= 1 and bid_hist["improved"] == 0:
            actions["memory_applied"].append(
                f"SKIP BID UP '{keyword}': previous bid increase degraded performance — learning loop override"
            )
            continue

        if attr["production"] > 0:
            # Gold standard: keyword has OD-attributed production revenue
            roas = attr["production"] / kw["cost"] if kw["cost"] > 0 else float("inf")
            actions["increase_bid"].append({
                "keyword": keyword,
                "match_type": kw["match_type"],
                "resource_name": kw["resource_name"],
                "current_bid_micros": kw.get("current_bid_micros", 0),
                "reason": f"ROAS {roas:.1f}x — ${attr['production']:.0f} production from ${kw['cost']:.2f} spend",
                "roas": roas,
            })
        elif kw_calls["confirmed_appts"] > 0 and kw["cost"] > 0:
            # This specific keyword drove confirmed OD appointments via inbound calls
            cost_per_appt = kw["cost"] / kw_calls["confirmed_appts"]
            if cost_per_appt < 300:
                actions["increase_bid"].append({
                    "keyword": keyword,
                    "match_type": kw["match_type"],
                    "resource_name": kw["resource_name"],
                    "current_bid_micros": kw.get("current_bid_micros", 0),
                    "reason": (
                        f"Keyword drove {kw_calls['confirmed_appts']} confirmed OD appt(s) "
                        f"via {kw_calls['calls']} inbound calls (${cost_per_appt:.0f}/appt)"
                    ),
                    "roas": 0,
                })
        elif kw_calls["booked_calls"] > 0 and kw["cost"] > 0:
            # Keyword drove booked calls (no OD match yet but call outcome = booked)
            cost_per_booking = kw["cost"] / kw_calls["booked_calls"]
            if cost_per_booking < 80:
                actions["increase_bid"].append({
                    "keyword": keyword,
                    "match_type": kw["match_type"],
                    "resource_name": kw["resource_name"],
                    "current_bid_micros": kw.get("current_bid_micros", 0),
                    "reason": (
                        f"Keyword drove {kw_calls['booked_calls']} booked call(s) "
                        f"at ${cost_per_booking:.2f}/booking"
                    ),
                    "roas": 0,
                })
        elif attr["booked"] > 0 and kw["cost"] > 0:
            # Form booking signal (checked after call signals — call data is stronger)
            cost_per_booking = kw["cost"] / attr["booked"]
            if cost_per_booking < 50:
                actions["increase_bid"].append({
                    "keyword": keyword,
                    "match_type": kw["match_type"],
                    "resource_name": kw["resource_name"],
                    "current_bid_micros": kw.get("current_bid_micros", 0),
                    "reason": f"${cost_per_booking:.2f}/booking — {attr['booked']} form bookings",
                    "roas": 0,
                })
        elif not kw_calls["calls"] and camp_calls["confirmed_appts"] > 0 and kw["cost"] > 0:
            # Legacy fallback: this keyword has no call attribution data, use campaign-level signal
            cost_per_appt = kw["cost"] / camp_calls["confirmed_appts"]
            if cost_per_appt < 300:
                actions["increase_bid"].append({
                    "keyword": keyword,
                    "match_type": kw["match_type"],
                    "resource_name": kw["resource_name"],
                    "current_bid_micros": kw.get("current_bid_micros", 0),
                    "reason": (
                        f"Campaign has {camp_calls['confirmed_appts']} confirmed OD appt(s) "
                        f"from inbound calls (${cost_per_appt:.0f}/appt — campaign-level only)"
                    ),
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
                "current_bid_micros": kw.get("current_bid_micros", 0),
                "reason": f"{attr['leads']} leads but 0 bookings from ${kw['cost']:.2f} spend",
            })

    # Rule 6: Tighten match type — broad keywords with call/lead signal but poor CPA
    # Do Add EXACT first, then Pause BROAD (so no impression gap between the two GAds calls).
    for kw in keyword_perf:
        keyword = kw["keyword"]
        match_type = (kw.get("match_type") or "").upper()
        if "BROAD" not in match_type:
            continue  # Only targets broad match keywords

        kw_calls = _kw_calls_for(keyword)
        attr = attribution.get(keyword, {"leads": 0, "booked": 0, "production": 0})
        acquisitions = kw_calls["calls"] + attr["leads"]

        # Skip if keyword has no signal at all (nothing worth keeping in exact)
        if acquisitions == 0:
            continue

        cpa = kw["cost"] / acquisitions if acquisitions else 0
        # Tighten if: has signal (calls or leads) but high CPA from broad waste
        if kw["cost"] > 20 and cpa > 40:
            # Learning loop guard: skip if previous tighten degraded
            tighten_hist = outcome_history.get((kw["resource_name"], "tighten_match_type"))
            if tighten_hist and tighten_hist["degraded"] >= 1 and tighten_hist["improved"] == 0:
                actions["memory_applied"].append(
                    f"SKIP TIGHTEN '{keyword}': previous match-type change degraded performance"
                )
                continue

            actions["tighten_match"].append({
                "keyword": keyword,
                "current_match_type": match_type,
                "proposed_match_type": "EXACT",
                "resource_name": kw["resource_name"],
                "ad_group_resource": kw.get("ad_group_resource", ""),
                "campaign": kw.get("campaign", ""),
                "campaign_resource": kw.get("campaign_resource", ""),
                "cost": kw["cost"],
                "calls": kw_calls["calls"],
                "leads": attr["leads"],
                "reason": (
                    f"Broad match '{keyword}' spent ${kw['cost']:.2f} with {acquisitions} acquisition(s) "
                    f"(${cpa:.0f}/acq). Tighten to exact match to eliminate irrelevant search terms "
                    f"while keeping proven converting queries."
                ),
            })

    # ── Negative keyword signals ──────────────────────────────────────
    # _HARD_NEGATIVES, _SOFT_NEGATIVES, _COMPETITOR_NAMES, _OUR_NAMES,
    # _is_competitor_term, _is_negative_intent are all defined at module level above.
    # EVERYTHING ELSE gets tracked and judged by real pipeline data:
    # cheap, low cost, affordable, discount, free — price-sensitive buyers
    # cost, price, how much, payment plan, financing — research/buying intent
    # review — evaluating the practice
    # clinical trial, medicaid, medicare — let data prove they don't convert
    # can't afford — might convert with financing options

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
                "campaign_resource": st.get("campaign_resource", ""),
                "campaign": st.get("campaign", ""),
                "ad_group_resource": st.get("ad_group_resource", ""),
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
                "campaign_resource": st.get("campaign_resource", ""),
                "campaign": st.get("campaign", ""),
                "ad_group_resource": st.get("ad_group_resource", ""),
                "reason": neg_reason,
            })
        elif st["conversions"] > 0 and term not in existing_keywords:
            # Count as real acquisition if: form lead attribution OR keyword-level call attribution
            term_has_real_leads = any(
                term in a_kw.lower() or a_kw.lower() in term
                for a_kw in attribution.keys()
            ) if attribution else False

            # Word-boundary check: split both into tokens and look for *significant* overlap.
            # Filter stop words so generic dental terms don't trigger false-positive harvesting.
            term_tokens = set(term.split()) - _HARVEST_STOP_WORDS
            term_has_call_attr = bool(term_tokens) and any(
                term_tokens & (set(kw_lower.split()) - _HARVEST_STOP_WORDS)
                for kw_lower in keyword_call_attribution.keys()
            ) if keyword_call_attribution else False

            if term_has_real_leads or term_has_call_attr:
                signal = "form lead + call attribution" if (term_has_real_leads and term_has_call_attr) \
                    else ("call attribution" if term_has_call_attr else "form lead attribution")
                actions["new_exact"].append({
                    "search_term": st["search_term"],
                    "clicks": st["clicks"],
                    "conversions": st["conversions"],
                    "cost": st["cost"],
                    "ad_group_resource": st.get("ad_group_resource", ""),
                    "ad_group": st.get("ad_group", ""),
                    "campaign_resource": st.get("campaign_resource", ""),
                    "reason": f"Has real {signal} + Google conversion",
                })
            else:
                # Conversion in Google but no signal in our system — flag for review
                actions["new_exact"].append({
                    "search_term": st["search_term"],
                    "clicks": st["clicks"],
                    "conversions": st["conversions"],
                    "cost": st["cost"],
                    "ad_group_resource": st.get("ad_group_resource", ""),
                    "ad_group": st.get("ad_group", ""),
                    "campaign_resource": st.get("campaign_resource", ""),
                    "reason": "Google conversion but NO lead/call in pipeline — verify before adding",
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
                "campaign_resource": st.get("campaign_resource", ""),
                "campaign": st.get("campaign", ""),
                "ad_group_resource": st.get("ad_group_resource", ""),
                "reason": "High impressions, zero clicks — likely irrelevant",
            })
        elif st.get("clicks", 0) > 5 and st["cost"] > 15 and st["conversions"] == 0:
            term_lower = st["search_term"].lower()
            # Guard A: term matches a form-attributed keyword
            has_leads = any(
                term_lower in a_kw.lower()
                for a_kw in attribution.keys()
            )
            # Guard B: term has significant token overlap with a call-attributed keyword.
            # This prevents negativing search terms that drove inbound calls even when
            # Google's conversion column shows 0 (calls tracked via Mango, not GAds).
            has_call_signal = False
            if keyword_call_attribution and not has_leads:
                sig_term = set(term_lower.split()) - _HARVEST_STOP_WORDS
                if sig_term:
                    for kw_lower_key, kw_data in keyword_call_attribution.items():
                        sig_kw = set(kw_lower_key.split()) - _HARVEST_STOP_WORDS
                        if sig_term & sig_kw and kw_data.get("calls", 0) > 0:
                            has_call_signal = True
                            break
            # Guard C: if we have ZERO keyword-level call data (gads_clicks sync hasn't
            # run yet), we can't distinguish converting vs junk terms — hold off on all
            # negatives for campaigns that are generating calls.
            # Once keyword attribution is populated, Guard B handles per-term protection.
            no_kw_data = not keyword_call_attribution
            camp_lower = st.get("campaign", "").lower()
            camp_has_calls = False
            if no_kw_data and call_attribution:
                camp_calls = call_attribution.get(camp_lower, {})
                camp_has_calls = camp_calls.get("calls", 0) >= 3

            if not has_leads and not has_call_signal and not camp_has_calls:
                actions["new_negatives"].append({
                    "search_term": st["search_term"],
                    "clicks": st.get("clicks", 0),
                    "cost": st["cost"],
                    "campaign_resource": st.get("campaign_resource", ""),
                    "campaign": st.get("campaign", ""),
                    "ad_group_resource": st.get("ad_group_resource", ""),
                    "reason": f"${st['cost']:.2f} spent, {st.get('clicks',0)} clicks, 0 conversions/leads/calls",
                })

    # Summary
    combined_acq = total_leads + total_booked_calls
    actions["summary"] = {
        "total_spend": round(total_spend, 2),
        "total_clicks": total_clicks,
        "total_leads": total_leads,
        "total_production": round(total_production, 2),
        "overall_roas": round(total_production / total_spend, 1) if total_spend > 0 else 0,
        "cost_per_lead": round(total_spend / total_leads, 2) if total_leads > 0 else 0,
        # Call enrichment
        "total_calls": total_calls,
        "total_booked_calls": total_booked_calls,
        "total_confirmed_appts": total_confirmed_appts,
        "cost_per_acquisition": round(total_spend / combined_acq, 2) if combined_acq > 0 else 0,
        # Actions
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
            field_mask_pb2.FieldMask(paths=["status"])
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
        field_mask_pb2.FieldMask(paths=["status"])
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


# Bid guardrails — hard limits enforced before any bid write
_MIN_BID_MICROS = 10_000       # $0.01 — Google rejects sub-cent bids
_MAX_BID_MICROS = 50_000_000   # $50.00 — hard ceiling for GDC ad spend


def _execute_bid_change(client, customer_id: str, resource_name: str,
                         new_bid_micros: int) -> bool:
    """
    Update the manual CPC bid (cpc_bid_micros) on a single keyword.
    Uses the same FieldMask pattern as _execute_single_pause.
    Does NOT check kill switch — caller must check first.
    Raises ValueError if bid is outside guardrail limits.
    Returns True on success.
    """
    if new_bid_micros < _MIN_BID_MICROS:
        raise ValueError(
            f"Bid {new_bid_micros} micros (${new_bid_micros/1_000_000:.4f}) "
            f"is below minimum ${_MIN_BID_MICROS/1_000_000:.2f}"
        )
    if new_bid_micros > _MAX_BID_MICROS:
        raise ValueError(
            f"Bid {new_bid_micros} micros (${new_bid_micros/1_000_000:.2f}) "
            f"exceeds maximum ${_MAX_BID_MICROS/1_000_000:.2f}"
        )

    service = client.get_service("AdGroupCriterionService")
    operation = client.get_type("AdGroupCriterionOperation")
    criterion = operation.update
    criterion.resource_name = resource_name
    criterion.cpc_bid_micros = new_bid_micros
    client.copy_from(
        operation.update_mask,
        field_mask_pb2.FieldMask(paths=["cpc_bid_micros"])
    )
    try:
        service.mutate_ad_group_criteria(
            customer_id=customer_id,
            operations=[operation],
        )
        return True
    except Exception as e:
        logger.error(f"Bid change failed for {resource_name} → {new_bid_micros}: {e}")
        raise


def _execute_add_keyword(client, customer_id: str, ad_group_resource: str,
                          keyword_text: str, match_type: str = "EXACT") -> bool:
    """
    Add a new keyword to an ad group.
    Does NOT check kill switch — caller must check first.
    Handles ALREADY_EXISTS gracefully (returns True, caller marks as duplicate).
    Always sends HEALTH_IN_PERSONALIZED_ADS exemption key — Google applies this
    policy to all dental/health advertisers and it is always exemptible. Sending
    it upfront avoids the rejection entirely.
    Returns True on success or duplicate.
    """
    match_type = (match_type or "EXACT").upper()
    if match_type not in ("EXACT", "PHRASE", "BROAD"):
        raise ValueError(f"Invalid match_type '{match_type}' — must be EXACT, PHRASE, or BROAD")
    service = client.get_service("AdGroupCriterionService")
    operation = client.get_type("AdGroupCriterionOperation")
    criterion = operation.create
    criterion.ad_group = ad_group_resource
    criterion.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
    criterion.keyword.text = keyword_text
    criterion.keyword.match_type = client.enums.KeywordMatchTypeEnum[match_type]
    # Always exempt HEALTH_IN_PERSONALIZED_ADS — applies to all dental keywords,
    # always exemptible, no downside to sending proactively.
    exempt_key = client.get_type("PolicyViolationKey")
    exempt_key.policy_name = "HEALTH_IN_PERSONALIZED_ADS"
    exempt_key.violating_text = keyword_text
    operation.exempt_policy_violation_keys.append(exempt_key)
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
            logger.info(f"Keyword '{keyword_text}' already exists in {ad_group_resource} — treating as success")
            return True
        logger.error(f"Add keyword failed '{keyword_text}' → {ad_group_resource}: {e}")
        raise


def _execute_add_negative(client, customer_id: str, campaign_resource: str,
                           keyword_text: str, match_type: str = "BROAD") -> bool:
    """
    Add a campaign-level negative keyword.
    Does NOT check kill switch — caller must check first.
    Handles ALREADY_EXISTS gracefully (returns True).
    Returns True on success or duplicate.
    """
    match_type = (match_type or "BROAD").upper()
    if match_type not in ("EXACT", "PHRASE", "BROAD"):
        raise ValueError(f"Invalid match_type '{match_type}' — must be EXACT, PHRASE, or BROAD")
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
        logger.info(f"Added negative '{keyword_text}' [{match_type}] to campaign {campaign_resource}")
        return True
    except Exception as e:
        err_str = str(e)
        if "KEYWORD_ALREADY_EXISTS" in err_str or "already exists" in err_str.lower():
            logger.info(f"Negative '{keyword_text}' already in {campaign_resource} — treating as success")
            return True
        logger.error(f"Add negative failed '{keyword_text}' → {campaign_resource}: {e}")
        raise


# ── Shared Negative Keyword List ─────────────────────────────────────────────

_SHARED_LIST_NAME = "GDC Competitor Negatives"


def _get_or_create_shared_negative_list(client, customer_id: str) -> str:
    """
    Return the resource_name of the shared negative keyword list named
    _SHARED_LIST_NAME.  Creates it if it doesn't exist yet.
    """
    ga_service = client.get_service("GoogleAdsService")
    query = f"""
        SELECT shared_set.resource_name, shared_set.name, shared_set.type
        FROM shared_set
        WHERE shared_set.type = 'NEGATIVE_KEYWORDS'
          AND shared_set.status = 'ENABLED'
    """
    try:
        response = ga_service.search(customer_id=customer_id, query=query)
        for row in response:
            if row.shared_set.name == _SHARED_LIST_NAME:
                logger.info(f"Found shared negative list: {row.shared_set.resource_name}")
                return row.shared_set.resource_name
    except Exception as e:
        logger.warning(f"Could not query shared sets: {e}")

    # Create new shared set
    shared_set_service = client.get_service("SharedSetService")
    operation = client.get_type("SharedSetOperation")
    shared_set = operation.create
    shared_set.name = _SHARED_LIST_NAME
    shared_set.type_ = client.enums.SharedSetTypeEnum.NEGATIVE_KEYWORDS
    try:
        response = shared_set_service.mutate_shared_sets(
            customer_id=customer_id, operations=[operation]
        )
        rn = response.results[0].resource_name
        logger.info(f"Created shared negative list '{_SHARED_LIST_NAME}': {rn}")
        return rn
    except Exception as e:
        logger.error(f"Failed to create shared negative list: {e}")
        raise


def _execute_add_to_shared_negative_list(client, customer_id: str,
                                          keyword_text: str,
                                          match_type: str = "BROAD") -> bool:
    """
    Add a keyword to the 'GDC Competitor Negatives' shared negative list
    and ensure every ENABLED campaign is linked to that list.

    This is the right approach for competitor brand terms — they are blocked
    account-wide and automatically apply to any new campaigns added later.

    Does NOT check kill switch — caller must check first.
    Returns True on success.
    """
    match_type = (match_type or "BROAD").upper()
    if match_type not in ("EXACT", "PHRASE", "BROAD"):
        match_type = "BROAD"

    # 1. Get or create the shared list
    shared_set_rn = _get_or_create_shared_negative_list(client, customer_id)

    # 2. Add keyword to the shared list
    shared_criterion_service = client.get_service("SharedCriterionService")
    op = client.get_type("SharedCriterionOperation")
    criterion = op.create
    criterion.shared_set = shared_set_rn
    criterion.keyword.text = keyword_text
    criterion.keyword.match_type = client.enums.KeywordMatchTypeEnum[match_type]
    try:
        shared_criterion_service.mutate_shared_criteria(
            customer_id=customer_id, operations=[op]
        )
        logger.info(f"Added '{keyword_text}' [{match_type}] to shared list '{_SHARED_LIST_NAME}'")
    except Exception as e:
        err_str = str(e)
        if "KEYWORD_ALREADY_EXISTS" in err_str or "already exists" in err_str.lower():
            logger.info(f"'{keyword_text}' already in shared list — continuing")
        else:
            logger.error(f"Failed to add '{keyword_text}' to shared list: {e}")
            raise

    # 3. Link shared list to all ENABLED campaigns (idempotent)
    ga_service = client.get_service("GoogleAdsService")
    camp_query = """
        SELECT campaign.resource_name
        FROM campaign
        WHERE campaign.status = 'ENABLED'
          AND campaign.advertising_channel_type = 'SEARCH'
    """
    try:
        camp_response = ga_service.search(customer_id=customer_id, query=camp_query)
        campaign_resources = [row.campaign.resource_name for row in camp_response]
    except Exception as e:
        logger.warning(f"Could not fetch campaigns for shared list linking: {e}")
        campaign_resources = []

    if campaign_resources:
        # Check which campaigns are already linked
        link_query = f"""
            SELECT campaign_shared_set.campaign, campaign_shared_set.shared_set
            FROM campaign_shared_set
            WHERE campaign_shared_set.shared_set = '{shared_set_rn}'
        """
        already_linked: set = set()
        try:
            link_response = ga_service.search(customer_id=customer_id, query=link_query)
            for row in link_response:
                already_linked.add(row.campaign_shared_set.campaign)
        except Exception as e:
            logger.warning(f"Could not check existing campaign links: {e}")

        campaign_shared_set_service = client.get_service("CampaignSharedSetService")
        link_ops = []
        for camp_rn in campaign_resources:
            if camp_rn not in already_linked:
                link_op = client.get_type("CampaignSharedSetOperation")
                css = link_op.create
                css.campaign = camp_rn
                css.shared_set = shared_set_rn
                link_ops.append(link_op)

        if link_ops:
            try:
                campaign_shared_set_service.mutate_campaign_shared_sets(
                    customer_id=customer_id, operations=link_ops
                )
                logger.info(f"Linked shared list to {len(link_ops)} campaign(s)")
            except Exception as e:
                err_str = str(e)
                if "already exists" in err_str.lower():
                    logger.info("Shared list already linked to campaigns")
                else:
                    logger.warning(f"Could not link shared list to some campaigns: {e}")
        else:
            logger.info("Shared list already linked to all campaigns")

    return True


# ── New Execute Functions (Opus Plan — May 2026) ──────────────────────────────

def _execute_enable_keyword(client, customer_id: str, resource_name: str) -> bool:
    """
    Enable a paused keyword by resource_name.
    Does NOT check kill switch — caller must check first.
    Returns True on success.
    """
    service = client.get_service("AdGroupCriterionService")
    operation = client.get_type("AdGroupCriterionOperation")
    criterion = operation.update
    criterion.resource_name = resource_name
    criterion.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
    client.copy_from(
        operation.update_mask,
        field_mask_pb2.FieldMask(paths=["status"])
    )
    try:
        service.mutate_ad_group_criteria(
            customer_id=customer_id,
            operations=[operation],
        )
        logger.info(f"Enabled keyword: {resource_name}")
        return True
    except Exception as e:
        logger.error(f"Enable keyword failed for {resource_name}: {e}")
        raise


def _get_campaign_budget_resource(client, customer_id: str, campaign_resource: str) -> tuple:
    """
    Return (budget_resource_name, current_amount_micros) for a campaign.
    Raises ValueError if campaign not found.
    """
    service = client.get_service("GoogleAdsService")
    query = f"""
        SELECT campaign.campaign_budget, campaign_budget.amount_micros
        FROM campaign
        WHERE campaign.resource_name = '{campaign_resource}'
        LIMIT 1
    """
    try:
        response = service.search(customer_id=customer_id, query=query)
        for row in response:
            return row.campaign.campaign_budget, row.campaign_budget.amount_micros
    except Exception as e:
        logger.error(f"Failed to get budget for {campaign_resource}: {e}")
        raise
    raise ValueError(f"Campaign not found: {campaign_resource}")


def _execute_budget_change(client, customer_id: str, campaign_resource: str,
                            new_daily_budget_usd: float) -> bool:
    """
    Update the daily budget for a campaign.
    Performs safety checks (absolute limits + 25% increase guard) before writing.
    Does NOT check kill switch — caller must check first.
    Returns True on success.
    """
    from campaign_safety import check_budget_absolute_limits, check_budget_change_safe, WriteBlockedError

    new_micros = int(new_daily_budget_usd * 1_000_000)

    # Absolute limits first
    check_budget_absolute_limits(new_micros)

    # Get current budget
    budget_resource, current_micros = _get_campaign_budget_resource(client, customer_id, campaign_resource)

    # 25% increase guard
    if not check_budget_change_safe(current_micros, new_micros):
        raise WriteBlockedError(
            f"Budget increase from ${current_micros/1_000_000:.2f} to ${new_daily_budget_usd:.2f} "
            f"exceeds 25% limit. Increase manually if needed."
        )

    service = client.get_service("CampaignBudgetService")
    operation = client.get_type("CampaignBudgetOperation")
    budget = operation.update
    budget.resource_name = budget_resource
    budget.amount_micros = new_micros
    client.copy_from(
        operation.update_mask,
        field_mask_pb2.FieldMask(paths=["amount_micros"])
    )
    try:
        service.mutate_campaign_budgets(
            customer_id=customer_id,
            operations=[operation],
        )
        logger.info(f"Budget updated for {campaign_resource}: "
                    f"${current_micros/1_000_000:.2f} → ${new_daily_budget_usd:.2f}/day")
        return True
    except Exception as e:
        logger.error(f"Budget change failed for {campaign_resource}: {e}")
        raise


def _execute_change_bid_strategy(client, customer_id: str, campaign_resource: str,
                                   bid_strategy: str, target_cpa_micros: int = 0,
                                   target_roas: float = 0.0) -> bool:
    """
    Change a campaign's bid strategy.
    Supports: MAXIMIZE_CONVERSIONS, TARGET_CPA, TARGET_ROAS, MAXIMIZE_CLICKS
    """
    campaign_service = client.get_service("CampaignService")
    operation = client.get_type("CampaignOperation")
    campaign = operation.update
    campaign.resource_name = campaign_resource

    strategy = bid_strategy.upper()
    from google.protobuf import field_mask_pb2

    # Input validation — Google Ads rejects zero target values
    if strategy == "TARGET_CPA" and int(target_cpa_micros) <= 0:
        raise ValueError("TARGET_CPA requires target_cpa_micros > 0")
    if strategy == "TARGET_ROAS" and float(target_roas) <= 0.0:
        raise ValueError("TARGET_ROAS requires target_roas > 0")

    # Google Ads API v17+ — all bidding strategies are set via oneof sub-message
    # fields on Campaign, NOT via bidding_strategy_type enum.
    #
    # MAXIMIZE_CLICKS  → campaign.target_spend   (TargetSpend sub-message)
    # MAXIMIZE_CONVERSIONS → campaign.maximize_conversions (MaximizeConversions sub-message)
    # TARGET_CPA       → campaign.target_cpa.target_cpa_micros
    # TARGET_ROAS      → campaign.target_roas.target_roas
    #
    # Setting bidding_strategy_type directly causes "Unknown field for Campaign" errors.
    if strategy == "MAXIMIZE_CONVERSIONS":
        # Use leaf-level field mask path — Google rejects parent-only paths for sub-messages
        # target_cpa_micros = 0 means no CPA target (pure maximize conversions)
        campaign.maximize_conversions.target_cpa_micros = 0
        paths = ["maximize_conversions.target_cpa_micros"]
    elif strategy == "MAXIMIZE_CLICKS":
        # target_spend is the Google Ads API name for Maximize Clicks
        # cpc_bid_ceiling_micros = 0 means no bid cap
        campaign.target_spend.cpc_bid_ceiling_micros = 0
        paths = ["target_spend.cpc_bid_ceiling_micros"]
    elif strategy == "TARGET_CPA":
        campaign.target_cpa.target_cpa_micros = int(target_cpa_micros)
        paths = ["target_cpa.target_cpa_micros"]
    elif strategy == "TARGET_ROAS":
        campaign.target_roas.target_roas = float(target_roas)
        paths = ["target_roas.target_roas"]
    else:
        raise ValueError(f"Unknown bid strategy: {bid_strategy}")

    # Use CopyFrom (not client.copy_from) — stable across SDK v24+
    operation.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=paths))

    try:
        response = campaign_service.mutate_campaigns(
            customer_id=customer_id,
            operations=[operation],
        )
        logger.info(f"Changed bid strategy to {bid_strategy} for {campaign_resource}")
        return True
    except Exception as e:
        logger.error(f"Failed to change bid strategy: {e}")
        raise


def _execute_change_match_type(client, customer_id: str, resource_name: str,
                                new_match_type: str,
                                keyword_text: str = "") -> bool:
    """
    Change a keyword's match type by removing the old keyword and adding a new one.

    Google Ads does not allow updating keyword.match_type via UPDATE — it is an
    IMMUTABLE_FIELD. The correct approach is REMOVE + CREATE.

    resource_name format: customers/CID/adGroupCriteria/AGID~CRITERIONID
    The ad group resource is derived from the resource_name.
    """
    service = client.get_service("AdGroupCriterionService")

    # Derive the ad group resource from the criterion resource name
    # customers/CID/adGroupCriteria/AGID~CRITERIONID → customers/CID/adGroups/AGID
    try:
        parts = resource_name.split("/adGroupCriteria/")
        if len(parts) != 2:
            raise ValueError(f"Cannot parse resource_name: {resource_name}")
        cid_part = parts[0]  # customers/CID
        ag_crit = parts[1]   # AGID~CRITERIONID
        ad_group_id = ag_crit.split("~")[0]
        ad_group_resource = f"{cid_part}/adGroups/{ad_group_id}"
    except Exception as e:
        raise ValueError(f"Could not derive ad group from '{resource_name}': {e}")

    # If keyword_text not provided, fetch it from Google Ads
    if not keyword_text:
        ga_service = client.get_service("GoogleAdsService")
        q = f"""
            SELECT ad_group_criterion.keyword.text
            FROM ad_group_criterion
            WHERE ad_group_criterion.resource_name = '{resource_name}'
            LIMIT 1
        """
        try:
            resp = ga_service.search(customer_id=customer_id, query=q)
            for row in resp:
                keyword_text = row.ad_group_criterion.keyword.text
                break
        except Exception as e:
            raise ValueError(f"Could not fetch keyword text for '{resource_name}': {e}")
        if not keyword_text:
            raise ValueError(f"Keyword text not found for resource: {resource_name}")

    match_type_enum = client.enums.KeywordMatchTypeEnum[new_match_type.upper()]

    # Step 1: REMOVE the existing keyword
    remove_op = client.get_type("AdGroupCriterionOperation")
    remove_op.remove = resource_name

    # Step 2: CREATE new keyword with the new match type
    create_op = client.get_type("AdGroupCriterionOperation")
    new_criterion = create_op.create
    new_criterion.ad_group = ad_group_resource
    new_criterion.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
    new_criterion.keyword.text = keyword_text
    new_criterion.keyword.match_type = match_type_enum

    try:
        service.mutate_ad_group_criteria(
            customer_id=customer_id,
            operations=[remove_op, create_op],
        )
        logger.info(f"Changed match type to {new_match_type} for '{keyword_text}' "
                    f"(removed {resource_name}, created new {new_match_type} keyword)")
        return True
    except Exception as e:
        logger.error(f"Failed to change match type for '{keyword_text}': {e}")
        raise


def _get_rsa_current_assets(client, customer_id: str, ad_group_ad_resource: str) -> dict:
    """
    Fetch the current headlines and descriptions for a Responsive Search Ad.
    Returns: {
        "headlines": [{"text": str, "pinned_field": str|None}],
        "descriptions": [{"text": str, "pinned_field": str|None}],
        "ad_group_ad_resource": str
    }
    Returns empty dict if not found.
    """
    service = client.get_service("GoogleAdsService")
    query = f"""
        SELECT
            ad_group_ad.resource_name,
            ad_group_ad.ad.responsive_search_ad.headlines,
            ad_group_ad.ad.responsive_search_ad.descriptions
        FROM ad_group_ad
        WHERE ad_group_ad.resource_name = '{ad_group_ad_resource}'
        LIMIT 1
    """
    try:
        response = service.search(customer_id=customer_id, query=query)
        for row in response:
            rsa = row.ad_group_ad.ad.responsive_search_ad
            headlines = []
            for h in rsa.headlines:
                headlines.append({
                    "text": h.text,
                    "pinned_field": str(h.pinned_field) if h.pinned_field else None,
                })
            descriptions = []
            for d in rsa.descriptions:
                descriptions.append({
                    "text": d.text,
                    "pinned_field": str(d.pinned_field) if d.pinned_field else None,
                })
            return {
                "headlines": headlines,
                "descriptions": descriptions,
                "ad_group_ad_resource": ad_group_ad_resource,
            }
    except Exception as e:
        logger.error(f"Failed to get RSA assets for {ad_group_ad_resource}: {e}")
    return {}


def _execute_update_rsa(client, customer_id: str, ad_group_ad_resource: str,
                         new_headlines: list, new_descriptions: list) -> bool:
    """
    Update a Responsive Search Ad by reading current assets, merging new ones,
    and writing back the full set.

    RSA constraint: max 15 headlines, max 4 descriptions.
    Character limits: headline ≤ 30 chars, description ≤ 90 chars.

    new_headlines: list of str (text only, no pinning — appended after existing unique ones)
    new_descriptions: list of str

    Does NOT check kill switch — caller must check first.

    NOTE: Google Ads API treats RSA headlines/descriptions as IMMUTABLE on update —
    they cannot be modified in-place via AdService.mutate_ads(). If the API rejects
    the mutation with IMMUTABLE_FIELD, the function returns False and the caller
    should treat the suggestion as advisory (manual action needed in Google Ads UI).
    A True return means the API accepted the update.
    """
    # Validate character limits
    for h in new_headlines:
        if len(h) > 30:
            raise ValueError(f"Headline '{h}' exceeds 30-char limit ({len(h)} chars)")
    for d in new_descriptions:
        if len(d) > 90:
            raise ValueError(f"Description '{d}' exceeds 90-char limit ({len(d)} chars)")

    # Fetch current assets
    current = _get_rsa_current_assets(client, customer_id, ad_group_ad_resource)
    if not current:
        raise ValueError(f"RSA not found: {ad_group_ad_resource}")

    existing_headline_texts = {h["text"].lower() for h in current.get("headlines", [])}
    existing_desc_texts = {d["text"].lower() for d in current.get("descriptions", [])}

    # Merge: keep existing assets, append unique new ones up to max
    merged_headlines = list(current.get("headlines", []))
    for text in new_headlines:
        if text.lower() not in existing_headline_texts and len(merged_headlines) < 15:
            merged_headlines.append({"text": text, "pinned_field": None})

    merged_descriptions = list(current.get("descriptions", []))
    for text in new_descriptions:
        if text.lower() not in existing_desc_texts and len(merged_descriptions) < 4:
            merged_descriptions.append({"text": text, "pinned_field": None})

    # Build the update operation via AdService (not AdGroupAdService).
    # RSA headlines/descriptions are immutable on AdGroupAd — they must be
    # updated at the Ad level using AdService.mutate_ads().
    #
    # ad_group_ad_resource format: customers/CID/adGroupAds/AGID~ADID
    # ad_resource format:          customers/CID/ads/ADID
    # Extract the ad resource name from the ad_group_ad resource.
    try:
        # "customers/CID/adGroupAds/AGID~ADID" → ad_id is after the ~
        parts = ad_group_ad_resource.split("~")
        if len(parts) != 2:
            raise ValueError(f"Cannot parse ad_group_ad_resource: {ad_group_ad_resource}")
        cid_part = ad_group_ad_resource.split("/adGroupAds/")[0]  # customers/CID
        ad_id = parts[1]
        ad_resource = f"{cid_part}/ads/{ad_id}"
    except Exception as e:
        raise ValueError(f"Could not derive ad resource from '{ad_group_ad_resource}': {e}")

    service = client.get_service("AdService")
    operation = client.get_type("AdOperation")
    ad = operation.update
    ad.resource_name = ad_resource

    rsa = ad.responsive_search_ad
    rsa.headlines.clear()
    for h in merged_headlines:
        asset = client.get_type("AdTextAsset")
        asset.text = h["text"]
        rsa.headlines.append(asset)

    rsa.descriptions.clear()
    for d in merged_descriptions:
        asset = client.get_type("AdTextAsset")
        asset.text = d["text"]
        rsa.descriptions.append(asset)

    client.copy_from(
        operation.update_mask,
        field_mask_pb2.FieldMask(
            paths=["responsive_search_ad.headlines",
                   "responsive_search_ad.descriptions"]
        )
    )

    try:
        response = service.mutate_ads(
            customer_id=customer_id,
            operations=[operation],
        )
        logger.info(f"RSA updated via AdService: {ad_resource} — "
                    f"{len(merged_headlines)} headlines, {len(merged_descriptions)} descriptions")
        return True
    except Exception as e:
        # Google Ads API rejects RSA asset mutations with IMMUTABLE_FIELD.
        # Treat as advisory rather than a hard failure so the approval flow
        # can continue. Caller should mark execution_result='advisory_applied'.
        err_str = str(e)
        if "IMMUTABLE_FIELD" in err_str or "cannot be modified" in err_str:
            logger.warning(
                f"RSA update rejected as IMMUTABLE_FIELD for {ad_resource} — "
                f"treating as advisory (manual action needed in Google Ads UI). "
                f"Suggested: {len(new_headlines)} new headlines, "
                f"{len(new_descriptions)} new descriptions."
            )
            return False
        logger.error(f"RSA update failed for {ad_resource}: {e}")
        raise


def _execute_replace_ad(client, customer_id: str,
                         old_ad_group_ad_resource: str,
                         new_headlines: list,
                         new_descriptions: list,
                         final_url: str,
                         ad_group_resource: str = "",
                         path1: str = "",
                         path2: str = "") -> dict:
    """
    Atomically PAUSE an existing RSA and CREATE a replacement RSA in the same
    ad group, using one mutate_ad_group_ads() call (all-or-nothing).

    RSA headlines/descriptions are IMMUTABLE on update — must PAUSE + CREATE new.

    Args:
        old_ad_group_ad_resource: customers/CID/adGroupAds/AGID~ADID (ad to pause)
        new_headlines:    3-15 strings, each <= 30 chars
        new_descriptions: 2-4 strings, each <= 90 chars
        final_url:        landing page URL for the new RSA (required)
        ad_group_resource: customers/CID/adGroups/AGID — derived from old resource if blank
        path1, path2:     optional display-URL path segments, each <= 15 chars

    Returns: {"paused_resource": str, "created_resource": str}

    Does NOT check kill switch — caller must check first.
    Raises ValueError on validation failures; raises GoogleAdsException on API failure.
    """
    # --- 1. Validate inputs --------------------------------------------------
    h_list = [h.strip() for h in (new_headlines or []) if (h or "").strip()]
    d_list = [d.strip() for d in (new_descriptions or []) if (d or "").strip()]
    if not (3 <= len(h_list) <= 15):
        raise ValueError(f"RSA needs 3-15 headlines (got {len(h_list)})")
    if not (2 <= len(d_list) <= 4):
        raise ValueError(f"RSA needs 2-4 descriptions (got {len(d_list)})")
    for h in h_list:
        if len(h) > 30:
            raise ValueError(f"Headline exceeds 30 chars ({len(h)}): '{h}'")
    for d in d_list:
        if len(d) > 90:
            raise ValueError(f"Description exceeds 90 chars ({len(d)}): '{d}'")
    if not final_url or not final_url.lower().startswith(("http://", "https://")):
        raise ValueError(f"final_url must be an http(s) URL (got '{final_url}')")
    if path1 and len(path1) > 15:
        raise ValueError(f"path1 exceeds 15 chars: '{path1}'")
    if path2 and len(path2) > 15:
        raise ValueError(f"path2 exceeds 15 chars: '{path2}'")
    # Google policy: phone numbers in ad text are PROHIBITED (PHONE_NUMBER_IN_AD_TEXT)
    import re as _re
    _phone_re = _re.compile(r'(\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}|\d{10}|1[\s.\-]?\d{3}[\s.\-]?\d{3}[\s.\-]?\d{4})')
    for _h in h_list:
        if _phone_re.search(_h):
            raise ValueError(f"Headline contains a phone number (Google policy violation): '{_h}'")
    for _d in d_list:
        if _phone_re.search(_d):
            raise ValueError(f"Description contains a phone number (Google policy violation): '{_d}'")

    # --- 2. Derive ad_group_resource if not supplied -------------------------
    if not ad_group_resource:
        try:
            cid_part = old_ad_group_ad_resource.split("/adGroupAds/")[0]
            ag_id    = old_ad_group_ad_resource.split("/adGroupAds/")[1].split("~")[0]
            ad_group_resource = f"{cid_part}/adGroups/{ag_id}"
        except Exception as e:
            raise ValueError(f"Could not derive ad_group from '{old_ad_group_ad_resource}': {e}")

    # --- 3. Pre-flight: confirm old ad exists and isn't REMOVED --------------
    ga_service = client.get_service("GoogleAdsService")
    q = (f"SELECT ad_group_ad.status FROM ad_group_ad "
         f"WHERE ad_group_ad.resource_name = '{old_ad_group_ad_resource}' LIMIT 1")
    found = False
    for row in ga_service.search(customer_id=customer_id, query=q):
        found = True
        removed_enum = client.enums.AdGroupAdStatusEnum.REMOVED
        if row.ad_group_ad.status == removed_enum:
            raise ValueError(
                f"Cannot replace: old ad is already REMOVED ({old_ad_group_ad_resource})"
            )
    if not found:
        raise ValueError(f"Old ad not found: {old_ad_group_ad_resource}")

    # --- 4. Build PAUSE operation (UPDATE status — status IS mutable) --------
    pause_op = client.get_type("AdGroupAdOperation")
    pause_aga = pause_op.update
    pause_aga.resource_name = old_ad_group_ad_resource
    pause_aga.status = client.enums.AdGroupAdStatusEnum.PAUSED
    pause_op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["status"]))

    # --- 5. Build CREATE operation (new RSA in same ad group) ----------------
    create_op = client.get_type("AdGroupAdOperation")
    new_aga = create_op.create
    new_aga.ad_group = ad_group_resource
    new_aga.status = client.enums.AdGroupAdStatusEnum.ENABLED

    ad = new_aga.ad
    ad.final_urls.append(final_url)
    rsa = ad.responsive_search_ad
    for h_text in h_list:
        asset = client.get_type("AdTextAsset")
        asset.text = h_text
        rsa.headlines.append(asset)
    for d_text in d_list:
        asset = client.get_type("AdTextAsset")
        asset.text = d_text
        rsa.descriptions.append(asset)
    if path1:
        rsa.path1 = path1
    if path2:
        rsa.path2 = path2

    # --- 6. Atomic mutate: partial_failure=False (default) = all-or-nothing --
    service = client.get_service("AdGroupAdService")
    try:
        response = service.mutate_ad_group_ads(
            customer_id=customer_id,
            operations=[pause_op, create_op],
        )
    except Exception as e:
        logger.error(f"replace_ad failed for {old_ad_group_ad_resource}: {e}")
        raise

    # results are ordered same as operations: [pause_result, create_result]
    paused_rn  = response.results[0].resource_name
    created_rn = response.results[1].resource_name
    logger.info(
        f"replace_ad: paused {paused_rn}, created {created_rn} "
        f"(ad_group={ad_group_resource}, {len(h_list)}H/{len(d_list)}D)"
    )
    return {"paused_resource": paused_rn, "created_resource": created_rn}


def _execute_pause_ad(client, customer_id: str,
                       ad_group_ad_resource: str) -> dict:
    """
    Pause a single RSA ad without creating a replacement.

    Args:
        ad_group_ad_resource: customers/CID/adGroupAds/AGID~ADID

    Returns: {"paused_resource": str}

    Raises ValueError on validation failures; raises GoogleAdsException on API failure.
    """
    if not ad_group_ad_resource:
        raise ValueError("ad_group_ad_resource is required")

    # Pre-flight: confirm ad exists and isn't already removed/paused
    ga_service = client.get_service("GoogleAdsService")
    q = (f"SELECT ad_group_ad.status FROM ad_group_ad "
         f"WHERE ad_group_ad.resource_name = '{ad_group_ad_resource}' LIMIT 1")
    found = False
    for row in ga_service.search(customer_id=customer_id, query=q):
        found = True
        status_enum = row.ad_group_ad.status
        if status_enum == client.enums.AdGroupAdStatusEnum.REMOVED:
            raise ValueError(f"Ad is already REMOVED: {ad_group_ad_resource}")
        if status_enum == client.enums.AdGroupAdStatusEnum.PAUSED:
            raise ValueError(f"Ad is already PAUSED: {ad_group_ad_resource}")
    if not found:
        raise ValueError(f"Ad not found: {ad_group_ad_resource}")

    # Build PAUSE operation
    pause_op = client.get_type("AdGroupAdOperation")
    pause_aga = pause_op.update
    pause_aga.resource_name = ad_group_ad_resource
    pause_aga.status = client.enums.AdGroupAdStatusEnum.PAUSED
    client.copy_from(
        pause_op.update_mask,
        field_mask_pb2.FieldMask(paths=["status"])
    )

    service = client.get_service("AdGroupAdService")
    try:
        response = service.mutate_ad_group_ads(
            customer_id=customer_id,
            operations=[pause_op],
        )
    except Exception as e:
        logger.error(f"pause_ad failed for {ad_group_ad_resource}: {e}")
        raise

    paused_rn = response.results[0].resource_name
    logger.info(f"pause_ad: paused {paused_rn}")
    return {"paused_resource": paused_rn}


def _verify_gads_change(client, customer_id: str, operation: str, context: dict) -> dict:
    """
    Read back the entity that was just mutated and return a human-readable confirmation dict.

    Args:
        operation: the operation string (e.g. "pause_keyword", "change_budget", "replace_ad")
        context: relevant resource names / IDs extracted by the caller

    Returns:
        {
          "confirmed": True/False,
          "summary": str   — one-line confirmation message shown in the UI
          "detail": dict   — raw read-back data for debugging
        }

    Never raises — on any failure returns {"confirmed": False, "summary": "Could not verify", "detail": {}}
    """
    ga_svc = client.get_service("GoogleAdsService")

    def _safe_search(query: str):
        try:
            return list(ga_svc.search(customer_id=customer_id, query=query))
        except Exception as e:
            logger.warning(f"_verify_gads_change search failed: {e}")
            return []

    try:
        # ── Keyword status (pause / enable / tighten_match_type / change_match_type) ─────
        if operation in ("pause_keyword", "enable_keyword", "tighten_match_type", "change_match_type"):
            resource_name = context.get("resource_name", "")
            if not resource_name:
                return {"confirmed": False, "summary": "Could not verify — missing resource_name", "detail": {}}
            rows = _safe_search(
                f"SELECT ad_group_criterion.resource_name, "
                f"ad_group_criterion.status, ad_group_criterion.keyword.text, "
                f"ad_group_criterion.keyword.match_type "
                f"FROM ad_group_criterion "
                f"WHERE ad_group_criterion.resource_name = '{resource_name}' LIMIT 1"
            )
            if not rows:
                return {"confirmed": False, "summary": "Keyword not found in read-back", "detail": {}}
            r = rows[0].ad_group_criterion
            status_name = r.status.name if hasattr(r.status, "name") else str(r.status)
            match_name = r.keyword.match_type.name if hasattr(r.keyword.match_type, "name") else str(r.keyword.match_type)
            kw_text = r.keyword.text
            detail = {"status": status_name, "match_type": match_name, "keyword_text": kw_text}
            old_status = (context.get("before_status") or "").upper()
            if operation == "pause_keyword":
                ok = "PAUSED" in status_name.upper()
                if ok and old_status and "PAUSED" in old_status:
                    summary = f"ℹ️ '{kw_text}' was already PAUSED — no change"
                elif ok:
                    summary = f"✅ '{kw_text}' paused"
                else:
                    summary = f"⚠️ '{kw_text}' status is {status_name} (expected PAUSED)"
            elif operation == "enable_keyword":
                ok = "ENABLED" in status_name.upper()
                if ok and old_status and "ENABLED" in old_status:
                    summary = f"ℹ️ '{kw_text}' was already ENABLED — no change"
                elif ok:
                    summary = f"✅ '{kw_text}' enabled"
                else:
                    summary = f"⚠️ '{kw_text}' status is {status_name} (expected ENABLED)"
            else:
                old_match = context.get("before_match_type", "")
                if old_match and old_match != match_name:
                    summary = f"✅ '{kw_text}' match type changed: {old_match} → {match_name}"
                elif old_match:
                    summary = f"ℹ️ '{kw_text}' match type unchanged ({match_name})"
                else:
                    summary = f"✅ '{kw_text}' now {match_name} / {status_name}"
            return {"confirmed": ok if operation in ("pause_keyword", "enable_keyword") else True, "summary": summary, "detail": detail}

        # ── Bid change ────────────────────────────────────────────────────────────────────
        elif operation in ("increase_bid", "decrease_bid"):
            resource_name = context.get("resource_name", "")
            expected_micros = context.get("new_bid_micros", 0)
            if not resource_name:
                return {"confirmed": False, "summary": "Could not verify — missing resource_name", "detail": {}}
            rows = _safe_search(
                f"SELECT ad_group_criterion.resource_name, "
                f"ad_group_criterion.keyword.text, "
                f"ad_group_criterion.effective_cpc_bid_micros, "
                f"ad_group_criterion.cpc_bid_micros "
                f"FROM ad_group_criterion "
                f"WHERE ad_group_criterion.resource_name = '{resource_name}' LIMIT 1"
            )
            if not rows:
                return {"confirmed": False, "summary": "Keyword not found in read-back", "detail": {}}
            r = rows[0].ad_group_criterion
            actual_micros = r.cpc_bid_micros or r.effective_cpc_bid_micros
            actual_usd = actual_micros / 1_000_000
            expected_usd = int(expected_micros) / 1_000_000 if expected_micros else None
            kw_text = r.keyword.text
            detail = {"keyword_text": kw_text, "cpc_bid_micros": actual_micros}
            old_bid_micros = context.get("before_bid_micros")
            try:
                old_bid_micros = int(old_bid_micros) if old_bid_micros not in (None, "", 0) else None
            except (TypeError, ValueError):
                old_bid_micros = None
            old_bid_usd = old_bid_micros / 1_000_000 if old_bid_micros else None
            if expected_usd is not None:
                ok = abs(actual_micros - int(expected_micros)) < 10_000  # within $0.01
                if ok and old_bid_usd is not None:
                    if abs(actual_usd - old_bid_usd) < 0.01:
                        summary = f"ℹ️ '{kw_text}' bid unchanged at ${actual_usd:.2f}"
                    else:
                        summary = f"✅ '{kw_text}' bid changed: ${old_bid_usd:.2f} → ${actual_usd:.2f}"
                elif ok:
                    summary = f"✅ '{kw_text}' bid set to ${actual_usd:.2f}"
                else:
                    summary = f"⚠️ '{kw_text}' bid is ${actual_usd:.2f} (expected ${expected_usd:.2f})"
            else:
                ok = True
                summary = f"✅ '{kw_text}' bid is ${actual_usd:.2f}"
            return {"confirmed": ok, "summary": summary, "detail": detail}

        # ── Budget change ─────────────────────────────────────────────────────────────────
        elif operation == "change_budget":
            campaign_resource = context.get("campaign_resource", "")
            expected_usd = context.get("new_daily_budget_usd", 0)
            if not campaign_resource:
                return {"confirmed": False, "summary": "Could not verify — missing campaign_resource", "detail": {}}
            rows = _safe_search(
                f"SELECT campaign.name, campaign_budget.amount_micros "
                f"FROM campaign "
                f"WHERE campaign.resource_name = '{campaign_resource}' LIMIT 1"
            )
            if not rows:
                return {"confirmed": False, "summary": "Campaign not found in read-back", "detail": {}}
            camp_name = rows[0].campaign.name
            actual_micros = rows[0].campaign_budget.amount_micros
            actual_usd = actual_micros / 1_000_000
            expected_float = float(expected_usd) if expected_usd else None
            detail = {"campaign_name": camp_name, "budget_micros": actual_micros, "budget_usd": actual_usd}
            old_budget_usd = context.get("before_daily_budget_usd")
            if expected_float is not None:
                ok = abs(actual_usd - expected_float) < 0.02
                if ok and old_budget_usd is not None:
                    if abs(actual_usd - float(old_budget_usd)) < 0.02:
                        summary = f"ℹ️ Budget unchanged at ${actual_usd:.2f}/day"
                    else:
                        summary = f"✅ Budget changed: ${float(old_budget_usd):.2f} → ${actual_usd:.2f}/day"
                elif ok:
                    summary = f"✅ Budget set to ${actual_usd:.2f}/day"
                else:
                    summary = f"⚠️ Budget is ${actual_usd:.2f}/day (expected ${expected_float:.2f})"
            else:
                ok = True
                summary = f"✅ Budget is ${actual_usd:.2f}/day"
            return {"confirmed": ok, "summary": summary, "detail": detail}

        # ── Campaign status (pause / enable) ──────────────────────────────────────────────
        elif operation == "status":
            campaign_resource = context.get("campaign_resource", "")
            expected_status = context.get("expected_status", "")  # "ENABLED" or "PAUSED"
            if not campaign_resource:
                return {"confirmed": False, "summary": "Could not verify — missing campaign_resource", "detail": {}}
            rows = _safe_search(
                f"SELECT campaign.name, campaign.status "
                f"FROM campaign "
                f"WHERE campaign.resource_name = '{campaign_resource}' LIMIT 1"
            )
            if not rows:
                return {"confirmed": False, "summary": "Campaign not found in read-back", "detail": {}}
            camp_name = rows[0].campaign.name
            status_name = rows[0].campaign.status.name if hasattr(rows[0].campaign.status, "name") else str(rows[0].campaign.status)
            detail = {"campaign_name": camp_name, "status": status_name}
            ok = (not expected_status) or (expected_status.upper() in status_name.upper())
            old_status = (context.get("before_status") or "").upper()
            _verb_map = {"ENABLED": "enabled", "PAUSED": "paused", "REMOVED": "removed"}
            _verb = _verb_map.get(status_name.upper(), status_name.lower())
            if ok and old_status and old_status in status_name.upper():
                summary = f"ℹ️ Campaign '{camp_name}' was already {_verb} — no change"
            elif ok and expected_status:
                summary = f"✅ Campaign '{camp_name}' {_verb}"
            elif ok:
                summary = f"✅ Campaign '{camp_name}' is now {_verb}"
            else:
                summary = f"⚠️ Campaign '{camp_name}' is {status_name} (expected {expected_status})"
            return {"confirmed": ok, "summary": summary, "detail": detail}

        # ── Ad status changes (pause_ad) ──────────────────────────────────────────────────
        elif operation == "pause_ad":
            ad_resource = context.get("ad_group_ad_resource", "")
            if not ad_resource:
                return {"confirmed": False, "summary": "Could not verify — missing ad_group_ad_resource", "detail": {}}
            rows = _safe_search(
                f"SELECT ad_group_ad.resource_name, ad_group_ad.status, "
                f"ad_group_ad.ad.responsive_search_ad.headlines "
                f"FROM ad_group_ad "
                f"WHERE ad_group_ad.resource_name = '{ad_resource}' LIMIT 1"
            )
            if not rows:
                return {"confirmed": False, "summary": "Ad not found in read-back", "detail": {}}
            r = rows[0].ad_group_ad
            status_name = r.status.name if hasattr(r.status, "name") else str(r.status)
            ok = "PAUSED" in status_name.upper()
            old_status = (context.get("before_status") or "").upper()
            detail = {"ad_resource": ad_resource, "status": status_name}
            if ok and old_status and "PAUSED" in old_status:
                summary = "ℹ️ Ad was already paused — no change"
            elif ok:
                summary = "✅ Ad paused"
            else:
                summary = f"⚠️ Ad status is {status_name} (expected PAUSED)"
            return {"confirmed": ok, "summary": summary, "detail": detail}

        # ── Ad group pause ────────────────────────────────────────────────────────────────
        elif operation == "pause_ad_group":
            ag_resource = context.get("ad_group_resource", "")
            ag_name = context.get("ad_group_name", "")
            if not ag_resource:
                return {"confirmed": False, "summary": "Could not verify — missing ad_group_resource", "detail": {}}
            rows = _safe_search(
                f"SELECT ad_group.resource_name, ad_group.name, ad_group.status "
                f"FROM ad_group "
                f"WHERE ad_group.resource_name = '{ag_resource}' LIMIT 1"
            )
            if not rows:
                return {"confirmed": False, "summary": "Ad group not found in read-back", "detail": {}}
            r = rows[0].ad_group
            status_name = r.status.name if hasattr(r.status, "name") else str(r.status)
            display_name = r.name or ag_name or ag_resource.split("/")[-1]
            ok = "PAUSED" in status_name.upper()
            old_status = (context.get("before_status") or "").upper()
            detail = {"ad_group_resource": ag_resource, "ad_group_name": display_name, "status": status_name}
            if ok and old_status and "PAUSED" in old_status:
                summary = f"ℹ️ Ad group '{display_name}' was already paused — no change"
            elif ok:
                summary = f"✅ Ad group '{display_name}' paused"
            else:
                summary = f"⚠️ Ad group '{display_name}' status is {status_name} (expected PAUSED)"
            return {"confirmed": ok, "summary": summary, "detail": detail}

        # ── Ad group enable ───────────────────────────────────────────────────────────────
        elif operation == "enable_ad_group":
            ag_resource = context.get("ad_group_resource", "")
            ag_name = context.get("ad_group_name", "")
            if not ag_resource:
                return {"confirmed": False, "summary": "Could not verify — missing ad_group_resource", "detail": {}}
            rows = _safe_search(
                f"SELECT ad_group.resource_name, ad_group.name, ad_group.status "
                f"FROM ad_group "
                f"WHERE ad_group.resource_name = '{ag_resource}' LIMIT 1"
            )
            if not rows:
                return {"confirmed": False, "summary": "Ad group not found in read-back", "detail": {}}
            r = rows[0].ad_group
            status_name = r.status.name if hasattr(r.status, "name") else str(r.status)
            display_name = r.name or ag_name or ag_resource.split("/")[-1]
            ok = "ENABLED" in status_name.upper()
            old_status = (context.get("before_status") or "").upper()
            detail = {"ad_group_resource": ag_resource, "ad_group_name": display_name, "status": status_name}
            if ok and old_status and "ENABLED" in old_status:
                summary = f"ℹ️ Ad group '{display_name}' was already active — no change"
            elif ok:
                summary = f"✅ Ad group '{display_name}' enabled"
            else:
                summary = f"⚠️ Ad group '{display_name}' status is {status_name} (expected ENABLED)"
            return {"confirmed": ok, "summary": summary, "detail": detail}

        # ── Replace ad ────────────────────────────────────────────────────────────────────
        elif operation == "replace_ad":
            old_resource = context.get("old_ad_group_ad_resource", "")
            new_resource = context.get("created_ad_group_ad_resource", "")
            results = {}

            # Verify old ad is PAUSED
            if old_resource:
                rows = _safe_search(
                    f"SELECT ad_group_ad.status FROM ad_group_ad "
                    f"WHERE ad_group_ad.resource_name = '{old_resource}' LIMIT 1"
                )
                if rows:
                    old_status = rows[0].ad_group_ad.status.name if hasattr(rows[0].ad_group_ad.status, "name") else str(rows[0].ad_group_ad.status)
                    results["old_ad_status"] = old_status
                    results["old_ad_paused"] = "PAUSED" in old_status.upper()

            # Verify new ad is ENABLED
            if new_resource:
                rows = _safe_search(
                    f"SELECT ad_group_ad.status, ad_group_ad.ad.responsive_search_ad.headlines "
                    f"FROM ad_group_ad "
                    f"WHERE ad_group_ad.resource_name = '{new_resource}' LIMIT 1"
                )
                if rows:
                    new_status = rows[0].ad_group_ad.status.name if hasattr(rows[0].ad_group_ad.status, "name") else str(rows[0].ad_group_ad.status)
                    results["new_ad_status"] = new_status
                    results["new_ad_enabled"] = "ENABLED" in new_status.upper()
                    # Extract first headline for confirmation
                    try:
                        headlines = rows[0].ad_group_ad.ad.responsive_search_ad.headlines
                        results["new_ad_first_headline"] = headlines[0].text if headlines else ""
                    except Exception:
                        pass

            old_ok = results.get("old_ad_paused", False)
            new_ok = results.get("new_ad_enabled", False)
            ok = old_ok and new_ok
            first_h = results.get("new_ad_first_headline", "")
            if ok:
                summary = f"✅ Old ad paused, new ad live" + (f' — \"{first_h}\"' if first_h else "")
            elif old_ok and not new_ok:
                summary = f"⚠️ Old ad paused but new ad status is {results.get('new_ad_status', 'unknown')}"
            elif not old_ok and new_ok:
                summary = f"⚠️ New ad live but old ad is {results.get('old_ad_status', 'unknown')} (expected PAUSED)"
            else:
                summary = f"⚠️ Could not confirm replacement — check Google Ads"
            return {"confirmed": ok, "summary": summary, "detail": results}

        # ── Add keyword (exact or negative) ──────────────────────────────────────────────
        elif operation in ("add_exact_keyword", "add_negative_keyword", "add_to_shared_negative_list"):
            keyword_text = context.get("keyword_text", "")
            if not keyword_text:
                return {"confirmed": True, "summary": "✅ Keyword operation submitted", "detail": {}}
            # For positive keywords, read back from ad_group_criterion
            if operation == "add_exact_keyword":
                ad_group_resource = context.get("ad_group_resource", "")
                # Escape single quotes in keyword_text for safe GAQL interpolation
                kw_escaped = keyword_text.replace("'", "\\'")
                ag_filter = (f" AND ad_group.resource_name = '{ad_group_resource}'"
                             if ad_group_resource else "")
                query = (
                    f"SELECT ad_group_criterion.keyword.text, ad_group_criterion.status "
                    f"FROM ad_group_criterion "
                    f"WHERE ad_group_criterion.keyword.text = '{kw_escaped}' "
                    f"AND ad_group_criterion.negative = false"
                    f"{ag_filter} LIMIT 1"
                )
                rows = _safe_search(query)
                if rows:
                    status_name = rows[0].ad_group_criterion.status.name if hasattr(rows[0].ad_group_criterion.status, "name") else str(rows[0].ad_group_criterion.status)
                    return {"confirmed": True, "summary": f"✅ Keyword '{keyword_text}' added ({status_name})", "detail": {"status": status_name}}
                return {"confirmed": True, "summary": f"✅ Keyword '{keyword_text}' submitted (verify in GAds)", "detail": {}}
            else:
                # Negative keywords: no easy way to query by text without knowing campaign_criterion resource
                return {"confirmed": True, "summary": f"✅ Negative keyword '{keyword_text}' submitted", "detail": {}}

        # ── Bid strategy change ───────────────────────────────────────────────────────────
        elif operation == "change_bid_strategy":
            campaign_resource = context.get("campaign_resource", "")
            expected_strategy = context.get("bid_strategy", "")
            if not campaign_resource:
                return {"confirmed": True, "summary": "✅ Bid strategy change submitted", "detail": {}}
            rows = _safe_search(
                f"SELECT campaign.name, campaign.bidding_strategy_type "
                f"FROM campaign WHERE campaign.resource_name = '{campaign_resource}' LIMIT 1"
            )
            if not rows:
                return {"confirmed": False, "summary": "Campaign not found in read-back", "detail": {}}
            camp_name = rows[0].campaign.name
            strategy_name = rows[0].campaign.bidding_strategy_type.name if hasattr(rows[0].campaign.bidding_strategy_type, "name") else str(rows[0].campaign.bidding_strategy_type)
            old_strategy = context.get("before_bid_strategy", "")
            detail = {"campaign_name": camp_name, "bidding_strategy_type": strategy_name}
            expected_upper = (expected_strategy or "").upper()
            actual_upper = strategy_name.upper()
            # confirmed = True if expected matches actual (or no expectation)
            confirmed = (not expected_upper) or (expected_upper == actual_upper)
            if old_strategy and old_strategy.upper() == actual_upper:
                summary = f"ℹ️ Bid strategy unchanged ({strategy_name})"
            elif expected_upper and not confirmed:
                summary = f"⚠️ Bid strategy is {strategy_name} (expected {expected_strategy})"
            elif old_strategy:
                summary = f"✅ Bid strategy changed: {old_strategy} → {strategy_name}"
            else:
                summary = f"✅ Bid strategy set to {strategy_name}"
            return {"confirmed": confirmed, "summary": summary, "detail": detail}

        # ── Geo exclusion ─────────────────────────────────────────────────────────────────
        elif operation == "geo_exclusion":
            geo_target_resource = context.get("geo_target_resource", "")
            campaign_resource = context.get("campaign_resource", "")
            if not campaign_resource or not geo_target_resource:
                return {"confirmed": True, "summary": "✅ Geo exclusion submitted", "detail": {}}
            rows = _safe_search(
                f"SELECT campaign_criterion.resource_name, campaign_criterion.negative "
                f"FROM campaign_criterion "
                f"WHERE campaign_criterion.campaign = '{campaign_resource}' "
                f"AND campaign_criterion.negative = true "
                f"AND campaign_criterion.location.geo_target_constant = '{geo_target_resource}' LIMIT 1"
            )
            confirmed = len(rows) > 0
            location_name = context.get("location_name", "") or geo_target_resource.split("/")[-1]
            summary = (f"✅ Location excluded: {location_name}"
                       if confirmed else
                       "⚠️ Geo exclusion submitted but not yet visible in read-back — check Google Ads")
            return {"confirmed": confirmed, "summary": summary, "detail": {"found": confirmed}}

        # ── Advisory / fallthrough ────────────────────────────────────────────────────────
        else:
            return {"confirmed": True, "summary": "✅ Change submitted to Google Ads", "detail": {}}

    except Exception as e:
        logger.warning(f"_verify_gads_change({operation}) failed: {e}")
        return {"confirmed": False, "summary": "Could not verify — check Google Ads", "detail": {"error": str(e)}}


def _resolve_geo_target_id(client, location_name: str, country_code: str = "US") -> tuple:
    """
    Resolve a location name to a GeoTargetConstant resource name.
    Returns (resource_name, canonical_name) or ("", "") if not found.
    """
    service = client.get_service("GeoTargetConstantService")
    try:
        request = client.get_type("SuggestGeoTargetConstantsRequest")
        request.locale = "en"
        request.country_code = country_code
        request.location_names.names.append(location_name)
        response = service.suggest_geo_target_constants(request=request)
        for suggestion in response.geo_target_constant_suggestions:
            gtc = suggestion.geo_target_constant
            return gtc.resource_name, gtc.canonical_name
    except Exception as e:
        logger.error(f"Failed to resolve geo target '{location_name}': {e}")
    return "", ""


def _execute_geo_exclusion(client, customer_id: str, campaign_resource: str,
                            geo_target_resource: str) -> bool:
    """
    Add a campaign-level geo exclusion (negative location target).
    Handles ALREADY_EXISTS gracefully (returns True).
    Does NOT check kill switch — caller must check first.
    Returns True on success or duplicate.
    """
    service = client.get_service("CampaignCriterionService")
    operation = client.get_type("CampaignCriterionOperation")
    criterion = operation.create
    criterion.campaign = campaign_resource
    criterion.negative = True
    criterion.location.geo_target_constant = geo_target_resource

    try:
        service.mutate_campaign_criteria(
            customer_id=customer_id,
            operations=[operation],
        )
        logger.info(f"Geo exclusion added: {geo_target_resource} on {campaign_resource}")
        return True
    except Exception as e:
        err_str = str(e)
        if "DUPLICATE_CAMPAIGN_CRITERION" in err_str or "already exists" in err_str.lower():
            logger.info(f"Geo exclusion already exists: {geo_target_resource} — treating as success")
            return True
        logger.error(f"Geo exclusion failed: {geo_target_resource} on {campaign_resource}: {e}")
        raise


def _execute_update_ad_schedule(client, customer_id: str, campaign_resource: str,
                                schedule_value) -> dict:
    """
    Replace the ad schedule on an existing campaign.
    Accepts the same formats as parse_ad_schedule() in google_ads_create.py.
    Does NOT check kill switch — caller must check first.
    Returns {"ok": bool, "pushed": int, "removed": int, "error": str|None}
    """
    from google_ads_create import parse_ad_schedule, push_ad_schedule
    slots = parse_ad_schedule(schedule_value)
    if not slots:
        return {"ok": False, "pushed": 0, "removed": 0,
                "error": f"Could not parse schedule: {schedule_value!r}"}
    return push_ad_schedule(client, customer_id, campaign_resource, slots, replace=True)


def _get_active_rsa_resources(client, customer_id: str, campaign_resource: str) -> list:
    """
    Fetch all ENABLED RSA ad resources for a campaign.
    Returns list of {ad_group_ad_resource, ad_group, ad_group_resource, headlines_count, descriptions_count}
    """
    service = client.get_service("GoogleAdsService")
    query = f"""
        SELECT
            ad_group_ad.resource_name,
            ad_group.name,
            ad_group.resource_name,
            ad_group_ad.ad.responsive_search_ad.headlines,
            ad_group_ad.ad.responsive_search_ad.descriptions
        FROM ad_group_ad
        WHERE campaign.resource_name = '{campaign_resource}'
            AND ad_group_ad.status = 'ENABLED'
            AND ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'
            AND ad_group.status = 'ENABLED'
    """
    results = []
    try:
        response = service.search(customer_id=customer_id, query=query)
        for row in response:
            rsa = row.ad_group_ad.ad.responsive_search_ad
            results.append({
                "ad_group_ad_resource": row.ad_group_ad.resource_name,
                "ad_group": row.ad_group.name,
                "ad_group_resource": row.ad_group.resource_name,
                "headlines_count": len(rsa.headlines),
                "descriptions_count": len(rsa.descriptions),
                "headline_texts": [h.text for h in rsa.headlines[:5]],  # preview for Claude context
            })
    except Exception as e:
        logger.warning(f"Could not fetch RSA resources for {campaign_resource}: {e}")
    return results


# ── Google Ads live negative keyword fetch ────────────────────────────────────

def _fetch_existing_negatives(client, customer_id: str) -> set:
    """
    Pull all negative keywords currently live in Google Ads — both campaign-level
    and from shared negative keyword lists (e.g. 'GDC Competitor Negatives').
    Returns a set of lowercased keyword texts.
    Also saves them to gads_negative_keywords table for offline reference.
    """
    ga_service = client.get_service("GoogleAdsService")
    existing: set = set()
    rows_to_save = []

    # 1. Campaign-level negatives
    camp_query = """
        SELECT
            campaign.name,
            campaign.resource_name,
            campaign_criterion.keyword.text,
            campaign_criterion.keyword.match_type
        FROM campaign_criterion
        WHERE campaign_criterion.negative = TRUE
          AND campaign_criterion.type = 'KEYWORD'
          AND campaign.status = 'ENABLED'
    """
    try:
        for row in ga_service.search(customer_id=customer_id, query=camp_query):
            text = row.campaign_criterion.keyword.text.strip().lower()
            if text:
                existing.add(text)
                rows_to_save.append((text,
                                     row.campaign_criterion.keyword.match_type.name,
                                     row.campaign.name,
                                     row.campaign.resource_name))
    except Exception as e:
        logger.warning(f"Could not fetch campaign-level negative keywords: {e}")

    # 2. Shared negative keyword lists
    shared_query = """
        SELECT
            shared_criterion.keyword.text,
            shared_criterion.keyword.match_type,
            shared_set.name
        FROM shared_criterion
        WHERE shared_criterion.type = 'KEYWORD'
    """
    try:
        for row in ga_service.search(customer_id=customer_id, query=shared_query):
            text = row.shared_criterion.keyword.text.strip().lower()
            if text:
                existing.add(text)
                rows_to_save.append((text,
                                     row.shared_criterion.keyword.match_type.name,
                                     f"[shared] {row.shared_set.name}",
                                     ""))
    except Exception as e:
        logger.warning(f"Could not fetch shared list negatives: {e}")

    logger.info(f"Fetched {len(existing)} unique existing negative keywords from Google Ads "
                f"(campaign-level + shared lists)")
    if existing:
        logger.info(f"Live negatives sample: {sorted(existing)[:20]}")

    # Save to DB for reference (full refresh each run)
    try:
        from database import _conn as _db_conn
        with _db_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS gads_negative_keywords (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    keyword_text TEXT NOT NULL,
                    match_type TEXT DEFAULT 'BROAD',
                    campaign_name TEXT DEFAULT '',
                    campaign_resource TEXT DEFAULT '',
                    synced_at TEXT NOT NULL
                )
            """)
            conn.execute("DELETE FROM gads_negative_keywords")
            now_ts = datetime.now(timezone.utc).isoformat()
            if rows_to_save:
                conn.executemany(
                    "INSERT INTO gads_negative_keywords "
                    "(keyword_text, match_type, campaign_name, campaign_resource, synced_at) "
                    "VALUES (?,?,?,?,?)",
                    [(t, m, cn, cr, now_ts) for t, m, cn, cr in rows_to_save]
                )
    except Exception as e:
        logger.warning(f"Could not save negative keywords to DB: {e}")

    return existing


# ── Deduplication helpers ─────────────────────────────────────────────────────

def _negative_already_handled(keyword_text: str, campaign_name: str,
                               account_level: bool = False,
                               live_negatives: set | None = None) -> bool:
    """
    Return True if this negative keyword should be skipped.

    Two sources of truth:
    1. Google Ads live state (live_negatives) — already blocked in the platform.
       Checks exact match AND whether a broader BROAD negative already covers this
       search term (e.g. "gentle dental" covers "gentle dental worcester ma").
    2. Audit log — previously REJECTED by the user. Don't re-suggest what was
       explicitly dismissed. (Pending/queued items are NOT suppressed — they
       haven't been pushed yet so they should still show as recommendations.)

    account_level param kept for backward-compat but no longer changes behavior.
    """
    kw_lower = keyword_text.strip().lower()

    # ── Check 1: live Google Ads negatives ──────────────────────────────────
    if live_negatives is not None:
        # 1a. Exact match
        if kw_lower in live_negatives:
            return True

        # 1b. Broader BROAD negative already covers this search term.
        #     e.g. "gentle dental" in live_negatives blocks "gentle dental worcester ma".
        for existing_neg in live_negatives:
            if not existing_neg:
                continue
            idx = kw_lower.find(existing_neg)
            if idx == -1:
                continue
            before_ok = (idx == 0 or not kw_lower[idx - 1].isalnum())
            after_idx = idx + len(existing_neg)
            after_ok = (after_idx == len(kw_lower) or not kw_lower[after_idx].isalnum())
            if before_ok and after_ok:
                return True

    # ── Check 2: already in audit log (pending, queued, or rejected) ────────
    # - pending_approval / queued: already recommended, sitting in queue — no need to duplicate
    # - rejected: user hit ✕, don't re-surface ever
    # - success: was pushed (should also be in live_negatives, but belt-and-suspenders)
    try:
        from database import _conn
        with _conn() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM gads_audit_log
                 WHERE LOWER(entity_name) = ?
                   AND operation IN ('add_negative_keyword', 'add_to_shared_negative_list')
                   AND execution_result IN ('pending_approval', 'queued', 'rejected', 'success')
                 LIMIT 1
                """,
                (kw_lower,),
            ).fetchone()
            if row:
                return True
    except Exception as e:
        logger.debug(f"Audit log check failed for '{keyword_text}': {e}")

    return False


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

    # ── Load optimizer memory (cross-run context) ─────────────────────────────
    from optimizer_memory import MemoryStore
    memory = MemoryStore()
    memory.load()
    last_run_date = memory.get_last_run_date()
    today = datetime.now(timezone.utc).date()
    if last_run_date and last_run_date < today:
        # Start from day after last run to avoid re-fetching already-seen data
        search_start = last_run_date + timedelta(days=1)
        logger.info(f"Optimizer memory: last run was {last_run_date}, fetching search terms since {search_start}")
    else:
        # First run OR same-day re-run: seed with last 30 days (safe to overlap on same day)
        search_start = today - timedelta(days=30)
        if last_run_date == today:
            logger.info("Optimizer memory: same-day re-run, re-fetching last 30 days to catch intraday updates")
        else:
            logger.info("Optimizer memory: first run, seeding with last 30 days of search terms")
    memory_digest = memory.build_digest(max_runs=10)

    # Fetch live negative keywords from Google Ads — used throughout run to skip already-applied negatives
    logger.info("Fetching existing negative keywords from Google Ads...")
    live_negatives = _fetch_existing_negatives(client, customer_id)

    # Collect data
    logger.info("Collecting campaign settings (budget, bidding strategy, impression share)...")
    campaign_settings = {}
    try:
        campaign_settings = _get_campaign_settings(client, customer_id, days=30)
        logger.info(f"Campaign settings fetched for {len(campaign_settings)} campaigns")
    except Exception as _cs_e:
        logger.warning(f"Campaign settings fetch failed (non-fatal): {_cs_e}")

    logger.info("Collecting keyword performance...")
    keyword_perf = _get_keyword_performance(client, customer_id, days=30)

    logger.info(f"Collecting search terms since {search_start}...")
    search_terms = _get_search_terms(client, customer_id, start_date=search_start)

    logger.info("Building lead attribution...")
    attribution = _get_keyword_attribution()

    logger.info("Building call attribution...")
    call_attribution = _get_call_attribution(days=30)
    if call_attribution:
        logger.info(f"Call attribution: {len(call_attribution)} campaigns, "
                    f"{sum(c['calls'] for c in call_attribution.values())} total calls, "
                    f"{sum(c['confirmed_appts'] for c in call_attribution.values())} confirmed appts")

    logger.info("Building keyword-level call attribution...")
    keyword_call_attribution = _get_keyword_call_attribution(days=30)
    if keyword_call_attribution:
        kw_call_total = sum(e["calls"] for e in keyword_call_attribution.values())
        kw_conf_total = sum(e["confirmed_appts"] for e in keyword_call_attribution.values())
        logger.info(f"Keyword call attribution: {len(keyword_call_attribution)} keywords, "
                    f"{kw_call_total} calls, {kw_conf_total} confirmed appts")
    else:
        logger.info("Keyword call attribution: 0 keywords (run gads-sync + attribute-keywords first)")

    logger.info("Building OD production summary...")
    od_production = _get_od_production_summary(days=30)

    # Fetch Google's own recommendations
    google_recs = []
    try:
        google_recs = _get_google_recommendations(client, customer_id)
        # Persist to DB for UI display between runs
        from database import upsert_google_rec
        import json as _json
        fetched_at = datetime.now(timezone.utc).isoformat()
        for gr in google_recs:
            upsert_google_rec(
                resource_name=gr["resource_name"],
                rec_type=gr["rec_type"],
                campaign_resource=gr.get("campaign_resource", ""),
                campaign_name=gr.get("campaign_name", ""),
                ad_group_resource=gr.get("ad_group_resource", ""),
                title=gr["title"],
                description=gr["description"],
                impact_json=_json.dumps(gr.get("impact", {})),
                details_json=_json.dumps(gr.get("details", {})),
                fetched_at=fetched_at,
            )
        logger.info(f"Stored {len(google_recs)} Google recommendations")
    except Exception as e:
        logger.warning(f"Google recommendations fetch failed (non-fatal): {e}")

    # ── Fetch and score ad performance for A/B testing loop ──────────────────
    logger.info("Fetching ad performance metrics for A/B testing analysis...")
    all_ads_with_metrics: list = []
    try:
        from database import get_ads_with_metrics as _get_ads_metrics
        raw_ads = _get_ads_metrics(days=30)
        # Build per-campaign CTR averages for benchmarking
        camp_ctr_totals: dict = {}
        for ad in raw_ads:
            if ad.get("status") != "ENABLED" or ad.get("ad_type") != "RESPONSIVE_SEARCH_AD":
                continue
            impr = ad.get("impressions") or 0
            clicks = ad.get("clicks") or 0
            cname = (ad.get("campaign_name") or "").strip().lower()
            if cname not in camp_ctr_totals:
                camp_ctr_totals[cname] = {"impressions": 0, "clicks": 0}
            camp_ctr_totals[cname]["impressions"] += impr
            camp_ctr_totals[cname]["clicks"] += clicks
        camp_avg_ctr: dict = {
            c: (v["clicks"] / v["impressions"]) if v["impressions"] > 0 else 0
            for c, v in camp_ctr_totals.items()
        }
        # Score each active RSA
        for ad in raw_ads:
            if ad.get("status") != "ENABLED" or ad.get("ad_type") != "RESPONSIVE_SEARCH_AD":
                continue
            impr = ad.get("impressions") or 0
            clicks = ad.get("clicks") or 0
            cost_micros = ad.get("cost_micros") or 0
            conv = ad.get("conversions") or 0
            cname = (ad.get("campaign_name") or "").strip().lower()
            ctr = (clicks / impr) if impr > 0 else 0
            avg_ctr = camp_avg_ctr.get(cname, 0)
            cost_usd = cost_micros / 1_000_000

            # Performance tier — shared logic via _score_tier
            tier = _score_tier(impr, clicks, cost_usd, conv, avg_ctr)

            # Build assets from assets_json if available
            assets = {}
            try:
                assets = json.loads(ad.get("assets_json") or "{}")
            except Exception:
                pass
            headlines = assets.get("headlines", [ad.get("headline_1",""), ad.get("headline_2",""), ad.get("headline_3","")])
            descriptions = assets.get("descriptions", [ad.get("description_1",""), ad.get("description_2","")])

            all_ads_with_metrics.append({
                "ad_id":               ad.get("ad_id",""),
                "ad_group_ad_resource": ad.get("ad_group_ad_resource",""),
                "ad_group_resource":   ad.get("ad_group_resource",""),
                "campaign_name":       ad.get("campaign_name",""),
                "campaign_id":         ad.get("campaign_id",""),
                "ad_group_name":       ad.get("ad_group_name",""),
                "final_url":           ad.get("final_url",""),
                "headlines":           [h for h in headlines if h],
                "descriptions":        [d for d in descriptions if d],
                "impressions_30d":     impr,
                "clicks_30d":          clicks,
                "cost_30d_usd":        round(cost_usd, 2),
                "conversions_30d":     round(conv, 2),
                "ctr":                 round(ctr, 4),
                "avg_campaign_ctr":    round(avg_ctr, 4),
                "performance_tier":    tier,
            })
        enabled_rsas = len(all_ads_with_metrics)
        weak_rsas = sum(1 for a in all_ads_with_metrics if a["performance_tier"] in ("weak","cold"))
        logger.info(f"Ad performance: {enabled_rsas} active RSAs scored, {weak_rsas} weak/cold")
    except Exception as _ads_err:
        logger.warning(f"Ad performance fetch failed (non-fatal): {_ads_err}")

    # ── Fetch and score ad group performance ──────────────────────────────────
    # Derive ad_group_resource from keyword_perf (API-returned, guaranteed valid)
    # so Claude receives real resource names, not synthesised strings.
    logger.info("Fetching ad group stats for scoring...")
    all_ag_stats: list = []
    try:
        from database import get_ad_group_stats as _get_ag_stats
        raw_ag = _get_ag_stats(days=30)

        # Build ad_group_id → ad_group_resource from keyword_perf (primary source)
        ag_id_to_resource: dict = {}
        for kw in keyword_perf:
            ag_res = kw.get("ad_group_resource", "")
            # Extract numeric ad_group_id from resource name: .../adGroups/{id}
            if ag_res and "/adGroups/" in ag_res:
                try:
                    ag_id = ag_res.split("/adGroups/")[-1]
                    if ag_id.isdigit():
                        ag_id_to_resource[ag_id] = ag_res
                except Exception:
                    pass

        # Build per-campaign-id avg CTR for ad group benchmarking
        # Use campaign_id (unique) not campaign_name to avoid collisions
        camp_id_ctr_totals: dict = {}
        for ag in raw_ag:
            cid = ag.get("campaign_id") or ag.get("campaign_name", "")
            if cid not in camp_id_ctr_totals:
                camp_id_ctr_totals[cid] = {"impressions": 0, "clicks": 0}
            camp_id_ctr_totals[cid]["impressions"] += ag.get("impressions") or 0
            camp_id_ctr_totals[cid]["clicks"] += ag.get("clicks") or 0
        camp_id_avg_ctr: dict = {
            cid: (v["clicks"] / v["impressions"]) if v["impressions"] > 0 else 0
            for cid, v in camp_id_ctr_totals.items()
        }

        for ag in raw_ag:
            cid = ag.get("campaign_id") or ag.get("campaign_name", "")
            impr = ag.get("impressions") or 0
            clicks = ag.get("clicks") or 0
            cost = float(ag.get("cost") or 0)
            conv = float(ag.get("conversions") or 0)
            avg_ctr = camp_id_avg_ctr.get(cid, 0)

            tier = _score_tier(impr, clicks, cost, conv, avg_ctr)

            # Prefer API-derived resource name; fall back to synthesised for new ad groups
            ag_id_str = str(ag.get("ad_group_id") or "")
            ag_resource = ag_id_to_resource.get(ag_id_str, "")
            if not ag_resource:
                # Synthesise only as last resort (new ad groups not yet in keyword cache)
                ag_resource = (
                    f"customers/{customer_id}/adGroups/{ag_id_str}"
                    if ag_id_str.isdigit() else ""
                )

            if not ag_resource:
                continue  # No usable resource name — skip this ad group

            all_ag_stats.append({
                "ad_group_id":      ag_id_str,
                "ad_group_resource": ag_resource,
                "ad_group_name":    ag.get("ad_group_name", ""),
                "campaign_name":    ag.get("campaign_name", ""),
                "campaign_id":      str(cid),
                "impressions_30d":  impr,
                "clicks_30d":       clicks,
                "cost_30d_usd":     round(cost, 2),
                "conversions_30d":  round(conv, 2),
                "lead_count_30d":   int(ag.get("lead_count") or 0),
                "revenue_30d":      float(ag.get("revenue") or 0),
                "ctr":              round((clicks / impr) if impr > 0 else 0, 4),
                "avg_campaign_ctr": round(avg_ctr, 4),
                "cpl":              float(ag.get("cpl") or 0),
                "performance_tier": tier,
            })

        weak_ags = sum(1 for a in all_ag_stats if a["performance_tier"] in ("weak", "cold"))
        logger.info(
            f"Ad group scoring: {len(all_ag_stats)} ad groups scored "
            f"(from {len(raw_ag)} in DB), {weak_ags} weak/cold"
        )
        if len(all_ag_stats) < len(raw_ag):
            logger.info(
                f"  {len(raw_ag) - len(all_ag_stats)} ad group(s) skipped — "
                f"no resource name in keyword cache (likely new groups not yet synced)"
            )
    except Exception as _ag_err:
        logger.warning(f"Ad group stats fetch failed (non-fatal): {_ag_err}")

    # ── Capture account-wide totals before any filtering ──────────────────────
    total_spend_all_campaigns = round(sum(k.get("cost", 0) for k in keyword_perf), 2)
    total_clicks_all_campaigns = sum(k.get("clicks", 0) for k in keyword_perf)

    # ── Determine which campaigns to analyze ──────────────────────────────────
    # Analyze ALL campaigns that have at least some keyword data/spend.
    # The old ai_review_enabled allow-list is ignored — every active campaign
    # with impressions gets Claude analysis so recommendations appear per-campaign.
    # Paused campaigns are included if they have recent spend data (last 30d).
    active_campaigns_with_data = {
        k.get("campaign", "").strip()
        for k in keyword_perf
        if k.get("campaign", "").strip()
    }
    logger.info(f"Campaigns with keyword data: {active_campaigns_with_data}")

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

    # Load outcome history once (AI learning loop — shared across all rule passes)
    outcome_history = _load_outcome_history(days_back=90)
    if outcome_history:
        logger.info(f"Outcome history loaded: {len(outcome_history)} entity-operation pairs from last 90d")

    # Analyze
    logger.info("Analyzing and generating recommendations...")
    actions = _analyze_keywords(
        keyword_perf, attribution, search_terms,
        call_attribution=call_attribution,
        keyword_call_attribution=keyword_call_attribution,
        campaign=primary_campaign,
        outcome_history=outcome_history,
    )

    # ── Phase A: Suppress recently-rejected recommendations ───────────────────
    # M6 fix: key suppression by (entity_name_lower, operation) tuple — NOT entity_name
    # alone. A rejected "decrease_bid" on keyword X should NOT suppress "pause_keyword"
    # on the same keyword X. Each action type maps to a distinct operation string.
    # Initialize rejection sets — always defined so Claude loop can reference them safely
    rejected_op_pairs: set = set()
    rejected_id_op_pairs: set = set()

    try:
        from database import get_recent_rejections
        recent_rejections = get_recent_rejections(days=30)
        suppressed_count = 0

        # Build (entity_name_lower, operation) and (entity_id, operation) sets
        for r in recent_rejections:
            ename = (r.get("entity_name") or "").lower()
            op = r.get("operation") or ""
            eid = r.get("entity_id") or ""
            if ename and op:
                rejected_op_pairs.add((ename, op))
            if eid and op:
                rejected_id_op_pairs.add((eid, op))

        # Map each action_type to the exact operation string stored in gads_audit_log
        ACTION_TO_OPERATION = {
            "pause":         "pause_keyword",
            "increase_bid":  "increase_bid",
            "decrease_bid":  "decrease_bid",
            "new_exact":     "add_exact_keyword",
            "new_negative":  "add_negative_keyword",
        }

        def _is_rejected(item: dict, operation: str) -> bool:
            """Return True if this exact (entity, operation) was recently rejected."""
            eid = item.get("resource_name") or item.get("ad_group_resource") or item.get("campaign_resource") or ""
            ename = (item.get("keyword") or item.get("search_term") or "").lower()
            if eid and (eid, operation) in rejected_id_op_pairs:
                return True
            if ename and (ename, operation) in rejected_op_pairs:
                return True
            return False

        for action_type, operation in ACTION_TO_OPERATION.items():
            before = len(actions.get(action_type, []))
            actions[action_type] = [a for a in actions.get(action_type, []) if not _is_rejected(a, operation)]
            after = len(actions[action_type])
            suppressed_count += (before - after)

        if suppressed_count > 0:
            logger.info(f"[phase_a] Suppressed {suppressed_count} recommendation(s) recently rejected by admin")
    except Exception as _rej_err:
        logger.warning(f"[phase_a] Rejection suppression check failed (non-fatal): {_rej_err}")

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
            campaign_name=kw.get("campaign", ""),
            priority=10,  # Pausing = high priority (stops waste)
            impact_estimate={"savings_30d_usd": round(kw.get("cost", 0), 2)},
        )
        kw["action_id"] = aid
        actions_pending += 1

    for kw in actions["increase_bid"]:
        current_bid = kw.get("current_bid_micros", 0)
        new_bid = int(current_bid * 1.10) if current_bid > 0 else 0
        aid = log_pending(
            operation="increase_bid",
            entity_type="keyword",
            entity_id=kw.get("resource_name", ""),
            entity_name=kw["keyword"],
            before_state={
                "match_type": kw.get("match_type", ""),
                "current_bid_micros": current_bid,
                "roas": kw.get("roas", 0),
            },
            after_state={
                "bid_change": "+10%",
                "new_bid_micros": new_bid,
            },
            optimizer_run_id=run_id,
            reason=kw.get("reason", ""),
            campaign_name=kw.get("campaign", ""),
            priority=30,
        )
        kw["action_id"] = aid
        actions_pending += 1

    for kw in actions["decrease_bid"]:
        current_bid = kw.get("current_bid_micros", 0)
        new_bid = int(current_bid * 0.90) if current_bid > 0 else 0
        aid = log_pending(
            operation="decrease_bid",
            entity_type="keyword",
            entity_id=kw.get("resource_name", ""),
            entity_name=kw["keyword"],
            before_state={
                "match_type": kw.get("match_type", ""),
                "current_bid_micros": current_bid,
            },
            after_state={
                "bid_change": "-10%",
                "new_bid_micros": new_bid,
            },
            optimizer_run_id=run_id,
            reason=kw.get("reason", ""),
            campaign_name=kw.get("campaign", ""),
            priority=40,
        )
        kw["action_id"] = aid
        actions_pending += 1

    for st in actions["new_exact"]:
        aid = log_pending(
            operation="add_exact_keyword",
            entity_type="keyword",
            entity_id=st.get("ad_group_resource", st["search_term"]),
            entity_name=st["search_term"],
            before_state={
                "type": "search_term",
                "clicks": st.get("clicks", 0),
                "conversions": st.get("conversions", 0),
            },
            after_state={
                "keyword_text": st["search_term"],
                "match_type": "EXACT",
                "ad_group_resource": st.get("ad_group_resource", ""),
                "ad_group": st.get("ad_group", ""),
            },
            optimizer_run_id=run_id,
            reason=st.get("reason", ""),
            campaign_name=st.get("campaign", ""),
            priority=20,
        )
        st["action_id"] = aid
        actions_pending += 1

    for st in actions["new_negatives"]:
        kw_text = st["search_term"]
        camp_name = st.get("campaign", "")
        if _negative_already_handled(kw_text, camp_name, live_negatives=live_negatives):
            logger.info(f"  SKIPPED negative '{kw_text}' — already covered by live Google Ads negative")
            continue
        aid = log_pending(
            operation="add_negative_keyword",
            entity_type="keyword",
            entity_id=st.get("campaign_resource", kw_text),
            entity_name=kw_text,
            before_state={
                "type": "search_term",
                "cost": st.get("cost", 0),
            },
            after_state={
                "keyword_text": kw_text,
                "match_type": "BROAD",
                "campaign_resource": st.get("campaign_resource", ""),
                "campaign": camp_name,
            },
            optimizer_run_id=run_id,
            reason=st.get("reason", ""),
            campaign_name=camp_name,
            priority=15,
            impact_estimate={"savings_30d_usd": round(st.get("cost", 0), 2)},
        )
        st["action_id"] = aid
        actions_pending += 1

    for kw in actions["tighten_match"]:
        aid = log_pending(
            operation="tighten_match_type",
            entity_type="keyword",
            entity_id=kw["resource_name"],
            entity_name=kw["keyword"],
            before_state={
                "match_type": kw["current_match_type"],
                "resource_name": kw["resource_name"],
            },
            after_state={
                "match_type": kw["proposed_match_type"],
                "ad_group_resource": kw.get("ad_group_resource", ""),
                "note": "Add EXACT first, then pause BROAD to avoid impression gap",
            },
            optimizer_run_id=run_id,
            reason=kw.get("reason", ""),
            campaign_name=kw.get("campaign", ""),
            priority=25,
        )
        kw["action_id"] = aid
        actions_pending += 1

    # Patch summary with account-wide spend/clicks (pre-filter values).
    # _analyze_keywords only sees allow-listed campaigns so its totals are partial.
    summary = actions["summary"]
    summary["total_spend"] = total_spend_all_campaigns
    summary["total_clicks"] = total_clicks_all_campaigns
    # Recompute derived metrics using corrected spend
    combined_acq = summary.get("total_leads", 0) + summary.get("total_booked_calls", 0)
    summary["overall_roas"] = round(summary["total_production"] / total_spend_all_campaigns, 1) if total_spend_all_campaigns > 0 else 0
    summary["cost_per_lead"] = round(total_spend_all_campaigns / summary["total_leads"], 2) if summary.get("total_leads", 0) > 0 else 0
    summary["cost_per_acquisition"] = round(total_spend_all_campaigns / combined_acq, 2) if combined_acq > 0 else 0
    # Keyword-level attribution quality metrics
    summary["keywords_with_call_attribution"] = len(keyword_call_attribution)
    summary["keyword_attributed_calls"] = sum(e["calls"] for e in keyword_call_attribution.values())

    # Claude structured recommendations — run once per active campaign.
    # Returns dicts with operation + exact API parameters, not plain text.
    logger.info("Calling Claude (Opus) for structured recommendations...")
    # Use ALL campaigns with keyword data — not just campaign_spend keys
    # (campaign_spend only covers the allow-listed set in legacy mode; now we use all)
    all_campaign_names = sorted(active_campaigns_with_data) or list(campaign_spend.keys()) or ([primary_campaign] if primary_campaign else [])
    priority_counter = 30
    advisories = []  # for report dashboard (human-readable reasons)

    # Operation → (entity_type, entity_id_field, entity_name_field)
    # entity_id_field: which key in the rec dict to use as entity_id in audit log
    # entity_name_field: which key to use as the human-readable entity_name
    _OP_MAP = {
        "add_negative_keyword":         ("keyword",  "campaign_resource", "keyword_text"),
        "add_to_shared_negative_list":  ("keyword",  "keyword_text",      "keyword_text"),  # added to account-wide shared list
        "pause_keyword":        ("keyword",  "resource_name",     "keyword_text"),
        "enable_keyword":       ("keyword",  "resource_name",     "keyword_text"),
        "increase_bid":         ("keyword",  "resource_name",     "keyword_text"),
        "decrease_bid":         ("keyword",  "resource_name",     "keyword_text"),
        "add_exact_keyword":    ("keyword",  "ad_group_resource", "keyword_text"),
        "ad_copy_suggestion":   ("ad",       "ad_resource",       "headline"),
        "geo_exclusion":        ("campaign", "geo_target_resource", "location_name"),
        "change_budget":        ("campaign", "campaign_resource", "campaign_resource"),
        "change_bid_strategy":  ("campaign", "campaign_resource", "bid_strategy"),
        "change_match_type":    ("keyword",  "resource_name",     "keyword_text"),
        "add_asset":            ("campaign", "campaign_resource",  "asset_type"),
        "replace_ad":           ("ad",       "old_ad_group_ad_resource", "old_ad_group_ad_resource"),
        "pause_ad_group":       ("ad_group", "ad_group_resource",        "ad_group_name"),
    }

    # Geo candidates for Grafton Dental Care (Worcester area, MA)
    _CANDIDATE_GEOS = [
        "Worcester, Massachusetts",
        "Shrewsbury, Massachusetts",
        "Northborough, Massachusetts",
        "Westborough, Massachusetts",
        "Grafton, Massachusetts",
        "Millbury, Massachusetts",
        "Auburn, Massachusetts",
    ]

    for camp_name in all_campaign_names:
        camp_lower = camp_name.strip().lower()

        camp_kw   = [k for k in keyword_perf  if k.get("campaign","").strip().lower() == camp_lower]
        camp_st   = [s for s in search_terms   if s.get("campaign","").strip().lower() == camp_lower]
        camp_kw_attr = {k: v for k, v in keyword_call_attribution.items()
                        if any(kw.get("keyword","").strip().lower() == k for kw in camp_kw)}

        # Get campaign resource_name for pre-fetches
        camp_resource = ""
        for kw in camp_kw:
            cr = kw.get("campaign_resource", "")
            if cr:
                camp_resource = cr
                break

        # Pre-fetch RSA resources for this campaign (gives Claude ad_group_ad_resource for ad copy API calls)
        camp_rsa_resources = []
        if camp_resource:
            try:
                camp_rsa_resources = _get_active_rsa_resources(client, customer_id, camp_resource)
                if camp_rsa_resources:
                    logger.info(f"  [{camp_name}] {len(camp_rsa_resources)} RSA(s) found for Claude context")
            except Exception as _rsa_e:
                logger.warning(f"  [{camp_name}] RSA pre-fetch failed (non-fatal): {_rsa_e}")

        # Filter ad performance to this campaign
        camp_ad_perf = [
            a for a in all_ads_with_metrics
            if a.get("campaign_name","").strip().lower() == camp_lower
        ]
        weak_count = sum(1 for a in camp_ad_perf if a["performance_tier"] in ("weak","cold"))
        if camp_ad_perf:
            logger.info(f"  [{camp_name}] {len(camp_ad_perf)} RSAs scored — {weak_count} weak/cold")

        # Filter ad group stats to this campaign
        camp_ag_perf = [
            a for a in all_ag_stats
            if a.get("campaign_name", "").strip().lower() == camp_lower
        ]
        if camp_ag_perf:
            weak_ag = sum(1 for a in camp_ag_perf if a["performance_tier"] in ("weak", "cold"))
            logger.info(f"  [{camp_name}] {len(camp_ag_perf)} ad groups — {weak_ag} weak/cold")
        else:
            logger.info(f"  [{camp_name}] No ad group stats (sync may be pending)")

        # Load landing page intel for this campaign's final_url
        camp_page_intel = ""
        try:
            from domain_crawler import build_site_context_for_url
            # Use the most common final_url from this campaign's ads
            camp_urls = [a.get("final_url","") for a in camp_ad_perf if a.get("final_url")]
            if camp_urls:
                primary_url = max(set(camp_urls), key=camp_urls.count)
                # Strip path to get domain root for context lookup
                from urllib.parse import urlparse as _urlparse
                parsed = _urlparse(primary_url)
                domain_url = f"{parsed.scheme}://{parsed.netloc}"
                camp_page_intel = build_site_context_for_url(domain_url)
                if camp_page_intel:
                    logger.info(f"  [{camp_name}] Landing page intel loaded ({len(camp_page_intel)} chars)")
        except Exception as _pi_err:
            logger.warning(f"  [{camp_name}] Landing page intel fetch failed (non-fatal): {_pi_err}")

        # Pre-resolve geo targets for this campaign (gives Claude geo_target_resource for API execution)
        camp_geo_resolutions: dict = {}
        if camp_resource:
            try:
                for geo_name in _CANDIDATE_GEOS:
                    rn, canonical = _resolve_geo_target_id(client, geo_name)
                    if rn:
                        camp_geo_resolutions[geo_name] = {
                            "geo_target_resource": rn,
                            "canonical_name": canonical,
                        }
                if camp_geo_resolutions:
                    logger.info(f"  [{camp_name}] {len(camp_geo_resolutions)} geo targets resolved")
            except Exception as _geo_e:
                logger.warning(f"  [{camp_name}] Geo pre-resolve failed (non-fatal): {_geo_e}")

        # Resolve campaign settings for this campaign by resource name
        camp_settings = campaign_settings.get(camp_resource, {}) if camp_resource else {}

        structured = _call_claude_advisories(
            camp_kw, attribution, camp_st,
            call_attribution, od_production,
            summary=summary, campaign=camp_name,
            keyword_call_attribution=camp_kw_attr,
            rsa_resources=camp_rsa_resources,
            geo_resolutions=camp_geo_resolutions,
            google_recs=[r for r in google_recs if r.get('campaign_name','').lower() == camp_name.lower() or not r.get('campaign_name')],
            optimizer_run_id=run_id,
            existing_negatives=live_negatives,
            memory_digest=memory_digest,
            camp_settings=camp_settings,
            ad_performance=camp_ad_perf,
            landing_page_intel=camp_page_intel,
            ad_group_performance=camp_ag_perf,
        )
        if not structured:
            continue

        logger.info(f"Claude recommendations for '{camp_name}': {len(structured)}")

        _replace_ad_count_for_camp = 0     # enforce one replace_ad per campaign per run
        _pause_ag_count_for_camp = 0       # enforce one pause_ad_group per campaign per run

        # Safety: how many active ad groups does this campaign have?
        # get_ad_group_stats has no status filter, so we proxy "active" as
        # impressions_30d > 0 — groups with zero impressions are not serving.
        # This prevents pausing the campaign's only impression-generating ad group.
        _active_ag_count = sum(
            1 for a in camp_ag_perf if (a.get("impressions_30d") or 0) > 0
        )

        for rec in structured:
            op = rec.get("operation", "claude_advisory")
            reason = rec.get("reason", "")

            # Dedup check for Claude negative keyword recs — same as rule-based negatives
            if op in ("add_negative_keyword", "add_to_shared_negative_list"):
                kw_text = rec.get("keyword_text", "").strip().lower()
                if kw_text and _negative_already_handled(kw_text, camp_name, live_negatives=live_negatives):
                    logger.info(f"  [{camp_name}] SKIPPED Claude negative '{kw_text}' — already covered")
                    continue

            # One replace_ad per campaign per run (A/B principle — focused changes)
            if op == "replace_ad":
                if _replace_ad_count_for_camp >= 1:
                    logger.info(f"  [{camp_name}] SKIPPED extra replace_ad — one-per-campaign limit")
                    continue
                _replace_ad_count_for_camp += 1

            # pause_ad_group: safety checks first (before rejection suppression),
            # but quota increment AFTER rejection suppression so rejected recs
            # don't consume the per-campaign limit.
            if op == "pause_ad_group":
                if _pause_ag_count_for_camp >= 1:
                    logger.info(f"  [{camp_name}] SKIPPED extra pause_ad_group — one-per-campaign limit")
                    continue
                if _active_ag_count <= 1:
                    logger.info(
                        f"  [{camp_name}] SKIPPED pause_ad_group '{rec.get('ad_group_name','')}' "
                        f"— only {_active_ag_count} serving ad group(s), cannot pause the last one"
                    )
                    continue

            # Rejection suppression for Claude recs (mirrors rule-based suppression above)
            op_meta_check = _OP_MAP.get(op)
            if op_meta_check:
                _, id_field_check, name_field_check = op_meta_check
                eid_check = str(rec.get(id_field_check, "")).lower()
                ename_check = str(rec.get(name_field_check, "")).lower()
                if (eid_check, op) in rejected_id_op_pairs or (ename_check, op) in rejected_op_pairs:
                    logger.info(f"  [{camp_name}] SKIPPED {op} '{ename_check}' — recently rejected by admin")
                    continue

            # Increment pause_ad_group quota only after passing rejection suppression
            if op == "pause_ad_group":
                _pause_ag_count_for_camp += 1

            advisories.append(f"[{camp_name}] {reason}")

            # Build before/after state from the structured fields
            after = {k: v for k, v in rec.items() if k != "operation"}

            # For replace_ad: populate before_state with current ad copy from ad_performance
            before = {}
            if op == "replace_ad":
                old_rn = rec.get("old_ad_group_ad_resource", "")
                # Find the matching ad in camp_ad_perf to populate before_state
                matched_ad = next(
                    (a for a in camp_ad_perf if a.get("ad_group_ad_resource") == old_rn),
                    None
                )
                if matched_ad:
                    before = {
                        "status": matched_ad.get("status", "ENABLED"),
                        "headlines": matched_ad.get("headlines", []),
                        "descriptions": matched_ad.get("descriptions", []),
                        "final_url": matched_ad.get("final_url", ""),
                        "ctr_30d": matched_ad.get("ctr", 0),
                        "impressions_30d": matched_ad.get("impressions_30d", 0),
                        "cost_30d_usd": matched_ad.get("cost_30d_usd", 0),
                        "conversions_30d": matched_ad.get("conversions_30d", 0),
                        "performance_tier": matched_ad.get("performance_tier", ""),
                    }
                else:
                    before = {"ad_group_ad_resource": old_rn, "status": "ENABLED"}

            # Determine entity fields
            op_meta = _OP_MAP.get(op)
            if op_meta:
                entity_type, id_field, name_field = op_meta
                entity_id   = str(rec.get(id_field, camp_lower.replace(" ", "_")))
                entity_name = str(rec.get(name_field, camp_name))
                # For replace_ad: derive a human-readable name instead of raw resource string
                if op == "replace_ad" and matched_ad:
                    entity_name = f"Replace ad in {matched_ad.get('ad_group_name', camp_name)}"
                elif op == "replace_ad":
                    entity_name = f"Replace ad — {camp_name}"
            else:
                entity_type = "campaign"
                entity_id   = camp_lower.replace(" ", "_")
                entity_name = camp_name

            aid = log_pending(
                operation=op,
                entity_type=entity_type,
                entity_id=entity_id,
                entity_name=entity_name,
                before_state=before,
                after_state=after,
                optimizer_run_id=run_id,
                reason=reason,
                campaign_name=camp_name,
                priority=priority_counter,
                impact_estimate=rec.get("estimated_monthly_impact", {}),
            )
            # Store google_rec_resource_name if this rec came from a Google recommendation
            google_rec_rn = rec.get("google_rec_resource_name", "")
            if google_rec_rn:
                try:
                    from database import _conn as _db_conn_opt
                    with _db_conn_opt() as _c:
                        _c.execute(
                            "UPDATE gads_audit_log SET google_rec_resource_name=? WHERE action_id=?",
                            (google_rec_rn, aid)
                        )
                except Exception as _grn_err:
                    logger.warning(f"Could not store google_rec_resource_name: {_grn_err}")
            actions_pending += 1
            priority_counter += 1
            logger.info(f"  [{op}] '{entity_name}' → {aid[:8]}")

        actions.setdefault("memory_applied", []).extend(
            [f"[claude:{camp_name}] {rec.get('reason','')}" for rec in structured]
        )

    # ── Account-level pass (cross-campaign patterns) ─────────────────────────
    logger.info("Calling Claude (Opus) for account-level recommendations...")
    # Build campaign_spend dict with resource info for the account-level function
    camp_spend_for_acct = {}
    for cn in all_campaign_names:
        cn_lower = cn.strip().lower()
        kws = [k for k in keyword_perf if k.get("campaign","").strip().lower() == cn_lower]
        camp_spend_for_acct[cn] = {
            "daily_budget_usd": next((k.get("daily_budget_micros", 0) / 1e6 for k in kws if k.get("daily_budget_micros")), None),
        }

    acct_structured = _call_claude_account_level(
        all_keyword_perf=keyword_perf,
        all_search_terms=search_terms,
        call_attribution=call_attribution,
        od_production=od_production,
        summary=summary,
        campaign_spend=camp_spend_for_acct,
        google_recs=[r for r in google_recs if not r.get("campaign_name")],
        optimizer_run_id=run_id,
        existing_negatives=live_negatives,
        memory_digest=memory_digest,
    )

    logger.info(f"Account-level recommendations: {len(acct_structured)}")
    _acct_negatives_logged: set = set()  # track within this loop to catch Claude dupes
    for rec in acct_structured:
        op = rec.get("operation", "claude_advisory")
        reason = rec.get("reason", rec.get("insight", ""))

        op_meta = _OP_MAP.get(op)
        if op_meta:
            entity_type, id_field, name_field = op_meta
            entity_id   = str(rec.get(id_field, "account"))
            entity_name = str(rec.get(name_field, "Account"))
        else:
            entity_type = "account"
            entity_id   = "account"
            entity_name = "Account"

        # Account-level negatives go to the shared list (applies to all campaigns, inc. future ones)
        if op == "add_negative_keyword":
            op = "add_to_shared_negative_list"
        if op == "add_to_shared_negative_list":
            kw = rec.get("keyword_text", entity_name).strip().lower()
            if kw in _acct_negatives_logged:
                logger.debug(f"Account-level Claude negative '{kw}' already logged this run — skipping")
                continue
            if _negative_already_handled(kw, "", account_level=True, live_negatives=live_negatives):
                logger.info(f"  SKIPPED account-level Claude negative '{kw}' — already covered by live Google Ads negative")
                continue
            _acct_negatives_logged.add(kw)

        advisories.append(f"[Account Level] {reason}")

        aid = log_pending(
            operation=op,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            before_state={},
            after_state={k: v for k, v in rec.items() if k not in ("operation", "campaign_name")},
            optimizer_run_id=run_id,
            reason=reason,
            campaign_name="",   # ← account-level: no campaign
            priority=priority_counter,
            impact_estimate=rec.get("estimated_monthly_impact", {}),
        )
        actions_pending += 1
        priority_counter += 1
        logger.info(f"  [ACCOUNT] [{op}] '{entity_name}' → {aid[:8]}")

        # Store google_rec_resource_name if applicable
        google_rec_rn = rec.get("google_rec_resource_name", "")
        if google_rec_rn:
            try:
                from database import _conn as _db_conn_acct
                with _db_conn_acct() as _c:
                    _c.execute(
                        "UPDATE gads_audit_log SET google_rec_resource_name=? WHERE action_id=?",
                        (google_rec_rn, aid)
                    )
            except Exception as _grn_err:
                logger.warning(f"Could not store google_rec_resource_name (account): {_grn_err}")

    # ── Account-level rule-based competitor negatives ────────────────────────
    # Find competitor terms that appeared in ≥2 campaigns in this run.
    # These are logged as add_negative_keyword with campaign_name="" (Account Level section).
    # The campaign_resource is set to the highest-spend campaign's resource.
    from collections import defaultdict as _defdict
    _comp_term_campaigns: dict = _defdict(list)  # term -> [(campaign_name, campaign_resource, cost)]
    _camp_spend_map: dict = {}  # campaign_name -> total spend from keyword_perf
    for _st in search_terms:
        _term = _st.get("search_term", "").strip().lower()
        _camp = _st.get("campaign", "").strip()
        _camp_res = _st.get("campaign_resource", "")
        if _term and _camp and _is_competitor_term(_term):
            _comp_term_campaigns[_term].append((_camp, _camp_res, _st.get("cost", 0.0)))
    for _kw in keyword_perf:
        _cn = _kw.get("campaign", "").strip()
        if _cn:
            _camp_spend_map[_cn] = _camp_spend_map.get(_cn, 0.0) + _kw.get("cost", 0.0)

    _acct_comp_logged = 0
    for _term, _occurrences in _comp_term_campaigns.items():
        _unique_camps = list({o[0] for o in _occurrences})
        if len(_unique_camps) < 2:
            continue  # single-campaign competitors are handled per-campaign
        if _negative_already_handled(_term, "", account_level=True, live_negatives=live_negatives):
            logger.info(f"  SKIPPED account-level competitor '{_term}' — already covered by live Google Ads negative")
            continue
        # Pick highest-spend campaign's resource for the API call
        _best_camp = max(_unique_camps, key=lambda c: _camp_spend_map.get(c, 0.0))
        _best_res = next((o[1] for o in _occurrences if o[0] == _best_camp and o[1]), "")
        if not _best_res:
            _best_res = next((o[1] for o in _occurrences if o[1]), "")
        if not _best_res:
            continue
        _total_cost = sum(o[2] for o in _occurrences)
        _camps_str = ", ".join(_unique_camps)
        aid = log_pending(
            operation="add_to_shared_negative_list",
            entity_type="keyword",
            entity_id=_term,
            entity_name=_term,
            before_state={"type": "competitor_search", "campaigns": _unique_camps, "cost": round(_total_cost, 2)},
            after_state={
                "keyword_text": _term,
                "match_type": "BROAD",
                "shared_list": _SHARED_LIST_NAME,
            },
            optimizer_run_id=run_id,
            reason=f"Competitor term in {len(_unique_camps)} campaigns ({_camps_str}) — adding to '{_SHARED_LIST_NAME}' shared list so it blocks all current and future campaigns. Cost: ${_total_cost:.2f}.",
            campaign_name="",   # account-level
            priority=10,        # high priority
            impact_estimate={"savings_30d_usd": round(_total_cost, 2)},
        )
        actions_pending += 1
        _acct_comp_logged += 1
        logger.info(f"  [ACCOUNT] [shared_list] '{_term}' (across {len(_unique_camps)} campaigns) → {aid[:8]}")

    if _acct_comp_logged:
        logger.info(f"Account-level: logged {_acct_comp_logged} cross-campaign competitor negatives")

    # Report
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
        "call_summary": {
            v["campaign_name"]: {
                "calls": v["calls"],
                "booked": v["booked_calls"],
                "confirmed_appts": v["confirmed_appts"],
            }
            for v in call_attribution.values()
        },
        "od_production_summary": od_production,
        "advisories": advisories,
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
    logger.info(f"  Total calls:          {summary.get('total_calls', 0)}")
    logger.info(f"  Total booked calls:   {summary.get('total_booked_calls', 0)}")
    logger.info(f"  Total confirmed appts:{summary.get('total_confirmed_appts', 0)}")
    logger.info(f"  Total production:     ${summary['total_production']}")
    logger.info(f"  Overall ROAS:         {summary['overall_roas']}x")
    logger.info(f"  Cost per lead:        ${summary['cost_per_lead']}")
    logger.info(f"  Cost per acquisition: ${summary.get('cost_per_acquisition', 'N/A')}")
    logger.info(f"  Keywords to pause:    {summary['keywords_to_pause']}")
    logger.info(f"  Keywords to bid up:   {summary['keywords_to_bid_up']}")
    logger.info(f"  Keywords to bid down: {summary['keywords_to_bid_down']}")
    logger.info(f"  New exact-match:      {summary['new_exact_match']}")
    logger.info(f"  New negatives:        {summary['new_negatives']}")
    logger.info(f"  Claude advisories:    {len(advisories)}")
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

    # ── Save run to optimizer memory ──────────────────────────────────────────
    try:
        # Collect top search terms for memory (highest cost, first 30)
        top_st = sorted(search_terms, key=lambda s: -s.get("cost", 0))[:30]
        top_st_slim = [
            {"term": s.get("search_term",""), "cost": round(s.get("cost",0),2),
             "clicks": s.get("clicks",0), "campaign": s.get("campaign","")}
            for s in top_st
        ]

        # Collect negatives added this run (rule-based, for negatives_added field)
        negatives_added = [
            st.get("search_term", "") for st in actions.get("new_negatives", [])
            if st.get("action_id")
        ]

        # Collect ALL recommendations from this run — rule-based + Claude per-campaign + account-level.
        # rec_id == gads_audit_log.action_id (same UUID string, different name in memory schema).
        # Pulling from gads_audit_log ensures we capture every operation type and every log_pending call.
        all_recs_for_memory = []
        try:
            from database import _conn as _mem_db_conn
            with _mem_db_conn() as _mc:
                _rec_rows = _mc.execute("""
                    SELECT action_id, operation, entity_name, reason
                    FROM gads_audit_log
                    WHERE optimizer_run_id = ?
                      AND execution_result = 'pending_approval'
                """, (run_id,)).fetchall()
            for _r in _rec_rows:
                all_recs_for_memory.append({
                    "rec_id": _r["action_id"],
                    "type": _r["operation"],
                    "target": _r["entity_name"],
                    "rationale": (_r["reason"] or "")[:200],
                    "status": "pending_approval",
                })
        except Exception as _recs_err:
            logger.warning(f"Could not pull recs from audit log for memory: {_recs_err}")

        run_entry = {
            "run_id": run_id,
            "run_date": datetime.now(timezone.utc).date().isoformat(),
            "trigger": trigger,
            "summary": {
                "total_spend": summary.get("total_spend", 0),
                "total_clicks": summary.get("total_clicks", 0),
                "total_leads": summary.get("total_leads", 0),
                "total_calls": summary.get("total_calls", 0),
                "total_production": summary.get("total_production", 0),
                "overall_roas": summary.get("overall_roas", 0),
                "cost_per_lead": summary.get("cost_per_lead", 0),
                "actions_pending": actions_pending,
            },
            "top_search_terms": top_st_slim,
            "negatives_added": negatives_added,
            "recommendations": all_recs_for_memory,
            "claude_notes": "\n".join(advisories[:10]) if advisories else "",
        }
        memory.append_run(run_entry)
        memory.save()
        logger.info(f"Optimizer memory updated: {len(all_recs_for_memory)} recs saved (run_id={run_id[:8]})")
    except Exception as _mem_err:
        logger.warning(f"Failed to save optimizer memory (non-fatal): {_mem_err}")

    # ── Save batch impact history ─────────────────────────────────────────────
    try:
        from database import _conn as _ih_conn
        from database import save_impact_history as _save_impact
        import json as _json
        # Aggregate impact estimates from all recs in this run
        _waste_usd = 0.0
        _impact_by_type: dict = {}
        _total_recs = 0
        with _ih_conn() as _ihc:
            _rows = _ihc.execute(
                "SELECT impact_estimate_json FROM gads_audit_log WHERE optimizer_run_id=?",
                (run_id,)
            ).fetchall()
        for _row in _rows:
            _total_recs += 1
            try:
                _ie = _json.loads(_row[0] or "{}")
                _s = float(_ie.get("savings_usd", 0) or _ie.get("savings_30d_usd", 0) or 0)
                _t = _ie.get("impact_type", "other")
                _impact_by_type[_t] = round(_impact_by_type.get(_t, 0) + _s, 2)
                if _ie.get("impact_type") == "waste_reduction" or "savings" in _ie:
                    _waste_usd += _s
            except Exception:
                pass

        # Build benchmark gap summary from current run's summary dict
        _gaps: dict = {}
        _cpl = summary.get("cost_per_lead", 0)
        if _cpl and float(_cpl) > 100:
            _gaps["cpl"] = {"current": _cpl, "target": 100, "gap": round(float(_cpl) - 100, 2), "unit": "usd"}
        _roas = summary.get("overall_roas", 0)
        if _roas and float(_roas) < 4:
            _gaps["roas"] = {"current": _roas, "target": 4.0, "gap": round(4.0 - float(_roas), 2), "unit": "multiple"}

        _save_impact(
            run_id=run_id,
            run_date=datetime.now(timezone.utc).date().isoformat(),
            estimated_waste_saved_usd=round(_waste_usd, 2),
            total_recs=_total_recs,
            benchmark_gaps_json=_json.dumps(_gaps),
            impact_by_type_json=_json.dumps(_impact_by_type),
        )
        logger.info(f"Impact history saved: waste_usd=${_waste_usd:.2f}, {_total_recs} recs")
    except Exception as _ih_err:
        logger.warning(f"Failed to save impact history (non-fatal): {_ih_err}")

    return report


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = optimize_campaign(dry_run=True)
    print(json.dumps(result, indent=2, default=str))
