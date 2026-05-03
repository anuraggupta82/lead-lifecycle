"""
IMAP Inbox Poller — reads incoming emails from info@nxtsmile.com (Zoho Mail).

Polls for UNSEEN messages, matches them to leads by From/Reply-To address,
stores them in the conversations/messages tables, and marks them \Seen.

Also provides send_reply() for staff replies via SMTP (reuses email_service internals).

Run via:
  POST /api/admin/email-inbox/poll   — manual trigger (admin endpoint)
  APScheduler: every 5 minutes       — wired in main.py lifespan

Design notes:
  - One conversation per lead (v1); channel always "email"
  - Messages are deduplicated by RFC 5322 Message-ID
  - Empty/missing Message-ID → synthesize a UUID-based fallback so INSERT OR IGNORE works
  - Bounce/self-sent guard: skip if From == our own smtp_user, or Auto-Submitted header set
  - IMAP4_SSL uses timeout=30 to prevent hanging
"""

import email
import imaplib
import logging
import smtplib
import uuid
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parseaddr
from typing import Optional

from config import get_settings
from database import (
    get_lead,
    get_lead_by_email,
    get_or_create_conversation,
    append_message,
    get_conversation,
    get_messages,
)

logger = logging.getLogger(__name__)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _extract_text_body(msg: email.message.Message) -> str:
    """Return the best plain-text body we can extract from a MIME message."""
    if msg.is_multipart():
        # Prefer text/plain; fall back to text/html stripped of tags
        plain = ""
        html = ""
        for part in msg.walk():
            ct = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if "attachment" in disp:
                continue
            try:
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                charset = part.get_content_charset() or "utf-8"
                decoded = payload.decode(charset, errors="replace")
                if ct == "text/plain" and not plain:
                    plain = decoded
                elif ct == "text/html" and not html:
                    html = decoded
            except Exception:
                pass
        return plain or _strip_html(html)
    else:
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
        except Exception:
            pass
    return ""


def _strip_html(html: str) -> str:
    """Very lightweight HTML → plain text (no external deps)."""
    import re
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _strip_quoted_reply(text: str) -> str:
    """
    Remove quoted reply chains from email body — show only the new reply text.
    Strips everything from common markers:
      - Lines starting with ">" (quoted text)
      - "On <date>, <name> wrote:" (Gmail/Apple Mail style)
      - "-----Original Message-----"
    """
    import re
    lines = text.splitlines()
    result = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(">"):
            break
        if re.match(r"^On .{10,200}wrote:\s*$", stripped):
            break
        if stripped.startswith("-----Original Message-----"):
            break
        if stripped.startswith("________________________________"):
            break
        result.append(line)
    cleaned = "\n".join(result).rstrip()
    # Strip trailing email signature marker  (-- on its own line)
    cleaned = re.sub(r"\n--\s*\n.*$", "", cleaned, flags=re.DOTALL).rstrip()
    return cleaned


def _decode_header_value(value: str) -> str:
    """Decode MIME encoded-word headers (e.g. =?UTF-8?Q?...?=) to plain text."""
    from email.header import decode_header as _dh
    parts = _dh(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded)


def _is_bounce_or_auto(msg: email.message.Message, our_address: str) -> bool:
    """
    Return True if this message should be silently skipped:
      - Auto-reply / automated bounce (Auto-Submitted header present and not 'no')
      - Sent by our own inbox (loop guard)
    """
    # Check Auto-Submitted header (RFC 3834)
    auto_sub = msg.get("Auto-Submitted", "no").lower()
    if auto_sub and auto_sub != "no":
        return True

    # Check From address
    _, from_addr = parseaddr(msg.get("From", ""))
    if from_addr.lower() == our_address.lower():
        return True

    # Common bounce/notification senders
    from_lower = from_addr.lower()
    if from_lower.startswith("mailer-daemon@") or from_lower.startswith("postmaster@"):
        return True

    return False


# ─── Poll ────────────────────────────────────────────────────────────────────

