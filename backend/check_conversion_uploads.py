import sqlite3
import json

conn = sqlite3.connect("pipeline.db")
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

# Query conversion_uploads table
cursor.execute("SELECT * FROM conversion_uploads;")
rows = cursor.fetchall()
print(f"Total conversion uploads: {len(rows)}")

for row in rows:
    # Try to find corresponding lead info
    lead_id = row['lead_id']
    cursor.execute("SELECT first_name, last_name, campaign_name, campaign_id, source, stage FROM leads WHERE id = ?;", (lead_id,))
    lead = cursor.fetchone()
    lead_info = dict(lead) if lead else None
    
    print(f"Upload ID: {row['id']} | Action: {row['conversion_action']} | Lead ID: {lead_id} | Status: {row['status']} | Value: {row['conversion_value']} | Uploaded At: {row['uploaded_at']}")
    if lead_info:
        print(f"  -> Lead: {lead_info['first_name']} {lead_info['last_name']} | Camp: {lead_info['campaign_name']} ({lead_info['campaign_id']}) | Source: {lead_info['source']} | Stage: {lead_info['stage']}")
    else:
        print("  -> Lead not found in database!")

conn.close()
