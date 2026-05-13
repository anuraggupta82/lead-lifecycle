"""
mango_pipeline.py — Call analysis pipeline orchestrator.

Runs on a scheduler tick (every N minutes, configurable via settings).
For each unprocessed inbound call:
  1. Download recording from Mango
  2. Transcribe with OpenAI Whisper (cloud) or local Whisper model
  3. PHI-scrub + strip greeting
  4. Summarize with Gemini via Vertex AI (HIPAA-compliant)
  5. Grade with Gemini via Vertex AI against configured criteria
  6. Resolve team member from transcript / extension
  7. Persist everything to mango_calls via database.update_mango_call_analysis
  8. Log AI costs to ai_usage via ai_costs helpers
  9. Clean up temp recording file

AI routing:
  - Transcription : OpenAI Whisper API (openai_api_key, covered by BAA)
  - Summary/Grade : Gemini via Google Vertex AI SDK (HIPAA-compliant under GCP BAA)
                    Configured via vertex_project_id / vertex_location /
                    vertex_credentials_path / vertex_model in settings.
                    Direct Gemini REST API (gemini_api_key) is NOT used.

The pipeline is gated by settings.mango_pipeline_enabled — when False, the
scheduler job exits immediately so no API calls are made.

Claude cost instrumentation:
  Any existing module that calls anthropic.Anthropic().messages.create() should
  instead call claude_complete() from this module. That wrapper is a drop-in
  replacement that logs token usage to ai_usage automatically.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

import database as db
from ai_costs import log_whisper, log_gemini, log_claude
from config import get_settings
from mango_service import fetch_fresh_recording_url

log = logging.getLogger(__name__)

# ── PHI scrubbing (ported verbatim from standalone core.py) ──────────────────

_PHI_PATTERNS = [
    (re.compile(r'\(?\b\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}\b'), '[PHONE]'),
    (re.compile(r'\b\d{10}\b'), '[PHONE]'),
    (re.compile(r'\b\d{3}-\d{2}-\d{4}\b'), '[SSN]'),
    (re.compile(r'\b(0?[1-9]|1[0-2])[/\-](0?[1-9]|[12]\d|3[01])[/\-](\d{4}|\d{2})\b'), '[DATE]'),
    (re.compile(r'\b[\w.+\-]+@[\w\-]+\.\w+\b'), '[EMAIL]'),
    (re.compile(r'\b[A-Z]{2,4}\d{6,12}\b'), '[ID]'),
    (re.compile(
        r'\b(?:date of birth|dob|born on|birthday)[:\s]+[A-Za-z0-9,\s/\-]+\b',
        re.IGNORECASE,
    ), '[DOB_INFO]'),
]


def _scrub_phi(text: str, caller_name: str = "") -> str:
    """Replace PHI patterns and caller name with redaction tokens."""
    scrubbed = text
    for pattern, replacement in _PHI_PATTERNS:
        scrubbed = pattern.sub(replacement, scrubbed)
    if caller_name and len(caller_name) > 3:
        scrubbed = re.sub(
            re.escape(caller_name), '[PATIENT]', scrubbed, flags=re.IGNORECASE
        )
    return scrubbed


# ── Greeting / voicemail stripping ───────────────────────────────────────────

_LIVE_GREETING_PATTERNS = [
    re.compile(
        r"Thank you for calling Grafton Dental Care\..*?"
        r"Thank you for calling Grafton Dental Care\.",
        re.DOTALL | re.IGNORECASE,
    ),
    re.compile(
        r"^Thank you for calling Grafton Dental Care\..*?"
        r"(?=(?:This is|Hi,?\s|Hello,?\s|Good\s(?:morning|afternoon|evening)))",
        re.DOTALL | re.IGNORECASE,
    ),
]

_VOICEMAIL_GREETING = re.compile(
    r"Hi there\.?\s+You'?ve? reached Grafton Dental Care voicemail\..*?"
    r"look forward to speaking with you soon\.?",
    re.DOTALL | re.IGNORECASE,
)

_MIN_CONTENT_WORDS = 15


def _strip_greeting(text: str) -> tuple[str, bool]:
    """Remove IVR/voicemail greeting. Returns (cleaned_text, is_voicemail)."""
    cleaned = _VOICEMAIL_GREETING.sub("", text, count=1).strip()
    if cleaned and len(cleaned) < len(text):
        return cleaned, True
    for pat in _LIVE_GREETING_PATTERNS:
        cleaned = pat.sub("", text, count=1).strip()
        if cleaned and len(cleaned) < len(text):
            return cleaned, False
    return text, False


def _is_empty_call(transcript: str) -> bool:
    """Return True if transcript has no real content beyond greeting/silence."""
    if not transcript or not transcript.strip():
        return True
    cleaned, _ = _strip_greeting(transcript)
    return len(cleaned.split()) < _MIN_CONTENT_WORDS


# ── Recording download ────────────────────────────────────────────────────────

def _fetch_recording(recording_url: str, call_uuid: str, token: Optional[str] = None) -> Path:
    """Download a Mango recording to the local temp dir. Returns the local path."""
    settings = get_settings()
    rec_dir = Path(settings.mango_recording_dir)
    rec_dir.mkdir(parents=True, exist_ok=True)
    local = rec_dir / f"{call_uuid}.mp3"

    if local.exists():
        return local

    headers: dict[str, str] = {}
    url_lower = recording_url.lower()
    # S3 URLs (amazonaws.com) must NOT have an Authorization header — S3 rejects
    # Bearer tokens with 400. The Mango S3 bucket is accessible without auth.
    # Only add the Bearer token for Mango API URLs (mangovoice.com etc.).
    is_s3 = ".amazonaws.com" in url_lower
    if token and not is_s3:
        headers["Authorization"] = f"Bearer {token}"

    log.info("[pipeline] Fetching recording: %s (auth=%s)", recording_url, "Bearer" if headers.get("Authorization") else "none")
    resp = requests.get(recording_url, headers=headers, timeout=60, stream=True)
    if not resp.ok:
        log.error("[pipeline] Recording fetch failed: HTTP %s — %s", resp.status_code, resp.text[:200])
    resp.raise_for_status()

    content_type = resp.headers.get("Content-Type", "")
    if any(ct in content_type for ct in ("text/html", "application/json", "application/xml", "text/xml")):
        raise ValueError(
            f"Recording download returned {content_type} instead of audio — "
            f"likely an S3 or auth error. Body: {resp.text[:200]}"
        )

    total = 0
    with open(local, "wb") as f:
        for chunk in resp.iter_content(65536):
            f.write(chunk)
            total += len(chunk)

    if total == 0:
        local.unlink(missing_ok=True)
        raise ValueError("Recording download returned empty file")

    return local


def _rebuild_recording_url(call_row: dict, msettings: dict) -> str:
    """Reconstruct the Mango S3 recording URL from the known pattern.

    Mango's call-listing endpoint frequently omits `recording_url` even when a
    recording exists. The standalone mango-call-analysis app builds the URL
    from this pattern:
        https://mango-prd.s3.amazonaws.com/recorded_calls/{account_uuid}/{MMYYYY}/{call_uuid}.mp3

    Returns "" if account_uuid or started_at are unavailable.
    """
    # account_uuid is a separate config (NOT the same as pbx_id). The
    # standalone app stores it as cfg["mango"]["account_uuid"]. We accept it
    # from db settings under "mango_account_uuid" — admin must set it for the
    # S3-pattern fallback to work.
    account_uuid = msettings.get("mango_account_uuid") or ""
    started_at = call_row.get("started_at") or ""
    uuid = call_row.get("uuid") or ""
    if not (account_uuid and started_at and uuid):
        return ""
    try:
        dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        mmyyyy = dt.strftime("%m%Y")
    except Exception:
        return ""
    return f"https://mango-prd.s3.amazonaws.com/recorded_calls/{account_uuid}/{mmyyyy}/{uuid}.mp3"


def _cleanup_recording(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


def cleanup_old_recordings() -> None:
    """Remove cached recordings older than mango_pipeline_recording_ttl_min minutes."""
    settings = get_settings()
    rec_dir = Path(settings.mango_recording_dir)
    if not rec_dir.exists():
        return
    cutoff = time.time() - (settings.mango_pipeline_recording_ttl_min * 60)
    for f in rec_dir.glob("*.mp3"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except Exception:
            pass


# ── Transcription ─────────────────────────────────────────────────────────────

def _transcribe_openai(audio_path: Path, api_key: str) -> str:
    """Transcribe audio via OpenAI Whisper cloud API."""
    url = "https://api.openai.com/v1/audio/transcriptions"
    with open(audio_path, "rb") as f:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (audio_path.name, f, "audio/mpeg")},
            data={
                "model": "whisper-1",
                "language": "en",
                "response_format": "text",
                "prompt": (
                    "This is a phone call for a dental office. "
                    "Callers may mention teeth, appointments, insurance, "
                    "dental procedures, and patient names."
                ),
            },
            timeout=120,
        )
    if resp.status_code == 401:
        raise ValueError("Invalid OpenAI API key.")
    if resp.status_code == 413:
        raise ValueError("Recording too large for Whisper API (max 25MB).")
    resp.raise_for_status()
    return resp.text.strip()


def _transcribe_local(audio_path: Path, model_name: str) -> str:
    """Transcribe audio using a locally loaded Whisper model (GPU if available)."""
    import whisper  # type: ignore
    import torch    # type: ignore

    model = whisper.load_model(model_name)
    fp16 = torch.cuda.is_available()
    try:
        result = model.transcribe(
            str(audio_path),
            language="en",
            fp16=fp16,
            initial_prompt=(
                "This is a phone call for a dental office. "
                "Callers may mention teeth, appointments, insurance, "
                "dental procedures, and patient names."
            ),
        )
        return result["text"].strip()
    except (RuntimeError, ValueError) as e:
        msg = str(e)
        if "reshape" in msg or "0 elements" in msg or "shape" in msg:
            return ""   # silent/empty call
        raise


# ── Vertex AI helpers (HIPAA-compliant Gemini via GCP) ───────────────────────

# Module-level init guard — avoids redundant vertexai.init() calls across ticks
_vertex_init_key: str = ""

_SUMMARY_PROMPT_LIVE = """\
You are a clinical assistant for Grafton Dental Care summarizing patient phone calls for the patient chart.

