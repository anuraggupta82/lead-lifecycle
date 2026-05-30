
---
## Session: May 25 2026 — nXtsmile Implants Negative Audit + Competitor RSA

### What was done
1. **Search term analysis** — Pulled live GAds data for nXtsmile Implants (05/23 launch). $138 spend, 35 terms, 0 conversions (normal at 2 days). All terms unclassified.

2. **61 negative keywords applied** via `apply_nxtsmile_negatives.py`:
   - Wrong procedure: single tooth implant terms
   - Snap-on/snap-in dentures (different product)
   - Clinical trials / free care seekers
   - Dental schools
   - Cheap/affordable/discount explicit signals
   - Medicare/insurance-driven
   - Local competitor names (Accord Dental, Dental Dreams, Grace Dental, Webster Lake, Davis Ortho, Dudley Family)
   - Nuvia navigational (location/address searches only)
   - ClearChoice navigational (location/address searches only)
   - Aspen Dental (all — budget chain)
   - Misc: eligibility research, dentkits, dental implant restoration
   - 1 failed: "cheapest place to get all on 4 dental implants near me" (Google 10-word limit — covered by shorter term)

3. **Competitor conquest strategy decided**: Nuvia + ClearChoice brand terms KEPT as keywords. Only navigational/location searches negated. Rationale: comparison shoppers are valid All-on-X candidates.

4. **Price-research terms decision**: NOT negating cost/price research queries. nxtsmile.com price cards make these valid pre-conversion intent. Review in 2-3 weeks.

5. **Competitor-contrast RSA added** to 2 ad groups via `add_competitor_rsa.py`:
   - All-on-4 Implants Worcester County (ID: 810208533826)
   - Dental Implants Cost Comparison (ID: 810174835656)
   - Key headlines: "Family-Owned, Not a Franchise", "Not a Corporate Chain", "ClearChoice Alternative MA", "Nuvia Alternative Near You", "One Doctor, Your Whole Journey", "Dr. Gupta Does It All In-House"
   - Path: Implants / Not-a-Chain

### Decisions logged to optimizer
- Decision 799a7071: Competitor conquest strategy
- Decision 44bbdf1a: Price-research watch list

### GitHub push needed
Files changed: `apply_nxtsmile_negatives.py` (new), `add_competitor_rsa.py` (new)

---
## Session: May 30 2026 — Negative Keywords & Conquest Strategy

### What was done
1. **Pushed Negatives**: Pushed competitors (nuvia, clear choice, polasky, babu, gedc, ashland family), out-of-scope (orthodontist, orthodontics, x rays), and research intent (cost, vs, how much, price) to active campaigns.
2. **Kept Extractions**: Explicitly kept "extractions" as GDC performs them in-house.
3. **Cross-Pollination Fix**: Blocked "implant(s)" from General and Emergency campaigns.

### Decisions logged to optimizer
- Decision: Blocked major national competitors (Clear Choice, Nuvia) and local competitors due to high CPCs starving the daily budget.
- Decision: Do not pause "extractions" as the office performs them in-house.

---
## Session: May 30 2026 — Optmyzr Analysis, Evaluation Framework, Conversion Strategy

### What was done

#### 1. Optmyzr Portal Analysis
- Studied entire Optmyzr portal (15 tool sections): Audit Hub (score 73/100), Optmyzr Express, PPC Investigator, Quality Score Tracker, RSA Optimizer, Search Term N-Grams, Magic Quadrants, Hour of Week, Geo Heatmap, Sidekick AI, Negative Keyword Finder, Keyword Lasso, Spend Projection, Vertical Benchmarks, Rule Engine
- **Verdict**: Optmyzr is an execution/workflow layer for agencies. Most of its best features (n-grams, negative keywords, bid management) are already built into our platform. Two genuinely useful tools not replicated: PPC Investigator (root cause analysis) and Magic Quadrants (scatter plot of IS vs CVR).
- Key data surfaced: Account QS 6.0, top impression share 0%, invalid click rate 19.6%, 84 keywords in Laggards quadrant, Monday 12-4pm peak traffic (1,340 impressions, 57 clicks).

#### 2. Evaluation Framework Built (evaluation_framework.py)
- Created `/lead-lifecycle/backend/evaluation_framework.py` — decision tree that scores the account every optimizer run
- **Account score formula**: QS (30pts) + Impression Share (25pts) + Invalid Click Health (25pts) + Audit (20pts). Current score: ~53/100
- Scores campaigns 0-10, ad groups 0-10, flags keywords at QS 1-3, finds duplicate keywords, detects dead AGs, identifies smart bidding on tiny budgets
- Injected into both per-campaign and account-level Claude prompts as structured block
- Added `get_account_evaluation()` MCP tool to marketing-mcp server
- **Opus review found 5 bugs** — all fixed before commit: ag_name_guess NameError, seed call ordering (was after loop, now before), ad group field name mismatch, savings math double-multiply, dir() guard
- 4 files changed: `evaluation_framework.py` (new), `ai_optimizer.py`, `read_tools.py`, `server.py`

