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

query_ag = """
    SELECT
        ad_group.id,
        ad_group.name,
        ad_group.status,
        ad_group.cpc_bid_micros
    FROM ad_group
    WHERE campaign.id = 23834204777
        AND ad_group.status != 'REMOVED'
"""

response = ga_service.search(customer_id=customer_id, query=query_ag)

ad_groups = []
for row in response:
    ad_groups.append({
        "id": row.ad_group.id,
        "name": row.ad_group.name,
        "status": row.ad_group.status.name,
        "default_cpc_usd": row.ad_group.cpc_bid_micros / 1000000.0 if row.ad_group.cpc_bid_micros else None
    })

print("AD GROUPS FOR EMERGENCY DENTISTRY:")
print(json.dumps(ad_groups, indent=2))
