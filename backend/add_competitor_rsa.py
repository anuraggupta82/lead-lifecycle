"""
Add competitor-contrast RSA to two ad groups in the nXtsmile Implants campaign.

Target ad groups:
  - All-on-4 Implants Worcester County  (ID: 201959101332)
  - Dental Implants Cost Comparison      (ID: 196128439625)

Run from the backend directory with the venv active:

    cd /path/to/lead-lifecycle/backend
    source venv/bin/activate
    python add_competitor_rsa.py
"""

import os, sys
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

from google.ads.googleads.client import GoogleAdsClient
from database import log_admin_manual_action, update_gads_action_result, set_audit_approval

CUSTOMER_ID = os.getenv("GOOGLE_ADS_CUSTOMER_ID", "").replace("-", "")


def _get_client():
    return GoogleAdsClient.load_from_dict({
        "developer_token":  os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN"),
        "client_id":        os.getenv("GOOGLE_ADS_CLIENT_ID"),
        "client_secret":    os.getenv("GOOGLE_ADS_CLIENT_SECRET"),
        "refresh_token":    os.getenv("GOOGLE_ADS_REFRESH_TOKEN"),
        "login_customer_id": os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "").replace("-", ""),
        "use_proto_plus":   True,
    })

# Ad group resource names
AD_GROUPS = [
    {
        "name": "All-on-4 Implants Worcester County",
        "resource": "customers/2498049505/adGroups/201959101332",
    },
    {
        "name": "Dental Implants Cost Comparison",
        "resource": "customers/2498049505/adGroups/196128439625",
    },
]

FINAL_URL   = "https://nxtsmile.com/"
PATH1       = "Implants"
PATH2       = "Not-a-Chain"

HEADLINES = [
    "Family-Owned, Not a Franchise",   # 30
    "Not a Corporate Chain",            # 22
    "ClearChoice Alternative MA",       # 27
    "Nuvia Alternative Near You",       # 27
    "Skip the Chain, Choose Local",     # 29
    "Your Dentist Knows Your Name",     # 29
    "One Doctor, Your Whole Journey",   # 30
    "Dr. Gupta Does It All In-House",   # 30
    "No Upsells, No Franchise Fees",    # 30
    "Local vs. Chain — See the Diff",   # 30
    "Permanent Teeth in 1 Day",         # 24
    "Free AI Smile Preview",            # 22
    "Top-Rated 5-Star Reviewed",        # 25
    "Worcester County Implants",        # 25
    "Free Implant Consultation",        # 25
]

DESCRIPTIONS = [
    "Considering ClearChoice or Nuvia? See why patients choose our family-owned practice first.",  # 90
    "Dr. Gupta handles your full case — consult to final teeth. Not a chain. Free preview.",        # 85
    "No franchise model. No rotating doctors. Just Dr. Gupta and your permanent new smile.",        # 85
    "Top-rated Worcester County implant care. Local, personal, permanent. Free consult today.",     # 88
]


def validate_copy():
    errors = []
    for i, h in enumerate(HEADLINES):
        if len(h) > 30:
            errors.append(f"Headline {i+1} too long ({len(h)} chars): '{h}'")
    for i, d in enumerate(DESCRIPTIONS):
        if len(d) > 90:
            errors.append(f"Description {i+1} too long ({len(d)} chars): '{d}'")
    return errors


def build_rsa_operation(client, ad_group_resource):
    op = client.get_type("AdGroupAdOperation")
    ad_group_ad = op.create
    ad_group_ad.ad_group = ad_group_resource
    ad_group_ad.status = client.enums.AdGroupAdStatusEnum.ENABLED

    rsa = ad_group_ad.ad.responsive_search_ad

    for text in HEADLINES:
        asset = client.get_type("AdTextAsset")
        asset.text = text
        rsa.headlines.append(asset)

    for text in DESCRIPTIONS:
        asset = client.get_type("AdTextAsset")
        asset.text = text
        rsa.descriptions.append(asset)

    ad_group_ad.ad.final_urls.append(FINAL_URL)
    rsa.path1 = PATH1
    rsa.path2 = PATH2

    return op


def main():
    # Validate copy first
    errors = validate_copy()
    if errors:
        print("VALIDATION ERRORS — fix before running:")
        for e in errors:
            print(f"  {e}")
        sys.exit(1)

    print("Copy validation passed.\n")

    client = _get_client()
    ad_group_ad_service = client.get_service("AdGroupAdService")

    for ag in AD_GROUPS:
        print(f"Adding competitor-contrast RSA to: {ag['name']}")

        action_id = log_admin_manual_action(
            operation="add_rsa",
            entity_type="ad_group",
            entity_id=ag["resource"],
            entity_name=ag["name"],
            before={},
            after={
                "headlines": HEADLINES,
                "descriptions": DESCRIPTIONS,
                "path1": PATH1,
                "path2": PATH2,
                "final_url": FINAL_URL,
                "note": "Competitor-contrast RSA: family-owned vs corporate chain angle (Nuvia/ClearChoice conquest)",
            },
            reason="competitor_contrast_rsa_may25_2026",
        )

        try:
            op = build_rsa_operation(client, ag["resource"])
            response = ad_group_ad_service.mutate_ad_group_ads(
                customer_id=CUSTOMER_ID,
                operations=[op],
            )
            resource_name = response.results[0].resource_name
            update_gads_action_result(action_id, executed=True, execution_result="success")
            set_audit_approval(action_id, "admin")
            print(f"  ✓ Created: {resource_name}\n")

        except Exception as e:
            update_gads_action_result(
                action_id, executed=True,
                execution_result="error", error_detail=str(e)
            )
            print(f"  ✗ Failed: {e}\n")

    print("Done.")


if __name__ == "__main__":
    main()
