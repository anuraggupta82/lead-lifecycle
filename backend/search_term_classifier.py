"""
Search Term Semantic Classifier
================================
Uses Claude Haiku to classify search terms by campaign context — answering
"would someone searching this term realistically book with Grafton Dental Care?"

Verdicts
--------
  keep      — relevant to the campaign service; keep showing our ad
  negative  — wrong practice, wrong service, or zero booking intent; should be negated
  conquest  — competitor brand (e.g. ClearChoice, Aspen Dental) but intentional
              targeting is valid for high-value campaigns (implants, Invisalign)

Design
------
- Classifies in batches of up to 50 terms per Haiku call (cheap + fast)
- Results are persisted to st_classifications — each term is classified only once
- The optimizer calls classify_new_terms_for_campaign() once per campaign before
  the main Opus pass; results feed into staged add_negative_keyword actions
- A manual /api/admin/classify-search-terms endpoint lets you trigger on demand

Campaign type detection
-----------------------
Uses the same _classify_campaign() logic as ai_optimizer.py (imported at call time).
Types: emergency | implants | invisalign | cosmetic | general
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

# ── Practice identity (used in every prompt) ─────────────────────────────────
_PRACTICE_NAME = "Grafton Dental Care"
_PRACTICE_LOCATION = "Grafton, MA (Worcester County)"
_PRACTICE_TAGLINE = (
    "A full-service dental practice offering general dentistry, emergency care, "
    "implants, periodontal treatment (gum recession), Invisalign, cosmetic dentistry, "
    "and dentures. Dr. Anurag Gupta."
)

# ── Campaign type → service description (what the campaign is advertising) ───
_CAMPAIGN_SERVICE_DESC = {
    "emergency":  "Emergency dental care — same-day appointments for toothache, broken teeth, dental pain",
    "implants":   "Dental implants and All-on-X full-arch restorations — high-value elective procedure",
    "invisalign": "Invisalign clear aligner orthodontics — elective cosmetic/alignment treatment",
    "cosmetic":   "Cosmetic dentistry — veneers, whitening, smile makeovers",
    "general":    "General dentistry — cleanings, fillings, crowns, routine dental care",
    "gum":        "Periodontal treatment — gum recession, scaling & root planing, gum grafting",
}

# ── Conquest brands: showing our ad for these is intentional for certain types ─
# Key = campaign type, value = list of brand substrings that are valid conquest
_CONQUEST_BRANDS: dict[str, list[str]] = {
    "implants":   ["clearchoice", "clear choice", "aspen dental", "affordable dentures",
                   "dentalimplantsolutions", "smile again", "teeth today"],
    "invisalign": ["smile direct", "byte", "candid", "smilelove", "orthly"],
    "cosmetic":   ["smile direct", "byte"],
    "emergency":  [],  # no conquest makes sense for emergency — if someone needs emergency care elsewhere, let them go
    "general":    [],
    "gum":        [],
}

# ── Batch size — how many terms per Haiku call ────────────────────────────────
BATCH_SIZE = 50


def _detect_campaign_type(campaign_name: str) -> str:
    """Classify campaign name into a service type. Mirrors ai_optimizer._classify_campaign."""
    try:
        from ai_optimizer import _classify_campaign
        return _classify_campaign(campaign_name)
    except Exception:
        n = campaign_name.lower()
        if any(w in n for w in ["emergency", "urgent", "pain", "toothache", "same day", "same-day"]):
            return "emergency"
        if any(w in n for w in ["implant", "all-on", "allon", "all on", "denture"]):
            return "implants"
        if any(w in n for w in ["invisalign", "aligner", "ortho", "braces"]):
            return "invisalign"
        if any(w in n for w in ["cosmetic", "veneer", "whitening", "smile makeover"]):
            return "cosmetic"
        if any(w in n for w in ["gum", "recession", "periodon", "perio"]):
            return "gum"
        return "general"


def _is_conquest(term: str, campaign_type: str) -> bool:
    """Return True if this term is a known conquest brand for this campaign type."""
    t = term.lower()
    for brand in _CONQUEST_BRANDS.get(campaign_type, []):
        if brand in t:
            return True
    return False


def _build_prompt(campaign_name: str, campaign_type: str, terms: list[dict]) -> str:
    service_desc = _CAMPAIGN_SERVICE_DESC.get(campaign_type, _CAMPAIGN_SERVICE_DESC["general"])
    conquest_brands = _CONQUEST_BRANDS.get(campaign_type, [])
    conquest_note = ""
    if conquest_brands:
        conquest_note = (
            f"\nCONQUEST BRANDS — these are real competitors in the {campaign_type} space. "
            f"Showing our ad when someone searches for them is INTENTIONAL and valid. "
            f"Mark these as 'conquest' not 'negative': {', '.join(conquest_brands)}"
        )

    terms_json = json.dumps(
        [{"id": i, "term": t["search_term"], "impressions": t.get("impressions", 0),
          "clicks": t.get("clicks", 0), "cost": round(float(t.get("cost", 0)), 2)}
         for i, t in enumerate(terms)],
        ensure_ascii=False
    )

    return f"""You are classifying Google Ads search terms for a dental practice.

