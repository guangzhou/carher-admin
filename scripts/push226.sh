#!/usr/bin/env bash
# push226.sh <localfile> <remotepath> — 经 jms ssh --tty 把文件推到 226,sha256 断言。
#
# 三条都是实测约束,别"优化"掉:
#   1. `jms scp` / 非 TTY exec channel 会卡死,只有 `--tty` 通
#      (见 memory feedback_jms_relay_dead_use_tty_kubeconfig_on_226)。
#   2. PTY 通道会**概率性丢字节 / 静默截断**,所以必须 sha256 断言;小文件不符就整段重传。
#   3. **单行 payload 太大会卡到十几分钟不回**:实测 142KB 的 .py(明文 b64 190KB / gz-b64 52KB)
#      单行推 5min+ 无响应;而 3000 字符一块、一块一个 session 的分块法是 aliyun-zerokey-join-pool.py
#      里跑通过的写法。所以 >CHUNK_THRESHOLD 就分块。
#      注意 session 才是成本单位(每块 ~10s),所以先 gzip 把块数压下来。
set -uo pipefail
LF="${1:?local file}"
RF="${2:?remote path}"
ASSET="${KVIA_ASSET:-k8s-work-226}"
CHUNK=3000
CHUNK_THRESHOLD=${CHUNK_THRESHOLD:-6000}
JMS="$(cd "$(dirname "$0")" && pwd)/jms"
[ -x "$JMS" ] || JMS="/Users/Liuguoxian/codes/carher-admin/scripts/jms"

WANT=$(shasum -a 256 "$LF" | cut -d' ' -f1)
JMSX(){ "$JMS" ssh --tty --timeout "$1" "$ASSET" "$2" 2>&1; }
# 读远端 sha:**空 ≠ 不符**。这条链路丢的常常是回显那一行,空值只说明"没读到",
# 必须重读而不是判失败 —— 2026-08-04 踩过:一次空读被当成内容不符,白重传一整轮(~13min)。
# 只有"读到了且值不同"才算真不符。
remote_sha(){
  local i got
  for i in 1 2 3; do
    got=$(JMSX 180 "echo GOT=\$(sha256sum $RF 2>/dev/null | cut -c1-64)" \
      | grep -oE 'GOT=[0-9a-f]{64}' | tail -1 | cut -d= -f2)
    [ -n "$got" ] && { echo "$got"; return 0; }
  done
  echo ""   # 三次都读不到,交给调用方决定(当前实现:视作未知,不触发重传)
}

# 先回查:远端已经是这份内容就一次 session 收工(链路塌陷时这一步能省十几分钟)。
PRE=$(remote_sha)
[ "$PRE" = "$WANT" ] && { echo "  ✓ $RF 已是目标内容 sha=${WANT:0:12}(跳过上传)"; exit 0; }

B=$(gzip -9 -c "$LF" | base64 | tr -d '\n')
echo "  [push] $(basename "$LF") $(wc -c < "$LF" | tr -d ' ')B → b64(gz) ${#B}B → $RF"

if [ "${#B}" -le "$CHUNK_THRESHOLD" ]; then
  for try in 1 2 3 4 5; do
    OUT=$("$JMS" ssh --tty --timeout 300 "$ASSET" \
      "printf '%s' '$B' | base64 -d | gunzip > $RF 2>/dev/null; echo GOT=\$(sha256sum $RF | cut -c1-64)" 2>&1)
    GOT=$(echo "$OUT" | grep -oE 'GOT=[0-9a-f]{64}' | tail -1 | cut -d= -f2)
    [ "$GOT" = "$WANT" ] && { echo "  ✓ pushed $RF sha=${WANT:0:12} (try $try)"; exit 0; }
    echo "  ⚠ sha 不符 (try $try) got=${GOT:0:12} want=${WANT:0:12} — 整段重传"
  done
  echo "  ❌ push $RF 5 次仍不符 — 别当成功往下走"; exit 1
fi

# 分块:盲发,不逐块回读。
# ⚠ 为什么不逐块校验长度:回读方向才是丢字节的那一侧 —— 2026-08-04 实测每次失败都恒为
# "远端 0",从没出现过部分长度,说明丢的是 `LEN=` 那行回显而不是数据本身;据此重发/截断
# 只是白烧 session,最后还会把本来传成功的块判成失败。**唯一可信的判据是解压后的 sha256**。
TOTAL=$(( (${#B} + CHUNK - 1) / CHUNK ))
for round in 1 2 3; do
  echo "  分块上传(第 $round 轮):$TOTAL 块 × ${CHUNK}B"
  i=0; off=0; SENT=1
  while [ "$off" -lt "${#B}" ]; do
    i=$((i + 1))
    C="${B:$off:$CHUNK}"
    REDIR=">>"; [ "$i" = 1 ] && REDIR=">"
    if ! "$JMS" ssh --tty --timeout 240 "$ASSET" "printf %s '$C' $REDIR $RF.b64" >/dev/null 2>&1; then
      echo "    块 $i/$TOTAL 发送异常(继续,末尾以 sha 为准)"
    fi
    echo "    · $i/$TOTAL"
    off=$((off + ${#C}))
  done
  [ "$SENT" = 1 ] || continue
  "$JMS" ssh --tty --timeout 240 "$ASSET" "base64 -d $RF.b64 2>/dev/null | gunzip > $RF 2>/dev/null; rm -f $RF.b64" >/dev/null 2>&1
  GOT=$(remote_sha)
  [ "$GOT" = "$WANT" ] && { echo "  ✓ pushed $RF sha=${WANT:0:12} ($TOTAL 块, 第 $round 轮)"; exit 0; }
  if [ -z "$GOT" ]; then
    echo "  ⚠ 第 $round 轮 sha 读不到(三次都空) —— 不能据此判失败,也不能判成功;下一轮前先再读一次"
    sleep 20
    GOT=$(remote_sha)
    [ "$GOT" = "$WANT" ] && { echo "  ✓ pushed $RF sha=${WANT:0:12}(补读确认)"; exit 0; }
  fi
  echo "  ⚠ 第 $round 轮解压后 sha 不符 got=${GOT:0:12} want=${WANT:0:12} — 整份重传"
  "$JMS" ssh --tty --timeout 120 "$ASSET" "rm -f $RF.b64 $RF" >/dev/null 2>&1
done
echo "  ❌ push $RF 3 轮仍不符 —— 链路当前不可用,别当成功往下走"
exit 1
