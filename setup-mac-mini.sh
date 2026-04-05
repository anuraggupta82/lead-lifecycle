#!/bin/bash
# =============================================================================
# setup-mac-mini.sh
# Run this on the Mac Mini to set up GCS credentials for the lead-lifecycle service.
# Usage: bash setup-mac-mini.sh /path/to/service-account-key.json
# =============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
KEY_DEST="$SCRIPT_DIR/gcs-service-account.json"
ENV_FILE="$BACKEND_DIR/.env"

# ── Step 1: Accept key file path ──────────────────────────────────────────────
if [ -z "$1" ]; then
  echo ""
  echo "Usage: bash setup-mac-mini.sh /path/to/service-account-key.json"
  echo ""
  echo "To get the key file:"
  echo "  1. Go to https://console.cloud.google.com/iam-admin/serviceaccounts"
  echo "  2. Select project: marketing-landing-page-491721"
  echo "  3. Click '1096868046685-compute@developer.gserviceaccount.com'"
  echo "  4. Keys tab → Add Key → Create new key → JSON → Download"
  echo "  5. Run: bash setup-mac-mini.sh ~/Downloads/<key-file>.json"
  echo ""
  exit 1
fi

KEY_SOURCE="$1"

if [ ! -f "$KEY_SOURCE" ]; then
  echo "ERROR: File not found: $KEY_SOURCE"
  exit 1
fi

# Validate it looks like a service account key
if ! python3 -c "import json,sys; d=json.load(open('$KEY_SOURCE')); assert d.get('type')=='service_account'" 2>/dev/null; then
  echo "ERROR: File does not look like a GCP service account key JSON."
  exit 1
fi

# ── Step 2: Copy key to project folder ───────────────────────────────────────
cp "$KEY_SOURCE" "$KEY_DEST"
echo "✓ Key file copied to: $KEY_DEST"

# ── Step 3: Install google-cloud-storage in venv ─────────────────────────────
VENV_PIP="$BACKEND_DIR/venv/bin/pip"
if [ ! -f "$VENV_PIP" ]; then
  echo "ERROR: venv not found at $BACKEND_DIR/venv"
  echo "Run: cd $BACKEND_DIR && python3 -m venv venv && venv/bin/pip install -r requirements.txt"
  exit 1
fi

echo "Installing google-cloud-storage in venv..."
"$VENV_PIP" install --quiet google-cloud-storage==2.18.2
echo "✓ google-cloud-storage installed"

# ── Step 4: Add GOOGLE_APPLICATION_CREDENTIALS to .env ───────────────────────
if [ ! -f "$ENV_FILE" ]; then
  echo "WARNING: .env not found at $ENV_FILE — creating it"
  touch "$ENV_FILE"
fi

# Remove any existing GOOGLE_APPLICATION_CREDENTIALS line
grep -v "^GOOGLE_APPLICATION_CREDENTIALS" "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"

# Append the new value
echo "GOOGLE_APPLICATION_CREDENTIALS=$KEY_DEST" >> "$ENV_FILE"
echo "✓ Added GOOGLE_APPLICATION_CREDENTIALS to $ENV_FILE"

# ── Step 5: Verify credentials work ──────────────────────────────────────────
echo ""
echo "Verifying GCS access..."
GOOGLE_APPLICATION_CREDENTIALS="$KEY_DEST" "$BACKEND_DIR/venv/bin/python3" - <<'PYEOF'
import os, sys
os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", os.environ.get("GOOGLE_APPLICATION_CREDENTIALS",""))
try:
    from google.cloud import storage
    client = storage.Client()
    bucket = client.bucket("nxtsmile-smile-images")
    blobs = list(bucket.list_blobs(max_results=1))
    print(f"✓ GCS access confirmed — bucket 'nxtsmile-smile-images' is reachable ({len(blobs)} blob(s) listed)")
except Exception as e:
    print(f"✗ GCS verification failed: {e}")
    print("  Check that the service account has 'Storage Object Viewer' role on the bucket.")
    sys.exit(1)
PYEOF

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo "============================================================"
echo "  Setup complete. Restart the lead-lifecycle service:"
echo "  cd $BACKEND_DIR && venv/bin/uvicorn main:app --host 0.0.0.0 --port 7070"
echo "============================================================"
echo ""
