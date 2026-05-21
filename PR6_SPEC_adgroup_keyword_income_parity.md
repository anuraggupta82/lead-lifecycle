# PR 6 — Ad-group + Keyword Income Parity + Confidence-Tier Breakdown

**Date drafted:** 2026-05-20
**Status:** SPEC — ready for Sonnet implementation
**Dependencies:** PR 4 (KPL paid_amount_365d, confidence_tier), PR 5 (Step-7 resilience)

## Why

After PR 4, the Campaigns table reads `kpl.paid_amount_365d` and shows real collected income. But three sibling surfaces remained on the old data:

1. **`get_ad_group_stats()` (`database.py:5494`)** aggregates `mango_calls.od_patient_income` directly — not from KPL. Inconsistent with the Campaigns table. Calls without an OD match miss income; new patients tracked only via forms aren't counted; and the `attributed_ad_group != ''` filter excludes the exact rows the booked_override path is designed to capture in KPL.

2. **Campaign detail panel → Ad Groups sub-tab (`frontend/index.html:10600-10748`)** renders only `impressions / clicks / CTR / cost / lead_count`. Income exists in the API response (`call_income`, `call_count`) but isn't rendered. After PR 6, both KPL-based `paid_income_365d` and the legacy `call_income` should be visible side-by-side initially, with KPL the primary.

3. **Campaign detail panel → Keywords sub-tab (`frontend/index.html:8787-8958`)** renders only `keyword / match_type / cpc_bid` from the GAds snapshot. No income. KPL has the data at keyword granularity but it's never served.

4. **No confidence-tier rollup anywhere.** When the operator looks at $199 on Emergency Dentistry, they can't tell whether that's high-confidence attribution or a booked_override guess. Important for the CallRail/WhatConverts decision later — we need to be able to answer "what % of tracked income comes from low-confidence attribution?".

## How to apply (future work)

- **Display surfaces include all confidence tiers** (high, low, booked_override, plus NULL for pre-migration rows). Same rule as PR 4.
- **Optimizer surfaces stay 'high'-only** (or NULL). PR 6 must NOT regress this — `intelligence_builder.py` filter from PR 4 stays in place.
- When adding new ad-group or keyword income aggregations, decide once whether the consumer is display or optimizer signal. Display by default; optimizer requires explicit 'high'-only filter.
- The Campaigns table, Ad Groups standalone tab, and detail-panel sub-tabs should all show the same income for the same campaign/ad_group/keyword.

## Architecture

### Backend — `database.py:get_ad_group_stats()`

Add a new CTE alongside `call_income_by_ag`. Don't remove the existing CTE — we want both numbers available initially so we can compare them and verify parity (and so we can see if any income falls out of the new path that the legacy path was catching).

New CTE:
```sql
kpl_income_by_ag AS (
    SELECT
        LOWER(ad_group_name) AS ad_group_key,
        COUNT(*) AS kpl_row_count,
        COALESCE(SUM(paid_amount_365d), 0) AS paid_income_365d,
        COALESCE(SUM(paid_amount_ltv), 0)  AS paid_income_ltv,
        COALESCE(SUM(CASE WHEN confidence_tier = 'high'             THEN paid_amount_365d ELSE 0 END), 0) AS income_high,
        COALESCE(SUM(CASE WHEN confidence_tier = 'low'              THEN paid_amount_365d ELSE 0 END), 0) AS income_low,
        COALESCE(SUM(CASE WHEN confidence_tier = 'booked_override'  THEN paid_amount_365d ELSE 0 END), 0) AS income_booked_override,
        COALESCE(SUM(CASE WHEN confidence_tier IS NULL              THEN paid_amount_365d ELSE 0 END), 0) AS income_unknown_tier
    FROM keyword_production_log
    WHERE ad_group_name != ''
      AND (confidence_tier IN ('high','low','booked_override') OR confidence_tier IS NULL)
    GROUP BY LOWER(ad_group_name)
)
```

LEFT JOIN to the main SELECT and add fields to the returned row dict:
- `paid_income_365d`, `paid_income_ltv`
- `kpl_row_count`
- `income_high`, `income_low`, `income_booked_override`, `income_unknown_tier`
- Keep `call_income` and `call_count` (legacy) untouched

