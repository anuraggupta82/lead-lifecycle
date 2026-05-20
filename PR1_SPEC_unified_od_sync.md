# PR 1 — Unified "Sync OpenDental" Button

**Goal:** Replace the current six independent Admin sync buttons with a single **"Sync OpenDental"** button that runs the full attribution chain in the correct order, in one background thread, with a polling progress endpoint so the UI can show "3/7 Matching OpenDental patients…".

**Why:** Today the Admin tab exposes six buttons (Sync Firestore, Match OpenDental, Google Ads Sync, Upload Conversions, Sync Call Production, Sync OD Payments). The order they're run in matters — Match before Google Ads Sync stamps leads without campaign/keyword; Refresh Income before Call Production misses calls. Anyone but Anurag will get it wrong. The unified button removes the foot-gun.

**Out of scope:** GA4 refresh, IMAP poll, Keyword Intelligence rebuild, Domain Crawler, Nearby Practices sync, AI Optimizer. Those run on their own schedules and are not part of the income-attribution chain. They stay in the Advanced disclosure.

---

## 1. The canonical chain

These seven steps, in this order, are what the new unified sync runs:

| # | Step | Module / Endpoint | Why it must come in this position |
|---|------|--------------------|-----------------------------------|
| 1 | Firestore Sync | `firestore_sync.sync_from_firestore()` + `sync_unsubscribes_from_firestore()` | Pulls new leads from web forms / smile design before anything else can attribute them. |
| 2 | Google Ads gclid → keyword | `google_ads_sync.sync_gclids_to_keywords(days_back=7)` | Stamps `campaign_id`/`ad_group_name`/`keyword_text` on leads. **Must precede OD match** — otherwise OD match runs against leads with empty campaign fields. |
| 3 | OD Patient Match + Treatment Stages | `od_matcher.run_full_od_sync()` | Links leads to `od_patient_num` and writes planned `attributed_production`. **Must precede payment pull** — payment sync needs `od_patient_num` set. |
| 4 | OD Payment Pull (PR 2) | `od_payment_sync.sync_od_payments(days_back=7)` | Writes `paid_amount_365d`/`paid_amount_ltv` on leads + keyword_production_log. **Must precede call production** — call production refresh uses lead paid amounts for dedup. |
| 5 | Call → Keyword Attribution | `call_keyword_attribution.attribute_calls_to_keywords(days=7)` | Resolves phone-only calls to a paid click via the 4-method fallback. **Must follow Google Ads sync** (needs `gads_clicks` table). |
| 6 | Call Production Log | `call_production_log.link_calls_to_keyword_production(days=7)` | Writes `keyword_production_log` rows for `call::%` patients. **Must follow call attribution + OD match**. |
| 7 | Conversion Upload to Google Ads | `google_ads_conversions.upload_offline_conversions()` | Uploads appointment-booked + shown events to Google Ads. **Last** — needs everything above to be fresh. |

If any step fails, **the chain continues** (subsequent steps may still produce partial value). The progress object captures the failure on that step so the UI can show a red marker.

---

## 2. New module: `backend/unified_od_sync.py`

A single orchestrator. New file, ~250 lines.

### 2a. Progress object (mirrors `ai_optimizer.py` pattern)

```python
UNIFIED_SYNC_STEPS = [
    ("Firestore Sync",          "Pulling new leads from web forms…"),
    ("Google Ads Resolver",     "Resolving gclids to campaign/ad group/keyword…"),
    ("OpenDental Patient Match","Matching leads to OD patients + treatment stages…"),
    ("OpenDental Payments",     "Pulling paid amounts from OD (365d + LTV)…"),
    ("Call → Keyword",          "Attributing phone calls to paid clicks…"),
    ("Call Production Log",     "Writing call-path keyword production…"),
    ("Conversion Upload",       "Uploading conversions to Google Ads…"),
]

_unified_sync_progress: dict = {
    "running": False,
    "step_index": 0,
    "step_label": "",
    "step_detail": "",
    "total_steps": len(UNIFIED_SYNC_STEPS),
    "pct": 0,
    "elapsed_sec": 0,
    "started_at": None,
    "step_results": [],         # list of {step, status, duration_ms, summary, error?}
    "trigger": "manual",        # 'manual' | 'scheduled'
}
```

Functions: `_set_progress(idx, ...)`, `_set_progress_done()`, `get_unified_sync_progress()`, `run_unified_od_sync(trigger='manual')`.

### 2b. Orchestrator function

