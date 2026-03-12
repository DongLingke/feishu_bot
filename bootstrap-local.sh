#!/bin/bash

set -euo pipefail

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required."
    exit 1
fi

MOCK_DIFY_HOST="${MOCK_DIFY_HOST:-127.0.0.1}"
MOCK_DIFY_PORT="${MOCK_DIFY_PORT:-18080}"
export MOCK_DIFY_HOST
export MOCK_DIFY_PORT

python3 -m pip install -r requirements.txt

python3 mock_dify_server.py &
MOCK_DIFY_PID=$!

cleanup() {
    if kill -0 "$MOCK_DIFY_PID" >/dev/null 2>&1; then
        kill "$MOCK_DIFY_PID" >/dev/null 2>&1 || true
        wait "$MOCK_DIFY_PID" 2>/dev/null || true
    fi
}

trap cleanup EXIT INT TERM

export DIFY_API_BASE_URL="${DIFY_API_BASE_URL:-http://${MOCK_DIFY_HOST}:${MOCK_DIFY_PORT}/v1}"
export DIFY_WORKFLOW_PAGE_URL="${DIFY_WORKFLOW_PAGE_URL:-http://${MOCK_DIFY_HOST}:${MOCK_DIFY_PORT}/mock-workflow}"
export DIFY_API_KEY="${DIFY_API_KEY:-local-test-key}"

echo "mock dify is running at ${DIFY_API_BASE_URL}"
echo "starting feishu bot..."

python3 main.py
