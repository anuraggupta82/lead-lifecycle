"""
competitor_policy.py — Competitor intent classification and negate policy for dental PPC.

Shared by:
  - main.py  (campaign wizard competitor_analysis build step + build-step-refine)
  - ai_optimizer.py (conquest guard, per-campaign advisory prompt context)
  - database.py CRUD helpers (upsert_competitor_candidate)

Key concepts:
  - local_office: a specific physical dental office in the practice's service area.
    Branded searches = contact-lookup intent. Always negate.
  - national_chain: a destination brand (ClearChoice, Nuvia, etc.) whose patients
    comparison-shop across providers. Conquest-eligible ONLY for implants/invisalign/cosmetic.
  - negate_override: per-competitor user override stored in campaign_build_json.
    null  → use auto-policy
    false → user forced allow (ignore negate)
    true  → user forced negate (rare, for conquest targets they want to negate anyway)
"""

from __future__ import annotations
import re
from typing import Any

# ── Campaign types where national chains are conquest-eligible ────────────────
CONQUEST_ELIGIBLE_TYPES: frozenset[str] = frozenset({"implants", "invisalign", "cosmetic"})

# ── Known national / destination dental chains ───────────────────────────────
# Each entry: name (display), stems (lowercase tokens for matching + negation)
NATIONAL_CHAINS: list[dict] = [
    {"name": "Aspen Dental",                    "stems": ["aspen dental", "aspendental"]},
    {"name": "Gentle Dental",                   "stems": ["gentle dental"]},
    {"name": "Heartland Dental",                "stems": ["heartland dental"]},
    {"name": "Western Dental",                  "stems": ["western dental"]},
    {"name": "ClearChoice",                     "stems": ["clearchoice", "clear choice dental", "clear choice"]},
    {"name": "Nuvia Dental Implant Center",     "stems": ["nuvia dental", "nuvia implant", "nuvia"]},
    {"name": "Affordable Dentures & Implants",  "stems": ["affordable dentures", "affordable dentures and implants"]},
    {"name": "Smile Direct Club",               "stems": ["smile direct", "smiledirectclub", "smile direct club"]},
    {"name": "Byte",                            "stems": ["byte aligners", "byte clear"]},
    {"name": "Candid Co",                       "stems": ["candid aligners", "candid co"]},
    {"name": "Dental Dreams",                   "stems": ["dental dreams"]},
    {"name": "Bright Now! Dental",              "stems": ["bright now dental", "bright now"]},
    {"name": "Comfort Dental",                  "stems": ["comfort dental"]},
    {"name": "Sage Dental",                     "stems": ["sage dental"]},
    {"name": "Pacific Dental Services",         "stems": ["pacific dental"]},
    {"name": "Coast Dental",                    "stems": ["coast dental"]},
    {"name": "Tend Dental",                     "stems": ["tend dental"]},
    {"name": "Tend",                            "stems": ["tend dental studio"]},
    {"name": "Small Smiles Dental",             "stems": ["small smiles"]},
    {"name": "DentalWorks",                     "stems": ["dentalworks", "dental works"]},
]

# Build a fast lookup set from all stems for O(1) classification
_NATIONAL_STEMS: set[str] = {s for chain in NATIONAL_CHAINS for s in chain["stems"]}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalize(name: str) -> str:
    """Lowercase, strip punctuation/extra spaces. Used for deduplication and matching."""
    if not name:
        return ""
    n = re.sub(r"[^a-z0-9 ]+", " ", name.lower()).strip()
    return re.sub(r" {2,}", " ", n)


def normalize(name: str) -> str:
    """Public alias for _normalize (used by database helpers and optimizer)."""
    n = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower()).strip()
    return re.sub(r" {2,}", " ", n)


