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
        campaign.name,
        ad_group_criterion.keyword.text,
        ad_group_criterion.status,
        ad_group_criterion.cpc_bid_micros,
        metrics.impressions,
        metrics.clicks,
        metrics.search_rank_lost_impression_share
    FROM keyword_view
    WHERE campaign.name IN ('Emergency Dentistry (05/09 22:00)', 'nXtsmile Implants (05/23 — 100/day) (05/23 23:33)')
        AND ad_group_criterion.status IN ('ENABLED', 'PAUSED')
        AND segments.date DURING TODAY
    ORDER BY campaign.name, metrics.impressions DESC
"""

response = ga_service.search(customer_id=customer_id, query=query)

results = {}
for row in response:
    c_name = row.campaign.name
    if c_name not in results:
        results[c_name] = []
    
    bid = row.ad_group_criterion.cpc_bid_micros / 1000000.0 if row.ad_group_criterion.cpc_bid_micros else None
    results[c_name].append({
        "keyword": row.ad_group_criterion.keyword.text,
        "status": row.ad_group_criterion.status.name,
        "bid": bid,
        "impressions": row.metrics.impressions,
        "clicks": row.metrics.clicks,
        "rank_lost_is": row.metrics.search_rank_lost_impression_share
    })

print(json.dumps(results, indent=2))

query2 = """
    SELECT
        campaign.name,
        campaign.bidding_strategy_type
    FROM campaign
    WHERE campaign.name IN ('Emergency Dentistry (05/09 22:00)', 'nXtsmile Implants (05/23 — 100/day) (05/23 23:33)')
"""
resp2 = ga_service.search(customer_id=customer_id, query=query2)
for row in resp2:
    print(f"Bidding Strategy for {row.campaign.name}: {row.campaign.bidding_strategy_type.name}")
