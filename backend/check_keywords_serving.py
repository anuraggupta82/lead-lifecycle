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

query_keywords = """
    SELECT
        campaign.name,
        ad_group.name,
        ad_group_criterion.keyword.text,
        ad_group_criterion.keyword.match_type,
        ad_group_criterion.status,
        ad_group_criterion.system_serving_status,
        ad_group_criterion.primary_status
    FROM ad_group_criterion
    WHERE campaign.status = 'ENABLED'
        AND ad_group.status = 'ENABLED'
        AND ad_group_criterion.type = 'KEYWORD'
        AND ad_group_criterion.status != 'REMOVED'
"""

response_kws = ga_service.search(customer_id=customer_id, query=query_keywords)

kws_list = []
for row in response_kws:
    kws_list.append({
        "campaign": row.campaign.name,
        "ad_group": row.ad_group.name,
        "keyword_text": row.ad_group_criterion.keyword.text,
        "match_type": row.ad_group_criterion.keyword.match_type.name,
        "status": row.ad_group_criterion.status.name,
        "system_serving_status": row.ad_group_criterion.system_serving_status.name,
        "primary_status": row.ad_group_criterion.primary_status.name
    })

print(f"Total active/paused keywords in active campaigns: {len(kws_list)}")

# Group by campaign, status, system_serving_status, primary_status
groups = {}
for kw in kws_list:
    key = (kw["campaign"], kw["status"], kw["system_serving_status"], kw["primary_status"])
    groups[key] = groups.get(key, 0) + 1

print("\nKeyword Serving Status breakdown:")
for key, count in sorted(groups.items(), key=lambda x: (x[0][0], x[0][1], -x[1])):
    print(f"Campaign: {key[0]} | Status: {key[1]} | Serving: {key[2]} | Primary: {key[3]} | Count: {count}")
