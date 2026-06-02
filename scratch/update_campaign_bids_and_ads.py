import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../backend")))

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.abspath(os.path.join(os.path.dirname(__file__), "../backend/.env")))

from google.ads.googleads.client import GoogleAdsClient
from google.protobuf.field_mask_pb2 import FieldMask
from database import log_admin_manual_action, update_gads_action_result, set_audit_approval

CUSTOMER_ID = os.getenv("GOOGLE_ADS_CUSTOMER_ID", "").replace("-", "")

CAMPAIGN_RESOURCE = "customers/2498049505/campaigns/23834204777"
CAMPAIGN_NAME     = "Emergency Dentistry (05/09 22:00)"

def get_client():
    return GoogleAdsClient.load_from_dict({
        "developer_token":  os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN"),
        "client_id":        os.getenv("GOOGLE_ADS_CLIENT_ID"),
        "client_secret":    os.getenv("GOOGLE_ADS_CLIENT_SECRET"),
        "refresh_token":    os.getenv("GOOGLE_ADS_REFRESH_TOKEN"),
        "login_customer_id": os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "").replace("-", ""),
        "use_proto_plus":   True,
    })

def update_ad_group_bid(client, ad_group_id, new_bid_usd):
    ad_group_service = client.get_service("AdGroupService")
    resource_name = f"customers/{CUSTOMER_ID}/adGroups/{ad_group_id}"
    
    action_id = log_admin_manual_action(
        operation="update_ad_group_bid",
        entity_type="ad_group",
        entity_id=resource_name,
        entity_name="Tooth Pain & Symptoms",
        before={"cpc_bid_micros": 3000000},
        after={"cpc_bid_micros": int(new_bid_usd * 1000000)},
        reason="Raise Tooth Pain & Symptoms default bid to $7 to match other ad groups and avoid keyword throttling",
    )
    
    try:
        op = client.get_type("AdGroupOperation")
        ad_group = op.update
        ad_group.resource_name = resource_name
        ad_group.cpc_bid_micros = int(new_bid_usd * 1000000)
        client.copy_from(op.update_mask, FieldMask(paths=["cpc_bid_micros"]))
        
        response = ad_group_service.mutate_ad_groups(
            customer_id=CUSTOMER_ID,
            operations=[op]
        )
        print(f"✓ Raised ad group bid for Tooth Pain & Symptoms to ${new_bid_usd:.2f}: {response.results[0].resource_name}")
        update_gads_action_result(action_id, executed=True, execution_result="success")
        set_audit_approval(action_id, "admin")
    except Exception as e:
        update_gads_action_result(action_id, executed=True, execution_result="error", error_detail=str(e))
        print(f"✗ Failed to raise bid: {e}")
        raise e

def pause_ad_group(client, ad_group_id, name):
    ad_group_service = client.get_service("AdGroupService")
    resource_name = f"customers/{CUSTOMER_ID}/adGroups/{ad_group_id}"
    
    action_id = log_admin_manual_action(
        operation="pause_ad_group",
        entity_type="ad_group",
        entity_id=resource_name,
        entity_name=name,
        before={"status": "ENABLED"},
        after={"status": "PAUSED"},
        reason="Pause Same-Day & Walk-In ad group as walk-ins are not seen and appointments are required",
    )
    
    try:
        op = client.get_type("AdGroupOperation")
        ad_group = op.update
        ad_group.resource_name = resource_name
        ad_group.status = client.enums.AdGroupStatusEnum.PAUSED
        client.copy_from(op.update_mask, FieldMask(paths=["status"]))
        
        response = ad_group_service.mutate_ad_groups(
            customer_id=CUSTOMER_ID,
            operations=[op]
        )
        print(f"✓ Paused ad group {name}: {response.results[0].resource_name}")
        update_gads_action_result(action_id, executed=True, execution_result="success")
        set_audit_approval(action_id, "admin")
    except Exception as e:
        update_gads_action_result(action_id, executed=True, execution_result="error", error_detail=str(e))
        print(f"✗ Failed to pause ad group {name}: {e}")
        raise e

