#!/bin/bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUN_SCRIPT="$PROJECT_DIR/run.sh"

if ! command -v git >/dev/null 2>&1; then
    echo "git is required."
    exit 1
fi

if [[ ! -d "$PROJECT_DIR/.git" ]]; then
    echo "current directory is not a git repository: $PROJECT_DIR"
    exit 1
fi

if [[ ! -x "$RUN_SCRIPT" ]]; then
    echo "run.sh not found or not executable: $RUN_SCRIPT"
    exit 1
fi

cd "$PROJECT_DIR"

current_branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
if [[ -z "$current_branch" || "$current_branch" == "HEAD" ]]; then
    current_branch="$(git symbolic-ref --short HEAD 2>/dev/null || true)"
fi

if [[ -z "$current_branch" || "$current_branch" == "HEAD" ]]; then
    echo "cannot determine current git branch."
    exit 1
fi

echo "pulling latest code from branch: $current_branch"
git pull --ff-only origin "$current_branch"

"$RUN_SCRIPT"
