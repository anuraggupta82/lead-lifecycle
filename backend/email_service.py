"""
Email service — sends follow-up emails via Zoho SMTP (info@nxtsmile.com).
All templates are HTML with plain-text fallback.
"""
import smtplib
import logging
import requests
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from config import get_settings

logger = logging.getLogger(__name__)


def _send(to_email: str, subject: str, html: str, plain: str = "") -> bool:
    settings = get_settings()
    if getattr(settings, "emails_disabled", False):
        logger.info(f"EMAILS DISABLED — skipped sending to {to_email}: {subject}")
        return False
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


def _send_msg(msg: MIMEMultipart) -> bool:
    """Send a pre-built MIME message via Zoho SMTP."""
    settings = get_settings()
    if getattr(settings, "emails_disabled", False):
        logger.info(f"EMAILS DISABLED — skipped sending to {msg.get('To', '?')}: {msg.get('Subject', '?')}")
        return False
    if not settings.smtp_password:
        logger.warning("SMTP password not set — email not sent")
        return False
    to_addr = msg.get("To", "?")
    subject = msg.get("Subject", "?")
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
            server.ehlo()
            server.starttls()
            server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg)
        logger.info(f"Email sent to {to_addr}: {subject}")
        return True
    except Exception as e:
        logger.error(f"Email failed to {to_addr}: {e}")
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
    <div class="logo">✨ nXtsmile @ {settings.practice_name}</div>
    {content}
    <div class="footer">
      You received this because you expressed interest in dental implants at {settings.practice_name}.
      &nbsp;|&nbsp; <a href="{unsubscribe_url}">Unsubscribe</a>
      &nbsp;|&nbsp; {settings.practice_name}, Grafton, MA
    </div>
  </div>
