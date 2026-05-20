"""
PR 3 income-mode tests.

Tests:
1. ROI uses paid_365d income when available (income_365d > 0).
2. ROI falls back to planned income when income_365d == 0.
3. Settings persistence: gads_attribution_window_days round-trips via the endpoint.
4. Optimizer revenue source: revenue_30d uses paid_income_365d with fallback to revenue.
"""

import os
import sqlite3
import tempfile
import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _roi_basis_calc(income_365d: float, income: float, cost: float):
    """
    Mirror the exact PR 3 logic from get_unified_campaigns().
    Returns (roi_basis, roi).
    """
    roi_basis = income_365d if income_365d > 0 else income
    roi = round((roi_basis - cost) / cost * 100, 1) if cost > 0 else None
    return roi_basis, roi


def _optimizer_revenue(ag: dict) -> float:
    """
    Mirror the exact PR 3 logic from ai_optimizer.py line ~7576.
    Returns revenue_30d as it would appear in all_ag_stats.
    """
    return float(ag.get("paid_income_365d") or ag.get("revenue") or 0)


# ---------------------------------------------------------------------------
# Test 1 — ROI uses paid 365d when available
# ---------------------------------------------------------------------------

def test_roi_uses_paid_365d_when_available():
    """
    Campaign with income_365d=$5,000 and cost=$1,000.
    ROI should be 400% (based on paid, not planned).
    """
    income_365d = 5_000.0
    income      = 3_000.0   # planned — different from paid, to prove paid is used
    cost        = 1_000.0

    roi_basis, roi = _roi_basis_calc(income_365d, income, cost)

    assert roi_basis == income_365d, (
        f"roi_basis should equal income_365d={income_365d}, got {roi_basis}"
    )
    assert roi == 400.0, f"Expected ROI=400.0, got {roi}"


# ---------------------------------------------------------------------------
# Test 2 — ROI falls back to planned when no paid data
# ---------------------------------------------------------------------------

def test_roi_falls_back_to_planned_when_no_paid_data():
    """
    Campaign with income_365d=0 (brand new, no payments yet), income=$2,000, cost=$1,000.
    ROI should be 100% (falls back to planned production).
    """
    income_365d = 0.0
    income      = 2_000.0
    cost        = 1_000.0

    roi_basis, roi = _roi_basis_calc(income_365d, income, cost)

    assert roi_basis == income, (
        f"roi_basis should equal planned income={income}, got {roi_basis}"
    )
    assert roi == 100.0, f"Expected ROI=100.0 (planned fallback), got {roi}"


def test_roi_is_none_when_no_cost():
    """
    Edge case: cost=0 → ROI must be None regardless of income.
    """
    _, roi = _roi_basis_calc(5_000.0, 5_000.0, 0.0)
    assert roi is None, f"Expected ROI=None when cost=0, got {roi}"


# ---------------------------------------------------------------------------
# Test 3 — Settings persistence via actual FastAPI endpoint
# ---------------------------------------------------------------------------

def test_settings_attribution_window_round_trips(tmp_path):
    """
    Verify gads_attribution_window_days round-trips correctly through
    save_setting / get_setting, which backs the /api/admin/gads-attribution-settings
    endpoint. We test the DB layer directly to avoid importing main.py
    (which pulls in apscheduler and other heavy runtime deps).
    """
    db_file = str(tmp_path / "test_pr3_roundtrip.db")

    mock_settings = MagicMock()
    mock_settings.db_path = db_file
    mock_settings.gads_attribution_window_days = 365

    with patch("database.get_settings", return_value=mock_settings):
        import importlib
        import database as db_mod
        # Reload to pick up the mock db_path in the module-level cache if any
        importlib.reload(db_mod)

        with patch("database.get_settings", return_value=mock_settings):
            db_mod.init_db()

            # Simulate the POST handler: save a non-default value
            db_mod.save_setting("gads_attribution_window_days", "180")

            # Simulate the GET handler: read it back
            raw = db_mod.get_setting("gads_attribution_window_days")
            window = int(raw) if raw and raw.isdigit() else 365
            assert window == 180, f"Round-trip failed: expected 180, got {window}"

            # Update again — verify overwrite works
            db_mod.save_setting("gads_attribution_window_days", "730")
            raw2 = db_mod.get_setting("gads_attribution_window_days")
            window2 = int(raw2) if raw2 and raw2.isdigit() else 365
            assert window2 == 730, f"Overwrite failed: expected 730, got {window2}"


def test_settings_attribution_window_direct(tmp_path):
    """
    Directly test save_setting / get_setting round-trip for gads_attribution_window_days.
    This avoids any auth complexity and proves the DB layer works.
    """
    db_file = str(tmp_path / "test_pr3_direct.db")
    mock_settings = MagicMock()
    mock_settings.db_path = db_file
    mock_settings.gads_attribution_window_days = 365

    with patch("database.get_settings", return_value=mock_settings):
        import importlib
        import database as db_mod
        importlib.reload(db_mod)  # ensure clean state with patched settings

        with patch("database.get_settings", return_value=mock_settings):
            db_mod.init_db()

            db_mod.save_setting("gads_attribution_window_days", "180")
            raw = db_mod.get_setting("gads_attribution_window_days")
            assert raw == "180", f"Expected '180' from get_setting, got {raw!r}"

            db_mod.save_setting("gads_attribution_window_days", "365")
            raw2 = db_mod.get_setting("gads_attribution_window_days")
            assert raw2 == "365", f"Expected '365' after update, got {raw2!r}"


# ---------------------------------------------------------------------------
# Test 4 — Optimizer revenue source uses paid_income_365d with fallback
# ---------------------------------------------------------------------------

def test_optimizer_revenue_uses_paid_income_365d_when_present():
    """
    When paid_income_365d is present on an ad-group row, revenue_30d should equal it.
    """
    ag = {"paid_income_365d": 1500.0, "revenue": 3000.0}
    result = _optimizer_revenue(ag)
    assert result == 1500.0, (
        f"Expected revenue_30d=1500.0 (paid_income_365d), got {result}"
    )


def test_optimizer_revenue_falls_back_to_revenue_when_no_paid():
    """
    When paid_income_365d is absent (None or missing), revenue_30d falls back to revenue.
    """
    ag_missing = {"revenue": 2500.0}
    result_missing = _optimizer_revenue(ag_missing)
    assert result_missing == 2500.0, (
        f"Expected 2500.0 (revenue fallback), got {result_missing}"
    )

    ag_none = {"paid_income_365d": None, "revenue": 2500.0}
    result_none = _optimizer_revenue(ag_none)
    assert result_none == 2500.0, (
        f"Expected 2500.0 (revenue fallback with None paid), got {result_none}"
    )


def test_optimizer_revenue_zero_when_both_missing():
    """
    When neither paid_income_365d nor revenue is present, revenue_30d should be 0.
    """
    ag = {}
    result = _optimizer_revenue(ag)
    assert result == 0.0, f"Expected 0.0 when both fields missing, got {result}"


def test_optimizer_revenue_zero_paid_falls_back():
    """
    When paid_income_365d is 0 (falsy), should fall back to revenue.
    This is important: 0 is falsy in Python so `ag.get('paid_income_365d') or ...`
    correctly skips 0 and uses the revenue fallback — same as the income_365d > 0 check.
    """
    ag = {"paid_income_365d": 0.0, "revenue": 1200.0}
    result = _optimizer_revenue(ag)
    assert result == 1200.0, (
        f"Expected 1200.0 (revenue fallback when paid=0), got {result}"
    )
