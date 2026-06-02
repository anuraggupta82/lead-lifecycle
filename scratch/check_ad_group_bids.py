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
          ad_group.id,
          ad_group.name,
          ad_group.cpc_bid_micros,
          ad_group.status
        FROM ad_group
        WHERE campaign.id = 23834204777
    """
    
    ga_service = client.get_service("GoogleAdsService")
    search_request = client.get_type("SearchGoogleAdsRequest")
    search_request.customer_id = CUSTOMER_ID
    search_request.query = query
    
    print("Fetching ad group bids for Emergency Dentistry campaign...\n")
    response = ga_service.search(request=search_request)
    
    for row in response:
        ag = row.ad_group
        bid = ag.cpc_bid_micros / 1000000 if ag.cpc_bid_micros else 0.0
        print(f"Ad Group: {ag.name} (ID: {ag.id})")
        print(f"  Status: {ag.status.name}")
        print(f"  Default Max CPC Bid: ${bid:.2f}")
        print("-" * 50)

if __name__ == "__main__":
    main()
