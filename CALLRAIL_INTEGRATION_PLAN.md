# CallRail Integration — Plan

**Owner:** Anurag
**Created:** 2026-05-21
**Status:** PLANNING (do not execute until approved)
**Related:** [[project_call_tracking]], [[project_call_analysis]], [[project_attribution_tracking]], [[SMART_PIPELINE_ROUTING_PLAN]]

---

## 0. Quick Facts

- **Plan:** Call Tracking entry tier — $50/mo (10 numbers, 500 minutes)
- **Estimated all-in cost:** ~$70–85/mo (vs Liine $199/mo — ~60% reduction)
- **HIPAA BAA:** Recommended but not strictly required if recording/voicemail/transcription stay disabled on CallRail's side (Mango handles everything locally)
- **Mango stays primary** for recording + transcription; CallRail is for attribution + DNI + webhooks

## 1. Why CallRail

Today, call attribution is patchwork:
- **Liine** is the legacy provider (blocked via WPCode after Practice Cafe migration; see [[project_call_tracking]]).
- **Mango** handles call recording + transcription for AI grading ([[project_call_analysis]]) — but Mango does not provide ad-source attribution at the call level.
- **Google Ads** has a native call conversion (AW-18046211904) but requires the number-swap JS snippet on the site, which hasn't been installed because of the Practice Cafe handoff.

CallRail solves three problems simultaneously:

1. **Dynamic Number Insertion (DNI)** — different ad sources see different phone numbers, so call → ad source attribution becomes deterministic, not statistical.
2. **Call routing & forwarding** — every tracking number forwards to the real office line, with custom whisper messages and business-hours rules.
3. **Webhook-driven call ingestion** — call events push into our pipeline in near-real-time, complete with source attribution, so the pipeline doesn't depend on Mango's nightly sync alone.

The downstream payoff: every campaign's row in the dashboard can show **calls / qualified calls / income from calls** as a first-class metric, not an inferred one.

---

## 2. What CallRail Gives Us

| Capability | How it helps |
|---|---|
| Tracking numbers (local + 800) | One per ad source (Google Ads search, Google Ads call extension, GMB, organic, etc.) |
| Dynamic Number Insertion (DNI) JS | Replaces website number with tracker per visitor's `gclid` / UTM / referrer |
| Call recordings + transcripts | Already done via Mango; CallRail's are redundant but useful as backup |
| Webhooks (`call_started`, `call_completed`, `voicemail`) | Pipeline ingestion in seconds, not nightly |
| Google Ads integration | CallRail can push call conversions directly to GAds (eliminates our number-swap snippet need) |
| Business hours + whisper | "Implant lead — Google Ads" whisper plays for receptionist before connecting |

---

## 3. Account & Plan Setup

### 3.1 CallRail Account Provisioning

Steps (Anurag executes; Claude documents and verifies):
1. Sign up at callrail.com → **Healthcare / Multi-Location Practice** vertical.
2. **Plan choice:** **Call Tracking** entry tier — **$50/mo** for 1 company, 10 local numbers, 500 minutes included; ~$0.03/min over. Reasons:
   - All features we need are in this tier: DNI, recording (if we enable it later), webhooks, API access, Google Ads integration.
   - Form Tracking ($45/mo add-on) NOT needed — we have GA4 + our own webhook for forms.
   - Conversation Intelligence ($45/mo add-on) NOT needed — Mango + our Gemini classifier do transcription/grading locally.
   - 10 numbers covers initial campaigns + a buffer; extra numbers ~$3 each.
3. Add company: **Grafton Dental Care**. Time zone: America/New_York. Country: US.
4. **HIPAA BAA — recommended but not strictly required if recording/transcription/voicemail all stay disabled.** Phone-number-as-PHI is legally ambiguous for healthcare practices, and CallRail offers the BAA free. Sign it as a safety measure — removes ambiguity, lets you enable voicemail later without re-doing compliance review, protects against breach exposure of your caller list (which is effectively a patient list).
   - **If skipping BAA:** strict rule — no recording, no voicemail-to-email, no transcription, ever. Any voicemail must route to an in-office mailbox, not CallRail's storage. Document this in `.env` as a guardrail.
