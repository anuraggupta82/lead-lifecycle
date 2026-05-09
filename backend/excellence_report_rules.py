"""
GDC Google Ads Excellence Report - Structured Rules for AI Optimizer
Extracted from: GDC_Google_Ads_Excellence_Report.docx (Claude Opus 4, May 2026)
Used by: ai_optimizer.py - injected into Claude prompts for impact benchmarking

This module is the authoritative reference of dental PPC benchmarks and
best-practice rules for the Grafton Dental Care AI Optimizer. All numbers
are taken directly from the Excellence Report; do not edit without
re-reading the source document.
"""

EXCELLENCE_RULES = {
    # =========================================================================
    # 1. INDUSTRY BENCHMARKS - what GDC's metrics should be measured against
    # =========================================================================
    "benchmarks": {
        # Section 13.1 - 2025 Dental Google Ads Benchmarks
        "ctr": {
            "industry_avg": 5.44,           # %
            "top_quartile": [8, 12],        # %
            "gdc_target": 7.0,              # > 7%
            "unit": "percent",
        },
        "cpc_overall": {
            "industry_avg": 7.85,           # $ per click (overall dental)
            "unit": "usd",
            "note": "Track by service - varies widely",
        },
        # Section 13.2 - CPC by Service (2025)
        "cpc_by_service": {
            "general":           {"low": 4,    "high": 12,  "trend": "rising_15pct_yoy"},
            "emergency":         {"low": 6,    "high": 15,  "trend": "rising"},
            "teeth_whitening":   {"low": 3,    "high": 8,   "trend": "stable"},
            "dental_implants":   {"low": 12,   "high": 35,  "trend": "rising_competitive"},
            "invisalign":        {"low": 8,    "high": 25,  "trend": "rising"},
            "all_on_4":          {"low": 15,   "high": 40,  "trend": "highly_competitive"},
            "veneers":           {"low": 8,    "high": 20,  "trend": "stable"},
            "brand":             {"low": 0.50, "high": 1.50, "trend": "low_declining"},
        },
        "conversion_rate": {
            "industry_avg": 9.08,           # % (click -> lead)
            "top_quartile": [12, 18],       # %
            "gdc_target": 10.0,             # > 10%
            "unit": "percent",
        },
        "cpl": {
            "industry_avg": 83.93,          # $ per lead
            "top_quartile": [50, 75],       # $
            "gdc_target": 100.0,            # < $100
            "unit": "usd",
        },
        "cost_per_new_patient": {
            "industry_avg": [150, 275],     # $
            "top_quartile": [75, 150],      # $
            "gdc_target": 175.0,            # < $175
            "unit": "usd",
        },
        "search_impression_share": {
            "industry_avg": [40, 60],       # %
            "top_quartile": [70, 85],       # %
            "gdc_target": 65.0,             # > 65%
            "unit": "percent",
        },
        "roas": {
            "industry_avg": [3, 5],         # multiple of spend
            "top_quartile": [5, 8],         # multiple
            "gdc_target": 4.0,              # > 4x
            "unit": "multiple",
        },
        # Section 13.3 - Conversion funnel (end-to-end)
        "funnel": {
            "click_to_lead":           {"industry_avg": 9,  "top": [12, 18], "unit": "percent"},
            "lead_to_booked":          {"range": [40, 60], "unit": "percent",
                                        "lever": "front_desk_call_handling"},
            "booked_to_showed":        {"range": [70, 85], "unit": "percent",
                                        "lever": "appointment_reminders"},
            "showed_to_accepted":      {"range": [60, 80], "unit": "percent",
                                        "lever": "case_presentation_financing"},
            "click_to_treated":        {"range": [3, 5],   "unit": "percent",
                                        "note": "End-to-end including front desk and treatment acceptance"},
        },
        # Daily budget tiers - suburban Boston market (Section 3.1)
        "daily_budget_tiers": {
            "minimum_viable":    {"daily": [50, 68],    "monthly": [1500, 2100],
                                  "outcome": "1-2 services; current GDC level"},
            "recommended":       {"daily": [83, 133],   "monthly": [2500, 4000],
                                  "outcome": "3-4 campaigns; competitive IS 60-70%"},
            "aggressive_growth": {"daily": [133, 200],  "monthly": [4000, 6000],
                                  "outcome": "Full service coverage; IS 75-85%"},
        },
    },

    # =========================================================================
    # 2. IMPACT RULES - specific optimizations with quantified impact
    # =========================================================================
    # impact_type values:
    #   waste_reduction  - recovers wasted spend (% of spend recoverable)
    #   conversion_lift  - increases conversion rate (% lift)
    #   bid_efficiency   - reduces CPC at same position (% CPC reduction)
    #   coverage_gain    - increases impression share (absolute IS pts gained)
    #   data_quality     - unlocks better bidding (no direct $ but enables Smart Bidding)
    # =========================================================================
    "impact_rules": [
        {
            "id": "negative_keywords",
            "title": "Comprehensive Negative Keyword List",
            "impact_type": "waste_reduction",
            "estimated_waste_pct": [20, 42],
            "estimated_cpa_reduction_pct": [0, 42],
            "priority": "critical",
            "effort": "low",
            "timeframe": "this_week",
            "description": (
                "Practices with strong negatives reduce wasted spend by 20-42% and lower "
                "CPA by up to 42%. Build 100+ negatives before launch covering: jobs/careers, "
                "free/low-cost seekers, DIY/consumer products, educational/research queries, "
                "wrong geography, and competitor brands. Review Search Terms Report weekly."
            ),
            "categories": [
                "jobs_careers", "free_low_cost", "diy_consumer_products",
                "educational_research", "wrong_geography", "competitor_brands",
            ],
            "triggers": ["no_negatives", "search_terms_unreviewed", "low_negative_count"],
            "review_cadence": "weekly",
        },
        {
            "id": "service_specific_landing_pages",
            "title": "Service-Specific Landing Pages",
            "impact_type": "conversion_lift",
            "estimated_lift_pct": [30, 50],
            "priority": "critical",
            "effort": "high",
            "timeframe": "month_1",
            "description": (
                "Sending paid traffic to a practice homepage instead of a service-specific "
                "landing page is the single most costly structural error in dental PPC. "
                "Service-specific landing pages increase conversion rates by 30-50%."
            ),
            "triggers": ["homepage_as_final_url", "no_service_lp", "single_lp_for_all_campaigns"],
        },
        {
            "id": "campaign_split_by_service",
            "title": "Split Into Service-Specific Campaigns",
            "impact_type": "waste_reduction",
            "estimated_waste_pct": [20, 30],
            "priority": "high",
            "effort": "medium",
            "timeframe": "month_1",
            "description": (
                "Single campaign mixes intent types, prevents granular budget control, and "
                "forces algorithm to serve one ad strategy across wildly different searcher "
                "needs. Splitting + redistributing existing budget into service-specific "
                "campaigns with proper negative lists typically recovers 20-30% of spend "
                "from irrelevant clicks."
            ),
            "recommended_campaigns": [
                {"name": "General / New Patient", "daily": 15, "priority": "high"},
                {"name": "Emergency Dental",      "daily": 20, "priority": "critical"},
                {"name": "Dental Implants",       "daily": 15, "priority": "high"},
                {"name": "Invisalign",            "daily": 10, "priority": "medium"},
                {"name": "Cosmetic Dentistry",    "daily": 8,  "priority": "medium"},
                {"name": "Brand Protection",      "daily": 5,  "priority": "critical"},
            ],
            "triggers": ["single_campaign_account", "campaign_count_lt_3"],
        },
        {
            "id": "ad_assets_full_deployment",
            "title": "Deploy All Ad Assets (Extensions)",
            "impact_type": "conversion_lift",
            "estimated_ctr_lift_pct": [10, 25],
            "priority": "high",
            "effort": "low",
            "timeframe": "this_week",
            "description": (
                "Ad assets improve CTR by 10-25% at zero additional cost per click. They "
                "increase ad real estate and contribute directly to Ad Rank. Running bare "
                "ads without assets is leaving significant performance on the table."
            ),
            "asset_impacts": {
                "call_assets":         {"ctr_lift_pct": [15, 25]},
                "location_assets":     {"ctr_lift_pct": [10, 20]},
                "sitelinks":           {"ctr_lift_pct": [10, 15], "min_count": 4},
                "callouts":            {"ctr_lift_pct": [5, 10],  "min_count": 4},
                "structured_snippets": {"ctr_lift_pct": [3, 8]},
                "image_assets":        {"ctr_lift_pct": [5, 10]},
                "promotion_assets":    {"ctr_lift_pct": [5, 10]},
            },
            "triggers": ["missing_call_asset", "missing_sitelinks", "missing_callouts",
                         "fewer_than_4_sitelinks", "no_image_assets"],
        },
        {
            "id": "geo_presence_only",
            "title": "Geographic Targeting - Presence Only",
            "impact_type": "waste_reduction",
            "estimated_waste_pct": [10, 25],
            "priority": "critical",
            "effort": "low",
            "timeframe": "today",
            "description": (
                "Google's default geo targeting is 'Presence OR Interest' - this includes "
                "people interested in your area but physically located elsewhere. For dental, "
                "change to 'Presence only'. Failure to make this change burns significant "
                "budget on out-of-area traffic."
            ),
            "recommended_radius_miles": {
                "urban":             [3, 5],
                "suburban":          [10, 15],   # Grafton, MA
                "rural":             [15, 25],
                "implants_specialty": [20, 30],
            },
            "triggers": ["geo_setting_presence_or_interest", "radius_gt_20_miles_general"],
        },
        {
            "id": "zip_code_bid_adjustments",
            "title": "Zip-Code Bid Adjustments from Geographic Report",
            "impact_type": "conversion_lift",
            "estimated_cvr_lift_pct": [50, 150],
            "priority": "medium",
            "effort": "low",
            "timeframe": "month_2",
            "description": (
                "After 60+ days of data, run Geographic Report and apply: +15-30% on "
                "high-converting zips, no adjustment on medium, -15-30% or exclude on "
                "non-converting zips. A comparable dental advertiser achieved a 150% "
                "conversion-rate increase purely from zip-level bid adjustments."
            ),
            "adjustments": {
                "high_converting":   {"adjustment_pct": [15, 30]},
                "medium_converting": {"adjustment_pct": 0},
                "non_converting":    {"adjustment_pct": [-30, -15], "alternative": "exclude"},
            },
            "data_minimum_days": 60,
            "triggers": ["no_geo_bid_adjustments", "60_days_data_available"],
        },
        {
            "id": "call_tracking_full",
            "title": "Full Call Tracking with Min Duration",
            "impact_type": "data_quality",
            "priority": "critical",
            "effort": "medium",
            "timeframe": "2_weeks",
            "description": (
                "Most dental practices track only form fills and miss the majority of "
                "conversions. Set minimum call duration of 60-120 seconds (90s recommended) "
                "to filter wrong numbers and hang-ups. Use Google call assets + Dynamic "
                "Number Insertion (CallRail/WhatConverts) for website calls."
            ),
            "min_call_duration_seconds": [60, 120],
            "recommended_seconds": 90,
            "triggers": ["form_only_tracking", "no_call_tracking", "no_min_call_duration"],
        },
        {
            "id": "offline_conversion_import",
            "title": "Offline Conversion Import (OpenDental -> Google Ads)",
            "impact_type": "data_quality",
            "priority": "strategic",
            "effort": "high",
            "timeframe": "quarter_2",
            "description": (
                "Highest-value tracking advancement available to GDC. GCLID flows from ad "
                "click through landing page form -> OpenDental booking -> revenue back to "
                "Google Ads via offline conversion import. Once revenue values are flowing, "
                "GDC can upgrade to Target ROAS - the algorithm shifts budget toward "
                "highest-revenue patients, not just the most form fills."
            ),
            "unlocks": ["target_roas_bidding", "data_driven_attribution"],
            "triggers": ["no_offline_conversion_import", "no_gclid_capture",
                         "od_revenue_not_uploaded"],
        },
        {
            "id": "branded_campaign",
            "title": "Branded Keyword Campaign",
            "impact_type": "conversion_lift",
            "estimated_cvr_lift_pct": [30, 50],
            "priority": "critical",
            "effort": "low",
            "timeframe": "this_week",
            "description": (
                "Branded keywords cost $0.50-$1.50 CPC. Conversion rates are 30-50% higher "
                "than non-branded. If you do not bid on your own brand, a competitor can "
                "appear above you in your own brand search results."
            ),
            "recommended_daily_budget": [5, 10],
            "expected_cpc": [0.50, 1.50],
            "triggers": ["no_brand_campaign", "competitor_bidding_on_brand"],
        },
        {
            "id": "rsa_full_headlines",
            "title": "Fill All 15 RSA Headline Slots",
            "impact_type": "conversion_lift",
            "estimated_clicks_lift_pct": [10, 15],
            "estimated_conv_lift_pct": [10, 15],
            "priority": "high",
            "effort": "low",
            "timeframe": "this_week",
            "description": (
                "Advertisers using all 15 headlines see 10-15% more clicks and conversions. "
                "Cover all 6 categories: keyword-focused, value proposition, social proof, "
                "offer/incentive, urgency/CTA, trust/comfort."
            ),
            "headline_categories": [
                "keyword_focused", "value_proposition", "social_proof",
                "offer_incentive", "urgency_cta", "trust_comfort",
            ],
            "triggers": ["rsa_headlines_lt_15", "rsa_descriptions_lt_4"],
        },
        {
            "id": "rsa_short_headlines",
            "title": "Use Short Headlines (<20 chars)",
            "impact_type": "bid_efficiency",
            "cpa_short_chars": 9.35,         # under 20 chars
            "cpa_long_chars": 18.27,         # over 20 chars
            "ctr_short_chars": 11.77,        # %
            "ctr_long_chars": 10.52,         # %
            "priority": "medium",
            "effort": "low",
            "timeframe": "this_week",
            "description": (
                "Headlines under 20 characters deliver CPA of $9.35 vs $18.27 for longer "
                "headlines, with 11.77% CTR vs 10.52%."
            ),
            "char_threshold": 20,
            "triggers": ["avg_headline_length_gt_20"],
        },
        {
            "id": "no_pinning",
            "title": "Avoid Headline Pinning",
            "impact_type": "conversion_lift",
            "priority": "medium",
            "effort": "low",
            "description": (
                "Pinning even one headline cuts Google's testing potential by 75% and "
                "typically drops Ad Strength. Do not pin unless required for compliance."
            ),
            "testing_potential_loss_pct": 75,
            "triggers": ["pinned_headlines_present"],
        },
        {
            "id": "match_type_strategy",
            "title": "Correct Match Type Strategy (2025)",
            "impact_type": "waste_reduction",
            "estimated_waste_pct": [30, 40],
            "priority": "high",
            "effort": "low",
            "description": (
                "Exact Match delivers best CPA (top-performing in 70.79% of accounts). "
                "Phrase Match is the recommended default for new keywords. Broad Match "
                "should ONLY be used once Smart Bidding is active - Broad Match without "
                "Smart Bidding routinely wastes 30-40% of budget. Enhanced CPC was "
                "deprecated by Google in March 2025 - do not use it."
            ),
            "triggers": ["broad_match_with_manual_cpc", "enhanced_cpc_in_use"],
        },
        {
            "id": "quality_score_improvement",
            "title": "Quality Score Improvement Path",
            "impact_type": "bid_efficiency",
            "estimated_cpc_reduction_pct": [40, 50],
            "priority": "high",
            "effort": "medium",
            "description": (
                "QS of 10 vs QS of 5 means you can pay half the CPC for the same ad position. "
                "Improving QS from 4 to 8 can reduce CPC by 40-50% while maintaining or "
                "improving position. Path: 1) separate campaigns by service, 2) build "
                "service-specific landing pages, 3) add all assets, 4) review keyword-level "
                "QS monthly and pause keywords scoring 3 or below."
            ),
            "qs_pause_threshold": 3,
            "review_cadence": "monthly",
            "triggers": ["keywords_with_qs_lt_4", "low_avg_quality_score"],
        },
        {
            "id": "budget_lost_is_remediation",
            "title": "Increase Budget When Search Budget Lost IS > 20%",
            "impact_type": "coverage_gain",
            "priority": "high",
            "effort": "low",
            "description": (
                "When search_budget_lost_is > 20%, increase daily budget rather than bids. "
                "Raising bids on a budget-capped campaign does nothing except raise your "
                "average CPC with no additional impressions."
            ),
            "thresholds": {
                "search_is_lt_40":         {"action": "increase_budget_or_tighten_targeting"},
                "search_budget_lost_gt_20": {"action": "increase_budget", "do_not": "raise_bids"},
                "search_rank_lost_gt_30":  {"action": "improve_qs_or_raise_bids"},
            },
            "triggers": ["search_budget_lost_is_gt_20"],
        },
        {
            "id": "rank_lost_is_remediation",
            "title": "Improve QS or Raise Bids When Search Rank Lost IS > 30%",
            "impact_type": "coverage_gain",
            "priority": "high",
            "effort": "medium",
            "description": (
                "Search Rank Lost IS > 30% means bids or Quality Score are too low. "
                "Improve ad relevance / landing page, or raise bids."
            ),
            "triggers": ["search_rank_lost_is_gt_30"],
        },
        {
            "id": "page_speed",
            "title": "Landing Page Speed (<3s mobile)",
            "impact_type": "conversion_lift",
            "estimated_cvr_loss_per_extra_sec_pct": 7,
            "priority": "high",
            "effort": "medium",
            "description": (
                "Each additional second of load time reduces conversion probability by 7%. "
                "Target under 3 seconds on mobile. Page speed affects both conversion rate "
                "and Quality Score (Core Web Vitals are part of Ad Rank)."
            ),
            "target_load_seconds": 3,
            "triggers": ["lp_load_time_gt_3s"],
        },
        {
            "id": "smart_bidding_too_early",
            "title": "Avoid Switching to Smart Bidding Too Early",
            "impact_type": "waste_reduction",
            "priority": "high",
            "effort": "low",
            "description": (
                "Moving to Smart Bidding before 30+ conversions/month causes erratic bidding "
                "with insufficient data. Wait for the threshold; until then use Manual CPC "
                "or (15-30/mo) Maximize Conversions as a bridge."
            ),
            "min_conversions_for_target_cpa": 30,
            "min_conversions_for_target_roas": 50,
            "triggers": ["target_cpa_with_lt_30_conv", "target_roas_with_lt_50_conv"],
        },
        {
            "id": "weekly_account_review",
            "title": "Weekly Account Review",
            "impact_type": "waste_reduction",
            "priority": "high",
            "effort": "low",
            "description": (
                "Account performance decays and competitor changes go undetected without "
                "weekly oversight. Weekly: Search Terms audit + pacing check. Monthly: RSA "
                "asset review + QS check."
            ),
            "weekly_tasks": ["search_terms_audit", "pacing_check"],
            "monthly_tasks": ["rsa_asset_review", "qs_check"],
            "triggers": ["search_terms_unreviewed_7d", "no_pacing_check_7d"],
        },
        {
            "id": "after_hours_emergency_pause",
            "title": "Pause Emergency Campaign 10 PM-7 AM Without Answering Service",
            "impact_type": "waste_reduction",
            "priority": "medium",
            "effort": "low",
            "description": (
                "Emergency searches continue 24/7 but if you have no after-hours answering "
                "service, pause 10 PM-7 AM to avoid wasting budget on unanswered calls."
            ),
            "triggers": ["emergency_24_7_no_answering_service"],
        },
        {
            "id": "device_bid_adjustments",
            "title": "Device Bid Adjustments After 60 Days",
            "impact_type": "bid_efficiency",
            "priority": "medium",
            "effort": "low",
            "description": (
                "60%+ of dental searches are mobile. Performance varies by service. After "
                "60+ days, check Device Report and apply adjustments. For emergency, expect "
                "to add +15-25% on mobile."
            ),
            "emergency_mobile_adjustment_pct": [15, 25],
            "triggers": ["no_device_adjustments_after_60d"],
        },
    ],

    # =========================================================================
    # 3. TARGET CPA BY SERVICE (Section 2.1, 2.2)
    # =========================================================================
    "target_cpa_by_service": {
        "emergency": {
            "low": 75, "high": 125,
            "rationale": "High urgency = high close rate, fast conversion",
            "first_procedure_value": [200, 600],
            "long_term_patient_value": [3000, 8000],
        },
        "general_new_patient": {
            "low": 100, "high": 175,
            "first_year_value": [800, 1200],
            "max_cpa": [80, 240],
        },
        "invisalign": {
            "low": 150, "high": 300,
            "case_value": [4500, 8000],
            "max_cpa": [450, 1600],
        },
        "dental_implants": {
            "low": 200, "high": 400,
            "case_value_single": [3500, 6000],
            "case_value_full": [5000, 30000],
            "max_cpa": [350, 1200],
            "note": "At $300 CPA / $5000 case = 16:1 ROAS",
        },
    },

    # =========================================================================
    # 4. BIDDING PHASES (Section 2.1 - Smart Bidding Progression)
    # =========================================================================
    "bidding_phases": [
        {
            "phase": 1,
            "name": "Launch",
            "conversions_per_month": [0, 15],
            "strategy": "MANUAL_CPC",
            "action": "Build data; review bids weekly; set bids based on target CPA x estimated CVR",
        },
        {
            "phase": 2,
            "name": "Emerging",
            "conversions_per_month": [15, 30],
            "strategy": "MAXIMIZE_CONVERSIONS",
            "action": "Bridge strategy; accelerates data collection; monitor CPA daily",
            "note": "No target set",
        },
        {
            "phase": 3,
            "name": "Mature",
            "conversions_per_month": [30, 50],
            "strategy": "TARGET_CPA",
            "action": "Set initial target 20% above current actual CPA; tighten over 4-8 weeks",
            "initial_target_offset_pct": 20,
            "tighten_period_weeks": [4, 8],
        },
        {
            "phase": 4,
            "name": "Advanced",
            "conversions_per_month": [50, 999],
            "strategy": "TARGET_ROAS",
            "action": "Requires offline conversion import (OpenDental revenue) flowing into Google Ads",
            "prerequisite": "offline_conversion_import_active",
        },
    ],

    # =========================================================================
    # 5. QUICK WINS - High-impact, low-effort gaps (Priority Action Plan top 5)
    # =========================================================================
    "quick_wins": [
        {
            "id": "negatives_100_plus",
            "title": "Build 100+ Negative Keyword List",
            "impact_description": "Recovers 20-42% of wasted ad spend",
            "estimated_impact_pct": [20, 42],
            "effort": "low",
            "timeframe": "this_week",
            "rank": 1,
        },
        {
            "id": "geo_presence_only_today",
            "title": "Change Geo Targeting to 'Presence Only'",
            "impact_description": "Stops budget burn on out-of-area searchers",
            "estimated_impact_pct": [10, 25],
            "effort": "low",
            "timeframe": "today",
            "rank": 2,
            "time_to_complete_min": 5,
        },
        {
            "id": "call_asset_with_schedule",
            "title": "Add Call Asset With Office-Hours Schedule",
            "impact_description": "+15-25% CTR; routes calls only when staff can answer",
            "estimated_impact_pct": [15, 25],
            "effort": "low",
            "timeframe": "today",
            "rank": 3,
        },
        {
            "id": "asset_full_set",
            "title": "Add Sitelinks (4+), Callouts (4+), Structured Snippets",
            "impact_description": "+10-25% CTR at zero additional cost",
            "estimated_impact_pct": [10, 25],
            "effort": "low",
            "timeframe": "this_week",
            "rank": 4,
        },
        {
            "id": "call_tracking_90s",
            "title": "Implement Call Tracking With 90s Min Duration",
            "impact_description": "Captures majority of conversions; filters spam/hangups; feeds Smart Bidding",
            "effort": "medium",
            "timeframe": "2_weeks",
            "rank": 5,
        },
        {
            "id": "branded_campaign_5_per_day",
            "title": "Launch Branded Keyword Campaign (~$5/day)",
            "impact_description": "Highest-intent searches; 30-50% higher CVR; blocks competitors",
            "estimated_impact_pct": [30, 50],
            "effort": "low",
            "timeframe": "this_week",
            "rank": 8,
            "recommended_daily_budget": 5,
        },
        {
            "id": "weekly_search_terms_review",
            "title": "Weekly Search Terms Report Review",
            "impact_description": "Add new negatives every week; expect 40-60 in month 1, then 10-20/mo",
            "effort": "low",
            "timeframe": "weekly",
            "rank": 9,
            "month_1_expected_negatives": [40, 60],
            "monthly_expected_negatives": [10, 20],
        },
    ],

    # =========================================================================
    # 6. COMMON MISTAKES - Top 10 (Section 14)
    # =========================================================================
    "common_mistakes": [
        {
            "id": "homepage_traffic",
            "rank": 1,
            "mistake": "Sending all traffic to homepage",
            "impact_description": "30-50% lower conversion rate",
            "estimated_impact_pct": [30, 50],
            "fix": "Build service-specific landing pages for each campaign",
        },
        {
            "id": "no_negatives",
            "rank": 2,
            "mistake": "No negative keyword list",
            "impact_description": "20-42% of budget wasted on irrelevant clicks",
            "estimated_impact_pct": [20, 42],
            "fix": "Build 100+ negatives before launch; review Search Terms weekly",
        },
        {
            "id": "broad_no_smart_bidding",
            "rank": 3,
            "mistake": "Broad match without Smart Bidding",
            "impact_description": "30-40% budget waste on mismatched searches",
            "estimated_impact_pct": [30, 40],
            "fix": "Start with Exact + Phrase Match; add Broad only with Target CPA",
        },
        {
            "id": "single_campaign",
            "rank": 4,
            "mistake": "Everything in one campaign",
            "impact_description": "Budget cannibalization; algorithm cannot optimize",
            "fix": "Separate campaigns by service with dedicated budgets",
        },
        {
            "id": "premature_smart_bidding",
            "rank": 5,
            "mistake": "Switching to Smart Bidding too early",
            "impact_description": "Erratic bidding; algorithm has insufficient data",
            "fix": "Wait for 30+ conversions/month before enabling Target CPA",
        },
        {
            "id": "form_only_tracking",
            "rank": 6,
            "mistake": "Tracking only form fills (no calls)",
            "impact_description": "Missing majority of conversions; Smart Bidding starved of data",
            "fix": "Implement call tracking (Google + DNI); add 60-120s minimum duration",
        },
        {
            "id": "no_assets",
            "rank": 7,
            "mistake": "No ad assets / extensions",
            "impact_description": "10-25% lower CTR at zero extra cost",
            "estimated_impact_pct": [10, 25],
            "fix": "Deploy call, location, sitelinks, callouts, structured snippets immediately",
        },
        {
            "id": "wide_geo",
            "rank": 8,
            "mistake": "Overly wide geographic targeting",
            "impact_description": "Impressions and clicks from non-service-area patients",
            "fix": "Set 'Presence only'; start 10-12 mile radius; exclude non-converting zones",
        },
        {
            "id": "generic_ad_copy",
            "rank": 9,
            "mistake": "Generic ad copy with no differentiator",
            "impact_description": "Low CTR; poor Quality Score; high CPC",
            "fix": "Lead with outcome, offer, urgency; use all 15 headline slots; include social proof",
        },
        {
            "id": "no_weekly_monitoring",
            "rank": 10,
            "mistake": "Not monitoring weekly",
            "impact_description": "Account performance decays; competitor changes go undetected",
            "fix": "Weekly: Search Terms audit + pacing check. Monthly: RSA asset review + QS check",
        },
    ],

    # =========================================================================
    # 7. AUXILIARY REFERENCE DATA
    # =========================================================================
    "ad_copy_themes": {
        # Section 5.3 - what converts for dental
        "high_converting": [
            {"theme": "same_day_immediate",     "example": "Same-Day Appointments Available",
             "use_for": ["emergency"]},
            {"theme": "financing_payment",      "example": "0% Financing Available / CareCredit Accepted",
             "use_for": ["implants", "invisalign", "cosmetic"]},
            {"theme": "anxiety_free",           "example": "Gentle, Anxiety-Free Dentistry",
             "use_for": ["all"]},
            {"theme": "new_patient_special",    "example": "New Patient Exam + X-Rays $99",
             "use_for": ["general"]},
            {"theme": "insurance_acceptance",   "example": "We Accept Delta Dental & Most PPOs",
             "use_for": ["all"]},
        ],
    },

    "landing_page_above_fold_requirements": [
        {"element": "h1_headline",      "requirement": "Mirror the search query"},
        {"element": "sub_headline",     "requirement": "Key differentiator: same-day, new patients, financing"},
        {"element": "phone_number",     "requirement": "Click-to-call, large font, top of page"},
        {"element": "cta_button",       "requirement": "Contrasting color (orange/green outperform blue); 'Book Now' or 'Call Now'"},
        {"element": "trust_signal",     "requirement": "Star rating with count, or years of experience"},
        {"element": "hero_image",       "requirement": "Real practice/team photo (not stock)"},
    ],

    "landing_page_form_max_fields": 4,
    "landing_page_form_fields": ["name", "phone", "email", "reason_for_visit"],

    "ad_strength_finding": {
        "average_qs_cpa": 12.43,
        "average_qs_cvr": 12.65,
        "excellent_qs_cpa": 28.68,
        "excellent_qs_cvr": 4.97,
        "note": "Average Ad Strength outperforms Excellent on actual conversion data",
    },

    "exact_match_top_performance_pct_of_accounts": 70.79,

    "below_fold_ignored_pct": 80,

    "video_testimonial_retention": {
        "video_pct": 95,
        "text_pct": 12,
    },

    "front_desk_factor": {
        "minimum_acceptable_close_rate_pct": 40,
        "warning": "Patient acquisition cost doubles or triples when inbound call close rates fall below 40%",
        "note": "Highest-leverage non-digital optimization available to GDC",
    },

    "attribution_model": {
        "default": "DATA_DRIVEN",
        "min_conversions_for_dda": 30,
        "fallback_below_threshold": "LAST_CLICK",
    },

    # =========================================================================
    # 8. METADATA
    # =========================================================================
    "_meta": {
        "source": "GDC_Google_Ads_Excellence_Report.docx",
        "research_model": "Claude Opus 4",
        "report_date": "May 2026",
        "market": "Suburban Boston (Grafton, MA)",
        "current_state_estimate": {
            "daily_budget_usd": 68,
            "monthly_budget_usd": 2040,
            "bidding_strategy": "MANUAL_CPC",
            "campaign_count": 1,
            "landing_pages": "homepage_or_1_page",
        },
        "domains_covered": 14,
    },
}


