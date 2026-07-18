#!/bin/bash
# start.sh — starts the lead-lifecycle server on port 7070

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PORT=7070
PID_FILE="server.pid"
LOG_DIR="backend/logs"
LOG_FILE="$LOG_DIR/server.log"

mkdir -p "$LOG_DIR"

# Kill any existing process on the port first
EXISTING_PIDS=$(lsof -ti tcp:"$PORT" 2>/dev/null || true)
if [ -n "$EXISTING_PIDS" ]; then
    echo "Port $PORT is in use by PID(s): $EXISTING_PIDS — stopping..."
    kill $EXISTING_PIDS 2>/dev/null || true
    sleep 2
    # Force-kill only if still alive
    STILL_ALIVE=$(lsof -ti tcp:"$PORT" 2>/dev/null || true)
    if [ -n "$STILL_ALIVE" ]; then
        kill -9 $STILL_ALIVE 2>/dev/null || true
    fi
fi

# Wait for the port to be free
for i in $(seq 1 20); do
    if [ -z "$(lsof -ti tcp:"$PORT" 2>/dev/null || true)" ]; then
        break
    fi
    sleep 0.5
done

if [ -n "$(lsof -ti tcp:"$PORT" 2>/dev/null || true)" ]; then
    echo "ERROR: Port $PORT is still in use after waiting. Aborting."
    exit 1
fi

# Start the server
nohup backend/venv/bin/python3 backend/main.py >> "$LOG_FILE" 2>&1 &
SERVER_PID=$!

echo "$SERVER_PID" > "$PID_FILE"

# Give it a moment to confirm it's alive
sleep 1

if kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "Server started successfully (PID $SERVER_PID) on port $PORT"
    echo "Logs: $LOG_FILE"
else
    echo "ERROR: Server failed to start. Check $LOG_FILE for details."
    rm -f "$PID_FILE"
    exit 1
fi
