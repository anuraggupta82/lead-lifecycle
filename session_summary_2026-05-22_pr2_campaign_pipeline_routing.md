# Session Summary — 2026-05-22: PR 0 + PR 2 Campaign Pipeline Routing

## What Was Built

### Problem
The pipeline dashboard was showing ALL inbound leads — every CallRail call (regardless of attribution) and every online scheduler booking — not just Google Ads leads. This made the pipeline unactionable.

### PR 0 — GAds-Only View Filter (Stopgap)
**Shipped 2026-05-22**

Added a hardcoded SQL filter to show only leads with a Google Ads attribution signal:
- `gclid` present (click-through)
- `campaign_id` present (GAds campaign tag)
- `utm_source` starts with 'google' or contains 'cpc'
- `notes` mentions 'Google Ads' or 'gclid' (CallRail fallback)
- `source = 'manual'` (dentist-entered leads always shown)

Frontend: "📱 GAds Only" / "👁 All Sources" toggle in the pipeline filter bar.

Backend: `get_all_leads(gads_only=True)`, `get_pipeline_stats(gads_only=True)`, `get_pipeline_enriched(show_all=False)` all honor the filter.

**Superseded by PR 2** (same toggle, but now driven by per-campaign DB rules instead of hardcoded SQL).

### PR 2 — Campaign-Level Pipeline Routing Rules
**Shipped 2026-05-22 | Opus-reviewed: CLEAN**

DB-driven per-campaign routing rules replace the hardcoded PR 0 filter.

#### Schema additions (campaigns table)
```sql
auto_enter_pipeline_rule TEXT NOT NULL DEFAULT 'always'
  -- 'always' | 'when_no_booking' | 'when_follow_up_flagged' | 'never'

pipeline_default_visibility TEXT NOT NULL DEFAULT 'shown'
  -- 'shown' | 'hidden'
```

#### Shared SQL helpers (database.py)
```python
def _pipeline_visibility_clause(lead_alias: str = "leads") -> str
def _pipeline_visibility_join(lead_alias: str = "leads") -> str
```

Logic: leads matched to a campaign → hide only if rule = 'never'; leads with no campaign match → fall back to PR 0 heuristic (gclid/utm_source/notes/source='manual').

#### Per-campaign default assignments (migration, idempotent)
| Campaign type | auto_enter_pipeline_rule | pipeline_default_visibility |
|---|---|---|
| Implant, All-on | always | shown |
| Emergency | always | shown |
| General Dentistry | when_follow_up_flagged | shown |
| Brand Awareness | never | hidden |
| Dentures | always | shown |

#### Frontend additions
- **Campaign table**: New "Pipeline" column with an inline `<select>` that PATCHes the rule on change. Color-coded: gray=always, amber=conditional, red=never. Optimistic state update.
- **Campaign wizard Step 2**: "Pipeline Routing" radio group with 4 options and descriptions. Default: 'always'.

#### Files changed
- `backend/database.py` (+257/-83): migration, shared helpers, updated get_all_leads/get_pipeline_stats/create_campaign/update_campaign_fields
- `backend/main.py` (+54/-10): import helpers, validators on request models, get_pipeline_enriched update
- `frontend/index.html` (+94/-1): Pipeline column in campaign table, wizard radio group, colSpan 14→15

## Opus Review Summary
Reviewed by Opus — CLEAN.
- Verified: 22 columns = 22 `?` placeholders = 22 values in `create_campaign` INSERT
- Verified: no remaining `colSpan={14}` in frontend
- Confirmed: LEFT JOIN alias resolution, NULL handling, migration idempotency all correct
- Minor notes (no action taken): 3 dead-code UPDATE statements that set defaults to defaults; docstring coupling note for shared helpers

## Architecture Notes
- `gads_only=True` must ONLY be passed by pipeline display callers (get_pipeline_enriched, /api/pipeline, /api/admin/stats). All other callers (od_matcher, mango_service, ai_optimizer) must use full unfiltered set.
- `SELECT leads.*` (not `SELECT *`) required when doing LEFT JOIN to avoid column name collision with `c.campaign_id`.
- `when_no_booking` and `when_follow_up_flagged` rules behave as `always` until PR 1 (Gemini classifier) ships — only `never` actively filters at SQL level in PR 2.

## Git Push
Ready to push. See git summary below.

## Next Steps
- PR 1 (Gemini Call Intelligence — Gemini classifier for follow-up + sentiment)
- PR 3 (Pipeline UI per-campaign filters + saved default view)
- PR 4 (Booked-stage entry for self-scheduled new GAds patients)
- PR 5 (Existing-patient guard + optimizer noise feedback)
- CallRail: Add CC before 2026-06-04 trial end
