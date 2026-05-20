# PR 2 — OD Payment Pull with 365d + LTV Income Tracking

**Goal:** Replace the current "INCOME = scheduled/treatment-planned production" approximation with **actual collected dollars** from OpenDental's `payment` table, tracked in two buckets — **365d attribution window** (default, used everywhere ROI is computed) and **LTV** (lifetime, surfaced as a secondary metric).

**Scope:** Google Ads–attributed patients only (leads with a `gclid` OR phone-call rows with `attributed_keyword_confidence >= 0.55`). Organic, SEO, direct, referral, and other paid channels are explicitly out of scope for this PR.

**Owner of attribution decisions:** A patient's first attributed touch (lead `created_at` for web/scheduler leads; call `started_at` for phone-only) is the anchor. Payments within 365 days of that anchor count toward 365d; all payments count toward LTV.

---

## 1. Schema changes

### 1a. `leads` table — new columns

```sql
ALTER TABLE leads ADD COLUMN paid_amount_365d REAL DEFAULT 0.0;
ALTER TABLE leads ADD COLUMN paid_amount_ltv  REAL DEFAULT 0.0;
ALTER TABLE leads ADD COLUMN first_payment_date TEXT DEFAULT '';   -- ISO date of first OD payment
ALTER TABLE leads ADD COLUMN paid_through_date  TEXT DEFAULT '';   -- ISO date of latest OD payment pulled
ALTER TABLE leads ADD COLUMN payment_synced_at  TEXT DEFAULT '';   -- when od_payment_sync last touched this lead
```

Add the same columns to the inline `CREATE TABLE` in `database.py` for fresh DBs.

Use idempotent `PRAGMA table_info(leads)` check + `ALTER TABLE ADD COLUMN` pattern that already exists elsewhere in `database.py` (search for `_migrate_` style code).

### 1b. `keyword_production_log` table — new columns

```sql
ALTER TABLE keyword_production_log ADD COLUMN paid_amount_365d REAL DEFAULT 0.0;
ALTER TABLE keyword_production_log ADD COLUMN paid_amount_ltv  REAL DEFAULT 0.0;
ALTER TABLE keyword_production_log ADD COLUMN payment_synced_at TEXT DEFAULT '';
```

These hold paid amounts for the **call::** rows (phone-only patients with no `leads` entry). For lead-path rows, `paid_amount_365d`/`paid_amount_ltv` mirror what's on the `leads` row, but are denormalized here so the keyword roll-up CTE doesn't need to join across both.

### 1c. Constants module

Add to `config.py`:

```python
gads_attribution_window_days: int = 365  # 365d default for ROI calculations
```

No environment variable required in this PR — config-driven only. PR 3 will expose this in the Admin UI.

---

## 2. New module: `backend/od_payment_sync.py`

### 2a. Public entry point

```python
def sync_od_payments(days_back: int = 7, full_resync: bool = False) -> dict:
    """
    Pull payments from OpenDental for all leads + call-only patients tied to
    Google Ads attribution. Updates paid_amount_365d / paid_amount_ltv on
    leads and keyword_production_log.

    Args:
        days_back: only re-pull patients whose OD payments may have changed in
                   the last N days. Default 7. Use full_resync=True for backfill.
        full_resync: ignore days_back, rebuild paid amounts for every attributed
                     patient. Use sparingly (e.g., one-shot after PR 2 ships).

    Returns:
        {
            "leads_synced": int,
            "calls_synced": int,
            "total_paid_365d": float,
            "total_paid_ltv":  float,
            "errors": int,
            "duration_seconds": float,
        }
    """
```

### 2b. Algorithm

1. **Open OD MySQL connection** using `_get_od_conn()` pattern from `call_production_log.py` (copy that function — same try/except, same charset).

2. **Collect target patients into a single list**, each item carrying `{od_patient_num, anchor_date, target_table, target_id}`:
   - From `leads`: every row with `od_patient_num != ''` AND `gclid != ''` AND (full_resync OR `payment_synced_at < now - days_back` OR `payment_synced_at == ''`). `anchor_date = leads.created_at`.
   - From `keyword_production_log`: every row with `lead_id LIKE 'call::%'` AND `od_patient_num != ''` AND (full_resync OR `payment_synced_at < now - days_back` OR `payment_synced_at == ''`). `anchor_date` = the corresponding `mango_calls.started_at` (join via `lead_id` which encodes `call::{uuid}`; uuid is the mango call UUID). If the join fails, fall back to `kpl.logged_at`.

3. **Bulk-query OD `payment` table** for those patient nums. Recommended query:

