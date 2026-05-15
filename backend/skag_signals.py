"""
SKAG Signal Collector — Single Keyword Ad Group recommendation engine.

Problem context
───────────────
Google's API blocks per-call search term data (privacy wall). The only way
to get definitive keyword attribution for phone calls is to ensure each ad
group contains exactly ONE keyword (SKAG = Single Keyword Ad Group). When a
call comes in via a SKAG ad group, the keyword is unambiguous.

This module identifies the best candidates for SKAG extraction from existing
multi-keyword ad groups based on call volume, OD appointment evidence, search
term data, and keyword volume signals.

Entry points
────────────
    from skag_signals import collect_skag_signals, get_skag_candidates_text

    # Both functions accept campaign_name (string) — matching what ai_optimizer
    # uses in its per-campaign loop. campaign_id is resolved internally.

    # For CLI inspection (no Claude):
    python3 skag_signals.py [campaign_name_substring]

No writes are made here. All writes happen in PR 4 (execute_create_skag).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ── Tunables ──────────────────────────────────────────────────────────────────
MIN_CLICKS_30D          = 15    # keyword must have ≥15 clicks in 30d to qualify
MIN_IMPRESSIONS_30D     = 200   # keyword must have ≥200 impressions
MIN_CALL_CONVERSIONS    = 1     # ≥1 call conversion for the ad group to qualify
MIN_AD_GROUP_SIZE       = 3     # source ad group must have ≥3 keywords (worth SKAG-ing)
MAX_CANDIDATES_RETURNED = 8     # cap returned candidates per run to avoid prompt bloat
LOOKBACK_DAYS           = 30    # all signal windows use 30d

# Scoring weights — must sum to 1.0
W_OD_SIGNAL    = 0.40   # OD appointment evidence (strongest signal)
W_CALLS        = 0.25   # call conversion volume
W_LONGTAIL     = 0.10   # specificity of the keyword (≥3 words = longtail bonus)
W_VOLUME       = 0.15   # raw click volume (normalized)
W_EFFICIENCY   = 0.10   # CTR quality signal (impression share proxy)


@dataclass
class SKAGCandidate:
    """A single keyword that's a strong candidate for SKAG extraction."""
    campaign_id:          str       # numeric GAds campaign ID (from call_search_terms)
    campaign_name:        str
    source_ad_group_id:   str       # empty at collection time; filled at execution
    source_ad_group_name: str
    keyword_text:         str
    match_type:           str       # current match type in source ad group
    ad_group_size:        int       # total keywords in source ad group
    clicks_30d:           int
    impressions_30d:      int
    cost_30d:             float
    conversions_30d:      float
    call_conversions_30d: float     # call-specific convs from gads_call_search_terms
    od_appointments:      int       # calls from this ag matched to OD appointments
    search_terms_in_ag:   int       # unique search terms recorded for this ad group
    impression_share:     float     # keyword impression share (0–1)
    score:                float = 0.0
    score_breakdown:      dict = field(default_factory=dict)
    suggested_ag_name:    str = ""  # generated at collection time

    def as_dict(self) -> dict:
        return {
            "campaign_id":          self.campaign_id,
            "campaign_name":        self.campaign_name,
            "source_ad_group_id":   self.source_ad_group_id,
            "source_ad_group_name": self.source_ad_group_name,
            "keyword_text":         self.keyword_text,
            "match_type":           self.match_type,
            "ad_group_size":        self.ad_group_size,
            "clicks_30d":           self.clicks_30d,
            "impressions_30d":      self.impressions_30d,
            "cost_30d":             round(self.cost_30d, 2),
            "conversions_30d":      round(self.conversions_30d, 2),
            "call_conversions_30d": round(self.call_conversions_30d, 2),
            "od_appointments":      self.od_appointments,
            "search_terms_in_ag":   self.search_terms_in_ag,
            "impression_share":     round(self.impression_share, 3),
            "score":                round(self.score, 4),
            "score_breakdown":      self.score_breakdown,
            "suggested_ag_name":    self.suggested_ag_name,
        }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _db_path() -> str:
    import os
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "pipeline.db")


def _normalize(value: float, max_val: float) -> float:
    """Normalize value to [0, 1] against a reference maximum."""
    if max_val <= 0:
        return 0.0
    return min(value / max_val, 1.0)


def _suggest_skag_name(keyword_text: str) -> str:
    """Generate a readable SKAG ad group name from the keyword."""
    kw_clean = keyword_text.strip().lower()
    kw_title = " ".join(w.capitalize() for w in kw_clean.split())
    return f"SKAG — {kw_title}"


