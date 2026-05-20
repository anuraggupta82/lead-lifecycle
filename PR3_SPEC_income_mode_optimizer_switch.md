# PR 3 — Income Display Toggle + Optimizer Reads Paid 365d

**Goal:** Make the dashboard's INCOME column user-switchable between **365d** (default), **LTV**, and **Planned**; expose the attribution window as a configurable Admin Setting; and switch the AI optimizer and ROI calculation to read **`income_365d`** (actual paid dollars) instead of **`income`** (planned production).

**Why:** PR 2 populated `income_365d` and `income_ltv` but kept the displayed INCOME number and the optimizer's ROI math on planned production for safety. PR 3 cuts the cord — the optimizer starts bidding off real collected money, and Anurag can switch the displayed view at will.

**Scope:** Google Ads attribution only (same as PR 2). No other channels.

---

## 1. Settings DB key + config

Add to `config.py`:
```python
gads_attribution_window_days: int = 365  # already added in PR 2 — keep
```

`config.py` already has the default. PR 3 adds **UI exposure** via the Admin → Settings panel.

Persist the user's override in the existing settings table:
- key: `gads_attribution_window_days`
- value: integer as string ("90", "180", "365", "730")
- read with the existing `get_setting()` helper, fall back to `config.py` default if missing

The window is currently **informational only** for PR 3 — it labels the tooltip and the column-header dropdown options. Recomputing `paid_amount_365d` for a different window is left for a future PR (would require re-running `od_payment_sync` with a new window).

---

## 2. Frontend: campaign table INCOME column dropdown

### 2a. Column header becomes interactive

In `frontend/index.html` around line 7321, replace:

```jsx
<th className="num" title="Attributed production value from OD (new patients only)">Income</th>
```

with a header that includes a small inline `<select>` dropdown:

```jsx
<th className="num" title="...">
  <span style={{display:'inline-flex',alignItems:'center',gap:6}}>
    Income
    <select
      value={incomeMode}
      onChange={e => { setIncomeMode(e.target.value); localStorage.setItem('gdc_income_mode', e.target.value); }}
      style={{fontSize:11, padding:'1px 4px', background:'transparent', border:'1px solid var(--border)', borderRadius:3, color:'var(--text)'}}
    >
      <option value="paid_365d">365d</option>
      <option value="paid_ltv">LTV</option>
      <option value="planned">Planned</option>
    </select>
  </span>
</th>
```

Add at the top of the `CampaignsView` (or whichever component owns this table) component state:
```jsx
const [incomeMode, setIncomeMode] = useState(
  localStorage.getItem('gdc_income_mode') || 'paid_365d'
);
```

### 2b. INCOME cell reads the right field

Update the existing cell (around line 7449) so the displayed dollar amount switches with `incomeMode`:

```jsx
const displayedIncome =
  incomeMode === 'paid_365d' ? (m.income_365d || 0) :
  incomeMode === 'paid_ltv'  ? (m.income_ltv  || 0) :
                                (m.income      || 0);  // planned

<td className="num"
    style={{color: displayedIncome > 0 ? '#059669' : 'var(--text-muted)'}}
    title={[
      `Planned production:  $${Number(m.income||0).toLocaleString('en-US',{maximumFractionDigits:0})}`,
      `Paid (365d):         $${Number(m.income_365d||0).toLocaleString('en-US',{maximumFractionDigits:0})}`,
      `Paid (LTV):          $${Number(m.income_ltv||0).toLocaleString('en-US',{maximumFractionDigits:0})}`,
    ].join('\n')}>
  {displayedIncome > 0 ? fmtMoney(displayedIncome) : '—'}
</td>
```

The tooltip stays — it always shows all three values, so users can see what they're missing relative to whichever view they picked.

### 2c. Mode badge when not on 365d

When `incomeMode !== 'paid_365d'`, add a small "🕐 LTV mode" or "🕐 Planned mode" badge near the table title so the user remembers they're not on the default. Sticky reminder via:

```jsx
{incomeMode !== 'paid_365d' && (
  <span style={{
    marginLeft:8, fontSize:11, padding:'2px 6px',
    background:'#fef3c7', color:'#92400e', borderRadius:4
  }}>
    🕐 {incomeMode === 'paid_ltv' ? 'LTV' : 'Planned'} mode
  </span>
)}
```

### 2d. ROI column reads the same field

