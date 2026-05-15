"""
One-shot backfill: for every campaign in the local DB with a missing launch_date,
query Google Ads for the earliest segment.date and write it back.

Usage:
    cd backend
    python scripts/backfill_launch_dates.py

Safe to run multiple times — only writes when launch_date is NULL/empty.
"""

import sys
import os

# Allow imports from backend/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import get_all_campaigns, set_campaign_launch_date, get_campaign_by_id
from ai_optimizer import _fetch_first_impression_date
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main():
    campaigns = get_all_campaigns()
    total = len(campaigns)
    missing = [c for c in campaigns if not c.get("launch_date")]
    logger.info(f"{len(missing)} of {total} campaigns have no launch_date — backfilling from Google Ads")

    filled = 0
    skipped = 0
    for c in missing:
        cid = c.get("campaign_id", "")
        name = c.get("campaign_name", cid)
        resource = c.get("gads_campaign_resource", "") or ""
        if not resource:
            logger.warning(f"  SKIP '{name}' — no gads_campaign_resource")
            skipped += 1
            continue

        date_str = _fetch_first_impression_date(resource)
        if date_str:
            set_campaign_launch_date(cid, date_str)
            logger.info(f"  ✅ '{name}' → {date_str}")
            filled += 1
        else:
            logger.warning(f"  ⚠️  '{name}' — no impression data found in GAds (new or no spend)")
            skipped += 1

    logger.info(f"\nDone. Filled={filled}, Skipped={skipped}, Already-had-date={total - len(missing)}")


if __name__ == "__main__":
    main()
