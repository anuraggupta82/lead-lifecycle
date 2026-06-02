import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../backend")))

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.abspath(os.path.join(os.path.dirname(__file__), "../backend/.env")))

from google.ads.googleads.client import GoogleAdsClient
from google.protobuf.field_mask_pb2 import FieldMask
from database import log_admin_manual_action, update_gads_action_result, set_audit_approval

CUSTOMER_ID = os.getenv("GOOGLE_ADS_CUSTOMER_ID", "").replace("-", "")

def get_client():
    return GoogleAdsClient.load_from_dict({
        "developer_token":  os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN"),
        "client_id":        os.getenv("GOOGLE_ADS_CLIENT_ID"),
        "client_secret":    os.getenv("GOOGLE_ADS_CLIENT_SECRET"),
        "refresh_token":    os.getenv("GOOGLE_ADS_REFRESH_TOKEN"),
        "login_customer_id": os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "").replace("-", ""),
        "use_proto_plus":   True,
    })

def recreate_ad_without_walkin(client, ad_group_id, ad_group_name, ad_id):
    ad_group_ad_service = client.get_service("AdGroupAdService")
    old_resource_name = f"customers/{CUSTOMER_ID}/adGroupAds/{ad_group_id}~{ad_id}"
    
    # 1. Retrieve current ad details
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
    
    # Filter headlines
    new_headlines_assets = []
    removed_any = False
    for h in rsa.headlines:
        if "walk-in" in h.text.lower() or "walk in" in h.text.lower():
            print(f"  Removing headline: '{h.text}' from {ad_group_name}")
            removed_any = True
        else:
            new_headlines_assets.append(h)
            
    if not removed_any:
        print(f"  No walk-in headlines found in {ad_group_name} - no action needed.")
        return
        
    # Filter descriptions (just in case)
    new_descriptions_assets = []
    for d in rsa.descriptions:
        if "walk-in" in d.text.lower() or "walk in" in d.text.lower():
            print(f"  Removing/modifying walk-in reference from description: '{d.text}'")
            # Replace it with appointment copy
            new_desc = d.text.replace("Walk-ins and same-day appointments", "Same-day appointments")
            new_desc = new_desc.replace("Walk-ins welcome", "Same-day slots")
            print(f"    -> New description: '{new_desc}'")
            asset = client.get_type("AdTextAsset")
            asset.text = new_desc
            if d.pinned_field:
                asset.pinned_field = d.pinned_field
            new_descriptions_assets.append(asset)
        else:
            new_descriptions_assets.append(d)
            
    ad_group_resource = f"customers/{CUSTOMER_ID}/adGroups/{ad_group_id}"
    
    # Log action to audit log (we log a recreation action)
    action_id = log_admin_manual_action(
        operation="recreate_rsa",
        entity_type="ad_group_ad",
        entity_id=old_resource_name,
        entity_name=f"RSA in {ad_group_name}",
        before={"headlines": old_headlines, "descriptions": old_descriptions},
        after={
            "headlines": [h.text for h in new_headlines_assets],
            "descriptions": [d.text for d in new_descriptions_assets],
            "note": "Recreate RSA without walk-in copy, and pause old RSA",
        },
        reason="Remove 'Walk-Ins' copy to align with appointment-only requirement",
    )
    
    try:
        # A. Pause the old ad
        pause_op = client.get_type("AdGroupAdOperation")
        ad_group_ad_pause = pause_op.update
        ad_group_ad_pause.resource_name = old_resource_name
        ad_group_ad_pause.status = client.enums.AdGroupAdStatusEnum.PAUSED
        client.copy_from(pause_op.update_mask, FieldMask(paths=["status"]))
        
        ad_group_ad_service.mutate_ad_group_ads(
            customer_id=CUSTOMER_ID,
            operations=[pause_op]
        )
        print(f"  ✓ Paused old ad {ad_id} in {ad_group_name}")
        
        # B. Create the new ad
        create_op = client.get_type("AdGroupAdOperation")
        ad_group_ad_create = create_op.create
        ad_group_ad_create.ad_group = ad_group_resource
        ad_group_ad_create.status = client.enums.AdGroupAdStatusEnum.ENABLED
        
        new_rsa = ad_group_ad_create.ad.responsive_search_ad
        
        # Add headlines
        for h in new_headlines_assets:
            asset = client.get_type("AdTextAsset")
            asset.text = h.text
            if h.pinned_field:
                asset.pinned_field = h.pinned_field
            new_rsa.headlines.append(asset)
            
        # Add descriptions
        for d in new_descriptions_assets:
            asset = client.get_type("AdTextAsset")
            asset.text = d.text
            if d.pinned_field:
                asset.pinned_field = d.pinned_field
            new_rsa.descriptions.append(asset)
            
        ad_group_ad_create.ad.final_urls.extend(ad_data.final_urls)
        new_rsa.path1 = rsa.path1
        new_rsa.path2 = rsa.path2
        
        response = ad_group_ad_service.mutate_ad_group_ads(
            customer_id=CUSTOMER_ID,
            operations=[create_op]
        )
        new_resource = response.results[0].resource_name
        print(f"  ✓ Created new RSA for {ad_group_name}: {new_resource}")
        
        update_gads_action_result(action_id, executed=True, execution_result="success")
        set_audit_approval(action_id, "admin")
    except Exception as e:
        update_gads_action_result(action_id, executed=True, execution_result="error", error_detail=str(e))
        print(f"  ✗ Failed to recreate ad for {ad_group_name}: {e}")
        raise e

def main():
    client = get_client()
    
    print("Starting RSA recreations...\n")
    
    # 1. Tooth Pain & Symptoms ad ID: 809870981944
    recreate_ad_without_walkin(client, "195596529359", "Tooth Pain & Symptoms", "809870981944")
    print()
    
    # 2. Emergency Dentist Core ad ID: 809836233261
    recreate_ad_without_walkin(client, "201527188972", "Emergency Dentist Core", "809836233261")
    print()
    
    print("All tasks completed successfully!")

if __name__ == "__main__":
    main()
