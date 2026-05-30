import os
import sys
from google_ads_write import add_negative_keyword_to_campaign

active_campaigns = {
    "Emergency Dentistry": "customers/2498049505/campaigns/23834204777",
    "General Dentistry": "customers/2498049505/campaigns/23849370858",
    "nXtsmile Implants": "customers/2498049505/campaigns/23870298927",
    "Brand Awareness": "customers/2498049505/campaigns/23851360218"
}

general_negatives = [
    "nuvia",
    "clear choice",
    "polasky",
    "babu",
    "gedc",
    "ashland family",
    "orthodontist",
    "orthodontics",
    "x rays",
    "reasons not to"
]

implants_research_negatives = [
    "cost",
    "vs",
    "how much",
    "price"
]

cross_pollination_negatives = [
    "implant",
    "implants"
]

print("Pushing General Negatives to all active campaigns...")
for name, resource in active_campaigns.items():
    print(f"\nCampaign: {name}")
    for neg in general_negatives:
        try:
            add_negative_keyword_to_campaign(resource, neg, "PHRASE")
            print(f"  ✓ {neg} (PHRASE)")
        except Exception as e:
            if "DUPLICATE_CRITERION" in str(e) or "already exists" in str(e).lower() or "ALREADY_EXISTS" in str(e):
                print(f"  - {neg} (already exists)")
            else:
                print(f"  ✗ {neg} (Error: {e})")

print("\nPushing Research Intent Negatives to Implants Campaign...")
implants_resource = active_campaigns["nXtsmile Implants"]
for neg in implants_research_negatives:
    try:
        add_negative_keyword_to_campaign(implants_resource, neg, "PHRASE")
        print(f"  ✓ {neg} (PHRASE)")
    except Exception as e:
        if "DUPLICATE_CRITERION" in str(e) or "already exists" in str(e).lower() or "ALREADY_EXISTS" in str(e):
            print(f"  - {neg} (already exists)")
        else:
            print(f"  ✗ {neg} (Error: {e})")

print("\nPushing Cross-Pollination Negatives to General & Emergency Campaigns...")
for name in ["Emergency Dentistry", "General Dentistry"]:
    resource = active_campaigns[name]
    print(f"Campaign: {name}")
    for neg in cross_pollination_negatives:
        try:
            add_negative_keyword_to_campaign(resource, neg, "PHRASE")
            print(f"  ✓ {neg} (PHRASE)")
        except Exception as e:
            if "DUPLICATE_CRITERION" in str(e) or "already exists" in str(e).lower() or "ALREADY_EXISTS" in str(e):
                print(f"  - {neg} (already exists)")
            else:
                print(f"  ✗ {neg} (Error: {e})")

print("\nDone.")
