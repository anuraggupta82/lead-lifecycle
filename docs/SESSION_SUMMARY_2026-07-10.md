# Session Summary — 2026-07-10

Continuation of the income/attribution work (see `SESSION_SUMMARY_2026-07-08.md` and `-07-09.md`). Today: Call Analysis transcription cost-gate, broadened income capture to **all** GAds patients, and confirmed + documented the Google call-report match.

## Guiding decision (owner): unify the GAds definition
The pipeline and income attribution must use the **same** "is this a Google Ads patient" rule — **gclid OR CallRail `source='google_ads'`** — no separate pathways. Then: pick the GAds patients the pipeline shows and get their per-patient income. gclid patients already carry a campaign; no-gclid (call-extension) patients get income captured now and are assigned a campaign later via the transcription feature.

## What shipped (committed + pushed)
1. **Auto-transcribe GAds calls only** — `get_calls_needing_processing` (database.py:9505) now gates the AUTO pipeline to Google Ads calls: `gads_call_id` set OR `lead_id` set OR EXISTS a `callrail_calls` row `source='google_ads'` (mirrors `get_mango_calls_needing_od_match`). Manual per-call transcription unaffected. **Impact (live):** of 491 inbound ≥30s calls/90d, ~190+ are GAds → ~300 (~60%) no longer auto-transcribed → cuts Vertex/Whisper cost.
2. **Capture OD income for ALL GAds patients** — `_collect_lead_targets` (od_payment_sync.py) was gated `gclid != ''` → now `gclid present OR EXISTS callrail_calls source='google_ads'` for the lead (same definition as the pipeline filter). `existing_patient=0` gate preserved. Unit test added (8 pass). **Verified live:** Paul Varghese (call-extension, no gclid) now shows **Collections $50** on his card. Commit `a18596c`.

Both: Sonnet-built, Opus-verified (py_compile + tests + live check).

## Confirmed already-implemented (documented, no build) — Google call-report match
Owner asked whether the Google call-report match exists. **It does, and it works.** `reconcile_attribution` (mango_service.py:862): CallRail-bridge tier (938-1036) + direct `gads_call_view` time-match (ATTR-FIX2, 1044-1095) — unmatched Mango call → `RECEIVED` report row within ±60s → stamps `gads_call_id` + `attributed_ad_group`, `match_method='gads_time_match'` @0.85 (time-only; Google redacts area code). `_parse_gads_dt`:594 ET→UTC. Endpoints: `/api/admin/gads/call-view`, `/api/admin/gads/call-conversions` (`matched_to_mango`), `/api/admin/mango/reconcile-now?days=N`. **State (verified Jul 10):** 115 reported calls, **87 matched (~76%)**, sync current. Fully documented in Plan §2.2.
- **Why DJL/Paul still lack a campaign:** their calls aren't in `gads_call_view` at all — Google didn't report them (Jun 23 had only 2 reported calls, both MISSED, other campaign). So there's nothing to match. Only Gemini transcript inference can assign them.

## Investigated, deferred (owner: don't get distracted)
**Sync-chain ordering** — mapped the actual `run_unified_od_sync` order: income refresh (steps 5-6) runs *before* call attribution (steps 7-8), and `run_full_od_sync` (step 3) matches leads but NOT mango calls (the call matchers `match_mango_calls_to_od_patients` / `match_calls_to_od_appointments` aren't in the chain — they run via Mango ingestion). Proposed reorder documented in Plan §2.1b. **Parked** per owner so income work isn't distracted; call matchers still run via ingestion.

## §2.1b status → 🟡 mostly done
Per-patient income for ALL GAds patients is DONE. Deferred/non-blocking: sync-chain ordering, 365d-vs-lifetime INCOME/ROI basis decision, EV compute, consolidate refresh-income endpoints, minor booked_override precision.

## Notes added for later
- **Call attribution VERIFY-WHEN-DONE:** the no-gclid patients' income is already captured (rolls under no/unknown campaign); when the Gemini campaign-assignment feature completes, VERIFY it correctly reflects at the campaign level (no income lost/double-counted).
- **Owner-side console check:** Google under-reports some call-extension calls (~24% of reported calls unmatched; DJL/Paul not reported at all). Verify Google Ads call-reporting is enabled and whether the call extension uses Google's forwarding number vs the CallRail number (Google won't report the latter).

