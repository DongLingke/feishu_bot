#!/bin/bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_FILE="$PROJECT_DIR/config.py"
RUN_SCRIPT="$PROJECT_DIR/run.sh"

if ! command -v git >/dev/null 2>&1; then
    echo "git is required."
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required."
    exit 1
fi

if [[ ! -d "$PROJECT_DIR/.git" ]]; then
    echo "current directory is not a git repository: $PROJECT_DIR"
    exit 1
fi

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "config.py not found: $CONFIG_FILE"
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

echo "请选择运行模式："
echo "0) 测试模式"
echo "1) 正式模式"
read -r -p "请输入 0 或 1: " selected_mode

case "$selected_mode" in
    0|1)
        ;;
    test|TEST|Test)
        selected_mode="0"
        ;;
    online|ONLINE|Online|prod|PROD|Prod|production|PRODUCTION|Production)
        selected_mode="1"
        ;;
    *)
        echo "无效输入，只能选择 0 或 1。"
        exit 1
        ;;
esac

python3 - "$CONFIG_FILE" "$selected_mode" <<'PY'
from pathlib import Path
import re
import sys

config_path = Path(sys.argv[1])
mode = sys.argv[2]
text = config_path.read_text(encoding="utf-8")
updated, count = re.subn(
    r"^MODE\s*=\s*[01]\s*#.*$",
    f"MODE = {mode}  # 0 表示测试环境，1 表示正式环境",
    text,
    count=1,
    flags=re.MULTILINE,
)
if count != 1:
    raise SystemExit("failed to update MODE in config.py")
config_path.write_text(updated, encoding="utf-8")
PY

echo "config.py 已切换到 MODE=$selected_mode"
"$RUN_SCRIPT"
