"""
backfill_voicemail_names.py

One-shot backfill: for every mango_call that has a transcript but an empty
ai_patient_name AND was identified as a voicemail (call_summary starts with 'VOICEMAIL'),
run the lightweight Gemini name-extraction pass and write the result back.

Usage:
    cd /Users/anurag/Documents/Projects/gdc-apps/marketing/lead-lifecycle/backend
    source venv/bin/activate
    python backfill_voicemail_names.py [--dry-run] [--limit N]

Options:
    --dry-run   Print what would be updated without writing to DB.
    --limit N   Process at most N calls (default: all).
"""

import argparse
import json
import logging
import re
import sqlite3
import sys
from pathlib import Path

# Reuse prompt, vertex caller, and greeting stripper from the live pipeline
# to stay in sync with any future changes to those functions.
sys.path.insert(0, str(Path(__file__).parent))
from mango_pipeline import (  # noqa: E402
    _NAME_EXTRACT_PROMPT_VOICEMAIL,
    _call_vertex,
    _strip_greeting,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "marketing.db"


def get_vertex_settings(conn: sqlite3.Connection) -> dict:
    cur = conn.execute("SELECT key, value FROM settings")
    s = {row[0]: row[1] for row in cur.fetchall()}
    return {
        "vertex_project_id":       s.get("vertex_project_id", ""),
        "vertex_location":         s.get("vertex_location", "us-central1"),
        "vertex_credentials_path": s.get("vertex_credentials_path", ""),
        "vertex_model":            s.get("vertex_model", "gemini-1.5-flash-001"),
    }


def extract_name(transcript: str, cfg: dict) -> str:
    """Strip greeting, run Gemini name extraction, return 'First Last' or ''."""
    # Strip IVR greeting to match live pipeline behavior
    cleaned, _ = _strip_greeting(transcript)

    # Skip trivially short transcripts (hang-ups, silence, IVR-only)
    if len(cleaned.split()) < 5:
        return ""

    prompt = _NAME_EXTRACT_PROMPT_VOICEMAIL.format(transcript=cleaned)
    try:
        raw, in_tok, out_tok = _call_vertex(
            prompt,
            cfg["vertex_model"],
            cfg["vertex_project_id"],
            cfg["vertex_location"],
            cfg["vertex_credentials_path"],
            temperature=0.0,
            max_tokens=128,
            response_mime_type="application/json",
        )
        log.debug("  Vertex tokens in=%d out=%d", in_tok, out_tok)
    except Exception:
        log.exception("  Vertex call failed — skipping")
        return ""

    if not raw:
        return ""

    name_raw = raw.strip()
    if name_raw.startswith("```"):
        name_raw = re.sub(r'^```\w*\n?', '', name_raw)
        name_raw = re.sub(r'\n?```$', '', name_raw)
        name_raw = name_raw.strip()

    try:
        parsed = json.loads(name_raw)
        first = (parsed.get("first_name") or "").strip()
        last  = (parsed.get("last_name") or "").strip()
        return f"{first} {last}".strip()
    except Exception as exc:
        log.warning("  JSON parse failed: %s — raw: %r", exc, name_raw[:200])
        return ""


def main():
    parser = argparse.ArgumentParser(description="Backfill ai_patient_name for voicemail calls")
    parser.add_argument("--dry-run", action="store_true", help="Print results without updating DB")
    parser.add_argument("--limit", type=int, default=0, help="Max calls to process (0 = all)")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    cfg = get_vertex_settings(conn)
    if not cfg["vertex_project_id"]:
        log.error("vertex_project_id not found in settings — aborting")
        sys.exit(1)
    log.info("Vertex config: project=%s model=%s", cfg["vertex_project_id"], cfg["vertex_model"])

    # mango_calls schema: PK is uuid (text), transcript is call_transcript, summary is call_summary
    query = """
        SELECT uuid, call_transcript, ai_patient_name, call_summary
        FROM mango_calls
        WHERE call_transcript IS NOT NULL
          AND call_transcript != ''
          AND (ai_patient_name IS NULL OR ai_patient_name = '')
          AND UPPER(call_summary) LIKE 'VOICEMAIL%'
        ORDER BY started_at DESC
    """
    if args.limit > 0:
        query += f" LIMIT {args.limit}"

    rows = conn.execute(query).fetchall()
    log.info("Found %d voicemail calls with empty ai_patient_name", len(rows))

    updated = 0
    skipped = 0
    batch = []

    for i, row in enumerate(rows, 1):
        uuid       = row["uuid"]
        transcript = row["call_transcript"]

        name = extract_name(transcript, cfg)
        if not name:
            log.info("  [%s] No name found — skipping", uuid)
            skipped += 1
            continue

        log.info("  [%s] Extracted: %r", uuid, name)
        if not args.dry_run:
            batch.append((name, uuid))
            # Commit in batches of 25
            if len(batch) >= 25:
                conn.executemany(
                    "UPDATE mango_calls SET ai_patient_name = ? WHERE uuid = ?", batch
                )
                conn.commit()
                batch.clear()
        updated += 1

    # Flush remaining
    if batch and not args.dry_run:
        conn.executemany(
            "UPDATE mango_calls SET ai_patient_name = ? WHERE uuid = ?", batch
        )
        conn.commit()

    log.info(
        "Done. Updated: %d | Skipped (no name found): %d | Dry run: %s",
        updated, skipped, args.dry_run,
    )
    conn.close()


if __name__ == "__main__":
    main()
