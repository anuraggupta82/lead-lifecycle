"""
Tests for unified_od_sync.py — PR 1

Run from backend/:
    source venv/bin/activate
    pytest tests/test_unified_od_sync.py -v
"""
import json
import sys
import os
import time
import threading
from unittest.mock import patch, MagicMock

import pytest

# ── Ensure backend/ is on sys.path ────────────────────────────────────────────
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)


# ─────────────────────────────────────────────────────────────────────────────
# Stub return values for all 9 underlying sync functions (updated for PR 1)
# ─────────────────────────────────────────────────────────────────────────────

FIRESTORE_STUB     = {"synced": 5, "unsub_applied": 2}
GADS_STUB          = {"resolved": 12, "unmatched": 3}
OD_MATCH_STUB      = {"matched": 10, "stages_updated": 4}
CALL_INTEL_STUB    = {"processed": 5, "errors": 0, "skipped": 1}   # PR 1 — new
REFRESH_INCOME_STUB = {"calls_updated": 3, "calls_refreshed": 3, "total_income_synced": 1200.0}
OD_PAYMENTS_STUB   = {"total_paid_365d": 4820, "patients_synced": 14}
CALL_KW_STUB       = {"attributed": 19, "total": 22, "below_threshold": 3}
CALL_PROD_STUB     = {"rows_written": 7}
CONVERSIONS_STUB   = {"uploaded": 4}


def _make_all_stubs():
    """Return a dict of all 9 module-level mock patches (PR 1 adds call_intelligence)."""
    return {
        "firestore_sync.sync_from_firestore":             MagicMock(return_value={"synced": 5}),
        "firestore_sync.sync_unsubscribes_from_firestore": MagicMock(return_value={"applied": 2}),
        "google_ads_sync.sync_gclids_to_keywords":        MagicMock(return_value=GADS_STUB),
        "od_matcher.run_full_od_sync":                    MagicMock(return_value=OD_MATCH_STUB),
        "call_intelligence.run_call_intelligence":        MagicMock(return_value=CALL_INTEL_STUB),
        "od_payment_sync.refresh_call_od_income":         MagicMock(return_value=REFRESH_INCOME_STUB),
        "od_payment_sync.sync_od_payments":               MagicMock(return_value=OD_PAYMENTS_STUB),
        "call_keyword_attribution.attribute_calls_to_keywords": MagicMock(return_value=CALL_KW_STUB),
        "call_production_log.link_calls_to_keyword_production": MagicMock(return_value=CALL_PROD_STUB),
        "google_ads_conversions.upload_offline_conversions":    MagicMock(return_value=CONVERSIONS_STUB),
    }


def _reset_progress():
    """Reset the module-level progress object to its initial state before each test."""
    import unified_od_sync as uos
    uos._unified_sync_progress.update({
        "running":      False,
        "step_index":   0,
        "step_label":   "",
        "step_detail":  "",
        "total_steps":  len(uos.UNIFIED_SYNC_STEPS),
        "pct":          0,
        "elapsed_sec":  0,
        "started_at":   None,
        "step_results": [],
        "trigger":      "manual",
    })


# ─────────────────────────────────────────────────────────────────────────────
# Test 1 — Happy path: all 9 steps succeed (updated for PR 1)
# ─────────────────────────────────────────────────────────────────────────────

def test_happy_path_all_steps_ok():
    """All 9 sync functions succeed → progress shows 9 ok steps, pct=100, running=False."""
    _reset_progress()

    stubs = _make_all_stubs()

    with patch.dict("sys.modules", {
        "firestore_sync":           _module_stub("firestore_sync",   stubs),
        "google_ads_sync":          _module_stub("google_ads_sync",  stubs),
        "od_matcher":               _module_stub("od_matcher",       stubs),
        "call_intelligence":        _module_stub("call_intelligence", stubs),
        "od_payment_sync":          _module_stub("od_payment_sync",  stubs),
        "call_keyword_attribution": _module_stub("call_keyword_attribution", stubs),
        "call_production_log":      _module_stub("call_production_log",      stubs),
        "google_ads_conversions":   _module_stub("google_ads_conversions",   stubs),
        "database":                 _db_stub(),
    }):
        import unified_od_sync as uos
        result = uos.run_unified_od_sync(trigger="test")

    assert result.get("running") is False,       "running must be False when done"
    assert result.get("pct") == 100,             "pct must be 100 when all steps finish"
    step_results = result.get("step_results", [])
    assert len(step_results) == 9,               "must record exactly 9 step results"
    ok_steps = [s for s in step_results if s.get("status") == "ok"]
    assert len(ok_steps) == 9,                   "all 9 steps must be ok"
    # Verify summaries are not empty
    for s in step_results:
        assert s.get("summary"), f"Step '{s['step']}' must have a summary"


