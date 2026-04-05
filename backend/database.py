"""
SQLite database — leads, events, follow-up queue, unsubscribes, OD matches.
"""
import sqlite3
import os
import json
import hashlib
from datetime import datetime, timezone
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
    smile_image_url TEXT DEFAULT '',       -- GCS signed URL to smile image
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
    ad_id           TEXT DEFAULT '',       -- ad creative ID
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
    author      TEXT NOT NULL DEFAULT 'admin',  -- 'admin' or 'ai_agent'
    active      INTEGER NOT NULL DEFAULT 1,     -- 1=active, 0=deactivated (soft delete)
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_optimizer_memory_category ON optimizer_memory(category, active);
CREATE INDEX IF NOT EXISTS idx_optimizer_memory_key ON optimizer_memory(key, active);

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
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_gads_kw_cache_key ON gads_keywords_cache(keyword_text, days);
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
        ("ad_id",               "TEXT DEFAULT ''"),
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
            author      TEXT NOT NULL DEFAULT 'admin',
            active      INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_optimizer_memory_category ON optimizer_memory(category, active)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_optimizer_memory_key ON optimizer_memory(key, active)")

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

    # Seed default memories if table is empty
    existing = conn.execute("SELECT COUNT(*) FROM optimizer_memory").fetchone()[0]
    if existing == 0:
        now = datetime.now(timezone.utc).isoformat()
        seeds = [
            ('keyword_override', 'all on 4 dental implants', 'never_pause',
             'Core campaign keyword — zero leads is gclid attribution gap, not actual performance', 'admin'),
            ('term_classification', 'free gingival graft', 'irrelevant',
             'Periodontal procedure name — patient already has implant, not a new full-arch case buyer', 'admin'),
            ('term_classification', 'free connective tissue graft', 'irrelevant',
             'Periodontal procedure name — not relevant to implant/cosmetic campaign', 'admin'),
            ('general', 'attribution_note', 'gclid_tracking_started_apr_2026',
             'All leads before April 2026 have no keyword attribution. Zero leads on a keyword before this date is not real data.', 'admin'),
        ]
        for category, key, value, reason, author in seeds:
            conn.execute(
                "INSERT INTO optimizer_memory (category, key, value, reason, author, active, created_at, updated_at) VALUES (?,?,?,?,?,1,?,?)",
                (category, key, value, reason, author, now, now)
            )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()


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
                        "smile_image_url","smile_generated_at","source","notes",
                        "booking_id","od_patient_num","attributed_production",
                        "treatment_plan_value","attributed_income",
                        "appointment_date","appointment_status","no_show_count",
                        "keyword_text","search_term","ad_group_name","ad_id",
                        "campaign_name","campaign_id",
                        "click_cost","gads_synced_at"]:
                if data.get(col) not in (None, ""):
                    fields.append(f"{col}=?")
                    values.append(data[col])
            if phone_raw:
                fields += ["phone_hash=?"]
                values += [_hash(phone_digits) if phone_digits else ""]
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
                    smile_image_url, notes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                lead_id, data.get("created_at", now), now,
                data.get("source", "unknown"), data.get("stage", "new"),
                data.get("first_name", ""), data.get("last_name", ""),
                email_raw, phone_raw,
                _hash(phone_digits) if phone_digits else "",
                _hash(email_raw) if email_raw else "",
                json.dumps(data.get("goals", [])) if isinstance(data.get("goals"), list) else data.get("goals", ""),
                data.get("gclid", ""), data.get("fbclid", ""), data.get("msclkid", ""),
                data.get("utm_source", ""), data.get("utm_medium", ""),
                data.get("utm_campaign", ""), data.get("utm_term", ""),
                data.get("utm_content", ""), data.get("landing_url", ""),
                data.get("smile_image_url", ""), data.get("notes", ""),
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


# ─── Follow-up Queue ─────────────────────────────────────────────────────────

def enqueue_follow_ups(lead_id: str, created_at: str):
    """Schedule the full follow-up sequence for a new lead."""
    from datetime import timedelta
    import dateutil.parser

    try:
        base = dateutil.parser.parse(created_at)
    except Exception:
        base = datetime.now(timezone.utc)

    sequence = [
        (1,  "email", "day1_email"),
        (3,  "sms",   "day3_sms"),
        (7,  "email", "day7_email"),
        (14, "email", "day14_email"),
        (21, "sms",   "day21_sms"),
        (30, "email", "day30_cold"),   # marks cold + deletes image
    ]

    with _conn() as conn:
        # Don't double-enqueue
        existing = conn.execute("SELECT COUNT(*) FROM follow_up_queue WHERE lead_id=?", (lead_id,)).fetchone()[0]
        if existing > 0:
            return

        for day, channel, template in sequence:
            send_at = (base + timedelta(days=day)).isoformat()
            conn.execute("""
                INSERT INTO follow_up_queue (lead_id, sequence_day, channel, template, scheduled_at, status)
                VALUES (?,?,?,?,?,'pending')
            """, (lead_id, day, channel, template, send_at))


