# Session Summary — 2026-05-20 (PR 3)

## Topic
PR 3: Income display toggle (365d / LTV / Planned), Admin Settings attribution window, ROI math switches to paid 365d with planned fallback, optimizer wired to read paid_income_365d (with safe fallback so day-1 behavior is unchanged).

## What we did

1. **Drafted PR 3 spec** — `lead-lifecycle/PR3_SPEC_income_mode_optimizer_switch.md`. Defined the column-header dropdown (`<select>` with paid_365d / paid_ltv / planned), localStorage persistence (`gdc_income_mode`), client-side ROI math that follows the displayed income, "🕐 mode" badge when not on default, Admin → Settings dropdown for the attribution window (90/180/365/730 days, default 365), ROI calc switch in `get_unified_campaigns()` with `income_365d > 0 ? income_365d : income` fallback to avoid -100% ROI on brand-new campaigns, and a soft-switch in the AI optimizer (read `paid_income_365d` with fallback to `revenue` — day-1 no-op because the underlying SQL doesn't return paid amounts yet).

2. **Sonnet implemented PR 3.** All 9 tests pass (spec called for 4; Sonnet added 5 bonus edge-case tests). Files touched:
   - **New** `backend/tests/test_pr3_income_mode.py` (9 tests, all pass)
   - **Modified** `backend/database.py` — ROI calc in `get_unified_campaigns()` at 2 sites (managed + synthetic rows) now uses `roi_basis = income_365d if income_365d > 0 else income`
   - **Modified** `backend/ai_optimizer.py` — `revenue_30d` ad-group rollup now uses `paid_income_365d` with fallback to `revenue`; TODO comments at 3 `attributed_production` read sites (deferred to future PR — needs per-keyword paid rollup)
   - **Modified** `backend/main.py` — `GadsAttributionSettingsRequest` Pydantic model + GET/POST endpoints for the attribution-window setting (validates against {90, 180, 365, 730})
   - **Modified** `frontend/index.html` — `incomeMode` state with localStorage, column header `<select>`, IIFE per row computing `displayedIncome` + `displayedRoi`, "🕐 mode" badge, new `GadsAttributionSettingsCard` in AdminTab

3. **Opus reviewed PR 3.** Verdict: **Ship with minor fixes** — Opus found one real bug and fixed it himself:
   - **Bug:** client-side `displayedRoi` didn't mirror the server-side `m.roi` fallback. Brand-new campaigns with planned income but zero paid would show +100% on the server but -100% on the client — exactly the day-1 problem the spec's mitigation aimed to prevent.
   - **Fix applied:** added `const roiBasis = displayedIncome > 0 ? displayedIncome : (m.income || 0)` to the IIFE so client + server compute the same ROI. The Income cell still shows `—` when paid is zero (so the user can see "no paid data yet"), but ROI stays sane.
   - All 9 tests still pass post-fix.
   - Three risk-but-acceptable items logged: optimizer fallback is a no-op today (needs SQL update in future PR), spec had a minor line-count typo on `attributed_production` sites (2 actual code sites, not 3), and `isdigit()` in the GET handler silently falls back to default on corrupted values.
   - Three test gaps worth adding later: frontend ROI parity test, FastAPI POST 422 validation test, integration test for synthetic-row ROI fallback.

## What's ready to push

PR 3 is complete and reviewed. Together with PRs 1 + 2 (already pushed under commit `ad32e92`), the dashboard now has:
- A single "🔄 Sync OpenDental Now" button that runs the 7-step chain (PR 1)
- Actual paid dollars from OD via `paid_amount_365d` / `paid_amount_ltv` (PR 2)
- User-switchable INCOME column with mode persistence (PR 3)
- ROI computed off paid 365d by default, with safe fallback to planned (PR 3)
- Admin Settings dropdown for the attribution window (PR 3, informational only)
- Optimizer wired to read paid 365d (PR 3, no-op until SQL is upgraded — but the hook is in place)

**Git commit summary:** `PR 3: Income display toggle + ROI switches to paid 365d (with planned fallback)`

**Git commit description:**
```
Adds user-switchable INCOME column (365d / LTV / Planned) to the campaign
table, exposes the Google Ads attribution window as an Admin Setting, and
switches ROI math (server-side in get_unified_campaigns) + the AI optimizer
revenue source to read paid_income_365d with safe fallback to planned
production.

Fallback semantics: roi_basis = income_365d if income_365d > 0 else income.
This prevents brand-new campaigns (cost > 0 but no OD payments yet) from
showing -100% ROI and getting mass-paused by the optimizer.

Frontend: column header has an inline <select> dropdown; selection persists
in localStorage as 'gdc_income_mode'. INCOME cell + ROI cell follow the
selected mode via a client-side IIFE. "🕐 LTV mode" / "🕐 Planned mode"
badge appears next to the table title when not on the default. Existing
tooltip with all three numbers (Planned / Paid 365d / Paid LTV) preserved.

Admin → Settings: new GadsAttributionSettingsCard with window dropdown
(90 / 180 / 365 / 730). Persisted via save_setting. Note: setting is
informational this PR — actually recomputing paid_amount_365d for a
non-365d window is a future PR.

Optimizer change: ad_group_stats revenue_30d now reads
ag.get('paid_income_365d') or ag.get('revenue') or 0. The underlying
get_ad_group_stats() SQL doesn't return paid amounts yet, so this is a
day-1 no-op — but the hook is in place for a future SQL upgrade. Three
attributed_production reads in ai_optimizer.py marked with TODO comments
(switch to leads.paid_amount_365d once a per-keyword paid rollup exists).

9 pytest tests pass (4 required by spec + 5 bonus edge cases). Opus-reviewed;
one frontend ROI parity bug found and fixed during review.
```

## Pending follow-ups

- **Call list filters PR** — see [[project-call-list-filters]]. Revert `get_mango_calls_needing_od_match` narrow filter + add stackable GAds/New/Existing/Converted checkbox filters. Planned for after PR 3.
- **Optimizer SQL upgrade** — add `SUM(l.paid_amount_365d)` to `get_ad_group_stats()` so the optimizer's `paid_income_365d` fallback actually has data. Until then, the optimizer keeps reading planned `revenue`. Future PR.
- **Per-keyword paid rollup** — needed before the 3 `attributed_production` sites in `ai_optimizer.py` can switch.
- **3 test cases Opus recommended** — frontend ROI parity, POST 422 validation, synthetic-row ROI integration.
- **Recompute helper for non-365 attribution windows** — currently the setting is informational. Adding `recompute_paid_for_window(days)` would let Anurag experiment with shorter/longer windows.
