# Session Summary — 2026-05-20 (PR 5 + PR 6)

## Topic
PR 5: Fix Step-7 OD-MySQL-unavailable bail-out + add patient name + income to appointment modal. PR 6: Income parity for Ad Groups + Keywords surfaces, confidence-tier breakdown, Attribution Confidence card.

## What we did

1. **Diagnosed Matthew Cornwell's missing KPL row from yesterday's PR 4 sync.** Confirmed via sandbox query that PR 4's Step 4 (refresh_call_od_income) ran correctly — `mango_calls.od_patient_income` updated $50 → $199 — but Step 7 (link_calls_to_keyword_production) bailed with `od_conn is None`, so no KPL row got written. Wrote the row manually from sandbox; user confirmed $199 displays correctly on Emergency Dentistry campaign.

2. **User asked three follow-up questions:**
   - Is the $199 cosmetic or does it aggregate? **Aggregates automatically** — PR 4's nightly chain will populate KPL going forward.
   - Should appointment modal show patient name + income? **Yes, two gaps:** Matthew's name shows "—" because `od_patient_name=''` (only `ai_patient_name='Matthew Cornwell'` was populated from transcript AI); no income column at all.
   - Is data passed to Ad Groups + Keywords? **Mostly no.** Campaigns table uses KPL (PR 4 fixed it). Ad Groups tab uses legacy mango_calls.od_patient_income only. Detail-panel Ad Groups + Keywords sub-tabs read only the raw GAds snapshot. No KPL data flowing.

3. **Strategic conversation re: CallRail / WhatConverts.** Recommended: build attribution first, run for 2-4 weeks, then evaluate. CallRail's main value is Dynamic Number Insertion (DNI) for certain keyword attribution — but you can't ROI-justify it without the attribution chain showing what % of tracked income comes from low-confidence calls. Once PR 6 ships the Attribution Confidence card, that number is one glance away.

4. **Drafted PR 5 + PR 6 specs** in parallel after reading code recon:
   - `lead-lifecycle/PR5_SPEC_step7_fix_and_modal.md` — Bug A (Step-7 resilience), Bug B (summary log), modal patient name + income
   - `lead-lifecycle/PR6_SPEC_adgroup_keyword_income_parity.md` — kpl_income_by_ag CTE, get_keyword_kpl_rollup helper, Ad Groups Income/Tier Mix/ROI columns, Keywords KPL table, Attribution Confidence card

5. **Sonnet implemented PR 5.** 7 tests pass.
   - Modified `backend/call_production_log.py` — removed early bail at lines 415-419, moved booked_override check before OD check inside loop, added `od_unavailable` to summary log
   - Modified `backend/unified_od_sync.py` — `_summarize_call_production` appends "(N OD unavail)" when non-zero
   - Modified `backend/main.py` — campaign-appts SELECT adds `mc.ai_patient_name` + `COALESCE(kpl.paid_amount_365d, 0)`, extended patient_name COALESCE
   - Modified `frontend/index.html` modal — new Income column, OD Patient cell uses patient_name with `(from transcript)` muted suffix when from ai_patient_name
   - New `backend/tests/test_pr5_step7_fix_and_modal.py` (7 tests)

