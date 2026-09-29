#!/bin/zsh -il
# 双击重启 GoldMonitor：停掉本目录里正在运行的实例，再用同一个 Python 在后台启动新版本。
cd "${0:A:h}" || exit 1
DIR="$PWD"
PY=""

for pid in $(pgrep -f "main\.py"); do
  cwd=$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')
  if [ "$cwd" = "$DIR" ]; then
    [ -z "$PY" ] && PY=$(ps -o command= -p "$pid" | awk '{print $1}')
    kill "$pid" && echo "已停止旧实例 (pid $pid)"
  fi
done

FOUND=""
for candidate in "$PY" python3 python; do
  [ -n "$candidate" ] || continue
  if "$candidate" -c "import PyQt6, requests" 2>/dev/null; then
    PY="$candidate"; FOUND=1; break
  fi
done
if [ -z "$FOUND" ]; then
  echo "找不到装有 PyQt6 的 Python，请先执行：pip install -r requirements.txt"
  read -r "?按回车关闭"
  exit 1
fi

sleep 1
nohup "$PY" main.py >> nohup.out 2>&1 &
NEW_PID=$!
disown
echo "GoldMonitor 已启动 (pid $NEW_PID)，Python：$("$PY" -c 'import sys; print(sys.executable)')"
sleep 2
if kill -0 "$NEW_PID" 2>/dev/null; then
  echo "运行正常，可以关闭这个窗口。"
else
  echo "启动失败，最近的输出："; tail -n 20 nohup.out
  read -r "?按回车关闭"
fi
