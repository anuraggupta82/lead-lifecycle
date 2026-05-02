# Step 10 — TCPA Stop Conditions: Manual Test Checklist

**Service URL:** `http://localhost:7070`  
**Admin password:** `GDC-pipeline-2026!` (set via `X-Admin-Password` header)  
**Sig mode for testing:** set `twilio_sig_mode = skip` in DB before running webhook tests

---

## Prerequisites

Before running these tests, ensure:

- [ ] Backend is running: `python main.py` in `/backend`
- [ ] At least one test lead exists (run Firestore sync or create via API)
- [ ] Note a test `lead_id` (e.g., `lead_abc123def456`) for use throughout
- [ ] Set Twilio sig mode to skip for local testing:

```bash
curl -s -X POST http://localhost:7070/api/admin/settings \
  -H "X-Admin-Password: GDC-pipeline-2026!" \
  -H "Content-Type: application/json" \
  -d '{"key": "twilio_sig_mode", "value": "skip"}'
```

---

## Section 1 — Database Migrations

Verify all Step 10 columns and tables exist after `init_db()`.

### 1.1 New columns on `leads` table

```bash
# Connect to SQLite and check schema
sqlite3 /path/to/leads.db ".schema leads" | grep -E "dnd_reason|dnd_set_at|paused_at|paused_reason|paused_until"
```

**Expected:** All 5 columns present: `dnd_reason`, `dnd_set_at`, `paused_at`, `paused_reason`, `paused_until`

- [ ] PASS / FAIL

### 1.2 New columns on `follow_up_queue` table

```bash
sqlite3 /path/to/leads.db ".schema follow_up_queue" | grep -E "cancelled_at|cancellation_reason"
```

**Expected:** Both columns present: `cancelled_at`, `cancellation_reason`

- [ ] PASS / FAIL

### 1.3 `sms_messages` table exists

```bash
sqlite3 /path/to/leads.db ".tables" | tr ' ' '\n' | grep sms_messages
```

**Expected:** `sms_messages` listed

- [ ] PASS / FAIL

---

## Section 2 — Admin Pause/Resume Endpoints

### 2.1 Pause a lead (indefinite)

```bash
LEAD_ID="lead_REPLACE_ME"

curl -s -X POST http://localhost:7070/api/admin/lead/$LEAD_ID/pause \
  -H "X-Admin-Password: GDC-pipeline-2026!" \
  -H "Content-Type: application/json" \
  -d '{"reason": "test pause", "until": ""}' | python3 -m json.tool
```

**Expected response:**
```json
{"status": "ok", "lead_id": "...", "paused": true}
```

- [ ] PASS / FAIL

**Verify in DB:**
```bash
sqlite3 /path/to/leads.db "SELECT paused_at, paused_reason, paused_until FROM leads WHERE id='$LEAD_ID';"
```
**Expected:** `paused_at` is set, `paused_reason = "test pause"`, `paused_until` is empty

- [ ] PASS / FAIL

**Verify queue rows cancelled:**
```bash
sqlite3 /path/to/leads.db "SELECT COUNT(*) FROM follow_up_queue WHERE lead_id='$LEAD_ID' AND status='cancelled';"
```
**Expected:** Count > 0 (assuming lead had pending queue rows)

- [ ] PASS / FAIL

**Verify lifecycle_events entry:**
```bash
sqlite3 /path/to/leads.db "SELECT event_type, detail, source FROM lifecycle_events WHERE lead_id='$LEAD_ID' ORDER BY created_at DESC LIMIT 3;"
```
**Expected:** `manual_pause` event logged with `source = stop_engine`

- [ ] PASS / FAIL

### 2.2 Pause a lead (timed — until future date)

```bash
curl -s -X POST http://localhost:7070/api/admin/lead/$LEAD_ID/pause \
  -H "X-Admin-Password: GDC-pipeline-2026!" \
  -H "Content-Type: application/json" \
  -d '{"reason": "patient traveling", "until": "2030-01-01T00:00:00Z"}' | python3 -m json.tool
```

**Expected:** `{"status": "ok", "paused": true}`

**Verify DB:** `paused_until = "2030-01-01T00:00:00Z"`

