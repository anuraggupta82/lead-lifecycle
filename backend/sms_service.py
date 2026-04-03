"""
SMS service via Twilio — TCPA compliant.
Every message includes STOP opt-out instructions.
"""
import logging
from config import get_settings

logger = logging.getLogger(__name__)


def _send_sms(to_number: str, body: str) -> bool:
    settings = get_settings()
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

    try:
        from twilio.rest import Client
        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        message = client.messages.create(
            body=body,
            from_=settings.twilio_from_number,
            to=e164,
        )
        logger.info(f"SMS sent to {e164}: SID {message.sid}")
        return True
    except Exception as e:
        logger.error(f"SMS failed to {e164}: {e}")
        return False


def send_day3_sms(lead: dict) -> bool:
    settings = get_settings()
    name = lead.get("first_name") or "there"
    body = (
        f"Hi {name}! Your free All-on-X consultation at {settings.practice_name} is still available. "
        f"Book now: {settings.practice_url} or call {settings.office_phone}. "
        f"Reply STOP to opt out."
    )
    return _send_sms(lead.get("phone", ""), body)


def send_day21_sms(lead: dict) -> bool:
    settings = get_settings()
    name = lead.get("first_name") or "there"
    body = (
        f"Hi {name}, Dr. Gupta still has openings for free implant consultations this month. "
        f"Call {settings.office_phone} or visit {settings.practice_url}. "
        f"Reply STOP to opt out."
    )
    return _send_sms(lead.get("phone", ""), body)
