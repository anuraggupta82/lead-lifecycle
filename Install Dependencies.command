#!/bin/bash
# Double-click this file on Mac Mini to install all dependencies
# for the Lead Lifecycle Pipeline.

DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND="$DIR/backend"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Grafton Dental Care — Lead Lifecycle Pipeline"
echo "  Dependency Installer"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── Check Python ──────────────────────────────────────────────
echo "[1/5] Checking Python..."
if ! command -v python3 &>/dev/null; then
  echo "  ✗ Python 3 not found."
  echo "    Install from https://www.python.org/downloads/"
  read -p "Press Enter to exit..."
  exit 1
fi
PYVER=$(python3 --version)
echo "  ✓ $PYVER"
echo ""

# ── Create virtual environment ────────────────────────────────
echo "[2/5] Setting up virtual environment..."
cd "$BACKEND"
if [ -d "venv" ]; then
  echo "  ✓ Virtual environment already exists"
else
  echo "  Creating venv..."
  python3 -m venv venv
  if [ $? -ne 0 ]; then
    echo "  ✗ Failed to create virtual environment"
    read -p "Press Enter to exit..."
    exit 1
  fi
  echo "  ✓ Virtual environment created"
fi
echo ""

# ── Activate and install pip packages ─────────────────────────
echo "[3/5] Installing Python packages..."
source venv/bin/activate

pip install --upgrade pip -q
pip install -r requirements.txt

if [ $? -ne 0 ]; then
  echo ""
  echo "  ✗ Some packages failed to install. Check errors above."
  read -p "Press Enter to exit..."
  exit 1
fi
echo ""
echo "  ✓ All Python packages installed"
echo ""

# ── Verify key imports ────────────────────────────────────────
echo "[4/5] Verifying imports..."
python3 -c "
import sys
errors = []
try:
    import fastapi
    print(f'  ✓ FastAPI {fastapi.__version__}')
except ImportError as e:
    errors.append(f'FastAPI: {e}')
    print(f'  ✗ FastAPI: {e}')

try:
    import uvicorn
    print(f'  ✓ Uvicorn')
except ImportError as e:
    errors.append(f'Uvicorn: {e}')
    print(f'  ✗ Uvicorn: {e}')

try:
    import twilio
    print(f'  ✓ Twilio {twilio.__version__}')
except ImportError as e:
    errors.append(f'Twilio: {e}')
    print(f'  ✗ Twilio: {e}')

try:
    import pymysql
    print(f'  ✓ PyMySQL {pymysql.__version__}')
except ImportError as e:
    errors.append(f'PyMySQL: {e}')
    print(f'  ✗ PyMySQL: {e}')

try:
    import apscheduler
    print(f'  ✓ APScheduler {apscheduler.__version__}')
except ImportError as e:
    errors.append(f'APScheduler: {e}')
    print(f'  ✗ APScheduler: {e}')

try:
    import httpx
    print(f'  ✓ HTTPX {httpx.__version__}')
except ImportError as e:
    errors.append(f'HTTPX: {e}')
    print(f'  ✗ HTTPX: {e}')

try:
    from pydantic_settings import BaseSettings
    print(f'  ✓ Pydantic Settings')
except ImportError as e:
    errors.append(f'Pydantic Settings: {e}')
    print(f'  ✗ Pydantic Settings: {e}')

if errors:
    sys.exit(1)
"

if [ $? -ne 0 ]; then
  echo ""
  echo "  ✗ Some imports failed. Check errors above."
  read -p "Press Enter to exit..."
  exit 1
fi
echo ""

# ── Check .env file ───────────────────────────────────────────
echo "[5/5] Checking configuration..."
if [ -f "$BACKEND/.env" ]; then
  echo "  ✓ .env file found"

  # Quick validation of key settings
  python3 -c "
from config import get_settings
s = get_settings()
print(f'  ✓ SMTP: {s.smtp_host} ({s.smtp_user})')
print(f'  ✓ Twilio: {\"configured\" if s.twilio_account_sid else \"not configured\"} (from: {s.twilio_from_number or \"none\"})')
print(f'  ✓ Database: {s.db_path}')
print(f'  ✓ OpenDental: {s.od_db_host}:{s.od_db_port}')
print(f'  ✓ GCP Project: {s.gcp_project}')
"
else
  echo "  ✗ No .env file found in $BACKEND"
  echo "    Copy your .env file to $BACKEND/.env before launching."
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Installation complete!"
echo ""
echo "  To start the pipeline, double-click:"
echo "    Launch Pipeline.command"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
read -p "Press Enter to close..."
