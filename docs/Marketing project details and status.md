# Marketing Project — Details & Status (Master Reference)

**Grafton Dental Care**  ·  Last updated: 2026-07-06

> **Purpose of this document.** Single source of truth for the entire marketing effort — what each part is, how it works, where it lives, and its current status. Read this first in any new session so we don't re-research from scratch. Detailed *plans* live in `Plan.md` (and topic plans it references); this document holds *facts & status*. When work completes, update the status here; when we plan work, add it to `Plan.md`.
>
> **Navigation.** These are markdown files (no fixed pages). Use the numbered **Index** below; each §-number is a heading you can search for. For true page numbers, export to PDF/Word on request.
>
> **Conventions.** [WORKS] = verified working · [PARTIAL] = works but incomplete/buggy · [BROKEN] = not working · [PLAN] = designed, not built. Code claims are from source; where a fact is external (WordPress/CallRail console) it is marked.

---

## Index

- **§1 Project Overview & Goal**
- **§2 Architecture & Environments** (where everything runs)
- **§3 Resources & Locations** (paths, databases, MCP tools, credentials, accounts, URLs) — *the reference future sessions need*
- **§4 Lead Lifecycle Dashboard & Pipeline** (the hub)
  - §4.1 Pipeline / CRM · §4.2 Follow-up (Email/SMS) · §4.3 Google Ads Campaign Build (ad creation) · §4.4 AI Optimizer (ad optimization) · §4.5 Marketing MCP Server · §4.6 Attribution & OpenDental Sync · §4.7 Call Analysis (Mango)
- **§5 Online Scheduler** (visitgdc.com)
- **§6 Landing Pages & Website** (§6.1 nxtsmile.com · §6.2 graftondentalcare.com)
- **§7 Tracking, Call Tracking & Attribution** (CallRail / GA4 / Clarity + the attribution architecture)
- **§8 Content, SEO & Organic Strategy**
- **§9 Compliance & Security** (PHI/BAA, secrets)
- **§10 Current Status Dashboard** (at-a-glance table)
- **§11 Key Facts & Gotchas** (must-knows before editing)
- **§12 Related Documents**

---

## §1 Project Overview & Goal

**Ultimate goal:** a closed-loop dental marketing system. A prospect finds GDC via a Google ad (later Meta/others), phone, or web → is captured as a lead → tracked through book → show → treat in OpenDental → their real revenue is fed back to the ad platform so bidding learns which ads/keywords bring real patients. Largely automated, supervised by the owner, with AI as the engineering/analysis layer.

**Practice context:** Grafton Dental Care, single location (Grafton, MA), ~3,255 active patients, ~$205–400K/month production, ~74 new patients / 90 days. The practice grows largely on its own; paid marketing's job is to profitably add high-value cases (implants / full-arch, worth $3,500–$25,000+). **Strategic focus:** implant/full-arch content and campaigns.

**Owner:** Dr. Anurag Gupta (owner/dentist). Default working mode = **plan first, approve before executing**; Sonnet builds, Opus verifies; git pushes via GitHub Desktop (ask before push, provide git summary + description).

---

## §2 Architecture & Environments

| Layer | Runs on | Survives Mac Mini outage? |
|---|---|---|
| Lead Lifecycle dashboard + pipeline, AI optimizer, ~20 scheduled jobs | **Local Mac Mini**, `localhost:7070` | No |
| Marketing MCP server (AI-driven campaign mgmt) | **Local** (Claude Desktop) | No |
| Mango call analysis (transcribe/grade) | **Local** | No |
| Online scheduler | **Google Cloud Run** (us-east4, project `lab-case-manager`) | Yes |
| nxtsmile landing site | **Google Cloud Run** | Yes |
| graftondentalcare.com | **WordPress** (GoDaddy + Cloudflare) | Yes |
| Data: `pipeline.db` | Local SQLite on Mac Mini (nightly backup to Google Drive) | Backup only |
| Data: OpenDental | Practice server (queried via analytics MCP) | Yes |

