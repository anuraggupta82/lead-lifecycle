import sqlite3
import datetime
import uuid

db_path = "/Users/anurag/Documents/Projects/gdc-apps/marketing/lead-lifecycle/backend/pipeline.db"

def add_decision(decision_id, decision_type, title, summary, detail, rule_learned):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    
    # 1. Add to mcp_decisions
    cur.execute("""
        INSERT INTO mcp_decisions (id, decision_type, title, summary, detail, rule_learned, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (decision_id, decision_type, title, summary, detail, rule_learned, now))
    
    # 2. Add to optimizer_memory
    cur.execute("""
        INSERT INTO optimizer_memory (category, key, value, reason, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, ("campaign_rule", "competitor_conquest", "block_all", summary, now, now))
    
    cur.execute("""
        INSERT INTO optimizer_memory (category, key, value, reason, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, ("keyword_override", "extractions", "never_pause", "Office performs extractions in-house.", now, now))
    
    conn.commit()
    conn.close()
    print("Decision logged to database.")

decision_id = str(uuid.uuid4())[:8]
title = "May 30 Negative Keyword Push"
summary = "Blocked national/local competitors and out-of-scope services, but kept extractions."
detail = "Pushed Nuvia, Clear Choice, and local competitors to negatives due to high CPCs starving budget. Kept extractions as they are performed in-house. Blocked 'implant' from general campaigns to fix cross-pollination."
rule_learned = "Do not pause 'extraction' keywords. Block competitor brands to preserve budget for high-intent generic searches."

add_decision(decision_id, "negative", title, summary, detail, rule_learned)
