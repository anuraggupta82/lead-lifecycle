"""
Diagnostic: test different GAQL queries against search_term_view
to find what actually returns data for call-related search terms.

Run from backend/ dir:
  source venv/bin/activate && python test_gads_search_terms.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from google.ads.googleads.client import GoogleAdsClient
from config import get_settings

settings = get_settings()
customer_id = settings.google_ads_customer_id.replace("-", "")
print(f"Customer ID: {customer_id}\n")

client = GoogleAdsClient.load_from_dict({
    "developer_token": settings.google_ads_developer_token,
    "client_id": settings.google_ads_client_id,
    "client_secret": settings.google_ads_client_secret,
    "refresh_token": settings.google_ads_refresh_token,
    "login_customer_id": settings.google_ads_login_customer_id,
    "use_proto_plus": True,
})

ga_service = client.get_service("GoogleAdsService")

# Test 1: plain search_term_view — all terms, no segment filter
print("=== Test 1: All search terms (no click_type filter) ===")
q1 = """
    SELECT
        search_term_view.search_term,
        campaign.name,
        ad_group.name,
        metrics.clicks,
        metrics.impressions
    FROM search_term_view
    WHERE segments.date DURING LAST_30_DAYS
      AND metrics.clicks > 0
    ORDER BY metrics.clicks DESC
    LIMIT 10
"""
try:
    rows = list(ga_service.search(customer_id=customer_id, query=q1))
    print(f"  Got {len(rows)} rows")
    for r in rows[:5]:
        print(f"  '{r.search_term_view.search_term}' | campaign={r.campaign.name} | clicks={r.metrics.clicks}")
except Exception as e:
    print(f"  ERROR: {e}")

# Test 2: search_term_view with click_type in SELECT (segment pull)
print("\n=== Test 2: search_term_view with click_type in SELECT ===")
q2 = """
    SELECT
        search_term_view.search_term,
        campaign.name,
        segments.click_type,
        metrics.clicks
    FROM search_term_view
    WHERE segments.date DURING LAST_30_DAYS
      AND metrics.clicks > 0
    LIMIT 10
"""
try:
    rows = list(ga_service.search(customer_id=customer_id, query=q2))
    print(f"  Got {len(rows)} rows")
    for r in rows[:5]:
        print(f"  '{r.search_term_view.search_term}' | click_type={r.segments.click_type} | clicks={r.metrics.clicks}")
except Exception as e:
    print(f"  ERROR: {e}")

# Test 3: click_type IN WHERE — what we tried
print("\n=== Test 3: click_type IN WHERE clause ===")
q3 = """
    SELECT
        search_term_view.search_term,
        campaign.name,
        segments.click_type,
        metrics.clicks
    FROM search_term_view
    WHERE segments.date DURING LAST_30_DAYS
      AND segments.click_type IN ('CALLS', 'MOBILE_CALL_TRACKING')
      AND metrics.clicks > 0
    LIMIT 10
"""
try:
    rows = list(ga_service.search(customer_id=customer_id, query=q3))
    print(f"  Got {len(rows)} rows")
    for r in rows[:5]:
        print(f"  '{r.search_term_view.search_term}' | click_type={r.segments.click_type} | clicks={r.metrics.clicks}")
except Exception as e:
    print(f"  ERROR: {e}")

# Test 4: call_view — actual call records with ad_group
print("\n=== Test 4: call_view (to confirm ad_group is available) ===")
q4 = """
    SELECT
        call_view.call_status,
        call_view.duration_seconds,
        call_view.start_call_date_time,
        campaign.name,
        ad_group.name,
        segments.date
    FROM call_view
    WHERE segments.date DURING LAST_30_DAYS
    ORDER BY segments.date DESC
    LIMIT 10
"""
try:
    rows = list(ga_service.search(customer_id=customer_id, query=q4))
    print(f"  Got {len(rows)} rows")
    for r in rows[:5]:
        print(f"  status={r.call_view.call_status} | dur={r.call_view.duration_seconds}s | campaign={r.campaign.name} | ag={r.ad_group.name} | date={r.segments.date}")
except Exception as e:
    print(f"  ERROR: {e}")

# Test 5: all unique click_types seen in search_term_view
print("\n=== Test 5: What click_types exist in search_term_view? ===")
q5 = """
    SELECT
        segments.click_type,
        metrics.clicks
    FROM search_term_view
    WHERE segments.date DURING LAST_30_DAYS
      AND metrics.clicks > 0
    ORDER BY metrics.clicks DESC
    LIMIT 50
"""
try:
    rows = list(ga_service.search(customer_id=customer_id, query=q5))
    print(f"  Got {len(rows)} rows total")
    click_types = {}
    for r in rows:
        ct = str(r.segments.click_type)
        click_types[ct] = click_types.get(ct, 0) + r.metrics.clicks
    for ct, total in sorted(click_types.items(), key=lambda x: -x[1]):
        print(f"  click_type={ct} => {total} clicks")
except Exception as e:
    print(f"  ERROR: {e}")

print("\nDone.")