# Google Ads API stores match_type as an integer enum or as the string enum name.
# Both forms appear in gads_keywords_cache depending on the SDK version.
_MATCH_TYPE_MAP: dict[str, str] = {
    "2":                      "EXACT",
    "3":                      "PHRASE",
    "4":                      "BROAD",
    "KEYWORD_MATCH_TYPE_EXACT":   "EXACT",
    "KEYWORD_MATCH_TYPE_PHRASE":  "PHRASE",
    "KEYWORD_MATCH_TYPE_BROAD":   "BROAD",
    "KEYWORDMATCHTYPE.EXACT":     "EXACT",
    "KEYWORDMATCHTYPE.PHRASE":    "PHRASE",
    "KEYWORDMATCHTYPE.BROAD":     "BROAD",
}


def _normalize_match_type(raw: str) -> str:
    """Convert any match_type representation to EXACT / PHRASE / BROAD."""
    if not raw:
        return "BROAD"
    key = str(raw).strip().upper().replace(" ", "")
    return _MATCH_TYPE_MAP.get(key, raw.upper())


def _resolve_campaign_id(conn: sqlite3.Connection, campaign_name: str) -> str:
    """
    Look up campaign_id for a campaign_name from gads_call_search_terms.
    Returns empty string if not found (campaign may not have call tracking).
    """
    row = conn.execute("""
        SELECT campaign_id FROM gads_call_search_terms
        WHERE LOWER(TRIM(campaign_name)) = LOWER(TRIM(?))
        LIMIT 1
    """, (campaign_name,)).fetchone()
    return row["campaign_id"] if row else ""


# ── Signal collectors ─────────────────────────────────────────────────────────

def _collect_ad_group_keywords(
    conn: sqlite3.Connection,
    campaign_name: str,
    days: int = LOOKBACK_DAYS,
) -> dict[str, list[sqlite3.Row]]:
    """
    Returns dict keyed by ad_group_name → list of keyword rows from
    gads_keywords_cache. Filters to keywords with ≥MIN_CLICKS_30D and
    ≥MIN_IMPRESSIONS_30D. Matches by campaign_name (exact, case-insensitive).
    """
    rows = conn.execute("""
        SELECT
            keyword_text, match_type, ad_group_name, campaign_name,
            impressions, clicks, cost, conversions, avg_cpc, quality_score,
            impression_share
        FROM gads_keywords_cache
        WHERE LOWER(TRIM(campaign_name)) = LOWER(TRIM(?))
          AND days = ?
          AND clicks >= ?
          AND impressions >= ?
    """, (campaign_name, days, MIN_CLICKS_30D, MIN_IMPRESSIONS_30D)).fetchall()

    by_ag: dict[str, list] = {}
    for r in rows:
        ag = r["ad_group_name"]
        by_ag.setdefault(ag, []).append(r)
    return by_ag


def _collect_all_keywords_for_ag_size(
    conn: sqlite3.Connection,
    campaign_name: str,
    days: int = LOOKBACK_DAYS,
) -> dict[str, int]:
    """
    Returns total keyword count per ad_group_name for the campaign
    (no click/impression filter — used to determine ad group size).
    """
    rows = conn.execute("""
        SELECT ad_group_name, COUNT(*) AS kw_count
        FROM gads_keywords_cache
        WHERE LOWER(TRIM(campaign_name)) = LOWER(TRIM(?))
          AND days = ?
        GROUP BY ad_group_name
    """, (campaign_name, days)).fetchall()
    return {r["ad_group_name"]: int(r["kw_count"]) for r in rows}


def _collect_call_search_terms_by_ag(
    conn: sqlite3.Connection,
    campaign_id: str,
    days: int = LOOKBACK_DAYS,
) -> dict[str, float]:
    """
    Returns total call conversions per ad_group_name from gads_call_search_terms.
    Filters by days to avoid double-counting if multiple windows are stored.
    """
    if not campaign_id:
        return {}
    rows = conn.execute("""
        SELECT ad_group_name, SUM(conversions) AS call_conv
        FROM gads_call_search_terms
        WHERE campaign_id = ?
          AND days = ?
        GROUP BY ad_group_name
    """, (campaign_id, days)).fetchall()
    return {r["ad_group_name"]: float(r["call_conv"] or 0) for r in rows}


def _collect_search_term_count_by_ag(
    conn: sqlite3.Connection,
    campaign_id: str,
    days: int = LOOKBACK_DAYS,
) -> dict[str, int]:
    """Count of unique search terms per ad_group_name from gads_call_search_terms."""
    if not campaign_id:
        return {}
    rows = conn.execute("""
        SELECT ad_group_name, COUNT(DISTINCT search_term) AS st_count
        FROM gads_call_search_terms
        WHERE campaign_id = ?
          AND days = ?
        GROUP BY ad_group_name
    """, (campaign_id, days)).fetchall()
    return {r["ad_group_name"]: int(r["st_count"] or 0) for r in rows}


