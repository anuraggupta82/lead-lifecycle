# Session Summary — 2026-05-20 (PR 4)

## Topic
PR 4: Fix three concrete attribution bugs surfaced by Matthew Cornwell's real case (PatNum 5728): stale `mango_calls.od_patient_income`, low-confidence calls excluded from `keyword_production_log`, and campaign INCOME rollups that missed KPL paid amounts.

## What we did

1. **Used opendental-analytics MCP to ground-truth Matthew's payments.** Confirmed via direct OD query:
   - PatNum 5728 paid $50 on 2026-05-18 + $149 on 2026-05-20 = **$199 total**
   - PayNum 9780 has four splits netting to $0 (+$165, -$165, +$34, -$34) — must not double-count to $364

2. **Pulled the call's pipeline.db state:**
   - `od_patient_income=50.0` (stale)
   - `attributed_keyword_confidence=0.0` (below the 0.55 KPL gate)
   - `od_appointment_id='31747'` (booked!)
   - `attributed_ad_group='Emergency Dentistry (05/09 22:00) > Tooth Pain & Symptoms'`
   - No leads row, no KPL row

3. **Drafted PR 4 spec** — `lead-lifecycle/PR4_SPEC_refresh_call_income.md`. Defined three changes:
   - **Bug 1:** New `refresh_call_od_income(days=90)` function in `od_payment_sync.py` — re-queries OD for every matched new-patient call, refreshes `mango_calls.od_patient_income`, also updates KPL paid amounts if a row exists
   - **Bug 2:** Lower KPL confidence floor from 0.55 to 0.30, with a special-case **booked_override** tier when `od_appointment_id` is set regardless of confidence. New `confidence_tier` column: 'high' / 'low' / 'booked_override'
   - **Bug 3:** Extend `get_unified_campaigns()` `call_paid_by_key` filter to include 'high', 'low', 'booked_override' tiers so campaign INCOME column reflects all three
   - Added as step 4 in the unified sync chain (now 8 steps total)

4. **Sonnet implemented PR 4.** 8 pytest tests pass. Files touched:
   - **New** `backend/tests/test_pr4_refresh_call_income.py` (8 tests, all pass)
   - **Modified** `backend/od_payment_sync.py` — added `refresh_call_od_income(days=90)`
   - **Modified** `backend/call_production_log.py` — `_MIN_CONFIDENCE_FOR_DISPLAY=0.30`, `_derive_confidence_tier()`, `_extract_campaign_name_from_ad_group()`, modified SQL filter `(conf >= 0.30 OR od_appointment_id != '')`, KPL INSERT now stamps `confidence_tier` and seeds `paid_amount_365d/ltv` from `od_patient_income` for booked_override rows
   - **Modified** `backend/database.py` — idempotent PRAGMA-checked migration adds `confidence_tier` column to KPL; extended `call_paid_by_key` rollup filter
   - **Modified** `backend/unified_od_sync.py` — 8 steps, "Refresh Call Income" inserted at index 3, summary extractor added
   - **Modified** `backend/main.py` — new `/api/admin/refresh-call-income` endpoint
   - **Modified** `frontend/index.html` — "Refresh Call Income" button in Advanced disclosure with confirm dialog

5. **Opus reviewed PR 4.** Verdict: **Ship with minor fixes** — Opus found a real bug Sonnet missed and fixed it himself:
   - **Bug found:** `intelligence_builder.py:rebuild_keyword_intelligence()` aggregates KPL `production_amount` + appointment counts into the optimizer prompt context. PR 4 would have silently polluted this signal with booked_override rows (Matthew's keyword `orthodontics near me` had $0 production but 1 "appointment" — that would have appeared in the optimizer's prompt as a positive signal even though confidence was 0). Spec said "grep all KPL reads and add confidence_tier='high' filter" but Sonnet only grepped `ai_optimizer.py`.
   - **Fix applied** by Opus: added `AND (confidence_tier = 'high' OR confidence_tier IS NULL)` to the 90d KPL aggregation in `rebuild_keyword_intelligence()`. All 8 tests still pass.
   - Risk-but-acceptable items: same-patient-multi-call double-count at campaign rollup (pre-existing, made more reachable by PR 4 — needs follow-up), UPSERT MAX-paid stuck-high logic on KPL writes, refresh skips when delta < $0.01 also skips KPL healing, `confidence_tier='low'` branch is dead code today because outer WHERE requires `od_appointment_id` (harmless, activates if filter is ever relaxed).
   - Test gaps recommended: two-calls-same-patient test for rollup dedup, intelligence_builder filter test for booked_override exclusion.