# ─────────────────────────────────────────────────────────────────────────────
# Test 2 — Single step failure: chain continues, downstream steps still run
# ─────────────────────────────────────────────────────────────────────────────

def test_single_step_failure_chain_continues():
    """Step 3 (OD Match) raises → steps 4–9 still run; step 3 marked error."""
    _reset_progress()

    stubs = _make_all_stubs()
    # Make step 3 blow up
    stubs["od_matcher.run_full_od_sync"] = MagicMock(side_effect=RuntimeError("OD unreachable"))
    od_stub = _module_stub("od_matcher", stubs)

    with patch.dict("sys.modules", {
        "firestore_sync":           _module_stub("firestore_sync",   stubs),
        "google_ads_sync":          _module_stub("google_ads_sync",  stubs),
        "od_matcher":               od_stub,
        "call_intelligence":        _module_stub("call_intelligence", stubs),
        "od_payment_sync":          _module_stub("od_payment_sync",  stubs),
        "call_keyword_attribution": _module_stub("call_keyword_attribution", stubs),
        "call_production_log":      _module_stub("call_production_log",      stubs),
        "google_ads_conversions":   _module_stub("google_ads_conversions",   stubs),
        "database":                 _db_stub(),
    }):
        import unified_od_sync as uos
        result = uos.run_unified_od_sync(trigger="test")

    step_results = result.get("step_results", [])
    assert len(step_results) == 9, "all 9 steps must be recorded even when one fails"

    step3 = next((s for s in step_results if s["step"] == "OpenDental Patient Match"), None)
    assert step3 is not None,               "step 3 result must be present"
    assert step3["status"] == "error",      "step 3 must be marked error"
    assert "OD unreachable" in step3.get("error", ""), "error message must be captured"

    # Steps 4–9 must have run (status ok or error, but present and not missing)
    later_names = {
        "Call Intelligence", "Refresh Call Income",
        "OpenDental Payments", "Call → Keyword", "Call Production Log", "Conversion Upload"
    }
    recorded_names = {s["step"] for s in step_results}
    assert later_names.issubset(recorded_names), "downstream steps must still be recorded"

    # In our test, steps 4–9 get stubs so they should be ok
    for s in step_results:
        if s["step"] in later_names:
            assert s["status"] == "ok", f"Downstream step '{s['step']}' should be ok"

    # Overall must still be done
    assert result.get("pct") == 100
    assert result.get("running") is False


# ─────────────────────────────────────────────────────────────────────────────
# Test 3 — Already running guard
# ─────────────────────────────────────────────────────────────────────────────

def test_already_running_guard():
    """If running=True, run_unified_od_sync returns immediately with status=already_running."""
    _reset_progress()

    import unified_od_sync as uos
    # Directly set running to True (simulate an in-progress chain)
    uos._unified_sync_progress["running"] = True

    stubs = _make_all_stubs()
    # None of the underlying functions should be called
    with patch.dict("sys.modules", {
        "firestore_sync":           _module_stub("firestore_sync",   stubs),
        "google_ads_sync":          _module_stub("google_ads_sync",  stubs),
        "od_matcher":               _module_stub("od_matcher",       stubs),
        "od_payment_sync":          _module_stub("od_payment_sync",  stubs),
        "call_keyword_attribution": _module_stub("call_keyword_attribution", stubs),
        "call_production_log":      _module_stub("call_production_log",      stubs),
        "google_ads_conversions":   _module_stub("google_ads_conversions",   stubs),
        "database":                 _db_stub(),
    }):
        result = uos.run_unified_od_sync(trigger="test")

    assert result.get("status") == "already_running", "must return already_running status"

    # No underlying sync should have been called
    for key, mock_fn in stubs.items():
        assert not mock_fn.called, f"{key} should NOT have been called"

    # Restore running=False so other tests don't break
    uos._unified_sync_progress["running"] = False


# ─────────────────────────────────────────────────────────────────────────────
# Test 4 — Last-run persistence in settings table
# ─────────────────────────────────────────────────────────────────────────────

def test_last_run_persistence():
    """After a successful run, save_setting is called with pct=100 in the JSON blob."""
    _reset_progress()

    stubs = _make_all_stubs()

    # Track what save_setting is called with
    saved_values = {}

    db_stub = _db_stub(saved_values=saved_values)

    with patch.dict("sys.modules", {
        "firestore_sync":           _module_stub("firestore_sync",   stubs),
        "google_ads_sync":          _module_stub("google_ads_sync",  stubs),
        "od_matcher":               _module_stub("od_matcher",       stubs),
        "od_payment_sync":          _module_stub("od_payment_sync",  stubs),
        "call_keyword_attribution": _module_stub("call_keyword_attribution", stubs),
        "call_production_log":      _module_stub("call_production_log",      stubs),
        "google_ads_conversions":   _module_stub("google_ads_conversions",   stubs),
        "database":                 db_stub,
    }):
        import unified_od_sync as uos
        uos.run_unified_od_sync(trigger="test")

    assert "unified_od_sync_last_run" in saved_values, \
        "save_setting must be called with key 'unified_od_sync_last_run'"

    persisted = json.loads(saved_values["unified_od_sync_last_run"])
    assert persisted.get("pct") == 100,         "persisted blob must have pct=100"
    assert persisted.get("running") is False,   "persisted blob must have running=False"


