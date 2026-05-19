"""
Google Ads campaign creation and lifecycle management.

Handles:
  - Campaign status changes (pause / resume / remove)
  - Fetching existing campaigns from Google Ads account (read-only)
  - Full campaign creation: budget → campaign → geo → ad groups → keywords → RSAs → extensions

All write operations check the kill switch before executing.
New campaigns are always created as PAUSED first, then ENABLED at the end.
"""
import logging
import json
import re
from config import get_settings

logger = logging.getLogger(__name__)

# ── Ad Schedule helpers ───────────────────────────────────────────────────────

_DAY_ALIASES = {
    "monday": "MONDAY", "mon": "MONDAY", "m": "MONDAY",
    "tuesday": "TUESDAY", "tue": "TUESDAY", "tues": "TUESDAY", "t": "TUESDAY",
    "wednesday": "WEDNESDAY", "wed": "WEDNESDAY", "w": "WEDNESDAY",
    "thursday": "THURSDAY", "thu": "THURSDAY", "thurs": "THURSDAY", "th": "THURSDAY",
    "friday": "FRIDAY", "fri": "FRIDAY", "f": "FRIDAY",
    "saturday": "SATURDAY", "sat": "SATURDAY", "sa": "SATURDAY",
    "sunday": "SUNDAY", "sun": "SUNDAY", "su": "SUNDAY",
}
_DAY_ORDER = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]


def parse_ad_schedule(value) -> list[dict]:
    """
    Parse an ad schedule value into a list of {day, start_hour, end_hour} dicts.

    Accepts:
      - Already-parsed list of dicts: [{"day": "MONDAY", "start_hour": 7, "end_hour": 23}, ...]
      - JSON string of the above
      - Free-text like "Mon-Thu 7am-11pm" or "Monday to Thursday 9am to 6pm"
      - "24/7" or "always" → all 7 days 0–24
      - "weekdays 9am-5pm" → Mon-Fri
      - "weekends 10am-3pm" → Sat-Sun

    Returns [] if nothing parseable found (caller should skip/warn).
    Hours use Google Ads convention: start_hour 0–23, end_hour 1–24 (24 = midnight).
    """
    if not value:
        return []

    # Already structured
    if isinstance(value, list):
        return [s for s in value if isinstance(s, dict) and "day" in s]

    # Try JSON parse first
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    return [s for s in parsed if isinstance(s, dict) and "day" in s]
            except Exception:
                pass

    text = value.strip().lower() if isinstance(value, str) else ""

    # Parse hour string like "7am", "11pm", "9:30am" → integer hour (round to hour)
    def _parse_hour(h: str) -> int | None:
        h = h.strip()
        m = re.match(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", h)
        if not m:
            return None
        hr = int(m.group(1))
        period = m.group(3) or ""
        if period == "pm" and hr != 12:
            hr += 12
        elif period == "am" and hr == 12:
            hr = 0
        return hr

    # Parse day range like "mon-thu", "monday to friday", "weekdays", "weekends"
    def _expand_days(day_text: str) -> list[str]:
        day_text = day_text.strip()
        # Shorthand groups
        if day_text in ("weekdays", "weekday", "week days"):
            return ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"]
        if day_text in ("weekends", "weekend", "week ends"):
            return ["SATURDAY", "SUNDAY"]
        if day_text in ("everyday", "every day", "daily", "all week", "24/7", "always"):
            return list(_DAY_ORDER)
        # Range: "mon-thu" or "monday to thursday"
        range_m = re.match(r"([a-z]+)\s*(?:-|to)\s*([a-z]+)", day_text)
        if range_m:
            start_day = _DAY_ALIASES.get(range_m.group(1))
            end_day   = _DAY_ALIASES.get(range_m.group(2))
            if start_day and end_day:
                si = _DAY_ORDER.index(start_day)
                ei = _DAY_ORDER.index(end_day)
                if ei >= si:
                    return _DAY_ORDER[si:ei+1]
                # Wrap-around (e.g. fri-mon) — unusual, expand linearly
                return _DAY_ORDER[si:] + _DAY_ORDER[:ei+1]
        # Single day
        single = _DAY_ALIASES.get(day_text)
        if single:
            return [single]
        return []

    # Special case: 24/7
    if re.search(r"24\s*/\s*7|always|all day|all week", text):
        return [{"day": d, "start_hour": 0, "end_hour": 24} for d in _DAY_ORDER]

    # Main pattern: "<days> <start_hour>-<end_hour>" or "<days> <start_hour> to <end_hour>"
    # e.g. "mon-thu 7am-11pm" / "monday to thursday 9am to 6pm"
    pattern = re.compile(
        r"([a-z]+(?:\s*(?:-|to)\s*[a-z]+)?)"   # day or range
        r"\s+"
        r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)"   # start hour
        r"\s*(?:-|to)\s*"
        r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)",  # end hour
        re.IGNORECASE
    )
    results = []
    for m in pattern.finditer(text):
        days = _expand_days(m.group(1).strip())
        start = _parse_hour(m.group(2))
        end   = _parse_hour(m.group(3))
        if not days or start is None or end is None:
            continue
        if end == 0:
            end = 24  # midnight expressed as 12am → 24
        for day in days:
            results.append({"day": day, "start_hour": start, "end_hour": end})

    return results


def push_ad_schedule(client, customer_id: str, campaign_resource: str,
                     schedule: list[dict], replace: bool = True) -> dict:
    """
    Push an ad schedule to Google Ads for a campaign.

    Args:
        client:            GoogleAdsClient
        customer_id:       str digits-only
        campaign_resource: campaigns/XXXXXXX resource name
        schedule:          list of {day, start_hour, end_hour}
        replace:           if True, remove all existing schedule criteria first

    Returns: {"ok": bool, "pushed": int, "removed": int, "error": str|None}
    """
    service = client.get_service("CampaignCriterionService")
    removed = 0

    if replace:
        # Fetch existing ad_schedule criteria and remove them
        try:
            ga_service = client.get_service("GoogleAdsService")
            query = f"""
                SELECT campaign_criterion.resource_name, campaign_criterion.type
                FROM campaign_criterion
                WHERE campaign_criterion.campaign = '{campaign_resource}'
                  AND campaign_criterion.type = 'AD_SCHEDULE'
            """
            existing = list(ga_service.search(customer_id=customer_id, query=query))
            if existing:
                remove_ops = []
                for row in existing:
                    op = client.get_type("CampaignCriterionOperation")
                    op.remove = row.campaign_criterion.resource_name
                    remove_ops.append(op)
                service.mutate_campaign_criteria(customer_id=customer_id, operations=remove_ops)
                removed = len(remove_ops)
        except Exception as e:
            logger.warning(f"push_ad_schedule: remove existing failed (continuing): {e}")

    if not schedule:
        return {"ok": True, "pushed": 0, "removed": removed, "error": None}

    day_enum = client.enums.DayOfWeekEnum
    ops = []
    for slot in schedule:
        day_str = (slot.get("day") or "").upper()
        start_h = int(slot.get("start_hour", 0))
        end_h   = int(slot.get("end_hour", 24))
        if not day_str or start_h >= end_h:
            continue
        try:
            day_val = day_enum[day_str]
        except KeyError:
            logger.warning(f"push_ad_schedule: unknown day '{day_str}' — skipping")
            continue
        op = client.get_type("CampaignCriterionOperation")
        c  = op.create
        c.campaign = campaign_resource
        c.ad_schedule.day_of_week  = day_val
        c.ad_schedule.start_hour   = start_h
        c.ad_schedule.start_minute = client.enums.MinuteOfHourEnum.ZERO
        c.ad_schedule.end_hour     = end_h
        c.ad_schedule.end_minute   = client.enums.MinuteOfHourEnum.ZERO
        ops.append(op)

    if not ops:
        return {"ok": True, "pushed": 0, "removed": removed, "error": "no valid schedule slots"}

    try:
        resp = service.mutate_campaign_criteria(customer_id=customer_id, operations=ops)
        return {"ok": True, "pushed": len(resp.results), "removed": removed, "error": None}
    except Exception as e:
        return {"ok": False, "pushed": 0, "removed": removed, "error": str(e)}

# ── Proximity targeting: hardcoded lat/lng table ──────────────────────────────
# Google Ads ProximityCriterion REQUIRES geo_point (lat/lng in micro-degrees).
# Sending only city_name is unreliable: the API may fail or silently geocode to
# a different city (there are Graftons in WI, OH, NH, VT, ND...).
# The address sub-message is display-only and does NOT drive ad serving.
_KNOWN_CITY_LATLNG: dict[tuple[str, str], tuple[float, float]] = {
    # Grafton MA + surrounding towns within 20 miles
    ("grafton",       "MA"): (42.2012, -71.6870),
    ("worcester",     "MA"): (42.2626, -71.8023),
    ("shrewsbury",    "MA"): (42.2959, -71.7128),
    ("westborough",   "MA"): (42.2695, -71.6162),
    ("northborough",  "MA"): (42.3195, -71.6412),
    ("southborough",  "MA"): (42.3048, -71.5220),
    ("upton",         "MA"): (42.1737, -71.6034),
    ("hopkinton",     "MA"): (42.2287, -71.5226),
    ("milford",       "MA"): (42.1395, -71.5161),
    ("sutton",        "MA"): (42.1498, -71.7659),
    ("millbury",      "MA"): (42.1953, -71.7603),
    ("auburn",        "MA"): (42.1948, -71.8356),
    ("leicester",     "MA"): (42.2473, -71.9070),
    ("spencer",       "MA"): (42.2456, -71.9923),
    ("holden",        "MA"): (42.3537, -71.8620),
    ("boylston",      "MA"): (42.3501, -71.7173),
    ("berlin",        "MA"): (42.3812, -71.6384),
    ("hudson",        "MA"): (42.3918, -71.5662),
}


def _resolve_city_latlng(
    city: str,
    state: str,
    log: list,
) -> tuple[float | None, float | None]:
    """
    Return (lat, lng) for a city/state pair.

    Tries the hardcoded _KNOWN_CITY_LATLNG table first (exact match after
    normalisation). Returns (None, None) when the city is unknown so the
    caller can surface a clear error rather than letting the API silently
    drop the criterion.

    To add a new city: look up coordinates on Google Maps, add an entry to
    _KNOWN_CITY_LATLNG above (key is (city.lower(), state.upper())).
    """
    key = (city.strip().lower(), (state or "MA").strip().upper())
    coords = _KNOWN_CITY_LATLNG.get(key)
    if coords:
        return coords
    log.append(
        f"  ⚠ City '{city}, {state}' not in lat/lng table — cannot create proximity criterion. "
        f"Add it to _KNOWN_CITY_LATLNG in google_ads_create.py."
    )
    return (None, None)


# Google Ads prohibits phone numbers in ad text (PHONE_NUMBER_IN_AD_TEXT policy).
# This regex catches common US formats: 508-318-4477, (508) 318-4477, 508.318.4477, 5083184477
_PHONE_RE = re.compile(r'\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}|\b\d{10}\b')

def _strip_phone_numbers(text: str) -> str:
    """Remove phone numbers from ad text to avoid PHONE_NUMBER_IN_AD_TEXT policy rejection."""
    return _PHONE_RE.sub('', text).strip()


# ── AI self-healing for ad-copy policy violations ────────────────────────────
_MAX_AI_FIXES = 2  # per ad group — caps total AI retry attempts

