# PR 4 — Refresh Call OD Income + KPL Coverage for Low-Confidence Calls

**Goal:** Close three concrete attribution bugs surfaced by Matthew Cornwell's case on 2026-05-20.

**Ground truth from OD (verified via opendental-analytics MCP, PatNum 5728):**
- Matthew paid $50 on 2026-05-18 (reservation fee) + $149 on 2026-05-20 (new patient comprehensive exam) = **$199 total**.
- Plus four splits on PayNum 9780 that net to $0 (+$165, -$165, +$34, -$34). Must NOT double-count these.

**Ground truth from pipeline.db (mango_calls row for PatNum 5728):**
- `od_patient_income = 50.0` (stale snapshot from May 18 match)
- `attributed_keyword_method = 'call_search_term'`, `attributed_keyword_confidence = 0.0`
- `od_appointment_id = 31747` (booked)
- No leads row. No keyword_production_log row.

**Dashboard shows:** `—` in INCOME column for Emergency Dentistry. ROI -93%. Patient Intelligence panel shows `Lifetime Income $50` (stale).

---

## The three bugs

### Bug 1: `mango_calls.od_patient_income` is a one-shot snapshot, never refreshed

`update_mango_call_od_income()` is called once at match time (in `od_matcher.match_calls_to_od_appointments()` around line 1301). After that, no code path refreshes it. New payments by already-matched patients never reach the dashboard.

### Bug 2: Low-confidence calls don't get a `keyword_production_log` row

`call_production_log.py` requires `attributed_keyword_confidence >= 0.55`. Matthew's call has confidence `0.0` (the `call_search_term` method assigned a method but no confidence). Without a KPL row, PR 2's `od_payment_sync` has nothing to update — paid_365d for Matthew = $0 forever.

This is the same root cause as the "phone blackhole" mentioned in old memory: calls with low confidence get campaign-level attribution but no per-keyword production logging.

### Bug 3: Campaign INCOME rollup only reads `mango_calls.od_patient_income`, not KPL paid amounts

`get_unified_campaigns()` `call_income_by_key` SQL (database.py ~3659–3675) reads from `mango_calls` only. PR 2 added `call_paid_by_key` reading KPL `paid_amount_365d/ltv`, but that's a separate rollup that contributes to `income_365d`/`income_ltv` ONLY. The `income` (planned, displayed default) column never benefits from KPL paid data.

---

## Scope

This PR fixes all three bugs in a single coherent change. Google Ads attribution only (same as PRs 2-3).

---

## 1. New module function: `refresh_call_od_income`

Add to `backend/od_payment_sync.py` (this module already owns OD payment queries; keep them together).

```python
def refresh_call_od_income(days: int = 90) -> dict:
    """
    Refresh mango_calls.od_patient_income for every new-patient call matched
    to an OD patient in the last `days` days. Re-queries OD for the current
    paid total since the call's started_at and writes back.

    Uses the same SUM(paysplit.SplitAmt) query as sync_od_payments to handle
    family splits and accounting reallocations correctly (PayNum 9780 case
    with +$165/-$165 nets to $0).

    Anchor: each call's started_at. Payments before that date are excluded
    (pre-existing patient defense — won't fire for new_patient rows but
    defensive).

    Returns: {
        "calls_refreshed": int,
        "calls_updated": int,        # only counts non-zero diff from prior value
        "total_income_synced": float,
        "errors": int,
        "duration_seconds": float,
    }
    """
```

**Algorithm:**

1. Open OD conn via `_get_od_conn()`. If unavailable, return `{"status":"skipped","reason":"od_unavailable"}` — don't raise.

2. Query pipeline.db:
```sql
SELECT uuid, od_patient_num, started_at, od_patient_income
FROM mango_calls
WHERE od_patient_status = 'new_patient'
  AND od_patient_num IS NOT NULL AND od_patient_num != ''
  AND od_appointment_id IS NOT NULL AND od_appointment_id != ''
  AND started_at >= datetime('now', ?-days days)
ORDER BY started_at DESC
```

3. Chunk patient nums to ≤ 500 per IN(...) clause. Reuse `_bulk_query_od_payments()` already in this module (it returns `{patnum -> [(date_str, amount), ...]}`).

