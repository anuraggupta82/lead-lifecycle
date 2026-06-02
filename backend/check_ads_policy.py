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

print(f"Total active ads fetched: {len(ads_list)}")
print("Disapproved or Limited Ads:")
disapproved_or_limited = [a for a in ads_list if a["approval_status"] not in ("APPROVED", "UNSPECIFIED", "UNKNOWN") or a["review_status"] == "UNDER_REVIEW" or len(a["policy_topics"]) > 0]
print(json.dumps(disapproved_or_limited, indent=2))

print("\nAll Active Ads Summary (approval status counts):")
counts = {}
for a in ads_list:
    key = (a["campaign"], a["approval_status"], a["review_status"])
    counts[key] = counts.get(key, 0) + 1

for key, count in counts.items():
    print(f"Campaign: {key[0]} | Approval: {key[1]} | Review: {key[2]} | Count: {count}")
