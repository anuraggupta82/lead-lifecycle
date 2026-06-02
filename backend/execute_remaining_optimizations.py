import os
import json
from google.ads.googleads.client import GoogleAdsClient
from config import get_settings
from ai_optimizer import _execute_bid_change
from database import log_admin_manual_action, update_gads_action_result, set_audit_approval

# Initialize Google Ads client
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

print("STARTING GOOGLE ADS REMAINING OPTIMIZATION EXECUTION...")

gd_campaign_resource = "customers/2498049505/campaigns/23849370858"

gd_keyword_adjustments = [
    {"keyword": '"same day dental appointment near me"', "match_type": "PHRASE", "new_bid": 35.0},
    {"keyword": "[same day dentist near me]", "match_type": "EXACT", "new_bid": 25.0}
]

for adj in gd_keyword_adjustments:
    # Query keyword criterion resource
    safe_kw = adj["keyword"].replace("'", "\\'")
    query = f"""
        SELECT
            ad_group_criterion.resource_name,
            ad_group_criterion.effective_cpc_bid_micros,
            ad_group.name
        FROM ad_group_criterion
        WHERE ad_group_criterion.type = 'KEYWORD'
            AND ad_group_criterion.status = 'ENABLED'
            AND ad_group_criterion.keyword.text = '{safe_kw}'
            AND ad_group_criterion.keyword.match_type = '{adj["match_type"]}'
            AND campaign.resource_name = '{gd_campaign_resource}'
    """
    
    rows = list(ga_service.search(customer_id=customer_id, query=query))
    if not rows:
        print(f"Keyword '{adj['keyword']}' ({adj['match_type']}) not found in General Dentistry campaign. Skipping.")
        continue
        
    for row in rows:
        kw_resource = row.ad_group_criterion.resource_name
        ag_name = row.ad_group.name
        old_bid_micros = row.ad_group_criterion.effective_cpc_bid_micros or 0
        new_bid_micros = int(adj["new_bid"] * 1_000_000)
        
        operation = "increase_bid" if new_bid_micros > old_bid_micros else "decrease_bid"
        
        action_id = log_admin_manual_action(
            operation=operation,
            entity_type="keyword",
            entity_id=kw_resource,
            entity_name=adj["keyword"],
            before={"cpc_bid_micros": old_bid_micros, "cpc_bid_usd": round(old_bid_micros / 1000000.0, 2)},
            after={"cpc_bid_micros": new_bid_micros, "cpc_bid_usd": adj["new_bid"]},
            reason="Adjust bid for under-bid high-quality keyword to match first-page CPC estimate",
        )
        
        try:
            _execute_bid_change(client, customer_id, kw_resource, new_bid_micros)
            print(f"Updated '{adj['keyword']}' ({adj['match_type']}) in ad group '{ag_name}' to ${adj['new_bid']:.2f}")
            update_gads_action_result(action_id, executed=True, execution_result="success")
            set_audit_approval(action_id, "admin")
        except Exception as e:
            print(f"Error updating bid for keyword '{adj['keyword']}': {e}")
            update_gads_action_result(action_id, executed=True, execution_result="error", error_detail=str(e))

print("\nEXECUTION COMPLETE!")