def _extract_policy_topics(gads_exc) -> list:
    """
    Walk a GoogleAdsException and pull out the policy topic strings
    (e.g. 'PHONE_NUMBER_IN_AD_TEXT', 'TRADEMARK', 'COPYRIGHTED_CONTENT').

    Returns [] if the exception is not a policy error — caller should
    treat an empty list as "non-policy failure, do not attempt AI fix".
    Handles both modern policy_finding_error and legacy policy_violation_error.
    """
    topics = []
    failure = getattr(gads_exc, "failure", None)
    if failure is None:
        return topics
    for err in getattr(failure, "errors", []) or []:
        ec = getattr(err, "error_code", None)
        if ec is None:
            continue
        # Modern shape: POLICY_FINDING
        pfe = getattr(ec, "policy_finding_error", 0)
        if pfe:
            details = getattr(err, "details", None)
            pfd = getattr(details, "policy_finding_details", None) if details else None
            for entry in getattr(pfd, "policy_topic_entries", []) or []:
                t = getattr(entry, "topic", None)
                if t:
                    topics.append(str(t))
        # Legacy shape: POLICY_VIOLATION
        pve = getattr(ec, "policy_violation_error", 0)
        if pve:
            details = getattr(err, "details", None)
            pvd = getattr(details, "policy_violation_details", None) if details else None
            key = getattr(pvd, "key", None) if pvd else None
            name = getattr(key, "policy_name", None) if key else None
            if name:
                topics.append(str(name))
    # Dedupe, preserve order
    seen = set()
    return [t for t in topics if not (t in seen or seen.add(t))]


def _ai_fix_ad_copy(headlines, descriptions, policy_topics, ad_group_name, ai_client):
    """
    Ask Claude Sonnet to rewrite headlines/descriptions that violated Google Ads policy.

    Args:
        headlines:      list of str — the rejected headlines
        descriptions:   list of str — the rejected descriptions
        policy_topics:  list of str — e.g. ['PHONE_NUMBER_IN_AD_TEXT']
        ad_group_name:  str — for context in the prompt
        ai_client:      anthropic.Anthropic() instance

    Returns:
        (fixed_headlines, fixed_descriptions) — both truncated, deduped, ready for AdTextAsset

    Raises:
        ValueError on malformed JSON, insufficient assets, or empty AI output
        RuntimeError if ai_client is None
    """
    if ai_client is None:
        raise RuntimeError("AI client not available for ad-copy self-heal")

    topics_str = ", ".join(policy_topics) if policy_topics else "POLICY_FINDING (topic unspecified)"

    system_msg = (
        "You are a Google Ads copywriter. You rewrite ad copy to pass Google's "
        "policy review while preserving the original intent and call-to-action."
    )

    user_msg = f"""Google Ads rejected the following ad for ad group "{ad_group_name}".

Violated policy topics: {topics_str}

Current headlines (max 30 chars each, need at least 3):
{json.dumps(headlines, indent=2)}

Current descriptions (max 90 chars each, need at least 2):
{json.dumps(descriptions, indent=2)}

Rewrite the offending lines so they comply with these rules:
- No phone numbers anywhere
- No trademarks, brand names you do not own, or competitor names
- No copyrighted slogans
- No sexually suggestive, shocking, or unsubstantiated medical claims
- No ALL-CAPS gimmicks, excessive punctuation (!!!, ???), or emoji
- Headlines must be <= 30 characters, descriptions <= 90 characters
- Keep headlines unique (no duplicates after truncation)
- Preserve the original offer/intent — this is a dental practice ad

Return ONLY a JSON object, no markdown, no commentary:
{{"headlines": ["...", "...", ...], "descriptions": ["...", "...", ...]}}

Provide at least 5 headlines and 3 descriptions so we have room after dedup/filter."""

    response = ai_client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1500,
        system=system_msg,
        messages=[{"role": "user", "content": user_msg}],
    )
    raw = response.content[0].text.strip()

    match = re.search(r'\{[\s\S]*\}', raw)
    if not match:
        raise ValueError(f"AI fix returned no JSON object: {raw[:200]!r}")
    try:
        parsed = json.loads(match.group())
    except json.JSONDecodeError as je:
        raise ValueError(f"AI fix JSON decode failed: {je}; raw={raw[:200]!r}")

    fixed_h = parsed.get("headlines") or []
    fixed_d = parsed.get("descriptions") or []
    if not isinstance(fixed_h, list) or not isinstance(fixed_d, list):
        raise ValueError("AI fix returned non-list headlines or descriptions")

    def _clean(items, cap):
        out, seen = [], set()
        for it in items:
            s = it if isinstance(it, str) else str(it or "")
            s = _strip_phone_numbers(s).strip()[:cap]
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out

    fixed_h = _clean(fixed_h, 30)[:15]
    fixed_d = _clean(fixed_d, 90)[:4]

    if len(fixed_h) < 3:
        raise ValueError(f"AI fix produced only {len(fixed_h)} valid headlines (need 3+)")
    if len(fixed_d) < 2:
        raise ValueError(f"AI fix produced only {len(fixed_d)} valid descriptions (need 2+)")

    return fixed_h, fixed_d


# GAds sentinel dates that mean "not set" — strip these when importing
_GADS_DATE_SENTINELS = {"2037-12-30", "1970-01-01", "0001-01-01", ""}


