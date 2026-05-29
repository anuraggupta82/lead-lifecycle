"""
Microsoft Clarity Data Export API — nightly sync for both GDC properties.

Pulls daily metrics (scroll depth, rage clicks, dead clicks, engagement time,
traffic, excessive scroll, quickback clicks, script errors) from the Clarity
live-insights API and writes them to the clarity_daily_metrics table.

API limits:
  - 10 requests per project per day
  - Max 3-day lookback (numOfDays: 1, 2, or 3)
  - 1,000-row cap, no pagination
  - Aggregate data only (no individual sessions)

Nightly job: pulls numOfDays=1 (yesterday's data) for both properties.
This is 2 calls/day total, leaving 8 in reserve per project for MCP queries.

Schema written to pipeline.db:
  clarity_daily_metrics (id, date, property, metric_name, device, page,
                         value_float, value_int, sessions_count, synced_at)
"""

import logging
import requests
import sqlite3
from datetime import datetime, date, timezone, timedelta
from typing import Optional
from config import get_settings

logger = logging.getLogger(__name__)

CLARITY_API_URL = "https://www.clarity.ms/export-data/api/v1/project-live-insights"

PROPERTIES = {
    "graftondentalcare.com": "CLARITY_TOKEN_GDC",
    "nxtsmile.com": "CLARITY_TOKEN_NXTSMILE",
}


def _get_db():
    settings = get_settings()
    return sqlite3.connect(settings.db_path)


def _ensure_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS clarity_daily_metrics (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT NOT NULL,
            property    TEXT NOT NULL,
            metric_name TEXT NOT NULL,
            device      TEXT,
            page        TEXT,
            value_float REAL,
            value_int   INTEGER,
            sessions_count INTEGER,
            raw_json    TEXT,
            synced_at   TEXT NOT NULL,
            UNIQUE(date, property, metric_name, device, page)
        )
    """)
    conn.commit()


def _get_token(property_name: str) -> Optional[str]:
    settings = get_settings()
    if property_name == "graftondentalcare.com":
        return settings.clarity_token_gdc or None
    elif property_name == "nxtsmile.com":
        return settings.clarity_token_nxtsmile or None
    return None


def _fetch_clarity(token: str, num_days: int = 1) -> Optional[list]:
    """Call the Clarity live-insights API. Returns parsed JSON or None on error."""
    try:
        r = requests.get(
            CLARITY_API_URL,
            params={
                "numOfDays": num_days,
                "dimension1": "Device",
                "dimension2": "popularPages",
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        if r.status_code == 200:
            return r.json()
        elif r.status_code == 429:
            logger.warning("Clarity API rate limit hit (10 req/day)")
            return None
        else:
            logger.error(f"Clarity API error {r.status_code}: {r.text[:200]}")
            return None
    except Exception as e:
        logger.error(f"Clarity API request failed: {e}")
        return None


def _parse_and_write(conn: sqlite3.Connection, property_name: str, data: list, target_date: str):
    """Parse Clarity API response and upsert into clarity_daily_metrics."""
    import json
    now = datetime.now(timezone.utc).isoformat()
    rows_written = 0

    for metric in data:
        metric_name = metric.get("metricName", "Unknown")
        for info in metric.get("information", []):
            device = info.get("Device")
            page = info.get("popularPages") or info.get("Page")

            # Extract primary value depending on metric type
            value_float = None
            value_int = None
            sessions_count = None

            if metric_name == "ScrollDepth":
                value_float = info.get("averageScrollDepth")
            elif metric_name == "EngagementTime":
                value_float = float(info.get("activeTime", 0))
                value_int = int(info.get("totalTime", 0))
            elif metric_name == "Traffic":
                value_int = int(info.get("totalSessionCount", 0))
                sessions_count = int(info.get("distinctUserCount", 0))
            else:
                # RageClick, DeadClick, ExcessiveScroll, QuickbackClick, ScriptError, ErrorClick
                value_float = info.get("sessionsWithMetricPercentage")
                sessions_count = int(info.get("sessionsCount", 0))
                value_int = int(info.get("subTotal", 0))

            try:
                conn.execute("""
                    INSERT INTO clarity_daily_metrics
                        (date, property, metric_name, device, page,
                         value_float, value_int, sessions_count, raw_json, synced_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(date, property, metric_name, device, page)
                    DO UPDATE SET
                        value_float=excluded.value_float,
                        value_int=excluded.value_int,
                        sessions_count=excluded.sessions_count,
                        raw_json=excluded.raw_json,
                        synced_at=excluded.synced_at
                """, (
                    target_date, property_name, metric_name, device, page,
                    value_float, value_int, sessions_count,
                    json.dumps(info), now
                ))
                rows_written += 1
            except Exception as e:
                logger.warning(f"Clarity write error for {metric_name}/{device}: {e}")

    conn.commit()
    return rows_written


def sync_property(property_name: str, num_days: int = 1) -> dict:
    """Sync one property. Returns status dict."""
    token = _get_token(property_name)
    if not token:
        return {"property": property_name, "status": "skipped", "reason": "no token configured"}

    data = _fetch_clarity(token, num_days)
    if data is None:
        return {"property": property_name, "status": "error", "reason": "API call failed"}

    # Target date = yesterday (the last full day)
    target_date = (date.today() - timedelta(days=1)).isoformat()

    conn = _get_db()
    _ensure_table(conn)
    rows = _parse_and_write(conn, property_name, data, target_date)
    conn.close()

    logger.info(f"Clarity sync: {property_name} → {rows} rows for {target_date}")
    return {"property": property_name, "status": "ok", "date": target_date, "rows": rows}


def run_nightly_sync() -> dict:
    """Run nightly sync for all configured properties. Called by APScheduler."""
    results = {}
    for prop in PROPERTIES:
        results[prop] = sync_property(prop, num_days=1)
    return results


def get_clarity_summary(property_name: str = None, days: int = 7) -> list:
    """
    Query clarity_daily_metrics for a summary of recent metrics.
    Used by the MCP tool and optimizer signal injection.
    Returns list of dicts with date, property, metric, device, value.
    """
    conn = _get_db()
    _ensure_table(conn)
    conn.row_factory = sqlite3.Row

    where_clauses = ["date >= date('now', ?)", "metric_name != 'Traffic'"]
    params = [f"-{days} days"]

    if property_name:
        where_clauses.append("property = ?")
        params.append(property_name)

    rows = conn.execute(f"""
        SELECT date, property, metric_name, device, page,
               value_float, value_int, sessions_count
        FROM clarity_daily_metrics
        WHERE {' AND '.join(where_clauses)}
        ORDER BY date DESC, property, metric_name, device
    """, params).fetchall()

    conn.close()
    return [dict(r) for r in rows]


def get_clarity_traffic(property_name: str = None, days: int = 7) -> list:
    """Get traffic metrics (session counts) by property and device."""
    conn = _get_db()
    _ensure_table(conn)
    conn.row_factory = sqlite3.Row

    where_clauses = ["date >= date('now', ?)", "metric_name = 'Traffic'"]
    params = [f"-{days} days"]

    if property_name:
        where_clauses.append("property = ?")
        params.append(property_name)

    rows = conn.execute(f"""
        SELECT date, property, device,
               value_int as sessions, sessions_count as users
        FROM clarity_daily_metrics
        WHERE {' AND '.join(where_clauses)}
        ORDER BY date DESC, property, device
    """, params).fetchall()

    conn.close()
    return [dict(r) for r in rows]
