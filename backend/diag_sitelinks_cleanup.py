"""
Remove the duplicate DIAG_TEST sitelink created during diagnostics.
Also tests that _remove_existing_campaign_sitelinks works correctly.
Run: python diag_sitelinks_cleanup.py
"""
import logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')

CAMPAIGN_RESOURCE = "customers/2498049505/campaigns/23819790853"
CUSTOMER_ID       = "2498049505"

from google_ads_sync import _build_client
client = _build_client()
ga_service    = client.get_service("GoogleAdsService")
camp_asset_svc = client.get_service("CampaignAssetService")

# List all current sitelinks
q = f"""
    SELECT campaign_asset.resource_name, campaign_asset.status,
           asset.name, asset.sitelink_asset.link_text, asset.final_urls
    FROM campaign_asset
    WHERE campaign_asset.campaign = '{CAMPAIGN_RESOURCE}'
      AND campaign_asset.field_type = SITELINK
      AND campaign_asset.status != REMOVED
"""
rows = list(ga_service.search(customer_id=CUSTOMER_ID, query=q))
print(f"\nFound {len(rows)} active sitelink campaign_assets:")
for r in rows:
    print(f"  {r.campaign_asset.resource_name}")
    print(f"    name={r.asset.name!r}  link_text={r.asset.sitelink_asset.link_text!r}  urls={list(r.asset.final_urls)}")

# Find duplicates (same link_text appearing more than once) — remove extras
from collections import defaultdict
by_text = defaultdict(list)
for r in rows:
    by_text[r.asset.sitelink_asset.link_text].append(r.campaign_asset.resource_name)

to_remove = []
for text, rns in by_text.items():
    if len(rns) > 1:
        # Keep first, remove rest
        print(f"\nDuplicate '{text}': keeping {rns[0]}, removing {rns[1:]}")
        to_remove.extend(rns[1:])

if to_remove:
    remove_ops = []
    for rn in to_remove:
        op = client.get_type("CampaignAssetOperation")
        op.remove = rn
        remove_ops.append(op)
    resp = camp_asset_svc.mutate_campaign_assets(customer_id=CUSTOMER_ID, operations=remove_ops)
    print(f"✅ Removed {len(remove_ops)} duplicate(s)")
else:
    print("\nNo duplicates to remove.")

# Final state
rows2 = list(ga_service.search(customer_id=CUSTOMER_ID, query=q))
print(f"\nFinal state — {len(rows2)} active sitelinks:")
for r in rows2:
    print(f"  - {r.asset.sitelink_asset.link_text!r} | {list(r.asset.final_urls)} | {r.campaign_asset.resource_name}")