def _collect_od_appointments_by_ag(
    conn: sqlite3.Connection,
    campaign_name: str,
    days: int = LOOKBACK_DAYS,
) -> dict[str, int]:
    """
    Returns OD-confirmed appointment count per ad_group_name within the lookback
    window. Joins gads_call_view (ad_group) → mango_calls (od_appointment_id).
    Only counts calls where od_appointment_id is set (confirmed OD match).
    Time-bounded so OD signal is on the same 30-day basis as all other signals.
    """
    rows = conn.execute("""
        SELECT gcv.ad_group_name, COUNT(mc.uuid) AS appt_count
        FROM gads_call_view gcv
        JOIN mango_calls mc ON mc.gads_call_id = gcv.call_id
        WHERE LOWER(TRIM(gcv.campaign_name)) = LOWER(TRIM(?))
          AND mc.od_appointment_id IS NOT NULL
          AND mc.od_appointment_id != ''
          AND mc.started_at >= datetime('now', '-' || ? || ' days')
        GROUP BY gcv.ad_group_name
    """, (campaign_name, days)).fetchall()
    return {r["ad_group_name"]: int(r["appt_count"] or 0) for r in rows}


def _get_already_pending_pairs(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """
    Returns (keyword_text, source_ad_group_name) pairs that already have an
    active recommendation — so we don't surface them again.
    'failed' is allowed to retry (transient API errors); 'rejected'/'reverted'
    are permanent user decisions. All other statuses block re-recommendation.
    """
    rows = conn.execute("""
        SELECT keyword_text, source_ad_group_name
        FROM skag_recommendations
        WHERE status NOT IN ('rejected', 'reverted', 'failed')
    """).fetchall()
    return {(r["keyword_text"], r["source_ad_group_name"]) for r in rows}


# ── Scoring ───────────────────────────────────────────────────────────────────

def score_candidate(
    c: SKAGCandidate,
    max_clicks: float,
    max_od: float,
    max_calls: float,
) -> tuple[float, dict]:
    """
    Score a candidate on a 0–1 scale. Returns (score, breakdown_dict).

    Weights:
      OD signal   40%  — confirmed OD appointments from this ad group
      Calls       25%  — call conversions attributed to ad group
      Volume      15%  — raw click volume normalized to peer max
      Long-tail   10%  — keyword specificity (≥3 words = full score)
      Efficiency  10%  — impression share as a CTR quality proxy
    """
    od_score        = _normalize(c.od_appointments, max(max_od, 1))
    call_score      = _normalize(c.call_conversions_30d, max(max_calls, 1))
    volume_score    = _normalize(c.clicks_30d, max(max_clicks, 1))
    longtail_score  = min(len(c.keyword_text.split()) / 3.0, 1.0)
    efficiency_score = min(c.impression_share, 1.0)  # already a 0–1 fraction

    total = (
        W_OD_SIGNAL  * od_score
        + W_CALLS    * call_score
        + W_VOLUME   * volume_score
        + W_LONGTAIL * longtail_score
        + W_EFFICIENCY * efficiency_score
    )

    breakdown = {
        "od":         round(od_score, 3),
        "calls":      round(call_score, 3),
        "volume":     round(volume_score, 3),
        "longtail":   round(longtail_score, 3),
        "efficiency": round(efficiency_score, 3),
        "total":      round(total, 4),
    }
    return round(total, 4), breakdown


# ── Main collector ────────────────────────────────────────────────────────────

def collect_skag_signals(
    campaign_name: str,
    db_path: Optional[str] = None,
    days: int = LOOKBACK_DAYS,
) -> list[SKAGCandidate]:
    """
    Collect, score, and return top SKAG candidates for a campaign.

    Accepts campaign_name (matching ai_optimizer.py's per-campaign loop).

    Steps:
    1. Pull keywords by ad group from gads_keywords_cache (by campaign_name).
    2. Resolve campaign_id from gads_call_search_terms.
    3. Skip ad groups with fewer than MIN_AD_GROUP_SIZE keywords.
    4. Cross-reference call convs, OD appointments, search term counts.
    5. Skip keywords already in an active skag_recommendation.
    6. Score each candidate and return top MAX_CANDIDATES_RETURNED.
    """
    if db_path is None:
        db_path = _db_path()

    candidates: list[SKAGCandidate] = []
    conn = _get_conn(db_path)

    try:
        with conn:
            # Resolve campaign_id (needed for call_search_terms joins)
            campaign_id = _resolve_campaign_id(conn, campaign_name)

            # Pull keyword data by ad group
            ag_keywords = _collect_ad_group_keywords(conn, campaign_name, days)
            # Pull full ad group sizes (no click filter — accurate count)
            ag_sizes    = _collect_all_keywords_for_ag_size(conn, campaign_name, days)
            # Pull cross-signals (all filtered to same lookback window)
            call_convs  = _collect_call_search_terms_by_ag(conn, campaign_id, days)
            st_counts   = _collect_search_term_count_by_ag(conn, campaign_id, days)
            od_appts    = _collect_od_appointments_by_ag(conn, campaign_name, days)
            pending     = _get_already_pending_pairs(conn)

            for ag_name, kw_rows in ag_keywords.items():
                # Use full ad group size for MIN_AD_GROUP_SIZE check
                full_ag_size = ag_sizes.get(ag_name, len(kw_rows))
                if full_ag_size < MIN_AD_GROUP_SIZE:
                    continue

                ag_call_convs = call_convs.get(ag_name, 0.0)
                ag_od_appts   = od_appts.get(ag_name, 0)
                ag_st_count   = st_counts.get(ag_name, 0)

                # Require at least some evidence this ad group drives calls
                if ag_call_convs < MIN_CALL_CONVERSIONS and ag_od_appts < 1:
                    continue

                for kw in kw_rows:
                    kw_text = kw["keyword_text"]
                    # Skip already-pending pairs
                    if (kw_text, ag_name) in pending:
                        continue

                    c = SKAGCandidate(
                        campaign_id=campaign_id,
                        campaign_name=campaign_name,
                        source_ad_group_id="",  # populated at execution time (PR 4)
                        source_ad_group_name=ag_name,
                        keyword_text=kw_text,
                        match_type=_normalize_match_type(kw["match_type"]),
                        ad_group_size=full_ag_size,
                        clicks_30d=int(kw["clicks"] or 0),
                        impressions_30d=int(kw["impressions"] or 0),
                        cost_30d=float(kw["cost"] or 0),
                        conversions_30d=float(kw["conversions"] or 0),
                        call_conversions_30d=ag_call_convs,
                        od_appointments=ag_od_appts,
                        search_terms_in_ag=ag_st_count,
                        impression_share=float(kw["impression_share"] or 0),
                        suggested_ag_name=_suggest_skag_name(kw_text),
                    )
                    candidates.append(c)

        # Score all candidates
        if candidates:
            max_clicks = max(c.clicks_30d for c in candidates) or 1.0
            max_od     = max(c.od_appointments for c in candidates) or 1.0
            max_calls  = max(c.call_conversions_30d for c in candidates) or 1.0

            for c in candidates:
                c.score, c.score_breakdown = score_candidate(
                    c, max_clicks, max_od, max_calls
                )

        # Sort by score descending, cap at MAX_CANDIDATES_RETURNED
        candidates.sort(key=lambda c: c.score, reverse=True)
        candidates = candidates[:MAX_CANDIDATES_RETURNED]

        logger.info(
            "collect_skag_signals(campaign=%r, id=%s): %d candidates",
            campaign_name, campaign_id or "unknown", len(candidates),
        )
        return candidates

    except Exception:
        logger.exception("collect_skag_signals failed for campaign %r", campaign_name)
        return []
    finally:
        conn.close()


def filter_pending_skags(
    candidates: list[SKAGCandidate],
    db_path: Optional[str] = None,
) -> list[SKAGCandidate]:
    """
    Remove candidates that now have an active recommendation (race-condition guard).
    Useful if collect_skag_signals was called and time passed before prompt injection.
    """
    if not candidates:
        return candidates
    if db_path is None:
        db_path = _db_path()
    conn = _get_conn(db_path)
    try:
        with conn:
            pending = _get_already_pending_pairs(conn)
    finally:
        conn.close()
    return [
        c for c in candidates
        if (c.keyword_text, c.source_ad_group_name) not in pending
    ]


# ── Prompt text formatter ─────────────────────────────────────────────────────

def get_skag_candidates_text(
    campaign_name: str,
    db_path: Optional[str] = None,
) -> str:
    """
    Return a plain-text block describing top SKAG candidates for this campaign.
    Designed to be injected into the per-campaign Claude optimizer prompt.

    Returns empty string if no candidates qualify (safe to inject unconditionally).
    """
    candidates = collect_skag_signals(campaign_name, db_path=db_path)
    if not candidates:
        return ""

    lines = [
        "── SKAG ATTRIBUTION OPPORTUNITY ──",
        "Google blocks per-call search term data. The only way to get definitive",
        "keyword attribution for phone calls is SKAG (one keyword per ad group).",
        "The following keywords are top candidates for SKAG extraction based on",
        "call volume, OD appointments, and search term evidence.",
        "",
        "When recommending SKAG creation, use action type 'create_skag' with fields:",
        "  keyword_text, source_ad_group_name, campaign_id, new_ad_group_name",
        "HARD RULES: Max 2 SKAGs per optimizer run. NEVER add negatives at creation",
        "time. NEVER pause the source keyword. Copy RSAs verbatim. Use EXACT match.",
        "",
    ]

    for i, c in enumerate(candidates, 1):
        lines.append(
            f"  #{i}  [score:{c.score:.2f}] \"{c.keyword_text}\"  ({c.match_type})"
        )
        lines.append(
            f"       Ad Group: {c.source_ad_group_name}  "
            f"(size: {c.ad_group_size} kws)"
        )
        lines.append(
            f"       Clicks: {c.clicks_30d}  |  Call Convs: {c.call_conversions_30d:.0f}"
            f"  |  OD Appts: {c.od_appointments}"
            f"  |  Search Terms in AG: {c.search_terms_in_ag}"
        )
        lines.append(
            f"       Suggested SKAG name: {c.suggested_ag_name}"
        )
        lines.append("")

    lines.append("── END SKAG CANDIDATES ──")
    return "\n".join(lines)


# ── PR 5: lock_skag_traffic — deferred source-side negative ──────────────────

SKAG_LOCK_DELAY_DAYS: int = 7   # Days after SKAG creation before adding negative


def lock_skag_traffic(db_path: str = "", dry_run: bool = False) -> int:
    """
    Nightly job: for every SKAG that was created in Google Ads 7+ days ago
    and is still status='created' (not yet locked), add an EXACT negative
    keyword to the SOURCE ad group so all traffic routes through the SKAG.

    Strategy:
    - Only acts on rows where status='created' AND created_in_gads_at is old enough.
    - Requires new_ad_group_id (source ad group resource) to resolve the source
      ad group resource via a GAds GAQL query.
    - On success: sets status='locked' and locked_at timestamp.
    - On failure: logs error, leaves status='created' so the next run retries.
    - dry_run=True: logs what would happen without touching GAds or the DB.

    Returns: number of SKAGs successfully locked this run.
    """
    from datetime import datetime, timezone, timedelta

    if not db_path:
        db_path = _db_path()

    cutoff = (datetime.now(timezone.utc) - timedelta(days=SKAG_LOCK_DELAY_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    conn = _get_conn(db_path)
    try:
        with conn:
            rows = conn.execute("""
                SELECT recommendation_id, campaign_id, campaign_name,
                       keyword_text, source_ad_group_name, new_ad_group_id,
                       created_in_gads_at
                FROM skag_recommendations
                WHERE status = 'created'
                  AND created_in_gads_at != ''
                  AND created_in_gads_at < ?
            """, (cutoff,)).fetchall()
    except Exception:
        logger.exception("lock_skag_traffic: DB query failed")
        return 0
    finally:
        conn.close()

    if not rows:
        logger.info("lock_skag_traffic: no SKAGs eligible for locking (< %d days old or none created)", SKAG_LOCK_DELAY_DAYS)
        return 0

    logger.info("lock_skag_traffic: %d SKAG(s) eligible for source-side negative", len(rows))

    locked_count = 0
    for row in rows:
        rec_id       = row["recommendation_id"]
        campaign_id  = row["campaign_id"]
        kw_text      = row["keyword_text"]
        source_ag    = row["source_ad_group_name"]
        new_ag_id    = row["new_ad_group_id"] or ""  # resource name of the NEW SKAG ad group
        created_at   = row["created_in_gads_at"]

        if not new_ag_id:
            logger.warning(
                "lock_skag_traffic: rec=%s has no new_ad_group_id — skipping '%s'",
                rec_id[:8], kw_text
            )
            continue

        # Resolve the SOURCE ad group resource name via GAds.
        # new_ag_id is the SKAG ad group resource (customers/NNN/adGroups/MMM).
        # We need to find the source ad group by name within the same campaign.
        source_ag_resource = ""
        try:
            from ai_optimizer import _build_client as _lock_build_client
            from config import get_settings as _lock_settings
            _s = _lock_settings()
            _cid = "".join(ch for ch in (_s.google_ads_customer_id or "") if ch.isdigit())

            # Derive campaign_resource from new_ag_id: customers/NNN/adGroups/MMM
            # → campaign resource is in the campaigns table
            _camp_resource = ""
            _conn2 = _get_conn(db_path)
            try:
                with _conn2:
                    _cr = _conn2.execute(
                        "SELECT gads_campaign_resource FROM campaigns WHERE campaign_id = ?",
                        (campaign_id,)
                    ).fetchone()
                    if _cr:
                        _camp_resource = _cr[0] or ""
            finally:
                _conn2.close()

            if not _camp_resource:
                logger.warning(
                    "lock_skag_traffic: cannot resolve campaign_resource for campaign_id=%s — skipping '%s'",
                    campaign_id, kw_text
                )
                continue

            _client = _lock_build_client()
            ga_service = _client.get_service("GoogleAdsService")
            _ag_escaped = source_ag.replace("'", "''")
            ag_q = f"""
                SELECT ad_group.resource_name
                FROM ad_group
                WHERE ad_group.campaign = '{_camp_resource}'
                  AND ad_group.name = '{_ag_escaped}'
                  AND ad_group.status != 'REMOVED'
                LIMIT 1
            """
            for r in ga_service.search(customer_id=_cid, query=ag_q):
                source_ag_resource = r.ad_group.resource_name
                break

        except Exception as e:
            logger.error(
                "lock_skag_traffic: could not resolve source ag '%s' for rec=%s: %s",
                source_ag, rec_id[:8], e
            )
            continue

        if not source_ag_resource:
            logger.warning(
                "lock_skag_traffic: source ag '%s' not found in GAds for rec=%s — skipping",
                source_ag, rec_id[:8]
            )
            continue

        if dry_run:
            logger.info(
                "lock_skag_traffic [DRY RUN]: would negate '%s' [EXACT] on ag '%s' (%s) for rec=%s",
                kw_text, source_ag, source_ag_resource, rec_id[:8]
            )
            locked_count += 1
            continue

        # Add the negative to the source ad group
        try:
            from google_ads_write import add_negative_keyword_to_ad_group
            add_negative_keyword_to_ad_group(
                ad_group_resource=source_ag_resource,
                keyword_text=kw_text,
                match_type="EXACT",
            )
        except Exception as e:
            logger.error(
                "lock_skag_traffic: failed to negate '%s' on '%s' (rec=%s): %s",
                kw_text, source_ag, rec_id[:8], e
            )
            continue

        # Mark locked in DB
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _conn3 = _get_conn(db_path)
        try:
            with _conn3:
                _conn3.execute("""
                    UPDATE skag_recommendations
                    SET status = 'locked',
                        locked_at = ?,
                        steps_completed = json_insert(
                            COALESCE(steps_completed, '[]'),
                            '$[#]', json_array('lock_traffic', ?)
                        )
                    WHERE recommendation_id = ?
                """, (now_iso, source_ag_resource, rec_id))
        except Exception as e:
            logger.error(
                "lock_skag_traffic: DB update failed for rec=%s: %s — negative was pushed, status not updated",
                rec_id[:8], e
            )
            # The negative is live even though the DB didn't update.
            # The next run will try again and hit KEYWORD_ALREADY_EXISTS (idempotent).
        finally:
            _conn3.close()

        logger.info(
            "lock_skag_traffic: locked '%s' — negated on '%s' (%s) [created %s]",
            kw_text, source_ag, source_ag_resource, created_at
        )
        locked_count += 1

    logger.info("lock_skag_traffic: %d/%d SKAG(s) locked this run", locked_count, len(rows))
    return locked_count


# ── PR 6: skag_outcomes snapshot + zombie revert ─────────────────────────────

SKAG_ZOMBIE_DAYS:        int = 30   # Days live before zombie check
SKAG_ZOMBIE_MIN_IMPR:    int = 50   # Min impressions expected in zombie window
SKAG_ZOMBIE_MIN_CALLS:   float = 0.5  # Min call conversions expected in zombie window


def snapshot_skag_outcomes(db_path: str = "") -> int:
    """
    Nightly job: pull 30-day performance metrics from GAds for every SKAG
    that is currently 'created' or 'locked', and write a row to skag_outcomes_30d.

    Uses the GAds search_stream on 'ad_group' resource filtered by the SKAG
    ad group resource name. Idempotent: UNIQUE(rec_id, snapshot_date) prevents
    duplicates on the same day.

    Returns: number of snapshots written.
    """
    from datetime import datetime, timezone

    if not db_path:
        db_path = _db_path()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Fetch all live SKAGs
    conn = _get_conn(db_path)
    try:
        with conn:
            rows = conn.execute("""
                SELECT id, recommendation_id, new_ad_group_id, created_in_gads_at
                FROM skag_recommendations
                WHERE status IN ('created', 'locked')
                  AND new_ad_group_id != ''
                  AND new_ad_group_id IS NOT NULL
            """).fetchall()
    except Exception:
        logger.exception("snapshot_skag_outcomes: DB query failed")
        return 0
    finally:
        conn.close()

    if not rows:
        logger.info("snapshot_skag_outcomes: no live SKAGs to snapshot")
        return 0

    logger.info("snapshot_skag_outcomes: snapshotting %d SKAG(s)", len(rows))

    try:
        from ai_optimizer import _build_client as _snap_build
        from config import get_settings as _snap_settings
        _s = _snap_settings()
        _cid = "".join(ch for ch in (_s.google_ads_customer_id or "") if ch.isdigit())
        _client = _snap_build()
        ga_service = _client.get_service("GoogleAdsService")
    except Exception as e:
        logger.error("snapshot_skag_outcomes: could not build GAds client: %s", e)
        return 0

    written = 0
    for row in rows:
        db_id     = row["id"]
        rec_id    = row["recommendation_id"]
        ag_res    = row["new_ad_group_id"]
        created   = row["created_in_gads_at"] or ""

        # Compute days_live
        days_live = 0
        try:
            created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            days_live = max(0, (datetime.now(timezone.utc) - created_dt).days)
        except Exception:
            pass

        # Pull 30-day metrics from GAds
        impr = clicks = cost_micros = 0
        call_convs = 0.0
        gads_pull_ok = True
        try:
            _ag_escaped = ag_res.replace("'", "''")
            q = f"""
                SELECT
                    metrics.impressions,
                    metrics.clicks,
                    metrics.cost_micros,
                    metrics.conversions
                FROM ad_group
                WHERE ad_group.resource_name = '{_ag_escaped}'
                  AND segments.date DURING LAST_30_DAYS
            """
            for r in ga_service.search(customer_id=_cid, query=q):
                impr        += r.metrics.impressions
                clicks      += r.metrics.clicks
                cost_micros += r.metrics.cost_micros
                call_convs  += r.metrics.conversions
        except Exception as e:
            logger.warning("snapshot_skag_outcomes: GAds query failed for %s: %s", ag_res[:30], e)
            gads_pull_ok = False

        # Skip insert if GAds pull failed — don't write zero-metric rows that
        # could falsely trigger zombie detection on a transient API outage.
        if not gads_pull_ok:
            continue

        # Write to skag_outcomes_30d
        _conn2 = _get_conn(db_path)
        try:
            with _conn2:
                _conn2.execute("""
                    INSERT OR IGNORE INTO skag_outcomes_30d
                        (skag_recommendation_id, snapshot_date, days_live,
                         impressions, clicks, cost_micros, call_conversions)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (db_id, today, days_live, impr, clicks, cost_micros, call_convs))
            written += 1
        except Exception as e:
            logger.error("snapshot_skag_outcomes: DB insert failed for rec=%s: %s", rec_id[:8], e)
        finally:
            _conn2.close()

    logger.info("snapshot_skag_outcomes: wrote %d/%d snapshot(s) for %s", written, len(rows), today)
    return written


def revert_zombie_skags(db_path: str = "", dry_run: bool = False) -> int:
    """
    Nightly job: find SKAGs that have been live for 30+ days with fewer than
    50 impressions AND fewer than 0.5 call conversions — these are 'zombies'
    that never received traffic. Mark them 'reverted' so the signal collector
    can re-surface them if traffic conditions change.

    Note: this does NOT remove the SKAG ad group from Google Ads (that requires
    manual cleanup or a separate admin action). It only updates the DB status
    so future optimizer runs stop treating the keyword as 'already handled'.

    Returns: number of zombies reverted.
    """
    if not db_path:
        db_path = _db_path()

    conn = _get_conn(db_path)
    try:
        with conn:
            # Find SKAGs with a recent snapshot showing zombie conditions
            zombie_rows = conn.execute("""
                SELECT sr.recommendation_id, sr.keyword_text, sr.source_ad_group_name,
                       sr.campaign_name, o.impressions, o.clicks, o.call_conversions,
                       o.days_live
                FROM skag_recommendations sr
                JOIN skag_outcomes_30d o ON o.skag_recommendation_id = sr.id
                WHERE sr.status IN ('created', 'locked')
                  AND o.days_live >= ?
                  AND o.snapshot_date = (
                      SELECT MAX(snapshot_date) FROM skag_outcomes_30d
                      WHERE skag_recommendation_id = sr.id
                  )
                  AND o.impressions < ?
                  AND o.call_conversions < ?
            """, (SKAG_ZOMBIE_DAYS, SKAG_ZOMBIE_MIN_IMPR, SKAG_ZOMBIE_MIN_CALLS)).fetchall()
    except Exception:
        logger.exception("revert_zombie_skags: DB query failed")
        return 0
    finally:
        conn.close()

    if not zombie_rows:
        logger.info("revert_zombie_skags: no zombie SKAGs found")
        return 0

    logger.info("revert_zombie_skags: %d zombie SKAG(s) found", len(zombie_rows))

    reverted = 0
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for row in zombie_rows:
        rec_id = row["recommendation_id"]
        kw     = row["keyword_text"]
        ag     = row["source_ad_group_name"]
        impr   = row["impressions"]
        calls  = row["call_conversions"]
        d_live = row["days_live"]

        if dry_run:
            logger.info(
                "revert_zombie_skags [DRY RUN]: would revert '%s' from '%s' "
                "(impr=%d calls=%.1f days_live=%d)",
                kw, ag, impr, calls, d_live
            )
            reverted += 1
            continue

        _conn2 = _get_conn(db_path)
        try:
            with _conn2:
                _conn2.execute("""
                    UPDATE skag_recommendations
                    SET status = 'reverted',
                        reverted_at = ?,
                        error = ?
                    WHERE recommendation_id = ?
                """, (
                    now_iso,
                    f"Zombie: {d_live}d live, {impr} impr, {calls:.1f} call_convs — below threshold",
                    rec_id,
                ))
            logger.info(
                "revert_zombie_skags: reverted '%s' from '%s' (impr=%d calls=%.1f days_live=%d rec=%s)",
                kw, ag, impr, calls, d_live, rec_id[:8]
            )
            reverted += 1
        except Exception as e:
            logger.error("revert_zombie_skags: DB update failed for rec=%s: %s", rec_id[:8], e)
        finally:
            _conn2.close()

    logger.info("revert_zombie_skags: reverted %d/%d zombie SKAG(s)", reverted, len(zombie_rows))
    return reverted


# ── CLI dump ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    # Accept either a campaign name substring or "all" to scan every campaign
    arg = sys.argv[1] if len(sys.argv) > 1 else "all"

    db = _db_path()
    conn = _get_conn(db)
    if arg.lower() == "all":
        campaign_names = [
            r[0] for r in conn.execute(
                "SELECT DISTINCT campaign_name FROM gads_keywords_cache ORDER BY campaign_name"
            ).fetchall()
        ]
    else:
        campaign_names = [
            r[0] for r in conn.execute(
                "SELECT DISTINCT campaign_name FROM gads_keywords_cache "
                "WHERE LOWER(campaign_name) LIKE LOWER(?)",
                (f"%{arg}%",)
            ).fetchall()
        ]
    conn.close()

    if not campaign_names:
        print(f"No campaigns found matching '{arg}'")
        sys.exit(0)

    all_candidates: list[SKAGCandidate] = []
    for cname in campaign_names:
        cands = collect_skag_signals(cname, db_path=db)
        all_candidates.extend(cands)

    print(f"\nSKAG signal scan — {len(campaign_names)} campaign(s)\n{'='*60}")

    if not all_candidates:
        print("  No candidates found. Possible reasons:")
        print(f"  1. No keywords with ≥{MIN_CLICKS_30D} clicks + ≥{MIN_IMPRESSIONS_30D} impressions")
        print(f"  2. Ad groups have <{MIN_AD_GROUP_SIZE} keywords (already lean)")
        print(f"  3. No call conversions or OD appointments for any ad group")
        print(f"  4. All qualifying keywords already have active SKAG recommendations")
    else:
        all_candidates.sort(key=lambda c: c.score, reverse=True)
        for c in all_candidates:
            print(f"\n  [{c.score:.3f}] \"{c.keyword_text}\" ({c.match_type})")
            print(f"    Campaign  : {c.campaign_name}")
            print(f"    Source AG : {c.source_ad_group_name} ({c.ad_group_size} kws)")
            print(f"    Clicks    : {c.clicks_30d}  Impr: {c.impressions_30d}  "
                  f"Cost: ${c.cost_30d:.2f}  IS: {c.impression_share:.0%}")
            print(f"    Call Convs: {c.call_conversions_30d:.1f}  "
                  f"OD Appts: {c.od_appointments}  "
                  f"ST in AG: {c.search_terms_in_ag}")
            print(f"    Score BD  : {json.dumps(c.score_breakdown)}")
            print(f"    SKAG Name : {c.suggested_ag_name}")

    print(f"\n{'='*60}")
    print(f"Total: {len(all_candidates)} candidates across {len(campaign_names)} campaign(s)")

    # Show prompt text for first campaign with candidates
    if all_candidates:
        first_camp = all_candidates[0].campaign_name
        print(f"\n── Prompt injection preview for '{first_camp}' ──")
        txt = get_skag_candidates_text(first_camp, db_path=db)
        print(txt if txt else "  (empty)")
