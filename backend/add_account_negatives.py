"""
One-time script: add account-level (CustomerNegativeCriterion) negative keywords.
Run from the backend/ directory with the venv active:
    python add_account_negatives.py

These block the terms across ALL campaigns in the account — no campaign resource needed.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from config import get_settings
from google_ads_sync import _build_client

CUSTOMER_ID = "2498049505"

# Terms to add as BROAD match account-level negatives
# (BROAD is correct for account negatives — catches all variations)
TERMS_TO_ADD = [
    ("masshealth",    "BROAD"),
    ("medicaid",      "BROAD"),
    ("free",          "BROAD"),
    ("dental school", "BROAD"),
    ("free trial",    "BROAD"),
    ("study",         "BROAD"),
    ("medicare",      "BROAD"),
    ("nuvia",         "BROAD"),
    ("clear choice",  "BROAD"),
    ("polasky",       "BROAD"),
    ("babu",          "BROAD"),
    ("gedc",          "BROAD"),
    ("ashland family","BROAD"),
    ("orthodontist",  "BROAD"),
    ("orthodontics",  "BROAD"),
    ("x rays",        "BROAD"),
]


def list_existing(client, customer_id):
    """
    customer_negative_criterion only exposes id/type/resource_name in GAQL —
    keyword.text and keyword.match_type are NOT selectable fields.
    We fetch the raw criterion objects via the service instead.
    """
    ga_service = client.get_service("GoogleAdsService")
    # Only selectable fields for this resource
    query = """
        SELECT
            customer_negative_criterion.id,
            customer_negative_criterion.type,
            customer_negative_criterion.resource_name
        FROM customer_negative_criterion
        WHERE customer_negative_criterion.type = 'KEYWORD'
    """
    rows = list(ga_service.search(customer_id=customer_id, query=query))
    # keyword.text is available on the proto object even though it can't be
    # selected in GAQL — access via the criterion service mutate read-back
    # or just return the count/resource names for display
    resources = [row.customer_negative_criterion.resource_name for row in rows]
    return resources


def add_account_negative(client, customer_id, keyword_text, match_type="BROAD"):
    service = client.get_service("CustomerNegativeCriterionService")
    operation = client.get_type("CustomerNegativeCriterionOperation")
    criterion = operation.create
    criterion.keyword.text = keyword_text
    criterion.keyword.match_type = client.enums.KeywordMatchTypeEnum[match_type]

    try:
        response = service.mutate_customer_negative_criteria(
            customer_id=customer_id,
            operations=[operation],
        )
        resource = response.results[0].resource_name
        print(f"  ✓ Added '{keyword_text}' [{match_type}] → {resource}")
        return True
    except Exception as e:
        err = str(e)
        if "already exists" in err.lower() or "DUPLICATE_CRITERION" in err or "ALREADY_EXISTS" in err:
            print(f"  ⓘ '{keyword_text}' already exists — skipped")
            return True
        print(f"  ✗ FAILED '{keyword_text}': {e}")
        return False


def main():
    print("Building Google Ads client...")
    client = _build_client()

    print("\nFetching existing account-level negative keyword count...")
    existing_resources = list_existing(client, CUSTOMER_ID)
    print(f"  {len(existing_resources)} existing account-level negative keyword(s) found")

    print(f"\nAdding {len(TERMS_TO_ADD)} account-level negative keywords...")
    success = 0
    for text, match in TERMS_TO_ADD:
        if add_account_negative(client, CUSTOMER_ID, text, match):
            success += 1

    print(f"\nDone: {success}/{len(TERMS_TO_ADD)} terms processed successfully.")

    print("\nVerifying final account-level negative keyword count...")
    final_resources = list_existing(client, CUSTOMER_ID)
    print(f"  {len(final_resources)} total account-level negative keyword(s) now in account")


if __name__ == "__main__":
    main()
