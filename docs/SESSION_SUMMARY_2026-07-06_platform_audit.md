# Session Summary — 2026-07-06 — Full Platform Inheritance Audit

## Objective
New-CTO inheritance audit of the whole marketing platform (dashboard, scheduler, campaign engine, call analysis, MCP). Combed all docs/memory + all code via Sonnet subagents, verified against live data (Opus, anti-hallucination), live UI walkthrough, external review by Fable 5, delivered exec Word doc + action tracker + architecture diagram + technical appendix.

## Method
- 5 parallel Sonnet agents: (1) docs/memory/roadmap, (2) dashboard backend monolith, (3) attribution+sync+call analysis, (4) optimizer+MCP+scheduler, (5) regression root-cause + security/risk register.
- Opus live verification via GAds MCP + OpenDental analytics MCP + dashboard UI at localhost:7070.
- Fable 5 external strategic recommendations.

## Verified headline findings
- **Attribution dead**: ad-click marker stopped being written to OD notes on May 19 (scheduler commit 6e4d671); reader code went dead. Direct-API replacement path exists but silently no-ops if `SCHEDULER_API` unset.
- **Ads flying blind**: 30d = $3,551 spend, 5 leads, 1 showed, 0 treated, $0 attributed revenue, ~$710 CPL. Dashboard shows 14 all-time leads vs 74 real new patients/90d (OD). Optimizing toward "Local actions" (directions/menu views).
- **Dashboard calc bugs**: campaign-level income/ROI double-count (dedup fix never backported from keyword view, database.py:4470 vs 5064); CPL uses GAds conversions not real leads (main.py:8181); MTD pacing mixes host-clock vs UTC.
- **Call analysis half-working**: 93/586 transcribed, batch_nightly.py disabled (sys.exit(0)), 0/4 GAds calls matched. Uses OpenAI Whisper (no BAA) — HIPAA risk.
- **Optimizer risk**: kill switch is a no-op (writes ENABLED, confirmed live), no concurrency lock, TOCTOU approval race, get_account_evaluation crashes live ("no such table: gads_campaign_settings").
- **Architecture**: 36k-line untested monolith (main.py 15,269; database.py 10,994; ai_optimizer.py 9,978; index.html 20,530); income/timezone math duplicated 8-10x = root cause of "fix one, break another." ~1% test coverage, no CI gate.
- **Security**: secrets in committed docs/config, unauthenticated PHI server, 9 real patient names hardcoded (admin.py:616), plain-string admin auth across ~292 endpoints.
- **SPOF**: whole core stack on one Mac Mini, ~20 unmonitored cron jobs, nightly-only backup.
- **Scheduler** = crown jewel (Google SSO+JWT+allowlist) BUT sends false "confirmed" email when OD write fails (stripe_router.py:662).

## Anti-hallucination corrections
- Memory "nxtsmile zero attribution capture" → WRONG; capture is mature, real break is OD-note write.
- Memory "call analysis uses Vertex AI" → WRONG; uses OpenAI (HIPAA issue).
- Income double-count → verified in code but LATENT (live incomes ~$0).

## Deliverables (in gdc-apps/marketing/)
- `GDC_Executive_Audit_2026-07-06.docx` — non-technical exec summary + 12-item prioritized action tracker + 90-day arc + decisions needed.
- `GDC_Technical_Appendix_2026-07-06.docx` — file:line evidence, full risk register, root-cause tables.
- `gdc_architecture.png` — annotated current-state diagram.

## Next / awaiting owner decision
1. Approve pause/floor ad spend. 2. Approve 2-week stop-the-bleeding list (false confirmations, HIPAA audio, secrets, kill switch, monitoring). 3. Retire Mango → CallRail transcription. 4. Six-month feature freeze. 5. Consolidate duplicated math into one metrics module + ~20 tests.

No code changed this session (planning-mode audit only).
