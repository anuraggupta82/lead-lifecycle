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
import sys
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from google.ads.googleads.client import GoogleAdsClient
from google.protobuf import field_mask_pb2
from config import get_settings

# ── MCP decisions injection ───────────────────────────────────────────────────
# Load prior Claude session decisions so the optimizer reasons with the benefit
# of strategic decisions made in Cowork sessions.
_MCP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "../../marketing-mcp")
if os.path.isdir(_MCP_PATH) and _MCP_PATH not in sys.path:
    sys.path.insert(0, _MCP_PATH)

try:
    from tools.decisions import get_decisions_for_campaign, get_global_decisions
    _DECISIONS_AVAILABLE = True
    logger_init = logging.getLogger(__name__)
    logger_init.info("MCP decisions module loaded — prior session decisions will be injected into prompts")
except ImportError:
    _DECISIONS_AVAILABLE = False
    def get_decisions_for_campaign(*args, **kwargs) -> str:  # type: ignore
        return ""
    def get_global_decisions(*args, **kwargs) -> str:  # type: ignore
        return ""
from database import get_all_leads

logger = logging.getLogger(__name__)


# ── Optimizer progress tracking ───────────────────────────────────────────────
# In-memory state written by optimize_campaign() and read by the progress endpoint.
# Thread-safe enough for single-process use (FastAPI + APScheduler share one process).

import time as _time_mod

# Ordered step definitions — (label, detail_template)
OPTIMIZER_STEPS = [
    ("Starting",            "Expiring stale recommendations..."),
    ("Syncing GAds Data",   "Pulling keywords, search terms, campaigns from Google Ads..."),
    ("Fetching Negatives",  "Loading existing negative keyword lists..."),
    ("Ad Performance",      "Fetching ad creative and ad group metrics..."),
    ("Classifying Terms",   "Running Haiku semantic classifier on search terms..."),
    ("Competitor Memory",   "Matching search terms against known competitor brands..."),
    ("Brand Negatives",     "Checking nearby practice brand stems against campaign negatives..."),
    ("Own-Brand Check",     "Ensuring GDC brand terms are negated on all acquisition campaigns..."),
    ("Brand Camp Check",    "Ensuring generic terms are negated on brand campaign..."),
    ("Rule-Based Engine",   "Applying rule-based optimization (pauses, bids, harvesting)..."),
    ("AI Per-Campaign",     "Calling Claude Opus for per-campaign recommendations..."),
    ("AI Account-Level",    "Calling Claude Opus for cross-campaign recommendations..."),
    ("Finalizing",          "Staging recommendations and updating optimizer memory..."),
]

_optimizer_progress: dict = {
    "running": False,
    "step_index": 0,
    "step_label": "",
    "step_detail": "",
    "total_steps": len(OPTIMIZER_STEPS),
    "pct": 0,
    "elapsed_sec": 0,
    "started_at": None,
    "campaign_context": "",   # e.g. "Emergency Dentistry (3/5)"
}


def _set_progress(step_index: int, campaign_context: str = "") -> None:
    """Update the global optimizer progress state. Called from optimize_campaign()."""
    global _optimizer_progress
    total = len(OPTIMIZER_STEPS)
    idx = max(0, min(step_index, total - 1))
    label, detail = OPTIMIZER_STEPS[idx]
    started = _optimizer_progress.get("started_at") or _time_mod.time()
    _optimizer_progress.update({
        "running": True,
        "step_index": idx,
        "step_label": label,
        "step_detail": detail,
        "total_steps": total,
        "pct": int(((idx + 1) / total) * 100),
        "elapsed_sec": int(_time_mod.time() - started),
        "started_at": started,
        "campaign_context": campaign_context,
    })


def _set_progress_done() -> None:
    """Mark the optimizer run as complete."""
    global _optimizer_progress
    started = _optimizer_progress.get("started_at") or _time_mod.time()
    _optimizer_progress.update({
        "running": False,
        "step_index": len(OPTIMIZER_STEPS),
        "step_label": "Complete",
        "step_detail": "All recommendations staged.",
        "total_steps": len(OPTIMIZER_STEPS),
        "pct": 100,
        "elapsed_sec": int(_time_mod.time() - started),
        "campaign_context": "",
    })


def get_optimizer_progress() -> dict:
    """Return a copy of the current progress state (called by the FastAPI endpoint)."""
    return dict(_optimizer_progress)


# ── Google Ads official rules — injected into every Claude prompt ─────────────
# Source: Google Ads Help + Advertising Policies (fetched May 2026)
GOOGLE_ADS_RULES = """
=== GOOGLE ADS HARD RULES (ENFORCE IN ALL AD COPY) ===

RESPONSIVE SEARCH ADS (RSA) — CHARACTER LIMITS:
- Headlines: max 30 characters each; provide 8–15 for best performance (min 3 required)
- Descriptions: max 90 characters each; provide 2–4 (4 recommended)
- Path1 / Path2: max 15 characters each (optional display URL segments)
- Korean/Japanese/Chinese: each character counts as 2 toward limits

RSA ASSET REQUIREMENTS:
- Minimum 3 headlines and 2 descriptions to launch
- Recommended 8–10+ headlines and all 4 descriptions for "Excellent" ad strength
- Each headline and description must be meaningfully different (no repetition within or across assets)
- Improving ad strength from Poor → Excellent averages +15% clicks and conversions

CAPITALIZATION:
- No ALL CAPS words (FLOWERS, FREE, BEST) unless it is a registered trademark
- No alternating caps (FlOwErS) or spaced letters (F.L.O.W.E.R.S)
- Title Case for headlines is acceptable and recommended
- Brand names and trademarks may use their official capitalization

PUNCTUATION & SYMBOLS:
- No gimmicky punctuation or symbols (!!!, f-r-e-e, fl@wers)
- No phone numbers in ad text (Google policy: PHONE_NUMBER_IN_AD_TEXT violation)
- Standard punctuation (periods, commas, hyphens, apostrophes) is fine
- Exclamation marks allowed once per description; not allowed in headlines

PROHIBITED CONTENT:
- False, misleading, or exaggerated claims ("cure", "guaranteed results", "best in the world")
- Clickbait or sensationalist language
- Overly generic filler ("Click here", "Buy products here", "Best service")
- Repetition of words or phrases within a headline, across headlines, or across descriptions
- Price claims or urgency tactics that are not accurate and verifiable

LANDING PAGE & URL:
- Display URL domain must match final URL domain exactly
- Landing page must have original, useful content — not just ads or redirects
- Every ad must point to a functional, relevant landing page

PATH FIELD BEST PRACTICES:
- Path fields show as: domain.com/path1/path2 in the ad
- Use concise, keyword-relevant slugs (e.g. "dentures", "grafton-ma", "implants")
- Hyphens allowed; no spaces; lowercase preferred
- HARD LIMIT: 15 characters each — never exceed this

AD STRENGTH TARGETS:
- Always aim for "Good" minimum, "Excellent" preferred before recommending launch
- To reach Excellent: maximize headline/description count, ensure diversity, include keywords

OPERATIONS REFERENCE (for replace_ad / ad_copy_suggestion):
- replace_ad: requires old_ad_group_ad_resource, new_headlines (list), new_descriptions (list),
  final_url (https://...), optional path1 (≤15 chars), optional path2 (≤15 chars)
- Never include phone numbers in any headline or description field
- Never repeat a phrase across multiple headlines or descriptions
=== END GOOGLE ADS HARD RULES ===
"""

# ── Campaign intent boundaries — injected into every per-campaign Claude prompt ──
# Each campaign type has a defined patient intent it is supposed to capture.
# The AI must enforce these boundaries when evaluating search terms and keywords.
CAMPAIGN_INTENT_RULES = """
=== CAMPAIGN INTENT BOUNDARIES (MANDATORY) ===

Every campaign at GDC targets a specific patient intent. A search term that does not match
the campaign's intent is OFF-INTENT and must be negated — even if it generated a lead,
even if another campaign is not running, even if the term is broadly dental-related.

This prevents budget dilution and ensures each campaign reaches the right patient.

CAMPAIGN TYPE DEFINITIONS AND INTENT BOUNDARIES:

EMERGENCY campaigns (name contains: emergency, urgent, same day, toothache, broken tooth):
  PERMITTED INTENTS: acute pain, urgent dental need, same-day appointment, broken/cracked tooth,
                     lost filling, abscess, swollen face, tooth knocked out, dental trauma
  PERMITTED SIGNALS: "emergency", "urgent", "same day", "same-day", "toothache", "tooth pain",
                     "dental pain", "pain", "broken tooth", "cracked tooth", "chipped tooth",
                     "chip", "abscessed", "abscess", "swollen", "knocked out", "after hours",
                     "open now", "open today", "open late", "weekend dentist", "24 hour dentist",
                     "bleeding", "bleeding gum", "tooth infection", "lost filling", "broken crown",
                     "dentist tonight", "dentist today", "dentist asap", "asap", "tonight", "today"
  OFF-INTENT (negate): generic dentist searches ("dentists near me", "dentist in [city]",
                        "dentist worcester", "dentist grafton", "[city] dentist", "[city] dental",
                        "family dentist", "dental cleaning", "teeth cleaning", "new patient",
                        "dental checkup", "affordable dentist", "best dentist", "local dentist",
                        "accepting new patients", "establish care") — these patients are shopping
                        for a regular dentist, not seeking emergency care.
  RULE: A patient searching "dentist worcester" or "dentists near me" has general/navigational
        intent. They belong in the General Dentistry campaign. Spending emergency budget on
        them is waste — they will not book a same-day emergency appointment.
        CONVERSION = patient scheduled a same-day/next-day appointment. A click that did not
        produce a booking is evidence of intent mismatch, not just a performance issue.

GENERAL DENTISTRY campaigns (name contains: general dentistry, new patients, general dental):
  PERMITTED INTENTS: finding a new dentist, routine care, checkups, cleanings, family dentistry,
                     new patient specials, affordable dental care, preventive care
  PERMITTED SIGNALS: "dentist near me", "dentist in [city]", "family dentist", "new patient",
                     "dental cleaning", "teeth cleaning", "affordable dentist", "accept new patients"
  OFF-INTENT (negate): emergency/urgent terms, specialty-specific terms (implants, veneers,
                        orthodontics) — those have dedicated campaigns.

IMPLANTS/DENTURES campaigns (name contains: implant, denture, all-on-4):
  PERMITTED INTENTS: tooth replacement research, implant cost, implant procedure, denture fitting,
                     all-on-4, snap-on dentures, full arch restoration
  PERMITTED SIGNALS: "dental implant", "implant cost", "tooth implant", "denture", "all on 4",
                     "missing teeth", "tooth replacement", "implant dentist"
  OFF-INTENT (negate): generic dentist searches, emergency searches, insurance searches

COSMETIC/INVISALIGN campaigns (name contains: cosmetic, veneer, invisalign, whitening, smile):
  PERMITTED INTENTS: smile improvement, teeth straightening, cosmetic enhancement
  PERMITTED SIGNALS: "invisalign", "clear aligners", "veneers", "teeth whitening",
                     "smile makeover", "cosmetic dentist", "teeth straightening"
  OFF-INTENT (negate): emergency terms, insurance terms, generic "dentist near me"

BRAND campaigns (name contains: grafton dental, brand, branded):
  PERMITTED INTENTS: name-based navigation only — patients who already know Grafton Dental Care
  PERMITTED SIGNALS: "grafton dental care", "dr anurag gupta", "dr gupta grafton",
                     "graftondentalcare.com", "gdc dental"
  OFF-INTENT (negate): everything that is not a variation of the practice name or doctor name.
                        Generic dental searches, service searches, competitor searches — all off-intent.

ENFORCEMENT RULE:
When reviewing search_terms for a campaign, check EACH term against the campaign's PERMITTED SIGNALS.

For EMERGENCY campaigns — DEFAULT-DENY posture:
  If the term does NOT contain at least one urgency signal:
    emergency, urgent, same day, same-day, toothache, tooth pain, dental pain, pain,
    broken tooth, cracked tooth, chipped tooth, chip, abscess, abscessed, swollen,
    knocked out, open now, open today, open late, after hours, 24 hour, weekend dentist,
    bleeding, tooth infection, lost filling, broken crown, asap, tonight, today
  → it is OFF-INTENT. Recommend add_negative_keyword regardless of cost or clicks.
  Even ONE impression of a navigational term wastes emergency budget on a patient who
  does not need same-day care. Also negate bare city-dentist patterns:
  "dentist [city]", "[city] dentist", "[city] dental", "dental [city]".

For ALL other campaign types:
  If a term does not match the campaign's PERMITTED SIGNALS and has ≥ 1 click:
  → Recommend add_negative_keyword (PHRASE match unless it is a very specific term, then EXACT)

In all cases:
  → Reason must explicitly state: "Off-intent for [campaign_type] campaign: '[term]' signals
    [inferred intent] which belongs in [correct campaign type] campaign."
  → Do NOT wait for zero-conversion data to recommend an intent negative. Intent is determined by
    the SEARCH QUERY MEANING, not by whether it happened to convert.
  → Conversion = patient scheduled an appointment. A click that did not produce a booking is
    evidence of intent mismatch, not just a performance issue.

=== END CAMPAIGN INTENT BOUNDARIES ===
"""


# ── Campaign lifecycle rules — injected into every per-campaign Claude prompt ──
LIFECYCLE_RULES = """
=== CAMPAIGN LIFECYCLE RULES (MANDATORY — DO NOT VIOLATE) ===

The "lifecycle" field in the campaign data contains: stage, days_since_launch, in_learning_period.

STAGE = new (days_since_launch <= 30) OR stage = unknown:
  ALLOWED operations: add_negative_keyword, claude_advisory, add_asset,
                      increase_bid (with strict conditions below),
                      change_bid_strategy → MANUAL_CPC (with strict conditions below)
  FORBIDDEN: decrease_bid, pause_keyword, add_exact_keyword,
             replace_ad, pause_ad_group, update_geo_targeting,
             change_budget, change_match_type, ad_copy_suggestion,
             change_bid_strategy to anything other than MANUAL_CPC
  REASON: Google's algorithm needs 14–30 days of uninterrupted data collection to
          optimize delivery. Structural changes and budget/strategy changes reset the
          learning phase. However, if a campaign launched on a smart bidding strategy
          with a budget too thin to feed it, the smart bidder starves and collects no
          data — which is worse than switching to Manual CPC temporarily.
  add_asset NOTE: Callouts and structured snippets (add_asset) are ALWAYS allowed at
          any lifecycle stage — they are zero-cost, do NOT reset the learning phase,
          and improve ad real estate and CTR. ALWAYS recommend add_asset if callouts
          or snippets are missing, regardless of stage.
  increase_bid RULE DURING NEW STAGE — two mandatory branches:

  BRANCH A — MUST emit increase_bid (not claude_advisory) when ALL of the following are true:
    1. search_rank_lost_is > 0.40 (losing >40% of auctions due to low rank, not budget)
    2. search_budget_lost_is < 0.20 (not budget-constrained — room exists to bid higher)
    3. The keyword has >= 10 impressions (enough signal that it is entering auctions)
    4. Set new_bid_micros = round(current_bid_micros * 1.08) — exactly +8%, no more
    5. Include current_bid_micros, search_rank_lost_is, search_budget_lost_is, impressions fields
    DO NOT downgrade to claude_advisory when all 4 conditions above are met. EMIT increase_bid.

  BRANCH B — emit claude_advisory ONLY when at least one condition is NOT met:
    Use reason: "LIFECYCLE_ADVISORY: Bid increase deferred — [state which condition failed].
    After day 30, consider increasing if rank lost IS remains above 40%."

  change_bid_strategy → MANUAL_CPC RULE DURING NEW STAGE:
  MUST emit change_bid_strategy (bid_strategy: "MANUAL_CPC") when ALL of the following are true:
    1. bidding_strategy_type is NOT MANUAL_CPC (i.e. campaign is on Maximize Clicks, Maximize Conversions, Target CPA, etc.)
    2. search_rank_lost_is > 0.40 (losing >40% of auctions — smart bidding has nothing to optimize)
    3. daily_budget_usd < 15.0 (budget too thin to feed smart bidding algorithm)
    REASON: Smart bidding on a sub-$15/day budget starves — Google can't gather enough auction
    signals to make intelligent decisions. Manual CPC lets us set competitive baseline bids
    immediately and gather real impression data during the learning window.
    Required op fields: bid_strategy:"MANUAL_CPC", campaign_resource, search_rank_lost_is (float),
    daily_budget_usd (float, from campaign_settings). DO NOT include target_cpa_micros or target_roas.

  ACTION: For all other changes you'd normally make, emit a claude_advisory describing
          WHAT you would do and WHEN (e.g. "After day 30, consider...").
          Prefix that advisory reason with "LIFECYCLE_ADVISORY: ".

STAGE = ramping (31 <= days_since_launch <= 90):
  ALLOWED: add_negative_keyword, increase_bid, decrease_bid, pause_keyword,
           ad_copy_suggestion, replace_ad, claude_advisory, update_geo_targeting,
           change_budget, change_match_type, add_asset
  FORBIDDEN:
    - change_bid_strategy UNLESS gads_conversions_30d >= 15 (insufficient conversion history)
    - add_exact_keyword (no match-type expansion until campaign is mature and bidding is stable)
    - pause_ad_group UNLESS the ad group has >= 50 clicks AND 0 conversions (clear waste)
  REASON: Enough data for tactical changes; not enough for Smart Bidding strategy switches.

STAGE = mature (days_since_launch > 90):
  All operations allowed — apply full optimization judgment.

WHEN REFUSING AN OPERATION due to lifecycle stage, emit a claude_advisory with
reason starting with "LIFECYCLE_BLOCKED: [operation_name] — " followed by the
reason you would have given if the campaign were mature.
=== END LIFECYCLE RULES ===
"""

LEARNING_PHASE_RULES = """
=== LEARNING PHASE DIAGNOSTIC RULES (campaigns age < 30 days) ===

RULE 1 — DIAGNOSE BEFORE PRESCRIBING:
Before recommending any change, identify the primary loss reason from campaign_settings:
- search_rank_lost_is > 0.40 → BID/QUALITY PROBLEM — fix bids or bidding strategy first
- search_budget_lost_is > 0.30 → BUDGET PROBLEM — raising budget is warranted
- Both < 0.20 → IMPRESSION QUALITY PROBLEM — review keywords and targeting
NEVER recommend a budget increase (change_budget) when search_budget_lost_is < 0.20.

RULE 2 — BIDDING STRATEGY BY BUDGET TIER:
- daily_budget_usd < 15 → MANUAL_CPC required. Smart bidding starves at this scale.
- daily_budget_usd 15–50 AND conversions_30d < 15 → MANUAL_CPC or MAXIMIZE_CLICKS
- daily_budget_usd > 50 AND conversions_30d 15–30 → MAXIMIZE_CONVERSIONS eligible
- daily_budget_usd > 50 AND conversions_30d >= 30 → TARGET_CPA eligible

RULE 3 — ONE RESET PER 7-DAY WINDOW:
Bid strategy changes, budget swings >20%, and geo target changes all reset Google's
learning clock. Emit at most ONE reset-class operation per optimizer run during learning.
If a change_bid_strategy op is already in your recommendations, do NOT also recommend
change_budget or update_geo_targeting in the same run.

RULE 4 — CONCENTRATE SPEND ON SIGNAL, DON'T EXPLORE:
For campaigns with daily_budget_usd < 15:
- Emit claude_advisory recommending pause of keywords with impressions >= 50 AND clicks == 0
  (reason: "CONCENTRATION: keyword has X impressions but 0 clicks — recommend pausing after day 30")
- Emit claude_advisory recommending pause of keywords with impressions >= 200 AND conversions == 0
- DO NOT recommend adding new keywords or match type expansion
- DO NOT flag keywords with impressions < 30 (insufficient signal to judge)
Concentrate the limited budget on keywords with any positive signal.

RULE 5 — DO NOT TREAT AD COPY AS A BID FIX:
If search_rank_lost_is > 0.40, the campaign is losing auctions due to low bids, NOT ad
quality. Ad copy changes (ad_copy_suggestion, replace_ad) will not meaningfully improve
Ad Rank at this scale. Defer ad copy work until bids are fixed and rank_lost_is < 0.30.

RULE 6 — BUDGET DRIFT IS A RED FLAG:
If actual monthly spend is severely below planned budget AND search_rank_lost_is > 0.40,
emit ONE claude_advisory explaining the cause: bids are too low to enter enough auctions
to spend the budget. Label the reason: "BUDGET_DRIFT_ALERT: [explanation]".
Do NOT silently accept severe budget under-delivery.

RULE 7 — N=1 CONVERSION IS A HYPOTHESIS, NOT A SIGNAL:
If a keyword has exactly 1 conversion, treat it as promising but unconfirmed.
- DO concentrate bids on it (it is the best signal available)
- DO NOT use it as justification for TARGET_CPA or aggressive budget reallocation
- DO surface it in the reason field so the human can validate

=== END LEARNING PHASE DIAGNOSTIC RULES ===
"""

# ── Valid structured snippet headers (Google's fixed approved list) ───────────
# These are the EXACT strings Google Ads accepts for structured_snippet_asset.header
# (case-sensitive). Submitting any other string → STRUCTURED_SNIPPET_INVALID_HEADER error.
VALID_SNIPPET_HEADERS = {
    "Amenities", "Brands", "Courses", "Degree programs", "Destinations",
    "Featured hotels", "Insurance coverage", "Models", "Neighborhoods",
    "Service catalog", "Shows", "Styles", "Types",
}
# For dental practices the most useful are:
#   "Service catalog" — list treatments offered
#   "Insurance coverage" — list accepted insurances
#   "Types" — sub-types of a service (e.g. implant types)


# ── Negative keyword signals (module-level so all functions can use them) ─────

_HARD_NEGATIVES = [
    "dental school", "dental schools",        # looking for student-rate work
    "diy", "home remed",                      # not seeking professional care
    "complaint", "lawsuit", "malpractice",    # legal research
    "salary", "job", "career", "how to become",  # career searches
]
_SOFT_NEGATIVES = []

# Services GDC does NOT offer at all — terms containing these should become
# account-level negatives, never suggested as new exact keywords.
_OUT_OF_SCOPE_SERVICES = [
    "oral surgeon", "oral surgery",
    "wisdom teeth removal", "wisdom tooth removal", "wisdom teeth extraction",
    "tooth extraction near", "extractions near",
    "orthodontist", "orthodontists",   # GDC does clear aligners but is NOT an orthodontist
    "braces", "metal braces", "ceramic braces",
    "invisalign",                      # competitor aligner brand
    "periodontist", "periodontists",
    "endodontist", "endodontists",
    "prosthodontist",
    "pediatric dentist", "kids dentist", "children dentist", "children's dentist",
    "dental school", "dental college",
    "medicaid dentist", "masshealth dentist", "medicaid dental", "masshealth dental",
    "medicaid", "masshealth", "mass health", "chip dental",
]

# Terms that could be valid — but ONLY in the context of a clear aligner campaign.
# The rule-based harvester should NOT add these as account-level keywords.
_ALIGNER_ONLY_TERMS = [
    "orthodontics", "orthodontic",
    "clear aligner", "clear aligners",
    "teeth straightening", "teeth alignment",
    "aligner",
]


def _is_out_of_scope(term: str) -> str:
    """Returns a reason string if the term is out of scope for GDC, else empty string."""
    t = term.lower()
    for signal in _OUT_OF_SCOPE_SERVICES:
        if signal in t:
            return f"Out-of-scope service: '{signal}' — GDC does not offer this"
    return ""


def _is_aligner_only(term: str) -> bool:
    """Returns True if this term should only be added in a clear aligner campaign context."""
    t = term.lower()
    return any(sig in t for sig in _ALIGNER_ONLY_TERMS)


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

# ── Geo default profiles by campaign type ─────────────────────────────────────────────────
# Injected into Claude prompt as "geo_defaults" so the AI knows the ideal state for each
# campaign type before analyzing actual location performance data.
# Safety constraints: EMERGENCY max ≤ 10 mi; IMPLANTS min ≥ 15 mi.
_GEO_DEFAULTS_BY_TYPE: dict[str, dict] = {
    "EMERGENCY": {
        "max_radius_miles": 10,
        "target_radius_miles": 7,
        "rationale": "Emergency dental patients cannot travel far; high urgency = low radius tolerance.",
        "excluded_zips": ["01608", "01610"],  # downtown Worcester — too competitive, low conversion
        "must_include_zips": ["01519", "01536", "01545", "01581", "01527", "01590", "01772"],
    },
    "GENERAL": {
        "max_radius_miles": 15,
        "target_radius_miles": 12,
        "rationale": "General dentistry patients will travel up to ~12 miles for a trusted practice.",
        "excluded_zips": [],
        "must_include_zips": [],
    },
    "ELECTIVE": {  # cosmetic, veneers, whitening, invisalign
        "max_radius_miles": 30,
        "target_radius_miles": 22,
        "rationale": "Elective patients research extensively and travel for quality; wider radius justified.",
        "excluded_zips": [],
        "must_include_zips": ["01701", "01702", "01746", "01748"],  # Framingham, Holliston, Hopkinton
    },
    "IMPLANTS": {  # implants, all-on-4, dentures
        "max_radius_miles": 50,
        "target_radius_miles": 40,
        "min_radius_miles": 15,  # safety: never shrink below 15 mi
        "rationale": "High-value implant patients travel 40+ miles for the right specialist and price.",
        "excluded_zips": [],
        "must_include_zips": [],
    },
    "BRAND": {
        "max_radius_miles": 30,
        "target_radius_miles": 25,
        "rationale": "Brand/awareness campaigns cast a wider net; name recognition is the goal.",
        "excluded_zips": [],
        "must_include_zips": [],
    },
}


def _geo_defaults_for_campaign(campaign_name: str) -> dict:
    """
    Return the geo default profile for a campaign.
    Delegates to _classify_campaign (single source of truth) so that geo type
    classification stays in sync with excellence target and LQI signal classification.
    """
    # _classify_campaign is defined below; forward-reference OK at call time.
    ctype = _classify_campaign(campaign_name)  # "emergency"|"implants"|"invisalign"|"cosmetic"|"brand"|"general"
    _TYPE_MAP = {
        "emergency":  "EMERGENCY",
        "implants":   "IMPLANTS",
        "invisalign": "ELECTIVE",
        "cosmetic":   "ELECTIVE",
        "brand":      "BRAND",
        "general":    "GENERAL",
    }
    return _GEO_DEFAULTS_BY_TYPE[_TYPE_MAP.get(ctype, "GENERAL")]


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


