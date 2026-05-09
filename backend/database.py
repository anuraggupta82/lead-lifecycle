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
    tags        TEXT DEFAULT '[]',
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

-- M5 fix: UNIQUE across (keyword_text, match_type, ad_group_name, campaign_name, days)
-- so the same keyword in different ad groups / match types gets separate rows.
CREATE UNIQUE INDEX IF NOT EXISTS idx_gads_kw_cache_key
    ON gads_keywords_cache(keyword_text, match_type, ad_group_name, campaign_name, days);

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

CREATE TABLE IF NOT EXISTS gads_impact_history (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                  TEXT NOT NULL,                   -- FK to gads_optimizer_runs.run_id
    run_date                TEXT NOT NULL,                   -- YYYY-MM-DD
    -- Batch-level estimated impact (sum of all recs in this run)
    estimated_waste_saved_usd  REAL DEFAULT 0.0,            -- sum of waste_reduction recs
    estimated_cpl_improvement  REAL DEFAULT 0.0,            -- weighted avg CPL delta
    estimated_coverage_gain    REAL DEFAULT 0.0,            -- impression share gain estimate
    total_recs              INTEGER DEFAULT 0,
    approved_recs           INTEGER DEFAULT 0,
    rejected_recs           INTEGER DEFAULT 0,
    -- Cumulative tracking (running totals)
    cum_estimated_savings   REAL DEFAULT 0.0,               -- cumulative approved savings to date
    -- Gap analysis vs Excellence Report benchmarks (JSON: {metric: {current, target, gap}})
    benchmark_gaps_json     TEXT DEFAULT '{}',
    -- Breakdown by impact type (JSON: {waste_reduction: usd, conversion_lift: usd, ...})
    impact_by_type_json     TEXT DEFAULT '{}',
    -- Actual measured impact after execution (filled in by a future measurement pass)
    actual_impact_json      TEXT DEFAULT '{}',
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_impact_history_run ON gads_impact_history(run_id);
CREATE INDEX IF NOT EXISTS idx_impact_history_date ON gads_impact_history(run_date DESC);

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

CREATE TABLE IF NOT EXISTS lead_calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id     TEXT NOT NULL,
    direction   TEXT NOT NULL DEFAULT 'outbound',  -- 'outbound' | 'inbound'
    outcome     TEXT NOT NULL DEFAULT 'no_answer', -- 'spoke' | 'left_vm' | 'no_answer' | 'callback_scheduled'
    duration_sec INTEGER DEFAULT 0,
    notes       TEXT DEFAULT '',
    logged_by   TEXT DEFAULT 'admin',
    logged_at   TEXT NOT NULL,
    FOREIGN KEY (lead_id) REFERENCES leads(id)
);

CREATE INDEX IF NOT EXISTS idx_lead_calls_lead ON lead_calls(lead_id);
CREATE INDEX IF NOT EXISTS idx_lead_calls_logged ON lead_calls(logged_at DESC);

-- ── Managed Campaigns ─────────────────────────────────────────────────────────
-- Tracks manually created and linked campaigns for attribution + planning.
-- campaign_id is a logical key (GAds numeric ID or auto-generated slug); NOT always a GAds ID.
-- Lead attribution still matches on lead.campaign_name — rename a campaign carefully.
CREATE TABLE IF NOT EXISTS campaigns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id     TEXT NOT NULL UNIQUE,   -- GAds ID or auto-slug; logical key
    campaign_name   TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'DRAFT',   -- DRAFT, ACTIVE, PAUSED, COMPLETED, ARCHIVED
    campaign_type   TEXT NOT NULL DEFAULT 'MANUAL',  -- MANUAL, GOOGLE_ADS, META, EMAIL
    -- Dental-specific fields
    service_focus   TEXT DEFAULT '',        -- Implants, Invisalign, Whitening, Emergency, New Patient, Hygiene, Cosmetic
    promo_offer     TEXT DEFAULT '',        -- e.g. "$99 exam + X-ray", "Free implant consult"
    target_audience TEXT DEFAULT '',        -- free-text description
    -- Planning
    objective       TEXT DEFAULT '',        -- e.g. "20 implant consults in 30 days"
    monthly_budget  REAL DEFAULT 0.0,
    expected_cpl    REAL DEFAULT 0.0,       -- target cost per lead $
    start_date      TEXT DEFAULT '',        -- YYYY-MM-DD
    end_date        TEXT DEFAULT '',        -- YYYY-MM-DD (optional)
    landing_page    TEXT DEFAULT '',        -- URL
    notes           TEXT DEFAULT '',
    -- AI Review
    ai_review_enabled INTEGER NOT NULL DEFAULT 0,  -- 1 = Opus actively monitors this campaign
    -- Meta
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns(status);
CREATE INDEX IF NOT EXISTS idx_campaigns_type   ON campaigns(campaign_type);

-- ── Phase A: Feedback Loop Foundation ────────────────────────────────────────

-- Append-only log: every OD appointment that can be attributed to a keyword click.
-- Written by od_matcher.py when it finds a patient match for a lead that has a gclid.
CREATE TABLE IF NOT EXISTS keyword_production_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at           TEXT NOT NULL,                  -- ISO timestamp (UTC)
    lead_id             TEXT NOT NULL,                  -- FK leads.id
    keyword_text        TEXT NOT NULL DEFAULT '',       -- matched keyword (lowercase)
    match_type          TEXT NOT NULL DEFAULT '',       -- BROAD, PHRASE, EXACT
    campaign_id         TEXT NOT NULL DEFAULT '',       -- GAds campaign ID
    campaign_name       TEXT NOT NULL DEFAULT '',
    ad_group_name       TEXT NOT NULL DEFAULT '',
    gclid               TEXT NOT NULL DEFAULT '',       -- Google click ID from lead
    od_patient_num      TEXT NOT NULL DEFAULT '',       -- OD patient number matched
    production_amount   REAL NOT NULL DEFAULT 0.0,      -- $ production from OD
    procedure_codes     TEXT NOT NULL DEFAULT '[]',     -- JSON array of procedure codes
    match_method        TEXT NOT NULL DEFAULT '',       -- 'email','phone','manual'
    appointment_date    TEXT NOT NULL DEFAULT '',       -- YYYY-MM-DD of OD appointment
    FOREIGN KEY (lead_id) REFERENCES leads(id),
    -- M4 fix: dedup guard — one row per (lead, patient); INSERT OR REPLACE updates production
    UNIQUE(lead_id, od_patient_num)
);

CREATE INDEX IF NOT EXISTS idx_kpl_keyword   ON keyword_production_log(keyword_text, campaign_id);
CREATE INDEX IF NOT EXISTS idx_kpl_lead      ON keyword_production_log(lead_id);
CREATE INDEX IF NOT EXISTS idx_kpl_logged    ON keyword_production_log(logged_at DESC);
CREATE INDEX IF NOT EXISTS idx_kpl_gclid     ON keyword_production_log(gclid);

-- Snapshot table: captured when admin clicks Apply on an optimizer recommendation.
-- Updated at T+7, T+30, T+90 to track whether the action improved performance.
CREATE TABLE IF NOT EXISTS applied_outcomes (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id           TEXT NOT NULL UNIQUE,           -- FK gads_audit_log.action_id
    applied_at          TEXT NOT NULL,                  -- when Apply was clicked (UTC)
    operation           TEXT NOT NULL,                  -- pause_keyword, set_budget, add_keyword, etc.
    entity_type         TEXT NOT NULL,                  -- keyword, campaign, ad_group, ad
    entity_id           TEXT NOT NULL,                  -- resource_name or id
    entity_name         TEXT NOT NULL,                  -- human-readable
    campaign_id         TEXT NOT NULL DEFAULT '',
    campaign_name       TEXT NOT NULL DEFAULT '',
    -- Pre-action snapshot (from before_state_json at apply time)
    pre_impressions_7d  INTEGER DEFAULT 0,
    pre_clicks_7d       INTEGER DEFAULT 0,
    pre_cost_7d         REAL DEFAULT 0.0,
    pre_conversions_7d  REAL DEFAULT 0.0,
    pre_cpc             REAL DEFAULT 0.0,
    -- Outcome snapshots (filled by nightly job at T+7, T+30, T+90)
    post_7d_json        TEXT DEFAULT '{}',              -- {impressions, clicks, cost, conversions}
    post_30d_json       TEXT DEFAULT '{}',
    post_90d_json       TEXT DEFAULT '{}',
    -- Status tracking
    outcome_7d_at       TEXT DEFAULT '',                -- when post_7d_json was filled
    outcome_30d_at      TEXT DEFAULT '',
    outcome_90d_at      TEXT DEFAULT '',
    verdict             TEXT DEFAULT 'pending',         -- pending, improved, neutral, degraded
    verdict_reason      TEXT DEFAULT '',
    -- AI recommendation context
    ai_reason           TEXT DEFAULT '',                -- why AI recommended this action
    optimizer_run_id    TEXT DEFAULT ''                 -- FK gads_optimizer_runs.run_id
);

CREATE INDEX IF NOT EXISTS idx_ao_applied    ON applied_outcomes(applied_at DESC);
CREATE INDEX IF NOT EXISTS idx_ao_campaign   ON applied_outcomes(campaign_id);
CREATE INDEX IF NOT EXISTS idx_ao_verdict    ON applied_outcomes(verdict);
CREATE INDEX IF NOT EXISTS idx_ao_entity     ON applied_outcomes(entity_id);

-- Denormalized intelligence table: one row per (keyword, date), joining GAds stats +
-- OD production + GA4 session quality. Rebuilt nightly by the intelligence job.
CREATE TABLE IF NOT EXISTS keyword_intelligence (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    date                    TEXT NOT NULL,              -- YYYY-MM-DD (stats period end date)
    keyword_text            TEXT NOT NULL,
    match_type              TEXT NOT NULL DEFAULT '',
    campaign_id             TEXT NOT NULL DEFAULT '',
    campaign_name           TEXT NOT NULL DEFAULT '',
    ad_group_name           TEXT NOT NULL DEFAULT '',
    -- GAds performance (from gads_keywords_cache)
    impressions             INTEGER DEFAULT 0,
    clicks                  INTEGER DEFAULT 0,
    cost_usd                REAL DEFAULT 0.0,
    avg_cpc                 REAL DEFAULT 0.0,
    conversions             REAL DEFAULT 0.0,
    quality_score           INTEGER DEFAULT 0,
    impression_share        REAL DEFAULT 0.0,
    -- OD production (from keyword_production_log, rolling 90d)
    od_appointments         INTEGER DEFAULT 0,          -- count of OD matches
    od_production_total     REAL DEFAULT 0.0,           -- sum of production_amount
    od_production_per_click REAL DEFAULT 0.0,           -- od_production_total / clicks (0 if no clicks)
    -- GA4 session quality (from ga4_cache, rolling 30d)
    ga4_sessions            INTEGER DEFAULT 0,
    ga4_avg_duration_sec    REAL DEFAULT 0.0,
    ga4_bounce_rate         REAL DEFAULT 0.0,           -- 0.0–1.0
    ga4_lead_events         INTEGER DEFAULT 0,          -- generate_lead events
    session_quality_score   REAL DEFAULT 0.0,           -- composite SQS (0–100)
    -- Decision history (from gads_audit_log, last 90d)
    times_recommended       INTEGER DEFAULT 0,          -- how many times AI recommended an action
    times_applied           INTEGER DEFAULT 0,          -- how many times admin applied
    times_rejected          INTEGER DEFAULT 0,          -- how many times admin rejected
    last_decision_at        TEXT DEFAULT '',            -- most recent Apply/Reject timestamp
    last_decision           TEXT DEFAULT '',            -- 'applied' or 'rejected'
    -- Computed signals
    true_roas               REAL DEFAULT 0.0,           -- od_production_total / cost_usd (0 if no cost)
    data_age_days           INTEGER DEFAULT 0,          -- days since keyword first seen
    confidence_tier         TEXT DEFAULT 'low',         -- low (<14d) | medium (14-90d) | high (90d+)
    rebuilt_at              TEXT NOT NULL               -- when this row was last rebuilt
    ,UNIQUE(keyword_text, match_type, campaign_id, date)
);

CREATE INDEX IF NOT EXISTS idx_ki_keyword    ON keyword_intelligence(keyword_text, campaign_id);
CREATE INDEX IF NOT EXISTS idx_ki_date       ON keyword_intelligence(date DESC);
CREATE INDEX IF NOT EXISTS idx_ki_campaign   ON keyword_intelligence(campaign_id, date DESC);
CREATE INDEX IF NOT EXISTS idx_ki_roas       ON keyword_intelligence(true_roas DESC);
CREATE INDEX IF NOT EXISTS idx_ki_sqs        ON keyword_intelligence(session_quality_score DESC);

