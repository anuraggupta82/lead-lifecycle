# Session Summary — Call List Filters + CallRail Attribution
**Date:** 2026-05-22

## What was done

### Goal
Switch call list attribution from pure Mango/GAds tracking to also include CallRail DNI data. Add stackable filter checkboxes to the call list.

### Opus audit findings (pre-implementation)
- `callrail_calls.mango_call_id` stores `mango_calls.uuid` (not a separate call_id) — all JOINs use `cc.mango_call_id = mc.uuid`
- CallRail's `keyword` field (DNI) was completely unused in attribution chain
- `get_mango_calls_needing_od_match` was too narrow: only matched gads_call_id OR lead_id — excluded CallRail DNI calls
- `attributed_keyword/match_type/ad_group` were already SELECT'd in `get_mango_calls()` but not displayed in frontend

### DB backlog check result
```
0 CallRail-linked Mango calls missing gads_call_id/lead_id
```
No existing backlog to worry about; fix is forward-looking.

---

## PRs shipped (all Opus-reviewed CLEAN)

### PR A — Expand `get_mango_calls_needing_od_match` (database.py)
- Added third eligibility branch: `EXISTS (SELECT 1 FROM callrail_calls cc WHERE cc.mango_call_id = mc.uuid AND cc.source = 'google_ads')`
- Uses EXISTS not LEFT JOIN to avoid duplicate rows when multiple CallRail events (call.created + call.completed) link to same Mango call
- Fully aliases `mc.*` columns for clarity

### PR B — CallRail keyword bridge — Method A-prime (call_keyword_attribution.py)
- Added `callrail_calls` correlated LEFT JOIN to the main SELECT (picks best row: non-empty keyword preferred, then newest by id)
- New **Method A-prime** (confidence 0.85) fires between A (0.95) and B (0.80)
- Fires when `cr_source = 'google_ads'` AND `cr_keyword != ''`
- Writes `method = 'callrail_dni'` to `attributed_keyword_method`
- `counts` dict extended with `method_a_prime` key
- Idempotent: only runs on rows where `attributed_keyword_method` is empty

### PR C — Stackable filters on `/api/admin/calls` (main.py + database.py)
**database.py `get_mango_calls()`:**
- New params: `gads_only: bool`, `patient_status: str` (comma-sep), `converted: bool`
- `gads_only`: WHERE (gads_call_id set) OR (EXISTS callrail google_ads with non-empty keyword)
- `patient_status`: WHERE mc.od_patient_status IN (split values)
- `converted`: WHERE booked_outcome='booked' OR ai_appointment_scheduled=1
- Added CallRail DNI SELECT columns: `callrail_keyword`, `callrail_gclid`, `callrail_campaign`, `callrail_source` via correlated LEFT JOIN (same 1:1 guard as PR B)
- `params[:-2]` COUNT slice remains correct for all filter combos (verified by Opus)

**main.py `/api/admin/calls`:**
- New query params: `gads_only: bool = False`, `patient_status: str = ""`, `converted: bool = False`
- New `attribution_label` branch: `"Ad call (DNI)"` for CallRail google_ads calls with keyword
- All 3 new params passed through to `get_mango_calls()`

### PR D — Frontend: checkboxes + keyword chip + CallRail hint (frontend/index.html)
- 4 new filter state vars with localStorage persistence: `gadsOnly`, `filterNew`, `filterExisting`, `convertedOnly`
- `loadCalls()` sends new params; patient_status logic: New only→`new_patient`, Existing only→`existing_active,existing_inactive`, both→omit, neither→omit
- Both useEffect deps arrays updated (7 vars each)
- `_attrBadge()` redesigned: label badge + keyword chip (tooltip shows method/confidence/ad_group/match_type) + "via CallRail: X" italic hint when CallRail keyword differs from attributed keyword
- Checkboxes added to toolbar with visual separator (1px dividers)
- "Showing X of Y calls · Filtered: GAds New Existing Converted" status line

---

## Opus review: CLEAN
One pre-existing cosmetic issue noted: empty-state `colSpan=11` should be 13 — not introduced by this PR.

---

## Files changed
- `backend/database.py` — `get_mango_calls()`, `get_mango_calls_needing_od_match()`
- `backend/main.py` — `/api/admin/calls` endpoint
- `backend/call_keyword_attribution.py` — SELECT + Method A-prime + counts dict
- `frontend/index.html` — `_attrBadge()`, filter state/hooks, `loadCalls()`, toolbar checkboxes

## Git push needed
**Summary:** `Call list filters + CallRail keyword attribution`

**Description:**
```
- get_mango_calls_needing_od_match(): add EXISTS branch for CallRail DNI google_ads calls
- get_mango_calls(): new stackable filter params (gads_only, patient_status, converted)
- get_mango_calls(): CallRail DNI JOIN — callrail_keyword/gclid/campaign/source in response
- /api/admin/calls: pass-through for 3 new filter params + "Ad call (DNI)" label branch
- call_keyword_attribution: Method A-prime (callrail_dni, 0.85 conf) between A and B
- Frontend: GAds Only / New Patient / Existing Patient / Converted checkboxes (localStorage)
- Frontend: keyword chip in _attrBadge + "via CallRail: X" hint when DNI data differs
- Opus reviewed CLEAN
```

Files: database.py, main.py, call_keyword_attribution.py, frontend/index.html