4. For each call:
   - `anchor_dt = parse(started_at).date()`
   - `total_paid = sum(amount for date, amount in payments[patnum] if date >= anchor_dt)`
   - **Critical:** this handles the +$165/-$165 case correctly because we're summing SplitAmt — net zero. Don't apply `max(0, amount)` or `if amount > 0` filters.
   - If the new total differs from the stored `od_patient_income` by ≥ $0.01, write it back via `update_mango_call_od_income(uuid, income=total_paid, production=existing_or_0)`.

5. Also write to KPL when a row exists:
   - For each call, check if a KPL `call::{uuid}` row exists.
   - If so, update its `paid_amount_365d` and `paid_amount_ltv` with the new totals (compute 365d window from `started_at`, LTV is total).
   - This keeps PR 2's data in sync with PR 4's refresh.

6. Bulk write back via `executemany`. Connection cleanup in try/finally.

---

## 2. Lower the KPL confidence gate from 0.55 to 0.30 — for *display only*

In `call_production_log.py`:

```python
_MIN_CONFIDENCE_FOR_PRODUCTION = 0.55      # KEEP — used by optimizer-bound rollups
_MIN_CONFIDENCE_FOR_DISPLAY    = 0.30      # NEW — lower bar for dashboard visibility
```

Modify the existing data-fetch query to use the lower threshold but add a `confidence_tier` column to KPL:

```sql
ALTER TABLE keyword_production_log ADD COLUMN confidence_tier TEXT DEFAULT 'high';
```

Values: `'high'` (>= 0.55), `'low'` (0.30–0.54), `'campaign_only'` (< 0.30 — still excluded).

In `link_calls_to_keyword_production`:
- Use 0.30 as the floor for writing a row.
- Stamp `confidence_tier='low'` when 0.30 <= conf < 0.55.
- Stamp `confidence_tier='high'` when conf >= 0.55.

Then in:
- `get_keyword_stats()` — UNCHANGED behavior for the optimizer (still filters `confidence_tier='high'` implicitly by reading rows where it equals 'high', OR by a confidence filter in the CTE).
- `get_unified_campaigns()` `call_paid_by_key` — include `confidence_tier IN ('high','low')`. This means the campaign INCOME column will reflect low-confidence calls too, but the optimizer still only learns from high-confidence ones.

**Matthew's row** has confidence = 0.0 — that's still below 0.30. So this fix alone doesn't help him. But the **next bullet** does.

---

## 3. Special-case: calls with `od_appointment_id` set bypass the confidence gate entirely

If a call has `od_appointment_id` set, the OD-side has already confirmed this patient booked. The confidence gate is meant to filter spurious keyword attributions — but when OD shows a real appointment, we no longer need the confidence guard for revenue tracking. The call is a confirmed acquisition; we just need to credit the campaign somehow.

In `link_calls_to_keyword_production`, change the filter from:

```sql
AND COALESCE(mc.attributed_keyword_confidence, 0) >= ?
```

to:

```sql
AND (
  COALESCE(mc.attributed_keyword_confidence, 0) >= 0.30
  OR (mc.od_appointment_id IS NOT NULL AND mc.od_appointment_id != '')
)
```

Stamp `confidence_tier='booked_override'` for the special-case rows so we can identify them.

For Matthew: `od_appointment_id='31747'` is set, so the override kicks in, a KPL row gets written with `attributed_keyword='orthodontics near me'`, `campaign_name='Emergency Dentistry (05/09 22:00)'` (extracted from `attributed_ad_group` via the ` > ` split trick), and `confidence_tier='booked_override'`. PR 4's `refresh_call_od_income` then writes his $199 into `paid_amount_365d` on that row.

---

## 4. `get_unified_campaigns()` updates

Two surgical changes in `database.py`:

### 4a. `call_income_by_key` (line ~3659) — make the rollup reflect KPL paid totals as a fallback

Current behavior: reads `mango_calls.od_patient_income` only. After PR 4 Bug 1 fix, this is fresh. Good — leave it.

But: the `mango_calls.od_patient_income` filter requires `attributed_ad_group != ''` AND `od_patient_status = 'new_patient'`. If those are set for Matthew (they are), he'll be counted in `m.income` (planned).

Verify in the test suite that Matthew's $199 shows up here after PR 4 runs.

### 4b. `call_paid_by_key` (line ~3680) — extend confidence_tier filter