def create_campaign_in_gads(campaign: dict, build: dict) -> dict:
    """
    Create a full Google Search campaign in Google Ads from dashboard data.

    Steps:
      1. Budget resource (daily budget)
      2. Campaign (PAUSED, Search, manual CPC)
      3. Geographic targeting (from geographic_targeting JSON)
      4. Ad groups (from build.ad_groups)
      5. Keywords — exact, phrase, broad per ad group (from build.keywords)
      6. Negative keywords (campaign-level)
      7. RSA ads (from build.ad_copy, one per ad group)
      8. Call extension (from call_extension_phone or practice phone)
      9. Enable campaign (ENABLED) — only after all assets created

    Args:
        campaign: dict from campaigns DB row (campaign_name, monthly_budget,
                  landing_page, call_extension_phone, geographic_targeting, etc.)
        build:    dict from campaign_build_json (keywords, ad_copy, ad_groups keys)

    Returns:
        {
            "ok": bool,
            "campaign_resource_name": str,
            "campaign_numeric_id": str,
            "ad_group_resources": [...],
            "keywords_added": int,
            "ads_created": int,
            "error": str | None,
            "log": [str, ...]   # step-by-step progress log
        }
    """
    from campaign_safety import check_writes_enabled, WriteBlockedError
    try:
        check_writes_enabled()
    except WriteBlockedError as e:
        return {"ok": False, "error": str(e), "log": [f"Blocked: {e}"]}

    settings = get_settings()
    customer_id = "".join(ch for ch in (settings.google_ads_customer_id or "") if ch.isdigit())
    if not customer_id:
        return {"ok": False, "error": "google_ads_customer_id not configured", "log": []}

    log = []

    try:
        client = _build_client()

        campaign_name    = campaign.get("campaign_name", "New Campaign")
        monthly_budget   = float(campaign.get("monthly_budget") or 0)
        daily_budget_usd = round(monthly_budget / 30.4, 2) if monthly_budget else 10.0
        # Fall back to practice_url (graftondentalcare.com) if no campaign-specific landing page is set.
        # NOTE: settings.practice_url must always be set to https://graftondentalcare.com in config.py.
        landing_page     = campaign.get("landing_page") or settings.practice_url or "https://graftondentalcare.com"
        call_phone       = campaign.get("call_extension_phone") or settings.office_phone or ""
        geo_json         = campaign.get("geographic_targeting") or ""
        start_date       = campaign.get("start_date") or ""

        kw_data    = build.get("keywords") or {}
        ac_data    = build.get("ad_copy") or {}
        ag_data    = build.get("ad_groups") or {}

        # ── Step 1: Budget resource ───────────────────────────────────────────
        log.append(f"Step 1: Creating daily budget ${daily_budget_usd}/day")
        budget_service = client.get_service("CampaignBudgetService")
        budget_op      = client.get_type("CampaignBudgetOperation")
        budget         = budget_op.create
        budget.name    = f"{campaign_name} Budget"
        budget.amount_micros = int(daily_budget_usd * 1_000_000)
        budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
        budget.explicitly_shared = False

        budget_response = budget_service.mutate_campaign_budgets(
            customer_id=customer_id, operations=[budget_op]
        )
        budget_resource = budget_response.results[0].resource_name
        log.append(f"  ✓ Budget created: {budget_resource}")

        # ── Step 2: Campaign (PAUSED) ─────────────────────────────────────────
        # Append a short timestamp suffix so retries never hit DUPLICATE_CAMPAIGN_NAME.
        # Previous partial attempts leave a paused campaign with the bare name.
        from datetime import datetime as _dt
        gads_campaign_name = f"{campaign_name} ({_dt.now().strftime('%m/%d %H:%M')})"
        log.append(f"Step 2: Creating campaign '{gads_campaign_name}' (PAUSED)")
        camp_service = client.get_service("CampaignService")
        camp_op      = client.get_type("CampaignOperation")
        camp         = camp_op.create
        camp.name    = gads_campaign_name
        camp.status  = client.enums.CampaignStatusEnum.PAUSED
        camp.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.SEARCH
        camp.campaign_budget = budget_resource

        # Bidding strategy — read from build JSON (ad_groups step writes launch_bidding_strategy).
        # Supported at launch: MANUAL_CPC (default), MAXIMIZE_CLICKS.
        # MAXIMIZE_CONVERSIONS is intentionally excluded — Google rejects smart bidding
        # on new campaigns with no conversion history.
        _launch_bid_strategy = (ag_data.get("launch_bidding_strategy") or {}) if isinstance(ag_data, dict) else {}
        _launch_strategy_type = (_launch_bid_strategy.get("strategy_type") or "MANUAL_CPC").upper().strip()
        if _launch_strategy_type not in ("MANUAL_CPC", "MAXIMIZE_CLICKS"):
            logger.warning(f"[create] Unsupported launch_bidding_strategy '{_launch_strategy_type}' — falling back to MANUAL_CPC")
            _launch_strategy_type = "MANUAL_CPC"

        if _launch_strategy_type == "MAXIMIZE_CLICKS":
            # target_spend activates Maximize Clicks in the proto-plus oneof.
            # IMPORTANT: proto-plus may not select the oneof when cpc_bid_ceiling_micros = 0
            # (zero is the proto3 default and may not register as an explicit assignment).
            # When no cap is desired, use 1 micro ($0.000001) as a sentinel so the oneof
            # is reliably selected while effectively imposing no real ceiling.
            _max_cpc_cap = _launch_bid_strategy.get("max_cpc_cap_usd") or 0.0
            try:
                _max_cpc_cap = float(str(_max_cpc_cap).replace("$", "").replace(",", "").strip()) if _max_cpc_cap else 0.0
            except (ValueError, TypeError):
                _max_cpc_cap = 0.0
            _ceiling_micros = int(_max_cpc_cap * 1_000_000) if _max_cpc_cap > 0 else 1
            camp.target_spend.cpc_bid_ceiling_micros = _ceiling_micros
            log.append(f"  ℹ Bidding: MAXIMIZE_CLICKS" + (f" (max CPC cap ${_max_cpc_cap:.2f})" if _max_cpc_cap else " (no CPC cap)"))
        else:
            # Manual CPC bidding — setting any field on camp.manual_cpc activates
            # the bidding_strategy oneof in proto-plus. enhanced_cpc_enabled=False
            # is the explicit field assignment that marks the oneof as selected.
            camp.manual_cpc.enhanced_cpc_enabled = False
            log.append("  ℹ Bidding: MANUAL_CPC")

        # Required in Google Ads API v24+ — this is an ENUM, not a bool.
        # UNSPECIFIED (0) is the proto-plus default so Google rejects it as
        # "REQUIRED field not present". Must set the explicit non-EU value (3).
        # client.enums.EuPoliticalAdvertisingStatusEnum already returns the
        # inner EuPoliticalAdvertisingStatus ProtoEnumMeta — access values
        # directly (no .EuPoliticalAdvertisingStatus sub-attribute needed).
        camp.contains_eu_political_advertising = (
            client.enums.EuPoliticalAdvertisingStatusEnum
            .DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
        )

        # Network settings — search only, no display, no partners
        camp.network_settings.target_google_search     = True
        camp.network_settings.target_search_network    = True
        camp.network_settings.target_content_network   = False
        camp.network_settings.target_partner_search_network = False

        # Start date
        if start_date:
            try:
                from datetime import datetime
                dt = datetime.strptime(start_date[:10], "%Y-%m-%d")
                camp.start_date = dt.strftime("%Y%m%d")
            except Exception:
                pass

        camp_response = camp_service.mutate_campaigns(
            customer_id=customer_id, operations=[camp_op]
        )
        camp_resource = camp_response.results[0].resource_name
        camp_numeric  = camp_resource.split("/campaigns/")[-1]
        log.append(f"  ✓ Campaign created: {camp_resource} (id={camp_numeric})")

        # ── Step 3: Geographic targeting ──────────────────────────────────────
        log.append("Step 3: Setting geographic targeting")
        geo_criterion_service = client.get_service("CampaignCriterionService")
        geo_ops = []

        # Parse geo_json — list of {type, value, radius, include}
        geo_locs = []
        if geo_json:
            try:
                parsed = json.loads(geo_json) if isinstance(geo_json, str) else geo_json
                geo_locs = parsed.get("locations", []) if isinstance(parsed, dict) else []
            except Exception:
                pass

        if geo_locs:
            geo_target_service = client.get_service("GeoTargetConstantService")
            geo_unit = parsed.get("unit", "miles") if isinstance(parsed, dict) else "miles"
            for loc in geo_locs:
                loc_type  = (loc.get("type") or "postal").lower()
                loc_value = str(loc.get("value", "")).strip()
                include   = loc.get("include", True)
                radius    = loc.get("radius")
                if not loc_value:
                    continue
                try:
                    if loc_type == "city" and radius is not None:
                        # Proximity (radius) targeting — REQUIRES geo_point (lat/lng).
                        # The ProximityCriterion.address is display-only and does NOT
                        # drive ad serving. Sending only city_name without geo_point is
                        # unreliable: the API may silently fail or geocode to the wrong
                        # Grafton (there are Graftons in WI, OH, NH, VT, ND, etc.).
                        city_part  = loc_value.split(",")[0].strip()
                        # Guard against "Grafton," (trailing comma) → empty state → default MA
                        raw_state  = loc_value.split(",")[1].strip() if "," in loc_value else ""
                        state_part = raw_state or "MA"
                        loc_radius = max(1, min(500, float(radius)))
                        if not include:
                            log.append(f"  ⚠ Negative radius targeting not supported by Google Ads — skipping '{loc_value}'")
                            continue
                        lat, lng = _resolve_city_latlng(city_part, state_part, log)
                        if lat is None or lng is None:
                            continue  # error already appended to log by helper
                        crit_op = client.get_type("CampaignCriterionOperation")
                        crit    = crit_op.create
                        crit.campaign = camp_resource
                        # geo_point is what Google Ads uses for the radius circle
                        crit.proximity.geo_point.latitude_in_micro_degrees  = int(round(lat  * 1_000_000))
                        crit.proximity.geo_point.longitude_in_micro_degrees = int(round(lng  * 1_000_000))
                        # address is display-only (shows in UI) — still useful
                        crit.proximity.address.city_name     = city_part
                        crit.proximity.address.province_code = state_part
                        crit.proximity.address.country_code  = "US"
                        crit.proximity.radius       = loc_radius
                        crit.proximity.radius_units = (
                            client.enums.ProximityRadiusUnitsEnum.MILES
                            if geo_unit == "miles"
                            else client.enums.ProximityRadiusUnitsEnum.KILOMETERS
                        )
                        geo_ops.append(crit_op)
                        log.append(f"  ✓ Proximity: {city_part}, {state_part} ({lat:.4f},{lng:.4f}) · {loc_radius} {geo_unit}")
                    else:
                        # GeoTargetConstant lookup for postal codes and named places
                        suggest_req = client.get_type("SuggestGeoTargetConstantsRequest")
                        suggest_req.locale = "en"
                        suggest_req.country_code = "US"
                        suggest_req.location_names.names.append(loc_value)
                        suggest_resp = geo_target_service.suggest_geo_target_constants(request=suggest_req)
                        for suggestion in (suggest_resp.geo_target_constant_suggestions or []):
                            geo_const = suggestion.geo_target_constant
                            crit_op   = client.get_type("CampaignCriterionOperation")
                            crit      = crit_op.create
                            crit.campaign = camp_resource
                            crit.location.geo_target_constant = geo_const.resource_name
                            if not include:
                                crit.negative = True
                            geo_ops.append(crit_op)
                            break  # take first match only
                except Exception as ge:
                    log.append(f"  ⚠ Geo lookup failed for '{loc_value}': {ge}")
        else:
            # Default: 15-mile radius around Grafton MA (42.2012° N, 71.6870° W)
            log.append("  No geo targets set — defaulting to 15-mile radius around Grafton MA")
            crit_op = client.get_type("CampaignCriterionOperation")
            crit    = crit_op.create
            crit.campaign = camp_resource
            # geo_point is required for reliable proximity targeting (address-only is display-only)
            crit.proximity.geo_point.latitude_in_micro_degrees  = 42_201_200
            crit.proximity.geo_point.longitude_in_micro_degrees = -71_687_000
            crit.proximity.address.city_name     = "Grafton"
            crit.proximity.address.province_code = "MA"
            crit.proximity.address.country_code  = "US"
            crit.proximity.radius               = 15
            crit.proximity.radius_units         = client.enums.ProximityRadiusUnitsEnum.MILES
            geo_ops.append(crit_op)

        # SAFETY: if geo_locs was specified but every lookup failed, geo_ops will be
        # empty — launching without it would target the entire world.  Abort here.
        if geo_locs and not geo_ops:
            raise RuntimeError(
                "Geographic targeting was requested but all geo lookups failed — "
                "campaign creation aborted to avoid worldwide targeting. "
                "Check geo location names and API connectivity."
            )

        if geo_ops:
            geo_response = geo_criterion_service.mutate_campaign_criteria(
                customer_id=customer_id, operations=geo_ops
            )
            log.append(f"  ✓ {len(geo_response.results)} geo target(s) applied")

        # ── Step 4 & 5: Ad Groups + Keywords ─────────────────────────────────
        log.append("Step 4: Creating ad groups and keywords")
        ag_service  = client.get_service("AdGroupService")
        kw_service  = client.get_service("AdGroupCriterionService")

        # Build ad group name → keywords map from build data
        # build.keywords has: exact_match[], phrase_match[], broad_match_modifier[], negative_keywords[]
        # build.ad_groups has: ad_groups[{name, keywords[], cpc_bid}]
        # Strategy: one ad group per build.ad_groups entry; assign keywords to it

        ag_entries = ag_data.get("ad_groups", []) if isinstance(ag_data, dict) else []
        if not ag_entries:
            # Fallback: single ad group with all keywords.
            # Use max_cpc_cap from launch_bidding_strategy if available, else 3.0.
            _fallback_bid = ((_launch_bid_strategy.get("max_cpc_cap_usd") or 0.0) if _launch_bid_strategy else 0.0)
            try:
                _fallback_bid = float(str(_fallback_bid).replace("$", "").replace(",", "").strip()) if _fallback_bid else 0.0
            except (ValueError, TypeError):
                _fallback_bid = 0.0
            ag_entries = [{"name": f"{campaign.get('service_focus','General')} - Search", "cpc_bid_usd": _fallback_bid or 5.0}]

        ad_group_resources = []
        keywords_added     = 0

        # All keywords from build (flat lists)
        exact_kws  = [k if isinstance(k, str) else k.get("keyword","") for k in kw_data.get("exact_match", [])]
        phrase_kws = [k if isinstance(k, str) else k.get("keyword","") for k in kw_data.get("phrase_match", [])]
        # BMM (+keyword) was deprecated by Google in 2021 — strip leading + from each word
        # and submit as plain BROAD match. The API hard-rejects any keyword containing +.
        _raw_broad = [k if isinstance(k, str) else k.get("keyword","") for k in kw_data.get("broad_match_modifier", [])]
        broad_kws  = [" ".join(w.lstrip("+") for w in kw.split()) for kw in _raw_broad]
        neg_kws_raw = [k if isinstance(k, str) else k.get("keyword","") for k in kw_data.get("negative_keywords", [])]
        # Google Ads rejects negatives with more than 10 words (KEYWORD_HAS_TOO_MANY_WORDS)
        neg_kws = [kw for kw in neg_kws_raw if kw.strip() and len(kw.split()) <= 10]
        if len(neg_kws) < len(neg_kws_raw):
            _skipped = [kw for kw in neg_kws_raw if len(kw.split()) > 10]
            logger.warning(f"[create] Skipped {len(_skipped)} negative keywords exceeding 10-word limit: {_skipped[:5]}")

        # Split keywords evenly across ad groups if multiple groups
        # (Simple approach: all groups get all keywords — Google doesn't mind)
        def _parse_bid(raw) -> float:
            """Safely parse a CPC bid value that may be a float, int, or string like '$3.50'."""
            if not raw:
                return 0.0
            try:
                return float(str(raw).replace("$", "").replace(",", "").strip())
            except (ValueError, TypeError):
                return 0.0

        # Fallback chain: per-ad-group bid → launch_bidding_strategy cap → 3.0
        _global_cap = _parse_bid(_launch_bid_strategy.get("max_cpc_cap_usd")) if _launch_bid_strategy else 0.0

        for ag_entry in ag_entries:
            ag_name    = ag_entry.get("name") or ag_entry.get("ad_group_name") or "Ad Group 1"
            # Read cpc_bid_usd (wizard schema) or legacy cpc_bid / suggested_cpc_usd fields.
            # Fall back to the campaign-level max_cpc_cap from launch_bidding_strategy, then $3.00.
            cpc_bid    = (
                _parse_bid(ag_entry.get("cpc_bid_usd"))
                or _parse_bid(ag_entry.get("suggested_cpc_usd"))
                or _parse_bid(ag_entry.get("cpc_bid"))
                or _global_cap
                or 5.0  # $5 default — safer floor for dental keywords than $3
            )

            # Create ad group
            ag_op      = client.get_type("AdGroupOperation")
            ag         = ag_op.create
            ag.name    = ag_name
            ag.campaign = camp_resource
            ag.status  = client.enums.AdGroupStatusEnum.ENABLED
            ag.type_   = client.enums.AdGroupTypeEnum.SEARCH_STANDARD
            ag.cpc_bid_micros = int(cpc_bid * 1_000_000)

            ag_response = ag_service.mutate_ad_groups(
                customer_id=customer_id, operations=[ag_op]
            )
            ag_resource = ag_response.results[0].resource_name
            ad_group_resources.append(ag_resource)
            log.append(f"  ✓ Ad group '{ag_name}': {ag_resource}")

            # Add keywords to this ad group
            kw_ops = []
            match_enum = client.enums.KeywordMatchTypeEnum

            for kw in exact_kws:
                if not kw.strip(): continue
                op = client.get_type("AdGroupCriterionOperation")
                c  = op.create
                c.ad_group = ag_resource
                c.status   = client.enums.AdGroupCriterionStatusEnum.ENABLED
                c.keyword.text       = kw.strip()
                c.keyword.match_type = match_enum.EXACT
                kw_ops.append(op)

            for kw in phrase_kws:
                if not kw.strip(): continue
                op = client.get_type("AdGroupCriterionOperation")
                c  = op.create
                c.ad_group = ag_resource
                c.status   = client.enums.AdGroupCriterionStatusEnum.ENABLED
                c.keyword.text       = kw.strip()
                c.keyword.match_type = match_enum.PHRASE
                kw_ops.append(op)

            for kw in broad_kws:
                if not kw.strip(): continue
                op = client.get_type("AdGroupCriterionOperation")
                c  = op.create
                c.ad_group = ag_resource
                c.status   = client.enums.AdGroupCriterionStatusEnum.ENABLED
                # Defensive: strip any residual + (BMM deprecated 2021, API rejects it)
                c.keyword.text       = " ".join(w.lstrip("+") for w in kw.strip().split())
                c.keyword.match_type = match_enum.BROAD
                kw_ops.append(op)

            if kw_ops:
                # Dental keywords trigger HEALTH_IN_PERSONALIZED_ADS policy
                # (is_exemptible=True). Add exemption key to every operation
                # so Google allows them without blocking the entire batch.
                exempt_key = client.get_type("PolicyViolationKey")
                exempt_key.policy_name = "HEALTH_IN_PERSONALIZED_ADS"
                for op in kw_ops:
                    op.exempt_policy_violation_keys.append(exempt_key)
                kw_response = kw_service.mutate_ad_group_criteria(
                    customer_id=customer_id, operations=kw_ops
                )
                count = len(kw_response.results)
                keywords_added += count
                log.append(f"    ✓ {count} keywords added to '{ag_name}'")

        # ── Step 6: Campaign-level negative keywords ──────────────────────────
        log.append("Step 6: Adding campaign-level negative keywords")
        camp_crit_service = client.get_service("CampaignCriterionService")
        neg_ops = []
        for kw in neg_kws:
            kw = kw.strip()
            if not kw: continue
            op = client.get_type("CampaignCriterionOperation")
            c  = op.create
            c.campaign = camp_resource
            c.negative = True
            c.keyword.text       = kw
            c.keyword.match_type = client.enums.KeywordMatchTypeEnum.BROAD
            neg_ops.append(op)

        if neg_ops:
            neg_response = camp_crit_service.mutate_campaign_criteria(
                customer_id=customer_id, operations=neg_ops
            )
            log.append(f"  ✓ {len(neg_response.results)} negative keywords added")
        else:
            log.append("  (no negative keywords)")

        # ── Step 7: RSA ads (with AI self-heal on policy rejection) ─────────────
        log.append("Step 7: Creating RSA ads")
        ad_service  = client.get_service("AdGroupAdService")
        ads_created = 0
        ads_failed  = 0
        ads_fixed   = 0  # ad groups that needed at least one AI fix

        # Lazy-init Anthropic client for self-heal. Gracefully disabled if
        # anthropic isn't installed or ANTHROPIC_API_KEY isn't set.
        _heal_ai_client = None
        try:
            import anthropic as _anthropic
            _heal_ai_client = _anthropic.Anthropic()
        except Exception as _ai_init_err:
            logger.info(f"AI self-heal disabled (init failed): {_ai_init_err}")

        # Import exception class here so it's available in the inner loop
        from google.ads.googleads.errors import GoogleAdsException as _GadsExc

        ac_groups = ac_data.get("ad_groups", [])

        # Helper: build a fresh AdGroupAdOperation proto each attempt.
        # Proto messages are not safely mutable after a failed RPC — always rebuild.
        def _build_rsa_op(ag_res, hdls, descs):
            from urllib.parse import urlparse as _urlparse
            op  = client.get_type("AdGroupAdOperation")
            ad  = op.create
            ad.ad_group = ag_res
            ad.status   = client.enums.AdGroupAdStatusEnum.ENABLED
            rsa = ad.ad.responsive_search_ad
            pp  = [p for p in _urlparse(landing_page).path.strip("/").split("/") if p]
            if pp:
                rsa.path1 = pp[0][:15]
            if len(pp) > 1:
                rsa.path2 = pp[1][:15]
            for h in hdls:
                a = client.get_type("AdTextAsset"); a.text = h[:30]
                rsa.headlines.append(a)
            for d in descs:
                a = client.get_type("AdTextAsset"); a.text = d[:90]
                rsa.descriptions.append(a)
            ad.ad.final_urls.append(landing_page)
            return op

        for i, ag_resource in enumerate(ad_group_resources):
            ag_label = f"ad group {i+1}"
            ac_group = ac_groups[i] if i < len(ac_groups) else (ac_groups[0] if ac_groups else {})
            ag_name  = ac_group.get("name") or ag_label

            headlines_raw    = ac_group.get("headlines", [])
            descriptions_raw = ac_group.get("descriptions", [])

            # Normalise: each item may be str or {text:...}
            headlines    = [h if isinstance(h, str) else h.get("text","") for h in headlines_raw]
            descriptions = [d if isinstance(d, str) else d.get("text","") for d in descriptions_raw]

            # Sanitize: strip phone numbers (PHONE_NUMBER_IN_AD_TEXT policy)
            headlines    = [_strip_phone_numbers(h) for h in headlines]
            descriptions = [_strip_phone_numbers(d) for d in descriptions]

            # Filter empty, cap at 15 headlines / 4 descriptions (API limits)
            headlines    = [h.strip() for h in headlines    if h.strip()][:15]
            descriptions = [d.strip() for d in descriptions if d.strip()][:4]

            if len(headlines) < 3:
                log.append(f"  ⚠ {ag_label}: only {len(headlines)} headline(s) — need 3+, skipping RSA")
                ads_failed += 1
                continue
            if len(descriptions) < 2:
                log.append(f"  ⚠ {ag_label}: only {len(descriptions)} description(s) — need 2+, skipping RSA")
                ads_failed += 1
                continue

            ai_attempts = 0
            ad_created  = False

            while True:
                try:
                    ad_op = _build_rsa_op(ag_resource, headlines, descriptions)
                    ad_service.mutate_ad_group_ads(
                        customer_id=customer_id, operations=[ad_op]
                    )
                    ads_created += 1
                    if ai_attempts > 0:
                        ads_fixed += 1
                        log.append(
                            f"  ✓ RSA created for {ag_label} after {ai_attempts} AI fix(es): "
                            f"{len(headlines)} headlines, {len(descriptions)} descriptions"
                        )
                    else:
                        log.append(
                            f"  ✓ RSA created for {ag_label}: "
                            f"{len(headlines)} headlines, {len(descriptions)} descriptions"
                        )
                    ad_created = True
                    break

                except _GadsExc as gae:
                    topics = _extract_policy_topics(gae)

                    if not topics:
                        # Non-policy failure (auth, quota, schema) — don't burn AI
                        # retries; propagate so the outer rollback runs.
                        logger.error(f"{ag_label} RSA failed (non-policy): {gae}")
                        log.append(f"  ✗ {ag_label} RSA failed (non-policy error) — aborting")
                        raise

                    # Policy violation — try AI fix if budget remains
                    if ai_attempts >= _MAX_AI_FIXES or _heal_ai_client is None:
                        log.append(
                            f"  ✗ {ag_label} RSA failed after {ai_attempts} AI fix(es) — "
                            f"skipping. Policy topics: {topics}"
                        )
                        logger.warning(
                            f"create_campaign_in_gads: {ag_label} RSA permanently failed. "
                            f"topics={topics} ai={'present' if _heal_ai_client else 'missing'}"
                        )
                        ads_failed += 1
                        break

                    ai_attempts += 1
                    log.append(
                        f"  ⚠ {ag_label} RSA rejected — policy topics: {topics}. "
                        f"Attempting AI fix ({ai_attempts}/{_MAX_AI_FIXES})…"
                    )
                    try:
                        headlines, descriptions = _ai_fix_ad_copy(
                            headlines, descriptions, topics, ag_name, _heal_ai_client
                        )
                        log.append(f"  ↻ AI returned fixed copy for {ag_label} — retrying submission")
                    except Exception as fix_err:
                        log.append(f"  ✗ AI fix attempt {ai_attempts} failed: {fix_err}")
                        logger.warning(f"create_campaign_in_gads AI fix failed ({ag_label}): {fix_err}")
                        if ai_attempts >= _MAX_AI_FIXES:
                            log.append(f"  ✗ {ag_label} RSA giving up — AI fixes exhausted. Topics: {topics}")
                            ads_failed += 1
                            break
                        # else: loop again — mutate will fail again, bump counter, then exit

        log.append(
            f"Step 7 complete: {ads_created}/{len(ad_group_resources)} ad groups got an RSA"
            + (f" ({ads_fixed} via AI self-heal)" if ads_fixed else "")
            + (f"; {ads_failed} failed" if ads_failed else "")
        )
        if ads_created == 0 and ad_group_resources:
            log.append("  ⚠ No RSA ads were created — campaign will not serve until ads are added manually")
            logger.warning(
                f"create_campaign_in_gads: '{campaign_name}' has 0 RSAs after Step 7"
            )

        # ── Step 8: Call extension ────────────────────────────────────────────
        if call_phone:
            log.append(f"Step 8: Adding call extension ({call_phone})")
            try:
                asset_service = client.get_service("AssetService")
                asset_op      = client.get_type("AssetOperation")
                asset         = asset_op.create
                # Strip non-digits for the phone_number field
                phone_digits  = "".join(ch for ch in call_phone if ch.isdigit() or ch == "+")
                asset.call_asset.phone_number    = phone_digits  # E.164 digits only
                asset.call_asset.country_code    = "US"
                asset.name = f"{campaign_name} Call"

                asset_response   = asset_service.mutate_assets(
                    customer_id=customer_id, operations=[asset_op]
                )
                asset_resource = asset_response.results[0].resource_name

                # Link asset to campaign
                camp_asset_service = client.get_service("CampaignAssetService")
                link_op  = client.get_type("CampaignAssetOperation")
                link     = link_op.create
                link.campaign      = camp_resource
                link.asset         = asset_resource
                link.field_type    = client.enums.AssetFieldTypeEnum.CALL
                camp_asset_service.mutate_campaign_assets(
                    customer_id=customer_id, operations=[link_op]
                )
                log.append(f"  ✓ Call extension linked to campaign")
            except Exception as ce:
                log.append(f"  ⚠ Call extension failed (non-fatal): {ce}")
        else:
            log.append("Step 8: No phone configured — skipping call extension")

        # ── Step 8b: Sitelinks ────────────────────────────────────────────────
        sitelinks_raw = campaign.get("sitelinks") or ""
        sitelinks_list = []
        if sitelinks_raw:
            try:
                sitelinks_list = json.loads(sitelinks_raw) if isinstance(sitelinks_raw, str) else sitelinks_raw
            except Exception:
                pass
        if sitelinks_list:
            log.append(f"Step 8b: Adding {len(sitelinks_list)} sitelink(s)")
            try:
                sl_result = add_sitelinks_to_campaign(camp_resource, sitelinks_list, customer_id)
                if sl_result["ok"]:
                    log.append(f"  ✓ {sl_result['count']} sitelink(s) linked to campaign")
                else:
                    log.append(f"  ⚠ Sitelinks Step 8b failed (non-fatal)")
                for err in sl_result.get("errors") or []:
                    log.append(f"    • {err}")
            except Exception as sle:
                log.append(f"  ⚠ Sitelinks Step 8b exception (non-fatal): {sle}")
                logger.warning(f"create_campaign_in_gads Step 8b sitelinks failed: {sle}")
        else:
            log.append("Step 8b: No sitelinks configured — skipping")

        # ── Step 8c: Ad schedule ──────────────────────────────────────────────
        # Read from launch checklist ad_schedule value (per-campaign, not office hours)
        _schedule_value = None
        _checklist = build.get("launch_checklist") or []
        for _item in _checklist:
            if isinstance(_item, dict) and _item.get("key") == "ad_schedule":
                if not _item.get("skipped"):
                    _schedule_value = _item.get("value")
                break
        if _schedule_value:
            log.append("Step 8c: Setting ad schedule")
            try:
                _slots = parse_ad_schedule(_schedule_value)
                if _slots:
                    _sched_result = push_ad_schedule(
                        client, customer_id, camp_resource, _slots, replace=False
                    )
                    if _sched_result["ok"]:
                        _days_summary = ", ".join(sorted(set(s["day"] for s in _slots)))
                        log.append(f"  ✓ Ad schedule set: {len(_slots)} slots ({_days_summary})")
                    else:
                        log.append(f"  ⚠ Ad schedule failed (non-fatal): {_sched_result['error']}")
                else:
                    log.append(f"  ⚠ Could not parse schedule '{_schedule_value}' — skipping")
            except Exception as _se:
                log.append(f"  ⚠ Ad schedule exception (non-fatal): {_se}")
                logger.warning(f"create_campaign_in_gads Step 8c schedule failed: {_se}")
        else:
            log.append("Step 8c: No ad schedule configured — ads run 24/7")

        # ── Step 8d: Attach shared negative keyword lists ─────────────────────
        log.append("Step 8d: Attaching account shared negative keyword lists")
        _neg_list_logs = _attach_shared_negative_lists(client, customer_id, camp_resource)
        log.extend(_neg_list_logs)

        # ── Step 8e: Callouts + Structured Snippets from campaign_build_json ──
        # If the campaign wizard (or AI optimizer) saved callouts/structured_snippets
        # into campaign_build_json, we attach them automatically at launch time.
        # Schema:
        #   build["callout_texts"]        = ["text1", "text2", ...]  (3–10 strings)
        #   build["structured_snippets"]  = [{"header": "...", "values": [...]}, ...]
        _callout_texts = build.get("callout_texts") or []
        if isinstance(_callout_texts, str):
            try:
                _callout_texts = json.loads(_callout_texts)
            except Exception:
                _callout_texts = []
        # Filter to strings only before passing to add_callouts_to_campaign (HIGH-3)
        _callout_texts = [t for t in _callout_texts if isinstance(t, str) and t.strip()][:10]
        if len(_callout_texts) >= 3:
            log.append(f"Step 8e: Adding {len(_callout_texts)} callout(s) from campaign build")
            try:
                _cl_result = add_callouts_to_campaign(camp_resource, _callout_texts, customer_id)
                if _cl_result["ok"]:
                    log.append(f"  ✓ {_cl_result['count']} callout(s) linked to campaign")
                else:
                    log.append(f"  ⚠ Callouts Step 8e failed (non-fatal): {'; '.join(_cl_result.get('errors') or [])}")
                for _cle in (_cl_result.get("errors") or []):
                    log.append(f"    • {_cle}")
            except Exception as _clex:
                log.append(f"  ⚠ Callouts Step 8e exception (non-fatal): {_clex}")
                logger.warning(f"create_campaign_in_gads Step 8e callouts failed: {_clex}")
        else:
            log.append("Step 8e: No callouts in campaign build — skipping")

        _snippets = build.get("structured_snippets") or []
        if isinstance(_snippets, str):
            try:
                _snippets = json.loads(_snippets)
            except Exception:
                _snippets = []
        if _snippets:
            from ai_optimizer import VALID_SNIPPET_HEADERS as _VALID_SNIPPET_HEADERS
            log.append(f"Step 8e: Adding {len(_snippets)} structured snippet(s) from campaign build")
            for _snip in _snippets:
                # HIGH-2: guard against non-dict entries (e.g. list of strings)
                if not isinstance(_snip, dict):
                    log.append(f"  ⚠ Skipping snippet — not a dict: {_snip!r}")
                    continue
                _hdr = (_snip.get("header") or "").strip()
                _vals = [v for v in (_snip.get("values") or []) if isinstance(v, str) and v.strip()]
                # HIGH-1: validate header against Google's required list
                if not _hdr or _hdr not in _VALID_SNIPPET_HEADERS or len(_vals) < 3:
                    log.append(
                        f"  ⚠ Skipping snippet — invalid/missing header or <3 values "
                        f"(header={_hdr!r}, values={len(_vals)})"
                    )
                    continue
                try:
                    _sn_result = add_structured_snippet_to_campaign(camp_resource, _hdr, _vals, customer_id)
                    if _sn_result["ok"]:
                        log.append(f"  ✓ Snippet '{_hdr}' linked to campaign ({len(_vals)} values)")
                    else:
                        log.append(f"  ⚠ Snippet '{_hdr}' failed (non-fatal): {'; '.join(_sn_result.get('errors') or [])}")
                except Exception as _snex:
                    log.append(f"  ⚠ Snippet '{_hdr}' exception (non-fatal): {_snex}")
                    logger.warning(f"create_campaign_in_gads Step 8e snippet failed: {_snex}")

        # ── Step 9: Enable campaign ───────────────────────────────────────────
        log.append("Step 9: Enabling campaign (PAUSED → ENABLED)")
        enable_result = set_campaign_status(camp_resource, "ENABLED")
        if enable_result["ok"]:
            log.append(f"  ✓ Campaign ENABLED and live in Google Ads")
        else:
            log.append(f"  ⚠ Enable failed: {enable_result['error']} — campaign remains PAUSED in Google Ads. Enable manually.")

        # ── Step 10: Post-launch URL verification ─────────────────────────────
        # Read back live ad final_urls from Google Ads and compare against intended landing_page.
        # A mismatch means the wrong page went live — flag it loudly so it can be caught immediately.
        url_warnings = []
        log.append("Step 10: Verifying ad final URLs in Google Ads...")
        try:
            live_ads = fetch_ads_for_campaign(camp_numeric)
            for live_ad in live_ads:
                live_urls = live_ad.get("final_urls", [])
                live_url_str = live_urls[0] if live_urls else ""
                # Normalise for comparison: strip trailing slash, lowercase
                intended_norm = landing_page.rstrip("/").lower()
                live_norm     = live_url_str.rstrip("/").lower()
                if live_norm and live_norm != intended_norm:
                    msg = (
                        f"⚠ URL MISMATCH on ad group '{live_ad.get('ad_group_name', '?')}': "
                        f"intended '{landing_page}' but Google Ads has '{live_url_str}'"
                    )
                    log.append(f"  {msg}")
                    url_warnings.append(msg)
                    logger.error(f"POST-LAUNCH URL MISMATCH [{campaign_name}]: {msg}")
                elif live_norm == intended_norm:
                    log.append(f"  ✓ '{live_ad.get('ad_group_name','?')}' → {live_url_str}")
                else:
                    log.append(f"  ? '{live_ad.get('ad_group_name','?')}' — no final URL found in live ad")
            if not live_ads:
                log.append("  ? No live ads returned yet — may take a moment to propagate")
        except Exception as _ve:
            log.append(f"  ? URL verification skipped (non-fatal): {_ve}")
            logger.warning(f"Post-launch URL verification failed (non-fatal): {_ve}")

        logger.info(
            f"create_campaign_in_gads: '{campaign_name}' created. "
            f"resource={camp_resource} kw={keywords_added} ads={ads_created}"
        )

        return {
            "ok":                     True,
            "campaign_resource_name": camp_resource,
            "campaign_numeric_id":    camp_numeric,
            "gads_campaign_name":     gads_campaign_name,   # timestamped name as stored in Google Ads
            "ad_group_resources":     ad_group_resources,
            "keywords_added":         keywords_added,
            "ads_created":            ads_created,
            "enabled":                enable_result["ok"],
            "url_warnings":           url_warnings,         # non-empty = landing page mismatch detected
            "error":                  None if enable_result["ok"] else f"Created but not enabled: {enable_result['error']}",
            "log":                    log,
        }

    except Exception as e:
        logger.error(f"create_campaign_in_gads failed: {e}", exc_info=True)
        log.append(f"FATAL ERROR: {e}")
        # ── Rollback: remove partial campaign from Google Ads ─────────────────
        # camp_resource is set after Step 2 succeeds. If we get here after that,
        # there is an orphaned PAUSED campaign (with budget/ad groups/keywords but
        # no ads) sitting in Google Ads. Remove it automatically.
        try:
            if 'camp_resource' in dir() or 'camp_resource' in locals():
                pass  # handled below
        except Exception:
            pass
        _camp_resource = locals().get('camp_resource')
        if _camp_resource:
            try:
                log.append(f"Rollback: removing partial campaign {_camp_resource} from Google Ads…")
                rb_client = _build_client()
                rb_service = rb_client.get_service("CampaignService")
                rb_op = rb_client.get_type("CampaignOperation")
                rb_op.remove = _camp_resource
                rb_service.mutate_campaigns(customer_id=customer_id, operations=[rb_op])
                log.append(f"  ✓ Partial campaign removed from Google Ads")
                logger.info(f"create_campaign_in_gads rollback: removed {_camp_resource}")
            except Exception as rb_err:
                log.append(f"  ⚠ Rollback failed: {rb_err} — remove {_camp_resource} manually in Google Ads")
                logger.warning(f"create_campaign_in_gads rollback failed for {_camp_resource}: {rb_err}")
        return {"ok": False, "error": str(e), "log": log}


