"""
call_keyword_attribution.py — Attribute inbound Mango calls to Google Ads keywords.

Attribution methods (in priority order):
  A — lead_gclid (0.95):
        Call has lead_id → lead has gclid + keyword_text. Direct, clean.
  B — lead_phone_recent_click (0.80):
        Call has lead_id → lead has no gclid. Find most-clicked keyword in the
        same campaign on the same date via gads_clicks.
  C — time_window_gclid (0.70):
        Call has gads_call_id → gads_call_view has campaign_id. Find the
        dominant keyword in gads_clicks for that campaign on the call's date.
        If multiple keywords tie, take the most-clicked one; confidence drops to 0.55.
  D — campaign_only (0.30):
        Fallback: attribute to campaign only (attributed_keyword stays '').

Run:
  - After every mango_service reconcile_attribution pass (daily sync + on-demand).
  - Also callable as a backfill: attribute_calls_to_keywords(days=90).

Returns count of newly attributed calls.
"""

import logging
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

_ET = ZoneInfo("America/New_York")

logger = logging.getLogger(__name__)


def _call_date_et(started_at: str) -> str | None:
    """
    Convert a UTC ISO timestamp to YYYY-MM-DD in Eastern Time.
    Google Ads segments.date is in account timezone (Eastern), so we must
    convert started_at (UTC) to ET before matching against gads_clicks.click_date.
    """
    if not started_at:
        return None
    try:
        # Handle both '2026-05-07T02:15:00+00:00' and '2026-05-07T02:15:00'
        s = started_at.replace("Z", "+00:00")
        if "+" not in s and "T" in s:
            s += "+00:00"
        dt_utc = datetime.fromisoformat(s)
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=timezone.utc)
        return dt_utc.astimezone(_ET).strftime("%Y-%m-%d")
    except Exception:
        # Fallback: plain date slice (UTC — better than nothing)
        return started_at[:10]


def attribute_calls_to_keywords(days: int = 30) -> dict:
    """
    Walk unattributed inbound Mango calls and assign keyword attribution.

    Only processes calls where attributed_keyword_method is '' (not yet attempted).
    Skips calls already attributed (any method, including campaign_only).

    Returns summary dict: {method_a, method_b, method_c, method_d, skipped, errors}
    """
    from database import _conn

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    counts = {"method_a": 0, "method_a_prime": 0, "method_b": 0, "method_c": 0,
              "method_d": 0, "skipped": 0, "errors": 0}

    try:
        with _conn() as conn:
            # Fetch unattributed inbound calls in the window.
            # CallRail bridge: correlated subquery picks the best callrail_calls row
            # (prefers rows with a non-empty keyword, then newest) to avoid duplicates
            # when multiple CallRail events (call.created + call.completed) link to
            # the same Mango call.
            rows = conn.execute("""
                SELECT
                  mc.uuid, mc.started_at, mc.lead_id, mc.gads_call_id,
                  l.gclid, l.keyword_text AS lead_keyword, l.search_term_type AS lead_match_type,
                  l.ad_group_name AS lead_ad_group, l.campaign_name AS lead_campaign_name,
                  l.campaign_id AS lead_campaign_id,
                  gcv.campaign_id AS gcv_campaign_id, gcv.campaign_name AS gcv_campaign_name,
                  -- PR B: CallRail DNI bridge (exact search query captured at landing time)
                  cr.keyword  AS cr_keyword,
                  cr.source   AS cr_source
                FROM mango_calls mc
                LEFT JOIN leads l            ON l.id        = mc.lead_id
                LEFT JOIN gads_call_view gcv ON gcv.call_id = mc.gads_call_id
                LEFT JOIN callrail_calls cr  ON cr.id = (
                    SELECT cc.id FROM callrail_calls cc
                    WHERE cc.mango_call_id = mc.uuid
                    ORDER BY (CASE WHEN COALESCE(cc.keyword,'') != '' THEN 0 ELSE 1 END),
                             cc.id DESC
                    LIMIT 1
                )
                WHERE mc.started_at >= ?
                  AND mc.direction = 'inbound'
                  AND (mc.attributed_keyword_method IS NULL OR mc.attributed_keyword_method = '')
            """, (cutoff,)).fetchall()

            for row in rows:
                try:
                    result = _attribute_one_call(conn, dict(row))
                    if result:
                        counts[result] += 1
                    else:
                        counts["skipped"] += 1
                except Exception as e:
                    logger.warning(f"Attribution error for call {row['uuid']}: {e}")
                    counts["errors"] += 1

    except Exception as e:
        logger.error(f"attribute_calls_to_keywords failed: {e}")
        counts["errors"] += 1

    total = sum(v for k, v in counts.items() if k != "errors")
    logger.info(
        f"Call attribution complete: "
        f"A={counts['method_a']} A'={counts['method_a_prime']} "
        f"B={counts['method_b']} C={counts['method_c']} "
        f"D={counts['method_d']} skip={counts['skipped']} err={counts['errors']} / {total} processed"
    )
    return counts