- [ ] PASS / FAIL

### 2.3 Resume a paused lead

```bash
curl -s -X POST http://localhost:7070/api/admin/lead/$LEAD_ID/resume \
  -H "X-Admin-Password: GDC-pipeline-2026!" | python3 -m json.tool
```

**Expected:** `{"status": "ok", "paused": false}`

**Verify in DB:** `paused_at`, `paused_reason`, `paused_until` are all empty/null

- [ ] PASS / FAIL

### 2.4 Pause non-existent lead returns 404

```bash
curl -s -o /dev/null -w "%{http_code}" -X POST http://localhost:7070/api/admin/lead/lead_DOESNOTEXIST/pause \
  -H "X-Admin-Password: GDC-pipeline-2026!" \
  -H "Content-Type: application/json" \
  -d '{"reason": "test"}'
```

**Expected:** `404`

- [ ] PASS / FAIL

---

## Section 3 — Admin DND Endpoints

### 3.1 Set DND on SMS channel

```bash
curl -s -X POST http://localhost:7070/api/admin/lead/$LEAD_ID/dnd \
  -H "X-Admin-Password: GDC-pipeline-2026!" \
  -H "Content-Type: application/json" \
  -d '{"channel": "sms", "reason": "admin test"}' | python3 -m json.tool
```

**Expected:** `{"status": "ok", "dnd_channels": ["sms"]}`

**Verify DB:**
```bash
sqlite3 /path/to/leads.db "SELECT unsubscribed_sms, dnd_reason FROM leads WHERE id='$LEAD_ID';"
```
**Expected:** `unsubscribed_sms = 1`, `dnd_reason = "admin test"`

- [ ] PASS / FAIL

**Verify SMS queue rows cancelled:**
```bash
sqlite3 /path/to/leads.db "SELECT COUNT(*) FROM follow_up_queue WHERE lead_id='$LEAD_ID' AND channel='sms' AND status='cancelled';"
```
**Expected:** Count > 0

- [ ] PASS / FAIL

**Verify email queue rows NOT cancelled** (channel-specific):
```bash
sqlite3 /path/to/leads.db "SELECT COUNT(*) FROM follow_up_queue WHERE lead_id='$LEAD_ID' AND channel='email' AND status='pending';"
```
**Expected:** Count > 0 (email rows should still be pending)

- [ ] PASS / FAIL

### 3.2 Set DND on email channel

```bash
curl -s -X POST http://localhost:7070/api/admin/lead/$LEAD_ID/dnd \
  -H "X-Admin-Password: GDC-pipeline-2026!" \
  -H "Content-Type: application/json" \
  -d '{"channel": "email", "reason": "patient request"}' | python3 -m json.tool
```

**Expected:** `{"dnd_channels": ["email"]}`

**Verify DB:** `unsubscribed_email = 1`

- [ ] PASS / FAIL

### 3.3 Set DND on all channels

```bash
curl -s -X POST http://localhost:7070/api/admin/lead/$LEAD_ID/dnd \
  -H "X-Admin-Password: GDC-pipeline-2026!" \
  -H "Content-Type: application/json" \
  -d '{"channel": "all", "reason": "requested complete opt-out"}' | python3 -m json.tool
```

**Expected:** `{"dnd_channels": ["sms", "email"]}`

**Verify:** Both `unsubscribed_sms` and `unsubscribed_email` = 1

- [ ] PASS / FAIL

**Verify all queue rows cancelled:**
```bash
sqlite3 /path/to/leads.db "SELECT COUNT(*) FROM follow_up_queue WHERE lead_id='$LEAD_ID' AND status='pending';"
```
**Expected:** `0`

- [ ] PASS / FAIL

### 3.4 Clear DND on SMS

First re-set SMS DND, then clear it:

```bash
curl -s -X POST http://localhost:7070/api/admin/lead/$LEAD_ID/clear-dnd \
  -H "X-Admin-Password: GDC-pipeline-2026!" \
  -H "Content-Type: application/json" \
  -d '{"channel": "sms"}' | python3 -m json.tool
```

**Expected:** `{"status": "ok", "channel": "sms"}`

