"""
SQLite database — leads, events, follow-up queue, unsubscribes, OD matches.
"""
import sqlite3
import os
import json
import hashlib
from datetime import datetime, timezone, timedelta
from typing import Optional
from config import get_settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id          TEXT PRIMARY KEY,          -- UUID from landing page
    created_at  TEXT NOT NULL,
    source      TEXT DEFAULT 'unknown',    -- 'smile_tool','contact_form','pearly','waitlist'
    stage       TEXT DEFAULT 'new',        -- lifecycle stage
    first_name  TEXT DEFAULT '',
    last_name   TEXT DEFAULT '',
    email       TEXT DEFAULT '',
    phone       TEXT DEFAULT '',
    phone_hash  TEXT DEFAULT '',           -- SHA-256(normalized phone) for OD matching
    email_hash  TEXT DEFAULT '',           -- SHA-256(lower email) for OD matching
    goals       TEXT DEFAULT '',           -- JSON array of goals from landing page
    gclid       TEXT DEFAULT '',
    fbclid      TEXT DEFAULT '',
    msclkid     TEXT DEFAULT '',
    utm_source  TEXT DEFAULT '',
    utm_medium  TEXT DEFAULT '',
    utm_campaign TEXT DEFAULT '',
    utm_term    TEXT DEFAULT '',
    utm_content TEXT DEFAULT '',
    landing_url TEXT DEFAULT '',
    smile_image_url TEXT DEFAULT '',       -- GCS signed URL (legacy, may be expired)
    smile_blob_name TEXT DEFAULT '',       -- GCS blob name for fresh re-signing
    smile_generated_at TEXT DEFAULT '',
    unsubscribed_email INTEGER DEFAULT 0,
    unsubscribed_sms   INTEGER DEFAULT 0,
    od_patient_num     TEXT DEFAULT '',    -- matched OpenDental PatNum
    od_matched_at      TEXT DEFAULT '',
    attributed_production REAL DEFAULT 0.0,
    treatment_plan_value  REAL DEFAULT 0.0,  -- estimated value from OD treatment plan
    attributed_income     REAL DEFAULT 0.0,  -- actual collections (payments received)
    booking_id  TEXT DEFAULT '',           -- scheduler booking ID when booked
    appointment_date TEXT DEFAULT '',      -- scheduled appointment date
    appointment_status TEXT DEFAULT '',    -- from OD: scheduled, confirmed, broken, complete
    no_show_count INTEGER DEFAULT 0,      -- number of broken appointments
    notes       TEXT DEFAULT '',
    -- Google Ads resolved fields (populated by google_ads_sync.py)
    keyword_text    TEXT DEFAULT '',       -- matched keyword from Google Ads
    search_term     TEXT DEFAULT '',       -- actual search query the user typed
    ad_group_name   TEXT DEFAULT '',       -- ad group name
    ad_group_id     TEXT DEFAULT '',       -- ad group numeric ID
    ad_id           TEXT DEFAULT '',       -- ad creative ID
    ad_name         TEXT DEFAULT '',       -- ad creative name
    campaign_name   TEXT DEFAULT '',       -- Google Ads campaign name
    campaign_id     TEXT DEFAULT '',       -- Google Ads campaign ID
    click_cost      REAL DEFAULT 0.0,     -- cost per click in dollars
    gads_synced_at  TEXT DEFAULT '',       -- when gclid was last resolved
    last_staff_contact_at TEXT DEFAULT '',  -- updated when staff logs a call/email/text
    -- Stage timestamps (auto-populated on stage transitions)
    auto_nurture_at TEXT DEFAULT '',
    scheduled_at    TEXT DEFAULT '',
    no_show_at      TEXT DEFAULT '',
    showed_at       TEXT DEFAULT '',
    tx_presented_at TEXT DEFAULT '',
    tx_accepted_at  TEXT DEFAULT '',
    tx_completed_at TEXT DEFAULT '',
    cold_at         TEXT DEFAULT '',
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(email);
CREATE INDEX IF NOT EXISTS idx_leads_phone_hash ON leads(phone_hash);
CREATE INDEX IF NOT EXISTS idx_leads_stage ON leads(stage);
CREATE INDEX IF NOT EXISTS idx_leads_created ON leads(created_at);
CREATE INDEX IF NOT EXISTS idx_leads_gclid ON leads(gclid);

CREATE TABLE IF NOT EXISTS lifecycle_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id     TEXT NOT NULL,
    event_type  TEXT NOT NULL,   -- 'lead_created','smile_completed','email_sent','sms_sent',
                                  -- 'booking_confirmed','call_matched','od_matched','stage_changed',
                                  -- 'unsubscribed','marked_cold'
    stage_from  TEXT DEFAULT '',
    stage_to    TEXT DEFAULT '',
    detail      TEXT DEFAULT '',  -- JSON blob with extra context
    source      TEXT DEFAULT '',  -- 'landing_page','scheduler','mango','od_sync','follow_up_engine','admin'
    created_at  TEXT NOT NULL,
    FOREIGN KEY (lead_id) REFERENCES leads(id)
);

CREATE INDEX IF NOT EXISTS idx_events_lead ON lifecycle_events(lead_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON lifecycle_events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_created ON lifecycle_events(created_at);

CREATE TABLE IF NOT EXISTS follow_up_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id     TEXT NOT NULL,
    sequence_day INTEGER NOT NULL,  -- 0,1,3,7,14,21,30
    channel     TEXT NOT NULL,      -- 'email' or 'sms'
    template    TEXT NOT NULL,      -- e.g. 'day1_email','day3_sms'
    scheduled_at TEXT NOT NULL,     -- ISO timestamp when to send
    sent_at     TEXT DEFAULT '',
    status      TEXT DEFAULT 'pending',  -- 'pending','sent','skipped','failed'
    error       TEXT DEFAULT '',
    FOREIGN KEY (lead_id) REFERENCES leads(id)
);

CREATE INDEX IF NOT EXISTS idx_queue_status ON follow_up_queue(status, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_queue_lead ON follow_up_queue(lead_id);

CREATE TABLE IF NOT EXISTS unsubscribes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id     TEXT NOT NULL,
    channel     TEXT NOT NULL,   -- 'email' or 'sms'
    reason      TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS od_matches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id         TEXT NOT NULL,
    od_patient_num  TEXT NOT NULL,
    match_method    TEXT NOT NULL,  -- 'email','phone','manual'
    match_confidence TEXT DEFAULT 'high',
    production_amount REAL DEFAULT 0.0,
    procedure_codes TEXT DEFAULT '',  -- JSON array
    last_synced_at  TEXT NOT NULL,
    FOREIGN KEY (lead_id) REFERENCES leads(id)
);

CREATE TABLE IF NOT EXISTS conversion_uploads (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id           TEXT NOT NULL,
    conversion_action TEXT NOT NULL,     -- 'Qualified Lead','Appointment Booked','Treatment Accepted','Treatment Completed'
    gclid             TEXT NOT NULL,
    conversion_time   TEXT NOT NULL,     -- when the conversion happened (ISO timestamp)
    conversion_value  REAL DEFAULT 0.0,  -- dollar value uploaded
    uploaded_at       TEXT DEFAULT '',   -- when we sent it to Google
    status            TEXT DEFAULT 'pending',  -- 'pending','uploaded','failed'
    google_response   TEXT DEFAULT '',   -- response from Google Ads API
    FOREIGN KEY (lead_id) REFERENCES leads(id)
);

CREATE INDEX IF NOT EXISTS idx_conversions_lead ON conversion_uploads(lead_id);
CREATE INDEX IF NOT EXISTS idx_conversions_status ON conversion_uploads(status);
CREATE INDEX IF NOT EXISTS idx_conversions_action ON conversion_uploads(conversion_action);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lead_notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id     TEXT NOT NULL,
    note_text   TEXT NOT NULL,
    author      TEXT DEFAULT 'admin',
    created_at  TEXT NOT NULL,
    FOREIGN KEY (lead_id) REFERENCES leads(id)
);

CREATE INDEX IF NOT EXISTS idx_notes_lead ON lead_notes(lead_id);
CREATE INDEX IF NOT EXISTS idx_notes_created ON lead_notes(created_at);

CREATE TABLE IF NOT EXISTS ga4_cache (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    report_type TEXT NOT NULL,           -- 'full_report' (or specific sub-report name)
    days        INTEGER NOT NULL,        -- date range used (e.g. 30)
    data        TEXT NOT NULL,           -- JSON blob of the GA4 response
    fetched_at  TEXT NOT NULL            -- ISO timestamp when fetched
);

CREATE INDEX IF NOT EXISTS idx_ga4_cache_type ON ga4_cache(report_type, days);

