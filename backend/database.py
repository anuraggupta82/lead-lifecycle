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
    smile_image_url TEXT DEFAULT '',       -- GCS signed URL to smile image
    smile_generated_at TEXT DEFAULT '',
    unsubscribed_email INTEGER DEFAULT 0,
    unsubscribed_sms   INTEGER DEFAULT 0,
    od_patient_num     TEXT DEFAULT '',    -- matched OpenDental PatNum
    od_matched_at      TEXT DEFAULT '',
    attributed_production REAL DEFAULT 0.0,
    booking_id  TEXT DEFAULT '',           -- scheduler booking ID when booked
    notes       TEXT DEFAULT '',
    -- Google Ads resolved fields (populated by google_ads_sync.py)
    keyword_text    TEXT DEFAULT '',       -- matched keyword from Google Ads
    search_term     TEXT DEFAULT '',       -- actual search query the user typed
    ad_group_name   TEXT DEFAULT '',       -- ad group name
    ad_id           TEXT DEFAULT '',       -- ad creative ID
    click_cost      REAL DEFAULT 0.0,     -- cost per click in dollars
    gads_synced_at  TEXT DEFAULT '',       -- when gclid was last resolved
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
"""

LIFECYCLE_STAGES = [
    "new",
    "engaged",
    "smile_completed",
    "nurturing",
    "scheduled",
    "confirmed",
    "showed",
    "treatment_presented",
    "treatment_accepted",
    "treatment_completed",
    "cold",
]

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
        ("keyword_text",   "TEXT DEFAULT ''"),
        ("search_term",    "TEXT DEFAULT ''"),
        ("ad_group_name",  "TEXT DEFAULT ''"),
        ("ad_id",          "TEXT DEFAULT ''"),
        ("click_cost",     "REAL DEFAULT 0.0"),
        ("gads_synced_at", "TEXT DEFAULT ''"),
    ]

    for col_name, col_type in new_columns:
        if col_name not in existing_cols:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {col_name} {col_type}")

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
                        "utm_term","smile_image_url","smile_generated_at","source","notes",
                        "booking_id","od_patient_num","attributed_production",
                        "keyword_text","search_term","ad_group_name","ad_id",
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
                    utm_campaign, utm_term, smile_image_url, notes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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


def update_stage(lead_id: str, new_stage: str, source: str = "system", detail: str = "") -> dict:
    """Advance a lead's lifecycle stage (never goes backwards)."""
    lead = get_lead(lead_id)
    if not lead:
        raise ValueError(f"Lead {lead_id} not found")

    old_stage = lead["stage"]
    if STAGE_ORDER.get(new_stage, 0) <= STAGE_ORDER.get(old_stage, 0) and new_stage != "cold":
        return lead  # Don't go backwards

    now = _now()
    with _conn() as conn:
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
                   l.smile_image_url, l.stage, l.unsubscribed_email, l.unsubscribed_sms,
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
        total = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        revenue = conn.execute("SELECT SUM(attributed_production) FROM leads").fetchone()[0] or 0.0
        pending_followups = conn.execute("SELECT COUNT(*) FROM follow_up_queue WHERE status='pending'").fetchone()[0]
        sent_today = conn.execute("""
            SELECT COUNT(*) FROM follow_up_queue
            WHERE status='sent' AND sent_at >= date('now')
        """).fetchone()[0]

        return {
            "total_leads": total,
            "by_stage": counts,
            "attributed_revenue": round(revenue, 2),
            "pending_follow_ups": pending_followups,
            "sent_today": sent_today,
        }