def get_due_follow_ups() -> list:
    """Return all pending follow-ups that are due now."""
    now = _now()
    with _conn() as conn:
        rows = conn.execute("""
            SELECT fq.*, l.email, l.phone, l.first_name, l.last_name,
                   l.goals, l.smile_image_url, l.stage, l.unsubscribed_email, l.unsubscribed_sms,
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
    """Return lead counts and revenue grouped by Google Ads campaign (only leads with campaign data)."""
    with _conn() as conn:
        rows = conn.execute("""
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
                SUM(click_cost) as total_ad_spend,
                AVG(NULLIF(click_cost, 0)) as avg_cpc,
                -- Calculated metrics
                CASE WHEN COUNT(*) > 0 THEN ROUND(SUM(click_cost) / COUNT(*), 2) ELSE 0 END as cpl,
                CASE WHEN SUM(CASE WHEN stage IN ('scheduled','showed','no_show',
                    'treatment_presented','treatment_accepted','treatment_completed') THEN 1 ELSE 0 END) > 0
                    THEN ROUND(SUM(click_cost) / SUM(CASE WHEN stage IN ('scheduled','showed','no_show',
                    'treatment_presented','treatment_accepted','treatment_completed') THEN 1 ELSE 0 END), 2)
                    ELSE 0 END as cpa,
                CASE WHEN SUM(click_cost) > 0
                    THEN ROUND(SUM(attributed_production) / SUM(click_cost), 2)
                    ELSE 0 END as roas,
                CASE WHEN COUNT(*) > 0
                    THEN ROUND(CAST(SUM(CASE WHEN stage IN ('treatment_presented','treatment_accepted','treatment_completed') THEN 1 ELSE 0 END) AS REAL) / COUNT(*) * 100, 1)
                    ELSE 0 END as conversion_rate
            FROM leads
            WHERE campaign_name != '' OR utm_campaign != ''
            GROUP BY COALESCE(NULLIF(campaign_name, ''), utm_campaign)
            ORDER BY lead_count DESC
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


def save_gads_keywords_cache(keywords: list, days: int = 30):
    """
    Save Google Ads keyword performance to cache.
    keywords: list of dicts with keyword_text, match_type, ad_group_name, campaign_name,
              impressions, clicks, cost, avg_cpc, conversions
    """
    now = _now()
    with _conn() as conn:
        for kw in keywords:
            conn.execute("""
                INSERT INTO gads_keywords_cache
                    (keyword_text, match_type, ad_group_name, campaign_name,
                     impressions, clicks, cost, avg_cpc, conversions, days, synced_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(keyword_text, days) DO UPDATE SET
                    match_type=excluded.match_type,
                    ad_group_name=excluded.ad_group_name,
                    campaign_name=excluded.campaign_name,
                    impressions=excluded.impressions,
                    clicks=excluded.clicks,
                    cost=excluded.cost,
                    avg_cpc=excluded.avg_cpc,
                    conversions=excluded.conversions,
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
                days,
                now,
            ))


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
                COALESCE(k.impressions, 0)  AS impressions,
                COALESCE(k.clicks, 0)       AS gads_clicks,
                COALESCE(k.cost, 0.0)       AS total_cost,
                COALESCE(k.avg_cpc, 0.0)    AS avg_cpc,
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


# ─── Optimizer Memory ─────────────────────────────────────────────────────────

def get_optimizer_memory(category: Optional[str] = None, active_only: bool = True) -> list:
    """Return all optimizer memory entries, optionally filtered by category."""
    with _conn() as conn:
        if category:
            rows = conn.execute(
                "SELECT * FROM optimizer_memory WHERE category=? AND active=? ORDER BY category, key",
                (category, 1 if active_only else 0)
            ).fetchall()
        elif active_only:
            rows = conn.execute(
                "SELECT * FROM optimizer_memory WHERE active=1 ORDER BY category, key"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM optimizer_memory ORDER BY category, key"
            ).fetchall()
        return [dict(r) for r in rows]


def get_optimizer_memory_dict() -> dict:
    """
    Return memory as a structured dict for fast lookup in the optimizer.
    {
      'term_classifications': {'search term': 'value', ...},
      'keyword_overrides': {'keyword': 'value', ...},
      'campaign_rules': {'rule_name': 'value', ...},
    }
    """
    entries = get_optimizer_memory(active_only=True)
    result = {
        'term_classifications': {},
        'keyword_overrides': {},
        'campaign_rules': {},
        'general': {},
    }
    for e in entries:
        key = e['key'].lower()
        val = e['value']
        if e['category'] == 'term_classification':
            result['term_classifications'][key] = val
        elif e['category'] == 'keyword_override':
            result['keyword_overrides'][key] = val
        elif e['category'] == 'campaign_rule':
            result['campaign_rules'][key] = val
        elif e['category'] == 'general':
            result['general'][key] = val
    return result


def add_optimizer_memory(category: str, key: str, value: str, reason: str, author: str = 'admin') -> dict:
    """Add a new memory entry. Returns the created record."""
    now = _now()
    key_lower = key.strip().lower()
    with _conn() as conn:
        # Deactivate any existing entry with same category+key
        conn.execute(
            "UPDATE optimizer_memory SET active=0, updated_at=? WHERE category=? AND key=? AND active=1",
            (now, category, key_lower)
        )
        cursor = conn.execute(
            "INSERT INTO optimizer_memory (category, key, value, reason, author, active, created_at, updated_at) VALUES (?,?,?,?,?,1,?,?)",
            (category, key_lower, value, reason, author, now, now)
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