Instructions:
- The pre-recorded phone greeting has been removed. Focus only on the actual conversation between staff and the caller.
- If any automated IVR greeting still appears, ignore it entirely.
- Write a concise clinical summary (3-6 sentences) of the call in professional dental office language.
- Do NOT repeat the transcript verbatim — summarize what was discussed and resolved.
- At the end, add a section titled "Action Steps:" with a bulleted list of any follow-up items.
- If there are no action steps, write "Action Steps: None."
- Never include PHI (patient names, phone numbers, dates of birth, insurance IDs, SSNs) in your response.
- Keep the total response under 350 words. Do not truncate — complete every sentence fully.

Transcript:
{transcript}

Summary:"""

_SUMMARY_PROMPT_VOICEMAIL = """\
You are a clinical assistant for Grafton Dental Care summarizing patient voicemail messages for the patient chart.

Instructions:
- The outgoing voicemail greeting has been removed. What follows is the message left by the caller.
- Begin your response with the label: VOICEMAIL
- Write a concise summary (2-5 sentences) of the message left by the caller in professional dental office language.
- Capture: the reason for the call, any specific request, urgency indicators, and any contact preference mentioned.
- Do NOT repeat the message verbatim — summarize only.
- At the end, add a section titled "Action Steps:" with a bulleted list of required follow-up.
- If there are no action steps, write "Action Steps: None."
- Never include PHI (patient names, phone numbers, dates of birth, insurance IDs, SSNs) in your response.
- Keep the total response under 300 words.

Voicemail message:
{transcript}

Summary:"""

_GRADING_PROMPT = """\
You are a call quality analyst for Grafton Dental Care. Grade the following phone call transcript based on the criteria below.

