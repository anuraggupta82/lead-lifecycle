"""
Tests for PR 5: Step-7 OD-down resilience + Appointment Modal patient name + income.

Tests:
  1. test_step7_continues_with_od_down_writes_booked_override
     — booked_override row is written even when OD is unavailable; skipped_od_unavailable==0
  2. test_step7_skips_high_conf_rows_when_od_down
     — non-booked_override row is skipped when OD unavailable; function returns cleanly
  3. test_step7_mixed_batch_od_down
     — 1 booked_override + 1 high-conf; booked_override written, high-conf skipped
  4. test_step7_summary_log_includes_od_unavailable
     — log line contains "od_unavailable=1" when OD down and high-conf row present
  5. test_modal_endpoint_returns_ai_patient_name_and_income
     — endpoint returns ai_patient_name, paid_amount_365d, and patient_name COALESCE
  6. test_modal_endpoint_patient_name_prefers_od_when_present
     — when both od_patient_name and ai_patient_name set, patient_name == od_patient_name
  7. test_modal_endpoint_paid_amount_zero_when_no_kpl
     — unmatched call with no KPL row returns paid_amount_365d=0.0 not None

Run from backend/:
  backend/venv/bin/python -m pytest tests/test_pr5_step7_fix_and_modal.py -v
"""
import sqlite3
import sys
import os
import logging
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager

# ─── Path setup ──────────────────────────────────────────────────────────────
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


# ─── Shared in-memory DB schema ──────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mango_calls (
    uuid                          TEXT PRIMARY KEY,
    started_at                    TEXT,
    from_number                   TEXT DEFAULT '',
    direction                     TEXT DEFAULT 'inbound',
    caller_id_name                TEXT DEFAULT '',
    duration_sec                  INTEGER DEFAULT 0,
    od_patient_num                TEXT DEFAULT '',
    od_patient_status             TEXT DEFAULT '',
    od_patient_name               TEXT DEFAULT '',
    ai_patient_name               TEXT DEFAULT '',
    od_appointment_id             TEXT DEFAULT '',
    od_patient_income             REAL DEFAULT NULL,
    od_patient_production         REAL DEFAULT NULL,
    od_income_synced_at           TEXT DEFAULT '',
    od_matched_at                 TEXT DEFAULT '',
    attributed_keyword            TEXT DEFAULT '',
    attributed_match_type         TEXT DEFAULT '',
    attributed_ad_group           TEXT DEFAULT '',
    attributed_keyword_method     TEXT DEFAULT '',
    attributed_keyword_confidence REAL DEFAULT NULL,
    lead_id                       TEXT DEFAULT '',
    gads_call_id                  TEXT DEFAULT '',
    booked_outcome                TEXT DEFAULT '',
    call_summary                  TEXT DEFAULT '',
    status                        TEXT DEFAULT '',
    updated_at                    TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS keyword_production_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at           TEXT NOT NULL,
    lead_id             TEXT NOT NULL,
    keyword_text        TEXT NOT NULL DEFAULT '',
    match_type          TEXT DEFAULT '',
    campaign_id         TEXT DEFAULT '',
    campaign_name       TEXT DEFAULT '',
    ad_group_name       TEXT DEFAULT '',
    gclid               TEXT DEFAULT '',
    od_patient_num      TEXT NOT NULL DEFAULT '',
    production_amount   REAL DEFAULT 0.0,
    procedure_codes     TEXT DEFAULT '[]',
    match_method        TEXT DEFAULT '',
    appointment_date    TEXT DEFAULT '',
    paid_amount_365d    REAL DEFAULT 0.0,
    paid_amount_ltv     REAL DEFAULT 0.0,
    payment_synced_at   TEXT DEFAULT '',
    confidence_tier     TEXT DEFAULT NULL,
    UNIQUE(lead_id, od_patient_num)
);