```sql
WHERE lead_id LIKE 'call::%'
  AND campaign_name != ''
  AND confidence_tier IN ('high', 'low', 'booked_override')
```

So low-confidence and booked-override KPL rows contribute to `income_365d` and `income_ltv` on the campaign rollup. The optimizer SQL (in `ai_optimizer.py`) keeps its strict 'high'-only filter.

---

## 5. Integration: add to unified sync chain

In `backend/unified_od_sync.py`, insert as **step 4.5** (or rename steps to make room):

Current 7 steps:
1. Firestore Sync
2. Google Ads Resolver
3. OD Patient Match
4. OD Payments (PR 2)
5. Call → Keyword Attribution
6. Call Production Log
7. Conversion Upload

After PR 4, **8 steps**:
1. Firestore Sync
2. Google Ads Resolver
3. OD Patient Match
4. **Refresh Call OD Income** ← NEW (this PR; moved earlier so step 5 OD Payments uses fresh data)
5. OD Payments (PR 2)
6. Call → Keyword Attribution
7. Call Production Log (now writes KPL for booked-override calls too)
8. Conversion Upload

Reasoning for ordering:
- Step 4 (refresh call income) needs to run BEFORE step 5 (OD payments) so that when OD Payments pulls fresh paid amounts, the call rows already show up-to-date OD-side info.
- Step 7 (call production log) needs to run AFTER call→keyword attribution and after the income refresh so the KPL row is written with the freshest paid amounts.

In code:
```python
UNIFIED_SYNC_STEPS = [
    ("Firestore Sync",          "Pulling new leads from web forms…"),
    ("Google Ads Resolver",     "Resolving gclids to campaign/ad group/keyword…"),
    ("OpenDental Patient Match","Matching leads to OD patients + treatment stages…"),
    ("Refresh Call Income",     "Re-pulling OD paid amounts for new-patient calls…"),  # NEW
    ("OpenDental Payments",     "Pulling paid amounts from OD (365d + LTV)…"),
    ("Call → Keyword",          "Attributing phone calls to paid clicks…"),
    ("Call Production Log",     "Writing call-path keyword production…"),
    ("Conversion Upload",       "Uploading conversions to Google Ads…"),
]
```

Update the `total_steps` constant and step indices accordingly. The progress dict will now show `4/8`, `5/8`, etc.

---

## 6. New admin endpoint

```python
@app.post("/api/admin/refresh-call-income", dependencies=[Depends(_require_admin)])
def admin_refresh_call_income(days: int = 90):
    try:
        from od_payment_sync import refresh_call_od_income
        result = refresh_call_od_income(days=days)
        return {"status": "ok", "result": result}
    except Exception as e:
        logger.error(f"Refresh call income failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
```

Add a button **"Refresh Call Income"** in the Advanced disclosure of AdminTab, between "Sync OD Payments" and "Backfill All Payments". One-click, with confirm dialog: *"This will re-query OpenDental for all new-patient calls in the last 90 days. Takes ~10 seconds. Continue?"*

---

## 7. Tests

Create `backend/tests/test_pr4_refresh_call_income.py`:

1. **Stale income refresh**: insert a `mango_calls` row with `od_patient_num='5728'`, `od_patient_status='new_patient'`, `od_patient_income=50`, `started_at='2026-05-18'`. Monkeypatch `_bulk_query_od_payments` to return `{'5728': [('2026-05-18', 50.0), ('2026-05-20', 149.0)]}`. Run `refresh_call_od_income()`. Assert `od_patient_income` updates to 199.

2. **Splits net to zero**: monkeypatch payments to include `[('2026-05-18', 50.0), ('2026-05-20', 165.0), ('2026-05-20', -165.0), ('2026-05-20', 149.0)]`. Assert refresh writes $199, not $364.

3. **Booked-override KPL write**: insert a `mango_calls` row with `attributed_keyword_confidence=0.0`, `od_appointment_id='31747'`. Run `link_calls_to_keyword_production`. Assert a KPL row is written with `confidence_tier='booked_override'`.

4. **Low-confidence still excluded if no appointment**: insert a row with `confidence=0.40`, `od_appointment_id=NULL`. Should not write KPL. (Wait — `confidence_tier='low'` would be written since 0.40 >= 0.30. Re-read spec — the floor for KPL write is 0.30 OR appointment-set. So 0.40 with no appointment SHOULD write. Fix test to reflect: 0.20 with no appointment should NOT write; 0.40 with no appointment SHOULD write with tier='low'.)