For EACH criterion, provide:
1. A score from 1-10 (10 = excellent)
2. A brief explanation (1-2 sentences) justifying the score

Grading Criteria:
{criteria_text}

IMPORTANT RULES:
- If the call is very short, a voicemail, or has no real conversation, give N/A for all criteria and explain why.
- Be fair and realistic. A score of 7–8 represents solid, professional performance. Reserve 9–10 for exceptional calls and 1–3 for genuinely poor performance. Most competent calls should score 6–8.
- Judge staff only on what is within their control. If a patient requests something the office cannot offer (e.g. weekend hours when the office is Mon–Thu), credit the staff for clearly explaining the limitation and offering alternatives.
- The pre-recorded IVR greeting ("Thank you for calling Grafton Dental Care...") is NOT staff performance — ignore it entirely.
- Never include PHI (patient names, phone numbers, dates of birth) in your response.
- Keep all string values on a single line — do not use line breaks inside JSON string values.
- Respond ONLY with valid JSON in this exact format (no markdown, no extra text):

{{
  "gradeable": true,
  "scores": [
    {{"criterion": "Criterion Name", "score": 8, "explanation": "Brief justification"}},
    ...
  ],
  "overall_notes": "1-2 sentence overall assessment of the call.",
  "overall_score": 7.5,
  "recommendations": [
    "Specific, actionable coaching tip #1.",
    "Specific, actionable coaching tip #2.",
    "Specific, actionable coaching tip #3 (only if genuinely warranted)."
  ]
}}

Guidelines for recommendations:
- Write 2-3 concrete, specific recommendations (not vague like "be more professional").
- Focus on the lowest-scoring criteria and what the staff member can do differently next time.
- Phrase each as a direct coaching instruction.
- If the call scored 9+ on all criteria, acknowledge strengths and suggest one stretch goal.
- Never include PHI in recommendations.

If the call is not gradeable (voicemail, empty, too short), respond with:
{{
  "gradeable": false,
  "reason": "Brief explanation of why this call cannot be graded"
}}

Transcript:
{transcript}
"""

_NEXT_ACTION_PROMPT = """\
You are a dental practice front-desk manager reviewing a call to decide the single \
best next action for the lead.

Inputs:
- Call summary: {summary}
- Overall grade (0-100, null if ungraded): {grade_score}
- Grade notes: {grade_notes}
- Lead lifecycle stage: {lead_stage}
- Appointment booked on this call: {booked_in_call}

Pick exactly ONE next action. Output STRICT JSON only — no markdown, no code fences, \
no commentary.

Action types (pick exactly one string):
  "book_appointment"  — lead is qualified and ready but no appointment yet
  "follow_up_call"    — needs a phone follow-up (hesitation, financing, missed details)
  "send_email"        — info/quote/insurance docs requested
  "no_action"         — handled in this call, spam, or wrong number
  "other"             — rare edge case

Priority levels:
  "urgent"  — patient is hot/ready, or same-day commitment made
  "soon"    — should happen in 1-3 days
  "low"     — informational, can wait a week+

due_in_days: integer, 0-14 (0=today). Use 0 for "no_action".

Output format (single JSON object):
{{
  "action_type": "follow_up_call",
  "priority": "soon",
  "description": "Follow up in 2 days about financing options for implant consult.",
  "due_in_days": 2,
  "reasoning": "Patient asked about cost and seemed hesitant; financing info may close them."
}}

