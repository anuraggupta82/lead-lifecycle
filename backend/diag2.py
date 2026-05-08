import sqlite3, os
db = os.path.expanduser("~/grafton_pipeline/pipeline.db")
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row

print("=== tina theroux exact stored values ===")
rows = conn.execute("""
  SELECT entity_name, operation, campaign_name, execution_result, created_at
  FROM gads_audit_log WHERE entity_name LIKE '%tina%' OR entity_name LIKE '%theroux%'
  ORDER BY created_at DESC
""").fetchall()
for r in rows:
    print(repr(dict(r)))

print("\n=== gads_negative_keywords: tina ===")
rows2 = conn.execute("SELECT keyword_text, match_type, campaign_name FROM gads_negative_keywords WHERE keyword_text LIKE '%tina%' OR keyword_text LIKE '%theroux%'").fetchall()
for r in rows2:
    print(repr(dict(r)))

print("\n=== error rows detail ===")
rows3 = conn.execute("""
  SELECT entity_name, operation, execution_result, error_detail, created_at
  FROM gads_audit_log WHERE execution_result = 'error' ORDER BY created_at DESC LIMIT 10
""").fetchall()
for r in rows3:
    print(repr(dict(r)))

print("\n=== optimizer_runs schema ===")
rows4 = conn.execute("PRAGMA table_info(gads_optimizer_runs)").fetchall()
for r in rows4:
    print(dict(r))

print("\n=== Emergency campaign negatives in gads_negative_keywords ===")
rows5 = conn.execute("""
  SELECT keyword_text, match_type, campaign_name FROM gads_negative_keywords
  WHERE campaign_name LIKE '%Emergency%' LIMIT 20
""").fetchall()
for r in rows5:
    print(dict(r))
if not rows5:
    print("(none found)")

conn.close()
