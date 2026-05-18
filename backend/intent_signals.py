"""
intent_signals.py — Shared keyword intent signal definitions for dental PPC campaigns.

Used by:
  - main.py  (campaign creation wizard build-step: keywords)
  - ai_optimizer.py (per-campaign Claude advisory prompts)

Keeping both in sync ensures the wizard and the optimizer speak the same
vocabulary when classifying search terms and recommending keywords/negatives.
"""

# ── Emergency urgency tokens ──────────────────────────────────────────────────
# A valid emergency keyword MUST contain at least one of these tokens.
# Used by: keyword wizard (main.py), search term classifier, CAMPAIGN_INTENT_RULES.
EMERGENCY_URGENCY_TOKENS: list[str] = [
    "emergency", "urgent", "urgently", "same day", "same-day",
    "toothache", "tooth ache", "tooth pain", "dental pain",
    "broken tooth", "cracked tooth", "chipped tooth", "chip",
    "knocked out", "knocked-out", "avulsed",
    "abscessed", "abscess", "swollen", "swelling",
    "lost filling", "lost crown", "broken crown", "broken bridge",
    "open now", "open today", "open late", "after hours",
    "24 hour", "24-hour", "weekend dentist", "weekend dental",
    "dentist tonight", "dentist today", "dentist asap",
    "pain relief", "tooth infection",
    "bleeding", "bleeding gum", "bleeding tooth",
    "asap", "tonight", "today",  # standalone urgency signals
]

# ── High-intent keyword pattern examples by campaign type ─────────────────────
# These are representative search queries that signal genuine buying intent.
INTENT_SIGNALS_BY_TYPE: dict[str, list[str]] = {
    "emergency":  [
        '"emergency dentist near me"', '"toothache near me"', '"dentist open now"',
        '"same day dentist"', '"broken tooth emergency"', '"urgent dental care"',
    ],
    "implants":   [
        '"dental implants near me"', '"tooth implant cost"', '"all on 4 implants"',
        '"dental implants grafton ma"', '"implant dentist worcester county"',
        '"single tooth implant price"',
    ],
    "invisalign": [
        '"invisalign near me"', '"invisalign cost"', '"clear braces near me"',
        '"orthodontist grafton ma"', '"teeth straightening cost"',
    ],
    "cosmetic":   [
        '"cosmetic dentist near me"', '"veneers cost near me"', '"teeth whitening dentist"',
        '"smile makeover grafton ma"', '"porcelain veneers price"',
    ],
    "gum":        [
        '"gum recession treatment near me"', '"periodontist near me"',
        '"gum graft cost"', '"scaling root planing near me"',
        '"receding gums treatment worcester"',
    ],
    "general":    [
        '"dentist near me"', '"family dentist grafton ma"', '"dentist accepting new patients"',
        '"dental cleaning near me"', '"affordable dentist worcester county"',
    ],
}

# Low-intent negative-keyword patterns by campaign type.
# Search terms containing these tokens are unlikely to convert for the given service.
NEG_INTENT_BY_TYPE: dict[str, list[str]] = {
    # Emergency: anything without urgency signal is wrong-intent.
    # Navigational/general searches belong in the General Dentistry campaign.
    "emergency":  [
        # Career / informational (always wrong)
        "jobs", "salary", "school", "training", "course", "free", "home remedy", "DIY",
        # Navigational — patient shopping for a regular dentist, not in acute pain
        "dentist near me", "dentists near me", "dentist in", "dentists in",
        "family dentist", "family dentistry", "new patient", "new patients",
        "dental cleaning", "teeth cleaning", "routine cleaning", "checkup",
        "dental checkup", "dental exam", "annual exam", "preventive",
        "affordable dentist", "best dentist", "top dentist", "local dentist",
        "accepting new patients", "establish care", "primary dentist",
        # Bare city-dentist patterns (e.g. "dentist worcester", "dentist grafton")
        # These are navigational; someone in acute pain searches "emergency dentist worcester"
        "dentist worcester", "dentist grafton", "dentist shrewsbury",
        "dentist westborough", "dentist northborough", "dentist millbury",
        "dentist auburn", "dentist milford", "dentist framingham",
        "dentist marlborough", "dentist hopkinton",
        # Service-specific that belong in other campaigns
        # NOTE: "dental implant emergency" / "broken implant" ARE valid emergencies —
        # these patterns are excluded from the fast-path block; Haiku handles them.
        "invisalign", "clear aligner", "braces", "orthodont",
        "veneer", "veneers", "whitening", "cosmetic",
        "cleaning near me", "hygienist",
    ],
    "implants":   [
        "jobs", "salary", "free", "insurance only", "medicaid", "snap-on smile",
        "flipper", "partial denture", "school", "course",
    ],
    "invisalign": [
        "jobs", "free", "insurance only", "medicaid", "braces for kids", "school",
        "metal braces only", "retainer only",
    ],
    "cosmetic":   ["jobs", "free", "insurance", "medicaid", "DIY", "at home", "school"],
    "gum":        ["jobs", "free", "home remedy", "oil pulling", "DIY", "insurance only", "school"],
    # General dentistry negatives: only true non-dental / career / irrelevant queries.
    # DO NOT include specialty services (implants, emergency, cosmetic, veneers, etc.) —
    # those are services GDC actually offers and should appear in general campaigns.
    "general":    ["jobs", "salary", "career", "free", "medicaid only", "school", "DIY",
                   "veterinary", "animal", "dog", "cat", "pet", "dental assistant",
                   "dental school", "dental hygienist", "receptionist"],
}


def get_intent_signals(campaign_type: str) -> dict:
    """
    Return the high-intent examples and low-intent negatives for a given campaign type.

    Args:
        campaign_type: one of "emergency", "implants", "invisalign", "cosmetic", "gum", "general"
                       (unknown values fall back to "general")

    Returns:
        {
          "campaign_type": str,
          "high_intent_examples": list[str],
          "low_intent_negatives": list[str],
        }
    """
    ctype = campaign_type.strip().lower() if campaign_type else "general"
    return {
        "campaign_type": ctype,
        "high_intent_examples": INTENT_SIGNALS_BY_TYPE.get(ctype, INTENT_SIGNALS_BY_TYPE["general"]),
        "low_intent_negatives": NEG_INTENT_BY_TYPE.get(ctype, NEG_INTENT_BY_TYPE["general"]),
        # For emergency campaigns only: keywords must contain one of these tokens
        "urgency_tokens_required": EMERGENCY_URGENCY_TOKENS if ctype == "emergency" else [],
    }