def _build_client():
    """Build authenticated Google Ads API client using shared credentials.

    Mirrors the exact same pattern used in google_ads_sync.py and ai_optimizer.py
    (confirmed working) — sub-account login_customer_id, not the manager ID.
    """
    from google.ads.googleads.client import GoogleAdsClient
    settings = get_settings()
    return GoogleAdsClient.load_from_dict({
        "developer_token":    settings.google_ads_developer_token,
        "client_id":          settings.google_ads_client_id,
        "client_secret":      settings.google_ads_client_secret,
        "refresh_token":      settings.google_ads_refresh_token,
        "login_customer_id":  settings.google_ads_login_customer_id,
        "use_proto_plus":     True,
    })


def _attach_shared_negative_lists(client, customer_id: str, camp_resource: str) -> list[str]:
    """
    Attach all account-level shared negative keyword lists to a newly created campaign.
    Returns a list of log strings describing what was done.
    Non-fatal — any exception is caught and returned as a warning log entry.
    """
    logs = []
    try:
        ga_service = client.get_service("GoogleAdsService")

        # Fetch all enabled shared negative keyword lists in the account
        list_query = """
            SELECT shared_set.resource_name, shared_set.name
            FROM shared_set
            WHERE shared_set.type = 'NEGATIVE_KEYWORDS'
              AND shared_set.status = 'ENABLED'
        """
        list_rows = list(ga_service.search(customer_id=customer_id, query=list_query))
        if not list_rows:
            logs.append("  ⓘ No shared negative keyword lists found in account — skipping")
            return logs

        # Check which lists are already linked to this campaign (idempotency)
        already_linked = set()
        link_query = f"""
            SELECT campaign_shared_set.shared_set
            FROM campaign_shared_set
            WHERE campaign_shared_set.campaign = '{camp_resource}'
        """
        try:
            link_rows = list(ga_service.search(customer_id=customer_id, query=link_query))
            already_linked = {row.campaign_shared_set.shared_set for row in link_rows}
        except Exception:
            pass  # If check fails, attempt to link anyway (mutate handles duplicates)

        css_service = client.get_service("CampaignSharedSetService")
        link_ops = []
        names_to_link = []
        for row in list_rows:
            ss_rn = row.shared_set.resource_name
            ss_name = row.shared_set.name
            if ss_rn in already_linked:
                logs.append(f"  ⓘ '{ss_name}' already linked — skipped")
                continue
            op = client.get_type("CampaignSharedSetOperation")
            op.create.campaign = camp_resource
            op.create.shared_set = ss_rn
            link_ops.append(op)
            names_to_link.append(ss_name)

        if link_ops:
            css_service.mutate_campaign_shared_sets(
                customer_id=customer_id, operations=link_ops
            )
            for name in names_to_link:
                logs.append(f"  ✓ Attached shared negative list: '{name}'")
        else:
            logs.append("  ⓘ All shared negative lists already linked")

    except Exception as e:
        logs.append(f"  ⚠ Shared negative list attach failed (non-fatal): {e}")
        logger.warning(f"_attach_shared_negative_lists failed for {camp_resource}: {e}")

    return logs


