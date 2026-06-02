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

query_schedule = """
    SELECT
        campaign.name,
        campaign_criterion.ad_schedule.day_of_week,
        campaign_criterion.ad_schedule.start_hour,
        campaign_criterion.ad_schedule.start_minute,
        campaign_criterion.ad_schedule.end_hour,
        campaign_criterion.ad_schedule.end_minute,
        campaign_criterion.bid_modifier
    FROM campaign_criterion
    WHERE campaign.status = 'ENABLED'
        AND campaign_criterion.type = 'AD_SCHEDULE'
        AND campaign_criterion.status != 'REMOVED'
"""

response = ga_service.search(customer_id=customer_id, query=query_schedule)

schedules = []
for row in response:
    sched = row.campaign_criterion.ad_schedule
    schedules.append({
        "campaign": row.campaign.name,
        "day_of_week": sched.day_of_week.name if sched.day_of_week else None,
        "start": f"{sched.start_hour:02d}:{sched.start_minute:02d}",
        "end": f"{sched.end_hour:02d}:{sched.end_minute:02d}",
        "bid_modifier": row.campaign_criterion.bid_modifier
    })

print("AD SCHEDULES FOR ENABLED CAMPAIGNS:")
print(json.dumps(schedules, indent=2))
