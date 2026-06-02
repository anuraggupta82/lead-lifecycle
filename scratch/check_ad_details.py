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
          ad_group_ad.ad_strength,
          ad_group_ad.policy_summary.review_status,
          ad_group_ad.policy_summary.approval_status,
          ad_group_ad.policy_summary.policy_topic_entries
        FROM ad_group_ad
        WHERE campaign.id = 23834204777
          AND ad_group_ad.status = ENABLED
    """
    
    ga_service = client.get_service("GoogleAdsService")
    search_request = client.get_type("SearchGoogleAdsRequest")
    search_request.customer_id = CUSTOMER_ID
    search_request.query = query
    
    print("Fetching active RSAs detail and strength...\n")
    response = ga_service.search(request=search_request)
    
    for row in response:
        ad_group_name = row.ad_group.name
        ad_id = row.ad_group_ad.ad.id
        strength = row.ad_group_ad.ad_strength.name
        review = row.ad_group_ad.policy_summary.review_status.name
        approval = row.ad_group_ad.policy_summary.approval_status.name
        
        print(f"Ad Group: {ad_group_name}")
        print(f"  Ad ID: {ad_id}")
        print(f"  Ad Strength: {strength}")
        print(f"  Policy Review Status: {review}")
        print(f"  Policy Approval Status: {approval}")
        
        topics = row.ad_group_ad.policy_summary.policy_topic_entries
        if topics:
            print("  Policy Topics:")
            for topic in topics:
                print(f"    - Topic: {topic.topic} (Type: {topic.type_.name})")
        print("-" * 50)

if __name__ == "__main__":
    main()