PRACTICE: {_PRACTICE_NAME}, {_PRACTICE_LOCATION}
SERVICES: {_PRACTICE_TAGLINE}

CAMPAIGN: "{campaign_name}"
THIS CAMPAIGN ADVERTISES: {service_desc}
{conquest_note}

TASK: For each search term below, decide if showing our ad is appropriate.
Ask yourself: "Would someone searching this term realistically book an appointment at {_PRACTICE_NAME}?"

VERDICT OPTIONS:
- "keep"     — relevant to our service; the searcher could become our patient
- "negative" — wrong practice (searching for a different dental office), wrong service,
               purely informational with no booking intent, or clearly irrelevant
- "conquest" — a competitor brand search but intentional targeting (see conquest brands above)

KEY RULES:
1. Any search that names a SPECIFIC OTHER dental practice (e.g. "auburn family dental",
   "attleboro falls dentistry", "atwill dental", "[any name] dental/dentistry/orthodontics")
   = "negative" — that person is looking for a DIFFERENT practice, not us.
2. Generic service searches ("gum recession treatment", "dental implants near me") = "keep"
3. Searches about our own practice or doctor = "keep"
4. Research/informational only ("what causes gum recession", "dental implant cost comparison") = "negative"
   UNLESS the campaign is general/emergency where broad awareness has value.
5. Conquest brands (listed above) = "conquest" — keep them unless campaign type has no conquest list.
6. Geographic: searches for "[town] dental" where the town is NOT Grafton = "negative"
   UNLESS we explicitly target that town (we target: Grafton, Shrewsbury, Westborough,
   Northborough, Millbury, Auburn, Worcester area).

SEARCH TERMS TO CLASSIFY:
{terms_json}

Return ONLY a JSON array, one object per term, with exactly these fields:
{{"id": <same id from input>, "verdict": "keep"|"negative"|"conquest", "reason": "<one short sentence>"}}

