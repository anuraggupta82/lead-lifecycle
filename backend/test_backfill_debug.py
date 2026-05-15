"""
Debug backfill_call_keyword_attribution — check JOIN and data state.
Run: source venv/bin/activate && python3 test_backfill_debug.py
"""
import sqlite3, os
db_path = os.path.join(os.path.dirname(__file__), "pipeline.db")

with sqlite3.connect(db_path) as conn:
    conn.row_factory = sqlite3.Row

    # 1. mango_calls with gads_call_id
    print("=== mango_calls with gads_call_id set ===")
    total = conn.execute("SELECT COUNT(*) FROM mango_calls").fetchone()[0]
    with_gads = conn.execute("SELECT COUNT(*) FROM mango_calls WHERE gads_call_id IS NOT NULL AND gads_call_id != ''").fetchone()[0]
    print(f"  Total mango_calls: {total}, with gads_call_id: {with_gads}")
    rows = conn.execute("SELECT uuid, gads_call_id, attributed_keyword, attributed_keyword_method FROM mango_calls WHERE gads_call_id IS NOT NULL AND gads_call_id != '' LIMIT 5").fetchall()
    for r in rows:
        print(f"  uuid={r['uuid'][:8]}... gads_call_id='{r['gads_call_id']}' kw='{r['attributed_keyword']}' method='{r['attributed_keyword_method']}'")

    # 2. gads_call_view call_ids
    print("\n=== gads_call_view call_ids ===")
    total_gcv = conn.execute("SELECT COUNT(*) FROM gads_call_view").fetchone()[0]
    print(f"  Total gads_call_view rows: {total_gcv}")
    rows = conn.execute("SELECT call_id, campaign_id, campaign_name, ad_group_name FROM gads_call_view LIMIT 5").fetchall()
    for r in rows:
        print(f"  call_id='{r['call_id']}' campaign_id='{r['campaign_id']}' campaign='{r['campaign_name']}' ag='{r['ad_group_name']}'")

    # 3. The actual JOIN — does it produce any rows?
    print("\n=== JOIN result: mango_calls JOIN gads_call_view ===")
    join_rows = conn.execute("""
        SELECT mc.uuid, mc.gads_call_id, gcv.call_id,
               gcv.campaign_id, gcv.campaign_name, gcv.ad_group_name,
               mc.attributed_keyword_method
        FROM mango_calls mc
        JOIN gads_call_view gcv ON gcv.call_id = mc.gads_call_id
        WHERE mc.gads_call_id IS NOT NULL AND mc.gads_call_id != ''
        LIMIT 10
    """).fetchall()
    print(f"  JOIN rows: {len(join_rows)}")
    for r in join_rows:
        print(f"  mc.gads_call_id='{r['gads_call_id']}' == gcv.call_id='{r['call_id']}' | method='{r['attributed_keyword_method']}' | campaign='{r['campaign_name']}'")

    # 4. If JOIN is 0, show raw call_id samples from both sides to spot format mismatch
    if len(join_rows) == 0:
        print("\n=== MISMATCH DEBUG: raw call_ids ===")
        mc_ids = [r[0] for r in conn.execute("SELECT gads_call_id FROM mango_calls WHERE gads_call_id IS NOT NULL AND gads_call_id != '' LIMIT 5").fetchall()]
        gcv_ids = [r[0] for r in conn.execute("SELECT call_id FROM gads_call_view LIMIT 5").fetchall()]
        print(f"  mango_calls.gads_call_id samples: {mc_ids}")
        print(f"  gads_call_view.call_id samples:   {gcv_ids}")

    # 5. Check the WHERE clause — method filter
    print("\n=== Rows that would pass the WHERE filter ===")
    eligible = conn.execute("""
        SELECT COUNT(*) FROM mango_calls mc
        JOIN gads_call_view gcv ON gcv.call_id = mc.gads_call_id
        WHERE mc.gads_call_id IS NOT NULL AND mc.gads_call_id != ''
          AND (mc.attributed_keyword_method IS NULL OR mc.attributed_keyword_method != 'call_search_term')
    """).fetchone()[0]
    print(f"  Eligible for backfill: {eligible}")

    # 6. gads_call_search_terms campaign_ids
    print("\n=== gads_call_search_terms campaign_ids ===")
    st_campaigns = conn.execute("SELECT DISTINCT campaign_id, campaign_name FROM gads_call_search_terms LIMIT 5").fetchall()
    for r in st_campaigns:
        print(f"  campaign_id='{r['campaign_id']}' name='{r['campaign_name']}'")

print("\nDone.")
