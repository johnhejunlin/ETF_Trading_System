#!/usr/bin/env zsh
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"
PYTHON_BIN="${PYTHON_BIN:-$SCRIPT_DIR/.venv/bin/python}"

cd "$SCRIPT_DIR" || exit 1

clear
echo "AI Stock 交易引擎启动 + 实时日志"
echo "工作目录: $SCRIPT_DIR"
echo "日志文件: $SCRIPT_DIR/trading_engine.log"
echo "停止交易引擎: python3 trading_engine.py --stop"
echo "----------------------------------------"
echo

"$PYTHON_BIN" trading_engine.py --clear-stop >/dev/null 2>&1 || true

echo "$(date '+%Y-%m-%d %H:%M:%S') 打开同花顺..."
open -a "同花顺" >/dev/null 2>&1 || open "/Applications/同花顺.app" >/dev/null 2>&1 || true

if ! accessibility_probe="$("$PYTHON_BIN" -c 'import subprocess, sys; result = subprocess.run(["osascript", "-e", "tell application \"System Events\" to tell process \"Terminal\" to return count of UI elements"], capture_output=True, text=True); sys.stderr.write(result.stderr or result.stdout) if result.returncode else None; raise SystemExit(result.returncode)' 2>&1)"; then
  python_app_path="$("$PYTHON_BIN" -c 'import sys; from pathlib import Path; print(Path(sys.prefix) / "Resources" / "Python.app")')"
  echo "$(date '+%Y-%m-%d %H:%M:%S') Python → osascript 辅助功能预检失败，交易引擎未启动。"
  echo "$accessibility_probe"
  echo "请把下面的 Python.app 添加到 系统设置 → 隐私与安全性 → 辅助功能 并开启："
  echo "$python_app_path"
  echo "然后 Command+Q 完全退出并重启 Terminal。"
  exit 1
fi
echo "$(date '+%Y-%m-%d %H:%M:%S') Python → osascript 辅助功能预检通过。"

touch trading_engine.log

running_processes="$(ps ax -o pid=,command= | awk -v self="$$" '$1 != self && $0 ~ /[[:space:]]trading_engine[.]py([[:space:]]|$)/ && $0 !~ /--status/ && $0 !~ /awk / {print}' || true)"
if [[ -n "$running_processes" ]]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') trading_engine.py 已在运行，直接进入实时日志。"
  echo
  echo "关闭此窗口不会自动停止既有后台引擎；如需停止请执行: python3 trading_engine.py --stop"
  echo "----------------------------------------"
  exec tail -n 80 -f trading_engine.log
else
  echo "$(date '+%Y-%m-%d %H:%M:%S') 前台启动 trading_engine.py，当前窗口即实时日志。"
  echo "按 Ctrl+C 可停止本次前台引擎；也可另开终端执行: python3 trading_engine.py --stop"
  echo "----------------------------------------"
  # Keep the Terminal-owned shell alive so child osascript calls retain the
  # same Accessibility-responsible process chain as the successful preflight.
  "$PYTHON_BIN" trading_engine.py
  exit $?
fi