def _build_institutional_memory_note(campaign: str = "") -> str:
    """
    Load the full optimizer_memory table + recent rejection history and format
    as a structured text block to inject into every Claude prompt. This gives
    Claude the complete institutional knowledge of what has been approved,
    rejected, and why — so it can make better recommendations each run.

    campaign: scopes rejection_pattern entries to this campaign (plus global ones).
              Pass "" for account-level prompts.
    """
    try:
        from database import get_optimizer_memory
        all_entries = get_optimizer_memory(active_only=True)  # ALL categories
    except Exception as _e:
        import logging as _log
        _log.getLogger(__name__).warning(f"[institutional_memory] load failed: {_e}")
        return ""

    if not all_entries:
        return ""

    lines = ["\n\n=== INSTITUTIONAL MEMORY (read every run — apply all rules) ==="]

    camp_lower = (campaign or "").lower()

    # Group by category for readability
    by_cat: dict = {}
    for e in all_entries:
        cat = e.get("category", "general")
        by_cat.setdefault(cat, []).append(e)

    def _is_in_scope(e: dict) -> bool:
        """Return True if entry is global or belongs to the current campaign."""
        entry_camp = (e.get("campaign") or "").lower()
        return not entry_camp or entry_camp == camp_lower

    # 1. Keyword overrides — never pause / always pause (scoped to global + this campaign)
    if "keyword_override" in by_cat:
        scoped = [e for e in by_cat["keyword_override"] if _is_in_scope(e)]
        if scoped:
            lines.append("\nKEYWORD OVERRIDES (permanent rules — never violate):")
            for e in scoped:
                scope = f" [{e['campaign']}]" if e.get("campaign") else " [global]"
                lines.append(f"  • {e['key']}: {e['value']} — {e['reason']}{scope}")

    # 2. Term classifications — good / negative / irrelevant (scoped, capped at 50 most recent)
    if "term_classification" in by_cat:
        scoped = [e for e in by_cat["term_classification"] if _is_in_scope(e)]
        if scoped:
            lines.append("\nTERM CLASSIFICATIONS (treat these search terms accordingly):")
            for e in scoped[:50]:
                scope = f" [{e['campaign']}]" if e.get("campaign") else " [global]"
                lines.append(f"  • \"{e['key']}\": {e['value']} — {e['reason']}{scope}")

    # 3. Campaign rules (scoped to global + this campaign)
    if "campaign_rule" in by_cat:
        scoped = [e for e in by_cat["campaign_rule"] if _is_in_scope(e)]
        if scoped:
            lines.append("\nCAMPAIGN RULES:")
            for e in scoped:
                scope = f" [{e['campaign']}]" if e.get("campaign") else " [global]"
                lines.append(f"  • {e['key']} = {e['value']} — {e['reason']}{scope}")

    # 4. Rejection patterns — most actionable for avoiding repeated bad suggestions
    rejection_entries = by_cat.get("rejection_pattern", [])
    if rejection_entries:
        # Filter to global + this campaign's rejections
        camp_lower = (campaign or "").lower()
        relevant = [
            e for e in rejection_entries
            if not e.get("campaign") or (e.get("campaign") or "").lower() == camp_lower
        ]
        if relevant:
            lines.append(
                "\nADMIN REJECTION PATTERNS (do NOT re-suggest these — the admin has explicitly rejected them):"
            )
            for e in relevant[:30]:  # cap at 30 to avoid prompt bloat
                scope = f" [campaign: {e['campaign']}]" if e.get("campaign") else " [all campaigns]"
                lines.append(f"  ✗ {e['key']}: {e['reason']}{scope}")

    # 5. Reclassification patterns — admin has moved recs between campaign/account level
    reclass_entries = by_cat.get("reclassification_pattern", [])
    if reclass_entries:
        relevant_reclass = [
            e for e in reclass_entries
            if not e.get("campaign") or (e.get("campaign") or "").lower() == camp_lower
        ]
        if relevant_reclass:
            # Determine context: are we in a per-campaign call or account-level call?
            is_account_level_call = not camp_lower
            lines.append(
                "\nRECLASSIFICATION PREFERENCES (admin has explicitly moved these recs to the correct level):"
            )
            for e in relevant_reclass[:20]:
                preferred_level = "ACCOUNT LEVEL" if e.get("value") == "prefer_account_level" else "CAMPAIGN LEVEL"
                scope = f" [campaign: {e['campaign']}]" if e.get("campaign") else " [global]"
                if preferred_level == "ACCOUNT LEVEL" and not is_account_level_call:
                    # Per-campaign run: suppress this rec — it belongs at account level
                    lines.append(f"  ✗ DO NOT emit {e['key']} at campaign level — admin moved it to ACCOUNT LEVEL. {e['reason']}{scope}")
                elif preferred_level == "CAMPAIGN LEVEL" and is_account_level_call:
                    # Account-level run: suppress this rec — it belongs per-campaign
                    lines.append(f"  ✗ DO NOT emit {e['key']} account-wide — admin scoped it to CAMPAIGN LEVEL. {e['reason']}{scope}")
                else:
                    # Same level as current run — emit normally at this level
                    lines.append(f"  → Emit {e['key']} at {preferred_level}. {e['reason']}{scope}")
            lines.append(
                "  Apply the above before generating recommendations — these are hard admin directives, not suggestions."
            )

    # 6. General notes (capped at 30 most recent)
    if "general" in by_cat:
        lines.append("\nGENERAL NOTES:")
        for e in by_cat["general"][:30]:
            lines.append(f"  • {e['key']}: {e['reason']}")

    lines.append(
        "\nAPPLY ALL OF THE ABOVE before generating recommendations. "
        "Rejection patterns are hard blocks — the admin has seen the data and made a deliberate choice. "
        "Do not re-suggest rejected actions even if the performance data looks compelling."
    )

    return "\n".join(lines)


def _build_mcp_decisions_note(campaign: str | None = None) -> str:
    """
    Load prior Claude session decisions from the MCP decisions system and format
    as a context block for injection into every optimizer Claude prompt.

    campaign: campaign name to scope decisions. Pass None for account-level prompt.

    This function is non-fatal — if the MCP module is unavailable or the DB
    has no decisions, it returns an empty string and the optimizer continues normally.
    """
    if not _DECISIONS_AVAILABLE:
        return ""

    try:
        parts = []

        # Per-campaign decisions
        if campaign:
            camp_decisions = get_decisions_for_campaign(
                campaign_name=campaign,
                days=90,
                limit=10,
            )
            if camp_decisions:
                parts.append(camp_decisions)

        # Global / account-level decisions always included
        global_decisions = get_global_decisions(days=30, limit=10)
        if global_decisions:
            parts.append(global_decisions)

        if not parts:
            return ""

        return "\n\n" + "\n\n".join(parts)

    except Exception as _e:
        import logging as _log
        _log.getLogger(__name__).warning(f"[mcp_decisions] load failed (non-fatal): {_e}")
        return ""