Rules:
- description: one sentence, action-oriented, under 25 words, no PHI
- reasoning: one sentence, under 25 words, no PHI
- If call is spam/voicemail with no callback ask, return action_type="no_action"
- If appointment was already booked on this call, return action_type="no_action"
"""


def _call_vertex(prompt: str, model: str, project_id: str, location: str,
                 credentials_path: str, temperature: float = 0.25,
                 max_tokens: int = 1200,
                 response_mime_type: str = "") -> tuple[str, int, int]:
    """Call Gemini via Vertex AI SDK (HIPAA-compliant). Returns (text, input_tokens, output_tokens).

    Uses the same init-guard pattern as Pearly's vertex_client.py to avoid
    redundant vertexai.init() calls across pipeline ticks.
    """
    global _vertex_init_key

    import vertexai
    from vertexai.generative_models import GenerativeModel, GenerationConfig

    # Only re-init when config changes
    init_key = f"{project_id}::{location}::{credentials_path}"
    if _vertex_init_key != init_key:
        init_kwargs: dict = {"project": project_id, "location": location}
        if credentials_path:
            from google.oauth2 import service_account
            creds = service_account.Credentials.from_service_account_file(
                credentials_path,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            init_kwargs["credentials"] = creds
        vertexai.init(**init_kwargs)
        _vertex_init_key = init_key
        log.info("[vertex] init complete (project=%s, location=%s)", project_id, location)

    gen_cfg_kwargs: dict = {"max_output_tokens": max_tokens, "temperature": temperature}
    if response_mime_type:
        gen_cfg_kwargs["response_mime_type"] = response_mime_type
    gen_config = GenerationConfig(**gen_cfg_kwargs)
    gmodel = GenerativeModel(model)
    response = gmodel.generate_content(prompt, generation_config=gen_config)

    # Extract text defensively (blocked responses return empty)
    text = ""
    try:
        text = response.text.strip()
    except Exception:
        log.warning("[vertex] response blocked or empty for model=%s", model)
        text = ""

    usage = getattr(response, "usage_metadata", None)
    in_tok  = getattr(usage, "prompt_token_count",     0) or 0
    out_tok = getattr(usage, "candidates_token_count", 0) or 0

    return text, in_tok, out_tok


def _summarize(transcript: str, vertex_project_id: str, vertex_location: str,
               vertex_credentials_path: str, vertex_model: str,
               call_uuid: str, caller_name: str = "") -> str:
    """PHI-scrub, strip greeting, summarize via Vertex AI Gemini. Logs cost."""
    cleaned, voicemail = _strip_greeting(transcript)
    scrubbed = _scrub_phi(cleaned, caller_name)
    prompt_tmpl = _SUMMARY_PROMPT_VOICEMAIL if voicemail else _SUMMARY_PROMPT_LIVE
    prompt = prompt_tmpl.format(transcript=scrubbed)

    text, in_tok, out_tok = _call_vertex(
        prompt, vertex_model, vertex_project_id, vertex_location,
        vertex_credentials_path, temperature=0.25, max_tokens=1200,
    )
    log_gemini(
        purpose="call_summary",
        model=vertex_model,
        input_tokens=in_tok,
        output_tokens=out_tok,
        call_id=call_uuid,
    )
    return text


def _grade(transcript: str, vertex_project_id: str, vertex_location: str,
           vertex_credentials_path: str, vertex_model: str,
           call_uuid: str, caller_name: str = "") -> dict:
    """PHI-scrub, build grading prompt, call Gemini. Logs cost. Returns grade dict."""
    cleaned, _ = _strip_greeting(transcript)
    scrubbed = _scrub_phi(cleaned, caller_name)

    criteria = db.get_call_grading_criteria()
    criteria_lines = [
        f"{i}. {c['name']} (Weight: {c['weight']}%): {c['description']}"
        for i, c in enumerate(criteria, 1)
        if c.get("enabled", 1)
    ]
    criteria_text = "\n".join(criteria_lines)

    prompt = _GRADING_PROMPT.format(criteria_text=criteria_text, transcript=scrubbed)

    raw, in_tok, out_tok = _call_vertex(
        prompt, vertex_model, vertex_project_id, vertex_location,
        vertex_credentials_path, temperature=0.15, max_tokens=2048,
        response_mime_type="application/json",
    )
    log_gemini(
        purpose="call_grade",
        model=vertex_model,
        input_tokens=in_tok,
        output_tokens=out_tok,
        call_id=call_uuid,
    )

    # Parse JSON — strip markdown code fences then sanitize embedded newlines
    if raw.startswith("```"):
        raw = re.sub(r'^```\w*\n?', '', raw)
        raw = re.sub(r'\n?```$', '', raw)
        raw = raw.strip()
    m = re.search(r'\{[\s\S]*\}', raw)
    if m:
        raw = m.group(0)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    def _sanitize(s: str) -> str:
        """Walk character-by-character and sanitize literal control chars inside JSON string values.
        Matches the approach proven in the standalone mango-call-analysis app (core.py)."""
        result = []
        in_str = False
        esc = False
        for ch in s:
            if esc:
                result.append(ch)
                esc = False
            elif ch == '\\':
                result.append(ch)
                esc = True
            elif ch == '"':
                result.append(ch)
                in_str = not in_str
            elif in_str and ch == '\n':
                result.append(' ')   # literal newline inside string → space
            elif in_str and ch == '\r':
                pass                 # strip carriage returns entirely
            elif in_str and ch == '\t':
                result.append(' ')   # tab inside string → space
            else:
                result.append(ch)
        return ''.join(result)

    try:
        return json.loads(_sanitize(raw))
    except json.JSONDecodeError as e:
        log.warning("[pipeline] Grade JSON parse failed after sanitize: %s — trying json_repair", e)

    # Last resort: json_repair handles embedded unescaped quotes, newlines, etc.
    try:
        from json_repair import repair_json
        repaired = repair_json(raw, return_objects=True)
        if isinstance(repaired, dict) and "gradeable" in repaired:
            log.info("[pipeline] Grade JSON recovered via json_repair")
            return repaired
    except Exception as repair_err:
        log.warning("[pipeline] json_repair also failed: %s — raw (first 300): %s",
                    repair_err, raw[:300])

    return {"gradeable": False, "reason": "AI returned unparseable JSON — check logs"}


def _extract_json_object(raw: str) -> dict | None:
    """Multi-tier JSON parser: strip fences → regex extract → sanitize → json_repair fallback.
    Returns a parsed dict or None on complete failure.
    """
    if not raw:
        return None

    # Tier 1: strip markdown code fences
    if raw.startswith("```"):
        raw = re.sub(r'^```\w*\n?', '', raw)
        raw = re.sub(r'\n?```$', '', raw)
        raw = raw.strip()

    # Tier 2: regex-extract first JSON object
    m = re.search(r'\{[\s\S]*\}', raw)
    if m:
        raw = m.group(0)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Tier 3: sanitize embedded control characters
    def _sanitize(s: str) -> str:
        result = []
        in_str = False
        esc = False
        for ch in s:
            if esc:
                result.append(ch)
                esc = False
            elif ch == '\\':
                result.append(ch)
                esc = True
            elif ch == '"':
                result.append(ch)
                in_str = not in_str
            elif in_str and ch == '\n':
                result.append(' ')
            elif in_str and ch == '\r':
                pass
            elif in_str and ch == '\t':
                result.append(' ')
            else:
                result.append(ch)
        return ''.join(result)

    try:
        return json.loads(_sanitize(raw))
    except json.JSONDecodeError:
        pass

    # Tier 4: json_repair last resort
    try:
        from json_repair import repair_json
        repaired = repair_json(raw, return_objects=True)
        if isinstance(repaired, dict):
            return repaired
    except Exception:
        pass

    return None


_NEXT_ACTION_FALLBACK = {
    "action_type": "other",
    "priority": "low",
    "description": "Manual review needed.",
    "due_in_days": 1,
    "reasoning": "",
}


def _suggest_next_action(
    summary: str,
    grade_overall_score,
    grade_overall_notes: str,
    lead_stage: str,
    booked_in_call: bool,
    vertex_project_id: str,
    vertex_location: str,
    vertex_credentials_path: str,
    vertex_model: str,
    call_uuid: str,
) -> dict:
    """Generate an AI next-action suggestion for a call. PHI-safe — works on summary only."""
    prompt = _NEXT_ACTION_PROMPT.format(
        summary=summary or "(no summary)",
        grade_score=grade_overall_score if grade_overall_score is not None else "null",
        grade_notes=grade_overall_notes or "(none)",
        lead_stage=lead_stage or "unknown",
        booked_in_call="yes" if booked_in_call else "no",
    )
    try:
        raw, in_tok, out_tok = _call_vertex(
            prompt=prompt,
            model=vertex_model,
            project_id=vertex_project_id,
            location=vertex_location,
            credentials_path=vertex_credentials_path,
            temperature=0.2,
            max_tokens=400,
            response_mime_type="application/json",
        )
        log_gemini(
            purpose="call_next_action",
            model=vertex_model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            call_id=call_uuid,
        )
        result = _extract_json_object(raw)
        if not result:
            log.warning("[pipeline] next_action JSON parse failed for %s — using fallback", call_uuid)
            return _NEXT_ACTION_FALLBACK.copy()
        # Clamp due_in_days
        result["due_in_days"] = max(0, min(14, int(result.get("due_in_days") or 0)))
        return result
    except Exception as e:
        log.warning("[pipeline] _suggest_next_action failed for %s: %s", call_uuid, e)
        return _NEXT_ACTION_FALLBACK.copy()


# ── Team member resolution ────────────────────────────────────────────────────

def _resolve_team_member(call_row: dict, transcript: str) -> Optional[str]:
    """Match transcript / extension to a configured team member name."""
    members = db.get_call_team_members()
    if not members:
        return None

    # 1. Name match in transcript (prefer early occurrence = self-introduction)
    if transcript:
        search_text = transcript.lower()
        early_text = search_text[:400]
        best_match: Optional[str] = None
        best_score = 0
        for m in members:
            if not m.get("active", 1):
                continue
            name = (m.get("name") or "").strip()
            if not name:
                continue
            parts = name.split()
            tokens = [name.lower()] + [p.lower() for p in parts if len(p) >= 2]
            for token in tokens:
                if len(token) < 3:
                    continue
                if token in early_text:
                    score = 2
                elif token in search_text:
                    score = 1
                else:
                    continue
                if score > best_score:
                    best_score = score
                    best_match = name
        if best_match:
            return best_match

    # 2. Extension fallback
    ext_raw = call_row.get("extension") or call_row.get("answered_by_extension") or ""
    if isinstance(ext_raw, dict):
        ext_num = str(ext_raw.get("number") or ext_raw.get("id") or "").strip()
    else:
        ext_num = str(ext_raw).strip()

    if ext_num:
        for m in members:
            if str(m.get("extension", "")).strip() == ext_num:
                return m.get("name")

    return None


# ── Single-call processor ─────────────────────────────────────────────────────

def process_call(call_row: dict, mango_token: Optional[str] = None) -> None:
    """
    Run the full analysis pipeline for a single mango_calls row.
    Updates the DB row in place. Raises on hard failures.
    """
    uuid = call_row.get("uuid") or ""
    log.info("[pipeline] Processing call %s (duration=%ds)",
             uuid, call_row.get("duration_sec") or 0)

    # Wrap EVERYTHING — even the in_progress write — so a setup-time exception
    # (DB error, missing settings row, etc.) is logged + persisted, not swallowed
    # by FastAPI's BackgroundTasks runner.
    audio_path: Optional[Path] = None
    try:
        # Use DB-first settings so Admin UI saves take effect without restart.
        msettings = db.get_mango_settings()
        settings = get_settings()  # kept for mango_recording_dir + non-Mango fields
        duration_sec = call_row.get("duration_sec") or 0
        caller_name = call_row.get("caller_id_name") or ""

        # Mark in-progress and bump attempt counter
        attempts = (call_row.get("pipeline_attempts") or 0) + 1
        db.update_mango_call_analysis(
            uuid,
            transcription_status="in_progress",
            pipeline_attempts=attempts,
        )

        # ── 1. Download recording ─────────────────────────────────────────────
        # Mango's S3 bucket is NOT publicly accessible. The only valid download
        # URLs are fresh pre-signed URLs returned by the Mango /calls/ API.
        # The recording_url stored in the DB was fetched at sync time and expires
        # within minutes — we must re-fetch from Mango's API to get a fresh one.
        recording_url = ""
        if mango_token:
            tok_mgr = getattr(process_call, "_token_mgr_cache", None)
            # Re-fetch a fresh pre-signed URL for this call from Mango API
            pbx_id = msettings.get("mango_pbx_id") or ""
            api_base = msettings.get("mango_api_base") or "https://api.mangovoice.com"
            try:
                from mango_service import MangoTokenManager, fetch_fresh_recording_url as _ffru
                # Build a lightweight token manager that just wraps the existing token
                class _SingleTokenMgr:
                    def get_token(self): return mango_token
                recording_url = _ffru(_SingleTokenMgr(), uuid, pbx_id, api_base=api_base)
            except Exception as e:
                log.warning("[pipeline] %s — could not re-fetch recording URL from Mango: %s", uuid, e)

        if not recording_url:
            msg = ("No recording available in Mango — call may be too short, "
                   "sent to voicemail without a message, or the recording has expired.")
            log.info("[pipeline] %s — %s", uuid, msg)
            db.update_mango_call_analysis(
                uuid, transcription_status="skipped_no_audio",
                pipeline_error=msg,
            )
            return
        log.info("[pipeline] %s — downloading fresh recording URL", uuid)

        audio_path = _fetch_recording(recording_url, uuid, token=None)  # pre-signed URL needs no Bearer

        # ── 2. Transcribe ─────────────────────────────────────────────────────
        openai_key = msettings["openai_api_key"]
        whisper_mode = msettings["mango_whisper_mode"]
        if whisper_mode != "local" and not openai_key:
            msg = ("OPENAI_API_KEY not configured — set it in Admin → Mango "
                   "settings or in backend/.env (OPENAI_API_KEY=...)")
            log.error("[pipeline] %s — %s", uuid, msg)
            db.update_mango_call_analysis(
                uuid, transcription_status="failed",
                pipeline_error=msg,
            )
            return

        if whisper_mode == "local":
            transcript = _transcribe_local(audio_path, msettings["mango_whisper_local_model"])
        else:
            transcript = _transcribe_openai(audio_path, openai_key)

        # Log Whisper cost
        log_whisper(
            duration_seconds=float(duration_sec),
            model="whisper-1" if whisper_mode == "api" else msettings["mango_whisper_local_model"],
            call_id=uuid,
        )

        # ── 3. Empty call detection ───────────────────────────────────────────
        if _is_empty_call(transcript):
            db.update_mango_call_analysis(
                uuid,
                call_transcript=transcript,
                transcription_status="done",
                summarized_at=datetime.now(timezone.utc).isoformat(),
                is_empty=1,
                pipeline_error="",
                pipeline_attempts=0,
            )
            log.info("[pipeline] Call %s is empty/voicemail-only — skipping AI analysis", uuid)
            return

        # ── 4. Summarize via Vertex AI (HIPAA-compliant Gemini) ───────────────
        vertex_project = msettings["vertex_project_id"]
        if not vertex_project:
            # Store transcript but skip AI analysis
            log.warning("[pipeline] %s — VERTEX_PROJECT_ID not configured, summary skipped", uuid)
            db.update_mango_call_analysis(
                uuid,
                call_transcript=transcript,
                transcription_status="done",
                summarized_at=datetime.now(timezone.utc).isoformat(),
                pipeline_error="VERTEX_PROJECT_ID not configured — summary skipped",
            )
            return

        vertex_location = msettings["vertex_location"]
        vertex_creds    = msettings["vertex_credentials_path"]
        vertex_model    = msettings["vertex_model"]

        summary = _summarize(
            transcript, vertex_project, vertex_location, vertex_creds, vertex_model,
            uuid, caller_name,
        )
        now_iso = datetime.now(timezone.utc).isoformat()

        # Persist transcript + summary now (in case grading fails)
        db.update_mango_call_analysis(
            uuid,
            call_transcript=transcript,
            call_summary=summary,
            transcription_status="done",
            summarized_at=now_iso,
            pipeline_error="",
            pipeline_attempts=0,  # reset attempt counter on success
        )

        # ── 5. Resolve team member ────────────────────────────────────────────
        team_member = _resolve_team_member(call_row, transcript)
        if team_member:
            db.update_mango_call_analysis(uuid, team_member=team_member)

        # ── 6. Grade ─────────────────────────────────────────────────────────
        # Initialize grade state for Step 7 scope
        grade: dict = {}
        gradeable: bool = False
        overall_pct: int | None = None
        if msettings["mango_pipeline_auto_grade"]:
            try:
                grade = _grade(
                    transcript, vertex_project, vertex_location, vertex_creds, vertex_model,
                    uuid, caller_name,
                )
                now_iso2 = datetime.now(timezone.utc).isoformat()

                gradeable = bool(grade.get("gradeable"))
                if gradeable:
                    # Normalise 1–10 scores → 0–100 scale and rename explanation→notes
                    raw_scores = grade.get("scores", [])
                    normalised_scores = [
                        {
                            "criterion": s.get("criterion", s.get("name", "")),
                            "score": round(float(s.get("score", 0)) * 10),
                            "notes": s.get("explanation", s.get("notes", "")),
                        }
                        for s in raw_scores
                    ]
                    raw_overall = float(grade.get("overall_score") or 0)
                    overall_pct = round(raw_overall * 10)
                    scores_json = json.dumps(normalised_scores)
                    recs_json = json.dumps(grade.get("recommendations", []))
                    db.update_mango_call_analysis(
                        uuid,
                        grade_scores_json=scores_json,
                        grade_overall_score=overall_pct,
                        grade_overall_notes=grade.get("overall_notes", ""),
                        grade_recommendations_json=recs_json,
                        grade_gradeable=1,
                        grade_reason="",
                        graded_at=now_iso2,
                    )
                else:
                    db.update_mango_call_analysis(
                        uuid,
                        grade_gradeable=0,
                        grade_reason=grade.get("reason", "Not gradeable"),
                        graded_at=now_iso2,
                    )
            except Exception as grade_err:
                gradeable = False
                log.warning("[pipeline] Grading failed for %s: %s", uuid, grade_err)
                db.update_mango_call_analysis(
                    uuid,
                    pipeline_error=f"Grading error: {grade_err}",
                )

        # ── Step 7: AI next-action suggestion ──────────────────────────────────
        if msettings.get("mango_pipeline_auto_suggest_action", True):
            try:
                # Re-read to check idempotency — skip if already suggested
                current = db.get_mango_call(uuid) or {}
                if not current.get("call_next_action"):
                    lead_stage = ""
                    if current.get("lead_id"):
                        ld = db.get_lead(current["lead_id"]) or {}
                        lead_stage = ld.get("stage", "")
                    booked_in_call = bool(current.get("od_appointment_id"))
                    nxt = _suggest_next_action(
                        summary=summary or "",
                        grade_overall_score=overall_pct if gradeable else None,
                        grade_overall_notes=grade.get("overall_notes", "") if gradeable else "",
                        lead_stage=lead_stage,
                        booked_in_call=booked_in_call,
                        vertex_project_id=vertex_project,
                        vertex_location=vertex_location,
                        vertex_credentials_path=vertex_creds,
                        vertex_model=vertex_model,
                        call_uuid=uuid,
                    )
                    due_iso = ""
                    if nxt.get("action_type") != "no_action":
                        from datetime import date as _date, timedelta
                        d = max(0, min(14, int(nxt.get("due_in_days") or 0)))
                        due_iso = (_date.today() + timedelta(days=d)).isoformat()
                    db.update_mango_call_analysis(
                        uuid,
                        call_next_action=nxt.get("description", ""),
                        call_next_action_type=nxt.get("action_type", "other"),
                        call_next_action_due=due_iso,
                        call_next_action_priority=nxt.get("priority", "soon"),
                        call_next_action_reasoning=nxt.get("reasoning", ""),
                        call_next_action_suggested_at=datetime.now(timezone.utc).isoformat(),
                        call_next_action_completed=0,
                    )
            except Exception as nxt_err:
                log.warning("[pipeline] next-action suggest failed for %s: %s", uuid, nxt_err)

        # ── Step 8: Auto-match to OD appointment + set booked_outcome ───────
        # Non-blocking — failure must NOT affect the rest of the pipeline.
        # booked_outcome is derived from od_appointment_id (the grading prompt
        # never returned it, so it was always NULL — we set it here instead).
        #
        # Guard: never stamp booked_outcome='booked' on voicemail/IVR calls.
        # An OD appointment match means the caller is a known patient, NOT that
        # they booked the appointment during this call.
        try:
            refreshed = db.get_mango_call(uuid) or {}

            # Detect voicemail/IVR: pipeline summary starts with VOICEMAIL,
            # or grading was skipped (grade_gradeable=0), or is_empty flag set.
            call_summary = (refreshed.get("call_summary") or "").upper()
            is_voicemail = (
                call_summary.startswith("VOICEMAIL")
                or refreshed.get("grade_gradeable") == 0
                or refreshed.get("is_empty") == 1
            )

            if is_voicemail:
                log.debug(
                    "[pipeline] Step 8: skipping booked_outcome for voicemail/IVR call %s", uuid
                )
            elif not refreshed.get("od_appointment_id"):
                from od_matcher import match_calls_to_od_appointments
                od_result = match_calls_to_od_appointments(days=90, target_uuid=uuid)
                if od_result.get("matched", 0) > 0:
                    db.update_mango_call_analysis(uuid, booked_outcome="booked")
                    log.info("[pipeline] Step 8: OD appointment matched → booked_outcome=booked for %s", uuid)
                else:
                    log.debug("[pipeline] Step 8: No OD appointment found for call %s "
                              "(new patient or OD offline)", uuid)
            else:
                # Already has od_appointment_id — ensure booked_outcome is set
                if not refreshed.get("booked_outcome"):
                    db.update_mango_call_analysis(uuid, booked_outcome="booked")
        except Exception as od_err:
            log.warning("[pipeline] Step 8 OD match failed for %s (non-fatal): %s", uuid, od_err)

        log.info("[pipeline] Call %s processed successfully", uuid)

    except Exception as err:
        # Log with full traceback so the actual failure surfaces in stdout/log file.
        log.exception("[pipeline] Failed to process call %s: %s: %s",
                      uuid, type(err).__name__, err)
        try:
            db.update_mango_call_analysis(
                uuid,
                transcription_status="failed",
                pipeline_error=f"{type(err).__name__}: {str(err)[:480]}",
            )
        except Exception as db_err:
            log.exception("[pipeline] Could not even write failure state for %s: %s",
                          uuid, db_err)

    finally:
        if audio_path:
            _cleanup_recording(audio_path)


# ── Bulk job processor ────────────────────────────────────────────────────────

def process_bulk_job(job_id: int, mango_token: Optional[str] = None) -> None:
    """
    Process a bulk job (user-triggered re-analysis of many calls).
    Runs synchronously in a background thread. Updates job row as it goes.
    """
    job = db.get_call_bulk_job(job_id)
    if not job:
        log.error("[bulk] Job %d not found", job_id)
        return

    options = job.get("options", {})
    min_seconds = int(options.get("min_seconds", 30))
    batch_size = int(options.get("batch_size", 50))
    days_back = int(options.get("days_back", 90))

    db.update_call_bulk_job(job_id, status="running", started_at=datetime.now(timezone.utc).isoformat())

    calls = db.get_calls_needing_processing(
        min_seconds=min_seconds,
        batch_size=batch_size,
        days_back=days_back,
    )
    total = len(calls)
    db.update_call_bulk_job(job_id, total=total)

    done = 0
    errors = 0

    for call_row in calls:
        try:
            process_call(call_row, mango_token=mango_token)
            done += 1
        except Exception as e:
            log.error("[bulk] Error on call %s: %s", call_row.get("uuid"), e)
            errors += 1
        db.update_call_bulk_job(
            job_id,
            done=done,
            errors=errors,
            current_label=f"Processing {done + errors}/{total}…",
        )

    db.update_call_bulk_job(
        job_id,
        status="done",
        done=done,
        errors=errors,
        finished_at=datetime.now(timezone.utc).isoformat(),
        current_label=f"Complete — {done} processed, {errors} errors",
    )
    log.info("[bulk] Job %d complete: %d processed, %d errors", job_id, done, errors)


def start_bulk_job_async(job_id: int, mango_token: Optional[str] = None) -> None:
    """Launch a bulk job in a daemon thread (non-blocking)."""
    t = threading.Thread(
        target=process_bulk_job, args=(job_id, mango_token), daemon=True
    )
    t.start()


# ── Scheduler tick ────────────────────────────────────────────────────────────

def _reset_stale_in_progress(stale_minutes: int = 15) -> int:
    """
    Reset any mango_calls rows stuck in transcription_status='in_progress'
    for longer than stale_minutes (e.g. due to a worker crash).
    Marks them as 'failed' so they re-enter the processing queue.
    Returns the number of rows reset.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)).isoformat()
    try:
        with db._conn() as conn:
            cur = conn.execute(
                """UPDATE mango_calls
                   SET transcription_status = 'failed',
                       pipeline_error = 'Reset from stale in_progress state',
                       updated_at = ?
                   WHERE transcription_status = 'in_progress'
                     AND updated_at < ?""",
                (datetime.now(timezone.utc).isoformat(), cutoff),
            )
            count = cur.rowcount
        if count > 0:
            log.warning("[pipeline] Reset %d stale in_progress call(s) to failed", count)
        return count
    except Exception as e:
        log.error("[pipeline] Failed to reset stale in_progress rows: %s", e)
        return 0


