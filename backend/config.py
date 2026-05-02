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

    # Database
    db_path: str = os.path.expanduser("~/grafton_pipeline/pipeline.db")

    # Kill switch — set EMAILS_DISABLED=true in .env to block all outbound email
    emails_disabled: bool = True

    # Kill switch — set SMS_DISABLED=true in .env to block all outbound SMS
    sms_disabled: bool = True

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
    practice_url: str = "https://nxtsmile.com"
    booking_url: str = "https://patient.rocks/Dashboard/PatientDashboard/N2NiNzM4ZGUtM2IxYS00YjZhLWJjMGItMjAxZjBl"

    # GA4 Measurement Protocol
    ga4_measurement_id: str = "G-B3G7NKS06D"
    ga4_api_secret: str = ""  # Create in GA4 Admin → Data Streams → Measurement Protocol API Secrets

    # GA4 Data API (for pulling analytics reports)
    ga4_property_id: str = ""  # Numeric property ID (not G-xxx), e.g. "123456789"
    ga4_service_account_json: str = ""  # Path to service account JSON file

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

    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache()
def get_settings() -> Settings:
    return Settings()
