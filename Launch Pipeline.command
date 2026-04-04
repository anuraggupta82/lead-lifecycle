#!/bin/bash
# Double-click this file on Mac to launch the Lead Lifecycle Pipeline
# Includes: Dashboard, Follow-ups, Google Ads sync, AI optimizer, OD matcher
# Make executable: chmod +x "Launch Pipeline.command"

DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND="$DIR/backend"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Grafton Dental Care — Lead Lifecycle Pipeline"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Check Python
if ! command -v python3 &>/dev/null; then
  echo "Python 3 not found. Install from https://www.python.org"
  read -p "Press Enter to exit..."
  exit 1
fi

cd "$BACKEND"

# Create venv if needed
if [ ! -d "venv" ]; then
  echo "Creating virtual environment..."
  python3 -m venv venv
fi

source venv/bin/activate

# Install/upgrade dependencies
echo "Installing dependencies..."
pip install -q -r requirements.txt

# Check .env
if [ ! -f ".env" ]; then
  echo ""
  echo "No .env file found."
  echo "   Copy .env.example to .env and fill in your credentials."
  echo ""
  cp .env.example .env
  echo "   Created .env from template. Edit it now, then re-run this launcher."
  open -a TextEdit .env 2>/dev/null || open .env
  read -p "Press Enter after saving your .env file..."
fi

echo ""
echo "Starting services:"
echo "  - Lead Lifecycle API        http://localhost:7070"
echo "  - Pipeline Dashboard        http://localhost:7070"
echo "  - Follow-up Engine          every 15 min"
echo "  - Firestore Sync            on startup"
echo "  - Google Ads GCLID Sync     daily 6 AM"
echo "  - AI Campaign Optimizer     daily 7 AM (dry-run)"
echo "  - OpenDental Matcher        daily 10 PM"
echo "  - Conversion Upload         daily 11 PM"
echo ""
echo "Press Ctrl+C to stop all services"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Open browser after 2 seconds
(sleep 2 && open "http://localhost:7070") &

python3 main.py
