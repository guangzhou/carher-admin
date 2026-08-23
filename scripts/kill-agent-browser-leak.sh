#!/usr/bin/env bash
# kill-agent-browser-leak.sh
#
# 本机 Mac 卡顿急救：扫描并清理 agent-browser skill 泄漏的自动化 Chrome 实例。
#
# 背景（2026-08-23 实测定案）：
#   agent-browser skill 会用 `--user-data-dir=/var/folders/.../T/agent-browser-chrome-<uuid>`
#   `--remote-debugging-...` 拉起临时 Chrome。任务结束若未回收，这些实例会长期空跑，
#   每套自带一个 gpu-process + 多个 renderer 空转打满 CPU（实测两套跑近 4 天，load 冲到 36，
#   WindowServer 被连累，整机卡）。它们忽略 SIGTERM，必须 -9。
#
# 判据（怎么确认就是它）：
#   - load average 远高于核数，但内存 free 充足（不是内存压力，是 CPU 被打满）
#   - `ps ... -r | head` 顶部是 "Google Chrome Helper (Renderer/GPU)" 各 ~100%
#   - 出现 >1 个 gpu-process / NetworkService  = 机器上跑着多套 Chrome 实例
#   - 这些 Chrome 主进程带 `--user-data-dir=.../T/agent-browser-chrome-*`
#
# 安全边界：只杀 command line 含 `agent-browser-chrome` 的进程，
#   绝不碰你自己的默认 profile Chrome / Cursor / 飞书 / claude。
#
# 用法：
#   ./kill-agent-browser-leak.sh            # 默认 dry-run，只报告不杀（先对齐）
#   ./kill-agent-browser-leak.sh --kill     # 确认后真杀（-9 全树）
#
# ⚠️ Claude Code 的 Bash 沙箱会拦截 kill（signal-0 探测放行，真正的 KILL 被拒，
#    命令返回成功但进程不死）。--kill 请在能直接执行的终端里跑，
#    或在 Claude Code 里用 dangerouslyDisableSandbox。

set -uo pipefail

MARK='agent-browser-chrome'
DO_KILL=0
[[ "${1:-}" == "--kill" ]] && DO_KILL=1

echo "===== 负载 / 内存 ====="
uptime
if command -v memory_pressure >/dev/null 2>&1; then
  memory_pressure 2>/dev/null | grep -i 'free percentage' || true
fi
echo

# 采集泄漏进程（含所有子进程）—— 换行分隔，兼容 macOS 自带 bash 3.2（无 mapfile）
PIDS=$(ps -Ao pid,command | grep "$MARK" | grep -v grep | awk '{print $1}')
COUNT=$(printf '%s\n' "$PIDS" | grep -c . )

if [[ -z "$PIDS" ]]; then
  echo "✅ 未发现 agent-browser 泄漏 Chrome，机器干净。"
  exit 0
fi

echo "===== 发现 ${COUNT} 个 agent-browser 相关进程 ====="
echo "----- 主实例（root）-----"
ps -ww -Ao pid,pcpu,etime,command | grep -E "user-data-dir=[^ ]*${MARK}" | grep -v grep | \
  while read -r line; do
    p=$(echo "$line" | awk '{print $1}')
    cpu=$(echo "$line" | awk '{print $2}')
    et=$(echo "$line" | awk '{print $3}')
    uid=$(echo "$line" | grep -oE "${MARK}-[0-9a-f-]+" | head -1)
    echo "  PID=$p  CPU=${cpu}%  up=${et}  profile=$uid"
  done
echo "----- 全树累计 CPU -----"
ps -Ao pcpu -p "$(printf '%s\n' "$PIDS" | paste -sd, -)" 2>/dev/null | tail -n +2 | \
  awk '{s+=$1} END {printf "  合计 %.1f%% CPU（约 %.1f 个核）\n", s, s/100}'
echo

if [[ $DO_KILL -eq 0 ]]; then
  echo "🟡 dry-run：以上进程未被终止。确认无误后重跑：$0 --kill"
  exit 0
fi

echo "===== 执行 kill -9（逐个）====="
printf '%s\n' "$PIDS" | xargs -n1 kill -9 2>/dev/null
sleep 2

LEFT=$(ps -Ao pid,command | grep "$MARK" | grep -v grep | awk '{print $1}')
if [[ -z "$LEFT" ]]; then
  echo "✅ 全部清除。当前负载："
  uptime
  echo "（load 1 分钟均值会在 1~2 分钟内继续回落）"
else
  echo "⚠️ 仍有残留：$(printf '%s ' $LEFT)"
  echo "   多半是 Bash 沙箱拦了 kill —— 请在真实终端直接执行本脚本 --kill。"
  exit 1
fi
