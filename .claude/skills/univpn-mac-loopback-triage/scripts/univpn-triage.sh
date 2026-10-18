#!/usr/bin/env bash
# UniVPN (macOS) 「接收返回码超时」诊断量具
#
# 2026-10-10 实证：该报错 99% 不是网关/网络问题，而是 UniVPNCS 崩溃时
# 把系统回环路由 127/8 -> lo0 一起删掉，导致 UI 连不上本机 RPC 29191。
#
# 本脚本只读不写，可反复跑。退出码：
#   0 = 一切正常（隧道已连或可连）
#   1 = 回环路由损坏（根因，需 root 修复）
#   2 = 回环正常但 UniVPNCS 未运行
#   3 = 其它（网关不可达等）
set -uo pipefail

VPN_DIR="$HOME/.UniVPN"
LOG_DIR="$VPN_DIR/log"
RPC_PORT=29191

say() { printf '%s\n' "$*"; }
hr()  { printf -- '--- %s ---\n' "$*"; }

rc=0

hr "① 回环路由（最高优先级：这是 2026-10-10 的真因）"
# 正常 macOS 必须有 127.0.0.1 -> lo0 的 UH 路由。缺失则 127.0.0.1 会去匹配
# default，被克隆成走 en0 的假路由（flags 带 WASCLONED），一切本机连接全废。
lo_route="$(netstat -rn -f inet 2>/dev/null | grep -E '^127' || true)"
if [ -z "$lo_route" ]; then
  say "✗ 路由表里完全没有 127 网段 —— 回环已损坏"
  rc=1
else
  say "$lo_route"
fi

iface="$(route -n get 127.0.0.1 2>/dev/null | awk '/interface:/{print $2}')"
flags="$(route -n get 127.0.0.1 2>/dev/null | awk '/flags:/{print $2}')"
say "127.0.0.1 当前出口接口 = ${iface:-未知}   flags=${flags:-未知}"
case "$iface" in
  lo0) say "✓ 回环走 lo0，正常" ;;
  "")  say "✗ 取不到接口" ; rc=1 ;;
  *)   say "✗ 回环被指到 ${iface}（应为 lo0）—— 回环已损坏" ; rc=1 ;;
esac
# WASCLONED 是「这条路由是从 default 克隆出来的」的铁证，不是真实写入的路由，
# 所以 `route delete -host 127.0.0.1 <gw>` 必然报 not in table —— 别浪费时间删它。
case "${flags:-}" in
  *WASCLONED*) say "  （flags 含 WASCLONED：此路由是 default 的克隆，delete 会报 not in table）" ;;
esac

hr "② 回环实测（不信路由表，直接连）"
# 阳性对照：随便一个端口自连。失败信息是 "Can't assign requested address"
# 而不是 "Connection refused" —— 前者=系统分配不出本地端口=回环坏，后者=没人监听。
python3 - <<'PY' 2>/dev/null || say "✗ 回环自连失败（python3 探针）"
import socket, threading, time, sys
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(('127.0.0.1', 39394)); s.listen(1)
except Exception as e:
    print('✗ bind 127.0.0.1 失败:', e); sys.exit(1)
threading.Thread(target=lambda: s.accept(), daemon=True).start()
time.sleep(0.3)
c = socket.socket(); c.settimeout(3)
try:
    c.connect(('127.0.0.1', 39394)); print('✓ 回环自连通（任意端口对照组）')
except Exception as e:
    print('✗ 回环自连失败:', e)
    print('  Errno 49 / Can\'t assign requested address = 回环路由坏，不是防火墙')
    sys.exit(1)
PY
[ $? -ne 0 ] && rc=1

hr "③ UniVPN 三个进程"
# UI 负责界面；PromoteService 监听 29191 做提权 RPC；UniVPNCS 才是真正建隧道的。
# 只有 UI+Promote 而没有 CS = 典型的「接收返回码超时」现场。
for p in "/Applications/UniVPN.app/Contents/MacOS/UniVPN" UniVPNPromoteService UniVPNCS; do
  pid="$(pgrep -f "$p" 2>/dev/null | head -1)"
  if [ -n "$pid" ]; then
    printf '%-22s pid %s\n' "$(basename "$p")" "$pid"
  else
    printf '%-22s 未运行\n' "$(basename "$p")"
  fi
