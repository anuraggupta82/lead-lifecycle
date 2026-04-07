# lead-lifecycle

## Purpose
Central hub connecting ad clicks → leads → bookings → treatment → revenue attribution. The closed-loop ROI engine for Grafton Dental Care's All-on-X PPC funnel. See the project README for the full data chain.

## Stack
Python 3, Flask (dashboard on port `7070`), SQLite (at `~/grafton_pipeline/pipeline.db` — outside the workspace), launchd (scheduling), Google Ads API, Twilio SMS, Gmail SMTP, Firestore, OpenDental MySQL.

## Run (dev)
```
./Launch Pipeline.command
# or:
cd backend && pip install -r requirements.txt && python app.py
```
Dashboard: http://localhost:7070

## Deploy
Currently runs on the dev Mac; production target is the office Mac Mini. Auto-starts via `com.grafton.pipeline.plist` launchd plist. The plist has hardcoded absolute paths under `/Users/anurag/Documents/Projects/gdc-apps/marketing/lead-lifecycle/` — patched during the gdc-apps restructure. See `_shared/infrastructure/mac-mini.md`.

## The 11-stage pipeline
`new → engaged → smile_completed → nurturing → scheduled → confirmed → showed → tx_presented → tx_accepted → tx_completed → [cold]`

## Scheduled jobs
- **Every 15 min:** Firestore sync (pulls new leads from nxtsmile.com)
- **Day 1, 3, 7, 14, 21, 30:** automated email + SMS nurture sequence (per lead)
- **6:00 AM daily:** GCLID resolver (matches click IDs to keyword/campaign/CPC)
- **7:00 AM daily:** AI optimizer (pause losers, boost winners)
- **10:00 PM nightly:** OpenDental patient match (SHA-256 phone/email hash)
- **11:00 PM nightly:** Google Ads offline conversion upload

## Integrations used
- [Google Ads](../../_shared/integrations/google-ads.md) — GCLID resolver, conversion uploader, optimizer
- [Firestore](../../_shared/integrations/firestore.md) — lead intake from nxtsmile.com
- [OpenDental](../../_shared/integrations/opendental.md) — revenue attribution via patient hash match
- [Twilio SMS](../../_shared/integrations/twilio-sms.md) — Day 3, Day 21 SMS touches
- Gmail SMTP — email touches

## Database (SQLite)
`leads`, `events`, `follow_up_queue`, `conversion_uploads`, `lead_notes`, `od_matches`. Located at `~/grafton_pipeline/pipeline.db` — **not** inside the repo, so it survives repo moves and `git clean`.

## Sibling
[`../lead-lifecycle-scripts/`](../lead-lifecycle-scripts/) — standalone Google Ads helper scripts that import from this repo's `backend/config.py`. Sibling, not nested, by design.

## Gotchas
- The launchd plist has **hardcoded absolute paths**. If the workspace ever moves, run a sed patch on it.
- The SQLite DB lives at `~/grafton_pipeline/pipeline.db` (outside the workspace). Don't relocate it without updating `.env` `DB_PATH`.
- Conversion upload latency: Google Ads takes 24–48 hours to incorporate uploaded conversions into bidding. Don't expect same-day signal.
- Default click attribution window is 30 days; All-on-X cycles are often longer — there's a real measurement gap.
- Don't send SMS in quiet hours; the scheduler enforces send windows — don't bypass.
- A2P 10DLC registration is required for US business SMS — verify it's in place before scaling.
- The CLAUDE.md inside this repo describes the AI marketing engine identity for Claude when running optimizer recommendations — follow it when iterating on the optimizer prompts.
