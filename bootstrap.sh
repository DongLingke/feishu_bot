#!/bin/bash

set -euo pipefail

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required."
    exit 1
fi

python3 -m pip install -r requirements.txt
python3 main.py
