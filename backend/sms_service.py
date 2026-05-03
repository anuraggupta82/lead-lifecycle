"""
SMS service via Twilio — TCPA compliant.
Every message includes STOP opt-out instructions.
"""
import logging
from config import get_settings

logger = logging.getLogger(__name__)


def _send_sms(to_number: str, body: str) -> bool:
    settings = get_settings()

    # Kill switch — independent of env
    if getattr(settings, "sms_disabled", False):
        logger.info(f"SMS DISABLED — skipped sending to {to_number}")
        return False

    if not all([settings.twilio_account_sid, settings.twilio_auth_token, settings.twilio_from_number]):
        logger.warning("Twilio not configured — SMS not sent")
        return False

    # Normalize phone number to E.164
    digits = "".join(c for c in to_number if c.isdigit())
    if len(digits) == 10:
        digits = "1" + digits
    if not digits.startswith("1") or len(digits) != 11:
        logger.warning(f"Invalid phone for SMS: {to_number}")
        return False
    e164 = f"+{digits}"

    # Dev-mode redirect — reroute to test phone, prefix body with original target
    original_e164 = e164
    is_dev = getattr(settings, "env", "dev").lower() == "dev"
    use_whatsapp = is_dev and bool(getattr(settings, "whatsapp_sandbox_number", ""))

    if is_dev:
        redirect = getattr(settings, "test_redirect_phone", "") or e164
        if redirect and redirect != e164:
            body = f"[DEV → {original_e164}] {body}"
            e164 = redirect

    try:
        from twilio.rest import Client
        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)

        if use_whatsapp:
            # Send via WhatsApp sandbox — bypasses A2P 10DLC requirement
            sandbox_from = f"whatsapp:{settings.whatsapp_sandbox_number}"
            sandbox_to = f"whatsapp:{e164}"
            message = client.messages.create(
                body=body,
                from_=sandbox_from,
                to=sandbox_to,
            )
            logger.info(f"WhatsApp sandbox sent to {sandbox_to} (orig={original_e164}): SID {message.sid}")
        else:
            message = client.messages.create(
                body=body,
                from_=settings.twilio_from_number,
                to=e164,
            )
            logger.info(f"SMS sent to {e164} (orig={original_e164}): SID {message.sid}")
        return True
    except Exception as e:
        logger.error(f"SMS failed to {e164} (orig={original_e164}): {e}")
        return False


def send_manual_sms(to_phone: str, body: str) -> bool:
    """Send a freeform manual SMS (no template). Respects kill switch + dev redirect."""
    return _send_sms(to_phone, body)


def send_day3_sms(lead: dict) -> bool:
    """Day 3 — friendly nudge referencing smile preview."""
    settings = get_settings()
    name = lead.get("first_name") or "there"
    # TODO: Update booking link once short URL / SMS tracking link is set up
    booking_link = settings.booking_url
    body = (
        f"Hi {name}, it's nXtsmile at Grafton Dental Care \U0001f60a "
        f"Did you get a chance to look at your smile preview? "
        f"We'd love to help make it a reality. "
        f"Book your free consultation: {booking_link} "
        f"or call us at {settings.office_phone}. "
        f"- Dr. Gupta's Team\n"
        f"Reply STOP to opt out."
    )
    return _send_sms(lead.get("phone", ""), body)


def send_no_show_sms(lead: dict) -> bool:
    """No-show follow-up — encourage rebooking after missed appointment."""
    settings = get_settings()
    name = lead.get("first_name") or "there"
    booking_link = settings.booking_url
    body = (
        f"Hi {name}, we missed you at your appointment at Grafton Dental Care. "
        f"We know things come up! We'd love to get you rescheduled. "
        f"A small deposit holds your spot and shows you're committed to your new smile. "
        f"Book again: {booking_link} or call {settings.office_phone}. "
        f"- Dr. Gupta's Team\n"
        f"Reply STOP to opt out."
    )
    return _send_sms(lead.get("phone", ""), body)

def send_day21_sms(lead: dict) -> bool:
    """Day 21 — warm re-engagement, pressure-free."""
    settings = get_settings()
    name = lead.get("first_name") or "there"
    # TODO: Update booking link once short URL / SMS tracking link is set up
    booking_link = settings.booking_url
    body = (
        f"Hi {name}, just checking in from nXtsmile \U0001f60a "
        f"Life gets busy, but your dream smile is still waiting. "
        f"Your free consultation with Dr. Gupta is just a call away — "
        f"{settings.office_phone} or book online: {booking_link}. "
        f"We're here whenever you're ready!\n"
        f"Reply STOP to opt out."
    )
    return _send_sms(lead.get("phone", ""), body)
