# Lead Lifecycle — Project Status

**Last updated:** 2026-05-21
**Maintainer:** Anurag

---

## What's Live Today

- **Lead lifecycle dashboard** running locally on port 7070 (Flask/FastAPI + React)
- **Income attribution chain** (PRs 1–6) shipped May 20 2026 — unified OD sync, payment pull (365d + LTV), refresh call income, ad-group/keyword income parity, modal with patient + income data
- **AI optimizer** with per-campaign + account-level Claude, Google Recs integration, lifecycle awareness, budget constraint, recommendation reclassification
- **Search term semantic classifier** (Haiku pre-pass) — negative/keep/conquest
- **SKAG attribution system** — 6-PR system shipped, Opus-reviewed
- **Sitelinks + structured snippets + callouts** — full extension fetch/Claude/validate/execute loop
- **Geo targeting** with 17-town MA proximity table
- **Attribution tracking** — visitgdc.com scheduler → OD Note ATTR: marker → nightly sync → lead creation
- **GA4 tracking** live on graftondentalcare.com (WPCode snippet #1801, multi-property reporting)
- **Marketing MCP server** — 26 tools, decisions system, optimizer injection (Opus-reviewed May 19 2026)

## In-Flight

| Item | State |
|---|---|
| **Twilio A2P 10DLC** | New campaign QE2c6890... IN_PROGRESS pending TCR review (filed May 15) |
| **graftondentalcare.com migration** | DNS flipped May 13; pending: UpdraftPlus, reCAPTCHA, cancel Practice Cafe |
| **Doctor Dental Care WP tracking** | Waiting on WP credentials from marketing company |
| **LSA integration** | BLOCKED on Google verification (license/insurance/background check) |
| **Call list filters** | Queued — revert narrow patient-match filter + add stackable checkboxes |
| **Dentures landing page** | Published May 10; live |

## Planning Phase (No Code Yet)

### Smart Pipeline Routing — 5-PR plan documented 2026-05-21
- See `SMART_PIPELINE_ROUTING_PLAN.md`
- Three-bucket lead model + Gemini follow-up classifier + per-campaign filters
- PR 1 (classifier) is the load-bearing piece
- Awaiting operator approval before drafting `PR7_SPEC_gemini_follow_up_classifier.md`

### CallRail Integration — 6-PR plan documented 2026-05-21 (updated)
- See `CALLRAIL_INTEGRATION_PLAN.md`
- Dashboard number management → GAds auto-placement → webhook ingestion
- **Plan tier:** Call Tracking entry — $50/mo; all-in ~$70–85/mo (vs Liine $199/mo, ~60% reduction)
- **Account live (2026-05-21):** account_id=`431682122`, company_id=`340886676`
- **First tracking number:** `+15085459356` (508-545-9356) — to be assigned via PR 2
- **Default forward target:** `5083184477`
- **Recording + transcription ENABLED** in CallRail (operator choice 2026-05-21) → **BAA Path A is required** (must sign before meaningful volume)
- Awaiting: API key generation + BAA confirmation before PR 1 builds out

### Gemini Call Intelligence — PR 1 scope expanded 2026-05-21
- Originally just `follow_up_needed` flag; now produces sentiment + outcomes + keywords too in a single Gemini call
- Replaces CallRail's $45/mo Conversation Intelligence add-on
- See `SMART_PIPELINE_ROUTING_PLAN.md` §4.1 and §5 PR 1 for full spec

### Geo Intelligence — 7-PR plan documented earlier May 2026
- AI geo targeting: radius/zip optimization per campaign type
- Opus plan saved

### AI Campaign Vision (deferred)
- Opus strategy + Sonnet agent in Google Ads
- Deferred until lead workflow complete

## Open Bugs / Adjacent Issues

- **localhost:7070 webhook from GoDaddy** — unreachable, breaks form-fill pipeline ingestion. Cloudflare tunnel is the candidate fix; also unblocks CallRail webhooks.
- **AW-360307486** — stray Practice Cafe Google Ads tag; vendor confirmation pending.
- **Google Ads number-swap snippet** — never installed; CallRail PR 3 will obsolete the need.
- **2 pre-existing test failures** in `test_unified_od_sync.py` (7 vs 8 steps) — not regressions.
- **Same-patient multi-call dedup** at campaign rollup — Opus-flagged from PR 4, queued.
- **Legacy `call_income` column** — drop from UI in ~2 weeks once KPL parity verified.

## Key Architectural Decisions in Memory

The full architecture is captured in memory (see `MEMORY.md` index). Recent additions:
- **Smart Pipeline Routing** memory entry (2026-05-21)
- **CallRail Integration** memory entry (2026-05-21)
- **Income Attribution PRs 1–6** detailed entries (2026-05-20)
- **Optimizer income_high mode** switched in PR 6 (2026-05-20)

## Next Decisions Required From Operator

1. Approve Smart Pipeline Routing plan? If yes, draft PR 7 spec (Gemini classifier).
2. Approve CallRail Integration plan? If yes, signup + BAA + API key, then start PR 1.
3. Confirm Cloudflare tunnel approach for public webhook URL.
4. Resolve 6 open design questions in Smart Pipeline Routing §6.
5. Answer 5 open operator questions in CallRail §14.

## Standing Project Conventions

- Default mode: **planning** — plan before executing
- Sub-agents preferred where useful; Sonnet default, Opus for verify/fix after code changes
- Git pushes via **GitHub Desktop** — Claude provides git summary + description only, not full push commands
- Session summaries created after every meaningful step
- Project memory updated as work progresses
- Marketing folder: `/Users/anurag/Documents/Projects/gdc-apps/marketing`
- Scheduler folder: `/Users/anurag/Documents/Projects/gdc-apps/operations/opendental-appointment-scheduler`
