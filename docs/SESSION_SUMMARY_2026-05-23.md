# Session Summary — May 23, 2026

## Overview
Landing page build for Emergency Dentistry campaign + critical campaign audit that uncovered two bugs affecting all active campaigns (mobile disabled, wrong schedule). Session continued with General Dentistry weekend schedule addition and call extension scheduling.

---

## 1. Emergency Dentist Landing Page (`emergency-dentist-lp.html`)

### Changes Made
- **$99 New Patient Offer** — Added prominent offer card (hero badge + full card with $99 price, exam + X-ray included, new patients only badge)
- **Hero CTA** — Removed "Book Online" from hero; call-only above the fold (emergency patients should be on the phone, not clicking through a booking flow)
- **Copy cleanup** — Removed strikethrough "$200–$250 value", removed "Limited slots available" scarcity language (creates wrong friction for pain patients)
- **Slot → Appointment** — Replaced all 10 instances of "slot/slots" throughout the page
- **SVG lightning bolt** — Fixed ⚡ emoji rendering as "ТЬб" in WordPress (font encoding bug); replaced with inline SVG
- **$50 deposit disclaimer** — Added "* Online booking requires a $50 fully refundable deposit" matching new-patient-special-v2.html
- **FAQ schema** — Updated cost question to lead with "$99 for new patients"
- **Meta/SEO** — Title: "Emergency Dentist Grafton MA — Same-Day | Grafton Dental Care"; Meta description: 156 chars, includes $99, phone, location
- **WordPress slug** — `urgent-care`

### Gemini Audit Response
- Nav menu kill: already handled by CSS (`display:none !important` on header/nav)
- Book Online deposit friction: fixed by removing Book Online from hero entirely
- Dayparting: addressed in campaign fixes below

---

## 2. Emergency Dentistry Campaign — Critical Fixes

### Bug 1: Mobile Completely Disabled (0.0x)
- **Root cause:** `set_device_bid_modifier` in `main.py` computes `bid_modifier_value = 1.0 + modifier`. Passing `modifier=-1.0` yields `0.0` which Google treats as device disabled. Guard only blocked MOBILE at exactly -1.0 but still let 0.0 through.
- **Fix applied live:** MOBILE 0.0x → 1.2x (+20%), DESKTOP 0.5x → 1.0x, TABLET 0.5x → 0.8x
- **Code fix:** Guard now rejects `modifier ≤ -0.9` for all devices + hard floor check at `bid_modifier_value < 0.1`

### Bug 2: Ad Schedule Running 24/7 (Bid Modifiers Never Applied Correctly)
- **Root cause:** May 22 rebuild set peak windows to `bid_modifier=1.0` (default no-op) instead of `1.1` (+10%). Off-hours windows were reduced but not blocked. Overnight and weekends still running.
- **Fix:** Replaced entire schedule with strict **Mon–Thu 10:00–18:00** only (matches office hours + landing page "We Can See You Today" promise)
- **Rationale:** Strict dayparting is correct here — emergency patients hitting a closed voicemail after reading "same day" will immediately go to a competitor

---

## 3. General Dentistry (New Landing Page) Campaign — Fixes

### Device Modifiers (same bug)
- MOBILE 0.0x → 1.2x, DESKTOP 0.5x → 1.0x, TABLET 0.5x → 0.8x

### Schedule Rebuilt
| Day | Office Hours | Evening | Notes |
|-----|-------------|---------|-------|
| Mon | 10–18 @ 1.0x | 18–21 @ 0.8x | Normal |
| Tue | 10–18 @ 1.0x | 18–21 @ 0.8x | Normal |
| Wed | 10–18 @ 1.0x | 18–21 @ 0.7x | Normalized from -10% (no data justifying reduction) |
| Thu | 10–18 @ 1.1x | 18–21 @ 0.9x | +10% — higher call volume observed |
| Fri | 10–15 @ 0.85x | — | Half day, reduced |

- Trimmed all 8am starts to 10am (office opens 10am)
- Evening windows kept for general dentistry — patients research after work

### Weekend Schedule Added (May 23 — Session 2)
| Day | Window | Modifier | Notes |
|-----|--------|----------|-------|
| Sat | 10–15 @ 1.0x | — | Matches Friday half-day window |
| Sun | 10–17 @ 1.0x | — | Extended for late-afternoon book-for-Monday traffic |

**Rationale (Opus):** Budget is the binding constraint (70.8% IS lost to budget, only 10% search IS). Bid reductions on weekends are economically incoherent when the campaign is already impression-share-constrained by budget. 111 Sunday search terms show clean general dentistry intent — zero emergency/pain queries. Weekend spend is ~$9.50/day (19% of $50 budget). Run at 1.0x.

### Call Extensions Scheduled
- **Assets updated:** `5083184477` (GDC main) + `15085459356` (CallRail DNI)
- **Schedule:** Mon–Thu 10:00–18:00, Fri 10:00–15:00
- **Rationale:** Phone number should not appear on Sat/Sun when office is closed; patients clicking call on weekends get voicemail
- **Decision logged:** `734cfe7e`

---

## Code Changes
- `main.py` — device bid modifier guard patched (rejects modifier ≤ -0.9, floor check on computed value)

## Git Pushes Needed
1. **Landing page** — `emergency-dentist-lp.html`
2. **Marketing app** — `main.py` device modifier guard fix

## Key Rules Learned
- `bid_modifier=0.0` = device disabled in Google Ads, NOT a percentage reduction
- Emergency campaigns: strict dayparting to match office hours when landing page makes same-day promises
- General dentistry: evening windows (18–21) valid for research traffic at reduced bids
- Audit device modifiers on ALL campaigns — same bug affected Emergency + General Dentistry
- When IS lost to budget > 60%, bid reductions are counterproductive — fix the budget or let it run at full bid
- Call extensions need their own schedule — otherwise phone shows during closed hours even if the ad schedule is correct
- `AssetService.mutate_assets` with `field_mask=call_asset.ad_schedule_targets` is the correct API path for call asset scheduling
