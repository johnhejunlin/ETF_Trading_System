#!/usr/bin/env zsh
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"
PYTHON_BIN="${PYTHON_BIN:-$SCRIPT_DIR/.venv/bin/python}"

cd "$SCRIPT_DIR"

"$PYTHON_BIN" trading_engine.py --clear-stop

if ps ax -o pid=,command= | awk '$0 ~ /[[:space:]]trading_engine[.]py([[:space:]]|$)/ && $0 !~ /--status/ && $0 !~ /awk / {found=1} END {exit found ? 0 : 1}'; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') trading_engine.py already running; skip start."
  exit 0
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') opening Trading_Engine.command."
open "$SCRIPT_DIR/Trading_Engine.command"
