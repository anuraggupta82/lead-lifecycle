import os
import json
from google.ads.googleads.client import GoogleAdsClient
from config import get_settings

settings = get_settings()
credentials = {
    "developer_token": settings.google_ads_developer_token,
    "refresh_token": settings.google_ads_refresh_token,
    "client_id": settings.google_ads_client_id,
    "client_secret": settings.google_ads_client_secret,
    "login_customer_id": settings.google_ads_login_customer_id,
    "use_proto_plus": True
}
client = GoogleAdsClient.load_from_dict(credentials)
customer_id = settings.google_ads_customer_id

ga_service = client.get_service("GoogleAdsService")

query_budget = """
    SELECT
        account_budget.id,
        account_budget.name,
        account_budget.status,
        account_budget.billing_setup,
        account_budget.approved_start_date_time,
        account_budget.approved_end_date_time,
        account_budget.approved_spending_limit_micros,
        account_budget.approved_spending_limit_type,
        account_budget.adjusted_spending_limit_micros,
        account_budget.adjusted_spending_limit_type
    FROM account_budget
"""

try:
    response = ga_service.search(customer_id=customer_id, query=query_budget)
    budgets = []
    for row in response:
        budgets.append({
            "id": row.account_budget.id,
            "name": row.account_budget.name,
            "status": row.account_budget.status.name,
            "billing_setup": row.account_budget.billing_setup,
            "approved_start": row.account_budget.approved_start_date_time,
            "approved_end": row.account_budget.approved_end_date_time,
            "approved_limit_usd": row.account_budget.approved_spending_limit_micros / 1000000.0 if row.account_budget.approved_spending_limit_micros else row.account_budget.approved_spending_limit_type.name,
            "adjusted_limit_usd": row.account_budget.adjusted_spending_limit_micros / 1000000.0 if row.account_budget.adjusted_spending_limit_micros else row.account_budget.adjusted_spending_limit_type.name
        })
    print("ACCOUNT BUDGETS:")
    print(json.dumps(budgets, indent=2))
except Exception as e:
    print(f"Error querying account_budget: {e}")
