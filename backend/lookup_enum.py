import os
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

# Let's inspect the CampaignPrimaryStatusReason enum values
reason_enum = client.get_type("CampaignPrimaryStatusReasonEnum").CampaignPrimaryStatusReason
for attr in dir(reason_enum):
    if not attr.startswith("_") and not attr.islower():
        val = getattr(reason_enum, attr)
        print(f"{val}: {attr}")
