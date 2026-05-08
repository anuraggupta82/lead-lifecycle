"""
Reset errored add_to_shared_negative_list rows back to pending_approval
so they can be re-approved now that the api_executed bug is fixed.
Run: python3 fix_error_rows.py
"""
import sqlite3, os

db = os.path.expanduser("~/grafton_pipeline/pipeline.db")
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row

# Find the error rows
rows = conn.execute("""
  SELECT action_id, entity_name, operation, error_detail
  FROM gads_audit_log
  WHERE execution_result = 'error'
    AND operation = 'add_to_shared_negative_list'
""").fetchall()

print(f"Found {len(rows)} errored add_to_shared_negative_list rows:")
for r in rows:
    print(f"  {r['action_id'][:8]} | '{r['entity_name']}'")

if rows:
    ids = [r['action_id'] for r in rows]
    placeholders = ','.join('?' * len(ids))
    conn.execute(f"""
      UPDATE gads_audit_log
         SET execution_result = 'pending_approval', error_detail = '', updated_at = datetime('now')
       WHERE action_id IN ({placeholders})
    """, ids)
    conn.commit()
    print(f"\nReset {len(rows)} rows back to pending_approval. Refresh the optimizer UI.")
else:
    print("Nothing to reset.")

conn.close()
