"""
Set final_url_suffix = '#results' on the nXtsmile Implants campaign.

This appends #results to every final URL in the campaign, sending paid
visitors directly to the Before & After section instead of the hero.
No ads are recreated — this is a campaign-level field update.

    cd /path/to/lead-lifecycle/backend
    source venv/bin/activate
    python3 set_nxtsmile_url_suffix.py
"""

import os, sys
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

from google.ads.googleads.client import GoogleAdsClient
from google.protobuf import field_mask_pb2
from database import log_admin_manual_action, update_gads_action_result, set_audit_approval

CAMPAIGN_RESOURCE = "customers/2498049505/campaigns/23870298927"
CAMPAIGN_NAME     = "nXtsmile Implants (05/23 — 100/day) (05/23 23:33)"
NEW_SUFFIX        = "#results"

CUSTOMER_ID = os.getenv("GOOGLE_ADS_CUSTOMER_ID", "").replace("-", "")


def _get_client():
    return GoogleAdsClient.load_from_dict({
        "developer_token":   os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN"),
        "client_id":         os.getenv("GOOGLE_ADS_CLIENT_ID"),
        "client_secret":     os.getenv("GOOGLE_ADS_CLIENT_SECRET"),
        "refresh_token":     os.getenv("GOOGLE_ADS_REFRESH_TOKEN"),
        "login_customer_id": os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "").replace("-", ""),
        "use_proto_plus":    True,
    })


def get_current_suffix(client) -> str:
    """Read the current final_url_suffix on the campaign."""
    ga = client.get_service("GoogleAdsService")
    q = f"""
        SELECT campaign.resource_name, campaign.final_url_suffix
        FROM campaign
        WHERE campaign.resource_name = '{CAMPAIGN_RESOURCE}'
    """
    current = ""
    for batch in ga.search_stream(customer_id=CUSTOMER_ID, query=q):
        for row in batch.results:
            current = row.campaign.final_url_suffix or ""
    return current


def main():
    client = _get_client()

    # Read current state
    current_suffix = get_current_suffix(client)
    print(f"Campaign   : {CAMPAIGN_NAME}")
    print(f"Current suffix : '{current_suffix or '(none)'}'")
    print(f"New suffix     : '{NEW_SUFFIX}'")

    if current_suffix == NEW_SUFFIX:
        print("\n✓ Already set — nothing to do.")
        return

    # Log the action
    action_id = log_admin_manual_action(
        operation="set_final_url_suffix",
        entity_type="campaign",
        entity_id=CAMPAIGN_RESOURCE,
        entity_name=CAMPAIGN_NAME,
        before={"final_url_suffix": current_suffix},
        after={"final_url_suffix": NEW_SUFFIX},
        reason="ga4_analysis_may25_2026_paid_visitors_not_reaching_before_after_section",
    )

    # Build the update operation
    campaign_service = client.get_service("CampaignService")
    op = client.get_type("CampaignOperation")
    campaign = op.update
    campaign.resource_name = CAMPAIGN_RESOURCE
    campaign.final_url_suffix = NEW_SUFFIX
    client.copy_from(
        op.update_mask,
        field_mask_pb2.FieldMask(paths=["final_url_suffix"])
    )

    try:
        response = campaign_service.mutate_campaigns(
            customer_id=CUSTOMER_ID,
            operations=[op],
        )
        resource = response.results[0].resource_name
        update_gads_action_result(action_id, executed=True, execution_result="success")
        set_audit_approval(action_id, "admin")
        print(f"\n✓ Updated: {resource}")
        print(f"  All paid clicks now land on nxtsmile.com/{NEW_SUFFIX}")
        print(f"  Before & After section will be the first thing visitors see.")
    except Exception as e:
        update_gads_action_result(
            action_id, executed=True,
            execution_result="error", error_detail=str(e)
        )
        print(f"\n✗ Failed: {e}")


if __name__ == "__main__":
    main()