```python
def run_unified_od_sync(trigger: str = "manual") -> dict:
    """
    Run the 7-step Google Ads income attribution chain in canonical order.
    Updates _unified_sync_progress as it goes. Each step is wrapped in try/except
    so a single failure does not block downstream steps.

    Returns the final progress dict.
    """
```

For each step:
1. Mark step as `in_progress` in the progress object.
2. Record `t_start = time.time()`.
3. Call the underlying sync function inside try/except.
4. On success: record `status='ok'`, `duration_ms`, and a 1-line `summary` extracted from the result dict (e.g., `"42 new leads, 18 matched"`).
5. On failure: record `status='error'`, `error` = `str(e)[:500]`. Log full traceback. **Continue to next step.**

After all 7 steps, call `_set_progress_done()` with a final summary.

### 2c. Summary line extractors

For each step, write a tiny `_summarize_<step>(result_dict) -> str` helper to turn the underlying function's result into a single human line. Examples:

- Firestore: `"24 new leads, 3 unsubscribes"`
- GAds resolver: `"12 gclids resolved, 8 still unmatched"`
- OD match: `"18 leads matched, 12 stages updated"`
- OD payments: `"$4,820 paid (365d) across 14 patients"`
- Call → keyword: `"19/22 calls attributed (3 below 0.55 threshold)"`
- Call production: `"7 new call::%% rows written"`
- Conversions: `"4 conversions uploaded to Google Ads"`

These are best-effort — if the result dict shape isn't what we expect, return `"completed"`.

### 2d. Thread safety

Use a module-level `_lock = threading.Lock()` and inside `run_unified_od_sync` check `if _unified_sync_progress["running"]: return {"status": "already_running", ...}`. The endpoint kicks off in a background thread (like `/api/admin/optimize`), so a double-click can't fire two chains.

---

## 3. New endpoints in `main.py`

Add three endpoints next to the existing admin sync routes:

```python
@app.post("/api/admin/sync-od-all", dependencies=[Depends(_require_admin)])
def admin_sync_od_all():
    """
    Kicks off the unified 7-step OD sync chain in a background thread.
    Poll progress via GET /api/admin/sync-od-all/progress.
    Returns immediately with 202-style {"status": "started"}.
    """
    from unified_od_sync import run_unified_od_sync, get_unified_sync_progress
    progress = get_unified_sync_progress()
    if progress.get("running"):
        return {"status": "already_running", "progress": progress}

    def _run():
        try:
            run_unified_od_sync(trigger="manual")
        except Exception as e:
            logger.error(f"Unified OD sync failed: {e}", exc_info=True)
            try:
                from unified_od_sync import _set_progress_done
                _set_progress_done(error=str(e))
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "started"}


@app.get("/api/admin/sync-od-all/progress", dependencies=[Depends(_require_admin)])
def admin_sync_od_all_progress():
    """Return current progress state (dict)."""
    from unified_od_sync import get_unified_sync_progress
    return get_unified_sync_progress()


@app.get("/api/admin/sync-od-all/last-run", dependencies=[Depends(_require_admin)])
def admin_sync_od_all_last_run():
    """
    Return the last completed run's progress dict (read from a sqlite settings row).
    Used by the UI to show "Last synced: 14 minutes ago · 7/7 ok" on page load
    even if the user wasn't watching the sync run.
    """
    from database import get_setting
    import json
    raw = get_setting("unified_od_sync_last_run")
    if not raw:
        return {"never_run": True}
    try:
        return json.loads(raw)
    except Exception:
        return {"never_run": True}
```

`run_unified_od_sync` writes its final progress object to `set_setting("unified_od_sync_last_run", json.dumps(progress))` at the end of every run.

---

## 4. APScheduler: replace individual jobs with the unified job

In `main.py` around lines 384–405, the current job graph has 6 separate jobs:
- `firestore_sync` every 15 min
- `gads_sync` at 06:00
- `od_sync` at 22:00
- `od_payment_sync` at 22:15
- `call_production` at 22:30
- `conversion_upload` at 23:00

**Keep Firestore sync at 15-min cadence as a fast-path** for landing-page form submissions — that's its own use case (lifecycle email triggers fire off new leads).

**Replace the five evening jobs (`gads_sync`, `od_sync`, `od_payment_sync`, `call_production`, `conversion_upload`) with one job:**

