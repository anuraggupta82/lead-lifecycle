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
          campaign.id,
          ad_group.id,
          ad_group.name,
          ad_group_ad.ad.id,
          ad_group_ad.ad.responsive_search_ad.headlines,
          ad_group_ad.ad.responsive_search_ad.descriptions,
          ad_group_ad.ad.final_urls,
          ad_group_ad.status
        FROM ad_group_ad
        WHERE campaign.id = 23834204777
    """
    
    ga_service = client.get_service("GoogleAdsService")
    search_request = client.get_type("SearchGoogleAdsRequest")
    search_request.customer_id = CUSTOMER_ID
    search_request.query = query
    
    print("Fetching ads for Emergency Dentistry campaign...\n")
    response = ga_service.search(request=search_request)
    
    for row in response:
        ad = row.ad_group_ad.ad
        ad_group_name = row.ad_group.name
        status = row.ad_group_ad.status
        print(f"Ad Group: {ad_group_name} (Status: {status.name})")
        print(f"Ad ID: {ad.id}")
        print("Final URLs:", list(ad.final_urls))
        
        rsa = ad.responsive_search_ad
        if rsa:
            print("Headlines:")
            for h in rsa.headlines:
                print(f"  - {h.text}")
            print("Descriptions:")
            for d in rsa.descriptions:
                print(f"  - {d.text}")
        else:
            print("No Responsive Search Ad details found for this ad type.")
        print("-" * 50)

if __name__ == "__main__":
    main()
