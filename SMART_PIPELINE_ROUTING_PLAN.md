# Smart Pipeline Routing — Plan

**Owner:** Anurag
**Created:** 2026-05-21
**Status:** PLANNING (do not execute until approved)
**Related:** [[project_lead_lifecycle_core]], [[project_attribution_tracking]], [[project_call_analysis]]

---

## 1. The Problem

The lead pipeline today ingests every form fill and every scheduler booking. That is wrong. Two observations forced this rethink:

1. A **test contact-form submission** on graftondentalcare.com did **not** reach the pipeline (webhook to `localhost:7070` from GoDaddy is unreachable — see CallRail plan and the open Cloudflare tunnel item). That's a bug.
2. **Candice Chase**, a scheduler-booked patient, **did** show up — but she didn't need follow-up. She self-served. Her presence in the pipeline crowds the surface that should only show **leads that need a human action**.

The pipeline is a worklist, not a CRM. Its value collapses the moment it stops being actionable. So we need a routing layer in front of it.

---

## 2. The Routing Model (Three Orthogonal Decisions)

Every inbound signal (form, call, scheduler booking) goes through three independent gates:

| Gate | Question | Default |
|---|---|---|
| **1. Enter DB?** | Do we record this contact at all? | Yes, almost always — even self-serve bookings get a DB row for attribution + reporting |
| **2. Enter Pipeline?** | Does this lead need follow-up? | Depends on **conversation signal** (Gemini) first, then **campaign rule** as fallback |
| **3. Show in Default View?** | Is this lead in the operator's everyday worklist? | Depends on **campaign visibility setting** (e.g., implants + general = yes; emergency = only when selected) |

Each gate is an independent decision. Skipping gate 2 doesn't skip gate 1. Skipping gate 3 doesn't remove from the pipeline — it just hides it from the default filter.

### The Three-Bucket Lead Model

This routing produces three buckets of inbound contacts:

- **Bucket 1 — Self-Serve (no follow-up needed).**
  Examples: routine cleaning booked via scheduler, patient self-cancels and rebooks, new GAds patient who scheduled themselves for a routine appointment.
  Routing: enter DB ✓, enter pipeline ✗, not in any view (lives in Reports + Campaign attribution only).

- **Bucket 2 — Warm Lead (needs nurture).**
  Examples: implant contact form, "call me back after I check my schedule" voicemail, missed call from nurture campaign, no-show after booking.
  Routing: enter DB ✓, enter pipeline ✓, show in default view (subject to campaign visibility).

- **Bucket 3 — Informational / Not-a-Lead.**
  Examples: "I'm looking for a MassHealth dentist," "Sorry, wrong office," current patient asking about parking.
  Routing: enter DB ✓ (for call analytics and optimizer feedback), enter pipeline ✗, surfaced to **optimizer** as a negative-signal feedback loop, not to operator.

---

## 3. Signal Hierarchy — Conversation Signal Trumps Campaign Signal

The order in which we evaluate signals matters:

1. **Conversation signal (Gemini follow-up classifier).** If we have a call transcript or a form message with enough content to classify, that wins. A "MassHealth seeker" call from an implant campaign does NOT enter the pipeline, regardless of the campaign rule.
2. **Campaign rule (fallback).** When there's no conversation signal (e.g., form fill with no message body, scheduler booking with no call), we fall back to the campaign's `auto_enter_pipeline_rule`.
3. **Explicit operator action.** Operator can always manually promote a lead into the pipeline or demote out of it, with a logged reason.

This ordering means: **the AI conversation classifier is the load-bearing piece.** Get it right, and 80% of the routing problem is solved. Get it wrong, and the pipeline either crowds (false positives) or misses real leads (false negatives).

---

## 4. Schema Changes

### 4.1 New columns on `mango_calls`