def _brand_stems(name: str, extra_stems: list[str] | None = None) -> list[str]:
    """
    Derive brand stems from a practice name.
    Returns a deduplicated list of lowercase tokens suitable for use as
    Google Ads negative keyword phrases.

    e.g. "Smith Family Dental of Grafton, LLC" → ["smith family dental", "smith family"]
    """
    n = normalize(name)
    if not n:
        return list(extra_stems or [])

    # Strip trailing location / legal suffixes
    n_clean = re.sub(
        r"\b(of [a-z]+(?: [a-z]+)? *$|llc\.?$|p\.?c\.?$|d\.?d\.?s\.?$|inc\.?$|pllc\.?$)",
        "", n,
    ).strip()
    n_clean = re.sub(r" {2,}", " ", n_clean).strip()

    stems: list[str] = []
    if n_clean:
        stems.append(n_clean)
        # If 3+ word name, also add a 2-word stem
        parts = n_clean.split()
        if len(parts) >= 3:
            two_word = " ".join(parts[:2])
            if two_word not in stems:
                stems.append(two_word)
        # Add no-space version for single-brand names (e.g. "aspendental")
        no_space = n_clean.replace(" ", "")
        if len(no_space) >= 6 and no_space not in stems:
            stems.append(no_space)

    for s in (extra_stems or []):
        s_norm = normalize(s)
        if s_norm and s_norm not in stems:
            stems.append(s_norm)

    # Remove duplicates, preserve order
    seen: set[str] = set()
    deduped = []
    for s in stems:
        if s not in seen:
            seen.add(s)
            deduped.append(s)
    return deduped


def classify(name: str) -> str:
    """
    Classify a competitor as 'local_office' or 'national_chain'.
    Matches against NATIONAL_STEMS first; falls back to local_office.
    """
    n = normalize(name)
    for stem in _NATIONAL_STEMS:
        # Word-boundary aware: "gentle dental" matches "gentle dental worcester"
        # but not "smith-gentle-dental" (edge case)
        if re.search(r"\b" + re.escape(stem) + r"\b", n):
            return "national_chain"
    return "local_office"


# ── Core policy function ──────────────────────────────────────────────────────

def apply_competitor_policy(
    comp_analysis: dict[str, Any],
    campaign_type: str = "general",
) -> dict[str, Any]:
    """
    Idempotent. Enriches each competitor in comp_analysis["competitors"] with:
      - classification  (local_office | national_chain)
      - brand_stems     (list of lowercase tokens)
      - negate          (bool — auto-policy decision)
      - negate_reason   (human-readable rationale)
      - negate_override (preserved if already set; never overwritten)

    Rebuilds derived top-level arrays:
      - competitor_negatives  (brand_stems of all effectively-negated competitors)
      - conquest_keywords     (brand_stems of all conquest-eligible competitors)

    NEVER overwrites negate_override if already present.

    Args:
        comp_analysis: the competitor_analysis dict from campaign_build_json
        campaign_type: campaign type string (emergency/implants/invisalign/cosmetic/gum/general)

    Returns:
        The mutated comp_analysis dict (also mutated in-place for caller convenience).
    """
    if not isinstance(comp_analysis, dict):
        return comp_analysis

    ctype = (campaign_type or "general").strip().lower()
    comps: list[dict] = comp_analysis.get("competitors") or []

    # Build a set of existing conquest keyword stems (from Claude's explicit list)
    # so we can detect when a national chain is intentionally targeted
    existing_conquest = {
        normalize(k) for k in (comp_analysis.get("conquest_keywords") or []) if k
    }

    for c in comps:
        name = (c.get("name") or "").strip()

        # ── 1. Classification ─────────────────────────────────────────────────
        if not c.get("classification"):
            c["classification"] = classify(name)

        cls = c["classification"]

        # ── 2. Brand stems ────────────────────────────────────────────────────
        if not c.get("brand_stems"):
            if cls == "national_chain":
                # Look up the canonical stems from the NATIONAL_CHAINS list
                n = normalize(name)
                chain_stems: list[str] | None = None
                for chain in NATIONAL_CHAINS:
                    if any(re.search(r"\b" + re.escape(s) + r"\b", n) for s in chain["stems"]):
                        chain_stems = chain["stems"]
                        break
                # M6 fix: copy the list to avoid mutating NATIONAL_CHAINS reference
                c["brand_stems"] = list(chain_stems) if chain_stems else _brand_stems(name)
            else:
                c["brand_stems"] = _brand_stems(name)

        stems: list[str] = c["brand_stems"]

        # ── 3. Auto-policy ────────────────────────────────────────────────────
        is_conquest_eligible = (
            cls == "national_chain"
            and ctype in CONQUEST_ELIGIBLE_TYPES
        )
        # Check if any stem appears in Claude's explicit conquest list
        in_conquest_list = any(normalize(s) in existing_conquest for s in stems)

        if cls == "local_office":
            c["negate"] = True
            c["negate_reason"] = "Local office — contact-lookup intent, won't convert"
        elif is_conquest_eligible or in_conquest_list:
            c["negate"] = False
            c["negate_reason"] = (
                f"National chain — comparison-shopping intent, conquest eligible for {ctype}"
            )
        else:
            c["negate"] = True
            c["negate_reason"] = (
                f"National chain — not conquest-eligible for {ctype} campaigns"
            )

        # ── 4. Preserve negate_override (NEVER overwrite) ────────────────────
        if "negate_override" not in c:
            c["negate_override"] = None

    # ── 5. Rebuild derived arrays ─────────────────────────────────────────────
    negatives: list[str] = []
    conquest: list[str] = []

    for c in comps:
        ovr = c.get("negate_override")
        # Effective negate: override wins if set, else use auto-policy
        effective_negate = c.get("negate", True) if ovr is None else bool(ovr)

        stems = c.get("brand_stems") or []
        if effective_negate:
            negatives.extend(stems)
        else:
            # M8 fix: local_office competitors must NEVER go into conquest_keywords.
            # negate_override=False on a local office means "don't negate" (e.g. a
            # related practice), but they are still not conquest targets — they just
            # won't have negatives added. Only national_chain entries are conquest targets.
            if c.get("classification") == "national_chain":
                conquest.extend(stems)

    comp_analysis["competitor_negatives"] = sorted(set(negatives))
    comp_analysis["conquest_keywords"] = sorted(set(conquest))

    return comp_analysis