-- ── Mango Voice call records (PBX metadata, synced every 5 min) ──────────────
CREATE TABLE IF NOT EXISTS mango_calls (
    uuid                 TEXT PRIMARY KEY,
    started_at           TEXT NOT NULL,
    ended_at             TEXT,
    direction            TEXT NOT NULL DEFAULT 'inbound',
    from_number          TEXT,
    to_number            TEXT,
    caller_id_name       TEXT,
    caller_id_number     TEXT,
    destination_number   TEXT,
    extension_number     TEXT,
    answered_by          TEXT,
    duration_sec         INTEGER DEFAULT 0,
    status               TEXT,
    recording_url        TEXT,
    recording_local_path TEXT,
    lead_id              TEXT,
    gads_call_id         TEXT,
    match_confidence     REAL,
    match_method         TEXT,
    -- Call analysis (Phase 2)
    call_transcript      TEXT,
    call_summary         TEXT,
    lead_quality         TEXT,   -- new_patient_inquiry|existing_patient|emergency|provider_referral|spam|wrong_number|other_admin
    handling_grade       TEXT,   -- excellent|good|needs_coaching|poor (only for new_patient/emergency)
    booked_outcome       TEXT,   -- booked|quoted_callback|declined|no_action_needed|abandoned
    call_category        TEXT,   -- free-form AI category label
    transcription_status TEXT DEFAULT 'pending',  -- pending|in_progress|done|failed|skipped_too_short|skipped_no_audio
    transcript_model     TEXT,
    transcribed_at       TEXT,
    od_appointment_id    TEXT,
    grade_overridden_by  TEXT,
    grade_overridden_at  TEXT,
    grade_override_reason TEXT,
    raw_payload          TEXT,   -- JSON blob of original Mango API response (for debugging field names)
    synced_at            TEXT NOT NULL,
    updated_at           TEXT
);
CREATE INDEX IF NOT EXISTS idx_mango_calls_started    ON mango_calls(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_mango_calls_lead       ON mango_calls(lead_id);
CREATE INDEX IF NOT EXISTS idx_mango_calls_from       ON mango_calls(from_number);
CREATE INDEX IF NOT EXISTS idx_mango_calls_status     ON mango_calls(status);
CREATE INDEX IF NOT EXISTS idx_mango_calls_direction  ON mango_calls(direction);

-- ── Google Ads call_view — calls placed directly from the ad ─────────────────
CREATE TABLE IF NOT EXISTS gads_call_view (
    call_id                      TEXT PRIMARY KEY,
    customer_id                  TEXT,
    campaign_id                  TEXT,
    campaign_name                TEXT,
    caller_country_code          TEXT,
    caller_area_code             TEXT,
    call_duration_sec            INTEGER DEFAULT 0,
    call_status                  TEXT,
    call_type                    TEXT,
    start_call_date_time         TEXT,
    synced_at                    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gads_cv_started  ON gads_call_view(start_call_date_time DESC);
CREATE INDEX IF NOT EXISTS idx_gads_cv_area     ON gads_call_view(caller_area_code);
CREATE INDEX IF NOT EXISTS idx_gads_cv_campaign ON gads_call_view(campaign_id);

CREATE TABLE IF NOT EXISTS gads_google_recs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at TEXT NOT NULL,
    resource_name TEXT NOT NULL UNIQUE,
    rec_type TEXT NOT NULL,
    campaign_resource TEXT DEFAULT '',
    campaign_name TEXT DEFAULT '',
    ad_group_resource TEXT DEFAULT '',
    title TEXT DEFAULT '',
    description TEXT DEFAULT '',
    impact_json TEXT DEFAULT '{}',
    details_json TEXT DEFAULT '{}',
    claude_verdict TEXT DEFAULT '',
    claude_reasoning TEXT DEFAULT '',
    dismissed INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_google_recs_fetched ON gads_google_recs(fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_google_recs_dismissed ON gads_google_recs(dismissed);

-- ─── Domain Registry + Crawler ───────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS domain_registry (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    domain_url      TEXT NOT NULL UNIQUE,   -- e.g. https://graftondentalcare.com
    label           TEXT DEFAULT '',        -- friendly name, e.g. "Main Practice Site"
    domain_type     TEXT DEFAULT 'practice',-- 'practice' | 'landing' | 'competitor'
    crawl_enabled   INTEGER DEFAULT 1,      -- 0 = disabled (skip scheduler)
    crawl_depth     INTEGER DEFAULT 20,     -- max pages to crawl per run
    last_crawled_at TEXT DEFAULT '',        -- ISO timestamp of last successful crawl
    crawl_status    TEXT DEFAULT 'idle',    -- 'idle' | 'running' | 'error' | 'done'
    pages_found     INTEGER DEFAULT 0,      -- pages stored from last crawl
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS domain_pages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    domain_id       INTEGER NOT NULL REFERENCES domain_registry(id) ON DELETE CASCADE,
    page_url        TEXT NOT NULL,
    page_title      TEXT DEFAULT '',
    meta_description TEXT DEFAULT '',
    h1_text         TEXT DEFAULT '',        -- first H1 on the page
    body_excerpt    TEXT DEFAULT '',        -- first ~2000 chars of cleaned body text
    word_count      INTEGER DEFAULT 0,
    has_form        INTEGER DEFAULT 0,      -- 1 if a <form> element was found
    cta_phrases     TEXT DEFAULT '[]',      -- JSON array of CTA button/link texts
    internal_links  TEXT DEFAULT '[]',      -- JSON array of internal hrefs found
    content_hash    TEXT DEFAULT '',        -- SHA-256 of raw HTML — used for incremental diff
    crawled_at      TEXT NOT NULL,
    UNIQUE(domain_id, page_url)
);

CREATE INDEX IF NOT EXISTS idx_domain_pages_domain ON domain_pages(domain_id);
CREATE INDEX IF NOT EXISTS idx_domain_pages_crawled ON domain_pages(crawled_at DESC);

CREATE TABLE IF NOT EXISTS domain_crawl_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    domain_id       INTEGER NOT NULL REFERENCES domain_registry(id) ON DELETE CASCADE,
    started_at      TEXT NOT NULL,
    finished_at     TEXT DEFAULT '',
    pages_crawled   INTEGER DEFAULT 0,
    pages_failed    INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'running', -- 'running' | 'done' | 'error'
    error_msg       TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_crawl_log_domain ON domain_crawl_log(domain_id, started_at DESC);
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
    db_dir = os.path.dirname(settings.db_path)
    if db_dir:
        try:
            os.makedirs(db_dir, exist_ok=True)
        except Exception as _mk_err:
            import logging as _logging
            _logging.getLogger(__name__).error(f"_conn: makedirs failed for {db_dir!r}: {_mk_err}")
    conn = sqlite3.connect(settings.db_path, check_same_thread=False, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_db():
    with _conn() as conn:
        # WAL mode is a database-level persistent setting — set once at startup only
        conn.execute("PRAGMA journal_mode=WAL")
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
        # Next-action tracking
        ("next_action_at",      "TEXT DEFAULT ''"),
        ("next_action_note",    "TEXT DEFAULT ''"),
        # OD relationship classification
        ("od_relationship",     "TEXT DEFAULT 'cold'"),
        # Lead tags (JSON array of strings)
        ("tags",                "TEXT DEFAULT '[]'"),
        # AI Max search term type — "exact","phrase","broad","ai_max",""
        ("search_term_type",    "TEXT DEFAULT ''"),
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

    # Add read_at column to sms_messages if missing
    sms_cols = {row[1] for row in conn.execute("PRAGMA table_info(sms_messages)").fetchall()}
    if "read_at" not in sms_cols:
        conn.execute("ALTER TABLE sms_messages ADD COLUMN read_at TEXT DEFAULT NULL")

    # Add read_at column to messages (email inbox) if missing
    msg_cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "read_at" not in msg_cols:
        conn.execute("ALTER TABLE messages ADD COLUMN read_at TEXT DEFAULT NULL")

    # Create lead_calls table if not exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lead_calls (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id      TEXT NOT NULL,
            direction    TEXT NOT NULL DEFAULT 'outbound',
            outcome      TEXT NOT NULL DEFAULT 'no_answer',
            duration_sec INTEGER DEFAULT 0,
            notes        TEXT DEFAULT '',
            logged_by    TEXT DEFAULT 'admin',
            logged_at    TEXT NOT NULL,
            FOREIGN KEY (lead_id) REFERENCES leads(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lead_calls_lead ON lead_calls(lead_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lead_calls_logged ON lead_calls(logged_at DESC)")

    # Managed campaigns table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS campaigns (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id     TEXT NOT NULL UNIQUE,
            campaign_name   TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'DRAFT',
            campaign_type   TEXT NOT NULL DEFAULT 'MANUAL',
            service_focus   TEXT DEFAULT '',
            promo_offer     TEXT DEFAULT '',
            target_audience TEXT DEFAULT '',
            objective       TEXT DEFAULT '',
            monthly_budget  REAL DEFAULT 0.0,
            expected_cpl    REAL DEFAULT 0.0,
            start_date      TEXT DEFAULT '',
            end_date        TEXT DEFAULT '',
            landing_page    TEXT DEFAULT '',
            notes           TEXT DEFAULT '',
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_campaigns_type   ON campaigns(campaign_type)")

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
    # M5 fix: migrate the narrow UNIQUE index to include match_type, ad_group_name, campaign_name
    # so same keyword in different ad groups / match types gets separate rows.
    try:
        # Check if old narrow index exists and current index is still narrow
        existing_indexes = {row[1] for row in conn.execute(
            "SELECT * FROM sqlite_master WHERE type='index' AND tbl_name='gads_keywords_cache'"
        ).fetchall()}
        # Drop the old narrow index if present (may not exist in fresh DBs using new schema)
        conn.execute("DROP INDEX IF EXISTS idx_gads_kw_cache_key")
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_gads_kw_cache_key
            ON gads_keywords_cache(keyword_text, match_type, ad_group_name, campaign_name, days)
        """)
    except Exception as _kw_idx_err:
        print(f"[db] gads_keywords_cache UNIQUE index migration (non-fatal): {_kw_idx_err}")
        # Fallback: just ensure the new index exists
        try:
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_gads_kw_cache_key
                ON gads_keywords_cache(keyword_text, match_type, ad_group_name, campaign_name, days)
            """)
        except Exception:
            pass

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

    # Add workflow_id to campaigns (Campaign Wizard migration)
    camp_cols = {row[1] for row in conn.execute("PRAGMA table_info(campaigns)").fetchall()}
    if "workflow_id" not in camp_cols:
        conn.execute("ALTER TABLE campaigns ADD COLUMN workflow_id INTEGER DEFAULT NULL")

    # Add strategy_json to campaigns (Opus Strategy migration)
    camp_cols = {row[1] for row in conn.execute("PRAGMA table_info(campaigns)").fetchall()}
    if "strategy_json" not in camp_cols:
        conn.execute("ALTER TABLE campaigns ADD COLUMN strategy_json TEXT DEFAULT NULL")

    # Add Google Ads resource columns to campaigns (Campaign Controls migration)
    camp_cols = {row[1] for row in conn.execute("PRAGMA table_info(campaigns)").fetchall()}
    if "gads_campaign_resource" not in camp_cols:
        conn.execute("ALTER TABLE campaigns ADD COLUMN gads_campaign_resource TEXT DEFAULT NULL")
    if "gads_campaign_numeric_id" not in camp_cols:
        conn.execute("ALTER TABLE campaigns ADD COLUMN gads_campaign_numeric_id TEXT DEFAULT NULL")
    if "deep_research_json" not in camp_cols:
        conn.execute("ALTER TABLE campaigns ADD COLUMN deep_research_json TEXT DEFAULT NULL")
    if "ai_review_enabled" not in camp_cols:
        conn.execute("ALTER TABLE campaigns ADD COLUMN ai_review_enabled INTEGER NOT NULL DEFAULT 0")
    if "campaign_build_json" not in camp_cols:
        conn.execute("ALTER TABLE campaigns ADD COLUMN campaign_build_json TEXT DEFAULT NULL")

    # AI Max columns (AI Max integration)
    camp_cols = {row[1] for row in conn.execute("PRAGMA table_info(campaigns)").fetchall()}
    if "ai_max_enabled" not in camp_cols:
        conn.execute("ALTER TABLE campaigns ADD COLUMN ai_max_enabled INTEGER NOT NULL DEFAULT 0")

    # Google Ads snapshot columns — stores raw synced state separately from user-edited build
    camp_cols = {row[1] for row in conn.execute("PRAGMA table_info(campaigns)").fetchall()}
    if "gads_campaign_snapshot" not in camp_cols:
        conn.execute("ALTER TABLE campaigns ADD COLUMN gads_campaign_snapshot TEXT DEFAULT NULL")
    if "gads_synced_build_at" not in camp_cols:
        conn.execute("ALTER TABLE campaigns ADD COLUMN gads_synced_build_at TEXT DEFAULT NULL")

    # Geographic targeting — campaign-level field edited from Launch checklist
    camp_cols = {row[1] for row in conn.execute("PRAGMA table_info(campaigns)").fetchall()}
    if "geographic_targeting" not in camp_cols:
        conn.execute("ALTER TABLE campaigns ADD COLUMN geographic_targeting TEXT DEFAULT ''")

    # Launch tab v2 — launch state columns (May 2026)
    camp_cols = {row[1] for row in conn.execute("PRAGMA table_info(campaigns)").fetchall()}
    if "launch_date" not in camp_cols:
        conn.execute("ALTER TABLE campaigns ADD COLUMN launch_date TEXT DEFAULT ''")
    if "call_extension_phone" not in camp_cols:
        conn.execute("ALTER TABLE campaigns ADD COLUMN call_extension_phone TEXT DEFAULT ''")
    if "skip_workflow" not in camp_cols:
        conn.execute("ALTER TABLE campaigns ADD COLUMN skip_workflow INTEGER NOT NULL DEFAULT 0")

    # Sitelinks column — stores JSON list of {title, url, description1, description2}
    camp_cols = {row[1] for row in conn.execute("PRAGMA table_info(campaigns)").fetchall()}
    if "sitelinks" not in camp_cols:
        conn.execute("ALTER TABLE campaigns ADD COLUMN sitelinks TEXT DEFAULT ''")

    # Sitelink library — shared pool of sitelinks that have been pushed to Google Ads
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sitelink_library (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            title         TEXT NOT NULL,
            url           TEXT NOT NULL,
            description1  TEXT NOT NULL DEFAULT '',
            description2  TEXT NOT NULL DEFAULT '',
            use_count     INTEGER NOT NULL DEFAULT 1,
            last_used_at  TEXT NOT NULL,
            UNIQUE(title, url)
        )
    """)

    # Add UNIQUE(lead_id, template) to follow_up_queue if not present
    # SQLite doesn't support adding UNIQUE constraints via ALTER TABLE — create a new index instead
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_follow_up_queue_lead_template
        ON follow_up_queue(lead_id, template)
    """)

    # ── Mango calls analysis columns (Call Analysis Phase 1 — May 2026) ──────────
    mango_cols = {row[1] for row in conn.execute("PRAGMA table_info(mango_calls)").fetchall()}
    mango_new_cols = [
        ("call_transcript",      "TEXT"),
        ("call_summary",         "TEXT"),
        ("lead_quality",         "TEXT"),
        ("handling_grade",       "TEXT"),
        ("booked_outcome",       "TEXT"),
        ("call_category",        "TEXT"),
        ("transcription_status", "TEXT DEFAULT 'pending'"),
        ("transcript_model",     "TEXT"),
        ("transcribed_at",       "TEXT"),
        ("od_appointment_id",    "TEXT"),
        ("grade_overridden_by",  "TEXT"),
        ("grade_overridden_at",  "TEXT"),
        ("grade_override_reason","TEXT"),
        ("raw_payload",          "TEXT"),
    ]
    for col_name, col_type in mango_new_cols:
        if col_name not in mango_cols:
            try:
                conn.execute(f"ALTER TABLE mango_calls ADD COLUMN {col_name} {col_type}")
            except Exception:
                pass
    # Composite index for dashboard filters
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mango_calls_dir_status ON mango_calls(direction, status, started_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mango_calls_transcript ON mango_calls(transcription_status)")

    # ── Mango calls analysis columns Phase 2 (Call Analysis — May 2026) ─────────
    # NOTE: Phase 2 columns must be migrated BEFORE v_campaign_call_stats view is created
    # because the view references grade_overall_score which is a Phase 2 column.
    mango_cols = {row[1] for row in conn.execute("PRAGMA table_info(mango_calls)").fetchall()}
    mango_phase2_cols = [
        ("team_member",                "TEXT DEFAULT ''"),
        ("grade_scores_json",          "TEXT DEFAULT ''"),
        ("grade_overall_score",        "REAL"),
        ("grade_overall_notes",        "TEXT DEFAULT ''"),
        ("grade_recommendations_json", "TEXT DEFAULT ''"),
        ("grade_gradeable",            "INTEGER"),
        ("grade_reason",               "TEXT DEFAULT ''"),
        ("graded_at",                  "TEXT DEFAULT ''"),
        ("summarized_at",              "TEXT DEFAULT ''"),
        ("is_empty",                   "INTEGER DEFAULT 0"),
        ("pipeline_error",             "TEXT DEFAULT ''"),
        ("pipeline_attempts",          "INTEGER DEFAULT 0"),
    ]
    for col_name, col_type in mango_phase2_cols:
        if col_name not in mango_cols:
            try:
                conn.execute(f"ALTER TABLE mango_calls ADD COLUMN {col_name} {col_type}")
            except Exception:
                pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mango_calls_team_member    ON mango_calls(team_member)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mango_calls_graded_at      ON mango_calls(graded_at DESC)")

    # ── Mango calls Phase 3: AI next-action (May 2026) ─────────────────────────
    mango_cols = {row[1] for row in conn.execute("PRAGMA table_info(mango_calls)").fetchall()}
    mango_phase3_cols = [
        ("call_next_action",              "TEXT DEFAULT ''"),
        ("call_next_action_type",         "TEXT DEFAULT ''"),
        ("call_next_action_due",          "TEXT DEFAULT ''"),
        ("call_next_action_completed",    "INTEGER DEFAULT 0"),
        ("call_next_action_completed_at", "TEXT DEFAULT ''"),
        ("call_next_action_completed_by", "TEXT DEFAULT ''"),
        ("call_next_action_suggested_at", "TEXT DEFAULT ''"),
        ("call_next_action_priority",     "TEXT DEFAULT 'soon'"),
        ("call_next_action_reasoning",    "TEXT DEFAULT ''"),
    ]
    for col_name, col_type in mango_phase3_cols:
        if col_name not in mango_cols:
            try:
                conn.execute(f"ALTER TABLE mango_calls ADD COLUMN {col_name} {col_type}")
            except Exception:
                pass

    # ── Mango calls Phase 4: keyword attribution (May 2026) ──────────────────
    mango_cols = {row[1] for row in conn.execute("PRAGMA table_info(mango_calls)").fetchall()}
    mango_phase4_cols = [
        ("attributed_keyword",            "TEXT DEFAULT ''"),
        ("attributed_match_type",         "TEXT DEFAULT ''"),
        ("attributed_ad_group",           "TEXT DEFAULT ''"),
        ("attributed_keyword_method",     "TEXT DEFAULT ''"),   # lead_gclid|lead_phone_recent_click|time_window_gclid|campaign_only
        ("attributed_keyword_confidence", "REAL DEFAULT 0.0"),
    ]
    for col_name, col_type in mango_phase4_cols:
        if col_name not in mango_cols:
            try:
                conn.execute(f"ALTER TABLE mango_calls ADD COLUMN {col_name} {col_type}")
            except Exception:
                pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mango_calls_attr_kw ON mango_calls(attributed_keyword)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mango_calls_attr_method ON mango_calls(attributed_keyword_method)")

    # ── Mango calls patient enrichment (May 2026) ─────────────────────────────
    mango_cols = {row[1] for row in conn.execute("PRAGMA table_info(mango_calls)").fetchall()}
    mango_patient_cols = [
        ("od_patient_num",    "TEXT DEFAULT ''"),   # OD PatNum matched by phone
        ("od_patient_status", "TEXT DEFAULT ''"),   # new_patient|existing_active|existing_inactive|unknown
        ("od_patient_name",   "TEXT DEFAULT ''"),   # First + Last from OD (not from Mango caller_id_name)
        ("od_matched_at",     "TEXT DEFAULT ''"),   # ISO timestamp of last OD match attempt
    ]
    for col_name, col_type in mango_patient_cols:
        if col_name not in mango_cols:
            try:
                conn.execute(f"ALTER TABLE mango_calls ADD COLUMN {col_name} {col_type}")
            except Exception:
                pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mango_calls_od_status ON mango_calls(od_patient_status)")

    # ── gads_clicks — persisted click_view rows (for time-window attribution) ─
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gads_clicks (
            gclid           TEXT PRIMARY KEY,
            click_date      TEXT NOT NULL,
            keyword_text    TEXT DEFAULT '',
            match_type      TEXT DEFAULT '',
            ad_group_name   TEXT DEFAULT '',
            ad_group_id     TEXT DEFAULT '',
            ad_id           TEXT DEFAULT '',
            ad_name         TEXT DEFAULT '',
            campaign_name   TEXT DEFAULT '',
            campaign_id     TEXT DEFAULT '',
            synced_at       TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gads_clicks_date     ON gads_clicks(click_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gads_clicks_kw       ON gads_clicks(keyword_text)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gads_clicks_campaign ON gads_clicks(campaign_id, click_date)")

    # ── v_campaign_call_stats view ────────────────────────────────────────────
    conn.executescript("""
CREATE VIEW IF NOT EXISTS v_campaign_call_stats AS
WITH attributed AS (
  SELECT
    (
      CASE
        WHEN gcv.campaign_id IS NOT NULL THEN
          (SELECT id FROM campaigns WHERE gads_campaign_numeric_id = gcv.campaign_id LIMIT 1)
        ELSE
          (SELECT id FROM campaigns
            WHERE LOWER(TRIM(campaign_name)) = LOWER(TRIM(l.campaign_name)) LIMIT 1)
      END
    )                     AS campaign_id,
    mc.uuid               AS call_uuid,
    mc.lead_id            AS lead_id,
    mc.status             AS call_status,
    mc.duration_sec       AS duration_sec,
    mc.grade_overall_score AS grade
  FROM mango_calls mc
  LEFT JOIN gads_call_view gcv ON gcv.call_id = mc.gads_call_id
  LEFT JOIN leads l            ON l.id = mc.lead_id
  WHERE (gcv.campaign_id IS NOT NULL OR l.campaign_name IS NOT NULL)
),
attrs_with_booked AS (
  SELECT a.*, COALESCE(l.stage,'') AS lead_stage
  FROM attributed a
  LEFT JOIN leads l ON l.id = a.lead_id
  WHERE a.campaign_id IS NOT NULL
)
SELECT
  a.campaign_id,
  c.campaign_name,
  COUNT(*)                                                            AS calls_total,
  SUM(CASE WHEN a.call_status = 'answered' THEN 1 ELSE 0 END)        AS calls_answered,
  SUM(CASE WHEN a.grade IS NOT NULL THEN 1 ELSE 0 END)               AS calls_graded,
  ROUND(AVG(a.grade), 1)                                             AS avg_call_grade,
  SUM(CASE WHEN a.duration_sec >= 60 THEN 1 ELSE 0 END)              AS calls_converted,
  COUNT(DISTINCT CASE WHEN a.lead_stage = 'booked' THEN a.lead_id END) AS calls_booked,
  CASE
    WHEN AVG(a.grade) IS NULL THEN 'no_data'
    WHEN AVG(a.grade) >= 80   THEN 'excellent'
    WHEN AVG(a.grade) >= 65   THEN 'good'
    ELSE                           'needs_work'
  END                                                                 AS call_quality_tier
FROM attrs_with_booked a
JOIN campaigns c ON c.id = a.campaign_id
GROUP BY a.campaign_id, c.campaign_name;
""")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_mango_calls_gads_lead ON mango_calls(gads_call_id, lead_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sms_messages_lead_dir ON sms_messages(lead_id, direction, received_at)")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_mango_calls_overall_score  ON mango_calls(grade_overall_score)")

    # ── AI usage cost ledger ──────────────────────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ai_usage (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            ts                  TEXT NOT NULL,
            api                 TEXT NOT NULL,
            model               TEXT DEFAULT '',
            purpose             TEXT DEFAULT '',
            call_id             TEXT DEFAULT '',
            campaign_id         TEXT DEFAULT '',
            lead_id             TEXT DEFAULT '',
            optimizer_run_id    TEXT DEFAULT '',
            minutes             REAL DEFAULT 0.0,
            input_tokens        INTEGER DEFAULT 0,
            output_tokens       INTEGER DEFAULT 0,
            cache_read_tokens   INTEGER DEFAULT 0,
            cache_write_tokens  INTEGER DEFAULT 0,
            cost_usd            REAL DEFAULT 0.0,
            request_ms          INTEGER DEFAULT 0,
            error               TEXT DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_usage_ts       ON ai_usage(ts DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_usage_api      ON ai_usage(api, ts DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_usage_purpose  ON ai_usage(purpose, ts DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_usage_call     ON ai_usage(call_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_usage_campaign ON ai_usage(campaign_id)")

    # ── Call grading rubric (editable via UI) ─────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS call_grading_criteria (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            weight      INTEGER NOT NULL DEFAULT 0,
            description TEXT NOT NULL DEFAULT '',
            sort_order  INTEGER NOT NULL DEFAULT 0,
            active      INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        )
    """)

    # ── Call team members (editable via UI) ───────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS call_team_members (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            extension   TEXT DEFAULT '',
            active      INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            UNIQUE(name)
        )
    """)

    # ── Bulk processing job state ─────────────────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS call_bulk_jobs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            status          TEXT NOT NULL DEFAULT 'queued',
            date_from       TEXT NOT NULL,
            date_to         TEXT NOT NULL,
            do_grade        INTEGER NOT NULL DEFAULT 0,
            options_json    TEXT DEFAULT '{}',
            total           INTEGER NOT NULL DEFAULT 0,
            done            INTEGER NOT NULL DEFAULT 0,
            graded          INTEGER NOT NULL DEFAULT 0,
            skipped         INTEGER NOT NULL DEFAULT 0,
            errors          INTEGER NOT NULL DEFAULT 0,
            current_label   TEXT DEFAULT '',
            started_at      TEXT NOT NULL,
            finished_at     TEXT DEFAULT '',
            error_msg       TEXT DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_call_bulk_jobs_status ON call_bulk_jobs(status, started_at DESC)")
    # Migration: add options_json column if this is an existing DB without it
    try:
        conn.execute("ALTER TABLE call_bulk_jobs ADD COLUMN options_json TEXT DEFAULT '{}'")
    except Exception:
        pass  # column already exists

    # ── call_to_revenue view ──────────────────────────────────────────────────────
    conn.execute("DROP VIEW IF EXISTS call_to_revenue")
    conn.execute("""
        CREATE VIEW call_to_revenue AS
        SELECT
            mc.uuid                     AS call_uuid,
            mc.started_at               AS call_started_at,
            mc.direction,
            mc.from_number,
            mc.duration_sec,
            mc.team_member,
            mc.lead_quality,
            mc.handling_grade,
            mc.booked_outcome,
            mc.grade_overall_score,
            mc.is_empty,
            mc.lead_id,
            l.first_name                AS lead_first_name,
            l.last_name                 AS lead_last_name,
            l.email                     AS lead_email,
            l.stage                     AS lead_stage,
            l.campaign_name             AS lead_campaign_name,
            l.campaign_id               AS lead_campaign_id,
            l.gclid                     AS lead_gclid,
            l.attributed_production     AS lead_production,
            l.attributed_income         AS lead_income,
            l.treatment_plan_value      AS lead_tx_plan_value,
            l.appointment_date          AS lead_appt_date,
            l.appointment_status        AS lead_appt_status,
            mc.gads_call_id,
            gcv.campaign_name           AS gads_campaign_name,
            gcv.campaign_id             AS gads_campaign_id,
            gcv.call_duration_sec       AS gads_call_duration_sec
        FROM mango_calls mc
        LEFT JOIN leads          l   ON l.id = mc.lead_id
                                     AND mc.lead_id IS NOT NULL
                                     AND mc.lead_id != ''
        LEFT JOIN gads_call_view gcv ON gcv.call_id = mc.gads_call_id
    """)

    # Seed grading rubric on first run
    n_crit = conn.execute("SELECT COUNT(*) FROM call_grading_criteria").fetchone()[0]
    if n_crit == 0:
        _seed_call_grading_criteria(conn)

    # Seed team members from extension map on first run
    n_tm = conn.execute("SELECT COUNT(*) FROM call_team_members").fetchone()[0]
    if n_tm == 0:
        _seed_call_team_members(conn)

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

    # ── Phase A: Feedback Loop Foundation ─────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS keyword_production_log (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_at           TEXT NOT NULL,
            lead_id             TEXT NOT NULL,
            keyword_text        TEXT NOT NULL DEFAULT '',
            match_type          TEXT NOT NULL DEFAULT '',
            campaign_id         TEXT NOT NULL DEFAULT '',
            campaign_name       TEXT NOT NULL DEFAULT '',
            ad_group_name       TEXT NOT NULL DEFAULT '',
            gclid               TEXT NOT NULL DEFAULT '',
            od_patient_num      TEXT NOT NULL DEFAULT '',
            production_amount   REAL NOT NULL DEFAULT 0.0,
            procedure_codes     TEXT NOT NULL DEFAULT '[]',
            match_method        TEXT NOT NULL DEFAULT '',
            appointment_date    TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (lead_id) REFERENCES leads(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kpl_keyword ON keyword_production_log(keyword_text, campaign_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kpl_lead    ON keyword_production_log(lead_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kpl_logged  ON keyword_production_log(logged_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kpl_gclid   ON keyword_production_log(gclid)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS applied_outcomes (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            action_id           TEXT NOT NULL UNIQUE,
            applied_at          TEXT NOT NULL,
            operation           TEXT NOT NULL,
            entity_type         TEXT NOT NULL,
            entity_id           TEXT NOT NULL,
            entity_name         TEXT NOT NULL,
            campaign_id         TEXT NOT NULL DEFAULT '',
            campaign_name       TEXT NOT NULL DEFAULT '',
            pre_impressions_7d  INTEGER DEFAULT 0,
            pre_clicks_7d       INTEGER DEFAULT 0,
            pre_cost_7d         REAL DEFAULT 0.0,
            pre_conversions_7d  REAL DEFAULT 0.0,
            pre_cpc             REAL DEFAULT 0.0,
            post_7d_json        TEXT DEFAULT '{}',
            post_30d_json       TEXT DEFAULT '{}',
            post_90d_json       TEXT DEFAULT '{}',
            outcome_7d_at       TEXT DEFAULT '',
            outcome_30d_at      TEXT DEFAULT '',
            outcome_90d_at      TEXT DEFAULT '',
            verdict             TEXT DEFAULT 'pending',
            verdict_reason      TEXT DEFAULT '',
            ai_reason           TEXT DEFAULT '',
            optimizer_run_id    TEXT DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ao_applied  ON applied_outcomes(applied_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ao_campaign ON applied_outcomes(campaign_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ao_verdict  ON applied_outcomes(verdict)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ao_entity   ON applied_outcomes(entity_id)")

    conn.execute("""
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
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ki_keyword  ON keyword_intelligence(keyword_text, campaign_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ki_date     ON keyword_intelligence(date DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ki_campaign ON keyword_intelligence(campaign_id, date DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ki_roas     ON keyword_intelligence(true_roas DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ki_sqs      ON keyword_intelligence(session_quality_score DESC)")

    # Add google_rec_resource_name column to gads_audit_log (migration for existing DBs)
    audit_cols_pre = {row[1] for row in conn.execute("PRAGMA table_info(gads_audit_log)").fetchall()}
    try:
        conn.execute("ALTER TABLE gads_audit_log ADD COLUMN google_rec_resource_name TEXT DEFAULT ''")
    except Exception:
        pass  # column already exists

    # Add reject_reason column to gads_audit_log (migration for existing DBs)
    audit_cols = {row[1] for row in conn.execute("PRAGMA table_info(gads_audit_log)").fetchall()}
    if "reject_reason" not in audit_cols:
        conn.execute("ALTER TABLE gads_audit_log ADD COLUMN reject_reason TEXT DEFAULT ''")
    # Phase 2+ columns: campaign linkage, priority, impact estimate, user feedback
    if "campaign_id" not in audit_cols:
        conn.execute("ALTER TABLE gads_audit_log ADD COLUMN campaign_id TEXT DEFAULT ''")
    if "campaign_name" not in audit_cols:
        conn.execute("ALTER TABLE gads_audit_log ADD COLUMN campaign_name TEXT DEFAULT ''")
    if "priority" not in audit_cols:
        conn.execute("ALTER TABLE gads_audit_log ADD COLUMN priority INTEGER DEFAULT 50")
    if "impact_estimate_json" not in audit_cols:
        conn.execute("ALTER TABLE gads_audit_log ADD COLUMN impact_estimate_json TEXT DEFAULT '{}'")
    if "user_feedback" not in audit_cols:
        conn.execute("ALTER TABLE gads_audit_log ADD COLUMN user_feedback TEXT DEFAULT ''")
    if "user_feedback_at" not in audit_cols:
        conn.execute("ALTER TABLE gads_audit_log ADD COLUMN user_feedback_at TEXT DEFAULT ''")
    # api_executed: True = actually hit Google Ads API; False/NULL = acknowledged-only or pending
    if "api_executed" not in audit_cols:
        conn.execute("ALTER TABLE gads_audit_log ADD COLUMN api_executed INTEGER DEFAULT 0")
    # queued_at: timestamp when admin moved rec to queue (before Push to Google)
    if "queued_at" not in audit_cols:
        conn.execute("ALTER TABLE gads_audit_log ADD COLUMN queued_at TEXT DEFAULT ''")
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_campaign ON gads_audit_log(campaign_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_priority ON gads_audit_log(priority, execution_result)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_queued ON gads_audit_log(execution_result, queued_at)")
    except Exception:
        pass

    # M4 fix: add UNIQUE(lead_id, od_patient_num) to keyword_production_log for existing DBs.
    # SQLite doesn't support ALTER TABLE ADD CONSTRAINT, so we use CREATE UNIQUE INDEX.
    # ON CONFLICT REPLACE semantics are handled at insert time via INSERT OR REPLACE.
    try:
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_kpl_lead_patient
            ON keyword_production_log(lead_id, od_patient_num)
        """)
    except Exception as _kpl_idx_err:
        print(f"[db] keyword_production_log UNIQUE index (non-fatal, may already exist): {_kpl_idx_err}")

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

    # ── One-time cleanup: remove tombstones for known test emails so they ─────
    # ── can re-submit fresh leads after a successful Firestore delete.     ─────
    _TEST_EMAILS_TO_CLEAR = [
        "anurag82@gmail.com",
        "anuraggupta@graftondentalcare.com",
    ]
    for _te in _TEST_EMAILS_TO_CLEAR:
        cur = conn.execute(
            "DELETE FROM deleted_leads WHERE email=? COLLATE NOCASE",
            (_te.strip().lower(),),
        )
        if cur.rowcount:
            print(f"[db] Cleared tombstone for test email: {_te} ({cur.rowcount} row(s))")

    # Create gads_impact_history table (migration for existing DBs)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS gads_impact_history (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id                  TEXT NOT NULL,
                run_date                TEXT NOT NULL,
                estimated_waste_saved_usd  REAL DEFAULT 0.0,
                estimated_cpl_improvement  REAL DEFAULT 0.0,
                estimated_coverage_gain    REAL DEFAULT 0.0,
                total_recs              INTEGER DEFAULT 0,
                approved_recs           INTEGER DEFAULT 0,
                rejected_recs           INTEGER DEFAULT 0,
                cum_estimated_savings   REAL DEFAULT 0.0,
                benchmark_gaps_json     TEXT DEFAULT '{}',
                impact_by_type_json     TEXT DEFAULT '{}',
                actual_impact_json      TEXT DEFAULT '{}',
                created_at              TEXT NOT NULL,
                updated_at              TEXT NOT NULL
            )
        """)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_impact_history_run ON gads_impact_history(run_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_impact_history_date ON gads_impact_history(run_date DESC)")
    except Exception:
        pass


def _seed_call_grading_criteria(conn):
    """Seed the 7 default Grafton Dental call grading criteria (from mango-call-analysis defaults)."""
    now = datetime.now(timezone.utc).isoformat()
    criteria = [
        (1,  "Enthusiasm & Warmth", 15,
         "Did the team member sound enthusiastic, upbeat, and genuinely welcoming throughout the call? "
         "People can feel your emotions over the phone — the goal is to sound bubbly and excited, not flat or rushed."),
        (2,  "Proper Greeting", 10,
         "Did the team member answer with the correct greeting: 'Thank you for calling [Practice], this is [Name], "
         "how can I help you?' Was the call answered promptly (within 3 rings)?"),
        (3,  "Call Control & Follow-Up Questions", 20,
         "Did the team member take control of the call — answering the patient's question quickly and immediately "
         "following up with their own question? Did they keep asking questions rather than letting the call go flat? "
         "Did they ask 'How did you hear about us?'"),
        (4,  "Information Collection", 20,
         "For new patients, did the team member collect all 6 required pieces of information: "
         "(1) Name, (2) Date of birth, (3) Email, (4) Cell phone, "
         "(5) How they heard about the office, (6) Whether other family members need an appointment?"),
        (5,  "Appointment Scheduling Technique", 15,
         "Did the team member offer two specific appointment time options (prioritizing today and tomorrow)? "
         "Did they ask about scheduling other family members? Did they repeat and confirm the appointment time? "
         "Did they ask the patient to call ASAP if anything changes?"),
        (6,  "Empathy & Concern Handling", 10,
         "Did the team member show genuine empathy for patients in pain or distress? Did they handle cost and "
         "insurance questions correctly — giving a price range then redirecting with a question — without "
         "proactively asking about insurance unless the patient raised it?"),
        (7,  "Active Listening & Clear Communication", 10,
         "Did the team member practice 'silence is golden' — stop talking after asking a question and wait for "
         "the patient to speak? Did they avoid TMI (too much information), keeping answers concise and not over-explaining?"),
    ]
    for sort_order, name, weight, description in criteria:
        conn.execute(
            "INSERT OR IGNORE INTO call_grading_criteria "
            "(name, weight, description, sort_order, active, created_at, updated_at) VALUES (?,?,?,?,1,?,?)",
            (name, weight, description, sort_order, now, now)
        )


def _seed_call_team_members(conn):
    """Seed default team members from mango_service EXTENSION_MAP."""
    now = datetime.now(timezone.utc).isoformat()
    defaults = [("Olivia", "101"), ("Ivette", "103")]
    for name, extension in defaults:
        conn.execute(
            "INSERT OR IGNORE INTO call_team_members (name, extension, active, created_at, updated_at) VALUES (?,?,1,?,?)",
            (name, extension, now, now)
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
                        "click_cost","gads_synced_at","tags"]:
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
                    smile_image_url, smile_blob_name, notes, tags)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                lead_id, data.get("created_at", now), now,
                data.get("source") or "unknown", data.get("stage", "new"),
                data.get("first_name", ""), data.get("last_name", ""),
                email_raw, phone_raw,
                _hash_phone(phone_raw) if phone_raw else "",
                _hash(email_raw) if email_raw else "",
                json.dumps(data.get("goals", [])) if isinstance(data.get("goals"), list) else data.get("goals", ""),
                data.get("gclid", ""), data.get("fbclid", ""), data.get("msclkid", ""),
                data.get("utm_source") or "", data.get("utm_medium") or "",
                data.get("utm_campaign", ""), data.get("utm_term", ""),
                data.get("utm_content", ""), data.get("landing_url") or "",
                data.get("smile_image_url", ""), data.get("smile_blob_name", ""),
                data.get("notes", ""),
                data.get("tags", "[]"),
            ))
            # Auto-note: inline into same connection so no nested-transaction risk
            try:
                src = data.get("source") or "unknown"
                utm_s = data.get("utm_source") or ""
                utm_m = data.get("utm_medium") or ""
                utm = f"{utm_s}/{utm_m}" if (utm_s or utm_m) else ""
                url = (data.get("landing_url") or "")[:200]
                parts = [f"Lead created via {src}"]
                if utm and utm != "/":
                    parts.append(utm)
                if url:
                    parts.append(url)
                note_text = " — ".join(parts)
                conn.execute(
                    "INSERT INTO lead_notes (lead_id, note_text, author, created_at) VALUES (?,?,?,?)",
                    (lead_id, note_text, "system", now),
                )
            except Exception:
                pass  # Auto-note failure must never block lead creation
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

    Priority 1 — explicit workflow_id on the matched campaign record
                  (matched by campaign_id first, then normalised campaign_name fallback).
    Priority 2 — workflow campaign_tag string match (legacy behaviour).
    Priority 3 — default workflow (campaign_tag='').

    Returns a list of step dicts ordered by sequence_day.
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)

    lead_id = lead.get("id", "?")

    # ── Priority 1: campaign row → workflow_id ────────────────────────────────
    camp_id   = (lead.get("campaign_id") or "").strip()
    camp_name = (lead.get("campaign_name") or lead.get("utm_campaign") or "").strip()

    camp_row = None
    if camp_id:
        camp_row = conn.execute(
            "SELECT workflow_id FROM campaigns WHERE campaign_id=? LIMIT 1", (camp_id,)
        ).fetchone()
    if not camp_row and camp_name:
        # Normalised name fallback (case-insensitive)
        camp_row = conn.execute(
            "SELECT workflow_id FROM campaigns WHERE LOWER(campaign_name)=LOWER(?) LIMIT 1",
            (camp_name,)
        ).fetchone()

    if camp_row and camp_row["workflow_id"]:
        wf_row = conn.execute(
            "SELECT id FROM workflows WHERE id=? AND active=1", (camp_row["workflow_id"],)
        ).fetchone()
        if wf_row:
            steps = conn.execute(
                "SELECT * FROM workflow_steps WHERE workflow_id=? AND active=1 ORDER BY sequence_day",
                (wf_row["id"],)
            ).fetchall()
            if steps:
                _log.info(f"Workflow routing [lead={lead_id}]: Priority 1 — campaign workflow_id={wf_row['id']}")
                return [dict(s) for s in steps]

    # ── Priority 2: campaign_tag string match ─────────────────────────────────
    raw_tag = camp_name or (lead.get("utm_campaign") or "")
    tag = _normalize_campaign_tag(raw_tag)
    if tag:
        row = conn.execute(
            "SELECT id FROM workflows WHERE campaign_tag=? AND active=1 LIMIT 1", (tag,)
        ).fetchone()
        if row:
            steps = conn.execute(
                "SELECT * FROM workflow_steps WHERE workflow_id=? AND active=1 ORDER BY sequence_day",
                (row["id"],)
            ).fetchall()
            if steps:
                _log.info(f"Workflow routing [lead={lead_id}]: Priority 2 — campaign_tag='{tag}' → workflow_id={row['id']}")
                return [dict(s) for s in steps]

    # ── Priority 3: default workflow ─────────────────────────────────────────
    row = conn.execute(
        "SELECT id FROM workflows WHERE campaign_tag='' AND active=1 LIMIT 1"
    ).fetchone()
    if not row:
        _log.warning(f"Workflow routing [lead={lead_id}]: No default workflow found — no steps scheduled")
        return []
    steps = conn.execute(
        "SELECT * FROM workflow_steps WHERE workflow_id=? AND active=1 ORDER BY sequence_day",
        (row["id"],)
    ).fetchall()
    _log.info(f"Workflow routing [lead={lead_id}]: Priority 3 — default workflow_id={row['id']}")
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


def get_mango_settings() -> dict:
    """Return Mango Voice + AI settings from DB, falling back to env/config defaults.

    Call-site rule: always use this helper instead of get_settings() directly so
    that credentials saved via the Admin UI take effect without a restart.
    """
    from config import get_settings
    cfg = get_settings()
    return {
        "mango_username":            get_setting("mango_username")            or cfg.mango_username,
        "mango_password":            get_setting("mango_password")            or cfg.mango_password,
        "mango_pbx_id":              get_setting("mango_pbx_id")              or cfg.mango_pbx_id,
        "mango_api_base":            get_setting("mango_api_base")            or cfg.mango_api_base,
        "mango_account_uuid":        get_setting("mango_account_uuid")        or "",
        "openai_api_key":            get_setting("mango_openai_api_key")      or cfg.openai_api_key,
        # Vertex AI (HIPAA-compliant Gemini) — replaces direct Gemini API key
        "vertex_project_id":         get_setting("vertex_project_id")         or cfg.vertex_project_id,
        "vertex_location":           get_setting("vertex_location")           or cfg.vertex_location,
        "vertex_credentials_path":   get_setting("vertex_credentials_path")   or cfg.vertex_credentials_path,
        "vertex_model":              get_setting("vertex_model")               or cfg.vertex_model,
        "mango_whisper_mode":        get_setting("mango_whisper_mode")        or cfg.mango_whisper_mode,
        "mango_whisper_local_model": get_setting("mango_whisper_local_model") or cfg.mango_whisper_local_model,
        "mango_enabled":             (get_setting("mango_enabled")            or str(cfg.mango_enabled)).lower() == "true",
        "mango_pipeline_enabled":    (get_setting("mango_pipeline_enabled")   or str(cfg.mango_pipeline_enabled)).lower() == "true",
        "mango_pipeline_auto_grade": (get_setting("mango_pipeline_auto_grade") or str(cfg.mango_pipeline_auto_grade)).lower() == "true",
        "mango_pipeline_auto_suggest_action": (get_setting("mango_pipeline_auto_suggest_action") or str(cfg.mango_pipeline_auto_suggest_action)).lower() == "true",
    }


def delete_workflow(workflow_id: int) -> bool:
    with _conn() as conn:
        # Null out any campaigns that reference this workflow before deleting
        conn.execute("UPDATE campaigns SET workflow_id=NULL WHERE workflow_id=?", (workflow_id,))
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
        # Check if the matched campaign has skip_workflow=1 — if so, don't enqueue anything
        camp_id = (lead.get("utm_campaign") or "").strip()
        if camp_id:
            skip_row = conn.execute(
                "SELECT skip_workflow FROM campaigns WHERE campaign_id=? LIMIT 1", (camp_id,)
            ).fetchone()
            if skip_row and skip_row["skip_workflow"]:
                _log.info(f"enqueue_follow_ups: skipping lead {lead['id']} — campaign '{camp_id}' has skip_workflow=1")
                return

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
        # Hot leads: implant prospects / reactivations not yet completed,
        # no-shows, or leads with an active treatment plan value.
        # NOTE: The OR-chain is intentional — no_show is excluded from clause 1
        # by 'stage NOT IN' but re-included by clause 2. Keep the comment.
        hot_leads_count = conn.execute("""
            SELECT COUNT(*) FROM leads WHERE (
                (od_relationship IN ('implant_prospect','reactivation')
                 AND stage NOT IN ('treatment_completed','cold'))
                OR (stage = 'no_show' AND no_show_count >= 1)
                OR (treatment_plan_value > 0
                    AND stage NOT IN ('treatment_completed','cold'))
            )
        """).fetchone()[0]

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
            "hot_leads_count": hot_leads_count,
        }


def get_hot_leads() -> list:
    """
    Return up to 20 leads that are 'hot' — implant prospects, reactivations,
    no-shows, or leads with an active treatment plan value — ordered by
    relationship priority then treatment plan value descending.
    """
    with _conn() as conn:
        rows = conn.execute("""
            SELECT * FROM leads
            WHERE (
                (od_relationship IN ('implant_prospect','reactivation')
                 AND stage NOT IN ('treatment_completed','cold'))
                OR (stage = 'no_show' AND no_show_count >= 1)
                OR (treatment_plan_value > 0
                    AND stage NOT IN ('treatment_completed','cold'))
            )
            ORDER BY
                CASE od_relationship
                    WHEN 'implant_prospect' THEN 1
                    WHEN 'reactivation' THEN 2
                    ELSE 3
                END,
                treatment_plan_value DESC
            LIMIT 20
        """).fetchall()
        return [dict(r) for r in rows]


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


# ─── Lead Tags ───────────────────────────────────────────────────────────────

def get_lead_tags(lead_id: str) -> list:
    lead = get_lead(lead_id)
    if not lead:
        return []
    raw = lead.get("tags") or "[]"
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []


def set_lead_tags(lead_id: str, tags: list) -> list:
    """Replace the tag list for a lead. Normalizes: lowercase, strip, non-empty, dedupe."""
    normalized = list(dict.fromkeys(
        t.lower().strip() for t in tags
        if isinstance(t, str) and t.strip()
    ))
    serialized = json.dumps(normalized)
    now = _now()
    with _conn() as conn:
        conn.execute("UPDATE leads SET tags=?, updated_at=? WHERE id=?", (serialized, now, lead_id))
    return normalized


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


def get_all_campaigns() -> list:
    """Return all managed campaigns ordered by created_at desc."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM campaigns ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_campaigns_with_workflows() -> list:
    """Return all campaigns with attached workflow name (single JOIN, no N+1)."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT c.*, w.name AS workflow_name
            FROM campaigns c
            LEFT JOIN workflows w ON c.workflow_id = w.id
            ORDER BY c.created_at DESC
        """).fetchall()
        return [dict(r) for r in rows]


def get_unified_campaigns(days: int = 30) -> list:
    """
    Unified campaign view — merges managed campaigns table with gads_daily_stats.
    Returns managed rows with embedded metrics, plus synthetic rows for GAds
    campaigns that exist in gads_daily_stats but were never imported.
    Computes is_inactive_90d from full history (not just the window).
    """
    import json as _json
    days = max(1, int(days))
    today = datetime.now(timezone.utc).date()
    cutoff_90d = (today - timedelta(days=90)).isoformat()

    with _conn() as conn:
        # ── Window aggregates from gads_daily_stats ──────────────────────
        window_rows = conn.execute("""
            SELECT
                LOWER(TRIM(campaign_name)) AS k,
                campaign_id                AS gads_numeric_id,
                campaign_name              AS gads_name,
                SUM(impressions)           AS impressions,
                SUM(clicks)                AS clicks,
                SUM(cost_micros)           AS cost_micros,
                SUM(conversions)           AS conversions
            FROM gads_daily_stats
            WHERE date >= DATE('now', ?)
              AND campaign_name != ''
            GROUP BY LOWER(TRIM(campaign_name))
        """, (f"-{days} day",)).fetchall()
        window_by_key = {r["k"]: dict(r) for r in window_rows}
        # Secondary lookup by GAds numeric campaign_id — handles name mismatches
        # (e.g. when dashboard name differs from the timestamped GAds display name)
        window_by_numeric_id = {
            str(r["gads_numeric_id"]): dict(r)
            for r in window_rows
            if r["gads_numeric_id"]
        }

        # ── Lifetime last-activity date (for 90-day rule) ────────────────
        lifetime_rows = conn.execute("""
            SELECT
                LOWER(TRIM(campaign_name)) AS k,
                campaign_id                AS gads_numeric_id,
                MAX(date)                  AS last_activity_date
            FROM gads_daily_stats
            WHERE campaign_name != ''
              AND (clicks > 0 OR cost_micros > 0)
            GROUP BY LOWER(TRIM(campaign_name))
        """).fetchall()
        last_activity_by_key = {r["k"]: r["last_activity_date"] for r in lifetime_rows}
        last_activity_by_numeric_id = {
            str(r["gads_numeric_id"]): r["last_activity_date"]
            for r in lifetime_rows if r["gads_numeric_id"]
        }

        # ── Lead counts per campaign in the window ───────────────────────
        lead_rows = conn.execute("""
            SELECT
                LOWER(TRIM(COALESCE(NULLIF(campaign_name,''), utm_campaign))) AS k,
                COUNT(*)               AS lead_count,
                SUM(attributed_income) AS attributed_income
            FROM leads
            WHERE created_at >= DATE('now', ?)
              AND (campaign_name != '' OR utm_campaign != '')
            GROUP BY LOWER(TRIM(COALESCE(NULLIF(campaign_name,''), utm_campaign)))
        """, (f"-{days} day",)).fetchall()
        leads_by_key = {r["k"]: dict(r) for r in lead_rows}

        # ── Managed campaigns ─────────────────────────────────────────────
        managed = conn.execute("""
            SELECT c.*, w.name AS workflow_name
            FROM campaigns c
            LEFT JOIN workflows w ON c.workflow_id = w.id
            ORDER BY c.created_at DESC
        """).fetchall()

        out = []
        managed_keys = set()

        for r in managed:
            row = dict(r)
            row["ai_review_enabled"] = bool(row.get("ai_review_enabled") or 0)
            row["ai_max_enabled"] = bool(row.get("ai_max_enabled") or 0)
            try:
                row["strategy_json"] = _json.loads(row["strategy_json"]) if row.get("strategy_json") else None
            except Exception:
                row["strategy_json"] = None

            name_key = (row.get("campaign_name") or "").strip().lower()
            managed_keys.add(name_key)

            # Match stats by name first; fall back to GAds numeric ID for
            # campaigns whose display name was timestamped (name mismatch)
            numeric_id = str(row.get("gads_campaign_numeric_id") or "")
            wm = window_by_key.get(name_key) or window_by_numeric_id.get(numeric_id) or {}
            if wm and name_key not in window_by_key and numeric_id:
                # Also register the GAds name key as managed so it won't create a synthetic row
                managed_keys.add((wm.get("gads_name") or "").strip().lower())
            lm = leads_by_key.get(name_key, {})
            cost = (wm.get("cost_micros") or 0) / 1_000_000.0
            leads = lm.get("lead_count") or 0
            revenue = lm.get("attributed_income") or 0
            cpl = round(cost / leads, 2) if leads > 0 else None
            roi = round((revenue - cost) / cost * 100, 1) if cost > 0 else None

            # UNMANAGED rows are orphaned imports — treat as unlinked so the UI shows 🗑 not ⏹ Stop
            is_gads_linked = bool(
                (row.get("gads_campaign_resource") or row.get("gads_campaign_numeric_id"))
                and row.get("status") != "UNMANAGED"
            )
            last = last_activity_by_key.get(name_key) or last_activity_by_numeric_id.get(numeric_id)
            if is_gads_linked:
                if last:
                    is_inactive_90d = last < cutoff_90d
                else:
                    created = (row.get("created_at") or "")[:10]
                    is_inactive_90d = bool(created and created < cutoff_90d)
            else:
                is_inactive_90d = False

            row.update({
                "is_synthetic": False,
                "is_gads_linked": is_gads_linked,
                "metrics": {
                    "impressions": wm.get("impressions") or 0,
                    "clicks": wm.get("clicks") or 0,
                    "cost": round(cost, 2),
                    "leads": leads,
                    "revenue": round(revenue, 2),
                    "cpl": cpl,
                    "roi": roi,
                },
                "last_activity_date": last,
                "is_inactive_90d": is_inactive_90d,
            })
            out.append(row)

        # ── Synthetic rows: GAds names not yet imported ───────────────────
        for k, wm in window_by_key.items():
            if k in managed_keys:
                continue
            lm = leads_by_key.get(k, {})
            cost = (wm.get("cost_micros") or 0) / 1_000_000.0
            leads = lm.get("lead_count") or 0
            revenue = lm.get("attributed_income") or 0
            cpl = round(cost / leads, 2) if leads > 0 else None
            roi = round((revenue - cost) / cost * 100, 1) if cost > 0 else None
            last = last_activity_by_key.get(k)
            is_inactive_90d = (last is None) or (last < cutoff_90d)

            out.append({
                "campaign_id": None,
                "campaign_name": wm["gads_name"],
                "campaign_type": "GOOGLE_ADS",
                "status": "UNMANAGED",
                "service_focus": "",
                "monthly_budget": 0.0,
                "start_date": "",
                "end_date": "",
                "workflow_id": None,
                "workflow_name": None,
                "strategy_json": None,
                "gads_campaign_resource": None,
                "gads_campaign_numeric_id": None,
                "ai_review_enabled": False,
                "ai_max_enabled": False,
                "is_synthetic": True,
                "is_gads_linked": True,
                "metrics": {
                    "impressions": wm.get("impressions") or 0,
                    "clicks": wm.get("clicks") or 0,
                    "cost": round(cost, 2),
                    "leads": leads,
                    "revenue": round(revenue, 2),
                    "cpl": cpl,
                    "roi": roi,
                },
                "last_activity_date": last,
                "is_inactive_90d": is_inactive_90d,
                "created_at": "",
                "updated_at": "",
            })

        return out


def set_campaign_ai_review(campaign_id: str, enabled: bool) -> bool:
    """Toggle the AI Review flag for a managed campaign."""
    now = _now()
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE campaigns SET ai_review_enabled=?, updated_at=? WHERE campaign_id=?",
            (1 if enabled else 0, now, campaign_id),
        )
        return cur.rowcount > 0


def set_campaign_ai_max(campaign_id: str, enabled: bool) -> bool:
    """
    Update the local ai_max_enabled flag for a managed campaign.
    Called ONLY after the Google Ads API mutate succeeds — never update
    local state before confirming the API call worked.
    """
    now = _now()
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE campaigns SET ai_max_enabled=?, updated_at=? WHERE campaign_id=?",
            (1 if enabled else 0, now, campaign_id),
        )
        return cur.rowcount > 0


def save_gads_campaign_snapshot(campaign_id: str, snapshot: dict) -> bool:
    """
    Persist the raw Google Ads campaign snapshot (keywords, ads, ad groups,
    campaign settings) to gads_campaign_snapshot. This is SEPARATE from
    campaign_build_json (user-edited build state) — syncing never overwrites
    what the user has manually edited in the wizard.
    """
    now = _now()
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE campaigns SET gads_campaign_snapshot=?, gads_synced_build_at=?, updated_at=? WHERE campaign_id=?",
            (json.dumps(snapshot), now, now, campaign_id),
        )
        return cur.rowcount > 0


def get_gads_campaign_snapshot(campaign_id: str) -> dict:
    """Return the latest Google Ads snapshot for a campaign, or {} if not synced yet."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT gads_campaign_snapshot, gads_synced_build_at FROM campaigns WHERE campaign_id=?",
            (campaign_id,)
        ).fetchone()
    if not row or not row[0]:
        return {}
    try:
        snap = json.loads(row[0])
        snap["_synced_at"] = row[1] or ""
        return snap
    except Exception:
        return {}


def upsert_sitelink_library(sitelinks: list) -> int:
    """
    Upsert a list of {title, url, description1?, description2?} into sitelink_library.
    On conflict (same title+url), increment use_count and update last_used_at.
    Returns number of rows upserted.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    count = 0
    with _conn() as conn:
        for sl in sitelinks:
            title = (sl.get("title") or "").strip()
            url   = (sl.get("url") or "").strip()
            if not title or not url:
                continue
            desc1 = (sl.get("description1") or "").strip()
            desc2 = (sl.get("description2") or "").strip()
            conn.execute("""
                INSERT INTO sitelink_library (title, url, description1, description2, use_count, last_used_at)
                VALUES (?, ?, ?, ?, 1, ?)
                ON CONFLICT(title, url) DO UPDATE SET
                    use_count    = use_count + 1,
                    description1 = excluded.description1,
                    description2 = excluded.description2,
                    last_used_at = excluded.last_used_at
            """, (title, url, desc1, desc2, now))
            count += 1
    return count


def get_sitelink_library() -> list:
    """Return all sitelinks from the library, most-used first."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT id, title, url, description1, description2, use_count, last_used_at
            FROM sitelink_library
            ORDER BY use_count DESC, last_used_at DESC
        """).fetchall()
    return [
        {
            "id": r[0], "title": r[1], "url": r[2],
            "description1": r[3], "description2": r[4],
            "use_count": r[5], "last_used_at": r[6],
        }
        for r in rows
    ]


def get_search_term_type_breakdown(campaign_id: str, days: int = 30) -> dict:
    """
    Return counts of leads per search_term_type for a campaign in the window.
    Used by the Performance tab to show AI Max vs standard match type breakdown.
    """
    with _conn() as conn:
        rows = conn.execute("""
            SELECT
                COALESCE(NULLIF(search_term_type, ''), 'unknown') AS stype,
                COUNT(*) AS cnt
            FROM leads
            WHERE campaign_id = ?
              AND created_at >= DATE('now', ?)
            GROUP BY stype
        """, (campaign_id, f"-{days} day")).fetchall()
    return {r["stype"]: r["cnt"] for r in rows}


def get_campaign_build(campaign_id: str) -> dict:
    """Return the campaign_build_json for a campaign, or {} if not set."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT campaign_build_json FROM campaigns WHERE campaign_id=?", (campaign_id,)
        ).fetchone()
    if not row or not row[0]:
        return {}
    try:
        return json.loads(row[0])
    except Exception:
        return {}


def save_campaign_build_step(campaign_id: str, step: str, data) -> bool:
    """
    Merge `data` into the campaign_build_json blob under key `step`.
    step: one of "keywords", "ad_copy", "ad_groups", "launch_checklist"
    """
    now = _now()
    build = get_campaign_build(campaign_id)
    build[step] = data
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE campaigns SET campaign_build_json=?, updated_at=? WHERE campaign_id=?",
            (json.dumps(build), now, campaign_id),
        )
        return cur.rowcount > 0


def update_campaign_workflow(campaign_id: str, workflow_id) -> bool:
    """Set or clear the workflow attached to a campaign. workflow_id=None clears it."""
    now = _now()
    raw_wf = workflow_id
    wf_val = int(raw_wf) if raw_wf not in (None, "", 0, "0") else None
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE campaigns SET workflow_id=?, updated_at=? WHERE campaign_id=?",
            (wf_val, now, campaign_id)
        )
        return cur.rowcount > 0


def update_campaign_strategy(campaign_id: str, strategy: dict) -> bool:
    """Persist the Opus-generated strategy JSON to the campaign record."""
    import json as _json
    now = _now()
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE campaigns SET strategy_json=?, updated_at=? WHERE campaign_id=?",
            (_json.dumps(strategy), now, campaign_id)
        )
        return cur.rowcount > 0


def create_campaign(data: dict) -> dict:
    """Insert a new campaign row. Returns the created row as a dict."""
    now = _now()
    # Auto-generate a slug if no campaign_id provided
    campaign_id = (data.get("campaign_id") or "").strip()
    if not campaign_id:
        slug = data["campaign_name"].lower().replace(" ", "_")
        # keep only alphanumeric + underscore, max 50 chars
        import re as _re
        slug = _re.sub(r"[^a-z0-9_]", "", slug)[:50]
        campaign_id = f"manual_{slug}_{now[:10].replace('-','')}"

    # DRAFT if no start_date, else ACTIVE
    status = data.get("status") or ("ACTIVE" if data.get("start_date") else "DRAFT")

    # Coerce workflow_id: empty string or None → None; otherwise int
    raw_wf = data.get("workflow_id")
    workflow_id = int(raw_wf) if raw_wf not in (None, "", 0, "0") else None

    gads_resource = data.get("gads_campaign_resource") or None
    gads_numeric  = data.get("gads_campaign_numeric_id") or None

    with _conn() as conn:
        conn.execute("""
            INSERT INTO campaigns
                (campaign_id, campaign_name, status, campaign_type,
                 service_focus, promo_offer, target_audience, objective,
                 monthly_budget, expected_cpl, start_date, end_date,
                 landing_page, notes, workflow_id, skip_workflow,
                 gads_campaign_resource, gads_campaign_numeric_id,
                 created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            campaign_id,
            data["campaign_name"],
            status,
            data.get("campaign_type", "MANUAL"),
            data.get("service_focus", ""),
            data.get("promo_offer", ""),
            data.get("target_audience", ""),
            data.get("objective", ""),
            float(data.get("monthly_budget") or 0),
            float(data.get("expected_cpl") or 0),
            data.get("start_date", ""),
            data.get("end_date", ""),
            data.get("landing_page", ""),
            data.get("notes", ""),
            workflow_id,
            1 if data.get("skip_workflow") else 0,
            gads_resource,
            gads_numeric,
            now, now,
        ))
        row = conn.execute(
            "SELECT * FROM campaigns WHERE campaign_id=?", (campaign_id,)
        ).fetchone()
        return dict(row)


def update_campaign_fields(campaign_id: str, fields: dict) -> bool:
    """
    Update editable campaign fields: campaign_name, service_focus, monthly_budget,
    start_date, end_date, notes, promo_offer, landing_page, objective, target_audience.
    Only columns explicitly passed in `fields` are updated.
    """
    ALLOWED = {
        "campaign_name", "service_focus", "monthly_budget", "start_date",
        "end_date", "notes", "promo_offer", "landing_page", "objective",
        "target_audience", "expected_cpl", "geographic_targeting",
        "launch_date", "call_extension_phone", "skip_workflow", "sitelinks",
    }
    safe = {k: v for k, v in fields.items() if k in ALLOWED}
    if not safe:
        return False
    now = _now()
    safe["updated_at"] = now
    set_clause = ", ".join(f"{k}=?" for k in safe)
    vals = list(safe.values()) + [campaign_id]
    with _conn() as conn:
        cur = conn.execute(
            f"UPDATE campaigns SET {set_clause} WHERE campaign_id=?", vals
        )
        return cur.rowcount > 0


def update_campaign_status(campaign_id: str, status: str, launch_date: str | None = None) -> bool:
    """
    Patch a campaign's status (ACTIVE, PAUSED, COMPLETED, ARCHIVED, SCHEDULED, QUEUED).
    Optionally also set launch_date (used by the Launch flow for SCHEDULED + ACTIVE).
    Pass launch_date="" to explicitly clear it (e.g. when moving back to QUEUED).
    """
    now = _now()
    with _conn() as conn:
        if launch_date is not None:
            conn.execute(
                "UPDATE campaigns SET status=?, updated_at=?, launch_date=? WHERE campaign_id=?",
                (status, now, launch_date, campaign_id)
            )
        else:
            conn.execute(
                "UPDATE campaigns SET status=?, updated_at=? WHERE campaign_id=?",
                (status, now, campaign_id)
            )
        return conn.execute(
            "SELECT COUNT(*) FROM campaigns WHERE campaign_id=?", (campaign_id,)
        ).fetchone()[0] > 0


def get_campaign_by_id(campaign_id: str):
    """Fetch a single campaign row by logical campaign_id. Returns dict or None."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM campaigns WHERE campaign_id=?", (campaign_id,)
        ).fetchone()
        return dict(row) if row else None


def delete_campaign(campaign_id: str) -> bool:
    """Permanently delete a campaign row. Returns True if a row was deleted."""
    with _conn() as conn:
        cur = conn.execute("DELETE FROM campaigns WHERE campaign_id=?", (campaign_id,))
        return cur.rowcount > 0


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
                ON CONFLICT(keyword_text, match_type, ad_group_name, campaign_name, days) DO UPDATE SET
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


def clear_tombstone(email: str) -> int:
    """Remove tombstone rows for the given email. Returns number of rows deleted."""
    with _conn() as conn:
        cur = conn.execute(
            "DELETE FROM deleted_leads WHERE email=? COLLATE NOCASE",
            (email.strip().lower(),),
        )
        return cur.rowcount


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
            "VALUES (?, 'outbound', 'practice', ?, ?, '', '', '', ?, ?, ?)",
            (conv_id, subject, body, now, sent_by, "manual")
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
    campaign_id: str = "",
    campaign_name: str = "",
    priority: int = 50,
    impact_estimate_json: str = "{}",
) -> None:
    """Insert a Google Ads audit log row. Called from campaign_audit.py."""
    now = _now()
    with _conn() as conn:
        conn.execute("""
            INSERT INTO gads_audit_log
                (action_id, operation, entity_type, entity_id, entity_name,
                 before_state_json, after_state_json, executed, execution_result,
                 actor, reason, error_detail, optimizer_run_id,
                 campaign_id, campaign_name, priority, impact_estimate_json,
                 created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            action_id, operation, entity_type, entity_id, entity_name,
            before_state_json, after_state_json,
            1 if executed else 0, execution_result,
            actor, reason, error_detail, optimizer_run_id,
            campaign_id, campaign_name, priority, impact_estimate_json,
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


def queue_gads_action(action_id: str, approver: str = "admin") -> None:
    """Move a pending_approval row to 'queued' state (admin approved, not yet pushed to Google)."""
    now = _now()
    with _conn() as conn:
        conn.execute("""
            UPDATE gads_audit_log
               SET execution_result='queued', approval_by=?, approved_at=?, queued_at=?, updated_at=?
             WHERE action_id=? AND execution_result='pending_approval'
        """, (approver, now, now, now, action_id))


def get_queued_actions(limit: int = 200) -> list:
    """Return all rows in 'queued' state, ordered by priority then created_at."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT * FROM gads_audit_log
             WHERE execution_result = 'queued'
             ORDER BY priority ASC, queued_at ASC
             LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def mark_api_executed(action_id: str, success: bool, error_detail: str = "",
                      api_executed: bool | None = None) -> None:
    """
    After a push attempt, update the audit row.
    - success: True → execution_result='success'; False → 'failed'
    - api_executed: explicitly controls the api_executed column.
      If None (default), mirrors the success flag.
      Pass False for advisory/acknowledged-only ops that succeeded overall
      but never made a real Google Ads API call.
    """
    now = _now()
    result = "success" if success else "failed"
    api_exec_val = (1 if success else 0) if api_executed is None else (1 if api_executed else 0)
    with _conn() as conn:
        conn.execute("""
            UPDATE gads_audit_log
               SET executed=?, execution_result=?, api_executed=?, error_detail=?, updated_at=?
             WHERE action_id=?
        """, (1 if success else 0, result, api_exec_val, error_detail, now, action_id))


def get_account_impact_summary(days: int = 90) -> dict:
    """
    Aggregate applied outcomes across ALL campaigns for account-level impact view.
    Returns totals + per-campaign breakdown.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _conn() as conn:
        # Per-operation type success counts
        op_rows = conn.execute("""
            SELECT operation,
                   COUNT(*) as total,
                   SUM(CASE WHEN api_executed=1 THEN 1 ELSE 0 END) as api_pushed,
                   SUM(CASE WHEN api_executed=0 THEN 1 ELSE 0 END) as acknowledged_only
              FROM gads_audit_log
             WHERE execution_result='success' AND updated_at >= ?
             GROUP BY operation
             ORDER BY total DESC
        """, (cutoff,)).fetchall()

        # Per-campaign summary
        camp_rows = conn.execute("""
            SELECT campaign_name,
                   COUNT(*) as applied,
                   SUM(CASE WHEN api_executed=1 THEN 1 ELSE 0 END) as api_pushed,
                   MIN(updated_at) as first_action,
                   MAX(updated_at) as last_action
              FROM gads_audit_log
             WHERE execution_result='success' AND updated_at >= ?
               AND campaign_name != ''
             GROUP BY campaign_name
             ORDER BY applied DESC
        """, (cutoff,)).fetchall()

        # Outcome verdicts (from learning loop)
        verdict_rows = conn.execute("""
            SELECT verdict, COUNT(*) as cnt
              FROM applied_outcomes
             WHERE applied_at >= ?
             GROUP BY verdict
        """, (cutoff,)).fetchall()

        # Recent applied actions (last 50)
        recent_rows = conn.execute("""
            SELECT action_id, operation, entity_name, campaign_name,
                   execution_result, api_executed, queued_at, updated_at, reason
              FROM gads_audit_log
             WHERE execution_result='success' AND updated_at >= ?
             ORDER BY updated_at DESC LIMIT 50
        """, (cutoff,)).fetchall()

    by_op = [dict(r) for r in op_rows]
    by_camp = [dict(r) for r in camp_rows]
    verdicts = {r["verdict"]: r["cnt"] for r in verdict_rows}
    recent = [dict(r) for r in recent_rows]

    total_applied = sum(r["total"] for r in op_rows)
    total_api_pushed = sum(r["api_pushed"] for r in op_rows)

    return {
        "days": days,
        "total_applied": total_applied,
        "total_api_pushed": total_api_pushed,
        "total_acknowledged_only": total_applied - total_api_pushed,
        "by_operation": by_op,
        "by_campaign": by_camp,
        "verdicts": verdicts,
        "recent_actions": recent,
    }


def log_admin_manual_action(
    operation: str,
    entity_type: str,
    entity_id: str,
    entity_name: str,
    before: dict,
    after: dict,
    reason: str = "manual_edit_via_dashboard",
) -> str:
    """
    Convenience wrapper for manual admin edits.
    Inserts a pending audit row with actor='admin_manual'.
    Returns the generated action_id UUID string.
    """
    import uuid as _uuid, json as _json
    action_id = str(_uuid.uuid4())
    log_gads_action(
        action_id=action_id,
        operation=operation,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_name=entity_name,
        before_state_json=_json.dumps(before),
        after_state_json=_json.dumps(after),
        executed=False,
        execution_result="pending_approval",
        actor="admin_manual",
        reason=reason,
    )
    return action_id


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


# ─── Unread SMS / Inbox ───────────────────────────────────────────────────────

def get_unread_sms_count() -> int:
    """Count inbound SMS messages not yet read by staff."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM sms_messages WHERE direction='inbound' AND read_at IS NULL"
        ).fetchone()
        return row[0] if row else 0


def get_unread_sms_leads() -> list:
    """
    Return leads that have at least one unread inbound SMS, with the most recent
    unread message body + timestamp. Ordered by most recent message first.
    """
    with _conn() as conn:
        rows = conn.execute("""
            SELECT
                sm.lead_id,
                l.first_name,
                l.last_name,
                l.phone,
                l.stage,
                COUNT(sm.id)              AS unread_count,
                MAX(sm.received_at)       AS last_received_at,
                (SELECT body FROM sms_messages
                 WHERE lead_id = sm.lead_id AND direction = 'inbound' AND read_at IS NULL
                 ORDER BY received_at DESC LIMIT 1) AS last_body
            FROM sms_messages sm
            LEFT JOIN leads l ON l.id = sm.lead_id
            WHERE sm.direction = 'inbound' AND sm.read_at IS NULL
            GROUP BY sm.lead_id
            ORDER BY last_received_at DESC
        """).fetchall()
        return [dict(r) for r in rows]


def mark_sms_read(lead_id: str) -> int:
    """Mark all unread inbound SMS for a lead as read. Returns rows updated."""
    now = _now()
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE sms_messages SET read_at=? "
            "WHERE lead_id=? AND direction='inbound' AND read_at IS NULL",
            (now, lead_id)
        )
        return cur.rowcount


# ─── Email Inbox Unread Tracking ──────────────────────────────────────────────

def get_unread_email_count() -> int:
    """Count inbound email messages not yet read by staff."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE direction='inbound' AND read_at IS NULL"
        ).fetchone()
        return row[0] if row else 0


def get_unread_email_leads() -> list:
    """
    Return leads (and unmatched conversations) that have at least one unread inbound email.
    Uses LEFT JOIN so emails from senders not yet matched to a lead still appear.
    """
    with _conn() as conn:
        rows = conn.execute("""
            SELECT
                COALESCE(l.id, 'unmatched-' || CAST(c.id AS TEXT)) AS lead_id,
                l.first_name, l.last_name,
                COALESCE(l.email, c.contact_email)                  AS email,
                MAX(m.received_at)                                   AS latest_at,
                COUNT(m.id)                                          AS unread_count,
                (SELECT m2.body FROM messages m2
                 WHERE m2.conversation_id = c.id
                   AND m2.direction = 'inbound'
                   AND m2.read_at IS NULL
                 ORDER BY m2.received_at DESC LIMIT 1)               AS latest_body,
                CASE WHEN l.id IS NULL THEN 1 ELSE 0 END             AS is_unmatched
            FROM messages m
            JOIN conversations c ON c.id = m.conversation_id
            LEFT JOIN leads l ON l.id = c.lead_id
            WHERE m.direction = 'inbound' AND m.read_at IS NULL
            GROUP BY c.id
            ORDER BY latest_at DESC
        """).fetchall()
        return [dict(r) for r in rows]


def mark_email_read(lead_id: str) -> int:
    """Mark all unread inbound emails for a lead as read. Returns rows updated."""
    now = _now()
    with _conn() as conn:
        cur = conn.execute(
            """UPDATE messages SET read_at=?
               WHERE direction='inbound' AND read_at IS NULL
               AND conversation_id IN (
                   SELECT id FROM conversations WHERE lead_id=?
               )""",
            (now, lead_id)
        )
        return cur.rowcount


# ─── Call Log ─────────────────────────────────────────────────────────────────

def log_call(lead_id: str, direction: str, outcome: str,
             duration_sec: int = 0, notes: str = "", logged_by: str = "admin") -> int:
    """Log a phone call attempt or received call. Returns new row id."""
    now = _now()
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO lead_calls (lead_id, direction, outcome, duration_sec, notes, logged_by, logged_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (lead_id, direction, outcome, duration_sec, notes, logged_by, now)
        )
        # Update last_staff_contact_at: any outbound attempt, or any direction if outcome='spoke'
        if direction == "outbound" or outcome == "spoke":
            conn.execute(
                "UPDATE leads SET last_staff_contact_at=? WHERE id=?", (now, lead_id)
            )
        return cur.lastrowid


def get_calls(lead_id: str) -> list:
    """Return call log for a lead, newest first."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, direction, outcome, duration_sec, notes, logged_by, logged_at "
            "FROM lead_calls WHERE lead_id=? ORDER BY logged_at DESC",
            (lead_id,)
        ).fetchall()
        return [dict(r) for r in rows]


# ─── Next Action ──────────────────────────────────────────────────────────────

def set_next_action(lead_id: str, next_action_at: str, next_action_note: str = "") -> None:
    """Set the next follow-up date and optional note on a lead."""
    with _conn() as conn:
        conn.execute(
            "UPDATE leads SET next_action_at=?, next_action_note=? WHERE id=?",
            (next_action_at, next_action_note, lead_id)
        )


def clear_next_action(lead_id: str) -> None:
    """Clear next action after it's been actioned."""
    with _conn() as conn:
        conn.execute(
            "UPDATE leads SET next_action_at='', next_action_note='' WHERE id=?",
            (lead_id,)
        )


# ─── Phase A: Feedback Loop — Keyword Production Log ─────────────────────────

def append_keyword_production_log(
    lead_id: str,
    keyword_text: str,
    match_type: str,
    campaign_id: str,
    campaign_name: str,
    ad_group_name: str,
    gclid: str,
    od_patient_num: str,
    production_amount: float,
    procedure_codes: list,
    match_method: str,
    appointment_date: str,
) -> dict:
    """Upsert one OD-attributed production event for a keyword click.

    M3+M4 fix: uses INSERT OR REPLACE on UNIQUE(lead_id, od_patient_num) so:
    - First call creates the row (new match)
    - Subsequent calls for the same (lead, patient) update production_amount
      to reflect cumulative OD production (crown work added 60 days later, etc.)
    - Prevents double-counting if OD sync runs multiple times
    """
    import json as _json
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        cur = conn.execute(
            """INSERT OR REPLACE INTO keyword_production_log
               (logged_at, lead_id, keyword_text, match_type, campaign_id, campaign_name,
                ad_group_name, gclid, od_patient_num, production_amount, procedure_codes,
                match_method, appointment_date)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now, lead_id, keyword_text.lower().strip(), match_type, campaign_id,
             campaign_name, ad_group_name, gclid, od_patient_num, production_amount,
             _json.dumps(procedure_codes), match_method, appointment_date),
        )
        row = conn.execute(
            "SELECT * FROM keyword_production_log WHERE rowid=?", (cur.lastrowid,)
        ).fetchone()
        return dict(row) if row else {}


def get_keyword_production_log(
    keyword_text: str = "",
    campaign_id: str = "",
    days: int = 90,
) -> list:
    """Return production log rows, optionally filtered by keyword and/or campaign."""
    cutoff = (datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - __import__("datetime").timedelta(days=days)).isoformat()
    with _conn() as conn:
        if keyword_text and campaign_id:
            rows = conn.execute(
                "SELECT * FROM keyword_production_log "
                "WHERE keyword_text=? AND campaign_id=? AND logged_at>=? ORDER BY logged_at DESC",
                (keyword_text.lower(), campaign_id, cutoff),
            ).fetchall()
        elif keyword_text:
            rows = conn.execute(
                "SELECT * FROM keyword_production_log "
                "WHERE keyword_text=? AND logged_at>=? ORDER BY logged_at DESC",
                (keyword_text.lower(), cutoff),
            ).fetchall()
        elif campaign_id:
            rows = conn.execute(
                "SELECT * FROM keyword_production_log "
                "WHERE campaign_id=? AND logged_at>=? ORDER BY logged_at DESC",
                (campaign_id, cutoff),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM keyword_production_log "
                "WHERE logged_at>=? ORDER BY logged_at DESC",
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]


# ─── Phase A: Feedback Loop — Applied Outcomes ───────────────────────────────

def snapshot_applied_outcome(
    action_id: str,
    operation: str,
    entity_type: str,
    entity_id: str,
    entity_name: str,
    campaign_id: str,
    campaign_name: str,
    before_state: dict,
    ai_reason: str = "",
    optimizer_run_id: str = "",
) -> None:
    """Snapshot an applied action for T+7/T+30/T+90 outcome tracking."""
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO applied_outcomes
               (action_id, applied_at, operation, entity_type, entity_id, entity_name,
                campaign_id, campaign_name, pre_impressions_7d, pre_clicks_7d,
                pre_cost_7d, pre_conversions_7d, pre_cpc, ai_reason, optimizer_run_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (action_id, now, operation, entity_type, entity_id, entity_name,
             campaign_id, campaign_name,
             int(before_state.get("impressions", 0)),
             int(before_state.get("clicks", 0)),
             float(before_state.get("cost_micros", 0)) / 1_000_000,
             float(before_state.get("conversions", 0)),
             float(before_state.get("avg_cpc", 0)),
             ai_reason, optimizer_run_id),
        )


def get_pending_outcome_snapshots(window_days: int = 7) -> list:
    """Return applied_outcomes rows whose post_{window_days}d_json hasn't been filled yet."""
    col = f"post_{window_days}d_json"
    filled_col = f"outcome_{window_days}d_at"
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM applied_outcomes WHERE {filled_col}='' ORDER BY applied_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def update_outcome_snapshot(action_id: str, window_days: int, stats: dict) -> None:
    """Fill in post-action stats at T+N window."""
    import json as _json
    now = datetime.now(timezone.utc).isoformat()
    col_json = f"post_{window_days}d_json"
    col_at = f"outcome_{window_days}d_at"
    with _conn() as conn:
        conn.execute(
            f"UPDATE applied_outcomes SET {col_json}=?, {col_at}=? WHERE action_id=?",
            (_json.dumps(stats), now, action_id),
        )


# ─── Phase A: Feedback Loop — Keyword Intelligence ───────────────────────────

def upsert_keyword_intelligence(rows: list) -> int:
    """Bulk upsert keyword_intelligence rows (from nightly rebuild job).
    Each row is a dict matching the table columns.
    Returns count of rows written."""
    import json as _json
    if not rows:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    written = 0
    with _conn() as conn:
        for r in rows:
            conn.execute(
                """INSERT INTO keyword_intelligence
                   (date, keyword_text, match_type, campaign_id, campaign_name,
                    ad_group_name, impressions, clicks, cost_usd, avg_cpc, conversions,
                    quality_score, impression_share, od_appointments, od_production_total,
                    od_production_per_click, ga4_sessions, ga4_avg_duration_sec,
                    ga4_bounce_rate, ga4_lead_events, session_quality_score,
                    times_recommended, times_applied, times_rejected,
                    last_decision_at, last_decision, true_roas,
                    data_age_days, confidence_tier, rebuilt_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(keyword_text, match_type, campaign_id, date)
                   DO UPDATE SET
                     impressions=excluded.impressions, clicks=excluded.clicks,
                     cost_usd=excluded.cost_usd, avg_cpc=excluded.avg_cpc,
                     conversions=excluded.conversions, quality_score=excluded.quality_score,
                     impression_share=excluded.impression_share,
                     od_appointments=excluded.od_appointments,
                     od_production_total=excluded.od_production_total,
                     od_production_per_click=excluded.od_production_per_click,
                     ga4_sessions=excluded.ga4_sessions,
                     ga4_avg_duration_sec=excluded.ga4_avg_duration_sec,
                     ga4_bounce_rate=excluded.ga4_bounce_rate,
                     ga4_lead_events=excluded.ga4_lead_events,
                     session_quality_score=excluded.session_quality_score,
                     times_recommended=excluded.times_recommended,
                     times_applied=excluded.times_applied,
                     times_rejected=excluded.times_rejected,
                     last_decision_at=excluded.last_decision_at,
                     last_decision=excluded.last_decision,
                     true_roas=excluded.true_roas, data_age_days=excluded.data_age_days,
                     confidence_tier=excluded.confidence_tier, rebuilt_at=excluded.rebuilt_at
                """,
                (
                    r.get("date", now[:10]),
                    r.get("keyword_text", "").lower().strip(),
                    r.get("match_type", ""),
                    r.get("campaign_id", ""),
                    r.get("campaign_name", ""),
                    r.get("ad_group_name", ""),
                    int(r.get("impressions", 0)),
                    int(r.get("clicks", 0)),
                    float(r.get("cost_usd", 0.0)),
                    float(r.get("avg_cpc", 0.0)),
                    float(r.get("conversions", 0.0)),
                    int(r.get("quality_score", 0)),
                    float(r.get("impression_share", 0.0)),
                    int(r.get("od_appointments", 0)),
                    float(r.get("od_production_total", 0.0)),
                    float(r.get("od_production_per_click", 0.0)),
                    int(r.get("ga4_sessions", 0)),
                    float(r.get("ga4_avg_duration_sec", 0.0)),
                    float(r.get("ga4_bounce_rate", 0.0)),
                    int(r.get("ga4_lead_events", 0)),
                    float(r.get("session_quality_score", 0.0)),
                    int(r.get("times_recommended", 0)),
                    int(r.get("times_applied", 0)),
                    int(r.get("times_rejected", 0)),
                    r.get("last_decision_at", ""),
                    r.get("last_decision", ""),
                    float(r.get("true_roas", 0.0)),
                    int(r.get("data_age_days", 0)),
                    r.get("confidence_tier", "low"),
                    now,
                ),
            )
            written += 1
    return written


def get_keyword_intelligence(
    campaign_id: str = "",
    min_clicks: int = 0,
    days: int = 30,
) -> list:
    """Return keyword intelligence rows for the optimizer to read."""
    cutoff = (datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - __import__("datetime").timedelta(days=days)).isoformat()[:10]
    with _conn() as conn:
        if campaign_id:
            rows = conn.execute(
                "SELECT * FROM keyword_intelligence "
                "WHERE campaign_id=? AND date>=? AND clicks>=? "
                "ORDER BY date DESC, true_roas DESC",
                (campaign_id, cutoff, min_clicks),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM keyword_intelligence "
                "WHERE date>=? AND clicks>=? ORDER BY date DESC, true_roas DESC",
                (cutoff, min_clicks),
            ).fetchall()
        return [dict(r) for r in rows]


# ─── Phase A: Feedback Loop — Decision History helpers ───────────────────────

def get_recent_rejections(entity_id: str = "", days: int = 30) -> list:
    """Return audit log rows where execution_result='rejected', optionally filtered by entity."""
    cutoff = (datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - __import__("datetime").timedelta(days=days)).isoformat()
    with _conn() as conn:
        if entity_id:
            rows = conn.execute(
                "SELECT * FROM gads_audit_log "
                "WHERE execution_result='rejected' AND entity_id=? AND created_at>=? "
                "ORDER BY created_at DESC",
                (entity_id, cutoff),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM gads_audit_log "
                "WHERE execution_result='rejected' AND created_at>=? ORDER BY created_at DESC",
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]


def record_reject_reason(action_id: str, reject_reason: str) -> None:
    """Store the reject_reason when admin rejects an optimizer recommendation."""
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            "UPDATE gads_audit_log SET reject_reason=?, updated_at=? WHERE action_id=?",
            (reject_reason, now, action_id),
        )


# ─── Mango Voice helpers ──────────────────────────────────────────────────────

_UPSERT_MANGO_SQL = """
    INSERT INTO mango_calls
       (uuid, started_at, ended_at, direction, from_number, to_number,
        caller_id_name, caller_id_number, destination_number,
        extension_number, answered_by, duration_sec, status,
        recording_url, recording_local_path,
        lead_id, gads_call_id, match_confidence, match_method,
        raw_payload, synced_at, updated_at)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
       ON CONFLICT(uuid) DO UPDATE SET
         ended_at=excluded.ended_at,
         direction=excluded.direction,
         from_number=excluded.from_number,
         to_number=excluded.to_number,
         caller_id_name=excluded.caller_id_name,
         caller_id_number=excluded.caller_id_number,
         destination_number=excluded.destination_number,
         extension_number=excluded.extension_number,
         answered_by=excluded.answered_by,
         duration_sec=excluded.duration_sec,
         status=excluded.status,
         recording_url=COALESCE(excluded.recording_url, mango_calls.recording_url),
         recording_local_path=COALESCE(excluded.recording_local_path, mango_calls.recording_local_path),
         lead_id=COALESCE(excluded.lead_id, mango_calls.lead_id),
         gads_call_id=COALESCE(excluded.gads_call_id, mango_calls.gads_call_id),
         match_confidence=COALESCE(excluded.match_confidence, mango_calls.match_confidence),
         match_method=COALESCE(excluded.match_method, mango_calls.match_method),
         raw_payload=excluded.raw_payload,
         updated_at=excluded.updated_at
"""


def _mango_call_params(call: dict, now: str) -> tuple:
    return (
        call["uuid"],
        call.get("started_at", ""),
        call.get("ended_at"),
        call.get("direction", "inbound"),
        call.get("from_number"),
        call.get("to_number"),
        call.get("caller_id_name"),
        call.get("caller_id_number"),
        call.get("destination_number"),
        call.get("extension_number"),
        call.get("answered_by"),
        int(call.get("duration_sec", 0)),
        call.get("status"),
        call.get("recording_url"),
        call.get("recording_local_path"),
        call.get("lead_id"),
        call.get("gads_call_id"),
        call.get("match_confidence"),
        call.get("match_method"),
        call.get("raw_payload"),
        now,
        now,
    )


def upsert_mango_call(call: dict) -> None:
    """Insert or update a single Mango call record. Opens its own connection.
    For bulk syncs use upsert_mango_calls_batch() to avoid fd exhaustion."""
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(_UPSERT_MANGO_SQL, _mango_call_params(call, now))


def upsert_mango_calls_batch(calls: list) -> int:
    """Insert or update a list of Mango call dicts in a single connection/transaction.
    Returns count of successfully upserted rows.
    Use this instead of calling upsert_mango_call() in a loop to avoid hitting
    the OS file descriptor limit (each _conn() open = 3 fds for WAL mode)."""
    if not calls:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    count = 0
    with _conn() as conn:
        for call in calls:
            if not call.get("uuid"):
                continue
            conn.execute(_UPSERT_MANGO_SQL, _mango_call_params(call, now))
            count += 1
    return count


def get_mango_calls(
    limit: int = 100,
    offset: int = 0,
    direction: str = "",
    status: str = "",
    days: int = 30,
) -> list:
    """Return mango_calls rows enriched with GAds campaign name, newest first."""
    cutoff = (datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - __import__("datetime").timedelta(days=days)).isoformat()
    clauses = ["mc.started_at >= ?"]
    params: list = [cutoff]
    if direction:
        clauses.append("mc.direction = ?")
        params.append(direction)
    if status:
        clauses.append("mc.status = ?")
        params.append(status)
    where = " AND ".join(clauses)
    params.extend([limit, offset])
    with _conn() as conn:
        rows = conn.execute(
            f"""SELECT mc.uuid, mc.started_at, mc.ended_at, mc.direction,
                       mc.from_number, mc.to_number, mc.caller_id_name, mc.caller_id_number,
                       mc.destination_number, mc.extension_number, mc.answered_by,
                       mc.duration_sec, mc.status, mc.recording_url, mc.recording_local_path,
                       mc.lead_id, mc.gads_call_id, mc.match_confidence, mc.match_method,
                       mc.call_transcript, mc.call_summary, mc.lead_quality, mc.handling_grade,
                       mc.booked_outcome, mc.call_category, mc.transcription_status,
                       mc.transcript_model, mc.transcribed_at, mc.od_appointment_id,
                       mc.grade_overridden_by, mc.grade_overridden_at, mc.grade_override_reason,
                       mc.raw_payload, mc.synced_at, mc.updated_at,
                       mc.team_member, mc.grade_overall_score, mc.grade_overall_notes,
                       mc.grade_gradeable, mc.grade_scores_json, mc.is_empty,
                       -- GAds enrichment
                       gcv.campaign_name  AS gads_campaign_name,
                       gcv.campaign_id    AS gads_campaign_id,
                       gcv.call_duration_sec AS gads_duration_sec,
                       -- Lead enrichment
                       l.campaign_name   AS lead_campaign_name,
                       l.campaign_id     AS lead_campaign_id,
                       l.od_patient_num  AS lead_od_patient_num,
                       -- Unified campaign: prefer GAds attribution, fall back to lead's campaign
                       COALESCE(
                           NULLIF(gcv.campaign_name, ''),
                           NULLIF(l.campaign_name, '')
                       ) AS effective_campaign_name,
                       COALESCE(
                           NULLIF(gcv.campaign_id, ''),
                           NULLIF(l.campaign_id, '')
                       ) AS effective_campaign_id
                FROM mango_calls mc
                LEFT JOIN gads_call_view gcv ON gcv.call_id = mc.gads_call_id
                LEFT JOIN leads l ON l.id = mc.lead_id
                WHERE {where}
                ORDER BY mc.started_at DESC LIMIT ? OFFSET ?""",
            params,
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) FROM mango_calls mc WHERE {where}",
            params[:-2],
        ).fetchone()[0]
    return [dict(r) for r in rows], total


def get_mango_call(uuid: str) -> dict | None:
    """Return a single mango_call by uuid, enriched with campaign + lead data."""
    with _conn() as conn:
        row = conn.execute(
            """SELECT mc.uuid, mc.started_at, mc.ended_at, mc.direction,
                      mc.from_number, mc.to_number, mc.caller_id_name, mc.caller_id_number,
                      mc.destination_number, mc.extension_number, mc.answered_by,
                      mc.duration_sec, mc.status, mc.recording_url, mc.recording_local_path,
                      mc.lead_id, mc.gads_call_id, mc.match_confidence, mc.match_method,
                      mc.call_transcript, mc.call_summary, mc.lead_quality, mc.handling_grade,
                      mc.booked_outcome, mc.call_category, mc.transcription_status,
                      mc.transcript_model, mc.transcribed_at, mc.od_appointment_id,
                      mc.grade_overridden_by, mc.grade_overridden_at, mc.grade_override_reason,
                      mc.raw_payload, mc.synced_at, mc.updated_at,
                      mc.team_member, mc.grade_overall_score, mc.grade_overall_notes,
                      mc.grade_gradeable, mc.grade_scores_json, mc.is_empty,
                      gcv.campaign_name  AS gads_campaign_name,
                      gcv.campaign_id    AS gads_campaign_id,
                      gcv.call_duration_sec AS gads_duration_sec,
                      l.campaign_name   AS lead_campaign_name,
                      l.campaign_id     AS lead_campaign_id,
                      l.od_patient_num  AS lead_od_patient_num,
                      COALESCE(
                          NULLIF(gcv.campaign_name, ''),
                          NULLIF(l.campaign_name, '')
                      ) AS effective_campaign_name,
                      COALESCE(
                          NULLIF(gcv.campaign_id, ''),
                          NULLIF(l.campaign_id, '')
                      ) AS effective_campaign_id
               FROM mango_calls mc
               LEFT JOIN gads_call_view gcv ON gcv.call_id = mc.gads_call_id
               LEFT JOIN leads l ON l.id = mc.lead_id
               WHERE mc.uuid=?""",
            (uuid,),
        ).fetchone()
    return dict(row) if row else None


def update_mango_call_attribution(
    uuid: str,
    lead_id: str = None,
    gads_call_id: str = None,
    match_confidence: float = None,
    match_method: str = None,
) -> None:
    """Update attribution fields on a mango_calls row."""
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            """UPDATE mango_calls SET
               lead_id=COALESCE(?,lead_id),
               gads_call_id=COALESCE(?,gads_call_id),
               match_confidence=COALESCE(?,match_confidence),
               match_method=COALESCE(?,match_method),
               updated_at=?
               WHERE uuid=?""",
            (lead_id, gads_call_id, match_confidence, match_method, now, uuid),
        )


def update_mango_call_od_appointment(uuid: str, od_appointment_id: str) -> None:
    """Store the matched OD appointment ID on a mango_calls row.
    Only overwrites if od_appointment_id is currently NULL or empty (preserves manual edits)."""
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            """UPDATE mango_calls SET od_appointment_id=?, updated_at=?
               WHERE uuid=?
                 AND (od_appointment_id IS NULL OR od_appointment_id = '')""",
            (od_appointment_id, now, uuid),
        )


def get_booked_calls_needing_od_match(days: int = 90) -> list:
    """Return calls graded as booked but not yet matched to an OD appointment."""
    cutoff = (datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - __import__("datetime").timedelta(days=days)).isoformat()
    with _conn() as conn:
        rows = conn.execute(
            """SELECT mc.uuid, mc.started_at, mc.from_number, mc.caller_id_number,
                      mc.booked_outcome, mc.od_appointment_id, mc.lead_id
               FROM mango_calls mc
               WHERE mc.booked_outcome = 'booked'
                 AND (mc.od_appointment_id IS NULL OR mc.od_appointment_id = '')
                 AND mc.started_at >= ?
               ORDER BY mc.started_at DESC""",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_mango_calls_unmatched(days: int = 7) -> list:
    """Return inbound mango_calls with no gads_call_id attribution, for the matcher."""
    cutoff = (datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - __import__("datetime").timedelta(days=days)).isoformat()
    with _conn() as conn:
        rows = conn.execute(
            """SELECT * FROM mango_calls
               WHERE direction='inbound' AND gads_call_id IS NULL
               AND started_at >= ? ORDER BY started_at DESC""",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_missed_mango_calls_since(since_iso: str) -> list:
    """Return missed inbound calls since a given ISO timestamp (for Inbox alerts)."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT * FROM mango_calls
               WHERE direction='inbound' AND status='missed'
               AND started_at >= ? ORDER BY started_at DESC""",
            (since_iso,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_mango_calls_needing_od_match(limit: int = 500) -> list:
    """Return inbound calls that haven't been matched against OpenDental yet."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT uuid, from_number, caller_id_name, started_at
               FROM mango_calls
               WHERE direction='inbound'
                 AND (od_matched_at IS NULL OR od_matched_at = '')
                 AND from_number IS NOT NULL AND from_number != ''
               ORDER BY started_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def update_mango_call_od_status(
    uuid: str,
    od_patient_num: str,
    od_patient_status: str,
    od_patient_name: str,
) -> None:
    """Persist OD patient match result for a Mango call."""
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            """UPDATE mango_calls
               SET od_patient_num=?, od_patient_status=?, od_patient_name=?, od_matched_at=?, updated_at=?
               WHERE uuid=?""",
            (od_patient_num, od_patient_status, od_patient_name, now, now, uuid),
        )


# ─── Google Ads call_view helpers ─────────────────────────────────────────────

def upsert_gads_call_view(call: dict) -> None:
    """Insert or ignore a Google Ads call_view record."""
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            """INSERT INTO gads_call_view
               (call_id, customer_id, campaign_id, campaign_name,
                caller_country_code, caller_area_code,
                call_duration_sec, call_status, call_type,
                start_call_date_time, synced_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(call_id) DO UPDATE SET
                 call_duration_sec=excluded.call_duration_sec,
                 call_status=excluded.call_status,
                 synced_at=excluded.synced_at""",
            (
                call["call_id"],
                call.get("customer_id", ""),
                call.get("campaign_id", ""),
                call.get("campaign_name", ""),
                call.get("caller_country_code", ""),
                call.get("caller_area_code", ""),
                int(call.get("call_duration_sec", 0)),
                call.get("call_status", ""),
                call.get("call_type", ""),
                call.get("start_call_date_time", ""),
                now,
            ),
        )


def upsert_gads_clicks(clicks: list) -> int:
    """
    Bulk-upsert click_view rows into gads_clicks table.
    clicks: list of dicts with keys: gclid, click_date, keyword_text, match_type,
            ad_group_name, ad_group_id, ad_id, ad_name, campaign_name, campaign_id
    Returns number of rows upserted.
    """
    if not clicks:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.executemany(
            """INSERT INTO gads_clicks
               (gclid, click_date, keyword_text, match_type,
                ad_group_name, ad_group_id, ad_id, ad_name,
                campaign_name, campaign_id, synced_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(gclid) DO UPDATE SET
                 click_date=excluded.click_date,
                 keyword_text=excluded.keyword_text,
                 match_type=excluded.match_type,
                 ad_group_name=excluded.ad_group_name,
                 campaign_name=excluded.campaign_name,
                 campaign_id=excluded.campaign_id,
                 synced_at=excluded.synced_at""",
            [
                (
                    c["gclid"], c["click_date"],
                    c.get("keyword_text", ""), c.get("match_type", ""),
                    c.get("ad_group_name", ""), c.get("ad_group_id", ""),
                    c.get("ad_id", ""), c.get("ad_name", ""),
                    c.get("campaign_name", ""), c.get("campaign_id", ""),
                    now,
                )
                for c in clicks
            ],
        )
    return len(clicks)


def get_gads_call_view(days: int = 30) -> list:
    """Return recent Google Ads call_view rows for attribution matching."""
    cutoff = (datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - __import__("datetime").timedelta(days=days)).isoformat()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM gads_call_view WHERE start_call_date_time >= ? ORDER BY start_call_date_time DESC",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_gads_call_conversions(days: int = 30, min_duration_sec: int = 0) -> list:
    """Return Google Ads call_view rows joined to their matched Mango call record.

    Each row = one GAds call, enriched with full Mango data (caller number,
    transcript, grade, team member, lead match) when a match exists.
    LEFT JOIN so GAds calls with no Mango match still appear (unmatched).
    """
    cutoff = (datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - __import__("datetime").timedelta(days=days)).isoformat()
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT
                cv.call_id,
                cv.campaign_id,
                cv.campaign_name,
                cv.caller_area_code,
                cv.caller_country_code,
                cv.call_duration_sec      AS gads_duration_sec,
                cv.call_status            AS gads_call_status,
                cv.call_type,
                cv.start_call_date_time,
                -- Mango match fields (NULL when unmatched)
                mc.uuid                   AS mango_uuid,
                mc.from_number,
                mc.caller_id_name,
                mc.started_at,
                mc.duration_sec           AS mango_duration_sec,
                mc.status                 AS mango_status,
                mc.direction,
                mc.answered_by,
                mc.team_member,
                mc.match_confidence,
                mc.match_method,
                mc.call_transcript,
                mc.call_summary,
                mc.grade_overall_score,
                mc.grade_overall_notes,
                mc.grade_gradeable,
                mc.grade_scores_json      AS grade_scores,
                mc.transcription_status,
                mc.recording_url,
                mc.lead_id,
                -- Lead match fields (NULL when no lead)
                TRIM(COALESCE(l.first_name,'') || ' ' || COALESCE(l.last_name,'')) AS lead_name,
                l.email                   AS lead_email,
                l.stage                   AS lead_stage,
                l.campaign_name           AS lead_campaign
            FROM gads_call_view cv
            LEFT JOIN mango_calls mc ON mc.gads_call_id = cv.call_id
            LEFT JOIN leads l ON l.id = mc.lead_id
            WHERE cv.start_call_date_time >= ?
              AND cv.call_duration_sec >= ?
            ORDER BY cv.start_call_date_time DESC
            """,
            (cutoff, min_duration_sec),
        ).fetchall()
    return [dict(r) for r in rows]


# ── AI Usage helpers ───────────────────────────────────────────────────────────

def insert_ai_usage(
    api: str,
    purpose: str,
    cost_usd: float,
    model: str = "",
    call_id: str = "",
    campaign_id: str = "",
    lead_id: str = "",
    optimizer_run_id: str = "",
    minutes: float = 0.0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    request_ms: int = 0,
    error: str = "",
) -> None:
    """Insert one AI usage row into ai_usage table."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _conn() as conn:
        conn.execute(
            """INSERT INTO ai_usage
               (ts, api, model, purpose, call_id, campaign_id, lead_id, optimizer_run_id,
                minutes, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                cost_usd, request_ms, error)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now, api, model, purpose, call_id, campaign_id, lead_id, optimizer_run_id,
             minutes, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
             round(cost_usd, 8), request_ms, error),
        )


def get_ai_cost_summary(days: int = 30) -> dict:
    """Return cost totals grouped by api, model, and purpose for the given window + lifetime."""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    cutoff_window = (now - timedelta(days=days)).isoformat(timespec="seconds")

    def _query(cutoff=None):
        where = "WHERE ts >= ?" if cutoff else ""
        params = (cutoff,) if cutoff else ()
        with _conn() as conn:
            total_row = conn.execute(
                f"SELECT SUM(cost_usd) as cost, SUM(minutes) as mins, "
                f"SUM(input_tokens+output_tokens) as tokens FROM ai_usage {where}", params
            ).fetchone()
            by_api = conn.execute(
                f"SELECT api, SUM(cost_usd) as cost, SUM(minutes) as mins, "
                f"SUM(input_tokens+output_tokens) as tokens, COUNT(*) as calls "
                f"FROM ai_usage {where} GROUP BY api", params
            ).fetchall()
            by_model = conn.execute(
                f"SELECT model, api, SUM(cost_usd) as cost, COUNT(*) as calls "
                f"FROM ai_usage {where} GROUP BY model, api ORDER BY cost DESC", params
            ).fetchall()
            by_purpose = conn.execute(
                f"SELECT purpose, api, SUM(cost_usd) as cost, COUNT(*) as calls "
                f"FROM ai_usage {where} GROUP BY purpose ORDER BY cost DESC", params
            ).fetchall()
            daily = conn.execute(
                f"SELECT substr(ts,1,10) as date, api, SUM(cost_usd) as cost "
                f"FROM ai_usage {where} GROUP BY date, api ORDER BY date DESC LIMIT 90", params
            ).fetchall()
        return {
            "total_cost": round(total_row["cost"] or 0, 4),
            "total_minutes": round(total_row["mins"] or 0, 2),
            "total_tokens": int(total_row["tokens"] or 0),
            "by_api": {r["api"]: {"cost": round(r["cost"] or 0, 4), "mins": round(r["mins"] or 0, 2),
                                   "tokens": int(r["tokens"] or 0), "calls": r["calls"]}
                       for r in by_api},
            "by_model": [{"model": r["model"], "api": r["api"],
                          "cost": round(r["cost"] or 0, 4), "calls": r["calls"]} for r in by_model],
            "by_purpose": [{"purpose": r["purpose"], "api": r["api"],
                            "cost": round(r["cost"] or 0, 4), "calls": r["calls"]} for r in by_purpose],
            "daily": [{"date": r["date"], "api": r["api"],
                       "cost": round(r["cost"] or 0, 4)} for r in daily],
        }

    window = _query(cutoff_window)
    lifetime = _query(None)
    return {"window_days": days, "window": window, "lifetime": lifetime}


# ── Call grading criteria helpers ──────────────────────────────────────────────

def get_call_grading_criteria() -> list:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM call_grading_criteria WHERE active=1 ORDER BY sort_order"
        ).fetchall()
    return [dict(r) for r in rows]


def set_call_grading_criteria(criteria: list) -> None:
    """Replace all active criteria with the supplied list (transactional)."""
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute("DELETE FROM call_grading_criteria")
        for i, c in enumerate(criteria):
            conn.execute(
                "INSERT INTO call_grading_criteria (name, weight, description, sort_order, active, created_at, updated_at) "
                "VALUES (?,?,?,?,1,?,?)",
                (c.get("name",""), int(c.get("weight", 0)), c.get("description",""), i, now, now)
            )


def reset_call_grading_criteria() -> None:
    """Restore the 7 default Grafton criteria."""
    with _conn() as conn:
        conn.execute("DELETE FROM call_grading_criteria")
        _seed_call_grading_criteria(conn)


# ── Call team members helpers ──────────────────────────────────────────────────

def get_call_team_members() -> list:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM call_team_members ORDER BY name"
        ).fetchall()
    return [dict(r) for r in rows]


def set_call_team_members(members: list) -> None:
    """Replace team members list (transactional)."""
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute("DELETE FROM call_team_members")
        for m in members:
            conn.execute(
                "INSERT OR IGNORE INTO call_team_members (name, extension, active, created_at, updated_at) VALUES (?,?,?,?,?)",
                (m.get("name",""), m.get("extension",""), 1 if m.get("active", True) else 0, now, now)
            )


# ── Call bulk job helpers ──────────────────────────────────────────────────────

def insert_call_bulk_job(
    date_from: str,
    date_to: str,
    do_grade: bool,
    options: Optional[dict] = None,
) -> int:
    """Create a new bulk job record, return its ID."""
    now = datetime.now(timezone.utc).isoformat()
    opts_json = json.dumps(options or {})
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO call_bulk_jobs (status, date_from, date_to, do_grade, options_json, started_at) VALUES (?,?,?,?,?,?)",
            ("queued", date_from, date_to, 1 if do_grade else 0, opts_json, now)
        )
        return cur.lastrowid


def get_call_bulk_job(job_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM call_bulk_jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["options"] = json.loads(d.get("options_json") or "{}")
    except Exception:
        d["options"] = {}
    return d


def get_active_call_bulk_job() -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM call_bulk_jobs WHERE status IN ('queued','running') ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def update_call_bulk_job(job_id: int, **kwargs) -> None:
    _BULK_JOB_COLS = {
        "status", "total", "done", "graded", "skipped", "errors",
        "current_label", "started_at", "finished_at", "error_msg",
    }
    fields = {k: v for k, v in kwargs.items() if k in _BULK_JOB_COLS}
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [job_id]
    with _conn() as conn:
        conn.execute(f"UPDATE call_bulk_jobs SET {sets} WHERE id=?", vals)


# ── Mango pipeline helpers ─────────────────────────────────────────────────────

def update_mango_call_analysis(uuid: str, **kwargs) -> None:
    """Update any subset of analysis columns on a mango_calls row."""
    _ALLOWED_COLS = {
        "call_transcript", "call_summary", "team_member",
        "grade_scores_json", "grade_overall_score", "grade_overall_notes",
        "grade_recommendations_json", "grade_gradeable", "grade_reason",
        "graded_at", "summarized_at", "is_empty", "pipeline_error", "pipeline_attempts",
        "lead_id", "gads_resource_name", "transcription_status",
        # Phase 3: AI next-action columns
        "call_next_action", "call_next_action_type", "call_next_action_due",
        "call_next_action_completed", "call_next_action_completed_at",
        "call_next_action_completed_by", "call_next_action_suggested_at",
        "call_next_action_priority", "call_next_action_reasoning",
    }
    fields = {k: v for k, v in kwargs.items() if k in _ALLOWED_COLS}
    if not fields:
        return
    now = datetime.now(timezone.utc).isoformat()
    fields["updated_at"] = now
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [uuid]
    with _conn() as conn:
        conn.execute(f"UPDATE mango_calls SET {sets} WHERE uuid=?", vals)


def get_calls_needing_processing(
    min_seconds: int = 30,
    max_attempts: int = 3,
    batch_size: int = 20,
    days_back: int = 90,
) -> list:
    """Return mango_calls that need transcription/analysis, newest first."""
    cutoff = (datetime.now(timezone.utc) - __import__("datetime").timedelta(days=days_back)).isoformat()
    with _conn() as conn:
        rows = conn.execute(
            """SELECT * FROM mango_calls
               WHERE direction = 'inbound'
                 AND duration_sec >= ?
                 AND status NOT IN ('missed', 'busy')
                 AND (transcription_status IS NULL OR transcription_status IN ('pending', 'failed'))
                 AND (pipeline_attempts IS NULL OR pipeline_attempts < ?)
                 AND started_at >= ?
               ORDER BY started_at DESC
               LIMIT ?""",
            (min_seconds, max_attempts, cutoff, batch_size)
        ).fetchall()
    return [dict(r) for r in rows]


def mark_call_action_completed(uuid: str, by_email: str = "") -> None:
    """Mark a call's AI next-action as completed."""
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            "UPDATE mango_calls SET call_next_action_completed=1, "
            "call_next_action_completed_at=?, call_next_action_completed_by=?, "
            "updated_at=? WHERE uuid=?",
            (now, by_email, now, uuid)
        )


def get_campaign_call_stats(campaign_id: int | None = None) -> list[dict]:
    """Return call quality stats per campaign from v_campaign_call_stats.
    Synthetic (unmanaged) campaigns have no row in the view — callers must handle None gracefully.
    """
    with _conn() as conn:
        if campaign_id is not None:
            rows = conn.execute(
                "SELECT * FROM v_campaign_call_stats WHERE campaign_id=?", (campaign_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM v_campaign_call_stats").fetchall()
        return [dict(r) for r in rows]


def get_urgent_action_count() -> int:
    """Lightweight count of urgent pending actions for the bell badge.
    Runs 3 fast COUNT queries instead of materialising the full 200-item feed.
    Called every 15s by the inbox/unread poll — must be cheap.
    """
    from datetime import date
    today = date.today().isoformat()
    count = 0
    with _conn() as conn:
        # Overdue AI call next-actions
        r = conn.execute("""
            SELECT COUNT(*) FROM mango_calls
            WHERE COALESCE(call_next_action,'') != ''
              AND COALESCE(call_next_action_completed,0) = 0
              AND COALESCE(call_next_action_type,'') NOT IN ('no_action','')
              AND COALESCE(call_next_action_due,'') != ''
              AND call_next_action_due < ?
        """, (today,)).fetchone()
        count += r[0] if r else 0
        # Missed callbacks in last 7 days with no outbound follow-up
        r = conn.execute("""
            SELECT COUNT(*) FROM mango_calls mc
            WHERE mc.direction = 'inbound'
              AND mc.status IN ('missed','no_answer','busy','voicemail')
              AND mc.started_at >= datetime('now','-7 days')
              AND NOT EXISTS (
                  SELECT 1 FROM mango_calls mc2
                  WHERE mc2.lead_id = mc.lead_id
                    AND mc2.direction = 'outbound'
                    AND mc2.started_at > mc.started_at
              )
        """).fetchone()
        count += r[0] if r else 0
        # Overdue user-set lead next actions
        r = conn.execute("""
            SELECT COUNT(*) FROM leads
            WHERE COALESCE(next_action_at,'') != ''
              AND next_action_at < datetime('now')
        """).fetchone()
        count += r[0] if r else 0
    return count


def get_pending_actions(limit: int = 200) -> list[dict]:
    """
    Unified prioritized feed for the Actions tab.
    Merges 4 sources: AI call next-actions, unread messages, missed callbacks, overdue lead next-actions.
    Returns list of dicts sorted by priority (urgent→soon→low) then due_at.
    """
    from datetime import datetime, timezone, date
    now_iso = datetime.now(timezone.utc).isoformat()
    today = date.today().isoformat()
    items = []

    with _conn() as conn:
        # Source A: AI call next-actions (uncompleted, non-no_action)
        rows = conn.execute("""
            SELECT mc.uuid, mc.call_next_action, mc.call_next_action_type,
                   mc.call_next_action_due, mc.call_next_action_priority,
                   mc.call_next_action_reasoning, mc.call_next_action_suggested_at,
                   mc.lead_id, mc.from_number, mc.started_at,
                   l.first_name, l.last_name, l.email, l.phone
              FROM mango_calls mc
              LEFT JOIN leads l ON l.id = mc.lead_id
             WHERE COALESCE(mc.call_next_action,'') != ''
               AND COALESCE(mc.call_next_action_completed,0) = 0
               AND COALESCE(mc.call_next_action_type,'') NOT IN ('no_action','')
             ORDER BY mc.call_next_action_due ASC
             LIMIT ?
        """, (limit,)).fetchall()
        for r in rows:
            r = dict(r)
            prio = r.get("call_next_action_priority") or "soon"
            due = r.get("call_next_action_due") or ""
            if due and due < today:
                prio = "urgent"  # overdue → escalate
            lead_name = " ".join(filter(None, [r.get("first_name"), r.get("last_name")])) or r.get("from_number") or "Unknown Caller"
            items.append({
                "action_id": f"call:{r['uuid']}",
                "kind": "ai_call_action",
                "priority": prio,
                "action_type": r.get("call_next_action_type", "other"),
                "lead_id": r.get("lead_id"),
                "lead_name": lead_name,
                "lead_phone": r.get("phone"),
                "lead_email": r.get("email"),
                "title": r.get("call_next_action", ""),
                "description": r.get("call_next_action_reasoning", ""),
                "due_at": due or None,
                "source_call_uuid": r["uuid"],
                "completed": False,
            })

        # Source B: Unread SMS leads
        sms_rows = conn.execute("""
            SELECT l.id AS lead_id, l.first_name, l.last_name, l.phone, l.email,
                   MAX(sm.received_at) AS last_msg
              FROM sms_messages sm
              JOIN leads l ON l.id = sm.lead_id
             WHERE sm.direction='inbound' AND (sm.read_at IS NULL OR sm.read_at='')
             GROUP BY l.id
        """).fetchall()
        for r in sms_rows:
            r = dict(r)
            lead_name = " ".join(filter(None, [r.get("first_name"), r.get("last_name")])) or "Unknown"
            items.append({
                "action_id": f"sms:{r['lead_id']}",
                "kind": "unread_message",
                "priority": "soon",
                "action_type": "reply",
                "lead_id": r["lead_id"],
                "lead_name": lead_name,
                "lead_phone": r.get("phone"),
                "lead_email": r.get("email"),
                "title": f"Unread SMS from {lead_name}",
                "description": "",
                "due_at": None,
                "source_call_uuid": None,
                "completed": False,
            })

        # Source C: Missed callbacks (inbound missed within 7 days, no outbound follow-up)
        missed_rows = conn.execute("""
            SELECT mc.uuid, mc.from_number, mc.caller_id_name, mc.started_at, mc.lead_id,
                   l.first_name, l.last_name, l.phone, l.email
              FROM mango_calls mc
              LEFT JOIN leads l ON l.id = mc.lead_id
             WHERE mc.direction = 'inbound'
               AND mc.status IN ('missed','no_answer','busy','voicemail')
               AND mc.started_at >= datetime('now','-7 days')
               AND NOT EXISTS (
                    SELECT 1 FROM mango_calls mc2
                    WHERE mc2.lead_id = mc.lead_id
                      AND mc2.direction = 'outbound'
                      AND mc2.started_at > mc.started_at
               )
             ORDER BY mc.started_at DESC
             LIMIT ?
        """, (limit,)).fetchall()
        for r in missed_rows:
            r = dict(r)
            lead_name = " ".join(filter(None, [r.get("first_name"), r.get("last_name")])) or r.get("caller_id_name") or r.get("from_number") or "Unknown Caller"
            items.append({
                "action_id": f"missed:{r['uuid']}",
                "kind": "missed_callback",
                "priority": "urgent",
                "action_type": "callback",
                "lead_id": r.get("lead_id"),
                "lead_name": lead_name,
                "lead_phone": r.get("phone") or r.get("from_number"),
                "lead_email": r.get("email"),
                "title": f"Missed call — call back {lead_name}",
                "description": f"Missed inbound call at {r.get('started_at','')}",
                "due_at": None,
                "source_call_uuid": r["uuid"],
                "completed": False,
            })

        # Source D: Overdue user-set lead next actions
        overdue_rows = conn.execute("""
            SELECT id, first_name, last_name, phone, email, next_action_at, next_action_note
              FROM leads
             WHERE COALESCE(next_action_at,'') != ''
               AND next_action_at < ?
        """, (now_iso,)).fetchall()
        for r in overdue_rows:
            r = dict(r)
            lead_name = " ".join(filter(None, [r.get("first_name"), r.get("last_name")])) or "Unknown"
            items.append({
                "action_id": f"lead:{r['id']}",
                "kind": "overdue_action",
                "priority": "urgent",
                "action_type": "follow_up_call",
                "lead_id": r["id"],
                "lead_name": lead_name,
                "lead_phone": r.get("phone"),
                "lead_email": r.get("email"),
                "title": r.get("next_action_note") or f"Follow up with {lead_name}",
                "description": f"Overdue since {r.get('next_action_at','')}",
                "due_at": r.get("next_action_at"),
                "source_call_uuid": None,
                "completed": False,
            })

    PRIO = {"urgent": 0, "soon": 1, "low": 2}
    items.sort(key=lambda x: (PRIO.get(x["priority"], 3), x.get("due_at") or "9999"))
    return items[:limit]


# ── Google Recommendations ────────────────────────────────────────────────────

def upsert_google_rec(resource_name: str, rec_type: str, campaign_resource: str,
                      campaign_name: str, ad_group_resource: str, title: str,
                      description: str, impact_json: str, details_json: str,
                      fetched_at: str) -> int:
    """Insert or update a Google recommendation row. Returns row id."""
    with _conn() as conn:
        conn.execute("""
            INSERT INTO gads_google_recs
                (resource_name, rec_type, campaign_resource, campaign_name,
                 ad_group_resource, title, description, impact_json, details_json, fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(resource_name) DO UPDATE SET
                rec_type=excluded.rec_type,
                campaign_resource=excluded.campaign_resource,
                campaign_name=excluded.campaign_name,
                title=excluded.title,
                description=excluded.description,
                impact_json=excluded.impact_json,
                details_json=excluded.details_json,
                fetched_at=excluded.fetched_at,
                claude_verdict='',
                claude_reasoning='',
                dismissed=0
        """, (resource_name, rec_type, campaign_resource, campaign_name,
              ad_group_resource, title, description, impact_json, details_json, fetched_at))
        row = conn.execute("SELECT id FROM gads_google_recs WHERE resource_name=?", (resource_name,)).fetchone()
        return row[0] if row else 0


def update_google_rec_verdict(resource_name: str, verdict: str, reasoning: str):
    with _conn() as conn:
        conn.execute("""
            UPDATE gads_google_recs SET claude_verdict=?, claude_reasoning=?
            WHERE resource_name=?
        """, (verdict, reasoning, resource_name))


def get_google_recs(dismissed: bool = False) -> list:
    with _conn() as conn:
        rows = conn.execute("""
            SELECT id, resource_name, rec_type, campaign_resource, campaign_name,
                   ad_group_resource, title, description, impact_json, details_json,
                   claude_verdict, claude_reasoning, dismissed, fetched_at
            FROM gads_google_recs
            WHERE dismissed = ?
            ORDER BY fetched_at DESC
        """, (1 if dismissed else 0,)).fetchall()
        cols = ['id','resource_name','rec_type','campaign_resource','campaign_name',
                'ad_group_resource','title','description','impact_json','details_json',
                'claude_verdict','claude_reasoning','dismissed','fetched_at']
        return [dict(zip(cols, r)) for r in rows]


def dismiss_google_rec(resource_name: str):
    with _conn() as conn:
        conn.execute("UPDATE gads_google_recs SET dismissed=1 WHERE resource_name=?", (resource_name,))


def save_impact_history(
    run_id: str,
    run_date: str,
    estimated_waste_saved_usd: float = 0.0,
    estimated_cpl_improvement: float = 0.0,
    estimated_coverage_gain: float = 0.0,
    total_recs: int = 0,
    approved_recs: int = 0,
    rejected_recs: int = 0,
    benchmark_gaps_json: str = "{}",
    impact_by_type_json: str = "{}",
) -> None:
    """Insert or replace an impact history record for an optimizer run.
    Computes cumulative savings by summing all prior approved savings."""
    now = _now()
    with _conn() as conn:
        # Compute running cumulative savings
        row = conn.execute(
            "SELECT COALESCE(MAX(cum_estimated_savings), 0) FROM gads_impact_history"
        ).fetchone()
        prior_cum = float(row[0]) if row else 0.0
        cum_savings = round(prior_cum + estimated_waste_saved_usd, 2)

        conn.execute("""
            INSERT INTO gads_impact_history
                (run_id, run_date, estimated_waste_saved_usd, estimated_cpl_improvement,
                 estimated_coverage_gain, total_recs, approved_recs, rejected_recs,
                 cum_estimated_savings, benchmark_gaps_json, impact_by_type_json,
                 actual_impact_json, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,'{}',?,?)
            ON CONFLICT(run_id) DO UPDATE SET
                estimated_waste_saved_usd=excluded.estimated_waste_saved_usd,
                estimated_cpl_improvement=excluded.estimated_cpl_improvement,
                estimated_coverage_gain=excluded.estimated_coverage_gain,
                total_recs=excluded.total_recs,
                approved_recs=excluded.approved_recs,
                rejected_recs=excluded.rejected_recs,
                cum_estimated_savings=excluded.cum_estimated_savings,
                benchmark_gaps_json=excluded.benchmark_gaps_json,
                impact_by_type_json=excluded.impact_by_type_json,
                updated_at=excluded.updated_at
        """, (
            run_id, run_date, estimated_waste_saved_usd, estimated_cpl_improvement,
            estimated_coverage_gain, total_recs, approved_recs, rejected_recs,
            cum_savings, benchmark_gaps_json, impact_by_type_json, now, now,
        ))


def get_impact_history(limit: int = 30) -> list:
    """Return impact history records newest first."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT * FROM gads_impact_history ORDER BY run_date DESC LIMIT ?""",
            (limit,)
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            for jf in ("benchmark_gaps_json", "impact_by_type_json", "actual_impact_json"):
                try:
                    d[jf.replace("_json", "")] = json.loads(d.get(jf) or "{}")
                except Exception:
                    d[jf.replace("_json", "")] = {}
            result.append(d)
        return result


# ─── Domain Registry helpers ─────────────────────────────────────────────────

def list_domains() -> list:
    """Return all registered domains ordered by created_at."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM domain_registry ORDER BY created_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_domain(domain_id: int) -> Optional[dict]:
    """Return a single domain record or None."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM domain_registry WHERE id=?", (domain_id,)
        ).fetchone()
        return dict(row) if row else None


def get_domain_by_url(domain_url: str) -> Optional[dict]:
    """Return domain record matching url (case-insensitive) or None."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM domain_registry WHERE LOWER(domain_url)=LOWER(?)", (domain_url,)
        ).fetchone()
        return dict(row) if row else None


def create_domain(
    domain_url: str,
    label: str = "",
    domain_type: str = "practice",
    crawl_enabled: bool = True,
    crawl_depth: int = 20,
) -> int:
    """Insert a new domain and return its id."""
    now = _now()
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO domain_registry
               (domain_url, label, domain_type, crawl_enabled, crawl_depth,
                crawl_status, created_at, updated_at)
               VALUES (?,?,?,?,?,'idle',?,?)""",
            (domain_url, label, domain_type, int(crawl_enabled), crawl_depth, now, now),
        )
        return cur.lastrowid


def update_domain(domain_id: int, **fields) -> None:
    """Update arbitrary fields on a domain_registry row."""
    allowed = {
        "label", "domain_type", "crawl_enabled", "crawl_depth",
        "last_crawled_at", "crawl_status", "pages_found", "updated_at",
    }
    cols = {k: v for k, v in fields.items() if k in allowed}
    if not cols:
        return
    cols["updated_at"] = _now()
    set_clause = ", ".join(f"{k}=?" for k in cols)
    with _conn() as conn:
        conn.execute(
            f"UPDATE domain_registry SET {set_clause} WHERE id=?",
            (*cols.values(), domain_id),
        )


def delete_domain(domain_id: int) -> None:
    """Delete a domain and cascade-delete its pages + crawl logs."""
    with _conn() as conn:
        conn.execute("DELETE FROM domain_registry WHERE id=?", (domain_id,))


def upsert_domain_page(
    domain_id: int,
    page_url: str,
    page_title: str = "",
    meta_description: str = "",
    h1_text: str = "",
    body_excerpt: str = "",
    word_count: int = 0,
    has_form: bool = False,
    cta_phrases: list = None,
    internal_links: list = None,
    content_hash: str = "",
) -> None:
    """Insert or replace a crawled page record."""
    now = _now()
    with _conn() as conn:
        conn.execute(
            """INSERT INTO domain_pages
               (domain_id, page_url, page_title, meta_description, h1_text,
                body_excerpt, word_count, has_form, cta_phrases, internal_links,
                content_hash, crawled_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(domain_id, page_url) DO UPDATE SET
                   page_title=excluded.page_title,
                   meta_description=excluded.meta_description,
                   h1_text=excluded.h1_text,
                   body_excerpt=excluded.body_excerpt,
                   word_count=excluded.word_count,
                   has_form=excluded.has_form,
                   cta_phrases=excluded.cta_phrases,
                   internal_links=excluded.internal_links,
                   content_hash=excluded.content_hash,
                   crawled_at=excluded.crawled_at""",
            (
                domain_id, page_url, page_title, meta_description, h1_text,
                body_excerpt, word_count, int(has_form),
                json.dumps(cta_phrases or []),
                json.dumps(internal_links or []),
                content_hash,
                now,
            ),
        )


def list_domain_pages(domain_id: int) -> list:
    """Return all crawled pages for a domain."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM domain_pages WHERE domain_id=? ORDER BY crawled_at DESC",
            (domain_id,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            for jf in ("cta_phrases", "internal_links"):
                try:
                    d[jf] = json.loads(d.get(jf) or "[]")
                except Exception:
                    d[jf] = []
            result.append(d)
        return result


def delete_domain_pages(domain_id: int) -> None:
    """Remove all cached pages for a domain (before a fresh crawl)."""
    with _conn() as conn:
        conn.execute("DELETE FROM domain_pages WHERE domain_id=?", (domain_id,))


def insert_crawl_log(domain_id: int) -> int:
    """Start a crawl log entry and return its id."""
    now = _now()
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO domain_crawl_log
               (domain_id, started_at, status) VALUES (?,?,'running')""",
            (domain_id, now),
        )
        return cur.lastrowid


def finish_crawl_log(
    log_id: int,
    pages_crawled: int = 0,
    pages_failed: int = 0,
    status: str = "done",
    error_msg: str = "",
) -> None:
    """Mark a crawl log entry as finished."""
    now = _now()
    with _conn() as conn:
        conn.execute(
            """UPDATE domain_crawl_log
               SET finished_at=?, pages_crawled=?, pages_failed=?, status=?, error_msg=?
               WHERE id=?""",
            (now, pages_crawled, pages_failed, status, error_msg, log_id),
        )
