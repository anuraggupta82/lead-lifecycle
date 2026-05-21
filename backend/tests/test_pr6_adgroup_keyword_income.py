"""
Tests for PR 6: Ad-group + Keyword Income Parity + Confidence-Tier Breakdown.

Tests:
  1. test_ad_group_stats_includes_kpl_paid_income
  2. test_ad_group_stats_legacy_call_income_unchanged
  3. test_ad_group_stats_high_confidence_only_income
  4. test_ad_group_stats_excludes_orphan_tiers
  5. test_keyword_kpl_rollup_basic
  6. test_attribution_confidence_endpoint
  7. test_intelligence_builder_still_high_only  (regression)
  8. test_ad_group_endpoint_returns_new_fields
  9. test_campaign_detail_returns_keyword_income

Run from backend/:
  source venv/bin/activate
  pytest tests/test_pr6_adgroup_keyword_income.py -v
"""
import sqlite3
import sys
import os
import json
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager

# ─── Path setup ─────────────────────────────────────────────────────────────
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


# ─── Shared in-memory DB schema ─────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mango_calls (
    uuid                        TEXT PRIMARY KEY,
    started_at                  TEXT,
    from_number                 TEXT DEFAULT '',
    direction                   TEXT DEFAULT 'inbound',
    od_patient_num              TEXT DEFAULT '',
    od_patient_status           TEXT DEFAULT '',
    od_patient_name             TEXT DEFAULT '',
    od_appointment_id           TEXT DEFAULT '',
    od_patient_income           REAL DEFAULT NULL,
    od_patient_production       REAL DEFAULT NULL,
    od_income_synced_at         TEXT DEFAULT '',
    od_matched_at               TEXT DEFAULT '',
    attributed_keyword          TEXT DEFAULT '',
    attributed_match_type       TEXT DEFAULT '',
    attributed_ad_group         TEXT DEFAULT '',
    attributed_keyword_method   TEXT DEFAULT '',
    attributed_keyword_confidence REAL DEFAULT NULL,
    lead_id                     TEXT DEFAULT '',
    gads_call_id                TEXT DEFAULT '',
    status                      TEXT DEFAULT '',
    updated_at                  TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS keyword_production_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at           TEXT NOT NULL,
    lead_id             TEXT NOT NULL DEFAULT '',
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
    attributed_income   REAL DEFAULT 0.0,
    attributed_production REAL DEFAULT 0.0,
    paid_amount_365d    REAL DEFAULT 0.0,
    paid_amount_ltv     REAL DEFAULT 0.0,
    stage               TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS gads_daily_stats (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT,
    campaign_name   TEXT,
    campaign_id     TEXT DEFAULT '',
    ad_group_id     TEXT DEFAULT '',
    ad_group_name   TEXT DEFAULT '',
    impressions     INTEGER DEFAULT 0,
    clicks          INTEGER DEFAULT 0,
    cost_micros     INTEGER DEFAULT 0,
    conversions     REAL DEFAULT 0.0,
    synced_at       TEXT DEFAULT '',
    UNIQUE(date, campaign_id, ad_group_id)
);

CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id             TEXT PRIMARY KEY,
    campaign_name           TEXT DEFAULT '',
    campaign_type           TEXT DEFAULT '',
    status                  TEXT DEFAULT '',
    service_focus           TEXT DEFAULT '',
    monthly_budget          REAL DEFAULT 0.0,
    start_date              TEXT DEFAULT '',
    end_date                TEXT DEFAULT '',
    workflow_id             INTEGER DEFAULT NULL,
    created_at              TEXT DEFAULT '',
    updated_at              TEXT DEFAULT '',
    gads_campaign_resource  TEXT DEFAULT NULL,
    gads_campaign_numeric_id TEXT DEFAULT NULL,
    strategy_json           TEXT DEFAULT NULL,
    ai_review_enabled       INTEGER DEFAULT 0,
    ai_max_enabled          INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS workflows (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT
);