**Single point of failure:** the Mac Mini hosts the whole local stack with no alerting and only nightly backups (a known risk — see `Plan.md`).

---

## §3 Resources & Locations  *(future-session reference)*

**Project root:** `/Users/anurag/Documents/Projects/gdc-apps`  ·  **Marketing folder:** `/Users/anurag/Documents/Projects/gdc-apps/marketing`

### §3.1 Code repositories
| What | Path | Notes |
|---|---|---|
| Lead Lifecycle dashboard | `marketing/lead-lifecycle/` | `backend/main.py` (~15k lines), `backend/database.py` (~11k), `backend/ai_optimizer.py` (~10k), `frontend/index.html` (~20k). Launch: **"Launch Pipeline.command"** → `localhost:7070` |
| Marketing MCP server | `marketing/marketing-mcp/` | `server.py` (41 tools), `tools/`, `decisions/` |
| Marketing engine (legacy) | `marketing/marketing-engine/` | No running code; superseded by lead-lifecycle |
| Online scheduler | `operations/opendental-appointment-scheduler/` | FastAPI + React; `backend/app/`, `frontend/`; Cloud Run |
| Mango call analysis | `operations/mango-call-analysis/` | Standalone app archived; active pipeline is inside lead-lifecycle |
| nxtsmile landing | `marketing/nxtsmile-landing-page-v1/` | Cloud Run; `index.html`, `contact-request/`, `backend/server.py` |
| graftondentalcare landing snippets | `marketing/new-patient-landing-page/`, `dentures-landing-page/`, `emergency-landing-page/` | WordPress body fragments only; live site config is in WP admin |

### §3.2 Databases & access rules
- **`pipeline.db`** — local SQLite (`lead-lifecycle/backend/pipeline.db`). **DO NOT** open with sqlite3/Python from the sandbox while the server runs (causes Bus error on macOS). **Use the dashboard API endpoints or the `gdc-marketing` MCP tools** instead.
- **OpenDental** — query via the `opendental-analytics` MCP (`run_sql_query`, `get_schema_info`). PHI is auto-scrubbed to tokens; pass a consistent `session_id` and use `reidentify_response()` when staff need real names. SQL gotcha: the tool rejects literal `%` and `REPLACE(`; use `RIGHT()`/`YEAR()`/`MONTH()` instead of `DATE_FORMAT`/`LIKE %`.
- **OD provider codes:** DOC1 = main dentist, DOC3 = Dr. Patel, HYG1 = hygienist, GDC = owner/admin.

### §3.3 MCP tools available
- **`gdc-marketing`** (41 tools, reads `pipeline.db` + Google Ads): `get_campaign_performance`, `get_campaigns_list`, `get_lead_data`, `get_search_terms`, `get_campaign_phone_stats`, `get_conversion_action_breakdown`, `get_account_evaluation`, `get_geo_performance`, `get_device_performance`, `get_keyword_*`, `get_clarity_metrics`, `get_ga4_metrics`, `get_decisions`, plus write/approve tools. (Note: server intermittently disconnects; reload via ToolSearch.)
- **`opendental-analytics`**: `run_sql_query`, `get_schema_info`, KPI/production/appointment tools.

### §3.4 Credentials & monitoring
- **Vault:** `/Users/anurag/Documents/Projects/_CREDENTIALS_VAULT/` (GCP service accounts, API keys, tokens — unencrypted on disk; treat carefully).
- **GCP read-only monitoring:** `_CREDENTIALS_VAULT/gcp-cowork-monitor.json` (service account `cowork-monitor@lab-case-manager.iam.gserviceaccount.com`, org `graftondentalcare.com`). Projects: `lab-case-manager`, `dentastock-prod`, `marketing-landing-page-491721`, `mythic-producer-287915`. GitHub-triggered builds in `us-east4`; manual builds in `global`.

