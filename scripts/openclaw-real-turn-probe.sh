#!/usr/bin/env bash
# openclaw-real-turn-probe.sh — 在 her 容器里用**真 OpenClaw 回合**打指定网关入口，判工具调用通不通。
#
# 用法（在 her 容器内跑，如 docker exec hermestest-13 bash /tmp/openclaw-real-turn-probe.sh ...）:
#   openclaw-real-turn-probe.sh <provider> <model> <baseUrl> [rounds] [message]
#   例: openclaw-real-turn-probe.sh carher-pro-chat gpt-6-astra http://10.68.13.198:30405/v1 4
#
# 为什么要真回合：合成 curl 只量得到网关；客户端对坏形状怎么反应（OpenClaw 2026.8.x 判 malformed、
# 旧版/Cursor 静默 {}）只有真客户端量得出（[[feedback_openclaw_agent_is_the_only_valid_toolcall_ruler]]）。
#
# 不碰在服务的实例：复制一份 runtime 配置到隔离 HOME，只改 <provider> 的 baseUrl，--local 跑嵌入式 agent，
# 不经 gateway、不投递飞书。跑完删掉隔离目录。
set -euo pipefail
PROVIDER=${1:?provider}; MODEL=${2:?model}; BASE=${3:?baseUrl}; ROUNDS=${4:-4}
MSG=${5:-"用 web_search 工具并行搜索这三个词：openclaw、litellm、kubernetes。三个搜索在同一轮里同时发起，不要串行。搜完用一句话汇总。"}
SRC=/data/.openclaw/openclaw.json
H=$(mktemp -d /tmp/ocprobe.XXXXXX); trap 'rm -rf "$H"' EXIT
mkdir -p "$H/.openclaw"
python3 - "$SRC" "$H/.openclaw/openclaw.json" "$PROVIDER" "$BASE" <<'PY'
import sys
src, dst, prov, base = sys.argv[1:]
s = open(src).read()
i = s.index('"%s": {' % prov)
j = s.index("baseUrl:", i)
k = s.index("\n", j)
assert j - i < 400, "baseUrl not inside the provider block"
open(dst, "w").write(s[:j] + 'baseUrl: "%s",' % base + s[k:])
PY
cp /data/.openclaw/base.json5 /data/.openclaw/docker.json5 "$H/.openclaw/" 2>/dev/null || true
ok=0; bad=0; other=0
for i in $(seq 1 "$ROUNDS"); do
  out=$(cd /tmp && HOME=$H OPENCLAW_HOME=$H/.openclaw OPENCLAW_CONFIG_PATH=$H/.openclaw/openclaw.json \
        timeout 300 openclaw agent --local --agent main --session-id "ocprobe-$(date +%s%N)" \
        --model "$PROVIDER/$MODEL" --timeout 280 --json -m "$MSG" 2>&1 || true)
  if grep -q "incomplete or malformed tool call" <<<"$out"; then r=MALFORMED; bad=$((bad+1))
  elif grep -q '"failures": 0' <<<"$out"; then r=OK; ok=$((ok+1))
  else r=OTHER; other=$((other+1)); fi
  echo "round $i $r $(grep -o -E '"calls": *[0-9]+' <<<"$out" | tail -1)"
done
echo "SUMMARY $BASE ok=$ok malformed=$bad other=$other"