# ─────────────────────────────────────────────────────────────────────────────
# Test 5 — Endpoint smoke test via FastAPI TestClient
# ─────────────────────────────────────────────────────────────────────────────

def test_endpoint_smoke():
    """
    POST /api/admin/sync-od-all → {"status": "started"}.
    Within 3 seconds, GET /api/admin/sync-od-all/progress shows pct=100.
    Uses FastAPI TestClient with all sync functions monkeypatched.
    """
    _reset_progress()

    stubs = _make_all_stubs()

    # We need to patch the modules at the sys.modules level before importing main,
    # because main.py does deferred imports inside the endpoint handler.
    # We also need a lightweight config stub so main.py doesn't fail on import.
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        pytest.skip("httpx not installed — skipping endpoint smoke test")

    # Patch all sync modules and config/database so main.py can be imported in test
    config_stub = _config_stub()
    db_stub = _db_stub()

    sync_patches = {
        "firestore_sync":           _module_stub("firestore_sync",   stubs),
        "google_ads_sync":          _module_stub("google_ads_sync",  stubs),
        "od_matcher":               _module_stub("od_matcher",       stubs),
        "od_payment_sync":          _module_stub("od_payment_sync",  stubs),
        "call_keyword_attribution": _module_stub("call_keyword_attribution", stubs),
        "call_production_log":      _module_stub("call_production_log",      stubs),
        "google_ads_conversions":   _module_stub("google_ads_conversions",   stubs),
    }

    # Import unified_od_sync directly (already on sys.path) and patch its imports
    with patch.dict("sys.modules", sync_patches):
        import unified_od_sync as uos
        # Re-reset in case a previous test left progress running
        _reset_progress()

        # Create a minimal FastAPI app that only has the 3 new endpoints
        # (avoids the complexity of importing all of main.py in tests)
        from fastapi import FastAPI, Depends
        mini_app = FastAPI()

        # Minimal admin dep that always passes
        def _noop_admin():
            pass

        @mini_app.post("/api/admin/sync-od-all")
        def _start():
            import threading as _t
            progress = uos.get_unified_sync_progress()
            if progress.get("running"):
                return {"status": "already_running", "progress": progress}
            def _run():
                try:
                    uos.run_unified_od_sync(trigger="test")
                except Exception:
                    pass
            _t.Thread(target=_run, daemon=True).start()
            return {"status": "started"}

        @mini_app.get("/api/admin/sync-od-all/progress")
        def _progress():
            return uos.get_unified_sync_progress()

        client = TestClient(mini_app, raise_server_exceptions=True)

        # POST to kick off
        resp = client.post("/api/admin/sync-od-all")
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("status") == "started", f"Expected 'started', got: {body}"

        # Poll until done or timeout (max 3 seconds)
        deadline = time.time() + 3.0
        final_progress = None
        while time.time() < deadline:
            p_resp = client.get("/api/admin/sync-od-all/progress")
            assert p_resp.status_code == 200
            p = p_resp.json()
            if not p.get("running"):
                final_progress = p
                break
            time.sleep(0.1)

        assert final_progress is not None, "Chain did not complete within 3 seconds"
        assert final_progress.get("pct") == 100, \
            f"Expected pct=100, got pct={final_progress.get('pct')}"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers: build module stubs without importing the real modules
# ─────────────────────────────────────────────────────────────────────────────

def _module_stub(module_name: str, stubs: dict):
    """
    Build a MagicMock module whose attributes are pulled from the stubs dict
    by matching '<module_name>.<attr>' keys.
    """
    mod = MagicMock()
    prefix = module_name + "."
    for full_key, mock_fn in stubs.items():
        if full_key.startswith(prefix):
            attr = full_key[len(prefix):]
            setattr(mod, attr, mock_fn)
    return mod


def _db_stub(saved_values: dict = None):
    """Build a minimal database module stub that captures save_setting calls."""
    mod = MagicMock()
    _store = saved_values if saved_values is not None else {}

    def _save(key, value):
        _store[key] = value

    def _get(key, default=""):
        return _store.get(key, default)

    mod.save_setting = MagicMock(side_effect=_save)
    mod.get_setting  = MagicMock(side_effect=_get)
    return mod


def _config_stub():
    """Minimal config stub so imports don't fail."""
    mod = MagicMock()
    settings = MagicMock()
    settings.mango_enabled = False
    settings.secret_admin_password = "testpass"
    mod.get_settings = MagicMock(return_value=settings)
    return mod
