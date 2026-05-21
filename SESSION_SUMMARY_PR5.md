# PR 5 Session Summary — CallRail Existing-Patient Guard + OD Enrichment
**Date:** 2026-05-21

## What was built

PR 5 adds an existing-patient guard to the CallRail webhook pipeline and a background OD enrichment job for callrail_calls rows.

### Core feature: Existing-patient guard
When a call comes in via CallRail and no existing lead matches by phone, the system now checks OpenDental before creating a new marketing lead:
- If the caller is an **existing active or inactive OD patient** → skip lead creation (it's a service call, not a new patient inquiry)
- If OD is unreachable → fail-safe: create the lead anyway (better to over-attribute than drop real new-patient leads)
- Toggle at runtime via `GET/POST /api/admin/callrail/guard-status`

### OD enrichment job
`enrich_callrail_calls_with_od(limit=500)` runs in two passes:
- **Pass 1**: Copy od_patient_num/status from linked leads (pure SQLite, no OD round-trip)
- **Pass 2**: Live OD phone lookup for any rows still unenriched

Runs automatically in the nightly `run_full_od_sync()` cron and on-demand via `POST /api/admin/callrail/enrich-calls`.

## Files changed

| File | Change |
|------|--------|
| `database.py` | Added `od_patient_status` column to callrail_calls (migration), index, and 3 helper functions: `get_callrail_calls_needing_od_enrich`, `update_callrail_call_od_match`, `backfill_callrail_od_from_leads` |
| `callrail_webhook.py` | Added `_existing_patient_guard_enabled()`, `_check_existing_od_patient()`, `_enrich_new_lead_with_od()`. Updated `_upsert_callrail_call` with od_patient_num/status params. Updated `process_webhook` with guard logic block |
| `od_matcher.py` | Added `enrich_callrail_calls_with_od()`. Hooked into `run_full_od_sync()`. Updated `_classify_od_status` docstring + `"no_match"` return value (was incorrectly returning `"new_patient"` for phone-not-found case) |
| `main.py` | Added 3 endpoints: `POST /api/admin/callrail/enrich-calls`, `GET /api/admin/callrail/guard-status`, `POST /api/admin/callrail/guard-status` |

## Opus verification fixes applied
1. **BUG (critical)**: `od_matcher.py` — `row.get("caller_phone_e164")` → `row.get("caller_number")` (Pass 2 was a silent no-op)
2. **WARNING**: `main.py` guard-status GET — now uses `_existing_patient_guard_enabled()` helper for consistent parsing
3. **WARNING**: `_classify_od_status` — returns `"no_match"` (not `"new_patient"`) when phone not found in OD; analytics now distinguishable

## OD patient status values (callrail_calls.od_patient_status)
- `no_match` — phone not in OD (true new patient inquiry)
- `new_patient` — in OD (PatStatus=0) but appointments all booked after this call
- `existing_active` — active OD patient (PatStatus=0, prior appointment exists)
- `existing_inactive` — Inactive/Archived (PatStatus=2/3)
- `unknown` — OD record found but unusual PatStatus (NonPatient, Deceased, Prospective)
- `""` — not yet enriched

## Ready to push
All 4 files pass syntax check. Opus verification complete with all 3 fixes applied.