**Verify DB:** `unsubscribed_sms = 0`, `dnd_reason = ""`

- [ ] PASS / FAIL

**Verify lifecycle_events `dnd_cleared` logged:**
```bash
sqlite3 /path/to/leads.db "SELECT event_type, detail FROM lifecycle_events WHERE lead_id='$LEAD_ID' ORDER BY created_at DESC LIMIT 3;"
```
**Expected:** `dnd_cleared` entry with `{"channel": "sms"}`

- [ ] PASS / FAIL

### 3.5 Clear DND on all channels

```bash
curl -s -X POST http://localhost:7070/api/admin/lead/$LEAD_ID/clear-dnd \
  -H "X-Admin-Password: GDC-pipeline-2026!" \
  -H "Content-Type: application/json" \
  -d '{"channel": "all"}' | python3 -m json.tool
```

**Expected:** Both `unsubscribed_sms` and `unsubscribed_email` = 0 in DB

- [ ] PASS / FAIL

---

## Section 4 — Twilio Inbound Webhook (sig mode: skip)

**Note:** These tests simulate Twilio POST requests. Twilio sends form-encoded data. Get the lead's actual phone number from DB before running these tests.

```bash
PHONE="+15083184477"  # Replace with a phone number that exists on a test lead
```

### 4.1 STOP keyword — known lead

```bash
curl -s -X POST http://localhost:7070/webhooks/twilio/inbound \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "From=$PHONE&To=%2B15085551234&Body=STOP&MessageSid=SM_test001"
```

**Expected TwiML response:**
```xml
<?xml version='1.0' encoding='UTF-8'?>
<Response><Message>You have been unsubscribed. Reply START to resubscribe.</Message></Response>
```

- [ ] PASS / FAIL

**Verify DB:** `unsubscribed_sms = 1` on the matching lead

- [ ] PASS / FAIL

**Verify `sms_messages` table has the inbound record:**
```bash
sqlite3 /path/to/leads.db "SELECT direction, body, from_number FROM sms_messages ORDER BY received_at DESC LIMIT 3;"
```
**Expected:** Row with `direction=inbound`, `body=STOP`

- [ ] PASS / FAIL

**Verify SMS queue rows cancelled for that lead:**
```bash
sqlite3 /path/to/leads.db "SELECT COUNT(*) FROM follow_up_queue WHERE lead_id='$LEAD_ID' AND status='cancelled' AND channel='sms';"
```
**Expected:** Count > 0

- [ ] PASS / FAIL

**Verify `sms_stop` in lifecycle_events:**
```bash
sqlite3 /path/to/leads.db "SELECT event_type, source FROM lifecycle_events WHERE lead_id='$LEAD_ID' ORDER BY created_at DESC LIMIT 5;"
```
**Expected:** `sms_stop` event with `source = stop_engine`

- [ ] PASS / FAIL

### 4.2 STOPALL variant

```bash
curl -s -X POST http://localhost:7070/webhooks/twilio/inbound \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "From=$PHONE&To=%2B15085551234&Body=STOPALL&MessageSid=SM_test002"
```

**Expected:** Same STOP TwiML reply, DND applied

- [ ] PASS / FAIL

### 4.3 UNSUBSCRIBE keyword

```bash
curl -s -X POST http://localhost:7070/webhooks/twilio/inbound \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "From=$PHONE&To=%2B15085551234&Body=UNSUBSCRIBE&MessageSid=SM_test003"
```

**Expected:** STOP TwiML reply

- [ ] PASS / FAIL

### 4.4 STOP — case insensitive

```bash
curl -s -X POST http://localhost:7070/webhooks/twilio/inbound \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "From=$PHONE&To=%2B15085551234&Body=stop&MessageSid=SM_test004"
```

**Expected:** STOP TwiML reply (lowercase should match)

- [ ] PASS / FAIL

### 4.5 STOP with trailing punctuation

```bash
curl -s -X POST http://localhost:7070/webhooks/twilio/inbound \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "From=$PHONE&To=%2B15085551234&Body=STOP!&MessageSid=SM_test005"
```

**Expected:** STOP TwiML reply (punctuation stripped from first word)

- [ ] PASS / FAIL

