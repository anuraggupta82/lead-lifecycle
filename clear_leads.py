"""
One-time script to clear all lead data for a fresh start.
Run from the Mac: python3 ~/Documents/Projects/gdc-apps/marketing/lead-lifecycle/backend/../../../clear_leads.py
Or: python3 /path/to/clear_leads.py
"""
import sqlite3
import os

DB_PATH = os.path.expanduser("~/grafton_pipeline/pipeline.db")

if not os.path.exists(DB_PATH):
    print(f"ERROR: Database not found at {DB_PATH}")
    exit(1)

conn = sqlite3.connect(DB_PATH)
# Check tables that exist
existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
print(f"Tables found: {sorted(existing)}")

tables = ['follow_up_queue','lifecycle_events','lead_notes','od_matches',
          'conversion_uploads','communication_log','deleted_leads','leads']

for t in tables:
    if t in existing:
        c = conn.execute(f"DELETE FROM {t}")
        print(f"  Cleared {t}: {c.rowcount} rows deleted")
    else:
        print(f"  Skipped {t}: table doesn't exist yet (will be created on next startup)")

conn.commit()
conn.close()
print("\nDone. All leads cleared for fresh start.")