def _remove_existing_campaign_sitelinks(campaign_resource_name: str, client, customer_id: str) -> int:
    """
    Remove all SITELINK-type CampaignAsset links from an existing campaign.
    Does NOT delete the underlying Asset resources (orphaned assets are harmless
    and Google Ads dedupes them account-wide on future creates).

    Returns the number of CampaignAsset links removed (0 if none).
    """
    service = client.get_service("GoogleAdsService")
    query = f"""
        SELECT campaign_asset.resource_name
        FROM campaign_asset
        WHERE campaign_asset.campaign = '{campaign_resource_name}'
          AND campaign_asset.field_type = SITELINK
          AND campaign_asset.status != REMOVED
    """
    try:
        rows = list(service.search(customer_id=customer_id, query=query))
    except Exception as qe:
        logger.warning(f"_remove_existing_campaign_sitelinks: query failed: {qe}")
        return 0

    if not rows:
        return 0

    camp_asset_service = client.get_service("CampaignAssetService")
    remove_ops = []
    for row in rows:
        op = client.get_type("CampaignAssetOperation")
        op.remove = row.campaign_asset.resource_name
        remove_ops.append(op)

    try:
        camp_asset_service.mutate_campaign_assets(
            customer_id=customer_id,
            operations=remove_ops,
        )
        logger.info(f"_remove_existing_campaign_sitelinks: removed {len(remove_ops)} sitelink links from {campaign_resource_name}")
        return len(remove_ops)
    except Exception as re_err:
        logger.warning(f"_remove_existing_campaign_sitelinks: remove failed: {re_err}")
        return 0