The `ROI` column currently uses `m.roi` (computed from `income` server-side). PR 3 needs ROI to track whichever income mode the user picked. Two options:

**Approach A (chosen for this PR):** compute ROI client-side from `m.cost` + `displayedIncome`. One small inline expression in the table; no backend change. Acceptable because the table is already client-rendered.

```jsx
const displayedRoi =
  (m.cost || 0) > 0
    ? Math.round(((displayedIncome - m.cost) / m.cost) * 100)
    : null;

// in the ROI cell:
{displayedRoi != null ? displayedRoi + '%' : '—'}
```

The server-side `m.roi` field stays in place for backwards compatibility but the UI ignores it when an alternate mode is selected.

**Approach B (deferred):** add `roi_365d` and `roi_ltv` to `get_unified_campaigns()`. Cleaner long-term but more code to touch and more places to keep in sync. Skip for now.

---

## 3. Admin → Settings — attribution window control

Find the Admin Settings panel (search for `od_db_host` in `frontend/index.html` — the existing settings UI). Add a new field group:

```jsx
<div className="setting-group">
  <label>
    Google Ads attribution window
    <select
      value={settings.gads_attribution_window_days || 365}
      onChange={e => setSettings({...settings, gads_attribution_window_days: parseInt(e.target.value)})}
    >
      <option value={90}>90 days</option>
      <option value={180}>180 days</option>
      <option value={365}>365 days (default)</option>
      <option value={730}>730 days</option>
    </select>
  </label>
  <p className="setting-help">
    How long after a Google Ads click counts toward that campaign's ROI.
    Default: 365 days. Changing this requires running "Backfill All Payments"
    afterward to recompute paid_amount_365d for all patients.
  </p>
</div>
```

Backend: the existing `/api/admin/settings` POST handler in `main.py` (around line 12178) already accepts dynamic fields. Add `gads_attribution_window_days: int = 365` to the Settings Pydantic model and pass it through `save_setting()`.

**Note:** Changing the window does NOT automatically recompute existing rows. `od_payment_sync.py` reads `config.gads_attribution_window_days` (or the setting if present) at runtime. The next "Backfill All Payments" click rebuilds everything with the new window. Add a one-line UI hint that says so.

---

## 4. Backend: optimizer + ROI computation read `income_365d`

### 4a. `get_unified_campaigns()` — make ROI default to paid 365d

In `backend/database.py` lines 3737 and 3794 (two places — managed and synthetic rows), change:

```python
roi = round((income - cost) / cost * 100, 1) if cost > 0 else None
```

to:

```python
# PR 3: ROI now keys off paid 365d (actual collected dollars), not planned production.
# Fall back to income (planned) only when no payment data exists yet for the campaign.
roi_basis = income_365d if income_365d > 0 else income
roi = round((roi_basis - cost) / cost * 100, 1) if cost > 0 else None
```

The fallback to planned `income` when `income_365d == 0` matters because brand-new campaigns won't have any OD payments yet — falling back to planned production avoids showing -100% ROI on campaigns that just launched.

### 4b. AI optimizer — switch revenue source

In `backend/ai_optimizer.py` line 7576:
```python
"revenue_30d": float(ag.get("revenue") or 0),
```

Becomes:
```python
# PR 3: optimizer reads paid 365d income, not planned production.
# Falls back to revenue (planned) when no payment data yet for the ad group.
"revenue_30d": float(ag.get("paid_income_365d") or ag.get("revenue") or 0),
```

This requires `paid_income_365d` to be present on the ad-group rows passed in via `raw_ag`. Trace where `raw_ag` comes from (likely a SQL query in the optimizer). If it doesn't already select `paid_income_365d` from the underlying view, **add it to that SELECT** — sum the lead-side `paid_amount_365d` and the call-side `keyword_production_log.paid_amount_365d` for that ad group.

If the existing ad-group rollup doesn't have access to paid amounts easily, the cheapest path is to leave the optimizer reading `revenue` (planned) for now and only flip the per-campaign roll-up in `get_unified_campaigns()`. Document this trade-off clearly in the code comment.

### 4c. `attributed_production` reads — leave alone

`ai_optimizer.py` lines 1571, 1766, 1777 read `attributed_production` directly from `leads`. These are used for keyword-level production attribution. **Leave them alone in PR 3.** Switching them is a larger change because it requires the per-keyword paid amount, which we don't have a clean rollup for yet. Future PR.

