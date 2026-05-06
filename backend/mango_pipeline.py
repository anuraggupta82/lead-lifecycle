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
    is_presigned = (
        ".amazonaws.com" in url_lower
        or "x-amz-signature" in url_lower
        or "awsaccesskeyid" in url_lower
        or "x-amz-credential" in url_lower
    )
    if token and not is_presigned:
        headers["Authorization"] = f"Bearer {token}"

    resp = requests.get(recording_url, headers=headers, timeout=60, stream=True)
    resp.raise_for_status()

    content_type = resp.headers.get("Content-Type", "")
    if "text/html" in content_type or "application/json" in content_type:
        raise ValueError(
            f"Recording download returned {content_type} — likely an auth error"
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
- Be fair but honest. Score based on what actually happened in the call.
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


def _call_vertex(prompt: str, model: str, project_id: str, location: str,
                 credentials_path: str, temperature: float = 0.25,
                 max_tokens: int = 1200) -> tuple[str, int, int]:
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

    gen_config = GenerationConfig(
        max_output_tokens=max_tokens,
        temperature=temperature,
    )
    gmodel = GenerativeModel(model, generation_config=gen_config)
    response = gmodel.generate_content(prompt)

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
            elif in_str and ch in '\n\r':
                result.append(' ')
            elif in_str and ch == '\t':
                result.append(' ')
            else:
                result.append(ch)
        return ''.join(result)

    return json.loads(_sanitize(raw))


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
    # Use DB-first settings so Admin UI saves take effect without restart.
    msettings = db.get_mango_settings()
    settings = get_settings()  # kept for mango_recording_dir + non-Mango fields
    uuid = call_row["uuid"]
    duration_sec = call_row.get("duration_sec") or 0
    recording_url = call_row.get("recording_url") or ""
    caller_name = call_row.get("caller_id_name") or ""

    log.info("[pipeline] Processing call %s (duration=%ds)", uuid, duration_sec)

    # Mark in-progress and bump attempt counter
    attempts = (call_row.get("pipeline_attempts") or 0) + 1
    db.update_mango_call_analysis(
        uuid,
        transcription_status="in_progress",
        pipeline_attempts=attempts,
    )

    audio_path: Optional[Path] = None

    try:
        # ── 1. Download recording ─────────────────────────────────────────────
        if not recording_url:
            db.update_mango_call_analysis(
                uuid, transcription_status="failed",
                pipeline_error="No recording URL available",
                is_empty=1,
            )
            return

        audio_path = _fetch_recording(recording_url, uuid, token=mango_token)

        # ── 2. Transcribe ─────────────────────────────────────────────────────
        openai_key = msettings["openai_api_key"]
        if not openai_key:
            db.update_mango_call_analysis(
                uuid, transcription_status="failed",
                pipeline_error="OPENAI_API_KEY not configured",
            )
            return

        whisper_mode = msettings["mango_whisper_mode"]
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
                transcript=transcript,
                transcription_status="done",
                summarized_at=datetime.now(timezone.utc).isoformat(),
                is_empty=1,
                pipeline_error="",
            )
            log.info("[pipeline] Call %s is empty/voicemail-only — skipping AI analysis", uuid)
            return

        # ── 4. Summarize via Vertex AI (HIPAA-compliant Gemini) ───────────────
        vertex_project = msettings["vertex_project_id"]
        if not vertex_project:
            # Store transcript but skip AI analysis
            db.update_mango_call_analysis(
                uuid,
                transcript=transcript,
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
            transcript=transcript,
            summary=summary,
            transcription_status="done",
            summarized_at=now_iso,
            pipeline_error="",
        )

        # ── 5. Resolve team member ────────────────────────────────────────────
        team_member = _resolve_team_member(call_row, transcript)
        if team_member:
            db.update_mango_call_analysis(uuid, team_member=team_member)

        # ── 6. Grade ─────────────────────────────────────────────────────────
        if msettings["mango_pipeline_auto_grade"]:
            try:
                grade = _grade(
                    transcript, vertex_project, vertex_location, vertex_creds, vertex_model,
                    uuid, caller_name,
                )
                now_iso2 = datetime.now(timezone.utc).isoformat()

                if grade.get("gradeable"):
                    scores_json = json.dumps(grade.get("scores", []))
                    recs_json = json.dumps(grade.get("recommendations", []))
                    db.update_mango_call_analysis(
                        uuid,
                        grade_scores_json=scores_json,
                        grade_overall_score=float(grade.get("overall_score") or 0),
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
                log.warning("[pipeline] Grading failed for %s: %s", uuid, grade_err)
                db.update_mango_call_analysis(
                    uuid,
                    pipeline_error=f"Grading error: {grade_err}",
                )

        log.info("[pipeline] Call %s processed successfully", uuid)

    except Exception as err:
        log.error("[pipeline] Failed to process call %s: %s", uuid, err, exc_info=True)
        db.update_mango_call_analysis(
            uuid,
            transcription_status="failed",
            pipeline_error=str(err)[:500],
        )

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
