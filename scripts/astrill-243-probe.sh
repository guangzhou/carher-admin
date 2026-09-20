#!/bin/sh
# astrill-243-probe.sh —— 判"某台 Astrill 服务器连得上吗"的**唯一可信量具**：抓包数入向包。
#
# 🔴 为什么必须抓包，而不是看 tun0 / ps / curl：
#   - `ps` 里的 `--port 1` 是参数回显，**不是真实行为**（实测真打的是 35700、14890 这类随机口）
#   - 容器里 `curl` 不走隧道，出口 IP 永远读成宿主的 172.235.204.67
#   - 243 宿主网络**劫持所有出站 TCP**，`/dev/tcp` 和 `curl telnet://` 对任何 IP:PORT 都报 open
#     ⇒ 在 243 上做的任何 TCP 连通性判断都是假绿
#   tcpdump 的**入向包数**不受这些影响：服务器回了就是回了。
#
# 判据：
#   入向 > 0  ⇒ 服务器在应答（能不能建成隧道是下一层问题）
#   入向 = 0  ⇒ 服务器对本账号完全沉默
#
# 用法: ASTRILL243_PW=xxx astrill-243-probe.sh <container> "<server name>" [目标IP]
#
# ⚠️ 只读诊断：不改配置、不删文件。抓包落在容器 /tmp/probe-*-lgx.pcap（下次运行覆盖）。
#    建议只在闲置容器（astrill-4 / astrill-5）上跑。
set -e
LX="$(dirname "$0")/astrill-243-lx.sh"
C="${1:?用法: $0 <container> \"<server name>\" [目标IP]}"
SRV="${2:?缺服务器名}"
WANT="$3"
TAG=$(echo "$SRV" | tr -cd 'A-Za-z0-9' | cut -c1-16)
PCAP="/tmp/probe-$TAG-lgx.pcap"

"$LX" "$C" "
  rm -f $PCAP
  timeout 55 tcpdump -i eth0 -n -s 300 -w $PCAP 'not port 22' >/dev/null 2>&1 &
  sleep 2
  export DISPLAY=:99
  xdotool mousemove 525 192 click 1; sleep 3
  xdotool key Escape; sleep 1
  xdotool mousemove 616 264 click 1; sleep 2
  xdotool mousemove 615 495 click 1; sleep 2
  xdotool mousemove 515 264 click 1; sleep 1
  i=0; while [ \$i -lt 45 ]; do xdotool key BackSpace; i=\$((i+1)); done
  xdotool type --delay 25 '$SRV'
  sleep 2
  xdotool mousemove 515 291 click 1; sleep 1
  xdotool mousemove 525 192 click 1
  sleep 50
"

echo "=== $SRV"
"$LX" "$C" "
  echo -n '  asovpnc : '; ps -eo args | grep [a]sovpnc | grep -oE -- '--remote [0-9.]+ --port [0-9-]+' | head -1
  echo -n '  tun0    : '; ip -4 a show tun0 2>/dev/null | grep -o 'inet [0-9.]*' || echo 无
  echo    '  出向对端:'
  tcpdump -r $PCAP -nn 2>/dev/null | grep -oE '> [0-9]+\.[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' | sort | uniq -c | sort -rn | head -6 | sed 's/^/    /'
  echo    '  入向对端:'
  tcpdump -r $PCAP -nn 2>/dev/null | grep -oE 'IP [0-9]+\.[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+ >' | sort | uniq -c | sort -rn | head -6 | sed 's/^/    /'
"
if [ -n "$WANT" ]; then
  IN=$("$LX" "$C" "tcpdump -r $PCAP -nn 2>/dev/null | grep -c 'IP $WANT'")
  echo "  --- 来自 $WANT 的入向包: ${IN:-0}"
  [ "${IN:-0}" -gt 0 ] && echo "  ✅ 服务器在应答" || echo "  ❌ 服务器完全沉默"
fi
"$LX" "$C" 'export DISPLAY=:99; xdotool mousemove 525 192 click 1'
echo "  （已复位 OFF）"
