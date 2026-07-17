# Session Summary — 2026-07-06

**Focus:** Full inheritance-style audit of the whole marketing platform, then a deep dive into call/gclid attribution, ending with two new master reference/planning documents. Planning-only session — **no application code was changed.**

## What we did

### 1. Whole-platform audit
- Ran 5 parallel Sonnet agents across all docs/memory + all code; verified findings against live Google Ads + OpenDental data (Opus); live click-through of the dashboard; external strategic review by Fable 5.
- Delivered: `GDC_Executive_Audit_2026-07-06.docx` (exec summary + action tracker + 90-day arc), `GDC_Technical_Appendix_2026-07-06.docx` (file:line evidence + risk register), `gdc_architecture.png`.
- Key verified facts: practice healthy (~$250K/mo, 74 new patients/90d); ad program spends ~$3.5K/30d with ~5 tracked leads, $0 attributed revenue, ~$710 CPL — largely a **measurement** problem. Root cause of recurring regressions = income/timezone math duplicated 8–10× across a ~36k-line untested monolith (~1% coverage, no CI).

### 2. Corrections to earlier claims (kept us honest)
- **OpenAI/Whisper transcription is BAA-covered and compliant** — the earlier "no BAA / HIPAA risk" finding was WRONG; corrected in docs + memory.
- "Attribution completely dead" was too absolute — the scheduler path works via `posted_appointments`; current `od_matcher` shows working tiered matching.
- **Keyword for call-extension calls IS achievable** via CallRail Call Details Forwarding (CDF) — my earlier "impossible for tap-to-call" was wrong (Gemini flagged it; verified plausible). Requires CDF config + a code change.

### 3. Functional reference
- Built `GDC_Functional_Reference_2026-07-06.docx` — plain, opinion-free, per-module (function / how it works today / intended design from owner's docs with citations / gaps), with owner-fill boxes. Replaces the "too editorial" first exec draft the owner rejected.

### 4. Attribution deep dive (the main technical thread)
- Agreed model: **gclid is the signal** (pool name is misleading — website pool tracks all visitors); **first-touch credit, store all touches, multi-touch indicator**; **only ad calls auto-processed**; **GBP ignored unless GAds first touch**; **existing patients recorded but excluded from new-patient conversions**.
- Traced gclid pass-through: nxtsmile + scheduler good; **graftondentalcare.com drops gclid** (no capture, plain scheduler links, no form webhook).
- **Root-caused a real bug via the DJL call:** the CallRail classifier (`callrail_webhook.py:130-146`) only recognizes `assignment_type=='gads_campaign'`, ignoring gclid AND missing the `gads_call_extension` enum → call-extension calls (highest-intent, e.g. an implant consult) tagged "Direct" → no `gads_call_id` → no campaign shown, though Google's report has it. Verified DJL = real active OD patient, correctly matched, "Scheduled" Aug 3.
- Confirmed via `get_campaign_phone_stats`: Google attributes call-extension calls to campaigns (General Dentistry 23, nXtsmile Implants 5 in 30d) — campaign is recoverable.
- Produced `GDC_Call_Attribution_Cleanup_Plan_2026-07-06.md` (planning only) with: three-number-type identification, classifier fix (gclid + call-extension enum), historical backfill, CallRail-CDF read step, CDF config checks, gclid pass-through fixes, gating, existing-patient rule, pagination/UI fixes, verification harness. Conversion upload explicitly deferred.

### 5. New master documents (this session's final deliverables)
- **`Marketing project details and status.md`** — comprehensive reference: overview, architecture, **resources & locations** (paths, DBs, MCP tools, credentials, accounts, URLs), all modules grouped (ad creation + AI optimization under Lead Lifecycle Dashboard), tracking/attribution, content, compliance, status dashboard, gotchas. Indexed with §-numbers.
- **`Plan.md`** — central plan/execution tracker: active work, backlog by area, sequenced roadmap, open decisions, registry of detailed plan files (incl. the attribution plan), recently-completed. Indexed.

## Decisions made
- Attribution model = first-touch + all touches + multi-touch flag.
- Only Google-Ads calls auto-transcribed; GBP ignored unless GAds first touch; existing patients attributed but not counted as new.
- Historical backfill added to the attribution plan (planning-only; **build not yet authorized**).
- Conversion upload deferred until attribution is clean.

## Open / next
- Owner to confirm: first-touch edge case; whether booking-intent ads point to graftondentalcare (needs WP work) or scheduler/nxtsmile.
- Do the CallRail↔Google CDF config checks (owner action in consoles).
- On go-ahead: build attribution Phase 1.1 (classifier) + 1.1b backfill (Sonnet builds, Opus verifies, git summary before push).

## Reference going forward
Start every future session from **`Marketing project details and status.md`** (facts/status) and **`Plan.md`** (what's being worked on). Update status doc on completion; add plans to Plan.md.
