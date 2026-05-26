# Session Summary — 2026-05-25: nXtsmile Campaign Optimization + GA4 Analysis

**Date:** May 25, 2026  
**Focus:** nXtsmile Implants campaign negative keywords, competitor RSAs, GA4 analysis, and landing page anchor fix

---

## What We Did

### 1. Search Term Analysis — nXtsmile Implants Campaign

Pulled and analyzed all search terms from the nXtsmile Implants campaign since launch (May 23). Key findings:

- **All-on-4 Worcester County AG ($18 CPC)** had most of the expensive clicks — off-intent terms like snap-on dentures, single tooth, clinical trials, and navigational competitor searches (nuvia/clearchoice location queries)
- **Cost Comparison AG ($7 CPC)** had reasonable terms mostly intent-matched
- **Implant Dentist Near Me AG ($6 CPC)** performing cleanly
- Price-research terms ("how much does a full set cost") — **decision: do NOT negate**. nxtsmile.com has price cards; these are pre-conversion intent. Watch for 2–3 weeks.

---

### 2. Negative Keywords Applied — 61/62 succeeded

Applied 62 campaign-level broad match negatives to nXtsmile Implants campaign (ID 23870298927). One failed: "cheapest place to get all on 4 dental implants near me" exceeded Google's 10-word limit. Shorter variant already covered.

**Categories negated:**
- Single tooth / single implant intent
- Snap-on / snap-in dentures
- Clinical trials / free care
- Dental schools
- Discount / cheapest / Medicare-driven
- Local competitor names (Accord Dental, Grace Dental, Webster Lake, etc.)
- Nuvia/ClearChoice navigational/location searches only (NOT conquest brand terms — kept for ads)
- Aspen Dental (all — budget chain not our patient)
- Misc off-intent (eligibility, dental school, restoration)

**Script:** `backend/apply_nxtsmile_negatives.py`  
**Logged:** 62 entries in `gads_audit_log`, all `execution_result='success'`

---

### 3. Competitor-Contrast RSA Added

**Decision:** Keep Nuvia/ClearChoice brand terms as conquest keywords (comparison shoppers are high-value). Answer with a "family-owned, not a corporate chain" angle rather than negating.

Added a competitor-contrast RSA to two ad groups:
- **All-on-4 Implants Worcester County** (AG ID 201959101332) — Ad ID 810208533826
- **Dental Implants Cost Comparison** (AG ID 196128439625) — Ad ID 810174835656

**Headlines include:** "Family-Owned, Not a Franchise", "Not a Corporate Chain", "ClearChoice Alternative MA", "Nuvia Alternative Near You", "Skip the Chain, Choose Local"  
**Path:** Implants / Not-a-Chain  
**Script:** `backend/add_competitor_rsa.py`

**API quirks resolved this session (saved to memory):**
- `load_from_env()` fails without `use_proto_plus` in env — use `load_from_dict()` always
- `ad.display_url` must NOT be set on RSAs — auto-generated, returns `VALUE_MUST_BE_UNSET`
- `path1/path2` set on `rsa` object directly, not on a separate `path` variable

---

### 4. Verification Script

`backend/verify_nxtsmile_changes.py` — reads live from Google Ads API (same `load_from_dict` pattern), checks negative count + spot-checks 9 key terms, prints all RSAs in both ad groups and flags competitor RSAs.

**Verification via audit log:** Both RSAs confirmed `execution_result='success'` with resource names returned. 61 negatives confirmed same. Live API call available by running the verify script in terminal.

---

### 5. GA4 Analysis — nxtsmile.com Today

Pulled GA4 data for nxtsmile.com (property 531016678) filtered to hostname = nxtsmile.com only. Key findings:

**Overview (today):**
- 15 real sessions, 48 users, 38 new users
- 80% bounce rate, 41s avg session duration
- 0 conversions (no conversion events configured)

**Traffic breakdown:**
- 11 sessions Unassigned (bots/crawlers — 100% bounce, 3s avg)
- 5 paid sessions from nXtsmile campaign (All-on-4 AG only attributed — 60% bounce, 52s avg)
- 4 cross-network sessions
- 1 direct

**Scroll depth:**
- GA4 fires scroll event at 90% — only 1 user today reached 90%
- No paid visitor scrolled past the fold
- 7-day data: only 7 users total have ever reached 90% scroll depth

**Paid session behavior:**
- 5 paid mobile sessions, 65s session duration but only 1–3s engagement
- All landing on `/` (hero section) — no scroll, no form interaction
- Before & After carousel (the strongest conversion asset) is at ~25% page depth — visitors not reaching it

**Pages:** nxtsmile.com is effectively a single-page site — all 40 page views hit `/`. `view_search_results` fired 5× (chatbot being used instead of the form).