Existing ROI calc: leave alone for this PR. The detail-panel display can compute its own ROI client-side with `paid_income_365d / cost` (matching the Campaigns table pattern from PR 3).

### Backend — campaign detail endpoint `main.py:7316`

No SQL changes needed at the endpoint level — the dict from `get_ad_group_stats` now includes the new fields and they flow through `admin_campaign_detail`. Verify by reading the endpoint and confirming nothing strips fields before serialization.

**Keyword-level KPL rollup — new helper in `database.py`:**
```python
def get_keyword_kpl_rollup(campaign_name: str, days: int = 30) -> list[dict]:
    """Return per-keyword income rollup from KPL for a given campaign,
       restricted to the lookback window via started_at on the source call.
       Used by the campaign detail panel Keywords sub-tab.
    """
```

Body: SELECT `keyword_text`, `match_type` (if available; else NULL), `COUNT(*)`, `SUM(paid_amount_365d)`, `SUM(paid_amount_ltv)`, plus per-tier breakdown columns, FROM `keyword_production_log` WHERE `campaign_name = ?` AND `keyword_text != ''` GROUP BY `keyword_text`. Filter same as ad-group CTE for tier inclusion.

Wire into `admin_campaign_detail`: add `"keyword_income": get_keyword_kpl_rollup(campaign_name, days=days)` to the returned dict.

### Frontend — Ad Groups sub-tab `index.html:10600-10748`

Header row: add three new columns between "Cost" and "Leads": **Income (365d) · Tier Mix · ROI**.

Per-row render:
- **Income (365d):** `ag.paid_income_365d || ag.call_income || 0`, formatted `$N`. Tooltip on hover shows `LTV $X` and `KPL rows: N`.
- **Tier Mix:** small inline chip cluster — `H$X` (green), `L$X` (yellow), `B$X` (purple) for high/low/booked_override. Hidden if all three are zero. Use the same micro-pill style as existing tier badges. NO icons inside the metric cell (respects [[feedback-campaign-table-ui]]).
- **ROI:** `(income - cost) / cost * 100`, formatted `±N%`. Same color rules as Campaigns table (negative red, positive green).

Sort current default stays unchanged.

### Frontend — Keywords sub-tab `index.html:8787-8958`

