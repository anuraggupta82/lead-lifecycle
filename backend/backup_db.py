"""
backup_db.py — Daily pipeline.db backup to Google Drive

Uses SQLite's online backup API (safe while app is running).
Compresses with gzip, uploads to Google Drive folder, keeps last 7 backups.

Schedule: called daily at 2 AM from main.py lifespan scheduler.
Can also be run standalone: python backup_db.py
"""

import gzip
import logging
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
DRIVE_FOLDER_ID = "1TXF-MfUM8qXEZYx7h7mfh0Rix5VizQdS"
SERVICE_ACCOUNT_JSON = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "..",
    "..",
    "..",
    "_CREDENTIALS_VAULT",
    "marketing landing page service account key.json",
)
DELEGATED_USER = "anuraggupta@graftondentalcare.com"
KEEP_LAST_N = 7  # number of daily backups to retain on Drive
BACKUP_PREFIX = "pipeline_backup_"


def _get_drive_service():
    """Build an authenticated Google Drive service client.

    Uses domain-wide delegation to impersonate the Workspace admin so
    backups appear in the admin's My Drive (not the service account's
    quota-less Drive).
    """
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    sa_path = os.path.abspath(SERVICE_ACCOUNT_JSON)
    creds = service_account.Credentials.from_service_account_file(
        sa_path,
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    creds = creds.with_subject(DELEGATED_USER)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _safe_sqlite_backup(src_path: str, dst_path: str):
    """Copy src_path → dst_path using SQLite's online backup API."""
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst, pages=100)  # 100 pages at a time, yields between chunks
    finally:
        dst.close()
        src.close()


def _compress(src_path: str, gz_path: str):
    with open(src_path, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)


def _upload_to_drive(service, gz_path: str, filename: str) -> str:
    """Upload file to Drive folder, return file ID."""
    from googleapiclient.http import MediaFileUpload

    file_metadata = {
        "name": filename,
        "parents": [DRIVE_FOLDER_ID],
    }
    media = MediaFileUpload(gz_path, mimetype="application/gzip", resumable=False)
    result = service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id,name,size",
    ).execute()
    return result.get("id", "")


def _prune_old_backups(service):
    """Delete backups older than KEEP_LAST_N from Drive folder."""
    results = service.files().list(
        q=f"'{DRIVE_FOLDER_ID}' in parents and name contains '{BACKUP_PREFIX}' and trashed=false",
        orderBy="createdTime desc",
        fields="files(id, name, createdTime)",
        pageSize=50,
    ).execute()

    files = results.get("files", [])
    to_delete = files[KEEP_LAST_N:]  # keep newest N, delete the rest
    for f in to_delete:
        try:
            service.files().delete(fileId=f["id"]).execute()
            logger.info(f"Pruned old backup: {f['name']}")
        except Exception as e:
            logger.warning(f"Failed to prune {f['name']}: {e}")

    return len(to_delete)


def run_backup(db_path: str) -> dict:
    """
    Main entry point. Backs up db_path to Google Drive.
    Returns a result dict with status, filename, size_mb.
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    filename = f"{BACKUP_PREFIX}{ts}.db.gz"

    logger.info(f"Starting backup of {db_path} → Drive/{filename}")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_db = os.path.join(tmp, "pipeline_snapshot.db")
        tmp_gz = os.path.join(tmp, filename)

        # Step 1: safe online copy
        try:
            _safe_sqlite_backup(db_path, tmp_db)
        except Exception as e:
            logger.error(f"Backup: SQLite copy failed: {e}")
            return {"status": "error", "error": str(e), "step": "sqlite_copy"}

        snapshot_mb = os.path.getsize(tmp_db) / 1_048_576
        logger.info(f"Backup: snapshot taken ({snapshot_mb:.1f} MB)")

        # Step 2: integrity check on the snapshot
        try:
            conn = sqlite3.connect(tmp_db)
            result = conn.execute("PRAGMA integrity_check").fetchone()
            conn.close()
            if result[0] != "ok":
                logger.error(f"Backup: snapshot integrity check failed: {result[0]}")
                return {"status": "error", "error": f"integrity: {result[0]}", "step": "integrity"}
        except Exception as e:
            logger.error(f"Backup: integrity check error: {e}")
            return {"status": "error", "error": str(e), "step": "integrity"}

        # Step 3: compress
        _compress(tmp_db, tmp_gz)
        gz_mb = os.path.getsize(tmp_gz) / 1_048_576
        logger.info(f"Backup: compressed ({gz_mb:.1f} MB)")

        # Step 4: upload
        try:
            service = _get_drive_service()
            file_id = _upload_to_drive(service, tmp_gz, filename)
            logger.info(f"Backup: uploaded to Drive as {filename} (id={file_id})")
        except Exception as e:
            logger.error(f"Backup: Drive upload failed: {e}")
            return {"status": "error", "error": str(e), "step": "upload"}

        # Step 5: prune old backups
        try:
            pruned = _prune_old_backups(service)
            if pruned:
                logger.info(f"Backup: pruned {pruned} old backup(s)")
        except Exception as e:
            logger.warning(f"Backup: prune failed (non-fatal): {e}")

    logger.info(f"Backup complete: {filename} ({gz_mb:.1f} MB compressed)")
    return {
        "status": "ok",
        "filename": filename,
        "snapshot_mb": round(snapshot_mb, 1),
        "compressed_mb": round(gz_mb, 1),
        "drive_file_id": file_id,
    }


def restore_latest(db_path: str) -> bool:
    """
    Download the most recent backup from Drive and restore it to db_path.
    Returns True on success. Used by startup integrity check.
    """
    import io
    from googleapiclient.http import MediaIoBaseDownload

    logger.warning(f"Restore: downloading latest backup to replace {db_path}")

    try:
        service = _get_drive_service()
        results = service.files().list(
            q=f"'{DRIVE_FOLDER_ID}' in parents and name contains '{BACKUP_PREFIX}' and trashed=false",
            orderBy="createdTime desc",
            fields="files(id, name, createdTime)",
            pageSize=1,
        ).execute()
        files = results.get("files", [])
        if not files:
            logger.error("Restore: no backups found on Drive")
            return False

        latest = files[0]
        logger.info(f"Restore: found {latest['name']} ({latest['id']})")

        with tempfile.TemporaryDirectory() as tmp:
            gz_path = os.path.join(tmp, latest["name"])
            restored_path = os.path.join(tmp, "restored.db")

            # Download
            request = service.files().get_media(fileId=latest["id"])
            with open(gz_path, "wb") as fh:
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()

            # Decompress
            with gzip.open(gz_path, "rb") as f_in, open(restored_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)

            # Verify
            conn = sqlite3.connect(restored_path)
            check = conn.execute("PRAGMA integrity_check").fetchone()
            conn.close()
            if check[0] != "ok":
                logger.error(f"Restore: downloaded backup failed integrity check: {check[0]}")
                return False

            # Swap in
            corrupt_path = db_path + ".corrupt"
            os.rename(db_path, corrupt_path)
            shutil.copy2(restored_path, db_path)
            logger.info(f"Restore: success. Corrupt db saved as {corrupt_path}")
            return True

    except Exception as e:
        logger.error(f"Restore: failed: {e}")
        return False


# ── Standalone run ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")

    # Resolve db path from config
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from config import get_settings

    db_path = get_settings().db_path
    result = run_backup(db_path)
    print(result)
