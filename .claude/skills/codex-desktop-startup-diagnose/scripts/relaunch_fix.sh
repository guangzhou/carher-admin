#!/usr/bin/env sh
# 用「让 chatgpt.com 快速失败」这一刀重启 Codex Desktop，并自动前后对照。
#
# 这是**干预实验**，不是配置微调：跑完你会拿到一张「改前 mounted / 改后 mounted」的对照表。
# 2026-09-09 实测 34.7s/33.1s → 4.5s/3.2s。
#
# 用法：  relaunch_fix.sh --yes          # 会退出并重启 Codex（会打断正在跑的轮次）
#         relaunch_fix.sh --revert --yes # 不带 flag 重启（回到原状，用来复现故障）
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
# 按 bundle id 找 app，不写死路径：2026-09-10 这个包被从 /Applications/Codex.app 挪到
# /Applications/OpenAI Codex.app（旧路径下子进程一 exec 就被 SIGKILL，只能换包名），
# 写死路径的脚本当场失效。CODEX_APP 优先。
find_app() {
  for a in /Applications/*.app; do
    id=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$a/Contents/Info.plist" 2>/dev/null)
    [ "$id" = "com.openai.codex" ] && { printf '%s' "$a"; return; }
  done
  printf '%s' /Applications/Codex.app
}
APP=${CODEX_APP:-$(find_app)}
REVERT=0; YES=0
for a in "$@"; do
  case "$a" in
    --revert) REVERT=1 ;;
    --yes|-y) YES=1 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

if [ "$YES" != 1 ]; then
  echo "这会退出并重启 Codex Desktop（正在跑的轮次会断）。确认请加 --yes。"
  exit 2
fi
[ -d "$APP" ] || { echo "找不到 $APP"; exit 2; }

echo "=== 改前（最后一次挂载）==="
python3 "$HERE/startup_timing.py" --days 2 | tail -3 || true

echo
echo "=== 退出 Codex ==="
# 用 AppleScript 的 quit 而不是 kill：让它正常保存 state_*.sqlite
osascript -e 'quit app "ChatGPT"' 2>/dev/null || true
# 等它真的走干净；主进程名是 ChatGPT（不是 Codex）
for i in 1 2 3 4 5 6 7 8 9 10; do
  pgrep -f "$APP/Contents/MacOS/ChatGPT" >/dev/null 2>&1 || break
  sleep 1
done
pgrep -f "$APP/Contents/MacOS/ChatGPT" >/dev/null 2>&1 && echo "警告：进程还在，继续也可能起不来第二个实例"

echo "=== 启动 ==="
if [ "$REVERT" = 1 ]; then
  echo "(revert 模式：不带 host-resolver-rules)"
  open -a "$APP"
else
  # --host-resolver-rules 是 Chromium 自带的 app 作用域 DNS 覆盖：只影响这个进程，
  # 不动系统 DNS、不需要 root。缺点是**只对本次启动有效**，用户自己双击就没了。
  open -a "$APP" --args \
    --host-resolver-rules="MAP chatgpt.com 127.0.0.1,MAP *.chatgpt.com 127.0.0.1"
fi

echo "=== 等渲染层挂载（最多 60s）==="
sleep 12
for i in 1 2 3 4 5 6 7 8; do
  if python3 "$HERE/startup_timing.py" --days 1 2>/dev/null | grep -q "最后一次挂载"; then break; fi
  sleep 6
done

echo
echo "=== 改后 ==="
python3 "$HERE/startup_timing.py" --days 1 | tail -8
echo
echo "退出码 0 = 当前挂载已在阈值内。持久化（免得下次双击又慢）见 SKILL.md §3。"
python3 "$HERE/startup_timing.py" --days 1 >/dev/null