def add_sitelinks_to_campaign(campaign_resource_name: str, sitelinks: list, customer_id: str = None, replace: bool = False) -> dict:
    """
    Create SitelinkAsset assets and link them to an existing campaign.

    Batches all asset creates into one mutate call, then all campaign links
    into a second mutate call (matches keyword/geo batching pattern).
    Non-fatal — designed to be called from Step 8b inside create_campaign_in_gads
    and from the /sitelinks endpoint for existing live campaigns.

    Args:
        campaign_resource_name: Full resource name e.g. "customers/.../campaigns/..."
        sitelinks: list of dicts with keys: title (required), url (required),
                   description1 (optional, max 35 chars), description2 (optional, max 35 chars)
        customer_id: Digits-only customer ID. If None, loaded from settings.
        replace: If True, remove existing SITELINK campaign_asset links first (edit workflow).
                 If False (default), append — safe for new campaigns that have no sitelinks yet.

    Returns:
        { "ok": bool, "count": int, "errors": [str, ...] }
    """
    from campaign_safety import check_writes_enabled, WriteBlockedError
    try:
        check_writes_enabled()
    except WriteBlockedError as e:
        logger.warning(f"add_sitelinks_to_campaign blocked by kill switch: {e}")
        return {"ok": False, "count": 0, "errors": [str(e)]}

    settings = get_settings()
    if not customer_id:
        customer_id = "".join(ch for ch in (settings.google_ads_customer_id or "") if ch.isdigit())
    if not customer_id:
        return {"ok": False, "count": 0, "errors": ["google_ads_customer_id not configured"]}

    # Derive campaign name for asset naming from resource name (e.g. ".../campaigns/12345")
    camp_id_suffix = campaign_resource_name.split("/campaigns/")[-1]

    errors = []
    try:
        client = _build_client()
        asset_service = client.get_service("AssetService")
        camp_asset_service = client.get_service("CampaignAssetService")

        # ── Pass 0 (edit mode): remove existing sitelink links before re-adding ──
        if replace:
            removed = _remove_existing_campaign_sitelinks(campaign_resource_name, client, customer_id)
            if removed:
                logger.info(f"add_sitelinks_to_campaign: removed {removed} existing sitelinks (replace=True)")

        # ── Pass 1: Create all sitelink assets in one batch ───────────────────
        asset_ops = []
        valid_sitelinks = []  # parallel list — only items that pass validation
        for sl in sitelinks:
            title = _strip_phone_numbers((sl.get("title") or "").strip())[:25]
            url   = (sl.get("url") or "").strip()
            desc1 = _strip_phone_numbers((sl.get("description1") or "").strip())[:35]
            desc2 = _strip_phone_numbers((sl.get("description2") or "").strip())[:35]

            if not title:
                errors.append(f"Sitelink skipped — empty title after sanitization: {sl!r}")
                continue
            if not url.startswith("https://"):
                errors.append(f"Sitelink '{title}' skipped — URL must start with https://: {url!r}")
                continue

            asset_op = client.get_type("AssetOperation")
            asset    = asset_op.create
            asset.name = f"Camp {camp_id_suffix} Sitelink - {title}"
            asset.sitelink_asset.link_text = title
            asset.final_urls.append(url)  # final_urls is on Asset, not SitelinkAsset

            # descriptions: must be both-present or both-absent (Google policy)
            if desc1 and desc2:
                asset.sitelink_asset.description1 = desc1
                asset.sitelink_asset.description2 = desc2
            elif desc1 or desc2:
                # One provided, one missing — skip both to avoid SITELINK_HAS_ONLY_DESCRIPTION1/2
                logger.warning(f"Sitelink '{title}': both description1 and description2 required; omitting descriptions")

            asset_ops.append(asset_op)
            valid_sitelinks.append({"title": title, "url": url})

        if not asset_ops:
            return {"ok": False, "count": 0, "errors": errors or ["No valid sitelinks to add"]}

        asset_response = asset_service.mutate_assets(
            customer_id=customer_id,
            operations=asset_ops,
        )

        # Collect successfully created asset resource names
        asset_resources = []
        for i, result in enumerate(asset_response.results):
            rn = result.resource_name
            if rn:
                asset_resources.append(rn)
            else:
                label = valid_sitelinks[i]["title"] if i < len(valid_sitelinks) else f"index {i}"
                errors.append(f"Asset create returned empty resource for sitelink '{label}'")

        if not asset_resources:
            return {"ok": False, "count": 0, "errors": errors}

        # ── Pass 2: Link all assets to the campaign in one batch ──────────────
        link_ops = []
        for asset_rn in asset_resources:
            link_op     = client.get_type("CampaignAssetOperation")
            link        = link_op.create
            link.campaign   = campaign_resource_name
            link.asset      = asset_rn
            link.field_type = client.enums.AssetFieldTypeEnum.SITELINK
            link_ops.append(link_op)

        link_response = camp_asset_service.mutate_campaign_assets(
            customer_id=customer_id,
            operations=link_ops,
        )

        # Count actually linked
        linked_count = sum(1 for r in link_response.results if r.resource_name)

        logger.info(f"add_sitelinks_to_campaign: {linked_count} sitelink(s) linked to {campaign_resource_name}")
        return {"ok": True, "count": linked_count, "errors": errors}

    except Exception as e:
        logger.error(f"add_sitelinks_to_campaign failed: {e}")
        return {"ok": False, "count": 0, "errors": [str(e)]}


def add_callouts_to_campaign(
    campaign_resource_name: str,
    callout_texts: list[str],
    customer_id: str | None = None,
) -> dict:
    """
    Create CalloutAsset assets and link them to an existing campaign.

    Two-pass: create all assets in one mutate, then link all to the campaign.
    Kill-switch guarded. Non-fatal — returns {"ok", "count", "errors"}.

    Args:
        campaign_resource_name: Full resource name e.g. "customers/.../campaigns/..."
        callout_texts: 3–10 strings, each ≤25 chars (pre-validated by caller)
        customer_id: Digits-only string. If None, loaded from settings.
    """
    from campaign_safety import check_writes_enabled, WriteBlockedError
    try:
        check_writes_enabled()
    except WriteBlockedError as e:
        logger.warning(f"add_callouts_to_campaign blocked by kill switch: {e}")
        return {"ok": False, "count": 0, "errors": [str(e)]}

    settings = get_settings()
    if not customer_id:
        customer_id = "".join(ch for ch in (settings.google_ads_customer_id or "") if ch.isdigit())
    if not customer_id:
        return {"ok": False, "count": 0, "errors": ["google_ads_customer_id not configured"]}

    camp_id_suffix = campaign_resource_name.split("/campaigns/")[-1]
    errors: list[str] = []

    try:
        client = _build_client()
        asset_service = client.get_service("AssetService")
        camp_asset_service = client.get_service("CampaignAssetService")

        # ── Pass 1: Create all callout assets in one batch ───────────────────────
        import time as _time
        # Short epoch suffix ensures unique asset names — Google returns INVALID_ARGUMENT
        # if you try to create an asset whose name already exists in the account.
        _epoch_suffix = str(int(_time.time()))[-6:]
        asset_ops = []
        valid_texts: list[str] = []
        for text in callout_texts:
            if not isinstance(text, str):
                errors.append(f"Callout skipped — non-string item: {text!r}")
                continue
            text = text.strip()[:25]
            if not text:
                errors.append("Callout skipped — empty text after sanitization")
                continue
            asset_op = client.get_type("AssetOperation")
            asset = asset_op.create
            asset.name = f"Camp {camp_id_suffix} Callout {_epoch_suffix} - {text}"
            asset.callout_asset.callout_text = text
            asset_ops.append(asset_op)
            valid_texts.append(text)

        if not asset_ops:
            return {"ok": False, "count": 0, "errors": errors or ["No valid callout texts"]}

        # Google allows max 10 callout assets per campaign — cap defensively
        if len(asset_ops) > 10:
            logger.warning(f"add_callouts_to_campaign: capping {len(asset_ops)} callouts to 10")
            asset_ops = asset_ops[:10]
            valid_texts = valid_texts[:10]

        asset_response = asset_service.mutate_assets(
            customer_id=customer_id,
            operations=asset_ops,
        )

        asset_resources: list[str] = []
        for i, result in enumerate(asset_response.results):
            rn = result.resource_name
            if rn:
                asset_resources.append(rn)
            else:
                label = valid_texts[i] if i < len(valid_texts) else f"index {i}"
                errors.append(f"Asset create returned empty resource for callout '{label}'")

        if not asset_resources:
            return {"ok": False, "count": 0, "errors": errors}

        # ── Pass 2: Link all callout assets to the campaign ──────────────────────
        link_ops = []
        for asset_rn in asset_resources:
            link_op = client.get_type("CampaignAssetOperation")
            link = link_op.create
            link.campaign = campaign_resource_name
            link.asset = asset_rn
            link.field_type = client.enums.AssetFieldTypeEnum.CALLOUT
            link_ops.append(link_op)

        link_response = camp_asset_service.mutate_campaign_assets(
            customer_id=customer_id,
            operations=link_ops,
        )

        linked_count = sum(1 for r in link_response.results if r.resource_name)
        logger.info(f"add_callouts_to_campaign: {linked_count} callout(s) linked to {campaign_resource_name}")
        return {"ok": True, "count": linked_count, "errors": errors}

    except Exception as e:
        logger.error(f"add_callouts_to_campaign failed: {e}")
        return {"ok": False, "count": 0, "errors": [str(e)]}


