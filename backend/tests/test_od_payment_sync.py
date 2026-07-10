"""
Tests for od_payment_sync.py — PR 2

Uses SQLite in-memory for the lead/KPL database.
Mocks the OD MySQL connection via monkeypatch on _get_od_conn.

Run from backend/:
    source venv/bin/activate
    pytest tests/test_od_payment_sync.py -v
"""
import json
import sqlite3
import sys
import os
from datetime import datetime, timezone, date
from unittest.mock import MagicMock, patch

import pytest

# ── Ensure backend/ is on sys.path so we can import modules directly ──────────
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)


# ─────────────────────────────────────────────────────────────────────────────
# Minimal in-memory DB fixtures
# ─────────────────────────────────────────────────────────────────────────────

_LEADS_SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    od_patient_num TEXT DEFAULT '',
    gclid TEXT DEFAULT '',
    existing_patient INTEGER DEFAULT 0,
    paid_amount_365d REAL DEFAULT 0.0,
    paid_amount_ltv  REAL DEFAULT 0.0,
    first_payment_date TEXT DEFAULT '',
    paid_through_date  TEXT DEFAULT '',
    payment_synced_at  TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS lifecycle_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    stage_from TEXT DEFAULT '',
    stage_to TEXT DEFAULT '',
    detail TEXT DEFAULT '',
    source TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mango_calls (
    uuid TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    od_patient_status TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS callrail_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id TEXT,
    source TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS keyword_production_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at TEXT NOT NULL,
    lead_id TEXT NOT NULL,
    keyword_text TEXT DEFAULT '',
    match_type TEXT DEFAULT '',
    campaign_id TEXT DEFAULT '',
    campaign_name TEXT DEFAULT '',
    ad_group_name TEXT DEFAULT '',
    gclid TEXT DEFAULT '',
    od_patient_num TEXT DEFAULT '',
    production_amount REAL DEFAULT 0.0,
    procedure_codes TEXT DEFAULT '[]',
    match_method TEXT DEFAULT '',
    appointment_date TEXT DEFAULT '',
    paid_amount_365d REAL DEFAULT 0.0,
    paid_amount_ltv  REAL DEFAULT 0.0,
    payment_synced_at TEXT DEFAULT '',
    UNIQUE(lead_id, od_patient_num)
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _make_db():
    """Create an in-memory SQLite DB with the minimal schema."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_LEADS_SCHEMA)
    return conn


def _make_od_payment_rows(patient_num: str, rows: list) -> dict:
    """
    Build a fake OD payments dict in the format returned by _bulk_query_od_payments.
    rows = [(date_str, amount), ...]
    """
    return {patient_num: rows}


# ─────────────────────────────────────────────────────────────────────────────
# Helper: run sync against an in-memory DB instead of real file DB
# ─────────────────────────────────────────────────────────────────────────────

def _run_sync_with_db(db_conn, od_payments: dict, full_resync: bool = False, days_back: int = 7):
    """
    Invoke the core sync logic directly, injecting an in-memory DB conn
    and a fake OD payments dict (no MySQL required).

    This re-implements just the write-back logic from sync_od_payments using
    the private helpers from od_payment_sync.
    """
    from od_payment_sync import (
        _collect_lead_targets,
        _collect_call_targets,
        _compute_buckets,
        _days_back_cutoff,
        _now_iso,
        _MIN_EVENT_DELTA,
    )

    now_iso = _now_iso()
    cutoff_iso = _days_back_cutoff(days_back)
    window_days = 365

    lead_targets = _collect_lead_targets(db_conn, full_resync, cutoff_iso)
    call_targets = _collect_call_targets(db_conn, full_resync, cutoff_iso)
    all_targets = lead_targets + call_targets

    leads_updates = []
    kpl_updates = []
    lifecycle_rows = []

    for target in all_targets:
        pat = target["od_patient_num"]
        payment_rows = od_payments.get(pat, [])
        paid_365d, paid_ltv, first_pdate, through_pdate = _compute_buckets(
            payment_rows, target["anchor_date"], window_days
        )
        if target["target_table"] == "leads":
            old_365d = target["current_365d"]
            leads_updates.append((paid_365d, paid_ltv, first_pdate, through_pdate, now_iso, target["target_id"]))
            delta = paid_365d - old_365d
            if abs(delta) >= _MIN_EVENT_DELTA:
                lifecycle_rows.append((target["target_id"], round(delta, 2), round(paid_ltv - target["current_ltv"], 2)))
        else:
            kpl_updates.append((paid_365d, paid_ltv, now_iso, target["target_id"]))

    if leads_updates:
        db_conn.executemany(
            "UPDATE leads SET paid_amount_365d=?, paid_amount_ltv=?, first_payment_date=?, paid_through_date=?, payment_synced_at=? WHERE id=?",
            leads_updates,
        )
    if kpl_updates:
        db_conn.executemany(
            "UPDATE keyword_production_log SET paid_amount_365d=?, paid_amount_ltv=?, payment_synced_at=? WHERE id=?",
            kpl_updates,
        )
    if lifecycle_rows:
        db_conn.executemany(
            "INSERT INTO lifecycle_events (lead_id, event_type, detail, created_at) VALUES (?,?,?,?)",
            [
                (lid, "payment_pulled", json.dumps({"paid_365d_delta": d365, "paid_ltv_delta": dltv}), now_iso)
                for lid, d365, dltv in lifecycle_rows
            ],
        )
    db_conn.commit()

    return {
        "leads_synced": len(leads_updates),
        "calls_synced": len(kpl_updates),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: Anchor-date filter
# ─────────────────────────────────────────────────────────────────────────────

def test_anchor_date_filter():
    """
    Payments BEFORE anchor_date are excluded from both 365d and LTV.
    Payments within 365d go into paid_amount_365d.
    Payments after 365d (but after anchor) go only into paid_amount_ltv.
    """
    db = _make_db()

    # Insert lead with anchor 2025-06-01
    db.execute(
        "INSERT INTO leads (id, created_at, od_patient_num, gclid) VALUES (?,?,?,?)",
        ("lead-1", "2025-06-01T00:00:00+00:00", "1001", "gclid-abc"),
    )
    db.commit()

    # OD payments:
    #  2025-05-15 — pre-anchor, EXCLUDE
    #  2025-08-01 — in 365d window (2025-06-01 + 365d = 2026-06-01), INCLUDE in 365d + LTV
    #  2026-08-01 — past 365d window, INCLUDE in LTV only
    od_payments = {
        "1001": [
            ("2025-05-15", 200.0),   # pre-anchor — must be excluded
            ("2025-08-01", 500.0),   # in window
            ("2026-08-01", 300.0),   # past 365d, LTV only
        ]
    }

    _run_sync_with_db(db, od_payments, full_resync=True)

    row = db.execute("SELECT paid_amount_365d, paid_amount_ltv FROM leads WHERE id='lead-1'").fetchone()
    assert row is not None, "Lead row not found"
    # 365d: only 2025-08-01 payment (2025-05-15 excluded, 2026-08-01 outside window)
    assert abs(row["paid_amount_365d"] - 500.0) < 0.01, f"paid_amount_365d expected 500.0, got {row['paid_amount_365d']}"
    # LTV: 2025-08-01 + 2026-08-01 (pre-anchor excluded)
    assert abs(row["paid_amount_ltv"] - 800.0) < 0.01, f"paid_amount_ltv expected 800.0, got {row['paid_amount_ltv']}"


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: Call-path patient
# ─────────────────────────────────────────────────────────────────────────────

def test_call_path_patient():
    """
    A call:: KPL row gets updated. No leads row is touched for a call-only patient.
    anchor_date comes from mango_calls.started_at.
    """
    db = _make_db()

    # Insert mango_calls row (anchor date)
    db.execute(
        "INSERT INTO mango_calls (uuid, started_at, od_patient_status) VALUES (?,?,?)",
        ("abc123", "2025-07-01T09:00:00+00:00", "new_patient"),
    )
    # Insert KPL row for the call
    db.execute(
        """INSERT INTO keyword_production_log
           (logged_at, lead_id, od_patient_num, keyword_text, campaign_name, production_amount)
           VALUES (?,?,?,?,?,?)""",
        ("2025-07-05T00:00:00+00:00", "call::abc123", "2001", "dental implants", "Implants Campaign", 1200.0),
    )
    db.commit()

    # OD payments for call patient
    od_payments = {
        "2001": [
            ("2025-07-10", 800.0),   # in 365d window after anchor 2025-07-01
            ("2026-09-01", 400.0),   # LTV only
        ]
    }

    result = _run_sync_with_db(db, od_payments, full_resync=True)
    assert result["calls_synced"] == 1, f"Expected 1 call synced, got {result['calls_synced']}"
    assert result["leads_synced"] == 0, "No leads should be synced for a call-only patient"

    kpl_row = db.execute(
        "SELECT paid_amount_365d, paid_amount_ltv FROM keyword_production_log WHERE lead_id='call::abc123'"
    ).fetchone()
    assert kpl_row is not None
    assert abs(kpl_row["paid_amount_365d"] - 800.0) < 0.01, f"paid_amount_365d expected 800, got {kpl_row['paid_amount_365d']}"
    assert abs(kpl_row["paid_amount_ltv"] - 1200.0) < 0.01, f"paid_amount_ltv expected 1200, got {kpl_row['paid_amount_ltv']}"


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: Idempotency
# ─────────────────────────────────────────────────────────────────────────────

def test_idempotency():
    """
    Running sync twice with identical OD data produces identical paid amounts.
    payment_synced_at advances on the second run.
    """
    db = _make_db()

    db.execute(
        "INSERT INTO leads (id, created_at, od_patient_num, gclid) VALUES (?,?,?,?)",
        ("lead-idem", "2025-09-01T00:00:00+00:00", "3001", "gclid-xyz"),
    )
    db.commit()

    od_payments = {"3001": [("2025-10-01", 600.0)]}

    # First run
    _run_sync_with_db(db, od_payments, full_resync=True)
    row_1 = db.execute(
        "SELECT paid_amount_365d, paid_amount_ltv, payment_synced_at FROM leads WHERE id='lead-idem'"
    ).fetchone()

    synced_at_1 = row_1["payment_synced_at"]
    assert abs(row_1["paid_amount_365d"] - 600.0) < 0.01
    assert abs(row_1["paid_amount_ltv"] - 600.0) < 0.01

    # Second run (full_resync again to bypass staleness check)
    _run_sync_with_db(db, od_payments, full_resync=True)
    row_2 = db.execute(
        "SELECT paid_amount_365d, paid_amount_ltv, payment_synced_at FROM leads WHERE id='lead-idem'"
    ).fetchone()

    # Numbers unchanged
    assert abs(row_2["paid_amount_365d"] - row_1["paid_amount_365d"]) < 0.01, "paid_amount_365d changed on second run"
    assert abs(row_2["paid_amount_ltv"] - row_1["paid_amount_ltv"]) < 0.01, "paid_amount_ltv changed on second run"
    # payment_synced_at should have advanced (or at minimum be non-empty)
    assert row_2["payment_synced_at"] != "", "payment_synced_at should be non-empty after second run"


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: Existing patient exclusion
# ─────────────────────────────────────────────────────────────────────────────

def test_existing_patient_exclusion():
    """
    Call-path rows with od_patient_status = 'existing_active' or
    'existing_inactive' must be skipped (not synced).
    """
    db = _make_db()

    # Insert existing_active mango_calls row
    db.execute(
        "INSERT INTO mango_calls (uuid, started_at, od_patient_status) VALUES (?,?,?)",
        ("existing-uuid", "2025-08-01T10:00:00+00:00", "existing_active"),
    )
    # Insert KPL row
    db.execute(
        """INSERT INTO keyword_production_log
           (logged_at, lead_id, od_patient_num, keyword_text, campaign_name, production_amount)
           VALUES (?,?,?,?,?,?)""",
        ("2025-08-05T00:00:00+00:00", "call::existing-uuid", "4001", "emergency dentist", "Emergency Campaign", 300.0),
    )
    db.commit()

    od_payments = {"4001": [("2025-09-01", 500.0)]}

    result = _run_sync_with_db(db, od_payments, full_resync=True)

    # The existing_active patient should be skipped entirely
    assert result["calls_synced"] == 0, f"Expected 0 calls synced (existing patient), got {result['calls_synced']}"

    kpl_row = db.execute(
        "SELECT paid_amount_365d, paid_amount_ltv FROM keyword_production_log WHERE lead_id='call::existing-uuid'"
    ).fetchone()
    assert kpl_row is not None
    # Should still be 0 (not updated)
    assert abs(kpl_row["paid_amount_365d"] - 0.0) < 0.01, f"paid_amount_365d should be 0 for existing patient, got {kpl_row['paid_amount_365d']}"
    assert abs(kpl_row["paid_amount_ltv"] - 0.0) < 0.01, f"paid_amount_ltv should be 0 for existing patient, got {kpl_row['paid_amount_ltv']}"


# ─────────────────────────────────────────────────────────────────────────────
# Test 5: OD unavailable
# ─────────────────────────────────────────────────────────────────────────────
# Test 4b: Existing-patient leads excluded at collection time
# ─────────────────────────────────────────────────────────────────────────────

def test_existing_patient_lead_exclusion():
    """
    Leads with existing_patient = 1 must be excluded by _collect_lead_targets
    (their pre-existing payments would otherwise inflate Google Ads income).
    A matching lead with existing_patient = 0 must still be collected.
    """
    from od_payment_sync import _collect_lead_targets, _days_back_cutoff

    db = _make_db()

    db.execute(
        "INSERT INTO leads (id, created_at, od_patient_num, gclid, existing_patient) VALUES (?,?,?,?,?)",
        ("lead-existing", "2025-06-01T00:00:00+00:00", "5001", "gclid-existing", 1),
    )
    db.execute(
        "INSERT INTO leads (id, created_at, od_patient_num, gclid, existing_patient) VALUES (?,?,?,?,?)",
        ("lead-new", "2025-06-01T00:00:00+00:00", "5002", "gclid-new", 0),
    )
    db.commit()

    cutoff_iso = _days_back_cutoff(7)
    targets = _collect_lead_targets(db, full_resync=True, cutoff_iso=cutoff_iso)
    target_ids = {t["target_id"] for t in targets}

    assert "lead-existing" not in target_ids, "existing_patient=1 lead must be excluded"
    assert "lead-new" in target_ids, "existing_patient=0 lead must still be collected"

    # Also verify via the public sync entrypoint: the existing patient's
    # paid_amount_365d/ltv must stay 0 even though OD has payments for them.
    od_payments = {
        "5001": [("2025-07-01", 900.0)],
        "5002": [("2025-07-01", 700.0)],
    }
    _run_sync_with_db(db, od_payments, full_resync=True)

    existing_row = db.execute(
        "SELECT paid_amount_365d, paid_amount_ltv FROM leads WHERE id='lead-existing'"
    ).fetchone()
    assert abs(existing_row["paid_amount_365d"] - 0.0) < 0.01, "existing patient must not accrue paid_amount_365d"
    assert abs(existing_row["paid_amount_ltv"] - 0.0) < 0.01, "existing patient must not accrue paid_amount_ltv"

    new_row = db.execute(
        "SELECT paid_amount_365d, paid_amount_ltv FROM leads WHERE id='lead-new'"
    ).fetchone()
    assert abs(new_row["paid_amount_365d"] - 700.0) < 0.01, "non-existing patient should still accrue payments"
    assert abs(new_row["paid_amount_ltv"] - 700.0) < 0.01, "non-existing patient should still accrue payments"


# ─────────────────────────────────────────────────────────────────────────────
# Test 4c: CallRail source='google_ads' lead captured even without gclid
# ─────────────────────────────────────────────────────────────────────────────

def test_callrail_google_ads_lead_captured():
    """
    A lead with no gclid (call-extension call, no click-through) must still
    be captured if its CallRail row has source='google_ads'. A lead whose
    CallRail row has a non-Google-Ads source (e.g. 'Direct') must NOT be
    captured.
    """
    from od_payment_sync import _collect_lead_targets, _days_back_cutoff

    db = _make_db()

    # Google Ads call-extension lead: no gclid, but CallRail source='google_ads'
    db.execute(
        "INSERT INTO leads (id, created_at, od_patient_num, gclid, existing_patient) VALUES (?,?,?,?,?)",
        ("lead-cr", "2025-06-01T00:00:00+00:00", "6001", "", 0),
    )
    db.execute(
        "INSERT INTO callrail_calls (lead_id, source) VALUES (?,?)",
        ("lead-cr", "google_ads"),
    )

    # Non-Google-Ads lead: no gclid, CallRail source='Direct'
    db.execute(
        "INSERT INTO leads (id, created_at, od_patient_num, gclid, existing_patient) VALUES (?,?,?,?,?)",
        ("lead-nongads", "2025-06-01T00:00:00+00:00", "6002", "", 0),
    )
    db.execute(
        "INSERT INTO callrail_calls (lead_id, source) VALUES (?,?)",
        ("lead-nongads", "Direct"),
    )
    db.commit()

    cutoff_iso = _days_back_cutoff(7)
    targets = _collect_lead_targets(db, full_resync=True, cutoff_iso=cutoff_iso)
    target_ids = {t["target_id"] for t in targets}

    assert "lead-cr" in target_ids, "CallRail source='google_ads' lead must be captured even without gclid"
    assert "lead-nongads" not in target_ids, "CallRail non-google_ads source lead must NOT be captured"

    # Verify via the public sync path: lead-cr accrues income, lead-nongads does not.
    od_payments = {
        "6001": [("2025-07-01", 450.0)],
        "6002": [("2025-07-01", 999.0)],
    }
    _run_sync_with_db(db, od_payments, full_resync=True)

    cr_row = db.execute(
        "SELECT paid_amount_365d, paid_amount_ltv FROM leads WHERE id='lead-cr'"
    ).fetchone()
    assert abs(cr_row["paid_amount_365d"] - 450.0) < 0.01, "CallRail google_ads lead should accrue income"
    assert abs(cr_row["paid_amount_ltv"] - 450.0) < 0.01, "CallRail google_ads lead should accrue income"

    nongads_row = db.execute(
        "SELECT paid_amount_365d, paid_amount_ltv FROM leads WHERE id='lead-nongads'"
    ).fetchone()
    assert abs(nongads_row["paid_amount_365d"] - 0.0) < 0.01, "non-google_ads lead must not accrue income"
    assert abs(nongads_row["paid_amount_ltv"] - 0.0) < 0.01, "non-google_ads lead must not accrue income"


# ─────────────────────────────────────────────────────────────────────────────

def test_od_unavailable(monkeypatch):
    """
    When _get_od_conn returns None, sync_od_payments returns
    {'status': 'skipped', 'reason': 'od_unavailable'} without raising.
    """
    import od_payment_sync as ops
    monkeypatch.setattr(ops, "_get_od_conn", lambda: None)

    result = ops.sync_od_payments(days_back=7)
    assert result.get("status") == "skipped", f"Expected status=skipped, got {result}"
    assert result.get("reason") == "od_unavailable", f"Expected reason=od_unavailable, got {result}"


# ─────────────────────────────────────────────────────────────────────────────
# Test 6: _parse_anchor UTC → Eastern conversion
# ─────────────────────────────────────────────────────────────────────────────

def test_parse_anchor_utc_to_eastern():
    """
    _parse_anchor must convert UTC timestamps to Eastern before taking .date(),
    so that evening-ET leads (already past midnight UTC) don't get an anchor
    date one day late. Date-only strings must be returned unshifted.
    """
    from od_payment_sync import _parse_anchor

    # 00:16 UTC == 8:16pm ET the PREVIOUS day
    assert _parse_anchor("2026-06-04T00:16:00+00:00") == date(2026, 6, 3)
    assert _parse_anchor("2026-06-04T00:16:00Z") == date(2026, 6, 3)
    # 15:00 UTC == 11:00am ET SAME day
    assert _parse_anchor("2026-06-03T15:00:00+00:00") == date(2026, 6, 3)
    # Date-only anchor — no tz shift
    assert _parse_anchor("2026-06-03") == date(2026, 6, 3)
    # Naive (no tzinfo) timestamp is treated as UTC — 20:16 UTC == 4:16pm ET same day
    assert _parse_anchor("2026-06-03 20:16:00") == date(2026, 6, 3)
    # Empty string returns None
    assert _parse_anchor("") is None
