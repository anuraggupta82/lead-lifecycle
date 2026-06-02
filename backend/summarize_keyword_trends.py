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
    WHERE segments.date BETWEEN '2026-05-28' AND '2026-05-31'
        AND campaign.status = 'ENABLED'
        AND ad_group.status = 'ENABLED'
        AND ad_group_criterion.status != 'REMOVED'
"""

response = ga_service.search(customer_id=customer_id, query=query_kw_daily)

by_date = {}
for row in response:
    date = row.segments.date
    if date not in by_date:
        by_date[date] = []
    by_date[date].append({
        "campaign": row.campaign.name,
        "ad_group": row.ad_group.name,
        "keyword": row.ad_group_criterion.keyword.text,
        "match_type": row.ad_group_criterion.keyword.match_type.name,
        "impressions": row.metrics.impressions,
        "clicks": row.metrics.clicks,
        "cost": row.metrics.cost_micros / 1000000.0
    })

for date in sorted(by_date.keys(), reverse=True):
    print(f"\n--- DATE: {date} ---")
    kws = by_date[date]
    # sort by impressions desc
    kws.sort(key=lambda x: x["impressions"], reverse=True)
    for kw in kws[:15]:  # show top 15
        print(f"[{kw['campaign'][:15]}] {kw['keyword']} ({kw['match_type']}): {kw['impressions']} imps, {kw['clicks']} clicks, ${kw['cost']:.2f}")
    if len(kws) > 15:
        print(f"... and {len(kws) - 15} more keywords")
