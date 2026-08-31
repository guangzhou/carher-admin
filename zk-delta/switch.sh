#!/usr/bin/env bash
# zk-delta/switch.sh —— 把本机 Cursor 在「今天的地址」和「zk-delta 小代理」之间来回切
#
# 写 Cursor 的 BYOK 配置必须先退出 Cursor GUI（外部写 state.vscdb 有内存覆盖竞态），
# 所以 on/off 都会先检查 Cursor 有没有在跑，在跑就拒绝。
#
# 08-31 修过两处：这个脚本原先调 scripts/zk-cursor-web/cursor_team_setup.js 来改地址，
# 但那个安装器是靠 process.execPath 推导 Cursor 安装目录、要用 Cursor 自带的 Electron
# 以 node 模式跑的；这里用系统 node 调它，它就去 Homebrew 的 node 前缀底下找 Cursor，
# 直接报「找不到 Cursor 资源目录」退出。而且不加 --apply 它只是 dry-run 什么都不写。
# 也就是说在那之前，switch.sh on 从来没真的切过 Cursor 的地址。
# 现在改成调 cursor_baseurl.js —— 只动 openAIBaseUrl 一个字段，理由见那个文件的注释。
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
# 只改 BYOK 地址一个字段。刻意不用 scripts/zk-cursor-web/cursor_team_setup.js：
# 那是装机安装器，--apply 会顺手重打 bundle、塞 6 个 cursor-g 模型、提示粘 Key，
# 还会在选中模型不以 cursor-g 开头时把 composer 覆盖成 cursor-g-5.6-sol ——
# 本机 composer 现在正是基准 cursor-web-fc-82-terra，拿它当开关会把基准换掉。
FLIP="$HERE/cursor_baseurl.js"

TODAY_URL="https://cc.auto-link.com.cn/pro/v1"
DELTA_URL="http://127.0.0.1:8788/v1"

PLIST="$HOME/Library/LaunchAgents/com.zkdelta.sidecar.plist"
LOG="$HOME/Library/Logs/zk-delta-sidecar.log"

# 采集：切过去之后自动攒真实 Cursor body，攒够就把离线金样第 ⑨ 项从红转绿。
# 默认开着——不开的话「切过去」和「验完」之间还差一步手工设环境变量，
# 那一步之前一直没人做，⑨ 就一直红着。封顶 40 条，采够自己停手。
# 不想采：CAPTURE_MAX=0 ./zk-delta/switch.sh on
CAPTURE_DIR="${ZKD_CAPTURE:-$HERE/tests/fixtures}"
CAPTURE_MAX="${CAPTURE_MAX:-40}"
[[ "$CAPTURE_MAX" == "0" ]] && CAPTURE_DIR=""

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
    <key>ZKD_CAPTURE</key><string>${CAPTURE_DIR}</string>
    <key>ZKD_CAPTURE_MAX</key><string>${CAPTURE_MAX}</string>
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
    # 没给 ZKD_KEY 时上游会 401 —— 那说明链路是通的（请求到了 LiteLLM 才谈得上鉴权），
    # 只是没带凭据。真正该慌的是连不上（000）或 5xx。所以这里分开说，不然 401 会被误读成坏了。
    SELF_CODE="$(curl -sS -m 20 -X POST http://127.0.0.1:8788/v1/chat/completions \
      -H "content-type: application/json" -H "authorization: Bearer ${ZKD_KEY:-sk-none}" \
      -d '{"model":"cursor-web-fc-82-terra","messages":[{"role":"user","content":"hi"}],"stream":false}' \
      -o /dev/null -w '%{http_code}' || echo 000)"
    case "$SELF_CODE" in
      200) echo "    自检 HTTP 200，链路通" ;;
      401|403) echo "    自检 HTTP $SELF_CODE —— 链路通，只是这条自检没带 ZKD_KEY（不影响 Cursor，它有自己的 Key）" ;;
      000) echo "    !! 自检连不上小代理，看 $LOG"; exit 1 ;;
      *)   echo "    !! 自检 HTTP ${SELF_CODE}，链路有问题，先别切"; exit 1 ;;
    esac
    echo "==> 改 Cursor BYOK 地址 -> $DELTA_URL"
    node "$FLIP" set "$DELTA_URL"
    echo
    echo "好了。开 Cursor，随便聊几轮。"
    echo "看省了多少：curl -s http://127.0.0.1:8788/metrics.json"
    if [[ -n "$CAPTURE_DIR" ]]; then
      echo
      echo "顺便在采真实 body（${CAPTURE_DIR}，上限 ${CAPTURE_MAX} 条，采够自停）。"
      echo "聊几轮之后跑一次，离线金样第 ⑨ 项就能从红转绿："
      echo "    node $HERE/tests/run.js"
    fi
    echo "要回退：$0 off"
    ;;
  off)
    pgrep -x Cursor >/dev/null && { echo "Cursor 还开着。先完全退出 Cursor（Cmd+Q）再跑这条。"; exit 1; }
    echo "==> Cursor BYOK 地址 -> $TODAY_URL"
    node "$FLIP" set "$TODAY_URL"
    echo "==> 停常驻小代理"
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "已回到今天的形态。"
    ;;
  status)
    echo "Cursor BYOK:"; node "$FLIP" get | sed 's/^/  /'
    echo
    echo -n "小代理: "; sidecar_alive && curl -s http://127.0.0.1:8788/healthz || echo "没活"
    echo
    echo -n "常驻: "; [[ -f "$PLIST" ]] && echo "已装 ($PLIST)" || echo "没装"
    echo -n "198 zk-delta: "; curl -fsS -m 8 https://cc.auto-link.com.cn/zkd/healthz || echo "打不通"
    echo
    if sidecar_alive; then echo "本轮统计:"; curl -s http://127.0.0.1:8788/metrics.json; echo; fi
    ;;
  *) echo "用法: $0 {on|off|status}"; exit 2 ;;
esac