Add a TODO comment at each site:
```python
# TODO PR 3+: switch to leads.paid_amount_365d once per-keyword rollup exists
```

---

## 5. New endpoint for the column-header dropdown context

Optional but useful: a `GET /api/income-mode-summary` that returns:
```json
{
  "current_window_days": 365,
  "campaigns_with_paid_data": 12,
  "campaigns_with_zero_paid": 3,
  "last_payment_sync_at": "2026-05-20T22:15:00+00:00"
}
```

The UI uses this once on mount to display a small "Data freshness" badge under the income mode dropdown:
> _"Last payment sync: 2 hours ago · 12 campaigns have paid data"_

Helps the user trust the displayed numbers.

Skip if it adds complexity — nice-to-have, not required.

---

## 6. Tests

Create `backend/tests/test_pr3_income_mode.py`:

1. **ROI uses paid when available.** Insert a campaign with `income_365d=$5,000` and `cost=$1,000`. Call `get_unified_campaigns()`. Assert `roi == 400` (not based on planned).
2. **ROI falls back to planned when no paid data.** Insert a campaign with `income_365d=0`, `income=$2,000`, `cost=$1,000`. Assert `roi == 100` (uses planned as fallback).
3. **Settings persistence.** `POST /api/admin/settings` with `gads_attribution_window_days=180`, then GET — assert it round-trips.
4. **Optimizer revenue source.** Mock the ad-group rollup. Assert `revenue_30d` in `all_ag_stats` equals `paid_income_365d` when present, falls back to `revenue` when not.

Run from `backend/`:
```bash
source venv/bin/activate
pytest tests/test_pr3_income_mode.py -v
```

---

## 7. Things to **NOT** do in this PR

- Do NOT recompute `paid_amount_365d` for a non-365 window. The data stays anchored to 365d; the setting is informational only this PR. A future PR can add a recompute helper.
- Do NOT change `attributed_production` reads in `ai_optimizer.py` (lines 1571/1766/1777). Future PR.
- Do NOT remove the existing `income` field. It stays as "planned production" forever — it's still useful.
- Do NOT touch `attributed_income` on leads. Same reason.

---

## 8. File-by-file change list

| File | Change |
|------|--------|
| `backend/database.py` | Change ROI calc in `get_unified_campaigns()` (2 places) to use `income_365d` with fallback. |
| `backend/ai_optimizer.py` | Switch `revenue_30d` ad-group rollup to read `paid_income_365d` with fallback to `revenue`. Add TODO comments at the 3 `attributed_production` sites. |
| `backend/main.py` | Add `gads_attribution_window_days` to Settings model + GET/POST handlers. Optional: add `/api/income-mode-summary`. |
| `frontend/index.html` | Column-header dropdown, `incomeMode` state with localStorage persistence, INCOME cell reads displayed mode, client-side ROI computation, "🕐 mode" badge when not 365d, Admin Settings → attribution window dropdown. |
| `backend/tests/test_pr3_income_mode.py` | **New file.** Four tests. |

---

## 9. Rollout

1. Sonnet implements end-to-end on a feature branch.
2. Opus reviews — focus on optimizer behavior change. Verify no campaign with zero paid data gets its bids tanked because ROI dropped to -100%.
3. Run pytest. All 4 tests green.
4. Manual test: load campaign table → default view is 365d → switch to LTV → verify column updates → switch to Planned → verify column updates → reload → mode persists.
5. Run a dry-run optimizer pass and verify recommendations look sane (no mass-pause of new campaigns).
6. Push via GitHub Desktop.

---

## 10. Risk + mitigation

**Risk:** the optimizer starts pausing campaigns that look profitable on planned but unprofitable on paid 365d. This is the *desired* behavior long-term — paid is the truth — but on day 1 it could over-pause campaigns whose patients just haven't paid yet.

**Mitigation:** the fallback to `revenue` (planned) when `paid_income_365d == 0` covers brand-new campaigns. For campaigns that have *some* paid data but less than planned, the optimizer will start being stricter, which is the point. Anurag can override individual pause recommendations via the existing approve/reject workflow.

**Second risk:** changing the column-header dropdown to "Planned" while the optimizer keeps reading 365d could confuse the user ("the dashboard says +200% ROI but the optimizer wants to pause it"). The "🕐 mode" badge near the table title mitigates this.
