# GDC Lead Lifecycle — Next Phases Plan

**Date:** May 2, 2026  
**Author:** Claude  
**For Opus review before execution**

---

## Overview

Two parallel tracks are ready for the next build session:

1. **Track A — Step 10 Pre-Production Bug Fixes** (4 issues flagged by Opus review that must be resolved before the stop-conditions system is safe for live use)
2. **Track B — Google Ads Phase 2** (wire the 3 remaining 501-stub operations + build the Optimizer Pending Approvals UI in the frontend)

These are ordered by risk. Track A is smaller, cleaner, and unblocks the TCPA compliance system. Track B is the bigger feature but all the plumbing exists — it's mostly wiring the execute functions and building the UI panel.

---

## Track A — Step 10 Bug Fixes (4 issues)

### A1 — CRITICAL: `cancel_queue_rows` BEGIN IMMEDIATE transaction conflict

**Problem:**  
`cancel_queue_rows()` in `database.py` manually calls `conn = _conn()` and then `conn.execute("BEGIN IMMEDIATE")`. Python's `sqlite3` module maintains implicit transaction state internally. When you call `BEGIN IMMEDIATE` directly via `.execute()`, Python's internal state doesn't know a transaction is open — so on the next `.execute()` that writes, Python may auto-issue its own `BEGIN` or you may get `"cannot start a transaction within a transaction"` depending on the isolation_level setting of the connection. This is fragile and can fail silently or raise at runtime under concurrent load.

**Fix:**  
Use the `with _conn() as conn:` context manager pattern (which correctly handles commit/rollback) and set the connection's `isolation_level = None` (autocommit) before manually issuing `BEGIN IMMEDIATE`. Or simpler: drop `BEGIN IMMEDIATE` entirely and rely on the context manager, since SQLite WAL mode handles read/write concurrency safely for a single-process app. The `_conn()` context manager already issues a single transaction via Python's sqlite3 context manager protocol.

**Code change — `database.py`, `cancel_queue_rows()`:**

```python
def cancel_queue_rows(lead_id: str, channels=None, reason: str = '') -> int:
    """Cancel pending queue rows. Returns count cancelled."""
    now_ts = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        if channels:
            placeholders = ",".join("?" * len(channels))
            result = conn.execute(
                f"UPDATE follow_up_queue SET status='cancelled', cancelled_at=?, cancellation_reason=? "
                f"WHERE lead_id=? AND status='pending' AND channel IN ({placeholders})",
                [now_ts, reason, lead_id] + list(channels)
            )
        else:
            result = conn.execute(
                "UPDATE follow_up_queue SET status='cancelled', cancelled_at=?, cancellation_reason=? "
                "WHERE lead_id=? AND status='pending'",
                (now_ts, reason, lead_id)
            )
        return result.rowcount
```

This drops `BEGIN IMMEDIATE` and uses the standard `with _conn() as conn:` pattern consistently with the rest of `database.py`.

---

### A2 — CRITICAL: Phone hash normalization (E.164 vs 10-digit mismatch)

**Problem:**  
Twilio delivers `From` numbers in E.164 format: `+15083184477` (11 digits for US). When the lead was originally saved, `save_lead()` stores the phone as-typed from the form (e.g., `5083184477` — 10 digits, no country code). The `_hash()` function computes SHA-256 of the raw string, so `hash("+15083184477") ≠ hash("5083184477")`. The `get_lead_by_phone()` hash lookup therefore always misses on E.164 input, falling back to the LIKE scan on every inbound Twilio message.

**Fix:**  
Normalize to the last 10 digits before hashing — both at write time (in `save_lead` / `upsert_lead`) and at read time (in `get_lead_by_phone`). This ensures the hash matches regardless of whether the phone was stored with or without country code.

**Code changes:**

In `database.py`, update `_hash_phone()` (or add it as a new helper):
```python
def _hash_phone(phone: str) -> str:
    """Hash the last 10 digits of a phone number (normalizes E.164 vs local format)."""
    digits = "".join(c for c in phone if c.isdigit())
    normalized = digits[-10:] if len(digits) >= 10 else digits
    return hashlib.sha256(normalized.encode()).hexdigest()
```

