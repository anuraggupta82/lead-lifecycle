# Session 18 — Jul 15-16, 2026

## Completed

### §2.3m: Existing-Patient Misclassification + Auto-Transcription Gate
- **Problem:** `_classify_od_status()` classified patients as `existing_active` based on ANY appointment entry before the call, even if never completed a visit. Sara Hanna (PatNum 5800) was tagged "Existing" despite being a new funnel_modal lead.
- **Fix 1 (od_matcher.py):** Changed to require COMPLETED appointments (`AptStatus=2`) with `AptDateTime` before call time. Both `_get_od_patient_info_by_phone` and `_get_od_patient_info_by_patnum` now fetch `earliest_completed_apt`. Same-day edge case handled correctly.
- **Fix 2 (mango_service.py):** `_queue_process_if_needed()` now has dual gate: source (gads_call_id/fbclid/paid match_method) AND patient status (not existing). Non-qualifying calls still available for manual transcription.
- **Opus-caught bug:** Stale-dict — `mc` dict wasn't updated with new `gads_call_id`/`match_method` before gate check. Fixed by updating `mc` at callrail_confirmed and gads_time_match call sites.
- **Future-proofing:** fbclid check added for Meta marketing (currently no column on mango_calls, safely returns None).

### Payment Sync Fix (from prior session, committed)
- `od_payment_sync.py` `_collect_lead_targets()` — added `campaign_id` gate so manually attributed leads (like Claire Richard) get payment sync. Commit 9ef57bc.

### Firestore Sync Investigation
- Sync returned 0 new / 14 skipped — all 14 docs already had matching leads in local DB.
- Sara's Jul 14 resubmission absorbed into existing Jun 17 lead (deterministic lead_id from email hash).
- Of 3 reported new submissions, only Sara's was in Firestore. Other 2 may not have reached Firestore.

### Conversion Upload Timestamp Fix
- **Problem:** "Qualified Lead" conversions for Andre, Sara, Richard failing with "conversion_date_time precedes the click".
- **Fix (google_ads_conversions.py):** Added click-time floor check in `upload_offline_conversions()`. Looks up `click_date` from `gads_clicks` table, computes floor = click_date + 30h UTC, bumps `ts_str` if below.
- Commit 7635255.

### §2.8: Non-blocking startup
- Moved 3 non-critical sync calls to background daemon thread. Portal startup reduced from 30-60s to ~1s.
- `backfill_communication_log()` stays blocking (prevents duplicate emails).
- Commit 7b7942c.

### §2.3n: CallRail→Mango Propagation + Name Fix + Pipeline Sort
- **Problem 1:** CallRail webhook arrives after mango sync → `gads_call_id`/`match_method` stay empty → auto-transcription gate blocks. Example: Paula Giangregorio's 10m56s GAds call never auto-transcribed.
- **Fix (mango_service.py):** `_link_unmatched_callrail_to_mango()` now propagates `gclid`→`gads_call_id`, sets `match_method='callrail_confirmed'`, and re-triggers `_queue_process_if_needed()`.
- **Problem 2:** Pipeline cards showed CallRail caller ID names ("GIANGREGORIO,PA") instead of OD-matched name.
- **Fix (mango_service.py):** `finalize_call_lead()` now uses `od_patient_name` as higher-priority fallback over `ai_patient_name`. Guarantor block still uses `ai_patient_name`.
- **Problem 3:** Pipeline kanban sorted by `updated_at` — old re-enriched leads jumped to top.
- **Fix (frontend/index.html):** Added `created_at DESC` sort in `byStage` useMemo.

### §2.3p: Webhook gclid Propagation + Name Cleanup
- **Problem 1:** CallRail webhook links calls to mango rows but doesn't propagate `gclid` → `gads_call_id`. Webhook-linked calls never trigger auto-transcription.
- **Fix (callrail_webhook.py):** After `_upsert_callrail_call()`, added propagation block: checks mango_uuid + click_id + google_ads source → updates gads_call_id, match_method='callrail_confirmed', re-triggers `_queue_process_if_needed()`.
- **Problem 2:** Pipeline cards showed caller-ID names (ALL CAPS, commas) for leads finalized before §2.3n.
- **Fix (main.py):** Added `POST /api/admin/calls/fix-caller-id-names` endpoint. Fixed 10 leads: GIANGREGORIO,PA→Paula Giangregorio, HELDENBERGH B.→Brian Heldenbergh, etc.

### Docs added to git repo
- Commit `a197bc5` — Added `docs/` folder with Plan.md, CLAUDE.md, Marketing master reference, all session summaries

## Git Commits (all pushed)
- `9ef57bc` — Fix: Include campaign-attributed leads in OD payment sync (Claire $24K)
- `97fd92e` — §2.3m: Fix existing-patient misclassification + gate auto-transcription
- `7b7942c` — §2.8: Non-blocking startup (3 syncs to background thread)
- `7635255` — Fix conversion upload timestamp — use click_date as floor
- `b68e74e` — §2.3n: CallRail→Mango propagation, OD name upgrade, pipeline sort
- `a197bc5` — Add project docs to repo (Plan.md, session summaries, CLAUDE.md, master reference)
- (pending) — §2.3p: Webhook gclid propagation + caller-ID name cleanup endpoint

## Pending
- §2.3o: Custom procedure code detection (Cnxtsmile, D7210, procedurelog check)
- Investigate why 2 of 3 Jul 14 form submissions didn't reach Firestore
- Claire Richard conversion upload to Google Ads (no gclid — cannot upload via standard offline conversion)
