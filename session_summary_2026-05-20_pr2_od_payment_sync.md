# Session Summary — 2026-05-20

## Topic
Income attribution audit + PR 2: OD payment pull with 365d + LTV tracking.

## What we did

1. **Audited the existing attribution chain.** Found the wiring is largely in place for the Google Ads slice: gclid capture (web, scheduler, smile design), 6 AM `google_ads_sync.sync_gclids_to_keywords()` stamps campaign/ad group/ad/keyword, 10 PM `od_matcher.run_full_od_sync()` matches OD patients + writes `attributed_production`, and `call_keyword_attribution` covers the phone-only Google Ads path at ≥0.55 confidence.

2. **Identified the real gaps** (narrowed scope to Google Ads only):
   - INCOME column reflects *planned/treatment-planned* production, not *collected* dollars.
   - Six separate Admin sync buttons with order dependencies.
   - Office-booked phone callers (no ATTR: marker, no visitgdc.com path) lose future payment attribution.
   - Returning Google Ads patients credited repeatedly with no time-window guard.

3. **Decision: dual-track income.** Store both `paid_amount_365d` (default for ROI/optimizer) and `paid_amount_ltv` (informational, visible on hover). 365d is the canonical decision metric; LTV preserves long-term truth without polluting bid logic.

4. **Drafted PR 2 spec.** `lead-lifecycle/PR2_SPEC_od_payment_sync.md` — full schema migration, `od_payment_sync.py` module, integration into APScheduler at 22:15 ET, new `/api/admin/sync-payments` endpoint, Admin tab buttons, INCOME cell tooltip, 5 pytest tests. Explicitly scoped to NOT change `roas`/`cpl`/optimizer reads.

5. **Sonnet implemented PR 2.** All 5 tests pass. Files touched:
   - **New** `backend/od_payment_sync.py` (~490 lines)
   - **New** `backend/tests/test_od_payment_sync.py`
   - **New** `backend/tests/__init__.py`
   - **Modified** `backend/database.py` — 5 cols on `leads`, 3 cols on `keyword_production_log`, idempotent PRAGMA-checked migration, `get_keyword_stats()` CTE extended with `income_365d`/`income_ltv` (dedup-safe via `call_attributed_patients`), `get_unified_campaigns()` extended similarly
   - **Modified** `backend/config.py` — `gads_attribution_window_days: int = 365`
   - **Modified** `backend/main.py` — `_od_payment_sync_job` at CronTrigger(22:15), `/api/admin/sync-payments` endpoint
   - **Modified** `frontend/index.html` — "Sync OD Payments" + "Backfill All Payments" buttons in AdminTab, INCOME cell tooltip showing Planned / Paid (365d) / Paid (LTV)

6. **Opus reviewed PR 2.** Verdict: **Ship with minor fixes (none required).** All 12 critical-verification points pass. No bugs found. Acceptable risks tracked:
   - Lead-side existing-patient gap (returning patients with new gclid still credited) — anchor-date filter is partial defense, full fix is follow-up PR.
   - Lifecycle event flood on first backfill (hundreds of `payment_pulled` events) — pre-warn or sentinel on first run.
   - Test gaps worth adding: 365d boundary-day test, re-sync with new payments test, kpl-without-mango_calls fallback test.

## What's ready to push

PR 2 is complete and tested. Push when ready.

**Git commit summary:** `PR 2: OD payment pull with 365d + LTV income tracking (Google Ads scope)`

**Git commit description:**
```
Adds od_payment_sync.py module that pulls collected dollars from OpenDental
payment + paysplit tables for Google Ads-attributed patients (gclid leads
and phone-only attributed calls). Stores two parallel buckets per patient:
paid_amount_365d (within 365 days of anchor — used for ROI decisions) and
paid_amount_ltv (lifetime — informational only).

Schema: 5 new columns on leads, 3 new columns on keyword_production_log.
Idempotent PRAGMA-checked migration runs at startup.

Integration: new APScheduler job at 22:15 ET (between OD match and call
production); new POST /api/admin/sync-payments endpoint with days/full
params. Admin tab gets Sync OD Payments + Backfill All Payments buttons.
INCOME cell now shows Planned / Paid (365d) / Paid (LTV) on hover; the
displayed number is unchanged this PR (PR 3 will add the column-header
dropdown to swap display values).

Optimizer untouched — roas/cpl still read from planned production.
get_keyword_stats() CTE and get_unified_campaigns() now surface income_365d
and income_ltv alongside revenue, with the same call_attributed_patients
dedup logic.

5 pytest tests pass: anchor-date filter, call-path patient, idempotency,
existing-patient exclusion, OD-unavailable graceful failure.

Opus-reviewed.
```

## Pending follow-ups

- PR 1: Single "Sync OpenDental" button that wraps Firestore → GAds → OD match → OD payments → call attribution → call production → conversion upload in correct order with progress polling.
- PR 3: 365d / LTV / Planned column-header dropdown; expose `gads_attribution_window_days` in Admin → Settings; optimizer switches from `revenue` (planned) to `income_365d` (paid).
- 3 test cases (boundary, re-sync, missing mango_calls fallback) — quick to add.
- Lead-side existing-patient filter (avoid crediting returning patients with new gclid).