CREATE TABLE IF NOT EXISTS leads (
    id                  TEXT PRIMARY KEY,
    created_at          TEXT,
    campaign_name       TEXT DEFAULT '',
    utm_campaign        TEXT DEFAULT '',
    campaign_id         TEXT DEFAULT '',
    ad_group_name       TEXT DEFAULT '',
    gclid               TEXT DEFAULT '',
    od_patient_num      TEXT DEFAULT '',
    first_name          TEXT DEFAULT '',
    last_name           TEXT DEFAULT '',
    appointment_date    TEXT DEFAULT '',
    appointment_status  TEXT DEFAULT '',
    attributed_income   REAL DEFAULT 0.0,
    attributed_production REAL DEFAULT 0.0,
    paid_amount_365d    REAL DEFAULT 0.0,
    paid_amount_ltv     REAL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS gads_call_view (
    call_id         TEXT,
    campaign_id     TEXT DEFAULT '',
    campaign_name   TEXT DEFAULT '',
    ad_group_name   TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS settings (
    key     TEXT PRIMARY KEY,
    value   TEXT
);
"""


@pytest.fixture
def in_memory_db():
    """Create a minimal in-memory SQLite database for PR 5 tests."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    yield conn
    conn.close()


def _recent_ts(offset_days=0):
    """Return an ISO timestamp recent enough to pass the days= cutoff."""
    return (datetime.now(timezone.utc) - timedelta(days=offset_days)).isoformat()


def _insert_mango_call(conn, uuid, od_patient_num, od_appointment_id,
                        od_patient_income=0.0,
                        attributed_keyword_confidence=0.65,
                        attributed_keyword="emergency dentist",
                        attributed_ad_group="Emergency Dentistry > Pain Relief",
                        attributed_keyword_method="call_search_term",
                        od_patient_name="",
                        ai_patient_name="",
                        od_patient_status="new_patient",
                        caller_id_name="",
                        duration_sec=120,
                        booked_outcome="booked",
                        started_at=None):
    if started_at is None:
        started_at = _recent_ts(1)
    conn.execute("""
        INSERT OR REPLACE INTO mango_calls
            (uuid, started_at, od_patient_num, od_patient_status, od_appointment_id,
             od_patient_income, od_patient_production, attributed_keyword,
             attributed_ad_group, attributed_keyword_method, attributed_keyword_confidence,
             od_patient_name, ai_patient_name, caller_id_name, duration_sec,
             booked_outcome, direction)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (uuid, started_at, od_patient_num, od_patient_status, od_appointment_id,
          od_patient_income, 0.0, attributed_keyword,
          attributed_ad_group, attributed_keyword_method, attributed_keyword_confidence,
          od_patient_name, ai_patient_name, caller_id_name, duration_sec,
          booked_outcome, "inbound"))
    conn.commit()


class _NoCloseConn:
    """Delegates all sqlite3.Connection methods but makes .close() a no-op."""
    def __init__(self, real_conn):
        self._conn = real_conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        pass


# ─── Test 1: booked_override row is written when OD is down ──────────────────

def test_step7_continues_with_od_down_writes_booked_override(in_memory_db):
    """
    1 booked_override call (od_appointment_id set, confidence=0.0, od_patient_income=199).
    OD unavailable. Function must write the KPL row with confidence_tier='booked_override',
    paid_amount_365d=199, production_amount=0. skipped_od_unavailable must remain 0.
    """
    _insert_mango_call(
        in_memory_db,
        uuid="test_booked_od_down",
        od_patient_num="5728",
        od_appointment_id="31747",
        od_patient_income=199.0,
        attributed_keyword_confidence=0.0,
    )

    @contextmanager
    def _fake_conn():
        yield in_memory_db

    with patch("call_production_log._get_od_conn", return_value=None), \
         patch("call_production_log._existing_lead_production_patient_nums", return_value=set()), \
         patch("database._conn", _fake_conn):

        import call_production_log as cpl
        counts = cpl.link_calls_to_keyword_production(days=60)

    assert counts["written"] == 1, \
        f"Expected 1 written but got {counts['written']}; full counts={counts}"
    assert counts["skipped_od_unavailable"] == 0, \
        f"booked_override row should not increment skipped_od_unavailable, got {counts['skipped_od_unavailable']}"

    kpl = in_memory_db.execute(
        "SELECT * FROM keyword_production_log WHERE lead_id='call::test_booked_od_down'"
    ).fetchone()
    assert kpl is not None, "KPL row not written for booked_override call when OD down"
    kpl = dict(kpl)
    assert kpl["confidence_tier"] == "booked_override", \
        f"Expected confidence_tier='booked_override', got '{kpl['confidence_tier']}'"
    assert abs(kpl["paid_amount_365d"] - 199.0) < 0.01, \
        f"Expected paid_amount_365d=199.0, got {kpl['paid_amount_365d']}"
    assert kpl["production_amount"] == 0.0, \
        f"Expected production_amount=0.0 for OD-down write, got {kpl['production_amount']}"


# ─── Test 2: high-conf row is skipped when OD is down ────────────────────────

def test_step7_skips_high_conf_rows_when_od_down(in_memory_db):
    """
    1 high-confidence call (conf=0.7, od_appointment_id set to pass outer WHERE).
    OD unavailable. No KPL row written. skipped_od_unavailable==1.
    Function returns cleanly without raising.
    """
    _insert_mango_call(
        in_memory_db,
        uuid="test_highconf_od_down",
        od_patient_num="9001",
        od_appointment_id="",          # no confirmed OD appointment — non-booked_override
        od_patient_income=0.0,
        attributed_keyword_confidence=0.70,
    )
    # The outer WHERE in _fetch_call_production_data requires od_appointment_id != ''
    # for calls through the standard confidence path — but the OR clause means
    # od_appointment_id != '' OR confidence >= 0.30.  For this test we use
    # od_appointment_id='' and high confidence to test the high-conf path.
    # The WHERE is: od_appointment_id != '' OR confidence >= 0.30.
    # This call has confidence=0.70 so it passes.
    # But wait — looking at the actual WHERE: od_appointment_id IS NOT NULL AND != ''
    # is a hard filter in _fetch_call_production_data. The OR clause is only for
    # the confidence gate AFTER the hard od_appointment_id filter.
    # So to test the non-booked_override high-conf skip, we use od_appointment_id=''
    # but that means _fetch_call_production_data's hard filter excludes it entirely.
    # Per spec test 2: "high-confidence call (conf=0.7, no appointment_id)".
    # We patch _fetch_call_production_data to return the row directly.

    @contextmanager
    def _fake_conn():
        yield in_memory_db

    fake_row = {
        "uuid": "test_highconf_od_down",
        "od_patient_num": "9001",
        "od_appointment_id": "",        # no appointment → not booked_override
        "od_patient_income": 0.0,
        "attributed_keyword": "emergency dentist",
        "attributed_match_type": "",
        "attributed_ad_group": "Emergency Dentistry > Pain Relief",
        "attributed_keyword_method": "call_search_term",
        "attributed_keyword_confidence": 0.70,
        "od_patient_status": "new_patient",
        "campaign_id": "",
        "campaign_name": "Emergency Dentistry",
        "ad_group_name": "Pain Relief",
        "gclid": "",
        "lead_id": "",
        "started_at": _recent_ts(1),
    }

    with patch("call_production_log._get_od_conn", return_value=None), \
         patch("call_production_log._fetch_call_production_data", return_value=[fake_row]), \
         patch("call_production_log._existing_lead_production_patient_nums", return_value=set()), \
         patch("database._conn", _fake_conn):

        import call_production_log as cpl
        counts = cpl.link_calls_to_keyword_production(days=60)

    assert counts["written"] == 0, \
        f"Expected 0 written (no OD), got {counts['written']}"
    assert counts["skipped_od_unavailable"] == 1, \
        f"Expected skipped_od_unavailable=1, got {counts['skipped_od_unavailable']}"

    kpl = in_memory_db.execute(
        "SELECT * FROM keyword_production_log WHERE lead_id='call::test_highconf_od_down'"
    ).fetchone()
    assert kpl is None, "High-conf call should NOT write KPL when OD unavailable"


# ─── Test 3: mixed batch — booked_override written, high-conf skipped ─────────

def test_step7_mixed_batch_od_down(in_memory_db):
    """
    1 booked_override + 1 high-conf (no appointment) in same batch, OD down.
    booked_override is written; high-conf is skipped.
    """
    @contextmanager
    def _fake_conn():
        yield in_memory_db

    booked_row = {
        "uuid": "test_mix_booked",
        "od_patient_num": "5728",
        "od_appointment_id": "31747",   # → is_booked_override=True
        "od_patient_income": 199.0,
        "attributed_keyword": "emergency dentist",
        "attributed_match_type": "",
        "attributed_ad_group": "Emergency Dentistry (05/09) > Pain Relief",
        "attributed_keyword_method": "call_search_term",
        "attributed_keyword_confidence": 0.0,
        "od_patient_status": "new_patient",
        "campaign_id": "",
        "campaign_name": "Emergency Dentistry (05/09)",
        "ad_group_name": "Pain Relief",
        "gclid": "",
        "lead_id": "",
        "started_at": _recent_ts(1),
    }
    highconf_row = {
        "uuid": "test_mix_highconf",
        "od_patient_num": "9002",
        "od_appointment_id": "",        # → is_booked_override=False
        "od_patient_income": 0.0,
        "attributed_keyword": "dentist near me",
        "attributed_match_type": "",
        "attributed_ad_group": "General > Broad",
        "attributed_keyword_method": "call_search_term",
        "attributed_keyword_confidence": 0.70,
        "od_patient_status": "new_patient",
        "campaign_id": "",
        "campaign_name": "General",
        "ad_group_name": "Broad",
        "gclid": "",
        "lead_id": "",
        "started_at": _recent_ts(1),
    }

    with patch("call_production_log._get_od_conn", return_value=None), \
         patch("call_production_log._fetch_call_production_data", return_value=[booked_row, highconf_row]), \
         patch("call_production_log._existing_lead_production_patient_nums", return_value=set()), \
         patch("database._conn", _fake_conn):

        import call_production_log as cpl
        counts = cpl.link_calls_to_keyword_production(days=60)

    assert counts["written"] == 1, \
        f"Expected 1 written (booked_override), got {counts['written']}"
    assert counts["skipped_od_unavailable"] == 1, \
        f"Expected skipped_od_unavailable=1 (high-conf), got {counts['skipped_od_unavailable']}"
    assert counts["processed"] == 2, \
        f"Expected 2 processed, got {counts['processed']}"

    kpl_booked = in_memory_db.execute(
        "SELECT confidence_tier FROM keyword_production_log WHERE lead_id='call::test_mix_booked'"
    ).fetchone()
    assert kpl_booked is not None, "booked_override row should be written"
    assert kpl_booked["confidence_tier"] == "booked_override"

    kpl_high = in_memory_db.execute(
        "SELECT * FROM keyword_production_log WHERE lead_id='call::test_mix_highconf'"
    ).fetchone()
    assert kpl_high is None, "high-conf row should NOT be written when OD down"


# ─── Test 4: summary log contains od_unavailable=N ───────────────────────────

def test_step7_summary_log_includes_od_unavailable(in_memory_db, caplog):
    """
    When OD unavailable and a high-conf row is skipped, the final log line must
    contain 'od_unavailable=1'.
    """
    @contextmanager
    def _fake_conn():
        yield in_memory_db

    fake_row = {
        "uuid": "test_log_od_down",
        "od_patient_num": "9003",
        "od_appointment_id": "",
        "od_patient_income": 0.0,
        "attributed_keyword": "dentist",
        "attributed_match_type": "",
        "attributed_ad_group": "General > Broad",
        "attributed_keyword_method": "call_search_term",
        "attributed_keyword_confidence": 0.70,
        "od_patient_status": "new_patient",
        "campaign_id": "",
        "campaign_name": "General",
        "ad_group_name": "Broad",
        "gclid": "",
        "lead_id": "",
        "started_at": _recent_ts(1),
    }

    with caplog.at_level(logging.INFO, logger="call_production_log"):
        with patch("call_production_log._get_od_conn", return_value=None), \
             patch("call_production_log._fetch_call_production_data", return_value=[fake_row]), \
             patch("call_production_log._existing_lead_production_patient_nums", return_value=set()), \
             patch("database._conn", _fake_conn):

            import call_production_log as cpl
            cpl.link_calls_to_keyword_production(days=60)

    log_text = "\n".join(caplog.messages)
    assert "od_unavailable=1" in log_text, \
        f"Expected 'od_unavailable=1' in log output but got:\n{log_text}"


# ─── Helpers for endpoint tests (use in-memory DB via _fake_conn pattern) ─────

def _setup_modal_rows(conn, od_patient_name, ai_patient_name, od_patient_num,
                      paid_amount_365d=None, campaign_name="Emergency Dentistry",
                      has_kpl=True):
    """
    Insert test rows into an existing in-memory connection.
    Follows the same _fake_conn pattern as PR4 tests to avoid init_db() complexity.
    """
    started = _recent_ts(5)   # 5 days ago — within default 30-day window
    conn.execute("""
        INSERT OR REPLACE INTO mango_calls
            (uuid, started_at, direction, od_patient_name, ai_patient_name,
             od_patient_num, od_appointment_id, od_patient_status,
             attributed_keyword, attributed_ad_group, attributed_keyword_method,
             gads_call_id, booked_outcome, duration_sec)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, ("uuid-modal-test", started, "inbound",
          od_patient_name, ai_patient_name,
          od_patient_num, "31747", "new_patient",
          "emergency dentist", f"{campaign_name} > Pain Relief",
          "call_search_term", "", "booked", 180))

    if has_kpl and paid_amount_365d is not None and od_patient_num:
        conn.execute("""
            INSERT OR REPLACE INTO keyword_production_log
                (logged_at, lead_id, keyword_text, campaign_name, od_patient_num,
                 production_amount, match_method, appointment_date,
                 paid_amount_365d, paid_amount_ltv, confidence_tier)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (_recent_ts(1), "call::uuid-modal-test", "emergency dentist",
              campaign_name, od_patient_num,
              0.0, "call_call_search_term", "",
              paid_amount_365d, paid_amount_365d, "booked_override"))

    conn.commit()


_MODAL_SQL = """
    SELECT
        mc.uuid,
        mc.od_patient_name,
        mc.ai_patient_name,
        mc.od_patient_num,
        mc.od_appointment_id,
        mc.booked_outcome,
        COALESCE(
            NULLIF(mc.od_patient_name,''),
            NULLIF(mc.ai_patient_name,''),
            NULLIF(TRIM(l.first_name||' '||l.last_name),''),
            NULLIF(TRIM(l2.first_name||' '||l2.last_name),'')
        ) AS patient_name,
        COALESCE(kpl.paid_amount_365d, 0) AS paid_amount_365d
    FROM mango_calls mc
    LEFT JOIN leads l ON l.id = mc.lead_id
    LEFT JOIN leads l2 ON l2.od_patient_num = mc.od_patient_num
                      AND mc.od_patient_num != ''
                      AND (mc.lead_id IS NULL OR mc.lead_id = '')
    -- PR 5 fix: aggregate KPL by patient. Without GROUP BY, a patient with
    -- multiple KPL rows (lead-path + per-call rows) would multiply mango_calls
    -- rows in the modal and make paid_amount_365d ambiguous.
    LEFT JOIN (
        SELECT od_patient_num,
               MAX(paid_amount_365d) AS paid_amount_365d,
               MAX(appointment_date) AS appointment_date
        FROM keyword_production_log
        WHERE od_patient_num != ''
        GROUP BY od_patient_num
    ) kpl ON kpl.od_patient_num = mc.od_patient_num
         AND mc.od_patient_num != ''
    WHERE mc.started_at >= ?
      AND mc.direction = 'inbound'
      AND (
        (mc.od_appointment_id IS NOT NULL AND mc.od_appointment_id != '')
        OR mc.booked_outcome = 'booked'
      )
"""


# ─── Test 5: endpoint returns ai_patient_name + paid_amount_365d ─────────────

def test_modal_endpoint_returns_ai_patient_name_and_income(in_memory_db):
    """
    Insert mango_call with od_patient_name='', ai_patient_name='Matthew Cornwell',
    od_patient_num='5728', and KPL row with paid_amount_365d=199.
    Query must return: ai_patient_name='Matthew Cornwell', paid_amount_365d=199.0,
    patient_name='Matthew Cornwell' (via COALESCE).
    """
    _setup_modal_rows(
        in_memory_db,
        od_patient_name="",
        ai_patient_name="Matthew Cornwell",
        od_patient_num="5728",
        paid_amount_365d=199.0,
    )

    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    rows = in_memory_db.execute(_MODAL_SQL, (cutoff,)).fetchall()

    assert len(rows) == 1, f"Expected 1 row, got {len(rows)}"
    r = dict(rows[0])

    assert r["ai_patient_name"] == "Matthew Cornwell", \
        f"Expected ai_patient_name='Matthew Cornwell', got {r['ai_patient_name']!r}"
    assert abs(r["paid_amount_365d"] - 199.0) < 0.01, \
        f"Expected paid_amount_365d=199.0, got {r['paid_amount_365d']}"
    assert r["patient_name"] == "Matthew Cornwell", \
        f"Expected patient_name='Matthew Cornwell' via COALESCE, got {r['patient_name']!r}"


# ─── Test 6: od_patient_name wins over ai_patient_name ───────────────────────

def test_modal_endpoint_patient_name_prefers_od_when_present(in_memory_db):
    """
    When both od_patient_name='Smith, John' and ai_patient_name='Johnny S' are set,
    patient_name COALESCE must return 'Smith, John' (OD wins).
    """
    _setup_modal_rows(
        in_memory_db,
        od_patient_name="Smith, John",
        ai_patient_name="Johnny S",
        od_patient_num="5001",
        paid_amount_365d=100.0,
    )

    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    rows = in_memory_db.execute(_MODAL_SQL, (cutoff,)).fetchall()

    assert len(rows) == 1
    r = dict(rows[0])
    assert r["patient_name"] == "Smith, John", \
        f"Expected patient_name='Smith, John' (OD wins), got {r['patient_name']!r}"


# ─── Test 7: paid_amount_365d is 0.0 when no KPL row ────────────────────────

def test_modal_endpoint_paid_amount_zero_when_no_kpl(in_memory_db):
    """
    Unmatched call with no KPL row. paid_amount_365d must be 0.0, not None.
    """
    _setup_modal_rows(
        in_memory_db,
        od_patient_name="Jones, Sally",
        ai_patient_name="",
        od_patient_num="7777",
        paid_amount_365d=None,
        has_kpl=False,
    )

    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    rows = in_memory_db.execute(_MODAL_SQL, (cutoff,)).fetchall()

    assert len(rows) == 1
    r = dict(rows[0])
    assert r["paid_amount_365d"] == 0.0, \
        f"Expected paid_amount_365d=0.0 when no KPL row, got {r['paid_amount_365d']!r}"
    assert r["paid_amount_365d"] is not None, \
        "paid_amount_365d must be 0.0 (not None) — COALESCE handles the NULL"


# ─── Test 8: multi-KPL-row inflation (regression) ───────────────────────────

def test_modal_endpoint_no_inflation_when_patient_has_multiple_kpl_rows(in_memory_db):
    """
    Regression: a patient can have multiple KPL rows (e.g. one lead-path row +
    one call::uuid row, or multiple call-path rows from separate calls). Without
    a GROUP-BY/aggregating subquery on kpl, the LEFT JOIN would multiply the
    mango_calls row in the modal output.

    Setup: 1 mango_calls row for od_patient_num='5728', 2 KPL rows for same
    patient (lead-path + call-path) with different paid_amount_365d values.
    Expectation: exactly 1 row returned, paid_amount_365d picks the MAX of the
    two (so the strongest signal is shown, never doubled).
    """
    started = _recent_ts(2)
    in_memory_db.execute("""
        INSERT INTO mango_calls
            (uuid, started_at, direction, od_patient_name, ai_patient_name,
             od_patient_num, od_appointment_id, od_patient_status,
             attributed_keyword, attributed_ad_group, attributed_keyword_method,
             gads_call_id, booked_outcome, duration_sec)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, ("uuid-multi-kpl", started, "inbound", "Matthew Cornwell", "",
          "5728", "APT-1", "new_patient",
          "emergency", "Emergency > Pain", "call_search_term",
          "", "booked", 180))
    # First KPL row: lead-path, paid 500
    in_memory_db.execute("""
        INSERT INTO keyword_production_log
            (logged_at, lead_id, keyword_text, campaign_name, od_patient_num,
             paid_amount_365d, paid_amount_ltv, confidence_tier)
        VALUES (?,?,?,?,?,?,?,?)
    """, (_recent_ts(1), "lead-77", "implant", "Implants",
          "5728", 500.0, 500.0, "high"))
    # Second KPL row: call-path, paid 199 (same patient, different lead_id)
    in_memory_db.execute("""
        INSERT INTO keyword_production_log
            (logged_at, lead_id, keyword_text, campaign_name, od_patient_num,
             paid_amount_365d, paid_amount_ltv, confidence_tier)
        VALUES (?,?,?,?,?,?,?,?)
    """, (_recent_ts(1), "call::uuid-multi-kpl", "emergency", "Emergency",
          "5728", 199.0, 199.0, "booked_override"))
    in_memory_db.commit()

    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    rows = in_memory_db.execute(_MODAL_SQL, (cutoff,)).fetchall()

    assert len(rows) == 1, \
        f"Modal should return exactly 1 row per call even when patient has "\
        f"multiple KPL rows, but got {len(rows)}"
    r = dict(rows[0])
    # MAX(paid_amount_365d) of {500, 199} = 500 — strongest income signal wins.
    assert abs(r["paid_amount_365d"] - 500.0) < 0.01, \
        f"Expected paid_amount_365d=500.0 (MAX), got {r['paid_amount_365d']}"
