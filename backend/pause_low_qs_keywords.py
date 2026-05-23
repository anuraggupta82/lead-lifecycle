#!/usr/bin/env python3
"""
Pause 3 low-QS keywords per Opus bid optimization recommendation.

Targets:
  1. 'dentist worcester' [EXACT] in Emergency Dentistry – Same-Day & Walk-In (QS=2, BELOW_AVERAGE LP)
  2. '[dentist near me]' [EXACT] in General Dentistry LP – Grafton Dentist–Branded Local (QS=4, hist_qs=1.0, BELOW_AVERAGE LP)
  3. '[dentist northborough ma]' [EXACT] in General Dentistry LP – Grafton Dentist–Branded Local (QS=1, out of geo, zero traffic)

Usage:
    cd /path/to/lead-lifecycle
    source venv/bin/activate
    python backend/pause_low_qs_keywords.py
"""

import sys
import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from the backend directory explicitly
_backend_dir = Path(__file__).parent
load_dotenv(_backend_dir / ".env", override=True)

sys.path.insert(0, str(_backend_dir))
from config import Settings
from google.ads.googleads.client import GoogleAdsClient
from google.protobuf import field_mask_pb2

settings = Settings()
customer_id = settings.google_ads_customer_id

config = {
    "developer_token": settings.google_ads_developer_token,
    "client_id": settings.google_ads_client_id,
    "client_secret": settings.google_ads_client_secret,
    "refresh_token": settings.google_ads_refresh_token,
    "login_customer_id": settings.google_ads_login_customer_id,
    "use_proto_plus": True,
}
client = GoogleAdsClient.load_from_dict(config)
ga_service = client.get_service("GoogleAdsService")
agc_service = client.get_service("AdGroupCriterionService")

# Step 1: Find resource names for the 3 target keywords
print("Step 1: Looking up keyword resource names...")
query = """
    SELECT
        ad_group_criterion.resource_name,
        ad_group_criterion.keyword.text,
        ad_group_criterion.keyword.match_type,
        ad_group_criterion.status,
        ad_group.id,
        ad_group.name,
        campaign.name
    FROM ad_group_criterion
    WHERE ad_group_criterion.type = 'KEYWORD'
      AND ad_group_criterion.status = 'ENABLED'
      AND campaign.status = 'ENABLED'
      AND (
        (ad_group_criterion.keyword.text = 'dentist worcester' AND ad_group.id = 195596529519 AND ad_group_criterion.keyword.match_type = 'EXACT')
        OR (ad_group_criterion.keyword.text = 'dentist near me' AND ad_group.id = 197340993780 AND ad_group_criterion.keyword.match_type = 'EXACT')
        OR (ad_group_criterion.keyword.text = 'dentist northborough ma' AND ad_group.id = 197340993780 AND ad_group_criterion.keyword.match_type = 'EXACT')
      )
"""

response = ga_service.search(customer_id=customer_id, query=query)
rows = list(response)
print(f"Found {len(rows)} keyword(s) to pause:\n")

resource_names = []
for row in rows:
    agc = row.ad_group_criterion
    print(f"  '{agc.keyword.text}' [{agc.keyword.match_type.name}]")
    print(f"    Campaign: {row.campaign.name}")
    print(f"    Ad Group: {row.ad_group.name}")
    print(f"    Resource: {agc.resource_name}")
    resource_names.append(agc.resource_name)

if len(rows) != 3:
    print(f"\nWARNING: Expected 3 rows, found {len(rows)}. Check query before proceeding.")
    if len(rows) == 0:
        print("No keywords found — they may already be paused or resource names differ.")
        sys.exit(1)

# Step 2: Build pause operations
print("\nStep 2: Building PAUSE operations...")
AdGroupCriterionOperation = client.get_type("AdGroupCriterionOperation")
AdGroupCriterion = client.get_type("AdGroupCriterion")
CriterionStatusEnum = client.enums.AdGroupCriterionStatusEnum

operations = []
for rn in resource_names:
    op = AdGroupCriterionOperation()
    criterion = op.update
    criterion.resource_name = rn
    criterion.status = CriterionStatusEnum.PAUSED
    op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["status"]))
    operations.append(op)

print(f"  Built {len(operations)} pause operation(s)")

# Step 3: Execute
print("\nStep 3: Executing pause operations...")
result = agc_service.mutate_ad_group_criteria(
    customer_id=customer_id,
    operations=operations,
)
print(f"\n✅ Done: {len(result.results)} keyword(s) paused successfully")
for r in result.results:
    print(f"  Paused: {r.resource_name}")
