# Session Summary — 2026-05-22
## PR 0: GAds-Only Pipeline Filter

### Problem
Pipeline was ingesting every inbound contact — not just Google Ads leads. Two visible examples: "CallRail" (random call, no GAds attribution) and "Candice Chase" (self-booked scheduler) were appearing alongside real GAds leads.

### Solution
Backend view filter (not an ingestion gate) applied at query time. Default behavior shows only GAds-attributed leads; toggle exposes all sources.

**Filter criteria — lead shown if ANY of:**
- `gclid` is present
- `campaign_id` is present
- `utm_source` LIKE `'google%'` or `'%cpc%'`
- `notes` mention `'Google Ads'` or `'gclid'` (CallRail fallback)
- `source = 'manual'` (always shown)

### Files Changed

| File | Change |
|---|---|
| `backend/database.py` | `get_all_leads()` — `gads_only: bool = False` param + COALESCE null-safe WHERE clause + notes fallback; `get_pipeline_stats()` — same param, all 5 internal queries respect filter |
| `backend/main.py` | `/api/pipeline`, `/api/pipeline/enriched`, `/api/admin/stats` — all accept `show_all: bool = False`; enriched endpoint uses inline `GADS_FILTER`; stats passes `gads_only=not show_all` |
| `frontend/index.html` | `showAllSources` state in App; `loadData()` appends `?show_all=true` when toggled; `useEffect` re-runs on state change; prop-drilled through `PipelineTab` → `KanbanBoard`; "📱 GAds Only" / "👁 All Sources" toggle in filter bar |

### Opus Review Amendments — All Implemented
1. COALESCE null-safety on gclid/campaign_id checks
2. Notes fallback for CallRail leads missing UTM params
3. `source='manual'` always shown
4. Stats endpoint also filtered (stage count badges consistent with kanban)

### Verification
SQL confirmed on live DB: "CallRail" (no gclid) hidden, "Test Caller" (gclid present) shown, "Gupta Anurag" (source=manual) shown.

### Sequencing
PR 0 in the Smart Pipeline Routing plan. When PR 2 ships (`auto_enter_pipeline_rule` + `pipeline_default_visibility` on campaigns table), the hardcoded SQL filter in `database.py` gets replaced with a JOIN to the campaigns table.

### Git Commit
**Summary:** `PR 0: GAds-only pipeline filter`
**Description:** Add show_all toggle and gads_only SQL filter to pipeline view. Default = GAds leads only. All other callers (od_matcher, mango_service, etc.) unaffected. Stopgap until PR 2 campaign-level visibility rules ship.