Update `get_lead_by_phone()` to use `_hash_phone()` instead of `_hash()` on the raw input.

Update `upsert_lead()` / `save_lead()` to call `_hash_phone(phone)` when computing `phone_hash` on write.

Add a one-time migration in `_migrate()` to backfill `phone_hash` on existing leads:
```python
# Step 10 phone hash backfill — normalize existing hashes to last-10-digits
rows = conn.execute("SELECT id, phone FROM leads WHERE phone != ''").fetchall()
for row in rows:
    corrected_hash = _hash_phone(row["phone"])
    conn.execute("UPDATE leads SET phone_hash=? WHERE id=?", (corrected_hash, row["id"]))
```

---

### A3 — HIGH: Duplicate `sms_stop` events written per STOP keyword

**Problem:**  
When a lead sends STOP, the webhook calls both:
1. `set_lead_dnd(lead_id, "sms")` — which internally calls `add_event(lead_id, "sms_stop", ...)` 
2. `_stop_handle(lead_id, "sms_stop")` — which calls `add_lead_event(lead_id, "sms_stop", ...)`

This creates two `sms_stop` rows in `lifecycle_events` for every STOP. The activity timeline shows the event twice; the test checklist flags this.

**Fix:**  
Remove the `add_event()` call from `set_lead_dnd()`. That function should only update the DB columns (`unsubscribed_sms=1`, `dnd_reason`, `dnd_set_at`). The event logging is `stop_engine.handle_event()`'s responsibility.

**Code change — `database.py`, `set_lead_dnd()`:**
- Remove the `add_event(lead_id, event_type, ...)` line at the end of `set_lead_dnd()`.
- The function should only do the DB column update + insert into `unsubscribes` table.

---

### A4 — HIGH: `log_only` mode allows state mutations on bad signature

**Problem:**  
In `log_only` sig mode, when a bad signature is detected, the code logs a warning but continues processing the full webhook body — including mutating lead state (setting DND, cancelling queue rows). This means a spoofed Twilio webhook with a fake number and fake STOP body could cancel a real lead's queue if the phone number matches.

