# Session: Jun 1 2026 — Attribution Audit + Campaign Keyword & Bid Fixes

**Done by:** Claude (Cowork)
**Duration:** ~4 hours

---

## 1. Full Attribution Audit

End-to-end audit of Google Ads tracking across all touchpoints.

### nXtsmile.com — SOLID
- swap.js DNI installed, gclid + all UTMs captured via URLSearchParams
- All 3 form paths (hero, funnel, smile tool) pass tracking to backend
- Cross-domain decorator passes attribution to visitgdc.com booking URLs
- CallRail DNI (company 340886676) installed

### visitgdc.com Scheduler — SOLID
- attribution.js captures gclid/UTMs into sessionStorage
- StepPayment.jsx sends all fields in checkout payload
- posted_appointments table stores gclid, utm_*, ga4_client_id

### graftondentalcare.com — CRITICAL GAP
- WPCode Snippet #1801 captures gclid into GF hidden fields (JS only)
- **No PHP webhook exists** to forward form submissions to lead pipeline
- **No cross-domain attribution** when visitors click through to visitgdc.com
- Fix needed: PHP gform_submission hook → POST to /api/leads

### CallRail → Backend — SOLID
- Webhook handler extracts keyword, gclid, campaign correctly
- _resolve_tracker_source() correctly overrides "Direct" for GAds call extension trackers
- callrail_calls table stores full attribution

### Key finding: ATTR: marker NOT written to OD
- Scheduler stores attribution in posted_appointments columns only (intentional per code comment)
- od_matcher.py ATTR: parser is a dead code path for scheduler bookings
- Need to verify a sync job reads posted_appointments → creates leads

---

## 2. CallRail Google Ads Integration — Activated

**Problem:** Integration was stuck "Pending" — required an active website pool.

