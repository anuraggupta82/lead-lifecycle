# Session Summary — July 5, 2026
## Attribution/Tracking Deep Investigation + CallRail Website Pool Fix

---

## What triggered this session

Dr. Gupta paused the "Emergency Dentistry" ad group before leaving for vacation on June 11 — it never synced back as paused on the dashboard. He also flagged nxtsmile.com had only 4 form submissions and zero calls in over a month of running. Both symptoms led to a much broader attribution audit.

---

## Findings (root-caused, not yet all fixed)

**1. OpenDental scheduler attribution pipeline broken.** Commit `6e4d671` ("removed attributes from appointments," May 19) deleted `_build_attr_marker()` from `stripe_router.py`. This function used to write gclid/UTM info into OpenDental appointment notes, which the marketing dashboard's nightly sync grepped to attribute bookings. Since May 19, **no booking has attribution written to OD notes** — the marketing dashboard's GAds funnel is effectively blind to real bookings. The scheduler's own `posted_appointments` table still correctly stores this data (via `_sanitize_attribution()`), it's just never been repointed to feed the dashboard.

**2. nxtsmile.com attribution capture is broken client-side.** `index.html` captures gclid/UTM into memory but never persists it (no cookie/localStorage) — any visitor who navigates to a second page before converting loses attribution entirely. `contact-request/index.html` has **zero** attribution-capture code at all. This was root-caused via a real case: Claire Richards, an actual paying nXtsmile patient, has zero gclid/UTM on her lead record despite genuine ad spend driving traffic.

**3. Major correction to the "funnel is fine" narrative.** Of 198 CallRail leads over 90 days, 158 were genuinely new callers who produced **zero** treated patients and $0 revenue. All $249,677 in "revenue" and all 17 "treated patients" in the dashboard's funnel stats were actually **existing patients** miscounted as new, due to a timing gap in the existing-patient guard (`callrail_webhook.py`) — the guard only catches OD matches that resolve before/at call time, but `od_matched_at` is often days later, so leads get created and never retroactively excluded.

**4. CallRail website pool wasn't swapping for non-Google-Ads traffic.** See fix below.

**5. No scheduled sync job pulls campaign status (pause/enable) from Google Ads back into the dashboard** — this is why the Emergency Dentistry pause never showed up. The working sync logic exists (`main.py:7762`, mapping at 7800-7827) but is only triggered manually, never on a schedule.

A full PR plan for all of the above is saved at `GDC_Attribution_PR_Plan_2026-07-05.md` in this folder — Phase 0 (no-code fixes), Phase 1 (3 parallel engineering PRs), Phase 2 (smaller follow-ups). **Nothing in that plan has been executed yet** except the CallRail piece below, which was a Phase 0 item.

---

## CallRail fix — executed today

**Problem:** the "GDC Website Pool - Google Ads" pool (4 numbers, created June 2) had its "Tracking sources" filter set to "Google Ads only." Any visitor not arriving via a Google Ads click — including all nxtsmile.com traffic types and organic/direct graftondentalcare.com visitors — saw no number swap, so calls from that traffic were untrackable and keyword data was lost.

**Fix:** Broadened the pool's tracking source from "Visitors from Google Ads" to "Track all visitors (recommended)." Saved via CallRail UI.

**Verified live, same session:**
- graftondentalcare.com (no referrer) → displayed 508-619-1411
- nxtsmile.com (with test gclid/UTM params) → displayed 508-501-8165

Both confirmed via CallRail's API (`trackers.json`) as real numbers belonging to the pool's 4-number set (+15084606344, +15085018165, +15086191411, +15089065447). Swapping is live and working correctly on both sites.

**Final confirmed CallRail configuration — 6 numbers total, no new number purchased:**
1. Website pool (4 numbers) — shared between graftondentalcare.com and nxtsmile.com, now tracking all visitors
2. Google My Business — 508-690-8583, unchanged
3. GAds Call Extension — 508-321-5428, unchanged

This matches Dr. Gupta's originally intended design (2 shared pool numbers minimum, GBP separate, call extension separate) — the pool being 4 numbers instead of 2 is a CallRail platform minimum for any pool, not a deliberate expansion.

**Still open:** why the filter got narrowed to Google-Ads-only in the first place is unconfirmed. Circumstantial timing lines up with the CallRail trial-to-paid billing transition, but this is not proven. No further action planned unless swapping regresses again.

---

## Marketing dashboard database — found and fixed (later in session)

