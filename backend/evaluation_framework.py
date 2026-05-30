"""
GDC Account Evaluation Framework — Decision Tree Engine
========================================================
Implements the structured evaluation methodology from GDC_Account_Evaluation_Methodology.md.

Runs at optimizer startup and produces a scored, prioritized findings report that gets:
  1. Injected into every Claude prompt (per-campaign + account-level)
  2. Exposed via the get_account_evaluation() MCP tool for on-demand Cowork sessions

Entry points:
    from evaluation_framework import run_evaluation, build_evaluation_prompt_block
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ── Thresholds (tunable) ──────────────────────────────────────────────────────
QS_GREAT = 7            # QS ≥ 7 = Great
QS_OK_MIN = 4           # QS 4-6 = OK
WASTE_RATE_TARGET = 0.15        # target: <15% of spend on negative-classified queries
WASTE_RATE_WARN = 0.25          # warn above 25%
INVALID_CLICK_RATE_WARN = 0.10  # industry norm; above = flag
TOP_IS_TARGET = 0.20            # target: ≥20% top-of-page impression share
ABS_TOP_IS_TARGET = 0.10
QS_TARGET_AVG = 7.0             # account-wide impression-weighted QS target
MIN_SPEND_FOR_JUDGMENT = 20.0   # ignore AGs/keywords with <$20 spend
MIN_CLICKS_FOR_QS_FLAG = 10     # only flag QS on keywords with ≥10 clicks
DEAD_AG_THRESHOLD = 0           # 0 impressions = dead
AD_GROUP_WASTE_SPEND = 50.0     # AG with >$50 spend + 0 conv = flag
KEYWORD_DUPLICATE_FLAG = True   # flag duplicate keywords across AGs in same campaign
TOP_IS_ZERO_ALERT = True        # flag when top impression share = 0


# ── Data classes ─────────────────────────────────────────────────────────────
@dataclass
class Finding:
    """A single finding from the evaluation tree."""
    level: str          # "critical" | "warning" | "info"
    category: str       # "account" | "campaign" | "ad_group" | "keyword" | "rsa"
    subject: str        # campaign/AG/keyword name
    issue: str          # short description
    detail: str         # full explanation with numbers
    action: str         # recommended action (maps to optimizer operations where possible)
    operation: str      # optimizer op type: "add_negative_keyword" | "pause_keyword" | "pause_ad_group" | "increase_bid" | "claude_advisory" | "ad_copy_suggestion"
    priority: int       # 1 (highest) to 5 (lowest)
    est_monthly_savings: float = 0.0   # estimated monthly $ impact

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "category": self.category,
            "subject": self.subject,
            "issue": self.issue,
            "detail": self.detail,
            "action": self.action,
            "operation": self.operation,
            "priority": self.priority,
            "est_monthly_savings": round(self.est_monthly_savings, 2),
        }


@dataclass
class EvaluationReport:
    """Full structured evaluation report."""
    account_score: float = 0.0      # 0-100
    account_score_breakdown: dict = field(default_factory=dict)
    campaign_scores: dict = field(default_factory=dict)   # campaign_name → score 0-10
    ad_group_scores: dict = field(default_factory=dict)   # "campaign > ag" → score 0-10
    keyword_issues: list = field(default_factory=list)    # list of Finding
    findings: list[Finding] = field(default_factory=list)
    priority_actions: list[Finding] = field(default_factory=list)  # top 10 sorted by priority

    def to_dict(self) -> dict:
        return {
            "account_score": round(self.account_score, 1),
            "account_score_breakdown": self.account_score_breakdown,
            "campaign_scores": {k: round(v, 1) for k, v in self.campaign_scores.items()},
            "ad_group_scores": {k: round(v, 1) for k, v in self.ad_group_scores.items()},
            "findings_count": len(self.findings),
            "critical_count": sum(1 for f in self.findings if f.level == "critical"),
            "warning_count": sum(1 for f in self.findings if f.level == "warning"),
            "priority_actions": [f.to_dict() for f in self.priority_actions],
            "all_findings": [f.to_dict() for f in self.findings],
        }


# ── Main entry point ──────────────────────────────────────────────────────────
def run_evaluation(
    account_intelligence: dict | None = None,
    campaign_settings_raw: dict | None = None,
    keyword_perf: list | None = None,
    ad_group_performance: list | None = None,
    search_terms: list | None = None,
    keyword_click_share: list | None = None,
    lqi: dict | None = None,
    campaign_name_filter: str | None = None,   # if set, only evaluate this campaign
) -> EvaluationReport:
    """
    Run the full decision tree evaluation.
    All inputs are optional — the framework degrades gracefully if data is missing.
    """
    report = EvaluationReport()
    findings: list[Finding] = []

    acct = account_intelligence or {}
    kw_perf = keyword_perf or []
    ag_perf = ad_group_performance or []
    st = search_terms or []
    kw_cs = keyword_click_share or []
    cs_raw = campaign_settings_raw or {}

    # Build name-keyed settings
    name_to_settings: dict = {}
    for rn, cs in cs_raw.items():
        cn = (cs.get("campaign_name") or "").strip()
        if cn:
            name_to_settings[cn] = cs

    # ── Section 1: Account Health ─────────────────────────────────────────────
    findings += _evaluate_account_health(acct, kw_cs)
    account_score, score_breakdown = _score_account(acct, kw_cs)
    report.account_score = account_score
    report.account_score_breakdown = score_breakdown

    # ── Section 2: Campaign Evaluation ───────────────────────────────────────
    campaign_findings, campaign_scores = _evaluate_campaigns(
        name_to_settings, kw_perf, st, campaign_name_filter
    )
    findings += campaign_findings
    report.campaign_scores = campaign_scores

    # ── Section 3: Ad Group Evaluation ───────────────────────────────────────
    ag_findings, ag_scores = _evaluate_ad_groups(ag_perf, kw_cs, campaign_name_filter)
    findings += ag_findings
    report.ad_group_scores = ag_scores

    # ── Section 4: Keyword Strategy ──────────────────────────────────────────
    kw_findings = _evaluate_keywords(kw_perf, kw_cs, st, campaign_name_filter)
    findings += kw_findings

    # ── Section 5: Impression Share / Bidding ────────────────────────────────
    findings += _evaluate_impression_share(name_to_settings, campaign_name_filter)

    # Sort and prioritize
    findings.sort(key=lambda f: (f.priority, -f.est_monthly_savings))
    report.findings = findings
    report.priority_actions = findings[:10]

    return report


# ── Section 1: Account Health ─────────────────────────────────────────────────
def _evaluate_account_health(acct: dict, kw_cs: list) -> list[Finding]:
    findings: list[Finding] = []

    invalid_rate = acct.get("invalid_click_rate") or 0.0
    top_is = acct.get("top_impression_pct") or 0.0
    abs_top_is = acct.get("abs_top_impression_pct") or 0.0
    search_partners = acct.get("search_partners_pct") or 0.0

    # Invalid click rate
    if invalid_rate > INVALID_CLICK_RATE_WARN:
        # Use account intelligence spend if available, else omit savings estimate
        _acct_spend = acct.get("total_spend_30d") or acct.get("cost") or 0
        _est_savings = _acct_spend * invalid_rate * 0.5 if _acct_spend else 0
        findings.append(Finding(
            level="warning",
            category="account",
            subject="Account",
            issue=f"Invalid click rate {invalid_rate*100:.1f}% — double industry norm",
            detail=(
                f"Invalid click rate is {invalid_rate*100:.1f}% (industry norm <10%). "
                f"Likely causes: broad match keywords attracting bot traffic, "
                f"search partners network, or display expansion leakage."
            ),
            action=(
                "Review search partners performance separately. "
                "Check placement exclusions. Consider tightening geo and match types."
            ),
            operation="claude_advisory",
            priority=3,
            est_monthly_savings=_est_savings,
        ))

    # Top impression share = 0
    if TOP_IS_ZERO_ALERT and top_is == 0.0:
        findings.append(Finding(
            level="critical",
            category="account",
            subject="Account",
            issue="Top-of-page impression share = 0% — ads never appear above organic results",
            detail=(
                "Zero top-of-page impression share means every ad impression is below "
                "the organic fold. For emergency dentistry and implant searches, position "
                "is critical — patients in acute pain click the first result. "
                "This is the single biggest leverage point in the account."
            ),
            action=(
                "Increase CPC bids on high-intent keywords to achieve top-of-page position. "
                "Target: >20% top-of-page IS for Emergency and Implant campaigns. "
                "Use 'increase_bid' operation on keywords with system_serving_status=BELOW_FIRST_PAGE_BID."
            ),
            operation="increase_bid",
            priority=1,
            est_monthly_savings=0.0,
        ))

    # Search partners over-weighted
    if search_partners > 0.12:
        findings.append(Finding(
            level="warning",
            category="account",
            subject="Account",
            issue=f"Search partners consuming {search_partners*100:.1f}% of clicks — above 10% threshold for local dental",
            detail=(
                f"Search partners (non-Google search sites) account for {search_partners*100:.1f}% "
                f"of clicks. For a local dental practice, search partner traffic typically converts "
                f"at much lower rates than google.com searches. Review per-campaign search partner "
                f"performance and consider disabling for campaigns where partner traffic doesn't convert."
            ),
            action="Audit search partner performance per campaign. Disable on Emergency and Brand campaigns via set_search_partners.",
            operation="claude_advisory",
            priority=4,
            est_monthly_savings=0.0,
        ))

    # QS distribution from keyword_click_share
    if kw_cs:
        qs_scores = [k.get("historical_qs_avg") for k in kw_cs if k.get("historical_qs_avg")]
        if qs_scores:
            avg_qs = sum(qs_scores) / len(qs_scores)
            low_qs = [k for k in kw_cs if (k.get("historical_qs_avg") or 10) < QS_OK_MIN]
            if avg_qs < QS_TARGET_AVG:
                findings.append(Finding(
                    level="warning",
                    category="account",
                    subject="Account",
                    issue=f"Account average QS {avg_qs:.1f} — below target of {QS_TARGET_AVG}",
                    detail=(
                        f"Average Quality Score across {len(qs_scores)} keywords is {avg_qs:.1f}. "
                        f"Target is {QS_TARGET_AVG}+. {len(low_qs)} keywords below QS {QS_OK_MIN}. "
                        f"Low QS increases CPC and reduces ad rank even with higher bids. "
                        f"Primary driver: 95%+ of impressions are on QS 4-6 keywords."
                    ),
                    action=(
                        "Improve RSA relevance for low-QS keywords. "
                        "Ensure headlines contain the exact keyword text. "
                        "Fix landing page / ad alignment for keywords with BELOW_AVERAGE landing page score."
                    ),
                    operation="ad_copy_suggestion",
                    priority=2,
                    est_monthly_savings=0.0,
                ))

    return findings


def _score_account(acct: dict, kw_cs: list) -> tuple[float, dict]:
    """
    Account Score = (QS/10 × 30) + (top_IS/target × 25) + (invalid_click_health × 25) + (placeholder audit × 20)
    Returns (score, breakdown_dict)
    """
    qs_scores = [k.get("historical_qs_avg") for k in kw_cs if k.get("historical_qs_avg")]
    avg_qs = sum(qs_scores) / len(qs_scores) if qs_scores else 6.0

    top_is = acct.get("top_impression_pct") or 0.0
    invalid_rate = acct.get("invalid_click_rate") or 0.0

    qs_component = (avg_qs / 10.0) * 30
    is_component = min(top_is / TOP_IS_TARGET, 1.0) * 25
    invalid_health = max(0, 1 - (invalid_rate / INVALID_CLICK_RATE_WARN)) * 25
    audit_placeholder = 14.6  # from Optmyzr audit score 73/100 * 20

    score = qs_component + is_component + invalid_health + audit_placeholder
    breakdown = {
        "qs_score": round(qs_component, 1),
        "qs_avg": round(avg_qs, 2),
        "impression_share_score": round(is_component, 1),
        "top_impression_pct": round(top_is * 100, 1),
        "invalid_click_health_score": round(invalid_health, 1),
        "invalid_click_rate_pct": round(invalid_rate * 100, 1),
        "audit_score": round(audit_placeholder, 1),
        "total": round(score, 1),
        "interpretation": (
            "Critical" if score < 40 else
            "Needs Work" if score < 60 else
            "Adequate" if score < 75 else
            "Good"
        )
    }
    return round(score, 1), breakdown


# ── Section 2: Campaign Evaluation ───────────────────────────────────────────
def _evaluate_campaigns(
    name_to_settings: dict,
    kw_perf: list,
    search_terms: list,
    campaign_filter: str | None,
) -> tuple[list[Finding], dict]:
    findings: list[Finding] = []
    campaign_scores: dict = {}

    # Group keyword perf by campaign
    by_campaign: dict[str, list] = {}
    for kw in kw_perf:
        cn = (kw.get("campaign") or "").strip()
        if cn:
            by_campaign.setdefault(cn, []).append(kw)

    # Group search terms by campaign
    st_by_campaign: dict[str, list] = {}
    for st in search_terms:
        cn = (st.get("campaign_name") or st.get("campaign") or "").strip()
        if cn:
            st_by_campaign.setdefault(cn, []).append(st)

    for campaign_name, kws in by_campaign.items():
        if campaign_filter and campaign_filter.lower() not in campaign_name.lower():
            continue

        cs = name_to_settings.get(campaign_name, {})
        spend = sum(k.get("cost", 0) for k in kws)
        clicks = sum(k.get("clicks", 0) for k in kws)
        impressions = sum(k.get("impressions", 0) for k in kws)
        conversions = sum(k.get("conversions", 0) for k in kws)

        st_camp = st_by_campaign.get(campaign_name, [])
        negative_classified = [s for s in st_camp if s.get("classification") == "negative"]
        neg_spend = sum(s.get("cost", 0) for s in negative_classified)
        waste_rate = neg_spend / spend if spend > 0 else 0.0

        # Score this campaign
        score = _score_campaign(spend, clicks, conversions, waste_rate, cs, kws)
        campaign_scores[campaign_name] = score

        # Waste rate finding
        if spend > MIN_SPEND_FOR_JUDGMENT and waste_rate > WASTE_RATE_WARN:
            findings.append(Finding(
                level="critical" if waste_rate > 0.30 else "warning",
                category="campaign",
                subject=campaign_name,
                issue=f"High waste rate: {waste_rate*100:.0f}% of spend on negative-intent queries",
                detail=(
                    f"${neg_spend:.0f} of ${spend:.0f} spend ({waste_rate*100:.0f}%) is on "
                    f"queries classified as negative/off-intent. "
                    f"Top wasted terms: "
                    + ", ".join(f"'{s['search_term']}' (${s.get('cost',0):.0f})"
                                for s in sorted(negative_classified, key=lambda x: -x.get("cost", 0))[:5])
                ),
                action=(
                    f"Apply {len(negative_classified)} negative keywords to eliminate waste. "
                    f"Estimated monthly savings: ${neg_spend:.0f}."
                ),
                operation="add_negative_keyword",
                priority=1,
                est_monthly_savings=neg_spend,
            ))

        # High spend / zero conversions
        if spend > 200 and conversions == 0:
            findings.append(Finding(
                level="critical",
                category="campaign",
                subject=campaign_name,
                issue=f"${spend:.0f} spend with 0 conversions",
                detail=(
                    f"Campaign has spent ${spend:.0f} in 30 days with zero Google Ads conversions. "
                    f"Either conversion tracking is broken, intent mismatch is severe, "
                    f"or landing page is not converting."
                ),
                action="Check conversion tracking. Review intent match. Audit landing page.",
                operation="claude_advisory",
                priority=1,
                est_monthly_savings=spend,
            ))

        # Top impression share = 0 (per campaign)
        camp_top_is = (cs.get("search_impression_share") or 0) * 100
        camp_rank_lost = (cs.get("search_rank_lost_is") or 0) * 100
        if camp_rank_lost > 40 and spend > MIN_SPEND_FOR_JUDGMENT:
            findings.append(Finding(
                level="critical",
                category="campaign",
                subject=campaign_name,
                issue=f"Losing {camp_rank_lost:.0f}% of auctions due to low Ad Rank / bids",
                detail=(
                    f"search_rank_lost_is = {camp_rank_lost:.0f}%. Bids are too low to win "
                    f"most eligible auctions. Campaign IS: {camp_top_is:.0f}%. "
                    f"Budget lost IS: {(cs.get('search_budget_lost_is') or 0)*100:.0f}% — "
                    + ("budget is NOT the constraint, bids are."
                       if (cs.get("search_budget_lost_is") or 0) < 0.20
                       else "budget is also a constraint.")
                ),
                action="Increase bids on top keywords to achieve first-page / top-of-page position.",
                operation="increase_bid",
                priority=1,
                est_monthly_savings=0.0,
            ))

    return findings, campaign_scores


def _score_campaign(
    spend: float, clicks: int, conversions: float,
    waste_rate: float, cs: dict, kws: list
) -> float:
    """Score a campaign 0-10."""
    if spend < MIN_SPEND_FOR_JUDGMENT:
        return 5.0  # insufficient data

    # Intent match proxy: waste rate
    intent_score = max(0, 1 - (waste_rate / 0.50)) * 2.5  # 0-2.5

    # Conversion efficiency
    if conversions > 0:
        cpa = spend / conversions
        conv_score = max(0, 2 - (cpa / 500)) * 2.5  # $0 CPA = 2.5pts, $500+ = 0pts
    else:
        conv_score = 0.0

    # IS / rank
    rank_lost = cs.get("search_rank_lost_is") or 0
    is_score = max(0, 1 - rank_lost) * 2.5  # 0% rank loss = 2.5pts

    # Structural (non-zero budget, valid bidding)
    struct_score = 2.5 if cs.get("daily_budget_usd", 0) > 0 else 0.0

    return round(intent_score + conv_score + is_score + struct_score, 1)


# ── Section 3: Ad Group Evaluation ───────────────────────────────────────────
def _evaluate_ad_groups(
    ag_perf: list,
    kw_cs: list,
    campaign_filter: str | None,
) -> tuple[list[Finding], dict]:
    findings: list[Finding] = []
    ag_scores: dict = {}

    # Build QS lookup: (campaign, ag) → avg QS
    qs_by_ag: dict[tuple, float] = {}
    for kw in kw_cs:
        cn = (kw.get("campaign_name") or "").strip()
        ag = (kw.get("ad_group_name") or "").strip()
        qs = kw.get("historical_qs_avg")
        if cn and ag and qs:
            key = (cn, ag)
            existing = qs_by_ag.get(key)
            qs_by_ag[key] = (existing + qs) / 2 if existing else qs

    for ag in ag_perf:
        cn = (ag.get("campaign_name") or "").strip()
        ag_name = (ag.get("ad_group_name") or "").strip()
        if not cn or not ag_name:
            continue
        if campaign_filter and campaign_filter.lower() not in cn.lower():
            continue

        # Support both optimizer key names (impressions_30d) and MCP/DB key names (impressions)
        impressions = ag.get("impressions") or ag.get("impressions_30d") or 0
        clicks = ag.get("clicks") or ag.get("clicks_30d") or 0
        cost = ag.get("cost") or ag.get("cost_30d_usd") or 0
        conversions = ag.get("conversions") or ag.get("conversions_30d") or 0
        ctr = ag.get("ctr") or (clicks / impressions if impressions > 0 else 0)
        avg_cpc = ag.get("avg_cpc") or (cost / clicks if clicks > 0 else 0)
        tier = ag.get("tier") or ag.get("performance_tier") or 3
        # Normalize string tier values from optimizer ("weak", "cold", "strong") → int
        if isinstance(tier, str):
            tier = {"strong": 1, "moderate": 2, "weak": 3, "cold": 4}.get(tier.lower(), 3)

        subject = f"{cn} > {ag_name}"
        avg_qs = qs_by_ag.get((cn, ag_name))

        # Score this AG
        score = _score_ad_group(impressions, clicks, cost, conversions, avg_qs, tier, ag_name=ag_name)
        ag_scores[subject] = score

        # Dead ad group
        if impressions == 0 and clicks == 0:
            findings.append(Finding(
                level="warning",
                category="ad_group",
                subject=subject,
                issue="Dead ad group — 0 impressions, 0 clicks",
                detail=(
                    f"Ad group '{ag_name}' in '{cn}' has zero impressions and clicks in 30 days. "
                    f"Keywords may be disapproved, budgeted out, or all below first-page bid. "
                    f"Structural noise in the account."
                ),
                action="Pause or delete this ad group if no recent activity is expected.",
                operation="pause_ad_group",
                priority=4,
                est_monthly_savings=0.0,
            ))
            continue

        # Unnamed / unthemed AG
        if ag_name.lower() in ("ad group 1", "ad group 2", "ad group 3", "default"):
            findings.append(Finding(
                level="warning",
                category="ad_group",
                subject=subject,
                issue=f"Generic unnamed ad group '{ag_name}' — likely mixed intent",
                detail=(
                    f"Ad group name '{ag_name}' is a Google default, indicating it was "
                    f"not intentionally themed. Mixed-intent AGs produce weak QS and "
                    f"poor RSA relevance. This AG has {clicks} clicks, ${cost:.0f} spend."
                ),
                action="Audit keyword themes in this AG. Split into themed AGs or rename with clear intent.",
                operation="claude_advisory",
                priority=3,
                est_monthly_savings=0.0,
            ))

        # High spend + zero conversions
        if cost > AD_GROUP_WASTE_SPEND and conversions == 0 and tier >= 3:
            findings.append(Finding(
                level="critical" if cost > 200 else "warning",
                category="ad_group",
                subject=subject,
                issue=f"${cost:.0f} spend with 0 conversions — Tier {tier}",
                detail=(
                    f"Ad group '{ag_name}' ({cn}) has ${cost:.0f} spend, "
                    f"{clicks} clicks, and 0 conversions. "
                    f"Avg CPC ${avg_cpc:.2f}, CTR {ctr*100:.1f}%. "
                    f"Tier {tier} — strong pause candidate."
                ),
                action=(
                    "Pause this ad group if spend continues with no conversions. "
                    "Check if keywords are too generic or if landing page is mismatched."
                ),
                operation="pause_ad_group",
                priority=2 if cost > 200 else 3,
                est_monthly_savings=cost,
            ))

        # Low QS
        if avg_qs and avg_qs < QS_OK_MIN and clicks >= MIN_CLICKS_FOR_QS_FLAG:
            findings.append(Finding(
                level="warning",
                category="ad_group",
                subject=subject,
                issue=f"Low avg QS {avg_qs:.1f} — ad/keyword relevance mismatch",
                detail=(
                    f"Ad group '{ag_name}' has average QS {avg_qs:.1f} (target: {QS_GREAT}+). "
                    f"QS below {QS_OK_MIN} increases CPC and reduces impression share. "
                    f"Check if RSA headlines contain the exact keyword text and if the "
                    f"landing page matches the ad's promise."
                ),
                action="Rewrite RSA headlines to include keyword text. Fix landing page / ad alignment.",
                operation="ad_copy_suggestion",
                priority=3,
                est_monthly_savings=0.0,
            ))

    # Check for duplicate keywords across AGs in same campaign
    if KEYWORD_DUPLICATE_FLAG:
        findings += _find_duplicate_keywords(kw_cs, campaign_filter)

    return findings, ag_scores


def _score_ad_group(
    impressions: int, clicks: int, cost: float,
    conversions: float, avg_qs: float | None, tier: int,
    ag_name: str = "",
) -> float:
    """Score an ad group 1-10."""
    if impressions == 0:
        return 1.0  # dead

    qs_score = (avg_qs / 10.0) * 3.0 if avg_qs else 1.5  # 0-3 pts
    conv_score = min(conversions / 3.0, 1.0) * 3.0  # 0-3 pts (3+ conv = max)

    # Efficiency: CTR proxy
    ctr = clicks / impressions if impressions > 0 else 0
    ctr_score = min(ctr / 0.07, 1.0) * 2.0  # 0-2 pts (7%+ CTR = max)

    # Structural: named, has spend; penalise generic/unthemed AGs
    struct_score = 1.0 if cost > 0 else 0.0
    generic_penalty = -1.0 if ag_name.lower() in ("ad group 1", "ad group 2", "ad group 3", "default") else 0.0

    return max(1.0, min(10.0, round(qs_score + conv_score + ctr_score + struct_score + generic_penalty, 1)))


# ── Section 4: Keyword Strategy ───────────────────────────────────────────────
def _evaluate_keywords(
    kw_perf: list,
    kw_cs: list,
    search_terms: list,
    campaign_filter: str | None,
) -> list[Finding]:
    findings: list[Finding] = []

    # Find QS-1 keywords
    qs1_keywords = [
        k for k in kw_cs
        if (k.get("historical_qs_avg") or 10) <= 2
        and (campaign_filter is None or campaign_filter.lower() in (k.get("campaign_name") or "").lower())
    ]
    for kw in qs1_keywords:
        cn = kw.get("campaign_name", "")
        ag = kw.get("ad_group_name", "")
        kt = kw.get("keyword_text", "")
        qs = kw.get("historical_qs_avg", "?")
        lp_score = kw.get("landing_page_quality_score", "")
        creative_score = kw.get("creative_quality_score", "")

        lp_note = ""
        if lp_score == "BELOW_AVERAGE":
            lp_note = " Landing page score BELOW_AVERAGE — likely LP/ad mismatch."
        if creative_score == "BELOW_AVERAGE":
            lp_note += " Creative score BELOW_AVERAGE — RSA headlines lack keyword relevance."

        findings.append(Finding(
            level="critical",
            category="keyword",
            subject=f"{cn} > {ag} > {kt}",
            issue=f"QS {qs} — Google has effectively blacklisted this keyword+ad combination",
            detail=(
                f"Keyword '{kt}' in '{ag}' ({cn}) has QS {qs}. "
                f"QS 1-2 means Google rarely serves this keyword regardless of bid. "
                f"{lp_note} "
                f"This keyword is wasting budget and dragging account QS."
            ),
            action=(
                "Either rewrite RSA to include the exact keyword phrase, "
                "fix landing page alignment, or pause this keyword. "
                "Do not raise bid — QS problem won't be fixed by bidding more."
            ),
            operation="pause_keyword",
            priority=2,
            est_monthly_savings=0.0,
        ))

    # Find high-spend negative search terms
    negative_terms = [
        s for s in search_terms
        if s.get("classification") == "negative"
        and s.get("cost", 0) >= 5.0
        and (campaign_filter is None or campaign_filter.lower() in (s.get("campaign_name") or "").lower())
    ]
    for st in sorted(negative_terms, key=lambda x: -x.get("cost", 0))[:15]:
        cn = st.get("campaign_name", "")
        term = st.get("search_term", "")
        cost = st.get("cost", 0)
        reason = st.get("classification_reason", "Off-intent query")
        findings.append(Finding(
            level="warning",
            category="keyword",
            subject=f"{cn} > [{term}]",
            issue=f"${cost:.0f} wasted on negative-intent search term '{term}'",
            detail=f"Search term '{term}' in campaign '{cn}' spent ${cost:.0f} with 0 conversions. Reason: {reason}",
            action=f"Add '{term}' as negative keyword (PHRASE match) to '{cn}'.",
            operation="add_negative_keyword",
            priority=1 if cost > 20 else 2,
            est_monthly_savings=cost,
        ))

    # Find below-first-page keywords from bid estimates
    below_fp = [
        k for k in kw_cs
        if k.get("creative_quality_score") == "BELOW_AVERAGE"
        and (k.get("landing_page_quality_score") or "") != "ABOVE_AVERAGE"
        and (campaign_filter is None or campaign_filter.lower() in (k.get("campaign_name") or "").lower())
    ]
    for kw in below_fp[:5]:
        cn = kw.get("campaign_name", "")
        kt = kw.get("keyword_text", "")
        findings.append(Finding(
            level="warning",
            category="keyword",
            subject=f"{cn} > {kt}",
            issue=f"BELOW_AVERAGE creative quality — RSA not relevant to this keyword",
            detail=(
                f"Keyword '{kt}' has BELOW_AVERAGE creative quality score, meaning "
                f"Google predicts low CTR for current RSA headlines vs. this keyword. "
                f"The ad does not contain text that matches what the searcher typed."
            ),
            action="Add headlines to the RSA that contain the exact keyword phrase.",
            operation="ad_copy_suggestion",
            priority=3,
            est_monthly_savings=0.0,
        ))

    return findings


def _find_duplicate_keywords(kw_cs: list, campaign_filter: str | None) -> list[Finding]:
    """Find the same keyword appearing in multiple ad groups within the same campaign."""
    findings: list[Finding] = []

    from collections import defaultdict
    # {campaign_name → {keyword_text → [ad_group_names]}}
    kw_to_ags: dict = defaultdict(lambda: defaultdict(list))
    for kw in kw_cs:
        cn = (kw.get("campaign_name") or "").strip()
        ag = (kw.get("ad_group_name") or "").strip()
        kt = (kw.get("keyword_text") or "").strip().lower().strip("[]\"")
        mt = kw.get("match_type", "")
        if cn and ag and kt:
            if campaign_filter and campaign_filter.lower() not in cn.lower():
                continue
            kw_to_ags[cn][f"{kt} [{mt}]"].append(ag)

    for cn, kw_map in kw_to_ags.items():
        for kt, ags in kw_map.items():
            if len(ags) > 1:
                findings.append(Finding(
                    level="warning",
                    category="keyword",
                    subject=f"{cn} > '{kt}'",
                    issue=f"Duplicate keyword across {len(ags)} ad groups — internal auction conflict",
                    detail=(
                        f"Keyword '{kt}' appears in {len(ags)} ad groups in '{cn}': "
                        + ", ".join(f"'{a}'" for a in ags)
                        + ". These AGs compete against each other in every auction, "
                        f"inflating CPC and splitting QS signals."
                    ),
                    action=(
                        f"Keep keyword in only the highest-QS ad group. "
                        f"Pause or remove it from the others."
                    ),
                    operation="pause_keyword",
                    priority=2,
                    est_monthly_savings=0.0,
                ))

    return findings


# ── Section 5: Impression Share / Bidding ─────────────────────────────────────
def _evaluate_impression_share(
    name_to_settings: dict,
    campaign_filter: str | None,
) -> list[Finding]:
    findings: list[Finding] = []

    for cn, cs in name_to_settings.items():
        if campaign_filter and campaign_filter.lower() not in cn.lower():
            continue

        rank_lost = (cs.get("search_rank_lost_is") or 0)
        budget_lost = (cs.get("search_budget_lost_is") or 0)
        daily_budget = cs.get("daily_budget_usd") or 0
        bidding = cs.get("bidding_strategy_type") or ""

        # Smart bidding on tiny budget
        if (daily_budget > 0 and daily_budget < 15.0
                and bidding not in ("MANUAL_CPC", "MAXIMIZE_CLICKS", "")):
            findings.append(Finding(
                level="critical",
                category="campaign",
                subject=cn,
                issue=f"Smart bidding ({bidding}) on ${daily_budget:.0f}/day budget — algorithm will starve",
                detail=(
                    f"Campaign '{cn}' is using {bidding} with only ${daily_budget:.0f}/day. "
                    f"Smart bidding requires ~15+ conversions/month to optimize. "
                    f"At this budget it cannot gather enough auction signals. "
                    f"The algorithm will under-deliver and waste learning time."
                ),
                action="Switch to MANUAL_CPC to regain control of bids at this budget level.",
                operation="change_bid_strategy",
                priority=2,
                est_monthly_savings=0.0,
            ))

    return findings


# ── Prompt injection ──────────────────────────────────────────────────────────
def build_evaluation_prompt_block(
    report: EvaluationReport,
    campaign_name: str | None = None,
    max_findings: int = 8,
) -> str:
    """
    Format the evaluation report as a structured block for injection into Claude prompts.
    If campaign_name is provided, filters to findings relevant to that campaign.
    """
    if not report.findings:
        return ""

    lines = [
        "=== GDC ACCOUNT EVALUATION — DECISION TREE FINDINGS ===",
        "",
        f"ACCOUNT HEALTH SCORE: {report.account_score}/100 "
        f"({report.account_score_breakdown.get('interpretation', '?')})",
        f"  QS component: {report.account_score_breakdown.get('qs_score', 0)}/30 "
        f"(avg QS {report.account_score_breakdown.get('qs_avg', '?')})",
        f"  Impression share: {report.account_score_breakdown.get('impression_share_score', 0)}/25 "
        f"(top IS {report.account_score_breakdown.get('top_impression_pct', 0):.1f}%)",
        f"  Invalid click health: {report.account_score_breakdown.get('invalid_click_health_score', 0)}/25 "
        f"(rate {report.account_score_breakdown.get('invalid_click_rate_pct', 0):.1f}%)",
        "",
    ]

    # Campaign scores
    if report.campaign_scores:
        lines.append("CAMPAIGN SCORES (0-10):")
        for cn, score in sorted(report.campaign_scores.items(), key=lambda x: x[1]):
            flag = " ⚠" if score < 5 else " ✓" if score >= 7 else ""
            lines.append(f"  {cn}: {score}/10{flag}")
        lines.append("")

    # Filter findings for this campaign
    relevant = report.findings
    if campaign_name:
        relevant = [
            f for f in report.findings
            if not f.subject or campaign_name.lower() in f.subject.lower()
            or f.category == "account"
        ]

    # Top priority findings
    critical = [f for f in relevant if f.level == "critical"]
    warnings = [f for f in relevant if f.level == "warning"]
    shown = (critical + warnings)[:max_findings]

    if shown:
        lines.append("PRIORITY FINDINGS (apply these before generic optimizations):")
        for i, f in enumerate(shown, 1):
            savings_str = f" [est. ${f.est_monthly_savings:.0f}/mo savings]" if f.est_monthly_savings > 0 else ""
            lines.append(f"  [{f.level.upper()}] P{f.priority} | {f.category.upper()} | {f.subject}")
            lines.append(f"    Issue: {f.issue}{savings_str}")
            lines.append(f"    Action: {f.action}")
            lines.append(f"    Suggested operation: {f.operation}")
            lines.append("")

    lines += [
        "EVALUATION RULES FOR THIS RUN:",
        "1. Address CRITICAL findings before issuing other recommendations.",
        "2. For every 'pause_keyword' finding above, check resource names exist before emitting.",
        "3. 'add_negative_keyword' findings correspond to search terms already classified as negative.",
        "4. Do NOT contradict these findings with conflicting actions in the same run.",
        "5. If a finding conflicts with a lifecycle rule, lifecycle rule wins — emit claude_advisory instead.",
        "=== END EVALUATION FINDINGS ===",
        "",
    ]

    return "\n".join(lines)