#### 3. Account Recommendations Executed
Pulled live data, cross-referenced with Gemini session notes and decisions folder. Revised recommendations after finding Gemini had already:
- Disabled Search Partners on all campaigns (May 29)
- Set PRESENCE_ONLY geo targeting (May 29)
- Set 60-second minimum call duration (May 29)
- Paused 55 broad match keywords account-wide (May 28)

**Executed (14 negatives + 2 assets + 1 rejection):**
- Emergency Dentistry: 7 off-intent negatives (x-rays research, Webster MA, gedc dental, generic location searches, smile dental)
- General Dentistry New LP: 7 competitor practice negatives (Polasky, Dr Costa, Dr Gobran, Nobscot, orthodontist worcester, misspellings)
- nXtsmile Implants: Approved callout + structured snippet assets
- Rejected "dentist worcester mass" exact keyword (out-of-area, patient won't drive 30min)

**Intentionally preserved per prior decisions:**
- Price-research terms ("how much does X cost") — nxtsmile.com has price cards (decision 44bbdf1a)
- ClearChoice/Nuvia conquest keywords — comparison shoppers are valid candidates (decision 799a7071)
- "Dental Implants Cost Comparison" AG — intentional conquest strategy, not waste

#### 4. Conversion Strategy Overhaul (Gemini recommendation implemented)
Changed `google_ads_conversions.py` based on Google algorithm best practice for high-ticket healthcare:

**Primary change — Appointment Booked fires at scheduling, not arrival:**
- Old: fired on `showed_at` (up to 10 days after click)
- New: fires on `scheduled_at` (same day or next day after click)
- Fallback chain: scheduled_at → showed_at → appointment_date (handles leapfrog leads)
- Cuts algorithm feedback loop from 10 days to <24 hours

**Primary vs Secondary conversion categorization:**
- PRIMARY (trains Smart Bidding): Qualified Lead only — fires same day, high volume
- SECONDARY (observation/ROAS reporting only): Appointment Booked, Treatment Accepted, Treatment Completed
- New function `set_conversion_categories()` + endpoint `POST /api/admin/set-conversion-categories`

**Multi-stage upload per lead:**
- `_resolve_conversions()` returns list of all applicable conversions per lead
- A treatment_completed lead now uploads all 3 downstream conversions in one run
- `_already_uploaded()` guard prevents duplication

**Opus review found 3 bugs** — all fixed: duplicate DEFAULT_VALUES shadow, FieldMask construction wrong (would crash on first API call), leapfrog leads silently dropped Appointment Booked
- 11/11 unit tests pass

### Files changed this session
- `lead-lifecycle/backend/evaluation_framework.py` — NEW
- `lead-lifecycle/backend/ai_optimizer.py` — evaluation framework injection + seed ordering fix
- `lead-lifecycle/backend/google_ads_conversions.py` — conversion strategy overhaul
- `lead-lifecycle/backend/main.py` — new /api/admin/set-conversion-categories endpoint
- `marketing-mcp/tools/read_tools.py` — get_account_evaluation() MCP tool
- `marketing-mcp/server.py` — tool registration

### Manual steps still needed
1. Start server and run: `curl -X POST http://localhost:7070/api/admin/set-conversion-categories -H "x-admin-password: GDC-pipeline-2026!"` (sets Primary/Secondary in Google Ads)
2. Pause 5 dead ad groups in Google Ads UI (safety-gated, cannot automate):
   - Dentures → Affordable Dentures - Grafton MA
   - General Dentistry → Dentist Near Me - Grafton Local
   - Gum recession → Gum Recession Treatment - General
   - Gum recession → Gum Grafting - Procedure Focused
   - Emergency May test → Emergency Dentist - General
3. Wait until June 4-5 to evaluate nXtsmile bid changes (per fb4f6cb7 decision)

### GitHub pushes needed
**Push 1:** evaluation_framework + optimizer injection + MCP tool
> "Add evaluation_framework decision tree — account scoring + optimizer prompt injection"

**Push 2:** Conversion strategy overhaul
> "Implement Gemini conversion strategy — fast Appointment Booked signal + Primary/Secondary categorization"
