"""
call_intelligence.py — Gemini Call Intelligence (PR 1, May 2026).

One Vertex AI call → 7 signals: follow-up flag/reason, sentiment+score,
outcome enum, keywords. Scoped to GAds-attributed mango_calls only.

Importable standalone (no FastAPI dependency). Vertex is delegated to
mango_pipeline._call_vertex which already handles vertexai.init + creds.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

CLASSIFIER_VERSION = "v1"

# Outcome enum — keep in lockstep with the prompt below
_VALID_OUTCOMES = {
    "appointment_booked",
    "inquiry_no_booking",
    "existing_patient_admin",
    "wrong_number_spam",
    "voicemail",
    "short_hangup",
    "other",
}
_VALID_SENTIMENTS = {"positive", "neutral", "negative"}

# Minimum transcript length (chars) before we even try; below this we
# fall back to call_summary, and if that's also empty we skip.
_MIN_TRANSCRIPT_CHARS = 40


SYSTEM_PROMPT = """You are a dental practice call analyst. You will receive the transcript (or short summary) of one inbound or outbound phone call at a dental office. Classify the call and return a single JSON object with EXACTLY these 8 keys — no extra keys, no commentary, no markdown:

{
  "follow_up_needed": <bool>,
  "follow_up_reason": "<one short sentence, under 20 words, no PHI, no patient last names>",
  "sentiment": "positive" | "neutral" | "negative",
  "sentiment_score": <float 0.0 to 1.0, where 1.0 = most positive, 0.5 = neutral, 0.0 = most negative>,
  "outcome": "appointment_booked" | "inquiry_no_booking" | "existing_patient_admin" | "wrong_number_spam" | "voicemail" | "short_hangup" | "other",
  "keywords": ["<keyword>", ...]
}

DEFINITIONS:

follow_up_needed = TRUE when the call shows patient interest or need that staff should act on:
  - new patient inquiring about a service, price, or insurance
  - existing patient asking for a callback or unresolved question
  - missed appointment / no-show / reschedule needed
  - treatment question that was not fully answered on the call
  - complaint, frustration, or escalation signal
  - caller asked staff to "call me back" or "send me info"
  - voicemail from a real patient (not spam) with a callback intent

follow_up_needed = FALSE when:
  - spam, robocall, telemarketer, wrong number
  - existing patient administrative call fully resolved on the line (e.g. confirmed an appointment, paid a bill, gave forwarding address)
  - appointment was booked successfully on this call AND no other open thread
  - short hangup with no content

follow_up_reason: ONE short sentence the front-desk team can act on. Examples:
  - "Caller asked about implant pricing; needs cost quote callback."
  - "Existing patient reported tooth pain; not booked yet."
  - "New-patient inquiry left voicemail about Saturday hours."
  Use generic phrasing (no patient last names, no DOBs, no full addresses).

sentiment: overall caller tone.
sentiment_score: float in [0.0, 1.0]. Anchor points: 0.0 = hostile/angry, 0.25 = frustrated, 0.5 = neutral/transactional, 0.75 = friendly, 1.0 = enthusiastic/grateful.

outcome — pick exactly one:
  - appointment_booked: a specific appointment date/time was confirmed on this call
  - inquiry_no_booking: caller asked about services/price/insurance but did not book
  - existing_patient_admin: existing patient calling about billing, records, confirmation, etc.
  - wrong_number_spam: spam, robocall, wrong number, or sales pitch to the practice
  - voicemail: the call went to voicemail (caller left a message OR hung up after greeting)
  - short_hangup: under ~15 seconds of substantive content, no clear intent captured
  - other: anything else (provider referrals, internal calls, unclear)

keywords: up to 5 treatment or service terms the caller mentioned. ALLOWED examples: "implants", "dentures", "cleaning", "crown", "root canal", "veneers", "invisalign", "whitening", "emergency", "extraction", "insurance", "financing", "Delta Dental", "pediatric", "Saturday hours". DISALLOWED: generic words like "appointment", "call", "office", "doctor", "phone", "hello", "thanks". Return [] if no relevant treatment keywords appear. Lowercase, deduped, max 5.

