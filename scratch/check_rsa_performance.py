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
    
    # Check ad performance for the last 30 days
    query = """
        SELECT
          ad_group.name,
          ad_group_ad.ad.id,
          ad_group_ad.status,
          metrics.impressions,
          metrics.clicks,
          metrics.cost_micros,
          metrics.conversions
        FROM ad_group_ad
        WHERE campaign.id = 23834204777
          AND ad_group_ad.status = ENABLED
          AND segments.date DURING LAST_30_DAYS
    """
    
    ga_service = client.get_service("GoogleAdsService")
    search_request = client.get_type("SearchGoogleAdsRequest")
    search_request.customer_id = CUSTOMER_ID
    search_request.query = query
    
    print("Fetching active RSA performance for the last 30 days...\n")
    response = ga_service.search(request=search_request)
    
    print(f"{'Ad Group':<30} | {'Ad ID':<15} | {'Status':<8} | {'Imps':<6} | {'Clicks':<6} | {'Cost':<8} | {'Convs':<5}")
    print("-" * 95)
    
    for row in response:
        ag_name = row.ad_group.name
        ad_id = row.ad_group_ad.ad.id
        status = row.ad_group_ad.status.name
        metrics = row.metrics
        cost = metrics.cost_micros / 1000000 if metrics.cost_micros else 0.0
        print(f"{ag_name:<30} | {ad_id:<15} | {status:<8} | {metrics.impressions:<6} | {metrics.clicks:<6} | ${cost:<7.2f} | {metrics.conversions:<5.1f}")

if __name__ == "__main__":
    main()