def remove_walkin_headline_from_rsa(client, ad_group_id, ad_group_name, ad_id):
    ad_group_ad_service = client.get_service("AdGroupAdService")
    resource_name = f"customers/{CUSTOMER_ID}/adGroupAds/{ad_group_id}~{ad_id}"
    
    # Retrieve current ad to get all headlines/descriptions/urls
    ga_service = client.get_service("GoogleAdsService")
    query = f"""
        SELECT
          ad_group_ad.ad.responsive_search_ad.headlines,
          ad_group_ad.ad.responsive_search_ad.descriptions,
          ad_group_ad.ad.responsive_search_ad.path1,
          ad_group_ad.ad.responsive_search_ad.path2,
          ad_group_ad.ad.final_urls
        FROM ad_group_ad
        WHERE ad_group_ad.ad.id = {ad_id} AND ad_group.id = {ad_group_id}
    """
    search_request = client.get_type("SearchGoogleAdsRequest")
    search_request.customer_id = CUSTOMER_ID
    search_request.query = query
    
    response = list(ga_service.search(request=search_request))
    if not response:
        print(f"✗ Could not find ad {ad_id} in group {ad_group_name}")
        return
        
    ad_data = response[0].ad_group_ad.ad
    rsa = ad_data.responsive_search_ad
    
    old_headlines = [h.text for h in rsa.headlines]
    old_descriptions = [d.text for d in rsa.descriptions]
    
    # Filter headlines to exclude "Walk-Ins Welcome Today" (case insensitive)
    new_headlines_assets = []
    removed_any = False
    for h in rsa.headlines:
        if "walk-in" in h.text.lower() or "walk in" in h.text.lower():
            print(f"  Removing headline: '{h.text}' from {ad_group_name}")
            removed_any = True
        else:
            new_headlines_assets.append(h)
            
    if not removed_any:
        print(f"  No walk-in headlines found in {ad_group_name}")
        return
        
    action_id = log_admin_manual_action(
        operation="update_rsa",
        entity_type="ad_group_ad",
        entity_id=resource_name,
        entity_name=f"RSA in {ad_group_name}",
        before={"headlines": old_headlines},
        after={"headlines": [h.text for h in new_headlines_assets]},
        reason="Remove 'Walk-Ins Welcome Today' headline to align with appointment-only requirement",
    )
    
    try:
        op = client.get_type("AdGroupAdOperation")
        ad_group_ad = op.update
        ad_group_ad.resource_name = resource_name
        
        # Build new RSA structure
        new_rsa = ad_group_ad.ad.responsive_search_ad
        
        # Re-add headlines
        for h in new_headlines_assets:
            asset = client.get_type("AdTextAsset")
            asset.text = h.text
            if h.pinned_field:
                asset.pinned_field = h.pinned_field
            new_rsa.headlines.append(asset)
            
        # Re-add descriptions (unchanged)
        for d in rsa.descriptions:
            asset = client.get_type("AdTextAsset")
            asset.text = d.text
            if d.pinned_field:
                asset.pinned_field = d.pinned_field
            new_rsa.descriptions.append(asset)
            
        ad_group_ad.ad.final_urls.extend(ad_data.final_urls)
        new_rsa.path1 = rsa.path1
        new_rsa.path2 = rsa.path2
        
        client.copy_from(op.update_mask, FieldMask(paths=[
            "ad.responsive_search_ad.headlines",
            "ad.responsive_search_ad.descriptions",
            "ad.final_urls",
            "ad.responsive_search_ad.path1",
            "ad.responsive_search_ad.path2"
        ]))
        
        response = ad_group_ad_service.mutate_ad_group_ads(
            customer_id=CUSTOMER_ID,
            operations=[op]
        )
        print(f"✓ Updated RSA for {ad_group_name}: {response.results[0].resource_name}")
        update_gads_action_result(action_id, executed=True, execution_result="success")
        set_audit_approval(action_id, "admin")
    except Exception as e:
        update_gads_action_result(action_id, executed=True, execution_result="error", error_detail=str(e))
        print(f"✗ Failed to update RSA: {e}")
        raise e

def main():
    client = get_client()
    
    print("Starting modifications...\n")
    
    # 1. Raise default bid of Tooth Pain & Symptoms (ID: 195596529359) to $7.00
    update_ad_group_bid(client, "195596529359", 7.00)
    print()
    
    # 2. Pause Same-Day & Walk-In ad group (ID: 195596529519)
    pause_ad_group(client, "195596529519", "Same-Day & Walk-In")
    print()
    
    # 3. Remove walk-in headlines from the remaining ads
    # Tooth Pain & Symptoms ad ID: 809870981944
    remove_walkin_headline_from_rsa(client, "195596529359", "Tooth Pain & Symptoms", "809870981944")
    print()
    
    # Emergency Dentist Core ad ID: 809836233261
    remove_walkin_headline_from_rsa(client, "201527188972", "Emergency Dentist Core", "809836233261")
    print()
    
    print("All tasks completed successfully!")

if __name__ == "__main__":
    main()
