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

query_geo = """
    SELECT
        campaign.name,
        campaign_criterion.criterion_id,
        campaign_criterion.location.geo_target_constant,
        campaign_criterion.negative,
        campaign_criterion.status
    FROM campaign_criterion
    WHERE campaign.status = 'ENABLED'
        AND campaign_criterion.type = 'LOCATION'
        AND campaign_criterion.status != 'REMOVED'
"""

response_geo = ga_service.search(customer_id=customer_id, query=query_geo)

criterions = []
geo_ids = []
for row in response_geo:
    constant = row.campaign_criterion.location.geo_target_constant
    criterion_id = constant.split("/geoTargetConstants/")[-1] if "/geoTargetConstants/" in constant else ""
    criterions.append({
        "campaign": row.campaign.name,
        "criterion_id": row.campaign_criterion.criterion_id,
        "location_constant": constant,
        "geo_id": criterion_id,
        "negative": row.campaign_criterion.negative,
        "status": row.campaign_criterion.status.name
    })
    if criterion_id:
        geo_ids.append(criterion_id)

# Resolve names
from google_ads_sync import _fetch_geo_target_names
name_map = _fetch_geo_target_names(client, customer_id, geo_ids)

for c in criterions:
    gid = c["geo_id"]
    c["name"] = name_map.get(gid, f"geo:{gid}")

print("GEOGRAPHIC TARGETING CRITERIA:")
print(json.dumps(criterions, indent=2))