CREATE TABLE IF NOT EXISTS optimizer_memory (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    category    TEXT NOT NULL,   -- 'keyword_override', 'term_classification', 'campaign_rule', 'general'
    key         TEXT NOT NULL,   -- the term or rule name (lowercase for matching)
    value       TEXT NOT NULL,   -- decision: 'negative', 'good_keyword', 'irrelevant', 'never_pause', etc.
    reason      TEXT NOT NULL,   -- human-readable explanation
    campaign    TEXT DEFAULT '',  -- NULL/empty = global; campaign name = scoped to that campaign only
    author      TEXT NOT NULL DEFAULT 'admin',  -- 'admin' or 'ai_agent'
    active      INTEGER NOT NULL DEFAULT 1,     -- 1=active, 0=deactivated (soft delete)
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_optimizer_memory_category ON optimizer_memory(category, active);
CREATE INDEX IF NOT EXISTS idx_optimizer_memory_key ON optimizer_memory(key, active);

CREATE TABLE IF NOT EXISTS gads_keywords_cache (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword_text            TEXT NOT NULL,
    match_type              TEXT DEFAULT '',
    ad_group_name           TEXT DEFAULT '',
    campaign_name           TEXT DEFAULT '',
    impressions             INTEGER DEFAULT 0,
    clicks                  INTEGER DEFAULT 0,
    cost                    REAL DEFAULT 0.0,
    avg_cpc                 REAL DEFAULT 0.0,
    conversions             REAL DEFAULT 0.0,
    quality_score           INTEGER DEFAULT 0,
    creative_quality_score  TEXT DEFAULT '',
    post_click_quality      TEXT DEFAULT '',
    search_predicted_ctr    TEXT DEFAULT '',
    impression_share        REAL DEFAULT 0.0,
    top_impression_pct      REAL DEFAULT 0.0,
    abs_top_impression_pct  REAL DEFAULT 0.0,
    budget_lost_is          REAL DEFAULT 0.0,
    rank_lost_is            REAL DEFAULT 0.0,
    days                    INTEGER DEFAULT 30,
    synced_at               TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_gads_kw_cache_key ON gads_keywords_cache(keyword_text, days);

CREATE TABLE IF NOT EXISTS gads_search_terms_cache (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    search_term     TEXT NOT NULL,
    status          TEXT DEFAULT 'NONE',    -- ADDED / EXCLUDED / NONE
    campaign_name   TEXT DEFAULT '',
    ad_group_name   TEXT DEFAULT '',
    impressions     INTEGER DEFAULT 0,
    clicks          INTEGER DEFAULT 0,
    cost            REAL DEFAULT 0.0,
    conversions     REAL DEFAULT 0.0,
    cpc             REAL DEFAULT 0.0,
    days            INTEGER DEFAULT 30,
    synced_at       TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_gads_st_cache_key ON gads_search_terms_cache(search_term, campaign_name, days);

CREATE TABLE IF NOT EXISTS gads_geo_cache (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    location_name   TEXT NOT NULL,
    location_type   TEXT DEFAULT '',
    campaign_name   TEXT DEFAULT '',
    impressions     INTEGER DEFAULT 0,
    clicks          INTEGER DEFAULT 0,
    cost            REAL DEFAULT 0.0,
    conversions     REAL DEFAULT 0.0,
    cpc             REAL DEFAULT 0.0,
    conversion_rate REAL DEFAULT 0.0,
    days            INTEGER DEFAULT 30,
    synced_at       TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_gads_geo_cache_key ON gads_geo_cache(location_name, campaign_name, days);

CREATE TABLE IF NOT EXISTS gads_schedule_cache (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_type    TEXT NOT NULL,  -- 'hour' / 'day' / 'device'
    segment_value   TEXT NOT NULL,  -- hour 0-23, day name, device name
    impressions     INTEGER DEFAULT 0,
    clicks          INTEGER DEFAULT 0,
    cost            REAL DEFAULT 0.0,
    conversions     REAL DEFAULT 0.0,
    cpc             REAL DEFAULT 0.0,
    conversion_rate REAL DEFAULT 0.0,
    days            INTEGER DEFAULT 30,
    synced_at       TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_gads_schedule_cache_key ON gads_schedule_cache(segment_type, segment_value, days);

CREATE TABLE IF NOT EXISTS communication_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id     TEXT NOT NULL,
    template    TEXT NOT NULL,        -- e.g. 'day1_email','day3_sms'
    channel     TEXT NOT NULL,        -- 'email' or 'sms'
    sent_at     TEXT NOT NULL,
    queue_id    INTEGER DEFAULT NULL, -- follow_up_queue.id, if applicable
    UNIQUE(lead_id, template)
);

CREATE INDEX IF NOT EXISTS idx_comm_log_lead ON communication_log(lead_id);
CREATE INDEX IF NOT EXISTS idx_comm_log_template ON communication_log(template);

CREATE TABLE IF NOT EXISTS deleted_leads (
    lead_id        TEXT PRIMARY KEY,
    email          TEXT DEFAULT '',
    deleted_at     TEXT NOT NULL,
    deleted_by     TEXT DEFAULT 'admin',
    reason         TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_deleted_leads_email ON deleted_leads(email);

CREATE TABLE IF NOT EXISTS conversations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id         TEXT,                          -- NULL = unmatched (no lead found)
    channel         TEXT NOT NULL DEFAULT 'email', -- 'email' (sms in future)
    contact_email   TEXT NOT NULL DEFAULT '',      -- the lead/contact's email address
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    FOREIGN KEY (lead_id) REFERENCES leads(id)
);

CREATE INDEX IF NOT EXISTS idx_conversations_lead ON conversations(lead_id);
CREATE INDEX IF NOT EXISTS idx_conversations_email ON conversations(contact_email);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    direction       TEXT NOT NULL,       -- 'inbound' | 'outbound'
    from_addr       TEXT NOT NULL DEFAULT '',
    subject         TEXT NOT NULL DEFAULT '',
    body            TEXT NOT NULL DEFAULT '',
    message_id      TEXT NOT NULL DEFAULT '',  -- RFC 5322 Message-ID (or synthetic UUID)
    in_reply_to     TEXT NOT NULL DEFAULT '',
    msg_references  TEXT NOT NULL DEFAULT '',  -- renamed from 'references' (SQL reserved)
    received_at     TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id)
);

-- Partial unique index: enforce dedup only when message_id is non-empty
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_msgid_unique
    ON messages(message_id)
    WHERE message_id != '';

CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_messages_received ON messages(received_at);

CREATE TABLE IF NOT EXISTS gads_daily_stats (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    date          TEXT NOT NULL,
    campaign_id   TEXT NOT NULL DEFAULT '',
    campaign_name TEXT DEFAULT '',
    ad_group_id   TEXT NOT NULL DEFAULT '',
    ad_group_name TEXT DEFAULT '',
    impressions   INTEGER DEFAULT 0,
    clicks        INTEGER DEFAULT 0,
    cost_micros   INTEGER DEFAULT 0,
    conversions   REAL DEFAULT 0.0,
    synced_at     TEXT NOT NULL,
    UNIQUE (date, campaign_id, ad_group_id)
);

CREATE INDEX IF NOT EXISTS idx_gads_daily_date ON gads_daily_stats(date);
CREATE INDEX IF NOT EXISTS idx_gads_daily_campaign ON gads_daily_stats(campaign_id, date);

-- ── Phase 1: Google Ads Campaign Management ────────────────────────────────

CREATE TABLE IF NOT EXISTS gads_optimizer_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           TEXT UNIQUE NOT NULL,           -- UUID
    started_at       TEXT NOT NULL,
    completed_at     TEXT DEFAULT '',
    mode             TEXT NOT NULL DEFAULT 'pending_approval',  -- 'pending_approval' | 'errored'
    trigger          TEXT NOT NULL DEFAULT 'scheduler_7am',     -- 'scheduler_7am' | 'admin_manual'
    primary_campaign TEXT DEFAULT '',
    summary_json     TEXT NOT NULL DEFAULT '{}',    -- report['summary'] dict
    report_json      TEXT NOT NULL DEFAULT '{}',    -- full report dict
    actions_pending  INTEGER DEFAULT 0,
    actions_executed INTEGER DEFAULT 0,
    actions_blocked  INTEGER DEFAULT 0,
    actions_errored  INTEGER DEFAULT 0,
    error            TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_runs_started ON gads_optimizer_runs(started_at DESC);

CREATE TABLE IF NOT EXISTS gads_audit_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id         TEXT UNIQUE NOT NULL,          -- UUID
    operation         TEXT NOT NULL,                 -- 'pause_keyword','set_budget','add_keyword',etc.
    entity_type       TEXT NOT NULL,                 -- 'keyword','campaign','ad_group','ad','budget'
    entity_id         TEXT NOT NULL,                 -- resource_name or campaign_id
    entity_name       TEXT NOT NULL,                 -- human-readable name
    before_state_json TEXT NOT NULL DEFAULT '{}',    -- JSON snapshot before change
    after_state_json  TEXT NOT NULL DEFAULT '{}',    -- JSON intended state after change
    executed          INTEGER NOT NULL DEFAULT 0,    -- 0=not executed, 1=executed
    execution_result  TEXT NOT NULL,                 -- 'pending_approval'|'success'|'blocked'|'error'|'rejected'|'expired'
    actor             TEXT NOT NULL DEFAULT 'ai_optimizer',  -- 'admin' | 'ai_optimizer' | 'system'
    reason            TEXT DEFAULT '',               -- why this action was recommended
    error_detail      TEXT DEFAULT '',               -- error message if execution_result='error'
    optimizer_run_id  TEXT DEFAULT '',               -- FK to gads_optimizer_runs.run_id
    approval_by       TEXT DEFAULT '',               -- who approved (admin)
    approved_at       TEXT DEFAULT '',               -- when approved
    created_at        TEXT NOT NULL,
    updated_at        TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_audit_created ON gads_audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_entity  ON gads_audit_log(entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_result  ON gads_audit_log(execution_result);
CREATE INDEX IF NOT EXISTS idx_audit_run     ON gads_audit_log(optimizer_run_id);

CREATE TABLE IF NOT EXISTS gads_spend_guardrails (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id      TEXT NOT NULL UNIQUE,
    campaign_name    TEXT NOT NULL DEFAULT '',
    daily_cap_usd    REAL NOT NULL,                  -- max allowed daily budget in dollars
    is_active        INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

-- ── Phase 3: Ad Creative Tables ───────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS gads_ads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ad_id           TEXT NOT NULL UNIQUE,
    customer_id     TEXT NOT NULL DEFAULT '',   -- Google Ads customer ID (for multi-account safety)
    ad_name         TEXT DEFAULT '',
    ad_group_id     TEXT DEFAULT '',
    ad_group_name   TEXT DEFAULT '',
    campaign_id     TEXT DEFAULT '',
    campaign_name   TEXT DEFAULT '',
    status          TEXT DEFAULT '',            -- ENABLED, PAUSED, REMOVED
    ad_type         TEXT DEFAULT '',            -- RESPONSIVE_SEARCH_AD, EXPANDED_TEXT_AD, etc.
    headline_1      TEXT DEFAULT '',
    headline_2      TEXT DEFAULT '',
    headline_3      TEXT DEFAULT '',
    description_1   TEXT DEFAULT '',
    description_2   TEXT DEFAULT '',
    final_url       TEXT DEFAULT '',
    assets_json     TEXT DEFAULT '[]',          -- full RSA headline/description assets as JSON
    synced_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gads_ads_campaign  ON gads_ads(campaign_id);
CREATE INDEX IF NOT EXISTS idx_gads_ads_ad_group  ON gads_ads(ad_group_id);
CREATE INDEX IF NOT EXISTS idx_gads_ads_status    ON gads_ads(status);

CREATE TABLE IF NOT EXISTS gads_ad_metrics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ad_id       TEXT NOT NULL,
    date        TEXT NOT NULL,
    impressions INTEGER DEFAULT 0,
    clicks      INTEGER DEFAULT 0,
    cost_micros INTEGER DEFAULT 0,
    conversions REAL    DEFAULT 0.0,
    UNIQUE(ad_id, date)
);

CREATE INDEX IF NOT EXISTS idx_gads_ad_metrics_date ON gads_ad_metrics(date);
CREATE INDEX IF NOT EXISTS idx_gads_ad_metrics_ad   ON gads_ad_metrics(ad_id, date);

-- ── Step 10: TCPA Stop Conditions ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS sms_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id     TEXT DEFAULT NULL,             -- NULL if number not matched to a lead
    direction   TEXT NOT NULL,                 -- 'inbound' | 'outbound'
    from_number TEXT NOT NULL DEFAULT '',
    to_number   TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL DEFAULT '',
    twilio_sid  TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL,
    FOREIGN KEY (lead_id) REFERENCES leads(id)
);

CREATE INDEX IF NOT EXISTS idx_sms_messages_lead ON sms_messages(lead_id);
CREATE INDEX IF NOT EXISTS idx_sms_messages_from ON sms_messages(from_number);
CREATE INDEX IF NOT EXISTS idx_sms_messages_received ON sms_messages(received_at DESC);
"""

LIFECYCLE_STAGES = [
    "new",
    "auto_nurture",
    "scheduled",
    "no_show",
    "showed",
    "treatment_presented",
    "treatment_accepted",
    "treatment_completed",
    "cold",
]

# Map old stage names to new ones (for migration & backward compat)
_STAGE_MIGRATION = {
    "engaged": "new",
    "smile_completed": "new",
    "nurturing": "auto_nurture",
    "confirmed": "scheduled",
}

STAGE_ORDER = {s: i for i, s in enumerate(LIFECYCLE_STAGES)}


def _conn() -> sqlite3.Connection:
    settings = get_settings()
    os.makedirs(os.path.dirname(settings.db_path), exist_ok=True)
    conn = sqlite3.connect(settings.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with _conn() as conn:
        conn.executescript(_SCHEMA)
        _migrate(conn)


def _migrate(conn):
    """Add columns that may not exist in older databases."""
    # Get existing columns on leads table
    cursor = conn.execute("PRAGMA table_info(leads)")
    existing_cols = {row[1] for row in cursor.fetchall()}

    new_columns = [
        ("keyword_text",        "TEXT DEFAULT ''"),
        ("search_term",         "TEXT DEFAULT ''"),
        ("ad_group_name",       "TEXT DEFAULT ''"),
        ("ad_group_id",         "TEXT DEFAULT ''"),
        ("ad_id",               "TEXT DEFAULT ''"),
        ("ad_name",             "TEXT DEFAULT ''"),
        ("campaign_name",       "TEXT DEFAULT ''"),
        ("campaign_id",         "TEXT DEFAULT ''"),
        ("click_cost",          "REAL DEFAULT 0.0"),
        ("gads_synced_at",      "TEXT DEFAULT ''"),
        # New tracking fields
        ("utm_content",         "TEXT DEFAULT ''"),
        ("landing_url",         "TEXT DEFAULT ''"),
        # New financial fields
        ("treatment_plan_value", "REAL DEFAULT 0.0"),
        ("attributed_income",   "REAL DEFAULT 0.0"),
        # New appointment fields
        ("appointment_date",    "TEXT DEFAULT ''"),
        ("appointment_status",  "TEXT DEFAULT ''"),
        ("no_show_count",       "INTEGER DEFAULT 0"),
        ("last_staff_contact_at", "TEXT DEFAULT ''"),
        # Stage timestamps
        ("auto_nurture_at",     "TEXT DEFAULT ''"),
        ("scheduled_at",        "TEXT DEFAULT ''"),
        ("no_show_at",          "TEXT DEFAULT ''"),
        ("showed_at",           "TEXT DEFAULT ''"),
        ("tx_presented_at",     "TEXT DEFAULT ''"),
        ("tx_accepted_at",      "TEXT DEFAULT ''"),
        ("tx_completed_at",     "TEXT DEFAULT ''"),
        ("cold_at",             "TEXT DEFAULT ''"),
        # GCS blob name for re-signing fresh signed URLs at email-send time
        ("smile_blob_name",     "TEXT DEFAULT ''"),
    ]

    for col_name, col_type in new_columns:
        if col_name not in existing_cols:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {col_name} {col_type}")

    # Migrate old stage names to new ones
    for old_stage, new_stage in _STAGE_MIGRATION.items():
        conn.execute("UPDATE leads SET stage=? WHERE stage=?", (new_stage, old_stage))

    # Create conversion_uploads table if not exists (already in _SCHEMA but safe to re-check)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversion_uploads (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id           TEXT NOT NULL,
            conversion_action TEXT NOT NULL,
            gclid             TEXT NOT NULL,
            conversion_time   TEXT NOT NULL,
            conversion_value  REAL DEFAULT 0.0,
            uploaded_at       TEXT DEFAULT '',
            status            TEXT DEFAULT 'pending',
            google_response   TEXT DEFAULT '',
            FOREIGN KEY (lead_id) REFERENCES leads(id)
        )
    """)

    # Create indexes if missing
    conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_gclid ON leads(gclid)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conversions_lead ON conversion_uploads(lead_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conversions_status ON conversion_uploads(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conversions_action ON conversion_uploads(conversion_action)")

    # Create lead_notes table if not exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lead_notes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id     TEXT NOT NULL,
            note_text   TEXT NOT NULL,
            author      TEXT DEFAULT 'admin',
            created_at  TEXT NOT NULL,
            FOREIGN KEY (lead_id) REFERENCES leads(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_lead ON lead_notes(lead_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_created ON lead_notes(created_at)")

    # GA4 cache table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ga4_cache (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            report_type TEXT NOT NULL,
            days        INTEGER NOT NULL,
            data        TEXT NOT NULL,
            fetched_at  TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ga4_cache_type ON ga4_cache(report_type, days)")

    # Optimizer memory table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS optimizer_memory (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            category    TEXT NOT NULL,
            key         TEXT NOT NULL,
            value       TEXT NOT NULL,
            reason      TEXT NOT NULL,
            campaign    TEXT DEFAULT '',
            author      TEXT NOT NULL DEFAULT 'admin',
            active      INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_optimizer_memory_category ON optimizer_memory(category, active)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_optimizer_memory_key ON optimizer_memory(key, active)")
    # Add campaign column to existing optimizer_memory tables (migration)
    try:
        conn.execute("ALTER TABLE optimizer_memory ADD COLUMN campaign TEXT DEFAULT ''")
    except Exception:
        pass  # column already exists
    conn.execute("CREATE INDEX IF NOT EXISTS idx_optimizer_memory_campaign ON optimizer_memory(campaign, active)")

    # Google Ads keywords cache
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gads_keywords_cache (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword_text    TEXT NOT NULL,
            match_type      TEXT DEFAULT '',
            ad_group_name   TEXT DEFAULT '',
            campaign_name   TEXT DEFAULT '',
            impressions     INTEGER DEFAULT 0,
            clicks          INTEGER DEFAULT 0,
            cost            REAL DEFAULT 0.0,
            avg_cpc         REAL DEFAULT 0.0,
            conversions     REAL DEFAULT 0.0,
            days            INTEGER DEFAULT 30,
            synced_at       TEXT NOT NULL
        )
    """)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_gads_kw_cache_key ON gads_keywords_cache(keyword_text, days)")

    # Add quality score and impression share columns to keywords cache (migration)
    kw_cache_cols = {row[1] for row in conn.execute("PRAGMA table_info(gads_keywords_cache)").fetchall()}
    kw_new_cols = [
        ("quality_score",           "INTEGER DEFAULT 0"),
        ("creative_quality_score",  "TEXT DEFAULT ''"),    # BELOW_AVERAGE / AVERAGE / ABOVE_AVERAGE
        ("post_click_quality",      "TEXT DEFAULT ''"),
        ("search_predicted_ctr",    "TEXT DEFAULT ''"),
        ("impression_share",        "REAL DEFAULT 0.0"),   # 0.0–1.0 (fraction, not percent)
        ("top_impression_pct",      "REAL DEFAULT 0.0"),
        ("abs_top_impression_pct",  "REAL DEFAULT 0.0"),
        ("budget_lost_is",          "REAL DEFAULT 0.0"),   # lost IS due to budget
        ("rank_lost_is",            "REAL DEFAULT 0.0"),   # lost IS due to rank/quality
    ]
    for col_name, col_type in kw_new_cols:
        if col_name not in kw_cache_cols:
            try:
                conn.execute(f"ALTER TABLE gads_keywords_cache ADD COLUMN {col_name} {col_type}")
            except Exception:
                pass

    # Add sent_by and message_type columns to messages table (Step 8 migration)
    msg_cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    for col_name, col_def in [("sent_by", "TEXT DEFAULT NULL"), ("message_type", "TEXT DEFAULT 'auto'")]:
        if col_name not in msg_cols:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {col_name} {col_def}")

    # Search terms cache table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gads_search_terms_cache (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            search_term     TEXT NOT NULL,
            status          TEXT DEFAULT 'NONE',
            campaign_name   TEXT DEFAULT '',
            ad_group_name   TEXT DEFAULT '',
            impressions     INTEGER DEFAULT 0,
            clicks          INTEGER DEFAULT 0,
            cost            REAL DEFAULT 0.0,
            conversions     REAL DEFAULT 0.0,
            cpc             REAL DEFAULT 0.0,
            days            INTEGER DEFAULT 30,
            synced_at       TEXT NOT NULL
        )
    """)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_gads_st_cache_key ON gads_search_terms_cache(search_term, campaign_name, days)")

    # Geo performance cache
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gads_geo_cache (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            location_name   TEXT NOT NULL,
            location_type   TEXT DEFAULT '',
            campaign_name   TEXT DEFAULT '',
            impressions     INTEGER DEFAULT 0,
            clicks          INTEGER DEFAULT 0,
            cost            REAL DEFAULT 0.0,
            conversions     REAL DEFAULT 0.0,
            cpc             REAL DEFAULT 0.0,
            conversion_rate REAL DEFAULT 0.0,
            days            INTEGER DEFAULT 30,
            synced_at       TEXT NOT NULL
        )
    """)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_gads_geo_cache_key ON gads_geo_cache(location_name, campaign_name, days)")

    # Schedule / device / hour-of-day cache
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gads_schedule_cache (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            segment_type    TEXT NOT NULL,
            segment_value   TEXT NOT NULL,
            impressions     INTEGER DEFAULT 0,
            clicks          INTEGER DEFAULT 0,
            cost            REAL DEFAULT 0.0,
            conversions     REAL DEFAULT 0.0,
            cpc             REAL DEFAULT 0.0,
            conversion_rate REAL DEFAULT 0.0,
            days            INTEGER DEFAULT 30,
            synced_at       TEXT NOT NULL
        )
    """)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_gads_schedule_cache_key ON gads_schedule_cache(segment_type, segment_value, days)")

    # ── Workflows + WorkflowSteps tables (Step 9 — campaign-specific sequences) ──
    conn.execute("""
        CREATE TABLE IF NOT EXISTS workflows (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            campaign_tag TEXT NOT NULL DEFAULT '',  -- '' = default; 'all_on_x', 'aligners', etc.
            description TEXT DEFAULT '',
            active      INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        )
    """)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_workflows_campaign_tag ON workflows(campaign_tag)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS workflow_steps (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_id  INTEGER NOT NULL,
            sequence_day INTEGER NOT NULL,
            channel      TEXT NOT NULL,         -- 'email' or 'sms'
            template_name TEXT NOT NULL,        -- unique slug, e.g. 'default_day1_email'
            subject      TEXT DEFAULT '',       -- email subject (blank for SMS)
            body         TEXT NOT NULL DEFAULT '',
            terminal     INTEGER NOT NULL DEFAULT 0,  -- 1 = side-effect step (marks cold, deletes GCS)
            active       INTEGER NOT NULL DEFAULT 1,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            FOREIGN KEY (workflow_id) REFERENCES workflows(id),
            UNIQUE(template_name)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_workflow_steps_workflow ON workflow_steps(workflow_id, sequence_day)")

    # Add workflow_step_id column to follow_up_queue (Step 9 migration)
    fq_cols = {row[1] for row in conn.execute("PRAGMA table_info(follow_up_queue)").fetchall()}
    if "workflow_step_id" not in fq_cols:
        conn.execute("ALTER TABLE follow_up_queue ADD COLUMN workflow_step_id INTEGER DEFAULT NULL")

    # Add UNIQUE(lead_id, template) to follow_up_queue if not present
    # SQLite doesn't support adding UNIQUE constraints via ALTER TABLE — create a new index instead
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_follow_up_queue_lead_template
        ON follow_up_queue(lead_id, template)
    """)

    # Seed default workflow if no workflows exist
    existing_workflows = conn.execute("SELECT COUNT(*) FROM workflows").fetchone()[0]
    if existing_workflows == 0:
        _seed_default_workflow(conn)

    # ── Conversations + Messages tables (Step 5 — bi-directional email inbox) ──
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id         TEXT,
            channel         TEXT NOT NULL DEFAULT 'email',
            contact_email   TEXT NOT NULL DEFAULT '',
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            FOREIGN KEY (lead_id) REFERENCES leads(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_lead ON conversations(lead_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_email ON conversations(contact_email)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            direction       TEXT NOT NULL,
            from_addr       TEXT NOT NULL DEFAULT '',
            subject         TEXT NOT NULL DEFAULT '',
            body            TEXT NOT NULL DEFAULT '',
            message_id      TEXT NOT NULL DEFAULT '',
            in_reply_to     TEXT NOT NULL DEFAULT '',
            msg_references  TEXT NOT NULL DEFAULT '',
            received_at     TEXT NOT NULL,
            FOREIGN KEY (conversation_id) REFERENCES conversations(id)
        )
    """)
    # Partial unique index for dedup — only enforce when message_id is non-empty
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_msgid_unique
        ON messages(message_id)
        WHERE message_id != ''
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_received ON messages(received_at)")

    # Google Ads daily stats — ad-group-level metrics for trend charts
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gads_daily_stats (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            date          TEXT NOT NULL,
            campaign_id   TEXT NOT NULL DEFAULT '',
            campaign_name TEXT DEFAULT '',
            ad_group_id   TEXT NOT NULL DEFAULT '',
            ad_group_name TEXT DEFAULT '',
            impressions   INTEGER DEFAULT 0,
            clicks        INTEGER DEFAULT 0,
            cost_micros   INTEGER DEFAULT 0,
            conversions   REAL DEFAULT 0.0,
            synced_at     TEXT NOT NULL,
            UNIQUE (date, campaign_id, ad_group_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gads_daily_date ON gads_daily_stats(date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gads_daily_campaign ON gads_daily_stats(campaign_id, date)")

    # ── Phase 1: Campaign management — audit log + guardrails + optimizer runs ──
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gads_optimizer_runs (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id           TEXT UNIQUE NOT NULL,
            started_at       TEXT NOT NULL,
            completed_at     TEXT DEFAULT '',
            mode             TEXT NOT NULL DEFAULT 'pending_approval',
            trigger          TEXT NOT NULL DEFAULT 'scheduler_7am',
            primary_campaign TEXT DEFAULT '',
            summary_json     TEXT NOT NULL DEFAULT '{}',
            report_json      TEXT NOT NULL DEFAULT '{}',
            actions_pending  INTEGER DEFAULT 0,
            actions_executed INTEGER DEFAULT 0,
            actions_blocked  INTEGER DEFAULT 0,
            actions_errored  INTEGER DEFAULT 0,
            error            TEXT DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_started ON gads_optimizer_runs(started_at DESC)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS gads_audit_log (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            action_id         TEXT UNIQUE NOT NULL,
            operation         TEXT NOT NULL,
            entity_type       TEXT NOT NULL,
            entity_id         TEXT NOT NULL,
            entity_name       TEXT NOT NULL,
            before_state_json TEXT NOT NULL DEFAULT '{}',
            after_state_json  TEXT NOT NULL DEFAULT '{}',
            executed          INTEGER NOT NULL DEFAULT 0,
            execution_result  TEXT NOT NULL,
            actor             TEXT NOT NULL DEFAULT 'ai_optimizer',
            reason            TEXT DEFAULT '',
            error_detail      TEXT DEFAULT '',
            optimizer_run_id  TEXT DEFAULT '',
            approval_by       TEXT DEFAULT '',
            approved_at       TEXT DEFAULT '',
            created_at        TEXT NOT NULL,
            updated_at        TEXT DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_created ON gads_audit_log(created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_entity  ON gads_audit_log(entity_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_result  ON gads_audit_log(execution_result)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_run     ON gads_audit_log(optimizer_run_id)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS gads_spend_guardrails (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id      TEXT NOT NULL UNIQUE,
            campaign_name    TEXT NOT NULL DEFAULT '',
            daily_cap_usd    REAL NOT NULL,
            is_active        INTEGER NOT NULL DEFAULT 1,
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL
        )
    """)

    # ── Phase 3: Ad Creative Tables ───────────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gads_ads (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ad_id           TEXT NOT NULL UNIQUE,
            customer_id     TEXT NOT NULL DEFAULT '',
            ad_name         TEXT DEFAULT '',
            ad_group_id     TEXT DEFAULT '',
            ad_group_name   TEXT DEFAULT '',
            campaign_id     TEXT DEFAULT '',
            campaign_name   TEXT DEFAULT '',
            status          TEXT DEFAULT '',
            ad_type         TEXT DEFAULT '',
            headline_1      TEXT DEFAULT '',
            headline_2      TEXT DEFAULT '',
            headline_3      TEXT DEFAULT '',
            description_1   TEXT DEFAULT '',
            description_2   TEXT DEFAULT '',
            final_url       TEXT DEFAULT '',
            assets_json     TEXT DEFAULT '[]',
            synced_at       TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gads_ads_campaign ON gads_ads(campaign_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gads_ads_ad_group ON gads_ads(ad_group_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gads_ads_status   ON gads_ads(status)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS gads_ad_metrics (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ad_id       TEXT NOT NULL,
            date        TEXT NOT NULL,
            impressions INTEGER DEFAULT 0,
            clicks      INTEGER DEFAULT 0,
            cost_micros INTEGER DEFAULT 0,
            conversions REAL    DEFAULT 0.0,
            UNIQUE(ad_id, date)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gads_ad_metrics_date ON gads_ad_metrics(date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gads_ad_metrics_ad   ON gads_ad_metrics(ad_id, date)")

    # ── Step 10b: Phone hash normalization backfill ────────────────────────────
    # Re-hash all leads using _hash_phone() (last-10-digits) so Twilio E.164
    # lookups resolve correctly. Safe: 10-digit stored phones produce the same
    # hash as before; 11-digit stored phones get corrected.
    try:
        rows = conn.execute("SELECT id, phone FROM leads WHERE phone != ''").fetchall()
        for row in rows:
            corrected = _hash_phone(row["phone"])
            conn.execute("UPDATE leads SET phone_hash=? WHERE id=?", (corrected, row["id"]))
        if rows:
            print(f"[db] Phone hash backfill: normalized {len(rows)} leads to last-10-digits")
    except Exception as _e:
        print(f"[db] Phone hash backfill failed (non-fatal): {_e}")

    # ── Step 10: TCPA Stop Conditions ─────────────────────────────────────────
    # New columns on leads table
    leads_cols = {row[1] for row in conn.execute("PRAGMA table_info(leads)").fetchall()}
    step10_lead_cols = [
        ("dnd_reason",    "TEXT DEFAULT ''"),
        ("dnd_set_at",    "TEXT DEFAULT ''"),
        ("paused_at",     "TEXT DEFAULT ''"),
        ("paused_reason", "TEXT DEFAULT ''"),
        ("paused_until",  "TEXT DEFAULT ''"),
    ]
    for col_name, col_def in step10_lead_cols:
        if col_name not in leads_cols:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {col_name} {col_def}")

    # New columns on follow_up_queue
    fq_cols2 = {row[1] for row in conn.execute("PRAGMA table_info(follow_up_queue)").fetchall()}
    step10_fq_cols = [
        ("cancelled_at",         "TEXT DEFAULT ''"),
        ("cancellation_reason",  "TEXT DEFAULT ''"),
    ]
    for col_name, col_def in step10_fq_cols:
        if col_name not in fq_cols2:
            conn.execute(f"ALTER TABLE follow_up_queue ADD COLUMN {col_name} {col_def}")

    # sms_messages table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sms_messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id     TEXT DEFAULT NULL,
            direction   TEXT NOT NULL,
            from_number TEXT NOT NULL DEFAULT '',
            to_number   TEXT NOT NULL DEFAULT '',
            body        TEXT NOT NULL DEFAULT '',
            twilio_sid  TEXT NOT NULL DEFAULT '',
            received_at TEXT NOT NULL,
            FOREIGN KEY (lead_id) REFERENCES leads(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sms_messages_lead ON sms_messages(lead_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sms_messages_from ON sms_messages(from_number)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sms_messages_received ON sms_messages(received_at DESC)")

    # Seed default memories if table is empty
    existing = conn.execute("SELECT COUNT(*) FROM optimizer_memory").fetchone()[0]
    if existing == 0:
        now = datetime.now(timezone.utc).isoformat()
        # (category, key, value, reason, campaign, author)
        # campaign='' means global (applies to all campaigns)
        seeds = [
            ('keyword_override', 'all on 4 dental implants', 'never_pause',
             'Core campaign keyword — zero leads is gclid attribution gap, not actual performance',
             '', 'admin'),
            ('term_classification', 'free gingival graft', 'irrelevant',
             'Periodontal procedure — not a buyer for implants/cosmetics. NOTE: valid for gum grafting campaigns.',
             'grafton_nxtsmile_all_on_x', 'admin'),
            ('term_classification', 'free connective tissue graft', 'irrelevant',
             'Periodontal procedure — not relevant to implant or cosmetic campaigns.',
             'grafton_nxtsmile_all_on_x', 'admin'),
            ('general', 'attribution_note', 'gclid_tracking_started_apr_2026',
             'All leads before April 2026 have no keyword attribution. Zero leads on a keyword before this date is not real data.',
             '', 'admin'),
        ]
        for category, key, value, reason, campaign, author in seeds:
            conn.execute(
                "INSERT INTO optimizer_memory (category, key, value, reason, campaign, author, active, created_at, updated_at) VALUES (?,?,?,?,?,?,1,?,?)",
                (category, key, value, reason, campaign, author, now, now)
            )


def _seed_default_workflow(conn):
    """Seed the default nXtsmile follow-up workflow into the DB."""
    from config import get_settings
    settings = get_settings()
    booking_link = getattr(settings, "booking_url", "https://graftondentalcare.com/book")
    office_phone = getattr(settings, "office_phone", "(508) 839-9900")

    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO workflows (name, campaign_tag, description, active, created_at, updated_at) VALUES (?,?,?,1,?,?)",
        ("nXtsmile Default", "", "Default 30-day nurture sequence for all leads", now, now),
    )
    wf_id = cur.lastrowid

    steps = [
        (1,  "email", "default_day1_email",  "Your Smile Preview Is Ready 🦷",
         "Hi {first_name},\n\nThank you for using our nXtsmile preview tool! We hope you love what you saw.\n\n"
         "Dr. Gupta and the team at Grafton Dental Care are ready to help make that smile a reality. "
         "Your free consultation is completely pressure-free — we'll review your goals and walk you through your options.\n\n"
         f"Book online: {booking_link}\nOr call us: {office_phone}\n\n"
         "We look forward to meeting you!\n\n— Dr. Gupta's Team at Grafton Dental Care\n\n"
         "To unsubscribe: {unsub_url}", False),
        (3,  "sms",   "default_day3_sms",    "",
         f"Hi {{first_name}}, it's nXtsmile at Grafton Dental Care 😊 Did you get a chance to look at your smile preview? "
         f"We'd love to help make it a reality. Book your free consultation: {booking_link} "
         f"or call us at {office_phone}. - Dr. Gupta's Team\nReply STOP to opt out.", False),
        (7,  "email", "default_day7_email",  "Still thinking about your new smile?",
         "Hi {first_name},\n\nJust checking in! Your smile preview is still saved and Dr. Gupta would love to "
         "chat about how we can help. Whether you're ready to schedule or just have questions, we're here.\n\n"
         f"Book your free consult: {booking_link}\nOr reply to this email.\n\n"
         "— Dr. Gupta's Team\n\nTo unsubscribe: {unsub_url}", False),
        (14, "email", "default_day14_email", "We saved a spot for you 🦷",
         "Hi {first_name},\n\nWe know life gets busy! We just wanted to remind you that your free smile consultation "
         "at Grafton Dental Care is just a click away.\n\n"
         "Our patients consistently tell us their only regret is waiting so long to come in.\n\n"
         f"Book now: {booking_link} | Call: {office_phone}\n\n"
         "— Dr. Gupta's Team\n\nTo unsubscribe: {unsub_url}", False),
        (21, "sms",   "default_day21_sms",   "",
         f"Hi {{first_name}}, just checking in from nXtsmile 😊 Life gets busy, but your dream smile is still waiting. "
         f"Your free consultation with Dr. Gupta is just a call away — {office_phone} or book online: {booking_link}. "
         f"We're here whenever you're ready!\nReply STOP to opt out.", False),
        (30, "email", "default_day30_cold",  "Last check-in from Grafton Dental Care",
         "Hi {first_name},\n\nThis will be our last outreach for now — we don't want to crowd your inbox! "
         "But if you ever decide you're ready to transform your smile, Dr. Gupta and the team are here.\n\n"
         f"Book anytime: {booking_link} | {office_phone}\n\n"
         "Wishing you all the best!\n\n— Dr. Gupta's Team at Grafton Dental Care\n\nTo unsubscribe: {unsub_url}", True),
    ]

    for day, channel, template_name, subject, body, terminal in steps:
        conn.execute(
            "INSERT INTO workflow_steps (workflow_id, sequence_day, channel, template_name, subject, body, terminal, active, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,1,?,?)",
            (wf_id, day, channel, template_name, subject, body, 1 if terminal else 0, now, now),
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()


def _hash_phone(phone: str) -> str:
    """Hash the last 10 digits of a phone number.
    Normalizes E.164 (+15083184477) and local (5083184477) to same hash.
    Uses _hash() internally for consistency with the rest of the codebase.
    """
    digits = "".join(c for c in phone if c.isdigit())
    normalized = digits[-10:] if len(digits) >= 10 else digits
    return _hash(normalized)


# ─── Leads ───────────────────────────────────────────────────────────────────

def upsert_lead(data: dict) -> dict:
    """Insert or update a lead. Returns the full lead row."""
    now = _now()
    lead_id = data.get("id") or data.get("lead_id")
    if not lead_id:
        raise ValueError("lead id is required")

    phone_raw = data.get("phone", "")
    email_raw = data.get("email", "")
    phone_digits = "".join(c for c in phone_raw if c.isdigit())

    with _conn() as conn:
        existing = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if existing:
            # Update non-empty fields only
            fields = []
            values = []
            for col in ["first_name","last_name","email","phone","goals","gclid",
                        "fbclid","msclkid","utm_source","utm_medium","utm_campaign",
                        "utm_term","utm_content","landing_url",
                        "smile_image_url","smile_blob_name","smile_generated_at","source","notes",
                        "booking_id","od_patient_num","attributed_production",
                        "treatment_plan_value","attributed_income",
                        "appointment_date","appointment_status","no_show_count",
                        "keyword_text","search_term","ad_group_name","ad_group_id",
                        "ad_id","ad_name","campaign_name","campaign_id",
                        "click_cost","gads_synced_at"]:
                if data.get(col) not in (None, ""):
                    fields.append(f"{col}=?")
                    values.append(data[col])
            if phone_raw:
                fields += ["phone_hash=?"]
                values += [_hash_phone(phone_raw) if phone_raw else ""]
            if email_raw:
                fields += ["email_hash=?"]
                values += [_hash(email_raw)]
            fields.append("updated_at=?")
            values.append(now)
            values.append(lead_id)
            conn.execute(f"UPDATE leads SET {', '.join(fields)} WHERE id=?", values)
        else:
            conn.execute("""
                INSERT INTO leads (id, created_at, updated_at, source, stage,
                    first_name, last_name, email, phone, phone_hash, email_hash,
                    goals, gclid, fbclid, msclkid, utm_source, utm_medium,
                    utm_campaign, utm_term, utm_content, landing_url,
                    smile_image_url, smile_blob_name, notes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                lead_id, data.get("created_at", now), now,
                data.get("source", "unknown"), data.get("stage", "new"),
                data.get("first_name", ""), data.get("last_name", ""),
                email_raw, phone_raw,
                _hash_phone(phone_raw) if phone_raw else "",
                _hash(email_raw) if email_raw else "",
                json.dumps(data.get("goals", [])) if isinstance(data.get("goals"), list) else data.get("goals", ""),
                data.get("gclid", ""), data.get("fbclid", ""), data.get("msclkid", ""),
                data.get("utm_source", ""), data.get("utm_medium", ""),
                data.get("utm_campaign", ""), data.get("utm_term", ""),
                data.get("utm_content", ""), data.get("landing_url", ""),
                data.get("smile_image_url", ""), data.get("smile_blob_name", ""),
                data.get("notes", ""),
            ))
        return dict(conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())


def get_lead(lead_id: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        return dict(row) if row else None


def get_lead_by_email(email: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM leads WHERE email=? COLLATE NOCASE ORDER BY created_at DESC LIMIT 1", (email,)).fetchone()
        return dict(row) if row else None


# Map stage name to its timestamp column
_STAGE_TIMESTAMP_COL = {
    "auto_nurture":        "auto_nurture_at",
    "scheduled":           "scheduled_at",
    "no_show":             "no_show_at",
    "showed":              "showed_at",
    "treatment_presented": "tx_presented_at",
    "treatment_accepted":  "tx_accepted_at",
    "treatment_completed": "tx_completed_at",
    "cold":                "cold_at",
}


def update_stage(lead_id: str, new_stage: str, source: str = "system", detail: str = "") -> dict:
    """Advance a lead's lifecycle stage (never goes backwards, except no_show is special)."""
    lead = get_lead(lead_id)
    if not lead:
        raise ValueError(f"Lead {lead_id} not found")

    old_stage = lead["stage"]
    # no_show can come from scheduled; cold can come from anywhere
    if new_stage == "no_show" and old_stage in ("scheduled",):
        pass  # allow
    elif STAGE_ORDER.get(new_stage, 0) <= STAGE_ORDER.get(old_stage, 0) and new_stage != "cold":
        return lead  # Don't go backwards

    now = _now()
    # Auto-populate stage timestamp
    ts_col = _STAGE_TIMESTAMP_COL.get(new_stage)
    with _conn() as conn:
        if ts_col:
            conn.execute(f"UPDATE leads SET stage=?, {ts_col}=?, updated_at=? WHERE id=?",
                         (new_stage, now, now, lead_id))
        else:
            conn.execute("UPDATE leads SET stage=?, updated_at=? WHERE id=?", (new_stage, now, lead_id))
    add_event(lead_id, "stage_changed", stage_from=old_stage, stage_to=new_stage, source=source, detail=detail)
    return get_lead(lead_id)


def get_all_leads(stage: str = None, limit: int = 200) -> list:
    with _conn() as conn:
        if stage:
            rows = conn.execute("SELECT * FROM leads WHERE stage=? ORDER BY updated_at DESC LIMIT ?", (stage, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM leads ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


# ─── Events ──────────────────────────────────────────────────────────────────

def add_event(lead_id: str, event_type: str, stage_from: str = "", stage_to: str = "",
              source: str = "system", detail: str = "") -> dict:
    now = _now()
    with _conn() as conn:
        conn.execute("""
            INSERT INTO lifecycle_events (lead_id, event_type, stage_from, stage_to, detail, source, created_at)
            VALUES (?,?,?,?,?,?,?)
        """, (lead_id, event_type, stage_from, stage_to, detail, source, now))
    return {"lead_id": lead_id, "event_type": event_type, "created_at": now}


def get_events(lead_id: str) -> list:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM lifecycle_events WHERE lead_id=? ORDER BY created_at ASC", (lead_id,)).fetchall()
        return [dict(r) for r in rows]


# ─── Workflows ───────────────────────────────────────────────────────────────

def _normalize_campaign_tag(tag: str) -> str:
    """Lower + strip campaign tag for consistent lookup."""
    return (tag or "").strip().lower()


def _get_workflow_steps_for_lead(conn, lead: dict) -> list:
    """Return active workflow steps for a lead.
    Resolves campaign tag from utm_campaign / campaign_name, then falls back to
    the default workflow (campaign_tag='').
    """
    raw_tag = lead.get("utm_campaign") or lead.get("campaign_name") or ""
    tag = _normalize_campaign_tag(raw_tag)

    # Try campaign-specific workflow first (if tag is non-empty)
    if tag:
        row = conn.execute(
            "SELECT id FROM workflows WHERE campaign_tag=? AND active=1 LIMIT 1", (tag,)
        ).fetchone()
        if row:
            steps = conn.execute(
                "SELECT * FROM workflow_steps WHERE workflow_id=? AND active=1 ORDER BY sequence_day",
                (row[0],)
            ).fetchall()
            if steps:
                return [dict(s) for s in steps]

    # Fall back to default workflow (campaign_tag='')
    row = conn.execute(
        "SELECT id FROM workflows WHERE campaign_tag='' AND active=1 LIMIT 1"
    ).fetchone()
    if not row:
        return []
    steps = conn.execute(
        "SELECT * FROM workflow_steps WHERE workflow_id=? AND active=1 ORDER BY sequence_day",
        (row[0],)
    ).fetchall()
    return [dict(s) for s in steps]


def get_all_workflows() -> list:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM workflows ORDER BY campaign_tag").fetchall()
        return [dict(r) for r in rows]


def get_workflow(workflow_id: int) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
        return dict(row) if row else None


def get_workflow_steps(workflow_id: int) -> list:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM workflow_steps WHERE workflow_id=? ORDER BY sequence_day",
            (workflow_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_workflow_step(step_id: int) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM workflow_steps WHERE id=?", (step_id,)).fetchone()
        return dict(row) if row else None


def get_workflow_step_by_template(template_name: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM workflow_steps WHERE template_name=? LIMIT 1", (template_name,)
        ).fetchone()
        return dict(row) if row else None


def upsert_workflow(workflow_id: Optional[int], name: str, campaign_tag: str,
                    description: str = "") -> dict:
    now = _now()
    tag = _normalize_campaign_tag(campaign_tag)
    with _conn() as conn:
        if workflow_id:
            conn.execute(
                "UPDATE workflows SET name=?, campaign_tag=?, description=?, updated_at=? WHERE id=?",
                (name, tag, description, now, workflow_id)
            )
            return dict(conn.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone())
        else:
            cur = conn.execute(
                "INSERT INTO workflows (name, campaign_tag, description, active, created_at, updated_at) "
                "VALUES (?,?,?,1,?,?)",
                (name, tag, description, now, now)
            )
            return dict(conn.execute("SELECT * FROM workflows WHERE id=?", (cur.lastrowid,)).fetchone())


def upsert_workflow_step(step_id: Optional[int], workflow_id: int, sequence_day: int,
                         channel: str, template_name: str, subject: str, body: str,
                         terminal: bool = False) -> dict:
    now = _now()
    with _conn() as conn:
        if step_id:
            conn.execute(
                "UPDATE workflow_steps SET workflow_id=?, sequence_day=?, channel=?, template_name=?, "
                "subject=?, body=?, terminal=?, updated_at=? WHERE id=?",
                (workflow_id, sequence_day, channel, template_name, subject, body,
                 1 if terminal else 0, now, step_id)
            )
            return dict(conn.execute("SELECT * FROM workflow_steps WHERE id=?", (step_id,)).fetchone())
        else:
            cur = conn.execute(
                "INSERT INTO workflow_steps (workflow_id, sequence_day, channel, template_name, subject, body, "
                "terminal, active, created_at, updated_at) VALUES (?,?,?,?,?,?,?,1,?,?)",
                (workflow_id, sequence_day, channel, template_name, subject, body,
                 1 if terminal else 0, now, now)
            )
            return dict(conn.execute("SELECT * FROM workflow_steps WHERE id=?", (cur.lastrowid,)).fetchone())


def delete_workflow_step(step_id: int) -> bool:
    with _conn() as conn:
        conn.execute("DELETE FROM workflow_steps WHERE id=?", (step_id,))
        return True


# ─── App Settings (persistent key/value store) ───────────────────────────────

def get_setting(key: str, default: str = "") -> str:
    with _conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default


def save_setting(key: str, value: str):
    with _conn() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))


def get_od_settings() -> dict:
    """Return OD connection settings from DB, falling back to env/config defaults."""
    from config import get_settings
    cfg = get_settings()
    return {
        "od_db_host":       get_setting("od_db_host")       or cfg.od_db_host,
        "od_db_port":       int(get_setting("od_db_port") or cfg.od_db_port),
        "od_db_user":       get_setting("od_db_user")       or cfg.od_db_user,
        "od_db_password":   get_setting("od_db_password")   or cfg.od_db_password,
        "od_db_name":       get_setting("od_db_name")       or cfg.od_db_name,
        "od_api_base":      get_setting("od_api_base")      or cfg.od_api_base,
        "od_developer_key": get_setting("od_developer_key") or cfg.od_developer_key,
        "od_customer_key":  get_setting("od_customer_key")  or cfg.od_customer_key,
    }


def delete_workflow(workflow_id: int) -> bool:
    with _conn() as conn:
        conn.execute("DELETE FROM workflow_steps WHERE workflow_id=?", (workflow_id,))
        conn.execute("DELETE FROM workflows WHERE id=?", (workflow_id,))
        return True


# ─── Follow-up Queue ─────────────────────────────────────────────────────────

def enqueue_follow_ups(lead: dict, created_at: str):
    """Schedule the follow-up sequence for a lead using their campaign workflow.

    Reads steps from workflow_steps DB. Falls back to default workflow if no
    campaign-specific workflow exists. Uses INSERT OR IGNORE to be idempotent.
    ``lead`` must be the full lead dict (needs id, utm_campaign, campaign_name).
    """
    from datetime import timedelta
    import dateutil.parser

    lead_id = lead["id"]

    try:
        base = dateutil.parser.parse(created_at)
        # Ensure timezone-aware for consistent arithmetic
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)
    except Exception:
        base = datetime.now(timezone.utc)

    with _conn() as conn:
        steps = _get_workflow_steps_for_lead(conn, lead)
        if not steps:
            return  # No workflow configured — nothing to enqueue

        for step in steps:
            send_at = (base + timedelta(days=step["sequence_day"])).isoformat()
            conn.execute("""
                INSERT OR IGNORE INTO follow_up_queue
                    (lead_id, sequence_day, channel, template, scheduled_at, status, workflow_step_id)
                VALUES (?,?,?,?,?,'pending',?)
            """, (lead_id, step["sequence_day"], step["channel"],
                  step["template_name"], send_at, step["id"]))


def get_due_follow_ups() -> list:
    """Return all pending follow-ups that are due now."""
    now = _now()
    with _conn() as conn:
        rows = conn.execute("""
            SELECT fq.*, l.email, l.phone, l.first_name, l.last_name,
                   l.goals, l.smile_image_url, l.smile_blob_name, l.stage,
                   l.unsubscribed_email, l.unsubscribed_sms,
                   l.source, l.gclid, l.utm_campaign
            FROM follow_up_queue fq
            JOIN leads l ON fq.lead_id = l.id
            WHERE fq.status='pending' AND fq.scheduled_at <= ?
            ORDER BY fq.scheduled_at ASC
        """, (now,)).fetchall()
        return [dict(r) for r in rows]


def mark_follow_up_sent(queue_id: int, status: str = "sent", error: str = ""):
    now = _now()
    with _conn() as conn:
        conn.execute("UPDATE follow_up_queue SET status=?, sent_at=?, error=? WHERE id=?",
                     (status, now, error, queue_id))


def get_follow_up_queue(lead_id: str) -> list:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM follow_up_queue WHERE lead_id=? ORDER BY sequence_day", (lead_id,)).fetchall()
        return [dict(r) for r in rows]


# ─── Unsubscribes ────────────────────────────────────────────────────────────

def unsubscribe(lead_id: str, channel: str, reason: str = ""):
    now = _now()
    field = "unsubscribed_email" if channel == "email" else "unsubscribed_sms"
    with _conn() as conn:
        conn.execute(f"UPDATE leads SET {field}=1, updated_at=? WHERE id=?", (now, lead_id))
        conn.execute("INSERT INTO unsubscribes (lead_id, channel, reason, created_at) VALUES (?,?,?,?)",
                     (lead_id, channel, reason, now))
        # Cancel pending follow-ups for this channel
        conn.execute("UPDATE follow_up_queue SET status='skipped' WHERE lead_id=? AND channel=? AND status='pending'",
                     (lead_id, channel))
    add_event(lead_id, "unsubscribed", detail=json.dumps({"channel": channel}), source="self")


# ─── Pipeline stats ──────────────────────────────────────────────────────────

def get_pipeline_stats() -> dict:
    with _conn() as conn:
        rows = conn.execute("SELECT stage, COUNT(*) as count FROM leads GROUP BY stage").fetchall()
        counts = {r["stage"]: r["count"] for r in rows}
        total = conn.execute("SELECT COUNT(*) FROM leads WHERE stage != 'cold'").fetchone()[0]
        total_all = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        revenue = conn.execute("SELECT SUM(attributed_production) FROM leads").fetchone()[0] or 0.0
        income = conn.execute("SELECT SUM(attributed_income) FROM leads").fetchone()[0] or 0.0
        pending_followups = conn.execute("SELECT COUNT(*) FROM follow_up_queue WHERE status='pending'").fetchone()[0]
        sent_today = conn.execute("""
            SELECT COUNT(*) FROM follow_up_queue
            WHERE status='sent' AND sent_at >= date('now')
        """).fetchone()[0]
        no_show_count = counts.get("no_show", 0)
        scheduled_count = counts.get("scheduled", 0)

        return {
            "total_leads": total,
            "total_all": total_all,
            "by_stage": counts,
            "attributed_revenue": round(revenue, 2),
            "attributed_income": round(income, 2),
            "pending_follow_ups": pending_followups,
            "sent_today": sent_today,
            "no_show_count": no_show_count,
            "scheduled_count": scheduled_count,
        }


# ─── Lead Notes ─────────────────────────────────────────────────────────────

def add_note(lead_id: str, note_text: str, author: str = "admin") -> dict:
    now = _now()
    with _conn() as conn:
        cursor = conn.execute(
            "INSERT INTO lead_notes (lead_id, note_text, author, created_at) VALUES (?,?,?,?)",
            (lead_id, note_text, author, now),
        )
        # When staff logs any action, stamp last_staff_contact_at so the
        # Staff Follow-Up row clears immediately
        if author == "staff":
            conn.execute(
                "UPDATE leads SET last_staff_contact_at=? WHERE id=?",
                (now, lead_id)
            )
        return {
            "id": cursor.lastrowid,
            "lead_id": lead_id,
            "note_text": note_text,
            "author": author,
            "created_at": now,
        }


def get_notes(lead_id: str) -> list:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM lead_notes WHERE lead_id=? ORDER BY created_at DESC", (lead_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def delete_note(note_id: int) -> bool:
    with _conn() as conn:
        conn.execute("DELETE FROM lead_notes WHERE id=?", (note_id,))
        return True


# ─── Force stage (admin override — allows backward movement) ───────────────

def force_stage(lead_id: str, new_stage: str, source: str = "admin", detail: str = "") -> dict:
    """Set a lead's stage to any value — allows backward movement for admin corrections."""
    lead = get_lead(lead_id)
    if not lead:
        raise ValueError(f"Lead {lead_id} not found")
    old_stage = lead["stage"]
    if old_stage == new_stage:
        return lead
    now = _now()
    with _conn() as conn:
        conn.execute("UPDATE leads SET stage=?, updated_at=? WHERE id=?", (new_stage, now, lead_id))
    add_event(lead_id, "stage_changed", stage_from=old_stage, stage_to=new_stage,
              source=source, detail=detail)
    return get_lead(lead_id)


# ─── Campaign stats ─────────────────────────────────────────────────────────

def get_campaign_stats() -> list:
    """Return lead counts and revenue grouped by Google Ads campaign.
    Total cost comes from the gads_keywords_cache (real Google Ads spend),
    falling back to SUM(click_cost) on leads if no cache data exists yet.
    CPL = real total campaign cost / number of leads.
    """
    with _conn() as conn:
        rows = conn.execute("""
            WITH campaign_leads AS (
                SELECT
                    COALESCE(NULLIF(campaign_name, ''), utm_campaign) as campaign,
                    COUNT(*) as lead_count,
                    SUM(CASE WHEN stage IN ('scheduled','showed','no_show',
                        'treatment_presented','treatment_accepted','treatment_completed') THEN 1 ELSE 0 END) as scheduled_count,
                    SUM(CASE WHEN stage = 'no_show' THEN 1 ELSE 0 END) as no_show_count,
                    SUM(CASE WHEN stage IN ('showed',
                        'treatment_presented','treatment_accepted','treatment_completed') THEN 1 ELSE 0 END) as showed_count,
                    SUM(CASE WHEN stage IN ('treatment_presented','treatment_accepted','treatment_completed') THEN 1 ELSE 0 END) as treated_count,
                    SUM(CASE WHEN stage = 'treatment_completed' THEN 1 ELSE 0 END) as completed_count,
                    SUM(CASE WHEN stage IN ('treatment_presented','treatment_accepted','treatment_completed') THEN 1 ELSE 0 END) as treatment_presented_count,
                    SUM(CASE WHEN stage IN ('treatment_accepted','treatment_completed') THEN 1 ELSE 0 END) as treatment_accepted_count,
                    SUM(attributed_production) as revenue,
                    SUM(attributed_income) as attributed_income,
                    SUM(click_cost) as leads_click_cost_sum,
                    AVG(NULLIF(click_cost, 0)) as avg_cpc
                FROM leads
                WHERE campaign_name != '' OR utm_campaign != ''
                GROUP BY COALESCE(NULLIF(campaign_name, ''), utm_campaign)
            ),
            campaign_gads_cost AS (
                -- Sum real Google Ads spend from keywords cache, grouped by campaign
                SELECT campaign_name, SUM(cost) as total_gads_cost
                FROM gads_keywords_cache
                WHERE days = 30 AND campaign_name != ''
                GROUP BY campaign_name
            )
            SELECT
                cl.campaign,
                cl.lead_count,
                cl.scheduled_count,
                cl.no_show_count,
                cl.showed_count,
                cl.treated_count,
                cl.completed_count,
                cl.treatment_presented_count,
                cl.treatment_accepted_count,
                cl.revenue,
                cl.attributed_income,
                cl.avg_cpc,
                -- Use real Google Ads cost when available, fall back to leads sum
                COALESCE(gc.total_gads_cost, cl.leads_click_cost_sum, 0) as total_ad_spend,
                -- CPL = real total cost / leads
                CASE WHEN cl.lead_count > 0
                    THEN ROUND(COALESCE(gc.total_gads_cost, cl.leads_click_cost_sum, 0) / cl.lead_count, 2)
                    ELSE 0 END as cpl,
                -- CPA = real total cost / scheduled leads
                CASE WHEN cl.scheduled_count > 0
                    THEN ROUND(COALESCE(gc.total_gads_cost, cl.leads_click_cost_sum, 0) / cl.scheduled_count, 2)
                    ELSE 0 END as cpa,
                -- ROAS = revenue / real total cost
                CASE WHEN COALESCE(gc.total_gads_cost, cl.leads_click_cost_sum, 0) > 0
                    THEN ROUND(cl.revenue / COALESCE(gc.total_gads_cost, cl.leads_click_cost_sum, 0), 2)
                    ELSE 0 END as roas,
                CASE WHEN cl.lead_count > 0
                    THEN ROUND(CAST(cl.treated_count AS REAL) / cl.lead_count * 100, 1)
                    ELSE 0 END as conversion_rate
            FROM campaign_leads cl
            LEFT JOIN campaign_gads_cost gc ON LOWER(cl.campaign) = LOWER(gc.campaign_name)
            ORDER BY cl.lead_count DESC
        """).fetchall()
        return [dict(r) for r in rows]


def get_google_ads_campaigns() -> list:
    """Return list of distinct Google Ads campaign names for filter dropdown."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT DISTINCT campaign_name
            FROM leads
            WHERE campaign_name != ''
            ORDER BY campaign_name
        """).fetchall()
        return [r["campaign_name"] for r in rows]


def get_distinct_sources() -> list:
    """Return list of distinct lead sources."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT DISTINCT source FROM leads WHERE source != '' ORDER BY source
        """).fetchall()
        return [r["source"] for r in rows]


def save_ga4_cache(report_type: str, days: int, data: dict):
    """Save GA4 report data to cache."""
    now = _now()
    with _conn() as conn:
        # Delete old cache for this report type + days
        conn.execute("DELETE FROM ga4_cache WHERE report_type=? AND days=?", (report_type, days))
        conn.execute(
            "INSERT INTO ga4_cache (report_type, days, data, fetched_at) VALUES (?,?,?,?)",
            (report_type, days, json.dumps(data, default=str), now),
        )


def get_ga4_cache(report_type: str, days: int, max_age_hours: int = 24) -> Optional[dict]:
    """Get cached GA4 report if it's fresh enough."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT data, fetched_at FROM ga4_cache WHERE report_type=? AND days=? ORDER BY fetched_at DESC LIMIT 1",
            (report_type, days),
        ).fetchone()
        if not row:
            return None
        try:
            from datetime import timedelta
            import dateutil.parser
            fetched = dateutil.parser.parse(row["fetched_at"])
            if datetime.now(timezone.utc) - fetched > timedelta(hours=max_age_hours):
                return None  # Stale
            return json.loads(row["data"])
        except Exception:
            return None


def get_keyword_stats() -> list:
    """
    Return keyword performance joined with lead/revenue attribution.
    Shows ALL keywords from Google Ads cache (even with zero leads),
    plus any keywords that came in via gclid attribution on leads.
    """
    with _conn() as conn:
        rows = conn.execute("""
            SELECT
                COALESCE(k.keyword_text, l.keyword_text)  AS keyword,
                COALESCE(k.ad_group_name, l.ad_group_name) AS ad_group_name,
                COALESCE(k.campaign_name, l.campaign_name) AS campaign_name,
                COALESCE(k.match_type, '')                 AS match_type,
                -- Google Ads metrics (from cache)
                COALESCE(k.impressions, 0)          AS impressions,
                COALESCE(k.clicks, 0)               AS gads_clicks,
                COALESCE(k.cost, 0.0)               AS total_cost,
                COALESCE(k.avg_cpc, 0.0)            AS avg_cpc,
                -- Quality & competitive signals
                COALESCE(k.quality_score, 0)        AS quality_score,
                COALESCE(k.creative_quality_score,'') AS creative_quality_score,
                COALESCE(k.post_click_quality,'')   AS post_click_quality,
                COALESCE(k.search_predicted_ctr,'') AS search_predicted_ctr,
                COALESCE(k.impression_share, 0.0)   AS impression_share,
                COALESCE(k.top_impression_pct, 0.0) AS top_impression_pct,
                COALESCE(k.abs_top_impression_pct, 0.0) AS abs_top_impression_pct,
                COALESCE(k.budget_lost_is, 0.0)     AS budget_lost_is,
                COALESCE(k.rank_lost_is, 0.0)       AS rank_lost_is,
                -- Lead attribution (from leads table)
                COUNT(l.id)  AS lead_count,
                SUM(CASE WHEN l.stage IN ('scheduled','showed','no_show',
                    'treatment_presented','treatment_accepted','treatment_completed') THEN 1 ELSE 0 END) AS scheduled_count,
                SUM(CASE WHEN l.stage = 'no_show' THEN 1 ELSE 0 END) AS no_show_count,
                SUM(CASE WHEN l.stage IN ('treatment_presented','treatment_accepted','treatment_completed') THEN 1 ELSE 0 END) AS treated_count,
                COALESCE(SUM(l.attributed_production), 0) AS revenue,
                -- Calculated metrics
                CASE WHEN COUNT(l.id) > 0
                    THEN ROUND(COALESCE(k.cost, SUM(l.click_cost)) / COUNT(l.id), 2)
                    ELSE 0 END AS cpl,
                CASE WHEN COALESCE(k.cost, 0) > 0
                    THEN ROUND(COALESCE(SUM(l.attributed_production), 0) / k.cost, 2)
                    ELSE 0 END AS roas,
                CASE WHEN COUNT(l.id) > 0
                    THEN ROUND(CAST(SUM(CASE WHEN l.stage IN ('treatment_presented','treatment_accepted','treatment_completed') THEN 1 ELSE 0 END) AS REAL) / COUNT(l.id) * 100, 1)
                    ELSE 0 END AS conversion_rate,
                k.synced_at
            FROM gads_keywords_cache k
            LEFT JOIN leads l ON LOWER(l.keyword_text) = LOWER(k.keyword_text)
            WHERE k.days = 30
            GROUP BY k.keyword_text

            UNION

            -- Also include leads whose keyword isn't in the cache yet
            SELECT
                l.keyword_text          AS keyword,
                l.ad_group_name         AS ad_group_name,
                l.campaign_name         AS campaign_name,
                ''                      AS match_type,
                0                       AS impressions,
                0                       AS gads_clicks,
                SUM(l.click_cost)       AS total_cost,
                AVG(NULLIF(l.click_cost,0)) AS avg_cpc,
                -- Quality & competitive signals (not available for non-cached keywords)
                0                       AS quality_score,
                ''                      AS creative_quality_score,
                ''                      AS post_click_quality,
                ''                      AS search_predicted_ctr,
                0.0                     AS impression_share,
                0.0                     AS top_impression_pct,
                0.0                     AS abs_top_impression_pct,
                0.0                     AS budget_lost_is,
                0.0                     AS rank_lost_is,
                COUNT(l.id)             AS lead_count,
                SUM(CASE WHEN l.stage IN ('scheduled','showed','no_show',
                    'treatment_presented','treatment_accepted','treatment_completed') THEN 1 ELSE 0 END) AS scheduled_count,
                SUM(CASE WHEN l.stage = 'no_show' THEN 1 ELSE 0 END) AS no_show_count,
                SUM(CASE WHEN l.stage IN ('treatment_presented','treatment_accepted','treatment_completed') THEN 1 ELSE 0 END) AS treated_count,
                COALESCE(SUM(l.attributed_production), 0) AS revenue,
                CASE WHEN COUNT(l.id) > 0 THEN ROUND(SUM(l.click_cost) / COUNT(l.id), 2) ELSE 0 END AS cpl,
                CASE WHEN SUM(l.click_cost) > 0
                    THEN ROUND(SUM(l.attributed_production) / SUM(l.click_cost), 2)
                    ELSE 0 END AS roas,
                CASE WHEN COUNT(l.id) > 0
                    THEN ROUND(CAST(SUM(CASE WHEN l.stage IN ('treatment_presented','treatment_accepted','treatment_completed') THEN 1 ELSE 0 END) AS REAL) / COUNT(l.id) * 100, 1)
                    ELSE 0 END AS conversion_rate,
                NULL AS synced_at
            FROM leads l
            WHERE l.keyword_text != ''
              AND LOWER(l.keyword_text) NOT IN (
                  SELECT LOWER(keyword_text) FROM gads_keywords_cache WHERE days = 30
              )
            GROUP BY l.keyword_text

            ORDER BY gads_clicks DESC, lead_count DESC
        """).fetchall()
        return [dict(r) for r in rows]


# ─── Google Ads Extended Caches ───────────────────────────────────────────────

def save_gads_keywords_cache(keywords: list, days: int = 30):
    """
    Save Google Ads keyword performance to cache (overwrites on conflict).
    Accepts the expanded field set including quality score and impression share.
    """
    now = _now()
    with _conn() as conn:
        for kw in keywords:
            conn.execute("""
                INSERT INTO gads_keywords_cache
                    (keyword_text, match_type, ad_group_name, campaign_name,
                     impressions, clicks, cost, avg_cpc, conversions,
                     quality_score, creative_quality_score, post_click_quality,
                     search_predicted_ctr, impression_share, top_impression_pct,
                     abs_top_impression_pct, budget_lost_is, rank_lost_is,
                     days, synced_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(keyword_text, days) DO UPDATE SET
                    match_type=excluded.match_type,
                    ad_group_name=excluded.ad_group_name,
                    campaign_name=excluded.campaign_name,
                    impressions=excluded.impressions,
                    clicks=excluded.clicks,
                    cost=excluded.cost,
                    avg_cpc=excluded.avg_cpc,
                    conversions=excluded.conversions,
                    quality_score=excluded.quality_score,
                    creative_quality_score=excluded.creative_quality_score,
                    post_click_quality=excluded.post_click_quality,
                    search_predicted_ctr=excluded.search_predicted_ctr,
                    impression_share=excluded.impression_share,
                    top_impression_pct=excluded.top_impression_pct,
                    abs_top_impression_pct=excluded.abs_top_impression_pct,
                    budget_lost_is=excluded.budget_lost_is,
                    rank_lost_is=excluded.rank_lost_is,
                    synced_at=excluded.synced_at
            """, (
                kw.get("keyword_text", ""),
                kw.get("match_type", ""),
                kw.get("ad_group_name", ""),
                kw.get("campaign_name", ""),
                kw.get("impressions", 0),
                kw.get("clicks", 0),
                kw.get("cost", 0.0),
                kw.get("avg_cpc", 0.0),
                kw.get("conversions", 0.0),
                kw.get("quality_score", 0),
                kw.get("creative_quality_score", ""),
                kw.get("post_click_quality", ""),
                kw.get("search_predicted_ctr", ""),
                kw.get("impression_share", 0.0),
                kw.get("top_impression_pct", 0.0),
                kw.get("abs_top_impression_pct", 0.0),
                kw.get("budget_lost_is", 0.0),
                kw.get("rank_lost_is", 0.0),
                days,
                now,
            ))


def save_gads_search_terms_cache(terms: list, days: int = 30):
    """Save search terms from search_term_view to cache."""
    now = _now()
    with _conn() as conn:
        for t in terms:
            conn.execute("""
                INSERT INTO gads_search_terms_cache
                    (search_term, status, campaign_name, ad_group_name,
                     impressions, clicks, cost, conversions, cpc, days, synced_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(search_term, campaign_name, days) DO UPDATE SET
                    status=excluded.status,
                    ad_group_name=excluded.ad_group_name,
                    impressions=excluded.impressions,
                    clicks=excluded.clicks,
                    cost=excluded.cost,
                    conversions=excluded.conversions,
                    cpc=excluded.cpc,
                    synced_at=excluded.synced_at
            """, (
                t.get("search_term", ""),
                t.get("status", "NONE"),
                t.get("campaign_name", ""),
                t.get("ad_group_name", ""),
                t.get("impressions", 0),
                t.get("clicks", 0),
                t.get("cost", 0.0),
                t.get("conversions", 0.0),
                t.get("cpc", 0.0),
                days,
                now,
            ))


def get_search_term_stats(campaign_name: str = "", days: int = 30) -> list:
    """
    Return cached search terms joined with lead attribution.
    campaign_name: filter to a specific campaign, or '' for all.
    """
    with _conn() as conn:
        where = "WHERE s.days = ?"
        params: list = [days]
        if campaign_name:
            where += " AND LOWER(s.campaign_name) LIKE ?"
            params.append(f"%{campaign_name.lower()}%")

        rows = conn.execute(f"""
            SELECT
                s.search_term,
                s.status,
                s.campaign_name,
                s.ad_group_name,
                s.impressions,
                s.clicks,
                s.cost,
                s.conversions  AS gads_conversions,
                s.cpc,
                COUNT(l.id)    AS lead_count,
                COALESCE(SUM(l.attributed_production), 0) AS revenue,
                s.synced_at
            FROM gads_search_terms_cache s
            LEFT JOIN leads l ON LOWER(l.search_term) = LOWER(s.search_term)
            {where}
            GROUP BY s.search_term, s.campaign_name
            ORDER BY s.cost DESC, s.clicks DESC
        """, params).fetchall()
        return [dict(r) for r in rows]


def save_gads_geo_cache(geo_data: list, days: int = 30):
    """Save geographic performance data to cache."""
    now = _now()
    with _conn() as conn:
        for g in geo_data:
            conn.execute("""
                INSERT INTO gads_geo_cache
                    (location_name, location_type, campaign_name,
                     impressions, clicks, cost, conversions, cpc, conversion_rate, days, synced_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(location_name, campaign_name, days) DO UPDATE SET
                    location_type=excluded.location_type,
                    impressions=excluded.impressions,
                    clicks=excluded.clicks,
                    cost=excluded.cost,
                    conversions=excluded.conversions,
                    cpc=excluded.cpc,
                    conversion_rate=excluded.conversion_rate,
                    synced_at=excluded.synced_at
            """, (
                g.get("location_name", ""),
                g.get("location_type", ""),
                g.get("campaign_name", ""),
                g.get("impressions", 0),
                g.get("clicks", 0),
                g.get("cost", 0.0),
                g.get("conversions", 0.0),
                g.get("cpc", 0.0),
                g.get("conversion_rate", 0.0),
                days,
                now,
            ))


def get_geo_stats(days: int = 30) -> list:
    """Return cached geographic performance data."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT location_name, location_type, campaign_name,
                   impressions, clicks, cost, conversions, cpc, conversion_rate, synced_at
            FROM gads_geo_cache
            WHERE days = ?
            ORDER BY conversions DESC, clicks DESC
        """, (days,)).fetchall()
        return [dict(r) for r in rows]


def save_gads_schedule_cache(schedule_data: dict, days: int = 30):
    """
    Save hour/day/device performance data to cache.
    schedule_data: {"by_hour": [...], "by_day": [...], "by_device": [...]}
    """
    now = _now()
    entries = []
    for item in schedule_data.get("by_hour", []):
        entries.append(("hour", str(item.get("hour", "")), item))
    for item in schedule_data.get("by_day", []):
        entries.append(("day", item.get("day", ""), item))
    for item in schedule_data.get("by_device", []):
        entries.append(("device", item.get("device", ""), item))

    with _conn() as conn:
        for seg_type, seg_val, d in entries:
            conn.execute("""
                INSERT INTO gads_schedule_cache
                    (segment_type, segment_value, impressions, clicks, cost,
                     conversions, cpc, conversion_rate, days, synced_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(segment_type, segment_value, days) DO UPDATE SET
                    impressions=excluded.impressions,
                    clicks=excluded.clicks,
                    cost=excluded.cost,
                    conversions=excluded.conversions,
                    cpc=excluded.cpc,
                    conversion_rate=excluded.conversion_rate,
                    synced_at=excluded.synced_at
            """, (
                seg_type, seg_val,
                d.get("impressions", 0),
                d.get("clicks", 0),
                d.get("cost", 0.0),
                d.get("conversions", 0.0),
                d.get("cpc", 0.0),
                d.get("conversion_rate", 0.0),
                days,
                now,
            ))


def get_schedule_stats(days: int = 30) -> dict:
    """Return cached schedule / device performance data."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT segment_type, segment_value, impressions, clicks,
                   cost, conversions, cpc, conversion_rate
            FROM gads_schedule_cache
            WHERE days = ?
            ORDER BY segment_type, segment_value
        """, (days,)).fetchall()
        result: dict = {"by_hour": [], "by_day": [], "by_device": []}
        for r in rows:
            d = dict(r)
            seg_type = d.pop("segment_type")
            seg_val = d.pop("segment_value")
            if seg_type == "hour":
                d["hour"] = int(seg_val) if seg_val.isdigit() else 0
                result["by_hour"].append(d)
            elif seg_type == "day":
                d["day"] = seg_val
                result["by_day"].append(d)
            elif seg_type == "device":
                d["device"] = seg_val
                result["by_device"].append(d)
        return result


# ─── Optimizer Memory ─────────────────────────────────────────────────────────

def get_optimizer_memory(category: Optional[str] = None, active_only: bool = True) -> list:
    """Return all optimizer memory entries, optionally filtered by category."""
    with _conn() as conn:
        base = "WHERE active=1" if active_only else "WHERE 1=1"
        if category:
            rows = conn.execute(
                f"SELECT * FROM optimizer_memory {base} AND category=? ORDER BY campaign DESC, category, key",
                (category,)
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT * FROM optimizer_memory {base} ORDER BY campaign DESC, category, key"
            ).fetchall()
        return [dict(r) for r in rows]


def get_optimizer_memory_dict(campaign: str = '') -> dict:
    """
    Return memory as a structured dict for fast lookup in the optimizer.
    Scoping rules:
      - Global entries (campaign='') apply to ALL campaigns.
      - Campaign-specific entries (campaign=X) apply ONLY to campaign X and OVERRIDE globals.

    Returns:
      {
        'term_classifications': {'search term': {'value': ..., 'reason': ..., 'scope': 'global'|'campaign'}},
        'keyword_overrides': {...},
        'campaign_rules': {...},
        'general': {...},
      }
    """
    entries = get_optimizer_memory(active_only=True)
    campaign_lower = (campaign or '').lower().strip()

    result = {
        'term_classifications': {},
        'keyword_overrides': {},
        'campaign_rules': {},
        'general': {},
    }

    def _bucket(cat):
        if cat == 'term_classification': return 'term_classifications'
        if cat == 'keyword_override':    return 'keyword_overrides'
        if cat == 'campaign_rule':       return 'campaign_rules'
        return 'general'

    # First pass: load global entries (campaign='')
    for e in entries:
        entry_camp = (e.get('campaign') or '').lower().strip()
        if entry_camp:
            continue  # skip campaign-specific on first pass
        key = e['key'].lower()
        bucket = _bucket(e['category'])
        result[bucket][key] = {
            'value': e['value'],
            'reason': e['reason'],
            'scope': 'global',
        }

    # Second pass: apply campaign-specific overrides (these win)
    if campaign_lower:
        for e in entries:
            entry_camp = (e.get('campaign') or '').lower().strip()
            if not entry_camp:
                continue  # already handled globals
            # Match if campaign_lower contains the entry's campaign fragment or exact match
            if entry_camp not in campaign_lower and campaign_lower not in entry_camp:
                continue
            key = e['key'].lower()
            bucket = _bucket(e['category'])
            result[bucket][key] = {
                'value': e['value'],
                'reason': e['reason'],
                'scope': f'campaign:{e["campaign"]}',
            }

    return result


def add_optimizer_memory(category: str, key: str, value: str, reason: str,
                         campaign: str = '', author: str = 'admin') -> dict:
    """Add a new memory entry. Campaign='' means global."""
    now = _now()
    key_lower = key.strip().lower()
    campaign_lower = (campaign or '').strip().lower()
    with _conn() as conn:
        # Deactivate existing entry with same category+key+campaign scope
        conn.execute(
            "UPDATE optimizer_memory SET active=0, updated_at=? WHERE category=? AND key=? AND LOWER(COALESCE(campaign,''))=? AND active=1",
            (now, category, key_lower, campaign_lower)
        )
        cursor = conn.execute(
            "INSERT INTO optimizer_memory (category, key, value, reason, campaign, author, active, created_at, updated_at) VALUES (?,?,?,?,?,?,1,?,?)",
            (category, key_lower, value, reason, campaign.strip(), author, now, now)
        )
        row = conn.execute("SELECT * FROM optimizer_memory WHERE id=?", (cursor.lastrowid,)).fetchone()
        return dict(row)


def deactivate_optimizer_memory(memory_id: int) -> bool:
    """Soft-delete a memory entry by ID."""
    now = _now()
    with _conn() as conn:
        conn.execute(
            "UPDATE optimizer_memory SET active=0, updated_at=? WHERE id=?",
            (now, memory_id)
        )
        return True


def update_optimizer_memory(memory_id: int, value: str, reason: str) -> Optional[dict]:
    """Update value and reason for an existing memory entry."""
    now = _now()
    with _conn() as conn:
        conn.execute(
            "UPDATE optimizer_memory SET value=?, reason=?, updated_at=? WHERE id=? AND active=1",
            (value, reason, now, memory_id)
        )
        row = conn.execute("SELECT * FROM optimizer_memory WHERE id=?", (memory_id,)).fetchone()
        return dict(row) if row else None


# ─── Communication Log (send-once dedupe) ────────────────────────────────────

def already_sent(lead_id: str, template: str) -> bool:
    """Return True if this lead has already received this template."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM communication_log WHERE lead_id=? AND template=? LIMIT 1",
            (lead_id, template),
        ).fetchone()
        return row is not None


def record_send(lead_id: str, template: str, channel: str, queue_id: Optional[int] = None) -> None:
    """Record that a template was successfully sent. Idempotent on (lead_id, template)."""
    now = _now()
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO communication_log (lead_id, template, channel, sent_at, queue_id) "
            "VALUES (?,?,?,?,?)",
            (lead_id, template, channel, now, queue_id),
        )


def backfill_communication_log() -> int:
    """
    One-time-ish backfill: copy every follow_up_queue row with status='sent'
    into communication_log so we don't re-send to leads who already got the
    email/SMS before this dedupe table existed.
    Safe to run on every startup — INSERT OR IGNORE means existing rows are
    untouched. Returns number of rows inserted.
    """
    with _conn() as conn:
        before = conn.execute("SELECT COUNT(*) FROM communication_log").fetchone()[0]
        conn.execute("""
            INSERT OR IGNORE INTO communication_log (lead_id, template, channel, sent_at, queue_id)
            SELECT lead_id, template, channel,
                   COALESCE(NULLIF(sent_at, ''), scheduled_at),
                   id
            FROM follow_up_queue
            WHERE status = 'sent'
        """)
        after = conn.execute("SELECT COUNT(*) FROM communication_log").fetchone()[0]
        return after - before


# ─── Deleted-lead tombstones ──────────────────────────────────────────────────

def is_deleted_lead(lead_id: str, email: str = "") -> bool:
    """Return True if this lead_id (or email) has been admin-deleted."""
    with _conn() as conn:
        if lead_id:
            row = conn.execute(
                "SELECT 1 FROM deleted_leads WHERE lead_id=? LIMIT 1", (lead_id,)
            ).fetchone()
            if row:
                return True
        if email:
            row = conn.execute(
                "SELECT 1 FROM deleted_leads WHERE email=? COLLATE NOCASE LIMIT 1",
                (email.strip().lower(),),
            ).fetchone()
            if row:
                return True
        return False


def add_deleted_lead_tombstone(lead_id: str, email: str = "", deleted_by: str = "admin",
                                reason: str = "") -> None:
    """Record that a lead was permanently deleted. Idempotent on lead_id."""
    now = _now()
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO deleted_leads (lead_id, email, deleted_at, deleted_by, reason) "
            "VALUES (?,?,?,?,?)",
            (lead_id, (email or "").strip().lower(), now, deleted_by, reason),
        )


# ─── Conversations + Messages (Step 5 — bi-directional email inbox) ──────────

def get_or_create_conversation(lead_id: Optional[str], channel: str, contact_email: str) -> dict:
    """
    Return the existing conversation for this lead (or contact_email if lead_id is None),
    or create a new one.

    One conversation per lead in v1. If lead_id is None (unmatched), we key on contact_email.
    """
    now = _now()
    with _conn() as conn:
        if lead_id:
            row = conn.execute(
                "SELECT * FROM conversations WHERE lead_id=? AND channel=? LIMIT 1",
                (lead_id, channel),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM conversations WHERE lead_id IS NULL AND contact_email=? AND channel=? LIMIT 1",
                (contact_email.lower().strip(), channel),
            ).fetchone()

        if row:
            return dict(row)

        # Create new conversation
        cursor = conn.execute(
            "INSERT INTO conversations (lead_id, channel, contact_email, created_at, updated_at) "
            "VALUES (?,?,?,?,?)",
            (lead_id, channel, contact_email.lower().strip(), now, now),
        )
        new_row = conn.execute(
            "SELECT * FROM conversations WHERE id=?", (cursor.lastrowid,)
        ).fetchone()
        return dict(new_row)


def append_message(
    conversation_id: int,
    direction: str,
    from_addr: str,
    subject: str,
    body: str,
    message_id: str,
    in_reply_to: str,
    msg_references: str,
    received_at: str,
) -> bool:
    """
    Insert a message into the messages table.
    Uses INSERT OR IGNORE for dedup on message_id (partial unique index handles empty IDs).
    Returns True if the row was inserted, False if it was a duplicate.
    """
    with _conn() as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO messages
                (conversation_id, direction, from_addr, subject, body,
                 message_id, in_reply_to, msg_references, received_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (conversation_id, direction, from_addr.lower().strip(), subject, body,
             message_id, in_reply_to, msg_references, received_at),
        )
        inserted = cursor.rowcount > 0

        if inserted:
            # Update conversation.updated_at
            conn.execute(
                "UPDATE conversations SET updated_at=? WHERE id=?",
                (received_at, conversation_id),
            )

        return inserted


def get_conversation(lead_id: str) -> Optional[dict]:
    """Return the conversation for a lead, or None."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM conversations WHERE lead_id=? AND channel='email' LIMIT 1",
            (lead_id,),
        ).fetchone()
        return dict(row) if row else None


def get_messages(conversation_id: int, limit: int = 50) -> list:
    """Return messages for a conversation, oldest first."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY received_at ASC LIMIT ?",
            (conversation_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_conversations(limit: int = 100, unmatched_only: bool = False) -> list:
    """
    Return recent conversations with latest message preview.
    Joins with leads to include name/email where available.
    """
    with _conn() as conn:
        filter_clause = "WHERE c.lead_id IS NULL" if unmatched_only else ""
        rows = conn.execute(f"""
            SELECT
                c.id, c.lead_id, c.channel, c.contact_email, c.created_at, c.updated_at,
                l.first_name, l.last_name,
                (SELECT subject FROM messages WHERE conversation_id=c.id ORDER BY received_at DESC LIMIT 1) AS last_subject,
                (SELECT body FROM messages WHERE conversation_id=c.id ORDER BY received_at DESC LIMIT 1) AS last_body_snippet,
                (SELECT direction FROM messages WHERE conversation_id=c.id ORDER BY received_at DESC LIMIT 1) AS last_direction,
                (SELECT COUNT(*) FROM messages WHERE conversation_id=c.id) AS message_count
            FROM conversations c
            LEFT JOIN leads l ON c.lead_id = l.id
            {filter_clause}
            ORDER BY c.updated_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            # Truncate snippet
            snippet = (d.get("last_body_snippet") or "")[:120]
            d["last_body_snippet"] = snippet
            results.append(d)
        return results


# ─── Google Ads Daily Stats ───────────────────────────────────────────────────

def save_gads_daily_stats(rows: list) -> int:
    """
    Upsert a list of daily ad-group stats rows.
    Each row must have: date, campaign_id, campaign_name, ad_group_id, ad_group_name,
    impressions, clicks, cost_micros, conversions.
    Returns number of rows processed.
    """
    if not rows:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        for row in rows:
            conn.execute("""
                INSERT INTO gads_daily_stats
                    (date, campaign_id, campaign_name, ad_group_id, ad_group_name,
                     impressions, clicks, cost_micros, conversions, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date, campaign_id, ad_group_id) DO UPDATE SET
                    campaign_name=excluded.campaign_name,
                    ad_group_name=excluded.ad_group_name,
                    impressions=excluded.impressions,
                    clicks=excluded.clicks,
                    cost_micros=excluded.cost_micros,
                    conversions=excluded.conversions,
                    synced_at=excluded.synced_at
            """, (
                row["date"],
                row["campaign_id"],
                row["campaign_name"],
                row["ad_group_id"],
                row["ad_group_name"],
                int(row.get("impressions", 0)),
                int(row.get("clicks", 0)),
                int(row.get("cost_micros", 0)),
                float(row.get("conversions", 0.0)),
                now,
            ))
    return len(rows)


def get_daily_stats(days: int = 30, campaign_id: Optional[str] = None) -> list:
    """
    Return daily aggregate stats (summed across ad groups) for the last N days.
    Optionally filter by campaign_id (pass None for all campaigns).
    Returns list of dicts ordered by date ASC.

    Groups by (date, campaign_id) only — uses MAX(campaign_name) so a renamed
    campaign doesn't produce duplicate rows per day.
    Date filter uses 'localtime' to match local timezone (consistent with OD/leads).
    """
    days = max(min(int(days), 90), 1)
    modifier = f"-{days} days"
    with _conn() as conn:
        if campaign_id:
            rows = conn.execute("""
                SELECT
                    date,
                    campaign_id,
                    MAX(campaign_name) AS campaign_name,
                    SUM(impressions)   AS impressions,
                    SUM(clicks)        AS clicks,
                    SUM(cost_micros)   AS cost_micros,
                    ROUND(SUM(cost_micros) / 1000000.0, 2) AS cost,
                    SUM(conversions)   AS conversions
                FROM gads_daily_stats
                WHERE date >= date('now', 'localtime', ?)
                  AND campaign_id = ?
                GROUP BY date, campaign_id
                ORDER BY date ASC
            """, (modifier, campaign_id)).fetchall()
        else:
            rows = conn.execute("""
                SELECT
                    date,
                    campaign_id,
                    MAX(campaign_name) AS campaign_name,
                    SUM(impressions)   AS impressions,
                    SUM(clicks)        AS clicks,
                    SUM(cost_micros)   AS cost_micros,
                    ROUND(SUM(cost_micros) / 1000000.0, 2) AS cost,
                    SUM(conversions)   AS conversions
                FROM gads_daily_stats
                WHERE date >= date('now', 'localtime', ?)
                GROUP BY date, campaign_id
                ORDER BY date ASC
            """, (modifier,)).fetchall()
        return [dict(r) for r in rows]


def get_ad_group_stats(days: int = 30) -> list:
    """
    Return ad-group-level aggregated stats.

    Metrics (impressions/clicks/cost) come from gads_daily_stats grouped by
    ad_group_id — accurate because this table is ad-group-grained.

    Lead attribution (lead_count, revenue, etc.) is joined from leads by
    ad_group_name + campaign_name string match — display-only accuracy is
    acceptable since each lead has exactly one ad_group_name.

    conversion_rate = treatment-accepted leads / total leads × 100.
    Date filter uses 'localtime' to match local timezone.
    """
    days = max(min(int(days), 90), 1)
    modifier = f"-{days} days"
    with _conn() as conn:
        rows = conn.execute("""
            WITH ag_metrics AS (
                -- Accurate ad-group metrics from daily stats table
                -- Use MAX(name) so renamed ad groups don't split into multiple rows
                SELECT
                    ad_group_id,
                    MAX(ad_group_name) AS ad_group_name,
                    campaign_id,
                    MAX(campaign_name) AS campaign_name,
                    SUM(impressions)   AS impressions,
                    SUM(clicks)        AS clicks,
                    ROUND(SUM(cost_micros) / 1000000.0, 2) AS cost,
                    CASE WHEN SUM(clicks) > 0
                        THEN ROUND(SUM(cost_micros) / 1000000.0 / SUM(clicks), 4)
                        ELSE 0.0 END AS avg_cpc,
                    SUM(conversions)   AS conversions
                FROM gads_daily_stats
                WHERE date >= date('now', 'localtime', ?)
                GROUP BY ad_group_id, campaign_id
            )
            SELECT
                ag.ad_group_id,
                ag.ad_group_name,
                ag.campaign_id,
                ag.campaign_name,
                ag.impressions,
                ag.clicks,
                ag.cost,
                ag.avg_cpc,
                ag.conversions,
                COUNT(DISTINCT l.id) AS lead_count,
                SUM(CASE WHEN l.stage IN (
                    'scheduled','showed','no_show',
                    'treatment_presented','treatment_accepted','treatment_completed'
                ) THEN 1 ELSE 0 END) AS scheduled_count,
                COALESCE(SUM(l.attributed_production), 0) AS revenue,
                CASE WHEN COUNT(DISTINCT l.id) > 0
                    THEN ROUND(ag.cost / COUNT(DISTINCT l.id), 2)
                    ELSE 0 END AS cpl,
                -- conversion_rate: treatment-accepted leads / total leads
                CASE WHEN COUNT(DISTINCT l.id) > 0
                    THEN ROUND(
                        CAST(SUM(CASE WHEN l.stage IN (
                            'treatment_accepted','treatment_completed'
                        ) THEN 1 ELSE 0 END) AS REAL)
                        / COUNT(DISTINCT l.id) * 100, 1)
                    ELSE 0 END AS conversion_rate
            FROM ag_metrics ag
            LEFT JOIN leads l
                ON LOWER(l.ad_group_name) = LOWER(ag.ad_group_name)
               AND LOWER(l.campaign_name) = LOWER(ag.campaign_name)
            GROUP BY ag.ad_group_id, ag.campaign_id
            ORDER BY ag.cost DESC
        """, (modifier,)).fetchall()
        return [dict(r) for r in rows]


# ─── Manual Messaging (Step 8) ────────────────────────────────────────────────

def save_outbound_message(lead_id: str, channel: str, subject: str, body: str,
                          sent_by: str = "admin") -> int:
    """
    Store a manually sent outbound message in the conversations/messages tables.
    Gets or creates a single conversation per (lead_id, channel).
    Returns the new message id.
    """
    now = _now()
    with _conn() as conn:
        # Get or create conversation for this lead + channel
        row = conn.execute(
            "SELECT id FROM conversations WHERE lead_id=? AND channel=? LIMIT 1",
            (lead_id, channel)
        ).fetchone()
        if row:
            conv_id = row[0]
            conn.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conv_id))
        else:
            # Fetch contact info from leads table for contact_email
            lead_row = conn.execute(
                "SELECT email FROM leads WHERE id=?", (lead_id,)
            ).fetchone()
            contact_email = lead_row[0] if lead_row else ""
            cur = conn.execute(
                "INSERT INTO conversations (lead_id, channel, contact_email, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (lead_id, channel, contact_email, now, now)
            )
            conv_id = cur.lastrowid

        # Insert outbound message
        cur = conn.execute(
            "INSERT INTO messages "
            "(conversation_id, direction, from_addr, subject, body, message_id, "
            " in_reply_to, msg_references, received_at, sent_by, message_type) "
            "VALUES (?, 'outbound', 'practice', ?, ?, '', '', ?, ?, ?)",
            (conv_id, subject, body, now, now, sent_by, "manual")
        )
        return cur.lastrowid


def get_lead_messages(lead_id: str) -> list:
    """
    Return all messages for a lead across all channels, ordered by received_at.
    Joins conversation channel so the frontend knows which channel each message used.
    """
    with _conn() as conn:
        rows = conn.execute("""
            SELECT
                m.id,
                m.direction,
                m.from_addr,
                m.subject,
                m.body,
                m.received_at,
                m.sent_by,
                m.message_type,
                c.channel
            FROM messages m
            JOIN conversations c ON c.id = m.conversation_id
            WHERE c.lead_id = ?
            ORDER BY m.received_at ASC, m.id ASC
        """, (lead_id,)).fetchall()
        return [dict(r) for r in rows]


# ─── Phase 1: Google Ads Campaign Management — Audit + Safety ────────────────

def log_gads_action(
    action_id: str,
    operation: str,
    entity_type: str,
    entity_id: str,
    entity_name: str,
    before_state_json: str,
    after_state_json: str,
    executed: bool,
    execution_result: str,
    actor: str,
    reason: str = "",
    error_detail: str = "",
    optimizer_run_id: str = "",
) -> None:
    """Insert a Google Ads audit log row. Called from campaign_audit.py."""
    now = _now()
    with _conn() as conn:
        conn.execute("""
            INSERT INTO gads_audit_log
                (action_id, operation, entity_type, entity_id, entity_name,
                 before_state_json, after_state_json, executed, execution_result,
                 actor, reason, error_detail, optimizer_run_id,
                 created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            action_id, operation, entity_type, entity_id, entity_name,
            before_state_json, after_state_json,
            1 if executed else 0, execution_result,
            actor, reason, error_detail, optimizer_run_id,
            now, now,
        ))


def update_gads_action_result(
    action_id: str,
    executed: bool,
    execution_result: str,
    error_detail: str = "",
) -> None:
    """Update an audit log row after the Google Ads API call returns."""
    now = _now()
    with _conn() as conn:
        conn.execute("""
            UPDATE gads_audit_log
               SET executed=?, execution_result=?, error_detail=?, updated_at=?
             WHERE action_id=?
        """, (1 if executed else 0, execution_result, error_detail, now, action_id))


def set_audit_approval(action_id: str, approver: str) -> None:
    """Stamp who approved and when — called after Apply button triggers execution."""
    now = _now()
    with _conn() as conn:
        conn.execute("""
            UPDATE gads_audit_log
               SET approval_by=?, approved_at=?, updated_at=?
             WHERE action_id=?
        """, (approver, now, now, action_id))


def get_audit_row(action_id: str) -> Optional[dict]:
    """Fetch a single audit log row by action_id."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM gads_audit_log WHERE action_id=?", (action_id,)
        ).fetchone()
        return dict(row) if row else None


def get_audit_log(limit: int = 100, entity_id: str = "", operation: str = "") -> list:
    """Fetch audit entries, most recent first. Optional filters."""
    with _conn() as conn:
        if entity_id and operation:
            rows = conn.execute("""
                SELECT * FROM gads_audit_log
                 WHERE entity_id=? AND operation=?
                 ORDER BY created_at DESC LIMIT ?
            """, (entity_id, operation, limit)).fetchall()
        elif entity_id:
            rows = conn.execute("""
                SELECT * FROM gads_audit_log
                 WHERE entity_id=?
                 ORDER BY created_at DESC LIMIT ?
            """, (entity_id, limit)).fetchall()
        elif operation:
            rows = conn.execute("""
                SELECT * FROM gads_audit_log
                 WHERE operation=?
                 ORDER BY created_at DESC LIMIT ?
            """, (operation, limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM gads_audit_log
                 ORDER BY created_at DESC LIMIT ?
            """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def get_pending_approvals() -> list:
    """Fetch audit entries awaiting admin approval."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT a.*, r.primary_campaign, r.started_at AS run_started_at
            FROM gads_audit_log a
            LEFT JOIN gads_optimizer_runs r ON r.run_id = a.optimizer_run_id
            WHERE a.execution_result = 'pending_approval'
            ORDER BY a.created_at DESC
        """).fetchall()
        return [dict(r) for r in rows]


def expire_stale_audit_rows(max_age_hours: int = 48) -> int:
    """
    Mark pending_approval rows older than max_age_hours as 'expired'.
    Returns count of rows expired.
    """
    now = _now()
    with _conn() as conn:
        cursor = conn.execute("""
            UPDATE gads_audit_log
               SET execution_result='expired', updated_at=?
             WHERE execution_result='pending_approval'
               AND created_at < datetime('now', ? || ' hours')
        """, (now, f"-{max_age_hours}"))
        return cursor.rowcount


def create_optimizer_run(
    run_id: str,
    trigger: str = "scheduler_7am",
    primary_campaign: str = "",
) -> None:
    """Create a new optimizer run record at the start of optimize_campaign()."""
    now = _now()
    with _conn() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO gads_optimizer_runs
                (run_id, started_at, mode, trigger, primary_campaign,
                 summary_json, report_json)
            VALUES (?,?,?,?,?,'{}','{}')
        """, (run_id, now, "pending_approval", trigger, primary_campaign))


def update_optimizer_run(
    run_id: str,
    summary_json: str = "",
    report_json: str = "",
    actions_pending: int = 0,
    actions_executed: int = 0,
    actions_blocked: int = 0,
    actions_errored: int = 0,
    mode: str = "",
    error: str = "",
) -> None:
    """Update an optimizer run record when it completes."""
    now = _now()
    with _conn() as conn:
        sets = []
        params = []
        if summary_json:
            sets.append("summary_json=?"); params.append(summary_json)
        if report_json:
            sets.append("report_json=?"); params.append(report_json)
        if mode:
            sets.append("mode=?"); params.append(mode)
        if error:
            sets.append("error=?"); params.append(error)
        sets += ["actions_pending=?", "actions_executed=?",
                 "actions_blocked=?", "actions_errored=?",
                 "completed_at=?"]
        params += [actions_pending, actions_executed, actions_blocked, actions_errored, now]
        params.append(run_id)
        conn.execute(
            f"UPDATE gads_optimizer_runs SET {', '.join(sets)} WHERE run_id=?",
            params
        )


def get_optimizer_runs(limit: int = 20) -> list:
    """Return recent optimizer runs, newest first."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM gads_optimizer_runs ORDER BY started_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_optimizer_run(run_id: str) -> Optional[dict]:
    """Fetch a single optimizer run by run_id."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM gads_optimizer_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        return dict(row) if row else None


# ─── Spend Guardrails ────────────────────────────────────────────────────────

def get_spend_guardrail(campaign_id: str) -> Optional[float]:
    """Return daily_cap_usd for a campaign, or None if no guardrail set."""
    with _conn() as conn:
        row = conn.execute("""
            SELECT daily_cap_usd FROM gads_spend_guardrails
            WHERE campaign_id=? AND is_active=1
        """, (campaign_id,)).fetchone()
        return row["daily_cap_usd"] if row else None


def get_all_spend_guardrails() -> list:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM gads_spend_guardrails ORDER BY campaign_name"
        ).fetchall()
        return [dict(r) for r in rows]


def upsert_spend_guardrail(campaign_id: str, campaign_name: str, daily_cap_usd: float) -> dict:
    now = _now()
    with _conn() as conn:
        conn.execute("""
            INSERT INTO gads_spend_guardrails
                (campaign_id, campaign_name, daily_cap_usd, is_active, created_at, updated_at)
            VALUES (?,?,?,1,?,?)
            ON CONFLICT(campaign_id) DO UPDATE SET
                campaign_name=excluded.campaign_name,
                daily_cap_usd=excluded.daily_cap_usd,
                is_active=1,
                updated_at=excluded.updated_at
        """, (campaign_id, campaign_name, daily_cap_usd, now, now))
        row = conn.execute(
            "SELECT * FROM gads_spend_guardrails WHERE campaign_id=?", (campaign_id,)
        ).fetchone()
        return dict(row)


# ─── Phase 3: Ad Creative Tables ─────────────────────────────────────────────

def save_gads_ads(ads: list, customer_id: str = "") -> int:
    """Upsert ad creative metadata rows. Returns count upserted."""
    if not ads:
        return 0
    now = _now()
    with _conn() as conn:
        for ad in ads:
            conn.execute("""
                INSERT INTO gads_ads
                    (ad_id, customer_id, ad_name, ad_group_id, ad_group_name,
                     campaign_id, campaign_name, status, ad_type,
                     headline_1, headline_2, headline_3,
                     description_1, description_2, final_url,
                     assets_json, synced_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(ad_id) DO UPDATE SET
                    customer_id   = excluded.customer_id,
                    ad_name       = excluded.ad_name,
                    ad_group_id   = excluded.ad_group_id,
                    ad_group_name = excluded.ad_group_name,
                    campaign_id   = excluded.campaign_id,
                    campaign_name = excluded.campaign_name,
                    status        = excluded.status,
                    ad_type       = excluded.ad_type,
                    headline_1    = excluded.headline_1,
                    headline_2    = excluded.headline_2,
                    headline_3    = excluded.headline_3,
                    description_1 = excluded.description_1,
                    description_2 = excluded.description_2,
                    final_url     = excluded.final_url,
                    assets_json   = excluded.assets_json,
                    synced_at     = excluded.synced_at
            """, (
                ad["ad_id"],
                ad.get("customer_id", customer_id),
                ad.get("ad_name", ""),
                ad.get("ad_group_id", ""),
                ad.get("ad_group_name", ""),
                ad.get("campaign_id", ""),
                ad.get("campaign_name", ""),
                ad.get("status", ""),
                ad.get("ad_type", ""),
                ad.get("headline_1", ""),
                ad.get("headline_2", ""),
                ad.get("headline_3", ""),
                ad.get("description_1", ""),
                ad.get("description_2", ""),
                ad.get("final_url", ""),
                json.dumps(ad.get("assets_json", [])),
                now,
            ))
    return len(ads)


def save_gads_ad_metrics(rows: list) -> int:
    """Upsert daily ad metrics. Returns count upserted."""
    if not rows:
        return 0
    with _conn() as conn:
        for row in rows:
            conn.execute("""
                INSERT INTO gads_ad_metrics
                    (ad_id, date, impressions, clicks, cost_micros, conversions)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(ad_id, date) DO UPDATE SET
                    impressions = excluded.impressions,
                    clicks      = excluded.clicks,
                    cost_micros = excluded.cost_micros,
                    conversions = excluded.conversions
            """, (
                row["ad_id"],
                row["date"],
                int(row.get("impressions", 0)),
                int(row.get("clicks", 0)),
                int(row.get("cost_micros", 0)),
                float(row.get("conversions", 0.0)),
            ))
    return len(rows)


def get_ads_with_metrics(days: int = 30) -> list:
    """
    Return all ad creatives joined with aggregated metrics for last N days,
    plus lead count from leads table joined on ad_id.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    with _conn() as conn:
        rows = conn.execute("""
            SELECT
                a.ad_id, a.ad_name, a.ad_group_id, a.ad_group_name,
                a.campaign_id, a.campaign_name, a.status, a.ad_type,
                a.headline_1, a.headline_2, a.headline_3,
                a.description_1, a.description_2, a.final_url, a.assets_json,
                COALESCE(SUM(m.impressions), 0) AS impressions,
                COALESCE(SUM(m.clicks), 0)      AS clicks,
                COALESCE(SUM(m.cost_micros), 0) AS cost_micros,
                COALESCE(SUM(m.conversions), 0) AS conversions,
                COALESCE(lc.lead_count, 0)       AS leads
            FROM gads_ads a
            LEFT JOIN gads_ad_metrics m
                ON m.ad_id = a.ad_id AND m.date >= ?
            LEFT JOIN (
                SELECT ad_id, COUNT(*) AS lead_count
                FROM leads
                WHERE ad_id != '' AND created_at >= ?
                GROUP BY ad_id
            ) lc ON lc.ad_id = a.ad_id
            GROUP BY a.ad_id
            ORDER BY cost_micros DESC
        """, (cutoff, cutoff)).fetchall()
        return [dict(r) for r in rows]


def get_ad_metrics_series(ad_id: str, days: int = 30) -> list:
    """Return daily metrics time-series for one ad."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    with _conn() as conn:
        rows = conn.execute("""
            SELECT date, impressions, clicks, cost_micros, conversions
            FROM gads_ad_metrics
            WHERE ad_id = ? AND date >= ?
            ORDER BY date ASC
        """, (ad_id, cutoff)).fetchall()
        return [dict(r) for r in rows]


# ─── Step 10: TCPA Stop Conditions ───────────────────────────────────────────

def get_lead_by_phone(phone: str) -> Optional[dict]:
    """Look up a lead by normalized phone number.
    Uses _hash_phone() (last-10-digit normalization) so E.164 (+15083184477)
    and local (5083184477) resolve to the same hash.
    """
    if not phone:
        return None
    digits = "".join(c for c in phone if c.isdigit())
    if not digits:
        return None
    # Normalize to last 10 for LIKE fallback
    suffix = digits[-10:] if len(digits) >= 10 else digits
    with _conn() as conn:
        # Hash match — normalized to last-10 on both sides
        phone_hash = _hash_phone(phone)
        row = conn.execute(
            "SELECT * FROM leads WHERE phone_hash=? ORDER BY created_at DESC LIMIT 1",
            (phone_hash,)
        ).fetchone()
        if row:
            return dict(row)
        # Fallback: LIKE match on stored phone (covers legacy rows before backfill)
        row = conn.execute(
            "SELECT * FROM leads WHERE phone LIKE ? ORDER BY created_at DESC LIMIT 1",
            (f"%{suffix}",)
        ).fetchone()
        return dict(row) if row else None


def set_lead_dnd(lead_id: str, channel: str, reason: str = "STOP keyword") -> None:
    """
    Mark a lead as DND for a channel. Reuses unsubscribed_sms / unsubscribed_email columns.
    Also stamps dnd_reason / dnd_set_at for audit purposes.
    Does NOT log a lifecycle_event — callers (stop_engine.handle_event) are
    responsible for event logging to avoid duplicate sms_stop/email_unsub entries.
    """
    now = _now()
    field = "unsubscribed_sms" if channel == "sms" else "unsubscribed_email"
    with _conn() as conn:
        conn.execute(
            f"UPDATE leads SET {field}=1, dnd_reason=?, dnd_set_at=?, updated_at=? WHERE id=?",
            (reason, now, now, lead_id)
        )
        conn.execute(
            "INSERT INTO unsubscribes (lead_id, channel, reason, created_at) VALUES (?,?,?,?)",
            (lead_id, channel, reason, now)
        )


def pause_lead(lead_id: str, reason: str = "admin", until: str = "") -> None:
    """Pause a lead's follow-up sequence. until='' means indefinite."""
    now = _now()
    with _conn() as conn:
        conn.execute(
            "UPDATE leads SET paused_at=?, paused_reason=?, paused_until=?, updated_at=? WHERE id=?",
            (now, reason, until, now, lead_id)
        )
    add_event(lead_id, "lead_paused", detail=json.dumps({"reason": reason, "until": until}),
              source="admin")


def resume_lead(lead_id: str) -> None:
    """Resume a paused lead."""
    now = _now()
    with _conn() as conn:
        conn.execute(
            "UPDATE leads SET paused_at='', paused_reason='', paused_until='', updated_at=? WHERE id=?",
            (now, lead_id)
        )
    add_event(lead_id, "lead_resumed", source="admin")


def cancel_queue_rows(lead_id: str, channels: Optional[list] = None, reason: str = "") -> int:
    """
    Cancel all pending follow-up queue rows for a lead (optionally filtered by channel).
    Uses BEGIN IMMEDIATE to prevent concurrent dispatch from racing.
    Returns count of rows cancelled.
    """
    now = _now()
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if channels:
            placeholders = ",".join("?" * len(channels))
            cursor = conn.execute(
                f"UPDATE follow_up_queue SET status='cancelled', cancelled_at=?, cancellation_reason=? "
                f"WHERE lead_id=? AND status='pending' AND channel IN ({placeholders})",
                [now, reason, lead_id] + list(channels)
            )
        else:
            cursor = conn.execute(
                "UPDATE follow_up_queue SET status='cancelled', cancelled_at=?, cancellation_reason=? "
                "WHERE lead_id=? AND status='pending'",
                (now, reason, lead_id)
            )
        count = cursor.rowcount
        conn.commit()
        return count
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def insert_sms_message(
    lead_id: Optional[str],
    direction: str,
    from_number: str,
    to_number: str,
    body: str,
    twilio_sid: str = "",
) -> int:
    """Store an inbound or outbound SMS message. Returns the new row id."""
    now = _now()
    with _conn() as conn:
        cursor = conn.execute("""
            INSERT INTO sms_messages
                (lead_id, direction, from_number, to_number, body, twilio_sid, received_at)
            VALUES (?,?,?,?,?,?,?)
        """, (lead_id, direction, from_number, to_number, body, twilio_sid, now))
        return cursor.lastrowid


def add_lead_event(lead_id: str, event_type: str, detail: str = "", source: str = "system") -> dict:
    """Thin wrapper around add_event for use by stop_engine and webhooks."""
    return add_event(lead_id, event_type, detail=detail, source=source)