## Next
- **Gemini transcript-based campaign inference** (the real remaining piece for no-gclid patients): assemble active-campaigns+context payload → classify transcript → confidence threshold so low-confidence guesses don't create silent attribution.
- **Call Analysis pagination** (quick; frontend hardcodes limit 200 at index.html:3887).
- Owner to specify other Call Analysis UI fixes (placeholder in Plan §2.3).

---

## Session 2 — GMB Pipeline Fix + Gemini Transcript Campaign Inference

### 3. GMB Pipeline Fix (frontend/index.html)
- **Problem:** `hasGadsAttribution` check (line ~1590) treated any non-empty `campaign_name` as GAds-attributed. GMB calls from CallRail had `campaign_name = "Google My Business"`, passing the filter and polluting the GAds-only pipeline view with existing patients calling from Google My Business.
- **Fix:** Added `knownCampaignNames` Set built from the `campaigns` prop (actual GAds campaigns fetched from Google). Replaced bare `l.campaign_name` check with `knownCampaignNames.has(...)`. GMB and other non-GAds CallRail campaign labels now properly excluded.
- **Files:** frontend/index.html (+16/-3)

### 4. Gemini Transcript Campaign Inference (mango_service.py + main.py)
**The remaining piece for no-gclid patients (DJL, Paul, etc.) — now built.**

New attribution tier in `reconcile_attribution()` waterfall, positioned after `gads_time_match` and before `phone_exact`:
- `gemini_inferred` — confidence 0.80 (high) or 0.65 (medium)
- `gemini_low_confidence` — confidence 0.45 (low); stored in DB but NOT surfaced as campaign-level attribution

**How it works:** For GAds calls (CallRail `source='google_ads'`) with no gclid and no `gads_call_view` match, reads the call transcript, feeds Gemini the currently-running campaigns + their service focus + top keywords, classifies to best-match campaign with confidence score.

**New code:**
- `_build_campaign_context_for_inference()` — assembles campaign names, service types, top keywords into structured context
- `infer_campaign_from_transcript()` — calls Gemini 2.5 Flash (Vertex AI, temperature 0.1, JSON response mode) with the transcript + campaign context
- `_CAMPAIGN_INFERENCE_PROMPT` — structured prompt template
- `POST /api/admin/mango/infer-campaigns` — admin batch endpoint for manual inference runs

**Updated attribution waterfall:**
1. `callrail_confirmed` (0.95) — CallRail campaign tracker match
2. `gads_time_match` (0.85) — Google call-report ±60s time match
3. `gemini_inferred` (0.80/0.65) — transcript-based Gemini classification (high/medium)
4. `gemini_low_confidence` (0.45) — low confidence, stored but not surfaced
5. `phone_exact` (0.90) — exact phone number match to known tracking number

**Key design decision:** Low-confidence inferences (0.45) are persisted for auditing/review but do NOT roll into campaign-level income or attribution metrics. This prevents silent mis-attribution from ambiguous transcripts.

**Files:** backend/mango_service.py (+184), backend/main.py (+63)

### Status
- 3 files modified, 260 insertions, 3 deletions
- **NOT YET COMMITTED** — pending owner review
- The VERIFY-WHEN-DONE note from earlier session is now actionable: once committed and run, verify that DJL/Paul-type patients correctly show campaign-level income after Gemini inference assigns them

## Git (verified via git)
Working tree: 3 files modified (not yet committed). Latest committed: `a18596c` (income for all GAds patients) → `913a041` (callrail gads no gclid) → `559b1df` → `35e0fd2` → `fa32300`. Transcription-gate + income-broadening committed + pushed; GMB fix + Gemini inference pending commit.

---

