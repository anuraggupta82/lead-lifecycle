"""
unified_od_sync.py — Canonical 8-step Google Ads income attribution chain.

Runs all steps in order, wrapping each in try/except so a single failure
never blocks downstream steps. Progress is written to a module-level dict
and polled by GET /api/admin/sync-od-all/progress.

Canonical step order (non-negotiable — wrong order = wrong attribution):
  1. Firestore Sync         — new leads must exist before anything attributes them
  2. Google Ads gclid→kw    — stamps campaign/ad_group/keyword before OD match
  3. OD Patient Match       — sets od_patient_num before payment pull
  4. Refresh Call Income    — re-pulls OD paid amounts for new-patient calls (PR 4)
                             Must run BEFORE OD Payments so fresh data is available.
  5. OD Payments            — paid amounts before call production dedup
  6. Call → Keyword         — needs gads_clicks table (step 2) and OD match (step 3)
  7. Call Production Log    — needs call attribution (step 6) + OD match (step 3)
  8. Conversion Upload      — needs everything above to be fresh
"""
import logging
import threading
import time as _time_mod
import json
import traceback
from typing import Optional

logger = logging.getLogger(__name__)

# ─── Step definitions ─────────────────────────────────────────────────────────

UNIFIED_SYNC_STEPS = [
    ("Firestore Sync",           "Pulling new leads from web forms…"),
    ("Google Ads Resolver",      "Resolving gclids to campaign/ad group/keyword…"),
    ("OpenDental Patient Match", "Matching leads to OD patients + treatment stages…"),
    ("Refresh Call Income",      "Re-pulling OD paid amounts for new-patient calls…"),   # PR 4 — index 3
    ("OpenDental Payments",      "Pulling paid amounts from OD (365d + LTV)…"),
    ("Call → Keyword",           "Attributing phone calls to paid clicks…"),
    ("Call Production Log",      "Writing call-path keyword production…"),
    ("Conversion Upload",        "Uploading conversions to Google Ads…"),
]

# ─── Module-level progress object (mirrors ai_optimizer.py pattern) ───────────

_unified_sync_progress: dict = {
    "running":      False,
    "step_index":   0,
    "step_label":   "",
    "step_detail":  "",
    "total_steps":  len(UNIFIED_SYNC_STEPS),
    "pct":          0,
    "elapsed_sec":  0,
    "started_at":   None,
    "step_results": [],          # list of {step, status, duration_ms, summary, error?}
    "trigger":      "manual",    # 'manual' | 'scheduled'
}

# Thread safety: the running-flag check + run-start must be inside this lock.
# Progress-object writes (dict.update) are sufficiently atomic for our purposes.
_lock = threading.Lock()


# ─── Progress helpers ─────────────────────────────────────────────────────────

def _set_progress(idx: int, step_results: Optional[list] = None) -> None:
    """Mark step idx as in_progress and update the global progress state."""
    global _unified_sync_progress
    total = len(UNIFIED_SYNC_STEPS)
    idx = max(0, min(idx, total - 1))
    label, detail = UNIFIED_SYNC_STEPS[idx]
    started = _unified_sync_progress.get("started_at") or _time_mod.time()
    update = {
        "running":     True,
        "step_index":  idx,
        "step_label":  label,
        "step_detail": detail,
        "total_steps": total,
        # pct reflects steps *completed* so far (not current in-progress step)
        "pct":         int((idx / total) * 100),
        "elapsed_sec": int(_time_mod.time() - started),
        "started_at":  started,
    }
    if step_results is not None:
        update["step_results"] = step_results
    _unified_sync_progress.update(update)


def _set_progress_done(error: Optional[str] = None) -> None:
    """Mark the run as complete (or fatally failed)."""
    global _unified_sync_progress
    started = _unified_sync_progress.get("started_at") or _time_mod.time()
    update = {
        "running":      False,
        "step_index":   len(UNIFIED_SYNC_STEPS),
        "step_label":   "Complete" if not error else "Failed",
        "step_detail":  "All steps finished." if not error else f"Fatal error: {error}",
        "total_steps":  len(UNIFIED_SYNC_STEPS),
        "pct":          100,
        "elapsed_sec":  int(_time_mod.time() - started),
    }
    if error:
        update["fatal_error"] = error
    _unified_sync_progress.update(update)


def get_unified_sync_progress() -> dict:
    """Return a shallow copy of the current progress state (called by the endpoint)."""
    return dict(_unified_sync_progress)


# ─── Per-step summary extractors (best-effort; default to "completed") ────────

def _summarize_firestore(r) -> str:
    """r is a combined dict of sync_from_firestore + sync_unsubscribes results."""
    try:
        synced = (r or {}).get("synced", 0)
        applied = (r or {}).get("unsub_applied", 0)
        return f"{synced} new leads, {applied} unsubscribes"
    except Exception:
        return "completed"


