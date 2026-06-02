import os
import json
from datetime import datetime, timedelta
from google.ads.googleads.client import GoogleAdsClient
from config import get_settings

settings = get_settings()
credentials = {
    "developer_token": settings.google_ads_developer_token,
    "refresh_token": settings.google_ads_refresh_token,
    "client_id": settings.google_ads_client_id,
    "client_secret": settings.google_ads_client_secret,
    "login_customer_id": settings.google_ads_login_customer_id,
    "use_proto_plus": True
}
client = GoogleAdsClient.load_from_dict(credentials)
customer_id = settings.google_ads_customer_id

ga_service = client.get_service("GoogleAdsService")

# Query search terms from May 24 to May 31 segmented by date
query_search_terms = """
    SELECT
        campaign.name,
        ad_group.name,
        search_term_view.search_term,
        segments.date,
        metrics.impressions,
        metrics.clicks,
        metrics.cost_micros
    FROM search_term_view
    WHERE segments.date BETWEEN '2026-05-24' AND '2026-05-31'
        AND campaign.status = 'ENABLED'
        AND ad_group.status = 'ENABLED'
    ORDER BY segments.date DESC, metrics.impressions DESC
"""

response = ga_service.search(customer_id=customer_id, query=query_search_terms)

terms = []
for row in response:
    terms.append({
        "campaign": row.campaign.name,
        "ad_group": row.ad_group.name,
        "search_term": row.search_term_view.search_term,
        "date": row.segments.date,
        "impressions": row.metrics.impressions,
        "clicks": row.metrics.clicks,
        "cost": row.metrics.cost_micros / 1000000.0
    })

print(f"Total search term rows fetched: {len(terms)}")
print("\nSearch terms on or after May 29 (Post-change):")
post_change_terms = [t for t in terms if t["date"] >= "2026-05-29"]
print(json.dumps(post_change_terms[:30], indent=2))

print("\nSearch terms before May 29 (Pre-change, top 30 by impressions):")
pre_change_terms = [t for t in terms if t["date"] < "2026-05-29"]
pre_change_terms.sort(key=lambda x: x["impressions"], reverse=True)
print(json.dumps(pre_change_terms[:30], indent=2))