## Session 3 — Pushed GMB fix + Gemini inference; DISCOVERED Mango↔CallRail linking bug

### What was committed and pushed (via GitHub Desktop)

**5. GMB Pipeline Fix (frontend/index.html)**
- Added `knownCampaignNames` Set in the `filtered` useMemo, built from `campaigns` prop only (actual GAds campaigns from Google)
- Replaced bare `l.campaign_name` in `hasGadsAttribution` with `knownCampaignNames.has(...)`
- Removed pipeline-sourced campaign names from `campaignChipNames` — now only shows known GAds campaigns
- **Partial fix:** "gmb" chip STILL appears because "gmb" is in the backend's `campaignStats` data, not just pipeline leads. Needs backend investigation to find which endpoint/query includes it.

**6. Gemini Transcript Campaign Inference (mango_service.py + main.py)**
- All code from Session 2 committed and pushed
- Admin endpoint changed from POST-only with header auth to GET+POST with `_require_admin_media` (supports `?pw=` query param for browser access)
- **STATUS: BLOCKED** — see discovery below

### KEY DISCOVERY: Mango↔CallRail Phone Number Linking Bug

`_link_unmatched_callrail_to_mango()` (mango_service.py:506) creates digit-only variants from CallRail numbers (`+17744524631`, `17744524631`, `7744524631`) and does SQL `WHERE from_number IN (...)`. But Mango stores formatted phone numbers like `(774) 452-4631`. The exact string IN match fails silently.

**Impact — breaks the entire chain:**
- No link → no `mango_call_id` on CallRail rows
- No `mango_call_id` → no transcript access for those calls
- No transcript → Gemini inference finds 0 eligible calls
- The entire Gemini campaign inference feature is inert until this is fixed

**Proof:** DJL ENTERPRISE's call IS in mango_calls (uuid 4736797033, Jun 23, 17:19 duration, from `(774) 452-4631` to `(508) 318-4477`), but the CallRail row has empty `mango_call_id` because `(774) 452-4631` != `7744524631`.

**Fix options:**
1. Normalize in SQL: strip non-digits from `from_number` in the WHERE clause (e.g., `REPLACE(REPLACE(REPLACE(from_number, '(', ''), ')', ''), '-', '')`)
2. Normalize on Mango ingestion: store phone numbers as digits-only when inserting into mango_calls
3. Both: normalize on ingestion going forward + SQL normalization for historical data

### Other issues identified (plan items, not implemented)

**A. GMB chip still showing** — frontend fix was necessary but insufficient. "gmb" is in backend `campaignStats`. Need to trace which backend endpoint/query includes it.

**B. Non-blocking startup** — Move 4 sync calls to background to cut 30-60s startup time. `backfill_communication_log` MUST stay blocking (prevents duplicate sends before scheduler starts).

**C. Auto-transcription gate false positives** — database.py:9505 auto-transcribes calls from GAds-labeled DNI pool even when CallRail `source` is Direct/Organic (e.g., Ilias Pritsoulis). Should check `callrail_calls.source='google_ads'`, not just tracker label.

**D. OD existing-patient timing** — Patients who convert from calls get retroactively flagged as "existing" after OD entry. Fix: compare OD `SecDateEntry` vs call date — if patient created AFTER the call, treat as "new_converted" not "existing_active".

### Correct next-steps sequence (set this session)
1. **Fix Mango↔CallRail phone number linking** — MUST be first, unblocks Gemini inference
2. **Fix auto-transcription gate** — prevents token waste on non-GAds calls
3. **Non-blocking startup** — quality of life, cuts load time
4. **GMB backend investigation** — find where "gmb" enters campaignStats
5. **OD existing-patient timing** — affects conversion upload accuracy
6. **Verify Gemini inference works** — after #1, re-run infer-campaigns

### Files pushed this session
- `frontend/index.html` — GMB pipeline fix (knownCampaignNames)
- `backend/mango_service.py` — Gemini inference logic
- `backend/main.py` — admin endpoint (GET/POST with ?pw= auth)
