#!/bin/sh
# hermestest-14「发消息没反应」分诊梯 —— 回答"我刚发的那条到底有没有到容器"。
#
# ★ 第一原则:**容器路(`openclaw agent` CLI)和用户路(飞书 WS 长连接)是两条不同的车道。**
#   反代腿验收全绿 ≠ 用户在飞书里发的消息能收到。反过来也一样。别拿一条的绿去判另一条。
#
# ⛔ 坏尺子(这三样在"消息完全不到"时照样绿,零判别力):
#   - docker ps 的 `healthy`
#   - RESTARTS=0
#   - 网关进程 PID 的 uptime
#   容器自己的探针看不见"WS 活着但不投递"。
#
# ✅ 唯一判据:**时间轴**。最后一条 `received from` 的时刻 vs 你按下发送的时刻。
#   比这个更早 ⇒ 那条消息根本没到容器,后面所有"模型/提示词"的猜测都是浪费。
#
# 已知的三种"没反应"是完全不同的病,先用本脚本分开:
#   A) 群里没 @ 机器人  → 日志 `did not mention bot, recording to history` → `rejected: no bot mention`
#      **这是设计如此,不是故障。** 私聊(p2p)不需要 @。
#   B) WS 停滞/积压     → 日志 `message om_... expired, discarding`(事件到达时已过有效期)
#      典型形态:先是若干 expired,然后彻底安静,没有 disconnect/reconnect、没有 error。
#   C) 真在处理但很慢   → 有 `received from`,有 session 在 processing,只是还没回。
#
# 用法(在 188 上执行):
#   ./h14-feishu-delivery-triage.sh              # 全量分诊
#   MINUTES=10 ./h14-feishu-delivery-triage.sh   # 只看最近 10 分钟
#
# ⚠️ 时区陷阱:hermestest-14 的日志是**北京时间**,而 9router pod 里是 **UTC(+8 小时差)**。
#    跨这两份日志拼时间轴之前,先 `kubectl exec <pod> -- date +%z` 确认,否则会把
#    9router 的 `[00:10]` 当成半夜、实际是早上 08:10。
#
# ⚠️ 重启网关会重建 WS,大概率"治好"B —— 但它同时**销毁证据**并**打断正在跑的 session**。
#    顺序永远是:先让用户在**私聊**里发一句、这里盯 30s(零风险,能把 B 和 A 分开),
#    再决定要不要重启。

set -eu
CONTAINER="${CONTAINER:-hermestest-14}"
MINUTES="${MINUTES:-15}"
DAY="$(date +%Y-%m-%d)"
LOG="${LOG:-/tmp/openclaw/openclaw-${DAY}.log}"
# 日志是 JSON 行,只取时间 + message 前 150 字,不然一屏被 payload 冲掉
DEC="s/.*\"time\":\"([^\"]{19}).*\"message\":\"([^\"]{0,150}).*/\1 | \2/"

x() { docker exec "$CONTAINER" sh -c "$1" 2>&1 || true; }

echo "NOW=$(date +%Y-%m-%dT%H:%M:%S)"
echo "CONTAINER=$CONTAINER  LOG=$LOG"
echo
echo "== 0. 容器面(参考,不是判据) =="
docker ps --filter "name=$CONTAINER" --format 'STATE={{.State}} STATUS={{.Status}}' || true
# `ps -C` 在容器的 busybox ps 上不存在(会吐空表头 ⇒ 看着像"进程没了"),用 grep 兜
x "ps -o pid,etime,args 2>/dev/null | grep -i '[o]penclaw' | head -5 || ps aux 2>/dev/null | grep -i '[o]penclaw' | head -5"

echo
echo "== 1. ★判据★ 最后一条真实入站 vs 现在 =="
LAST="$(x "grep -a 'received from' '$LOG' | tail -1 | sed -E '$DEC'")"
echo "LAST_INBOUND: ${LAST:-<今天一条入站都没有>}"
echo "  ↑ 如果这个时刻早于你按下发送的时刻 ⇒ 消息没到容器,病在 WS(B),不在模型。"