Currently renders pills from `gadsSnap.keywords` (text only). PR 6 adds a separate table BELOW the existing pills (don't replace pills — operators use them for quick visual scan).

Table headers: **Keyword · Match Type · KPL Rows · Income (365d) · LTV · Tier Mix**.

Data source: new `data.keyword_income` array from the endpoint. Render only keywords with `paid_income_365d > 0 OR kpl_row_count > 0`. Empty state: "No keyword-level income data yet for this campaign in the selected window."

### Confidence breakdown surface

Three places to expose tier mix at a glance:

1. **Ad Groups sub-tab table** — Tier Mix column (see above).
2. **Campaigns table tooltip on INCOME cell** — extend the existing tooltip (PR 3) to include a bottom line: `H $X · L $Y · B $Z` when any non-high tier is non-zero. If all income is high-confidence, omit the breakdown to keep tooltip clean.
3. **Admin tab → new small "Attribution Confidence" card** between AISpendCard and GadsAttributionSettingsCard. Single-glance summary across ALL campaigns last 30 days:
   - `High-confidence: $X (N%)`
   - `Low-confidence: $Y (N%)`
   - `Booked-override: $Z (N%)`
   - Subtitle: "If low+booked share is consistently >30%, consider call tracking service (CallRail/WhatConverts)."

Backend: new endpoint `GET /api/admin/attribution-confidence?days=30` returning the three sums + percentages. Implementation: simple SUM-by-tier query on KPL.

## Endpoints

- `GET /api/admin/attribution-confidence?days=30` — NEW. Returns `{high_365d, low_365d, booked_override_365d, total_365d, days}`.

## Tests

New file: `backend/tests/test_pr6_adgroup_keyword_income.py`

Cases:
1. **`test_ad_group_stats_includes_kpl_paid_income`** — Insert 1 KPL row for ad_group_name='X' with paid_amount_365d=199, confidence_tier='booked_override'. Assert returned ad_group dict for 'X' has `paid_income_365d=199`, `income_booked_override=199`, `income_high=0`.
2. **`test_ad_group_stats_legacy_call_income_unchanged`** — Same input plus one mango_calls row with od_patient_income=199 attributed to same ad group. Assert both `call_income=199` AND `paid_income_365d=199` are returned (don't double-add; both fields independent).
3. **`test_ad_group_stats_high_confidence_only_income`** — KPL row with conf_tier='high', paid_amount=500. Assert `income_high=500`, `income_booked_override=0`.
4. **`test_ad_group_stats_excludes_orphan_tiers`** — A KPL row with `confidence_tier='garbage'` (shouldn't exist but defensive). Assert it's NOT included in `paid_income_365d`. (Filter is `IN ('high','low','booked_override') OR IS NULL`.)
5. **`test_keyword_kpl_rollup_basic`** — Insert 2 KPL rows for same campaign, same keyword. Assert single grouped row with `kpl_row_count=2`, summed paid amount.
6. **`test_attribution_confidence_endpoint`** — Insert 1 high ($500), 1 low ($100), 1 booked_override ($199). Hit `/api/admin/attribution-confidence?days=30`. Assert returned dict has correct sums and percentages.
7. **`test_intelligence_builder_still_high_only`** — Regression test. Insert 1 high + 1 booked_override KPL row for the same keyword. Run `rebuild_keyword_intelligence()`. Assert the resulting `keyword_intelligence` row has `total_production` = the HIGH amount only (booked_override excluded — PR 4 invariant must hold).
8. **`test_ad_group_endpoint_returns_new_fields`** — End-to-end via `GET /api/admin/campaigns/{id}/detail`. Assert response.ad_groups[0] has `paid_income_365d`, `income_high`, `income_booked_override` keys.
9. **`test_campaign_detail_returns_keyword_income`** — End-to-end. Assert response has `keyword_income` key with the expected list shape.

## Known acceptable risks

- **Two income numbers visible simultaneously** (`call_income` legacy + `paid_income_365d` new). Intentional for first 2 weeks — lets us compare and verify parity. Plan to drop `call_income` from the UI in a follow-up PR once we trust the KPL path. Backend keeps both for now.
- **`get_keyword_kpl_rollup` doesn't filter by call-recency window**. KPL doesn't store a clean "call started_at" — it stores `created_at` on the KPL row. For days=30, we filter by `kpl.created_at >= NOW - 30 days`. Imperfect (a KPL row created later for an older call falls outside), but matches existing behavior elsewhere.
- **Attribution Confidence card uses `kpl.created_at` window**, same caveat.
- **Tier Mix chip cluster adds visual density** to the Ad Groups sub-tab. Acceptable because the sub-tab is already detail-heavy; if operators complain we can move chips into the tooltip.
- **No optimizer touch in this PR.** `paid_amount_365d` still NOT wired into `get_ad_group_stats` consumers in `ai_optimizer.py`. The optimizer SQL upgrade is still TODO (PR 7 or later). PR 6 fixes display only.

## Verification target

After PR 6:
1. Open Emergency Dentistry detail panel → Ad Groups sub-tab. Tooth Pain & Symptoms row shows `Income $199 · Tier Mix [B$199] · ROI -71%`.
2. Same panel → Keywords sub-tab → scroll past pills to new income table → row for `orthodontics near me` shows `1 KPL row · $199 · LTV $199 · [B$199]`.
3. Admin tab → new Attribution Confidence card → `High $0 · Low $0 · Booked-override $199 (100%)` (because right now Matthew is our only tracked income).
4. Campaigns table tooltip on INCOME for Emergency Dentistry → bottom line shows `B $199`.
5. Run all 9 new tests + existing PR 4 tests. All pass.

## Files touched

- `backend/database.py` — get_ad_group_stats() new CTE + new helper get_keyword_kpl_rollup()
- `backend/main.py` — admin_campaign_detail returns keyword_income; new /api/admin/attribution-confidence endpoint
- `frontend/index.html` — Ad Groups sub-tab columns, Keywords sub-tab table, INCOME tooltip extension, Attribution Confidence card
- `backend/tests/test_pr6_adgroup_keyword_income.py` — NEW, 9 tests

Cross-refs: [[project-pr4-refresh-call-income]], [[project-pr3-income-mode]], [[project-call-income-attribution]], [[feedback-campaign-table-ui]].
