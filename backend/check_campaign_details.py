import os
import json
from datetime import datetime, timedelta
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

# Query daily performance for last 8 days (including today)
query_daily = """
    SELECT
        campaign.id,
        campaign.name,
        segments.date,
        metrics.impressions,
        metrics.clicks,
        metrics.cost_micros,
        metrics.conversions
    FROM campaign
    WHERE segments.date BETWEEN '{}' AND '{}'
        AND campaign.status = 'ENABLED'
    ORDER BY segments.date DESC, campaign.name
"""

# Let's run from 7 days ago until today (2026-05-31)
end_date = "2026-05-31"
start_date = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")

formatted_query = query_daily.format(start_date, end_date)
response = ga_service.search(customer_id=customer_id, query=formatted_query)

daily_results = []
for row in response:
    daily_results.append({
        "campaign_id": row.campaign.id,
        "name": row.campaign.name,
        "date": row.segments.date,
        "impressions": row.metrics.impressions,
        "clicks": row.metrics.clicks,
        "cost": row.metrics.cost_micros / 1000000.0,
        "conversions": row.metrics.conversions
    })

print("DAILY STATS LAST 8 DAYS:")
print(json.dumps(daily_results, indent=2))