### 4.6 STOP not in first word position — should NOT trigger

```bash
curl -s -X POST http://localhost:7070/webhooks/twilio/inbound \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "From=$PHONE&To=%2B15085551234&Body=Please+STOP+emailing+me&MessageSid=SM_test006"
```

**Expected:** Empty `<Response/>` (STOP is not the first word — treated as regular reply)

- [ ] PASS / FAIL

**Verify lead's `unsubscribed_sms` was NOT changed by this message**

- [ ] PASS / FAIL

### 4.7 STOP from unknown number — no confirmation sent

```bash
UNKNOWN_PHONE="+19999999999"

curl -s -X POST http://localhost:7070/webhooks/twilio/inbound \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "From=$UNKNOWN_PHONE&To=%2B15085551234&Body=STOP&MessageSid=SM_test007"
```

**Expected:** Empty `<Response/>` (no confirmation to unknown number per CTIA guidelines)

- [ ] PASS / FAIL

**Verify `sms_messages` row logged with `lead_id = NULL` (unknown number):**
```bash
sqlite3 /path/to/leads.db "SELECT lead_id, from_number, body FROM sms_messages ORDER BY received_at DESC LIMIT 3;"
```
**Expected:** Row with `lead_id = NULL` and `body = STOP`

- [ ] PASS / FAIL

### 4.8 START keyword — re-subscribe

First ensure the lead is on SMS DND, then send START:

```bash
curl -s -X POST http://localhost:7070/webhooks/twilio/inbound \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "From=$PHONE&To=%2B15085551234&Body=START&MessageSid=SM_test008"
```

**Expected TwiML:**
```xml
<Response><Message>You have been resubscribed. Reply STOP to unsubscribe.</Message></Response>
```

- [ ] PASS / FAIL

**Verify DB:** `unsubscribed_sms = 0`

- [ ] PASS / FAIL

**Verify `sms_resubscribed` in lifecycle_events**

- [ ] PASS / FAIL

### 4.9 YES and UNSTOP keywords (START variants)

```bash
curl -s -X POST http://localhost:7070/webhooks/twilio/inbound \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "From=$PHONE&To=%2B15085551234&Body=YES&MessageSid=SM_test009"
```

**Expected:** START TwiML reply

- [ ] PASS / FAIL

### 4.10 HELP keyword

```bash
curl -s -X POST http://localhost:7070/webhooks/twilio/inbound \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "From=$PHONE&To=%2B15085551234&Body=HELP&MessageSid=SM_test010"
```

**Expected TwiML:**
```xml
<Response><Message>Grafton Dental Care: Reply STOP to unsubscribe. Call 508-318-4477 for help.</Message></Response>
```

- [ ] PASS / FAIL

### 4.11 Regular reply (non-keyword) — log only

```bash
curl -s -X POST http://localhost:7070/webhooks/twilio/inbound \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "From=$PHONE&To=%2B15085551234&Body=Is+my+appointment+confirmed%3F&MessageSid=SM_test011"
```

**Expected:** Empty `<Response/>`

- [ ] PASS / FAIL

**Verify `replied` event in lifecycle_events (log only — no queue cancellation):**
```bash
sqlite3 /path/to/leads.db "SELECT event_type, detail FROM lifecycle_events WHERE lead_id='$LEAD_ID' ORDER BY created_at DESC LIMIT 3;"
```
**Expected:** `replied` event, no new cancellations

- [ ] PASS / FAIL

### 4.12 Regular reply from unknown number — empty response, no event logged

```bash
curl -s -X POST http://localhost:7070/webhooks/twilio/inbound \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "From=$UNKNOWN_PHONE&To=%2B15085551234&Body=Hello+there&MessageSid=SM_test012"
```

**Expected:** Empty `<Response/>` (no lead matched, no event to log)

- [ ] PASS / FAIL

---

## Section 5 — Signature Verification Modes

### 5.1 log_only mode — bad sig logs but continues