def _summarize_gads(r) -> str:
    """r is the return value of sync_gclids_to_keywords."""
    try:
        resolved   = (r or {}).get("resolved", 0)
        unmatched  = (r or {}).get("unmatched", 0)
        return f"{resolved} gclids resolved, {unmatched} still unmatched"
    except Exception:
        return "completed"


def _summarize_od_match(r) -> str:
    """r is the return value of run_full_od_sync."""
    try:
        matched = (r or {}).get("matched", 0)
        updated = (r or {}).get("stages_updated", 0)
        if matched == 0 and updated == 0:
            # Fallback: some versions return different keys
            matched = (r or {}).get("new_matches", 0)
            updated = (r or {}).get("updated", 0)
        return f"{matched} leads matched, {updated} stages updated"
    except Exception:
        return "completed"


def _summarize_refresh_call_income(r) -> str:
    """r is the return value of refresh_call_od_income (PR 4)."""
    try:
        if (r or {}).get("status") == "skipped":
            return "skipped (OD unavailable)"
        updated = (r or {}).get("calls_updated", 0)
        total   = (r or {}).get("calls_refreshed", 0)
        income  = (r or {}).get("total_income_synced", 0.0) or 0.0
        return f"{updated}/{total} calls updated, ${income:,.0f} total income"
    except Exception:
        return "completed"


def _summarize_od_payments(r) -> str:
    """r is the return value of sync_od_payments."""
    try:
        total_paid = (r or {}).get("total_paid_365d", 0) or 0
        patients   = (r or {}).get("patients_synced", 0)
        return f"${total_paid:,.0f} paid (365d) across {patients} patients"
    except Exception:
        return "completed"


def _summarize_call_kw(r) -> str:
    """r is the return value of attribute_calls_to_keywords."""
    try:
        attributed = (r or {}).get("attributed", 0)
        total      = (r or {}).get("total", 0)
        below      = (r or {}).get("below_threshold", 0)
        return f"{attributed}/{total} calls attributed ({below} below 0.55 threshold)"
    except Exception:
        return "completed"


def _summarize_call_production(r) -> str:
    """r is the return value of link_calls_to_keyword_production."""
    try:
        # 'written' is the key in the counts dict; 'rows_written' is an alias
        written = (r or {}).get("written", 0) or (r or {}).get("rows_written", 0)
        od_unavail = (r or {}).get("skipped_od_unavailable", 0)
        msg = f"{written} call:: rows written/updated"
        if od_unavail > 0:
            msg += f" ({od_unavail} OD unavail)"
        return msg
    except Exception:
        return "completed"


def _summarize_conversions(r) -> str:
    """r is the return value of upload_offline_conversions."""
    try:
        uploaded = (r or {}).get("uploaded", 0)
        return f"{uploaded} conversions uploaded to Google Ads"
    except Exception:
        return "completed"


# ─── Orchestrator ─────────────────────────────────────────────────────────────

