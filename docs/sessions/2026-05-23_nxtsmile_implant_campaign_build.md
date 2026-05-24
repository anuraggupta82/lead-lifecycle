# nXtsmile Implant Campaign — Build Session
**Date:** 2026-05-23
**Status:** DRAFT — pending user Launch click
**Campaign ID:** `manual_nxtsmile_implants_0523__100day_20260524`
**Landing page:** https://nxtsmile.com/

## Decisions made this session

| Topic | Choice |
|---|---|
| Budget | $100/day start ($3,040/mo) → scale to $6k/mo on proof |
| Goal | Phone calls + lead funnel submissions (both Primary) |
| Geo | 15 miles around Grafton, MA (Worcester County focus) |
| Approach | Fresh build (legacy paused campaign 23709615996 stays paused) |
| Bidding | Manual CPC per-AG ($18 AG-1, $7 AG-2, $6 AG-3) → Maximize Conversions only after 15+ conv in trailing 30d (don't force at day 14) |
| Schedule | Mon–Fri 8a–6p, Sat 8a–2p; +20% Mon/Tue; +15% mobile |
| Conversion tracking | Already wired on LP: AW-18046211904 + CallRail DNI (company 340886676) + GA4 G-B3G7NKS06D |
| Implant-supported dentures | EXCLUDED per user direction — negatived snap-in / overdenture / implant-retained denture |
| Brand positioning | Top-Rated, All Under One Roof, 3D-Guided Surgery, In-House Sedation, AI Smile Preview |

## Ad-group structure (revised)
- **AG-1 All-on-X Best Quality & High-Tech + Conquest** — 50% budget, $18 max CPC, 27 keywords (added 7 conquest kws targeting clearchoice/nuvia/aspen/affordable dentures), 3 RSAs (added 1 conquest-themed RSA)
- **AG-2 Affordability & Financial Flexibility** — 25% budget, $7 max CPC, 16 keywords, 2 RSAs
- **AG-3 Problem-Aware Missing Teeth** — 25% budget, $6 max CPC, 14 keywords, 2 RSAs

Total: 57 keywords, 7 RSAs (15 headlines + 4 descriptions each).

## Extensions
- 4 sitelinks (Smile Preview, How It Works, Financing, Meet Dr Gupta) — all → nxtsmile.com root
- 9 callouts (One Roof, 3D-Guided, Sedation, AI Preview, Same-Day, Consult, CareCredit, Cherry, Top-Rated Grafton)
- 1 structured snippet (Services: All-on-4, All-on-X, Single Implants, Full-Mouth Reconstruction, Sedation, 3D-Guided)

## Negative keywords (34 — revised)
**Excluded:** bright now, western dental, snap in/on dentures, overdenture, implant supported/retained denture, dentures only, partials, partial dentures, dental bridge, veneers, whitening, teeth cleaning, braces, invisalign, wisdom teeth, extraction only, root canal, lawsuit, complications, failure, mexico, dental tourism, medicaid, free implants, charity, grants, kids, children, pediatric, teen, diy, do it yourself

**REMOVED from negatives per user direction (May 23 2026):** clearchoice, clear choice, nuvia, nuvia smiles, aspen dental, affordable dentures, affordable dentures and implants — these national-chain competitors are kept ENABLED so our ads compete for comparison shoppers. AG-1 now has explicit conquest keywords.

**Intentionally NOT negated** (AG-2 targets these): cost, price, how much, financing, payment plan, affordable, monthly, insurance, reviews, near me

## Opus pre-launch verification — findings + fixes

**🔴 BLOCKERS (all fixed):**
- 17 descriptions over 90-char limit — all trimmed
- 4 headlines over 30-char limit — all rewritten
- 1 callout over 25-char limit ("CareCredit and Cherry Financing" 31c) — split into two: "CareCredit Accepted" + "Cherry Financing"

**🟡 RECOMMENDED (addressed):**
- Sitelink "smilin" typo — fixed to "new smile"
- AG-2 `#financing` anchor URL — switched to root https://nxtsmile.com/
- "In-House Sedation" headline — softened to "Sedation Available" pending sedation-provider confirmation

**🟢 LOOKS GOOD per Opus:**
- Negative-keyword logic correct (cost/price/financing intentionally kept enabled for AG-2)
- LP claims match ad copy (3D-guided, sedation, one roof, same-day, Dr Gupta DMD BDS all visible on LP)
- No keyword duplication across ad groups
- No banned superlatives
- 15+4 RSA setup = optimal ad strength
- Cross-campaign cannibalization risk LOW (only Brand Awareness is active and overlaps with brand-only kws)

**❓ Open questions Opus raised:**
1. Old `nXtsmile All-on-X Implants` (23709615996, PAUSED) — keep paused, archive, or delete? Shares >40 keywords with new draft → must not be reactivated
2. Confirm Dr. Gupta personally provides sedation (vs referred anesthesiologist) — currently using "Sedation Available" instead of "In-House Sedation" as a safe default
3. Launch script must read geo + schedule from `strategy_json` (geographic_targeting column is empty as expected for DRAFT)
4. Bid modifiers in strategy_json (mobile 1.15, Mon 1.20, Tue 1.20) — verify launch script applies them

## What's saved in the dashboard
- `strategy` step (goals, audience, bidding, geo spec, scale triggers)
- `keywords` step (3 ad groups, 50 keywords with per-keyword max CPC)
- `ad_copy` step (6 RSAs, all character-limit compliant)
- `ad_groups` step (themes + budget shares + campaign-level negatives)
- `sitelinks` step (4 sitelinks)
- `callouts` step (9 callouts)
- `snippets` step (1 services snippet)

## Not saved yet (gated on launch — endpoint requires campaign_resource)
- `geo` — applied at launch time from strategy_json
- `schedule` — applied at launch time from strategy_json
- Call extension — intentionally empty so CallRail DNI on LP owns attribution

## Next steps
1. **User:** Review draft in dashboard
2. **User:** Press Launch when ready
3. After launch: verify geo (15mi Grafton) and schedule (Mon–Sat hours) propagated correctly
4. Add account-level negative `implant supported denture` (if not already present) to keep General Dentistry campaign from picking up those searches
5. Confirm CallRail nXtsmile DNI pool can handle $100/day click volume
6. Decide on legacy campaign 23709615996 (recommend: keep paused 30 days, then delete)

## Git push
No code changes this session — all work was in the dashboard via MCP. No push needed.

## Risks / things to watch in first 30 days (revised per Gemini feedback)
| Metric | Soft target | Panic threshold | Action |
|---|---|---|---|
| Daily spend | $90–110 | <$70 with Budget Lost IS <30% | Raise AG-1 max CPC further to $22 |
| CTR | >6% | <4% | Rewrite RSAs, audit ad-LP message match |
| AG-1 Search Lost IS (rank) | <30% | >50% after 72h | Raise AG-1 max CPC ($18 → $22), AG-1 likely starved at $18 in MA implant auction |
| Avg CPC AG-1 | <$15 | >$20 | Tighten exact-match keywords, add specific-intent negatives |
| Conv/day | ≥0.5 | 0 after day 10 | Audit LP funnel completion + ad-LP match |
| CPL (blended) | <$250 | >$400 | Investigate (NOT pause) — $300 CPL is normal baseline for All-on-X |
| Cost per booked consult | <$600 | >$1000 | Restructure |
| ROAS (north star) | Positive on 1 closed case in 30d | No closed cases in 60d | Restructure |

**Key calibration change:** $200 CPL was unrealistic for cold All-on-X search. Industry average $250–$450. Focus on ROAS — one $25k case at $8k acquisition = 3.1x ROAS.

## Scale plan (revised)
Trigger: **30-day rolling CPL <$300 AND booked-consult rate >25% of leads AND ROAS positive on at least 1 closed case**.
1. +25% budget bump
2. Expand geo to 20mi
3. Add AG-4 Senior-focused
4. Add AG-5 limited competitor conquest expansion (Boston implant centers)
5. Test PMax with same conversion goals

## Bidding-strategy switch (revised)
**Switch to Maximize Conversions ONLY after 15+ conversions in trailing 30 days.** If day 14 has <10 conversions, stay on Manual CPC — do NOT force the switch. Max Conversions needs dense recent data; forcing it on sparse data underperforms vs Manual CPC.

## Scale plan
Trigger: 14-day rolling CPL <$200 AND booked-consult rate >25% of leads
1. +25% budget bump
2. Expand geo to 20mi
3. Add AG-4 Senior-focused
4. Add AG-5 limited competitor conquest
5. Test PMax with same conversion goals
