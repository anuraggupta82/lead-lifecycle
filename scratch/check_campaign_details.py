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
          campaign.name,
          campaign.status,
          campaign.bidding_strategy_type,
          campaign.maximize_conversions.target_cpa_micros,
          campaign.maximize_conversion_value.target_roas,
          campaign.targeting_setting.target_restrictions
        FROM campaign
        WHERE campaign.id = 23834204777
    """
    
    ga_service = client.get_service("GoogleAdsService")
    search_request = client.get_type("SearchGoogleAdsRequest")
    search_request.customer_id = CUSTOMER_ID
    search_request.query = query
    
    print("Fetching campaign settings...\n")
    response = ga_service.search(request=search_request)
    
    for row in response:
        c = row.campaign
        print(f"Campaign: {c.name} (ID: {c.id})")
        print(f"Status: {c.status.name}")
        print(f"Bidding Strategy Type: {c.bidding_strategy_type.name}")
        if c.maximize_conversions and c.maximize_conversions.target_cpa_micros:
            print(f"Target CPA: ${c.maximize_conversions.target_cpa_micros / 1000000:.2f}")
        if c.maximize_conversion_value and c.maximize_conversion_value.target_roas:
            print(f"Target ROAS: {c.maximize_conversion_value.target_roas:.2f}")
        print("-" * 50)

if __name__ == "__main__":
    main()
