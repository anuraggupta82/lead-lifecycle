# Lead Lifecycle Pipeline — Grafton Dental Care
**Last Updated:** April 4, 2026 | **Status:** Running on dev Mac, ready for Mac Mini deploy

Central hub connecting ad clicks → leads → bookings → treatment → revenue attribution.

---

## The Ultimate Goal

Grafton Dental Care runs Google Ads campaigns targeting patients who need All-on-X dental implants ($15k–$45k cases). The ultimate goal of this entire system is a **closed-loop ROI engine**:

```
Google Ads click (gclid)
  → nxtsmile.com captures lead + gclid
    → this service resolves gclid → keyword / campaign / CPC
      → automated email + SMS nurture sequence
        → patient books appointment (online scheduler)
          → appointment confirmed in OpenDental
            → patient shows, treatment presented
              → treatment accepted + revenue recorded in OD
                → revenue attributed back to the keyword that generated the lead
                  → conversion uploaded to Google Ads
                    → AI optimizer adjusts bids (pause losers, boost winners)
                      → Google sends BETTER clicks at lower cost
                        → ROAS compounds over time
```

**Today:** $50/day ad budget, ~$1.34 CPC, 4 leads in pipeline, 0 revenue attributed yet.

**Target:** Track every dollar from ad spend to chair-side production. Know exactly which keyword, ad, and campaign generates All-on-X patients. Prove ROAS > 10x. Then scale spend confidently.

### Why This Matters
- All-on-X cases are $15k–$45k. One attributed patient = weeks of ad spend justified.
- Without attribution, you're flying blind — can't know if $50/day is worth it or if budget should shift.
- With attribution: you see "all on 4 dental implants [exact]" → 3 leads → 1 scheduled → $28k produced → 44x ROAS → raise bid. "restorative dentistry [broad]" → $94 spent → 0 leads → pause it.
- The Google Ads bidding algorithm learns which clicks produce revenue when you upload offline conversions — it will automatically find more patients like your best ones.

---

## What It Does (Today)

- **Receives leads** from nxtsmile.com (Firestore sync every 15 min; real-time events coming)
- **Tracks leads** through full sales funnel across 11 stages
- **Runs automated follow-up** — 6-touch email + SMS sequence over 30 days
- **Resolves Google Ads data** — matches gclid to keyword, ad group, campaign, CPC (daily 6 AM)
- **Matches patients to OpenDental** — revenue attribution via phone/email hash (nightly 10 PM)
- **Uploads offline conversions** to Google Ads API — teaches bidding algorithm what converts (nightly 11 PM)
- **AI optimizer** — pauses wasted spend, boosts proven keywords (daily 7 AM)
- **Pipeline dashboard** — real-time Kanban-style view of all leads by stage, with Google Ads campaign reporting

---

## Architecture Diagram

```
SOURCES
  Google Ads ($50/day, grafton_nxtsmile_* campaigns)
    → nxtsmile.com landing page (Cloudflare Pages)
      → Firestore (GCP) ─────────────────────────────┐
  Smile tool widget                                   │ 15-min sync
  Pearly chatbot                                      │
  Contact form                                        ↓
                                           THIS SERVICE (Mac Mini, port 7070)
                                           ┌─────────────────────────────────┐
                                           │ SQLite: leads, events,           │
                                           │  follow_up_queue, conversion_    │
                                           │  uploads, lead_notes, od_matches │
                                           │                                  │
                                           │ Stages: new → engaged →          │
                                           │  smile_completed → nurturing →   │
                                           │  scheduled → confirmed → showed  │
                                           │  → tx_presented → tx_accepted →  │
                                           │  tx_completed → [cold]           │
                                           └─────────────────────────────────┘
                                                    ↓               ↓
                                          NURTURE SEQUENCE    INTEGRATIONS
                                          Day 1: email        OpenDental (office LAN)
                                          Day 3: SMS           → revenue attribution
                                          Day 7: email         → treatment stage sync
                                          Day 14: email       Google Ads API
                                          Day 21: SMS          → GCLID resolver
                                          Day 30: cold +       → conversion uploader
                                                  delete img   → AI optimizer
                                                              Appointment Scheduler
                                                               → booking events
```

