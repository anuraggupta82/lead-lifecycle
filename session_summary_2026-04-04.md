# Lead Lifecycle / nXtsmile Session Summary
**Date:** April 4, 2026

## Primary Goal
Build a fully automated, AI-driven marketing engine for Grafton Dental Care that tracks every dollar spent on advertising to every dollar produced in dental production.

## What Was Completed

### SMS Templates & Twilio Setup
- Built Day 3 and Day 21 SMS templates in `backend/sms_service.py`
- Configured Twilio credentials in `.env` (SID: AC92fe..., From: +15083181222)
- A2P 10DLC registration submitted — awaiting campaign approval (1-3 business days)
- Error 30034 on test send is expected until A2P approved

### Privacy & Legal Pages (nxtsmile.com)
- Created `privacy.html` — full Privacy Policy + HIPAA Notice of Privacy Practices
- Created `terms.html` — Terms & Conditions (SMS program, AI disclaimers, MA law)
- Updated `index.html` footer with links and SMS consent disclosure at form
- Deployed to Cloudflare

### Delete Image Endpoint
- Added `/delete-image/{lead_id}` GET endpoint in `main.py`
- Deletes GCS image, clears DB field, logs event, shows confirmation HTML

### OpenDental MySQL Connection
- VPN connection to GraftonServer (192.168.1.157) working
- Fixed `od_matcher.py` proctp query — this OD version uses `ProcCode` varchar directly, not `CodeNum` FK
- OD matcher running successfully (2 matched patients)

### Mac Mini Deployment
- Created `Install Dependencies.command` (double-clickable install script)
- Pipeline running on Mac Mini

### Implementation Plan (docx)
- Added Section 7: AI Marketing Intelligence (design principles, 5 modular agents, recommendation queue, 4 implementation phases)

### Hosting Notes
- nxtsmile.com frontend: Cloudflare (static)
- nxtsmile backend API: GCP Cloud Run
- lead-lifecycle backend: local / office server (Mac Mini)

## Root Cause: Google Ads Data Not Populating

**The #1 blocker** for the entire attribution chain:

`/mnt/Projects/Applications/nxtsmile-landing-page-v1/backend/server.py` line 532:
- `LeadData` Pydantic model only has: `name, phone, email, concern, goals, preferred_time, source`
- **Missing:** gclid, fbclid, msclkid, utm_source, utm_medium, utm_campaign, utm_term, utm_content, landing_url
- Frontend sends tracking data via `getTrackingData()` but Pydantic silently drops unknown fields
- Result: gclid never reaches Firestore → pipeline's Google Ads sync finds nothing to match (all 6 leads show `no_gclid: 6`)

## Planned Changes (Awaiting Approval)

### A) Fix nxtsmile Backend LeadData Model (BLOCKER)
- Add gclid, fbclid, msclkid, all UTM fields, landing_url to `LeadData` in `server.py`
- Redeploy to GCP Cloud Run

### B) Expand leads Database Schema
Add columns to `leads` table in `database.py`:
- `utm_content`, `landing_url`, `device_type`
- Stage timestamps: `engaged_at`, `appointment_set_at`, `in_treatment_at`, `completed_at`, `lost_at`, `cold_at`
- Financial: `attributed_income`, `treatment_plan_value`
- Call tracking: `call_source`, `call_duration`, `call_recording_url`
- Staff workflow: `assigned_to`, `last_contacted_at`, `next_followup_at`, `notes`

### C) Auto-populate Stage Timestamps
- Modify `update_stage()` to set corresponding `{stage}_at` column automatically

### D) Expand OD Production Queries
- Pull income (collections) in addition to production (billed amounts)

## Other Pending Tasks
- Wait for Twilio A2P approval, then test SMS end-to-end
- Mango call integration (match calls to leads by phone) — Week 2
- CSV upload for external agency ROI comparison — Week 2
- Dashboard improvements (user noted "many things needed to be changed")
- Campaign-aware template system for future campaigns (Invisalign, etc.)

## Key Files Reference
| File | Location |
|------|----------|
| SMS templates | `/mnt/lead-lifecycle/backend/sms_service.py` |
| Pipeline API | `/mnt/lead-lifecycle/backend/main.py` |
| Database schema | `/mnt/lead-lifecycle/backend/database.py` |
| OD matcher | `/mnt/lead-lifecycle/backend/od_matcher.py` |
| Google Ads sync | `/mnt/lead-lifecycle/backend/google_ads_sync.py` |
| Firestore sync | `/mnt/lead-lifecycle/backend/firestore_sync.py` |
| nxtsmile backend | `/mnt/Projects/Applications/nxtsmile-landing-page-v1/backend/server.py` |
| nxtsmile frontend | `/mnt/Projects/Applications/nxtsmile-landing-page-v1/index.html` |
| Privacy policy | `/mnt/Projects/Applications/nxtsmile-landing-page-v1/privacy.html` |
| Terms page | `/mnt/Projects/Applications/nxtsmile-landing-page-v1/terms.html` |
| Install script | `/mnt/lead-lifecycle/Install Dependencies.command` |
| Implementation plan | `/mnt/Marketing/GDC_Marketing_Implementation_Plan.docx` |
| Twilio creds | `/mnt/lead-lifecycle/backend/.env` |
