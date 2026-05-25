
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