```bash
# Switch to log_only mode
curl -s -X POST http://localhost:7070/api/admin/settings \
  -H "X-Admin-Password: GDC-pipeline-2026!" \
  -H "Content-Type: application/json" \
  -d '{"key": "twilio_sig_mode", "value": "log_only"}'

# Send STOP with no signature header
curl -s -X POST http://localhost:7070/webhooks/twilio/inbound \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "From=$PHONE&To=%2B15085551234&Body=STOP&MessageSid=SM_sig001"
```

**Expected:** Request succeeds (STOP TwiML returned), warning logged in server console: `Twilio signature mismatch`

**IMPORTANT:** Even with bad sig in log_only, verify no state mutation occurred on a known lead when the sig is mismatched in this test setup. This relates to the Opus HIGH finding — if your `twilio_auth_token` env var is not set, sig check is skipped entirely, so this test only validates if the token is configured.

- [ ] PASS / FAIL (or N/A if auth token not configured)

### 5.2 enforce mode — bad sig returns 403

```bash
# Switch to enforce mode
curl -s -X POST http://localhost:7070/api/admin/settings \
  -H "X-Admin-Password: GDC-pipeline-2026!" \
  -H "Content-Type: application/json" \
  -d '{"key": "twilio_sig_mode", "value": "enforce"}'

# Send with bad/missing signature
curl -s -o /dev/null -w "%{http_code}" -X POST http://localhost:7070/webhooks/twilio/inbound \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "From=$PHONE&To=%2B15085551234&Body=STOP&MessageSid=SM_sig002"
```

**Expected:** HTTP 403 (only if `TWILIO_AUTH_TOKEN` is set in .env; otherwise no-ops)

- [ ] PASS / FAIL (or N/A if auth token not configured)

```bash
# Restore skip mode for remaining tests
curl -s -X POST http://localhost:7070/api/admin/settings \
  -H "X-Admin-Password: GDC-pipeline-2026!" \
  -H "Content-Type: application/json" \
  -d '{"key": "twilio_sig_mode", "value": "skip"}'
```

---

## Section 6 — Follow-up Engine: Pre-dispatch Re-read

These tests verify the engine skips cancelled/paused rows even if they were already fetched by `get_due_follow_ups()`.

### 6.1 Cancelled row is skipped at dispatch time

1. Enqueue a follow-up manually (or wait for a real one to enter `pending` status)
2. Cancel it via admin DND: `POST /api/admin/lead/$LEAD_ID/dnd` with `channel: all`
3. Trigger the queue processor manually:

```bash
curl -s -X POST http://localhost:7070/api/admin/run-queue \
  -H "X-Admin-Password: GDC-pipeline-2026!"
```

**Expected in server logs:** `Queue item <id> was cancelled mid-batch; skipping`

**Verify:** No email/SMS sent to lead (check email delivery or SMS logs)

- [ ] PASS / FAIL

### 6.2 Paused lead rows are skipped

1. Pause a lead with a future `until` date (e.g., 2030)
2. Enqueue follow-ups (or check for existing pending rows)
3. Run queue processor

**Expected in logs:** `skipped` with reason `lead_paused_until=...`

- [ ] PASS / FAIL

### 6.3 Indefinitely paused lead rows are skipped

1. Pause lead indefinitely: `{"reason": "test", "until": ""}`
2. Run queue processor

**Expected in logs:** `skipped` with reason `lead_paused_indefinitely`

- [ ] PASS / FAIL

### 6.4 Resumed lead is no longer skipped

1. Resume the lead from test 6.3
2. Run queue processor

**Expected:** Lead's pending queue rows are processed normally (not skipped for pause)

- [ ] PASS / FAIL

---

## Section 7 — Booked Transition (Firestore Sync)

### 7.1 Booked transition fires stop engine

This test requires a lead that is currently in a non-booked stage (e.g., `new`, `auto_nurture`) and a Firestore record that changes to `scheduled`.

1. Manually update a lead's stage to `new` in SQLite:
```bash
sqlite3 /path/to/leads.db "UPDATE leads SET stage='new' WHERE id='$LEAD_ID';"
```

2. Trigger a sync:
```bash
curl -s -X POST http://localhost:7070/api/admin/sync \
  -H "X-Admin-Password: GDC-pipeline-2026!"
```

3. If the Firestore record for this lead has `stage: scheduled`, verify the stop engine fired.