Return ONLY the JSON object. No prose, no code fences."""


def _build_prompt(call: dict) -> str | None:
    """Compose the full prompt. Returns None if no usable text is available."""
    transcript = (call.get("call_transcript") or "").strip()
    summary = (call.get("call_summary") or "").strip()
    direction = (call.get("direction") or "inbound").strip() or "inbound"
    caller = (call.get("caller_id_name") or "").strip()

    if len(transcript) >= _MIN_TRANSCRIPT_CHARS:
        body = transcript
        body_kind = "TRANSCRIPT"
    elif summary:
        body = summary
        body_kind = "SUMMARY (no transcript available)"
    else:
        return None

    header = f"Call direction: {direction}\n"
    if caller:
        header += f"Caller display name: {caller}\n"

    return (
        SYSTEM_PROMPT
        + "\n\n--- CALL " + body_kind + " ---\n"
        + header
        + "\n"
        + body
    )


def _safe_defaults(reason: str = "classifier_skipped") -> dict:
    """Return a safe-default payload that still satisfies the DB schema."""
    return {
        "follow_up_needed":   False,
        "follow_up_reason":   "",
        "classified_at":      datetime.now(timezone.utc).isoformat(),
        "classifier_version": CLASSIFIER_VERSION,
        "sentiment":          "neutral",
        "sentiment_score":    0.5,
        "outcome":            "other",
        "keywords":           [],
        "_skip_reason":       reason,   # internal; not persisted
    }


def _coerce_output(raw: dict) -> dict:
    """Normalise a raw Gemini JSON dict into the canonical 7-key shape."""
    out = _safe_defaults("ok")
    out.pop("_skip_reason", None)

    out["follow_up_needed"] = bool(raw.get("follow_up_needed", False))
    out["follow_up_reason"] = str(raw.get("follow_up_reason") or "")[:500]

    sentiment = str(raw.get("sentiment") or "neutral").lower().strip()
    if sentiment not in _VALID_SENTIMENTS:
        sentiment = "neutral"
    out["sentiment"] = sentiment

    try:
        score = float(raw.get("sentiment_score"))
        score = max(0.0, min(1.0, score))
    except (TypeError, ValueError):
        score = 0.5
    out["sentiment_score"] = score

    outcome = str(raw.get("outcome") or "other").lower().strip()
    if outcome not in _VALID_OUTCOMES:
        outcome = "other"
    out["outcome"] = outcome

    kws_raw = raw.get("keywords") or []
    if not isinstance(kws_raw, list):
        kws_raw = []
    seen: set = set()
    kws_clean = []
    for k in kws_raw:
        k = str(k or "").strip().lower()
        if not k or k in seen:
            continue
        seen.add(k)
        kws_clean.append(k[:60])
        if len(kws_clean) >= 5:
            break
    out["keywords"] = kws_clean
    return out


def classify_call(call: dict, settings) -> dict:
    """
    Classify one call. `call` must include uuid + call_transcript/call_summary.
    `settings` is a config.Settings instance (for Vertex project/location/creds).

    Returns a dict with all 7 signal keys ready for save_call_intelligence().
    On any error returns safe defaults (never raises).
    """
    from mango_pipeline import _call_vertex
    from ai_costs import log_gemini

    prompt = _build_prompt(call)
    if prompt is None:
        return _safe_defaults("no_transcript")

    try:
        text, in_tok, out_tok = _call_vertex(
            prompt,
            model=settings.vertex_model,
            project_id=settings.vertex_project_id,
            location=settings.vertex_location,
            credentials_path=settings.vertex_credentials_path,
            temperature=0.15,
            max_tokens=512,
            response_mime_type="application/json",
        )
    except Exception as exc:
        logger.error("[call_intel] vertex call failed for %s: %s", call.get("uuid"), exc)
        return _safe_defaults("vertex_error")

    # Cost logging — always, even on parse fail
    try:
        log_gemini(
            purpose="call_intelligence",
            model=settings.vertex_model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            call_id=call.get("uuid", ""),
        )
    except Exception as exc:
        logger.warning("[call_intel] log_gemini failed: %s", exc)

    if not text:
        return _safe_defaults("empty_response")

    try:
        raw = json.loads(text)
    except Exception as exc:
        logger.warning("[call_intel] JSON parse failed for %s: %s | text=%r",
                       call.get("uuid"), exc, text[:300])
        return _safe_defaults("parse_error")

    result = _coerce_output(raw)
    result["classified_at"] = datetime.now(timezone.utc).isoformat()
    result["classifier_version"] = CLASSIFIER_VERSION
    return result


def run_call_intelligence(limit: int = 100) -> dict:
    """
    Batch orchestrator — pulls up to `limit` GAds-attributed unclassified
    calls, classifies each, and writes results to DB one-by-one.

    Safe to run repeatedly. Returns counters:
      {"processed": N, "errors": M, "skipped": K}

    - processed: classification ran and was saved (includes safe-default rows
                 where the model returned empty/garbage — those still get
                 classified_at stamped so we don't retry forever).
    - errors:    save_call_intelligence() raised; row left as-is.
    - skipped:   call had no usable transcript or summary; classified_at is
                 still stamped with a safe-default row so we don't reprocess.
    """
    from config import get_settings
    from database import get_calls_for_intelligence, save_call_intelligence

    settings = get_settings()
    calls = get_calls_for_intelligence(limit=limit)
    processed = 0
    errors = 0
    skipped = 0

    for call in calls:
        try:
            result = classify_call(call, settings)
            skip_reason = result.pop("_skip_reason", None)
            save_call_intelligence(call["uuid"], result)
            if skip_reason in ("no_transcript",):
                skipped += 1
            else:
                processed += 1
        except Exception as exc:
            errors += 1
            logger.error("[call_intel] failed for uuid=%s: %s", call.get("uuid"), exc)

    logger.info(
        "[call_intel] run complete — processed=%d errors=%d skipped=%d (limit=%d)",
        processed, errors, skipped, limit,
    )
    return {"processed": processed, "errors": errors, "skipped": skipped}