**Problem:** the `mcp__gdc-marketing__*` MCP tools (`get_ad_groups`, `get_campaign_performance`, `get_account_evaluation`) all started failing with `database disk image is malformed`.

**Diagnosis:** the local `pipeline.db` had a 42MB WAL (write-ahead log) file — almost as large as the 59MB main database file — indicating it hadn't been checkpointed in a long time. This MCP server runs as a local process on Dr. Gupta's Mac (per `marketing-mcp` setup docs), reading the real file directly; a sandbox-side copy of the same file passed integrity_check clean, which pointed at a live-file/stale-connection issue rather than confirmed corruption.

**Fix (executed by Dr. Gupta in Terminal):**
1. Quit Claude Desktop to release the file lock
2. Backed up `pipeline.db` → `pipeline.db.backup-2026-07-05`
3. `sqlite3 pipeline.db "PRAGMA wal_checkpoint(TRUNCATE);"` → returned `0|0|0` (clean checkpoint, no errors)
4. `sqlite3 pipeline.db "PRAGMA integrity_check;"` → returned `ok`
5. Relaunched Claude Desktop

**Result:** confirmed fixed — the MCP tools now return data instead of the corruption error. This was a stale/unmerged WAL, not real data corruption. No data was lost.

---

## New bug found: `get_campaign_performance` MCP tool under-reports spend

While checking whether Google Ads spend matches the dashboard, direct Google Ads API pulls were compared against two different dashboard surfaces:

- **The real product UI** (Reports & Campaigns → Campaigns tab, localhost:7070) reports spend correctly — e.g., nXtsmile Implants shows $4,146 in the UI vs $4,145.72 pulled directly from the Google Ads API for the same campaign. General Dentistry New Landing Page: $2,548 vs $2,547.95. Emergency Dentistry: $1,036 vs $1,035.56. These all match — **the live dashboard product is accurate.**
- **The `get_campaign_performance` MCP tool**, however, returned near-zero spend for the same campaigns ($0, $0, $45.28 total) — a genuine, previously undiscovered bug in that specific tool's query (likely in `marketing-mcp/tools/read_tools.py`), unrelated to the WAL/database issue above and unrelated to the attribution-capture problems found earlier. This bug was initially misdiagnosed (assumed to be gclid-gating matching the earlier attribution findings) — that explanation was wrong and was corrected once the real product UI was checked directly.

**Not yet fixed — added to the list of open items.** The actual `campaigns` table/UI is trustworthy for spend; the MCP tool specifically is not, until this is fixed.

---

## What's NOT done yet (from the full PR plan, plus new items from today)

- nxtsmile.com attribution persistence fix (localStorage-based capture across pages + fixing the blind contact-request form)
- OpenDental scheduler → dashboard sync repoint (read from `posted_appointments` instead of dead OD-note grep)
- Existing-patient retroactive exclusion from funnel/revenue stats
- Scheduled campaign-status pull-sync job (the actual fix for the Emergency Dentistry pause-sync issue)
- nxtsmile.com landing page CTA/scroll-depth fix
- reCAPTCHA modal fix on graftondentalcare.com/new-patient-special/
- Cross-check of `posted_appointments` vs OpenDental directly, to confirm how many real new-patient bookings exist that the broken dashboard sync simply never counted
- Fix the `get_campaign_performance` MCP tool bug (under-reports spend; real dashboard UI is unaffected)

---

## Corrections made during this session (for the record)

Several claims made during this session were wrong and were caught and corrected:
- A fabricated name ("Amrita") in an early draft of the Phase 0 list
- A reference to a CallRail support ticket that was never actually opened
- An initial theory (from Opus, based on a premise fed to it) that the shared 2-number pool design was architecturally unsound — direct inspection of CallRail's UI showed no domain-awareness limitation exists in the way assumed, and Dr. Gupta confirmed the original design had in fact worked and produced real keyword data
- Two PR-plan figures cited for "zero-conversion ad groups" ($2,863 and $354 spend) didn't match live Google Ads data when checked directly — the real zero-conversion outlier is "All-on-4 Implants Worcester County" ($3,122/190 clicks/0 conversions), not the two ad groups originally flagged
- An explanation that the `get_campaign_performance` MCP tool's low spend numbers were due to gclid-gating (tying it to the known attribution problem) — wrong; the real dashboard UI shows correct spend, so this is an isolated bug in that one tool, not a symptom of the broader attribution issue

Dr. Gupta flagged the volume of corrections directly. The common thread across them: conclusions stated before verifying against a live source (a screenshot, the real UI, a direct API pull) that was available and should have been checked first. This is a process/discipline issue (verify before asserting), not attributable with confidence to the model itself.