**Expected in logs:** `Firestore sync: fired stop_engine 'booked' for lead ...`

**Verify:** `booked` event in lifecycle_events, all queue rows cancelled

- [ ] PASS / FAIL (or N/A if Firestore record not in booked stage)

### 7.2 Re-sync does NOT re-fire for already-booked lead

Run sync again immediately after test 7.1.

**Expected:** No second `booked` event in lifecycle_events (transition already happened)

```bash
sqlite3 /path/to/leads.db "SELECT COUNT(*) FROM lifecycle_events WHERE lead_id='$LEAD_ID' AND event_type='booked';"
```
**Expected:** Still `1` (not `2`)

- [ ] PASS / FAIL

---

## Section 8 — Email Unsubscribe

### 8.1 Unsubscribe link in email

```bash
curl -s http://localhost:7070/unsubscribe/$LEAD_ID/email
```

**Expected:** Redirect (302) or confirmation page

**Verify DB:** `unsubscribed_email = 1`

- [ ] PASS / FAIL

### 8.2 Follow-up engine skips unsubscribed email

With `unsubscribed_email = 1` on the lead, run queue processor.

**Expected in logs:** `skipped` with reason `unsubscribed` for any `channel=email` queue rows

- [ ] PASS / FAIL

### 8.3 Follow-up engine skips SMS-unsubscribed lead

With `unsubscribed_sms = 1`:

**Expected:** SMS queue rows skipped with reason `unsubscribed`

- [ ] PASS / FAIL

---

## Section 9 — Frontend UI Tests

Open the dashboard at `http://localhost:7070` and navigate to a lead detail panel.

### 9.1 Paused lead shows status banner

1. Pause a lead via API (test 2.1)
2. Open that lead in the dashboard

**Expected:** Purple "Paused" status banner visible below the tab nav

- [ ] PASS / FAIL

### 9.2 DND lead shows status banner

1. Set DND on a lead via API (test 3.1)
2. Open that lead in the dashboard

**Expected:** Amber "DND" status banner visible

- [ ] PASS / FAIL

### 9.3 Staff Actions tab shows Sequence Controls section

Open a lead → Staff Actions tab

**Expected:** "Sequence Controls" section with Pause/Resume and DND/Clear DND buttons

- [ ] PASS / FAIL

### 9.4 Pause button pauses lead

Click "Pause" in the Sequence Controls section

**Expected:**
- Confirmation prompt appears (or direct API call fires)
- Banner changes to "Paused" state
- Resume button becomes active

- [ ] PASS / FAIL

### 9.5 Resume button resumes lead

With a paused lead, click "Resume"

**Expected:** Paused banner disappears, lead is active again

- [ ] PASS / FAIL

### 9.6 Set DND (SMS) button

Click "DND SMS"

**Expected:** Amber DND banner appears, SMS queue rows cancelled

- [ ] PASS / FAIL

### 9.7 Clear DND button

With DND set, click "Clear DND"

**Expected:** DND banner disappears

- [ ] PASS / FAIL

### 9.8 Sequence schedule shows cancelled rows

After cancelling rows via DND or Pause, go to the Sequence tab in lead detail.

**Expected:** Cancelled rows shown at 55% opacity with `cancellation_reason` displayed

- [ ] PASS / FAIL

### 9.9 Activity timeline shows Step 10 events

Go to the Activity tab in lead detail after running several stop-condition tests.

**Expected:** `sms_stop`, `manual_pause`, `dnd_set`, `dnd_cleared`, `replied` events appear with colored dots (red for stop events, green for re-subscribe events)

- [ ] PASS / FAIL

---

## Section 10 — Admin Authentication

### 10.1 No password header returns 401/422

```bash
curl -s -o /dev/null -w "%{http_code}" -X POST http://localhost:7070/api/admin/lead/$LEAD_ID/pause \
  -H "Content-Type: application/json" \
  -d '{"reason": "test"}'
```

**Expected:** `422` (missing required header) or `401`

- [ ] PASS / FAIL

### 10.2 Wrong password returns 401

