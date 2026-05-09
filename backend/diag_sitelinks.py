"""
Diagnostic: test sitelink creation step by step on the Emergency campaign.
Run from backend dir with the venv active:
  python diag_sitelinks.py
"""
import sys, json, logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
log = logging.getLogger(__name__)

CAMPAIGN_RESOURCE = "customers/2498049505/campaigns/23819790853"
CUSTOMER_ID       = "2498049505"

TEST_SITELINKS = [
    {"title": "Book Online",  "url": "https://visitgdc.com/",                        "description1": "", "description2": ""},
    {"title": "About Us",     "url": "https://graftondentalcare.com/dental-office/",  "description1": "", "description2": ""},
    {"title": "Our Services", "url": "https://graftondentalcare.com/services/",       "description1": "", "description2": ""},
]

from google_ads_sync import _build_client
client = _build_client()
ga_service       = client.get_service("GoogleAdsService")
asset_service    = client.get_service("AssetService")
camp_asset_svc   = client.get_service("CampaignAssetService")

# ── Step 1: List existing sitelink campaign_assets ───────────────────────────
print("\n=== Existing sitelinks on campaign ===")
q = f"""
    SELECT campaign_asset.resource_name, campaign_asset.status,
           asset.name, asset.sitelink_asset.link_text,
           asset.final_urls
    FROM campaign_asset
    WHERE campaign_asset.campaign = '{CAMPAIGN_RESOURCE}'
      AND campaign_asset.field_type = SITELINK
"""
try:
    rows = list(ga_service.search(customer_id=CUSTOMER_ID, query=q))
    if rows:
        for r in rows:
            print(f"  CampaignAsset: {r.campaign_asset.resource_name}")
            print(f"    status: {r.campaign_asset.status.name}")
            print(f"    asset name: {r.asset.name}")
            print(f"    link_text: {r.asset.sitelink_asset.link_text}")
            print(f"    final_urls: {list(r.asset.final_urls)}")
    else:
        print("  (none found)")
except Exception as e:
    print(f"  ERROR querying: {e}")

# ── Step 2: Try creating one sitelink asset ───────────────────────────────────
print("\n=== Creating test sitelink asset ===")
sl = TEST_SITELINKS[0]
op = client.get_type("AssetOperation")
asset = op.create
asset.name = f"DIAG_TEST — {sl['title']}"
asset.sitelink_asset.link_text = sl["title"]
asset.final_urls.append(sl["url"])
print(f"  Asset to create: name={asset.name!r} link_text={asset.sitelink_asset.link_text!r} final_urls={list(asset.final_urls)}")

try:
    resp = asset_service.mutate_assets(customer_id=CUSTOMER_ID, operations=[op])
    asset_rn = resp.results[0].resource_name
    print(f"  ✅ Asset created: {asset_rn}")
except Exception as e:
    print(f"  ❌ Asset create FAILED: {e}")
    sys.exit(1)

# ── Step 3: Link asset to campaign ───────────────────────────────────────────
print("\n=== Linking asset to campaign ===")
link_op = client.get_type("CampaignAssetOperation")
link = link_op.create
link.campaign   = CAMPAIGN_RESOURCE
link.asset      = asset_rn
link.field_type = client.enums.AssetFieldTypeEnum.SITELINK
print(f"  Linking {asset_rn} → {CAMPAIGN_RESOURCE}")

try:
    link_resp = camp_asset_svc.mutate_campaign_assets(customer_id=CUSTOMER_ID, operations=[link_op])
    link_rn = link_resp.results[0].resource_name
    print(f"  ✅ CampaignAsset created: {link_rn}")
except Exception as e:
    print(f"  ❌ Link FAILED: {e}")
    sys.exit(1)

# ── Step 4: Read back to confirm ─────────────────────────────────────────────
print("\n=== Reading back campaign assets after creation ===")
try:
    rows2 = list(ga_service.search(customer_id=CUSTOMER_ID, query=q))
    print(f"  Total sitelink campaign_assets now: {len(rows2)}")
    for r in rows2:
        print(f"  - {r.asset.sitelink_asset.link_text!r} | {list(r.asset.final_urls)} | status={r.campaign_asset.status.name}")
except Exception as e:
    print(f"  ERROR reading back: {e}")

print("\nDone.")
