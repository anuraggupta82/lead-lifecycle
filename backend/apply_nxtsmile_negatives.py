"""
Apply negative keywords to nXtsmile Implants (05/23) campaign.
Run from the backend directory with the venv active:

    cd /path/to/lead-lifecycle/backend
    source venv/bin/activate   (or: venv/Scripts/activate on Windows)
    python apply_nxtsmile_negatives.py

Pushes all negatives directly to Google Ads and logs to gads_audit_log.
"""

import os, sys
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

from google_ads_write import add_negative_keyword_to_campaign
from database import log_admin_manual_action, update_gads_action_result, set_audit_approval

CAMPAIGN_RESOURCE = "customers/2498049505/campaigns/23870298927"
CAMPAIGN_NAME     = "nXtsmile Implants (05/23 — 100/day) (05/23 23:33)"

NEGATIVES = [
    # ── Wrong procedure: single tooth ───────────────────────────────────────
    "single tooth implant cost",
    "cost of dental implant for one tooth",
    "implant for one tooth",
    "single tooth implant cost without insurance",
    "screwless dental implant cost",

    # ── Snap-on / snap-in dentures (different product) ──────────────────────
    "snap on dentures",
    "snap in dentures",
    "snap in dental implants",
    "snap on dentures near me",
    "snap in dentures near me",
    "affordable snap in dentures near me",

    # ── Clinical trials / free care ──────────────────────────────────────────
    "clinical trials for dental implants near me",
    "paid clinical trials for dental implants near me",
    "dental implant clinical trials near me",
    "dental implants clinical trials near me",
    "dental implant study near me",
    "free dental implants near me",
    "free dental implants for seniors near me",
    "no fee dental implants",
    "free or low cost dental implants from dental schools",

    # ── Dental schools ───────────────────────────────────────────────────────
    "dental schools that do implants near me",
    "harvard dental school implant cost",

    # ── Cheapest / discount (explicit price objectors) ──────────────────────
    "affordable dental implants near me",
    "affordable dental implants",
    "affordable denture implants",
    "cheapest place to get dental implants near me",
    "cheapest place to get all on 4 dental implants near me",
    "full mouth dental implants cost cheapest",
    "cheap dentures",
    "most affordable dental implants",

    # ── Medicare / insurance-driven ──────────────────────────────────────────
    "dentures for seniors on medicare",
    "does medicare pay for dentures",
    "how to get medicare to pay for dental implants",
    "free dental implants for seniors",
    "low cost dentist for seniors near me",
    "medicare oral surgeons near me",
    "affordable dental care for seniors",

    # ── Local competitor names ───────────────────────────────────────────────
    "accord dental grafton ma",
    "dental dreams grafton st",
    "grace dental framingham",
    "grace dental framingham ma",
    "grace dental",
    "webster lake dental",
    "davis orthodontics near me",
    "dudley family dental",

    # ── Nuvia: navigational/location only (brand terms kept for conquest) ────
    "nuvia dental implant center locations near me",
    "nuvia dental implant center wellesley ma",
    "is there a nuvia dental near me",
    "where is nuvia dental implant center located",
    "nuvia location",

    # ── ClearChoice: navigational/location only ──────────────────────────────
    "clearchoice dental implant center framingham",
    "clear choice framingham",
    "clearchoice near me",

    # ── Aspen Dental (budget chain — all negated) ────────────────────────────
    "aspen dental near me",
    "aspen dental implant cost",
    "aspen dental dentures payment plan",
    "aspen dental framingham",
    "pictures of aspen dental dentures",

    # ── Misc off-intent ──────────────────────────────────────────────────────
    "who cannot get dental implants",
    "dental implant eligibility",
    "dentkits",
    "dental implant restoration",
]

def main():
    print(f"Applying {len(NEGATIVES)} negative keywords to:")
    print(f"  {CAMPAIGN_NAME}\n")

    ok = 0
    failed = []

    for kw in NEGATIVES:
        action_id = log_admin_manual_action(
            operation="add_negative_keyword",
            entity_type="campaign",
            entity_id=CAMPAIGN_RESOURCE,
            entity_name=CAMPAIGN_NAME,
            before={},
            after={"keyword_text": kw, "match_type": "BROAD",
                   "campaign_resource": CAMPAIGN_RESOURCE},
            reason="nxtsmile_implants_negative_audit_may25_2026",
        )
        try:
            add_negative_keyword_to_campaign(CAMPAIGN_RESOURCE, kw, "BROAD")
            update_gads_action_result(action_id, executed=True, execution_result="success")
            set_audit_approval(action_id, "admin")
            print(f"  ✓  {kw}")
            ok += 1
        except Exception as e:
            update_gads_action_result(action_id, executed=True,
                                      execution_result="error", error_detail=str(e))
            print(f"  ✗  {kw}  →  {e}")
            failed.append(kw)

    print(f"\n{'='*60}")
    print(f"Done. {ok} applied, {len(failed)} failed.")
    if failed:
        print("Failed keywords:")
        for kw in failed:
            print(f"  - {kw}")

if __name__ == "__main__":
    main()