```sql
ALTER TABLE mango_calls ADD COLUMN follow_up_needed TEXT;
  -- enum: 'yes' | 'no' | 'unclear' | NULL (not yet classified)
ALTER TABLE mango_calls ADD COLUMN follow_up_reason TEXT;
  -- e.g., 'masshealth_seeker', 'wrong_office', 'callback_requested',
  -- 'schedule_check_needed', 'left_voicemail', 'current_patient_inquiry'
ALTER TABLE mango_calls ADD COLUMN follow_up_classified_at TIMESTAMP;
ALTER TABLE mango_calls ADD COLUMN follow_up_classifier_version TEXT;
  -- so we can re-classify with newer prompts and track which version made the call

-- Scope expansion (2026-05-21): also produce CallRail-equivalent signals locally,
-- so the Gemini classifier replaces what CallRail's Conversation Intelligence
-- add-on would have done — but at our cost ($0/transcript since Mango already
-- transcribes; just a Gemini API call).
ALTER TABLE mango_calls ADD COLUMN sentiment TEXT;
  -- enum: 'positive' | 'neutral' | 'negative' | 'mixed'
ALTER TABLE mango_calls ADD COLUMN sentiment_score REAL;
  -- 0.0 (very negative) to 1.0 (very positive)
ALTER TABLE mango_calls ADD COLUMN outcome TEXT;
  -- enum: 'appointment_booked' | 'callback_scheduled' | 'info_provided'
  --      | 'wrong_number' | 'voicemail' | 'no_match' | 'patient_existing'
  --      | 'price_shopping' | 'insurance_not_accepted' | 'other'
ALTER TABLE mango_calls ADD COLUMN keywords JSON;
  -- array of extracted keywords/topics, e.g.
  --   ["implant", "free consultation", "tomorrow", "insurance"]
```

### 4.2 New columns on `campaigns`

Replace the existing single `workflow` attribute with two finer-grained controls:

```sql
ALTER TABLE campaigns ADD COLUMN pipeline_default_visibility TEXT DEFAULT 'shown';
  -- enum: 'shown' (in default view) | 'hidden' (must be explicitly selected)
ALTER TABLE campaigns ADD COLUMN auto_enter_pipeline_rule TEXT DEFAULT 'when_follow_up_flagged';
  -- enum:
  --   'always'                  — every inbound enters pipeline (e.g., implant campaigns)
  --   'when_no_booking'         — only if no appointment was created (e.g., general campaigns)
  --   'when_follow_up_flagged'  — only if Gemini says follow_up_needed='yes' (DEFAULT)
  --   'never'                   — DB only, never pipeline (e.g., brand campaigns for existing patients)
```

**Migration of existing campaigns:**
- Implant campaigns → `auto_enter_pipeline_rule='always'`, `pipeline_default_visibility='shown'`
- General/cleaning campaigns → `auto_enter_pipeline_rule='when_follow_up_flagged'`, `pipeline_default_visibility='shown'`
- Emergency campaigns → `auto_enter_pipeline_rule='always'`, `pipeline_default_visibility='hidden'` (only show when operator filters for emergencies)
- New campaigns default → `pipeline_default_visibility='hidden'` (force operator to opt-in; prevents silent crowding)

### 4.3 New table — `pipeline_entry_audit`

Every entry (and non-entry) decision logged so we can debug routing later:

```sql
CREATE TABLE pipeline_entry_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lead_id INTEGER,
  source_type TEXT,             -- 'form' | 'call' | 'scheduler' | 'manual'
  source_id TEXT,               -- mango_calls.call_id, scheduler appt num, form submission id
  campaign_id INTEGER,
  decision TEXT,                -- 'entered' | 'skipped'
  reason TEXT,                  -- 'follow_up_needed=no:masshealth_seeker'
                                -- 'campaign_rule=when_no_booking:had_booking=true'
                                -- 'manual_promote_by_anurag'
  classifier_signal TEXT,       -- raw Gemini output if applicable
  campaign_rule TEXT,           -- which rule was in force at decision time
  decided_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

This audit log is critical. Without it, "why is this lead in/not in the pipeline?" is unanswerable.

---

## 5. The 5-PR Sequence

Smallest blast radius first, then build outward.

### PR 1 — Gemini Call Intelligence (Follow-Up + Sentiment + Outcomes + Keywords)
**Why first:** smallest blast radius (additive columns, no routing changes yet), biggest leverage (powers PRs 2–5). Also replaces CallRail's Conversation Intelligence add-on ($45/mo we don't pay).

- New module: `app/services/call_intelligence.py` (was: `follow_up_classifier.py`)
- Add seven columns to `mango_calls` (see 4.1):
  - `follow_up_needed`, `follow_up_reason`, `follow_up_classified_at`, `follow_up_classifier_version`
  - `sentiment`, `sentiment_score`, `outcome`, `keywords`
- Vertex AI / Gemini prompt: ingests transcript + call metadata, returns JSON:
  ```json
  {
    "follow_up_needed": "yes|no|unclear",
    "follow_up_reason": "callback_requested",
    "confidence": 0.92,
    "sentiment": "positive",
    "sentiment_score": 0.78,
    "outcome": "callback_scheduled",
    "keywords": ["implant", "tomorrow", "free consultation"]
  }
  ```
- Single Gemini call produces all four signals — cheaper than four separate calls and ensures internal consistency (a "negative" sentiment + "appointment_booked" outcome is suspicious and should be a flag, not a normal result).
- Classifier runs as part of mango call sync pipeline (post-transcription).
- Backfill job for last 90 days of calls.
- New endpoint: `GET /api/calls/:id/intelligence` for debugging (returns all four signals).
- **No routing changes yet.** Just classification + storage.
- Unit tests with fixture transcripts covering 8+ reason categories, 4 sentiments, 9 outcomes.
- Opus review after Sonnet writes.

**Acceptance:**
- `follow_up_needed` accuracy ≥90% on a hand-labeled set of 50 calls.
- `sentiment` accuracy ≥85% (more subjective; lower bar).
- `outcome` accuracy ≥90% on labeled set.
- Keywords: at least 3 meaningful keywords per call where transcript is >20s.
- Latency <5s per call (slightly higher than 3s due to expanded prompt).
- Cost <$0.02/call (Gemini Pro is cheap; expanded prompt is still well under target).

### PR 2 — Campaign-Level Pipeline Rules (Schema + Wizard)
- Schema changes 4.2 — add `pipeline_default_visibility` + `auto_enter_pipeline_rule`
- Migration: backfill existing campaigns per the migration map above
- Campaign Build Wizard (Strategy tab) adds two new controls with sensible defaults and inline help
- Schema validator: warn at launch if rule is `always` + visibility is `hidden` (likely a mistake)
- **No ingestion changes yet.** Just schema + UI to set rules.
- Test: edit existing campaign, change rule, see DB updated.

**Acceptance:** every existing campaign has both attributes populated. Wizard saves new campaigns with non-default values.

### PR 3 — Pipeline UI: Per-Campaign Filters + Saved Default View
- New filter dropdown in pipeline header: multi-select campaign chips
- "Save as Default View" button — writes selection to `user_settings.pipeline_default_campaigns` (per-user)
- "Reset to Default View" button — restores saved selection
- Counter behavior: filtered counts on top of stage columns reflect the active filter; "Total in pipeline" shows unfiltered count parenthetically
- Filter persists across sessions via localStorage as fallback
- **Still no ingestion changes.** This is read-side only.

**Acceptance:** user can hide emergency campaign by default and only see it when they multi-select it.

### PR 4 — Booked-Stage Entry for Self-Scheduled New GAds Patients
- When attribution-tagged scheduler booking ingests, if the patient is **new** and the campaign's `auto_enter_pipeline_rule='always'`, create a lead at the **Booked** stage (not Auto-Nurture)
- Lead gets a `self_booked=TRUE` flag and a `✓ self-booked` badge in UI
- Treatment field auto-populated from the appointment type ("Informational" if generic cleaning, otherwise the procedure code)
- Distinguishes operator action: badge tells you this lead didn't need outreach to book, but you should still confirm they show up
- **First ingestion-side change.** Limit to scheduler path only; form/call ingestion unchanged.

**Acceptance:** Candice Chase-style booking from an implants campaign lands at Booked stage with `✓ self-booked` badge. Existing patient via scheduler does NOT create a pipeline card.

### PR 5 — Existing-Patient Guard + Optimizer Noise Feedback
- Before any pipeline ingestion, run OD phone+name match. If existing patient → skip pipeline (DB row still created, tagged `existing_patient=TRUE`)
- "Informational" calls (Bucket 3, e.g., MassHealth seeker, wrong office) feed back to the AI Optimizer as a **negative-keyword candidate signal**
  - Already-existing infrastructure: `lqi_signals.py` collectors
  - New collector: `existing_patient_call_signal`, `wrong_intent_call_signal`
- Optimizer prompt now includes "this campaign generated N informational/wrong-intent calls last 7d — consider adding negatives"
- Wire `pipeline_entry_audit` into the optimizer prompt for transparency

**Acceptance:** existing-patient calls do not create pipeline cards; MassHealth-seeker calls show up in optimizer's account-level recommendations as negative-keyword candidates.

---

## 6. Open Design Decisions (Resolve Before PR 1)

These don't block PR 1 by themselves but they shape PRs 4–5. Pin them down before code.

### Q1 — Is the pipeline a post-visit tracker?
Today the pipeline has stages: Auto-Nurture → Scheduled → No-Show → Showed → Tx Presented → Tx Accepted. The last two are post-visit. **Recommended:** Yes, keep post-visit. The pipeline doubles as a treatment-presented/accepted tracker because that's where ROI shows up. We just need to make sure post-visit leads don't crowd the operator's worklist (default view filter solves this; post-visit goes to a "Showed/Tx" sub-tab).

### Q2 — Missed call from a nurture campaign — what happens?
A missed call from a patient already on an implant nurture sequence is a top-priority lead. **Recommended:** force-promote to pipeline with a `missed_call_flag=TRUE` and a red dot in the UI, regardless of Gemini classification (because there's no transcript to classify).

### Q3 — Shadow pipeline for self-serve leads?
Bucket 1 leads (self-serve bookings) have no presence in the pipeline. **Recommended:** No shadow pipeline. They're already captured in Reports + Campaign attribution. Adding a shadow surface just splits attention. If operator needs to find a Bucket 1 lead, they search by phone or name.

### Q4 — Classification change after entry — auto-remove?
If a lead enters the pipeline and a later call gets classified as `follow_up_needed=no`, do we auto-remove? **Recommended:** No. Once in the pipeline, manual demotion only. Auto-add is fine; auto-remove silently is dangerous (operator may have already invested time).

### Q5 — Wrong-office calls feeding optimizer — how aggressive?
**Recommended:** Suggest negatives but don't auto-apply. The optimizer surfaces them as **pending optimizer actions** for operator approval. Existing approval flow handles this cleanly.

### Q6 — "Awaiting Their Callback" as a stage?
A patient who says "I'll call you back after I check my schedule" — is that a new stage or just an Auto-Nurture tag? **Recommended:** Tag, not stage. The stage tells you where in the funnel; the tag tells you what's blocking. Adding a stage per blocker explodes the kanban.

---

## 7. Edge Cases (Captured for PR Specs)

These appeared during design discussion and should be addressed at the relevant PR:

- **Multiple calls from same lead — re-classify each, latest classification wins for routing.** Older classifications stay in `mango_calls` for audit.
- **Filter affects view, not ingestion.** Counts on filtered view show filtered numbers; total leads (unfiltered) shown parenthetically next to header.
- **New-campaign default visibility = hidden.** Forces operator to opt-in to seeing the campaign's leads. Prevents silent pipeline crowding when launching test campaigns.
- **Classifier confidence threshold.** Below threshold → `follow_up_needed='unclear'` → fall back to campaign rule. Don't gamble with low-confidence "no" classifications.
- **PHI in transcripts.** GDC has BAA with Google ([[feedback_phi_scrubbing]]) — Vertex AI is in scope, no scrubbing needed.

---

## 8. Risks

- **Classifier false negatives** (real lead classified as "no follow-up") leak revenue. Mitigation: keep `auto_enter_pipeline_rule='always'` for implants until classifier proves itself for ≥30 days.
- **Schema migration on `campaigns`** with existing data — test the migration on a DB copy first.
- **Operator surprise** when pipeline behavior changes. Mitigation: PR 3 ships with an explanatory tooltip and a "show all" toggle until operator gets used to defaults.
- **Audit log size.** `pipeline_entry_audit` grows ~1 row per inbound; manageable. Add a quarterly archive job if it exceeds 500k rows.

---

## 9. What Each PR Does NOT Do

To prevent scope creep — explicit non-goals per PR:

- **PR 1 does not change routing.** It only adds classification columns and a backfill.
- **PR 2 does not change ingestion.** It only adds schema + wizard controls.
- **PR 3 does not change ingestion.** It only changes what the operator sees.
- **PR 4 only touches scheduler ingestion.** Form and call ingestion are unchanged.
- **PR 5 wires up existing infrastructure** (OD matcher, LQI signals, optimizer prompt) — no new optimizer behavior is invented.

---

## 10. Open Items Outside This Plan (Tracked Elsewhere)

These are real-but-adjacent and should not creep into these PRs:

- Cloudflare tunnel for public webhook URL so GoDaddy can reach the pipeline ingestion endpoint (separate infra ticket).
- Practice Cafe to verify AW-360307486 removal (vendor task).
- Google Ads call conversion number swap installation (separate tracking ticket; bridges into CallRail plan).

---

## 11. Next Action

Awaiting operator decision on:

1. Approve this plan?
2. If yes, start with PR 1 — draft `PR7_SPEC_gemini_follow_up_classifier.md` (renumbered from PR1 above since PRs 1–6 are taken in this folder).
3. If concerns: address open design decisions in §6 first.