```python
def _unified_od_sync_job():
    _stamp("unified_od_sync")
    try:
        from unified_od_sync import run_unified_od_sync
        result = run_unified_od_sync(trigger="scheduled")
        logger.info(f"Scheduled unified OD sync: pct={result.get('pct')}, "
                    f"steps={len(result.get('step_results',[]))}")
    except Exception as e:
        logger.error(f"Scheduled unified OD sync failed: {e}", exc_info=True)

ads_scheduler.add_job(_unified_od_sync_job, CronTrigger(hour=22, minute=0),
                      id="unified_od_sync", name="Unified OD Sync (chain)",
                      max_instances=1, coalesce=True, replace_existing=True)
```

Remove the five `add_job` calls being replaced. **Keep their underlying job functions** (`_gads_sync_job`, `_od_sync_job`, etc.) — they're still referenced by individual admin endpoints in the Advanced disclosure.

Also bump GAds sync from 06:00 to inside the 22:00 chain. The reason 06:00 existed historically is to refresh keyword cache before the 07:00 AI optimizer. That separate use case still matters, so **keep a 06:00 GAds-only refresh job** (rename to `gads_morning_refresh`) that calls only `sync_gclids_to_keywords` for that purpose. The 22:00 chain refreshes it again for the OD attribution work.

---

## 5. Frontend changes (`frontend/index.html`)

### 5a. AdminTab refactor

In the `AdminTab` component (around line 16183), restructure as follows:

**Top section — Primary sync (the new big button):**

```jsx
<div style={{marginBottom:24, padding:16, background:'var(--bg-card)', borderRadius:8}}>
  <h3 style={{marginTop:0}}>Sync OpenDental</h3>
  <p style={{color:'var(--text-muted)', fontSize:13, marginBottom:12}}>
    Pulls new leads, resolves Google Ads attribution, matches patients,
    pulls payments, and uploads conversions. Runs nightly at 10 PM ET.
  </p>
  <button
    className="btn btn-teal"
    style={{fontSize:16, padding:'10px 20px'}}
    onClick={startUnifiedSync}
    disabled={unifiedSyncRunning}
  >
    {unifiedSyncRunning ? '⟳ Syncing…' : '🔄 Sync OpenDental Now'}
  </button>
  {unifiedSyncRunning && (
    <UnifiedSyncProgress progress={unifiedSyncProgress} />
  )}
  {lastSyncDisplay && !unifiedSyncRunning && (
    <div style={{marginTop:12, fontSize:13, color:'var(--text-muted)'}}>
      Last synced: {lastSyncDisplay.relative} · {lastSyncDisplay.summary}
    </div>
  )}
</div>
```

**Bottom section — Advanced (collapsed by default):**

```jsx
<details style={{marginTop:24}}>
  <summary style={{cursor:'pointer', color:'var(--text-muted)'}}>
    Advanced — run individual sync steps
  </summary>
  <div style={{marginTop:12}}>
    {/* All 6 existing buttons stay here, unchanged */}
  </div>
</details>
```

### 5b. `UnifiedSyncProgress` component

New small component, ~50 lines. Polls `/api/admin/sync-od-all/progress` every 1.5 seconds while `running=true`. Renders:

- Overall progress bar with `pct`%
- Current step label + detail
- A vertical list of all 7 steps with status icons:
  - ✅ green for `ok` steps
  - 🔴 red for `error` steps (hover shows error)
  - ⟳ teal for `in_progress`
  - ⚪ gray for `pending`
- Each completed step shows its `summary` line (e.g., `"$4,820 paid (365d) across 14 patients"`).
- When `running=false` and `pct=100`, replace the polling with a static "Done · last summary" line.

### 5c. Start logic

```jsx
async function startUnifiedSync() {
  try {
    setUnifiedSyncRunning(true);
    await api('/api/admin/sync-od-all', { method: 'POST' });
    pollUnifiedSync();
  } catch (e) {
    alert('Failed to start sync: ' + e.message);
    setUnifiedSyncRunning(false);
  }
}

async function pollUnifiedSync() {
  try {
    const p = await api('/api/admin/sync-od-all/progress');
    setUnifiedSyncProgress(p);
    if (p.running) {
      setTimeout(pollUnifiedSync, 1500);
    } else {
      setUnifiedSyncRunning(false);
      loadLastSync();   // refresh the "Last synced" line
    }
  } catch (e) {
    setUnifiedSyncRunning(false);
  }
}

async function loadLastSync() {
  try {
    const r = await api('/api/admin/sync-od-all/last-run');
    if (!r.never_run) {
      setLastSyncDisplay({
        relative: relativeTimeFrom(r.started_at, r.elapsed_sec),
        summary: `${r.step_results.filter(s=>s.status==='ok').length}/${r.total_steps} ok`,
      });
    }
  } catch(_) {}
}
```