**Critical gap: no conversion tracking.** GA4 tracks page_view, scroll, session_start, first_visit, user_engagement — but nothing is marked as a conversion event. Google Ads has no signal to optimize toward.

**Geo:** Boston, Providence, Attleboro, Manchester-by-the-Sea, Medway (MA-local sessions); Toronto, NY, Bangalore in Unassigned bucket (bots).

---

### 6. URL Anchor Fix — All 8 RSAs Updated

**Problem:** Paid visitors landing on hero (`/`) not reaching Before & After section. Immediate fix: change final URL to `https://nxtsmile.com/#results` so paid clicks land directly on Before & After.

**Approach:** RSA `final_urls` are immutable — must remove and recreate. Two ad groups already had 3 RSAs (at the limit), so had to remove all first, then create new ones.

**Method:** Direct Google Ads API (`use_proto_plus=False`, sandbox bash pattern from session 2026-05-23)

**Results:**
| Ad Group | RSAs replaced | New URL |
|---|---|---|
| All-on-4 Implants Worcester County | 3/3 | `https://nxtsmile.com/#results` |
| Dental Implants Cost Comparison | 3/3 | `https://nxtsmile.com/#results` |
| Implant Dentist Near Me Worcester | 2/2 | `https://nxtsmile.com/#results` |

All 8 RSAs verified ENABLED with `#results` URL via GAQL read-back.

**Error encountered and fixed:** First attempt failed on 2 ad groups with `RESOURCE_LIMIT_EXCEEDED` — those had 3 RSAs already (at Google's per-ad-group limit). Fixed by removing all 3 first, then creating 3 new ones sequentially.

---

### 7. Funnel-as-Modal — Deferred (Plan Written)

**Problem identified:** The 6-step funnel form is inline in the hero. Paid visitors landing on `/#results` would need to scroll back up to interact with it. On mobile, the form asks for commitment before visitors have seen proof.

**Proposed fix (later):** Move the funnel into a modal overlay triggered by CTA button clicks. Hero becomes clean visual + single button. Form pops on demand.

**Plan document:** `nxtsmile-landing-page-v1/docs/PLAN_funnel_modal.md`

Covers: exact HTML/CSS/JS changes (~50 lines total), all 7 `href="#consultation"` locations to update, hero layout changes, full testing checklist, ~2–3hr effort estimate. No backend or GA4 changes needed.

**Status: DEFERRED** — implement when ready to do a website revamp session.

---

## Files Created/Modified This Session

| File | Action |
|---|---|
| `backend/apply_nxtsmile_negatives.py` | Created — 62 negatives script |
| `backend/add_competitor_rsa.py` | Created — competitor-contrast RSA script |
| `backend/verify_nxtsmile_changes.py` | Created — live API verification script |
| `backend/set_nxtsmile_url_suffix.py` | Created — campaign suffix script (reference) |
| `nxtsmile-landing-page-v1/docs/PLAN_funnel_modal.md` | Created — deferred modal plan |

All 8 RSAs recreated in Google Ads via direct API (no script file — sandbox-run).

---

## Decisions Logged

| Decision | Detail |
|---|---|
| Price-research terms → watch, don't negate | nxtsmile.com has price cards; these are pre-conversion intent. Review in 2–3 weeks. |
| Nuvia/ClearChoice → conquest, not negate | Comparison shoppers are high-value. Answer with family-owned angle in ads. |
| 62 campaign-level negatives | Covers wrong procedure, snap-on, trials, dental schools, Medicare, local + national competitor navigational |
| Landing URL → `#results` anchor | Paid visitors go directly to Before & After instead of hero |
| Funnel modal → deferred | Good UX improvement but requires website revamp session |

---

## API Pattern Notes (for future sessions)

- **3-RSA-per-ad-group limit:** Can't create a 4th RSA. To replace all RSAs when at the limit: remove all first, then create new ones.
- **`use_proto_plus=False` in direct API scripts** — enum access via `client.get_type("...Enum").EnumClass.VALUE`, not subscript
- **RSAs are immutable** — to change final_url or copy: remove + recreate
- The `final_url_suffix` on Campaign (field `campaign.final_url_suffix`) can append anchors without touching ads — but RSA recreation was chosen here to be explicit and auditable

---

## Open Items / Next Steps

1. **GitHub push** — 3 new backend scripts + plan doc
2. **Conversion tracking on nxtsmile.com** — no conversion events configured; Google Ads optimizing blind. Add `generate_lead` on form submit + `phone_call_click` on phone tap (future session)
3. **Price-research terms review** — revisit in 2–3 weeks to check if cost queries are converting
4. **Funnel modal** — implement when ready for nxtsmile.com revamp
5. **Monitor `/#results` performance** — check if bounce rate + engagement improves over next 48–72 hours
6. **Legacy campaign 23709615996** — still paused, delete after 30 days from May 23 = ~June 22