def _attribute_one_call(conn, row: dict) -> str:
    """
    Try attribution methods A → B → C → D for one call row.
    Updates mango_calls in place. Returns method key string or None if skipped.
    """
    uuid = row["uuid"]
    started_at = row["started_at"]
    # Use Eastern date so it matches gads_clicks.click_date (Google Ads account timezone = ET)
    call_date = _call_date_et(started_at)

    # ── Method A: lead has gclid + keyword ───────────────────────────────────
    if row.get("lead_id") and row.get("gclid") and row.get("lead_keyword"):
        _write_attribution(conn, uuid,
                           keyword=row["lead_keyword"],
                           match_type=row.get("lead_match_type", ""),
                           ad_group=row.get("lead_ad_group", ""),
                           method="lead_gclid",
                           confidence=0.95)
        return "method_a"

    # ── Method A-prime: CallRail DNI captured the exact search query ─────────
    # Priority: between A (gclid-confirmed) and B (campaign+date inference).
    # CallRail's keyword is the literal search query that landed the visitor, which
    # is more accurate than inferring from gads_clicks but lacks a gclid-level link.
    # Only fires for google_ads-sourced calls with a non-empty keyword.
    if (row.get("cr_source") == "google_ads"
            and (row.get("cr_keyword") or "").strip()):
        _write_attribution(conn, uuid,
                           keyword=row["cr_keyword"].strip(),
                           match_type="",      # CallRail does not report match_type
                           ad_group="",        # CallRail does not report ad_group
                           method="callrail_dni",
                           confidence=0.85)
        return "method_a_prime"

    # ── Method B: lead exists but no gclid — find dominant keyword by campaign+date ──
    # Many leads only have campaign_name (not campaign_id). Try both.
    if row.get("lead_id") and call_date:
        lead_camp_id = row.get("lead_campaign_id") or ""
        lead_camp_name = row.get("lead_campaign_name") or ""
        kw = _best_keyword_for_campaign_date(
            conn, lead_camp_id, lead_camp_name, call_date
        )
        if kw:
            _write_attribution(conn, uuid,
                               keyword=kw["keyword_text"],
                               match_type=kw.get("match_type", ""),
                               ad_group=kw.get("ad_group_name", ""),
                               method="lead_campaign_date",
                               confidence=0.80)
            return "method_b"

    # ── Method C: gads_call_view gives campaign → match by campaign + date ───
    gcv_campaign_id = row.get("gcv_campaign_id")
    if gcv_campaign_id and call_date:
        kw, confidence = _best_keyword_for_campaign_date_with_conf(
            conn, gcv_campaign_id, call_date
        )
        if kw:
            _write_attribution(conn, uuid,
                               keyword=kw["keyword_text"],
                               match_type=kw.get("match_type", ""),
                               ad_group=kw.get("ad_group_name", ""),
                               method="time_window_gclid",
                               confidence=confidence)
            return "method_c"

    # ── Method D: campaign-only fallback ─────────────────────────────────────
    # Mark as attempted so we don't re-process. keyword stays ''.
    campaign_name = (row.get("gcv_campaign_name") or
                     row.get("lead_campaign_name") or "")
    if campaign_name or row.get("lead_id") or row.get("gads_call_id"):
        _write_attribution(conn, uuid,
                           keyword="",
                           match_type="",
                           ad_group="",
                           method="campaign_only",
                           confidence=0.30)
        return "method_d"

    # No signal at all — mark with terminal method so we don't re-scan every run
    _write_attribution(conn, uuid,
                       keyword="",
                       match_type="",
                       ad_group="",
                       method="no_signal",
                       confidence=0.0)
    return "method_d"  # count toward method_d (campaign-only bucket)


def _best_keyword_for_campaign_date(
    conn, campaign_id: str, campaign_name: str, date: str
) -> dict | None:
    """
    Find the keyword with the most clicks in a campaign on a given date.
    Tries campaign_id first; falls back to campaign_name match.
    Groups by keyword_text only (to avoid fragmentation by empty match_type rows).
    Returns dict with keyword_text, match_type, ad_group_name or None.
    """
    # Build WHERE clause — prefer numeric campaign_id, fall back to name
    if campaign_id:
        rows = conn.execute("""
            SELECT keyword_text,
                   MAX(match_type)   AS match_type,
                   MAX(ad_group_name) AS ad_group_name,
                   COUNT(*)           AS click_count
            FROM gads_clicks
            WHERE campaign_id = ? AND click_date = ? AND keyword_text != ''
            GROUP BY keyword_text
            ORDER BY click_count DESC
            LIMIT 1
        """, (campaign_id, date)).fetchall()
        if rows:
            return dict(rows[0])

    # Fallback: match by campaign_name (case-insensitive)
    if campaign_name:
        rows = conn.execute("""
            SELECT keyword_text,
                   MAX(match_type)   AS match_type,
                   MAX(ad_group_name) AS ad_group_name,
                   COUNT(*)           AS click_count
            FROM gads_clicks
            WHERE LOWER(TRIM(campaign_name)) = LOWER(TRIM(?))
              AND click_date = ? AND keyword_text != ''
            GROUP BY keyword_text
            ORDER BY click_count DESC
            LIMIT 1
        """, (campaign_name, date)).fetchall()
        if rows:
            return dict(rows[0])

    return None


