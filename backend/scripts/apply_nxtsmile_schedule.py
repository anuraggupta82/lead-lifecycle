#!/usr/bin/env python3
"""
One-shot: apply ad schedule + Mon/Tue +20% bid modifiers to the nXtsmile Implants
campaign by calling push_ad_schedule() directly. Bypasses the buggy
/set-schedule HTTP endpoint.

Usage:
    cd /Users/anurag/Documents/Projects/gdc-apps/marketing/lead-lifecycle/backend
    source venv/bin/activate
    python scripts/apply_nxtsmile_schedule.py

Reads GOOGLE_ADS_* from backend/.env (via config.py).
"""
import sys
import os

# Make backend importable from this scripts/ subdir
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

from config import get_settings  # noqa: E402
from google_ads_create import push_ad_schedule, _build_client  # noqa: E402

CAMPAIGN_RESOURCE = "customers/2498049505/campaigns/23870298927"

SCHEDULE = [
    {"day": "MONDAY",    "start_hour": 8,  "end_hour": 18, "bid_modifier": 1.2},
    {"day": "TUESDAY",   "start_hour": 8,  "end_hour": 18, "bid_modifier": 1.2},
    {"day": "WEDNESDAY", "start_hour": 8,  "end_hour": 18, "bid_modifier": 1.0},
    {"day": "THURSDAY",  "start_hour": 8,  "end_hour": 18, "bid_modifier": 1.0},
    {"day": "FRIDAY",    "start_hour": 8,  "end_hour": 18, "bid_modifier": 1.0},
    {"day": "SATURDAY",  "start_hour": 8,  "end_hour": 14, "bid_modifier": 1.0},
    # No Sunday entry = ads paused on Sundays
]


def main():
    settings = get_settings()
    customer_id = "".join(ch for ch in (settings.google_ads_customer_id or "") if ch.isdigit())
    print(f"Customer ID: {customer_id}")
    print(f"Campaign:    {CAMPAIGN_RESOURCE}")
    print(f"Slots:       {len(SCHEDULE)}")
    print()

    client = _build_client()
    result = push_ad_schedule(
        client=client,
        customer_id=customer_id,
        campaign_resource=CAMPAIGN_RESOURCE,
        schedule=SCHEDULE,
        replace=True,  # remove any existing schedule criteria first
    )

    print("Result:", result)
    if not result["ok"]:
        print(f"FAILED: {result.get('error')}")
        sys.exit(1)

    print()
    print(f"SUCCESS — pushed {result['pushed']} ad-schedule criteria, "
          f"removed {result['removed']} pre-existing")
    print()
    print("Verify in Google Ads UI:")
    print("  Campaigns > nXtsmile Implants (05/23 - 100/day) > Settings > Ad schedule")
    print("  Expected: Mon/Tue 8a-6p (+20%), Wed/Thu/Fri 8a-6p, Sat 8a-2p")


if __name__ == "__main__":
    main()