5. **OD unavailable**: monkeypatch `_get_od_conn` to None. Assert `refresh_call_od_income` returns `{"status":"skipped"}` without raising.

6. **Unified sync chain has 8 steps**: import `UNIFIED_SYNC_STEPS`, assert `len(...) == 8` and step 4 is "Refresh Call Income".

7. **`get_unified_campaigns` reflects KPL paid amounts**: insert a mango_calls row + a KPL row with `paid_amount_365d=199.0`, `confidence_tier='booked_override'`, both tied to `campaign_name='Emergency Dentistry'`. Call `get_unified_campaigns()`. Assert `metrics.income_365d` for Emergency Dentistry includes $199.

Run from `backend/`:
```bash
source venv/bin/activate
pytest tests/test_pr4_refresh_call_income.py -v
```

---

## 8. Things to NOT do

- Do NOT change the optimizer's 0.55 confidence threshold. The optimizer reads `confidence_tier='high'` only. Display surfaces (campaign INCOME column) read all three tiers.
- Do NOT remove `mango_calls.od_patient_income`. It's still the source for `m.income` (planned). PR 4 just refreshes it; the field stays.
- Do NOT touch leads-side payment handling. PRs 2-3 cover that.
- Do NOT modify the 6 original admin endpoints. Just add the new one.

---

## 9. File-by-file change list

| File | Change |
|------|--------|
| `backend/database.py` | Add `confidence_tier` column to `keyword_production_log` with idempotent PRAGMA-checked migration. Extend `get_unified_campaigns()` `call_paid_by_key` filter to include 'low' and 'booked_override' tiers. |
| `backend/od_payment_sync.py` | Add `refresh_call_od_income(days=90)` function. Reuse `_bulk_query_od_payments()`. |
| `backend/call_production_log.py` | Lower floor from 0.55 to 0.30. Stamp `confidence_tier` based on conf level + booked-override logic. |
| `backend/unified_od_sync.py` | Insert "Refresh Call Income" as new step 4. Update step list to 8 entries. Add summary extractor. |
| `backend/main.py` | New `/api/admin/refresh-call-income` endpoint. |
| `backend/ai_optimizer.py` | If the optimizer reads KPL rows directly, add `WHERE confidence_tier='high'` to those queries to preserve current behavior. Verify by grepping for `keyword_production_log` reads. |
| `frontend/index.html` | Add "Refresh Call Income" button in Advanced disclosure with confirm dialog. |
| `backend/tests/test_pr4_refresh_call_income.py` | **New file.** 7 tests. |

---

## 10. Rollout

1. Sonnet implements end-to-end.
2. Opus reviews — focus on double-counting risk (PayNum 9780 splits), optimizer 'high'-tier preservation, and migration idempotency.
3. Run pytest. All 7 tests green.
4. Manual test: Anurag clicks "Refresh Call Income" → Matthew's row updates from $50 to $199 (verify via call detail panel "Lifetime Income"). The Campaign INCOME column for Emergency Dentistry should also update to reflect $199 (via `m.income` path) and `income_365d` (via KPL booked-override path).
5. Push via GitHub Desktop.

---

## 11. Verification specific to Matthew's case

After PR 4 ships, running the unified sync should produce:

| Field | Before PR 4 | After PR 4 |
|---|---|---|
| `mango_calls.od_patient_income` (5728) | 50.0 | 199.0 |
| KPL row for `call::4713642545` | none | 1 row, `confidence_tier='booked_override'`, `paid_amount_365d=199.0`, `paid_amount_ltv=199.0` |
| Dashboard call detail "Lifetime Income" | $50 | $199 |
| Campaigns table "Emergency Dentistry" → INCOME (365d mode) | — | $199 |
| Campaigns table "Emergency Dentistry" → INCOME (Planned mode) | $0 | $199 |
| Campaigns table "Emergency Dentistry" → ROI | -93% | -71% ((199-692)/692) |

ROI is still negative because $692 spent against $199 paid is genuinely negative ROI. That's the truth — Matthew alone doesn't pay back the spend, but he's the start of attribution working correctly.