---

## Dashboard Features (localhost:7070)

### Pipeline Tab
- **Flat card layout** — compact inline chips like lab case manager. Each chip shows name, source badge, phone, Google Ads campaign, notes count, time since update.
- **Staff Follow-Up row** (top, yellow) — automatically surfaces leads in early stages that have been around 1+ day and need a human call or email. Staff can see this at a glance.
- **Normal stage rows** — New Lead, Engaged, Smile Done, Nurturing, Scheduled, Confirmed, Showed, Tx Presented, Tx Accepted, Tx Completed
- **Long-Term Nurture row** (bottom, purple) — leads in nurturing for 21+ days (past most automated follow-ups). May need a personal touch or different offer.
- **Drag-and-drop** — drag a chip to any row to move the lead to that stage
- **Filters** — by Google Ads campaign, source, or text search
- **Detail panel** — click any chip to see full lead info, Notes, Timeline, Follow-up schedule. Add notes, move stage.

### Reports & Campaigns Tab
- **Google Ads Campaign Performance** — leads / scheduled / showed / treated / conv% / revenue / ad spend / ROAS per campaign (`grafton_nxtsmile_allonx`, `_implants`, `_dentures`, `_branding`)
- **Keyword Performance** — same metrics broken down by keyword + ad group
- **Lead Funnel** — visual bar chart of stage distribution
- **Lead Sources (Non-Ads)** — organic sources (smile_tool, contact_form, referral, etc.) in a separate table

### Admin Tab
- Action buttons: Sync Firestore, Match OpenDental, Run Follow-ups, Google Ads Sync, Upload Conversions, AI Optimizer (dry run)
- Scheduled jobs overview (6AM / 7AM / 10PM / 11PM)

---

## Lead Stages

```
new           — lead arrived, not yet engaged
engaged       — initial contact / day 1 email sent
smile_completed — completed smile preview tool
nurturing     — in the 30-day automated follow-up sequence
scheduled     — appointment booked via scheduler
confirmed     — appointment confirmed (reminder sent)
showed        — attended consultation (Mango call match or manual)
treatment_presented — treatment plan presented in OD
treatment_accepted  — treatment plan accepted
treatment_completed — procedure done, production recorded
cold          — 30 days elapsed, no progress (day 30 email sent, smile image deleted)
```

**Staff Follow-Up** = leads in new/engaged/smile_completed/nurturing for 1+ days — need human outreach
**Long-Term Nurture** = leads in nurturing for 21+ days — automated sequence mostly done, may need personal touch

---

## Automated Follow-Up Sequence

| Day | Channel | Template | What It Does |
|-----|---------|---------|--------------|
| 1 | Email | `day1_email` | "How did your smile preview look?" — engagement check-in |
| 3 | SMS | `day3_sms` | "Consultation still available" (Twilio / TCPA compliant) |
| 7 | Email | `day7_email` | Objection probe — address cost, candidacy, nervousness |
| 14 | Email | `day14_email` | Financing focus — $0 down, CareCredit, Cherry |
| 21 | SMS | `day21_sms` | Final SMS nudge |
| 30 | Email | `day30_cold` | "Still here if you need us" — marks cold, deletes GCS smile image |

Follow-ups **stop automatically** once a lead reaches `scheduled` or later stages.

---

## Scheduled Jobs (APScheduler)

| Time | Job | File |
|------|-----|------|
| Every 15 min | Follow-up engine | `follow_up_engine.py` |
| Every 15 min | Firestore sync | `firestore_sync.py` |
| 6:00 AM | Google Ads GCLID resolver | `google_ads_sync.py` |
| 7:00 AM | AI campaign optimizer | `ai_optimizer.py` |
| 10:00 PM | OpenDental matcher + treatment stages | `od_matcher.py` |
| 11:00 PM | Offline conversion upload | `google_ads_conversions.py` |

---

## Google Ads Campaigns (Active)

| Campaign Name | Target | Status |
|---|---|---|
| `grafton_nxtsmile_allonx` | "all on 4 implants near me" type searches | Active |
| `grafton_nxtsmile_implants` | General dental implant searches | Active |
| `grafton_nxtsmile_dentures` | Denture alternative searchers | Active |
| `grafton_nxtsmile_branding` | Brand name / nxtsmile searches | Active |