def backfill_booked_outcome() -> dict:
    """
    Two-phase backfill:
    Phase 1 — Run match_calls_to_od_appointments(days=90) to populate od_appointment_id
              for the ~7,800 calls that have od_patient_num but no appointment match yet.
              (The appointment matcher previously required booked_outcome='booked' to run,
              which was never written — this unlocks all those historical calls.)
    Phase 2 — Set booked_outcome='booked' for all inbound calls that now have
              od_appointment_id, so call_production_log can find them.

    Safe to run multiple times — each step is idempotent.
    Returns: {"appointment_match": dict, "booked_outcome_updated": int}
    """
    from database import _conn
    from datetime import datetime, timezone

    # Phase 1: run appointment matcher for past 90 days
    log.info("[pipeline] backfill_booked_outcome Phase 1: running appointment matcher (days=90)")
    try:
        from od_matcher import match_calls_to_od_appointments
        appt_result = match_calls_to_od_appointments(days=90)
        log.info("[pipeline] backfill Phase 1 done: %s", appt_result)
    except Exception as e:
        log.warning("[pipeline] backfill Phase 1 appointment match failed (non-fatal): %s", e)
        appt_result = {"error": str(e)}

    # Phase 2: stamp booked_outcome on all calls with od_appointment_id.
    # Exclude voicemail/IVR calls — an OD match on a voicemail means the
    # caller is a known patient, not that they booked during the call.
    log.info("[pipeline] backfill_booked_outcome Phase 2: stamping booked_outcome")
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        cur = conn.execute("""
            UPDATE mango_calls
               SET booked_outcome = 'booked', updated_at = ?
             WHERE direction = 'inbound'
               AND od_appointment_id IS NOT NULL
               AND od_appointment_id != ''
               AND (booked_outcome IS NULL OR booked_outcome = '')
               AND (is_empty IS NULL OR is_empty = 0)
               AND (grade_gradeable IS NULL OR grade_gradeable != 0)
               AND (UPPER(call_summary) NOT LIKE 'VOICEMAIL%')
        """, (now,))
        updated = cur.rowcount
    log.info("[pipeline] backfill Phase 2 done: stamped %d rows with booked_outcome='booked'", updated)
    return {"appointment_match": appt_result, "booked_outcome_updated": updated}


