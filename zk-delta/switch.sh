#!/usr/bin/env bash
# zk-delta/switch.sh —— 把本机 Cursor 在「今天的地址」和「zk-delta 小代理」之间来回切
#
# 为什么单独做一个脚本、而不是我半夜直接给你切了：
#   写 Cursor 的 BYOK 配置必须先退出 Cursor GUI（外部写 state.vscdb 有内存覆盖竞态，
#   见 cursor_team_setup.js 开头的注意事项）。你的 Cursor 当时开着，强退有丢未保存内容的风险，
#   所以这一步留给你按一下。
#
# 用法：
#   ./zk-delta/switch.sh on      切到 zk-delta（会先装好常驻小代理，再改 Cursor 配置）
#   ./zk-delta/switch.sh off     切回今天的地址（并停掉常驻小代理）
#   ./zk-delta/switch.sh status  看现在指到哪、小代理活没活
#
# on 之后如果哪里不对，随时 ./zk-delta/switch.sh off 一条命令回到今天的形态。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
SETUP="$REPO/scripts/zk-cursor-web/cursor_team_setup.js"

TODAY_URL="https://cc.auto-link.com.cn/pro/v1"
DELTA_URL="http://127.0.0.1:8788/v1"

PLIST="$HOME/Library/LaunchAgents/com.zkdelta.sidecar.plist"
LOG="$HOME/Library/Logs/zk-delta-sidecar.log"

NODE_BIN="$(command -v node || true)"
[[ -n "$NODE_BIN" ]] || { echo "找不到 node"; exit 2; }

sidecar_alive () { curl -fsS -m 3 http://127.0.0.1:8788/healthz >/dev/null 2>&1; }

install_agent () {
  mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")"
  # 先清掉任何占着 8788 的游离小代理（比如手工 nohup 起来调试的那种），
  # 否则 launchctl 拉起来的那个会因为端口被占而反复重启，KeepAlive 还会把它藏起来看不出错。
  local squatters
  squatters="$(pgrep -f 'sidecar/sidecar\.js' || true)"
  if [[ -n "$squatters" ]]; then
    echo "    清掉占着端口的游离小代理: $squatters"
    # shellcheck disable=SC2086
    kill $squatters 2>/dev/null || true
    sleep 1
  fi
  cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.zkdelta.sidecar</string>
  <key>ProgramArguments</key>
  <array>
    <string>${NODE_BIN}</string>
    <string>${HERE}/sidecar/sidecar.js</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>ZKD_PORT</key><string>8788</string>
    <key>ZKD_DELTA_URL</key><string>https://cc.auto-link.com.cn/zkd/v1/delta</string>
    <key>ZKD_UPSTREAM</key><string>https://cc.auto-link.com.cn/pro</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${LOG}</string>
  <key>StandardErrorPath</key><string>${LOG}</string>
</dict></plist>
PLIST_EOF
  launchctl unload "$PLIST" 2>/dev/null || true
  launchctl load "$PLIST"
  for i in $(seq 1 30); do sidecar_alive && return 0; sleep 0.5; done
  echo "小代理没起来，看 $LOG"; return 1
}

case "${1:-status}" in
  on)
    pgrep -x Cursor >/dev/null && { echo "Cursor 还开着。先完全退出 Cursor（Cmd+Q）再跑这条，否则配置会被它的内存覆盖回去。"; exit 1; }
    echo "==> 装常驻小代理"
    install_agent
    echo "==> 自检：小代理在回落模式下能不能打通今天的链路"
    curl -fsS -m 20 -X POST http://127.0.0.1:8788/v1/chat/completions \
      -H "content-type: application/json" -H "authorization: Bearer ${ZKD_KEY:-sk-none}" \
      -d '{"model":"cursor-web-fc-82-terra","messages":[{"role":"user","content":"hi"}],"stream":false}' \
      -o /dev/null -w '    自检 HTTP %{http_code}\n' || true
    echo "==> 改 Cursor BYOK 地址 -> $DELTA_URL"
    node "$SETUP" --base-url "$DELTA_URL"
    echo
    echo "好了。开 Cursor，随便聊几轮。"
    echo "看省了多少：curl -s http://127.0.0.1:8788/metrics.json"
    echo "要回退：$0 off"
    ;;
  off)
    pgrep -x Cursor >/dev/null && { echo "Cursor 还开着。先完全退出 Cursor（Cmd+Q）再跑这条。"; exit 1; }
    echo "==> Cursor BYOK 地址 -> $TODAY_URL"
    node "$SETUP" --base-url "$TODAY_URL"
    echo "==> 停常驻小代理"
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "已回到今天的形态。"
    ;;
  status)
    echo -n "小代理: "; sidecar_alive && curl -s http://127.0.0.1:8788/healthz || echo "没活"
    echo
    echo -n "常驻: "; [[ -f "$PLIST" ]] && echo "已装 ($PLIST)" || echo "没装"
    echo -n "198 zk-delta: "; curl -fsS -m 8 https://cc.auto-link.com.cn/zkd/healthz || echo "打不通"
    echo
    if sidecar_alive; then echo "本轮统计:"; curl -s http://127.0.0.1:8788/metrics.json; echo; fi
    ;;
  *) echo "用法: $0 {on|off|status}"; exit 2 ;;
esac