```bash
curl -s -o /dev/null -w "%{http_code}" -X POST http://localhost:7070/api/admin/lead/$LEAD_ID/pause \
  -H "X-Admin-Password: wrongpassword" \
  -H "Content-Type: application/json" \
  -d '{"reason": "test"}'
```

**Expected:** `401`

- [ ] PASS / FAIL

---

## Section 11 — Known Issues (Pre-Production Fixes Needed)

The following issues were identified during the Opus review. These tests are **expected to fail** or surface edge-case bugs until the fixes are applied.

### 11.1 ⚠️ Phone hash normalization (CRITICAL)

Twilio delivers E.164 format: `+15083184477` (11 digits). Lead stored with `5083184477` (10 digits). The SHA-256 of these two strings differs, so `get_lead_by_phone()` hash lookup misses and falls back to the LIKE scan.

**Test:** Send a STOP from `+15083184477` where lead is stored as `5083184477`.

**Expected (after fix):** Hash match on first lookup. **Current behavior:** LIKE fallback (works but slow, full table scan).

- [ ] Hash match works (FIX NOT YET APPLIED — use LIKE fallback for now)

### 11.2 ⚠️ Duplicate events on STOP keyword (HIGH)

`set_lead_dnd()` calls `add_event()` internally, AND `handle_event()` in the webhook also calls `add_lead_event()`. This creates two `sms_stop` events per STOP keyword.

**Test:** Send STOP from a known lead. Count `sms_stop` events in lifecycle_events.

```bash
sqlite3 /path/to/leads.db "SELECT COUNT(*) FROM lifecycle_events WHERE lead_id='$LEAD_ID' AND event_type='sms_stop';"
```

**Expected (after fix):** `1`. **Current behavior:** `2` (known bug, FIX NOT YET APPLIED)

- [ ] Noted / FIX PENDING

### 11.3 ⚠️ BEGIN IMMEDIATE transaction (CRITICAL)

`cancel_queue_rows()` uses `BEGIN IMMEDIATE` without `isolation_level=None`. Under concurrent load this may fail silently.

**Test:** Run queue processor and DND simultaneously from two terminal windows (race condition — hard to reproduce reliably in dev).

- [ ] Noted / FIX PENDING — low risk in single-user local setup

### 11.4 ⚠️ log_only mode allows state mutations on bad sig (HIGH)

In `log_only` mode, a request with a bad signature still mutates lead state (cancels queue rows, sets DND). This is the intended behavior of `log_only` for dev, but is a risk if accidentally left in production.

**Mitigation:** Ensure `twilio_sig_mode = enforce` before going live.

- [ ] Verify sig mode is set to `enforce` before production deployment

---

## Test Run Summary

| Section | Tests | Passed | Failed | Notes |
|---------|-------|--------|--------|-------|
| 1 — DB Migrations | 3 | | | |
| 2 — Pause/Resume | 4 | | | |
| 3 — DND | 5 | | | |
| 4 — Twilio Webhook | 12 | | | |
| 5 — Sig Verification | 2 | | | |
| 6 — Engine Pre-read | 4 | | | |
| 7 — Booked Transition | 2 | | | |
| 8 — Email Unsub | 3 | | | |
| 9 — Frontend UI | 9 | | | |
| 10 — Auth | 2 | | | |
| **Total** | **46** | | | |

**Tester:** _______________  **Date:** _______________

---

## Post-Test Cleanup

Reset any test leads back to normal state:

```bash
sqlite3 /path/to/leads.db "UPDATE leads SET unsubscribed_sms=0, unsubscribed_email=0, dnd_reason='', dnd_set_at='', paused_at='', paused_reason='', paused_until='' WHERE id='$LEAD_ID';"
sqlite3 /path/to/leads.db "UPDATE follow_up_queue SET status='pending', cancelled_at='', cancellation_reason='' WHERE lead_id='$LEAD_ID' AND status='cancelled';"
```

Restore Twilio sig mode to appropriate value for your environment:
```bash
# For production: enforce
# For local dev: log_only or skip
curl -s -X POST http://localhost:7070/api/admin/settings \
  -H "X-Admin-Password: GDC-pipeline-2026!" \
  -H "Content-Type: application/json" \
  -d '{"key": "twilio_sig_mode", "value": "log_only"}'
```
