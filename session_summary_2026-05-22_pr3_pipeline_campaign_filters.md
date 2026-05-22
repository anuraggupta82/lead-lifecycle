# Session Summary — PR 3: Pipeline Campaign Filters + `when_follow_up_flagged` Enforcement
**Date:** 2026-05-22  
**PRs Shipped:** PR 3  
**Status:** Complete, Opus-reviewed CLEAN (after two bug fixes)

---

## What Was Built

PR 3 of the Smart Pipeline Routing 5-PR plan. Two major capabilities:

### 1. `when_follow_up_flagged` SQL enforcement (backend)

`_pipeline_visibility_clause()` in `database.py` was extended to actually enforce the `when_follow_up_flagged` campaign routing rule that PR 2 introduced but left unenforced.

**Logic added** (correlated subqueries, no new joins for callers):
- If campaign rule is `when_follow_up_flagged`:
  - Show if any `mango_calls` row for that lead has `follow_up_needed = 1` (Gemini flagged it)
  - Show if lead has NO classified calls yet (benefit of the doubt — `NOT EXISTS` where `classified_at != ''`)
  - Hide if lead has classified calls but none were flagged
- `always` / `when_no_booking` / NULL rules: unchanged (short-circuit via `!= 'when_follow_up_flagged'`)
- `never` rule: unchanged (outer `!= 'never'` guard still wins)

No changes to `_pipeline_visibility_join()` or any callers — the mango_calls check is inlined as `EXISTS`/`NOT EXISTS` subqueries.

### 2. Campaign chip filter UI (frontend)

New chip row in KanbanBoard filter bar, below the existing filter controls:

- **Multi-select chips** — one per campaign; clicking toggles inclusion
- **"All" chip** — clears selection (show all campaigns)
- **Client-side filtering** — no backend refetch; full pipeline is still loaded
- **Manual leads always show** — leads with empty `campaign_name` pass through regardless of active chips
- **localStorage persistence** — key `nxtsmile_pipeline_campaign_filter`; survives page reloads
- **Server-side default** — "💾 Save Default" button POSTs to `/api/admin/pipeline-default-campaigns`; on mount, loads and adopts if no localStorage session filter exists
- **Reset button** — appears only when current selection ≠ saved default AND default is non-empty

### 3. Two new API endpoints (backend `main.py`)

- `GET /api/admin/pipeline-default-campaigns` — reads `settings.pipeline_default_campaigns` (JSON array of names)
- `POST /api/admin/pipeline-default-campaigns` — upserts the setting

Both gated by `_require_admin`. No new DB table — uses existing `settings(key, value)`.

---

## Opus Review Findings and Fixes

Two bugs found and fixed before merge:

**Issue 1 (blocking):** `campaignChipNames` useMemo used `c?.campaign_name` but `get_campaign_stats()` rows use key `campaign`. Chips built from the campaigns prop would always be empty; only pipeline-data fallback populated them.  
**Fix:** `(c?.campaign_name || c?.campaign || '').trim()` — handles both shapes.

**Issue 2 (UX data loss):** Campaign filter block `if (!cn || !campaignSet.has(cn)) return false` was dropping leads with no `campaign_name` (manual leads) whenever any chip was selected.  
**Fix:** `if (cn && !campaignSet.has(cn)) return false` — only filter if lead HAS a campaign name that isn't in the selected set.

**Issue 3 (dead code):** `defaultLoaded` state was set but never read. Removed.

---

## Files Changed

| File | Changes |
|------|---------|
| `backend/database.py` | `_pipeline_visibility_clause()` — added `when_follow_up_flagged` EXISTS/NOT EXISTS enforcement |
| `backend/main.py` | Added `List` import; 2 new endpoints + `PipelineDefaultCampaignsRequest` model |
| `frontend/index.html` | KanbanBoard: campaigns prop, chip state, localStorage, save/reset default, chip row UI; PipelineTab: campaigns prop thread-through; App: `campaigns={campaignStats}` passed in |

**Diff stats:** 3 files, ~262 insertions, 13 deletions

---

## PR Sequence Status

| PR | Status | Notes |
|----|--------|-------|
| PR 0 | ✅ SHIPPED | GAds-only view filter stopgap |
| PR 1 | ✅ SHIPPED | Gemini Call Intelligence (8→9 sync steps) |
| PR 2 | ✅ SHIPPED | Campaign-level routing rules |
| PR 3 | ✅ SHIPPED | Pipeline campaign chips + `when_follow_up_flagged` enforcement |
| PR 4 | Pending | Booked-stage entry for self-scheduled GAds patients |
| PR 5 | Pending | Existing-patient guard + optimizer noise feedback |

---

## Git Push Summary

**Title:** `feat(pipeline): PR3 — campaign chip filters + when_follow_up_flagged SQL enforcement`  
**Description:**
```
- _pipeline_visibility_clause(): enforce when_follow_up_flagged via correlated EXISTS on mango_calls
  - show if any call has follow_up_needed=1
  - show if no classified calls yet (benefit of doubt)
  - hide if classified calls exist but none flagged
- New: GET/POST /api/admin/pipeline-default-campaigns (settings table, JSON array)
- KanbanBoard: multi-select campaign chip row with localStorage + server-default persistence
- Fixes (Opus review): campaign key 'campaign' not 'campaign_name'; manual leads now always visible
```