def poll_once() -> dict:
    """
    Connect to IMAP, fetch all UNSEEN messages, process each one.
    Returns a summary dict: {fetched, matched, unmatched, skipped, errors}
    """
    settings = get_settings()

    fetched = 0
    matched = 0
    unmatched = 0
    skipped = 0
    errors = 0

    try:
        imap = imaplib.IMAP4_SSL("imappro.zoho.com", 993, timeout=30)
    except Exception as e:
        logger.error(f"IMAP connection failed: {e}")
        return {"fetched": 0, "matched": 0, "unmatched": 0, "skipped": 0, "errors": 1, "error": str(e)}

    try:
        imap.login(settings.smtp_user, settings.imap_password)
        imap.select("INBOX")

        # Search last 7 days (ALL, not just UNSEEN) so we catch messages
        # opened in Zoho webmail before the poller ran. Dedup by message_id in DB.
        from datetime import datetime, timedelta
        since_date = (datetime.utcnow() - timedelta(days=7)).strftime("%d-%b-%Y")
        status, data = imap.search(None, f'SINCE "{since_date}"')
        if status != "OK":
            logger.warning(f"IMAP SEARCH returned status: {status}")
            return {"fetched": 0, "matched": 0, "unmatched": 0, "skipped": 0, "errors": 0}

        msg_ids = data[0].split()
        logger.info(f"IMAP poll: {len(msg_ids)} messages in last 7 days")

        for msg_id in msg_ids:
            fetched += 1
            try:
                status, msg_data = imap.fetch(msg_id, "(RFC822)")
                if status != "OK" or not msg_data or not msg_data[0]:
                    errors += 1
                    continue

                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)

                # Skip bounces/auto-replies before doing anything else
                if _is_bounce_or_auto(msg, settings.smtp_user):
                    logger.debug(f"Skipping auto/bounce message {msg_id}")
                    skipped += 1
                    continue

                result = _process_message(msg)
                if result == "matched":
                    matched += 1
                elif result == "unmatched":
                    unmatched += 1
                elif result == "skipped":
                    skipped += 1
                else:
                    errors += 1

            except Exception as e:
                logger.error(f"Error processing message {msg_id}: {e}")
                errors += 1

    except imaplib.IMAP4.error as e:
        logger.error(f"IMAP error during poll: {e}")
        errors += 1
    finally:
        try:
            imap.logout()
        except Exception:
            pass

    result = {
        "fetched": fetched,
        "matched": matched,
        "unmatched": unmatched,
        "skipped": skipped,
        "errors": errors,
    }
    logger.info(f"IMAP poll complete: {result}")
    return result


def _process_message(msg: email.message.Message) -> str:
    """
    Parse a single MIME message, look up the lead by sender email,
    and store it. Returns: 'matched', 'unmatched', 'skipped', 'error'
    """
    # Extract From address (prefer Reply-To if set)
    reply_to_raw = msg.get("Reply-To", "")
    from_raw = msg.get("From", "")
    _, from_addr = parseaddr(reply_to_raw or from_raw)
    from_addr = from_addr.lower().strip()

    if not from_addr:
        logger.debug("Message has no From address — skipping")
        return "skipped"

    # Normalize Message-ID
    message_id = (msg.get("Message-ID") or "").strip()
    if not message_id:
        # Synthesize a stable fallback so INSERT OR IGNORE dedup still works
        # (two emails with empty Message-ID will each get a fresh UUID — rare edge case)
        message_id = f"<synthetic-{uuid.uuid4()}@nxtsmile.local>"

    subject = _decode_header_value(msg.get("Subject", "")).strip()
    body = _strip_quoted_reply(_extract_text_body(msg))
    received_at = datetime.now(timezone.utc).isoformat()

    # Parse date header if available
    date_header = msg.get("Date", "")
    if date_header:
        try:
            from email.utils import parsedate_to_datetime
            received_at = parsedate_to_datetime(date_header).astimezone(timezone.utc).isoformat()
        except Exception:
            pass  # fall back to now

    # In-reply-to / references for threading
    in_reply_to = (msg.get("In-Reply-To") or "").strip()
    msg_references = (msg.get("References") or "").strip()

    # Look up lead by sender email
    lead = get_lead_by_email(from_addr)
    if not lead:
        logger.info(f"No lead found for {from_addr} — storing as unmatched inbox message")
        # Store without lead_id so staff can still see it
        lead_id = None
        unmatched = True
    else:
        lead_id = lead["id"]
        unmatched = False

    # Get or create conversation for this lead
    conv = get_or_create_conversation(lead_id=lead_id, channel="email", contact_email=from_addr)
    conv_id = conv["id"]

    # Append message (INSERT OR IGNORE on message_id — safe dedup)
    inserted = append_message(
        conversation_id=conv_id,
        direction="inbound",
        from_addr=from_addr,
        subject=subject,
        body=body,
        message_id=message_id,
        in_reply_to=in_reply_to,
        msg_references=msg_references,
        received_at=received_at,
    )

    if not inserted:
        logger.debug(f"Duplicate message {message_id} — skipped")
        return "skipped"

    logger.info(
        f"Stored inbound email from {from_addr} "
        f"(lead={lead_id or 'unmatched'}, conv={conv_id}, subject='{subject[:60]}')"
    )
    return "unmatched" if unmatched else "matched"