def add_structured_snippet_to_campaign(
    campaign_resource_name: str,
    header: str,
    values: list[str],
    customer_id: str | None = None,
) -> dict:
    """
    Create a single StructuredSnippetAsset and link it to an existing campaign.

    One asset per header (Google allows only one snippet per header per campaign).
    Kill-switch guarded. Non-fatal — returns {"ok", "count", "errors"}.

    Args:
        campaign_resource_name: Full resource name e.g. "customers/.../campaigns/..."
        header: Exact Google-required header string, e.g. "Service catalog", "Types"
                Must be one of VALID_SNIPPET_HEADERS defined in ai_optimizer.py.
        values: 3–10 strings, each ≤25 chars (pre-validated by caller)
        customer_id: Digits-only string. If None, loaded from settings.
    """
    from campaign_safety import check_writes_enabled, WriteBlockedError
    try:
        check_writes_enabled()
    except WriteBlockedError as e:
        logger.warning(f"add_structured_snippet_to_campaign blocked by kill switch: {e}")
        return {"ok": False, "count": 0, "errors": [str(e)]}

    settings = get_settings()
    if not customer_id:
        customer_id = "".join(ch for ch in (settings.google_ads_customer_id or "") if ch.isdigit())
    if not customer_id:
        return {"ok": False, "count": 0, "errors": ["google_ads_customer_id not configured"]}

    camp_id_suffix = campaign_resource_name.split("/campaigns/")[-1]
    errors: list[str] = []

    try:
        client = _build_client()
        asset_service = client.get_service("AssetService")
        camp_asset_service = client.get_service("CampaignAssetService")

        # ── Pass 1: Create the structured snippet asset ───────────────────────────
        # Google structured snippets: one asset holds the header + all values.
        clean_values = [v.strip()[:25] for v in values if isinstance(v, str) and v.strip()]
        if len(clean_values) < 3:
            return {
                "ok": False, "count": 0,
                "errors": [f"Structured snippet needs ≥3 values, got {len(clean_values)}"],
            }
        clean_values = clean_values[:10]  # hard cap

        asset_op = client.get_type("AssetOperation")
        asset = asset_op.create
        asset.name = f"Camp {camp_id_suffix} Snippet - {header}"
        asset.structured_snippet_asset.header = header
        asset.structured_snippet_asset.values.extend(clean_values)

        asset_response = asset_service.mutate_assets(
            customer_id=customer_id,
            operations=[asset_op],
        )

        asset_rn = asset_response.results[0].resource_name if asset_response.results else ""
        if not asset_rn:
            return {"ok": False, "count": 0, "errors": ["Asset create returned empty resource"]}

        # ── Pass 2: Link the asset to the campaign ───────────────────────────────
        link_op = client.get_type("CampaignAssetOperation")
        link = link_op.create
        link.campaign = campaign_resource_name
        link.asset = asset_rn
        link.field_type = client.enums.AssetFieldTypeEnum.STRUCTURED_SNIPPET

        link_response = camp_asset_service.mutate_campaign_assets(
            customer_id=customer_id,
            operations=[link_op],
        )

        linked_count = sum(1 for r in link_response.results if r.resource_name)
        logger.info(
            f"add_structured_snippet_to_campaign: '{header}' snippet linked to {campaign_resource_name} "
            f"({len(clean_values)} values)"
        )
        return {"ok": True, "count": linked_count, "errors": errors}

    except Exception as e:
        logger.error(f"add_structured_snippet_to_campaign failed: {e}")
        return {"ok": False, "count": 0, "errors": [str(e)]}


def fetch_campaigns_from_gads() -> list:
    """
    Pull all non-REMOVED campaigns from the Google Ads account.
    READ-ONLY — no kill switch needed.

    Returns list of dicts:
        resource_name, campaign_id (numeric str), campaign_name,
        gads_status ("ENABLED"|"PAUSED"), channel_type,
        start_date, end_date (sentinel dates stripped to ""),
        daily_budget_usd, monthly_budget_usd (daily × 30, approximate)

    Raises on auth/API failure so callers can surface a 502.
    """
    settings = get_settings()
    # Strip non-digits defensively (some envs set "249-804-9505")
    customer_id = "".join(ch for ch in (settings.google_ads_customer_id or "") if ch.isdigit())
    if not customer_id:
        raise ValueError("google_ads_customer_id is not configured")

    client = _build_client()
    service = client.get_service("GoogleAdsService")

    # Pass 1: campaigns — select campaign.campaign_budget (resource name pointer),
    # NOT campaign_budget.amount_micros. Cross-resource joins (FROM campaign +
    # campaign_budget.* field) produce a cryptic "501 GRPC target method can't be
    # resolved". We resolve budgets in a separate pass instead.
    # NOTE: campaign.start_date / campaign.end_date removed from API in v20+.
    campaign_query = """
        SELECT
            campaign.resource_name,
            campaign.id,
            campaign.name,
            campaign.status,
            campaign.advertising_channel_type,
            campaign.campaign_budget
        FROM campaign
        WHERE campaign.status IN (ENABLED, PAUSED)
        ORDER BY campaign.name ASC
    """

    campaigns_raw = []
    budget_resources = set()
    for row in service.search(customer_id=customer_id, query=campaign_query):
        budget_rn = row.campaign.campaign_budget or ""
        if budget_rn:
            budget_resources.add(budget_rn)
        campaigns_raw.append({
            "resource_name":    row.campaign.resource_name,
            "campaign_id":      str(row.campaign.id),
            "campaign_name":    row.campaign.name or "",
            "gads_status":      str(row.campaign.status.name),  # "ENABLED" or "PAUSED" (use .name not str() — str gives integer)
            "channel_type":     str(row.campaign.advertising_channel_type),
            "start_date":       "",
            "end_date":         "",
            "_budget_resource": budget_rn,
        })

    # Pass 2: fetch daily budget amounts by budget resource name
    budget_micros: dict = {}
    if budget_resources:
        in_list = ", ".join(f"'{rn}'" for rn in budget_resources)
        budget_query = f"""
            SELECT
                campaign_budget.resource_name,
                campaign_budget.amount_micros
            FROM campaign_budget
            WHERE campaign_budget.resource_name IN ({in_list})
        """
        for row in service.search(customer_id=customer_id, query=budget_query):
            budget_micros[row.campaign_budget.resource_name] = (
                row.campaign_budget.amount_micros or 0
            )

    # Stitch results and compute USD budgets
    results = []
    for c in campaigns_raw:
        budget_rn    = c.pop("_budget_resource", "")
        daily_micros = budget_micros.get(budget_rn, 0)
        daily_usd    = round(daily_micros / 1_000_000.0, 2)
        c["daily_budget_usd"]   = daily_usd
        c["monthly_budget_usd"] = round(daily_usd * 30, 2)  # approximate monthly
        results.append(c)

    logger.info(f"fetch_campaigns_from_gads: {len(results)} campaigns returned")
    return results


def set_campaign_status(campaign_resource_name: str, target_status: str) -> dict:
    """
    Change a Google Ads campaign's status via the API.

    Args:
        campaign_resource_name: Full resource name, e.g.
            "customers/1234567890/campaigns/9876543210"
        target_status: One of "PAUSED", "ENABLED", "REMOVED"
            REMOVED is irreversible — use only for Stop.

    Returns:
        { "ok": bool, "resource_name": str, "error": str | None }

    Raises nothing — all errors are returned in the dict so callers
    can decide whether to update local DB or surface to the user.
    """
    # ── Kill-switch check ──────────────────────────────────────────────────────
    from campaign_safety import check_writes_enabled, WriteBlockedError
    try:
        check_writes_enabled()
    except WriteBlockedError as e:
        logger.warning(f"set_campaign_status blocked by kill switch: {e}")
        return {"ok": False, "resource_name": campaign_resource_name, "error": str(e)}

    settings = get_settings()
    customer_id = settings.google_ads_customer_id

    valid_statuses = {"PAUSED", "ENABLED", "REMOVED"}
    if target_status not in valid_statuses:
        return {
            "ok": False,
            "resource_name": campaign_resource_name,
            "error": f"Invalid status '{target_status}'. Must be one of {valid_statuses}",
        }

    try:
        client = _build_client()
        campaign_service = client.get_service("CampaignService")

        campaign_operation = client.get_type("CampaignOperation")

        if target_status == "REMOVED":
            # Google Ads API v24 requires the `remove` field for deletion —
            # setting status=REMOVED via an update operation returns INVALID_ENUM_VALUE.
            campaign_operation.remove = campaign_resource_name
        else:
            # PAUSED / ENABLED — standard update + field mask
            campaign = campaign_operation.update
            campaign.resource_name = campaign_resource_name
            status_enum = client.enums.CampaignStatusEnum
            status_map = {
                "PAUSED":  status_enum.PAUSED,
                "ENABLED": status_enum.ENABLED,
            }
            campaign.status = status_map[target_status]

            # Explicit field mask — only update `status`, never touch other fields.
            from google.protobuf import field_mask_pb2
            client.copy_from(
                campaign_operation.update_mask,
                field_mask_pb2.FieldMask(paths=["status"]),
            )

        # Single-operation mutate
        response = campaign_service.mutate_campaigns(
            customer_id=customer_id,
            operations=[campaign_operation],
        )

        updated_resource = response.results[0].resource_name if response.results else campaign_resource_name
        logger.info(f"GAds campaign status → {target_status}: {updated_resource}")
        return {"ok": True, "resource_name": updated_resource, "error": None}

    except Exception as e:
        logger.error(f"GAds set_campaign_status failed ({target_status}): {e}")
        return {"ok": False, "resource_name": campaign_resource_name, "error": str(e)}


def _get_campaign_channel_type(campaign_resource_name: str) -> str:
    """
    Fetch the advertising_channel_type for a campaign resource name.
    Returns the string value e.g. "SEARCH", "PERFORMANCE_MAX", "DISPLAY", or ""
    on failure.
    """
    settings = get_settings()
    customer_id = "".join(ch for ch in (settings.google_ads_customer_id or "") if ch.isdigit())
    try:
        client = _build_client()
        service = client.get_service("GoogleAdsService")
        query = f"""
            SELECT campaign.advertising_channel_type
            FROM campaign
            WHERE campaign.resource_name = '{campaign_resource_name}'
            LIMIT 1
        """
        rows = list(service.search(customer_id=customer_id, query=query))
        if rows:
            return str(rows[0].campaign.advertising_channel_type).upper()
    except Exception as e:
        logger.warning(f"_get_campaign_channel_type failed: {e}")
    return ""


def enable_ai_max(campaign_resource_name: str) -> dict:
    """
    Enable AI Max on an existing Google Search campaign.

    AI Max expands reach beyond the keyword list using Google's AI for search
    term matching. Only valid for SEARCH channel type campaigns.

    Does NOT enable Final URL expansion — that is left OFF by default because
    it breaks landing_url attribution. Implement separately when needed.

    Respects the global kill switch.

    Returns:
        { "ok": bool, "resource_name": str, "error": str | None }
    """
    from campaign_safety import check_writes_enabled, WriteBlockedError
    try:
        check_writes_enabled()
    except WriteBlockedError as e:
        logger.warning(f"enable_ai_max blocked by kill switch: {e}")
        return {"ok": False, "resource_name": campaign_resource_name, "error": str(e)}

    # AI Max only works on Search campaigns
    channel = _get_campaign_channel_type(campaign_resource_name)
    if channel and channel != "SEARCH":
        return {
            "ok": False,
            "resource_name": campaign_resource_name,
            "error": f"AI Max only applies to Search campaigns (this is {channel})",
        }

    settings = get_settings()
    customer_id = "".join(ch for ch in (settings.google_ads_customer_id or "") if ch.isdigit())

    try:
        client = _build_client()
        campaign_service = client.get_service("CampaignService")

        campaign_operation = client.get_type("CampaignOperation")
        campaign = campaign_operation.update
        campaign.resource_name = campaign_resource_name

        # Set the single AI Max master switch
        campaign.ai_max_setting.enable_ai_max = True

        from google.protobuf import field_mask_pb2
        client.copy_from(
            campaign_operation.update_mask,
            field_mask_pb2.FieldMask(paths=["ai_max_setting.enable_ai_max"]),
        )

        response = campaign_service.mutate_campaigns(
            customer_id=customer_id,
            operations=[campaign_operation],
        )

        updated = response.results[0].resource_name if response.results else campaign_resource_name
        logger.info(f"AI Max ENABLED on campaign: {updated}")
        return {"ok": True, "resource_name": updated, "error": None}

    except Exception as e:
        logger.error(f"enable_ai_max failed: {e}")
        return {"ok": False, "resource_name": campaign_resource_name, "error": str(e)}


