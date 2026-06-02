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

query_bids = """
    SELECT
        ad_group.name,
        ad_group_criterion.keyword.text,
        ad_group_criterion.keyword.match_type,
        ad_group_criterion.status,
        ad_group_criterion.cpc_bid_micros
    FROM ad_group_criterion
    WHERE campaign.id = 23834204777
        AND ad_group_criterion.type = 'KEYWORD'
        AND ad_group_criterion.status = 'ENABLED'
"""

response = ga_service.search(customer_id=customer_id, query=query_bids)

keywords_bids = []
for row in response:
    keywords_bids.append({
        "ad_group": row.ad_group.name,
        "keyword": row.ad_group_criterion.keyword.text,
        "match_type": row.ad_group_criterion.keyword.match_type.name,
        "status": row.ad_group_criterion.status.name,
        "bid_usd": row.ad_group_criterion.cpc_bid_micros / 1000000.0 if row.ad_group_criterion.cpc_bid_micros else None
    })

print("KEYWORD BIDS FOR EMERGENCY DENTISTRY:")
print(json.dumps(keywords_bids, indent=2))
print(f"Total enabled keywords: {len(keywords_bids)}")
