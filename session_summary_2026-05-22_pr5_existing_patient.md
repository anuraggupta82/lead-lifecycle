# Session Summary — PR 5: Existing-Patient Guard + Optimizer Noise
**Date:** 2026-05-22

## What was done

### PR 5: existing_patient flag + optimizer noise feedback

**Goal:** Tag leads from existing OD patients so they're hidden from the pipeline worklist (they crowd it without being actionable new-patient leads). Also surface existing-patient call patterns to the AI optimizer as a negative keyword signal.

**Files changed:** database.py, od_matcher.py, lqi_signals.py, ai_optimizer.py

### Part A — Existing-patient guard

**database.py:**
- `existing_patient INTEGER DEFAULT 0` added to leads schema (line ~77, after self_booked)
- Idempotent migration added after self_booked migration
- `upsert_lead()` INSERT: 32 columns/placeholders now (was 31)
- `upsert_lead()` UPDATE: sticky upgrade block — only writes 1, never demotes to 0
- `_pipeline_visibility_clause()`: outer `COALESCE(existing_patient, 0) = 0 AND (...)` guard added — existing-patient leads hidden from pipeline regardless of campaign rule

**od_matcher.py:**
- `match_leads_to_od()`: after `_get_appointment_info()`, sets `existing_patient = 1 if apt_info.get("has_showed") else 0`. UPDATE uses sticky `CASE WHEN ? = 1 THEN 1 ELSE existing_patient END` pattern.
- `sync_scheduler_direct_leads()`: calls `_get_appointment_info(od_conn, pat_num)` at lead creation time to detect prior completed appointments. Sets `existing_patient` in lead_data + event detail + logger.

### Part B — Optimizer noise feedback

**lqi_signals.py:**
- New `collect_existing_patient_calls(conn, days=30)` — queries mango_calls joined to leads, groups by utm_campaign, counts calls where `od_patient_status IN ('existing_active','existing_inactive')`. Returns per-campaign `{existing_calls, total_calls, existing_pct, top_terms[]}` + totals.
- Added to `collect_all()` collectors list under key `"existing_patient_calls"`.

**ai_optimizer.py:**
- `_build_lqi_campaign_slice()`: per-campaign existing_patient_calls lookup added, returned under key `"existing_patient_calls"`
- Per-campaign Claude prompt: section 8 added — advisory when existing_pct >= 0.30 AND total_calls >= 5
- `_build_lqi_account_summary()`: existing_patient_calls added with 30%/5-call pre-filter gate
- Account-level Claude prompt: section 7 added with identical threshold + negative keyword suggestion logic

## Key design decisions
- Existing-patient leads **stay in DB** — not deleted, just hidden from pipeline
- Form/mango/callrail leads get flagged on the NEXT nightly od_matcher run (24h lag acceptable)
- Scheduler leads get flagged at creation time (pat_num available immediately)
- Sticky upgrade: once flagged existing_patient=1, never reverted

## Opus review: CLEAN

## Git push needed
**Summary:** `PR 5 — existing_patient guard + optimizer noise feedback`

**Description:**
```
- Add existing_patient INTEGER DEFAULT 0 to leads table (schema + migration)
- upsert_lead(): INSERT includes existing_patient; UPDATE is sticky (never demotes 1→0)
- _pipeline_visibility_clause(): outer guard hides existing-patient leads from pipeline
- match_leads_to_od(): set existing_patient=1 when OD patient has prior completed appts
- sync_scheduler_direct_leads(): detect existing patient at lead creation time
- lqi_signals: new collect_existing_patient_calls() wired into collect_all()
- ai_optimizer: per-campaign + account-level LQI slice + prompt sections for noise signal
- Opus reviewed CLEAN (32 placeholders balanced, sticky upgrade consistent)
```

Files: database.py, od_matcher.py, lqi_signals.py, ai_optimizer.py
