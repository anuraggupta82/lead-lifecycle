import os
import json
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

query = """
    SELECT
        campaign.id,
        campaign.name,
        campaign.status,
        metrics.impressions,
        metrics.clicks,
        metrics.cost_micros,
        metrics.conversions
    FROM campaign
    WHERE segments.date DURING LAST_30_DAYS
        AND campaign.status != 'REMOVED'
    ORDER BY metrics.cost_micros DESC
"""

response = ga_service.search(customer_id=customer_id, query=query)

results = []
for row in response:
    results.append({
        "campaign_id": row.campaign.id,
        "name": row.campaign.name,
        "status": row.campaign.status.name,
        "impressions": row.metrics.impressions,
        "clicks": row.metrics.clicks,
        "cost": row.metrics.cost_micros / 1000000.0,
        "conversions": row.metrics.conversions
    })
print(json.dumps(results, indent=2))
