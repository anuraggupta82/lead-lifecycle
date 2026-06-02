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

query_criteria = """
    SELECT
        campaign.name,
        campaign_criterion.type,
        campaign_criterion.negative,
        campaign_criterion.status,
        campaign_criterion.location.geo_target_constant,
        campaign_criterion.proximity.radius,
        campaign_criterion.proximity.radius_units,
        campaign_criterion.proximity.address.city_name,
        campaign_criterion.proximity.address.postal_code
    FROM campaign_criterion
    WHERE campaign.status = 'ENABLED'
        AND campaign_criterion.status != 'REMOVED'
"""

response = ga_service.search(customer_id=customer_id, query=query_criteria)

criteria_list = []
for row in response:
    criteria_list.append({
        "campaign": row.campaign.name,
        "type": row.campaign_criterion.type_.name,
        "negative": row.campaign_criterion.negative,
        "status": row.campaign_criterion.status.name,
        "location": row.campaign_criterion.location.geo_target_constant if row.campaign_criterion.location else None,
        "proximity": {
            "radius": row.campaign_criterion.proximity.radius if row.campaign_criterion.proximity else None,
            "units": row.campaign_criterion.proximity.radius_units.name if row.campaign_criterion.proximity and row.campaign_criterion.proximity.radius_units else None,
            "city": row.campaign_criterion.proximity.address.city_name if row.campaign_criterion.proximity and row.campaign_criterion.proximity.address else None,
            "zip": row.campaign_criterion.proximity.address.postal_code if row.campaign_criterion.proximity and row.campaign_criterion.proximity.address else None
        }
    })

print("ALL CAMPAIGN CRITERIA:")
print(json.dumps(criteria_list, indent=2))
