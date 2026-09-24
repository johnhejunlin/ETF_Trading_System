#!/usr/bin/env zsh
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"
PYTHON_BIN="${PYTHON_BIN:-$SCRIPT_DIR/.venv/bin/python}"

cd "$SCRIPT_DIR" || exit 1

clear
echo "AI Stock 交易引擎"
echo "工作目录: $SCRIPT_DIR"
echo "启动命令: $PYTHON_BIN trading_engine.py"
echo

if ! accessibility_probe="$("$PYTHON_BIN" -c 'import subprocess, sys; result = subprocess.run(["osascript", "-e", "tell application \"System Events\" to tell process \"Terminal\" to return count of UI elements"], capture_output=True, text=True); sys.stderr.write(result.stderr or result.stdout) if result.returncode else None; raise SystemExit(result.returncode)' 2>&1)"; then
  python_app_path="$("$PYTHON_BIN" -c 'import sys; from pathlib import Path; print(Path(sys.prefix) / "Resources" / "Python.app")')"
  echo "Python 子进程无法使用 macOS 辅助功能，交易引擎未启动。"
  echo "$accessibility_probe"
  echo "请把下面的 Python.app 添加到 系统设置 → 隐私与安全性 → 辅助功能 并开启："
  echo "$python_app_path"
  echo "然后 Command+Q 完全退出并重启 Terminal。"
  echo
  read "answer?按回车关闭窗口。"
  exit 1
fi
echo "Python → osascript 辅助功能预检通过。"
echo

if [[ -f STOP_TRADING ]]; then
  echo "注意: 检测到 STOP_TRADING，一键停止文件仍然存在。"
  echo "如需恢复运行，请先在项目目录执行: python3 trading_engine.py --clear-stop"
  echo
fi

running_processes="$(ps ax -o pid=,command= | awk -v self="$$" '$1 != self && $0 ~ /[[:space:]]trading_engine[.]py([[:space:]]|$)/ && $0 !~ /--status/ && $0 !~ /awk / {print}' || true)"
if [[ -n "$running_processes" ]]; then
  echo "检测到交易引擎可能已经在运行:"
  echo "$running_processes"
  echo
  read "answer?仍要再启动一个新实例吗？输入 YES 继续: "
  if [[ "$answer" != "YES" ]]; then
    echo "已取消启动。"
    echo "关闭这个窗口即可。"
    exit 0
  fi
fi

echo "账户同步由 trading_engine 发起 AppleScript bridge 请求；若快照缺失或过期，引擎会等待回写。"
echo

# Keep the Terminal-owned shell alive. Replacing it with Python via `exec`
# changes the process responsible for later osascript Accessibility requests.
"$PYTHON_BIN" trading_engine.py
exit $?