echo
echo "== 2. 最近 ${MINUTES} 分钟有没有任何入站 =="
CUT="$(date -d "-${MINUTES} min" +%H:%M:%S 2>/dev/null || date -v-${MINUTES}M +%H:%M:%S)"
echo "CUTOFF=$CUT"
x "grep -a 'received from' '$LOG' | sed -E '$DEC' | awk -v c=\"$CUT\" 'substr(\$0,12,8)>=c'" | tail -20
echo "RECENT_INBOUND_COUNT=$(x "grep -ac 'received from' '$LOG' " )  # 今日累计"

echo
echo "== 3. B 的签名:事件过期丢弃(WS 停滞/积压) =="
E="$(x "grep -a 'expired, discarding' '$LOG'")"
echo "EXPIRED_COUNT=$(printf '%s\n' "$E" | grep -c 'expired' || true)"
printf '%s\n' "$E" | sed -E "$DEC" | head -15
echo "  ↑ 非 0 = 事件到达时已过有效期 ⇒ 长连接停滞过。这是最强的 B 证据。"

echo
echo "== 4. A 的签名:群里没 @ 机器人(设计如此,不是故障) =="
echo "NO_MENTION_COUNT=$(x "grep -ac 'did not mention bot' '$LOG'")"
echo "REJECTED_COUNT=$(x "grep -ac 'no bot mention' '$LOG'")"
x "grep -a 'did not mention bot' '$LOG' | sed -E '$DEC' | tail -5"

echo
echo "== 5. 分车道计数(飞书 vs cron;别把 cron 的错读成飞书的病) =="
# ⚠️ 日志里飞书那条车道写作 `feishu[default]`,**不是** `channel=feishu`。
#    我第一版按 `channel=feishu` 数,在明明有 72 条入站时读出 0 —— 典型的坏尺子。
echo "feishu_lines=$(x "grep -ac 'feishu\[' '$LOG'")"
echo "cron_lines=$(x "grep -ac 'cron' '$LOG'")"

echo
echo "== 6. 按会话分入站(p2p 不需要 @;群需要) =="
x "grep -ao 'oc_[0-9a-f]\{32\}' '$LOG' | sort | uniq -c | sort -rn | head -8"

echo
echo "== 7. WS 连接事件(有没有 disconnect/reconnect;全空 = 悄悄停了) =="
x "grep -aiE 'websocket|long ?conn|disconnect|reconnect' '$LOG' | sed -E '$DEC' | tail -12"

echo
echo "== 8. C 的签名:有没有 session 卡在 processing =="
x "grep -a 'processing' '$LOG' | sed -E '$DEC' | tail -8"

echo
echo "== 9. 绝对最后几行(不过滤,看它最后在干什么) =="
x "tail -12 '$LOG' | sed -E '$DEC'"

echo
echo "== 分诊结论怎么读 =="
cat <<'EOF'
  §3 EXPIRED_COUNT>0 且 §2 最近无入站   ⇒ B:WS 停滞。下一步=让用户在**私聊**发一句并盯 30s。
                                            仍无 `received from` ⇒ 重启网关(先说清会打断 §8 的 session)。
  §2 有入站 但 §4 命中该会话            ⇒ A:群里没 @。让用户 @ 一下机器人,或改私聊。不是故障。
  §2 有入站 且 §8 有 processing          ⇒ C:在跑。等,或去 9router 侧看 trace-9router.sh。
  §1 时刻晚于发送时刻 且 §8 空 且无回复  ⇒ 消息到了但没产出 ⇒ 这时才轮到查模型/反代腿。
  ⚠️ 未定性就别下结论:本脚本区分不出 WS 是"断开"还是"活着但静默"(两者都没有 error 行)。
EOF