def run_pipeline_tick(mango_token: Optional[str] = None) -> None:
    """
    Called by APScheduler every mango_pipeline_interval_min minutes.
    Picks up unprocessed calls and runs the pipeline on each.
    Gated by mango_pipeline_enabled (DB-first so UI toggle takes effect immediately).
    """
    msettings = db.get_mango_settings()
    settings = get_settings()  # kept for min_seconds / max_per_run (not yet in DB)

    if not msettings["mango_pipeline_enabled"]:
        return

    # Recover any calls left stuck in_progress from a previous crash
    _reset_stale_in_progress(stale_minutes=15)

    # Don't run if a bulk job is active (avoid API cost contention)
    active_bulk = db.get_active_call_bulk_job()
    if active_bulk:
        log.debug("[pipeline] Skipping tick — bulk job %d is active", active_bulk["id"])
        return

    calls = db.get_calls_needing_processing(
        min_seconds=settings.mango_pipeline_min_seconds,
        max_attempts=3,
        batch_size=settings.mango_pipeline_max_per_run,
        days_back=90,
    )

    if not calls:
        log.debug("[pipeline] No calls need processing")
        cleanup_old_recordings()
        return

    log.info("[pipeline] Tick: processing %d call(s)", len(calls))
    for call_row in calls:
        process_call(call_row, mango_token=mango_token)

    cleanup_old_recordings()


