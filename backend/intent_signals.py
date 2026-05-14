"""
intent_signals.py — Shared keyword intent signal definitions for dental PPC campaigns.

Used by:
  - main.py  (campaign creation wizard build-step: keywords)
  - ai_optimizer.py (per-campaign Claude advisory prompts)

Keeping both in sync ensures the wizard and the optimizer speak the same
vocabulary when classifying search terms and recommending keywords/negatives.
"""

# High-intent keyword pattern examples by campaign type.
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
    "emergency":  ["jobs", "salary", "school", "training", "course", "free", "home remedy", "DIY"],
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
    }