5. Set destination number: real office line `508-839-5566` (or whatever the primary forwarding target is — confirm at execution time).
6. Generate **API v3 key**. Store in `_CREDENTIALS_VAULT/callrail-api.json` with the company ID and account ID.

### 3.2 Webhook Configuration (CallRail → Our Backend)

CallRail can POST to a webhook on call lifecycle events. The webhook target:

- **Production:** `https://<public-tunnel>/api/callrail/webhook` (Cloudflare tunnel exposing the local 7070 service — same fix already needed for the GoDaddy → pipeline form webhook).
- **Auth:** CallRail supports HMAC signing; enable it and store the secret in `.env` as `CALLRAIL_WEBHOOK_SECRET`.
- **Events to subscribe:** `call_completed`, `call_modified`, `sms_received` (if SMS-from-tracker is enabled later), `voicemail`.

If the Cloudflare tunnel isn't up yet, fall back to **CallRail polling** — a backend cron pulls calls every 5 min via `GET /v3/a/{account_id}/calls.json`. Webhook is preferred; polling is the working fallback.

### 3.3 HIPAA Posture (Two Paths)

**Path A — Sign BAA (recommended):**
- [ ] Request CallRail BAA at signup (free).
- [ ] Disable CallRail-side transcription (Mango handles this).
- [ ] Optional: enable call recording with **recording disclosure prompt** (state requirement) — Mango is primary, CallRail recording is backup only.
- [ ] Limit CallRail user accounts to Anurag + 1 admin until access policy is decided.
- [ ] Tracking number outbound caller ID = office line, not personal cells.

**Path B — Skip BAA (lean tier, strict guardrails):**
- [ ] Disable call recording on every CallRail number (default OFF when no BAA).
- [ ] Disable voicemail-to-email and voicemail storage on CallRail; route voicemails to office IVR/mailbox instead.
- [ ] Disable transcription (Conversation Intelligence is not on this plan anyway).
- [ ] Document the rule in `backend/.env`: `CALLRAIL_NO_BAA_MODE=true` — code checks this and refuses to enable recording via API.
- [ ] If you later want voicemail or recording on CallRail, sign BAA first.

**Why Path A is still recommended despite Path B being valid:** phone-number-attached-to-dental-practice is on the line between "metadata" and "PHI." BAA removes the ambiguity and gives you a clean compliance posture. Free + free = sign it.

---

## 4. Dashboard UI — Number Management

This is the operator-facing piece. Numbers are a configuration artifact, not a transaction, so they live under **Campaign Settings → Tracking Numbers** rather than in their own top-level nav.

### 4.1 New Page — `/admin/tracking-numbers`

Top-level table view with columns:

| # | Column | Source |
|---|---|---|
| 1 | Tracking Number | CallRail |
| 2 | Friendly Name | User-entered or auto-generated |
| 3 | Assigned To | Campaign / Ad Source / "Pool" |
| 4 | Forward To | Office line (editable) |
| 5 | Calls (30d) | Local pull from `callrail_calls` table |
| 6 | Status | Active / Paused |
| 7 | Actions | Edit / Reassign / Pause |

Two primary actions on this page:
- **+ Import from CallRail** — pulls latest list of numbers from CallRail API, populates the table, marks new ones unassigned.
- **+ Create New Number** — calls CallRail API to provision a new number (local area code, prefix selectable), saves to DB, prompts for friendly name + assignment.

### 4.2 Number Assignment Modal

When operator clicks "Assign" on an unassigned number:

```
┌─────────────────────────────────────────┐
│ Assign Tracking Number                  │
│ (774) 555-0123                          │
├─────────────────────────────────────────┤
│ Assign To:                              │
│   ◯ Google Ads Campaign  [dropdown ▼]   │
│   ◯ Google Ads Call Extension [dropdown]│
│   ◯ Static Source (GMB, organic, etc.)  │
│   ◯ Number Pool (DNI rotation)          │
│                                         │
│ Forwarding:                             │
│   Forward to: [508-839-5566        ▼]   │
│   Whisper: ☑ Play "Lead from {source}"  │
│   Business hours only: ☑                │
│   After-hours: ◯ Voicemail ◯ Send Text  │
│                                         │
│ [Cancel]                  [Save & Push] │
└─────────────────────────────────────────┘
```

"Save & Push" performs two writes:
1. Update local DB (`callrail_numbers` table).
2. Push to CallRail API (sets destination, whisper, business hours).
3. **If assigned to a Google Ads campaign or call extension:** push to Google Ads via the existing `google_ads_client` to populate the campaign's call extension or — if assigned to a specific call extension — update that extension's phone number.

### 4.3 Bulk Operations

- **Import** — bulk pull from CallRail.
- **Reconcile** — compare DB ↔ CallRail; flag drift (e.g., a number deleted in CallRail but still active locally).
- **Export CSV** — number, assignment, forwarding, monthly cost — for audit.

---

## 5. Database Schema

```sql
-- One row per CallRail tracking number we control
CREATE TABLE callrail_numbers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  callrail_id TEXT UNIQUE NOT NULL,          -- CallRail's internal ID
  phone_number TEXT NOT NULL,                -- E.164 format: +15085551234
  friendly_name TEXT,                        -- "Implant Search - Google Ads"
  assignment_type TEXT,                      -- 'gads_campaign' | 'gads_call_extension'
                                             -- | 'static_source' | 'pool' | 'unassigned'
  assigned_campaign_id INTEGER,              -- FK to campaigns.id (if gads_*)
  assigned_call_extension_id TEXT,           -- GAds call extension resource name
  static_source_label TEXT,                  -- 'GMB', 'organic', 'direct_mail_jan2026'
  forward_to TEXT,                           -- E.164
  whisper_message TEXT,                      -- 'Lead from Google Ads - Implant Search'
  business_hours_only BOOLEAN DEFAULT 1,
  after_hours_behavior TEXT,                 -- 'voicemail' | 'sms' | 'callback'
  status TEXT DEFAULT 'active',              -- 'active' | 'paused' | 'released'
  monthly_cost_cents INTEGER,                -- pulled from CallRail; for cost reports
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  last_synced_at TIMESTAMP
);

-- One row per call event from CallRail
CREATE TABLE callrail_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  callrail_call_id TEXT UNIQUE NOT NULL,
  tracking_number_id INTEGER,                -- FK to callrail_numbers.id
  caller_number TEXT,                        -- E.164
  caller_name TEXT,                          -- CallRail caller-ID lookup
  caller_city TEXT,
  caller_state TEXT,
  called_at TIMESTAMP,
  duration_seconds INTEGER,
  direction TEXT,                            -- 'inbound' | 'outbound'
  status TEXT,                               -- 'answered' | 'missed' | 'voicemail' | 'busy'
  first_call BOOLEAN,                        -- CallRail's repeat-caller flag
  source TEXT,                               -- 'google_ads' | 'organic' | etc.
  campaign TEXT,                             -- gads campaign name passed through DNI
  keyword TEXT,
  gclid TEXT,
  landing_page TEXT,
  recording_url TEXT,                        -- CallRail-hosted recording (BAA-covered)
  -- Linking
  mango_call_id TEXT,                        -- best-guess match to mango_calls (phone + time)
  lead_id INTEGER,                           -- FK to leads.id if matched
  od_patient_num INTEGER,                    -- if existing patient
  -- Bookkeeping
  ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  raw_payload JSON                           -- full webhook body for debugging
);

CREATE INDEX idx_callrail_calls_called_at ON callrail_calls(called_at);
CREATE INDEX idx_callrail_calls_caller_number ON callrail_calls(caller_number);
CREATE INDEX idx_callrail_calls_tracking_number ON callrail_calls(tracking_number_id);
```

