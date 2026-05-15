"""
Direct test of sync_call_search_terms to see what's happening.
Run: source venv/bin/activate && python test_sync_direct.py
"""
import logging
logging.basicConfig(level=logging.DEBUG)

import sys, os
sys.path.insert(0, os.path.dirname(__file__))

# Test the query directly
from google.ads.googleads.client import GoogleAdsClient
from config import get_settings

settings = get_settings()
customer_id = settings.google_ads_customer_id.replace("-", "")
print(f"Customer: {customer_id}")

client = GoogleAdsClient.load_from_dict({
    "developer_token": settings.google_ads_developer_token,
    "client_id": settings.google_ads_client_id,
    "client_secret": settings.google_ads_client_secret,
    "refresh_token": settings.google_ads_refresh_token,
    "login_customer_id": settings.google_ads_login_customer_id,
    "use_proto_plus": True,
})

ga_service = client.get_service("GoogleAdsService")

days = 30
query = f"""
    SELECT
        search_term_view.search_term,
        ad_group_criterion.keyword.text,
        ad_group_criterion.keyword.match_type,
        campaign.id,
        campaign.name,
        ad_group.name,
        metrics.clicks,
        metrics.conversions
    FROM search_term_view
    WHERE
        segments.date DURING LAST_{days}_DAYS
        AND metrics.clicks > 0
    ORDER BY metrics.clicks DESC
"""

print(f"\nRunning query against customer {customer_id}...")
try:
    response = ga_service.search(customer_id=customer_id, query=query)
    rows = list(response)
    print(f"Got {len(rows)} rows")
    for r in rows[:10]:
        print(f"  term='{r.search_term_view.search_term}' | kw='{r.ad_group_criterion.keyword.text}' | ag='{r.ad_group.name}' | campaign='{r.campaign.name}' | clicks={r.metrics.clicks}")
except Exception as e:
    print(f"ERROR: {e}")

# Now test the actual function
print("\n--- Skipping API query test ---
if False:")
from google_ads_sync import sync_call_search_terms
n = sync_call_search_terms(days=30)
print(f"sync_call_search_terms returned: {n}")

# Check DB state
print("\n--- Checking gads_call_search_terms table ---")
import sqlite3, os
db_path = os.path.join(os.path.dirname(__file__), "pipeline.db")
with sqlite3.connect(db_path) as conn:
    rows = conn.execute("SELECT COUNT(*) FROM gads_call_search_terms").fetchone()
    print(f"Rows in gads_call_search_terms: {rows[0]}")

    rows = conn.execute("SELECT search_term, campaign_name, ad_group_name FROM gads_call_search_terms LIMIT 5").fetchall()
    for r in rows:
        print(f"  {r}")

# Check backfill state
print("\n--- Checking attributed_keyword on gads_call_view ---")
with sqlite3.connect(db_path) as conn:
    total = conn.execute("SELECT COUNT(*) FROM gads_call_view").fetchone()[0]
    with_kw = conn.execute("SELECT COUNT(*) FROM gads_call_view WHERE attributed_keyword != '' AND attributed_keyword IS NOT NULL").fetchone()[0]
    print(f"Total call_view rows: {total}, with keyword: {with_kw}")
    rows = conn.execute("SELECT call_id, attributed_keyword, attributed_keyword_method, attributed_ad_group FROM gads_call_view WHERE attributed_keyword != '' LIMIT 5").fetchall()
    for r in rows:
        print(f"  {r}")
