#!/bin/sh
# astrill-243-switch.sh —— 在 243 的某个 Astrill 容器里切换出口服务器。
#
# 实测耗时 ~26s/次（2026-09-13，astrill-4 三台 Private 全部成功，含落点核对）。
#
# 🔑 三个必须遵守的点，每一个都是踩坑换来的：
#   1) 必须先点 OFF。Astrill 在 ON 态下换服务器**不会重连**，不 OFF 就是在测旧服务器。
#   2) 必须点 All 标签页 (615,495)。下拉框默认落在 Recommended 页，
#      搜公共服务器名会 "Nothing found"，而且**不报错**——静默连回原来那台。
#   3) 搜索框 ctrl+a 不清空（是追加），必须连按 BackSpace。
#
# 🔴 判据只认 tun0，不认 curl。容器里 curl 不走隧道，出口 IP 读出来永远是宿主的
#    172.235.204.67，会把"连上了"误判成"没通"。
#
# 用法: ASTRILL243_PW=xxx astrill-243-switch.sh <container> "<server name>"
# 例:   ASTRILL243_PW=xxx astrill-243-switch.sh astrill-4 "Seattle Supercharged 1 (Private)"
#
# ⚠️ 只读切换：不改配置、不删文件。失败不会留下半开状态（超时后容器停在 ON 但无 tun0）。
#
# 注意：**故意不用 `set -e`** —— 轮询 tun0 时 grep 无匹配会返回非 0，
# 那是"还没连上，继续等"的正常中间态，不是错误。用 `set -e` 会静默退出、什么都不打印。
LX="$(dirname "$0")/astrill-243-lx.sh"
C="${1:?用法: $0 <container> \"<server name>\"}"
SRV="${2:?缺服务器名}"

T0=$(date +%s)
ATTEMPT=0
TUN=""
MAX=3
while [ $ATTEMPT -lt $MAX ] && [ -z "$TUN" ]; do
  ATTEMPT=$((ATTEMPT + 1))
  # 🔴 每次尝试前先把 GUI 彻底压回 OFF 并清干净残留进程。
  # 实测：不做这一步时，**单次切换经常不生效**（搜 Seattle 结果仍连着上一台 Los Angeles，
  # 且开关停在 OFF）。连续批量切换之所以看起来好用，是因为前一轮的收尾恰好起了这个作用。
  "$LX" "$C" "
    export DISPLAY=:99
    xdotool mousemove 525 192 click 1; sleep 4
    pkill -x asovpnc; pkill -x asovpnc.real; pkill -x asproxy
    sleep 2
  " >/dev/null 2>&1 || true

  "$LX" "$C" "
    export DISPLAY=:99
    xdotool key Escape; sleep 1
    xdotool mousemove 616 264 click 1; sleep 3      # 服务器下拉箭头
    xdotool mousemove 615 495 click 1; sleep 3      # All 标签页（见要点2）
    xdotool mousemove 515 264 click 1; sleep 1      # 搜索框
    i=0; while [ \$i -lt 45 ]; do xdotool key BackSpace; i=\$((i+1)); done   # 见要点3
    xdotool type --delay 25 '$SRV'
    sleep 3
    xdotool mousemove 515 291 click 1; sleep 2      # 第一行结果
    xdotool mousemove 525 192 click 1               # ON
  "

  i=0
  while [ $i -lt 40 ]; do
    sleep 2
    TUN=$("$LX" "$C" "ip -4 a show tun0 2>/dev/null | grep -o 'inet [0-9.]*'" || true)
    [ -n "$TUN" ] && break
    i=$((i + 2))
  done
  [ -z "$TUN" ] && [ $ATTEMPT -lt $MAX ] && echo "  第 $ATTEMPT/$MAX 次未通，重试" >&2
done
T1=$(date +%s)
RMT=$("$LX" "$C" "ps -eo args | grep [a]sovpnc | grep -oE -- '--remote [0-9.]+ --port [0-9-]+' | head -1" || true)
TUN=$(echo "$TUN" | tr -d '\n' | sed 's/ *$//')

printf '%-42s %-34s tun=[%s] 耗时=%ss\n' "$SRV" "${RMT:-?}" "${TUN:-未通}" "$((T1 - T0))"
[ -n "$TUN" ] || { echo "❌ 未通。核对：服务器名是否在 All 页存在；是否是 200 台公共服务器之一（对本账号集体沉默）" >&2; exit 1; }

# 🔑 连上了 ≠ 连对了。GUI 选行有时序竞争，实测出现过"搜 Seattle 结果选中 Los Angeles"。
# 三台 Private 的 IP 是已知的，用 --remote 反查确认落点，对不上就明说，不许假绿。
EXPECT=""
case "$SRV" in
  *"San Jose"*)    EXPECT=104.168.13.250 ;;
  *"Seattle"*)     EXPECT=38.246.151.75  ;;
  *"Los Angeles"*) EXPECT=104.129.16.172 ;;
esac
if [ -n "$EXPECT" ]; then
  case "$RMT" in
    *"$EXPECT"*) echo "  ✅ 落点核对通过（$EXPECT）" ;;
    *) echo "  ⚠️ 落点不符：期望 $EXPECT，实际 $RMT —— GUI 选错行了，重跑一次" >&2; exit 1 ;;
  esac
fi