---

## 6. Google Ads Auto-Placement Logic

This is the "dashboard places the number in ads at the correct place" piece.

### 6.1 Number → Ad Placement Mapping

| Assignment Type | What we push to Google Ads |
|---|---|
| `gads_campaign` | Create or update a **Call Extension** on the campaign, phone = tracking number. Country = US. |
| `gads_call_extension` | Update the specific call extension resource to use this number. |
| `static_source` | No GAds push. Number is for non-paid sources (GMB, organic, print). |
| `pool` | No direct GAds push. DNI JS on the website serves these numbers based on visitor source. |

### 6.2 Implementation in `backend/app/services/google_ads_extensions.py`

New module exposes:

```python
def upsert_call_extension(campaign_id: int, phone_e164: str) -> str:
    """Idempotent: if campaign has a call extension, update it; else create."""

def remove_call_extension(campaign_id: int) -> None:
    """Detach the call extension from the campaign (used on unassign/pause)."""

def list_call_extensions(campaign_id: int) -> list[dict]:
    """Read-back for verification."""
```

Uses existing `google_ads_client` infrastructure. **All writes must verify via read-back** — same pattern as ad group tier verification ([[project_ad_group_intelligence]]).

### 6.3 DNI for Website Pool

Separate from call extensions: the website itself needs a JS snippet from CallRail that:
1. Reads `gclid` / UTM params / `document.referrer`.
2. Replaces all instances of the office number on the page with a pool tracking number.
3. CallRail's attribution then ties subsequent calls to the source.

**This snippet goes into WPCode** as a new snippet (separate from the existing `snippet-2-gform-pipeline-webhook.php`). Anurag installs after Practice Cafe migration is fully clean ([[project_graftondentalcare_migration]]).

---

## 7. Webhook → Pipeline Ingestion

### 7.1 Webhook Handler

```python
# backend/app/api/callrail_webhook.py
@router.post("/api/callrail/webhook")
async def callrail_webhook(request: Request):
    # 1. HMAC verify (header 'signature')
    # 2. Parse payload — call_completed | voicemail | etc.
    # 3. Insert into callrail_calls
    # 4. Match to mango_calls (phone + time within ±2 min)
    # 5. Match to leads (phone + 30 days) or create new lead
    # 6. Apply Smart Pipeline Routing (see SMART_PIPELINE_ROUTING_PLAN)
    # 7. Return 200 quickly; defer heavy work to background task
```

### 7.2 Cross-Linking with Mango Calls

CallRail captures **before** the call connects (so we get all calls including no-answer/voicemail/busy). Mango captures **at the office line** (so it only sees answered calls). The two streams overlap for answered calls.