### §3.5 External accounts & URLs
| Service | Key IDs / URLs |
|---|---|
| Dashboard | `http://localhost:7070` |
| Scheduler | `visitgdc.com` (also `book.graftondentalcare.com`) — Cloud Run project `lab-case-manager` |
| Landing sites | `nxtsmile.com` (Cloud Run), `graftondentalcare.com` (WordPress) |
| Google Ads | Campaigns: General Dentistry New Landing Page `23849370858`, nXtsmile Implants `23870298927`, Emergency Dentistry, Brand awareness. Conversion account `AW-18046211904`. API v24 (venv Python 3.11; proto enums via `.name`). |
| CallRail | Account `431682122`, company `340886676`. Numbers: **Website Pool "GDC Website Pool – Google Ads"** = 508-460-6344 / 508-501-8165 / 508-619-1411 / 508-906-5447 (now tracks **all** visitors); **GMB** = 508-690-8583; **GAds Call Extension** = 508-321-5428. Forward target (office) = 508-318-4477. Recording provided by MangoVoice (CallRail recording intentionally off — HIPAA). CDF (Call Details Forwarding) noted active Jun 1. |
| GA4 | WPCode snippet #1801; `generate_lead` event fires on form 1. **Data API** via SA `ga4-reader@marketing-landing-page-491721.iam.gserviceaccount.com` (key: `_CREDENTIALS_VAULT/marketing landing page service account key.json`, scope `analytics.readonly`). Property IDs: **nxtsmile 531016678, graftondentalcare 536128204, visitgdc 533672873** |
| Search Console | `sc-domain:nxtsmile.com` verified Jul 7; graftondentalcare.com also present. Same SA (`ga4-reader@…`) granted access → organic queries via `searchconsole.googleapis.com` (scope `webmasters.readonly`). Note: GA4 does NOT provide organic search terms — use GSC. nxtsmile GSC data populates ~Jul 10 |
| Clarity | via `get_clarity_metrics` MCP tool |

---

## §4 Lead Lifecycle Dashboard & Pipeline  *(the hub — most modules live under it)*

Runs locally on `localhost:7070`. Backend is a large monolith (`main.py`/`database.py`/`ai_optimizer.py`); frontend is a single `index.html` SPA. Nav tabs: Inbox, Dashboard, Pipeline, Reports & Campaigns, Workflows, Call Analysis, Tracking #s, AI Optimizer, Admin.

### §4.1 Pipeline / CRM  — [PARTIAL]
**Purpose:** worklist tracking each lead new → auto-nurture → scheduled → no-show → showed → treatment presented → accepted → completed → cold (backward moves blocked except no-show/cold).
**Status:** works; data quality is weak (leads with business-name/city caller-IDs, malformed phones); only ~14 leads captured all-time vs 74 real new patients/90d → capture gaps upstream (see §7).
**Key files:** `database.py` (`LIFECYCLE_STAGES` ~1094), pipeline endpoints `main.py:11913`, frontend Kanban `index.html:1476+`.

### §4.2 Follow-up (Email/SMS)  — [PARTIAL]
**Purpose:** multi-touch nurture (email + SMS) on days 0,1,3,7,14,21,30; 9am–6pm ET window; stops at scheduled+; TCPA STOP/opt-out engine.
**Status:** engine runs (15-min tick); de-dupe table exists; reply-gate/stop logic present. Mix of legacy hardcoded templates + newer DB-driven workflow templates; SMS booking link still a TODO for a tracked short URL.
**Key files:** `follow_up_engine.py`, stop engine `main.py:~4161+`, `stop_engine.py`.

### §4.3 Google Ads — Campaign Build (ad creation)  — [WORKS]
**Purpose:** 8-step wizard (Strategy, Competitors, Keywords, Ad Copy, Ad Groups, Launch, Performance, Geo) that builds & launches a real Search campaign (budget → campaign PAUSED → 15-mi geo → ad groups/keywords → RSA with auto-heal on policy rejection → extensions → ENABLE → URL verify).
**Status:** functional; ad copy drafted by Claude; `GOOGLE_ADS_RULES` enforced. Live account currently small and under-performing (see §10).
**Key files:** wizard `index.html:8774+`, build/launch `main.py:5401+`, `google_ads_create.py`.

