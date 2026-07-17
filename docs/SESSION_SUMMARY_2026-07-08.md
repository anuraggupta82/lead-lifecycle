# Session Summary — 2026-07-08

**Focus:** §2.1b income/revenue layer — make campaign INCOME & ROI reflect the owner's model (patient = unit, actual collected dollars, deduped, refreshed each sync, rolled up to campaign ROI). Plus a GAds-Only pipeline filter fix. All builds Sonnet-implemented / Opus-verified per project rules.

## Owner decisions locked this session
- **Income = collected dollars only; ROI is based on this number.** Treatment-*planned* value is a separate future feature (case-acceptance tracking), never mixed into income. (`leads.treatment_plan_value` already captures it.)
- **Income model:** a lead is identified → assigned to a campaign (first-touch) → each OD sync records that patient's total collected dollars → updates every sync → sum of all patients rolls up to campaign ROI. Patient (`od_patient_num`) is the unit, counted once.
- **gclid is the sole Google-Ads indicator** for the pipeline. A phone (CallRail) lead is a GAds lead only if Google tagged it with a gclid; otherwise it's a plain call and must not appear in the GAds view.
- **Old `gads_time_match` attribution stays in queue** (not addressed now). It's an honest, labeled fallback grounded in Google's call report; the verified path (gclid via CallRail CDF) supersedes it once CDF delivers. Reconcile as part of an attribution-confidence tiering decision later.

## What shipped (code) — all UNCOMMITTED unless noted
1. **Fix #1 — campaign income double-count** (`database.py` `get_unified_campaigns`, lead_rows query ~4357-4394). **COMMITTED + PUSHED** (with CLAUDE.md). Backported the `get_keyword_stats` `call_attributed_patients` dedup: the four lead-path money sums now exclude patients already counted on the call path. Counts left unconditional. Fixes managed + synthetic loops via the shared query. Preventive (no overlap patient today).
2. **GAds Only filter — require gclid for CallRail** (`frontend/index.html:1474`). Added `callrail` to `NON_GADS_SOURCES` so phone leads show under GAds Only only if they carry a gclid (or campaign_name). Fixes PAUL VARGHESE (callrail, no gclid) appearing in the GAds view. Frontend-only; browser hard-refresh, no server restart.
3. **Fix #4 — payment-sync anchor timezone** (`od_payment_sync.py` `_parse_anchor`). Converts UTC anchor timestamps → America/New_York before `.date()` (OD `PayDate` is Eastern), so evening-ET leads no longer get an anchor a day late that drops same-day payments (e.g. booking-night deposits). Date-only anchors unshifted. Unit test (6 cases).
4. **Fix #2 — existing-patient income leak** (`od_payment_sync.py` `_collect_lead_targets` + `database.py` lead_rows). (a) Both payment-sync lead queries now filter `COALESCE(existing_patient,0)=0`; (b) all four campaign money sums AND the dedup guard with `existing_patient=0`. Existing patients who clicked an ad no longer inflate campaign income; their ad touch is still counted (counts unconditional). Unit test added. Call path was already `od_patient_status='new_patient'`-gated.

Verification: py_compile clean on all; `tests/test_od_payment_sync.py` 7 passed; Opus re-read every diff and re-ran compile/tests independently.

## Fix #3 was a MISDIAGNOSIS — no change made
The prior plan/memory flagged "Fix #3 estimated-vs-collected: `booked_override` seeds full payment, OVER-states." **Traced the code before building and it was wrong:** income is already collected-only.
- `leads.attributed_income` (`database.py:43`) = "actual collections (payments received)", set by od_matcher from `_get_patient_income()` = `SUM(paysplit.SplitAmt)` (od_matcher.py:500,715-720).
- `mango_calls.od_patient_income` (`database.py:1762`) = `SUM(paysplit.SplitAmt)` collected.
- `booked_override` seeds `od_patient_income` (collected), not an estimate.
- `treatment_plan_value` (the planned number) is a SEPARATE column, not in income.
No correct code was changed. (Process note: the bad claim came from repeating a stale prior-session note without re-reading the code — caught at the build gate. Going forward, verify plan/memory notes against code before recommending changes.)

## Remaining §2.1b work (queued)
- **Sync-chain wiring** (the real "updates every sync" gap): nightly chain runs income refresh BEFORE attribution and never runs the call patient/appointment matchers → collected income may not refresh reliably each sync. Re-order: attribute → match OD patient/appointment → refresh collected income.
- **DECISION NEEDED — INCOME/ROI window:** trailing-365d collected vs lifetime collected. Column has a 365d/LTV selector; ROI keys off `income_365d`. Implants collect over months/years (lifetime may reflect true ROI better) vs 365d standard ad window. Pick canonical basis + make column display and ROI basis consistent.
- **APPTS/CALLS column rework** (owner-flagged Jul 8): APPTS should count NEW patients who scheduled (form ∪ call, deduped by patient), click → names → patient card + income; remove CALLS column. APPTS bug: `index.html:7734` counts calls-only while INCOME counts form revenue → nXtsmile $50-but-blank-appt mismatch. Build after the patient ledger so both read one source.
- **Backend/board GAds filter divergence:** KPI count chips use `_pipeline_visibility_clause` (database.py:3403), a different definition than the board's client-side filter. Unify so board == counts.
- **EV (expected value)** compute from OD funnel — feeds conversion upload (deferred).
- **Minor:** `booked_override` seeds lifetime collected into the 365d bucket; upsert `MAX()` can lock a slightly-high `paid_amount_365d`. Seed 0 into 365d (let od_payment_sync window it) or seed ltv only.
- **NOTE:** with CDF/gclid not delivering yet (0/267 calls carry gclid — owner console task), essentially all CallRail leads are currently hidden from the GAds board (incl. DJL). Honest state; they reappear once gclid capture works. Campaign INCOME/APPTS still attribute those calls via `gads_call_view` — separate path.

## Git state / push guidance
- **Pushed:** Fix #1 (`database.py` dedup) + `CLAUDE.md` guardrail.
- **Uncommitted (this session):**
  - `backend/database.py` (Fix #2 existing-patient guard on the four sums)
  - `backend/od_payment_sync.py` (Fix #4 tz + Fix #2 gate)
  - `backend/tests/test_od_payment_sync.py`
  - `frontend/index.html` (GAds filter)
  - `backend/main.py`, `backend/unified_od_sync.py` (older resolver-window 7→30, still pending)
- Suggested commits: (A) income accuracy — database.py + od_payment_sync.py + tests; (B) GAds filter — index.html; (C) resolver window — main.py + unified_od_sync.py.
- **Server restart** needed for the database.py / od_payment_sync.py changes to take effect (running instance has old code). Frontend change needs only a browser hard-refresh.

## Deployment reality (unchanged)
Dashboard runs manually on the owner's MacBook (localhost:7070); syncs are manual; not on the Mac Mini / not 24/7. Transient "Failed to fetch" during live checks = MacBook connectivity blips, not bugs.
