# PR 5 — Step-7 OD-down resilience + Appointment Modal patient name + income

**Date drafted:** 2026-05-20
**Status:** SPEC — ready for Sonnet implementation
**Dependencies:** PR 4 (refresh_call_od_income, confidence_tier, booked_override path)

## Why

Two real gaps surfaced after PR 4 shipped:

1. **Bug A — Step 7 (`link_calls_to_keyword_production`) bails entirely when OD MySQL is unreachable.** Even though booked_override rows don't need OD production data (they seed `paid_amount_365d` from `mango_calls.od_patient_income`, which is refreshed in step 4), they get skipped along with everything else. This caused Matthew Cornwell's KPL row to be missing after the live sync despite step 4 working correctly. The dashboard showed ROI -71% but INCOME "—" until the row was manually written from the sandbox.

2. **Bug B — Summary text hides skip reasons.** Current log reads `"Done: processed=N written=0 unchanged=0 lead_wins=0 no_prod=0 errors=0 total_production=$0.00"`. The `skipped_od_unavailable` counter is initialized and populated but never logged, so when OD is down the operator sees a "0 written" message with no indication why.

3. **Modal Gap A — Patient name shows "—" for callers like Matthew.** Modal reads `r.od_patient_name` only. Matthew's `od_patient_name=""` (OD name sync didn't populate it) but his `ai_patient_name="Matthew Cornwell"` from transcript extraction is available. SQL already builds a `patient_name` COALESCE alias but the frontend ignores it.

4. **Modal Gap B — No income column at all.** Operator can see an appointment was matched but can't see how much that patient has paid. KPL is already joined to the query, just not selected/rendered.

## How to apply (future work)

- The booked_override path is **independent of OD MySQL availability** going forward — only the production-amount lookup (step 7 high/low tier path) requires OD. Future regressions should preserve this invariant.
- Step-7 summary must continue to surface non-zero skip counters. If a new skip counter is added, add it to the log.
- Appointment modal is a "what just happened" surface — patient name and income belong there. Stay tier-agnostic on display (show 365d income for booked_override rows the same way as for high-confidence rows).

## Architecture

### Bug A fix — `backend/call_production_log.py:link_calls_to_keyword_production()`

Lines 415–419 today:
```python
od_conn = _get_od_conn()
if od_conn is None:
    logger.warning("[call_prod] OpenDental unavailable — cannot fetch production amounts")
    counts["skipped_od_unavailable"] = len(call_rows)
    return counts
```

**New behavior:** don't bail. Continue to the per-call loop with `od_conn=None`. Inside the loop:
- For booked_override rows (`is_booked_override=True`): skip OD lookup entirely, write KPL row with `production_amount=0.0` (the existing code path already handles this — see line ~450). Increment `counts["written"]` and a NEW `counts["written_booked_override_no_od"]` for visibility.
- For non-booked_override rows: increment `counts["skipped_od_unavailable"]`, `continue`. Don't try to query OD with a None connection.

If `od_conn is None` AND no candidate rows are booked_override, still log the warning but DO NOT early-return — let the loop run so we count each row's skip explicitly.

Pseudocode:
```python
od_conn = _get_od_conn()
if od_conn is None:
    logger.warning("[call_prod] OpenDental unavailable — booked_override rows will still be written; production lookups skipped")

for call_row in call_rows:
    counts["processed"] += 1
    is_booked_override = bool(call_row.get("od_appointment_id") or "")

    if od_conn is None and not is_booked_override:
        counts["skipped_od_unavailable"] += 1
        continue

    # existing production lookup + write logic continues
    # for booked_override rows, _write_call_production_row already seeds from mc.od_patient_income
```

### Bug B fix — summary log line at lines 483–488

Add `skipped_od_unavailable` to the log string. Also add a `skipped_booked_override_written` (or surface the new counter from Bug A). Final log:

```python
logger.info(
    f"[call_prod] Done: processed={counts['processed']} written={counts['written']} "
    f"unchanged={counts['unchanged']} lead_wins={counts['skipped_lead_wins']} "
    f"no_prod={counts['skipped_no_production']} od_unavailable={counts['skipped_od_unavailable']} "
    f"errors={counts['errors']} total_production=${counts['total_production']:.2f}"
)
```

Also update the `unified_od_sync.py` step summary extractor so the per-step display in the AdminTab shows skip counts when non-zero. Find the extractor (search for "call_prod" or step-7 summary builder in `unified_od_sync.py`) and add a branch that appends "(N OD unavail)" to the step message when `skipped_od_unavailable > 0`.

### Modal endpoint — `backend/main.py:8911-8958`

Add two SELECTs to the existing query (no new JOINs needed; `kpl` is already LEFT JOINed at lines 8935–8937):

```sql
mc.ai_patient_name,                       -- new: AI-extracted patient name fallback
COALESCE(kpl.paid_amount_365d, 0) AS paid_amount_365d   -- new: income for this booking
```

