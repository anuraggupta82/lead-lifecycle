import os
import json
from google.ads.googleads.client import GoogleAdsClient
from config import get_settings
from google_ads_write import replace_campaign_locations, _customer_id_from_resource
from ai_optimizer import _execute_bid_change
from database import log_admin_manual_action, update_gads_action_result, set_audit_approval
from google.protobuf import field_mask_pb2

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

print("STARTING GOOGLE ADS OPTIMIZATION EXECUTION...")

# Campaigns resources
gd_campaign_resource = "customers/2498049505/campaigns/23849370858"
nxt_campaign_resource = "customers/2498049505/campaigns/23870298927"

# -------------------------------------------------------------
# TASK 1: Update General Dentistry geographic radius to 15 miles
# -------------------------------------------------------------
print("\n--- TASK 1: Updating General Dentistry geographic radius to 15 miles ---")
geo_json = {
    "unit": "miles",
    "locations": [
        {
            "type": "city",
            "value": "Grafton, MA",
            "radius": 15.0,
            "include": True
        }
    ]
}
geo_json_str = json.dumps(geo_json)

# Log action in database
action_id_geo = log_admin_manual_action(
    operation="set_campaign_locations",
    entity_type="campaign",
    entity_id=gd_campaign_resource,
    entity_name="General Dentistry New Landing Page (05/16 16:42)",
    before={"geographic_targeting": "radius=5mi"},
    after={"geo_json": geo_json_str},
    reason="Expand targeting radius to 15 miles to resolve physical location restriction conflict",
)

try:
    result_geo = replace_campaign_locations(gd_campaign_resource, geo_json_str)
    print(f"Success! Geo locations updated: {result_geo}")
    update_gads_action_result(action_id_geo, executed=True, execution_result="success")
    set_audit_approval(action_id_geo, "admin")
except Exception as e:
    print(f"Error updating geo locations: {e}")
    update_gads_action_result(action_id_geo, executed=True, execution_result="error", error_detail=str(e))


# -------------------------------------------------------------
# TASK 2: Adjust keyword bids for General Dentistry New Landing Page
# -------------------------------------------------------------
print("\n--- TASK 2: Adjusting General Dentistry keyword bids ---")

gd_keyword_adjustments = [
    {"keyword": "same day dental appointment near me", "match_type": "PHRASE", "new_bid": 35.0},
    {"keyword": "same day dentist near me", "match_type": "EXACT", "new_bid": 25.0},
    {"keyword": "general dentist near me", "match_type": "PHRASE", "new_bid": 16.0}
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


# -------------------------------------------------------------
# TASK 3: Increase ad group bids for nXtsmile Implants (AG-2 and AG-3) to $20.00
# -------------------------------------------------------------
print("\n--- TASK 3: Adjusting nXtsmile Implants ad group default CPC bids ---")

ad_group_bid_adjustments = [
    {"name": "Dental Implants Cost Comparison", "new_bid": 20.0},
    {"name": "Implant Dentist Near Me Worcester", "new_bid": 20.0}
]

ag_service = client.get_service("AdGroupService")

for adj in ad_group_bid_adjustments:
    # Query ad group resource name
    safe_ag = adj["name"].replace("'", "\\'")
    query = f"""
        SELECT
            ad_group.resource_name,
            ad_group.cpc_bid_micros
        FROM ad_group
        WHERE ad_group.name = '{safe_ag}'
            AND campaign.resource_name = '{nxt_campaign_resource}'
    """
    
    rows = list(ga_service.search(customer_id=customer_id, query=query))
    if not rows:
        print(f"Ad group '{adj['name']}' not found in nXtsmile campaign. Skipping.")
        continue
        
    row = rows[0]
    ag_resource = row.ad_group.resource_name
    old_bid_micros = row.ad_group.cpc_bid_micros or 0
    new_bid_micros = int(adj["new_bid"] * 1_000_000)
    
    action_id = log_admin_manual_action(
        operation="update_ad_group_bid",
        entity_type="ad_group",
        entity_id=ag_resource,
        entity_name=adj["name"],
        before={"cpc_bid_micros": old_bid_micros, "cpc_bid_usd": round(old_bid_micros / 1000000.0, 2)},
        after={"cpc_bid_micros": new_bid_micros, "cpc_bid_usd": adj["new_bid"]},
        reason="Increase ad group default CPC bid to match implant keyword first-page CPC estimates",
    )
    
    try:
        operation = client.get_type("AdGroupOperation")
        ad_group = operation.update
        ad_group.resource_name = ag_resource
        ad_group.cpc_bid_micros = new_bid_micros
        client.copy_from(
            operation.update_mask,
            field_mask_pb2.FieldMask(paths=["cpc_bid_micros"])
        )
        ag_service.mutate_ad_groups(
            customer_id=customer_id,
            operations=[operation]
        )
        print(f"Updated ad group '{adj['name']}' default CPC bid to ${adj['new_bid']:.2f}")
        update_gads_action_result(action_id, executed=True, execution_result="success")
        set_audit_approval(action_id, "admin")
    except Exception as e:
        print(f"Error updating bid for ad group '{adj['name']}': {e}")
        update_gads_action_result(action_id, executed=True, execution_result="error", error_detail=str(e))


# -------------------------------------------------------------
# TASK 4: Adjust specific keyword bids for nXtsmile Implants
# -------------------------------------------------------------
print("\n--- TASK 4: Adjusting nXtsmile Implants specific keyword bids ---")

nxt_keyword_adjustments = [
    {"keyword": "teeth implants cost", "match_type": "PHRASE", "new_bid": 40.0},
    {"keyword": "tooth implant cost near me", "match_type": "EXACT", "new_bid": 32.0},
    {"keyword": "same day teeth implants", "match_type": "PHRASE", "new_bid": 30.0},
    {"keyword": "dental implants near me", "match_type": "EXACT", "new_bid": 24.0}
]

for adj in nxt_keyword_adjustments:
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
            AND campaign.resource_name = '{nxt_campaign_resource}'
    """
    
    rows = list(ga_service.search(customer_id=customer_id, query=query))
    if not rows:
        print(f"Keyword '{adj['keyword']}' ({adj['match_type']}) not found in nXtsmile campaign. Skipping.")
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
            reason="Adjust bid for under-bid high-quality implant keyword to match first-page CPC estimate",
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