def _build_excellence_block(campaign_name: str, summary: dict, camp_settings: dict,
                             campaign_stats: dict | None = None,
                             planned_targets: dict | None = None) -> str:
    """
    Build the campaign-type-aware excellence block injected into the Claude prompt.
    Pulls numeric targets from excellence_targets DB and computes a live gap analysis
    against the campaign's actual metrics. Returns empty string on error so the
    optimizer continues working even if the DB call fails.

    campaign_stats: per-campaign 30-day stats (preferred for CPA/CPL comparisons).
    summary: account-wide totals (fallback only — DO NOT use for per-campaign benchmarks).
    """
    try:
        from database import get_excellence_targets
        ctype = _classify_campaign(campaign_name)

        # Pull relevant targets: 'all' targets + service-specific targets
        targets_all = get_excellence_targets(applies_to='all')
        targets_service = get_excellence_targets(applies_to=ctype) if ctype != 'all' else []
        all_targets = targets_all + targets_service

        # Prefer campaign_stats for per-campaign metrics; fall back to account summary
        # IMPORTANT: summary.cost_per_lead / cost_per_acquisition are ACCOUNT TOTALS —
        # they must NOT be used as the campaign's CPA benchmark. Use campaign_stats instead.
        cs = campaign_stats or {}
        _cpl  = cs.get('cpl_usd')  or 0
        _cpa  = cs.get('cpa_usd')  or 0
        _roas = cs.get('roas')     or 0

        # Metric name → live value mapping (from campaign_stats + camp_settings)
        live_values = {
            'ctr':                    cs.get('ctr_pct', 0),
            'conv_rate':              0,  # not tracked at campaign level yet
            'cpl':                    _cpl,
            'cost_per_new_patient':   _cpa,
            'impression_share':       (camp_settings.get('search_impression_share') or 0) * 100,
            'roas':                   _roas,
            'budget_lost_is_threshold': (camp_settings.get('search_budget_lost_is') or 0) * 100,
            'rank_lost_is_threshold':   (camp_settings.get('search_rank_lost_is') or 0) * 100,
            # CPA targets: use campaign-level CPA not account total
            'cpa_min':  _cpa,
            'cpa_max':  _cpa,
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

        # PR 4: planned vs live targets section
        planned_lines = []
        pt = planned_targets or {}
        _planned_monthly = pt.get("monthly_budget") or 0
        _planned_cpl = pt.get("expected_cpl") or 0
        if _planned_monthly or _planned_cpl:
            planned_lines.append("")
            planned_lines.append("=== PRACTICE'S OWN PLANNED TARGETS (from campaign creation wizard) ===")
            if _planned_monthly:
                live_daily = (camp_settings or {}).get("daily_budget_usd") or 0
                live_monthly = live_daily * 30
                drift = round((live_monthly - _planned_monthly) / _planned_monthly * 100) if _planned_monthly else 0
                drift_str = f"(DRIFT: {drift:+d}%)" if abs(drift) > 20 else "(on track)"
                planned_lines.append(f"  Planned monthly budget: ${_planned_monthly:.0f}  |  Live monthly: ${live_monthly:.0f}  {drift_str}")
            if _planned_cpl:
                live_cpl = (campaign_stats or {}).get("cpl_usd") or 0
                if live_cpl:
                    cpl_ratio = live_cpl / _planned_cpl
                    if cpl_ratio > 1.5:
                        planned_lines.append(
                            f"  Target CPL: ${_planned_cpl:.0f}  |  Live CPL: ${live_cpl:.0f}  "
                            f"⚠ OVER TARGET ({cpl_ratio:.1f}×) — prefer waste-reduction recs over coverage-gain recs."
                        )
                    else:
                        planned_lines.append(f"  Target CPL: ${_planned_cpl:.0f}  |  Live CPL: ${live_cpl:.0f}  (within target)")
                else:
                    planned_lines.append(f"  Target CPL: ${_planned_cpl:.0f}  |  Live CPL: no data yet")

        lines = [
            f"=== GDC EXCELLENCE BENCHMARKS — GAP ANALYSIS (campaign type: {ctype}) ===",
        ] + gap_lines + planned_lines + [
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
    oos_reason = _is_out_of_scope(t)
    if oos_reason:
        return oos_reason
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

    # Quality score enum mapping (Google Ads API enum value → string)
    _qs_map = {0: None, 1: "UNKNOWN", 2: "BELOW_AVERAGE", 3: "AVERAGE", 4: "ABOVE_AVERAGE"}

    query = f"""
        SELECT
            ad_group_criterion.keyword.text,
            ad_group_criterion.keyword.match_type,
            ad_group_criterion.status,
            ad_group_criterion.resource_name,
            ad_group_criterion.effective_cpc_bid_micros,
            ad_group_criterion.cpc_bid_micros,
            ad_group_criterion.quality_info.quality_score,
            ad_group_criterion.quality_info.creative_quality_score,
            ad_group_criterion.quality_info.post_click_quality_score,
            ad_group_criterion.quality_info.search_predicted_ctr,
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
    # Aggregate per keyword (same keyword appears multiple times with date segmentation)
    agg: dict = {}
    try:
        response = service.search(customer_id=customer_id, query=query)
        for row in response:
            kw_text = row.ad_group_criterion.keyword.text
            if not kw_text:
                continue
            cost = (row.metrics.cost_micros or 0) / 1_000_000.0
            clicks = row.metrics.clicks or 0
            # Use cpc_bid_micros (manual CPC) if set; fall back to effective_cpc
            current_bid = (row.ad_group_criterion.cpc_bid_micros or
                           row.ad_group_criterion.effective_cpc_bid_micros or 0)
            # Dedup key: criterion resource_name is globally unique per kw+match_type+ad_group.
            # Using it (rather than kw_text+ad_group) handles the case where the same keyword
            # text exists with multiple match types in the same ad group.
            key = row.ad_group_criterion.resource_name
            if key not in agg:
                qi = row.ad_group_criterion.quality_info
                agg[key] = {
                    "keyword": kw_text,
                    "match_type": str(row.ad_group_criterion.keyword.match_type),
                    "status": str(row.ad_group_criterion.status),
                    "resource_name": row.ad_group_criterion.resource_name,
                    "current_bid_micros": current_bid,
                    "ad_group": row.ad_group.name,
                    "ad_group_resource": row.ad_group.resource_name,
                    "campaign": row.campaign.name,
                    "campaign_resource": row.campaign.resource_name,
                    "impressions": 0,
                    "clicks": 0,
                    "cost": 0.0,
                    "conversions": 0.0,
                    "conversion_value": 0.0,
                    # Quality Score fields — not day-segmentable; take first non-None value
                    "quality_score": qi.quality_score or None,
                    "ad_relevance": _qs_map.get(int(qi.creative_quality_score) if qi.creative_quality_score else 0),
                    "landing_page_experience": _qs_map.get(int(qi.post_click_quality_score) if qi.post_click_quality_score else 0),
                    "expected_ctr_qs": _qs_map.get(int(qi.search_predicted_ctr) if qi.search_predicted_ctr else 0),
                }
            agg[key]["impressions"] += row.metrics.impressions or 0
            agg[key]["clicks"] += clicks
            agg[key]["cost"] += cost
            agg[key]["conversions"] += row.metrics.conversions or 0
            agg[key]["conversion_value"] += row.metrics.conversions_value or 0

        for kw_data in agg.values():
            c = kw_data["clicks"] or 1
            kw_data["cpc"] = round(kw_data["cost"] / c, 4) if kw_data["cost"] > 0 else 0.0
            results.append(kw_data)
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
                "campaign_status": str(row.campaign.status).replace("CampaignStatus.", ""),
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


def _score_tier(impr: int, clicks: int, cost_usd: float, conv: float, avg_ctr: float,
                daily_budget_usd: float = 0.0) -> str:
    """
    Shared tier scorer for ads and ad groups.
    Returns: "cold" | "weak" | "average" | "strong"

    Thresholds (aligned across both uses):
    - cold:   < 100 impressions — not enough data
    - weak:   zero conversions after ≥ zero-conv threshold spend
    - strong: CTR ≥ 120% of campaign average
    - average: CTR ≥ 50% of campaign average
    - weak:   CTR < 50% of campaign average

    Zero-conv threshold scales with daily budget to respect Google's learning phase:
      max($30, daily_budget_usd × 5) — roughly 5 days of spend at budget pace.
      When daily_budget_usd is 0 (unknown), falls back to $30 (preserves old behavior).
    """
    if impr < 100:
        return "cold"
    # Scale pause threshold by budget: ~5 days of spend, min $30 (preserves old behavior
    # when budget is unknown). Prevents premature pausing during Google's learning phase.
    _zero_conv_threshold = max(30.0, daily_budget_usd * 5) if daily_budget_usd > 0 else 30.0
    if conv == 0 and cost_usd >= _zero_conv_threshold:
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


def _bid_confidence_pct(conversions: float) -> float:
    """
    Returns bid adjustment multiplier (as a fraction, e.g. 0.10 = 10%) scaled
    by conversion volume to reflect data confidence:
      < 5 conversions  → ±5%  (low confidence — limited signal)
      5–20 conversions → ±10% (medium confidence)
      20+ conversions  → ±15% (high confidence — well-supported decision)

    Defaults to low confidence (5%) when conversions is 0 or None.
    Applied symmetrically to both increase_bid and decrease_bid operations.
    """
    conv = float(conversions or 0)
    if conv >= 20:
        return 0.15
    if conv >= 5:
        return 0.10
    return 0.05


def _write_back_competitor_memory(
    run_id: str,
    search_terms: list,
) -> dict:
    """
    Match recent search terms against known competitor brand stems in
    competitor_practices. Bumps confidence + spend for confirmed matches.
    Queues brand-like terms tagged 'conquest' by the semantic classifier
    (via st_classifications table) for human review.

    C2 fix: search_term rows use cost (dollars float) not cost_micros;
            campaign key is campaign_resource not campaign_id.
    C3 fix: query st_classifications table to get verdict per (term, campaign).

    Returns: {"confirmed": [...], "new_candidates": [...]}
    """
    import re as _re
    from database import get_all_practices_with_stems, upsert_competitor_candidate

    # ── 1. Load all known competitor brand stems ──────────────────────────────
    practices = []
    try:
        practices = get_all_practices_with_stems()
    except Exception:
        pass

    # Build stem → practice_id map for fast lookup
    stem_to_pid: dict[str, int] = {}
    for p in practices:
        for stem in (p.get("brand_stems") or []):
            s = stem.strip().lower()
            if s:
                stem_to_pid[s] = p["id"]

    # ── 2. Load classifier verdicts from st_classifications (C3 fix) ─────────
    # Build map: (normalized_term, campaign_name_lower) → verdict
    # so we can detect conquest-tagged terms without relying on inline row fields.
    verdict_map: dict[tuple, str] = {}
    try:
        from database import _conn
        with _conn() as _vc:
            rows_v = _vc.execute(
                "SELECT search_term, campaign_name, verdict FROM st_classifications "
                "WHERE verdict = 'conquest'"
            ).fetchall()
        for rv in rows_v:
            k = (rv[0].strip().lower(), (rv[1] or "").strip().lower())
            verdict_map[k] = rv[2]
    except Exception:
        pass  # no st_classifications table yet — silently skip

    # ── 3. Walk search terms ──────────────────────────────────────────────────
    confirmed: list[dict] = []
    new_candidates: list[dict] = []

    for row in (search_terms or []):
        term = (row.get("search_term") or "").strip().lower()
        if not term or len(term) < 4:
            continue

        # C2 fix: cost is stored as dollars (float); convert to micros for the DB
        cost_dollars = float(row.get("cost") or 0.0)
        spend_micros = int(round(cost_dollars * 1_000_000))
        clicks = int(row.get("clicks") or 0)
        campaign_name = (row.get("campaign") or row.get("campaign_name") or "").strip()
        # C2 fix: use campaign_resource (the full resource string) as campaign_id
        campaign_resource = row.get("campaign_resource") or ""

        # Try to match against known stems (word-boundary to avoid false positives)
        matched_pid: int | None = None
        for stem, pid in stem_to_pid.items():
            if len(stem) < 4:
                continue
            if _re.search(r"\b" + _re.escape(stem) + r"\b", term):
                matched_pid = pid
                break

        if matched_pid is not None:
            # Known competitor — bump confidence/spend in campaign policy
            try:
                from database import _now
                from search_term_classifier import _detect_campaign_type as _detect_ct_cm
                ctype = _detect_ct_cm(campaign_name) or "general"
                now = _now()
                with _conn() as _c:
                    _c.execute("""
                        INSERT INTO competitor_campaign_policy
                            (practice_id, campaign_type, negate, confidence,
                             spend_seen_micros, clicks_seen, last_confirmed_at, last_updated_at)
                        VALUES (?,?,1,10,?,?,?,?)
                        ON CONFLICT(practice_id, campaign_type) DO UPDATE SET
                            confidence = MIN(100, confidence + 5),
                            spend_seen_micros = spend_seen_micros + excluded.spend_seen_micros,
                            clicks_seen = clicks_seen + excluded.clicks_seen,
                            last_confirmed_at = excluded.last_confirmed_at,
                            last_updated_at = excluded.last_updated_at
                    """, (matched_pid, ctype, spend_micros, clicks, now, now))
            except Exception as _upd_err:
                logger.debug(f"Competitor memory update failed for pid={matched_pid}: {_upd_err}")
            confirmed.append({"term": term, "practice_id": matched_pid, "spend_micros": spend_micros})

        else:
            # C3 fix: check verdict from st_classifications table (not inline row field)
            camp_key = campaign_name.strip().lower()
            verdict = verdict_map.get((term, camp_key), "")
            if verdict == "conquest" and spend_micros > 0:
                try:
                    from search_term_classifier import _detect_campaign_type as _detect_ct_cm2
                    ctype2 = _detect_ct_cm2(campaign_name) or "general"
                    upsert_competitor_candidate(
                        search_term=term,
                        campaign_id=campaign_resource,
                        campaign_name=campaign_name,
                        campaign_type=ctype2,
                        spend_micros=spend_micros,
                        clicks=clicks,
                    )
                    new_candidates.append({"term": term, "campaign_name": campaign_name, "spend_micros": spend_micros})
                except Exception as _cand_err:
                    logger.debug(f"Competitor candidate upsert failed for '{term}': {_cand_err}")

    return {"confirmed": confirmed, "new_candidates": new_candidates}


def _compute_budget_click_signal(
    planned_ad_groups: list,
    monthly_budget: float,
    live_daily_budget: float,
    live_cpc_avg: float,
    bidding_strategy: str,
) -> dict:
    """
    PR 2: Mirror the frontend Launch-tab clicks/day calculation, plus a live counterpart.

    Detects:
      - "under_smart_bidding_floor": Smart Bidding active but < 10 clicks/day
      - "cpc_drift_high": live CPC > 1.5× planned estimated CPC
      - "ok": no action needed

    Returns a dict with all signals; safe to pass directly into Claude context.
    """
    import math

    # Weighted average planned CPC from wizard ad groups
    planned_est_cpc = 0.0
    total_budget_pct = sum(ag.get("daily_budget_pct", 0) for ag in planned_ad_groups)
    if planned_ad_groups and total_budget_pct > 0:
        weighted_cpc = sum(
            ag.get("suggested_cpc_usd", 0) * ag.get("daily_budget_pct", 0)
            for ag in planned_ad_groups
        )
        planned_est_cpc = weighted_cpc / total_budget_pct

    planned_daily_budget = (monthly_budget / 30) if monthly_budget else 0
    planned_clicks_per_day = (planned_daily_budget / planned_est_cpc) if planned_est_cpc > 0 else 0

    live_clicks_per_day = (live_daily_budget / live_cpc_avg) if live_cpc_avg > 0 and live_daily_budget > 0 else 0

    _smart_bidding_strats = {"MAXIMIZE_CONVERSIONS", "TARGET_CPA", "TARGET_ROAS", "MAXIMIZE_CONVERSION_VALUE"}
    smart_bidding = (bidding_strategy or "").upper() in _smart_bidding_strats
    smart_bidding_starved = smart_bidding and live_clicks_per_day > 0 and live_clicks_per_day < 10

    cpc_drift_high = (
        planned_est_cpc > 0
        and live_cpc_avg > 0
        and live_cpc_avg > planned_est_cpc * 1.5
    )

    if smart_bidding_starved:
        flag = "under_smart_bidding_floor"
    elif cpc_drift_high:
        flag = "cpc_drift_high"
    else:
        flag = "ok"

    min_budget_for_smart_bidding = math.ceil(live_cpc_avg * 10) if live_cpc_avg > 0 else 0

    return {
        "planned_est_cpc": round(planned_est_cpc, 2),
        "planned_clicks_per_day": round(planned_clicks_per_day, 1),
        "live_cpc": round(live_cpc_avg, 2),
        "live_clicks_per_day": round(live_clicks_per_day, 1),
        "smart_bidding_active": smart_bidding,
        "smart_bidding_starved": smart_bidding_starved,
        "cpc_drift_high": cpc_drift_high,
        "min_daily_budget_for_smart_bidding": min_budget_for_smart_bidding,
        "flag": flag,
    }


def _lifecycle_sieve(ops: list, lifecycle: dict, conversions_30d: float, campaign_settings: dict = None) -> list:
    """
    Defense-in-depth post-filter: ensure Claude didn't violate lifecycle rules.
    Blocked ops are converted to claude_advisory rows with 'LIFECYCLE_BLOCKED:' prefix
    so they remain visible in the approval queue.

    lifecycle — the dict from build_lifecycle_block()
    conversions_30d — from campaign_stats (used for ramping change_bid_strategy guard)

    Fails OPEN: if lifecycle is absent or has no stage, ops pass through unchanged.
    """
    from lifecycle import STAGE_NEW, STAGE_RAMPING, STAGE_UNKNOWN

    # Fail-open: no lifecycle data → don't block anything
    if not lifecycle or not lifecycle.get("stage"):
        return ops

    stage = lifecycle.get("stage")

    # Operations always safe in any stage (zero-risk)
    # add_asset (callouts/snippets) is always allowed — zero cost, improves ad real estate
    # and Quality Score without disrupting the learning phase
    _ALWAYS_ALLOWED = {"add_negative_keyword", "claude_advisory", "add_asset"}

    # Operations forbidden in new/unknown (increase_bid and change_bid_strategy handled separately below)
    _FORBIDDEN_NEW = {
        "decrease_bid", "pause_keyword", "add_exact_keyword",
        "replace_ad", "pause_ad_group", "update_geo_targeting",
        "change_budget", "change_match_type", "ad_copy_suggestion",
    }

    filtered = []
    for op in ops:
        if not isinstance(op, dict):
            continue
        op_type = op.get("operation", "")
        original_reason = op.get("reason", "")

        # --- new / unknown: special handling for increase_bid ---
        # Allow small bid increases (≤8%) during learning ONLY when impression rank loss
        # is high and the campaign is NOT budget-constrained. This corrects bids that were
        # set too low at launch without resetting the learning phase.
        if stage in (STAGE_NEW, STAGE_UNKNOWN) and op_type == "increase_bid":
            current_micros  = int(op.get("current_bid_micros") or 0)
            new_micros      = int(op.get("new_bid_micros") or 0)
            rank_lost       = float(op.get("search_rank_lost_is") or 0)
            budget_lost     = float(op.get("search_budget_lost_is") or 0)
            impressions     = int(op.get("impressions") or 0)

            # M1: if current_micros = 0, campaign is on smart bidding — increase_bid not applicable
            if current_micros == 0:
                filtered.append({
                    "operation": "claude_advisory",
                    "reason": (
                        f"LIFECYCLE_BLOCKED: increase_bid not applicable — campaign is on smart bidding "
                        f"(current_bid_micros=0). Consider change_bid_strategy→MANUAL_CPC if "
                        f"search_rank_lost_is>{rank_lost:.0%} and budget<$15/day. "
                        f"Original intent: {original_reason}"
                    ),
                    "estimated_monthly_impact": {
                        "savings_usd": 0, "impact_type": "bid_efficiency",
                        "confidence": "low",
                        "benchmark_gap": "increase_bid not applicable on smart bidding — use change_bid_strategy→MANUAL_CPC",
                    },
                })
                logger.info(f"[lifecycle_sieve] Blocked increase_bid during {stage}: smart bidding (current_micros=0)")
                continue

            pct_increase = (new_micros - current_micros) / current_micros

            allowed = (
                rank_lost    > 0.40 and   # losing >40% of auctions due to low rank
                budget_lost  < 0.20 and   # not budget-constrained
                impressions  >= 10  and   # enough signal that the keyword is entering auctions
                pct_increase <= 0.08      # max +8% increase
            )

            if allowed:
                logger.info(
                    f"[lifecycle_sieve] Allowing increase_bid during {stage} stage "
                    f"(rank_lost={rank_lost:.0%}, +{pct_increase:.0%}, impressions={impressions})"
                )
                filtered.append(op)
            else:
                block_reason = []
                if rank_lost <= 0.40:   block_reason.append(f"rank_lost_IS={rank_lost:.0%} (need >40%)")
                if budget_lost >= 0.20: block_reason.append(f"budget_lost_IS={budget_lost:.0%} — budget-constrained, increase budget first")
                if impressions < 10:    block_reason.append(f"only {impressions} impressions — insufficient signal")
                if pct_increase > 0.08: block_reason.append(f"requested +{pct_increase:.0%} exceeds 8% learning-phase cap")
                filtered.append({
                    "operation": "claude_advisory",
                    "reason": (
                        f"LIFECYCLE_BLOCKED: increase_bid suppressed during '{stage}' stage — "
                        f"{'; '.join(block_reason)}. "
                        f"Original intent: {original_reason}"
                    ),
                    "estimated_monthly_impact": {
                        "savings_usd": 0, "impact_type": "bid_efficiency",
                        "confidence": "low",
                        "benchmark_gap": f"bid increase deferred: {'; '.join(block_reason)}",
                    },
                })
                logger.info(f"[lifecycle_sieve] Blocked increase_bid during {stage}: {'; '.join(block_reason)}")
            continue

        # --- new / unknown: change_bid_strategy → MANUAL_CPC allowed when smart bidding + thin budget ---
        # Smart bidding on <$15/day starves — no auction signals. Manual CPC lets us set
        # competitive baseline bids immediately.
        if stage in (STAGE_NEW, STAGE_UNKNOWN) and op_type == "change_bid_strategy":
            target_strategy = (op.get("bid_strategy") or "").upper()
            _cs = campaign_settings or {}
            # M4: read rank_lost from op first, then campaign_settings (lifecycle dict doesn't have it)
            rank_lost    = float(op.get("search_rank_lost_is") or _cs.get("search_rank_lost_is") or 0)
            daily_budget = float(op.get("daily_budget_usd") or _cs.get("daily_budget_usd") or 0)

            if target_strategy == "MANUAL_CPC":
                # C1: require a known positive budget; 0 means missing data — block conservatively
                if daily_budget <= 0:
                    filtered.append({
                        "operation": "claude_advisory",
                        "reason": (
                            f"LIFECYCLE_BLOCKED: change_bid_strategy→MANUAL_CPC suppressed — "
                            f"daily budget unknown (0 or missing); cannot verify thin-budget condition. "
                            f"Original intent: {original_reason}"
                        ),
                        "estimated_monthly_impact": {"savings_usd": 0, "impact_type": "bid_efficiency",
                                                     "confidence": "low", "benchmark_gap": "budget data missing — bid strategy change deferred"},
                    })
                elif rank_lost > 0.40 and 0 < daily_budget < 15.0:
                    # All conditions met — allow
                    logger.info(
                        f"[lifecycle_sieve] Allowing change_bid_strategy→MANUAL_CPC during {stage} "
                        f"(rank_lost={rank_lost:.0%}, budget=${daily_budget:.2f}/day)"
                    )
                    filtered.append(op)
                else:
                    # Conditions not met — block with specific reason
                    block_parts = []
                    if rank_lost <= 0.40:
                        block_parts.append(f"rank_lost_IS={rank_lost:.0%} (need >40%)")
                    if daily_budget >= 15.0:
                        block_parts.append(f"budget=${daily_budget:.2f}/day (need <$15 — smart bidding viable at this budget)")
                    filtered.append({
                        "operation": "claude_advisory",
                        "reason": (
                            f"LIFECYCLE_BLOCKED: change_bid_strategy→MANUAL_CPC suppressed — "
                            f"{'; '.join(block_parts)}. "
                            f"Original intent: {original_reason}"
                        ),
                        "estimated_monthly_impact": {"savings_usd": 0, "impact_type": "bid_efficiency",
                                                     "confidence": "low", "benchmark_gap": "bid strategy change deferred"},
                    })
            else:
                # Non-MANUAL_CPC strategy switches always blocked during new stage
                filtered.append({
                    "operation": "claude_advisory",
                    "reason": (
                        f"LIFECYCLE_BLOCKED: change_bid_strategy to {target_strategy} suppressed — "
                        f"campaign in '{stage}' stage. Only MANUAL_CPC is allowed during learning. "
                        f"Original intent: {original_reason}"
                    ),
                    "estimated_monthly_impact": {"savings_usd": 0, "impact_type": "bid_efficiency",
                                                 "confidence": "low", "benchmark_gap": "bid strategy change deferred until day 31+"},
                })
            continue

        # --- Any stage: block change_budget when budget_lost_is < 20% (bid problem, not budget problem) ---
        if op_type == "change_budget":
            _cs_budget = campaign_settings or {}
            budget_lost_is = float(op.get("search_budget_lost_is") or _cs_budget.get("search_budget_lost_is") or 0)
            if budget_lost_is < 0.20:
                filtered.append({
                    "operation": "claude_advisory",
                    "reason": (
                        f"OPTIMIZER_BLOCKED: change_budget suppressed — search_budget_lost_is={budget_lost_is:.0%} "
                        f"(threshold: >20%). Current impression loss is rank/bid-side, not budget-side. "
                        f"Fix bids first; raise budget only after budget_lost_is exceeds 30%. "
                        f"Original intent: {original_reason}"
                    ),
                    "estimated_monthly_impact": {
                        "savings_usd": 0, "impact_type": "waste_reduction",
                        "confidence": "high",
                        "benchmark_gap": f"budget increase deferred: budget_lost_is={budget_lost_is:.0%} — not budget-constrained",
                    },
                })
                logger.info(f"[sieve] Blocked change_budget: budget_lost_is={budget_lost_is:.0%} < 20%")
                continue

        # --- Any stage: pause_keyword requires >= 30 impressions for valid signal ---
        if op_type == "pause_keyword":
            kw_impressions = int(op.get("impressions") or 0)
            if 0 < kw_impressions < 30:
                filtered.append({
                    "operation": "claude_advisory",
                    "reason": (
                        f"OPTIMIZER_BLOCKED: pause_keyword suppressed for '{op.get('keyword_text', '')}' — "
                        f"only {kw_impressions} impressions (minimum 30 required for valid signal). "
                        f"Original intent: {original_reason}"
                    ),
                    "estimated_monthly_impact": {
                        "savings_usd": 0, "impact_type": "waste_reduction",
                        "confidence": "low",
                        "benchmark_gap": f"pause deferred: {kw_impressions} impressions < 30 minimum",
                    },
                })
                logger.info(f"[sieve] Blocked pause_keyword '{op.get('keyword_text','')}': only {kw_impressions} impressions")
                continue

        # --- new/unknown: only one reset-class op per run ---
        # NOTE: change_bid_strategy ops always hit `continue` in the earlier dedicated block,
        # so they never reach this guard. In practice this guard prevents a second
        # update_geo_targeting op, or an update_geo_targeting after a change_bid_strategy
        # already passed through. A second change_bid_strategy is blocked by the earlier block.
        _RESET_CLASS_OPS = {"change_bid_strategy", "update_geo_targeting"}
        if stage in (STAGE_NEW, STAGE_UNKNOWN) and op_type in _RESET_CLASS_OPS:
            already_has_reset = any(
                f.get("operation") in _RESET_CLASS_OPS
                for f in filtered
                if isinstance(f, dict)
            )
            if already_has_reset:
                filtered.append({
                    "operation": "claude_advisory",
                    "reason": (
                        f"LIFECYCLE_BLOCKED: {op_type} suppressed — a reset-class operation is already "
                        f"queued this run. Google's learning clock can only be disrupted once per optimizer run. "
                        f"Original intent: {original_reason}"
                    ),
                    "estimated_monthly_impact": {
                        "savings_usd": 0, "impact_type": "bid_efficiency",
                        "confidence": "high",
                        "benchmark_gap": "second reset-class op deferred to next optimizer run",
                    },
                })
                logger.info(f"[sieve] Blocked second reset-class op '{op_type}' during {stage}")
                continue

        # --- new/unknown: defer ad copy changes when loss is bid-driven not quality-driven ---
        _AD_COPY_OPS = {"ad_copy_suggestion", "replace_ad"}
        if stage in (STAGE_NEW, STAGE_UNKNOWN) and op_type in _AD_COPY_OPS:
            _cs_ad = campaign_settings or {}
            rank_lost_ad = float(op.get("search_rank_lost_is") or _cs_ad.get("search_rank_lost_is") or 0)
            if rank_lost_ad > 0.40:
                filtered.append({
                    "operation": "claude_advisory",
                    "reason": (
                        f"LIFECYCLE_BLOCKED: {op_type} deferred — search_rank_lost_is={rank_lost_ad:.0%} "
                        f"indicates auction losses are bid-driven, not quality-driven. "
                        f"Ad copy improvements will not meaningfully improve Ad Rank. "
                        f"Fix bids first; revisit ad copy after rank_lost_is < 30%. "
                        f"Original intent: {original_reason}"
                    ),
                    "estimated_monthly_impact": {
                        "savings_usd": 0, "impact_type": "conversion_lift",
                        "confidence": "low",
                        "benchmark_gap": "ad copy deferred: rank loss is bid-side",
                    },
                })
                logger.info(f"[sieve] Blocked {op_type} during {stage}: rank_lost_is={rank_lost_ad:.0%} > 40%")
                continue

        # --- new / unknown: block everything else in _FORBIDDEN_NEW ---
        if stage in (STAGE_NEW, STAGE_UNKNOWN) and op_type in _FORBIDDEN_NEW:
            filtered.append({
                "operation": "claude_advisory",
                "reason": (
                    f"LIFECYCLE_BLOCKED: {op_type} suppressed — campaign in '{stage}' stage "
                    f"(learning period active). Original intent: {original_reason}"
                ),
                "estimated_monthly_impact": {
                    "savings_usd": 0,
                    "impact_type": "waste_reduction",
                    "confidence": "low",
                    "benchmark_gap": f"lifecycle gate: {op_type} deferred until day 31+",
                },
            })
            logger.info(f"[lifecycle_sieve] Blocked '{op_type}' during {stage} stage")
            continue

        # --- ramping: no change_bid_strategy without conversions, no add_exact_keyword ---
        if stage == STAGE_RAMPING:
            if op_type == "change_bid_strategy" and float(conversions_30d or 0) < 15:
                filtered.append({
                    "operation": "claude_advisory",
                    "reason": (
                        f"LIFECYCLE_BLOCKED: change_bid_strategy requires 15+ conversions "
                        f"(have {conversions_30d:.0f}) — campaign still ramping. "
                        f"Original intent: {original_reason}"
                    ),
                    "estimated_monthly_impact": {
                        "savings_usd": 0,
                        "impact_type": "bid_efficiency",
                        "confidence": "low",
                        "benchmark_gap": "need 15+ conversions for Smart Bidding",
                    },
                })
                logger.info(f"[lifecycle_sieve] Blocked change_bid_strategy — only {conversions_30d} conv in ramping stage")
                continue
            if op_type == "add_exact_keyword":
                filtered.append({
                    "operation": "claude_advisory",
                    "reason": (
                        f"LIFECYCLE_BLOCKED: add_exact_keyword (match-type expansion) deferred "
                        f"until mature stage. Original intent: {original_reason}"
                    ),
                    "estimated_monthly_impact": {
                        "savings_usd": 0,
                        "impact_type": "coverage_gain",
                        "confidence": "low",
                        "benchmark_gap": "match expansion deferred to mature stage",
                    },
                })
                logger.info(f"[lifecycle_sieve] Blocked add_exact_keyword in ramping stage")
                continue

            # B6: pause_ad_group in ramping — only allowed when ad group has ≥50 clicks AND 0 conversions.
            # The sieve can't inspect per-ad-group clicks, so we enforce the conservative read:
            # If campaign-level conversions_30d > 0, the ad group MIGHT have conversions — don't block.
            # If conversions_30d == 0, the campaign has zero conv, so any ad group is a genuine waste.
            # The prompt already enforces the ≥50-clicks threshold per-ad-group; here we catch strategy flips.
            if op_type == "pause_ad_group":
                # Allow only when there are zero campaign-level conversions (clearest waste signal)
                # or when conversions_30d is unknown (fail-open for ramping)
                if float(conversions_30d or 0) > 0:
                    filtered.append({
                        "operation": "claude_advisory",
                        "reason": (
                            f"LIFECYCLE_BLOCKED: pause_ad_group deferred during ramping stage "
                            f"(campaign has {conversions_30d:.0f} conversions — ad group may be contributing). "
                            f"Original intent: {original_reason}"
                        ),
                        "estimated_monthly_impact": {
                            "savings_usd": 0,
                            "impact_type": "waste_reduction",
                            "confidence": "low",
                            "benchmark_gap": "pause_ad_group blocked until conversion attribution is clear (mature stage)",
                        },
                    })
                    logger.info(
                        f"[lifecycle_sieve] Blocked pause_ad_group in ramping stage "
                        f"(campaign has {conversions_30d} conversions)"
                    )
                    continue

        filtered.append(op)

    return filtered


# Per-run cache for _fetch_first_impression_date — avoids re-querying GAds for the same resource
_first_impression_cache: dict = {}


def _fetch_first_impression_date(campaign_resource: str) -> str | None:
    """
    Query Google Ads for the earliest segment.date for this campaign.
    Returns a 'YYYY-MM-DD' string or None on failure.

    Results are cached in _first_impression_cache for the lifetime of the process
    (optimizer run). On success, the caller should write the date back to the DB
    via database.set_campaign_launch_date() so future runs don't need this API call.
    """
    if not campaign_resource:
        return None
    if campaign_resource in _first_impression_cache:
        return _first_impression_cache[campaign_resource]
    try:
        from config import get_settings as _gs
        _sett = _gs()
        _client_cfg = {
            "developer_token": _sett.google_ads_developer_token,
            "client_id": _sett.google_ads_client_id,
            "client_secret": _sett.google_ads_client_secret,
            "refresh_token": _sett.google_ads_refresh_token,
            "login_customer_id": str(_sett.google_ads_customer_id).replace("-", ""),
            "use_proto_plus": True,
        }
        _client = GoogleAdsClient.load_from_dict(_client_cfg, version="v18")
        _svc = _client.get_service("GoogleAdsService")
        _cid = str(_sett.google_ads_customer_id).replace("-", "")
        _q = (
            f"SELECT segments.date FROM campaign "
            f"WHERE campaign.resource_name = '{campaign_resource}' "
            f"AND segments.date DURING LAST_365_DAYS "
            f"ORDER BY segments.date ASC LIMIT 1"
        )
        _resp = _svc.search(customer_id=_cid, query=_q)
        for _row in _resp:
            date_str = _row.segments.date
            if date_str:
                _first_impression_cache[campaign_resource] = date_str
                return date_str
    except Exception as _e:
        logger.debug(f"[lifecycle] _fetch_first_impression_date failed for {campaign_resource}: {_e}")
    _first_impression_cache[campaign_resource] = None
    return None


def _build_lqi_campaign_slice(campaign: str, lqi: dict) -> dict:
    """
    Filter the account-wide LQI signals down to what's relevant for a single campaign.
    call and bad_search_terms are filtered to this campaign only.
    All other sub-signals (sources, schedule, cold_leads, no_shows) are account-wide
    context that Claude should consider even at the per-campaign level.
    """
    camp_l = (campaign or "").strip().lower()
    # Per-campaign call quality
    lqi_camp_calls = {}
    for cname, payload in (lqi.get("calls") or {}).get("by_campaign", {}).items():
        if cname.strip().lower() == camp_l:
            lqi_camp_calls = payload
            break
    # Per-campaign bad search terms
    lqi_camp_bad_terms = []
    for cname, terms in (lqi.get("search_terms") or {}).get("by_campaign", {}).items():
        if cname.strip().lower() == camp_l:
            lqi_camp_bad_terms = terms
            break
    # Per-campaign geo signals
    lqi_camp_geo: dict = {}
    geo_by_camp = (lqi.get("geo") or {}).get("by_campaign", {})
    for cname, payload in geo_by_camp.items():
        if cname.strip().lower() == camp_l:
            lqi_camp_geo = payload
            break

    return {
        "sources":          lqi.get("sources", {}),
        "calls":            lqi_camp_calls,
        "bad_search_terms": lqi_camp_bad_terms,
        "schedule":         lqi.get("schedule", {}),
        "cold_leads":       lqi.get("cold_leads", {}),
        "no_shows":         lqi.get("no_shows", {}),
        "geo":              lqi_camp_geo,
    }


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
                             ad_group_performance: list | None = None,
                             lqi: dict | None = None,
                             # PR 1: wizard context
                             campaign_brief: dict | None = None,
                             competitor_intel: dict | None = None,
                             planned_build: dict | None = None,
                             # PR 2: budget feasibility
                             budget_feasibility: dict | None = None,
                             # PR 3: intent signals
                             intent_signals: dict | None = None,
                             # PR 7: conquest keyword protection (set of lowercase terms)
                             conquest_keywords_protected: set | None = None,
                             # Lifecycle: age + stage classification block
                             lifecycle: dict | None = None,
                             # Budget constraint: True = no budget increases allowed
                             budget_constrained: bool = False,
                             # Existing campaign assets: callouts, snippets, sitelinks already live
                             existing_campaign_assets: dict | None = None) -> list:
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

        # ── Per-campaign stats (THIS campaign only) ──────────────────────────────────
        # keyword_perf is already filtered to this campaign before being passed in.
        # Compute campaign-level spend/clicks/leads so Claude does NOT confuse them
        # with the account_summary totals (which cover all campaigns combined).
        _camp_spend   = round(sum(k.get("cost", 0) for k in keyword_perf), 2)
        _camp_clicks  = sum(k.get("clicks", 0) for k in keyword_perf)
        _camp_impr    = sum(k.get("impressions", 0) for k in keyword_perf)
        _camp_convs   = sum(k.get("conversions", 0) for k in keyword_perf)
        _camp_leads   = sum(v.get("count", v.get("leads", 0)) for v in attribution.values())
        _camp_calls   = sum(
            v.get("calls", 0)
            for cn, v in call_attribution.items()
            if cn.lower() == campaign.strip().lower()
        )
        _camp_booked  = sum(
            v.get("booked_calls", 0)
            for cn, v in call_attribution.items()
            if cn.lower() == campaign.strip().lower()
        )
        _camp_acq     = _camp_leads + _camp_booked
        _camp_prod    = float((od_production.get("by_campaign") or {}).get(campaign, 0))
        campaign_stats = {
            "spend_30d_usd":        _camp_spend,
            "clicks_30d":           _camp_clicks,
            "impressions_30d":      _camp_impr,
            "gads_conversions_30d": round(_camp_convs, 1),
            "form_leads_30d":       _camp_leads,
            "calls_30d":            _camp_calls,
            "booked_calls_30d":     _camp_booked,
            "total_acquisitions":   _camp_acq,
            "od_production_usd":    round(_camp_prod, 2),
            "cpa_usd":              round(_camp_spend / _camp_acq, 2) if _camp_acq > 0 else None,
            "cpl_usd":              round(_camp_spend / _camp_leads, 2) if _camp_leads > 0 else None,
            "ctr_pct":              round(_camp_clicks / _camp_impr * 100, 2) if _camp_impr > 0 else None,
            "roas":                 round(_camp_prod / _camp_spend, 2) if _camp_spend > 0 and _camp_prod > 0 else None,
        }
        # ─────────────────────────────────────────────────────────────────────────────

        context = {
            "campaign_name": campaign,
            "campaign_resource": campaign_resource,
            "campaign_settings": camp_settings or {},   # budget, bidding strategy, impression share
            # campaign_stats = THIS campaign's 30-day numbers only
            # account_summary = ALL campaigns combined (for context/benchmarking only)
            "campaign_stats": campaign_stats,
            "account_summary": summary,
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
            "lqi": _build_lqi_campaign_slice(campaign, lqi or {}),
            "geo_defaults": _geo_defaults_for_campaign(campaign),  # ideal geo profile for this campaign type
            # PR 1: wizard context — strategy, competitors, planned structure
            "campaign_brief": campaign_brief or {},
            "competitor_intel": competitor_intel or {},
            "planned_build_summary": planned_build or {},
            # PR 2: budget feasibility signal
            "budget_feasibility": budget_feasibility or {},
            # PR 3: keyword intent signals (same vocabulary as creation wizard)
            "keyword_intent_signals": intent_signals or {},
            # Lifecycle: age + stage classification
            "lifecycle": lifecycle or {},
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

        excellence_block = _build_excellence_block(campaign, summary, camp_settings or {},
                                                    campaign_stats=campaign_stats,
                                                    planned_targets={
                                                        "monthly_budget": (campaign_brief or {}).get("planned_monthly_budget_usd", 0),
                                                        "expected_cpl":   (campaign_brief or {}).get("target_cpl_usd", 0),
                                                    })

        # ── PR 1: campaign brief + competitor intel note ──────────────────────
        campaign_brief_note = ""
        if campaign_brief:
            live_monthly = round((camp_settings or {}).get("daily_budget_usd", 0) * 30, 0)
            planned_monthly = campaign_brief.get("planned_monthly_budget_usd") or 0
            target_cpl = campaign_brief.get("target_cpl_usd") or 0
            live_cpl = campaign_stats.get("cpl_usd") or 0
            drift_budget = round((live_monthly - planned_monthly) / planned_monthly * 100) if planned_monthly else 0
            drift_cpl = round((live_cpl - target_cpl) / target_cpl * 100) if target_cpl and live_cpl else 0

            strategy = campaign_brief.get("strategy") or {}
            campaign_brief_note = "\n\nCAMPAIGN BRIEF (set during campaign creation — align recs with practice intent):\n"
            if campaign_brief.get("service_focus"):
                campaign_brief_note += f"- service_focus: {campaign_brief['service_focus']}\n"
            if campaign_brief.get("objective"):
                campaign_brief_note += f"- objective: {campaign_brief['objective']}\n"
            if campaign_brief.get("target_audience"):
                campaign_brief_note += f"- target_audience: {campaign_brief['target_audience']}\n"
            if campaign_brief.get("promo_offer"):
                campaign_brief_note += f"- promo_offer: {campaign_brief['promo_offer']}\n"
            if planned_monthly:
                drift_str = f" (DRIFT: {drift_budget:+d}% vs planned)" if abs(drift_budget) > 20 else " (on track)"
                campaign_brief_note += f"- planned_monthly_budget_usd: ${planned_monthly:.0f} | live_monthly: ${live_monthly:.0f}{drift_str}\n"
            if target_cpl:
                cpl_str = ""
                if live_cpl and live_cpl > target_cpl * 1.5:
                    cpl_str = f" ⚠ OVER TARGET by {drift_cpl:+d}% — prefer waste-reduction recs over coverage-gain"
                elif live_cpl:
                    cpl_str = f" ({drift_cpl:+d}% vs target)"
                campaign_brief_note += f"- target_cpl_usd: ${target_cpl:.0f} | live_cpl: ${live_cpl:.0f}{cpl_str}\n"
            if strategy.get("key_messages"):
                campaign_brief_note += f"- key_messages: {strategy.get('key_messages')}\n"
            if strategy.get("ad_headlines"):
                campaign_brief_note += f"- intended_headline_themes: {strategy.get('ad_headlines')}\n"
            if strategy.get("implementation_instructions"):
                campaign_brief_note += f"- operator_notes: {strategy.get('implementation_instructions')}\n"

        competitor_intel_note = ""
        if competitor_intel and (competitor_intel.get("our_differentiators") or competitor_intel.get("conquest_keywords") or competitor_intel.get("competitors")):
            competitor_intel_note = "\n\nCOMPETITOR INTELLIGENCE (from campaign creation wizard):\n"
            if competitor_intel.get("our_differentiators"):
                competitor_intel_note += f"- our_differentiators (use as headline angles for replace_ad): {competitor_intel['our_differentiators']}\n"
            if competitor_intel.get("conquest_keywords"):
                competitor_intel_note += (
                    f"- conquest_keywords (INTENTIONAL competitor targets — DO NOT recommend as negatives): "
                    f"{competitor_intel['conquest_keywords']}\n"
                )
            if competitor_intel.get("positioning_notes"):
                competitor_intel_note += f"- positioning_notes: {competitor_intel['positioning_notes']}\n"
            if competitor_intel.get("competitors"):
                for comp in competitor_intel["competitors"][:5]:
                    competitor_intel_note += (
                        f"  • {comp.get('name','?')} ({comp.get('location','')}): "
                        f"emphasis={comp.get('likely_emphasis','')} | "
                        f"gap_we_can_address={comp.get('gap_we_can_address','')}\n"
                    )

        planned_build_note = ""
        if planned_build and (planned_build.get("planned_keywords") or planned_build.get("planned_ad_groups")):
            planned_build_note = "\n\nPLANNED BUILD SUMMARY (wizard's intended structure — flag drift from live):\n"
            pk = planned_build.get("planned_keywords") or {}
            if pk.get("exact_match"):
                planned_build_note += f"- planned_exact_match_keywords: {(pk.get('exact_match') or [])[:10]}\n"
            if pk.get("phrase_match"):
                planned_build_note += f"- planned_phrase_match_keywords: {(pk.get('phrase_match') or [])[:10]}\n"
            if pk.get("negative_keywords"):
                planned_build_note += f"- planned_negative_keywords: {(pk.get('negative_keywords') or [])[:10]}\n"
            pags = planned_build.get("planned_ad_groups") or []
            if pags:
                planned_build_note += "- planned_ad_groups (theme / suggested CPC / budget pct):\n"
                for ag in pags[:5]:
                    planned_build_note += (
                        f"  • {ag.get('name','?')}: suggested_cpc=${ag.get('suggested_cpc_usd',0):.2f}, "
                        f"budget_pct={ag.get('daily_budget_pct',0)}%, theme={ag.get('theme','')}\n"
                    )

        # ── PR 2: budget feasibility note ──────────────────────────────────────
        budget_feasibility_note = ""
        if budget_feasibility:
            flag = budget_feasibility.get("flag", "ok")
            if flag != "ok":
                budget_feasibility_note = "\n\nBUDGET FEASIBILITY SIGNAL (wizard launch-tab calculator):\n"
                budget_feasibility_note += (
                    f"- planned_est_cpc: ${budget_feasibility.get('planned_est_cpc', 0):.2f} | "
                    f"planned_clicks_per_day: {budget_feasibility.get('planned_clicks_per_day', 0):.1f}\n"
                    f"- live_cpc: ${budget_feasibility.get('live_cpc', 0):.2f} | "
                    f"live_clicks_per_day: {budget_feasibility.get('live_clicks_per_day', 0):.1f}\n"
                    f"- flag: {flag}\n"
                )
                if flag == "under_smart_bidding_floor":
                    min_budget = budget_feasibility.get("min_daily_budget_for_smart_bidding", 0)
                    budget_feasibility_note += (
                        f"  Smart Bidding strategy is active but campaign has <10 clicks/day. "
                        f"Recommend EITHER change_budget to ≥${min_budget:.0f}/day OR switch "
                        f"to MAXIMIZE_CLICKS/MANUAL_CPC until volume grows. Cite both options.\n"
                    )
                elif flag == "cpc_drift_high":
                    budget_feasibility_note += (
                        f"  Live CPC is >1.5× planned CPC. Likely Quality Score or ad relevance issue. "
                        f"Prefer replace_ad or change_match_type before recommending bid changes.\n"
                    )

        # ── PR 3: intent signals note ──────────────────────────────────────────
        intent_signals_note = ""
        if intent_signals and intent_signals.get("high_intent_examples"):
            ctype_intent = intent_signals.get("campaign_type", "general")
            intent_signals_note = (
                f"\n\nKEYWORD INTENT SIGNALS for {ctype_intent} campaigns:\n"
                f"- high_intent_examples: {intent_signals['high_intent_examples']}\n"
                f"  → When a search_term matches these patterns with 0 conversions and ≥10 clicks, "
                f"prefer add_exact_keyword (capture intent precisely) over pause_keyword.\n"
            )
            if intent_signals.get("low_intent_negatives"):
                intent_signals_note += (
                    f"- low_intent_negatives: {intent_signals['low_intent_negatives']}\n"
                    f"  → Any search term containing these tokens with ≥$5 spend → add_negative_keyword (PHRASE).\n"
                )

        # ── Lifecycle note (load-bearing — Claude must respect stage rules) ────
        lifecycle_note = ""
        if lifecycle:
            _lc_stage  = lifecycle.get("stage", "unknown")
            _lc_days   = lifecycle.get("days_since_launch")
            _lc_src    = lifecycle.get("source", "none")
            _lc_learn  = lifecycle.get("in_learning_period", False)
            _lc_day_str = f"day {_lc_days}" if _lc_days is not None else "age unknown"
            _lc_warning = ""
            if _lc_stage in ("new", "unknown"):
                _lc_warning = "\n⚠️  LEARNING PERIOD ACTIVE — most operations are restricted. See LIFECYCLE_RULES. Exceptions: (1) increase_bid IS allowed for MANUAL_CPC campaigns when search_rank_lost_is > 0.40 AND search_budget_lost_is < 0.20 AND impressions >= 10 (max +8%). (2) change_bid_strategy → MANUAL_CPC IS allowed when current strategy is NOT MANUAL_CPC AND daily_budget_usd < 15.0 AND search_rank_lost_is > 0.40."
            elif _lc_stage == "ramping":
                _lc_warning = "\n⚠️  RAMPING — no change_bid_strategy unless conversions_30d ≥ 15; no add_exact_keyword."
            lifecycle_note = (
                f"\n\nCAMPAIGN LIFECYCLE (apply LIFECYCLE_RULES above)\n"
                f"Stage: {_lc_stage} ({_lc_day_str}, source={_lc_src})\n"
                f"In learning period: {_lc_learn}{_lc_warning}\n"
            )

        # ── Budget constraint note ────────────────────────────────────────────
        budget_constrained_note = ""
        if budget_constrained:
            budget_constrained_note = (
                "\n\n=== BUDGET CONSTRAINED MODE (MANDATORY) ===\n"
                "The practice has enabled Budget Constrained mode. This means:\n"
                "1. DO NOT recommend change_budget to increase any campaign's daily budget.\n"
                "2. DO NOT recommend change_bid_strategy that is expected to increase spend "
                "(e.g. MAXIMIZE_CLICKS without a target CPC cap, or removing a MANUAL_CPC constraint).\n"
                "3. Instead of budget increases, recommend bid adjustments, ad copy improvements, "
                "negative keywords, geo refinements, schedule changes, and ad group optimizations "
                "to improve ROI WITHIN the current budget.\n"
                "4. If a campaign is budget-limited and cannot improve without more spend, say so in "
                "a claude_advisory — do NOT generate a change_budget rec.\n"
                "=== END BUDGET CONSTRAINED MODE ===\n"
            )

        # ── Existing campaign assets note ─────────────────────────────────────
        # Always emit this block — Claude needs to know what's present AND what's missing
        # so it can proactively recommend callouts/snippets for campaigns that have none.
        _ea = existing_campaign_assets or {}
        _ea_callouts = _ea.get("callouts") or []
        _ea_snippets = _ea.get("structured_snippets") or []
        _ea_sitelinks = _ea.get("sitelinks") or []

        assets_note = "\n\nCAMPAIGN ASSET STATUS:\n"
        if _ea_callouts:
            assets_note += f"- Callouts already set ({len(_ea_callouts)}): {_ea_callouts}\n"
        else:
            assets_note += "- Callouts: NONE — recommend add_asset CALLOUT with 3-8 practice-specific callout_texts\n"
        if _ea_snippets:
            for snip in _ea_snippets:
                assets_note += f"- Structured snippet [{snip['header']}]: {snip['values']}\n"
        else:
            assets_note += (
                "- Structured snippets: NONE — recommend add_asset STRUCTURED_SNIPPET "
                "using header \"Service catalog\" with services specific to this campaign type\n"
            )
        if _ea_sitelinks:
            assets_note += f"- Sitelinks already set ({len(_ea_sitelinks)}): {_ea_sitelinks}\n"
        else:
            assets_note += "- Sitelinks: NONE (managed by wizard — do not recommend via add_asset)\n"
        assets_note += (
            "Rules: Do NOT recommend a CALLOUT with text already listed above. "
            "Do NOT recommend a STRUCTURED_SNIPPET with a header already listed above. "
            "ALWAYS recommend add_asset CALLOUT if callouts are NONE. "
            "ALWAYS recommend add_asset STRUCTURED_SNIPPET if no snippet exists for this campaign type."
        )

        # ── SKAG attribution opportunity note ─────────────────────────────────
        # Collected per campaign — returns empty string if no candidates qualify.
        # Only present when there is genuine signal (call convs or OD appts).
        skag_note = ""
        try:
            from skag_signals import get_skag_candidates_text as _skag_text
            _raw_skag = _skag_text(campaign)
            if _raw_skag:
                skag_note = "\n\n" + _raw_skag
        except Exception as _skag_err:
            logger.warning("SKAG signals failed for %r (non-fatal): %s", campaign, _skag_err)

        prompt = excellence_block + GOOGLE_ADS_RULES + CAMPAIGN_INTENT_RULES + LIFECYCLE_RULES + (LEARNING_PHASE_RULES if lifecycle and lifecycle.get("stage") in ("new", "unknown") else "") + """
You are the Chief Marketing Officer (CMO) for Grafton Dental Care, a private dental practice in Grafton, MA. You think at two levels simultaneously:

STRATEGIC level — Is this campaign serving the right patients? Are we allocating budget toward services with the highest patient lifetime value (implants, Invisalign, crowns > cleanings > emergency)? Is a campaign in a learning phase that needs patience rather than intervention? Should budget shift from a low-converting campaign to a proven one? Are we missing an entire patient segment worth targeting?

TACTICAL level — Given the strategic picture, what specific, executable Google Ads operations will move the needle this week?

Always lead with strategic insight in your claude_advisory slots, then follow with tactical ops. If a campaign is new (learning phase), say so explicitly and be conservative with changes. If a campaign is underperforming due to poor service-keyword fit, say so — don't just recommend bid decreases.

Analyze the data and return up to 7 SPECIFIC, EXECUTABLE recommendations.

CRITICAL — SPEND AND CPA DATA SCOPING:
The context JSON contains TWO separate spend/performance fields:
- "campaign_stats": THIS campaign's 30-day numbers ONLY — spend, clicks, leads, CPA, calls
- "account_summary": ALL campaigns combined — use ONLY for cross-campaign context, NOT as this campaign's benchmark
When citing spend, CPA, CPL, or lead counts in your reason field, ALWAYS use campaign_stats numbers.
Never cite account_summary.total_spend or account_summary.cost_per_acquisition as this campaign's numbers.

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

=== PRE-FLIGHT CHECKS — RUN THESE BEFORE GENERATING ANY RECOMMENDATIONS ===

CHECK 1 — SMART BIDDING ON THIN BUDGET (learning phase):
  IF lifecycle.stage IN ('new', 'unknown')
  AND campaign_settings.bidding_strategy_type != 'MANUAL_CPC'  (i.e. Maximize Clicks, Maximize Conversions, Target CPA, etc.)
  AND campaign_settings.search_rank_lost_is > 0.40
  AND campaign_settings.daily_budget_usd < 15.0
  THEN → emit ONE change_bid_strategy op with:
    {"operation": "change_bid_strategy", "bid_strategy": "MANUAL_CPC",
     "campaign_resource": <from data>, "search_rank_lost_is": <value>, "daily_budget_usd": <value>,
     "reason": "Campaign is on [strategy] with $X/day budget — smart bidding cannot learn on sub-$15/day. Switching to Manual CPC enables competitive baseline bids during the learning window. search_rank_lost_is=[value] confirms [X]% of auctions are lost due to bid level."}
  DO NOT emit a claude_advisory instead. DO NOT say "after day 30". EMIT THE OP NOW.

CHECK 2 — MANUAL CPC TOO LOW (learning phase):
  IF lifecycle.stage IN ('new', 'unknown')
  AND campaign_settings.bidding_strategy_type == 'MANUAL_CPC'
  AND campaign_settings.search_rank_lost_is > 0.40
  AND campaign_settings.search_budget_lost_is < 0.20
  AND any keyword has impressions >= 10
  THEN → emit increase_bid ops for qualifying keywords:
    new_bid_micros = round(current_bid_micros * 1.08), include current_bid_micros, search_rank_lost_is, search_budget_lost_is, impressions
  DO NOT emit a claude_advisory instead. EMIT THE OPS NOW.

CHECK 3 — QUALITY SCORE DIAGNOSTICS (run for every keyword in keyword_performance with impressions >= 20):
  The fields "ad_relevance", "landing_page_experience", and "expected_ctr_qs" on each keyword contain
  Google's Quality Score component ratings: "ABOVE_AVERAGE", "AVERAGE", "BELOW_AVERAGE", or null (not rated yet).

  RULE A — Landing Page Experience is BELOW_AVERAGE:
    IF a keyword has landing_page_experience == "BELOW_AVERAGE"
    AND (search_rank_lost_is > 0.25 OR the keyword has cost > $5 with 0 conversions)
    THEN → emit a claude_advisory (NOT increase_bid). State specifically:
      - Which keyword(s) have BELOW_AVERAGE landing page experience
      - That raising bids will NOT fix this — Google is penalizing the landing page itself
      - What to check: page load speed, content relevance to the keyword, mobile usability
      - Use "insight" field for the advisory text

  RULE B — Ad Relevance is BELOW_AVERAGE:
    IF a keyword has ad_relevance == "BELOW_AVERAGE" AND impressions >= 50
    THEN → emit an ad_copy_suggestion (NOT increase_bid). The ad copy needs to match the keyword
      more closely. Include the keyword text in your suggestion.

  RULE C — Expected CTR is BELOW_AVERAGE (and ad relevance is OK):
    IF a keyword has expected_ctr_qs == "BELOW_AVERAGE" AND ad_relevance != "BELOW_AVERAGE"
    AND impressions >= 100
    THEN → the keyword intent may not match searcher expectations. Emit a claude_advisory suggesting
      either a match type change (e.g., BROAD → PHRASE) or pausing the keyword.

  IMPORTANT: Do NOT emit increase_bid for any keyword where landing_page_experience or ad_relevance
  is BELOW_AVERAGE. A bid increase cannot fix a Quality Score penalty — it only raises your cost
  for the same poor position. Fix the underlying issue first.

=== END PRE-FLIGHT CHECKS ===

Each recommendation MUST be a JSON object with these fields:
- "operation": one of: add_negative_keyword | pause_keyword | increase_bid | decrease_bid | add_exact_keyword | ad_copy_suggestion | geo_exclusion | enable_keyword | change_budget | change_bid_strategy | change_match_type | add_asset | replace_ad | pause_ad_group | update_geo_targeting | claude_advisory
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
  "new_bid_micros": integer (current bid ± 10-20%),
  "current_bid_micros": integer (the keyword's current bid — required for learning-phase sieve),
  "search_rank_lost_is": float (from campaign_settings — required for learning-phase sieve),
  "search_budget_lost_is": float (from campaign_settings — required for learning-phase sieve),
  "impressions": integer (keyword's 30d impressions — required for learning-phase sieve)

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
  "bid_strategy": "MAXIMIZE_CONVERSIONS"|"TARGET_CPA"|"TARGET_ROAS"|"MAXIMIZE_CLICKS"|"MANUAL_CPC",
  "target_cpa_micros": integer (only for TARGET_CPA),
  "target_roas": float (only for TARGET_ROAS),
  "campaign_resource": campaign resource name,
  "search_rank_lost_is": float (from campaign_settings — required for learning-phase sieve),
  "daily_budget_usd": float (from campaign_settings.daily_budget_usd — required for learning-phase sieve)

For change_match_type:
  "keyword_text": keyword text,
  "resource_name": keyword resource_name,
  "new_match_type": "EXACT"|"PHRASE"|"BROAD"

For add_asset:
  "asset_type": "CALLOUT"|"STRUCTURED_SNIPPET"
    (Note: CALL is managed by the campaign wizard — do NOT recommend CALL via add_asset.
     SITELINK is managed by the wizard — do NOT recommend SITELINK via add_asset.)
  "campaign_resource": campaign resource name (required)
  "reason": 1-2 sentences explaining why this asset is needed

  If asset_type == "CALLOUT":
    "callout_texts": array of 3-10 unique strings, each STRICTLY ≤25 chars,
                     no phone numbers, no URLs, no trailing punctuation,
                     Title Case preferred, must not duplicate existing callouts.
    Example: ["Free Consultations", "Saturday Hours", "Same-Day Emergency"]

  If asset_type == "STRUCTURED_SNIPPET":
    "snippet_header": MUST be one of exactly (case-sensitive):
                      "Service catalog" | "Insurance coverage" | "Types" |
                      "Amenities" | "Brands" | "Styles" | "Neighborhoods"
      For dental: use "Service catalog" for treatments, "Insurance coverage" for insurers.
    "values": array of EXACTLY 3-10 unique strings, each STRICTLY ≤25 chars,
              no phone numbers, no URLs, no punctuation at end.
              Pick values from services_offered in the campaign data.
    Example: {"snippet_header": "Service catalog", "values": ["Dental Implants", "Veneers", "Emergency Care"]}

  RULES for add_asset:
    - ALWAYS recommend CALLOUT if CAMPAIGN ASSET STATUS shows "Callouts: NONE"
    - ALWAYS recommend STRUCTURED_SNIPPET if CAMPAIGN ASSET STATUS shows "Structured snippets: NONE"
    - Do NOT recommend if the callout_text already exists in CAMPAIGN ASSET STATUS callouts list
    - Do NOT recommend a STRUCTURED_SNIPPET if that header already exists in CAMPAIGN ASSET STATUS
    - Minimum 3 values/callout_texts required — fewer will be rejected by the API
    - Maximum 1 add_asset op per (campaign, asset_type) per run
    - Ground callout_texts and values in actual services/features from campaign_build_json and landing_page_intel
    - For CALLOUT: include practice differentiators like hours, financing, insurance, emergency access
    - For STRUCTURED_SNIPPET: use "Service catalog" for treatment lists specific to this campaign type

For replace_ad (A/B ad testing — pause underperformer, create improved version):
  "old_ad_group_ad_resource": EXACT ad_group_ad_resource from ad_performance or rsa_resources data (required)
  "new_headlines": array of 10-15 strings, each STRICTLY ≤30 chars — count every character
  "new_descriptions": array of 3-4 strings, each STRICTLY ≤90 chars
  "final_url": copy from old ad's final_url unless campaign context suggests a better page
  "path1": optional display-URL segment ≤15 chars (e.g. "dentures")
  "path2": optional display-URL segment ≤15 chars (e.g. "grafton-ma")
  "ad_group_resource": copy from the ad_performance row (required)
  Use replace_ad ONLY when: CTR < 50% of campaign average for 30+ days with ≥200 impressions,
  OR zero conversions after spending ≥ max($30, 5× daily budget), OR impressions < 100 in 30 days when budget allows.
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
    - cost_30d_usd ≥ max($30, 5× daily_budget_usd) AND conversions_30d = 0 AND lead_count_30d = 0 AND impressions_30d ≥ 100
    - performance_tier is "weak" or "cold"
    - The campaign has at least 2 active ad groups (never pause the campaign's ONLY ad group)
  This is a last resort — prefer replace_ad or keyword changes within the group first.
  Limit: only ONE pause_ad_group per campaign per optimizer run.

For update_geo_targeting (AI geo radius / ZIP recommendation — requires admin approval):
  "campaign_resource": campaign resource name (from data)
  "proposed_radius_miles": integer — recommended new radius
  "add_zip_codes": list of ZIP code strings to ADD to targeting (empty list if none)
  "remove_zip_codes": list of ZIP code strings to REMOVE from targeting (empty list if none)
  "geo_rationale": 2-3 sentence explanation grounded in the geo_signals data (distance bands, leakage alerts, low/high perf locs)
  Safety rules you MUST follow (violations will be rejected automatically):
  - Only ONE update_geo_targeting recommendation per campaign per optimizer run
  - Never propose an empty location set (at least 5 ZIPs or a radius must remain)
  - Emergency campaigns: proposed_radius_miles MUST be ≤ 10 — never exceed this
  - Implant/All-on-4 campaigns: proposed_radius_miles MUST be ≥ 15 — never shrink below
  - Only recommend if geo_signals data has ≥ 30 clicks in at least one location being changed
  - If geo_signals is absent or has insufficient data, do NOT return update_geo_targeting

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
- CALLOUT_EXTENSION → "add_asset" operation with asset_type="CALLOUT" and callout_texts array
- STRUCTURED_SNIPPET_EXTENSION → "add_asset" operation with asset_type="STRUCTURED_SNIPPET", snippet_header and values
- SITELINK_EXTENSION / CALL_EXTENSION → advisory only (managed by wizard — do not generate add_asset for these)
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
- For estimated_monthly_impact.savings_usd: use the keyword/search term cost data to estimate realistically. For waste_reduction ops (negatives, pauses): savings = the monthly spend being wasted. For conversion_lift ops (ad copy, landing page): savings = estimated CPL reduction × monthly lead volume. For bid_efficiency: savings = bid delta × monthly clicks. Use 0 if genuinely unknown.

LEAD QUALITY INTELLIGENCE (LQI) — high-signal context for this campaign:
The "lqi" field in the data contains six sub-fields. Use them as follows:

1. lqi.sources — quality_score per lead source (smile_tool, contact_form, pearly, gads_call).
   If a source feeding this campaign has quality_score < 40 AND leads >= 10, flag it in your
   reason and prefer recommendations that pull spend AWAY from that source's traffic
   (e.g. add_negative_keyword on bad search terms, decrease_bid on keywords feeding it).
   Never recommend pausing the campaign solely on source score — source mix is informational.

2. lqi.calls — campaign-scoped Google Ads call data:
   - total_calls, short_calls (<60s), short_pct, missed_calls, avg_duration_sec
   - shortest[]: up to 10 shortest calls with transcript_snippet
   If short_pct >= 0.40 OR missed_calls >= 3, return add_negative_keyword for any obvious
   wrong-intent pattern visible in transcript_snippets (e.g. "wrong number", "looking for
   [other practice]"). If transcripts show qualified callers hanging up on hold, return a
   claude_advisory describing the front-desk issue — do NOT pause keywords in that case.

3. lqi.bad_search_terms — terms with $5+ spend that produced 0 leads, classified into
   "spanish" | "competitor" | "wrong_intent" | "zero_lead". For EACH term where reason is
   "spanish" OR "competitor" OR "wrong_intent" → return add_negative_keyword with
   match_type="PHRASE". For "zero_lead", only flag if cost >= $20.

4. lqi.schedule — CAMPAIGN-TYPE-AWARE SCHEDULE ANALYSIS:
   Data: lqi.schedule.hotspots = [{dow, dow_name, hour, calls, short_calls, short_pct, in_office_hours, flag}]
         lqi.schedule.by_hour   = [{hour, calls, short_calls, missed, in_office_hours}]
         lqi.schedule.by_dow    = [{dow, dow_name, calls, short_calls, in_office_hours}]

   FIRST determine this campaign's intent type from its name:
   - EMERGENCY type: contains "emergency", "urgent", "pain", "toothache", "broken", "same day", "same-day"
   - ELECTIVE type: contains "implant", "veneer", "denture", "invisalign", "smile", "cosmetic", "whitening"
   - GENERAL type: everything else

   THEN apply these rules:

   EMERGENCY campaigns:
   - Any hotspot with in_office_hours=false AND calls >= 3 is waste (emergency patients need immediate answers).
   - Recommend pausing overnight hours (9pm–7am) if any after-hours hotspot shows short_pct > 0.35.
   - If by_dow shows weekend day with short_pct > 0.50 AND calls >= 3 → recommend DOW suppression.
   - Return: claude_advisory with specific hours/days to suppress, framed as "patients who call after hours
     will immediately call a competitor who answers — this spend is nearly zero-ROI."

   ELECTIVE campaigns:
   - Evening hours (6pm–9pm) can still convert — research happens at home. Do NOT suppress these.
   - Flag only extreme overnight hours (11pm–6am) with calls >= 5 and short_pct > 0.50.
   - Prefer recommending bid adjustments (change_bid_strategy) over full suppression.

   GENERAL campaigns:
   - Flag hotspots where in_office_hours=false AND short_pct >= 0.50 AND calls >= 3.
   - Recommend schedule tightening as a conservative spend optimization.

   Always cite specific data. Budget discipline beats spend volume for a small practice.

5. lqi.cold_leads — cold rate by utm_campaign, source, keyword + time_to_first_contact
   medians. If by_keyword shows a keyword with cold_rate >= 0.7 AND leads >= 5, recommend
   decrease_bid or pause_keyword. If no_staff_contact_pct >= 0.30, return a claude_advisory
   naming the staff follow-up gap.

6. lqi.no_shows — no-show rate by campaign/source + reminder stats + lead age at booking.
   If by_campaign for THIS campaign shows no_show_rate >= 0.25 AND booked >= 5, return a
   claude_advisory. If reminders.no_show_no_reminders_pct > 0.5, mention the reminder gap.

7. lqi.geo — geo intelligence signals for this campaign:
   Structure:
     lqi.geo.by_campaign[THIS_CAMPAIGN].targeted[] — locations in the campaign's current geo targeting
       Each entry: location_name, distance_band, clicks, cost, conversions, cvr, cpl
     lqi.geo.by_campaign[THIS_CAMPAIGN].physical[] — where users ACTUALLY were when they clicked
       Same fields. Physical rows NOT in the targeted set = demand leakage (people converting outside our targeting)
     lqi.geo.by_campaign[THIS_CAMPAIGN].leakage_alerts[] — high-value physical locations not yet in targeting
     lqi.geo.by_campaign[THIS_CAMPAIGN].low_perf_locs[] — targeted locations with clicks≥30 and CVR < 50% of avg
     lqi.geo.by_campaign[THIS_CAMPAIGN].high_perf_locs[] — targeted locations with CVR ≥ 120% of avg
     lqi.geo.by_campaign[THIS_CAMPAIGN].distance_band_rollup — {band: {clicks, conversions, cost}}

   Also injected (not from LQI): "geo_defaults" field in the campaign data contains the
   _GEO_DEFAULTS_BY_TYPE profile for this campaign type (target_radius, must_include_zips, etc.)

   How to use:
   - If leakage_alerts has entries with conversions ≥ 1: propose update_geo_targeting to ADD those ZIPs
   - If low_perf_locs has location(s) with clicks ≥ 30 and CVR < 30% of avg: propose removing them via update_geo_targeting or geo_exclusion
   - If distance_band_rollup shows 25+ band has lowest CVR AND highest cost: propose shrinking radius
   - If distance_band_rollup shows strong CVR in 15-25 band for an EMERGENCY campaign: surface as
     advisory only (emergency constraint overrides data — do NOT propose expanding beyond 10 mi)
   - Always cite specific band CVR numbers from the rollup in your geo_rationale
   - MINIMUM DATA FLOOR: do not recommend update_geo_targeting unless at least one location has ≥ 30 clicks

LQI is signal, not gospel. Cross-check each LQI-driven rec against keyword_performance and
ad_performance — never invent a resource name to satisfy an LQI flag.""" + rsa_note + geo_note + ad_perf_note + ag_perf_note + page_intel_note + campaign_brief_note + competitor_intel_note + planned_build_note + budget_feasibility_note + intent_signals_note + lifecycle_note + budget_constrained_note + skag_note + assets_note + _build_institutional_memory_note(campaign) + feedback_block + _build_mcp_decisions_note(campaign)

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
            _add_asset_seen: set[tuple] = set()  # (campaign_resource, asset_type) — max 1 per run
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
                    # PR 7: conquest keyword protection — never stage intentional competitor targets as negatives
                    if conquest_keywords_protected:
                        import re as _re_ck
                        kw_text = item.get("keyword_text", "").strip().lower()
                        _conquest_hit = False
                        for ck in conquest_keywords_protected:
                            if len(ck) < 4:
                                continue  # too short to match safely
                            # word-boundary match: conquest term appears as a whole word in the negative
                            if _re_ck.search(r'\b' + _re_ck.escape(ck) + r'\b', kw_text):
                                _conquest_hit = True
                                break
                            # reverse: the proposed negative term is a whole word inside the conquest phrase
                            if len(kw_text) >= 4 and _re_ck.search(r'\b' + _re_ck.escape(kw_text) + r'\b', ck):
                                _conquest_hit = True
                                break
                        if _conquest_hit:
                            logger.warning(
                                f"Dropping add_negative_keyword '{item.get('keyword_text','')}' — "
                                f"matches conquest keyword (intentional target)"
                            )
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
                elif op == "update_geo_targeting":
                    # ── Safety guards for geo targeting changes ──────────────────────────────
                    # Ensure campaign_resource is populated and valid
                    cr = item.get("campaign_resource", "")
                    if cr and cr not in valid_camp_resources:
                        if campaign_resource:
                            item["campaign_resource"] = campaign_resource
                        else:
                            logger.warning("Dropping update_geo_targeting — no valid campaign_resource")
                            continue
                    # Require proposed_radius_miles
                    radius = item.get("proposed_radius_miles")
                    if not radius:
                        logger.warning("Dropping update_geo_targeting — missing proposed_radius_miles")
                        continue
                    # Emergency campaigns: hard cap at 10 mi
                    # Use _classify_campaign (string return) for reliable type comparison — avoids
                    # fragile dict-identity checks that break if _GEO_DEFAULTS_BY_TYPE is refactored.
                    _camp_type = _classify_campaign(campaign)
                    if _camp_type == "emergency" and int(radius) > 10:
                        logger.warning(
                            f"Dropping update_geo_targeting — Emergency campaign radius {radius} > 10 mi safety cap"
                        )
                        continue
                    # Implants campaigns: must not shrink below 15 mi
                    if _camp_type == "implants" and int(radius) < 15:
                        logger.warning(
                            f"Dropping update_geo_targeting — Implants campaign radius {radius} < 15 mi safety floor"
                        )
                        continue
                    # Never result in an empty location set
                    add_zips = item.get("add_zip_codes") or []
                    remove_zips = item.get("remove_zip_codes") or []
                    if remove_zips and not add_zips and not radius:
                        logger.warning("Dropping update_geo_targeting — would remove locations without adding any")
                        continue
                    # Require geo_rationale
                    if not item.get("geo_rationale"):
                        item["geo_rationale"] = item.get("reason", "Geo optimization based on performance data")
                elif op == "add_asset":
                    # ── Validate add_asset ops (callouts + structured snippets) ──────────────
                    asset_type = item.get("asset_type", "")
                    if asset_type not in ("CALLOUT", "STRUCTURED_SNIPPET"):
                        logger.warning(f"Dropping add_asset — unsupported asset_type '{asset_type}'")
                        continue
                    # Enforce max 1 add_asset per (campaign_resource, asset_type) per optimizer run
                    _cr_for_dedup = item.get("campaign_resource") or campaign_resource
                    if not _cr_for_dedup:
                        logger.warning("Dropping add_asset — missing campaign_resource")
                        continue
                    _dedup_key = (_cr_for_dedup, asset_type)
                    if _dedup_key in _add_asset_seen:
                        logger.warning(
                            f"Dropping add_asset — duplicate {asset_type} for same campaign in this run"
                        )
                        continue
                    if asset_type == "CALLOUT":
                        texts = item.get("callout_texts") or []
                        if not isinstance(texts, list) or len(texts) < 3:
                            logger.warning(
                                f"Dropping add_asset CALLOUT — need ≥3 callout_texts, got {len(texts) if isinstance(texts, list) else texts!r}"
                            )
                            continue
                        # Clip to 25 chars and filter blanks
                        texts = [t[:25].strip() for t in texts if isinstance(t, str) and t.strip()]
                        if len(texts) < 3:
                            logger.warning("Dropping add_asset CALLOUT — fewer than 3 non-empty texts after cleaning")
                            continue
                        if len(texts) > 10:
                            texts = texts[:10]
                        item["callout_texts"] = texts
                    elif asset_type == "STRUCTURED_SNIPPET":
                        header = item.get("snippet_header", "")
                        if header not in VALID_SNIPPET_HEADERS:
                            logger.warning(
                                f"Dropping add_asset STRUCTURED_SNIPPET — invalid header '{header}'. "
                                f"Must be one of: {sorted(VALID_SNIPPET_HEADERS)}"
                            )
                            continue
                        values = item.get("values") or []
                        if not isinstance(values, list) or len(values) < 3:
                            logger.warning(
                                f"Dropping add_asset STRUCTURED_SNIPPET — need ≥3 values, got {len(values) if isinstance(values, list) else values!r}"
                            )
                            continue
                        # Clip to 25 chars and filter blanks
                        values = [v[:25].strip() for v in values if isinstance(v, str) and v.strip()]
                        if len(values) < 3:
                            logger.warning("Dropping add_asset STRUCTURED_SNIPPET — fewer than 3 non-empty values after cleaning")
                            continue
                        if len(values) > 10:
                            values = values[:10]
                        item["values"] = values
                    # Mark this (campaign, asset_type) pair as seen for dedup
                    _add_asset_seen.add(_dedup_key)

                # ── Recently-applied suppression ──────────────────────────────────────
                # Don't re-surface an action that was successfully pushed in the last 14 days.
                # Google's data takes 24-72h to reflect changes — re-suggesting too soon
                # just creates noise and can double-apply bids/budgets.
                # Exempt: claude_advisory (no side effects), add_negative_keyword (handled
                # separately by _negative_already_handled), add_exact_keyword (adding again
                # is harmless — Google dedupes), enable_keyword.
                _exempt_from_recency = {"claude_advisory", "add_negative_keyword",
                                        "add_exact_keyword", "enable_keyword"}
                if op not in _exempt_from_recency:
                    _entity = (
                        item.get("keyword_text") or item.get("entity_name") or
                        item.get("ad_group_name") or item.get("location_name") or
                        item.get("headline") or item.get("insight", "")[:60]
                    )
                    _cr = item.get("campaign_resource", "") or campaign_resource
                    if _was_recently_applied(op, _entity, campaign_resource=_cr, days=14):
                        logger.info(
                            f"Suppressing '{op}' for '{_entity}' — already applied within 14 days"
                        )
                        continue

                validated.append(item)
            logger.info(f"Claude returned {len(arr)} recs, {len(validated)} passed validation")
            # Lifecycle sieve — defense in depth: convert any violated ops to advisories
            _conv_30d = sum(k.get("conversions", 0) for k in keyword_perf)
            sieved = _lifecycle_sieve(validated, lifecycle or {}, _conv_30d, camp_settings or {})
            if len(sieved) != len(validated):
                logger.info(
                    f"[lifecycle_sieve] {len(validated) - len(sieved)} ops blocked, "
                    f"{len(sieved)} recs after sieve"
                )
            # Budget constraint sieve — convert change_budget increases to advisories
            if budget_constrained:
                sieved = _budget_constraint_sieve(sieved, camp_settings or {})
            # Experiment sieve — block structural changes mid-test
            try:
                from database import list_ab_experiments as _lab
                _running = _lab(status="RUNNING")
                _exp_camps = set()
                for _re in _running:
                    if _re.get("base_campaign_name"):
                        _exp_camps.add(_re["base_campaign_name"].strip().lower())
                    if _re.get("trial_campaign_name"):
                        _exp_camps.add(_re["trial_campaign_name"].strip().lower())
                if _exp_camps:
                    sieved = _experiment_sieve(sieved, _exp_camps, current_campaign=campaign)
            except Exception as _es_err:
                logger.debug(f"experiment_sieve failed (non-fatal): {_es_err}")
            return sieved
    except Exception as e:
        logger.warning(f"Claude advisory call failed (non-fatal): {e}")
    return []


def _budget_constraint_sieve(
    ops: list,
    camp_settings: dict,
    budget_by_campaign: dict | None = None,
) -> list:
    """
    Post-filter when budget_constrained=True.

    Per-campaign mode (budget_by_campaign=None):
      - camp_settings is the single campaign's settings dict with 'daily_budget_usd'.
      - Any change_budget rec increasing daily budget → converted to claude_advisory.
      - If daily_budget_usd is missing/zero, skip sieving (unknown baseline, don't block).

    Account-level mode (budget_by_campaign is a dict of campaign_name → daily_budget_usd):
      - Each rec's current budget is resolved from budget_by_campaign keyed by campaign_name.
      - If a campaign's budget can't be looked up, the rec passes through unchanged.

    Budget reductions are always allowed.
    Spend-increasing change_bid_strategy (MAXIMIZE_CLICKS/MAXIMIZE_CONVERSIONS without a cap)
    are also converted to advisories.
    """
    # Strategies that increase spend when used without a bid cap
    _UNCAPPED_SPEND_STRATEGIES = {"MAXIMIZE_CLICKS", "MAXIMIZE_CONVERSIONS"}

    # Per-campaign: resolve current budget once
    _per_camp_budget: float | None = None
    if budget_by_campaign is None:
        raw = camp_settings.get("daily_budget_usd")
        if raw is None or float(raw or 0) <= 0.0:
            # Unknown current budget — cannot safely determine what's an increase; pass everything
            logger.warning(
                "[budget_constraint_sieve] camp_settings missing daily_budget_usd — skipping sieve"
            )
            return ops
        _per_camp_budget = float(raw)

    filtered = []
    for op in ops:
        operation = op.get("operation", "")

        # ── change_budget guard ───────────────────────────────────────────────
        if operation == "change_budget":
            # Resolve current budget for this rec
            if budget_by_campaign is not None:
                cn = op.get("campaign_name", "")
                raw_b = budget_by_campaign.get(cn)
                if raw_b is None:
                    filtered.append(op)  # unknown campaign — pass through
                    continue
                current_budget = float(raw_b or 0.0)
                if current_budget <= 0.0:
                    filtered.append(op)  # unknown baseline — pass through
                    continue
            else:
                current_budget = _per_camp_budget  # already validated above

            # Validate proposed value
            proposed_raw = op.get("new_daily_budget_usd")
            if proposed_raw is None:
                filtered.append(op)  # missing field — let executor handle it
                continue
            try:
                proposed = float(proposed_raw)
            except (TypeError, ValueError):
                logger.warning(
                    f"[budget_constraint_sieve] Non-numeric new_daily_budget_usd: {proposed_raw!r}"
                )
                filtered.append(op)
                continue

            if proposed <= 0.0:
                logger.warning(
                    f"[budget_constraint_sieve] Dropping change_budget — invalid proposed=${proposed:.2f}"
                )
                continue  # drop nonsense, not even advisory

            if proposed == current_budget:
                continue  # no-op, drop silently

            if proposed > current_budget:
                reason = op.get("reason", "")
                camp_lbl = op.get("campaign_name", "?")
                filtered.append({
                    "operation": "claude_advisory",
                    "insight": (
                        f"BUDGET_CONSTRAINED: change_budget to ${proposed:.2f}/day suppressed "
                        f"(Budget Constrained mode is ON — current budget ${current_budget:.2f}/day). "
                        f"Original reasoning: {reason}"
                    ),
                    "reason": reason,
                    "campaign_name": op.get("campaign_name", ""),
                    "estimated_monthly_impact": op.get("estimated_monthly_impact", {}),
                })
                logger.info(
                    f"[budget_constraint_sieve] Blocked change_budget increase for "
                    f"'{camp_lbl}': ${current_budget:.2f} → ${proposed:.2f}/day"
                )
                continue
            # proposed < current_budget → decrease is allowed, pass through
            filtered.append(op)
            continue

        # ── change_bid_strategy guard — block uncapped spend-increasing strategies ──
        if operation == "change_bid_strategy":
            new_strat = (op.get("bid_strategy") or "").upper()
            has_cap = bool(
                op.get("target_cpa_micros") or
                op.get("target_roas") or
                op.get("cpc_bid_ceiling_micros")
            )
            if new_strat in _UNCAPPED_SPEND_STRATEGIES and not has_cap:
                reason = op.get("reason", "")
                camp_lbl = op.get("campaign_name", "?")
                filtered.append({
                    "operation": "claude_advisory",
                    "insight": (
                        f"BUDGET_CONSTRAINED: change_bid_strategy to {new_strat} (uncapped) suppressed "
                        f"(Budget Constrained mode is ON — this strategy typically increases spend). "
                        f"Original reasoning: {reason}"
                    ),
                    "reason": reason,
                    "campaign_name": op.get("campaign_name", ""),
                    "estimated_monthly_impact": op.get("estimated_monthly_impact", {}),
                })
                logger.info(
                    f"[budget_constraint_sieve] Blocked uncapped change_bid_strategy to "
                    f"{new_strat} for '{camp_lbl}'"
                )
                continue

        filtered.append(op)
    return filtered


def _experiment_sieve(
    ops: list,
    active_experiment_campaigns: set,
    current_campaign: str = "",
) -> list:
    """
    Block structural changes to campaigns that are mid-experiment.
    Negatives and advisories are still allowed — only ops that change
    bids/budgets/strategy/pauses are blocked to preserve test integrity.

    current_campaign: the campaign name being optimized in per-campaign mode.
    Per-campaign Claude ops often omit campaign_name from each op (it's
    implicit context), so we fall back to current_campaign when the field is
    absent so the sieve correctly guards structural changes.
    """
    _BLOCKED_MID_EXPERIMENT = {
        "increase_bid", "decrease_bid", "change_budget",
        "change_bid_strategy", "pause_keyword", "pause_ad_group",
        "replace_ad", "change_match_type",
    }
    if not active_experiment_campaigns:
        return ops

    _current = (current_campaign or "").strip().lower()

    filtered = []
    for op in ops:
        op_type = op.get("operation", "")
        # Use op's own campaign_name if present; fall back to the ambient campaign
        camp = (op.get("campaign_name") or _current or "").strip().lower()
        if op_type in _BLOCKED_MID_EXPERIMENT and camp in active_experiment_campaigns:
            filtered.append({
                "operation": "claude_advisory",
                "campaign_name": op.get("campaign_name", ""),
                "insight": (
                    f"EXPERIMENT_BLOCKED: {op_type} suppressed — campaign '{op.get('campaign_name','')}' "
                    f"is mid-experiment. Structural changes during an A/B test invalidate the comparison. "
                    f"Wait until the experiment concludes before making this change."
                ),
                "reason": op.get("reason", ""),
            })
        else:
            filtered.append(op)
    return filtered


def _build_lqi_account_summary(lqi: dict) -> dict:
    """
    Build the account-level LQI summary injected into _call_claude_account_level.
    Includes source scoreboard, schedule summary, cross-campaign bad terms, calls, cold leads, no-shows.
    """
    src_q = lqi.get("sources") or {}
    source_scoreboard = sorted(
        [
            {
                "source":        s,
                "leads":         v.get("leads", 0),
                "booked_rate":   v.get("booked_rate", 0),
                "showed_rate":   v.get("showed_rate", 0),
                "cold_rate":     v.get("cold_rate", 0),
                "od_match_rate": v.get("od_match_rate", 0),
                "quality_score": v.get("quality_score", 0),
            }
            for s, v in src_q.items()
        ],
        key=lambda x: -x["quality_score"],
    )
    sched = lqi.get("schedule") or {}
    schedule_summary = {
        "by_dow":             sched.get("by_dow", []),
        "hotspots":           (sched.get("hotspots") or [])[:10],
        "practice_hours_raw": sched.get("practice_hours_raw", ""),
    }
    bt = lqi.get("search_terms") or {}
    bad_terms_account = {
        "totals":      bt.get("totals", {}),
        "by_campaign": {
            c: terms[:10]
            for c, terms in (bt.get("by_campaign") or {}).items()
        },
    }
    calls_lqi = lqi.get("calls") or {}
    calls_account = {
        "by_campaign":      calls_lqi.get("by_campaign", {}),
        "shortest_overall": (calls_lqi.get("shortest_overall") or [])[:10],
    }
    return {
        "source_scoreboard": source_scoreboard,
        "schedule_summary":  schedule_summary,
        "bad_search_terms":  bad_terms_account,
        "calls":             calls_account,
        "cold_leads":        lqi.get("cold_leads", {}),
        "no_shows":          lqi.get("no_shows", {}),
    }


# ── Budget reallocation thresholds ────────────────────────────────────────────
_REALLOC_RECEIVER_BUDGET_LOST_MIN: float = 0.30  # must be losing ≥30% to budget to qualify as receiver
_REALLOC_DONOR_ROAS_MIN: float           = 1.0   # donor must have ROAS ≥ 1 (not losing money)
_REALLOC_DONOR_BUDGET_LOST_MAX: float    = 0.15  # donor should NOT be budget-constrained itself
_REALLOC_MIN_FLOOR_USD: float            = 15.0  # never reduce a campaign below $15/day
_REALLOC_MAX_SINGLE_TRANSFER_USD: float  = 20.0  # cap single transfer per run
_REALLOC_MIN_SPEND_FOR_SIGNAL: float     = 5.0   # campaign must have spent ≥$5 to generate signal


def _build_budget_reallocation_signals(
    camp_perf: dict,
    budget_constrained: bool = False,
) -> dict:
    """
    Pre-compute budget reallocation donor/receiver candidates from camp_perf.

    Returns:
    {
        "reallocation_allowed": bool,   # False when budget_constrained=True
        "total_daily_budget_usd": float,
        "receivers": [                  # budget-constrained, high-signal campaigns
            {
                "campaign_name": str,
                "daily_budget_usd": float,
                "search_budget_lost_is": float,
                "roas_30d": float | None,
                "production_30d": float,
                "lifecycle_stage": str,
            }
        ],
        "donors": [                     # under-spending, lower-priority campaigns
            {
                "campaign_name": str,
                "daily_budget_usd": float,
                "search_budget_lost_is": float,
                "roas_30d": float | None,
                "headroom_usd": float,  # max transferable = daily_budget - floor
            }
        ],
        "note": str,   # human-readable summary of why reallocation is or isn't recommended
    }
    """
    if budget_constrained:
        return {
            "reallocation_allowed": False,
            "total_daily_budget_usd": 0.0,
            "receivers": [],
            "donors": [],
            "note": "Budget constrained mode is active — no budget increases allowed.",
        }

    total_daily = 0.0
    receivers = []
    donors = []

    for cn, cp in camp_perf.items():
        daily = float(cp.get("daily_budget_usd") or 0)
        total_daily += daily

        # Skip new/unknown campaigns — not enough data for reallocation judgment
        if cp.get("lifecycle_stage") in ("new", "unknown"):
            continue
        if cp.get("in_learning_period"):
            continue

        spend = float(cp.get("spend_30d") or 0)
        if spend < _REALLOC_MIN_SPEND_FOR_SIGNAL:
            continue

        budget_lost = float(cp.get("search_budget_lost_is") or 0)
        roas = cp.get("roas_30d")  # may be None
        prod = float(cp.get("production_30d") or 0)

        # Receiver: budget-constrained campaign that is generating value
        if budget_lost >= _REALLOC_RECEIVER_BUDGET_LOST_MIN and daily > 0:
            receivers.append({
                "campaign_name":         cn,
                "daily_budget_usd":      round(daily, 2),
                "search_budget_lost_is": round(budget_lost, 3),
                "roas_30d":              roas,
                "production_30d":        round(prod, 2),
                "lifecycle_stage":       cp.get("lifecycle_stage", "unknown"),
            })

        # Donor: not budget-constrained, ROAS ≥ floor (not actively losing money),
        # and has headroom above the $15/day minimum floor
        headroom = max(daily - _REALLOC_MIN_FLOOR_USD, 0.0)
        if (
            budget_lost <= _REALLOC_DONOR_BUDGET_LOST_MAX
            and (roas is None or roas >= _REALLOC_DONOR_ROAS_MIN)
            and headroom > 0
            and daily > 0
        ):
            donors.append({
                "campaign_name":         cn,
                "daily_budget_usd":      round(daily, 2),
                "search_budget_lost_is": round(budget_lost, 3),
                "roas_30d":              roas,
                "headroom_usd":          round(min(headroom, _REALLOC_MAX_SINGLE_TRANSFER_USD), 2),
            })

    # Sort: receivers by budget_lost descending, donors by headroom descending
    receivers.sort(key=lambda x: -x["search_budget_lost_is"])
    donors.sort(key=lambda x: -x["headroom_usd"])

    if receivers and donors:
        note = (
            f"{len(receivers)} receiver(s) losing budget IS, "
            f"{len(donors)} donor(s) with headroom. "
            f"Max single transfer: ${_REALLOC_MAX_SINGLE_TRANSFER_USD:.0f}/day. "
            f"Net account budget must stay at ${total_daily:.2f}/day."
        )
    elif receivers and not donors:
        note = f"{len(receivers)} receiver(s) need budget but no eligible donors found (all at floor or also budget-constrained)."
    elif donors and not receivers:
        note = "No campaigns are budget-constrained. No reallocation needed."
    else:
        note = "Insufficient data for reallocation analysis."

    return {
        "reallocation_allowed": True,
        "total_daily_budget_usd": round(total_daily, 2),
        "receivers": receivers,
        "donors": donors,
        "note": note,
    }


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
    existing_negatives_by_campaign: dict | None = None,
    memory_digest: dict | None = None,
    lqi: dict | None = None,
    # PR 5: account-wide competitor intel union
    competitor_intel_union: dict | None = None,
    # Budget constraint: True = no budget increases allowed
    budget_constrained: bool = False,
    # campaign_name → daily_budget_usd map — used by budget sieve to allow decreases
    budget_by_campaign: dict | None = None,
    # Campaign lifecycle map: campaign_name → build_lifecycle_block() dict
    # Used to inject stage/age into camp_perf and enforce lifecycle-aware prompt rules
    campaign_lifecycle_map: dict | None = None,
    # Raw campaign_settings dict (resource_name-keyed) from _get_campaign_settings()
    # Used to enrich camp_perf with search_budget_lost_is, roas, bidding_strategy_type
    campaign_settings_raw: dict | None = None,
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

        # ── Cannibalization signal ────────────────────────────────────────────
        # For each search term appearing in multiple ACTIVE campaigns, compute
        # which campaign converts it and which doesn't.  Only flag pairs where
        # BOTH campaigns are currently active so we never recommend a negative
        # that would leave a term uncovered if the "winner" campaign is later paused.
        _active_campaign_names: set = set()
        try:
            from database import get_all_campaigns as _get_all_camps
            for _c in (_get_all_camps() or []):
                if (_c.get("status") or "").upper() == "ACTIVE":
                    _active_campaign_names.add((_c.get("campaign_name") or "").strip())
        except Exception as _can_err:
            logger.debug(f"cannibalization: campaign status fetch failed: {_can_err}")

        # Build per-term conversion map from search terms data
        _term_conv: dict = {}   # term -> {campaign_name: {clicks, conversions, cost}}
        for _st in all_search_terms:
            _t = (_st.get("search_term") or "").strip().lower()
            _c = (_st.get("campaign") or "").strip()
            if not _t or not _c:
                continue
            if _t not in _term_conv:
                _term_conv[_t] = {}
            if _c not in _term_conv[_t]:
                _term_conv[_t][_c] = {"clicks": 0, "conversions": 0.0, "cost": 0.0}
            _term_conv[_t][_c]["clicks"]      += int(_st.get("clicks", 0) or 0)
            _term_conv[_t][_c]["conversions"] += float(_st.get("conversions", 0) or 0)
            _term_conv[_t][_c]["cost"]        += float(_st.get("cost", 0) or 0)

        cannibalization_signals: list = []
        for _term, _camp_data in _term_conv.items():
            _active_camps_for_term = {c: d for c, d in _camp_data.items() if c in _active_campaign_names}
            if len(_active_camps_for_term) < 2:
                continue   # only flag when 2+ active campaigns compete for same term
            # Find winner (most conversions) and losers (zero conversions + spend > 0)
            _winner = max(_active_camps_for_term, key=lambda c: _active_camps_for_term[c]["conversions"])
            _winner_conv = _active_camps_for_term[_winner]["conversions"]
            for _loser, _ld in _active_camps_for_term.items():
                if _loser == _winner:
                    continue
                if _ld["cost"] > 0 and _ld["conversions"] == 0 and _winner_conv > 0:
                    cannibalization_signals.append({
                        "search_term":    _term,
                        "loser_campaign": _loser,
                        "loser_clicks":   _ld["clicks"],
                        "loser_cost":     round(_ld["cost"], 2),
                        "winner_campaign": _winner,
                        "winner_conversions": _winner_conv,
                        "both_active":    True,  # guaranteed by filter above
                        "note": (
                            f"Both campaigns ACTIVE. Safe to add negative to '{_loser}'. "
                            f"If '{_winner}' is later paused, remove this negative or '{_loser}' "
                            f"will go dark for this query."
                        ),
                    })
        # Cap to 20 highest-cost signals
        cannibalization_signals.sort(key=lambda x: -x["loser_cost"])
        cannibalization_signals = cannibalization_signals[:20]

        # Build name-keyed settings lookup from raw resource-keyed campaign_settings
        _cs_raw = campaign_settings_raw or {}
        _name_to_settings: dict = {}
        for _rn, _cs in _cs_raw.items():
            _cn = (_cs.get("campaign_name") or "").strip()
            if _cn:
                _name_to_settings[_cn] = _cs

        # Build per-campaign budget/performance summary
        _lc_map = campaign_lifecycle_map or {}
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
            _lc = _lc_map.get(cn) or {}
            _cs_entry = _name_to_settings.get(cn) or {}
            _daily_bud = (
                _cs_entry.get("daily_budget_usd")
                or (campaign_spend.get(cn, {}).get("daily_budget_usd") if isinstance(campaign_spend.get(cn), dict) else None)
            )
            _roas_30d = round(prod / spend, 2) if spend > 0 else None
            camp_perf[cn] = {
                "campaign_resource": cr,
                "spend_30d": round(spend, 2),
                "clicks": clicks,
                "calls": calls,
                "booked_calls": booked,
                "production_30d": round(prod, 2),
                "roas_30d": _roas_30d,          # production / spend; None if no spend
                "daily_budget_usd": _daily_bud,
                "search_budget_lost_is": _cs_entry.get("search_budget_lost_is"),
                "search_rank_lost_is": _cs_entry.get("search_rank_lost_is"),
                "bidding_strategy_type": _cs_entry.get("bidding_strategy_type"),
                # Lifecycle awareness — used by account-level Claude to respect learning phase
                "lifecycle_stage": _lc.get("stage", "unknown"),
                "days_since_launch": _lc.get("days_since_launch"),
                "in_learning_period": _lc.get("in_learning_period", True),  # conservative default
            }

        # ── GI-3: Budget reallocation signal ─────────────────────────────────
        # Pre-compute donor/receiver candidates so Claude has a structured signal
        # rather than having to reason over raw campaign_performance fields.
        _realloc_signals: dict = _build_budget_reallocation_signals(
            camp_perf=camp_perf,
            budget_constrained=budget_constrained,
        )

        # Fetch call quality flags for account-level signal
        _call_quality_flags: dict = {}
        try:
            from database import get_call_flag_summary
            _call_quality_flags = get_call_flag_summary(days=30)
        except Exception as _cqf_err:
            logger.debug(f"call_flag_summary fetch failed (non-fatal): {_cqf_err}")

        # GI-3: N-gram waste signals
        _ngram_waste_signals: dict = {}
        try:
            from ngram_analysis import compute_ngram_waste
            _ngram_waste_signals = compute_ngram_waste(days=30)
        except Exception as _ngram_err:
            logger.debug(f"ngram_analysis fetch failed (non-fatal): {_ngram_err}")

        # Read account budget constraint
        _account_budget = 0.0
        try:
            _acct_budget_raw = _get_setting("account_monthly_budget") or "0"
            _account_budget = float(_acct_budget_raw)
        except Exception:
            pass

        # A/B experiment signals — feed active experiments into account-level Claude
        _active_experiments: list = []
        try:
            from database import list_ab_experiments, get_ab_experiment_lead_metrics
            from experiment_metrics import get_gads_experiment_metrics, compute_winner_signal
            from datetime import date as _exp_date
            _running_exps = list_ab_experiments(status="RUNNING")
            for _exp in _running_exps[:5]:  # cap at 5 to avoid token bloat
                _exp_start = _exp.get("start_date") or ""
                _days = 0
                if _exp_start:
                    try:
                        _days = (_exp_date.today() - _exp_date.fromisoformat(_exp_start)).days
                    except Exception:
                        pass
                _gads_m = get_gads_experiment_metrics(
                    base_campaign_resource=_exp["base_campaign_resource"],
                    trial_campaign_resource=_exp.get("trial_campaign_resource", ""),
                    start_date=_exp_start,
                )
                _lead_m = get_ab_experiment_lead_metrics(
                    base_campaign_name=_exp.get("base_campaign_name", ""),
                    trial_campaign_name=_exp.get("trial_campaign_name", ""),
                    control_url=_exp.get("control_url", ""),
                    variant_url=_exp.get("variant_url", ""),
                    start_date=_exp_start,
                )
                _signal = compute_winner_signal(_gads_m, _lead_m, days_running=_days)
                _active_experiments.append({
                    "id":              _exp["id"],
                    "name":            _exp["experiment_name"],
                    "type":            _exp["experiment_type"],
                    "days_running":    _days,
                    "base_campaign":   _exp.get("base_campaign_name", ""),
                    "trial_campaign":  _exp.get("trial_campaign_name", ""),
                    "control_url":     _exp.get("control_url", ""),
                    "variant_url":     _exp.get("variant_url", ""),
                    "winner_signal":   _signal,
                })
        except Exception as _exp_err:
            logger.debug(f"experiment signals fetch failed (non-fatal): {_exp_err}")

        context = {
            "account_summary": summary,
            "account_monthly_budget_usd": _account_budget,  # 0 = not set
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
            # Per-campaign negatives: {campaign_name: [list of negative keyword texts]}
            # Used by STANDING NEGATIVE POLICY check to identify which campaigns are missing required negatives
            "negatives_by_campaign": {
                cn: sorted(kws)
                for cn, kws in (existing_negatives_by_campaign or {}).items()
            } if existing_negatives_by_campaign else {},
            "optimizer_memory": memory_digest or {},
            "call_quality_flags": _call_quality_flags,
            "lqi": _build_lqi_account_summary(lqi or {}),
            # PR 5: account-wide competitor intel
            "competitor_intel_union": competitor_intel_union or {},
            # GI-3: N-gram waste signals
            "ngram_waste_signals": _ngram_waste_signals,
            # GI-3: Budget reallocation candidates (pre-computed)
            "budget_reallocation_signals": _realloc_signals,
            # A/B experiment signals
            "active_ab_experiments": _active_experiments,
            # Cannibalization: terms where 2+ active campaigns compete and one converts, other doesn't
            "cannibalization_signals": cannibalization_signals,
            # Which campaigns are currently ACTIVE (used to validate cross-campaign negative safety)
            "active_campaign_names": sorted(_active_campaign_names),
        }

        # Account-level: use aggregate summary, no specific camp_settings
        acct_excellence_block = _build_excellence_block("", summary, {})

        # Budget constraint block for account-level prompt
        _acct_budget_constrained_note = ""
        if budget_constrained:
            _acct_budget_constrained_note = """

=== BUDGET CONSTRAINED MODE (MANDATORY) ===
The practice has enabled Budget Constrained mode. This means:
1. DO NOT recommend change_budget to increase any campaign's daily budget.
2. Budget reductions (trimming over-spending campaigns) are still allowed.
3. Google's recommendations that increase spend (RAISE_TARGET_CPA, EXPAND_TARGETING,
   MARGINAL_ROI_CAMPAIGN_BUDGET, CAMPAIGN_BUDGET) must be converted to claude_advisory
   observations, NOT change_budget or change_bid_strategy recs.
4. Focus on waste elimination, negative keywords, bid adjustments within current caps,
   and ad quality improvements to maximize ROI at current spend levels.
=== END BUDGET CONSTRAINED MODE ===
"""

        prompt = acct_excellence_block + GOOGLE_ADS_RULES + CAMPAIGN_INTENT_RULES + _acct_budget_constrained_note + """
You are the Chief Marketing Officer (CMO) for Grafton Dental Care, a private dental practice in Grafton, MA, performing an ACCOUNT-LEVEL portfolio review.

Think like a CMO: you are not optimizing individual campaigns in isolation — you are managing a portfolio of bets. Ask yourself:
- Is the overall budget allocated toward the services that drive the most patient lifetime value (implants, Invisalign, crowns > routine > emergency)?
- Are any campaigns cannibalizing each other's traffic or budget?
- Is there a campaign in learning phase that the team is over-touching? Should we leave it alone for 2–3 more weeks?
- Is there a gap in coverage — a high-value service (e.g. dental implants, Invisalign) with no campaign at all?
- Is the overall account burn rate on track vs the monthly budget, and is the mix right?

You have already reviewed individual campaigns. Now identify issues and opportunities that span the whole account or cannot be attributed to one campaign.

Return up to 15 ACCOUNT-LEVEL recommendations as a JSON array (standing policy negatives may consume several slots). Each must have:
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
0. BUDGET DISCIPLINE (check account_monthly_budget_usd first):
   - If account_monthly_budget_usd > 0: compare sum of campaign daily_budgets × 30 vs account budget.
     If campaigns are over the account budget, flag which campaign(s) to trim via change_budget.
     If campaigns are under the account budget AND one campaign has strong conversion data, suggest
     reallocating headroom to that campaign via change_budget (NOT to the weakest campaign).
   - IMPORTANT: Be skeptical of budget increase recommendations. Only recommend budget increases when:
     (a) the campaign has 3+ conversions in the window AND CPL is below industry average ($150 for dental)
     (b) the campaign is impression-share-limited (shown in camp_settings if available)
     (c) there is clear headroom within the account_monthly_budget
   - Never recommend increasing budget just because spend is low. Low spend without conversions = pause, not increase.
1. COMPETITOR NAMES appearing across multiple campaigns → add_negative_keyword (highest-spend campaign's resource)
2. BUDGET REBALANCING — if one campaign has 0 conversions/calls but high spend vs another with conversions → change_budget
3. BID STRATEGY — if a campaign has enough conversion data to switch strategies → change_bid_strategy
4. MISSING ASSETS — sitelinks/callouts that should exist on all campaigns but don't → add_asset
5. CROSS-CAMPAIGN CANNIBALIZATION — use the "cannibalization_signals" field:
   Each entry has: search_term, loser_campaign (spending with 0 conversions), loser_cost,
   winner_campaign (converting), winner_conversions, both_active (always true here).

   RULE: Only recommend add_negative_keyword for cannibalized terms when both_active=true.
   The negative goes on the loser_campaign (use campaign_resources[loser_campaign] for campaign_resource).

   CRITICAL — always pair each cannibalization negative with a claude_advisory that says:
   "This negative was added because [winner_campaign] is currently active and converting this term.
   If [winner_campaign] is paused or stopped, remove this negative from [loser_campaign] so it can
   recapture that traffic." This ensures the team knows the negative is conditional on both campaigns running.

   Do NOT add cannibalization negatives if either campaign is in learning phase (check lifecycle_stage).
   Do NOT flag terms where loser_cost < $1.00 (not worth the friction).
6. ACCOUNT HEALTH — any account-wide pattern not captured by individual campaign reviews
7. CALL EXPERIENCE: The field "call_quality_flags" in the data shows missed/short Google Ads calls
   flagged for follow-up. If missed_call_rate_pct > 15% OR any campaign has 3+ missed new-patient
   calls in 30d OR short_gads_calls > 5, return a claude_advisory. The advisory should name the
   specific campaigns bleeding qualified leads at the phone, cite exact counts, and suggest whether
   the issue is likely after-hours coverage, IVR routing, or call handling. Do NOT recommend pausing
   those campaigns — the spend is generating calls; the problem is downstream of the click.

STANDING NEGATIVE POLICY — GDC does NOT accept these patients (insurance/program restrictions):
The following terms must ALWAYS be negatives on EVERY active campaign. If any campaign in
"existing_negative_keywords" is missing one of these terms, recommend add_negative_keyword for that
campaign immediately — do not wait for it to appear in search terms:
  masshealth, mass health, medicaid, medicaid dentist, medicaid dental, medicare, chip dental,
  masshealth dentist, masshealth dental, free dental, low income dental, sliding scale dental,
  dental assistance program, state dental insurance

CHECK: For each active campaign in "campaign_resources", look up that campaign's negatives in
"negatives_by_campaign" (dict of campaign_name → list of negative keywords). If a campaign is
missing any of the required terms above, generate one add_negative_keyword rec per missing term
(BROAD match, campaign_resource from "campaign_resources"). This takes priority over all other
recommendations — fill these gaps first before considering other account-level issues.

IMPORTANT:
- Only flag competitor negatives here if they appear in multiple campaigns (single-campaign terms were already handled per-campaign)
- Use only campaign_resource values from the "campaign_resources" field in the data
- EXISTING NEGATIVES: The field "existing_negative_keywords" in the data lists keywords already live as negatives in Google Ads. Do NOT recommend add_negative_keyword for any term already in that list. Only suggest NEW terms not yet blocked.
- OPTIMIZER MEMORY: The field "optimizer_memory" in the data contains historical run summaries. Use it to: (1) avoid repeating rejected recommendations, (2) identify recurring patterns, (3) highlight new trends. Do not re-suggest anything in "rejected_patterns".
- Return ONLY a valid JSON array, no markdown, no explanation outside the array
- For estimated_monthly_impact.savings_usd: use the keyword/search term cost data to estimate realistically. For waste_reduction ops (negatives, pauses): savings = the monthly spend being wasted. For conversion_lift ops (ad copy, landing page): savings = estimated CPL reduction × monthly lead volume. For bid_efficiency: savings = bid delta × monthly clicks. Use 0 if genuinely unknown.

LEAD QUALITY INTELLIGENCE (LQI) — account-wide signals:
The "lqi" field in the account data contains pre-computed quality signals across all campaigns. Use them to generate account-level advisories:

1. SOURCE QUALITY SCOREBOARD (lqi.source_scoreboard):
   - Each item has: source, leads, booked_rate, showed_rate, cold_rate, od_match_rate, quality_score (0–100).
   - Rank all sources by quality_score. Flag any source with quality_score < 40 as low-quality.
   - If a source has high leads but booked_rate < 0.10, suggest ad copy / landing page review for that source.
   - If cold_rate > 0.60 for a source, recommend reviewing that source for misleading intent signals.

2. SCHEDULE WASTE — CAMPAIGN-TYPE-AWARE ANALYSIS (lqi.schedule_summary):
   Data: lqi.schedule_summary.hotspots = [{dow, dow_name, hour, calls, short_calls, short_pct, in_office_hours, flag}]
         lqi.schedule_summary.by_dow   = [{dow, dow_name, calls, short_calls, in_office_hours}]
         lqi.schedule_summary.by_hour  = [{hour, calls, short_calls, missed, in_office_hours}]

   CORE PRINCIPLE: Budget is limited. Every dollar spent when users cannot or will not convert is waste.
   Google's default recommendation is always to run 24/7 — but this serves Google's revenue, not yours.
   Your job is to spend smarter, not more.

   CAMPAIGN-TYPE SCHEDULING RULES (apply per-campaign, not account-wide):

   A. EMERGENCY / URGENT campaigns (keywords: emergency, pain, toothache, urgent, broken tooth, same day):
      - Patients in pain need IMMEDIATE resolution. If they call after hours and get no answer, they call
        the next result — the impression and click spend is 100% wasted.
      - STRONG recommendation to pause overnight hours (typically 9pm–7am) for emergency campaigns.
      - Weekend performance must be evaluated against whether the office is open on weekends.
        If office is closed Sat/Sun, suppress those days unless there is a same-day emergency protocol.
      - High short_pct after-hours = strong signal (patients hung up, no answer).

   B. ELECTIVE / CONSIDERED-PURCHASE campaigns (implants, veneers, dentures, Invisalign, smile makeover):
      - Patients research over days/weeks. An after-hours impression may lead to a next-morning call.
      - Evening hours (7pm–9pm) can be valuable for these — people research at home.
      - However, very late night (11pm–5am) is still waste for dental services.
      - Recommend keeping evening hours but potentially reducing bids (change_bid_strategy) rather than full pause.

   C. GENERAL / HYGIENE / CHECKUP campaigns:
      - Standard office hours + modest evening window usually optimal.
      - Weekend suppression is appropriate if office closed, but verify first.

   ANALYSIS STEPS:
   1. Look at hotspots with in_office_hours=false AND short_pct >= 0.40 → these are confirmed waste windows.
   2. For each waste window, identify which campaign type it belongs to using its name.
   3. For emergency-type campaigns: emit a change_bid_strategy (or ideally note for operator) to add an
      ad schedule excluding the waste hours.
   4. For elective campaigns: only flag extreme hours (midnight–5am). Evening is usually fine.
   5. If by_dow shows a specific day-of-week has short_pct > 0.50 AND calls > 3, recommend schedule exclusion for that DOW.
   6. Always cite the specific data: "DOW X, hour Y: Z calls, A short (B%) — outside office hours".
   7. Do NOT blindly recommend running 24/7. Restraint is the right strategy for a small practice.

3. BAD SEARCH TERMS — CROSS-CAMPAIGN (lqi.bad_search_terms):
   - lqi.bad_search_terms.by_campaign maps campaign_name to a list of {search_term, classification, cost, clicks}.
   - Find terms classified as "spanish", "competitor", or "wrong_intent" that appear in 2+ campaigns.
   - For each cross-campaign bad term, return an add_negative_keyword operation targeting the highest-spend campaign's resource (use campaign_resources in the data).
   - Do NOT re-suggest negatives already in existing_negative_keywords.

4. SHORT CALL / MISSED CALL ADVISORY (lqi.calls):
   - lqi.calls.by_campaign maps campaign_name to {short_calls, total_calls, short_pct, missed_calls}.
   - If any campaign's short_pct > 0.30, generate a claude_advisory about call handling quality for that campaign.
   - Call out the top 2 campaigns by short_pct and suggest likely cause (after-hours, IVR, ad copy mismatch).
   - If any campaign has missed_calls > 3 in 30d, flag it explicitly.

5. COLD LEAD PIPELINE (lqi.cold_leads):
   - lqi.cold_leads.time_to_first_contact_min has cold_median and converted_median (both in minutes, not hours).
   - If cold_median > 120 (2 hours), recommend a follow-up speed improvement advisory.
   - lqi.cold_leads.by_utm_campaign lists {utm_campaign, leads, cold, cold_rate}.
   - If a specific utm_campaign has cold_rate > 0.60, flag it and recommend landing page or offer review.
   - lqi.cold_leads.no_staff_contact_pct: if > 0.40, flag as critical — cold leads never contacted.

6. NO-SHOW PATTERNS (lqi.no_shows):
   - lqi.no_shows.by_campaign lists {campaign, booked, no_shows, no_show_rate}.
   - If any campaign has no_show_rate > 0.30, generate a claude_advisory recommending reminder sequence improvements.
   - lqi.no_shows.lead_age_at_booking_days has no_show_median and showed_median (days between lead creation and booking).
   - If no_show_median > 14 days, flag the long booking lag as a likely no-show driver.
   - lqi.no_shows.reminders.no_show_no_reminders_pct: if > 0.50, flag as critical — no-shows not receiving reminders.

ACCOUNT-LEVEL LIFECYCLE AWARENESS (MANDATORY):
Each campaign in "campaign_performance" has three lifecycle fields:
  - lifecycle_stage: "new" (≤30d) | "ramping" (31–90d) | "mature" (>90d) | "unknown"
  - days_since_launch: integer days since first launch (null if unknown)
  - in_learning_period: bool — true when stage is new/unknown OR ramping + thin data

RULES FOR ACCOUNT-LEVEL RECOMMENDATIONS:
1. NEVER recommend change_bid_strategy, change_budget (increase), or pause_ad_group for a campaign
   where in_learning_period = true. These reset the learning phase. Downgrade to claude_advisory.
2. For "new" campaigns (stage = "new" or days_since_launch < 30):
   - Cross-campaign negative keyword additions ARE allowed (negatives don't reset learning).
   - Do NOT cite poor CPA or low conversion rate as justification for structural changes —
     the campaign has not had enough time to collect valid data.
   - DO flag if a new campaign shows alarming waste signals (high spend, 0 clicks) as advisory.
3. For "ramping" campaigns (31–90d, in_learning_period = false):
   - Tactical changes (bid adjustments, negatives) are allowed.
   - Strategy switches (change_bid_strategy) require conversions_30d >= 15 — check campaign_performance.
4. For "mature" campaigns (>90d): all cross-campaign account-level operations are allowed.
5. When blocking an operation due to lifecycle, emit claude_advisory with reason starting:
   "LIFECYCLE_BLOCKED (account-level): [operation] on [campaign_name] — [reason]"

ACCOUNT-WIDE COMPETITOR INTELLIGENCE (from campaign creation wizard data):
The "competitor_intel_union" field contains the union of all conquest keywords and differentiators across all managed campaigns.
- all_conquest_keywords: these are INTENTIONAL competitor brand targets — NEVER recommend them as cross-campaign add_negative_keyword.
- all_differentiators: the practice's committed positioning themes — use these when writing account-wide advisory framing.
- by_campaign: per-campaign breakdown for reference.
If you see a search term that appears in all_conquest_keywords, treat it as intentional targeting, not waste.

DYNAMIC BUDGET REALLOCATION (GI-3):
The "budget_reallocation_signals" field contains pre-computed donor/receiver candidates.
- "reallocation_allowed": false when Budget Constrained mode is on — do NOT recommend any budget changes.
- "total_daily_budget_usd": the current total daily spend across all campaigns — MUST be preserved exactly.
- "receivers": campaigns that are losing >=""" + f"{_REALLOC_RECEIVER_BUDGET_LOST_MIN:.0%}" + """% of impression share to budget and are generating value (production > 0 or strong ROAS).
- "donors": campaigns with headroom above the $""" + f"{_REALLOC_MIN_FLOOR_USD:.0f}" + """/day floor that are not themselves budget-constrained.
- Each donor has "headroom_usd" — the maximum you may transfer from that campaign in a single run.

REALLOCATION RULES (MANDATORY):
1. ONLY recommend reallocation if reallocation_allowed = true AND receivers list is non-empty AND donors list is non-empty.
2. Each reallocation requires EXACTLY TWO paired change_budget operations:
   - One DECREASE on the donor: new_daily_budget_usd = donor.daily_budget_usd - transfer_amount
   - One INCREASE on the receiver: new_daily_budget_usd = receiver.daily_budget_usd + transfer_amount
   - The transfer_amount must equal the same value in both operations (net-zero constraint).
3. transfer_amount must not exceed donor.headroom_usd AND must not exceed $""" + f"{_REALLOC_MAX_SINGLE_TRANSFER_USD:.0f}" + """/day per transfer.
4. Never reduce a donor below $""" + f"{_REALLOC_MIN_FLOOR_USD:.0f}" + """/day (headroom already enforces this — trust the pre-computed value).
5. Never recommend reallocation for campaigns where in_learning_period = true.
6. Prefer receivers with the highest search_budget_lost_is and highest roas_30d (most value being left on the table).
7. Prefer donors with the lowest search_budget_lost_is (most clearly not budget-constrained).
8. In the "reason" field for BOTH change_budget operations, cite:
   - donor campaign name, receiver campaign name, transfer amount
   - receiver's search_budget_lost_is and roas_30d
   - The note: "NET-ZERO: account total stays at $X/day"
9. Emit at most ONE reallocation pair per optimizer run (one donor decrease + one receiver increase).
10. If no clean pair exists (e.g., receivers have no production yet, or all donors are at floor), emit claude_advisory explaining what data is needed before reallocation is warranted.

A/B EXPERIMENT MONITORING:
The "active_ab_experiments" field lists currently running Google Ads A/B experiments.
Each entry has: name, type (landing_page|ad_copy), days_running, base_campaign, trial_campaign,
control_url, variant_url, and winner_signal (ready, winner, confidence, summary, base_stats, trial_stats).

RULES:
1. If winner_signal.ready = true AND winner_signal.winner != 'inconclusive':
   - Emit an experiment_advisory (use operation="experiment_advisory") with:
     - insight: which arm won, the primary metric, relative lift, and the summary
     - reason: "Recommend promoting [winner] arm. Review metrics and promote manually in Google Ads UI."
     - Include base_stats and trial_stats in the insight for reference.
2. If winner_signal.ready = false:
   - Emit experiment_advisory only if days_running > 30 AND still insufficient_data,
     to flag that the experiment may be underpowered.
3. Do NOT emit change_budget, change_bid_strategy, increase_bid, or pause_keyword for
   campaigns that are part of an active experiment (base_campaign or trial_campaign names).
   These campaigns are mid-test — structural changes invalidate the comparison.
4. experiment_advisory uses operation="experiment_advisory", not "claude_advisory".
   This allows the frontend to display it in the A/B Tests tab rather than the main rec queue.

N-GRAM WASTE ANALYSIS (GI-3):
The "ngram_waste_signals" field contains statistical patterns across ALL non-converting search terms.
- "unigrams": single tokens that appear across many non-converting search terms with significant total waste.
- "bigrams": two-word phrases that appear across many non-converting search terms with significant total waste.
- Each entry has: token, total_waste (USD), distinct_terms (# unique search terms containing it), example_terms.
USE THESE TO IDENTIFY BROAD-MATCH NEGATIVE KEYWORDS:
- A unigram or bigram with distinct_terms ≥ 3 AND total_waste ≥ $15 is a strong candidate for a cross-campaign broad negative.
- Before recommending, check "all_conquest_keywords" — never negate a conquest keyword.
- Also check "existing_negative_keywords" — skip if already negated.
- Prefer bigrams over unigrams when the bigram captures the intent more precisely.
- Highly service-relevant tokens (implant, invisalign, crown, emergency, etc.) are pre-filtered — do not appear in ngram signals.
- Generate add_negative_keyword with match_type "broad" for the strongest candidates (top 3–5 by waste).
- The "reason" field should cite: total_waste, distinct_terms, and a sample of example_terms.""" + _build_institutional_memory_note("") + _build_mcp_decisions_note(None)

        msg = client.messages.create(
            model="claude-sonnet-4-5",
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
                model="claude-sonnet-4-5",
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
            # Operations valid at account level — anything else is a campaign-level op that leaked
            _ACCOUNT_LEVEL_OPS = {"add_negative_keyword", "change_bid_strategy", "change_budget", "add_asset", "claude_advisory", "experiment_advisory"}
            # PR 5: build conquest keyword protection set for account-level
            _acct_conquest: set = {
                k.strip().lower()
                for k in ((competitor_intel_union or {}).get("all_conquest_keywords") or [])
                if k.strip()
            }
            validated = []
            for item in arr:
                if not isinstance(item, dict) or not item.get("operation"):
                    continue
                # Stash original campaign_name before clobbering — needed by
                # budget_constraint_sieve which looks up budget_by_campaign[campaign_name].
                # (C1 fix: preserve for sieve, then zero out for storage.)
                item["_original_campaign_name"] = item.get("campaign_name", "")
                # Force campaign_name to empty — these are account-level
                item["campaign_name"] = ""
                op = item["operation"]
                # PR 5: drop conquest keyword negatives at account level too
                if op == "add_negative_keyword" and _acct_conquest:
                    import re as _re_acct_ck
                    kw_text = item.get("keyword_text", "").strip().lower()
                    _acct_conquest_hit = False
                    for ck in _acct_conquest:
                        if len(ck) < 4:
                            continue
                        if _re_acct_ck.search(r'\b' + _re_acct_ck.escape(ck) + r'\b', kw_text):
                            _acct_conquest_hit = True
                            break
                        if len(kw_text) >= 4 and _re_acct_ck.search(r'\b' + _re_acct_ck.escape(kw_text) + r'\b', ck):
                            _acct_conquest_hit = True
                            break
                    if _acct_conquest_hit:
                        logger.warning(
                            f"Account-level: dropping add_negative_keyword '{item.get('keyword_text','')}' — "
                            f"matches conquest keyword"
                        )
                        continue
                # Drop any operation that is not valid at account level
                if op not in _ACCOUNT_LEVEL_OPS:
                    logger.warning(f"Account-level: dropping '{op}' — campaign-level operation leaked into account pass (should come from per-campaign Claude)")
                    continue
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

                # Recently-applied suppression (account-level)
                # change_budget is exempt: budget situations change week-to-week;
                # suppressing paired reallocation ops would leave an inconsistent state.
                _exempt_acct = {"claude_advisory", "add_negative_keyword",
                                 "add_exact_keyword", "enable_keyword", "change_budget"}
                if op not in _exempt_acct:
                    _entity_acct = (
                        item.get("keyword_text") or item.get("entity_name") or
                        item.get("ad_group_name") or item.get("insight", "")[:60]
                    )
                    _cr_acct = item.get("campaign_resource", "")
                    if _was_recently_applied(op, _entity_acct, campaign_resource=_cr_acct, days=14):
                        logger.info(
                            f"Account-level: suppressing '{op}' for '{_entity_acct}' — applied within 14 days"
                        )
                        continue

                validated.append(item)
            logger.info(f"Account-level Claude returned {len(arr)} recs, {len(validated)} passed validation")
            # Budget constraint sieve — block change_budget increases at account level too.
            # C1 fix: temporarily restore _original_campaign_name so the sieve can look up
            # budget_by_campaign[campaign_name] correctly, then strip the helper field.
            if budget_constrained:
                for _v in validated:
                    if _v.get("_original_campaign_name"):
                        _v["campaign_name"] = _v["_original_campaign_name"]
                pre_len = len(validated)
                validated = _budget_constraint_sieve(
                    validated,
                    camp_settings={},          # unused in account-level mode
                    budget_by_campaign=budget_by_campaign,  # name→daily_budget_usd map
                )
                if len(validated) != pre_len:
                    logger.info(
                        f"[budget_constraint_sieve][account] {pre_len - len(validated)} "
                        f"change_budget increases/strategy changes converted to advisories"
                    )
                # Re-zero campaign_name after sieve (account-level recs must be campaign-agnostic)
                for _v in validated:
                    _v["campaign_name"] = ""
            # Strip the helper field from all recs regardless of budget_constrained mode
            for _v in validated:
                _v.pop("_original_campaign_name", None)

            # M4 fix: Net-zero verification for change_budget pairs.
            # Claude is instructed to emit exactly one paired decrease+increase with equal amounts.
            # Enforce this programmatically: find all change_budget ops, compute net delta.
            # If delta != 0 (mismatched pair or one-sided op), drop all change_budget ops
            # and emit a single advisory explaining what happened.
            _budget_ops = [v for v in validated if v.get("operation") == "change_budget"]
            if _budget_ops and not budget_constrained:
                _net_delta = 0.0
                _parse_ok = True
                for _bop in _budget_ops:
                    _orig_cn = _bop.get("_original_campaign_name", "")
                    _curr_bud = (_acct_budget_by_campaign or {}).get(_orig_cn, 0.0) if _orig_cn else 0.0
                    _new_bud = float(_bop.get("new_daily_budget_usd") or 0)
                    if _curr_bud <= 0:
                        _parse_ok = False
                        break
                    _net_delta += _new_bud - _curr_bud
                if _parse_ok and abs(_net_delta) > 0.50:  # allow $0.50 rounding tolerance
                    logger.warning(
                        f"[net-zero] Account-level budget reallocation rejected: "
                        f"net delta=${_net_delta:+.2f} (expected 0). "
                        f"Ops: {[(v.get('_original_campaign_name','?'), v.get('new_daily_budget_usd')) for v in _budget_ops]}"
                    )
                    validated = [v for v in validated if v.get("operation") != "change_budget"]
                    validated.append({
                        "operation": "claude_advisory",
                        "campaign_name": "",
                        "insight": (
                            f"BUDGET_REALLOCATION_REJECTED: Claude proposed {len(_budget_ops)} change_budget op(s) "
                            f"with a net delta of ${_net_delta:+.2f}/day — violates net-zero constraint. "
                            f"No budget changes were made. Review reallocation signals and retry."
                        ),
                        "reason": f"net_delta=${_net_delta:+.2f} exceeds $0.50 tolerance",
                    })
                elif _budget_ops:
                    logger.info(
                        f"[net-zero] Account-level budget reallocation approved: "
                        f"{len(_budget_ops)} change_budget op(s), net delta=${_net_delta:+.2f}"
                    )

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
            model="claude-sonnet-4-5",
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
                      outcome_history: dict | None = None,
                      lifecycle: dict | None = None,
                      lifecycle_by_campaign: dict | None = None) -> dict:
    """
    Apply optimization rules. Returns recommended actions.
    campaign: name of the campaign being evaluated — used to scope memory lookups.
              Empty string = global memory only.
    outcome_history: pre-loaded from _load_outcome_history(); if None, loaded here.
    lifecycle: dict from build_lifecycle_block() for the primary campaign — if
               in_learning_period, bid and pause rules are suppressed for that campaign.
    lifecycle_by_campaign: dict[campaign_name → lifecycle_block] for per-keyword campaign
               lookup; overrides the primary lifecycle when present.
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

    # Lifecycle guard — suppress bid and pause rules during learning period
    _in_learning = (lifecycle or {}).get("in_learning_period", False)
    _lc_stage    = (lifecycle or {}).get("stage", "unknown")
    _lc_by_camp  = lifecycle_by_campaign or {}
    if _in_learning:
        logger.info(
            f"[rules] '{campaign}' in learning period (stage={_lc_stage}) — "
            f"Rule 1 (pause) and Rule 2 (bid) suppressed; negatives still active"
        )

    def _kw_in_learning(kw: dict) -> bool:
        """Check if a keyword's campaign is in the learning period."""
        camp = kw.get("campaign", "")
        if camp in _lc_by_camp:
            return _lc_by_camp[camp].get("in_learning_period", False)
        # Campaign not found in lifecycle map — default to False (fail-open).
        # This covers cross-campaign keywords where we don't have a separate lifecycle block.
        if camp and camp != campaign:
            logger.warning(
                f"[rules] _kw_in_learning: no lifecycle entry for campaign '{camp}' "
                f"— defaulting to in_learning=False (fail-open)"
            )
        return False

    # Rule 1: Pause keywords with spend > threshold and zero leads/calls
    # Suppressed per-keyword during learning period — pausing during learning resets Google's algorithm
    for kw in keyword_perf:
        if _kw_in_learning(kw):
            continue  # skip pause check for keywords in learning campaigns
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
    # Suppressed per-keyword during learning period — bid changes reset Google's learning algorithm
    for kw in keyword_perf:
        if _kw_in_learning(kw):
            continue  # skip bid increase for keywords in learning campaigns
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
            # Skip aligner-only terms at account level — only valid in a clear aligner campaign
            if _is_aligner_only(term):
                logger.info(f"  SKIP harvest '{term}' — aligner-only term, not appropriate as account-level keyword")
                continue

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
    Supports: MANUAL_CPC, MAXIMIZE_CONVERSIONS, TARGET_CPA, TARGET_ROAS, MAXIMIZE_CLICKS
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
    elif strategy == "MANUAL_CPC":
        # Switch to Manual CPC by setting manual_cpc sub-message.
        # enhanced_cpc_enabled = False ensures true manual bidding without eCPC override.
        campaign.manual_cpc.enhanced_cpc_enabled = False
        paths = ["manual_cpc.enhanced_cpc_enabled"]
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
        if strategy == "MANUAL_CPC":
            logger.warning(
                "[change_bid_strategy→MANUAL_CPC] Strategy switched. IMPORTANT: keyword bids may have "
                "defaulted to ad-group level CPC. Verify keyword-level bids are set competitively "
                "in Google Ads before the next auction cycle to avoid zero traffic."
            )
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



def _execute_create_skag(
    client,
    customer_id: str,
    campaign_resource: str,
    source_ad_group_name: str,
    keyword_text: str,
    new_ad_group_name: str,
    recommendation_id: str,
    action_id: str,
    skag_created_this_run: list,
) -> dict:
    """
    Execute a SKAG creation: create the ad group, add the EXACT keyword,
    copy the source ad group's RSA verbatim, and update skag_recommendations.

    Safety guards:
    1. Max 2 SKAGs per optimizer run (checked via skag_created_this_run list).
    2. Idempotency: if recommendation_id already has status=created, skip.
    3. Requires non-empty campaign_resource.

    Args:
        client:               GoogleAdsClient
        customer_id:          digits-only customer ID string
        campaign_resource:    customers/NNN/campaigns/MMM
        source_ad_group_name: human-readable source ad group name (for resource lookup)
        keyword_text:         the single keyword to isolate
        new_ad_group_name:    name for the new SKAG ad group
        recommendation_id:    FK to skag_recommendations.recommendation_id
        action_id:            optimizer queue action_id (for audit log)
        skag_created_this_run: mutable list accumulating created rec_ids this run

    Returns:
        {"ad_group_resource": str, "keyword_resource": str,
         "ad_resource": str, "rsa_copied": bool, "blocked": bool, "reason": str}
    """
    from datetime import datetime, timezone
    from database import _conn as _get_db_conn

    # ── Guard 1: per-run cap ─────────────────────────────────────────────────
    MAX_SKAGS_PER_RUN = 2
    if len(skag_created_this_run) >= MAX_SKAGS_PER_RUN:
        reason = (
            f"SKAG per-run cap reached ({MAX_SKAGS_PER_RUN}) — "
            f"'{keyword_text}' deferred to next run"
        )
        logger.info("_execute_create_skag: %s", reason)
        return {"blocked": True, "reason": reason, "ad_group_resource": "",
                "keyword_resource": "", "ad_resource": "", "rsa_copied": False}

    # ── Guard 2: idempotency + atomic in-flight claim ───────────────────────
    # Use a single connection for both the read and the optimistic status flip
    # so that concurrent approvals can't both pass the "status != created" check.
    # We atomically claim the row with UPDATE...WHERE status NOT IN ('created','approved')
    # and then verify rowcount to detect a race.
    with _get_db_conn() as conn:
        rec = conn.execute(
            "SELECT status, new_ad_group_id FROM skag_recommendations "
            "WHERE recommendation_id = ?",
            (recommendation_id,)
        ).fetchone()

    if rec is None:
        reason = (
            f"recommendation_id={recommendation_id!r} not found in skag_recommendations. "
            "Only optimizer-surfaced candidates can be SKAG-created."
        )
        logger.warning("_execute_create_skag: %s", reason)
        return {"blocked": True, "reason": reason, "ad_group_resource": "",
                "keyword_resource": "", "ad_resource": "", "rsa_copied": False}

    if rec["status"] == "created":
        reason = f"SKAG already created (recommendation_id={recommendation_id})"
        logger.info("_execute_create_skag: %s", reason)
        return {"blocked": True, "reason": reason,
                "ad_group_resource": rec["new_ad_group_id"] or "",
                "keyword_resource": "", "ad_resource": "", "rsa_copied": False}

    if rec["status"] == "approved":
        reason = (
            f"SKAG is already in-flight (status=approved) — "
            f"recommendation_id={recommendation_id}"
        )
        logger.info("_execute_create_skag: %s", reason)
        return {"blocked": True, "reason": reason, "ad_group_resource": "",
                "keyword_resource": "", "ad_resource": "", "rsa_copied": False}

    # ── Guard 3: campaign_resource required ──────────────────────────────────
    if not campaign_resource or not campaign_resource.startswith("customers/"):
        reason = f"Missing or invalid campaign_resource: '{campaign_resource}'"
        logger.warning("_execute_create_skag: %s", reason)
        _mark_skag_failed(recommendation_id, reason)
        return {"blocked": True, "reason": reason, "ad_group_resource": "",
                "keyword_resource": "", "ad_resource": "", "rsa_copied": False}

    # ── Atomic in-flight claim: flip to 'approved' ONLY if still 'pending'/'locked' ─
    # rowcount==0 means another process claimed it between our read and now.
    with _get_db_conn() as conn:
        cur = conn.execute(
            "UPDATE skag_recommendations SET status = 'approved' "
            "WHERE recommendation_id = ? AND status NOT IN ('created', 'approved', 'locked', 'rejected', 'reverted')",
            (recommendation_id,)
        )
        claimed = cur.rowcount > 0

    if not claimed:
        reason = (
            f"SKAG concurrent claim rejected — recommendation_id={recommendation_id} "
            "was claimed by another process or has a terminal status"
        )
        logger.warning("_execute_create_skag: %s", reason)
        return {"blocked": True, "reason": reason, "ad_group_resource": "",
                "keyword_resource": "", "ad_resource": "", "rsa_copied": False}

    # ── Resolve source ad group resource name ────────────────────────────────
    source_ag_resource = ""
    try:
        ga_service = client.get_service("GoogleAdsService")
        _ag_escaped = source_ad_group_name.replace("'", "''")
        ag_query = f"""
            SELECT ad_group.resource_name
            FROM ad_group
            WHERE ad_group.campaign = '{campaign_resource}'
              AND ad_group.name = '{_ag_escaped}'
              AND ad_group.status != 'REMOVED'
            LIMIT 1
        """
        for row in ga_service.search(customer_id=customer_id, query=ag_query):
            source_ag_resource = row.ad_group.resource_name
            break
    except Exception as e:
        logger.warning(
            "_execute_create_skag: could not resolve source ag resource for '%s': %s",
            source_ad_group_name, e
        )

    # ── Execute: call create_skag_ad_group ───────────────────────────────────
    try:
        from google_ads_write import create_skag_ad_group
        result = create_skag_ad_group(
            customer_id=customer_id,
            campaign_resource=campaign_resource,
            new_ad_group_name=new_ad_group_name,
            keyword_text=keyword_text,
            source_ad_group_resource=source_ag_resource,
        )
    except Exception as e:
        err_str = str(e)[:500]
        logger.error(
            "_execute_create_skag failed for '%s': %s", keyword_text, err_str
        )
        _mark_skag_failed(recommendation_id, err_str)
        return {"blocked": False, "error": err_str, "ad_group_resource": "",
                "keyword_resource": "", "ad_resource": "", "rsa_copied": False}

    # ── Update DB: mark created ───────────────────────────────────────────────
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _get_db_conn() as conn:
        conn.execute("""
            UPDATE skag_recommendations
            SET status = 'created',
                new_ad_group_id = ?,
                created_in_gads_at = ?,
                steps_completed = json_insert(
                    COALESCE(steps_completed, '[]'),
                    '$[#]', json_array('create_ad_group', ?)
                )
            WHERE recommendation_id = ?
        """, (
            result["ad_group_resource"],
            now_iso,
            result["ad_group_resource"],
            recommendation_id,
        ))

    skag_created_this_run.append(recommendation_id)
    logger.info(
        "_execute_create_skag: '%s' [EXACT] → %s (rsa_copied=%s)",
        keyword_text, result["ad_group_resource"], result["rsa_copied"]
    )
    return {**result, "blocked": False, "reason": "", "error": ""}


def _update_skag_status(recommendation_id: str, status: str) -> None:
    """Update skag_recommendations.status; no-op if recommendation_id not found."""
    from database import _conn as _get_db_conn
    with _get_db_conn() as conn:
        conn.execute(
            "UPDATE skag_recommendations SET status = ? WHERE recommendation_id = ?",
            (status, recommendation_id)
        )


def _mark_skag_failed(recommendation_id: str, error: str) -> None:
    """Mark a SKAG recommendation as failed with an error message."""
    from database import _conn as _get_db_conn
    with _get_db_conn() as conn:
        conn.execute(
            "UPDATE skag_recommendations SET status = 'failed', error = ? "
            "WHERE recommendation_id = ?",
            (error[:500], recommendation_id)
        )


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
        logger.warning("path1 '%s' exceeds 15 chars — truncating to '%s'", path1, path1[:15])
        path1 = path1[:15]
    if path2 and len(path2) > 15:
        logger.warning("path2 '%s' exceeds 15 chars — truncating to '%s'", path2, path2[:15])
        path2 = path2[:15]
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

    # --- 6. Two-step mutate: PAUSE first, then CREATE -------------------------
    # Google evaluates the RSA-per-ad-group limit (max 3 enabled) before
    # applying any operation in a batch. Sending pause+create together fails
    # with RESOURCE_LIMIT when the ad group already has 3 enabled RSAs, even
    # though the pause would have freed a slot. Split into sequential calls so
    # the count is decremented before the new RSA is created.
    service = client.get_service("AdGroupAdService")

    # Step 6a: Pause the old ad
    try:
        pause_response = service.mutate_ad_group_ads(
            customer_id=customer_id,
            operations=[pause_op],
        )
    except Exception as e:
        logger.error(f"replace_ad pause step failed for {old_ad_group_ad_resource}: {e}")
        raise

    paused_rn = pause_response.results[0].resource_name
    logger.info(f"replace_ad: paused {paused_rn}")

    # Step 6b: Create the new ad (slot now free)
    try:
        create_response = service.mutate_ad_group_ads(
            customer_id=customer_id,
            operations=[create_op],
        )
    except Exception as e:
        # Pause already executed — log prominently so the ad group isn't left
        # with only the paused ad and no replacement.
        logger.error(
            f"replace_ad CREATE step failed after pause — ad group {ad_group_resource} "
            f"may now have fewer enabled RSAs than expected. "
            f"Paused ad: {paused_rn}. Error: {e}"
        )
        raise

    created_rn = create_response.results[0].resource_name
    logger.info(
        f"replace_ad: created {created_rn} "
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

        # ── Geo targeting update (advisory in PR3 — execution added in PR4) ─────────────────
        elif operation == "update_geo_targeting":
            radius = context.get("proposed_radius_miles")
            add_zips = context.get("add_zip_codes") or []
            remove_zips = context.get("remove_zip_codes") or []
            parts = []
            if radius:
                parts.append(f"radius → {radius} mi")
            if add_zips:
                parts.append(f"add ZIPs: {', '.join(str(z) for z in add_zips[:5])}")
            if remove_zips:
                parts.append(f"remove ZIPs: {', '.join(str(z) for z in remove_zips[:5])}")
            # Note: by the time _verify_gads_change is called, the change has already been
            # executed via replace_campaign_locations in main.py. The summary reflects the
            # applied change. A full read-back verify (querying campaign_criterion) is a
            # future improvement; for now flag as unconfirmed so the UI shows manual review.
            summary = "📍 Geo update applied — " + "; ".join(parts) if parts else "📍 Geo targeting updated — verify in Google Ads"
            return {"confirmed": False, "summary": summary, "detail": {"check_google_ads": True}}

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


# ── Campaign asset fetch (callouts, structured snippets, sitelinks) ───────────

def _fetch_campaign_assets(client, customer_id: str, campaign_resource: str) -> dict:
    """
    Fetch all active callout, structured snippet, and sitelink assets linked
    to a campaign.  Returns a dict suitable for injection into Claude's context:

    {
      "callouts": ["Free Consultations", "Saturday Hours", ...],
      "structured_snippets": [
          {"header": "Service catalog", "values": ["Implants", "Veneers", ...]},
          ...
      ],
      "sitelinks": ["About Us", "Book Online", ...]
    }

    Non-fatal — returns empty structure on any error.
    """
    result = {"callouts": [], "structured_snippets": [], "sitelinks": []}
    try:
        service = client.get_service("GoogleAdsService")
        query = f"""
            SELECT
                campaign_asset.field_type,
                campaign_asset.status,
                asset.type,
                asset.callout_asset.callout_text,
                asset.structured_snippet_asset.header,
                asset.structured_snippet_asset.values,
                asset.sitelink_asset.link_text
            FROM campaign_asset
            WHERE campaign.resource_name = '{campaign_resource}'
              AND campaign.status != 'REMOVED'
              AND campaign_asset.status != 'REMOVED'
              AND campaign_asset.field_type IN ('CALLOUT', 'STRUCTURED_SNIPPET', 'SITELINK')
        """
        rows = list(service.search(customer_id=customer_id, query=query))
        for row in rows:
            ft = row.campaign_asset.field_type.name  # e.g. "CALLOUT"
            a = row.asset
            if ft == "CALLOUT":
                text = a.callout_asset.callout_text
                if text:
                    result["callouts"].append(text)
            elif ft == "STRUCTURED_SNIPPET":
                header = a.structured_snippet_asset.header
                values = list(a.structured_snippet_asset.values)
                if header:
                    result["structured_snippets"].append({"header": header, "values": values})
            elif ft == "SITELINK":
                link_text = a.sitelink_asset.link_text
                if link_text:
                    result["sitelinks"].append(link_text)
    except Exception as e:
        logger.warning(f"_fetch_campaign_assets failed for {campaign_resource} (non-fatal): {e}")
    return result


# ── Google Ads live negative keyword fetch ────────────────────────────────────

def _fetch_existing_negatives(client, customer_id: str) -> tuple:
    """
    Pull all negative keywords currently live in Google Ads — both campaign-level
    and from shared negative keyword lists (e.g. 'GDC Competitor Negatives').
    Returns (flat_set, negatives_by_campaign) where:
      flat_set              — set of lowercased keyword texts (account-wide union)
      negatives_by_campaign — dict[campaign_name → set of lowercased keyword texts]
    Also saves them to gads_negative_keywords table for offline reference.
    """
    ga_service = client.get_service("GoogleAdsService")
    existing: set = set()
    negatives_by_campaign: dict = {}   # campaign_name → set of lowercase keyword texts
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
            camp_name = row.campaign.name
            if text:
                existing.add(text)
                negatives_by_campaign.setdefault(camp_name, set()).add(text)
                rows_to_save.append((text,
                                     row.campaign_criterion.keyword.match_type.name,
                                     camp_name,
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
                f"(campaign-level + shared lists); {len(negatives_by_campaign)} campaigns have negatives")
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

    return existing, negatives_by_campaign


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


def _was_recently_applied(operation: str, entity_name: str,
                           campaign_resource: str = "",
                           days: int = 14) -> bool:
    """
    Return True if the same (operation, entity_name) was successfully applied
    within the last `days` days for the same campaign.

    Prevents Claude from re-recommending actions that were just approved and
    pushed — e.g. a bid increase that was applied yesterday coming back today
    because the performance data hasn't fully reflected the change yet.

    entity_name is lowercased for comparison.
    campaign_resource is optional — if provided, scopes the check to that campaign.
    """
    entity_lower = (entity_name or "").strip().lower()
    if not entity_lower:
        return False
    try:
        from database import _conn
        with _conn() as conn:
            if campaign_resource:
                row = conn.execute(
                    """
                    SELECT 1 FROM gads_audit_log
                     WHERE operation = ?
                       AND LOWER(entity_name) = ?
                       AND campaign_resource = ?
                       AND execution_result = 'success'
                       AND created_at >= datetime('now', ?)
                     LIMIT 1
                    """,
                    (operation, entity_lower, campaign_resource, f"-{days} days"),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT 1 FROM gads_audit_log
                     WHERE operation = ?
                       AND LOWER(entity_name) = ?
                       AND execution_result = 'success'
                       AND created_at >= datetime('now', ?)
                     LIMIT 1
                    """,
                    (operation, entity_lower, f"-{days} days"),
                ).fetchone()
            return row is not None
    except Exception as e:
        logger.debug(f"Recently-applied check failed for op={operation} entity='{entity_name}': {e}")
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

    # Read budget constraint setting
    _budget_constrained = (get_setting("budget_constrained") or "false") == "true"
    if _budget_constrained:
        logger.info("Budget Constrained mode is ON — change_budget increases will be suppressed")

    # Expire recommendations older than 48 hours before generating new ones.
    # This keeps the queue clean — each optimizer run produces a fresh set.
    # Users have 48h to review and apply recommendations before they're swept.
    _optimizer_progress["started_at"] = _time_mod.time()
    _set_progress(0)  # Starting
    expired = expire_stale_pending(max_age_hours=48)

    try:
        _set_progress(1)  # Syncing GAds Data
        client = _build_client()
    except Exception as e:
        logger.error(f"Failed to create Google Ads client: {e}")
        create_optimizer_run(run_id, trigger=trigger)
        update_optimizer_run(run_id, mode="errored", error=str(e))
        _set_progress_done()
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
    live_negatives, live_negatives_by_campaign = _fetch_existing_negatives(client, customer_id)

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

    # When budget constrained: filter Google Recs that recommend budget/bid increases
    if _budget_constrained and google_recs:
        _BUDGET_INCREASE_REC_TYPES = {
            "CAMPAIGN_BUDGET",
            "MARGINAL_ROI_CAMPAIGN_BUDGET",
            # MOVE_UNUSED_BUDGET is neutral (redistributes, not increases total) — excluded
            "RAISE_TARGET_CPA",             # was RAISE_TARGET_CPA_BID_TOO_LOW — not a valid type
            "TARGET_CPA_OPT_IN",            # opt-in often requires budget bump
            "MAXIMIZE_CONVERSIONS_OPT_IN",  # recommends a higher budget
            "MAXIMIZE_CLICKS_OPT_IN",       # uncapped strategy increases spend
            "USE_BROAD_MATCH_KEYWORD",      # broad match significantly increases reach/spend
            "FORECASTING_CAMPAIGN_BUDGET",
            "FORECASTING_SET_TARGET_ROAS",
        }
        _before = len(google_recs)
        google_recs = [
            gr for gr in google_recs
            if gr.get("rec_type", "") not in _BUDGET_INCREASE_REC_TYPES
        ]
        _filtered_count = _before - len(google_recs)
        if _filtered_count:
            logger.info(
                f"[budget_constraint] Filtered {_filtered_count} Google Recs that increase spend "
                f"({_before} → {len(google_recs)} remaining)"
            )

    _set_progress(2)  # Fetching Negatives
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
        # Build campaign name → daily_budget_usd for _score_tier learning-phase scaling
        camp_name_to_budget: dict = {
            (s.get("campaign_name") or "").strip().lower(): float(s.get("daily_budget_usd") or 0)
            for s in campaign_settings.values()
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

            # Performance tier — pass daily_budget_usd so threshold scales with campaign budget
            tier = _score_tier(impr, clicks, cost_usd, conv, avg_ctr,
                               daily_budget_usd=camp_name_to_budget.get(cname, 0.0))

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

    _set_progress(3)  # Ad Performance
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

        # Build campaign_id/name → daily_budget_usd for ad group tier scoring
        camp_id_to_budget: dict = {}
        for _rn, _cs in campaign_settings.items():
            _budget = float(_cs.get("daily_budget_usd") or 0)
            _cname = (_cs.get("campaign_name") or "").strip().lower()
            if _cname:
                camp_id_to_budget[_cname] = _budget
            # Also key by numeric campaign ID extracted from resource name
            if "/campaigns/" in _rn:
                try:
                    _cid = _rn.split("/campaigns/")[-1]
                    if _cid.isdigit():
                        camp_id_to_budget[_cid] = _budget
                except Exception:
                    pass

        for ag in raw_ag:
            cid = ag.get("campaign_id") or ag.get("campaign_name", "")
            impr = ag.get("impressions") or 0
            clicks = ag.get("clicks") or 0
            cost = float(ag.get("cost") or 0)
            conv = float(ag.get("conversions") or 0)
            avg_ctr = camp_id_avg_ctr.get(cid, 0)
            _ag_daily_budget = camp_id_to_budget.get(str(cid).strip().lower(), 0.0)

            # Performance tier — pass daily_budget_usd so threshold scales with campaign budget
            tier = _score_tier(impr, clicks, cost, conv, avg_ctr,
                               daily_budget_usd=_ag_daily_budget)

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

    # ── LQI signals (lead quality intelligence) ──────────────────────────────
    # Collected once per run; passed into both per-campaign and account-level
    # Claude prompts for richer context on call quality, source quality,
    # schedule waste, bad search terms, cold leads, and no-shows.
    try:
        from lqi_signals import collect_all as _lqi_collect_all
        lqi_signals = _lqi_collect_all(days=30)
        logger.info(
            f"LQI signals collected: sources={len(lqi_signals.get('sources', {}))}, "
            f"bad_terms={lqi_signals.get('search_terms', {}).get('totals', {}).get('terms_flagged', 0)}, "
            f"call_campaigns={len(lqi_signals.get('calls', {}).get('by_campaign', {}))}"
        )
    except Exception as _lqi_err:
        logger.warning(f"LQI collection failed (non-fatal): {_lqi_err}")
        lqi_signals = {}

    # ── Capture account-wide totals before any filtering ──────────────────────
    total_spend_all_campaigns = round(sum(k.get("cost", 0) for k in keyword_perf), 2)
    total_clicks_all_campaigns = sum(k.get("clicks", 0) for k in keyword_perf)

    # ── Determine which campaigns to analyze ──────────────────────────────────
    # Only generate per-campaign AI recommendations for ENABLED (active) campaigns.
    # Paused campaigns are excluded from the recommendation loop but their keyword
    # performance data and decision history still flow into the account-level Claude
    # pass for learning (keyword_perf is not filtered — it retains all campaigns).
    _enabled_camp_names: set[str] = {
        s["campaign_name"].strip()
        for s in campaign_settings.values()
        if s.get("campaign_status", "").upper() == "ENABLED" and s.get("campaign_name", "").strip()
    }
    active_campaigns_with_data = {
        k.get("campaign", "").strip()
        for k in keyword_perf
        if k.get("campaign", "").strip() and k.get("campaign", "").strip() in _enabled_camp_names
    }
    logger.info(
        f"Campaigns with keyword data (ENABLED only): {active_campaigns_with_data}  "
        f"(paused campaigns excluded from per-campaign AI recs; data still used for account-level learning)"
    )

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

    _set_progress(9)  # Rule-Based Engine
    # Analyze
    logger.info("Analyzing and generating recommendations...")
    # Build per-campaign lifecycle map for the rule engine guard
    # (rule engine runs once over all campaigns' keywords combined)
    from lifecycle import build_lifecycle_block as _blc, compute_days_since_launch as _cdsl
    from database import get_campaign_by_name as _gcbn, get_campaign_launch_date as _gcld
    _rule_lifecycle_by_camp: dict = {}
    for _rc in set(kw.get("campaign","") for kw in keyword_perf if kw.get("campaign")):
        try:
            _rc_row = _gcbn(_rc)
            _rc_ld = _gcld(_rc_row["campaign_id"]) if _rc_row else None
            _rc_clicks = sum(k.get("clicks",0) for k in keyword_perf if k.get("campaign")==_rc)
            _rc_conv   = sum(k.get("conversions",0) for k in keyword_perf if k.get("campaign")==_rc)
            _rule_lifecycle_by_camp[_rc] = _blc(
                launch_date=_rc_ld,
                conversions_30d=_rc_conv,
                clicks_30d=_rc_clicks,
            )
        except Exception as _rule_lc_err:
            logger.debug(f"[lifecycle] rule engine camp lookup failed for '{_rc}': {_rule_lc_err}")
            _rule_lifecycle_by_camp[_rc] = {}

    # Filter keyword_perf to ENABLED campaigns only for rule-based and AI per-campaign analysis.
    # Full keyword_perf (including paused campaigns) is retained for account-level learning.
    keyword_perf_active = [
        k for k in keyword_perf
        if k.get("campaign", "").strip() in _enabled_camp_names
    ]
    logger.info(
        f"Rule engine: {len(keyword_perf_active)} kw rows from ENABLED campaigns "
        f"(filtered from {len(keyword_perf)} total rows including paused)"
    )

    actions = _analyze_keywords(
        keyword_perf_active, attribution, search_terms,
        call_attribution=call_attribution,
        keyword_call_attribution=keyword_call_attribution,
        campaign=primary_campaign,
        outcome_history=outcome_history,
        lifecycle=_rule_lifecycle_by_camp.get(primary_campaign, {}),
        lifecycle_by_campaign=_rule_lifecycle_by_camp,
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

    # Build resource_name → conversions lookup from keyword_perf for bid confidence scaling.
    # keyword_perf is in scope from the enclosing optimize_campaign function.
    _kw_conv_map: dict = {}
    try:
        for _kp in keyword_perf:
            _rn = _kp.get("resource_name") or ""
            if _rn:
                _kw_conv_map[_rn] = float(_kp.get("conversions") or 0)
    except Exception as _kc_err:
        logger.warning(f"[phase_a] Could not build kw conversion map (non-fatal): {_kc_err}")

    for kw in actions["increase_bid"]:
        current_bid = kw.get("current_bid_micros", 0)
        # Scale bid adjustment by conversion confidence: <5 conv=5%, 5-20=10%, 20+=15%
        _conv = _kw_conv_map.get(kw.get("resource_name", ""), kw.get("conversions", 0))
        _pct = _bid_confidence_pct(_conv)
        new_bid = int(current_bid * (1 + _pct)) if current_bid > 0 else 0
        _kw_camp = kw.get("campaign", "") or primary_campaign  # fallback: keyword resource determines campaign
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
                "bid_change": f"+{int(_pct * 100)}%",
                "new_bid_micros": new_bid,
                "confidence_conversions": _conv,
            },
            optimizer_run_id=run_id,
            reason=kw.get("reason", ""),
            campaign_name=_kw_camp,
            priority=30,
        )
        kw["action_id"] = aid
        actions_pending += 1

    for kw in actions["decrease_bid"]:
        current_bid = kw.get("current_bid_micros", 0)
        # Scale bid adjustment by conversion confidence: <5 conv=5%, 5-20=10%, 20+=15%
        _conv = _kw_conv_map.get(kw.get("resource_name", ""), kw.get("conversions", 0))
        _pct = _bid_confidence_pct(_conv)
        new_bid = max(int(current_bid * (1 - _pct)), 10_000) if current_bid > 0 else 0  # floor at $0.01
        _kw_camp = kw.get("campaign", "") or primary_campaign  # fallback: use primary campaign
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
                "bid_change": f"-{int(_pct * 100)}%",
                "new_bid_micros": new_bid,
                "confidence_conversions": _conv,
            },
            optimizer_run_id=run_id,
            reason=kw.get("reason", ""),
            campaign_name=_kw_camp,
            priority=40,
        )
        kw["action_id"] = aid
        actions_pending += 1

    # Fetch already-pending exact keyword terms to avoid duplicates across runs
    _pending_exact_terms: set = set()
    try:
        from database import _conn as _dedup_conn
        with _dedup_conn() as _dc:
            _dup_rows = _dc.execute(
                "SELECT entity_name FROM gads_audit_log WHERE operation='add_exact_keyword' AND execution_result='pending_approval'"
            ).fetchall()
            _pending_exact_terms = {r[0].lower() for r in _dup_rows}
    except Exception as _de:
        logger.warning(f"Could not load pending exact terms for dedup: {_de}")

    for st in actions["new_exact"]:
        # Skip if this exact term is already pending approval from a previous run
        if st["search_term"].lower() in _pending_exact_terms:
            logger.info(f"  DEDUP skip add_exact_keyword '{st['search_term']}' — already pending approval")
            continue
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

    _set_progress(4)  # Classifying Terms
    # ── Semantic search term classification (Haiku pre-pass) ─────────────────
    # Runs BEFORE the Opus loop. For each campaign, classifies all unclassified
    # search terms using Claude Haiku so the Opus pass can focus on strategy.
    # Negative-verdict terms are staged as add_negative_keyword pending actions.
    logger.info("Running semantic search term classifier (Haiku)...")
    _classifier_negatives_staged = 0
    _classifier_api_key = get_setting("anthropic_api_key") or ""
    _all_camp_names_for_classify = sorted(active_campaigns_with_data) or list(campaign_spend.keys()) or ([primary_campaign] if primary_campaign else [])

    for _classify_camp in _all_camp_names_for_classify:
        try:
            from search_term_classifier import classify_new_terms_for_campaign
            _clf_result = classify_new_terms_for_campaign(
                campaign_name=_classify_camp,
                days=30,
                api_key=_classifier_api_key,
            )
            # Get campaign_resource for this campaign (needed for staging)
            # Bug #9 fix: prefer keyword_perf lookup, fall back to campaign_settings
            # (covers new campaigns that have search terms but no keywords yet)
            _clf_camp_resource = ""
            for kw in keyword_perf:
                if kw.get("campaign", "").strip().lower() == _classify_camp.strip().lower():
                    _clf_camp_resource = kw.get("campaign_resource", "")
                    if _clf_camp_resource:
                        break
            if not _clf_camp_resource:
                # Fall back to campaign_settings map (keyed by resource name)
                for _cr, _cs in campaign_settings.items():
                    if (_cs.get("campaign_name") or "").strip().lower() == _classify_camp.strip().lower():
                        _clf_camp_resource = _cr
                        break

            # Stage negative verdicts as pending add_negative_keyword actions
            # Bug #1 fix: use a local set to dedup within this campaign's staging loop
            # INVALID_ARGUMENT fix: skip staging entirely if we couldn't resolve the
            # campaign resource — executing without it causes a gRPC INVALID_ARGUMENT error.
            if not _clf_camp_resource:
                logger.warning(
                    f"[{_classify_camp}] Classifier: skipping {len(_clf_result.get('negatives',[]))} negatives — "
                    f"could not resolve campaign_resource (campaign may not have keywords yet)"
                )
                continue
            _clf_staged_kws: set[str] = set()
            for neg in _clf_result.get("negatives", []):
                kw_text = neg["search_term"].strip().lower()
                # Skip if already staged in this loop iteration
                if kw_text in _clf_staged_kws:
                    continue
                # Skip if already a live negative in Google Ads
                if _negative_already_handled(kw_text, _classify_camp, live_negatives=live_negatives):
                    continue
                # Bug #11 fix: include actual cost in impact estimate
                _clf_cost = round(float(neg.get("cost") or 0), 2)
                aid = log_pending(
                    operation="add_negative_keyword",
                    entity_type="keyword",
                    entity_id=_clf_camp_resource or kw_text,
                    entity_name=neg["search_term"],
                    before_state={"type": "search_term", "source": "semantic_classifier"},
                    after_state={
                        "keyword_text": neg["search_term"],
                        "match_type": "PHRASE",
                        "campaign_resource": _clf_camp_resource,
                        "campaign": _classify_camp,
                    },
                    optimizer_run_id=run_id,
                    reason=f"[Semantic] {neg['reason']}",
                    campaign_name=_classify_camp,
                    priority=18,  # between rule-based negatives (15) and Opus recs (30+)
                    impact_estimate={"savings_30d_usd": _clf_cost},
                )
                if aid:
                    _clf_staged_kws.add(kw_text)
                    _classifier_negatives_staged += 1

            _n = len(_clf_result.get("negatives", []))
            _c = len(_clf_result.get("conquests", []))
            _tot = _clf_result.get("classified", 0)
            if _tot > 0:
                logger.info(
                    f"  [{_classify_camp}] Classifier: {_tot} classified, "
                    f"{_n} negatives staged, {_c} conquest (kept)"
                )
        except Exception as _clf_err:
            logger.warning(f"  [{_classify_camp}] Classifier failed (non-fatal): {_clf_err}")

    if _classifier_negatives_staged:
        logger.info(f"Semantic classifier staged {_classifier_negatives_staged} negative(s) total")

    _set_progress(5)  # Competitor Memory
    # ── Competitor memory write-back ─────────────────────────────────────────
    # Match recent search terms against known competitor brand stems.
    # Bumps confidence for confirmed competitors; queues unknown brand-like
    # terms for human review. Must run BEFORE Claude advisories so the
    # enriched memory is available in competitor_intel context.
    try:
        _comp_memory_result = _write_back_competitor_memory(
            run_id=run_id,
            search_terms=search_terms,
        )
        if _comp_memory_result.get("confirmed"):
            logger.info(
                f"Competitor memory: confirmed {len(_comp_memory_result['confirmed'])} brand term(s), "
                f"queued {len(_comp_memory_result.get('new_candidates', []))} new candidate(s)"
            )
    except Exception as _cm_err:
        logger.warning(f"Competitor memory write-back failed (non-fatal): {_cm_err}")

    # ── Nearby-practice brand-negative cross-check ───────────────────────────
    # Compare the brand stems from all nearby_practices (within 20 miles) against
    # the active campaign-level negative keywords. Stage any missing ones as
    # add_negative_keyword pending_approval items so the admin can apply them in one click.
    _set_progress(6)  # Brand Negative Check
    try:
        from database import get_brand_negatives_for_campaign
        _nearby_stems = get_brand_negatives_for_campaign(max_miles=20.0)
        if _nearby_stems:
            # C3 fix: live_negatives is a set of lowercased negative keyword texts already
            # fetched from Google Ads at startup — no need to re-iterate keywords list.
            _existing_negs_lower: set[str] = set(s.lower().strip() for s in live_negatives)

            # Also scan existing pending_approval add_negative_keyword rows so we
            # don't re-stage the same brand negative multiple times.
            from database import get_pending_actions
            _pending = get_pending_actions(limit=500)
            for _pa in _pending:
                if _pa.get("operation") == "add_negative_keyword":
                    _after = _pa.get("after_state") or {}
                    if isinstance(_after, str):
                        try:
                            import json as _j; _after = _j.loads(_after)
                        except Exception:
                            _after = {}
                    _kt = (_after.get("keyword_text") or "").lower().strip()
                    if _kt:
                        _existing_negs_lower.add(_kt)

            # C2 fix: build all_campaign_names_for_brand_check from campaign_settings
            # (campaign_settings is keyed by resource_name and already available in scope).
            # Only include ENABLED campaigns — don't stage negatives on paused campaigns.
            _all_camp_for_brand = sorted(
                {s["campaign_name"] for s in campaign_settings.values()
                 if s.get("campaign_name") and s.get("campaign_status", "").upper() == "ENABLED"}
            ) or ([primary_campaign] if primary_campaign else [])

            # C4 fix: build a name→resource_name lookup from campaign_settings dict
            # (keyed by resource_name → {"campaign_name": ..., "resource_name": ..., ...})
            _camp_name_to_res: dict[str, str] = {
                s["campaign_name"].lower(): rn
                for rn, s in campaign_settings.items()
                if s.get("campaign_name")
            }

            _brand_staged = 0
            for _stem in _nearby_stems:
                _stem_lower = _stem.lower().strip()
                if not _stem_lower or _stem_lower in _existing_negs_lower:
                    continue
                # Stage as pending for every active campaign (brand negatives apply account-wide)
                for _camp_name in _all_camp_for_brand:
                    if not _camp_name:
                        continue
                    _camp_res = _camp_name_to_res.get(_camp_name.lower(), "")
                    if not _camp_res:
                        continue
                    # C1 fix: use log_pending() from campaign_audit (already imported at top of function)
                    log_pending(
                        operation="add_negative_keyword",
                        entity_type="keyword",
                        entity_id=_camp_res,
                        entity_name=_stem_lower,
                        before_state={"source": "nearby_practices_db"},
                        after_state={"keyword_text": _stem_lower, "match_type": "PHRASE", "campaign_resource": _camp_res},
                        optimizer_run_id=run_id,
                        reason=f"Brand negative: nearby practice stem '{_stem_lower}' missing from campaign negatives — prevents accidental clicks from patients searching for a competitor's contact info.",
                        campaign_name=_camp_name,
                        priority=5,
                        impact_estimate={"savings_30d_usd": 0},
                    )
                    _existing_negs_lower.add(_stem_lower)  # don't re-stage across campaigns
                    _brand_staged += 1
                    break  # one pending row per stem (applied account-wide when approved)

            if _brand_staged:
                logger.info(f"[brand_neg_check] Staged {_brand_staged} missing brand negative(s) as pending_approval")
    except Exception as _bnc_err:
        logger.warning(f"Brand-negative cross-check failed (non-fatal): {_bnc_err}")

    # ── Own-brand negatives on acquisition campaigns ───────────────────────────
    # For every acquisition (non-brand) campaign, ensure GDC's own brand stems
    # are present as PHRASE negatives on EACH campaign individually.
    # This stops existing patients from burning acquisition budget when they
    # search "grafton dental" or "dr gupta".
    #
    # Opus bug fixes applied:
    #   C1: use get_pending_approvals (not get_pending_actions) + after_state_json key
    #   C2: live_negatives_by_campaign keyed by campaign name, not resource_name
    #   M1: dedup per (stem, campaign_res) tuple — never skip a campaign because
    #       another campaign already has the stem staged
    _set_progress(7)  # Own-Brand Check
    try:
        import json as _bp_json
        from brand_policy import get_own_brand_negatives, is_brand_campaign as _is_brand_camp
        from database import get_pending_approvals as _get_pending_approvals
        _own_brand_stems = get_own_brand_negatives()

        # Build per-campaign pending set: {(campaign_resource, keyword_text)} already queued
        _ob_pending_pairs: set[tuple] = set()
        for _pa in (_get_pending_approvals() or []):
            if _pa.get("operation") == "add_negative_keyword":
                _after_raw = _pa.get("after_state_json") or "{}"
                try:
                    _after = _bp_json.loads(_after_raw) if isinstance(_after_raw, str) else _after_raw
                except Exception:
                    _after = {}
                _kt = (_after.get("keyword_text") or "").lower().strip()
                _cr = (_after.get("campaign_resource") or "").strip()
                if _kt and _cr:
                    _ob_pending_pairs.add((_cr, _kt))

        _ob_camp_name_to_res: dict[str, str] = {
            s["campaign_name"].lower(): rn
            for rn, s in campaign_settings.items()
            if s.get("campaign_name")
        }
        # Reverse map for display names (resource_name → display campaign name)
        _ob_res_to_display: dict[str, str] = {
            rn: s["campaign_name"]
            for rn, s in campaign_settings.items()
            if s.get("campaign_name")
        }

        _ob_staged = 0
        for _stem in _own_brand_stems:
            _stem_lower = _stem.lower().strip()
            if not _stem_lower:
                continue
            for _camp_name_lower, _camp_res in _ob_camp_name_to_res.items():
                # Skip brand campaigns — they WANT brand traffic
                if _is_brand_camp(_camp_name_lower):
                    continue
                # Skip PAUSED campaigns — only stage negatives on ENABLED campaigns
                _ob_camp_status = campaign_settings.get(_camp_res, {}).get("campaign_status", "").upper()
                if _ob_camp_status and _ob_camp_status != "ENABLED":
                    continue
                # C1+M1 fix: check per-campaign pending pairs (not flat account set)
                if (_camp_res, _stem_lower) in _ob_pending_pairs:
                    continue
                # C2 fix: look up by campaign name (how live_negatives_by_campaign is keyed)
                # The dict is keyed by raw campaign name (mixed case from GAds), so try
                # both lowercased lookup and a scan for case-insensitive match.
                _raw_camp_name = _ob_res_to_display.get(_camp_res, _camp_name_lower)
                _camp_live_negs = (
                    live_negatives_by_campaign.get(_raw_camp_name)
                    or live_negatives_by_campaign.get(_raw_camp_name.lower())
                    or set()
                )
                _camp_negs_lower = set(n.lower().strip() for n in _camp_live_negs)
                if _stem_lower in _camp_negs_lower:
                    continue
                # Skip if a broader PHRASE negative already covers this stem
                # (e.g. "grafton dental" covers "grafton dental care" under PHRASE match)
                _already_covered = any(
                    _stem_lower.startswith(existing) or existing in _stem_lower
                    for existing in _camp_negs_lower if existing
                )
                if _already_covered:
                    continue
                _display_name = _ob_res_to_display.get(_camp_res, _raw_camp_name)
                log_pending(
                    operation="add_negative_keyword",
                    entity_type="keyword",
                    entity_id=_camp_res,
                    entity_name=_stem_lower,
                    before_state={"source": "brand_policy_own_brand"},
                    after_state={
                        "keyword_text": _stem_lower,
                        "match_type": "PHRASE",
                        "campaign_resource": _camp_res,
                    },
                    optimizer_run_id=run_id,
                    reason=(
                        f"Own-brand negative: '{_stem_lower}' missing from acquisition campaign — "
                        f"existing patients searching your practice name should not burn acquisition budget. "
                        f"Route these searches to the Brand campaign instead."
                    ),
                    campaign_name=_display_name,
                    priority=8,
                    impact_estimate={"savings_30d_usd": 0, "impact_type": "budget_efficiency"},
                )
                _ob_pending_pairs.add((_camp_res, _stem_lower))  # prevent re-staging this run
                _ob_staged += 1

        if _ob_staged:
            logger.info(f"[own_brand_check] Staged {_ob_staged} own-brand negative(s) on acquisition campaigns")
    except Exception as _ob_err:
        logger.warning(f"Own-brand negative cross-check failed (non-fatal): {_ob_err}")

    # ── Generic dental negatives on brand campaign ────────────────────────────
    # For every brand campaign, ensure generic dental/service terms are present
    # as PHRASE negatives. This stops shopping-intent searchers from burning brand
    # budget (those searches belong in acquisition campaigns).
    _set_progress(8)  # Brand Camp Check
    try:
        import json as _bp_json2
        from brand_policy import get_generic_dental_negatives, is_brand_campaign as _is_brand_camp2
        from database import get_pending_approvals as _get_pending_approvals2
        _generic_stems = get_generic_dental_negatives()

        # Build per-campaign pending set for brand check
        _gc_pending_pairs: set[tuple] = set()
        for _pa in (_get_pending_approvals2() or []):
            if _pa.get("operation") == "add_negative_keyword":
                _after_raw = _pa.get("after_state_json") or "{}"
                try:
                    _after = _bp_json2.loads(_after_raw) if isinstance(_after_raw, str) else _after_raw
                except Exception:
                    _after = {}
                _kt = (_after.get("keyword_text") or "").lower().strip()
                _cr = (_after.get("campaign_resource") or "").strip()
                if _kt and _cr:
                    _gc_pending_pairs.add((_cr, _kt))

        _gc_camp_name_to_res: dict[str, str] = {
            s["campaign_name"].lower(): rn
            for rn, s in campaign_settings.items()
            if s.get("campaign_name")
        }
        _gc_res_to_display: dict[str, str] = {
            rn: s["campaign_name"]
            for rn, s in campaign_settings.items()
            if s.get("campaign_name")
        }

        _gc_staged = 0
        for _stem in _generic_stems:
            _stem_lower = _stem.lower().strip()
            if not _stem_lower:
                continue
            for _camp_name_lower, _camp_res in _gc_camp_name_to_res.items():
                # Only apply to brand campaigns
                if not _is_brand_camp2(_camp_name_lower):
                    continue
                # Skip PAUSED campaigns — only stage negatives on ENABLED campaigns
                _gc_camp_status = campaign_settings.get(_camp_res, {}).get("campaign_status", "").upper()
                if _gc_camp_status and _gc_camp_status != "ENABLED":
                    continue
                if (_camp_res, _stem_lower) in _gc_pending_pairs:
                    continue
                _raw_camp_name = _gc_res_to_display.get(_camp_res, _camp_name_lower)
                _camp_live_negs = (
                    live_negatives_by_campaign.get(_raw_camp_name)
                    or live_negatives_by_campaign.get(_raw_camp_name.lower())
                    or set()
                )
                _camp_negs_lower = set(n.lower().strip() for n in _camp_live_negs)
                if _stem_lower in _camp_negs_lower:
                    continue
                _already_covered = any(
                    _stem_lower.startswith(existing) or existing in _stem_lower
                    for existing in _camp_negs_lower if existing
                )
                if _already_covered:
                    continue
                _display_name = _gc_res_to_display.get(_camp_res, _raw_camp_name)
                log_pending(
                    operation="add_negative_keyword",
                    entity_type="keyword",
                    entity_id=_camp_res,
                    entity_name=_stem_lower,
                    before_state={"source": "brand_policy_generic_dental"},
                    after_state={
                        "keyword_text": _stem_lower,
                        "match_type": "PHRASE",
                        "campaign_resource": _camp_res,
                    },
                    optimizer_run_id=run_id,
                    reason=(
                        f"Brand campaign negative: '{_stem_lower}' missing — generic dental searches "
                        f"indicate shopping/acquisition intent and should be served by acquisition campaigns, "
                        f"not the brand budget."
                    ),
                    campaign_name=_display_name,
                    priority=8,
                    impact_estimate={"savings_30d_usd": 0, "impact_type": "budget_efficiency"},
                )
                _gc_pending_pairs.add((_camp_res, _stem_lower))
                _gc_staged += 1

        if _gc_staged:
            logger.info(f"[brand_camp_check] Staged {_gc_staged} generic negative(s) on brand campaign(s)")
    except Exception as _gc_err:
        logger.warning(f"Brand-campaign generic-negative cross-check failed (non-fatal): {_gc_err}")

    # Claude structured recommendations — run once per active campaign.
    # Returns dicts with operation + exact API parameters, not plain text.
    _set_progress(10)  # AI Per-Campaign
    logger.info("Calling Claude (Opus) for structured recommendations...")
    # Use ALL campaigns with keyword data — not just campaign_spend keys
    # (campaign_spend only covers the allow-listed set in legacy mode; now we use all)
    all_campaign_names = sorted(active_campaigns_with_data) or list(campaign_spend.keys()) or ([primary_campaign] if primary_campaign else [])
    _total_camps = len(all_campaign_names)
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
        "update_geo_targeting": ("campaign", "campaign_resource",  "campaign_resource"),
        "change_budget":        ("campaign", "campaign_resource", "campaign_resource"),
        "change_bid_strategy":  ("campaign", "campaign_resource", "bid_strategy"),
        "change_match_type":    ("keyword",  "resource_name",     "keyword_text"),
        "add_asset":            ("campaign", "campaign_resource",  "asset_type"),
        "replace_ad":           ("ad",       "old_ad_group_ad_resource", "old_ad_group_ad_resource"),
        "pause_ad_group":       ("ad_group", "ad_group_resource",        "ad_group_name"),
        "create_skag":          ("ad_group", "source_ad_group_name",     "keyword_text"),
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

    # PR 5: accumulate competitor intel across all campaigns for account-level pass
    _per_camp_competitor_blocks: dict = {}  # camp_name -> competitor_intel dict

    for _camp_idx, camp_name in enumerate(all_campaign_names):
        _set_progress(10, campaign_context=f"{camp_name} ({_camp_idx + 1}/{_total_camps})")
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

        # Pre-fetch existing campaign assets (callouts, snippets, sitelinks)
        camp_existing_assets: dict = {"callouts": [], "structured_snippets": [], "sitelinks": []}
        if camp_resource:
            try:
                camp_existing_assets = _fetch_campaign_assets(client, customer_id, camp_resource)
                n_assets = (len(camp_existing_assets["callouts"])
                            + len(camp_existing_assets["structured_snippets"])
                            + len(camp_existing_assets["sitelinks"]))
                if n_assets:
                    logger.info(f"  [{camp_name}] {n_assets} existing asset(s) fetched for Claude context")
            except Exception as _ae:
                logger.warning(f"  [{camp_name}] Asset pre-fetch failed (non-fatal): {_ae}")

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

        # ── PR 1: load wizard context from DB ────────────────────────────────
        from database import get_campaign_by_gads_resource as _get_camp_by_res, \
                             get_campaign_by_name as _get_camp_by_name
        _camp_local_row = None
        if camp_resource:
            _camp_local_row = _get_camp_by_res(camp_resource)
        if not _camp_local_row:
            _camp_local_row = _get_camp_by_name(camp_name)

        _camp_build: dict = {}
        _camp_strategy: dict = {}
        if _camp_local_row:
            _cb_raw = _camp_local_row.get("campaign_build_json")
            if _cb_raw:
                try:
                    _camp_build = json.loads(_cb_raw) if isinstance(_cb_raw, str) else (_cb_raw or {})
                except Exception:
                    _camp_build = {}
            _cs_raw = _camp_local_row.get("strategy_json")
            if _cs_raw:
                try:
                    _camp_strategy = json.loads(_cs_raw) if isinstance(_cs_raw, str) else (_cs_raw or {})
                except Exception:
                    _camp_strategy = {}

        _competitor_intel: dict = _camp_build.get("competitor_analysis") or {}
        _conquest_kws: set = {
            k.strip().lower() for k in (_competitor_intel.get("conquest_keywords") or []) if k.strip()
        }
        # PR 5: collect for account-level union
        if _competitor_intel:
            _per_camp_competitor_blocks[camp_name] = _competitor_intel

        _campaign_brief: dict = {}
        if _camp_local_row:
            _campaign_brief = {
                "service_focus":             _camp_local_row.get("service_focus", ""),
                "objective":                 _camp_local_row.get("objective", ""),
                "target_audience":           _camp_local_row.get("target_audience", ""),
                "promo_offer":               _camp_local_row.get("promo_offer", ""),
                "planned_monthly_budget_usd": _camp_local_row.get("monthly_budget") or 0,
                "target_cpl_usd":            _camp_local_row.get("expected_cpl") or 0,
                "strategy":                  _camp_strategy,
            }

        _planned_build: dict = {
            "planned_keywords":  _camp_build.get("keywords") or {},
            "planned_ad_groups": (_camp_build.get("ad_groups") or {}).get("ad_groups") or [],
        }

        # ── Lifecycle classification ──────────────────────────────────────────
        from lifecycle import build_lifecycle_block as _build_lifecycle
        _launch_date_db = (_camp_local_row or {}).get("launch_date") or None
        _first_imp_date = None
        _camp_id_for_lifecycle = (_camp_local_row or {}).get("campaign_id") or ""
        if not _launch_date_db and camp_resource:
            # Fallback: query GAds for earliest segment date in LAST_365_DAYS (cached per run)
            # NOTE: We do NOT write this back to DB — GAds only reports LAST_365_DAYS, so
            # a campaign launched >365 days ago would get a wrong date persisted permanently.
            # The backfill script (scripts/backfill_launch_dates.py) handles DB population
            # for campaigns that were launched via our wizard and have a real launch_date.
            _first_imp_date = _fetch_first_impression_date(camp_resource)
            if _first_imp_date:
                logger.info(f"[lifecycle] using first_impression_date '{_first_imp_date}' for '{camp_name}' (in-memory only)")

        _camp_lifecycle = _build_lifecycle(
            launch_date=_launch_date_db,
            first_impression_date=_first_imp_date,
            conversions_30d=sum(k.get("conversions", 0) for k in camp_kw),
            clicks_30d=sum(k.get("clicks", 0) for k in camp_kw),
        )
        logger.info(
            f"[lifecycle] '{camp_name}': stage={_camp_lifecycle['stage']}, "
            f"days={_camp_lifecycle['days_since_launch']}, "
            f"source={_camp_lifecycle['source']}, "
            f"in_learning={_camp_lifecycle['in_learning_period']}"
        )

        # ── PR 2: budget feasibility signal ──────────────────────────────────
        _live_cpc_avg = 0.0
        _camp_clicks_total = sum(k.get("clicks", 0) for k in camp_kw)
        _camp_cost_total   = sum(k.get("cost", 0) for k in camp_kw)
        if _camp_clicks_total > 0:
            _live_cpc_avg = _camp_cost_total / _camp_clicks_total

        _budget_feasibility = _compute_budget_click_signal(
            planned_ad_groups=_planned_build["planned_ad_groups"],
            monthly_budget=(_campaign_brief.get("planned_monthly_budget_usd") or 0),
            live_daily_budget=(camp_settings.get("daily_budget_usd") or 0),
            live_cpc_avg=_live_cpc_avg,
            bidding_strategy=(camp_settings.get("bidding_strategy_type") or ""),
        )

        # ── PR 3: intent signals ──────────────────────────────────────────────
        try:
            from intent_signals import get_intent_signals as _get_intent_signals
            from search_term_classifier import _detect_campaign_type as _detect_ct
            _intent_signals = _get_intent_signals(_detect_ct(camp_name))
        except Exception as _is_err:
            logger.debug(f"[optimizer] intent_signals load failed (non-fatal): {_is_err}")
            _intent_signals = {}

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
            lqi=lqi_signals,
            # PR 1
            campaign_brief=_campaign_brief,
            competitor_intel=_competitor_intel,
            planned_build=_planned_build,
            # PR 2
            budget_feasibility=_budget_feasibility,
            # PR 3
            intent_signals=_intent_signals,
            # PR 7
            conquest_keywords_protected=_conquest_kws,
            # Lifecycle
            lifecycle=_camp_lifecycle,
            # Budget constraint
            budget_constrained=_budget_constrained,
            # Existing campaign assets (callouts, snippets, sitelinks)
            existing_campaign_assets=camp_existing_assets,
        )
        if not structured:
            continue

        logger.info(f"Claude recommendations for '{camp_name}': {len(structured)}")

        _replace_ad_count_for_camp = 0     # enforce one replace_ad per campaign per run
        _pause_ag_count_for_camp = 0       # enforce one pause_ad_group per campaign per run
        _geo_seen_this_run: set = set()    # enforce one update_geo_targeting per campaign per run (in-memory)

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

            # Build before/after state from the structured fields
            # Must happen before PR7 guards so guards can use before["current_radius_miles"]
            # without a second DB round-trip.
            after = {k: v for k, v in rec.items() if k != "operation"}
            before = {}  # reset unconditionally each iteration to prevent cross-rec leakage

            # For update_geo_targeting: populate before_state with current geo JSON + radius
            if op == "update_geo_targeting" and camp_resource:
                try:
                    from database import get_geo_json_for_campaign_resource as _get_geo
                    _current_geo_raw = _get_geo(camp_resource)
                    if _current_geo_raw:
                        _current_geo = json.loads(_current_geo_raw)
                        _locs = _current_geo.get("locations") or []
                        # Extract current radius from any city-type proximity entry
                        _city_entry = next(
                            (l for l in _locs if l.get("type") == "city" and l.get("radius")),
                            None
                        )
                        _current_radius = int(_city_entry["radius"]) if _city_entry else None
                        _current_zips = [
                            str(l.get("value", ""))
                            for l in _locs
                            if l.get("type") == "postal" and l.get("value")
                        ]
                        before = {
                            "current_geo_json": _current_geo_raw,
                            "current_radius_miles": _current_radius,
                            "current_zip_codes": _current_zips,
                        }
                except Exception as _geo_before_err:
                    logger.warning(f"  [{camp_name}] Could not fetch current geo for before_state: {_geo_before_err}")

            # ── PR7 Safety guards for update_geo_targeting ───────────────────────────────
            if op == "update_geo_targeting":
                # Guard 1: One-per-run (in-memory) — skip if a geo rec was already approved
                # for this campaign in THIS run (catches same-batch duplicates from Claude).
                if camp_name in _geo_seen_this_run:
                    logger.info(f"  [{camp_name}] SKIPPED update_geo_targeting — geo rec already queued this run")
                    continue

                # Guard 1b: Persistent — skip if a pending_approval geo rec already exists
                # in the DB. Prevents stacking across separate runs.
                try:
                    from database import _conn as _db_conn_pr7
                    with _db_conn_pr7() as _pr7_c:
                        _existing_pending = _pr7_c.execute(
                            """SELECT COUNT(*) FROM gads_audit_log
                               WHERE operation='update_geo_targeting'
                               AND campaign_name=?
                               AND execution_result='pending_approval'""",
                            (camp_name,),
                        ).fetchone()[0]
                    if _existing_pending > 0:
                        logger.info(
                            f"  [{camp_name}] SKIPPED update_geo_targeting — "
                            f"{_existing_pending} pending geo rec(s) already await approval"
                        )
                        continue
                except Exception as _pr7_err:
                    logger.warning(f"  [{camp_name}] One-per-run geo check failed (non-fatal): {_pr7_err}")

                # Guard 2: Progressive shrink — reject if proposed radius is >30% smaller
                # than current radius. Requires incremental changes to avoid over-shrinking.
                # Reuses before["current_radius_miles"] already fetched above (no double-fetch).
                _prop_radius = rec.get("proposed_radius_miles")
                _cur_radius_for_guard = before.get("current_radius_miles")
                if _cur_radius_for_guard and _prop_radius:
                    _shrink_pct = (_cur_radius_for_guard - float(_prop_radius)) / _cur_radius_for_guard
                    if _shrink_pct > 0.30:
                        logger.warning(
                            f"  [{camp_name}] SKIPPED update_geo_targeting — proposed {_prop_radius} mi "
                            f"is {_shrink_pct*100:.0f}% smaller than current {_cur_radius_for_guard} mi "
                            f"(max allowed shrink: 30%)"
                        )
                        continue
                    logger.info(
                        f"  [{camp_name}] Geo shrink guard passed — "
                        f"{'growth' if _shrink_pct <= 0 else f'{_shrink_pct*100:.0f}% shrink'}"
                    )
                else:
                    logger.info(f"  [{camp_name}] Geo shrink guard skipped — no current radius baseline")

                # Mark this campaign as having a geo rec in this run
                _geo_seen_this_run.add(camp_name)
            # ────────────────────────────────────────────────────────────────────────────

            advisories.append(f"[{camp_name}] {reason}")

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

            # For create_skag: bake recommendation_id into after_state BEFORE logging
            # so that gads_approve_action can read it back from after_state_json.
            if op == "create_skag":
                after["recommendation_id"] = ""  # placeholder — replaced after log_pending gives us aid

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

            # ── create_skag: seed skag_recommendations + patch after_state_json ──
            if op == "create_skag" and aid:
                _skag_kw        = rec.get("keyword_text", "")
                _skag_src_ag    = rec.get("source_ad_group_name", "")
                _skag_new_ag    = rec.get("new_ad_group_name") or f"SKAG — {_skag_kw}"
                _skag_camp_id   = (_camp_local_row.get("campaign_id") if _camp_local_row else "") or ""
                _skag_score     = rec.get("score", 0.0)
                _skag_breakdown = json.dumps(rec.get("score_breakdown", {}))
                _skag_snapshot  = json.dumps(rec.get("signal_snapshot", {}))
                try:
                    from database import _conn as _skag_ins_conn
                    with _skag_ins_conn() as _sc:
                        # Use INSERT OR IGNORE — the unique partial index prevents
                        # duplicate (keyword, source_ag) pairs that aren't rejected/reverted.
                        _sc.execute("""
                            INSERT OR IGNORE INTO skag_recommendations
                                (recommendation_id, campaign_id, campaign_name,
                                 source_ad_group_name, keyword_text, match_type,
                                 new_ad_group_name, score, score_breakdown, signal_snapshot, status)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                        """, (
                            aid, _skag_camp_id, camp_name,
                            _skag_src_ag, _skag_kw, "EXACT",
                            _skag_new_ag, _skag_score, _skag_breakdown, _skag_snapshot,
                        ))
                        # Patch after_state_json to include recommendation_id = aid
                        _after_patched = json.dumps({**after, "recommendation_id": aid})
                        _sc.execute(
                            "UPDATE gads_audit_log SET after_state_json=? WHERE action_id=?",
                            (_after_patched, aid)
                        )
                    logger.info(
                        "  [create_skag] seeded skag_recommendations rec=%s kw='%s' ag='%s'",
                        aid[:8], _skag_kw, _skag_src_ag
                    )
                except Exception as _skag_ins_err:
                    logger.warning(
                        "  [create_skag] failed to seed skag_recommendations for %s (non-fatal): %s",
                        aid[:8], _skag_ins_err
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

    _set_progress(11)  # AI Account-Level
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

    # PR 5: build account-wide competitor intel union from per-campaign blocks
    _all_conquest_kws = sorted({
        k.strip().lower()
        for intel in _per_camp_competitor_blocks.values()
        for k in (intel.get("conquest_keywords") or [])
        if k.strip()
    })
    _all_differentiators = sorted({
        d
        for intel in _per_camp_competitor_blocks.values()
        for d in (intel.get("our_differentiators") or [])
        if d
    })
    _competitor_intel_union: dict = {
        "all_conquest_keywords": _all_conquest_kws,
        "all_differentiators": _all_differentiators,
        "by_campaign": {
            cn: {
                "conquest_keywords": intel.get("conquest_keywords") or [],
                "differentiators": intel.get("our_differentiators") or [],
            }
            for cn, intel in _per_camp_competitor_blocks.items()
        },
    }

    # Build campaign_name → daily_budget_usd map for budget sieve at account level
    # Sources in priority order: campaign_settings (resource-keyed) → camp_spend_for_acct
    _acct_budget_by_campaign: dict = {}
    for cn in all_campaign_names:
        # campaign_settings is keyed by resource name; we need name-keyed lookup
        # camp_spend_for_acct has daily_budget_usd from keyword_perf daily_budget_micros
        bud = (camp_spend_for_acct.get(cn) or {}).get("daily_budget_usd")
        if bud is None:
            # Fallback: scan campaign_settings for a matching name
            for cs in campaign_settings.values():
                if cs.get("campaign_name", "").strip().lower() == cn.strip().lower():
                    bud = cs.get("daily_budget_usd")
                    break
        if bud is not None:
            _acct_budget_by_campaign[cn] = float(bud or 0.0)

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
        existing_negatives_by_campaign=live_negatives_by_campaign,
        memory_digest=memory_digest,
        lqi=lqi_signals,
        competitor_intel_union=_competitor_intel_union,
        budget_constrained=_budget_constrained,
        budget_by_campaign=_acct_budget_by_campaign if _budget_constrained else None,
        campaign_lifecycle_map=_rule_lifecycle_by_camp,
        campaign_settings_raw=campaign_settings,
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

    _set_progress(12)  # Finalizing
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

    _set_progress_done()
    return report


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = optimize_campaign(dry_run=True)
    print(json.dumps(result, indent=2, default=str))