Call `loadLastSync()` once on `AdminTab` mount so the user sees the last-run line even before they click the button.

---

## 6. Settings DB key

Add a getter/setter pair to `database.py` if not already present (search for existing `get_setting`/`set_setting` — these likely exist for `od_db_*` config). Use them with key `unified_od_sync_last_run`. Value is JSON-serialized final progress dict.

---

## 7. Tests

Create `backend/tests/test_unified_od_sync.py`:

1. **Happy path**: monkeypatch every underlying sync function to return a stub dict. Run `run_unified_od_sync()`. Assert progress shows 7 ok steps, `pct=100`, `running=false`, all step summaries set.

2. **Single step failure**: monkeypatch step 3 (`run_full_od_sync`) to raise `RuntimeError("OD unreachable")`. Assert the chain continues, step 3 is marked `error` with the message, steps 4–7 still execute.

3. **Already running guard**: directly set `_unified_sync_progress["running"] = True`. Call `run_unified_od_sync()`. Assert it returns immediately with `status="already_running"` and does NOT execute any step.

4. **Last-run persistence**: monkeypatch all syncs to succeed. Run. Assert `get_setting("unified_od_sync_last_run")` returns a JSON dict with `pct=100`.

5. **Endpoint smoke test**: start the FastAPI test client, POST `/api/admin/sync-od-all`. With all sync functions monkeypatched, assert the response is `{"status": "started"}` and within 2 seconds the progress endpoint shows `pct=100`.

Run with:
```bash
cd /Users/anurag/Documents/Projects/gdc-apps/marketing/lead-lifecycle/backend
source venv/bin/activate
pytest tests/test_unified_od_sync.py -v
```

---

## 8. Things to **NOT** do in this PR

- Do NOT remove the 6 individual admin endpoints (`/api/admin/sync`, `/api/admin/match`, `/api/admin/gads-sync`, `/api/admin/upload-conversions`, `/api/admin/sync-call-production`, `/api/admin/sync-payments`). They stay for the Advanced disclosure + scripted use.
- Do NOT change any of the underlying sync function signatures. The orchestrator calls them as-is.
- Do NOT touch the AI optimizer, GA4 pull, IMAP poll, domain crawler, or nearby practices sync — they're not in the chain.
- Do NOT add `unified_od_sync_last_run` to the existing `settings` schema migration; just use the existing key/value `get_setting`/`set_setting` helpers.

---

## 9. File-by-file change list

| File | Change |
|------|--------|
| `backend/unified_od_sync.py` | **New file.** Progress object, orchestrator, summary extractors. |
| `backend/main.py` | Add 3 endpoints (`POST /api/admin/sync-od-all`, GET `/progress`, GET `/last-run`). Replace 5 evening APScheduler jobs with one `unified_od_sync` job at 22:00. Add `gads_morning_refresh` at 06:00. |
| `frontend/index.html` | Refactor `AdminTab`: primary unified-sync card + Advanced disclosure. New `UnifiedSyncProgress` component. Polling logic. |
| `backend/tests/test_unified_od_sync.py` | **New file.** Five tests. |

---

## 10. Rollout

1. Sonnet implements the spec end-to-end.
2. Opus reviews — read every file, verify ordering, failure isolation, thread safety, UI polling.
3. Run pytest. All 5 tests green.
4. Anurag clicks "Sync OpenDental Now" in Admin → watches progress bar fill up.
5. Verify last-run line persists across page reloads.
6. Push via GitHub Desktop. Commit summary + description provided.

---

## 11. Edge cases to verify

- **Double-click guard**: clicking the button twice in 100 ms must NOT start two chains. The `running` flag + thread lock handles this server-side; the button also disables on click.
- **Tab close while running**: if the user closes the browser tab mid-sync, the background thread continues to completion on the server. Next page load shows the last-run line.
- **Step that returns no result dict**: some underlying functions might return `None`. The summary extractor must handle that — default to `"completed"`.
- **OD MySQL down during the chain**: steps 3 and 4 will fail (graceful), but steps 5–7 should still run. The UI shows two red steps, five green.
- **Conversion upload requires fresh OD data**: step 7 reads from `leads.showed_at` set in step 3. If step 3 failed, step 7 has nothing new to upload — that's fine, it returns 0 uploads.
- **`unified_od_sync_last_run` JSON corruption**: the `/last-run` endpoint must return `{"never_run": True}` if the JSON parse fails, not crash.
