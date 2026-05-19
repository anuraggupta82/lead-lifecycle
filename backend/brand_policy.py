"""
brand_policy.py — Single source of truth for GDC own-brand and generic-dental
keyword policy.

Used by:
  - ai_optimizer.py  — cross-check functions that stage missing negatives
  - google_ads_create.py — wizard belt-and-suspenders negative injection (P2)
  - intent_signals.py — brand campaign type entry (P2)

All lists use lowercase strings. Match type is always PHRASE when applied as
negatives (so close-variants like "grafton dental care worcester" are also blocked).
"""

from __future__ import annotations

# ── GDC own-brand stems ───────────────────────────────────────────────────────
# These should be NEGATIVES on every ACQUISITION campaign so that existing
# patients searching for the practice by name don't burn acquisition budget.
# They should be KEPT (positive) on the brand campaign only.

_OWN_BRAND_STEMS: list[str] = [
    # Practice name variants
    "grafton dental care",
    "grafton dental",
    "gdc dental",
    "gdc grafton",
    "graftondentalcare",
    "graftondentalcare.com",
    # Doctor name variants
    "dr gupta",
    "dr. gupta",
    "doctor gupta",
    "anurag gupta",
    "dr anurag gupta",
    "dr. anurag gupta",
    "gupta dentist",
    "dr gupta grafton",
    "dr gupta dentist",
    "dr gupta dds",
    # Branded contact-lookup patterns (existing patient navigational intent)
    "grafton dental care reviews",
    "grafton dental care hours",
    "grafton dental care phone",
    "grafton dental care appointment",
    "grafton dental care address",
]

# ── Generic dental terms ──────────────────────────────────────────────────────
# These should be NEGATIVES on the BRAND campaign so that shopping/acquisition
# intent doesn't burn the brand budget (those searches belong to acquisition
# campaigns which bid on these terms intentionally).

_GENERIC_DENTAL_STEMS: list[str] = [
    # Pure generic intent
    "dentist",
    "dentists",
    "dental",
    "near me",
    "family dentist",
    "family dentistry",
    "dentist near me",
    "dentists near me",
    "find a dentist",
    "local dentist",
    "dental cleaning",
    "teeth cleaning",
    "routine cleaning",
    "dental checkup",
    "dental exam",
    "new patient",
    "new patients",
    "accepting new patients",
    "best dentist",
    "top dentist",
    "affordable dentist",
    "cheap dentist",
    # Procedure-specific acquisition terms (belong to dedicated campaigns)
    "dental implant",
    "dental implants",
    "implant dentist",
    "tooth implant",
    "all on 4",
    "all-on-4",
    "all on four",
    "denture",
    "dentures",
    "invisalign",
    "clear aligner",
    "clear aligners",
    "teeth straightening",
    "veneer",
    "veneers",
    "teeth whitening",
    "cosmetic dentist",
    "smile makeover",
    "emergency dentist",
    "emergency dental",
    "same day dentist",
    "dentist open now",
    "after hours dentist",
    "toothache",
    "tooth pain",
    "gum recession",
    "gum treatment",
    "periodontist",
    # City-without-brand patterns (shopping intent, not brand recall)
    "dentist grafton ma",
    "grafton ma dentist",
    "dentist grafton",
    "grafton dentist",
    "dentist worcester",
    "worcester dentist",
    "dentist shrewsbury",
    "shrewsbury dentist",
    "dentist westborough",
    "westborough dentist",
    "dentist northborough",
    "northborough dentist",
    "dentist millbury",
    "millbury dentist",
    "dentist auburn",
    "auburn dentist",
    "dentist milford",
    "milford dentist",
    "dentist framingham",
    "framingham dentist",
    "dentist marlborough",
    "marlborough dentist",
    "dentist hudson",
    "hudson dentist",
]

# ── Tokens used to detect brand campaigns from campaign name ──────────────────
# Must stay in sync with _CAMPAIGN_TYPE_TOKENS["brand"] in ai_optimizer.py.
_BRAND_CAMPAIGN_TOKENS: list[str] = ["grafton dental", "brand", "branded", "awareness"]


def get_own_brand_negatives() -> list[str]:
    """
    Return the full list of GDC own-brand stems that should be PHRASE negatives
    on every acquisition campaign.
    """
    return list(_OWN_BRAND_STEMS)


def get_generic_dental_negatives() -> list[str]:
    """
    Return the full list of generic dental terms that should be PHRASE negatives
    on every brand campaign.
    """
    return list(_GENERIC_DENTAL_STEMS)


def is_brand_campaign(campaign_name: str) -> bool:
    """Return True if the campaign name indicates a brand/awareness campaign."""
    name = (campaign_name or "").lower()
    return any(tok in name for tok in _BRAND_CAMPAIGN_TOKENS)


def is_acquisition_campaign(campaign_name: str) -> bool:
    """
    Return True if the campaign is an acquisition campaign (not brand/awareness).
    Acquisition campaigns should never spend budget on branded searches.
    """
    return not is_brand_campaign(campaign_name)