def disable_ai_max(campaign_resource_name: str) -> dict:
    """
    Disable AI Max on a Google Search campaign.

    Historical lead attribution (search_term_type='ai_max') is preserved as-is —
    disabling AI Max only stops new AI-expanded queries, it does not retroactively
    change historical data.

    Respects the global kill switch.

    Returns:
        { "ok": bool, "resource_name": str, "error": str | None }
    """
    from campaign_safety import check_writes_enabled, WriteBlockedError
    try:
        check_writes_enabled()
    except WriteBlockedError as e:
        logger.warning(f"disable_ai_max blocked by kill switch: {e}")
        return {"ok": False, "resource_name": campaign_resource_name, "error": str(e)}

    settings = get_settings()
    customer_id = "".join(ch for ch in (settings.google_ads_customer_id or "") if ch.isdigit())

    try:
        client = _build_client()
        campaign_service = client.get_service("CampaignService")

        campaign_operation = client.get_type("CampaignOperation")
        campaign = campaign_operation.update
        campaign.resource_name = campaign_resource_name
        campaign.ai_max_setting.enable_ai_max = False

        from google.protobuf import field_mask_pb2
        client.copy_from(
            campaign_operation.update_mask,
            field_mask_pb2.FieldMask(paths=["ai_max_setting.enable_ai_max"]),
        )

        response = campaign_service.mutate_campaigns(
            customer_id=customer_id,
            operations=[campaign_operation],
        )

        updated = response.results[0].resource_name if response.results else campaign_resource_name
        logger.info(f"AI Max DISABLED on campaign: {updated}")
        return {"ok": True, "resource_name": updated, "error": None}

    except Exception as e:
        logger.error(f"disable_ai_max failed: {e}")
        return {"ok": False, "resource_name": campaign_resource_name, "error": str(e)}


def fetch_campaign_build_data(campaign_resource_name: str) -> dict:
    """
    Pull existing keywords, ad copies, and ad groups from Google Ads for a
    specific campaign and return them formatted as campaign_build_json steps.

    READ-ONLY — no kill switch needed.

    Three separate GAQL queries (same two-pass pattern used elsewhere):
      Pass 1: keyword_view — all enabled/paused keywords for this campaign
      Pass 2: ad_group_ad   — all enabled/paused RSAs / ETAs for this campaign
      Pass 3: ad_group      — all enabled/paused ad groups for this campaign

    The returned dict is stored to `gads_campaign_snapshot` (NOT campaign_build_json)
    so it never clobbers user-edited wizard state.  The frontend seeds the wizard
    from the snapshot only when campaign_build_json is empty.

    Returns:
        {
            "keywords":          { ... }   # Keywords step format
            "ad_copy":           { ... }   # Ad Copy step format
            "ad_groups":         { ... }   # Ad Groups step format
            "campaign_settings": { ... }   # campaign-level meta pulled from Pass 3
            "synced_from_gads_at": "ISO timestamp"
        }

    On failure returns:
        { "error": str, "synced_from_gads_at": None }
    """
    from datetime import datetime, timezone

    settings = get_settings()
    customer_id = "".join(ch for ch in (settings.google_ads_customer_id or "") if ch.isdigit())
    if not customer_id:
        return {"error": "google_ads_customer_id is not configured", "synced_from_gads_at": None}

    try:
        client = _build_client()
        service = client.get_service("GoogleAdsService")

        # ── Pass 1: Keywords ──────────────────────────────────────────────────
        # keyword_view is the correct resource (ad_group_criterion includes all
        # criterion types; keyword_view is pre-filtered to keywords only).
        keyword_query = f"""
            SELECT
                ad_group_criterion.resource_name,
                ad_group_criterion.keyword.text,
                ad_group_criterion.keyword.match_type,
                ad_group_criterion.status,
                ad_group_criterion.negative,
                ad_group_criterion.cpc_bid_micros,
                ad_group_criterion.effective_cpc_bid_micros,
                ad_group.id,
                ad_group.name
            FROM keyword_view
            WHERE campaign.resource_name = '{campaign_resource_name}'
              AND ad_group_criterion.status IN (ENABLED, PAUSED)
            ORDER BY ad_group.name ASC, ad_group_criterion.keyword.text ASC
        """

        keywords_list = []
        neg_keywords_list = []
        for row in service.search(customer_id=customer_id, query=keyword_query):
            crit = row.ad_group_criterion
            kw_text = crit.keyword.text or ""
            match_type = str(crit.keyword.match_type).upper().replace("KEYWORD_MATCH_TYPE_", "")
            # Normalise match type strings from proto enum names
            # e.g. "EXACT" → "Exact", "PHRASE" → "Phrase", "BROAD" → "Broad"
            match_type_clean = match_type.capitalize() if match_type else "Broad"

            cpc_micros = crit.cpc_bid_micros or 0
            eff_micros  = crit.effective_cpc_bid_micros or 0
            # Only surface explicit per-keyword max-CPC overrides.
            # effective_cpc_bid_micros falls back to $0.01 floor for paused
            # campaigns — that value is real but meaningless to display.
            cpc_usd = round(cpc_micros / 1_000_000.0, 2) if cpc_micros else None
            eff_cpc_usd = round(eff_micros / 1_000_000.0, 2) if eff_micros else None

            entry = {
                "keyword":       kw_text,
                "match_type":    match_type_clean,
                "cpc_bid":       cpc_usd,       # explicit override only (None if inherited)
                "effective_cpc": eff_cpc_usd,   # Google's effective floor (informational)
                "ad_group":   row.ad_group.name or "",
                "ad_group_id": str(row.ad_group.id),
                "resource_name": crit.resource_name,
                "negative":   bool(crit.negative),
                "status":     str(crit.status).upper(),
            }
            if crit.negative:
                neg_keywords_list.append(entry)
            else:
                keywords_list.append(entry)

        keywords_step = {
            "keywords":          keywords_list,
            "negative_keywords": neg_keywords_list,
            "source":            "imported_from_google_ads",
        }

        # ── Pass 2: Ad Copies (RSA / ETA) ────────────────────────────────────
        ad_query = f"""
            SELECT
                ad_group_ad.resource_name,
                ad_group_ad.status,
                ad_group_ad.ad.id,
                ad_group_ad.ad.type,
                ad_group_ad.ad.responsive_search_ad.headlines,
                ad_group_ad.ad.responsive_search_ad.descriptions,
                ad_group_ad.ad.responsive_search_ad.path1,
                ad_group_ad.ad.responsive_search_ad.path2,
                ad_group_ad.ad.final_urls,
                ad_group_ad.ad_strength,
                ad_group_ad.policy_summary.approval_status,
                ad_group.id,
                ad_group.name
            FROM ad_group_ad
            WHERE campaign.resource_name = '{campaign_resource_name}'
              AND ad_group_ad.status IN (ENABLED, PAUSED)
        """

        ads_list = []
        for row in service.search(customer_id=customer_id, query=ad_query):
            ad = row.ad_group_ad.ad
            rsa = ad.responsive_search_ad

            # Extract headline texts (proto-plus list of AdTextAsset)
            headlines = []
            if rsa and rsa.headlines:
                for h in rsa.headlines:
                    text = h.text if hasattr(h, "text") else ""
                    if text:
                        headlines.append(text)

            # Extract description texts
            descriptions = []
            if rsa and rsa.descriptions:
                for d in rsa.descriptions:
                    text = d.text if hasattr(d, "text") else ""
                    if text:
                        descriptions.append(text)

            final_urls = list(ad.final_urls) if ad.final_urls else []

            ad_strength_raw = str(row.ad_group_ad.ad_strength).upper()
            # Normalise proto enum prefix (AD_STRENGTH_UNSPECIFIED → "UNSPECIFIED")
            ad_strength = ad_strength_raw.replace("AD_STRENGTH_", "")

            approval_raw = str(row.ad_group_ad.policy_summary.approval_status).upper()
            approval = approval_raw.replace("POLICY_APPROVAL_STATUS_", "")

            ads_list.append({
                "resource_name":  row.ad_group_ad.resource_name,
                "ad_id":          str(ad.id),
                "ad_type":        str(ad.type).upper(),
                "ad_group":       row.ad_group.name or "",
                "ad_group_id":    str(row.ad_group.id),
                "headlines":      headlines,
                "descriptions":   descriptions,
                "path1":          rsa.path1 if rsa else "",
                "path2":          rsa.path2 if rsa else "",
                "final_urls":     final_urls,
                "ad_strength":    ad_strength,
                "approval_status": approval,
                "status":         str(row.ad_group_ad.status).upper(),
                "source":         "imported_from_google_ads",
            })

        ad_copy_step = {
            "ads":    ads_list,
            "source": "imported_from_google_ads",
        }

        # ── Pass 3: Ad Groups ─────────────────────────────────────────────────
        ad_group_query = f"""
            SELECT
                ad_group.resource_name,
                ad_group.id,
                ad_group.name,
                ad_group.status,
                ad_group.type,
                ad_group.cpc_bid_micros,
                ad_group.target_cpa_micros
            FROM ad_group
            WHERE campaign.resource_name = '{campaign_resource_name}'
              AND ad_group.status IN (ENABLED, PAUSED)
            ORDER BY ad_group.name ASC
        """

        ad_groups_list = []
        for row in service.search(customer_id=customer_id, query=ad_group_query):
            ag = row.ad_group
            cpc_micros = ag.cpc_bid_micros or 0
            target_cpa_micros = ag.target_cpa_micros or 0

            ad_groups_list.append({
                "resource_name":  ag.resource_name,
                "ad_group_id":    str(ag.id),
                "ad_group_name":  ag.name or "",
                "status":         str(ag.status).upper(),
                "type":           str(ag.type).upper(),
                "cpc_bid_usd":    round(cpc_micros / 1_000_000.0, 2) if cpc_micros else None,
                "target_cpa_usd": round(target_cpa_micros / 1_000_000.0, 2) if target_cpa_micros else None,
                "source":         "imported_from_google_ads",
            })

        ad_groups_step = {
            "ad_groups": ad_groups_list,
            "source":    "imported_from_google_ads",
        }

        # campaign_settings: derive from ad group data (no separate query needed)
        # These are aggregated summaries useful for the Strategy tab seed
        unique_ag_types = list({ag["type"] for ag in ad_groups_list})
        campaign_settings = {
            "ad_group_count":  len(ad_groups_list),
            "keyword_count":   len(keywords_list),
            "neg_kw_count":    len(neg_keywords_list),
            "ad_count":        len(ads_list),
            "ad_group_types":  unique_ag_types,
            "source":          "imported_from_google_ads",
        }

        synced_at = datetime.now(timezone.utc).isoformat()
        logger.info(
            f"fetch_campaign_build_data: {len(keywords_list)} kw, "
            f"{len(ads_list)} ads, {len(ad_groups_list)} ad groups "
            f"for {campaign_resource_name}"
        )

        return {
            "keywords":          keywords_step,
            "ad_copy":           ad_copy_step,
            "ad_groups":         ad_groups_step,
            "campaign_settings": campaign_settings,
            "synced_from_gads_at": synced_at,
        }

    except Exception as e:
        logger.error(f"fetch_campaign_build_data failed: {e}")
        return {"error": str(e), "synced_from_gads_at": None}