### §4.4 AI Optimizer (ad optimization)  — [PARTIAL / RISKY]
**Purpose:** daily 7am (and on-demand) review; per-campaign = Claude Opus, account-level = Sonnet; proposes bids/budgets/negatives/ad-copy for **human approval** (nothing auto-executes; approval queue in `gads_audit_log`).
**Status:** approval queue + post-mutation read-back verification work. Risks: the "kill switch" is a no-op (writes always enabled — `campaign_safety.py`), no concurrency lock between manual + cron runs, and `get_account_evaluation` currently errors live ("no such table: gads_campaign_settings"). Guardrails that do hold: +25%/op budget cap, $5–$500/day bounds, NEVER_AUTOMATE list.
**Key files:** `ai_optimizer.py`, approval endpoints `main.py:1855+`.

### §4.5 Marketing MCP Server  — [PARTIAL]
**Purpose:** lets Claude manage marketing conversationally (query performance/leads, create/launch campaigns, approve optimizer actions); logs strategic "decisions" that get injected into future optimizer prompts.
**Status:** 41 tools live; reads `pipeline.db` directly, writes via dashboard API. The documented two-flag write-gate is currently a no-op in code (returns enabled). Docs list ~19 tools (stale).
**Key files:** `marketing-mcp/server.py`, `tools/`, `decisions.py`.

### §4.6 Attribution & OpenDental Sync  — [PARTIAL]  *(see §7 for the full attribution architecture)*
**Purpose:** nightly 10pm chain: pull leads → gclid→keyword → OD patient match → call classification → refresh call income → OD payments → call→keyword attribution → call production log → upload conversions to Google Ads.
**Status:** scheduler-booking attribution reads directly from `posted_appointments` (works); the old OD-note "ATTR:" path is dead but superseded. Call attribution has gaps (see §7). Income math is computed in several places and disagrees (double-count risk at campaign level; estimated-vs-actual not flagged).
**Key files:** `unified_od_sync.py`, `od_matcher.py`, `od_payment_sync.py`, `call_keyword_attribution.py`, `google_ads_conversions.py`.

### §4.7 Call Analysis (Mango)  — [PARTIAL]
**Purpose:** transcribe (Whisper) + grade (Gemini/Vertex) inbound calls; match to lead/patient; surface on the Call Analysis tab.
**Status:** transcribes **all** answered inbound calls (not just ad calls) — over-processing; nightly batch has been disabled at times; call→campaign attribution frequently blank (see §7). Call Analysis list is capped at 200 rows with no pagination.
**Compliance:** transcription uses OpenAI Whisper + Gemini-via-Vertex — **BAA in place with both Google and OpenAI; this is compliant** (do not flag as a risk).
**Key files:** `mango_pipeline.py`, `mango_service.py`, calls endpoints `main.py:10819+`, `database.py` (`get_calls_needing_processing` ~9479).

---

## §5 Online Scheduler (visitgdc.com)  — [WORKS]  *(strongest component)*

**Purpose:** public booking page; shows real OD availability, optional Stripe deposit, writes appointment into OpenDental with read-back verification. Captures `gclid/gbraid/wbraid/utm/ga4_client_id` and persists to `posted_appointments` (pipeline pulls it).
**Status:** solid. Real Google SSO + JWT + domain allowlist on admin (the template other modules should follow). **Known bug to fix:** can email a "confirmed" booking even if the OD write failed (`stripe_router.py`); no double-submit guard on the free path; some timezone mixing (ET vs machine clock).
**Key files:** `scheduler/frontend/src/utils/attribution.js`, `backend/app/routers/stripe_router.py`, `od_matcher.py` (`sync_scheduler_posted_appointments`).

---

## §6 Landing Pages & Website