```sql
SELECT
    p.PatNum            AS od_patient_num,
    DATE(p.PayDate)     AS payment_date,
    SUM(ps.SplitAmt)    AS amount
FROM payment p
JOIN paysplit ps ON ps.PayNum = p.PayNum
WHERE p.PatNum IN (%s)
  AND p.PayDate IS NOT NULL
  AND p.PayDate != '0001-01-01'
GROUP BY p.PatNum, DATE(p.PayDate)
ORDER BY p.PatNum, p.PayDate
```

Batch the `IN (...)` to chunks of 500 patient nums to avoid huge query plans. Use parameterised values (pymysql `%s`).

**Why `paysplit` not `payment.PayAmt`?** `paysplit.SplitAmt` is the actual amount allocated to the patient's account; `payment.PayAmt` can include splits across family members. The existing OD code in `od_matcher.py` already uses this pattern — mirror it.

4. **For each patient**, walk their payment rows in date order. Compute:
   - `anchor_dt` = parse `anchor_date` to `date`
   - `cutoff_365` = `anchor_dt + 365 days`
   - `paid_365d` = sum of `amount` where `payment_date <= cutoff_365` AND `payment_date >= anchor_dt`
   - `paid_ltv` = sum of `amount` where `payment_date >= anchor_dt`
   - `first_payment_date` = earliest `payment_date` where `payment_date >= anchor_dt`, else `''`
   - `paid_through_date` = max(`payment_date`)

   **Important edge case:** payments before `anchor_dt` are explicitly **excluded** from both 365d and LTV. These are pre-existing patient payments — they would inflate Google Ads' apparent ROI for someone who was already a patient. If `od_matcher` already flagged the lead/call as `existing_active`/`existing_inactive`, they should already be excluded upstream, but this is a defensive second filter.

5. **Write back** in a single `executemany`:
   - For lead-path patients: `UPDATE leads SET paid_amount_365d=?, paid_amount_ltv=?, first_payment_date=?, paid_through_date=?, payment_synced_at=? WHERE id=?`
   - For call-path patients: `UPDATE keyword_production_log SET paid_amount_365d=?, paid_amount_ltv=?, payment_synced_at=? WHERE id=?`

6. **Emit a `lifecycle_events` row** for each lead where `paid_amount_365d` changed by ≥ $50 since the last sync. Event type `payment_pulled`, `detail` JSON `{paid_365d_delta, paid_ltv_delta}`. Skip for call rows.

### 2c. Error handling

- If OD MySQL is unavailable, log a warning and return `{"status": "skipped", "reason": "od_unavailable"}`. Don't raise — the unified sync (PR 1) needs to keep going.
- If a single patient's query fails, log and continue. Increment `errors` counter.
- Wrap the whole sync in a `try/finally` that closes the OD connection.

---

## 3. Integration points

### 3a. Wire into the existing nightly cron

In `main.py` around line 388, add a new APScheduler job **after** `od_sync` and **before** `call_production`:

```python
ads_scheduler.add_job(_od_payment_sync_job, CronTrigger(hour=22, minute=15),
                      id="od_payment_sync", name="OD Payment Pull (365d + LTV)",
                      replace_existing=True)
```

Define `_od_payment_sync_job()` at the top with the other job functions; it should call `sync_od_payments(days_back=7)` and log the result.

### 3b. New admin endpoint

In `main.py`, add next to the other admin sync endpoints:

```python
@app.post("/api/admin/sync-payments", dependencies=[Depends(_require_admin)])
def admin_sync_payments(days: int = 7, full: bool = False):
    """
    On-demand: pull OD payments for all attributed patients.
    days=N to re-sync patients last touched > N days ago.
    full=true to rebuild from scratch (use after deploying PR 2).
    """
    try:
        from od_payment_sync import sync_od_payments
        result = sync_od_payments(days_back=days, full_resync=full)
        return {"status": "ok", "result": result}
    except Exception as e:
        logger.error(f"OD payment sync failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
```

### 3c. Frontend Admin tab

In `frontend/index.html` `AdminTab` (around line 16183), add a new button right after **Sync Call Production**:

```jsx
<button className="btn btn-teal" onClick={()=>handleAction('payments', onSyncPayments)} disabled={loading.payments}>
  {loading.payments?'Syncing...':'Sync OD Payments'}
</button>
```

Pass through in the AdminTab props and from the parent:

```jsx
onSyncPayments={() => api('/api/admin/sync-payments?days=7', { method: 'POST' })}
```

Also add a "Backfill all payments" button next to it that calls `/api/admin/sync-payments?full=true`. Confirm dialog on click.

---

## 4. `get_keyword_stats()` update — campaign table INCOME column

Modify the CTE in `database.py` lines 4187–4363 to expose two new aggregates per row:

- `income_365d` — sum of `leads.paid_amount_365d` (lead path) + sum of `keyword_production_log.paid_amount_365d` for `call::%` rows (call path), with the same `call_attributed_patients` dedup logic
- `income_ltv` — same as above but using `paid_amount_ltv`

Keep `revenue` (existing) **unchanged** — it's the planned-production number, still useful as a forward indicator. Add `income_365d` and `income_ltv` as new fields alongside `revenue`.

**`roas` and `cpl` keep using `revenue` for now** to avoid breaking the optimizer mid-flight. A follow-up PR will switch optimizer reads to `income_365d`.

---

## 5. Frontend INCOME column changes

Find the campaign table column that currently displays `revenue` (memory mentions `project_pr5_income_column` and `project_campaign_table_income`). For this PR, **add a hover tooltip** to the INCOME cell with both new numbers:

```
Planned production:  $X,XXX
Paid (365d):         $Y,YYY
Paid (LTV):          $Z,ZZZ
```

The displayed number remains `revenue` (planned production) in this PR — no behavior change to what's visible. PR 3 will add the column-header dropdown to switch the displayed value between 365d / LTV / Planned.

Rationale: ship the data first, ship the UI swap second. That way if PR 2 has a bug we discover by comparing planned vs paid in the tooltip, we can fix it before the optimizer ever reads the new field.

---

## 6. Tests / verification

Write a small `tests/test_od_payment_sync.py`:

1. **Anchor-date filter**: insert a lead with `created_at = 2025-06-01`, insert OD payments at 2025-05-15 (pre-anchor), 2025-08-01 (in-window), 2026-08-01 (past 365d). Run sync. Assert `paid_amount_365d == amount of 2025-08-01 row`, `paid_amount_ltv == sum of 2025-08-01 and 2026-08-01`, pre-anchor excluded.

2. **Call-path patient**: insert a `mango_calls` row at `2025-07-01`, insert matching `keyword_production_log` row with `lead_id = 'call::abc'`, insert OD payments. Assert call row gets updated, no lead row touched.

3. **Idempotency**: run sync twice with no OD changes. Assert second run produces identical numbers and `payment_synced_at` advances.

4. **Existing patient exclusion**: insert lead with `od_patient_status='existing_active'` (if that field is on the call/lead path). Assert sync skips — no payment pull.

5. **OD unavailable**: monkeypatch `_get_od_conn` to return `None`. Assert sync returns `status=skipped` and doesn't raise.

Run with pytest from `backend/`:

```bash
cd /Users/anurag/Documents/Projects/gdc-apps/marketing/lead-lifecycle/backend
source venv/bin/activate
pytest tests/test_od_payment_sync.py -v
```

---

## 7. Things to **NOT** do in this PR

- Do NOT change `roas`, `cpl`, or the optimizer's revenue source. PR 3 will do that.
- Do NOT add the column-header dropdown for 365d/LTV switching. PR 3.
- Do NOT remove the existing `attributed_income` column from `leads` — it's still used elsewhere. Leave it in place; just stop relying on it for display.
- Do NOT touch SEO/organic/direct attribution. Out of scope.
- Do NOT modify `od_matcher.py`'s production logic. It still writes `attributed_production` (planned). PR 2 only adds the parallel paid track.

---

## 8. File-by-file change list

| File | Change |
|------|--------|
| `backend/database.py` | Schema: 5 new cols on `leads`, 3 new cols on `keyword_production_log`. Add migration in the existing `_migrate_*` flow. Modify `get_keyword_stats()` to expose `income_365d` and `income_ltv`. |
| `backend/config.py` | Add `gads_attribution_window_days: int = 365`. |
| `backend/od_payment_sync.py` | **New file.** ~200–300 lines. |
| `backend/main.py` | Add `_od_payment_sync_job()`, add APScheduler job at 22:15 ET, add `/api/admin/sync-payments` endpoint. |
| `frontend/index.html` | Add `Sync OD Payments` + `Backfill All Payments` buttons in AdminTab. Add tooltip on INCOME cell showing Planned/Paid 365d/Paid LTV. |
| `backend/tests/test_od_payment_sync.py` | **New file.** Five tests above. |

---

## 9. Rollout

1. Sonnet implements all of the above on a feature branch.
2. Opus reviews — read every file changed, verify SQL correctness, edge cases, schema migration safety on existing DBs.
3. Run pytest locally. All 5 tests green.
4. Anurag clicks **Backfill All Payments** in Admin once to populate historical data.
5. Verify dashboard tooltip shows sensible Paid 365d numbers for a few known Google Ads patients.
6. Push via GitHub Desktop. Commit summary + description provided.