def get_benchmark(metric: str):
    """Convenience accessor for benchmark values."""
    return EXCELLENCE_RULES["benchmarks"].get(metric)


def get_target_cpa(service: str):
    """Get target CPA range for a dental service category."""
    return EXCELLENCE_RULES["target_cpa_by_service"].get(service)


def get_bidding_phase_for_volume(monthly_conversions: float):
    """Return the recommended bidding phase given a monthly conversion count."""
    for phase in EXCELLENCE_RULES["bidding_phases"]:
        lo, hi = phase["conversions_per_month"]
        if lo <= monthly_conversions < hi:
            return phase
    return EXCELLENCE_RULES["bidding_phases"][-1]


def get_impact_rule(rule_id: str):
    """Look up an impact rule by id."""
    for rule in EXCELLENCE_RULES["impact_rules"]:
        if rule["id"] == rule_id:
            return rule
    return None


def rules_triggered(account_state: dict):
    """
    Given a dict of account-state flags, return all impact rules whose
    `triggers` overlap with the flags set to True in account_state.

    Example:
        rules_triggered({"no_negatives": True, "homepage_as_final_url": True})
    """
    active_flags = {k for k, v in account_state.items() if v}
    matched = []
    for rule in EXCELLENCE_RULES["impact_rules"]:
        triggers = set(rule.get("triggers", []))
        if triggers & active_flags:
            matched.append(rule)
    return matched