### §6.1 nxtsmile.com  — [WORKS]
Full All-on-X funnel (smile tool, chatbot, lead form). Captures `gclid/fbclid/msclkid/utm/ga4_client_id` (localStorage, 30-day first-touch), appends real gclid params onto scheduler links and form payloads, fires Google + Meta (Pixel + CAPI) conversions. LCP tuned to ~2.6s. **Key files:** `nxtsmile-landing-page-v1/index.html:2522-2635, 4313-4338`.

### §6.2 graftondentalcare.com  — [PARTIAL] *(WordPress)*
Migrated Practice Cafe → GoDaddy + Cloudflare (DNS flipped May 13). Landing pages are WordPress with Gravity Forms. **Gaps:** no in-repo gclid capture; outbound "Book" links to the scheduler carry no gclid; Gravity Forms may lack hidden gclid/utm fields and there's no webhook from these forms into the pipeline. Blog/organic content published here (see §8). **Work tracked in `Plan.md`.**

---

## §7 Tracking, Call Tracking & Attribution

### §7.1 Tools
- **CallRail** — DNI number-swap + call attribution. Website pool now tracks all visitors (fixed Jul 5). Google Ads integration + Call Details Forwarding (CDF) can supply campaign **and** keyword for ad calls.
- **GA4** — multi-property; `generate_lead` event; scheduler captures GA4 client id.
- **Microsoft Clarity** — session behavior; pulled nightly into `pipeline.db`, read via MCP.