**Actions:**
- Deleted old Website Pool 1 & 2 (source type, couldn't be used as session pool)
- Created **"GDC Website Pool - Google Ads"** via CallRail UI wizard:
  - Type: session (website pool), 4 numbers, 508 area code, Google Ads source
  - Forwards to 508-318-4477
- Connected Google Ads account (customer 2498049505) in integration settings
- **Integration status: Active** — CDF (Call Details Forwarding) now enabled
- Set **90-second minimum call duration** filter in CallRail → Integration Filters → Google Ads

**WPCode snippet:** No change needed. Token `e23ec68ba569c11c32cf` is company-level and covers all trackers including the new pool.

---

## 3. Backend: 90s Call Duration Filter

**File:** `lead-lifecycle/backend/google_ads_conversions.py`

Added call duration check before uploading conversions to Google Ads:
- For `source = 'callrail'` leads, queries `callrail_calls.duration_seconds`
- Skips upload if call < 90 seconds
- Mirrors the CallRail integration filter set today
- Constant `CALL_MIN_DURATION_SECONDS = 90`

**Git commit ready:** `feat: 90s call duration filter for Google Ads conversion uploads`

---

## 4. Bidding Strategy — All Campaigns to Manual CPC

Switched via MCP tool (backend running):
- ✅ nXtsmile Implants → MANUAL_CPC
- ✅ General Dentistry → MANUAL_CPC
- ✅ Emergency Dentistry → MANUAL_CPC
- ⏸️ Brand Awareness → paused by user, skipped

Rationale: Fewer than 30 conversions/30d — insufficient data for smart bidding.

---

## 5. Emergency Dentistry — Keyword Fix

**Problem:** Only 2 impressions today. Root cause: broad match pause on May 28 left keyword set too narrow. Not a bid problem.

**17 keywords added** via Google Ads API v24 (HEALTH policy exemption applied):

| Ad Group | Keywords Added |
|---|---|
| Emergency Dentist Core | [emergency dentist near me], [emergency dental open today], [dentist open near me today], [emergency dentist in worcester], [24 hour emergency dental extraction], "no dental insurance need tooth pulled", "dental clinic near me open now", [immediate tooth extraction near me], [teeth extractions near me], "are there any dentists open today" |
| Same-Day & Walk-In | [emergency dental], [dental clinic near me open now] |
| Tooth Pain & Symptoms | "what can i do for severe tooth pain", "tooth hurts at night", "what to do for infected gum", "treatment for a cracked tooth", [tooth abscess] |

Oral surgeon keywords excluded (not a service offered).

---

## 6. General Dentistry — Keyword Fix + Bid Reset

**Problem:** Impressions tanked (same broad match pause root cause). User raised bids with Gemini yesterday — wrong fix.

**25 keywords added** across High Intent, Branded Local, No Insurance ad groups. Key additions: [dentist near me], [dental clinic near me], [dental cleaning near me], [grafton dental care], [dentist in worcester], [dental cleaning without insurance], [affordable dentist near me], senior/denture queries.

Removed: `"i need dental work but have no money"` — wrong intent.

**82 keyword bids reset** from Maximize Conversions inflation ($15–$35) back to $4.00–$6.00:
- Phrase/broad → $4.00–$4.50
- Exact geo → $5.00–$6.00
- Worst: "same day dental appointment near me" was $35 → reset to $4.50

---

## 7. nXtsmile Implants — Bid Reset Only

Checked decision log first. May 24 design: AG-1=$18, AG-2=$7, AG-3=$6. May 28 rule: "do not touch bids before June 4-5."

**15 keywords reset** to designed AG max CPCs. Gemini had overbid:
- "teeth implants cost" in Near Me AG: $40 → $6
- "tooth implant cost near me": $32 → $6/$7/$18 by AG
- "same day teeth implants": $30 → $6/$7/$18 by AG

All other keywords left untouched. Re-evaluation date: **June 4-5 2026**.

---

## Decisions Logged
- 6aecb222 — Emergency Dentistry keyword additions
- ab921b2a — General Dentistry keyword additions + bid reset
- 84ea1708 — nXtsmile bid reset to designed levels

---

## Pending Items
1. **P0:** Build GF → lead pipeline PHP webhook for graftondentalcare.com
2. **P1:** Verify scheduler → lead-lifecycle sync job exists and runs
3. **P2:** Add gclid decoration to graftondentalcare.com booking links
4. **P3:** Add `{keyword}` ValueTrack to all GAds final URLs
5. **June 4-5:** Re-evaluate nXtsmile impressions/CPL — consider bid reduction if still <10 clicks/day

---

# Session Continuation: Jun 2-3 2026 — Campaign Monitoring + OD Matcher Fix + Scheduler Analysis

## Google Ads — Campaign Recovery (Jun 2)

### All 3 campaigns switched to Manual CPC
Emergency, General Dentistry, nXtsmile. Brand Awareness paused by user.

### Emergency Dentistry
17 keywords added (exact/phrase) from search term data. Result: 2 impressions Jun 1 → 23 impressions Jun 2 within hours of fix.

### General Dentistry
25 keywords added. 82 keyword bids reset from Maximize Conversions inflation ($15-35) back to $4-6. Removed "i need dental work but have no money."

### nXtsmile Implants — June 4-5 Re-evaluation (Opus analysis)
Opus reasoning on bid strategy. Key findings:
- Market clearing price $15-25 for implant keywords
- "dental implant specialist" eating 38% of budget ($306/14d) at mediocre intent
- Ad group defaults inflated to $20 by Maximize Conversions — root cause of $23-33 CPCs

Changes applied:
- Near Me AG default: $6 → $12
- Cost Comparison AG default: $7 → $14  
- All-on-4: holds $18
- "dental implant specialist" in All-on-4: explicit $9 cap
- "nuvia dental" broad negative removed → replaced with 18 navigational-only negatives
  (phone number, directions, hours, location, near me — keeps research/conquest traffic)
- Next gate: June 20 at ~150 total clicks → if still 0 leads, investigate landing page

Decision logged: d0b76835

## CallRail Integration Fix

### Google Ads integration activated
- Created "GDC Website Pool - Google Ads" (session pool, 4 numbers, 508 area code)
- Integration status: Active, CDF enabled
- 90-second call duration filter set in CallRail

### Backend: 90s call duration filter
google_ads_conversions.py updated — callrail leads with calls <90s skipped before conversion upload.

## OD Matcher Improvements

### Root cause: Richard Tomaszewski not matching
- Phone was WkPhone (work) — matcher only queried HmPhone/WirelessPhone
- Email had space: "gmail. com" — hash mismatch
- Manual fix: linked to PatNum 5750, stage → scheduled, date Jun 4 10 AM

### Code fixes (od_matcher.py + database.py):
1. Added WkPhone to OD patient query
2. Added name-first matching tiers:
   - Tier 1: Full name (first+last) → unique → secondary verification
   - Tier 2: Phone (HmPhone, WirelessPhone, WkPhone — new)
   - Tier 3: Email
   - Tier 4: Last name only + secondary
3. Email sanitization on lead creation — strips all whitespace before hashing

Git commit ready: `fix: OD matcher — WkPhone + name matching tiers + email sanitization`

## visitgdc.com GA4 Analysis

### Funnel data (7 days, property 533672873):
- 48 sessions total
- 33 landed on scheduler
- 11 started form (33% start rate)
- 2 completed bookings (6% overall, 18% of starters)
- 67% bounce before touching form → slow React hydration on first load

### Traffic: graftondentalcare.com referral drives most traffic (26 sessions, 7m 47s)
### Google CPC: 4 sessions, 8m 09s — highly engaged, 0 conversions

### Root causes identified:
1. Slow first load (React SPA hydration) — loses 67% before form_start
2. Deposit friction — some abandoning at payment step

## Scheduler Next Steps (saved to NEXT_STEPS.md)
- P0: Loading skeleton in index.html
- P1: Deposit step copy reassurance
- P2: Register step_name/step_number as GA4 custom dimensions
- P3: Add book.graftondentalcare.com + book.nxtsmile.com via GCP custom domain mapping
- P4: Domain-aware theming (hostname-based GDC vs nXtsmile branding)

## Pending Git Pushes
1. `feat: 90s call duration filter for Google Ads conversion uploads` — google_ads_conversions.py
2. `fix: OD matcher — WkPhone + name matching tiers + email sanitization` — od_matcher.py + database.py
