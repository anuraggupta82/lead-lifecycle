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
    
    # Check ad group performance from 2026-05-22 to 2026-05-30
    query = """
        SELECT
          ad_group.name,
          metrics.impressions,
          metrics.clicks,
          metrics.cost_micros,
          metrics.conversions
        FROM ad_group
        WHERE campaign.id = 23834204777
          AND segments.date >= '2026-05-22'
          AND segments.date <= '2026-05-30'
    """
    
    ga_service = client.get_service("GoogleAdsService")
    search_request = client.get_type("SearchGoogleAdsRequest")
    search_request.customer_id = CUSTOMER_ID
    search_request.query = query
    
    print("Fetching ad group performance from 5/22/26 to 5/30/26...\n")
    response = ga_service.search(request=search_request)
    
    print(f"{'Ad Group':<30} | {'Imps':<6} | {'Clicks':<6} | {'Cost':<8} | {'Convs':<5}")
    print("-" * 65)
    
    for row in response:
        ag_name = row.ad_group.name
        metrics = row.metrics
        cost = metrics.cost_micros / 1000000 if metrics.cost_micros else 0.0
        print(f"{ag_name:<30} | {metrics.impressions:<6} | {metrics.clicks:<6} | ${cost:<7.2f} | {metrics.conversions:<5.1f}")

if __name__ == "__main__":
    main()
