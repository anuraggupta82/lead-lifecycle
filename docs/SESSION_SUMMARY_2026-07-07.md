# Session Summary — 2026-07-07

**Focus:** Built and shipped the call→campaign attribution fixes, then diagnosed the revenue (INCOME/ROI) layer and the keyword-attribution gap. Follows the Jul 6 audit/planning session. Dashboard runs manually on the owner's MacBook (not on Mac Mini yet — see below), so all syncs were triggered by hand.

## What shipped (code)

1. **Build 1 — CallRail classifier fix** (committed `0944ccf`): `_resolve_tracker_source` now tags a call `google_ads` when it has a gclid OR the number is `gads_campaign`/`gads_call_extension` (was only `gads_campaign` → call-extension calls were mislabeled "Direct"). Also read CallRail campaign/keyword for those calls; new guarded dry-run backfill endpoint.
2. **config.py startup fix** (committed with Build 2): declared `scheduler_internal_key` (present in `.env`, undeclared → pydantic crash on restart).
3. **Build 2 — direct `gads_call_view`→Mango time-match** (committed): campaign attribution no longer depends on the CallRail bridge that broke May 22. `reconcile_attribution` + the backfill now match Google's call report to Mango calls directly by time (±60s, RECEIVED, closest/no-reuse).
   - **Backfill applied:** 24 historical ad calls attributed to campaign + ad group (incl. nXtsmile Implants → All-on-4 / Implant Dentist Near Me). Google match count **61 → 85** confirmed.
4. **Resolver window fix** (UNCOMMITTED — commit pending): widened the gclid→keyword resolver from a 7-day to a 30-day `click_view` window at all 3 recurring call sites (`main.py:205,434`; `unified_od_sync.py:313`). Weeks-old gclid leads were being skipped. Takes effect on next restart.

Each build: Sonnet implemented, Opus verified, py_compile clean.

## Key diagnoses & findings

- **Revenue layer (wire #2) — why campaign INCOME/ROI is still blank:** attribution is only step 1 of 3. A call needs (1) `attributed_ad_group` ✅, (2) OD patient match, (3) OD appointment + production. The chain's OD step matches *leads* not *calls*, so the 24 attributed calls had no income link. Ran "Match OD" (16 matched) + "Refresh Income" → income stayed ~$328 total, **because the collected revenue genuinely isn't there yet**: the recovered calls are mostly short non-converting hangups, and real implant consults (e.g. Andre) are booked for *future* dates (no production until treated). ROI on collected revenue is a months-lagging metric for implants.
- **DJL trace:** real active OD patient, correctly matched, "Scheduled" Aug 3. Campaign recoverable from Google's call report; keyword only obtainable for call-extension calls via CallRail CDF.
- **CDF is NOT delivering** (verified in CallRail UI): 0/267 calls in 30d have gclid/keyword/campaign in CallRail itself. Google's side is correctly configured. So it's an upstream **owner console task** (verify CallRail↔Google integration), not our code.
- **gclid landing-page capture WORKS** (read-only test): swap.js fires on graftondentalcare.com + nxtsmile.com, gclid survives, tracking template clean (`{lpurl}?…&keyword={keyword}`). Form leads carry gclid.
- **gclid→keyword resolver FIXED:** root cause was the 7-day window skipping weeks-old leads. Ran the 90-day backfill → **all 5 gclid leads resolved** (Andre = "full mouth dental implants"; carrie/Sara/Renee/Richard too). Confirms the owner's point that campaigns are **Search-only** ⇒ every gclid has a recoverable keyword.
- **OD sync chain transient failure:** the "Patient Match failed" was a momentary MacBook connectivity blip (couldn't resolve accounts.google.com / OD MySQL / open pipeline.db), recovered 18s later. Not a persistent bug — but flagged a robustness fix (don't fail the whole step on a transient blip).
- **Account-level negative keywords:** the GAds account-level Negative keywords page is EMPTY, but the AI optimizer pushes negatives into a **shared negative keyword list** (`SharedSet`, `ai_optimizer.py:6061+`), not true account-level. Key risk to verify: is the shared list actually *attached* to every active campaign? Investigation queued in Plan §2.2.

## Constraints / context confirmed by owner

- **Campaigns are SEARCH-ONLY** — no Performance Max, no Dynamic Search Ads, no Search Partners ⇒ every gclid has a real keyword in Google's `click_view`.
- **Not deployed on Mac Mini yet** — dashboard runs manually on the MacBook; syncs are manual; no 24/7 automation. "Nightly" jobs only run when triggered. Mac Mini deployment is a future workstream.

## Evening: Claire ($24,050 lead) attribution + GA4/GSC investigation

- **Claire Richard** (OD PatNum 5754, $24,050 collections, nxtsmile contact_form Jun 3) had **zero attribution** (no gclid/utm/ga4_client_id). Investigated whether she can be traced.
- **Code:** confirmed nxtsmile's modal and contact-form submit paths use the SAME capture (`getTrackingData()`); the separate `new-smile.html` is broken/unrouted. Her blank = her first pageview simply carried no gclid/utm.
- **GA4 (queried the Data API directly, nxtsmile prop 531016678):** no conversion event fired for her funnel path on Jun 3; the new-user sessions around her 8:16 EDT submit were all **(direct)/(none)** with **zero google/cpc** in the window → **not verifiably PPC.** DECISION: do NOT assign her to the nXtsmile Implants campaign (would fabricate ROI); rule reaffirmed — credit PPC only on gclid-confirmed leads.
- **Caveat (owner):** nxtsmile's GA4 "direct/organic" is polluted by owner/Claude **test traffic** (522 "direct", DIAGTEST gclid loads, test@test.com/DFWEWEF leads) — not real demand.
- **Organic terms need Search Console, not GA4** (Google hides organic query). 

## Access set up (Jul 7) — for future API pulls
- **GA4 Data API:** works via SA `ga4-reader@marketing-landing-page-491721.iam.gserviceaccount.com` (key: `_CREDENTIALS_VAULT/marketing landing page service account key.json`). Property IDs: **nxtsmile 531016678, graftondentalcare 536128204, visitgdc 533672873**. Scope `analytics.readonly`.
- **Google Search Console:** owner **verified `sc-domain:nxtsmile.com`** and **granted the same SA access** Jul 7. Organic queries pullable via the Search Console API (`searchconsole.googleapis.com`, scope `webmasters.readonly`) once data populates (~Jul 10). Revisit organic-query analysis then.

## Git state

- Committed: Build 1 (`0944ccf`) + Build 2 + config fix.
- **Pending commit:** resolver window change (`main.py`, `unified_od_sync.py`) — git summary: *"Widen gclid→keyword resolver window 7→30 days."*

## Where we are / next

Attribution detour is closed (call→campaign works; keyword layer works for gclid leads). **Next = return to §2.1b (revenue layer): make campaign INCOME/ROI real** — start with the highest-impact income leak (campaign-level double-count), then existing-patient leak, estimated-vs-collected, timezone; plus wiring OD patient/appointment matching for attributed calls. Owner-side: verify CallRail CDF; assign website DNI pool numbers; the negative-keyword-list attachment check.

All tracked in `Plan.md` (§1 + §2.1b marked as the return point) and memory.