# ── Claude cost instrumentation wrapper ──────────────────────────────────────
#
# Drop-in replacement for anthropic_client.messages.create().
# Any module that calls Claude should import and use claude_complete() instead
# of calling the Anthropic client directly. This ensures all Claude usage is
# logged to the ai_usage table.
#
# Usage:
#   from mango_pipeline import claude_complete
#
#   response = claude_complete(
#       client,          # anthropic.Anthropic() instance
#       model="claude-sonnet-4-5",
#       max_tokens=1024,
#       messages=[{"role": "user", "content": "..."}],
#       purpose="campaign_strategy",        # required for cost tracking
#       campaign_id="...",                  # optional
#   )
#   # response is a standard anthropic.Message object
#
# ─────────────────────────────────────────────────────────────────────────────

def claude_complete(
    client,
    *,
    model: str,
    max_tokens: int,
    messages: list,
    system: Optional[str] = None,
    purpose: str = "unknown",
    campaign_id: str = "",
    lead_id: str = "",
    optimizer_run_id: str = "",
    **kwargs,
):
    """
    Wrapper around anthropic client.messages.create() that logs token usage.

    Parameters
    ----------
    client           : anthropic.Anthropic() instance
    model            : Claude model string
    max_tokens       : max output tokens
    messages         : conversation messages list
    system           : optional system prompt string
    purpose          : cost tag — e.g. 'campaign_strategy', 'campaign_build',
                       'ad_copy', 'optimizer_run', 'sitelinks', 'workflow_ai'
    campaign_id      : optional Google Ads campaign ID
    lead_id          : optional pipeline lead ID
    optimizer_run_id : optional optimizer run ID
    **kwargs         : any additional args passed to messages.create()

    Returns
    -------
    anthropic.types.Message  (same as messages.create())
    """
    call_kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
        **kwargs,
    }
    if system is not None:
        call_kwargs["system"] = system

    t0 = time.monotonic()
    response = client.messages.create(**call_kwargs)
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    try:
        # Streaming responses return an iterator, not a Message with .usage.
        # Skip cost logging for streams — the caller should handle that separately.
        if not hasattr(response, "usage"):
            log.warning(
                "[pipeline] claude_complete: response has no .usage (streaming?); "
                "cost not logged for purpose=%s", purpose
            )
            return response

        in_tok = response.usage.input_tokens if response.usage else 0
        out_tok = response.usage.output_tokens if response.usage else 0
        log_claude(
            purpose=purpose,
            model=model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            campaign_id=campaign_id,
            lead_id=lead_id,
            optimizer_run_id=optimizer_run_id,
            request_ms=elapsed_ms,
        )
    except Exception as log_err:
        log.warning("[pipeline] claude_complete: cost logging failed: %s", log_err)

    return response
