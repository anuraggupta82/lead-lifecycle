# Session Summary — PR 4: Self-Booked Flag
**Date:** 2026-05-22  
**Session:** Continuation (PR 3 shipped → PR 4 planned and shipped)

## What was done

### PR 4: self_booked flag + badge for scheduler-sourced GAds leads

**Goal:** When the nightly `sync_scheduler_direct_leads()` creates a lead from a visitgdc.com scheduler booking with GAds attribution AND the campaign has `auto_enter_pipeline_rule='always'`, flag that lead as `self_booked=1` and show a badge in the pipeline UI.

**Files changed:**
- `backend/database.py`
- `backend/od_matcher.py`
- `frontend/index.html`

### Architecture decision
PR 4 is implemented in `od_matcher.py`'s `sync_scheduler_direct_leads()` — NOT in the scheduler's `stripe_router.py`. Rationale: the scheduler (Cloud Run) cannot reach the local pipeline SQLite DB; the od_matcher already parses ATTR: markers from OD appointment notes and is the correct integration point.

### Key implementation details

**database.py:**
- `self_booked INTEGER DEFAULT 0` added to CREATE TABLE schema (line ~76)
- Idempotent migration added after `ga4_client_id` migration (line ~2360)
- `upsert_lead()` INSERT: column + `?` placeholder count now 31
- `upsert_lead()` UPDATE: sticky upgrade — dedicated block only writes 1, never overwrites 1→0 (prevents any caller from demoting the flag)

**od_matcher.py:**
- After `attr = _parse_attr_marker(note_text)`, checks if `utm_campaign` is present
- If yes: looks up `auto_enter_pipeline_rule` from local SQLite campaigns table
- Uses `contextlib.closing(_local_conn())` — critical fix per Opus review to prevent connection leak in per-row loop (sqlite3 `with conn:` commits but does NOT close)
- Sets `self_booked=1` only if campaign rule is exactly `'always'`
- `lead_data` includes `"self_booked": self_booked`
- Event detail and logger.info updated to include `self_booked`

**frontend/index.html:**
- `.kc-selfbooked { background: #a7f3d0; color: #047857; }` CSS added
- KanbanCard: `✓ Self-Booked` badge shown when `lead.self_booked == 1`
  - Alongside appointment_date badge if date present
  - Standalone row if no appointment_date (defensive, future-proof)

## Opus review findings
- **Issue 1 (fixed):** Connection leak — `with _conn() as:` doesn't close the connection; per-row loop would leak FDs. Fixed with `contextlib.closing()`.
- **Issue 2 (kept):** Dead UI branch for `!appointment_date && self_booked==1` — harmless, left for future-proofing.
- **Issue 3 (fixed):** Redundant `has_gads_attr` variable removed; simplified to `if utm_campaign_attr:`.
- **Status:** CLEAN after fixes.

## How self_booked flows
1. Nightly od_matcher runs `sync_scheduler_direct_leads()`
2. For each OD appointment with ATTR: marker, parses utm_campaign
3. Looks up campaigns table → if rule='always', sets self_booked=1
4. `upsert_lead()` writes self_booked=1 to SQLite
5. `/api/admin/pipeline-enriched` returns all columns via SELECT * → self_booked flows to frontend
6. KanbanCard renders ✓ Self-Booked badge

## Status
PR 4 shipped. PR 5 (existing-patient guard + optimizer noise feedback) is next.

## Git push needed
**Summary:** PR 4 — self_booked flag for scheduler GAds leads  
**Description:**
- Add `self_booked INTEGER DEFAULT 0` column to leads table (schema + idempotent migration)
- In `sync_scheduler_direct_leads()`: check utm_campaign → look up campaign rule → set self_booked=1 if rule='always'
- Use `contextlib.closing()` for per-row SQLite lookup to prevent connection leak
- `upsert_lead()` INSERT includes self_booked; UPDATE is sticky (only promotes 1, never demotes)
- Frontend KanbanCard: green ✓ Self-Booked badge when self_booked==1
- Opus reviewed CLEAN (connection leak fixed, dead variable removed)