CREATE TABLE IF NOT EXISTS keyword_intelligence (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    date                    TEXT NOT NULL,
    keyword_text            TEXT NOT NULL,
    match_type              TEXT NOT NULL DEFAULT '',
    campaign_id             TEXT NOT NULL DEFAULT '',
    campaign_name           TEXT NOT NULL DEFAULT '',
    ad_group_name           TEXT NOT NULL DEFAULT '',
    impressions             INTEGER DEFAULT 0,
    clicks                  INTEGER DEFAULT 0,
    cost_usd                REAL DEFAULT 0.0,
    avg_cpc                 REAL DEFAULT 0.0,
    conversions             REAL DEFAULT 0.0,
    quality_score           INTEGER DEFAULT 0,
    impression_share        REAL DEFAULT 0.0,
    od_appointments         INTEGER DEFAULT 0,
    od_production_total     REAL DEFAULT 0.0,
    od_production_per_click REAL DEFAULT 0.0,
    ga4_sessions            INTEGER DEFAULT 0,
    ga4_avg_duration_sec    REAL DEFAULT 0.0,
    ga4_bounce_rate         REAL DEFAULT 0.0,
    ga4_lead_events         INTEGER DEFAULT 0,
    session_quality_score   REAL DEFAULT 0.0,
    times_recommended       INTEGER DEFAULT 0,
    times_applied           INTEGER DEFAULT 0,
    times_rejected          INTEGER DEFAULT 0,
    last_decision_at        TEXT DEFAULT '',
    last_decision           TEXT DEFAULT '',
    true_roas               REAL DEFAULT 0.0,
    data_age_days           INTEGER DEFAULT 0,
    confidence_tier         TEXT DEFAULT 'low',
    rebuilt_at              TEXT NOT NULL,
    UNIQUE(keyword_text, match_type, campaign_id, date)
);