6. **Opus reviewed PR 5. Verdict: Ship with critical fixes — Opus found a pre-existing bug PR 5 made worse:**
   - **Bug found:** Modal's `LEFT JOIN keyword_production_log kpl ON kpl.od_patient_num = mc.od_patient_num` had no aggregation. KPL's UNIQUE constraint is `(lead_id, od_patient_num)` so a single patient often has multiple KPL rows (call-path + lead-path). The JOIN multiplied call rows. Pre-existed PR 4; PR 5 made it semantically worse by adding `paid_amount_365d` to the SELECT (before PR 5, only `appointment_date` was selected, so the duplication was nearly invisible).
   - **Fix applied** by Opus: replaced raw kpl JOIN with aggregating subquery using `MAX(paid_amount_365d)` and `MAX(appointment_date)` grouped by `od_patient_num`. Added regression test `test_modal_endpoint_no_inflation_when_patient_has_multiple_kpl_rows`. All 16 tests (8 PR4 + 8 PR5) pass.
   - Noted-but-left items: `skipped_od_unavailable` counter is effectively dead in production because `_fetch_call_production_data` SQL pre-filters `od_appointment_id != ''` on the outer WHERE — every reaching row is already booked_override, so the OD-down skip branch only fires when SQL is mocked. Harmless; the actual fix (allow booked_override through when OD down) IS what the spec wanted. The counter staying at 0 just means the log line shows `od_unavailable=0` always. Worth a future cleanup if outer SQL filter ever relaxes.