Bidding: Maximize Clicks, $8 max CPC, $50/day. Location: 25-mile radius of Grafton, MA (presence only).

**Keyword types:** 50+ keywords across exact/phrase/broad match. 33+ negatives (DIY, free, clinical trials, OOA).

**First live optimization (Apr 4):** Paused "implant dentistry" broad match ($94 spent, 67 clicks, 0 leads). Added 5 negative keywords. Projected savings: ~$94/week redirected to converting terms.

---

## Quick Start (Mac Mini Deployment)

```bash
# 1. Pull latest code
cd ~/Documents/Projects/Applications/lead-lifecycle
git pull

# 2. Install Google Ads library (enables conversion upload + AI optimizer)
source backend/venv/bin/activate
pip install google-ads

# 3. Double-click launcher (or from Terminal):
open "Launch Pipeline.command"

# 4. Dashboard: http://localhost:7070
```

**Auto-start on login:**
```bash
cp com.grafton.pipeline.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.grafton.pipeline.plist
```

---

## File Structure

```
lead-lifecycle/
├── backend/
│   ├── main.py                  — FastAPI app + all routes
│   ├── database.py              — SQLite schema + CRUD helpers
│   ├── config.py                — Settings (reads .env)
│   ├── follow_up_engine.py      — APScheduler (every 15 min)
│   ├── email_service.py         — Gmail SMTP email templates
│   ├── sms_service.py           — Twilio SMS
│   ├── firestore_sync.py        — Pull leads from nxtsmile Firestore
│   ├── od_matcher.py            — Match leads → OD patients + treatment stages
│   ├── google_ads_sync.py       — GCLID → keyword/ad/campaign/CPC resolver
│   ├── google_ads_conversions.py— Offline conversion uploader
│   ├── ga4_events.py            — GA4 Measurement Protocol events
│   ├── ai_optimizer.py          — AI campaign optimizer
│   ├── .env                     — All credentials (NOT in git)
│   ├── requirements.txt
│   └── .gitignore
├── frontend/
│   └── index.html               — React pipeline dashboard (single file, CDN)
├── Launch Pipeline.command      — Mac double-click launcher
├── com.grafton.pipeline.plist   — launchd auto-start
└── README.md
```

---

## API Reference

### Public
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Pipeline dashboard (React SPA) |
| `GET` | `/health` | Health check |
| `POST` | `/api/events` | Receive lifecycle events |
| `GET` | `/unsubscribe/{lead_id}/{channel}` | One-click unsubscribe |

### Admin (requires `X-Admin-Password: GDC-pipeline-2026!`)
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/pipeline/enriched` | All leads with notes count + campaign filter |
| `GET` | `/api/lead/{id}` | Full lead + timeline + follow-up queue |
| `GET` | `/api/admin/stats` | Funnel stats + revenue |
| `GET` | `/api/admin/campaigns` | Google Ads campaigns + keyword stats |
| `POST` | `/api/admin/sync` | Pull new leads from Firestore |
| `POST` | `/api/admin/match` | Match leads → OpenDental (office LAN only) |
| `POST` | `/api/admin/run-queue` | Manually trigger follow-up engine |
| `POST` | `/api/admin/gads-sync` | Resolve gclids → keywords/campaigns |
| `POST` | `/api/admin/upload-conversions` | Upload offline conversions to Google Ads |
| `POST` | `/api/admin/optimize` | Run AI optimizer (`?dry_run=true` default) |
| `PUT` | `/api/admin/lead/{id}/stage` | Advance stage (forward only) |
| `PUT` | `/api/admin/lead/{id}/force-stage` | Set any stage (allows backward) |
| `GET` | `/api/admin/lead/{id}/notes` | Get lead notes |
| `POST` | `/api/admin/lead/{id}/notes` | Add note to lead |
| `DELETE` | `/api/admin/note/{note_id}` | Delete a note |

### Event Types (POST /api/events)
```json
// New lead from nxtsmile.com
{"event_type": "lead_created", "lead_id": "uuid", "source": "smile_tool",
 "first_name": "Jason", "email": "...", "phone": "...", "gclid": "...",
 "utm_campaign": "grafton_nxtsmile_allonx", "created_at": "2026-04-01T10:00:00Z"}

