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

query_network = """
    SELECT
        campaign.name,
        segments.ad_network_type,
        segments.date,
        metrics.impressions,
        metrics.clicks,
        metrics.cost_micros
    FROM campaign
    WHERE segments.date BETWEEN '2026-05-27' AND '2026-05-31'
        AND campaign.status = 'ENABLED'
"""

response = ga_service.search(customer_id=customer_id, query=query_network)

network_data = []
for row in response:
    network_data.append({
        "campaign": row.campaign.name,
        "network": row.segments.ad_network_type.name,
        "date": row.segments.date,
        "impressions": row.metrics.impressions,
        "clicks": row.metrics.clicks,
        "cost": row.metrics.cost_micros / 1000000.0
    })

print("PERFORMANCE BY AD NETWORK TYPE:")
print(json.dumps(network_data, indent=2))