def _best_keyword_for_campaign_date_with_conf(
    conn, campaign_id: str, date: str
) -> tuple[dict | None, float]:
    """
    Like _best_keyword_for_campaign_date but returns (kw, confidence).
    Confidence = 0.70 if one keyword dominates (>70% of clicks),
                 0.55 if multiple keywords compete.
    Groups by keyword_text only to avoid fragmentation.
    """
    rows = conn.execute("""
        SELECT keyword_text,
               MAX(match_type)    AS match_type,
               MAX(ad_group_name) AS ad_group_name,
               COUNT(*)           AS click_count
        FROM gads_clicks
        WHERE campaign_id = ? AND click_date = ? AND keyword_text != ''
        GROUP BY keyword_text
        ORDER BY click_count DESC
    """, (campaign_id, date)).fetchall()

    if not rows:
        return None, 0.0

    total = sum(r["click_count"] for r in rows)
    top = dict(rows[0])
    # If total somehow 0 (shouldn't happen), default to low confidence
    confidence = 0.55 if total == 0 else (0.70 if top["click_count"] / total >= 0.70 else 0.55)
    return top, confidence


def _write_attribution(conn, uuid: str, keyword: str, match_type: str,
                        ad_group: str, method: str, confidence: float) -> None:
    """Update mango_calls attribution columns for one call."""
    conn.execute("""
        UPDATE mango_calls
           SET attributed_keyword            = ?,
               attributed_match_type         = ?,
               attributed_ad_group           = ?,
               attributed_keyword_method     = ?,
               attributed_keyword_confidence = ?,
               updated_at                    = ?
         WHERE uuid = ?
    """, (keyword, match_type, ad_group, method, confidence,
          datetime.now(timezone.utc).isoformat(), uuid))


def get_attribution_diagnostics(days: int = 30) -> dict:
    """
    Return a diagnostic summary of call attribution quality for the admin endpoint.
    Includes method breakdown + top 10 unattributed calls.
    """
    from database import _conn

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    out = {
        "days": days,
        "method_breakdown": {},
        "total_attributed_calls": 0,
        "total_inbound_calls": 0,
        "top_unattributed": [],
    }

    try:
        with _conn() as conn:
            # Total inbound in window
            total = conn.execute(
                "SELECT COUNT(*) FROM mango_calls WHERE started_at >= ? AND direction='inbound'",
                (cutoff,)
            ).fetchone()[0]
            out["total_inbound_calls"] = total

            # Method breakdown
            method_rows = conn.execute("""
                SELECT
                  COALESCE(NULLIF(attributed_keyword_method,''), 'unattributed') AS method,
                  COUNT(*) AS cnt
                FROM mango_calls
                WHERE started_at >= ? AND direction='inbound'
                GROUP BY method
                ORDER BY cnt DESC
            """, (cutoff,)).fetchall()
            breakdown = {r["method"]: r["cnt"] for r in method_rows}
            out["method_breakdown"] = breakdown
            # Exclude non-keyword methods from "keyword-attributed" count
            # campaign_only = matched campaign but not keyword
            # no_signal = no GAds signal at all (terminal state to prevent re-scanning)
            # callrail_campaign_only = ATTR-FIX 2026-07-06: CallRail-confirmed call-extension
            #   (tap-to-call) call with a campaign but no captured search keyword
            NON_KEYWORD = {"unattributed", "campaign_only", "no_signal", "callrail_campaign_only"}
            keyword_attributed = sum(
                v for k, v in breakdown.items()
                if k not in NON_KEYWORD
            )
            out["total_keyword_attributed_calls"] = keyword_attributed
            out["total_method_assigned_calls"] = total - breakdown.get("unattributed", 0)

            # Top 10 unattributed calls
            unattr = conn.execute("""
                SELECT mc.uuid, mc.started_at, mc.from_number, mc.duration_sec,
                       mc.lead_id, mc.gads_call_id,
                       COALESCE(l.campaign_name, '') AS campaign_name
                FROM mango_calls mc
                LEFT JOIN leads l ON l.id = mc.lead_id
                WHERE mc.started_at >= ? AND mc.direction='inbound'
                  AND (mc.attributed_keyword_method IS NULL OR mc.attributed_keyword_method = '')
                ORDER BY mc.started_at DESC
                LIMIT 10
            """, (cutoff,)).fetchall()
            out["top_unattributed"] = [dict(r) for r in unattr]

    except Exception as e:
        logger.error(f"Attribution diagnostics failed: {e}")
        out["error"] = str(e)

    return out