</body>
</html>"""


def _get_goal_case_description(goals: str) -> str:
    """Map patient goals to a case description for the email."""
    if not goals:
        return "transformed smiles and restored confidence"
    goals_lower = goals.lower()
    if "missing" in goals_lower:
        return "replaced missing teeth and restored confidence"
    elif "denture" in goals_lower:
        return "replaced uncomfortable dentures with permanent teeth and restored confidence"
    elif "cosmetic" in goals_lower or "makeover" in goals_lower:
        return "created a beautiful cosmetic transformation and restored confidence"
    elif "full mouth" in goals_lower or "full arch" in goals_lower:
        return "performed a complete full mouth restoration and restored confidence"
    else:
        return "transformed smiles and restored confidence"


def _get_case_photo(goals: str) -> bytes:
    """Load the appropriate before/after case photo based on patient goals.
    Falls back to default.jpg if no tag-specific image exists."""
    from pathlib import Path
    case_dir = Path(__file__).parent / "case_photos"
    goals_lower = (goals or "").lower()
    tag_map = {
        "missing": "missing_teeth.jpg",
        "denture": "denture.jpg",
        "cosmetic": "cosmetic.jpg",
        "makeover": "cosmetic.jpg",
        "full mouth": "full_mouth.jpg",
        "full arch": "full_mouth.jpg",
    }
    for keyword, filename in tag_map.items():
        if keyword in goals_lower:
            tagged_path = case_dir / filename
            if tagged_path.exists():
                return tagged_path.read_bytes()
    default_path = case_dir / "default.jpg"
    if default_path.exists():
        return default_path.read_bytes()
    return b""


def _fetch_smile_image(url: str) -> bytes:
    """Download smile preview image from a GCS signed URL."""
    if not url:
        return b""
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200 and len(resp.content) > 1000:
            return resp.content
        logger.warning(f"Smile image fetch failed: status={resp.status_code} size={len(resp.content)}")
    except Exception as e:
        logger.warning(f"Smile image fetch error: {e}")
    return b""


def _resign_smile_url(blob_name: str) -> str:
    """
    Generate a fresh 7-day signed URL from a GCS blob name.
    Called at email-send time so Day 14 / Day 30 emails always get a valid URL
    even though GCS V4 signed URLs cap at 7 days.
    Returns '' on failure (email falls back to text link).
    """
    if not blob_name:
        return ""
    try:
        from datetime import timedelta
        from google.cloud import storage as gcs_storage
        import google.auth
        from google.auth.transport import requests as g_requests

        settings = get_settings()
        credentials, _ = google.auth.default()
        credentials.refresh(g_requests.Request())

        # Use the known compute SA email (set in config or env)
        sa_email = getattr(settings, "gcs_sa_email",
                           "1096868046685-compute@developer.gserviceaccount.com")

        client = gcs_storage.Client()
        blob = client.bucket(settings.gcs_bucket).blob(blob_name)
        signed_url = blob.generate_signed_url(
            expiration=timedelta(days=7),
            method="GET",
            version="v4",
            service_account_email=sa_email,
            access_token=credentials.token,
        )
        return signed_url
    except Exception as e:
        logger.warning(f"GCS re-sign failed for {blob_name}: {e}")
        return ""


def _get_smile_bytes(lead: dict) -> bytes:
    """
    Get smile image bytes for a lead.
    Strategy (in order):
      1. Direct GCS blob download (works with any valid GCS credentials — no signBlob needed)
      2. Re-sign a fresh signed URL from blob name (needs signBlob IAM permission)
      3. Fetch from stored signed URL (may be expired for Day 14/30)
    """
    blob_name = lead.get("smile_blob_name", "")

    # Strategy 1: Direct download from GCS — fastest, most reliable
    if blob_name:
        try:
            from google.cloud import storage as gcs_storage
            settings = get_settings()
            client = gcs_storage.Client()
            blob = client.bucket(settings.gcs_bucket).blob(blob_name)
            data = blob.download_as_bytes()
            if len(data) > 1000:
                logger.info(f"Smile image downloaded directly from GCS: {blob_name} ({len(data)} bytes)")
                return data
            logger.warning(f"GCS blob too small ({len(data)} bytes): {blob_name}")
        except Exception as e:
            # If blob is gone (404), clear it from the local DB so we don't keep trying
            if "404" in str(e) or "No such object" in str(e):
                logger.info(f"GCS blob deleted remotely, clearing smile_blob_name for lead: {blob_name}")
                try:
                    from database import _conn
                    lead_id = lead.get("lead_id") or lead.get("id", "")
                    if lead_id:
                        with _conn() as conn:
                            conn.execute(
                                "UPDATE leads SET smile_blob_name='', smile_image_url='' WHERE id=?",
                                (lead_id,)
                            )
                except Exception as db_err:
                    logger.warning(f"Could not clear smile_blob_name: {db_err}")
                return b""
            logger.warning(f"GCS direct download failed for {blob_name}: {e}")

    # Strategy 2: Re-sign and fetch via HTTP (needs signBlob permission)
    if blob_name:
        fresh_url = _resign_smile_url(blob_name)
        if fresh_url:
            data = _fetch_smile_image(fresh_url)
            if data:
                return data

    # Strategy 3: Stored signed URL (may be expired for Day 14/30)
    stored_url = lead.get("smile_image_url", "")
    if stored_url:
        return _fetch_smile_image(stored_url)
    return b""


def send_day1_email(lead: dict, unsubscribe_url: str) -> bool:
    settings = get_settings()
    name = (lead.get("first_name") or "there").title()
    lead_id = lead.get("lead_id") or lead.get("id", "")

    # Fetch smile image — re-signs fresh URL from blob name so it never expires
    smile_bytes = _get_smile_bytes(lead)

    # Build image block — embedded cid if we have the image, fallback text if not
    if smile_bytes:
        image_block = """
        <img src="cid:smile_preview"
             style="width:100%;max-height:420px;object-fit:contain;border-radius:12px;border:1px solid #e5e7eb;display:block;margin:0 auto 24px;"
             alt="Your smile preview" />
        """
    else:
        image_block = """
        <p style="background:#e8f5f5;border-radius:12px;padding:40px;text-align:center;color:#0d7a7f;
                  font-size:16px;font-weight:600;margin:0 0 24px;">
          Your smile preview is waiting at<br/>
          <a href="{url}" style="color:#0d7a7f;">nxtsmile.com</a>
        </p>
        """.format(url=settings.practice_url)

    msg = MIMEMultipart("related")
    msg["Subject"] = f"Your new smile is closer than you think, {name} :)"
    msg["From"] = settings.email_from
    msg["To"] = lead["email"]

    html = f"""
    <html><body style="font-family:Arial,sans-serif;color:#333;max-width:560px;margin:0 auto;padding:0;">
      <div style="background:#0d7a7f;padding:28px 32px;border-radius:12px 12px 0 0;">
        <h2 style="color:#fff;margin:0;font-size:24px;">Your new smile is closer than you think, {name} :)</h2>
      </div>
      <div style="padding:28px 32px;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 12px 12px;">

        <p style="color:#333;font-size:15px;line-height:1.6;margin:0 0 20px;">
          Hi {name},
        </p>

        <p style="color:#333;font-size:15px;line-height:1.6;margin:0 0 20px;">
          We hope you loved your smile preview! Take another look — this could be you.
        </p>

        {image_block}

        <p style="color:#999;font-size:11px;margin:0 0 20px;text-align:center;">
          *AI generated image. Actual results will vary.
        </p>

        <p style="color:#333;font-size:15px;line-height:1.6;margin:0 0 12px;">
          Every smile transformation starts with a single step. Hundreds of patients walked into
          Grafton Dental Care feeling unsure — and walked out with a smile they couldn't stop showing off.
        </p>

        <p style="color:#333;font-size:15px;line-height:1.6;margin:0 0 12px;">
          You deserve to eat the foods you love, laugh without thinking twice, and feel proud every
          time you look in the mirror. Dr. Gupta and the nXtsmile team are here to make that happen.
        </p>

        <p style="color:#333;font-size:15px;line-height:1.6;margin:0 0 24px;">
          Your free consultation is waiting — no pressure, no obligation. Just a conversation
          about what's possible.
        </p>

        <p style="text-align:center;margin:0 0 12px;">
          <a href="{settings.booking_url}"
             style="display:inline-block;background:#0d7a7f;color:#fff;padding:14px 32px;border-radius:10px;
                    font-weight:700;text-decoration:none;font-size:16px;">
            Book My Free Consultation
          </a>
        </p>

        <p style="text-align:center;margin:0 0 28px;">
          <a href="{settings.practice_url}#callback"
             style="display:inline-block;background:#fff;color:#0d7a7f;padding:12px 28px;border-radius:10px;
                    font-weight:700;text-decoration:none;font-size:15px;border:2px solid #0d7a7f;">
            Request a Callback
          </a>
        </p>

        <hr style="border:none;border-top:1px solid #e5e7eb;margin:0 0 20px;"/>

        <p style="text-align:center;color:#333;font-size:14px;font-weight:700;margin:0 0 4px;">
          nXtsmile @ {settings.practice_name}
        </p>
        <p style="text-align:center;color:#0d7a7f;font-size:14px;margin:0 0 4px;">
          <a href="https://www.nxtsmile.com" style="color:#0d7a7f;text-decoration:none;">www.nxtsmile.com</a>
        </p>
        <p style="text-align:center;color:#333;font-size:14px;margin:0 0 16px;">
          {settings.office_phone}
        </p>

        <div style="background:#f8fafa;border-radius:8px;padding:14px 16px;margin:0 0 20px;">
          <p style="color:#666;font-size:12px;line-height:1.5;margin:0;">
            🔒 Your photo is securely stored and will be automatically deleted after 30 days.
            If you'd like it removed now, <a href="{settings.nxtsmile_api}/delete-image/{lead_id}" style="color:#0d7a7f;">click here to delete immediately</a>.
          </p>
        </div>

        <div style="font-size:12px;color:#999;border-top:1px solid #eee;padding-top:16px;">
          You received this because you expressed interest in dental implants at {settings.practice_name}.
          &nbsp;|&nbsp; <a href="{unsubscribe_url}" style="color:#0d7a7f;">Unsubscribe</a>
          &nbsp;|&nbsp; {settings.practice_name}, Grafton, MA
        </div>
      </div>
    </body></html>
    """

    msg.attach(MIMEText(html, "html"))

    # Embed smile preview image
    if smile_bytes:
        try:
            img_mime = MIMEImage(smile_bytes, _subtype="png")
            img_mime.add_header("Content-ID", "<smile_preview>")
            img_mime.add_header("Content-Disposition", "inline", filename="smile-preview.png")
            msg.attach(img_mime)
        except Exception as e:
            logger.warning(f"Could not attach smile image: {e}")

    return _send_msg(msg)


def send_day7_email(lead: dict, unsubscribe_url: str) -> bool:
    settings = get_settings()
    name = (lead.get("first_name") or "there").title()
    lead_id = lead.get("lead_id") or lead.get("id", "")
    goals = lead.get("goals", "")
    # Parse goals if stored as JSON string
    if goals and goals.startswith("["):
        import json
        try:
            goals = ", ".join(json.loads(goals))
        except Exception:
            pass

    case_description = _get_goal_case_description(goals)
    case_photo_bytes = _get_case_photo(goals)

    # Case photo block
    if case_photo_bytes:
        case_photo_block = """
        <img src="cid:case_photo"
             style="width:100%;border-radius:12px;border:1px solid #e5e7eb;display:block;margin:0 auto 8px;"
             alt="Before and after dental implant case by Dr. Gupta" />
        <p style="color:#999;font-size:11px;margin:0 0 20px;text-align:center;">
          *Actual patient results. Individual results may vary.
        </p>
        """
    else:
        case_photo_block = ""

    msg = MIMEMultipart("related")
    msg["Subject"] = f"What's holding you back, {name}?"
    msg["From"] = settings.email_from
    msg["To"] = lead["email"]

    html = f"""
    <html><body style="font-family:Arial,sans-serif;color:#333;max-width:560px;margin:0 auto;padding:0;">
      <div style="background:#0d7a7f;padding:28px 32px;border-radius:12px 12px 0 0;">
        <h2 style="color:#fff;margin:0;font-size:24px;">What's holding you back, {name}?</h2>
      </div>
      <div style="padding:28px 32px;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 12px 12px;">

        <p style="color:#333;font-size:15px;line-height:1.6;margin:0 0 20px;">
          Hi {name},
        </p>

        <p style="color:#333;font-size:15px;line-height:1.6;margin:0 0 20px;">
          We've had a lot of people tell us the same things before they finally came in:
        </p>

        <div style="background:#f8fafa;border-radius:10px;padding:20px 24px;margin:0 0 24px;">
          <p style="color:#333;font-size:15px;line-height:1.6;margin:0 0 16px;">
            <strong>"I'm worried about the cost."</strong><br/>
            We work with CareCredit, Cherry, and in-house financing — many patients pay as little as $300 a month.
          </p>
          <p style="color:#333;font-size:15px;line-height:1.6;margin:0 0 16px;">
            <strong>"I'm not sure I'm a candidate."</strong><br/>
            That's exactly what the free consultation is for. There's no commitment — just answers.
          </p>
          <p style="color:#333;font-size:15px;line-height:1.6;margin:0 0 16px;">
            <strong>"I'm nervous."</strong><br/>
            Dr. Gupta has helped hundreds of patients just like you. The consultation is relaxed and pressure-free.
          </p>
          <p style="color:#333;font-size:15px;line-height:1.6;margin:0;">
            <strong>"Would it hurt?"</strong><br/>
            Dr. Gupta is an expert in painless dentistry. You will be provided comfortable sedation to make the procedure as painless as possible.
          </p>
        </div>

        <p style="color:#333;font-size:15px;line-height:1.6;margin:0 0 8px;">
          Here's a patient where Dr. Gupta {case_description}. This could be you.
        </p>

        {case_photo_block}

        <p style="text-align:center;margin:0 0 12px;">
          <a href="{settings.booking_url}"
             style="display:inline-block;background:#0d7a7f;color:#fff;padding:14px 32px;border-radius:10px;
                    font-weight:700;text-decoration:none;font-size:16px;">
            Book My Free Consultation
          </a>
        </p>

        <p style="text-align:center;margin:0 0 28px;">
          <a href="{settings.practice_url}#callback"
             style="display:inline-block;background:#fff;color:#0d7a7f;padding:12px 28px;border-radius:10px;
                    font-weight:700;text-decoration:none;font-size:15px;border:2px solid #0d7a7f;">
            Request a Callback
          </a>
        </p>

        <hr style="border:none;border-top:1px solid #e5e7eb;margin:0 0 20px;"/>

        <p style="text-align:center;color:#333;font-size:14px;font-weight:700;margin:0 0 4px;">
          nXtsmile @ {settings.practice_name}
        </p>
        <p style="text-align:center;color:#0d7a7f;font-size:14px;margin:0 0 4px;">
          <a href="https://www.nxtsmile.com" style="color:#0d7a7f;text-decoration:none;">www.nxtsmile.com</a>
        </p>
        <p style="text-align:center;color:#333;font-size:14px;margin:0 0 20px;">
          {settings.office_phone}
        </p>

        <div style="background:#f8fafa;border-radius:8px;padding:14px 16px;margin:0 0 20px;">
          <p style="color:#666;font-size:12px;line-height:1.5;margin:0;">
            🔒 Your photo is securely stored and will be automatically deleted after 30 days.
            If you'd like it removed now, <a href="{settings.nxtsmile_api}/delete-image/{lead_id}" style="color:#0d7a7f;">click here to delete immediately</a>.
          </p>
        </div>

        <div style="font-size:12px;color:#999;border-top:1px solid #eee;padding-top:16px;">
          You received this because you expressed interest in dental implants at {settings.practice_name}.
          &nbsp;|&nbsp; <a href="{unsubscribe_url}" style="color:#0d7a7f;">Unsubscribe</a>
          &nbsp;|&nbsp; {settings.practice_name}, Grafton, MA
        </div>
      </div>
    </body></html>
    """

    msg.attach(MIMEText(html, "html"))

    # Embed case photo
    if case_photo_bytes:
        try:
            case_img = MIMEImage(case_photo_bytes, _subtype="jpeg")
            case_img.add_header("Content-ID", "<case_photo>")
            case_img.add_header("Content-Disposition", "inline", filename="case-photo.jpg")
            msg.attach(case_img)
        except Exception as e:
            logger.warning(f"Could not attach case photo: {e}")

    return _send_msg(msg)


def send_day14_email(lead: dict, unsubscribe_url: str) -> bool:
    settings = get_settings()
    name = (lead.get("first_name") or "there").title()
    lead_id = lead.get("lead_id") or lead.get("id", "")

    # Fetch smile image — re-signs fresh URL from blob name so it never expires
    smile_bytes = _get_smile_bytes(lead)

    if smile_bytes:
        image_block = """
        <img src="cid:smile_preview"
             style="width:100%;max-height:420px;object-fit:contain;border-radius:12px;border:1px solid #e5e7eb;display:block;margin:0 auto 24px;"
             alt="Your smile preview" />
        <p style="color:#999;font-size:11px;margin:-16px 0 20px;text-align:center;">
          *AI generated image. Actual results will vary.
        </p>
        """
    else:
        image_block = ""

    msg = MIMEMultipart("related")
    msg["Subject"] = f"Your new smile might cost less than you think, {name}"
    msg["From"] = settings.email_from
    msg["To"] = lead["email"]

    html = f"""
    <html><body style="font-family:Arial,sans-serif;color:#333;max-width:560px;margin:0 auto;padding:0;">
      <div style="background:#0d7a7f;padding:28px 32px;border-radius:12px 12px 0 0;">
        <h2 style="color:#fff;margin:0;font-size:24px;">Did you know nXtsmile can be $0 down?</h2>
      </div>
      <div style="padding:28px 32px;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 12px 12px;">

        <p style="color:#333;font-size:15px;line-height:1.6;margin:0 0 20px;">
          Hi {name},
        </p>

        <p style="color:#333;font-size:15px;line-height:1.6;margin:0 0 20px;">
          We wanted to share something that surprises most people — All-on-X dental implants don't have to
          be a huge upfront expense. With our financing options, many patients pay as little as $300 a month
          — and they eat what they want, smile with confidence, and never worry about dentures slipping again.
        </p>

        {image_block}

        <div style="background:#f8fafa;border-radius:10px;padding:20px 24px;margin:0 0 24px;">
          <p style="color:#333;font-size:15px;font-weight:700;margin:0 0 12px;">Your financing options:</p>
          <p style="color:#333;font-size:15px;line-height:1.6;margin:0 0 10px;">
            🏦 <strong>CareCredit</strong> — 0% interest available
          </p>
          <p style="color:#333;font-size:15px;line-height:1.6;margin:0 0 10px;">
            🍒 <strong>Cherry</strong> — instant approval, flexible monthly plans
          </p>
          <p style="color:#333;font-size:15px;line-height:1.6;margin:0;">
            🏥 <strong>In-house financing</strong> — we'll work with your situation
          </p>
        </div>

        <p style="color:#333;font-size:15px;line-height:1.6;margin:0 0 24px;">
          We'll discuss your financing options at your free consultation — a full treatment plan
          personalized to your budget. No surprises.
        </p>

        <p style="text-align:center;margin:0 0 12px;">
          <a href="{settings.booking_url}"
             style="display:inline-block;background:#0d7a7f;color:#fff;padding:14px 32px;border-radius:10px;
                    font-weight:700;text-decoration:none;font-size:16px;">
            Book My Free Consultation
          </a>
        </p>

        <p style="text-align:center;margin:0 0 28px;">
          <a href="{settings.practice_url}#callback"
             style="display:inline-block;background:#fff;color:#0d7a7f;padding:12px 28px;border-radius:10px;
                    font-weight:700;text-decoration:none;font-size:15px;border:2px solid #0d7a7f;">
            Request a Callback
          </a>
        </p>

        <hr style="border:none;border-top:1px solid #e5e7eb;margin:0 0 20px;"/>

        <p style="text-align:center;color:#333;font-size:14px;font-weight:700;margin:0 0 4px;">
          nXtsmile @ {settings.practice_name}
        </p>
        <p style="text-align:center;color:#0d7a7f;font-size:14px;margin:0 0 4px;">
          <a href="https://www.nxtsmile.com" style="color:#0d7a7f;text-decoration:none;">www.nxtsmile.com</a>
        </p>
        <p style="text-align:center;color:#333;font-size:14px;margin:0 0 20px;">
          {settings.office_phone}
        </p>

        <div style="background:#f8fafa;border-radius:8px;padding:14px 16px;margin:0 0 20px;">
          <p style="color:#666;font-size:12px;line-height:1.5;margin:0;">
            🔒 Your photo is securely stored and will be automatically deleted after 30 days.
            If you'd like it removed now, <a href="{settings.nxtsmile_api}/delete-image/{lead_id}" style="color:#0d7a7f;">click here to delete immediately</a>.
          </p>
        </div>

        <div style="font-size:12px;color:#999;border-top:1px solid #eee;padding-top:16px;">
          You received this because you expressed interest in dental implants at {settings.practice_name}.
          &nbsp;|&nbsp; <a href="{unsubscribe_url}" style="color:#0d7a7f;">Unsubscribe</a>
          &nbsp;|&nbsp; {settings.practice_name}, Grafton, MA
        </div>
      </div>
    </body></html>
    """

    msg.attach(MIMEText(html, "html"))

    # Embed smile preview image
    if smile_bytes:
        try:
            img_mime = MIMEImage(smile_bytes, _subtype="png")
            img_mime.add_header("Content-ID", "<smile_preview>")
            img_mime.add_header("Content-Disposition", "inline", filename="smile-preview.png")
            msg.attach(img_mime)
        except Exception as e:
            logger.warning(f"Could not attach smile image: {e}")

    return _send_msg(msg)


def send_day30_cold_email(lead: dict, unsubscribe_url: str) -> bool:
    """Final email — marks cold, leaves door open, deletes smile image."""
    settings = get_settings()
    name = (lead.get("first_name") or "there").title()
    lead_id = lead.get("lead_id") or lead.get("id", "")
    # Fetch smile image — re-signs fresh URL from blob name so it never expires
    smile_bytes = _get_smile_bytes(lead)

    if smile_bytes:
        image_block = """
        <img src="cid:smile_preview"
             style="width:100%;max-height:420px;object-fit:contain;border-radius:12px;border:1px solid #e5e7eb;display:block;margin:0 auto 8px;"
             alt="Your smile preview" />
        <p style="color:#999;font-size:11px;margin:0 0 20px;text-align:center;">
          *AI generated image. Actual results will vary.
        </p>
        """
    else:
        image_block = ""

    msg = MIMEMultipart("related")
    msg["Subject"] = f"Still here whenever you're ready, {name}"
    msg["From"] = settings.email_from
    msg["To"] = lead["email"]

    html = f"""
    <html><body style="font-family:Arial,sans-serif;color:#333;max-width:560px;margin:0 auto;padding:0;">
      <div style="background:#0d7a7f;padding:28px 32px;border-radius:12px 12px 0 0;">
        <h2 style="color:#fff;margin:0;font-size:24px;">Still here whenever you're ready, {name}</h2>
      </div>
      <div style="padding:28px 32px;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 12px 12px;">

        <p style="color:#333;font-size:15px;line-height:1.6;margin:0 0 20px;">
          Hi {name},
        </p>

        <p style="color:#333;font-size:15px;line-height:1.6;margin:0 0 16px;">
          We know life gets busy and sometimes the timing just isn't right. That's completely okay.
        </p>

        <p style="color:#333;font-size:15px;line-height:1.6;margin:0 0 16px;">
          Whether it's next week, next month, or next year — you deserve a smile you're proud of,
          and we'd love to help make that happen. Whenever you're ready, reach out to us.
        </p>

        {image_block}

        <div style="background:#f8fafa;border-radius:10px;padding:16px 20px;margin:0 0 24px;">
          <p style="color:#555;font-size:14px;line-height:1.5;margin:0;">
            🔒 Your smile preview will be deleted today as part of our privacy policy.
            If you'd like to start fresh in the future, we can always create a new one for you.
          </p>
        </div>

        <p style="text-align:center;margin:0 0 4px;">
          <a href="{settings.booking_url}"
             style="display:inline-block;background:#0d7a7f;color:#fff;padding:14px 32px;border-radius:10px;
                    font-weight:700;text-decoration:none;font-size:16px;">
            I'm Ready!
          </a>
        </p>
        <p style="text-align:center;color:#999;font-size:12px;margin:0 0 16px;">
          Will take you to schedule your free consultation
        </p>

        <p style="text-align:center;margin:0 0 28px;">
          <a href="{settings.practice_url}#callback"
             style="display:inline-block;background:#fff;color:#0d7a7f;padding:12px 28px;border-radius:10px;
                    font-weight:700;text-decoration:none;font-size:15px;border:2px solid #0d7a7f;">
            Request a Callback
          </a>
        </p>

        <hr style="border:none;border-top:1px solid #e5e7eb;margin:0 0 20px;"/>

        <p style="text-align:center;color:#333;font-size:15px;line-height:1.6;margin:0 0 20px;">
          Wishing you a healthy, confident smile — whenever the time is right.
        </p>

        <p style="text-align:center;color:#333;font-size:14px;font-weight:700;margin:0 0 4px;">
          nXtsmile @ {settings.practice_name}
        </p>
        <p style="text-align:center;color:#0d7a7f;font-size:14px;margin:0 0 4px;">
          <a href="https://www.nxtsmile.com" style="color:#0d7a7f;text-decoration:none;">www.nxtsmile.com</a>
        </p>
        <p style="text-align:center;color:#333;font-size:14px;margin:0 0 16px;">
          {settings.office_phone}
        </p>

        <div style="font-size:12px;color:#999;border-top:1px solid #eee;padding-top:16px;">
          You received this because you expressed interest in dental implants at {settings.practice_name}.
          &nbsp;|&nbsp; <a href="{unsubscribe_url}" style="color:#0d7a7f;">Unsubscribe</a>
          &nbsp;|&nbsp; {settings.practice_name}, Grafton, MA
        </div>
      </div>
    </body></html>
    """

    msg.attach(MIMEText(html, "html"))

    # Embed smile preview image
    if smile_bytes:
        try:
            img_mime = MIMEImage(smile_bytes, _subtype="png")
            img_mime.add_header("Content-ID", "<smile_preview>")
            img_mime.add_header("Content-Disposition", "inline", filename="smile-preview.png")
            msg.attach(img_mime)
        except Exception as e:
            logger.warning(f"Could not attach smile image: {e}")

    return _send_msg(msg)


def send_no_show_email(lead: dict, unsub_url: str) -> bool:
    """No-show follow-up — encourage rebooking with a deposit after missed appointment."""
    settings = get_settings()
    name = (lead.get("first_name") or "there").title()
    lead_id = lead.get("lead_id") or lead.get("id", "")
    delete_url = f"{settings.nxtsmile_api}/delete-image/{lead_id}"
    booking_link = settings.booking_url

    html = f"""<!DOCTYPE html><html><body style="font-family:Arial,sans-serif;color:#333;max-width:600px;margin:0 auto">
    <div style="background:#0d7a7f;padding:28px 32px;border-radius:12px 12px 0 0">
      <h2 style="color:#fff;margin:0">We Missed You, {name}!</h2>
    </div>
    <div style="background:#f9fafb;padding:28px 32px;border-radius:0 0 12px 12px;border:1px solid #e5e7eb">
      <p>We noticed you weren't able to make it to your appointment at Grafton Dental Care. Life happens — we completely understand!</p>

      <p>Your consultation with Dr. Gupta is still available. To secure your spot, we ask for a small
      refundable deposit when you rebook — it helps us reserve the time just for you.</p>

      <div style="text-align:center;margin:28px 0">
        <a href="{booking_link}" style="background:#0d7a7f;color:#fff;padding:14px 32px;
           border-radius:8px;text-decoration:none;font-weight:bold;font-size:16px;display:inline-block">
           Rebook My Consultation</a>
      </div>

      <p>Or call us directly at <strong>{settings.office_phone}</strong> — we're happy to help find a time that works.</p>

      <p style="color:#999;font-size:12px;margin-top:28px;border-top:1px solid #e5e7eb;padding-top:16px">
        {settings.practice_name} · {settings.practice_url}<br>
        <a href="{unsub_url}" style="color:#999">Unsubscribe</a>
        {f' · <a href="{delete_url}" style="color:#999">Delete my smile image</a>' if lead.get("smile_image_url") else ""}
      </p>
    </div></body></html>"""

    return _send(lead.get("email", ""), f"We missed you, {name}! Let's reschedule", html)


def send_office_new_lead(lead: dict) -> bool:
    """Notify office when a new lead arrives."""
    settings = get_settings()
    name = f"{lead.get('first_name','')} {lead.get('last_name','')}".strip().title() or "Unknown"
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