7. **Sonnet implemented PR 6.** 9 tests pass after fixing 2 test-only issues (a `import main` block that failed in Linux sandbox without apscheduler, and a wrong patch target for intelligence_builder's local `from database import _conn`).
   - Modified `backend/database.py` — new `kpl_income_by_ag` CTE in `get_ad_group_stats()`, new helper `get_keyword_kpl_rollup()`
   - Modified `backend/main.py` — `admin_campaign_detail` returns `keyword_income`, new `/api/admin/attribution-confidence` endpoint
   - Modified `frontend/index.html` — Ad Groups sub-tab Income/Tier Mix/ROI columns, Keywords sub-tab KPL income table, INCOME tooltip tier breakdown, AttributionConfidenceCard in AdminTab
   - New `backend/tests/test_pr6_adgroup_keyword_income.py` (9 tests)

8. **Opus reviewed PR 6. Verdict: Ship with two critical fixes — Opus found two real bugs:**
   - **Critical Bug 1 (optimizer pollution):** `ai_optimizer.py:7584` reads `ag.get("paid_income_365d")`. PR 3 added that line with TODO comments expecting it to be 'high'-only when wired. PR 6 populated it with ALL tiers (high+low+booked_override+NULL). Result: booked_override income would have leaked into per-ad-group `revenue_30d` → polluting tier scoring and Claude prompt context. PR 4's intelligence_builder filter caught the keyword-level pollution; this was the hidden ad-group-level pollution. **Fix:** changed read to `ag.get("income_high")` (the new high-only field). intelligence_builder.py invariant verified intact at line 204.
   - **Critical Bug 2 (KPL multi-row double-count):** Same patient can have two KPL rows (call-path `call::uuid` + lead-path `<lead-id>`), both carrying the same `paid_amount_365d` for the same payment. PR 6's naive SUMs in `kpl_income_by_ag` CTE, `get_keyword_kpl_rollup`, AND `/api/admin/attribution-confidence` would have double-counted ($199 → $398). `get_unified_campaigns` avoided this with `lead_id LIKE 'call::%'` filter; PR 6 didn't. **Fix:** Wrapped each aggregator in a `kpl_dedup` CTE — for each (ad-group/keyword, od_patient_num), keeps the call-path row when present (richer tier classification), else the lead-path row. Rows without od_patient_num pass through individually. Same fix on all three surfaces.
   - Added 2 regression tests for the multi-row case. All 27 tests pass (8 PR4 + 8 PR5 + 11 PR6).

## What's ready to push

PR 5 and PR 6 are complete, Opus-reviewed, all 27 tests passing.

**Combined git commit summary:**
```
PR 5 + PR 6: Step-7 OD resilience + modal patient/income + ad-group/keyword income parity
```

**Combined git commit description:**
```
Two PRs ship together to close gaps surfaced by PR 4.

PR 5 — Step-7 OD-MySQL-unavailable resilience + appointment modal patient
name + income:
- Bug A: link_calls_to_keyword_production no longer bails when OD MySQL is
  unreachable. booked_override rows continue to write (they seed
  paid_amount_365d from refreshed mango_calls.od_patient_income and don't
  need OD production data). Non-booked rows skip with counter increment.
- Bug B: summary log line now includes od_unavailable=N. unified_od_sync
  step summary appends "(N OD unavail)" when non-zero.
- Modal endpoint /api/admin/calls/campaign-appts now SELECTs
  ai_patient_name + paid_amount_365d. patient_name COALESCE prefers
  od_patient_name → ai_patient_name → lead name.
- Modal frontend adds Income column. OD Patient cell shows patient_name
  with "(from transcript)" muted suffix when the name came from
  ai_patient_name (i.e., od_patient_name was empty).
- Opus-reviewed: fixed pre-existing modal JOIN bug where kpl JOIN
  multiplied call rows when patient had multiple KPL rows. Replaced raw
  JOIN with aggregating subquery using MAX. 8 tests.

PR 6 — Ad-group + Keyword income parity + Attribution Confidence card:
- get_ad_group_stats() adds new kpl_income_by_ag CTE alongside the
  legacy call_income_by_ag (both numbers kept for parity verification).
  Returns paid_income_365d, paid_income_ltv, kpl_row_count, plus per-tier
  breakdown (income_high, income_low, income_booked_override).
- New get_keyword_kpl_rollup() helper for per-keyword income rollups.
- admin_campaign_detail returns keyword_income array.
- New /api/admin/attribution-confidence?days=30 endpoint with per-tier
  sums + percentages.
- Frontend: Ad Groups sub-tab adds Income/Tier Mix/ROI columns. Keywords
  sub-tab adds KPL income table below pills. Campaigns INCOME tooltip
  extended with tier breakdown. New AttributionConfidenceCard in AdminTab.
- Opus-reviewed: fixed (1) optimizer pollution where ai_optimizer.py was
  reading paid_income_365d (all tiers) → switched to income_high. PR 4's
  intelligence_builder high-only filter intact. (2) KPL multi-row-per-
  patient double-counting: same patient often has call-path + lead-path
  KPL rows summing to 2x. Added kpl_dedup CTE to all three aggregators
  (ad-group, keyword, attribution-confidence) preferring call-path tier
  when present. 11 tests including 2 new regression tests for dedup.

All 27 tests pass (8 PR4 + 8 PR5 + 11 PR6). No regressions.
```

## Pending follow-ups

- **CallRail/WhatConverts decision (revisit in 2-4 weeks).** Watch Attribution Confidence card. If low+booked_override share is consistently >30% of tracked income, DNI is worth the $30-150/mo. Until then, current attribution chain captures the high-value cases via booked_override.
- **Call list filters PR** (still queued from PR 4 session): revert narrow `get_mango_calls_needing_od_match` + stackable GAds/New/Existing/Converted checkboxes.
- **Optimizer SQL upgrade.** `get_ad_group_stats()` now has `income_high` — but `ai_optimizer.py` would benefit from also reading `income_high` in the 3 `attributed_production` sites (lines 1572, 1778, 1766 TODOs). Currently they read planned production. Conservative; future PR.
- **`skipped_od_unavailable` counter dead in production.** Today the outer SQL filter in `_fetch_call_production_data` requires `od_appointment_id != ''`, so non-booked rows never reach the loop. Counter stays at 0. If we ever relax that filter to capture non-booked phone consults, the counter activates correctly. Harmless.
- **Drop legacy `call_income` from UI in 2 weeks** after KPL parity verified. Spec called for both numbers visible during transition.
- **Same-patient multi-call dedup at campaign rollup** (`get_unified_campaigns()` `call_income_by_key` and `call_paid_by_key`) — still flagged from PR 4 review. PR 6's dedup pattern could be reused.
- **2 pre-existing test failures in `test_unified_od_sync.py`** — asserts 7 steps but PR 4 made it 8. Out of PR 5/6 scope, but easy fix when convenient.