def run_unified_od_sync(trigger: str = "manual") -> dict:
    """
    Run the 7-step Google Ads income attribution chain in canonical order.

    Thread-safe: a second call while already running returns immediately with
    {"status": "already_running"} — the lock protects the running-flag check
    and the run-start assignment together.

    Each step is wrapped in its own try/except so a failure in step N does NOT
    prevent steps N+1 through 7 from running. Failures are recorded in
    step_results with status='error'.

    At the end (success or partial), the final progress dict is persisted to
    the settings table under key 'unified_od_sync_last_run' so the /last-run
    endpoint can return it even after a page reload.

    Returns the final progress dict.
    """
    global _unified_sync_progress

    # ── Double-click guard (running check + start inside lock) ────────────────
    with _lock:
        if _unified_sync_progress.get("running"):
            logger.info("[unified_sync] Already running — ignoring duplicate start request")
            return {"status": "already_running", **_unified_sync_progress}
        # Mark running immediately inside the lock so a concurrent call sees it
        _unified_sync_progress.update({
            "running":      True,
            "step_index":   0,
            "step_label":   UNIFIED_SYNC_STEPS[0][0],
            "step_detail":  UNIFIED_SYNC_STEPS[0][1],
            "total_steps":  len(UNIFIED_SYNC_STEPS),
            "pct":          0,
            "elapsed_sec":  0,
            "started_at":   _time_mod.time(),
            "step_results": [],
            "trigger":      trigger,
        })

    step_results = []
    started_at = _unified_sync_progress["started_at"]

    def _run_step(step_idx: int, fn, summarize_fn):
        """Execute a single step, record result, and return (status, result)."""
        label, detail = UNIFIED_SYNC_STEPS[step_idx]
        _set_progress(step_idx, step_results=step_results)
        t_start = _time_mod.time()
        try:
            result = fn()
            duration_ms = int((_time_mod.time() - t_start) * 1000)
            try:
                summary = summarize_fn(result)
            except Exception:
                summary = "completed"
            entry = {
                "step":        label,
                "status":      "ok",
                "duration_ms": duration_ms,
                "summary":     summary,
            }
            step_results.append(entry)
            logger.info(f"[unified_sync] Step {step_idx+1}/{len(UNIFIED_SYNC_STEPS)} '{label}' ok — {summary} ({duration_ms}ms)")
            return "ok", result
        except Exception as exc:
            duration_ms = int((_time_mod.time() - t_start) * 1000)
            error_msg = str(exc)[:500]
            logger.error(
                f"[unified_sync] Step {step_idx+1}/{len(UNIFIED_SYNC_STEPS)} '{label}' FAILED: {exc}\n"
                + traceback.format_exc()
            )
            entry = {
                "step":        label,
                "status":      "error",
                "duration_ms": duration_ms,
                "summary":     "failed",
                "error":       error_msg,
            }
            step_results.append(entry)
            return "error", None

    try:
        # ── Step 1: Firestore Sync ────────────────────────────────────────────
        def _do_firestore():
            from firestore_sync import sync_from_firestore, sync_unsubscribes_from_firestore
            r1 = sync_from_firestore()
            r2 = sync_unsubscribes_from_firestore()
            # Merge into a single dict for the summarizer
            merged = dict(r1 or {})
            merged["unsub_applied"] = (r2 or {}).get("applied", 0)
            return merged

        _run_step(0, _do_firestore, _summarize_firestore)

        # ── Step 2: Google Ads gclid → keyword ───────────────────────────────
        def _do_gads():
            from google_ads_sync import sync_gclids_to_keywords
            return sync_gclids_to_keywords(days_back=7)

        _run_step(1, _do_gads, _summarize_gads)

        # ── Step 3: OD Patient Match + Treatment Stages ───────────────────────
        def _do_od_match():
            from od_matcher import run_full_od_sync
            return run_full_od_sync()

        _run_step(2, _do_od_match, _summarize_od_match)

        # ── Step 4: Refresh Call OD Income (PR 4) ────────────────────────────
        # Must run BEFORE OD Payments (step 5) so fresh mango_calls.od_patient_income
        # is available and any new KPL rows from step 7 get correct paid amounts.
        def _do_refresh_call_income():
            from od_payment_sync import refresh_call_od_income
            return refresh_call_od_income(days=90)

        _run_step(3, _do_refresh_call_income, _summarize_refresh_call_income)

        # ── Step 5: OD Payment Pull (PR 2) ────────────────────────────────────
        def _do_od_payments():
            from od_payment_sync import sync_od_payments
            return sync_od_payments(days_back=7)

        _run_step(4, _do_od_payments, _summarize_od_payments)

        # ── Step 6: Call → Keyword Attribution ───────────────────────────────
        def _do_call_kw():
            from call_keyword_attribution import attribute_calls_to_keywords
            return attribute_calls_to_keywords(days=7)

        _run_step(5, _do_call_kw, _summarize_call_kw)

        # ── Step 7: Call Production Log ───────────────────────────────────────
        def _do_call_production():
            from call_production_log import link_calls_to_keyword_production
            return link_calls_to_keyword_production(days=7)

        _run_step(6, _do_call_production, _summarize_call_production)

        # ── Step 8: Conversion Upload to Google Ads ───────────────────────────
        def _do_conversions():
            from google_ads_conversions import upload_offline_conversions
            return upload_offline_conversions()

        _run_step(7, _do_conversions, _summarize_conversions)

    finally:
        # Always mark done and persist, even if something panicked above
        _set_progress_done()
        # Stamp the final step_results into the progress object before persisting
        _unified_sync_progress["step_results"] = step_results
        _unified_sync_progress["elapsed_sec"] = int(_time_mod.time() - started_at)

        # Persist to settings table for /last-run endpoint
        try:
            from database import save_setting
            save_setting("unified_od_sync_last_run", json.dumps(_unified_sync_progress))
        except Exception as exc:
            logger.error(f"[unified_sync] Failed to persist last-run to settings: {exc}")

        ok_count = sum(1 for s in step_results if s.get("status") == "ok")
        total_n = len(UNIFIED_SYNC_STEPS)
        logger.info(
            f"[unified_sync] Chain complete — {ok_count}/{total_n} steps ok "
            f"trigger={trigger} elapsed={_unified_sync_progress.get('elapsed_sec')}s"
        )

    return dict(_unified_sync_progress)
