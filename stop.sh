#!/bin/bash
# stop.sh — stops the lead-lifecycle server

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PORT=7070
PID_FILE="server.pid"

STOPPED_ANY=false

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE" 2>/dev/null || true)
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
        echo "Stopping server (PID $PID) and any child processes..."

        # Graceful shutdown first (SIGTERM), then force-kill if needed
        if command -v pgrep >/dev/null 2>&1; then
            CHILD_PIDS=$(pgrep -P "$PID" 2>/dev/null || true)
            if [ -n "$CHILD_PIDS" ]; then
                kill $CHILD_PIDS 2>/dev/null || true
            fi
        fi
        kill "$PID" 2>/dev/null || true
        sleep 3
        # Force-kill anything still alive
        if kill -0 "$PID" 2>/dev/null; then
            kill -9 "$PID" 2>/dev/null || true
        fi
        if command -v pgrep >/dev/null 2>&1; then
            CHILD_PIDS=$(pgrep -P "$PID" 2>/dev/null || true)
            if [ -n "$CHILD_PIDS" ]; then
                kill -9 $CHILD_PIDS 2>/dev/null || true
            fi
        fi
        STOPPED_ANY=true
    else
        echo "No running process found for PID in $PID_FILE (already stopped)."
    fi
    rm -f "$PID_FILE"
else
    echo "No $PID_FILE found — server may not have been started via start.sh."
fi

# Safety net: kill anything still listening on the port
REMAINING_PIDS=$(lsof -ti tcp:"$PORT" 2>/dev/null || true)
if [ -n "$REMAINING_PIDS" ]; then
    echo "Killing remaining process(es) on port $PORT: $REMAINING_PIDS"
    kill -9 $REMAINING_PIDS 2>/dev/null || true
    STOPPED_ANY=true
fi

if [ "$STOPPED_ANY" = true ]; then
    echo "Server stopped."
else
    echo "Nothing was running."
fi