done
pgrep -f UniVPNCS >/dev/null 2>&1 || { say "✗ UniVPNCS 未运行 —— UI 等不到回码就报超时"; [ $rc -eq 0 ] && rc=2; }

hr "④ 本机 RPC 29191（UI 和 PromoteService 的唯一通道）"
listener="$(lsof -nP -iTCP:"$RPC_PORT" 2>/dev/null | tail -n +2)"
say "${listener:-（无人监听 ${RPC_PORT}）}"
if nc -z -G 3 127.0.0.1 "$RPC_PORT" 2>/dev/null; then
  say "✓ 能连上 127.0.0.1:$RPC_PORT"
else
  say "✗ 连不上 127.0.0.1:${RPC_PORT}（若 ① 已报损坏，这是其后果而非独立故障）"
fi

hr "⑤ 隧道是否已建立（utun 带 IPv4 才算通）"
up=0
for i in $(ifconfig -l 2>/dev/null | tr ' ' '\n' | grep '^utun'); do
  a="$(ifconfig "$i" 2>/dev/null | awk '/inet /{print $2}')"
  [ -n "$a" ] && { say "✓ $i -> $a"; up=1; }
done
[ $up -eq 0 ] && say "（没有带 IPv4 的 utun：隧道未建立。注意 utun0-3 是系统自带的，光看 utun 存在不算通）"

hr "⑥ 配置里的网关 + 实测可达性"
for f in "$VPN_DIR"/config/*.ini; do
  [ -e "$f" ] || continue
  host="$(awk -F' *= *' '/^GatewayAddress/{print $2}' "$f" | tr -d '\r')"
  port="$(awk -F' *= *' '/^GatewayPort/{print $2}'    "$f" | tr -d '\r')"
  mode="$(awk -F' *= *' '/^TunnelMode/{print $2}'     "$f" | tr -d '\r')"
  [ -n "$host" ] || continue
  printf '%-24s %s:%s (TunnelMode=%s)  ' "$(basename "$f")" "$host" "$port" "$mode"
  if nc -z -G 6 "$host" "$port" 2>/dev/null; then say "TCP OPEN"; else say "不可达"; [ $rc -eq 0 ] && rc=3; fi
done

hr "⑦ 客户端自己的日志（比 unified log 有用得多）"
# 真因几乎总在这两个文件里。UI 日志说「连不上 RPC」，CS 日志说崩在哪。
say "日志目录: $LOG_DIR"
ui="$(ls -t "$LOG_DIR"/UniVPN_UniVPNUI_*.log 2>/dev/null | head -1)"
cs="$(ls -t "$LOG_DIR"/UniVPN_UniVPNCS_*.log 2>/dev/null | head -1)"
if [ -n "$ui" ]; then
  say "最新 UI 日志: $(basename "$ui")"
  grep -E "can't connect RPC|RPC connection failed|Cnem succed|GetLogined" "$ui" 2>/dev/null \
    | tail -4 | sed 's/\r$//' | sed 's/^/    /'
fi
if [ -n "$cs" ]; then
  say "最新 CS 日志: $(basename "$cs")"
  grep -E "SSL Connect failed|Ensure exit route|Restore Route|Delete default route" "$cs" 2>/dev/null \
    | tail -4 | sed 's/\r$//' | sed 's/^/    /'
fi

hr "⑧ 崩溃报告"
ls -t ~/Library/Logs/DiagnosticReports/UniVPNCS-*.ips 2>/dev/null | head -3 | sed 's/^/    /' \
  || say "（无 UniVPNCS 崩溃报告）"

say ""
hr "判定"
case $rc in
  0) say "✓ 回环正常、CS 在跑。若仍报超时，看 ⑦ 的 CS 日志尾部。" ;;
  1) say "✗ 根因=回环路由损坏。修复（需 root，可逆）："
     say "    sudo route -n add -net 127.0.0.0 -netmask 255.0.0.0 127.0.0.1"
     say "  或让系统自己重建（更稳）："
     say "    sudo ifconfig lo0 down && sudo ifconfig lo0 up"
     say "  然后完全退出 UniVPN 再打开。" ;;
  2) say "✗ 回环正常但 UniVPNCS 没起来。完全退出 UniVPN（三个进程都杀）再打开："
     say "    pkill -f UniVPN" ;;
  3) say "✗ 回环和进程都正常，但网关不可达。换另一条线路（见 ⑥ 的 TunnelMode）。" ;;
esac
exit $rc
