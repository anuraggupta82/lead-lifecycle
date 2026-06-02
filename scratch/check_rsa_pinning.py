import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../backend")))

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.abspath(os.path.join(os.path.dirname(__file__), "../backend/.env")))

from google.ads.googleads.client import GoogleAdsClient

CUSTOMER_ID = os.getenv("GOOGLE_ADS_CUSTOMER_ID", "").replace("-", "")

def main():
    client = GoogleAdsClient.load_from_dict({
        "developer_token":  os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN"),
        "client_id":        os.getenv("GOOGLE_ADS_CLIENT_ID"),
        "client_secret":    os.getenv("GOOGLE_ADS_CLIENT_SECRET"),
        "refresh_token":    os.getenv("GOOGLE_ADS_REFRESH_TOKEN"),
        "login_customer_id": os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "").replace("-", ""),
        "use_proto_plus":   True,
    })
    
    query = """
        SELECT
          ad_group.name,
          ad_group_ad.ad.id,
          ad_group_ad.ad.responsive_search_ad.headlines,
          ad_group_ad.ad.responsive_search_ad.descriptions
        FROM ad_group_ad
        WHERE campaign.id = 23834204777
          AND ad_group_ad.status = ENABLED
    """
    
    ga_service = client.get_service("GoogleAdsService")
    search_request = client.get_type("SearchGoogleAdsRequest")
    search_request.customer_id = CUSTOMER_ID
    search_request.query = query
    
    print("Checking headline pinning for active RSAs...\n")
    response = ga_service.search(request=search_request)
    
    for row in response:
        ag_name = row.ad_group.name
        ad_id = row.ad_group_ad.ad.id
        rsa = row.ad_group_ad.ad.responsive_search_ad
        
        print(f"Ad Group: {ag_name} (Ad ID: {ad_id})")
        print("Headlines:")
        for h in rsa.headlines:
            pin = h.pinned_field.name if h.pinned_field else "Not Pinned"
            print(f"  - {h.text:<40} | Pin: {pin}")
            
        print("Descriptions:")
        for d in rsa.descriptions:
            pin = d.pinned_field.name if d.pinned_field else "Not Pinned"
            print(f"  - {d.text:<60} | Pin: {pin}")
        print("-" * 80)

if __name__ == "__main__":
    main()