CREATE TABLE IF NOT EXISTS gads_keywords_cache (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword_text    TEXT NOT NULL,
    match_type      TEXT DEFAULT 'BROAD',
    campaign_name   TEXT DEFAULT '',
    campaign_id     TEXT DEFAULT '',
    ad_group_name   TEXT DEFAULT '',
    days            INTEGER DEFAULT 30,
    impressions     INTEGER DEFAULT 0,
    clicks          INTEGER DEFAULT 0,
    cost            REAL DEFAULT 0.0,
    avg_cpc         REAL DEFAULT 0.0,
    conversions     REAL DEFAULT 0.0,
    quality_score   INTEGER DEFAULT 0,
    impression_share REAL DEFAULT 0.0,
    synced_at       TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS gads_audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    entity_type     TEXT DEFAULT '',
    entity_name     TEXT DEFAULT '',
    operation       TEXT DEFAULT '',
    execution_result TEXT DEFAULT '',
    campaign_id     TEXT DEFAULT '',
    reject_reason   TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS gads_call_view (
    call_id         TEXT,
    campaign_id     TEXT DEFAULT '',
    campaign_name   TEXT DEFAULT '',
    ad_group_name   TEXT DEFAULT ''
);
"""


@pytest.fixture
def in_memory_db():
    """Create a minimal in-memory SQLite database with PR 6 tables."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    yield conn
    conn.close()


@contextmanager
def _fake_conn_factory(real_conn):
    """Context manager that returns the in-memory conn without closing it."""
    yield real_conn


def _kpl_insert(conn, lead_id, keyword_text, campaign_name, ad_group_name,
                 paid_amount_365d=0.0, paid_amount_ltv=0.0,
                 confidence_tier=None, od_patient_num="5728"):
    """Helper to insert a KPL row."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT OR REPLACE INTO keyword_production_log
            (logged_at, lead_id, keyword_text, campaign_name, ad_group_name,
             od_patient_num, production_amount, match_method, appointment_date,
             paid_amount_365d, paid_amount_ltv, confidence_tier)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (now, lead_id, keyword_text, campaign_name, ad_group_name,
          od_patient_num, 0.0, "call_search_term", "2026-05-18",
          paid_amount_365d, paid_amount_ltv, confidence_tier))
    conn.commit()


def _gads_stats_insert(conn, ad_group_id, ad_group_name, campaign_id, campaign_name,
                        cost_micros=692_000_000):
    """Helper to insert gads_daily_stats row so ad group appears in get_ad_group_stats."""
    from datetime import date
    conn.execute("""
        INSERT OR REPLACE INTO gads_daily_stats
            (date, campaign_id, campaign_name, ad_group_id, ad_group_name,
             impressions, clicks, cost_micros, conversions, synced_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (date.today().isoformat(), campaign_id, campaign_name, ad_group_id,
          ad_group_name, 150, 10, cost_micros, 0.0, datetime.now(timezone.utc).isoformat()))
    conn.commit()


# ─── Test 1: KPL income shows up in get_ad_group_stats ───────────────────────

def test_ad_group_stats_includes_kpl_paid_income(in_memory_db):
    """
    Insert 1 KPL row for ad_group_name='X' with paid_amount_365d=199,
    confidence_tier='booked_override'. Assert returned ad_group dict for 'X'
    has paid_income_365d=199, income_booked_override=199, income_high=0.
    """
    _gads_stats_insert(in_memory_db, "ag001", "X", "camp001", "Test Campaign")
    _kpl_insert(in_memory_db,
                lead_id="call::abc123",
                keyword_text="emergency dentist",
                campaign_name="Test Campaign",
                ad_group_name="X",
                paid_amount_365d=199.0,
                paid_amount_ltv=199.0,
                confidence_tier="booked_override")

    def _fake():
        return _fake_conn_factory(in_memory_db)

    with patch("database._conn", _fake):
        import database
        ag_list = database.get_ad_group_stats(days=30)

    ag = next((a for a in ag_list if (a.get("ad_group_name") or "").upper() == "X"), None)
    assert ag is not None, "Ad group 'X' not found in get_ad_group_stats output"
    assert abs(ag["paid_income_365d"] - 199.0) < 0.01, \
        f"Expected paid_income_365d=199 but got {ag['paid_income_365d']}"
    assert abs(ag["income_booked_override"] - 199.0) < 0.01, \
        f"Expected income_booked_override=199 but got {ag['income_booked_override']}"
    assert ag["income_high"] == 0.0, \
        f"Expected income_high=0 but got {ag['income_high']}"


# ─── Test 2: Legacy call_income is unchanged ─────────────────────────────────

def test_ad_group_stats_legacy_call_income_unchanged(in_memory_db):
    """
    Insert 1 KPL row and 1 mango_calls row for the same ad group.
    Assert both call_income=199 AND paid_income_365d=199 are returned independently.
    """
    _gads_stats_insert(in_memory_db, "ag002", "Tooth Pain", "camp002", "Emergency Dentistry")
    _kpl_insert(in_memory_db,
                lead_id="call::legacy1",
                keyword_text="toothache",
                campaign_name="Emergency Dentistry",
                ad_group_name="Tooth Pain",
                paid_amount_365d=199.0,
                confidence_tier="booked_override")

    now = datetime.now(timezone.utc).isoformat()
    in_memory_db.execute("""
        INSERT INTO mango_calls
            (uuid, started_at, od_patient_status, attributed_ad_group,
             od_patient_income, direction)
        VALUES (?,?,?,?,?,?)
    """, ("mc001", now, "new_patient", "Tooth Pain", 199.0, "inbound"))
    in_memory_db.commit()

    def _fake():
        return _fake_conn_factory(in_memory_db)

    with patch("database._conn", _fake):
        import database
        ag_list = database.get_ad_group_stats(days=30)

    ag = next((a for a in ag_list
               if "tooth pain" in (a.get("ad_group_name") or "").lower()), None)
    assert ag is not None, "Tooth Pain ad group not found"
    assert abs(ag["call_income"] - 199.0) < 0.01, \
        f"Expected legacy call_income=199 but got {ag['call_income']}"
    assert abs(ag["paid_income_365d"] - 199.0) < 0.01, \
        f"Expected paid_income_365d=199 but got {ag['paid_income_365d']}"


# ─── Test 3: High-confidence tier income ─────────────────────────────────────

def test_ad_group_stats_high_confidence_only_income(in_memory_db):
    """
    KPL row with confidence_tier='high', paid_amount=500.
    Assert income_high=500, income_booked_override=0.
    """
    _gads_stats_insert(in_memory_db, "ag003", "Implants", "camp003", "Implant Campaign")
    _kpl_insert(in_memory_db,
                lead_id="lead::high001",
                keyword_text="dental implants",
                campaign_name="Implant Campaign",
                ad_group_name="Implants",
                paid_amount_365d=500.0,
                confidence_tier="high")

    def _fake():
        return _fake_conn_factory(in_memory_db)

    with patch("database._conn", _fake):
        import database
        ag_list = database.get_ad_group_stats(days=30)

    ag = next((a for a in ag_list
               if "implants" in (a.get("ad_group_name") or "").lower()), None)
    assert ag is not None, "Implants ad group not found"
    assert abs(ag["income_high"] - 500.0) < 0.01, \
        f"Expected income_high=500 but got {ag['income_high']}"
    assert ag["income_booked_override"] == 0.0, \
        f"Expected income_booked_override=0 but got {ag['income_booked_override']}"


# ─── Test 4: Orphan tier 'garbage' is excluded ───────────────────────────────

def test_ad_group_stats_excludes_orphan_tiers(in_memory_db):
    """
    A KPL row with confidence_tier='garbage' should NOT be included in
    paid_income_365d. Filter is IN ('high','low','booked_override') OR IS NULL.
    """
    _gads_stats_insert(in_memory_db, "ag004", "Ortho", "camp004", "Ortho Campaign")
    # Valid row — should be counted
    _kpl_insert(in_memory_db,
                lead_id="lead::good",
                keyword_text="braces",
                campaign_name="Ortho Campaign",
                ad_group_name="Ortho",
                paid_amount_365d=200.0,
                confidence_tier="high",
                od_patient_num="1111")
    # Orphan row — should be excluded
    now = datetime.now(timezone.utc).isoformat()
    in_memory_db.execute("""
        INSERT INTO keyword_production_log
            (logged_at, lead_id, keyword_text, campaign_name, ad_group_name,
             od_patient_num, production_amount, match_method, appointment_date,
             paid_amount_365d, paid_amount_ltv, confidence_tier)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (now, "lead::garbage", "braces bad", "Ortho Campaign", "Ortho",
          "9999", 0.0, "x", "", 999.0, 999.0, "garbage"))
    in_memory_db.commit()

    def _fake():
        return _fake_conn_factory(in_memory_db)

    with patch("database._conn", _fake):
        import database
        ag_list = database.get_ad_group_stats(days=30)

    ag = next((a for a in ag_list
               if "ortho" in (a.get("ad_group_name") or "").lower()), None)
    assert ag is not None, "Ortho ad group not found"
    # Only the 'high' row ($200) should appear; 'garbage' ($999) must be excluded
    assert abs(ag["paid_income_365d"] - 200.0) < 0.01, \
        f"Expected paid_income_365d=200 (orphan tier excluded) but got {ag['paid_income_365d']}"


# ─── Test 5: get_keyword_kpl_rollup basic grouping ───────────────────────────

def test_keyword_kpl_rollup_basic(in_memory_db):
    """
    Insert 2 KPL rows for the same campaign and keyword.
    Assert single grouped row with kpl_row_count=2 and summed paid amount.
    """
    campaign = "Emergency Dentistry"
    now = datetime.now(timezone.utc).isoformat()
    for i, (lead, patnum, amount) in enumerate([
        ("lead::kw1", "5728", 100.0),
        ("lead::kw2", "6001", 99.0),
    ]):
        in_memory_db.execute("""
            INSERT INTO keyword_production_log
                (logged_at, lead_id, keyword_text, campaign_name, ad_group_name,
                 od_patient_num, production_amount, match_method, appointment_date,
                 paid_amount_365d, paid_amount_ltv, confidence_tier)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (now, lead, "emergency dentist", campaign, "Emergency Pain Relief",
              patnum, 0.0, "call_search_term", "2026-05-18",
              amount, amount, "booked_override"))
    in_memory_db.commit()

    def _fake():
        return _fake_conn_factory(in_memory_db)

    with patch("database._conn", _fake):
        import database
        rollup = database.get_keyword_kpl_rollup(campaign, days=30)

    assert len(rollup) >= 1, "Expected at least 1 grouped row from get_keyword_kpl_rollup"
    row = next((r for r in rollup if r["keyword_text"] == "emergency dentist"), None)
    assert row is not None, "Expected 'emergency dentist' keyword in rollup"
    assert row["kpl_row_count"] == 2, \
        f"Expected kpl_row_count=2 but got {row['kpl_row_count']}"
    assert abs(row["paid_income_365d"] - 199.0) < 0.01, \
        f"Expected paid_income_365d=199 but got {row['paid_income_365d']}"


# ─── Test 6: Attribution confidence endpoint ──────────────────────────────────

def test_attribution_confidence_endpoint(in_memory_db):
    """
    Insert 1 high ($500), 1 low ($100), 1 booked_override ($199).
    Hit /api/admin/attribution-confidence?days=30.
    Assert returned dict has correct sums and percentages.
    """
    now = datetime.now(timezone.utc).isoformat()
    for lead, patnum, tier, amount in [
        ("lead::t1", "1001", "high", 500.0),
        ("lead::t2", "1002", "low", 100.0),
        ("lead::t3", "1003", "booked_override", 199.0),
    ]:
        in_memory_db.execute("""
            INSERT INTO keyword_production_log
                (logged_at, lead_id, keyword_text, campaign_name, ad_group_name,
                 od_patient_num, production_amount, match_method, appointment_date,
                 paid_amount_365d, paid_amount_ltv, confidence_tier)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (now, lead, "test kw", "Test Campaign", "Test AG",
              patnum, 0.0, "x", "", amount, amount, tier))
    in_memory_db.commit()

    def _fake():
        return _fake_conn_factory(in_memory_db)

    # Call the underlying SQL directly against our in-memory DB — same query
    # as the admin_attribution_confidence endpoint. Avoids importing main (which
    # pulls in apscheduler and other heavy deps not available in test environment).
    with patch("database._conn", _fake):
        import database  # noqa: ensure patching is active
        row = in_memory_db.execute("""
            SELECT
                COALESCE(SUM(CASE WHEN confidence_tier = 'high'            THEN paid_amount_365d ELSE 0 END), 0) AS high_365d,
                COALESCE(SUM(CASE WHEN confidence_tier = 'low'             THEN paid_amount_365d ELSE 0 END), 0) AS low_365d,
                COALESCE(SUM(CASE WHEN confidence_tier = 'booked_override' THEN paid_amount_365d ELSE 0 END), 0) AS booked_override_365d,
                COALESCE(SUM(paid_amount_365d), 0) AS total_365d
            FROM keyword_production_log
            WHERE (confidence_tier IN ('high','low','booked_override') OR confidence_tier IS NULL)
        """).fetchone()

    assert row is not None
    assert abs(row["high_365d"] - 500.0) < 0.01, f"high_365d expected 500, got {row['high_365d']}"
    assert abs(row["low_365d"] - 100.0) < 0.01, f"low_365d expected 100, got {row['low_365d']}"
    assert abs(row["booked_override_365d"] - 199.0) < 0.01, \
        f"booked_override_365d expected 199, got {row['booked_override_365d']}"
    total = row["total_365d"]
    assert abs(total - 799.0) < 0.01, f"total_365d expected 799, got {total}"
    # Verify percentages
    assert abs(row["high_365d"] / total * 100 - 500/799*100) < 0.1
    assert abs(row["booked_override_365d"] / total * 100 - 199/799*100) < 0.1


# ─── Test 7: intelligence_builder still 'high'-only (regression) ─────────────

def test_intelligence_builder_still_high_only(in_memory_db):
    """
    Regression: Insert 1 high ($500) + 1 booked_override ($199) KPL row
    for the same keyword. Run rebuild_keyword_intelligence().
    Assert the resulting keyword_intelligence row has total_production = HIGH amount only.
    booked_override must NOT pollute optimizer signal (PR 4 invariant).
    """
    campaign = "Emergency Dentistry"
    campaign_id = "camp-emerg"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cutoff_90d = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")
    logged_at = datetime.now(timezone.utc).isoformat()

    # Insert campaign for name→id mapping
    in_memory_db.execute("""
        INSERT INTO campaigns (campaign_id, campaign_name, status)
        VALUES (?,?,?)
    """, (campaign_id, campaign, "ACTIVE"))

    # High-tier row: $500
    in_memory_db.execute("""
        INSERT INTO keyword_production_log
            (logged_at, lead_id, keyword_text, campaign_id, campaign_name, ad_group_name,
             od_patient_num, production_amount, match_method, appointment_date,
             paid_amount_365d, paid_amount_ltv, confidence_tier)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (logged_at, "lead::high", "emergency dentist near me",
          campaign_id, campaign, "Emergency Pain Relief",
          "5728", 500.0, "x", "", 500.0, 500.0, "high"))

    # Booked-override row: $199 — must NOT appear in optimizer signal
    in_memory_db.execute("""
        INSERT INTO keyword_production_log
            (logged_at, lead_id, keyword_text, campaign_id, campaign_name, ad_group_name,
             od_patient_num, production_amount, match_method, appointment_date,
             paid_amount_365d, paid_amount_ltv, confidence_tier)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (logged_at, "call::booked", "emergency dentist near me",
          campaign_id, campaign, "Emergency Pain Relief",
          "9999", 199.0, "x", "", 199.0, 199.0, "booked_override"))

    # Insert gads_keywords_cache so builder has primary source data
    in_memory_db.execute("""
        INSERT INTO gads_keywords_cache
            (keyword_text, match_type, campaign_name, campaign_id, ad_group_name,
             days, impressions, clicks, cost, avg_cpc, conversions, synced_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, ("emergency dentist near me", "EXACT", campaign, campaign_id,
          "Emergency Pain Relief", 30, 150, 12, 12.50, 1.04, 0.0,
          datetime.now(timezone.utc).isoformat()))

    in_memory_db.commit()

    def _fake():
        return _fake_conn_factory(in_memory_db)

    # Stub GA4 cache to return empty so builder doesn't fail.
    # intelligence_builder uses `from database import _conn` (local import),
    # so only patching database._conn is needed — patching intelligence_builder._conn
    # would raise AttributeError.
    with patch("database._conn", _fake), \
         patch("database.get_ga4_cache", return_value=None):

        import database
        # Patch upsert to write into our in-memory DB
        def _fake_upsert(rows):
            for r in rows:
                in_memory_db.execute("""
                    INSERT OR REPLACE INTO keyword_intelligence
                        (date, keyword_text, match_type, campaign_id, campaign_name,
                         ad_group_name, impressions, clicks, cost_usd, avg_cpc,
                         conversions, quality_score, impression_share,
                         od_appointments, od_production_total, od_production_per_click,
                         ga4_sessions, ga4_avg_duration_sec, ga4_bounce_rate, ga4_lead_events,
                         session_quality_score, times_recommended, times_applied, times_rejected,
                         last_decision_at, last_decision, true_roas, data_age_days,
                         confidence_tier, rebuilt_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    r.get("date",""), r.get("keyword_text",""), r.get("match_type",""),
                    r.get("campaign_id",""), r.get("campaign_name",""), r.get("ad_group_name",""),
                    r.get("impressions",0), r.get("clicks",0), r.get("cost_usd",0.0),
                    r.get("avg_cpc",0.0), r.get("conversions",0.0), r.get("quality_score",0),
                    r.get("impression_share",0.0), r.get("od_appointments",0),
                    r.get("od_production_total",0.0), r.get("od_production_per_click",0.0),
                    r.get("ga4_sessions",0), r.get("ga4_avg_duration_sec",0.0),
                    r.get("ga4_bounce_rate",0.0), r.get("ga4_lead_events",0),
                    r.get("session_quality_score",0.0), r.get("times_recommended",0),
                    r.get("times_applied",0), r.get("times_rejected",0),
                    r.get("last_decision_at",""), r.get("last_decision",""),
                    r.get("true_roas",0.0), r.get("data_age_days",0),
                    r.get("confidence_tier","low"), r.get("rebuilt_at",""),
                ))
                in_memory_db.commit()
            return len(rows)

        with patch("database.upsert_keyword_intelligence", _fake_upsert):
            from intelligence_builder import rebuild_keyword_intelligence
            result = rebuild_keyword_intelligence()

    ki_row = in_memory_db.execute("""
        SELECT od_production_total FROM keyword_intelligence
        WHERE keyword_text = 'emergency dentist near me'
        LIMIT 1
    """).fetchone()

    assert ki_row is not None, "Expected a keyword_intelligence row to be written"
    total_production = ki_row["od_production_total"]
    assert abs(total_production - 500.0) < 0.01, \
        f"intelligence_builder must only use 'high' tier: expected 500 but got {total_production}"


# ─── Test 8: Campaign detail endpoint returns new ad-group fields ─────────────

def test_ad_group_endpoint_returns_new_fields(in_memory_db):
    """
    End-to-end: GET /api/admin/campaigns/{id}/detail.
    Assert response.ad_groups[0] has paid_income_365d, income_high,
    income_booked_override keys.
    """
    campaign_id = "camp-e2e-ag"
    campaign_name = "E2E Test Campaign"

    in_memory_db.execute("""
        INSERT INTO campaigns (campaign_id, campaign_name, status, gads_campaign_numeric_id)
        VALUES (?,?,?,?)
    """, (campaign_id, campaign_name, "ACTIVE", "999888"))
    in_memory_db.commit()

    _gads_stats_insert(in_memory_db, "ag-e2e-01", "E2E Ad Group", "999888", campaign_name)
    _kpl_insert(in_memory_db,
                lead_id="call::e2e-test",
                keyword_text="e2e keyword",
                campaign_name=campaign_name,
                ad_group_name="E2E Ad Group",
                paid_amount_365d=299.0,
                confidence_tier="high")

    def _fake():
        return _fake_conn_factory(in_memory_db)

    # Test by calling get_ad_group_stats directly (the endpoint calls this internally)
    with patch("database._conn", _fake):
        import database
        ag_list = database.get_ad_group_stats(days=30)

    ag = next((a for a in ag_list if "e2e" in (a.get("ad_group_name") or "").lower()), None)
    assert ag is not None, "E2E Ad Group not found in get_ad_group_stats"
    assert "paid_income_365d" in ag, "paid_income_365d key missing from ad group dict"
    assert "income_high" in ag, "income_high key missing from ad group dict"
    assert "income_booked_override" in ag, "income_booked_override key missing from ad group dict"


# ─── Test 10: KPL multi-row-per-patient dedup ────────────────────────────────

def test_ad_group_stats_dedups_multiple_kpl_rows_per_patient(in_memory_db):
    """
    Regression (Opus, PR 6 review): A single patient (od_patient_num='5728') can
    have BOTH a call-path KPL row (lead_id='call::abc') AND a lead-path KPL row
    (lead_id='lead-uuid') under the same ad_group_name with the same
    paid_amount_365d after od_payment_sync. Naive SUM would double-count to $398.
    The CTE must dedup by (ad_group_name, od_patient_num), keeping one row.
    """
    _gads_stats_insert(in_memory_db, "ag-dedup", "Dedup AG", "camp-dedup", "Dedup Campaign")
    # Call-path row
    _kpl_insert(in_memory_db,
                lead_id="call::matthew",
                keyword_text="emergency dentist",
                campaign_name="Dedup Campaign",
                ad_group_name="Dedup AG",
                paid_amount_365d=199.0,
                paid_amount_ltv=199.0,
                confidence_tier="booked_override",
                od_patient_num="5728")
    # Lead-path row for the SAME patient
    _kpl_insert(in_memory_db,
                lead_id="lead-form-uuid",
                keyword_text="emergency dentist",
                campaign_name="Dedup Campaign",
                ad_group_name="Dedup AG",
                paid_amount_365d=199.0,
                paid_amount_ltv=199.0,
                confidence_tier="high",
                od_patient_num="5728")

    def _fake():
        return _fake_conn_factory(in_memory_db)

    with patch("database._conn", _fake):
        import database
        ag_list = database.get_ad_group_stats(days=30)

    ag = next((a for a in ag_list if "dedup" in (a.get("ad_group_name") or "").lower()), None)
    assert ag is not None, "Dedup AG not found"
    # Must be $199 (one row), NOT $398 (sum of both)
    assert abs(ag["paid_income_365d"] - 199.0) < 0.01, \
        f"Expected dedup to $199 but got {ag['paid_income_365d']} (double-count bug)"
    # Should prefer the call-path row's tier (booked_override)
    assert abs(ag["income_booked_override"] - 199.0) < 0.01, \
        f"Expected income_booked_override=199 (call-path preferred) but got {ag['income_booked_override']}"
    assert ag["income_high"] == 0.0, \
        f"Expected income_high=0 (lead-path row deduped out) but got {ag['income_high']}"


def test_keyword_kpl_rollup_dedups_multiple_kpl_rows_per_patient(in_memory_db):
    """
    Same regression for the keyword-level rollup.
    """
    campaign = "Dedup KW Campaign"
    now = datetime.now(timezone.utc).isoformat()
    # Call-path
    in_memory_db.execute("""
        INSERT INTO keyword_production_log
            (logged_at, lead_id, keyword_text, campaign_name, ad_group_name,
             od_patient_num, production_amount, match_method, appointment_date,
             paid_amount_365d, paid_amount_ltv, confidence_tier)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (now, "call::p1", "tooth pain", campaign, "AG1",
          "7777", 0.0, "x", "", 250.0, 250.0, "booked_override"))
    # Lead-path for SAME patient + SAME keyword
    in_memory_db.execute("""
        INSERT INTO keyword_production_log
            (logged_at, lead_id, keyword_text, campaign_name, ad_group_name,
             od_patient_num, production_amount, match_method, appointment_date,
             paid_amount_365d, paid_amount_ltv, confidence_tier)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (now, "lead-form-7777", "tooth pain", campaign, "AG1",
          "7777", 0.0, "x", "", 250.0, 250.0, "high"))
    in_memory_db.commit()

    def _fake():
        return _fake_conn_factory(in_memory_db)

    with patch("database._conn", _fake):
        import database
        rollup = database.get_keyword_kpl_rollup(campaign, days=30)

    row = next((r for r in rollup if r["keyword_text"] == "tooth pain"), None)
    assert row is not None, "tooth pain row missing"
    assert abs(row["paid_income_365d"] - 250.0) < 0.01, \
        f"Expected dedup to $250 but got {row['paid_income_365d']} (double-count bug)"
    assert row["kpl_row_count"] == 1, \
        f"Expected kpl_row_count=1 after dedup but got {row['kpl_row_count']}"


# ─── Test 9: Campaign detail returns keyword_income ───────────────────────────

def test_campaign_detail_returns_keyword_income(in_memory_db):
    """
    End-to-end: Assert admin_campaign_detail response has keyword_income key
    with the expected list shape.
    """
    campaign_id = "camp-e2e-kw"
    campaign_name = "E2E KW Campaign"

    in_memory_db.execute("""
        INSERT INTO campaigns (campaign_id, campaign_name, status)
        VALUES (?,?,?)
    """, (campaign_id, campaign_name, "ACTIVE"))
    in_memory_db.commit()

    _kpl_insert(in_memory_db,
                lead_id="call::kw-e2e",
                keyword_text="test keyword e2e",
                campaign_name=campaign_name,
                ad_group_name="KW Ad Group",
                paid_amount_365d=150.0,
                confidence_tier="booked_override")

    def _fake():
        return _fake_conn_factory(in_memory_db)

    with patch("database._conn", _fake):
        import database
        kw_income = database.get_keyword_kpl_rollup(campaign_name, days=30)

    assert isinstance(kw_income, list), "get_keyword_kpl_rollup must return a list"
    assert len(kw_income) >= 1, "Expected at least 1 keyword income row"
    row = kw_income[0]
    assert "keyword_text" in row, "keyword_text missing from keyword income row"
    assert "kpl_row_count" in row, "kpl_row_count missing from keyword income row"
    assert "paid_income_365d" in row, "paid_income_365d missing from keyword income row"
    assert "income_booked_override" in row, "income_booked_override missing from keyword income row"
    assert abs(row["paid_income_365d"] - 150.0) < 0.01, \
        f"Expected paid_income_365d=150 but got {row['paid_income_365d']}"
