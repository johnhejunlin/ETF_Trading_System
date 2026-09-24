#!/usr/bin/env zsh
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"
PYTHON_BIN="${PYTHON_BIN:-$SCRIPT_DIR/.venv/bin/python}"

cd "$SCRIPT_DIR"

echo "$(date '+%Y-%m-%d %H:%M:%S') requesting trading engine stop."
"$PYTHON_BIN" trading_engine.py --stop
