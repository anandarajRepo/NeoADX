#!/usr/bin/env bash
# NeoGap — gracefully stop the gap trading strategy
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$SCRIPT_DIR/.neoadx.pid"

if [[ ! -f "$PID_FILE" ]]; then
    echo "NeoGap is not running (no PID file found)."
    exit 0
fi

PID=$(cat "$PID_FILE")

if kill -0 "$PID" 2>/dev/null; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Stopping NeoGap (PID $PID)…"
    kill -SIGTERM "$PID"
    sleep 5
    if kill -0 "$PID" 2>/dev/null; then
        echo "Process still alive — sending SIGKILL"
        kill -SIGKILL "$PID"
    fi
    echo "NeoGap stopped."
else
    echo "NeoGap process (PID $PID) is not running."
fi

rm -f "$PID_FILE"
