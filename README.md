# Lead Lifecycle Pipeline Dashboard
**Grafton Dental Care — nXtsmile Marketing Pipeline**

Central hub connecting ad clicks → leads → bookings → treatment → revenue attribution.

---

## What It Does

Receives leads from nxtsmile.com, tracks them through the full sales funnel, runs automated email/SMS follow-up sequences, matches booked patients to OpenDental for revenue attribution, and shows everything in a real-time pipeline dashboard.

```
Google Ads → nxtsmile.com → [this service] → email/SMS follow-ups
                                           → appointment scheduler
                                           → OpenDental revenue match
                                           → pipeline dashboard
```

### Lead Stages
```
new → engaged → smile_completed → nurturing → scheduled →
confirmed → showed → treatment_presented → treatment_accepted → treatment_completed
```

### Automated Follow-Up Sequence
| Day | Channel | Template |
|-----|---------|---------|
| 1 | Email | "How did your smile preview look?" |
| 3 | SMS | "Consultation still available" (Twilio) |
| 7 | Email | Objection probe |
| 14 | Email | Financing focus |
| 21 | SMS | Final nudge |
| 30 | Email | Mark cold, delete smile image from GCS |

---

## Quick Start (Mac Mini)

```bash
# 1. Clone the repo
git clone https://github.com/anuraggupta82/lead-lifecycle.git
cd lead-lifecycle

# 2. Double-click the launcher (or from Terminal):
chmod +x "Launch Pipeline.command"
open "Launch Pipeline.command"

# 3. On first run: edit .env when prompted, then re-run
# 4. Dashboard opens at http://localhost:7070
```

**Auto-start on login (Mac Mini):**
```bash
cp com.grafton.pipeline.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.grafton.pipeline.plist
```

---

## Architecture

```
lead-lifecycle/
├── backend/
│   ├── main.py              — FastAPI app (all routes)
│   ├── database.py          — SQLite schema + helpers (leads, events, queue)
│   ├── config.py            — Settings (reads .env)
│   ├── follow_up_engine.py  — APScheduler (runs every 15 min)
│   ├── email_service.py     — Gmail SMTP email templates
│   ├── sms_service.py       — Twilio SMS (Day 3 + Day 21)
│   ├── firestore_sync.py    — Pull existing leads from nxtsmile Firestore
│   ├── od_matcher.py        — Match leads to OpenDental patients (nightly)
│   ├── requirements.txt
│   ├── .env.example         — Copy to .env and fill in credentials
│   └── .gitignore
├── frontend/
│   └── index.html           — React pipeline dashboard (single file)
├── Launch Pipeline.command  — Mac double-click launcher
├── com.grafton.pipeline.plist — launchd auto-start plist
└── README.md
```

---

## API Reference

### Public
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Pipeline dashboard (React SPA) |
| `GET` | `/health` | Health check |
| `POST` | `/api/events` | Receive lifecycle events (landing page, scheduler, Mango) |
| `GET` | `/unsubscribe/{lead_id}/{channel}` | One-click unsubscribe (from email links) |

### Admin (requires `X-Admin-Password` header)
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/pipeline` | All leads with stage + last event |
| `GET` | `/api/lead/{id}` | Full lead detail + event timeline + follow-up queue |
| `GET` | `/api/admin/stats` | Funnel stats + revenue |
| `GET` | `/api/admin/queue` | Pending follow-ups due now |
| `POST` | `/api/admin/sync` | Pull new leads from Firestore |
| `POST` | `/api/admin/match` | Match leads to OpenDental patients (office LAN only) |
| `POST` | `/api/admin/run-queue` | Manually trigger follow-up engine |
| `PUT` | `/api/admin/lead/{id}/stage` | Manually advance lead stage |

### Event Types (POST /api/events)
```json
{"event_type": "lead_created", "lead_id": "...", "source": "smile_tool",
 "first_name": "Jason", "email": "...", "phone": "...", "gclid": "...",
 "utm_campaign": "nXtsmile-all-on-x", "created_at": "2026-04-01T10:00:00Z"}

{"event_type": "smile_completed", "lead_id": "...", "smile_image_url": "..."}

{"event_type": "booking_confirmed", "lead_id": "...", "booking_id": "...",
 "source": "scheduler"}

{"event_type": "call_matched", "lead_id": "...", "source": "mango"}

{"event_type": "stage_update", "lead_id": "...",
 "detail": {"stage": "treatment_accepted"}}
```

---

## Wiring Guide

### 1. nxtsmile.com backend → this service
Add to `server.py` after saving a lead to Firestore:
```python
import httpx
PIPELINE_URL = "http://MAC_MINI_IP:7070"

# After lead saved:
httpx.post(f"{PIPELINE_URL}/api/events", json={
    "event_type": "lead_created",
    "lead_id": lead_id,
    "source": form_type,     # "smile_tool" / "contact_form" / "pearly"
    "first_name": first_name,
    "email": email,
    "phone": phone,
    "gclid": tracking_data.get("gclid",""),
    "utm_campaign": tracking_data.get("utm_campaign",""),
    "created_at": timestamp,
}, timeout=3)
```

### 2. Appointment scheduler → this service
Add to booking confirmation webhook in the scheduler backend:
```python
httpx.post(f"{PIPELINE_URL}/api/events", json={
    "event_type": "booking_confirmed",
    "email": patient_email,      # used for lead lookup
    "source": "scheduler",
    "booking_id": booking_id,
    "detail": {"appointment_type": apt_type, "date": apt_date},
}, timeout=3)
```

### 3. Update booking URL on nxtsmile.com
Replace `patient.rocks` temp URL with:
`https://scheduler-web-981004615066.us-east4.run.app/book/implant-consult`

---

## Configuration (.env)

Key variables (see `.env.example` for full list):

```bash
ADMIN_PASSWORD=GDC-pipeline-2026!
SMTP_PASSWORD=ssttumljpulosbts          # Gmail App Password
TWILIO_ACCOUNT_SID=ACxxx                # Get from twilio.com
TWILIO_AUTH_TOKEN=xxx
TWILIO_FROM_NUMBER=+15083184477
NXTSMILE_API=https://nxtsmile-api-1096868046685.us-east4.run.app
FIRESTORE_SECRET=grafton2026
OD_DB_HOST=GraftonServer                # Office LAN only
```

---

## PHI & Compliance
- Phone/email stored only in SQLite on Mac Mini (office-controlled hardware)
- OpenDental matching uses SHA-256 hashes — raw PHI never compared directly
- Smile images stored in GCS (encrypted at rest, GCP BAA)
- Signed URLs expire after 1 hour in follow-up emails
- SMS: TCPA compliant — consent captured in nxtsmile.com footer, STOP on every message
- Email: CAN-SPAM compliant — unsubscribe on every follow-up
- Day 30: smile image deleted from GCS automatically

---

## Logs
```bash
# If running via launchd:
tail -f /usr/local/var/log/grafton-pipeline.log

# If running manually:
# Logs print to terminal
```
