# Marketing Project — Central Plan & Execution Tracker

**Grafton Dental Care**  ·  Last updated: 2026-07-18 (Session 19)

> **How this file works.** This is the single place to see *what we're working on and what's next* across the whole marketing effort. It pairs with **`Marketing project details and status.md`** (facts & current status). Flow: we **add plans here** → build (Sonnet builds, Opus verifies, ask before git push) → when done, **update the status doc** and mark the item `[DONE]` here. Big topics get their **own detailed plan file** (e.g. the attribution plan) — registered in §5 with its location, and summarized here.
>
> **Navigation.** Markdown (no fixed pages); use the numbered **Index** and §-headings. Export to PDF/Word for true page numbers on request.
>
> **Status tags:** `[NOW]` in progress · `[NEXT]` approved, up next · `[QUEUED]` planned, not scheduled · `[BLOCKED]` waiting on something · `[DONE]` complete · `[PLAN]` needs a detailed plan written.  **Priority:** P0 (safety/compliance/bleeding money) · P1 (high impact) · P2 (improvement).

---

## Index

- **§1 Active now**
- **§2 Backlog by area**
  - §2.1 Attribution & Tracking · §2.2 Lead Lifecycle Dashboard (calc/optimizer/MCP) · §2.3 Call Analysis · §2.4 Scheduler · §2.5 Landing Pages & Website · §2.6 Content & Organic · §2.7 Compliance & Security · §2.8 Platform Health
- **§3 Sequenced roadmap** (suggested order)
- **§4 Open decisions** (waiting on owner)
- **§5 Detailed plan files** (registry)
- **§6 Recently completed**

---

## §1 Active now

| Item | Status | Priority | Detail |
|---|---|---|---|
| Call attribution — **Builds 1 & 2 SHIPPED + backfill applied** | `[DONE — verify going-forward]` | P1 | Classifier fix (gclid + `gads_call_extension`) + direct `gads_call_view`→Mango time-match (±60s, RECEIVED) both built, Opus-verified, live. Backfill applied Jul 7: **24 historical ad calls attributed** to campaign+ad-group (incl. nXtsmile Implants → All-on-4 / Implant Dentist Near Me). Google match count **61 → 85** confirmed. **Not yet git-committed** — commit `config.py`+`main.py`+`mango_service.py` (see git summary in session notes). |

**⟲ DETOUR RESOLVED (Jul 7 evening) — resolver fixed.**

**➡ CURRENT SEQUENCE (owner-set, Jul 7):**
1. `[DONE Jul 7 — data pending ~2-3 days]` **nxtsmile.com verified in Google Search Console** (`sc-domain:nxtsmile.com`, added by owner Jul 7; Performance report "processing, check in a day or so"). Marketing SA `ga4-reader@marketing-landing-page-491721.iam.gserviceaccount.com` was **GRANTED access to the property Jul 7** → organic queries are pullable via the Search Console API (`searchconsole.googleapis.com` / `webmasters.readonly`, same key at `_CREDENTIALS_VAULT/marketing landing page service account key.json`) once data populates (~Jul 10). GA4 nxtsmile "organic/direct" is unreliable (test-traffic polluted: 522 "direct", DIAGTEST loads, test@test.com/DFWEWEF leads). REVISIT organic-query analysis in a few days once GSC data populates.
2. `[THEN]` **§2.1b — income / OpenDental matching** (make campaign INCOME & ROI real).
3. `[AFTER income]` **Fix graftondentalcare.com "New Patient Special" page errors** (owner noticed many errors on that page — see §2.5). We paused the revenue / OpenDental-matching workstream (§2.1b) to fix a keyword-attribution gap surfaced by lead "Andre." **KEY CONSTRAINT (owner confirmed Jul 7): campaigns are SEARCH-ONLY — no Performance Max, no Dynamic Search Ads, no Search Partners.** That means every gclid has a real keyword recoverable from Google's `click_view`. **➡ ONCE THE RESOLVER IS FIXED, RETURN to §2.1b (OpenDental matching / make campaign INCOME & ROI real) — that is the main workstream we stepped away from.**