No markdown, no explanation outside the array."""


def classify_batch(
    campaign_name: str,
    campaign_type: str,
    terms: list[dict],
    api_key: str,
) -> list[dict]:
    """
    Send one batch of terms to Haiku. Returns list of
    {search_term, campaign_name, verdict, reason, classifier}.
    """
    if not terms:
        return []

    from anthropic import Anthropic
    client = Anthropic(api_key=api_key)

    prompt = _build_prompt(campaign_name, campaign_type, terms)
    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text if msg.content else "[]"

        # Log cost
        try:
            from ai_costs import log_claude
            log_claude(
                purpose="search_term_classification",
                model="claude-haiku-4-5-20251001",
                input_tokens=msg.usage.input_tokens,
                output_tokens=msg.usage.output_tokens,
                campaign_id=campaign_name,
            )
        except Exception:
            pass

    except Exception as e:
        logger.error(f"[st_classifier] Haiku call failed for '{campaign_name}': {e}")
        return []

    # Parse response
    m = re.search(r"\[[\s\S]*\]", text)
    if not m:
        logger.warning(f"[st_classifier] No JSON array in Haiku response for '{campaign_name}'")
        return []

    try:
        results = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        logger.warning(f"[st_classifier] JSON parse error: {e}")
        return []

    # Map id → original term
    id_to_term = {i: t["search_term"] for i, t in enumerate(terms)}

    classified = []
    for r in results:
        if not isinstance(r, dict):
            continue
        term_id = r.get("id")
        verdict = r.get("verdict", "keep").lower().strip()
        reason = r.get("reason", "")
        if verdict not in ("keep", "negative", "conquest"):
            verdict = "keep"  # safe default
        search_term = id_to_term.get(term_id)
        if not search_term:
            continue
        classified.append({
            "search_term": search_term,
            "campaign_name": campaign_name,
            "verdict": verdict,
            "reason": reason,
            "classifier": "haiku",
        })

    logger.info(
        f"[st_classifier] '{campaign_name}': {len(classified)}/{len(terms)} classified — "
        f"neg={sum(1 for c in classified if c['verdict']=='negative')}, "
        f"keep={sum(1 for c in classified if c['verdict']=='keep')}, "
        f"conquest={sum(1 for c in classified if c['verdict']=='conquest')}"
    )
    return classified


def classify_new_terms_for_campaign(
    campaign_name: str,
    days: int = 30,
    api_key: Optional[str] = None,
    force_reclassify: bool = False,
) -> dict:
    """
    Main entry point. Classifies all unclassified search terms for a campaign.

    Returns:
        {
            "classified": int,       — total terms classified this run
            "negatives": list[dict], — terms with verdict='negative' (for staging)
            "conquests": list[dict], — terms with verdict='conquest' (informational)
            "skipped": int,          — terms already classified (skipped)
        }
    """
    from database import (
        get_unclassified_search_terms, save_st_classifications_bulk,
        get_setting, get_st_classifications,
    )

    _api_key = api_key or get_setting("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    if not _api_key:
        logger.warning("[st_classifier] No Anthropic API key — skipping classification")
        return {"classified": 0, "negatives": [], "conquests": [], "skipped": 0}

    campaign_type = _detect_campaign_type(campaign_name)

    if force_reclassify:
        # Pull ALL terms for this campaign
        from database import get_search_term_stats
        all_terms = get_search_term_stats(campaign_name=campaign_name, days=days)
        unclassified = [
            {"search_term": t["search_term"], "campaign_name": campaign_name,
             "impressions": t.get("impressions", 0), "clicks": t.get("clicks", 0),
             "cost": t.get("cost", 0)}
            for t in all_terms
        ]
    else:
        unclassified = get_unclassified_search_terms(campaign_name=campaign_name, days=days)

    # Pre-filter conquest terms locally (fast path — no Haiku call needed)
    pre_classified = []
    to_classify = []
    for t in unclassified:
        if _is_conquest(t["search_term"], campaign_type):
            pre_classified.append({
                "search_term": t["search_term"],
                "campaign_name": campaign_name,
                "verdict": "conquest",
                "reason": f"Known conquest brand for {campaign_type} campaigns",
                "classifier": "rule",
            })
        else:
            to_classify.append(t)

    logger.info(
        f"[st_classifier] '{campaign_name}' ({campaign_type}): "
        f"{len(unclassified)} unclassified — "
        f"{len(pre_classified)} conquest (rule), {len(to_classify)} → Haiku"
    )

    # Send to Haiku in batches
    all_classified = list(pre_classified)
    for i in range(0, len(to_classify), BATCH_SIZE):
        batch = to_classify[i:i + BATCH_SIZE]
        results = classify_batch(campaign_name, campaign_type, batch, _api_key)
        all_classified.extend(results)

    # Persist to DB
    saved = save_st_classifications_bulk(all_classified)
    logger.info(f"[st_classifier] '{campaign_name}': saved {saved} classifications to DB")

    negatives = [c for c in all_classified if c["verdict"] == "negative"]
    conquests = [c for c in all_classified if c["verdict"] == "conquest"]

    return {
        "classified": len(all_classified),
        "negatives": negatives,
        "conquests": conquests,
        "skipped": len(unclassified) - len(all_classified),
    }


def get_pending_negative_classifications(campaign_name: str = "") -> list[dict]:
    """
    Return classified-negative terms that haven't yet been staged as
    add_negative_keyword actions. Used by the optimizer to avoid re-staging.
    """
    from database import get_st_classifications
    classified = get_st_classifications(campaign_name=campaign_name)
    return [c for c in classified if c["verdict"] == "negative"]
