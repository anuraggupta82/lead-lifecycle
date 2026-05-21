# Session Summary — 2026-05-21 — Smart Pipeline Routing + CallRail Plans

**Mode:** Planning (no code written)
**Outputs:** 2 plan documents, 2 memory entries, 1 index update, 1 session summary, 1 project status update

---

## What Happened

Started from prior-context observation that the lead pipeline ingests everything (Candice Chase example) while a test contact-form submission missed the pipeline entirely. Designed two coordinated planning documents:

1. **Smart Pipeline Routing** — rethink which leads belong in the pipeline at all.
2. **CallRail Integration** — establish per-source call attribution + dashboard-driven number management + Google Ads auto-placement.

The two are coupled: CallRail webhook ingestion will feed the routing model in Smart Pipeline Routing, and the Gemini classifier from Smart Pipeline Routing will classify CallRail-ingested calls.

---

## Decisions Locked

### Smart Pipeline Routing

- **Routing model:** three orthogonal gates — Enter DB / Enter Pipeline / Show in Default View
- **Three lead buckets:** Self-Serve (DB only) / Warm Lead (DB + Pipeline + view) / Informational (DB + optimizer feedback)
- **Signal hierarchy:** Gemini conversation signal > campaign rule > operator override
- **Schema additions:**
  - `mango_calls.follow_up_needed/reason/classified_at/version`
  - `campaigns.pipeline_default_visibility` + `auto_enter_pipeline_rule`
  - new `pipeline_entry_audit` table for routing-decision audit
- **PR sequence:** classifier → schema+wizard → UI filters → booked-stage entry → existing-patient guard + optimizer feedback

### CallRail Integration

- **Plan tier:** Call Tracking entry — $50/mo. All-in ~$70–85/mo (vs Liine $199/mo, ~60% reduction).
- **HIPAA BAA:** recommended but **not strictly required** if recording/voicemail/transcription stay disabled on CallRail's side. Mango handles all of that locally. Two paths documented: Path A (sign BAA, recommended) vs Path B (strict no-recording mode).
- **Numbers are config, not transactions** — live under Campaign Settings → Tracking Numbers
- **Schema:** `callrail_numbers` (config) + `callrail_calls` (events with Mango cross-link)
- **GAds auto-placement:** assigning a number to a campaign pushes a call extension to Google Ads with read-back verification
- **Webhook ingestion** with polling fallback for the first 30 days (and as a permanent belt-and-suspenders if tunnel goes down)
- **PR sequence:** API client/sync → UI → GAds push → webhooks → linking/lead creation → DNI + cleanup

---

## Files Created This Session

| File | Purpose |
|---|---|
| `gdc-apps/marketing/lead-lifecycle/SMART_PIPELINE_ROUTING_PLAN.md` | Full 5-PR plan + schema + open design decisions |
| `gdc-apps/marketing/lead-lifecycle/CALLRAIL_INTEGRATION_PLAN.md` | Full 6-PR plan + setup steps + cost model + risks |
| `gdc-apps/marketing/lead-lifecycle/session_summary_2026-05-21_pipeline_and_callrail_plans.md` | This summary |
| memory: `project_smart_pipeline_routing.md` | Memory entry for pipeline plan |
| memory: `project_callrail_integration.md` | Memory entry for CallRail plan |
| `PROJECT_STATUS.md` | Updated project status snapshot (this session) |

`MEMORY.md` index also updated with two new entries under "Lead Lifecycle App."

---

## Open Items for Anurag

### Smart Pipeline Routing
1. Approve the plan?
2. Pick first PR to spec: PR 1 (Gemini classifier) is recommended.
3. Resolve open design questions §6 (post-visit scope, missed-call handling, shadow pipeline, classification-change-after-entry, optimizer aggressiveness, awaiting-callback as stage vs tag).

### CallRail Integration
1. Approve the plan?
2. Sign up for CallRail (Call Tracking $50/mo tier) — decide BAA Path A (sign it) vs Path B (skip + enforce no-recording mode).
3. Generate API key → save to `_CREDENTIALS_VAULT/callrail-api.json`.
4. Confirm forward target (`508-839-5566`?), receptionist whisper preference, after-hours behavior, DNI pool size, multi-location scope.

### Adjacent Items (not blocking these plans)
- Cloudflare tunnel for public webhook URL (needed for CallRail webhook reliability + GoDaddy form ingestion).
- Practice Cafe to verify AW-360307486 removal.
- Google Ads call conversion number-swap snippet (will be obsoleted by CallRail PR 3).

---

## Next Action

Wait for Anurag's go-ahead on either plan. When ready:
- For Smart Pipeline Routing: draft `PR7_SPEC_gemini_follow_up_classifier.md`.
- For CallRail: confirm BAA signed + API key in vault, then start PR 1 read-only sync.

No git push needed — planning docs only, no code.
