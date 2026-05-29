"""
Configuration — reads from .env file.
"""
import os
from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Service
    host: str = "0.0.0.0"
    port: int = 7070
    secret_key: str = "changeme-pipeline-secret"
    admin_password: str = "GDC-pipeline-2026!"
    # Public-facing base URL — used for unsubscribe links in emails.
    # Set to Cloud Run service URL in prod .env, e.g.:
    #   BASE_URL=https://marketing-backend-xxxx-uc.a.run.app
    base_url: str = "http://localhost:7070"

    # Database
    # Default: same folder as this file (backend/pipeline.db).
    # Override in .env with DB_PATH=/full/path/to/pipeline.db
    # On a new machine: just copy pipeline.db here and set DB_PATH accordingly.
    db_path: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pipeline.db")

    # Kill switch — set EMAILS_DISABLED=true in .env to block all outbound email
    emails_disabled: bool = True

    # Kill switch — set SMS_DISABLED=true in .env to block all outbound SMS
    sms_disabled: bool = True

    # Kill switch — must be True in .env AND runtime DB toggle must be 'true'
    # for any Google Ads WRITE operation to execute. Reads are always allowed.
    # See campaign_safety.py for the two-layer check logic.
    campaign_write_ops_enabled: bool = False

    # Environment + test redirect (dev mode reroutes mail/SMS to these)
    env: str = "dev"                              # "dev" or "prod"
    test_redirect_email: str = "anurag82@gmail.com"
    test_redirect_phone: str = "+13122134799"

    # Email (Zoho SMTP — marketing emails from nXtsmile)
    smtp_host: str = "smtp.zoho.com"
    smtp_port: int = 587
    smtp_user: str = "info@nxtsmile.com"
    smtp_password: str = ""
    imap_password: str = ""  # Zoho IMAP password for info@nxtsmile.com (may differ from SMTP)
    email_from: str = "nXtsmile <info@nxtsmile.com>"
    notify_email: str = "info@graftondentalcare.com"

    # Twilio SMS
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""   # e.g. +15083184477
    whatsapp_sandbox_number: str = ""  # Set to +14155238886 in dev to use WhatsApp sandbox

    # Firestore (GCP) — for syncing existing nxtsmile leads
    gcp_project: str = "marketing-landing-page-491721"
    firestore_collection: str = "leads"
    firestore_secret: str = "grafton2026"

    # nxtsmile backend (to pull leads via API as fallback)
    nxtsmile_api: str = "https://nxtsmile-api-1096868046685.us-east4.run.app"

    # Appointment scheduler (GCP)
    scheduler_api: str = "https://scheduler-api-981004615066.us-east4.run.app"
    scheduler_admin_password: str = "GDC-admin-2026!"

    # OpenDental (local — only accessible on office network)
    od_db_host: str = "GraftonServer"
    od_db_port: int = 3306
    od_db_user: str = "root"
    od_db_password: str = ""
    od_db_name: str = "opendental"
    od_api_base: str = "http://GraftonServer:30223/api/v1"
    od_developer_key: str = "MBSPzyk526RfVo3O"
    od_customer_key: str = "UwPAttsiOFMuNNaz"

    # GCS (smile image storage)
    gcs_bucket: str = "nxtsmile-smile-images"
    gcs_sa_email: str = "1096868046685-compute@developer.gserviceaccount.com"

    # Practice
    office_phone: str = "508-318-4477"
    practice_name: str = "Grafton Dental Care"
    practice_url: str = "https://graftondentalcare.com"
    booking_url: str = "https://patient.rocks/Dashboard/PatientDashboard/N2NiNzM4ZGUtM2IxYS00YjZhLWJjMGItMjAxZjBl"

    # GA4 Measurement Protocol
    ga4_measurement_id: str = "G-B3G7NKS06D"
    ga4_api_secret: str = ""  # Create in GA4 Admin → Data Streams → Measurement Protocol API Secrets

    # GA4 Data API (for pulling analytics reports)
    # Single-property fallback (used by Measurement Protocol sender only)
    ga4_property_id: str = ""  # Numeric property ID (not G-xxx), e.g. "123456789"
    ga4_service_account_json: str = ""  # Path to service account JSON file
    # Multi-property map: JSON string keyed by domain → GA4 numeric property ID
    # e.g. '{"nxtsmile.com": "531016678", "graftondentalcare.com": "536128204", "visitgdc.com": "533672873"}'
    # Add new domains here as new landing pages are created.
    ga4_properties: str = "{}"

    # Google Places API (competitor discovery)
    google_places_api_key: str = ""

    # Google Ads API
    google_ads_client_id: str = ""
    google_ads_client_secret: str = ""
    google_ads_refresh_token: str = ""
    google_ads_developer_token: str = ""
    google_ads_customer_id: str = "2498049505"
    google_ads_login_customer_id: str = "2498049505"
    google_ads_manager_id: str = "4814239317"
    # Comma-separated Google geo target constant resource names for Keyword Planner
    # e.g. "geoTargetConstants/1020615" for Worcester MA metro area
    # Find IDs at: https://developers.google.com/google-ads/api/data/geotargets
    google_ads_geo_target_ids: str = ""

    # Mango Voice (PBX call tracking)
    mango_username: str = ""
    mango_password: str = ""
    mango_pbx_id: str = "9021"
    mango_api_base: str = "https://api.mangovoice.com"
    # Set to false to disable Mango sync (e.g. if credentials not yet configured)
    mango_enabled: bool = False

    # OpenAI (Whisper transcription — BAA covered)
    openai_api_key: str = ""
    # Max recording duration to transcribe (seconds). Calls shorter than this skip transcription.
    call_transcription_min_sec: int = 30
    # Max calls to auto-transcribe per scheduler run (cost guard)
    call_transcription_batch_size: int = 10

    # ── Vertex AI (call summarization + grading — HIPAA-compliant via BAA) ────
    # Uses google-cloud-aiplatform SDK; data stays within GCP under BAA.
    # gemini_api_key is NOT used — all Gemini calls go through Vertex.
    vertex_project_id: str = "marketing-landing-page-491721"
    vertex_location: str = "us-central1"
    vertex_credentials_path: str = ""        # SA key file; blank = ADC / Cloud Run SA
    vertex_model: str = "gemini-2.5-flash"   # model for summary + grading

    # ── Call Analysis pipeline ───────────────────────────────────────────────
    mango_pipeline_enabled: bool = False       # Master on/off switch
    mango_pipeline_auto_grade: bool = True     # Run Gemini/Vertex grading after each summary
    mango_pipeline_auto_suggest_action: bool = True  # Run AI next-action suggestion after each grade
    mango_pipeline_only_inbound: bool = True   # Skip outbound calls
    mango_pipeline_min_seconds: int = 30       # Skip calls shorter than this
    mango_pipeline_max_per_run: int = 20       # Cost guard: max calls per scheduler tick
    mango_pipeline_interval_min: int = 10      # Minutes between pipeline runs
    mango_pipeline_recording_ttl_min: int = 60 # Delete cached recordings older than this
    mango_recording_dir: str = "/tmp/gdc_recordings"

    # Whisper backend: 'api' = OpenAI cloud (default), 'local' = on-device GPU
    mango_whisper_mode: str = "api"
    mango_whisper_local_model: str = "large-v2"

    # ── Google Ads Attribution Window (PR 2) ─────────────────────────────────
    # Number of days after lead/call anchor to count as 365d attribution window.
    # Default 365 (one year). PR 3 will expose this in the Admin UI.
    # No environment variable required — config-driven only.
    gads_attribution_window_days: int = 365

    # ── CallRail (call tracking — HIPAA Path B: no recording) ────────────────
    # Recording is DISABLED on all trackers. Do NOT re-enable without signing BAA.
    # Credentials at: _CREDENTIALS_VAULT/callrail-api.json
    callrail_api_key: str = ""
    callrail_account_id: str = "431682122"
    callrail_company_id: str = "340886676"
    callrail_company_resource_id: str = "COM019e4b3eeb1878a78c115d6f8a56cd9b"
    # Webhook secret (HMAC-SHA256) — set in .env after generating in CallRail dashboard
    callrail_webhook_secret: str = ""

    # ── Microsoft Clarity (session recording + heatmaps) ─────────────────────
    # Tokens generated in Clarity → Settings → Data Export (long-lived JWTs)
    # Credentials at: _CREDENTIALS_VAULT/Clarity Token.rtf
    clarity_token_gdc: str = ""       # graftondentalcare.com project
    clarity_token_nxtsmile: str = ""  # nxtsmile.com project

    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache()
def get_settings() -> Settings:
    return Settings()