# ─── Reply ────────────────────────────────────────────────────────────────────

def send_reply(lead_id: str, body: str, subject: Optional[str] = None) -> dict:
    """
    Send a staff reply to the lead's email address via SMTP.
    Stores the outbound message in the conversation.

    Returns: {"sent": True, "message_id": "...", "to": "..."}
    Raises: ValueError if lead not found or has no email.
    """
    lead = get_lead(lead_id)
    if not lead:
        raise ValueError(f"Lead {lead_id} not found")

    to_email = (lead.get("email") or "").strip()
    if not to_email:
        raise ValueError(f"Lead {lead_id} has no email address")

    settings = get_settings()

    # Get or create conversation once (used for both subject lookup and threading)
    conv = get_or_create_conversation(lead_id=lead_id, channel="email", contact_email=to_email)
    msgs = get_messages(conv["id"], limit=20)
    inbound = [m for m in msgs if m["direction"] == "inbound" and m.get("message_id")]

    # Build reply subject
    if not subject:
        inbound_all = [m for m in msgs if m["direction"] == "inbound"]
        if inbound_all:
            last_subj = inbound_all[-1].get("subject", "")
            if last_subj.lower().startswith("re:"):
                subject = last_subj
            elif last_subj:
                subject = f"Re: {last_subj}"
            else:
                subject = "Your inquiry"
        else:
            subject = "Following up on your inquiry"

    # Build MIME message
    mime = MIMEMultipart("mixed")
    mime["From"] = settings.smtp_user
    mime["To"] = to_email
    mime["Subject"] = subject

    out_message_id = f"<{uuid.uuid4()}@nxtsmile.com>"
    mime["Message-ID"] = out_message_id

    # Threading headers: reference last inbound message
    if inbound:
        last_msg_id = inbound[-1]["message_id"]
        mime["In-Reply-To"] = last_msg_id
        mime["References"] = last_msg_id

    # UTF-8 to handle non-ASCII reply text correctly
    mime.attach(MIMEText(body, "plain", "utf-8"))

    # In dev mode: redirect to test address
    actual_recipient = _resolve_recipient(to_email, settings)

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.sendmail(settings.smtp_user, actual_recipient, mime.as_string())

        logger.info(f"Reply sent to {actual_recipient} (lead={lead_id})")

        # Store outbound message
        sent_at = datetime.now(timezone.utc).isoformat()
        append_message(
            conversation_id=conv["id"],
            direction="outbound",
            from_addr=settings.smtp_user,
            subject=subject,
            body=body,
            message_id=out_message_id,
            in_reply_to=inbound[-1]["message_id"] if inbound else "",
            msg_references="",
            received_at=sent_at,
        )

        return {"sent": True, "message_id": out_message_id, "to": actual_recipient}

    except Exception as e:
        logger.error(f"SMTP reply failed to {actual_recipient}: {e}")
        raise


def _resolve_recipient(to_email: str, settings) -> str:
    """Mirror email_service.py dev-mode redirect logic."""
    env = getattr(settings, "env", "prod")
    if env == "dev":
        redirect = getattr(settings, "test_redirect_email", None)
        if redirect:
            logger.debug(f"Dev mode: redirecting {to_email} → {redirect}")
            return redirect
    return to_email