**Fix:**  
In `log_only` mode, when signature validation fails, still process the message for logging and SMS storage (so we can see what's coming in) but short-circuit any state mutations. The simplest approach: set a `sig_valid` flag and gate the STOP/START keyword handling on it.

**Code change — `main.py`, `twilio_inbound_webhook()`:**
```python
sig_valid = True  # default
if sig_mode != "skip" and settings.twilio_auth_token:
    valid = _verify_twilio_signature(...)
    if not valid:
        logger.warning(f"Twilio signature mismatch from={from_number}")
        if sig_mode == "enforce":
            return _Resp(content=TWIML_EMPTY, status_code=403)
        # log_only: mark invalid but continue for logging only
        sig_valid = False

# ... after lead lookup and SMS storage ...

# Gate state mutations on sig_valid
if first_word in _SMS_STOP_WORDS:
    if not sig_valid:
        logger.warning(f"Skipping STOP state mutation — signature invalid (log_only mode)")
        return _Resp(content=TWIML_EMPTY, media_type="application/xml")
    # ... rest of STOP handling ...
```

---

## Track B — Google Ads Phase 2

### B1 — Wire bid changes: `increase_bid` and `decrease_bid`

**What:** Currently returns HTTP 501. Need to implement `_execute_bid_change()` in `ai_optimizer.py` using `AdGroupCriterionService` with a `max_cpc_bid_micros` update.

**How it works:**
- `gads_audit_log` rows for bid changes have `before_state_json` = `{"current_bid_micros": N}` and `after_state_json` = `{"new_bid_micros": N}`.
- The approve endpoint reads `after_state_json["new_bid_micros"]` and applies it.
- `AdGroupCriterionService.mutate_ad_group_criteria` with `update_mask = "effective_cpc_bid_micros"` (for manual CPC keywords).

**New function in `ai_optimizer.py`:**
```python
def _execute_bid_change(client, customer_id: str, resource_name: str, new_bid_micros: int) -> bool:
    service = client.get_service("AdGroupCriterionService")
    operation = client.get_type("AdGroupCriterionOperation")
    criterion = operation.update
    criterion.resource_name = resource_name
    criterion.effective_cpc_bid_micros = new_bid_micros
    client.copy_from(operation.update_mask, 
                     client.get_type("FieldMask")(paths=["effective_cpc_bid_micros"]))
    service.mutate_ad_group_criteria(customer_id=customer_id, operations=[operation])
    return True
```

**`main.py` approve endpoint:** Replace the 501 for `increase_bid`/`decrease_bid` with:
```python
elif operation in ("increase_bid", "decrease_bid"):
    after = json.loads(row["after_state_json"])
    new_bid_micros = after.get("new_bid_micros")
    if not new_bid_micros:
        raise HTTPException(status_code=422, detail="after_state_json missing new_bid_micros")
    client = _build_client()
    _execute_bid_change(client, customer_id, resource_name=row["entity_id"], 
                        new_bid_micros=new_bid_micros)
    update_gads_action_result(action_id, executed=True, execution_result="success")
    set_audit_approval(action_id, approver="admin")
```

**Guard:** Add a `MAX_BID_MICROS = 50_000_000` (= $50 CPC) hard cap in `campaign_safety.py` — reject any bid change that would set a keyword above this. Bids are in micros (1,000,000 = $1.00).

---

### B2 — Wire `add_exact_keyword`

**What:** Add a new exact-match keyword to an ad group via `AdGroupCriterionService`.

**How it works:**
- `entity_id` in the audit row = `customers/{cid}/adGroups/{ad_group_id}` (the ad group resource name)
- `after_state_json` = `{"keyword_text": "dental implants boston", "match_type": "EXACT"}`
- Use `AdGroupCriterionOperation` with a new `keyword` criterion (not an update — a create).

**New function in `ai_optimizer.py`:**
```python
def _execute_add_keyword(client, customer_id: str, ad_group_resource: str, 
                          keyword_text: str, match_type: str = "EXACT") -> bool:
    service = client.get_service("AdGroupCriterionService")
    operation = client.get_type("AdGroupCriterionOperation")
    criterion = operation.create
    criterion.ad_group = ad_group_resource
    criterion.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
    criterion.keyword.text = keyword_text
    criterion.keyword.match_type = client.enums.KeywordMatchTypeEnum[match_type]
    service.mutate_ad_group_criteria(customer_id=customer_id, operations=[operation])
    return True
```

**Guard:** `campaign_safety.py` — check `ad_group_resource` is not in the `never_automate` list.

---

### B3 — Wire `add_negative_keyword`

**What:** Add a campaign-level negative keyword via `CampaignCriterionService`.

**How it works:**
- `entity_id` = campaign resource name (e.g., `customers/{cid}/campaigns/{campaign_id}`)
- `after_state_json` = `{"keyword_text": "free dental", "match_type": "BROAD"}`
- Negative keywords typically use BROAD at campaign level.

**New function in `ai_optimizer.py`:**
```python
def _execute_add_negative(client, customer_id: str, campaign_resource: str,
                           keyword_text: str, match_type: str = "BROAD") -> bool:
    service = client.get_service("CampaignCriterionService")
    operation = client.get_type("CampaignCriterionOperation")
    criterion = operation.create
    criterion.campaign = campaign_resource
    criterion.negative = True
    criterion.keyword.text = keyword_text
    criterion.keyword.match_type = client.enums.KeywordMatchTypeEnum[match_type]
    service.mutate_campaign_criteria(customer_id=customer_id, operations=[operation])
    return True
```

---

### B4 — Frontend: Pending Approvals Panel in the Admin/Optimizer tab

**What:** Currently the optimizer tab shows a read-only report after "Run AI Optimizer". It does not show previously-generated `pending_approval` rows at all. There's no way to see or act on recommendations from prior optimizer runs without using curl.

**New panel:** "Pending Recommendations" card that:
1. Loads on mount via `GET /api/admin/gads/pending-approvals`
2. Groups rows by `operation` type
3. Shows each row with: keyword name, operation, reason, before/after state, created_at, a green **Apply** button and a red **Reject** button
4. Apply → `POST /api/admin/gads/approve/{action_id}` → row disappears or changes to "Applied ✓"
5. Reject → `POST /api/admin/gads/reject/{action_id}` → row changes to "Rejected"
6. Empty state: "No pending recommendations. Run the optimizer to generate new ones."
7. Write-controls status banner at top of panel: shows whether writes are enabled, with a toggle (calls `POST /api/admin/gads/writes-enabled`)

**Panel location:** Insert above the existing "Run AI Optimizer" button section in the `AdminSection` component.

**Response shape** from `GET /api/admin/gads/pending-approvals`:
```json
{
  "pending": [
    {
      "action_id": "uuid",
      "operation": "pause_keyword",
      "entity_name": "dental implants cost",
      "reason": "0 leads, $142 spend over 30 days",
      "before_state_json": "{}",
      "after_state_json": "{}",
      "created_at": "2026-05-02T07:00:00Z"
    }
  ]
}
```

**UI layout per row:**
```
[⛔ pause_keyword]  "dental implants cost"
  Reason: 0 leads, $142 spend over 30 days
  Before: enabled | After: paused
  Requested: May 2, 2026 7:00 AM
  [✅ Apply]  [❌ Reject]
```

---

### B5 — Write Controls status card in frontend

Currently `GET /api/admin/gads/writes-status` and `POST /api/admin/gads/writes-enabled` exist but there's no UI for them. Add a small status banner to the Pending Approvals panel:

```
Google Ads Writes: [DISABLED]  [Enable Writes]
Note: Both env var and runtime switch must be enabled for any action to execute.
```

When writes are disabled, Apply buttons are grayed out with a tooltip: "Enable Google Ads writes first."

---

## Execution Order

1. **A1** — Fix `cancel_queue_rows` (5 min, 10 lines changed)
2. **A2** — Phone hash normalization + backfill migration (20 min)
3. **A3** — Remove duplicate event from `set_lead_dnd` (2 min, 1 line deleted)
4. **A4** — `sig_valid` flag in Twilio webhook (15 min)
5. **B1** — `_execute_bid_change` + wire in approve endpoint (30 min)
6. **B2** — `_execute_add_keyword` + wire (20 min)
7. **B3** — `_execute_add_negative` + wire (15 min)
8. **B4 + B5** — Frontend Pending Approvals panel + Write Controls (60-90 min, largest piece)
9. Syntax check all modified files
10. Opus final review

---

## Files Modified

| File | Changes |
|------|---------|
| `backend/database.py` | A1: fix `cancel_queue_rows`; A2: add `_hash_phone()`, update `upsert_lead` + `get_lead_by_phone`, add backfill migration; A3: remove `add_event` from `set_lead_dnd` |
| `backend/main.py` | A4: `sig_valid` flag; B1/B2/B3: replace 501 stubs in approve endpoint |
| `backend/ai_optimizer.py` | B1/B2/B3: add `_execute_bid_change`, `_execute_add_keyword`, `_execute_add_negative` |
| `backend/campaign_safety.py` | B1: add `MAX_BID_MICROS` guard; B2: ad group resource check |
| `frontend/index.html` | B4/B5: Pending Approvals panel + Write Controls banner in AdminSection |

---

## What's NOT in this plan (deferred)

- Sendgrid email event webhook (needs Sendgrid account migration)
- OD appointment created → stop engine (requires OD API polling or webhook)
- TCPA compliance analytics card (nice-to-have dashboard add-on)
- Google Ads Phase 3: per-ad-creative level tables (`gads_ads`, `gads_ad_metrics`)
- Two-way SMS Inbox (major feature, separate project)
