"""
Verify that negatives and competitor RSAs were actually applied to Google Ads.

    cd /path/to/lead-lifecycle/backend
    source venv/bin/activate
    python3 verify_nxtsmile_changes.py
"""

import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv()

from google.ads.googleads.client import GoogleAdsClient

client = GoogleAdsClient.load_from_dict({
    "developer_token":   os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN"),
    "client_id":         os.getenv("GOOGLE_ADS_CLIENT_ID"),
    "client_secret":     os.getenv("GOOGLE_ADS_CLIENT_SECRET"),
    "refresh_token":     os.getenv("GOOGLE_ADS_REFRESH_TOKEN"),
    "login_customer_id": os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "").replace("-", ""),
    "use_proto_plus":    True,
})
customer_id = os.getenv("GOOGLE_ADS_CUSTOMER_ID", "").replace("-", "")
ga = client.get_service("GoogleAdsService")
CAMPAIGN_ID = "23870298927"

# ── 1. Verify negative keywords ──────────────────────────────────────────────
print("=" * 65)
print("NEGATIVE KEYWORDS — campaign 23870298927 (nXtsmile Implants 05/23)")
print("=" * 65)

q = f"""
SELECT campaign_criterion.keyword.text, campaign_criterion.keyword.match_type
FROM campaign_criterion
WHERE campaign.id = {CAMPAIGN_ID}
  AND campaign_criterion.type = KEYWORD
  AND campaign_criterion.negative = TRUE
ORDER BY campaign_criterion.keyword.text
"""
resp = ga.search_stream(customer_id=customer_id, query=q)
negatives = []
for batch in resp:
    for row in batch.results:
        negatives.append(row.campaign_criterion.keyword.text)

print(f"Total negatives live: {len(negatives)}\n")
for kw in sorted(negatives):
    print(f"  [-] {kw}")

# Spot-check key ones
expected = [
    "single tooth implant cost",
    "snap on dentures",
    "clinical trials for dental implants near me",
    "affordable dental implants",
    "aspen dental near me",
    "nuvia location",
    "clearchoice near me",
    "grace dental",
    "who cannot get dental implants",
]
print("\n--- Spot-check ---")
missing = [kw for kw in expected if kw not in negatives]
if not missing:
    print("  ✓ All spot-check negatives confirmed live")
else:
    print(f"  ✗ Missing: {missing}")

# ── 2. Verify competitor-contrast RSAs ───────────────────────────────────────
print()
print("=" * 65)
print("RSAs in ad groups 201959101332 + 196128439625")
print("=" * 65)

q2 = f"""
SELECT ad_group.id, ad_group.name, ad_group_ad.ad.id,
       ad_group_ad.ad.responsive_search_ad.headlines,
       ad_group_ad.ad.responsive_search_ad.descriptions,
       ad_group_ad.ad.responsive_search_ad.path1,
       ad_group_ad.ad.responsive_search_ad.path2,
       ad_group_ad.status, ad_group_ad.ad.final_urls
FROM ad_group_ad
WHERE campaign.id = {CAMPAIGN_ID}
  AND ad_group.id IN (201959101332, 196128439625)
  AND ad_group_ad.ad.type = RESPONSIVE_SEARCH_AD
ORDER BY ad_group.name, ad_group_ad.ad.id
"""
resp2 = ga.search_stream(customer_id=customer_id, query=q2)
competitor_rsa_found = []

for batch in resp2:
    for row in batch.results:
        ad  = row.ad_group_ad.ad
        rsa = ad.responsive_search_ad
        headlines = [h.text for h in rsa.headlines]
        is_competitor_rsa = "Family-Owned, Not a Franchise" in headlines

        print(f"\nAd Group : {row.ad_group.name} (ID {row.ad_group.id})")
        print(f"Ad ID    : {ad.id}  |  Status: {row.ad_group_ad.status.name}")
        print(f"Path     : {rsa.path1 or '(none)'}/{rsa.path2 or '(none)'}")
        print(f"URL      : {list(ad.final_urls)}")
        print(f"Headlines: {headlines[:4]}{'...' if len(headlines) > 4 else ''}")

        if is_competitor_rsa:
            print("  *** COMPETITOR-CONTRAST RSA ✓")
            competitor_rsa_found.append(row.ad_group.name)

print()
print("=" * 65)
print("SUMMARY")
print("=" * 65)
print(f"  Negatives live      : {len(negatives)}")
print(f"  Competitor RSAs live: {len(competitor_rsa_found)}")
for ag in competitor_rsa_found:
    print(f"    ✓  {ag}")
if len(competitor_rsa_found) == 2:
    print("\n  ALL CHANGES VERIFIED ✓")
else:
    print(f"\n  ✗ Expected 2 competitor RSAs, found {len(competitor_rsa_found)}")
