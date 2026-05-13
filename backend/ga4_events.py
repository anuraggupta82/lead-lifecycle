"""
GA4 Measurement Protocol — sends server-side events to Google Analytics.
Fires when leads hit key lifecycle stages.

This provides a unified funnel view in GA4:
  ad click → page view → form submit → appointment booked → treatment completed

Also enables remarketing audiences (e.g. "used smile tool but didn't book").

Setup:
  1. Get Measurement Protocol API secret from GA4 Admin → Data Streams → API Secrets
  2. Set GA4_API_SECRET and GA4_MEASUREMENT_ID in .env
"""

import logging
import json
import urllib.request
from typing import Optional
from config import get_settings

logger = logging.getLogger(__name__)

GA4_ENDPOINT = "https://www.google-analytics.com/mp/collect"


def send_ga4_event(
    client_id: str,
    event_name: str,
    params: Optional[dict] = None,
    ga4_client_id: Optional[str] = None,
) -> bool:
    """
    Send a server-side event to GA4 via Measurement Protocol.

    Args:
        client_id: Lead UUID (used as fallback; for proper session stitching
                   pass the browser's GA4 client_id via ga4_client_id param)
        event_name: GA4 event name (e.g. 'lead_qualified', 'appointment_booked')
        params: Optional event parameters
        ga4_client_id: Browser GA4 client_id from _ga cookie (e.g. "GA1.1.123.456").
                       When provided, events stitch to browser sessions in GA4 funnel.
                       When absent, events land in a separate pseudo-session.
    """
    # DB-E1 fix: use browser-side ga4_client_id if available so server events
    # stitch to the browser session in GA4 funnel attribution.
    effective_client_id = ga4_client_id if ga4_client_id else client_id
    settings = get_settings()

    if not settings.ga4_api_secret or not settings.ga4_measurement_id:
        logger.debug("GA4 Measurement Protocol not configured — skipping event")
        return False

    url = (
        f"{GA4_ENDPOINT}"
        f"?measurement_id={settings.ga4_measurement_id}"
        f"&api_secret={settings.ga4_api_secret}"
    )

    payload = {
        "client_id": effective_client_id,
        "events": [
            {
                "name": event_name,
                "params": params or {},
            }
        ],
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=10)
        status = resp.getcode()

        if status == 204:
            logger.debug(f"GA4 event sent: {event_name} for client {client_id}")
            return True
        else:
            logger.warning(f"GA4 returned status {status} for event {event_name}")
            return False

    except Exception as e:
        logger.warning(f"GA4 event failed ({event_name}): {e}")
        return False


# ── Convenience functions for each stage ─────────────────────────────────────

def track_lead_created(lead_id: str, source: str = "", gclid: str = "", ga4_client_id: str = ""):
    """Lead submitted contact info."""
    send_ga4_event(lead_id, "lead_qualified", {
        "source": source,
        "has_gclid": "yes" if gclid else "no",
        "value": 200,
        "currency": "USD",
    }, ga4_client_id=ga4_client_id or None)


def track_smile_completed(lead_id: str, ga4_client_id: str = ""):
    """Lead used the AI smile tool."""
    send_ga4_event(lead_id, "smile_completed", {
        "value": 250,
        "currency": "USD",
    }, ga4_client_id=ga4_client_id or None)


def track_appointment_booked(lead_id: str, booking_type: str = "implant_consult", ga4_client_id: str = ""):
    """Lead booked an appointment."""
    send_ga4_event(lead_id, "appointment_booked", {
        "booking_type": booking_type,
        "value": 500,
        "currency": "USD",
    }, ga4_client_id=ga4_client_id or None)


def track_treatment_presented(lead_id: str, plan_value: float = 0, ga4_client_id: str = ""):
    """Treatment plan entered in OpenDental."""
    send_ga4_event(lead_id, "treatment_presented", {
        "plan_value": plan_value,
        "value": plan_value or 15000,
        "currency": "USD",
    }, ga4_client_id=ga4_client_id or None)


def track_treatment_accepted(lead_id: str, plan_value: float = 0, ga4_client_id: str = ""):
    """Patient accepted treatment, procedures scheduled."""
    send_ga4_event(lead_id, "treatment_accepted", {
        "plan_value": plan_value,
        "value": plan_value or 15000,
        "currency": "USD",
    }, ga4_client_id=ga4_client_id or None)


def track_treatment_completed(lead_id: str, production: float = 0, ga4_client_id: str = ""):
    """Implant procedure completed — actual production.
    DB-E4 fix: send real production value only; no hardcoded fallback.
    When production is unknown, value=0 is more honest than a fictional $25,000.
    """
    send_ga4_event(lead_id, "treatment_completed", {
        "production": production,
        "value": production,  # DB-E4: was `production or 25000` — hardcoded fallback removed
        "currency": "USD",
    }, ga4_client_id=ga4_client_id or None)
