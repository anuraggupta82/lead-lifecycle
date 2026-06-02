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

# Query campaigns with status, primary status, budget and bidding strategy info
query_status = """
    SELECT
        campaign.id,
        campaign.name,
        campaign.status,
        campaign.serving_status,
        campaign.primary_status,
        campaign.primary_status_reasons,
        campaign.bidding_strategy_type,
        campaign_budget.amount_micros,
        campaign_budget.status,
        campaign_budget.delivery_method
    FROM campaign
    WHERE campaign.status != 'REMOVED'
"""

response = ga_service.search(customer_id=customer_id, query=query_status)

campaign_details = []
for row in response:
    reasons = [str(r) for r in row.campaign.primary_status_reasons] if row.campaign.primary_status_reasons else []
    campaign_details.append({
        "campaign_id": row.campaign.id,
        "name": row.campaign.name,
        "status": row.campaign.status.name,
        "serving_status": row.campaign.serving_status.name,
        "primary_status": row.campaign.primary_status.name,
        "primary_status_reasons": reasons,
        "bidding_strategy_type": row.campaign.bidding_strategy_type.name,
        "budget_usd": row.campaign_budget.amount_micros / 1000000.0,
        "budget_status": row.campaign_budget.status.name,
        "budget_delivery": row.campaign_budget.delivery_method.name
    })

print("CAMPAIGN STATUS & SETTINGS:")
print(json.dumps(campaign_details, indent=2))