// Lead completed smile preview
{"event_type": "smile_completed", "lead_id": "...", "smile_image_url": "gs://..."}

// Appointment booked
{"event_type": "booking_confirmed", "lead_id": "...", "booking_id": "...", "source": "scheduler"}

// Appointment cancelled
{"event_type": "booking_cancelled", "lead_id": "..."}

// Mango call matched
{"event_type": "call_matched", "lead_id": "..."}

// Manual stage override
{"event_type": "stage_update", "lead_id": "...", "detail": {"stage": "treatment_accepted"}}
```

---

## Wiring Guide (Remaining Integrations)

### 1. Wire nxtsmile.com → real-time events (vs 15-min Firestore sync)
Add to `server.py` (Cloud Run) after saving lead to Firestore:
```python
import httpx
PIPELINE_URL = "http://MAC_MINI_IP:7070"

httpx.post(f"{PIPELINE_URL}/api/events", json={
    "event_type": "lead_created",
    "lead_id": lead_id,
    "source": form_type,          # "smile_tool" / "contact_form" / "pearly"
    "first_name": first_name,
    "email": email,
    "phone": phone,
    "gclid": tracking.get("gclid", ""),
    "utm_campaign": tracking.get("utm_campaign", ""),
    "created_at": timestamp,
}, timeout=3)
```

### 2. Wire appointment scheduler → booking events
Add to booking confirmation in scheduler backend:
```python
httpx.post(f"{PIPELINE_URL}/api/events", json={
    "event_type": "booking_confirmed",
    "email": patient_email,
    "source": "scheduler",
    "booking_id": booking_id,
}, timeout=3)
```

### 3. Update booking URL on nxtsmile.com
Replace `patient.rocks` link:
```
https://scheduler-web-981004615066.us-east4.run.app/book/implant-consult
```

---

## Configuration (.env)

Key variables:
```bash
# Service
ADMIN_PASSWORD=GDC-pipeline-2026!
DB_PATH=/Users/anurag/grafton_pipeline/pipeline.db

# Email (Gmail App Password)
SMTP_PASSWORD=ssttumljpulosbts

# Twilio SMS
TWILIO_ACCOUNT_SID=ACxxx
TWILIO_AUTH_TOKEN=xxx
TWILIO_FROM_NUMBER=+15083184477

# Firestore
FIRESTORE_SECRET=grafton2026
NXTSMILE_API=https://nxtsmile-api-1096868046685.us-east4.run.app

# OpenDental (office LAN only)
OD_DB_HOST=GraftonServer
OD_API_BASE=http://GraftonServer:30223/api/v1

# Google Ads API
GOOGLE_ADS_CLIENT_ID=...
GOOGLE_ADS_CLIENT_SECRET=...
GOOGLE_ADS_REFRESH_TOKEN=...
GOOGLE_ADS_DEVELOPER_TOKEN=lAW9zit21XyLlLebqJX11w
GOOGLE_ADS_CUSTOMER_ID=2498049505

# GA4
GA4_MEASUREMENT_ID=G-B3G7NKS06D
GA4_API_SECRET=pOjjQP45SVWcDXAh5kZMBg
```

---

## PHI & Compliance

- All lead data stored in SQLite on Mac Mini (office-controlled hardware, not cloud)
- OpenDental matching: SHA-256 hash of phone/email — raw PHI never compared directly
- Smile images: GCS bucket `nxtsmile-smile-images` (encrypted at rest, GCP BAA, 30-day lifecycle delete)
- Signed URLs for email links: 1-hour expiry
- SMS: TCPA compliant — opt-in consent in nxtsmile.com footer, STOP on every message
- Email: CAN-SPAM compliant — unsubscribe link on every follow-up
- Day 30: smile image deleted from GCS automatically
- MA 201 CMR 17.00: encryption + access controls for MA resident PII

---

## Logs

```bash
# If running via launchd:
tail -f /usr/local/var/log/grafton-pipeline.log

# If running manually:
# Logs print to terminal in real-time
```
