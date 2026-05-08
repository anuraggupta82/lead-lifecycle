"""
Diagnostic: check negative keyword audit log status and optimizer run history.
Run from the backend folder with the venv active:
  python diag_negatives.py
"""
import sqlite3, os, sys

db = os.path.expanduser("~/grafton_pipeline/pipeline.db")
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row

print("=" * 70)
print("NEGATIVE KEYWORD AUDIT LOG (all statuses, newest first)")
print("=" * 70)
rows = conn.execute("""
  SELECT operation, entity_name, campaign_name, execution_result,
         api_executed, created_at, updated_at
  FROM gads_audit_log
  WHERE operation IN ('add_negative_keyword','add_to_shared_negative_list')
  ORDER BY created_at DESC LIMIT 80
""").fetchall()
for r in rows:
    api = "✓API" if r["api_executed"] else "    "
    print(f"  [{r['execution_result']:18}] {api} | '{r['entity_name']}' | camp='{r['campaign_name'][:30]}' | {r['created_at'][:16]}")

print()
print("=" * 70)
print("OPTIMIZER RUNS (last 10)")
print("=" * 70)
try:
    runs = conn.execute("""
      SELECT run_id, started_at, completed_at, mode, total_actions
      FROM gads_optimizer_runs
      ORDER BY started_at DESC LIMIT 10
    """).fetchall()
    for r in runs:
        print(f"  [{r['mode']}] started={r['started_at'][:16]} actions={r['total_actions']}")
except Exception as e:
    print(f"  gads_optimizer_runs error: {e}")

print()
print("=" * 70)
print("LIVE NEGATIVE KEYWORDS CACHED (gads_negative_keywords table)")
print("=" * 70)
try:
    negs = conn.execute("""
      SELECT keyword_text, match_type, campaign_name, synced_at
      FROM gads_negative_keywords
      ORDER BY synced_at DESC LIMIT 60
    """).fetchall()
    if negs:
        print(f"  (synced at {negs[0]['synced_at'][:16]})")
        for n in negs:
            print(f"  [{n['match_type']:7}] '{n['keyword_text']}' — {n['campaign_name'][:40]}")
    else:
        print("  (empty — _fetch_existing_negatives may not have run yet)")
except Exception as e:
    print(f"  gads_negative_keywords error: {e}")

conn.close()
