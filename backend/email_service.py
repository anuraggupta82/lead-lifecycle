"""
Email service — sends follow-up emails via Gmail SMTP.
All templates are HTML with plain-text fallback.
"""
import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from config import get_settings

logger = logging.getLogger(__name__)


def _send(to_email: str, subject: str, html: str, plain: str = "") -> bool:
    settings = get_settings()
    if not settings.smtp_password:
        logger.warning("SMTP password not set — email not sent")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = settings.email_from
        msg["To"] = to_email
        msg.attach(MIMEText(plain or "Please view this in an HTML email client.", "plain"))
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
            server.ehlo()
            server.starttls()
            server.login(settings.smtp_user, settings.smtp_password)
            server.sendmail(settings.smtp_user, [to_email], msg.as_string())
        logger.info(f"Email sent to {to_email}: {subject}")
        return True
    except Exception as e:
        logger.error(f"Email failed to {to_email}: {e}")
        return False


def _base_html(content: str, unsubscribe_url: str, settings) -> str:
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
           background: #f5f5f5; margin: 0; padding: 20px; }}
    .card {{ background: #fff; border-radius: 12px; max-width: 560px;
             margin: 0 auto; padding: 36px; box-shadow: 0 2px 8px rgba(0,0,0,.08); }}
    .logo {{ color: #0d7a7f; font-size: 22px; font-weight: 700; margin-bottom: 24px; }}
    h2 {{ color: #111; margin: 0 0 16px; }}
    p {{ color: #444; line-height: 1.6; margin: 0 0 16px; }}
    .btn {{ display: inline-block; background: #0d7a7f; color: #fff !important;
            text-decoration: none; padding: 14px 28px; border-radius: 8px;
            font-weight: 600; margin: 8px 0 24px; }}
    .phone {{ font-size: 18px; font-weight: 700; color: #0d7a7f; }}
    .footer {{ font-size: 12px; color: #999; margin-top: 24px; border-top: 1px solid #eee;
               padding-top: 16px; }}
    a {{ color: #0d7a7f; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="logo">✨ {settings.practice_name}</div>
    {content}
    <div class="footer">
      You received this because you expressed interest in dental implants at {settings.practice_name}.
      &nbsp;|&nbsp; <a href="{unsubscribe_url}">Unsubscribe</a>
      &nbsp;|&nbsp; {settings.practice_name}, Grafton, MA
    </div>
  </div>
</body>
</html>"""


def send_day1_email(lead: dict, unsubscribe_url: str) -> bool:
    settings = get_settings()
    name = lead.get("first_name") or "there"
    html_content = f"""
    <h2>How did your smile preview look, {name}?</h2>
    <p>We saw you took a look at what your new smile could look like — we hope it was exciting!</p>
    <p>A lot of our patients say that the moment they saw their preview, everything clicked. If yours had that effect, we'd love to meet you.</p>
    <p>Your <strong>free consultation</strong> with Dr. Gupta is still available — no pressure, no obligation. Just a chance to see if nXtsmile is right for you.</p>
    <a href="{settings.practice_url}#consult" class="btn">Book My Free Consultation →</a>
    <p>Or call us directly at <span class="phone">{settings.office_phone}</span></p>
    <p>— The nXtsmile Team at {settings.practice_name}</p>
    """
    return _send(
        lead["email"],
        f"How did your smile preview look, {name}?",
        _base_html(html_content, unsubscribe_url, settings),
        f"Hi {name}, how did your smile preview look? Book your free consult at {settings.practice_url} or call {settings.office_phone}"
    )


def send_day7_email(lead: dict, unsubscribe_url: str) -> bool:
    settings = get_settings()
    name = lead.get("first_name") or "there"
    html_content = f"""
    <h2>What's holding you back, {name}?</h2>
    <p>We've had a lot of people tell us the same things before they finally came in:</p>
    <p>
      <strong>"I'm worried about the cost."</strong><br>
      We work with CareCredit, Cherry, and in-house financing — many patients pay less than $100/month.
    </p>
    <p>
      <strong>"I'm not sure I'm a candidate."</strong><br>
      That's exactly what the free consultation is for. There's no commitment — just answers.
    </p>
    <p>
      <strong>"I'm nervous."</strong><br>
      Dr. Gupta has helped hundreds of patients just like you. The consultation is relaxed and pressure-free.
    </p>
    <a href="{settings.practice_url}#consult" class="btn">Get My Questions Answered →</a>
    <p>Or call us at <span class="phone">{settings.office_phone}</span> — we're happy to talk before you book.</p>
    <p>— Dr. Gupta & the nXtsmile Team</p>
    """
    return _send(
        lead["email"],
        "A few things we hear a lot...",
        _base_html(html_content, unsubscribe_url, settings),
        f"Hi {name}, wondering what's holding you back. Call us at {settings.office_phone} or visit {settings.practice_url}"
    )


def send_day14_email(lead: dict, unsubscribe_url: str) -> bool:
    settings = get_settings()
    name = lead.get("first_name") or "there"
    html_content = f"""
    <h2>Did you know nXtsmile can be $0 down?</h2>
    <p>Hi {name} — we wanted to share something that surprises most people.</p>
    <p>All-on-X dental implants don't have to be a huge upfront expense. With our financing options, most patients pay <strong>less per month than a car payment</strong> — and they eat what they want, smile with confidence, and never worry about dentures slipping again.</p>
    <p><strong>Your options:</strong></p>
    <p>
      🏦 <strong>CareCredit</strong> — 0% interest for 12–18 months<br>
      🍒 <strong>Cherry</strong> — instant approval, flexible monthly plans<br>
      🏥 <strong>In-house financing</strong> — we'll work with your situation
    </p>
    <p>Your free consultation includes a full treatment plan with financing options personalized to your budget. No surprises.</p>
    <a href="{settings.practice_url}#consult" class="btn">See My Financing Options →</a>
    <p>Or call <span class="phone">{settings.office_phone}</span> to talk finances before your visit.</p>
    """
    return _send(
        lead["email"],
        "Your new smile might cost less than you think",
        _base_html(html_content, unsubscribe_url, settings),
        f"Hi {name}, nXtsmile financing starts at $0 down. Call {settings.office_phone} or visit {settings.practice_url}"
    )


def send_day30_cold_email(lead: dict, unsubscribe_url: str) -> bool:
    """Final email — marks cold, leaves door open."""
    settings = get_settings()
    name = lead.get("first_name") or "there"
    html_content = f"""
    <h2>Still here if you need us, {name}</h2>
    <p>We won't keep reaching out — but we wanted you to know the door is always open.</p>
    <p>Whenever you're ready to explore your options, Dr. Gupta would love to meet you. The consultation is always free, always no-pressure.</p>
    <a href="{settings.practice_url}" class="btn">Visit nXtsmile.com →</a>
    <p>Or call us at <span class="phone">{settings.office_phone}</span></p>
    <p>Wishing you a healthy, confident smile — whenever the time is right.</p>
    <p>— Dr. Gupta & the team at {settings.practice_name}</p>
    """
    return _send(
        lead["email"],
        "Still here if you need us",
        _base_html(html_content, unsubscribe_url, settings),
        f"Hi {name}, still here if you need us. {settings.practice_url} or {settings.office_phone}"
    )


def send_office_new_lead(lead: dict) -> bool:
    """Notify office when a new lead arrives."""
    settings = get_settings()
    name = f"{lead.get('first_name','')} {lead.get('last_name','')}".strip() or "Unknown"
    source = lead.get("source", "unknown")
    email = lead.get("email", "")
    phone = lead.get("phone", "")
    campaign = lead.get("utm_campaign", "") or lead.get("gclid", "")

    html = f"""<!DOCTYPE html><html><body style="font-family:sans-serif;padding:20px">
    <h2>🦷 New Lead: {name}</h2>
    <table style="border-collapse:collapse;width:100%">
      <tr><td style="padding:8px;color:#666">Name</td><td style="padding:8px"><strong>{name}</strong></td></tr>
      <tr style="background:#f9f9f9"><td style="padding:8px;color:#666">Email</td><td style="padding:8px"><a href="mailto:{email}">{email}</a></td></tr>
      <tr><td style="padding:8px;color:#666">Phone</td><td style="padding:8px"><a href="tel:{phone}">{phone}</a></td></tr>
      <tr style="background:#f9f9f9"><td style="padding:8px;color:#666">Source</td><td style="padding:8px">{source}</td></tr>
      <tr><td style="padding:8px;color:#666">Campaign</td><td style="padding:8px">{campaign or 'organic'}</td></tr>
      <tr style="background:#f9f9f9"><td style="padding:8px;color:#666">Time</td><td style="padding:8px">{lead.get('created_at','')}</td></tr>
    </table>
    <p style="margin-top:20px"><a href="http://localhost:7070" style="background:#0d7a7f;color:#fff;padding:10px 20px;border-radius:6px;text-decoration:none">View Pipeline Dashboard →</a></p>
    </body></html>"""
    return _send(settings.notify_email, f"🦷 New Lead: {name} ({source})", html)