def merge_overrides_on_regenerate(
    new_comps: list[dict],
    old_comps: list[dict],
) -> list[dict]:
    """
    After Claude regenerates the competitor list, copy negate_override values
    from matching old competitors onto the new list (by normalized name).
    This preserves user decisions across regeneration.

    Args:
        new_comps: competitors[] from the freshly-generated competitor_analysis
        old_comps: competitors[] from the previously-saved competitor_analysis

    Returns:
        new_comps with negate_override values merged in (mutates in place too).
    """
    old_overrides: dict[str, Any] = {
        normalize(c.get("name", "")): c.get("negate_override")
        for c in (old_comps or [])
        if c.get("negate_override") is not None
    }
    for c in (new_comps or []):
        key = normalize(c.get("name", ""))
        if key in old_overrides:
            c["negate_override"] = old_overrides[key]
    return new_comps


def get_effective_negatives(comp_analysis: dict) -> list[str]:
    """
    Return the effective list of brand stems to negate, respecting overrides.
    Convenience wrapper — returns comp_analysis["competitor_negatives"] if present,
    otherwise recomputes from competitors[].
    """
    if "competitor_negatives" in comp_analysis:
        return comp_analysis["competitor_negatives"]
    # Fallback: compute on the fly
    negatives: list[str] = []
    for c in (comp_analysis.get("competitors") or []):
        ovr = c.get("negate_override")
        effective = c.get("negate", True) if ovr is None else bool(ovr)
        if effective:
            negatives.extend(c.get("brand_stems") or [])
    return sorted(set(negatives))


def get_effective_conquest(comp_analysis: dict) -> list[str]:
    """
    Return the effective list of brand stems to use as conquest targets.
    Convenience wrapper.
    """
    if "conquest_keywords" in comp_analysis:
        return comp_analysis["conquest_keywords"]
    conquest: list[str] = []
    for c in (comp_analysis.get("competitors") or []):
        ovr = c.get("negate_override")
        effective_negate = c.get("negate", True) if ovr is None else bool(ovr)
        if not effective_negate:
            conquest.extend(c.get("brand_stems") or [])
    return sorted(set(conquest))
