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
          ad_group_criterion.criterion_id,
          ad_group_criterion.keyword.text,
          ad_group_criterion.keyword.match_type,
          ad_group_criterion.cpc_bid_micros,
          ad_group_criterion.status,
          ad_group_criterion.quality_info.quality_score,
          ad_group_criterion.position_estimates.first_page_cpc_micros,
          ad_group_criterion.position_estimates.first_position_cpc_micros
        FROM ad_group_criterion
        WHERE campaign.id = 23834204777 
          AND ad_group_criterion.type = KEYWORD
          AND ad_group_criterion.status = ENABLED
    """
    
    ga_service = client.get_service("GoogleAdsService")
    search_request = client.get_type("SearchGoogleAdsRequest")
    search_request.customer_id = CUSTOMER_ID
    search_request.query = query
    
    print("Fetching active keywords and bids for Emergency Dentistry campaign...\n")
    response = ga_service.search(request=search_request)
    
    keywords_by_group = {}
    for row in response:
        ag_name = row.ad_group.name
        criterion = row.ad_group_criterion
        kw_text = criterion.keyword.text
        match_type = criterion.keyword.match_type.name
        bid = criterion.cpc_bid_micros / 1000000 if criterion.cpc_bid_micros else 0.0
        status = criterion.status.name
        qs = criterion.quality_info.quality_score if criterion.quality_info and criterion.quality_info.quality_score else "N/A"
        
        first_page = criterion.position_estimates.first_page_cpc_micros / 1000000 if criterion.position_estimates and criterion.position_estimates.first_page_cpc_micros else 0.0
        first_pos = criterion.position_estimates.first_position_cpc_micros / 1000000 if criterion.position_estimates and criterion.position_estimates.first_position_cpc_micros else 0.0
        
        if ag_name not in keywords_by_group:
            keywords_by_group[ag_name] = []
            
        keywords_by_group[ag_name].append({
            "text": kw_text,
            "match_type": match_type,
            "bid": bid,
            "qs": qs,
            "first_page": first_page,
            "first_pos": first_pos
        })
        
    for ag_name, kws in keywords_by_group.items():
        print(f"Ad Group: {ag_name}")
        print(f"{'Keyword':<40} | {'Match':<8} | {'Bid':<6} | {'QS':<3} | {'1st Page':<8} | {'1st Pos':<8}")
        print("-" * 83)
        for kw in sorted(kws, key=lambda x: x["bid"], reverse=True):
            bid_str = f"${kw['bid']:.2f}" if kw['bid'] else "Auto"
            fp_str = f"${kw['first_page']:.2f}" if kw['first_page'] else "N/A"
            fpos_str = f"${kw['first_pos']:.2f}" if kw['first_pos'] else "N/A"
            print(f"{kw['text']:<40} | {kw['match_type']:<8} | {bid_str:<6} | {kw['qs']:<3} | {fp_str:<8} | {fpos_str:<8}")
        print("\n" + "="*85 + "\n")

if __name__ == "__main__":
    main()
