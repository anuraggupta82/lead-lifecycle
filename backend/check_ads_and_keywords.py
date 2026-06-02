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

# Query active campaigns' ads, their status, approval status, policy topics
query_ads = """
    SELECT
        campaign.name,
        ad_group.name,
        ad_group_ad.ad.id,
        ad_group_ad.status,
        ad_group_ad.ad.type,
        ad_group_ad.policy_summary.approval_status,
        ad_group_ad.policy_summary.review_status,
        ad_group_ad.policy_summary.policy_topic_entries
    FROM ad_group_ad
    WHERE campaign.status = 'ENABLED'
        AND ad_group.status = 'ENABLED'
        AND ad_group_ad.status != 'REMOVED'
"""

response_ads = ga_service.search(customer_id=customer_id, query=query_ads)

ads_list = []
for row in response_ads:
    policy_topics = []
    if row.ad_group_ad.policy_summary.policy_topic_entries:
        for entry in row.ad_group_ad.policy_summary.policy_topic_entries:
            policy_topics.append({
                "topic": entry.topic,
                "type": entry.type_.name if entry.type_ else ""
            })
    
    ads_list.append({
        "campaign": row.campaign.name,
        "ad_group": row.ad_group.name,
        "ad_id": row.ad_group_ad.ad.id,
        "status": row.ad_group_ad.status.name,
        "ad_type": row.ad_group_ad.ad.type_.name,
        "approval_status": row.ad_group_ad.policy_summary.approval_status.name,
        "review_status": row.ad_group_ad.policy_summary.review_status.name,
        "policy_topics": policy_topics
    })

print("ADS STATUS AND POLICY SUMMARY:")
print(json.dumps(ads_list, indent=2))

# Query keywords that are active, their serving status and primary status, quality score
query_keywords = """
    SELECT
        campaign.name,
        ad_group.name,
        ad_group_criterion.criterion_id,
        ad_group_criterion.keyword.text,
        ad_group_criterion.keyword.match_type,
        ad_group_criterion.status,
        ad_group_criterion.system_serving_status,
        ad_group_criterion.primary_status,
        ad_group_criterion.quality_info.quality_score
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
        "criterion_id": row.ad_group_criterion.criterion_id,
        "keyword_text": row.ad_group_criterion.keyword.text,
        "match_type": row.ad_group_criterion.keyword.match_type.name,
        "status": row.ad_group_criterion.status.name,
        "system_serving_status": row.ad_group_criterion.system_serving_status.name,
        "primary_status": row.ad_group_criterion.primary_status.name,
        "quality_score": row.ad_group_criterion.quality_info.quality_score if row.ad_group_criterion.quality_info else None
    })

print("\nKEYWORDS STATUS:")
print(json.dumps(kws_list[:50], indent=2))  # print first 50
print(f"Total active/paused keywords fetched: {len(kws_list)}")
