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

# Query keyword-level daily metrics for last 4 days
query_kw_daily = """
    SELECT
        campaign.name,
        ad_group.name,
        ad_group_criterion.keyword.text,
        ad_group_criterion.keyword.match_type,
        segments.date,
        metrics.impressions,
        metrics.clicks,
        metrics.cost_micros
    FROM keyword_view
    WHERE segments.date BETWEEN '{}' AND '{}'
        AND campaign.status = 'ENABLED'
        AND ad_group.status = 'ENABLED'
        AND ad_group_criterion.status != 'REMOVED'
    ORDER BY segments.date DESC, metrics.impressions DESC
"""

# May 28 to May 31
start_date = "2026-05-28"
end_date = "2026-05-31"

formatted_query = query_kw_daily.format(start_date, end_date)
response = ga_service.search(customer_id=customer_id, query=formatted_query)

daily_kws = []
for row in response:
    daily_kws.append({
        "campaign": row.campaign.name,
        "ad_group": row.ad_group.name,
        "keyword": row.ad_group_criterion.keyword.text,
        "match_type": row.ad_group_criterion.keyword.match_type.name,
        "date": row.segments.date,
        "impressions": row.metrics.impressions,
        "clicks": row.metrics.clicks,
        "cost": row.metrics.cost_micros / 1000000.0
    })

print("DAILY KEYWORD PERFORMANCE (May 28 - May 31):")
print(json.dumps(daily_kws, indent=2))