- **Match key:** `caller_number` + `called_at` ±2 minutes.
- **CallRail-only calls** (missed/voicemail): pure CallRail row, flagged `mango_call_id=NULL`.
- **Mango-only calls** (calls that didn't come through a tracked number): pure Mango, no CallRail row.
- **Linked calls:** both rows, linked via `mango_call_id` foreign key.

### 7.3 Smart Pipeline Routing Hook

CallRail webhook ingestion feeds the same routing model as [[SMART_PIPELINE_ROUTING_PLAN]]:
- Bucket 1 (self-serve): not yet possible from a call alone — calls always trigger routing eval.
- Bucket 2 (warm lead): default for unanswered calls + Gemini-classified `follow_up_needed='yes'`.
- Bucket 3 (informational): Gemini-classified `follow_up_needed='no'` (after Mango transcription completes; CallRail row updated retroactively).

**Sequence quirk:** CallRail webhook fires within seconds of call end. Mango transcription takes 1–5 min. Initial routing decision uses campaign rule; Gemini classifier re-evaluates after transcription arrives and updates routing if needed. The `pipeline_entry_audit` log captures both decisions.

---

## 8. The 6-PR Sequence

### PR 1 — CallRail API Client + Number Sync (Read-Only)
- New module: `backend/app/services/callrail_client.py` (uses CallRail API v3)
- New cron: pull list of numbers nightly, populate `callrail_numbers` table
- `.env` additions: `CALLRAIL_API_KEY`, `CALLRAIL_ACCOUNT_ID`, `CALLRAIL_COMPANY_ID`
- Schema: `callrail_numbers` table only (calls table comes in PR 4)
- **No UI yet.** Just verify we can read numbers from CallRail.

**Acceptance:** cron run shows all CallRail numbers in DB; manual update in CallRail reflected within 24h.

### PR 2 — Number Management UI (Frontend-Heavy)
- New page: `/admin/tracking-numbers`
- Table view + filter/sort
- Assignment modal (see §4.2)
- **No GAds push yet** (PR 3 does that)
- "Save" updates DB and pushes destination/whisper to CallRail
- Reconcile button to diff DB ↔ CallRail

**Acceptance:** operator can create, name, forward, and pause numbers entirely from dashboard.

### PR 3 — Google Ads Auto-Placement
- New module: `google_ads_extensions.py` with upsert/remove/list
- Hook into PR 2's Save: if assignment is `gads_campaign` or `gads_call_extension`, push to GAds
- Read-back verify before marking the assignment successful
- Failure UX: clear error toast with retry button; assignment stays in `pending_gads_push` state
- **Opus review** post-Sonnet for ad-side write paths

**Acceptance:** assigning a number to "Implant Search" campaign creates a call extension on that campaign within 60s; verified via Google Ads UI.

### PR 4 — Webhook Ingestion + Call Storage
- Webhook endpoint with HMAC verify
- `callrail_calls` schema
- Background task for cross-linking to Mango
- Polling fallback cron (every 5 min) for first 30 days as belt-and-suspenders
- Test webhook with CallRail's "Send Test Event" button
- **Depends on Cloudflare tunnel being up** (separate infra; document as blocker)

**Acceptance:** within 30s of a real call ending, `callrail_calls` has the row.

### PR 5 — Mango ↔ CallRail Linking + Lead Creation
- Match logic: phone + time within ±2 min (similar to [[project_call_flags]] time-only matcher)
- New CallRail-attributed call → match to existing lead or create new
- Existing-patient guard (from Smart Pipeline Routing PR 5)
- `attributed_ad_group` populated from CallRail's `keyword` field where present
- Confidence tier scoring: `high` if gclid+keyword present, `low` if only campaign present

**Acceptance:** answered call from tracking number creates correctly-attributed lead + links Mango call.

### PR 6 — DNI Website Snippet + Number Pool
- WPCode snippet (CallRail-provided) installed on graftondentalcare.com
- Pool of 5 tracking numbers reserved in CallRail for DNI rotation
- Validation: hover-test on landing pages confirms swap
- Cost reporting: monthly minutes + per-number cost shown in dashboard
- Cleanup: archive old Liine number tracking, eliminate the legacy AW-360307486 stray

**Acceptance:** visitor with `?gclid=test` sees a different number than visitor with `?utm_source=email`.

---

## 9. Forwarding & Routing Rules

Operator-facing rules per number (configurable in §4.2 modal):

- **Default forwarding target:** office line (configurable in env: `CALLRAIL_DEFAULT_FORWARD`).
- **Whisper message:** auto-generated per assignment, editable. Format: `Lead from {source} — {campaign_or_label}`.
- **Business hours:** Mon–Thu 8a–5p, Fri 8a–2p (configurable). Override per number.
- **After-hours behavior:**
  - **Voicemail** (default) — CallRail records, transcribes (or not, under BAA), forwards transcript to ops email.
  - **SMS callback** — sends caller an SMS: "Sorry we missed you — we'll call back tomorrow at 8am. Reply BOOK to schedule online: https://visitgdc.com" — uses our [[project_unsubscribe_service]] for compliance.
  - **Live callback queue** — adds to a follow-up list shown in pipeline as Auto-Nurture.

After-hours behavior is configurable per number, so an emergency campaign can route to a different on-call mechanism than a routine cleaning campaign.

---

## 10. Risk & Failure Modes

| Risk | Mitigation |
|---|---|
| CallRail API outage breaks number-management UI | UI shows last-known DB state; writes queue with retry; pending writes badge |
| Webhook delivery failure | 5-min polling fallback runs in parallel for 30 days; alert if drift |
| Cloudflare tunnel down | Polling fallback covers; tunnel restart documented in runbook |
| HMAC secret leak | Rotate key in CallRail; update `.env`; redeploy |
| Number assignment race (two operators editing same number) | Soft lock + last-write-wins with audit log warning |
| GAds call extension write fails after CallRail save | Assignment marked `pending_gads_push`; Retry button + alert |
| Cost overrun (over 500 min/month) | Cost report in dashboard; alert at 80% of plan |
| Caller-ID spoofing (fake calls) | CallRail filters most spam; flag short-duration calls as low-confidence for AI optimizer |

---

## 11. What This Does NOT Replace

- **Mango** continues to handle in-office call grading/transcription. CallRail's transcripts are backup.
- **Google Ads call conversion (AW-18046211904)** stays — CallRail can push conversions directly, which removes our number-swap-JS dependency. Verify with Tag Assistant after PR 3.
- **OD patient sync** unchanged. Existing-patient detection is still phone+name match against OD.
- **Liine** stays disabled. Already blocked via WPCode.

---

## 12. Costs (Estimated)

| Item | Monthly |
|---|---|
| Call Tracking entry tier (1 company, 10 numbers, 500 min) | $50 |
| Extra numbers (5–10 more for DNI pool, ~$3 each) | +$15–30 |
| Overage minutes (est. 200 over @ ~$0.03/min) | +$6 |
| **Estimated total** | **~$70–85/mo** |

Compared to Liine ($199/mo last quoted) this is roughly a 60% reduction with more capabilities. Plan upgrade path: if Conversation Intelligence or Form Tracking is ever wanted, add-ons can be enabled later without changing plan tier.

---

## 13. Cross-Plan Dependencies

- **Cloudflare tunnel** must be up before PR 4 webhook is reliable. Polling fallback covers in the interim.
- **Smart Pipeline Routing PR 1 (Gemini classifier)** must be live before CallRail-ingested calls can be routed to Bucket 2 vs Bucket 3 cleanly. PR 4 can ship before classifier — calls just fall back to campaign rule until classifier is online.
- **HIPAA BAA** — sign at account creation (Path A) OR enforce strict no-recording/no-voicemail mode (Path B). Either path is workable; BAA is recommended. Required only if recording or voicemail-to-email is enabled.

---

## 14. Open Questions for Operator

1. **Forward target —** is `508-839-5566` correct, or is there a separate "new patient" line?
2. **Receptionist whisper —** is whisper desirable for receptionist, or distracting? Default ON unless told otherwise.
3. **After-hours default —** voicemail or SMS callback? SMS feels better but requires Twilio A2P clearance ([[project_twilio_a2p_compliance]]) for outbound from a non-personal number.
4. **DNI pool size —** start with 5? 10?
5. **Multi-location —** is Doctor Dental Care getting CallRail too, or just GDC for now?

---

## 15. Next Action

Awaiting operator decision on:
1. Approve this plan?
2. Sign up for CallRail and sign BAA (Anurag).
3. Once API key is in `_CREDENTIALS_VAULT`, start PR 1 (read-only sync).