Update the existing `patient_name` COALESCE chain to include `ai_patient_name`:
```sql
COALESCE(
    NULLIF(mc.od_patient_name, ''),
    NULLIF(mc.ai_patient_name, ''),
    NULLIF(TRIM(l.first_name||' '||l.last_name), ''),
    NULLIF(TRIM(l2.first_name||' '||l2.last_name), '')
) AS patient_name
```

`paid_amount_365d` only LEFT JOINs by `od_patient_num`, so it will be 0 for unmatched calls — that's the correct display (no income known).

### Modal frontend — `frontend/index.html` lines 12054–12131

**Header row (line 12064 area):** add new `<th>Income</th>` between "Duration" and any trailing column.

**Body row (lines 12088–12102):** replace direct `r.od_patient_name` render with COALESCE to the new `r.patient_name` SQL alias:

```js
const displayName = r.patient_name || r.od_patient_name || r.ai_patient_name || '';
// existing badge logic continues
```

When the displayed name came from `ai_patient_name` (i.e., `od_patient_name` is empty), append a small italic suffix `<span class="muted-italic">(from transcript)</span>` so operators know the name is AI-extracted and may not match OD exactly.

**Body row (after Duration cell ~line 12128):** add new `<td>` for income:
```js
const inc = Number(r.paid_amount_365d || 0);
return inc > 0
  ? `<td class="num">$${inc.toFixed(0)}</td>`
  : `<td class="muted">—</td>`;
```

## Endpoints

No new endpoints. Modifies existing `/api/admin/calls/campaign-appts`.

## Tests

New file: `backend/tests/test_pr5_step7_fix_and_modal.py`

Cases:
1. **`test_step7_continues_with_od_down_writes_booked_override`** — Set up: 1 booked_override call (od_appointment_id='X', confidence=0.0, od_patient_income=199.0). Mock `_get_od_conn()` to return None. Assert: function returns successfully, KPL row gets written with `confidence_tier='booked_override'` and `paid_amount_365d=199.0`, `production_amount=0.0`. Assert `counts['skipped_od_unavailable']==0` (this row didn't need OD) and `counts['written']==1`.
2. **`test_step7_skips_high_conf_rows_when_od_down`** — Set up: 1 high-confidence call (conf=0.7, no appointment_id). Mock OD unavailable. Assert: no KPL row written, `counts['skipped_od_unavailable']==1`, `counts['written']==0`. Function still returns cleanly without raising.
3. **`test_step7_mixed_batch_od_down`** — 1 booked_override + 1 high-conf in same batch, OD down. Assert: booked_override gets written, high-conf gets skipped, summary counts both correctly.
4. **`test_step7_summary_log_includes_od_unavailable`** — Run scenario from test 2, capture log output, assert string contains "od_unavailable=1".
5. **`test_modal_endpoint_returns_ai_patient_name_and_income`** — Insert a mango_calls row with od_patient_name='', ai_patient_name='Matthew Cornwell', od_patient_num=5728, and a matching KPL row with paid_amount_365d=199. Hit `/api/admin/calls/campaign-appts?campaign_name=...`. Assert response includes both `ai_patient_name` and `paid_amount_365d=199.0`, and `patient_name='Matthew Cornwell'` via COALESCE.
6. **`test_modal_endpoint_patient_name_prefers_od_when_present`** — When both `od_patient_name='Smith, John'` and `ai_patient_name='Johnny S'` are set, `patient_name='Smith, John'` (OD wins).
7. **`test_modal_endpoint_paid_amount_zero_when_no_kpl`** — Unmatched call with no KPL row. Assert `paid_amount_365d=0.0` not None.

## Known acceptable risks

- The `(from transcript)` suffix may be cosmetically noisy if many calls go through booked_override. Acceptable trade-off; helps operators trust the data. Can be hidden behind a setting later if needed.
- `ai_patient_name` quality is a function of Vertex AI transcript extraction. Could occasionally be wrong/garbled. Acceptable because the OD apt # is still shown in the modal as the authoritative reference.
- Step-7 bail fix doesn't retry OD — it just allows the booked_override path through. Production lookups for high-confidence calls still need OD. Daily refresh job retries naturally.

## Verification target

After PR 5:
- Force `_get_od_conn()` to return None (or pull the network cable). Trigger unified sync. Step 7 logs: `written=1 od_unavailable=N total_production=$0.00`. Matthew-like booked_override calls get KPL rows.
- Open Appointment Details modal on Emergency Dentistry. Matthew row shows "Matthew Cornwell *(from transcript)*" in OD Patient column and "$199" in Income column.

## Files touched

- `backend/call_production_log.py` — Bug A logic + Bug B counter
- `backend/unified_od_sync.py` — step summary extractor (small)
- `backend/main.py` — campaign-appts endpoint SQL (+2 SELECTs)
- `frontend/index.html` — modal header + body (+Income col, COALESCE name)
- `backend/tests/test_pr5_step7_fix_and_modal.py` — NEW, 7 tests

Cross-refs: [[project-pr4-refresh-call-income]], [[project-call-income-attribution]].