**Next on attribution:**
- `[DONE Jul 7]` **Fixed gclid→keyword resolver.** Root cause = the daily/chain runs used a **7-day** `click_view` window, so weeks-old gclid leads were skipped → 0 resolved. Ran the **90-day backfill** (`/api/admin/gads-sync?days_back=90`) → **all 5 gclid leads resolved**, incl. Andre = "full mouth dental implants" (ad group Dental Implants Cost Comparison), carrie/Sara/Renee/Richard too. Permanent fix: widened the recurring windows 7→30 (`main.py:205,434`; `unified_od_sync.py:313`; restart to activate). Deeper gaps: run the 90d backfill button. Keyword lands in `keyword_text`/`search_term`/`ad_group`/`campaign` (note: separate `attributed_keyword` field still blank — display-wiring check for later). Original diagnosis: `sync_gclids_to_keywords` ("Google Ads Resolver", unified chain step 2) logged "0 gclids resolved" despite `click_view` pulling 119 gclids. Lead **Andre Brassard** (Jul 7, has gclid, attributed to nXtsmile Implants, booked) has NO keyword. Search-only ⇒ the keyword exists in Google's `click_view` for every gclid, so the resolver should fill it for ALL gclid leads/calls — this is the keyword layer that call-extension calls otherwise can't get. Investigate why 0: timing (Andre's click not yet in `click_view`?) or a matching/JOIN bug (`leads.gclid` ↔ `gads_clicks.gclid`).
- Context (gclid capture diagnosed Jul 7, read-only): landing-page capture WORKS — CallRail swap.js fires on graftondentalcare.com + nxtsmile.com, gclid survives, tracking template is clean (`{lpurl}?utm_source=…&keyword={keyword}`, no stripping). Form leads carry gclid (Andre proves it). nxtsmile persists `gclid` + `utm_term` but NOT the raw `keyword=` param → keyword only lives inside the `landing_url` string and is lost on navigation to the contact-request page (session staleness). ⇒ keyword must come from the RESOLVER, not the landing URL.
- `[NEXT]` Verify the **nightly reconcile** auto-attributes NEW ad calls via the direct match (note: no 24/7 automation yet — runs only when owner triggers on MacBook).
- `[BLOCKED — owner console]` **CallRail CALL gclid capture:** 0/267 calls in 30d have gclid/keyword in CallRail itself. NOT our sync and NOT the landing pages (swap.js + template ruled out Jul 7). Remaining causes: most calls are genuinely GMB/organic/call-extension (no gclid expected), PLUS a possible CallRail session-staleness issue (attr/session values didn't refresh on a fresh URL in the test — confirm with an INCOGNITO test-call). Call-extension calls have no web session → no gclid ever; their campaign comes from Google `call_view` (Build 2, working) or CDF (CallRail↔Google integration — verify owner-side).
- `[QUEUED]` Assign the **website DNI pool numbers** in Tracking #s (currently in "Reserve"). Call-extension number 508-321-5428 is correctly assigned.
- `[QUEUED]` Existing-patient handling: direct-match currently attributes campaign to all matched calls (records the ad touch); ensure existing patients are excluded from new-patient **conversion** counting downstream.

---

## §2 Backlog by area

### §2.1 Attribution & Tracking  *(detailed plan: `GDC_Call_Attribution_Cleanup_Plan_2026-07-06.md`)*
- `[NEXT]` **P1** Fix CallRail classifier — recognize gclid (website pool) AND `gads_call_extension` enum (call extension). *Root unlock.*
- `[QUEUED]` **P1** Historical backfill — re-match past ad calls to Google's call report for campaign credit (fixes ROI retroactively; campaign only, no keyword backfill).
- `[QUEUED]` **P1** Read CallRail CDF campaign/keyword for call-extension calls (code currently ignores them for this type).
- `[QUEUED]` **P0-config** Verify CallRail↔Google Ads integration + Google "send data to call-reporting provider" (CDF) are active; check Keywords column populates.
- `[QUEUED]` **P1** gclid pass-through fixes: graftondentalcare.com capture + decorate scheduler links + Gravity Forms hidden fields + webhook to pipeline (WordPress work).
- `[QUEUED]` **P1** Make CallRail journey (first-touch) the identification source of truth; store all touches + multi-touch flag.
- `[QUEUED]` **P2** Synthetic gclid test-click harness + reconciliation alert (so a disabled pool can't go unnoticed).
- `[QUEUED]` **P2** Conversion upload to Google/Meta — **DEFERRED** until attribution is clean; research what each platform permits for healthcare first. **Design decisions captured (Jul 7):**
  - **What value to upload = EXPECTED value per event, not actual.** Smart Bidding optimizes on averages over many conversions; the 0–$48k per-consult variance is handled by the law of large numbers. Upload EV = (implant revenue collected from booked consults over a period) ÷ (consults booked in that period) — an *unbiased* blended number (bakes in show-rate × close-rate × avg case). Illustrative: ~100 booked → ~55 show → ~18 treat @ ~$18k ≈ $3,200 per booked consult. Do NOT upload $0 or the max $48k. Over-valuing → Google overbids/wastes spend.
  - **Which event to optimize on = volume-vs-signal tradeoff.** "Booked/qualified consult" = most volume (Smart Bidding needs ~15–30 conv/mo) but noisy; "Showed" = cleaner + within the ~54-day window but sparser. At GDC's low implant volume, optimize on the earliest event with enough volume (likely booked/qualified consult) at blended EV; move to "showed" once show-volume is sufficient.
  - **Targeting upgrade:** differentiate value by treatment type known at booking (full-arch ~$8k EV vs single-tooth ~$2k) via separate conversion actions or conversion value rules → Google targets high-value case types/keywords, not just "any consult."
  - **Restatement to update value later — verified limits (Jul 7):** RESTATEMENT adjustment can change an uploaded conversion's value, but only within **~54 days** (hard cap), and it only meaningfully retrains bidding within **~7 days**; must wait 24h after the conversion; identify via **order_id** (set it on every upload). ⇒ Implant true value lands MONTHS later = outside the window, so **don't rely on late restatement for final case value** — upload the EV up front; use restatement only for short-cycle fixes (no-show retraction, near-term corrections).
  - **No-gclid leads (e.g. Claire):** use **Enhanced Conversions for Leads** (hashed email/phone; Google-side match) — same EV-value logic applies.
  - **Prereq:** the actual EV (blended + by treatment type) must come from real OpenDental funnel data → compute it as part of §2.1b income matching (see below).

### §2.1b Revenue layer / "wire #2" — make campaign INCOME & ROI real  🟡 **MOSTLY DONE — 2 items open**
> **Calculation DONE:** income = actual OD collected dollars, deduped per patient, new-patients-only, rolled to campaign ROI. Shipped: Fix #1 double-count dedup, Fix #2 existing-patient leak, Fix #4 payment-anchor timezone (Fix #3 was a misdiagnosis — income already collected-only); APPTS/CALLS column reworked to deduped new-patients-scheduled (form∪call) + drill-down → patient card.
> **Income capture broadened to ALL GAds patients (DONE Jul 9):** `_collect_lead_targets` (od_payment_sync.py) was gclid-only → now `gclid OR callrail source=google_ads` (same definition as the pipeline filter). So call-extension GAds patients (DJL/Paul/T,A — no gclid) now get their per-patient collected income captured. gclid patients already carry a campaign → roll up now; **no-gclid patients' income is captured but sits under no/unknown campaign until the call→campaign transcription feature assigns them** (see §2.3 Gemini item VERIFY-WHEN-DONE note). Owner scope (Jul 9): finish per-patient income now, don't get distracted by campaign rollup — it'll follow automatically once call attribution is built. Takes effect next sync run after restart.
> **DEFERRED (owner Jul 9 — non-blocking):** sync-chain ordering (refresh income each sync / add call matchers to chain) — parked so income work isn't distracted; call matchers still run via Mango ingestion. 365d-vs-lifetime INCOME/ROI basis decision; EV compute (feeds conversion upload); consolidate the two refresh-income endpoints; minor booked_override 365d-bucket precision.
Attribution (call→campaign) is done; income (call→patient→OD production→INCOME) is the missing half. A call needs 3 things for income: (1) attributed_ad_group ✅, (2) od_patient_num + status=new_patient (from `match_mango_calls_to_od_patients`), (3) od_appointment_id + production (from `match_calls_to_od_appointments`) → then `refresh_call_od_income` (od_payment_sync.py:325). The 24 backfilled calls have only (1). The nightly chain never runs the call patient/appointment matchers (only Mango ingestion does) and runs income refresh before attribution.
- `[NEXT]` **(A) Unblock:** run "Match OD" + "Refresh Income" (existing Call Analysis buttons / endpoints main.py:1613) for attributed calls → income appears for the subset who are new patients w/ production (expect small).
- `[DONE Jul 8 — pending server restart]` **Fix #1 campaign-level double-count.** Backported the `get_keyword_stats` `call_attributed_patients` dedup into the `lead_rows` query in `get_unified_campaigns` (database.py:4357-4394): the four lead-path money sums (attributed_income/production, paid_amount_365d/ltv) now exclude patients already counted on the call path; counts left unconditional. Fixes both managed + synthetic loops via the shared query. Sonnet built, Opus verified, py_compile clean. Latent today (no overlapping patient yet) so visible numbers unchanged; takes effect next restart. UNCOMMITTED.
- `[DONE Jul 8 — UNCOMMITTED]` **Fix #2 existing-patient leak on leads path.** (a) `od_payment_sync._collect_lead_targets` now filters `COALESCE(existing_patient,0)=0` in both queries (was skipped for leads); (b) `get_unified_campaigns` lead_rows: all four money sums now AND the dedup guard with `COALESCE(existing_patient,0)=0` so existing patients' collections don't inflate campaign income (counts stay unconditional — ad touch still recorded). Call path already `od_patient_status='new_patient'`-gated. Unit test added (existing_patient=1 lead excluded / stays $0; new lead accrues). Sonnet built, Opus verified, py_compile + 7 tests pass.
- `[NOT NEEDED — misdiagnosis, verified Jul 8]` **Fix #3 "estimated-vs-collected".** Traced the fields: income is ALREADY collected-only. `attributed_income` (database.py:43) = "actual collections (payments received)"; `od_patient_income` (database.py:1762) = `SUM(paysplit.SplitAmt)` collected; `treatment_plan_value` (the PLANNED number) is a SEPARATE column set by od_matcher and does NOT feed income. `booked_override` seeds `od_patient_income` (collected), not an estimate — so it does not overstate with planned money. No change made (won't touch correct code). Owner decision Jul 8: INCOME=collected, ROI on it (already true); treatment-planned = future case-acceptance column (data already captured in `leads.treatment_plan_value`).
  - `[QUEUED — minor precision]` booked_override seeds LIFETIME collected into the 365d bucket, and the upsert `MAX(seed, existing)` can lock a slightly-high `paid_amount_365d` if lifetime > true 365d window. Small; fix by seeding 0 into 365d (let od_payment_sync compute the windowed value) or seed only ltv.
- `[QUEUED — the real "updates every sync" gap]` **Sync-chain wiring.** Nightly chain runs income refresh BEFORE attribution and never runs the call patient/appointment matchers (only Mango ingestion does) → collected income may not refresh reliably each sync. Order the chain: attribute → match OD patient/appointment → refresh collected income. This is what makes the owner's "updates with every sync" real.
- `[DECISION NEEDED]` **INCOME/ROI window: trailing-365d collected vs lifetime collected.** Column has a 365d/LTV selector; ROI keys off `income_365d`. For implants (collections lag months/years) lifetime may reflect true ROI better; 365d is the standard ad window. Pick the canonical basis + make column display and ROI basis consistent.
- `[DONE Jul 8 — UNCOMMITTED]` **Fix #4 od_payment_sync timezone.** `_parse_anchor` now converts UTC anchor timestamps → America/New_York before `.date()` (OD PayDate is Eastern), so evening-ET leads no longer get an anchor one day late that drops same-day payments (e.g. booking-night deposits). Date-only anchors left unshifted. Unit test (6 cases). Sonnet built, Opus verified, py_compile + tests pass.
- `[QUEUED]` Consolidate the two divergent refresh-income endpoints (main.py:1613 vs 11304).
- `[QUEUED — feeds conversion upload]` **Compute the expected conversion value (EV) from OD funnel data.** From OpenDental: implant consults booked → showed → treatment accepted → collected, over a trailing period. Output = blended EV per booked consult (illustrative ~$3,200) AND EV by treatment type (full-arch vs single-tooth). This is the number to upload to Google (see §2.1 conversion-upload item). Recompute quarterly.
- `[DONE Jul 8 — verified live, UNCOMMITTED]` **Reworked the Campaigns-table APPTS column + dropped CALLS column (owner's patient-level model).** Backend: `_campaign_scheduled_new_patients()` + `/api/admin/campaigns/scheduled-new-patient-counts` + repointed `/api/admin/calls/campaign-appts` (main.py) — deduped new-patient-scheduled = form-scheduled leads ∪ call-booked appts, keyed by od_patient_num. Frontend (index.html): removed CALLS column; APPTS reads the bulk count; drill-down modal reworked to Patient/Source/Appt Date/Status/Income; patient name → patient card via onOpenLead (setSelectedId+setTab). Verified live: nXtsmile APPTS=2 (Andre form $50 + Richard form), click name opens the lead card. DJL correctly excluded (his call has no gads_call_id/campaign + no booked marker — zero GAds attribution). Sonnet built (backend+frontend), Opus verified live in browser.
  - `[NOTE]` Original queued spec below (kept for context):
  - **Bug found Jul 8:** APPTS (`index.html:7734`, `new_appts` from `/api/admin/calls/campaign-appts`) counts *calls only* ("new patient OD appointment matches from calls"). INCOME counts form-lead revenue too. So nXtsmile Implants shows **$50 income** (Andre = `contact_form` lead, scheduled Jul 13, $50 collected) but **blank APPTS** (he's a form lead, invisible to the call-only column). DJL (call, scheduled Aug 3) also blank — his call isn't OD-appt-matched to the campaign yet. Backend already knows 2 scheduled new patients for nXtsmile; the column just reads the wrong source.
  - **Target behavior:** APPTS = count of NEW patients (new-only, deduped by `od_patient_num`) who scheduled from that campaign, **form ∪ call** union (Andre + DJL both count, once each). Click the number → list patient names → click a name → patient card (full pipeline info + income). Reads from the same patient ledger as INCOME.
  - **Remove the CALLS column** (`index.html:7733`): a new-patient caller is already a lead, so call count is redundant on this table.
  - Files: `frontend/index.html:7733-7914` (columns + drill-down), backend campaign-appts endpoint / `get_unified_campaigns` (union form-scheduled + call-scheduled new patients), patient-card modal reuse.

### §2.2 Lead Lifecycle Dashboard (calculations, optimizer, MCP)
- `[QUEUED — bug, review/fix later, owner-flagged Jul 7]` **Deleting a lead fails: FOREIGN KEY constraint.** `admin_delete_lead` (`main.py:4645`) runs `DELETE FROM leads WHERE id=?` without first removing child rows that reference the lead → `sqlite3.IntegrityError: FOREIGN KEY constraint failed`. Fix: delete dependent rows first in a transaction (lifecycle_events, follow_up_queue, communication_log, mango_calls/callrail_calls links, keyword_production_log, unsubscribes, conversion_uploads, etc.) OR add `ON DELETE CASCADE`. Non-urgent.
- `[QUEUED — P1]` **Account-level negative keywords — where do they live, and where SHOULD they? (owner flagged Jul 7).** GAds Account settings → Negative keywords page is EMPTY, but the AI optimizer pushes negatives into a **shared negative keyword list** (`SharedSet` NEGATIVE_KEYWORDS, `ai_optimizer.py:6061+`), NOT true account-level (`CustomerNegativeCriterion`). Separate one-time script `add_account_negatives.py` uses true account-level but likely never run. **VERIFY FIRST: is the optimizer's shared list actually ATTACHED (`CampaignSharedSet`) to every active campaign?** If not, those negatives block nothing. Then decide best home: true **account-level** (auto-applies to ALL + future campaigns, max 1,000 — best for universal junk: jobs/free/DIY/competitor brands/out-of-scope services) vs **shared list** (managed/selective, must attach per campaign). Recommended split: universal excludes → account-level; thematic managed sets → shared list attached to all campaigns. Confirm nothing lost/duplicated between the two paths.
- `[QUEUED]` **P1** Fix campaign-level income/ROI double-count (dedup fix exists at keyword level, not backported) — `database.py:4470` vs `5064`.
- `[DONE Jul 9 — VERIFIED LIVE; commit 913a041]` **GAds Only board filter — recognize call-extension calls.** ENDPOINT GOTCHA: the Kanban board fetches `/api/pipeline/enriched` (`get_pipeline_enriched`), NOT `/api/pipeline` — v2a patched the wrong function (`get_pipeline`) and appeared to fail; fixed by attaching `callrail_source` in `get_pipeline_enriched` (main.py ~12376). DJL/Paul/T,A now show under GAds (verified on screen Jul 9). History below: History: v1 (mistaken) added `callrail` to `NON_GADS_SOURCES` requiring a gclid — but that HID legitimate ad calls. Verified via CallRail (`callrail_calls.source`): DJL, PAUL VARGHESE, and T,A are all `source='google_ads'` — **call-extension calls** (caller dialed the ad's call-extension number 508-321-5428; no gclid because they never hit the site). CallRail's own dashboard shows DJL: First touch Google Ads, Source google_ads, Medium cpc. So the earlier "Paul has zero attribution / hide him" conclusion was WRONG (only checked mango_calls, not callrail_calls.source). **Corrected rule (owner Jul 8): a phone lead is Google Ads if it has a gclid OR `callrail_calls.source='google_ads'`.** Fix v2: backend `get_pipeline` (main.py:958-980) attaches `callrail_source` per lead (batch join callrail_calls, prefer 'google_ads'); frontend (index.html:1590) `hasGadsAttribution` now includes `l.callrail_source === 'google_ads'`. `callrail` stays in `NON_GADS_SOURCES` so non-ad callrail (Google My Business / Google Organic / Direct / no callrail row) stays hidden. Result: DJL/Paul/T,A show; Westborough/Gill/Zechner (no callrail row) hidden. Sonnet built, Opus verified (compile + code); **live verify pending server restart + browser reload.**
  - `[QUEUED — follow-up A]` **Backend/board filter divergence.** KPI count chips come from `get_pipeline_stats(gads_only=True)` → `_pipeline_visibility_clause` (database.py:3403), a DIFFERENT definition. Board and counts can disagree. Unify: apply the same gclid-OR-callrail_google_ads rule in the backend clause so board == counts.
- `[STRATEGY — owner Jul 8; call-extension campaign attribution]` **Attribution waterfall for phone calls (which campaign?).** Two kinds of GAds phone calls: (a) website-pool DNI → carries a **gclid** → reconcile with Google's own call report (already built; e.g. Andre). (b) **call-extension** (508-321-5428) → **no gclid**; CallRail says source=google_ads but gives NO campaign. Waterfall: (1) gclid present → Google report match. (2) source=google_ads, no gclid → try Google call report (`gads_call_view`) time-match for the campaign. (3) if Google has nothing → **use the call transcript + Gemini to INFER the campaign from call context.** Requires feeding Gemini the currently-running campaigns + their details (services, keywords, geo) so it can classify accurately. Alternatives owner weighed & set aside: a separate CallRail number per call-extension (costs extra to capture the few manual-dial calls) and Google's own call-extension number (also passes no gclid). Decision: rely on CallRail detail for attribution + Gemini transcript inference as the no-gclid fallback. Campaign-level INCOME/APPTS for DJL/Paul/T,A stays $0/uncounted until this lands (they show as GAds in the pipeline via the filter fix above, but carry no campaign yet).
  - `[✅ IMPLEMENTED & WORKING — documented Jul 10]` **Waterfall step (1)+(2): Google call-report time-match.** How it works: `reconcile_attribution` (mango_service.py:862) matches each unmatched inbound Mango call to a row in `gads_call_view` (Google's call report). Two tiers: **Tier A — CallRail bridge** (mango_service.py:938-1036): if a CallRail row confirms GAds, find the closest `gads_call_view` row within ±60s. **Tier B — direct time-match** (ATTR-FIX2, mango_service.py:1044-1095): independent of CallRail — for any unmatched call, find a `gads_call_view` row with `call_status='RECEIVED'` within ±60s (closest, no reuse) and stamp `gads_call_id` + `attributed_ad_group` ("Campaign > Ad Group"), `match_method='gads_time_match'`, confidence 0.85. Google redacts caller area code on call-extension rows, so **time (±60s) is the only signal**. `_parse_gads_dt` (mango_service.py:594) converts Google's Eastern times → UTC. `get_gads_call_view` pulls the report; the `_mango_gads_call_view_job` (main.py) syncs it (current — synced today).
    - **Trigger/window:** runs inside `reconcile_attribution` (routine default `days=7`; wider re-runs via `/api/admin/mango/reconcile-now?days=N`, e.g. the Jul 7 90-day backfill).
    - **Endpoints:** `GET /api/admin/gads/call-view` (raw report rows), `GET /api/admin/gads/call-conversions` (returns `matched_to_mango`, `conversions_60s`), `POST /api/admin/mango/reconcile-now`.
    - **State (verified Jul 10):** 115 Google-reported calls, **87 matched to Mango** (~76%), 41 conversions within 60s. Working example: "Auburn, MA" call `240985619694827638` → "General Dentistry New Landing Page", `gads_time_match` @0.85.
    - **Limitation (why DJL/Paul aren't attributed):** the match can only attribute calls Google **actually reports**. DJL (Jun 23 14:07) and Paul (Jul 8 evening) are **not in `gads_call_view` at all** (verified: Jun 23 had only 2 reported calls, both MISSED, different campaign) — so there's nothing to match. ~24% of reported calls are also unmatched (mostly MISSED/0-duration). For Google-unreported calls, only waterfall step (3) Gemini transcript inference can assign the campaign. **Owner-side follow-up:** check the Google Ads console — is call reporting fully enabled, and does the call extension use Google's forwarding number vs the CallRail number (which Google won't report)?
- `[QUEUED]` **P1** CPL uses Google conversions instead of real leads — `main.py:8181`.
- `[QUEUED]` **P1** MTD budget pacing mixes host-clock vs UTC — `main.py:5014+`.
- `[QUEUED]` **P0** AI optimizer safety: make the kill switch real, add concurrency lock, fix `get_account_evaluation` crash (missing `gads_campaign_settings` table).
- `[QUEUED]` **P2** MCP write-gate is a no-op (returns enabled); align with documented two-flag gate.
- `[PLAN]` **P1** Consolidate duplicated income/timezone math into one shared module + ~20 tests + CI gate (root cause of "fix one, break another").
- `[QUEUED — P1, owner Jul 12]` **APPTS count accuracy: separate Canceled & No-Show from scheduled appointments.**
  - **Problem:** `_FORM_SCHEDULED_STAGES` includes `no_show` in APPTS count, inflating the number. Canceled appointments (e.g. Christine Leveque, nXtsmile) also count as APPTS. Owner wants accurate APPTS = only active/completed appointments.
  - **New stage: `canceled`** — distinct from `no_show` (didn't show up) and `lost` (fully gone). Canceled = had appointment, actively canceled, warm lead needing follow-up.
  - **Fix APPTS count:** Remove `no_show` and `canceled` from `_FORM_SCHEDULED_STAGES` so they don't inflate APPTS.
  - **New CANCELED column** on campaign table — count of leads with stage `canceled`. Clickable modal showing patient name, original appt date, source. Show only when count > 0.
  - **New NO-SHOW column** on campaign table — same pattern. Separate from APPTS.
  - **Follow-up routing (Phase 2):** Canceled and no-show leads feed into automatic re-engagement workflow (email/SMS sequence or front desk callback flag). Owner will set up the specific follow-up automation.

### §2.3 Call Analysis  *(ACTIVE workstream — owner Jul 9)*
**Transcription**
- `[DONE Jul 9 — needs restart]` **Auto-transcribe/grade GAds calls ONLY.** Owner: "it is transcribing everything; I need it to auto transcribe only gads calls." Added the GAds gate to `get_calls_needing_processing` (`database.py:9505`): a call qualifies if `gads_call_id` set OR `lead_id` set OR EXISTS a `callrail_calls` row with `source='google_ads'` (mirrors `get_mango_calls_needing_od_match` 8686-8694). Manual per-call transcription unaffected. **Impact (live):** of 491 inbound ≥30s calls/90d, ~190+ are GAds → ~300 (~60%) no longer auto-transcribed. Opus-built, py_compile OK, impact verified live. Takes effect next auto-pipeline run after restart.

**UI**
- `[DONE — Session 10]` **Pagination** — 100/page with prev/next arrow buttons, page indicator, range display. Page resets on filter change. Controls hidden for single-page results. Commit `b3b8cca`.
- `[QUEUED]` **P2** Show campaign/keyword + first-touch source in the calls list once classification is fixed.
- `[REVIEW — owner Jul 9]` **Other Call Analysis UI items — owner to review & specify.** Placeholder: Dr. Gupta has additional UI fixes to identify on the Call Analysis tab; capture them here when reviewed. (Not building until specified.)

**Gemini campaign inference (call-extension attribution fallback)** — from the §2.2 attribution waterfall
- `[DONE Jul 10 — WORKING, known campaign-context issue]` Code built Sessions 2-3, unblocked and verified Session 4. `_build_campaign_context_for_inference()`, `infer_campaign_from_transcript()`, `_CAMPAIGN_INFERENCE_PROMPT` (mango_service.py); admin endpoint GET/POST `/api/admin/mango/infer-campaigns?pw=&limit=N` (main.py). Tiers: `gemini_inferred` 0.80/0.65, `gemini_low_confidence` 0.45 (stored, not surfaced). First successful inference: dentures/implant call correctly identified service but matched to stale "Dentures and implant supported dentures" campaign instead of active nXtsmile (see known issue below).
  - `[NOW — P1]` **Fix campaign context staleness + automated campaign status sync.** Root cause found Session 6 (Jul 11): `_build_campaign_context_for_inference()` was rewritten to use `gads_keywords_cache` (30-day impressions > 0) instead of stale `campaigns.status`, but paused campaigns with earlier impressions still leak through (T,a's Jul 2 call → "Dentures" campaign was paused but had 30-day impressions). **Investigation revealed 4 systemic gaps:**
    1. **No automated status sync.** `admin_sync_campaign_from_gads()` exists but is manual-only — not in any cron. Daily 6 AM job only refreshes `gads_keywords_cache`.
    2. **"Sync Google Ads" button doesn't sync status.** `admin_sync_all_active_campaigns()` syncs performance data only.
    3. **No activation/deactivation dates.** `campaigns` table has `start_date`/`end_date` (free-text planning) but no `paused_at`/`activated_at` timestamps.
    4. **AI optimizer also stale.** Cannibalization detection trusts `campaigns.status == 'ACTIVE'`.
    **Fix plan (6 items):**
    - `[DONE Jul 11 Session 6 — UNCOMMITTED]` Add automated campaign status sync to daily 6 AM cron (pull ENABLED/PAUSED from GAds API for ALL campaigns). New `sync_all_campaign_statuses_from_gads()` (main.py, near :7784) — ONE read-only `fetch_campaigns_from_gads()` call, reconciles `campaigns.status` via `update_campaign_status` only on change, skips campaigns absent from the GAds response (never blanks). Shared `_map_gads_status_to_db()` helper (ENABLED→ACTIVE / PAUSED→PAUSED) also refactored into the manual `admin_sync_campaign_from_gads`. Wired into `_gads_morning_refresh_job` (main.py:430) in a separate try/except so status-sync failure can't break keyword refresh. **Verified live (read-only dry run):** caught "Emergency Dentistry" = ACTIVE in DB but PAUSED in GAds → would flip to PAUSED. py_compile clean. **Pre-existing bug noted (not fixed):** the manual sync-from-gads path puts `status` into `synced_fields` but `update_campaign_fields` ALLOWED-list drops it (status not whitelisted) → manual path never actually persisted status; the new bulk path uses `update_campaign_status` and works.
    - Add `paused_at`/`activated_at` timestamp fields to campaigns table
    - Wire "Sync Google Ads" button to also pull campaign status
    - Fix Gemini context: filter `impressions > 0 AND campaigns.status != 'PAUSED'` (depends on sync)
    - Sync Gemini-inferred campaign attribution to campaign leads/appointments (Paul/DJL don't show in nXtsmile campaign page despite being attributed — `effective_campaign_id` not set)
    - Gemini should extract real patient name from transcript (DJL ENTERPRISE is caller ID, not patient name; `ai_patient_name` field exists but empty)
  - **Bugs fixed during verification (Session 4):** (1) Gemini wraps JSON in markdown fences — added fence stripping + `{...}` extraction. (2) `max_tokens` 300→1200 (response truncated mid-JSON). (3) Removed `response_mime_type="application/json"` (deprecated SDK doesn't enforce it reliably). (4) Added Vertex block-reason logging (mango_pipeline.py).
  - `[VERIFY-WHEN-DONE — owner Jul 9]` **Per-patient income for these no-gclid call-extension patients is ALREADY captured** (od_payment_sync `_collect_lead_targets` broadened Jul 9 to `gclid OR callrail source=google_ads` — same definition as the pipeline). They currently roll up under **no/unknown campaign** (campaign_name empty). **When this campaign-assignment feature is completed, VERIFY the already-captured income then correctly reflects at the CAMPAIGN level** (rolls to the assigned campaign in `get_unified_campaigns` INCOME/ROI) — no income should be lost or double-counted in the transition.

**Mango↔CallRail data linking + pipeline fixes**
- `[DONE Jul 10]` **Fix Mango↔CallRail phone number linking.** Used SQL REPLACE chain to strip `( ) - space +` from `from_number`, then last-10-digit LIKE suffix match. Applied in both `mango_service.py` (`_link_unmatched_callrail_to_mango()`) and `callrail_webhook.py` (`_find_mango_match()`). Result: 7/8 GAds calls linked (1 remaining = toll-free spam). Manual reconcile now calls `_link_unmatched_callrail_to_mango(days=90)` before reconciliation for historical backfill; auto-sync stays at 7 days.
- `[DONE Jul 10]` **Fix auto-transcription gate false positives.** `_resolve_tracker_source()` (callrail_webhook.py:158) now only forces `google_ads` for `gads_call_extension` tracker type, not `gads_campaign` (DNI pool). DNI pool calls trust the webhook `source` field. Prevents wasted Vertex/Whisper tokens on non-GAds calls.
- `[NEXT — P1]` **GMB chip still showing in pipeline.** Frontend `knownCampaignNames` fix (Jul 10) was necessary but insufficient — "gmb" appears in backend `campaignStats` data, not just pipeline leads. Need to investigate which backend endpoint/query includes it and filter it out.
- `[NEXT — P2]` **OD existing-patient timing.** Patients who convert from calls get retroactively flagged as "existing" after being entered in OpenDental. Fix: compare patient's OD `SecDateEntry` vs call date — if patient was created AFTER the call, treat as "new_converted" not "existing_active". Affects conversion upload eligibility.

**Remaining next-steps sequence (updated Jul 11 Session 7+):**
1. ~~Fix Mango↔CallRail phone linking~~ ✅ DONE
2. ~~Fix auto-transcription gate~~ ✅ DONE
3. ~~Verify Gemini inference~~ ✅ WORKING
4. ~~Fix CallRail source case mismatch~~ ✅ DONE (Session 6)
5. ~~Investigate campaign context staleness~~ ✅ ROOT CAUSE FOUND (Session 6)
6. ~~Push source case fixes + campaign context rewrite~~ ✅ PUSHED (commit fd2ea69)
7. ~~Automated campaign status sync~~ ✅ DONE (Session 7 — daily 6 AM cron + "Sync Google Ads" button)
8. ~~Date-aware campaign filtering~~ ✅ DONE (Session 7 — activated_at/paused_at + history table + GAds backfill)
9. ~~Lead-campaign attribution sync~~ ✅ DONE (Session 7+ — infer-campaigns writes campaign to linked lead; commit 56bf339)
10. ~~Patient name extraction~~ ✅ DONE (Session 8 — commit 3dcf0ec)
11. **Auto-create leads from GAds-attributed calls** — see §2.3a below
12. ~~ATTR-FIX3: lead_id linking on GAds-attributed calls~~ ✅ DONE (Session 9-10 — Opus verified, commit b3b8cca)
13. ~~Remove concurrency semaphore~~ ✅ DONE (Session 9-10 — sequential processing, commit b3b8cca)
14. ~~Reset 27 stuck in_progress calls~~ ✅ DONE (Session 9 — DB reset; Session 11 — bulk reprocess 50 calls, 0 errors)
15. ~~Call analysis pagination~~ ✅ DONE (Session 10 — 100/page with nav arrows, commit b3b8cca)
16. ~~Campaign inference LIKE fallback~~ ✅ DONE (Session 11 — Gemini truncated names now match via LIKE, commit 848ac27)
17. ~~Donna Zechner deleted~~ ✅ (Session 11 — 11s missed call, not a real lead)
18. ~~DJL/Timothy/T,A/Paul campaign attribution~~ ✅ DONE (Session 11 — all have campaign_id now)
19. **60-second lead minimum** — DECIDED (Session 11). Pre-recorded message is ~40s; `duration_sec >= 60` for auto-lead creation.
20. **Auto-create leads from GAds-attributed calls** — see §2.3a below. Confirmed gap: UUID 4746236607 (Auburn MA, 91s, GAds General Dentistry) has full attribution but no lead.
21. Non-blocking startup (quality of life — see §2.8)
22. GMB backend investigation (find where "gmb" enters campaignStats)
23. OD existing-patient timing (affects conversion upload accuracy)
24. ~~Propagate ai_patient_name to lead names~~ ✅ DONE (Session 14 — commit a0198b7)
25. **Calls tab in lead detail modal** — see §2.3c below
26. **PlayButton seek bar** — see §2.3d below
27. **Manually attribute Claire Richard to nxtsmile Implants campaign + upload as conversion.** OD #5754, lead `lead_2109247c455d88c8`, $24,050 collections. Contact form Jun 3 8:16 AM on nxtsmile.com. No gclid captured (known page-navigation bug). Deep research (Session 12, Jul 11): GA4 session invisible (ad blocker/JS failure), but 8 nxtsmile ad clicks that day for "full mouth dental implants"/"teeth implants cost" — matches her case notes ("failing teeth, can't eat"). nxtsmile.com has zero organic clicks in GSC. Circumstantial case strong; owner approved attribution. Set `campaign_id`/`campaign_name` to nxtsmile Implants campaign.

### §2.3a Auto-Create Leads from GAds-Attributed Calls  `[PLAN — P1]`

**Problem.** 92 calls have a `gads_call_id` (matched to Google Ads call report) but only 4 have a linked lead record. By industry standard, every inbound call from a Google Ad is a lead — whether they booked or not. These 88 calls show in campaign CALL counts but not LEAD counts, and have no follow-up tracking.

**Industry definition (researched Jul 11).** A lead = any tracked inquiry (call or form) from a prospective patient. CPL ($40–$120 dental) is calculated on calls + forms, not bookings. Booking is a conversion (CPA $150–$350), not the lead threshold.

**⚠️ CRITICAL FILTER — CallRail ≠ Google Ads (owner feedback Jul 11)**
Existing patients save the CallRail tracking number in their phone and call it later for routine reasons. These calls arrive with `callrail_source='google_ads'` but are NOT real ad calls. Confirmed examples:
- **Laura Mora** — wife of existing patient Javier (OD #5757). Called the tracking number to schedule; not from an ad.
- **Adam Meyers** — existing patient since 03/2025. Called the tracking number directly; not a new lead.

**Rule:** Auto-lead creation MUST filter to `gads_call_id IS NOT NULL` (confirmed in Google's `call_view` report via ±60s time-match), NOT just `callrail_source='google_ads'`. Additionally, exclude calls where `od_patient_status` indicates an existing patient. Once gclid starts flowing reliably for calls, that becomes the most accurate source — but should still reconcile with the Google call report.

**Design: auto-lead creation with call-quality tiers + follow-up routing**

**Step 0 — Qualifying filter (gate before anything else)**
Only process calls where ALL of:
1. `gads_call_id IS NOT NULL` — confirmed in Google's `call_view` (not just CallRail source)
2. `direction = 'inbound'`
3. `duration_sec >= 60` — GDC pre-recorded message is ~40s; under 60s = no meaningful human conversation (DECIDED Jul 11 Session 11)
4. `od_patient_status NOT IN ('existing_active', 'existing_inactive')` OR `od_patient_status IS NULL`
5. NOT already linked to a lead (`lead_id IS NULL` or empty)

This eliminates false positives from existing patients calling the saved tracking number.

**Step 1 — Deduplication (critical, do first)**
Before creating any lead, check for existing leads by phone number (last 10 digits, strip formatting). Rules:
- **Exact phone match found** → link `mango_calls.lead_id` to existing lead. Do NOT create a duplicate. If the existing lead has no campaign but this call does, UPDATE the lead's campaign_name/campaign_id (first-touch attribution stays, campaign backfill is additive).
- **Existing lead from a DIFFERENT campaign** → keep the original campaign (first-touch wins). Log the new campaign touch in `lifecycle_events` as a multi-touch signal. Do NOT overwrite.
- **No match** → create new lead (Step 2).

**Step 2 — Call quality classification (determines lead stage + follow-up)**
Use signals already available on the Mango call to classify into tiers:

| Tier | Signal | Lead Stage | Follow-up Priority |
|---|---|---|---|
| **A — Spoke, didn't book** | `duration >= 60s` AND `is_missed = false` AND `booked_outcome != 'booked'` AND (`call_transcript` present OR `answered_by` set) | `new` | HIGH — staff follow-up call within 24h |
| **B — Missed call** | `is_missed = true` OR `answered_by IS NULL` with `duration < 15s` | `new` | URGENT — callback ASAP (they clicked an ad and nobody answered) |
| **C — Voicemail** | `is_missed = true` AND `duration >= 15s` (long enough to leave VM) | `new` | HIGH — listen to VM + callback |
| **D — Short/IVR hangup** | `duration 10-59s` AND `is_missed = false` AND no transcript/staff interaction | `new` | MEDIUM — outreach attempt (heard IVR, hung up before staff) |
| **E — Very short (<10s)** | `duration < 10s` | skip / flag | LOW — likely misdial or immediate hangup; review before creating lead |
| **F — Existing patient** | `od_patient_status IN ('existing_active', 'existing_inactive')` | skip | NONE — not a new lead (ad touch still recorded on the call) |

Refinements if transcript is available (Gemini grading):
- `ai_appointment_scheduled = 1` → already booked, lead should exist; just link
- `booked_outcome = 'booked'` → same
- `call_category = 'spam'` or `lead_quality = 'not_qualified'` → skip lead creation

**Step 3 — Lead record creation**
Fields to populate:
- `phone` — from Mango `from_number`
- `name` — from `caller_id_name` (with caveat: may be business name like "DJL ENTERPRISE"; if `ai_patient_name` is set, prefer that)
- `source` — `'google_ads_call'`
- `campaign_name` / `campaign_id` — from `gads_call_view` match (via `gads_campaign_name` on the call)
- `stage` — `'new'`
- `call_quality_tier` — A/B/C/D from classification above
- `created_at` — call's `started_at` (not NOW — preserves timeline)
- `callrail_source` — `'google_ads'` if known
- Link `mango_calls.lead_id` → new lead ID

**Step 4 — Follow-up queue integration**
- Tier B (missed) and C (voicemail) → auto-insert into `follow_up_queue` with `priority = 'urgent'`, `reason = 'Missed Google Ads call — callback needed'`
- Tier A (spoke, didn't book) → insert with `priority = 'high'`, `reason = 'Google Ads caller spoke with staff but did not schedule'`
- Tier D (short/IVR) → insert with `priority = 'medium'`, `reason = 'Google Ads caller hung up during IVR — outreach attempt'`
- Follow-up entries should surface in the Inbox / Action Items on the dashboard

**Step 5 — When to run**
- **Real-time:** hook into `reconcile_attribution` — after a call gets `gads_call_id` stamped, immediately check for lead and create if needed
- **Backfill:** one-time sweep of all 88 existing GAds calls without leads (admin endpoint `POST /api/admin/calls/create-missing-leads`)
- **Ongoing:** runs automatically as part of Mango ingestion + reconcile flow

**Step 6 — Verification & safety**
- Dry-run mode first: endpoint returns what WOULD be created without writing
- Log all auto-created leads with `source='google_ads_call_auto'` so they're distinguishable
- Never create a lead for outbound calls
- Never create a lead if `od_patient_status` = existing (ad touch still tracked on the call record)
- Audit: compare lead count before/after; verify no phone number appears twice in leads table

**Open questions for owner:**
- Should Tier E (<10s calls) create leads at all, or just be flagged for review?
- Should the follow-up queue auto-assign to a specific team member (e.g. Ivette)?
- For the 88 historical calls: run the backfill now, or wait until patient name extraction (#10) is done so leads get real names?
- Multi-touch: if someone called from Campaign A (no book), then Campaign B (booked) — which campaign gets credit?

### §2.3b Propagate ai_patient_name to Lead Names `[DONE — Session 14]`

**Problem:** Leads created from CallRail webhooks or auto-creation get caller ID as their name (e.g. "DJL ENTERPRISE", "BURNS JEANINE", "Unknown"). Patient name extraction (#10, Session 8) populates `ai_patient_name` on the mango_call from Gemini transcript analysis, but this never flows back to update the lead's `first_name`/`last_name`.

**Fix plan:**
1. In `finalize_call_lead()` (mango_service.py), add name propagation step:
   - Query linked mango_call's `ai_patient_name`
   - If lead name looks like caller ID (all-caps business name, "Unknown", city/state pattern) AND `ai_patient_name` is set → update lead's first_name/last_name
   - If lead has `od_patient_num` → pull confirmed name from OpenDental as highest-priority source
   - Priority: OD patient name > ai_patient_name > existing lead name
2. Add `ai_patient_name` to the finalize SQL query (alongside existing gcv fields)
3. Name detection heuristic: all-caps + multiple words, contains comma (city pattern), equals "Unknown"
4. One-time backfill endpoint for existing leads with caller-ID-style names
5. Frontend: show `ai_patient_name` in lead detail Info tab as "Patient Name" when different from lead name

**Affected leads (examples):**
- DJL ENTERPRISE → Christine (from transcript)
- BURNS JEANINE → Jeanine Burns (already fixed by auto-creation title-case, but verify)
- "Unknown" leads → patient name from transcript if available

### §2.3c Calls Tab in Lead Detail Modal `[DONE — Session 16]`

**Problem:** No way to view call details (transcript, recording, grading) from the pipeline lead card. Users must switch to the Call Analysis tab and manually find the call.

**Solution:** Add a "Calls" tab alongside Info / Conversation / Activity in the lead detail modal.

**Tab contents per linked call:**
- Call date/time, duration, direction (inbound/outbound)
- Campaign attribution (campaign name, keyword if available)
- Call quality/category badges (from Gemini grading)
- Audio player (Mango recording URL, requires admin auth)
- Expandable transcript section (full text, scrollable)
- OD patient match info (patient num, match confidence, status)
- Caller ID vs AI patient name (if different)

**Implementation:**
1. Backend: Add `GET /api/lead/{lead_id}/calls` endpoint returning linked mango_calls with all fields
2. Frontend: Add "Calls" tab to the lead detail modal tab bar
3. Render call cards with expandable sections for transcript and audio
4. If no linked calls, show "No calls linked to this lead"

### §2.3d PlayButton Seek Bar `[DONE — Session 15]`

**Problem:** The PlayButton only has play/pause — no way to scrub forward or backward in a call recording. Users must listen linearly.

**Solution:** Enhance PlayButton with an inline seek bar (range slider) and elapsed/total time display.

**Implementation:**
1. Add `currentTime` / `duration` state via `timeupdate` and `loadedmetadata` audio events
2. Show `<input type="range">` slider inline when playing, bound to audio.currentTime
3. Display `mm:ss / mm:ss` elapsed/total time
4. Keep component compact — seek bar appears inline next to play/pause button

### §2.3e Whisper Language Auto-Detect `[DONE — Session 16]`

**Problem:** Whisper was hardcoded to `language="en"`, causing Spanish (and other non-English) calls to produce gibberish transcripts. Manuel Montez's call about implant costs in Spanish was transcribed as meaningless English, and Gemini summarized "no patient-specific information was exchanged."

**Fix:** Removed `language="en"` from both OpenAI API and local Whisper modes. Whisper now auto-detects the spoken language and transcribes accurately. Gemini summaries are always in English regardless of transcript language.

### §2.3f APPTS Modal Days Mismatch Fix `[DONE — Session 16]`

**Problem:** APPTS count column used `callDays` (365 or 3650) but clicking the count opened a modal that hardcoded `days=30`. Result: count showed "1" but modal showed "No scheduled new patients found."

**Fix:** Changed modal click handler from `days=30` to `${unifiedMonth ? 365 : (unifiedDays || 3650)}`, matching the count endpoint's logic. Commit `5a32c47`.

### §2.3h Gemini booked_outcome False Positives `[SHELVED — owner Jul 12, revisit after more data next week]`

**Problem:** Gemini classifies confirmation/follow-up calls as `booked_outcome: "booked"` even when no new appointment was scheduled. This inflates APPTS counts and creates phantom conversions. Discovered via Christine Grondin / Emily case (Session 16): a confirmation call for an existing appointment was tagged "booked."

**Fix plan:**
1. Add prompt engineering to distinguish "confirming existing appointment" vs "scheduling new appointment"
2. Consider requiring both `booked_outcome = 'booked'` AND `od_appointment_id IS NOT NULL` for APPTS count (removes Gemini-only bookings)
3. Audit all `booked_outcome = 'booked'` calls without matching `od_appointment_id` to quantify false positive rate

### §2.3i Gemini Campaign Inference — Non-Marketing Call Filtering `[SHELVED — owner Jul 12, revisit after gclid data flows]`

**Problem:** Gemini campaign inference assigns non-marketing calls (refunds, admin, transfers) to campaigns. Example: Emily's refund-related call was inferred as General Dentistry campaign. These inflate lead counts and dilute conversion metrics.

**Fix plan:**
1. Add call-purpose classification step before campaign inference (marketing inquiry vs admin/billing/refund/confirmation)
2. Only run campaign inference on calls classified as marketing inquiries
3. Store classification as `call_purpose` field in mango_calls

### §2.3j Caller-ID Name Artifact Filtering `[QUEUED — P2]`

**Problem:** Auto-lead creation pulls caller-ID strings as patient names. Common artifacts: city/state names ("Auburn MA", "Westborough MA", "Emily MA"), carrier labels, "WIRELESS CALLER". These become lead names and pollute the pipeline.

**Fix plan:**
1. Build a rejection list: US city/state names, carrier labels, generic terms
2. For caller-ID names matching the list, leave lead name blank (to be filled by Whisper ai_patient_name or manual entry)
3. Integrate with §2.3b name propagation — ai_patient_name should always override caller-ID artifacts

### §2.3k OD Guarantor vs Patient Matching `[DONE — Session 17, commit 7f23d85]`

**Problem:** OD phone matching finds the guarantor record (parent) instead of the actual patient (child). Example: Christine Grondin (guarantor OD 5744) matched instead of Emily Grondin (patient OD 5735) because the phone is on the guarantor record.

**Fix:** Three new helpers in od_matcher.py: `_get_family_members()`, `_resolve_guarantor_family()`, `_get_od_patient_info_by_patnum()`. Resolution priority: (1) ai_patient_name first-name match against family, (2) appointment proximity, (3) ambiguous fallback (keeps guarantor). Wired into `match_calls_to_od_appointments`, `match_mango_calls_to_od_patients`, and `finalize_call_lead`. Key fix: uses first-name comparison (not token intersection) to avoid false matches on shared last names.

### §2.3l General Dentistry Campaign Restart Decision `[QUEUED — owner decision]`

**Context:** GD campaign paused Jul 11 after investigation showed 9 leads, 0 OD conversions, -100% ROI ($2,872 spent). Budget moved to nXtsmile Implants. Before reactivating, need:
1. Landing page review (is the GDC general dentistry landing page converting?)
2. Call handling audit (are GD calls being answered and converted?)
3. Fix §2.3h (booked_outcome false positives) and §2.3i (non-marketing call filtering) first
4. Consider: is general dentistry even worth PPC, or is organic sufficient?

### §2.3m Existing-Patient Misclassification + Auto-Transcription Gate `[DONE — Session 18]`

**Problem 1:** `_classify_od_status()` tagged patients as `existing_active` if they had ANY appointment entry before the call — even if they'd never actually visited the practice. Example: Sara Hanna (PatNum 5800) filled funnel_modal → got booked → called in → tagged "Existing" despite never completing a visit. This skewed call analysis and excluded new-patient calls from conversion upload.

**Fix:** Changed classification to require a COMPLETED appointment (`AptStatus=2`) with `AptDateTime` before the call time. Patients with only scheduled/future appointments are now `new_patient`. Same-day edge case handled correctly (call at 9 AM, completed at 10 AM → still new_patient for that call).

**Problem 2:** Auto-transcription (`_queue_process_if_needed`) had no source or patient gate — it transcribed any call >15s, wasting Whisper costs on organic/existing-patient calls.

**Fix:** Added dual gate: (a) source gate — requires GAds attribution (`gads_call_id`, `match_method=callrail_confirmed/gads_time_match`) or `fbclid` (future Meta); (b) patient gate — skips `existing_active`/`existing_inactive`. Non-qualifying calls remain available for manual transcription. Also fixed stale-dict bug where `mc` wasn't updated with new `gads_call_id`/`match_method` before the gate check.

### §2.3n CallRail→Mango Attribution Propagation + Name Fix + Sort `[DONE — Session 18]`

**Problem 1:** When CallRail webhook arrives AFTER the mango sync already processed a call, the `gads_call_id` and `match_method` fields on `mango_calls` stay empty. The auto-transcription gate (which checks these fields) never fires, so GAds calls that arrive via CallRail late don't get auto-transcribed.

**Fix:** `_link_unmatched_callrail_to_mango()` now propagates `gclid`→`gads_call_id` and sets `match_method='callrail_confirmed'` on the mango_calls record when it links a google_ads CallRail call. Also re-triggers `_queue_process_if_needed()` so the auto-transcription gate runs with updated fields.

**Problem 2:** Pipeline cards showed CallRail caller ID format names (e.g. "GIANGREGORIO,PA") instead of the real patient name found during OD matching. `finalize_call_lead()` only used `ai_patient_name` (Whisper) for name upgrade, ignoring `od_patient_name`.

**Fix:** Extended `finalize_call_lead()` to use `od_patient_name` as higher-priority fallback over `ai_patient_name` when upgrading caller-ID-style names. Guarantor re-derivation block still uses `ai_patient_name` correctly (needs transcript name to resolve family members).

**Problem 3:** Pipeline kanban columns sorted by `updated_at` — old leads bumped to top by enrichment touches.

**Fix:** Added client-side `created_at DESC` sort in the `byStage` useMemo so newest leads always appear first in every column.

### §2.3o Custom Procedure Code Detection `[PLAN — P2]`

**Problem:** `_get_treatment_plan_status()` only checks `proctp`/`treatplan` tables with CDT implant codes (D6010-D6099, D6194). Andre Brassard's treatment plan is in `procedurelog` (ProcStatus=1) with custom code `Cnxtsmile` and `D7210` (surgical extractions) — neither in the code list. His stage stays `showed` despite having a $48K treatment plan.

**Plan:**
1. Add `Cnxtsmile` to `CDT_IMPLANT_CODES` (practice-specific full-arch code)
2. Add `D7210` (surgical extraction — common pre-implant procedure) to the code list
3. Add fallback check on `procedurelog` ProcStatus=1 entries (treatment-planned procedures not yet in `proctp`/`treatplan`)
4. Audit OD for other custom codes that should trigger treatment_presented

### §2.3p Webhook gclid Propagation + Name Cleanup `[DONE — Jul 16]`

**Problem 1:** CallRail webhook links calls to mango rows (`mango_call_id`) but doesn't propagate `gclid` → `gads_call_id`. Webhook-linked calls never trigger auto-transcription because the mango_calls row lacks `gads_call_id`.
**Fix (callrail_webhook.py):** After `_upsert_callrail_call()`, added block that checks if `mango_uuid` + `_click_id` + google_ads source → updates `gads_call_id`, sets `match_method='callrail_confirmed'`, re-triggers `_queue_process_if_needed()`.

**Problem 2:** Pipeline cards showed caller-ID-style names (ALL CAPS, commas, city/state) instead of patient names. Existing leads finalized before the §2.3n code change were stuck.
**Fix (main.py):** Added `POST /api/admin/calls/fix-caller-id-names` endpoint. Finds leads with caller-ID names, updates from `od_patient_name` (priority) or `ai_patient_name` via linked `mango_calls`. Supports dry_run. Fixed 10 leads on first run.

### §2.3q Payment Sync Auto-Trigger `[DONE — Session 19, commit a386455]`

**Problem:** When the OD matcher updated `attributed_income` (live SUM from paysplit), the campaign table's income column (`paid_amount_365d`) didn't update because `sync_od_payments()` skipped leads synced within `days_back=7` via the `payment_synced_at` staleness guard.

**Fix (od_matcher.py):** Compare prev vs new `attributed_income`; if changed (>$0.01 tolerance), clear `payment_synced_at` so step 5 of the unified sync chain re-queries OD.

**Verified:** Claire Richard's campaign income updated from $24,178 to $47,155 after OD sync.

### §2.3r Campaign Table Improvements — ROAS + CPAppt + First-Apt Status `[DONE — Session 19, commit a386455]`

- **ROI → ROAS:** Renamed the column header in the campaign table (calculation unchanged — Revenue/Spend).
- **CPAppt (Cost per Appointment):** New column = Total Spend / patients who showed (completed appointments only). New backend endpoint `/api/admin/campaigns/showed-counts`. Showed = stage in (showed/treatment_presented/accepted/completed) OR appointment_status='complete' OR showed_at populated. Shows "$1,434" for nXtsmile.
- **First-appointment status tracking:** `_get_appointment_info()` (od_matcher.py) previously prioritized "scheduled" over "complete" — Claire showed "scheduled" (next apt 2026-08-10) despite completing her consult with $47K income. Now tracks the earliest appointment (consult); `appointment_date`/`appointment_status` show the first apt, not the latest scheduled. Stage transitions (`has_showed`/`has_scheduled`/`has_broken`) unchanged — only display changed.
- **Server management scripts:** `start.sh`/`stop.sh` — PID file management, graceful SIGTERM-first shutdown, port-free check. `.gitignore` updated for `server.pid`.
- **Slow page load fix:** bloated WAL file (40MB, recurrence of Jul 5 issue) forced full scans on every parallel mount-time API query. `PRAGMA wal_checkpoint(TRUNCATE)` dropped page load from 30s to 8ms. TODO: add periodic auto-checkpoint to prevent recurrence.
- **database.py NameError fix:** migration code used `log.info()` with no logger defined → crash on startup when old 7-criteria grading exists. Fixed to `print()`.
- **Call grading overhaul** (pre-existing, committed): 7-criteria numeric scoring → 14-criteria pass/fail rubric (100pts); updated prompt + response parsing in mango_pipeline.py.
- **Blocker found:** `gcloud auth application-default login` needed — Firestore sync failing with RefreshError.

### §2.3g Local Whisper Support `[PLAN — P3]`

**Problem:** All transcription currently goes through OpenAI's cloud API ($0.006/min). For high call volume, local Whisper on Mac Mini (or GPU server) would eliminate per-call cost.

**Plan:**
1. The `_transcribe_local()` function already exists but requires `whisper` + `torch` packages
2. Add Mac Mini deployment option: install whisper + torch (CPU/MPS), set `mango_whisper_mode=local` in Admin
3. Benchmark local vs API quality (whisper-1 vs large-v3 local) on a sample of 20 calls
4. Add language detection metadata to mango_calls table (detected_language column)
5. Consider whisper.cpp or faster-whisper for lower memory footprint on Mac Mini

### §2.4 Scheduler
- `[QUEUED]` **P0** Only send "confirmed" email after the OD write succeeds; add double-submit guard on the free path.
- `[QUEUED]` **P2** Resolve ET-vs-machine-clock timezone mixing.

### §2.5 Landing Pages & Website
- `[QUEUED — after income matching, owner-flagged Jul 7]` **graftondentalcare.com "New Patient Special" page has many errors.** Owner noticed numerous errors on that page. Investigate + fix after §2.1b income matching. (Page files likely in `new-patient-landing-page/` / `new-patient-special*.html`, but the live page is WordPress.)
- `[QUEUED — owner]` **Verify nxtsmile.com in Google Search Console** (see §1 sequence) — prerequisite to seeing real organic queries; do before income matching.
- `[QUEUED — P1, raised by a $24,050 lost lead]` **nxtsmile contact-request path drops first-touch attribution.** Leads via `contact_form`/`funnel_contact_request` (the contact-request page) arrive with NO gclid/utm/keyword/ga4_client_id (bare `landing_url`), while `funnel_modal` leads keep the full first-touch URL. Example: **Claire Richard** (OD PatNum 5754, **$24,050 collections**, Jun 3) — zero attribution, untraceable; Richard Tomaszewski (Jun 2, funnel_modal) had full gclid+keyword. Fix: make the contact-request submit persist + send the stored first-touch `attr_*` (mirror the funnel-modal path, `index.html:4313`). Prevents future high-value leads going dark. NOTE: once no signal is captured, the lead is **retroactively untraceable** (Google won't reverse-lookup by email/phone). **GA4 direct-API check (Jul 7):** nxtsmile GA4 (prop 531016678) had NO lead/conversion event for her funnel path on Jun 3, and the new-user sessions around her 8:16 EDT submit were all (direct)/(none) with ZERO google/cpc in the window → **not verifiably PPC.** DECISION: do NOT assign her to the nXtsmile Implants campaign (would fabricate ROI). Durable recovery = Enhanced Conversions for Leads (deferred). Also found: nxtsmile funnel_contact_request path fires no GA4 conversion event (tracking gap). Rule reaffirmed: **credit PPC only on gclid-confirmed leads.**
- `[QUEUED]` **P1** graftondentalcare.com: gclid capture + scheduler-link decoration + Gravity Forms hidden fields + pipeline webhook (see §2.1).
- `[QUEUED]` **P2** nxtsmile: remove dead `handleFormSubmit`; confirm swap.js.

### §2.6 Content & Organic  *(strategy doc: `ORGANIC_STRATEGY_YEAR1.md`)*
- `[NOW/ONGOING]` **P1** Implant long-form content, ~1/week (52-post plan). Several drafted/scheduled.
- `[QUEUED]` **P2** Google Business Profile + service-area pages + backlinks pillars.

### §2.7 Compliance & Security
- `[QUEUED]` **P0** Rotate secrets committed in docs/config; move to a secrets manager.
- `[QUEUED]` **P1** Replace single shared admin password with SSO (reuse scheduler's pattern) or restrict dashboard to owner's devices.
- `[QUEUED]` **P1** Close unauthenticated endpoints; tighten CORS.
- (Note: OpenAI/Whisper transcription is BAA-covered — **not** a task.)

### §2.8 Platform Health
- `[QUEUED]` **P1** Mac Mini monitoring (heartbeat alert) + hourly `pipeline.db` backup to cloud.
- `[DONE — Session 18, commit 7b7942c]` **Non-blocking startup.** Moved 3 sync calls (reset_skipped_no_audio, backfill_call_keyword_attribution, sync_from_firestore) to a background daemon thread. Portal is usable immediately on startup. `backfill_communication_log` stays blocking (prevents duplicate sends).
- `[QUEUED]` **P2** Plan Mac Mini → Cloud Run migration for the dashboard (after math consolidation).
- `[PLAN]` **P2** Test coverage + CI gate for money math.

### §2.9 Facebook Retargeting Campaign (Meta)  *(ref: `project_facebook_retargeting_jun30.md`)*

**Goal:** Retarget nxtsmile.com visitors via Meta/Facebook ads to convert warm traffic into consult bookings.

**Infrastructure already done (Jun 30 2026):**
- Meta Pixel (`1024139923307877`) installed + verified (PageView + ViewContent firing)
- Server-side CAPI (`_send_meta_capi_lead()`) live on Cloud Run — SHA-256 hashed PII, `await`-based (no fire-and-forget)
- `META_PIXEL_ACCESS_TOKEN` in Cloud Run env vars
- Data source restriction: "Health & wellness provider" — review rejected; CAPI bypasses browser-side blocks
- CTA decision: drive to nxtsmile.com (not phone or instant form) — AI smile preview reduces friction

**Setup steps:**
1. `[QUEUED — P1]` **Create AI video ad.** Use Creatify for AI avatar video (9:16, 30s). 4 retargeting scripts already written (validation / cost / fear / candidacy angles). Finish selected script, generate video.
2. `[QUEUED — P1]` **Build Custom Audience** in Meta Ads Manager: Website visitors → last 180 days → nxtsmile.com. Need ~100 visitors minimum before Meta will serve ads.
3. `[QUEUED — P1]` **Launch retargeting campaign.** Objective: Traffic (not Conversions) until Lead restriction lifts. Start with AI video creative.
4. `[QUEUED — P2]` **Testimonial videos.** Research videographer/editor to create emotional patient testimonial videos from real cases. Replace AI video with these once available. Multiple versions for A/B testing.
5. `[QUEUED — P1]` **Lead lifecycle tracking for Meta.** Ensure Meta-sourced leads are properly attributed in lead lifecycle dashboard — verify `source` field captures Meta/Facebook origin, UTM params flow through, and pipeline board shows correct attribution. Test end-to-end: ad click → nxtsmile form → lead in dashboard with Meta attribution.
6. `[QUEUED — P2]` **Re-appeal data source category** once CAPI is confirmed working with real leads — shows Meta we have direct server integration.

### §2.10 nXtsmile Blog — Full-Arch Case Studies

**Goal:** Build organic traffic to nxtsmile.com through case study blog content. Patients are arriving from Google search and ChatGPT directly to nxtsmile.com — a blog captures and converts this organic interest.

**Strategy shift (owner decision Jul 17 2026):** Previous rule (PROJECT_STATUS.md #10, ORGANIC_STRATEGY_YEAR1.md) kept all blog content on graftondentalcare.com with CTAs to nxtsmile. Owner now wants a blog directly on nxtsmile.com for case studies. Rationale: nxtsmile.com is already receiving direct organic/AI traffic; case studies on the same domain keep visitors engaged without a domain switch.

**Content plan:**
- **Cadence:** Start at 1 post/month, increase over time
- **Format:** Each post covers a real patient case — their unique situation, challenges, and how the full-arch restoration was handled
- **YouTube integration:** Some posts will include an embedded YouTube video of the case (owner-produced)
- **Focus:** Full-arch restoration / All-on-X only (aligned with nxtsmile positioning)

**Technical implementation (research needed before build):**
- nxtsmile.com is static HTML + FastAPI on Cloud Run (no CMS, no WordPress)
- Options to evaluate:
  1. **Static HTML blog pages** — hand-coded, served by existing Cloud Run setup. Simplest, no new dependencies. Each post is an HTML file with shared header/footer template.
  2. **Static site generator** (e.g., Hugo, Eleventy) — markdown-based authoring, auto-generates HTML. Better for scaling past ~10 posts.
  3. **Headless CMS** (e.g., Ghost, Strapi, Contentful) — richest editing experience, overkill for 1/month cadence initially.
  4. **FastAPI blog routes** — serve blog content from database/markdown via existing backend. Keeps everything in one codebase.
- **Recommendation:** Start with option 1 (static HTML) for speed. Migrate to option 2 if cadence increases past 2/month.
- Need: `/blog/` route, blog index page, individual post template, meta tags for SEO, structured data (Article schema), sitemap update, GSC already verified (Jul 7)

### §2.11 Lead Lifecycle Reporting Optimization

**Goal:** Continuously improve lead lifecycle dashboard reporting based on owner observation and real-world usage.

- `[ONGOING]` **Observation-driven refinements.** As owner reviews the dashboard in daily use, note any confusing metrics, missing data, or incorrect attributions. Fix iteratively — no fixed scope, driven by what surfaces.
- `[ONGOING]` **Pipeline board accuracy.** Ensure stage transitions, lead counts, and attribution labels reflect reality. Correct any misclassifications as they're discovered (e.g., existing-patient handling per §2.3m).

### §2.12 GAds Campaign Analysis & Optimization (Claude-Driven)

**Goal:** Use Claude + GDC Marketing MCP tools to regularly analyze Google Ads campaign performance and recommend optimizations.

- `[QUEUED — P1]` **Automated performance review.** Claude accesses GAds reports via MCP (get_campaign_performance, get_search_terms, get_keyword_landscape, get_geo_performance, get_device_performance) to analyze spend, conversions, CPA, and ROAS.
- `[QUEUED — P1]` **Search term analysis.** Regular review of search terms for negative keyword opportunities, wasted spend, and new keyword ideas.
- `[QUEUED — P2]` **Bid & budget optimization.** Use keyword bid estimates, click share data, and auction insights to recommend bid adjustments and budget reallocation.
- `[QUEUED — P2]` **Scheduled analysis cadence.** Set up periodic (weekly or bi-weekly) Claude-driven campaign review with actionable recommendations for owner approval.

---

## §3 Sequenced roadmap (suggested order)

1. **Facebook retargeting campaign** (§2.9) — finish AI video, build audience, launch campaign, verify lead lifecycle tracking. *← next up.*
2. **nXtsmile blog setup** (§2.10) — build blog infrastructure, publish first case study post.
3. **GAds analysis & optimization** (§2.12) — Claude-driven campaign reviews via MCP tools.
4. **Lead lifecycle reporting** (§2.11) — ongoing, observation-driven refinements.
5. **Attribution cleanup** (§2.1) — Phase 1.1 (classifier) + 1.1b backfill; CallRail/Google CDF config checks in parallel.
6. **Stop-the-bleeding safety** (§2.4 false-confirm email, §2.7 secrets, §2.2 optimizer kill switch, §2.8 monitoring/backups).
7. **Dashboard calculation fixes** (§2.2 double-count, CPL, pacing) + call-analysis gating (§2.3).
8. **graftondentalcare gclid + forms** (§2.5) to complete end-to-end capture.
9. **Structural** — consolidate math + tests/CI (§2.2), then plan cloud migration (§2.8).
10. **Conversion upload** (§2.1, deferred) once attribution is trustworthy.

---

## §4 Open decisions (waiting on owner)

- **Attribution first-touch edge:** organic-first-then-ad caller counts as organic — confirm.
- **graftondentalcare ad traffic:** point booking-intent ads at graftondentalcare (needs WP link decoration) or straight at scheduler/nxtsmile?
- **Go-ahead to start building** the attribution Phase 1.1 + backfill (owner chose "add backfill to plan" — build not yet authorized).
- **HIPAA stance for later conversion upload** — Google-only hashed PII vs none (deferred).

---

## §5 Detailed plan files (registry)

| Topic | File (in `marketing/`) | Status |
|---|---|---|
| Call / gclid attribution cleanup | `GDC_Call_Attribution_Cleanup_Plan_2026-07-06.md` | Planning complete; not built |
| Inheritance audit (context) | `GDC_Executive_Audit_2026-07-06.docx`, `GDC_Technical_Appendix_2026-07-06.docx` | Reference |
| Functional reference | `GDC_Functional_Reference_2026-07-06.docx` | Reference |
| Organic/content strategy | `ORGANIC_STRATEGY_YEAR1.md` | Ongoing |
| *(future topic plans register here)* | | |

---

## §6 Recently completed

- **2026-07-17/18 (Session 19, commit a386455)** — **§2.3q payment sync auto-trigger** (od_matcher.py clears `payment_synced_at` on `attributed_income` change so campaign income refreshes without waiting out the 7-day staleness guard; verified Claire Richard $24,178 → $47,155). **§2.3r campaign table improvements**: ROI column renamed ROAS; new CPAppt column (spend / showed patients, new `/api/admin/campaigns/showed-counts` endpoint); first-appointment (not latest-scheduled) status tracking in `_get_appointment_info()`. Also: `start.sh`/`stop.sh` server scripts, slow-page-load fix (WAL checkpoint, 30s→8ms), database.py NameError fix (log.info→print), call grading overhaul (7-criteria numeric → 14-criteria pass/fail rubric). Blocker: `gcloud auth application-default login` needed for Firestore sync. Prior to this: nXtsmile lead notification email overhaul (urgent subject, parsed concern rows, funnel/progress-bar UI fixes; commits c05c159/b673f98/58985f8). Full detail: `SESSION_SUMMARY_2026-07-17.md`.
- **2026-07-11 (Session 6 cont.)** — **Task #13 automated campaign status sync SHIPPED (code, UNCOMMITTED).** New `sync_all_campaign_statuses_from_gads()` + shared `_map_gads_status_to_db()` helper + wired into 6 AM `_gads_morning_refresh_job` + on-demand `POST /api/admin/campaigns/sync-statuses` endpoint (all `backend/main.py`). Verified read-only vs live data — caught "Emergency Dentistry" ACTIVE-in-DB / PAUSED-in-GAds. End-to-end write-test started but **interrupted (not yet confirmed)**. Found pre-existing bug: manual sync-from-gads never persisted status (`update_campaign_fields` drops non-whitelisted `status`) — relevant to item #7. **Infra:** installed `com.grafton.pipeline.plist` under launchd (KeepAlive+RunAtLoad) so the dashboard can be restarted via `launchctl kickstart -k gui/$(id -u)/com.grafton.pipeline` (owner granted standing restart permission). First load hit exit-78 (missing `/usr/local/var/log`); fixed by repointing installed-plist logs to `~/Library/Logs/grafton-pipeline.log` — server live under launchd (PID 15806). **TODO:** repo plist still has the `/usr/local/var/log` path — fix before a fresh install. Full detail: `SESSION_SUMMARY_2026-07-11.md`.
- **2026-07-11 (Session 5+6)** — **Concurrency throttle** (Semaphore(2)) fixed file descriptor exhaustion; 395 calls attributed on 90-day reconcile. **CallRail source case fix** ("Google Ads" vs "google_ads") in 6+ files — Paul Varghese now correctly shows "Ad call" + Gemini inferred "nXtsmile Implants". **Campaign sync investigation** — root cause of T,a's wrong "Dentures" attribution: no automated GAds→dashboard campaign status sync; paused campaigns with 30-day impressions leak into Gemini context. Plan created: daily status sync cron + paused_at/activated_at fields + Gemini context filter + lead-campaign sync + patient name extraction. CallRail pool bumped 4→5 numbers (deactivated idle "First Number"). **UNCOMMITTED:** source case fixes, campaign context rewrite, force parameter on infer-campaigns.
- **2026-07-10 (Session 4)** — **Phone linking bug FIXED** (REPLACE chain for formatted Mango numbers; 7/8 GAds calls now linked). **Auto-transcription gate FIXED** (DNI pool no longer forces google_ads). **Gemini inference VERIFIED WORKING** (1 inferred — wrong campaign due to stale context; JSON parse fix for markdown fences; max_tokens 300→1200; response_mime_type removed). Manual reconcile now does 90-day relink before attribution. Vertex block-reason logging added.
- **2026-07-10 (Session 3)** — GMB pipeline fix pushed (frontend `knownCampaignNames` Set — partial, backend still serves "gmb" in campaignStats). Gemini transcript campaign inference BUILT and pushed (mango_service.py + main.py; GET/POST `/api/admin/mango/infer-campaigns`). Admin endpoint auth changed to `_require_admin_media` (`?pw=` for browser access). **DISCOVERED:** Mango↔CallRail phone number linking bug — formatted vs digit-only phone mismatch breaks the entire Mango↔CallRail bridge, blocks Gemini inference.
- **2026-07-10 (Sessions 1-2)** — Unified GAds definition shipped; auto-transcribe GAds-only (~60% cost cut); income for ALL GAds patients (commit a18596c); confirmed Google call-report match (87/115). GMB pipeline fix + Gemini inference code built.
- **2026-07-06** — Full platform inheritance audit (docs + code + live data + external review); Functional Reference; master status doc + this Plan; call-attribution diagnosis & cleanup plan (incl. verified DJL call-extension root cause); CallRail website pool fix confirmed live (Jul 5).
