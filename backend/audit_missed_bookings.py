"""
Audit script: find calls that were processed but may have missed booking detection.

Run from the backend/ directory:
    python3 audit_missed_bookings.py

Outputs:
  1. Overall stats
  2. Calls with ai_appointment_scheduled=1 but no booked_outcome  (NEW - fixable immediately)
  3. Calls with no booked_outcome, long duration, summarized — likely candidates to re-examine
  4. SQL to fix the ai_appointment_scheduled=1 cases in one shot
"""
import sqlite3, os

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pipeline.db")
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

cols = {r[1] for r in conn.execute("PRAGMA table_info(mango_calls)").fetchall()}
has_ai_col = "ai_appointment_scheduled" in cols
print(f"DB: {DB}")
print(f"ai_appointment_scheduled column present: {has_ai_col}\n")

# ── 1. Overall stats ────────────────────────────────────────────────────────
total     = conn.execute("SELECT COUNT(*) FROM mango_calls WHERE direction='inbound'").fetchone()[0]
summarized= conn.execute("SELECT COUNT(*) FROM mango_calls WHERE direction='inbound' AND summarized_at != '' AND summarized_at IS NOT NULL").fetchone()[0]
booked    = conn.execute("SELECT COUNT(*) FROM mango_calls WHERE booked_outcome='booked'").fetchone()[0]

print(f"Total inbound calls : {total}")
print(f"  Summarized        : {summarized}")
print(f"  booked_outcome=booked: {booked}")

# ── 2. Calls where Gemini already said appointment_scheduled=1 but booked_outcome not set ─
if has_ai_col:
    ai_fixable = conn.execute("""
        SELECT uuid, started_at, from_number, caller_id_name,
               call_duration_seconds, od_patient_num,
               ai_appointment_type, ai_patient_name,
               substr(call_summary, 1, 200) as summary_preview
        FROM mango_calls
        WHERE direction='inbound'
          AND ai_appointment_scheduled = 1
          AND (booked_outcome IS NULL OR booked_outcome = '')
        ORDER BY started_at DESC
    """).fetchall()
    print(f"\n{'='*60}")
    print(f"FIXABLE NOW — ai_appointment_scheduled=1 but no booked_outcome: {len(ai_fixable)}")
    for r in ai_fixable:
        print(f"\n  {r['started_at'][:16]} | {r['caller_id_name'] or r['from_number']} | {r['call_duration_seconds']}s")
        print(f"  OD patient: {r['od_patient_num'] or 'none'}")
        print(f"  AI type: {r['ai_appointment_type']} | AI patient: {r['ai_patient_name']}")
        print(f"  Summary: {r['summary_preview']}...")
    if ai_fixable:
        uuids = ", ".join(f"'{r['uuid']}'" for r in ai_fixable)
        print(f"\n  SQL to fix all {len(ai_fixable)} at once:")
        print(f"  UPDATE mango_calls SET booked_outcome='booked' WHERE uuid IN ({uuids});")
else:
    print("\n  [ai_appointment_scheduled column not yet in DB — run the app once to migrate]")

# ── 3. Undetected bookings — long calls, summarized, no booked_outcome ─────
candidates = conn.execute("""
    SELECT uuid, started_at, from_number, caller_id_name,
           call_duration_seconds, od_patient_num,
           substr(call_summary, 1, 300) as summary_preview
    FROM mango_calls
    WHERE direction='inbound'
      AND (booked_outcome IS NULL OR booked_outcome = '')
      AND summarized_at IS NOT NULL AND summarized_at != ''
      AND call_duration_seconds >= 180
    ORDER BY started_at DESC
""").fetchall()
print(f"\n{'='*60}")
print(f"NEEDS REVIEW — summarized, ≥3min, no booked_outcome: {len(candidates)}")
for r in candidates:
    print(f"\n  {r['started_at'][:16]} | {r['caller_id_name'] or r['from_number']} | {r['call_duration_seconds']}s | OD: {r['od_patient_num'] or 'none'}")
    print(f"  {r['summary_preview']}...")

# ── 4. Calls to re-run the pipeline on ─────────────────────────────────────
print(f"\n{'='*60}")
print("To re-run the pipeline on ALL unbooked summarized calls ≥3min (Gemini will re-evaluate):")
rerun_uuids = [r['uuid'] for r in candidates]
if rerun_uuids:
    uuids_str = ", ".join(f"'{u}'" for u in rerun_uuids)
    print(f"\n  -- Clear summarized_at so pipeline re-processes them:")
    print(f"  UPDATE mango_calls SET summarized_at='', pipeline_error='' WHERE uuid IN ({uuids_str});")
    print(f"\n  ({len(rerun_uuids)} calls would be re-queued)")
else:
    print("  No candidates found.")

conn.close()
