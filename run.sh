#!/bin/bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="$PROJECT_DIR/output.log"
PID_FILE="$PROJECT_DIR/run.pid"

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required."
    exit 1
fi

declare -a PIDS=()

if [[ -f "$PID_FILE" ]]; then
    saved_pid="$(tr -d '[:space:]' < "$PID_FILE")"
    if [[ "$saved_pid" =~ ^[0-9]+$ ]] && kill -0 "$saved_pid" 2>/dev/null; then
        PIDS+=("$saved_pid")
    fi
fi

while IFS= read -r pid; do
    [[ -n "$pid" ]] || continue

    duplicate="0"
    for existing_pid in "${PIDS[@]}"; do
        if [[ "$existing_pid" == "$pid" ]]; then
            duplicate="1"
            break
        fi
    done

    if [[ "$duplicate" == "0" ]]; then
        PIDS+=("$pid")
    fi
done < <(pgrep -f "$PROJECT_DIR/main.py" || true)

if [[ "${#PIDS[@]}" -gt 0 ]]; then
    echo "stopping existing process: ${PIDS[*]}"
    kill "${PIDS[@]}" 2>/dev/null || true

    for _ in {1..10}; do
        still_running="0"
        for pid in "${PIDS[@]}"; do
            if kill -0 "$pid" 2>/dev/null; then
                still_running="1"
                break
            fi
        done

        if [[ "$still_running" == "0" ]]; then
            break
        fi
        sleep 1
    done

    for pid in "${PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            echo "force killing process: $pid"
            kill -9 "$pid" 2>/dev/null || true
        fi
    done
fi

rm -f "$PID_FILE"
: > "$LOG_FILE"

nohup bash -c '
cd "$1"
python3 -m pip install -r requirements.txt
exec python3 "$1/main.py"
' _ "$PROJECT_DIR" >> "$LOG_FILE" 2>&1 &

new_pid=$!
echo "$new_pid" > "$PID_FILE"

echo "service started: pid=$new_pid"
echo "log file: $LOG_FILE"