---

---

## Continued session (Jul 5, evening)

### Phase 1 Opus plan — completed

Opus produced a detailed file-by-file implementation plan for all 3 Phase 1 PRs. Plan is fully actionable. Key points:

- **PR 1 (nxtsmile attribution):** 2 files — `index.html` (replace in-memory `_trackingData` with sessionStorage first-touch persistence + async GA4 client_id capture) and `contact-request/index.html` (add sessionStorage read-back + wire tracking into POST body). All existing call sites inherit the fix automatically.
- **PR 2 (OD sync → dashboard):** Option B confirmed feasible — `SCHEDULER_API` and `SCHEDULER_ADMIN_PASSWORD` already in `.env`. Change `od_matcher.py` to call `GET /admin/bookings` instead of grepping OD notes, update `unified_od_sync.py` to use the new function. **One open question: confirm `/admin/bookings` accepts machine-to-machine auth (password/token) vs Google SSO only** before building.
- **PR 3 (existing-patient filter):** 2 files — `database.py` `get_campaign_stats()` CTE and `marketing-mcp/tools/read_tools.py` (4 query locations). All need `AND COALESCE(existing_patient, 0) = 0`. Revenue/treated numbers will visibly drop after deploy — expected and correct.

All 3 PRs are independent, no file overlaps, can be worked in parallel.

### pipeline.db direct-access crash — new rule

Querying `pipeline.db` directly from bash sandbox while the server is running causes a "Bus error: 10" crash on macOS (confirmed Jul 5). Rule: always use API endpoints or MCP tools for data queries when the server is live. Never open the file directly from the sandbox. Added to memory.

### Lead lookup — Carrie (Jun 22)

nXtsmile lead `lead_4df0b165e35765db` (Carrie, carrie.demko@healthyuclinics.com) has full gclid attribution: `EAIaIQobChMI...`, utm_source=google, utm_medium=cpc, campaign 23870298927, keyword="nuvia dental" (competitor conquest). One of only 4 leads with clean attribution out of 12 total. ga4_client_id still blank (PR 1 will fix).

---

**Next step:** Approve Phase 1 PR plan and begin implementation. Resolve the `/admin/bookings` auth question first (needed for PR 2 Option B). Phase 0 remaining: clear 9 pending optimizer actions + decide on budget reduction while PRs are in flight.

---

## Late session (Jul 5, ~11pm)

### GAds Only filter bug — fixed

`frontend/index.html` line 1584: all CallRail leads were passing the "GAds Only" filter regardless of whether they had a gclid/campaign — `source === 'callrail'` was treated as GAds attribution by definition. Fixed to require actual gclid or campaign_name on CallRail leads. One-liner change. Ready to push.

**Git summary:** "Fix GAds Only filter — exclude unattributed CallRail leads"
**Description:** CallRail leads without gclid/campaign_name were incorrectly passing the GAds Only filter. Now requires actual GAds attribution to qualify.

### Cross-session attribution gap identified

Current Phase 1 PR 1 uses `sessionStorage` (Opus spec), which only persists gclid/UTM within a single browser session. Doesn't cover:
- Return visitors (close browser, come back 3 days later) — attribution lost
- Cross-channel journeys (click GAds → visit nxtsmile.com → leave → find GBP → call) — original ad click gets zero credit

**Recommended upgrade to PR 1:** switch from `sessionStorage` to `localStorage` with a 30-day TTL for first-touch attribution. Single line change, covers the most common return-visit case.

Cross-channel (different device or channel) is architecturally harder and not solvable without a logged-in identifier or end-to-end phone tracking.

### Cookie/privacy consent — not required

localStorage attribution tracking does not require a cookie consent banner for a MA dental practice (GDPR N/A, CCPA small business exemption, HIPAA N/A for gclid/UTM alone). Google Ads ToS requires a privacy policy on the site disclosing analytics/advertising cookies — verify nxtsmile.com has one.

### Carrie (nuvia dental) — attribution confirmed

Lead `lead_4df0b165e35765db` has full gads_sync-resolved attribution: keyword="nuvia dental", ad group="All-on-4 Implants Worcester County", click cost=$19.10. Confirmed gads_sync resolves keyword_text from gclid correctly for nxtsmile leads. Sara's blank keyword is a one-off sync miss on her specific gclid, not a systemic bug. The `keyword=` URL param is redundant — gads_sync is the correct resolution path.