## What's ready to push

PR 4 is complete and reviewed.

**Git commit summary:**
```
PR 4: Refresh call OD income + KPL coverage for low-confidence + booked calls
```

**Git commit description:**
```
Fixes three concrete attribution bugs surfaced by real patient case Matthew
Cornwell (PatNum 5728) on 2026-05-20:

Bug 1: mango_calls.od_patient_income was a one-shot snapshot set at match
time, never refreshed. Patients who paid more after the initial match (like
Matthew, +$149 today) had their additional payments invisible to the
dashboard.

Bug 2: keyword_production_log required attributed_keyword_confidence >= 0.55.
Matthew's call had confidence 0.0 but od_appointment_id set — a confirmed
acquisition. Without a KPL row, PR 2's payment sync had nothing to update,
so paid 365d stayed at $0.

Bug 3: get_unified_campaigns() call_paid_by_key rollup only filtered KPL
rows with the implicit 'high' confidence threshold, so even after PR 4
created low-tier rows, they wouldn't reach the campaign INCOME column.

Fixes:
- New refresh_call_od_income(days=90) in od_payment_sync.py re-queries OD
  for every matched new-patient call's current paid total and writes back
  to mango_calls.od_patient_income + KPL paid_amount_365d/ltv. Uses
  SUM(paysplit.SplitAmt) so accounting reversals net correctly (PayNum
  9780's +165/-165/+34/-34 splits → $0, not $364).
- Lowered KPL confidence floor from 0.55 to 0.30 OR od_appointment_id set.
  New confidence_tier column: 'high' (>=0.55), 'low' (0.30-0.54),
  'booked_override' (any conf if appointment is booked).
- get_unified_campaigns() call_paid_by_key extended to include all three
  display tiers. Optimizer reads in intelligence_builder.py still filter
  to high-confidence only (caught by Opus during review).
- Added as step 4 of the unified sync chain (now 8 steps).
- New /api/admin/refresh-call-income endpoint + button in Advanced.

Verified against real OD data via opendental-analytics MCP before writing
the spec. 8 pytest tests pass. Opus-reviewed; one intelligence_builder
optimizer-pollution bug found and fixed during review.

After this PR runs, Matthew's mango_calls.od_patient_income updates from
$50 to $199, a new KPL row gets written with confidence_tier='booked_override'
and paid_amount_365d=199.0, and Emergency Dentistry's campaign INCOME
column reflects the $199 in 365d mode.
```

## Pending follow-ups

- **Call list filters PR** (already queued as [[project-call-list-filters]]): revert narrow `get_mango_calls_needing_od_match` filter + add stackable GAds/New/Existing/Converted checkboxes.
- **Same-patient multi-call dedup at campaign rollup**: Opus noted `call_income_by_key` and `call_paid_by_key` in `get_unified_campaigns()` lack the `call_attributed_patients` CTE dedup that the keyword-level rollup uses. Two calls for the same patient against the same campaign currently double-count. Worth a small follow-up PR.
- **Two test cases Opus recommended**: two-calls-same-patient rollup test + intelligence_builder booked_override filter test.
- **Optimizer SQL upgrade from PR 3**: still pending — add `SUM(l.paid_amount_365d)` to `get_ad_group_stats()` so the optimizer's `paid_income_365d` fallback actually has data.
