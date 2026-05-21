# Session Summary — 2026-05-21 (afternoon) — CallRail Account Live + Gemini Scope Expanded

**Mode:** Planning + account setup
**Outputs:** Account info saved, plan updates applied to both Smart Pipeline Routing and CallRail Integration plans, memory updated.

---

## What Happened

Operator signed up for CallRail on the **$50/mo Call Tracking entry tier**. Northstar (first-run setup) flow completed with:

- Account ID: `431682122`
- Company ID: `340886676`
- First tracking number: **`+15085459356`** (508-545-9356)
- Call forwarding target: **`5083184477`** (the real office line)
- Text forwarding: not configured at signup
- **Record + transcribe: ENABLED** ("Recommended" toggle left ON)
- Recording disclosure: "This call may be recorded and shared with third-party providers."

Operator additionally requested a **scope expansion**: the Gemini classifier (PR 1 of the Smart Pipeline Routing plan) should also produce **sentiment, outcomes, and keywords** — the same signals CallRail's Conversation Intelligence add-on ($45/mo) would have provided. Since Mango already transcribes locally and Gemini is cheap, doing it in-house is the right move.

---

## Impact of Enabling Record + Transcribe

This decision **locks in BAA Path A**. CallRail is now storing call audio + transcripts that may contain PHI (patient names, medical complaints, insurance details). Path B (no-BAA mode) is no longer viable unless recording is turned off again.

**Action required:** confirm BAA is signed with CallRail before any meaningful call volume flows through `+15085459356`. CallRail offers the BAA free at signup; it's likely already part of the workflow but worth verifying in Account Settings.

---

## Plan Updates Applied

### Smart Pipeline Routing PR 1 — scope expansion
Originally produced only `{follow_up_needed, reason, confidence}`. Now produces:
```json
{
  "follow_up_needed": "yes|no|unclear",
  "follow_up_reason": "callback_requested",
  "confidence": 0.92,
  "sentiment": "positive|neutral|negative|mixed",
  "sentiment_score": 0.0..1.0,
  "outcome": "appointment_booked|callback_scheduled|info_provided|wrong_number|voicemail|no_match|patient_existing|price_shopping|insurance_not_accepted|other",
  "keywords": ["implant", "tomorrow", "free consultation"]
}
```

Single Gemini call returns all signals — cheaper than separate calls and ensures internal consistency (a negative-sentiment + appointment-booked outcome becomes a useful flag for operator follow-up).

**Schema additions to `mango_calls`:**
- `sentiment` (enum text)
- `sentiment_score` (real, 0.0–1.0)
- `outcome` (enum text)
- `keywords` (JSON array)

**Updated acceptance criteria:**
- `follow_up_needed` ≥90%, `sentiment` ≥85%, `outcome` ≥90% on hand-labeled set
- Latency <5s/call (was <3s)
- Cost <$0.02/call (was <$0.01)

### CallRail plan — no plan-level changes (already correct at $50 tier); account-specific info moved to new memory entry `project_callrail_account.md`

---

## Files Touched This Sub-Session

| File | What Changed |
|---|---|
| `SMART_PIPELINE_ROUTING_PLAN.md` | §4.1 schema expanded with 4 new columns; §5 PR 1 expanded scope, prompt, acceptance criteria |
| `PROJECT_STATUS.md` | CallRail account details added; Gemini scope expansion noted |
| memory: `project_callrail_account.md` (NEW) | Account IDs, first number, forward target, recording posture, BAA requirement |
| memory: `project_smart_pipeline_routing.md` | Schema + PR 1 description updated |
| memory: `MEMORY.md` | Added pointer to new CallRail account memory entry |

---

## Cost Picture Updated

Original plan estimated $70–85/mo. Decision to enable recording + transcription doesn't change CallRail cost (recording is included in $50 tier; CallRail Conversation Intelligence is what would have cost $45 extra — we are NOT enabling that, we're doing it ourselves with Gemini).

Gemini incremental cost: ~$0.02/call × ~500 calls/month = **~$10/month**. Compares to CallRail Conversation Intelligence at $45/month → **net savings ~$35/month** by doing it in-house, plus we get keywords and follow-up-needed routing which CallRail doesn't provide.

---

## Open Items for Anurag

1. **Verify BAA is signed with CallRail** (Account Settings → Compliance). If not auto-signed during northstar setup, request and sign now.
2. **Generate CallRail API v3 key** → save to `_CREDENTIALS_VAULT/callrail-api.json` along with account_id and company_id.
3. **Confirm `+15085459356` assignment** — which campaign or source should it represent? Suggest assigning to a pilot Google Ads campaign first so we can validate the ingestion end-to-end before rolling out more numbers.
4. **PR 1 spec drafting** — ready to draft `PR7_SPEC_gemini_call_intelligence.md` whenever you greenlight it.

---

## Next Action

Wait for operator decision on PR 1 spec drafting + API key generation. No code yet — still planning mode.
