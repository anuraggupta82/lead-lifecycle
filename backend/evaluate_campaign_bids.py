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

# Query active keywords and bid estimates for the two campaigns
query_bids = """
    SELECT
        campaign.name,
        ad_group.name,
        ad_group_criterion.keyword.text,
        ad_group_criterion.keyword.match_type,
        ad_group_criterion.status,
        ad_group_criterion.system_serving_status,
        ad_group_criterion.primary_status,
        ad_group_criterion.effective_cpc_bid_micros,
        ad_group_criterion.quality_info.quality_score,
        ad_group_criterion.position_estimates.first_page_cpc_micros,
        ad_group_criterion.position_estimates.top_of_page_cpc_micros,
        ad_group_criterion.position_estimates.first_position_cpc_micros
    FROM ad_group_criterion
    WHERE campaign.id IN (23849370858, 23870298927)
        AND ad_group_criterion.type = 'KEYWORD'
        AND ad_group_criterion.status = 'ENABLED'
"""

response = ga_service.search(customer_id=customer_id, query=query_bids)

keywords_evaluation = []
for row in response:
    kw = row.ad_group_criterion
    pe = kw.position_estimates
    
    bid = kw.effective_cpc_bid_micros / 1000000.0 if kw.effective_cpc_bid_micros else None
    first_page = pe.first_page_cpc_micros / 1000000.0 if pe.first_page_cpc_micros else None
    top_of_page = pe.top_of_page_cpc_micros / 1000000.0 if pe.top_of_page_cpc_micros else None
    first_pos = pe.first_position_cpc_micros / 1000000.0 if pe.first_position_cpc_micros else None
    
    under_bid = False
    if bid and first_page:
        under_bid = bid < first_page
        
    keywords_evaluation.append({
        "campaign": row.campaign.name,
        "ad_group": row.ad_group.name,
        "keyword": kw.keyword.text,
        "match_type": kw.keyword.match_type.name,
        "bid": bid,
        "first_page_est": first_page,
        "top_of_page_est": top_of_page,
        "first_pos_est": first_pos,
        "under_bid": under_bid,
        "quality_score": kw.quality_info.quality_score if kw.quality_info else None,
        "serving_status": kw.system_serving_status.name,
        "primary_status": kw.primary_status.name
    })

# Write to json file
with open("keywords_evaluation.json", "w") as f:
    json.dump(keywords_evaluation, f, indent=2)

print(f"Total active keywords evaluated: {len(keywords_evaluation)}")

# Summary stats per campaign
campaigns = [
    "General Dentistry New Landing Page (05/16 16:42)",
    "nXtsmile Implants (05/23 — 100/day) (05/23 23:33)"
]

for c in campaigns:
    c_kws = [k for k in keywords_evaluation if k["campaign"] == c]
    print(f"\n--- CAMPAIGN: {c} ---")
    print(f"Total active keywords: {len(c_kws)}")
    under_bid_kws = [k for k in c_kws if k["under_bid"]]
    print(f"Under-bid keywords (below first page estimate): {len(under_bid_kws)}")
    rarely_served = [k for k in c_kws if k["serving_status"] == "RARELY_SERVED"]
    print(f"Rarely served keywords (low search volume): {len(rarely_served)}")
    
    if under_bid_kws:
        print("\nTop 10 Under-Bid Keywords:")
        under_bid_kws.sort(key=lambda x: x["first_page_est"] or 0, reverse=True)
        for k in under_bid_kws[:10]:
            print(f" - {k['keyword']} ({k['match_type']}) | Bid: ${k['bid']:.2f} | First Page Est: ${k['first_page_est']:.2f} | Gap: ${k['first_page_est'] - k['bid']:.2f}")
            
    # Keywords with serving issues
    if rarely_served:
        print("\nTop 10 Rarely Served Keywords:")
        for k in rarely_served[:10]:
            print(f" - {k['keyword']} ({k['match_type']}) | Bid: ${k['bid']} | Quality Score: {k['quality_score']}")