### §7.2 The attribution model (agreed design)
- **gclid is the signal** for website traffic — never infer "Google Ads" from a pool number (the website pool tracks all visitors).
- **First-touch credit**, store all touches, show a multi-touch indicator (use CallRail's journey).
- **Three tracking-number types:** Website Pool → identify by **gclid** (gives campaign + keyword); **Call Extension** (508-321-5428, tap-to-call, no gclid) → identify by the **number**, campaign from Google's call report and campaign+keyword from **CallRail CDF**; **GBP** → ignore unless Google-Ads first touch.
- **Only ad calls auto-transcribe/grade**; existing patients recorded but excluded from new-patient conversions.

### §7.3 Current status / known breaks  — [BROKEN in places]
- **Ad calls mis-tagged:** classifier (`callrail_webhook.py:130-146`) only recognizes `assignment_type=='gads_campaign'` — it **ignores gclid** and **misses the `gads_call_extension` enum**, so call-extension calls (often highest-intent, e.g. implant consults) are tagged "Direct," cascade to no `gads_call_id`, and show **no campaign** despite Google having it.
- **Over-processing:** transcription/grading gate is missing on `get_calls_needing_processing()` — all inbound calls processed, not just ad calls.
- **gclid pass-through gaps:** graftondentalcare.com doesn't capture/forward gclid; pool→pipeline linkage produced "0 matched to Mango."
- **Verified example (DJL, Jun 23):** real active OD patient, correctly matched, lead "Scheduled" Aug 3, but no Google campaign/keyword. Google's report shows the campaign exists (nXtsmile Implants). Campaign is recoverable; keyword only going-forward via CDF.
- **Full fix plan:** `GDC_Call_Attribution_Cleanup_Plan_2026-07-06.md` (referenced from `Plan.md`).

---

## §8 Content, SEO & Organic Strategy  — [ONGOING]

**Strategy:** 100% implant content for 12 months (Jun 2026–May 2027), ~1 long-form post/week (52 posts) to build topical authority; 5-pillar organic plan (content + Google Business Profile + service-area pages + backlinks + technical) aiming to shift the mix toward ~55% organic by month 18. Reusable long-form template (FAQ schema + MedicalWebPage E-E-A-T). Several implant posts drafted/scheduled. **Reference docs:** `ORGANIC_STRATEGY_YEAR1.md`, `project_blog_content_strategy` (memory), Blog Posts folder.

---

## §9 Compliance & Security

- **PHI / BAA:** BAA in place with **Google** (Vertex AI/Gemini) **and OpenAI** (Whisper). Call transcription is therefore compliant. OD analytics MCP scrubs PHI before it reaches the AI.
- **HIPAA & ad platforms (for later, when we do conversion upload):** Google permits hashed-PII Enhanced Conversions under its terms (no health/condition data, consented, server-side); **Meta has no BAA** and restricts health-category conversions — send only de-identified/click-ID signals to Meta. *Conversion upload is deferred until attribution is clean.*
- **Security backlog (from audit):** secrets committed in some docs/config (rotate + vault), unauthenticated endpoints, single shared admin password on the dashboard, no CI/tests (~1% coverage). Tracked in `Plan.md`.

---

## §10 Current Status Dashboard

| Area | Status | One-line |
|---|---|---|
| Scheduler | [WORKS] | Cloud-hosted, secure, writes to OD; fix false-confirm email |
| nxtsmile landing | [WORKS] | Captures + forwards gclid correctly |
| Campaign build wizard | [WORKS] | Builds/launches real campaigns |
| AI optimizer | [PARTIAL] | Approval queue works; kill switch no-op; eval tool errors |
| Pipeline/CRM | [PARTIAL] | Works; capturing few leads; data quality weak |
| Follow-up email/SMS | [PARTIAL] | Runs; template split; SMS link TODO |
| Call analysis | [PARTIAL] | Over-processing; attribution blank; 200-row cap |
| Attribution (calls) | [BROKEN] | Classifier ignores gclid + misses call-extension enum |
| gclid pass-through | [PARTIAL] | Good on nxtsmile/scheduler; broken on graftondentalcare |
| graftondentalcare site | [PARTIAL] | No gclid capture/forward; form not webhooked |
| MCP server | [PARTIAL] | 41 tools; write-gate no-op; intermittent disconnects |
| Content/organic | [ONGOING] | Implant content engine on schedule |
| Live ad performance | [ATTENTION] | ~$3.5K/30d, few tracked leads, $0 attributed revenue, ~$710 CPL — largely a measurement problem |

---

## §11 Key Facts & Gotchas  *(read before editing)*

1. **`pipeline.db`** — no direct sqlite/Python access while the server runs (Bus error). Use API/MCP.
2. **OD analytics SQL** — no literal `%`, no `REPLACE(`; use `RIGHT()`, `YEAR()`, `MONTH()`.
3. **Timezone** — all OD-bound dates must use `America/New_York`, not naive/UTC.
4. **Google Ads API** — v24, venv Python 3.11; read proto enums via `.name`; bidding via sub-message fields.
5. **gclid** — the attribution signal; the "Google Ads" pool name is misleading (tracks all visitors).
6. **Call-extension calls** — no gclid; identify by number; keyword only via CDF (going forward), campaign via Google's call report.
7. **PHI** — BAA with Google + OpenAI; do NOT re-flag transcription as non-compliant.
8. **Regressions** — income/timezone math is duplicated across ~8–10 places; fixing one spot often leaves siblings wrong. ~1% test coverage, no CI.
9. **Git** — pushes via GitHub Desktop; ask before push; provide git summary + description.
10. **Monolith sizes** — `main.py` ~15k, `database.py` ~11k, `ai_optimizer.py` ~10k, `index.html` ~20k lines.

---

## §12 Related Documents

- **`Plan.md`** — central plan & execution tracker (start here for what's being worked on).
- **`GDC_Call_Attribution_Cleanup_Plan_2026-07-06.md`** — detailed call/gclid attribution fix plan.
- **`Marketing Platform — Functional Reference` (`GDC_Functional_Reference_2026-07-06.docx`)** — plain per-module reference this document builds on.
- **`GDC_Executive_Audit_2026-07-06.docx`** + **`GDC_Technical_Appendix_2026-07-06.docx`** — inheritance audit (exec + evidence/risk register).
- **`ORGANIC_STRATEGY_YEAR1.md`**, blog strategy docs — content plan.
- Session summaries: `SESSION_SUMMARY_2026-07-06.md` (today) and prior dated summaries.
- Memory index: the space's `MEMORY.md` (per-topic detail files).
