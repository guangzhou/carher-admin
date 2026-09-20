#!/usr/bin/env bash
# 用户判断: 病在 toolcall,照 grok 那条腿改造。
# 数据支撑(不是顺着说): 12:06:41 / 12:11:42 两发真实失败的报错原文是
#   MidStreamFallbackError: Cursor AgentService requested an unsupported IDE tool
# 就在 9router/Cursor 这条腿上。grok 走 sub2api→api.x.ai 普通 OpenAI 兼容通道,
# 没有 Cursor 自带工具这套协议,所以一直绿。
#
# 本轮只读,目的是定改造范围,三件事:
#   1) 现在线上 bundle 到底 decline 了哪几个通道(skill 里记了 native/web_search/fetch 三条,
#      但那是文档,必须看运行时 bundle 里真有没有 —— 铁律1: 改 open-sse 要重建镜像,
#      文档说改了不等于 bundle 里有)
#   2) 日志里有没有 "Unhandled interaction_query fields=[...]" —— 那才是还没接住的新 field,
#      按 skill: 没数据不预造,只对日志真出现过的 field 动手
#   3) 那个 402 卡在哪。不修好它,改完也没法验收
set -uo pipefail
NS=litellm-product
P9=$(sudo kubectl -n $NS get pod -l app=9router -o jsonpath='{.items[0].metadata.name}')
POD=$(sudo kubectl -n $NS get pod -l app=litellm-proxy -o jsonpath='{.items[0].metadata.name}')
echo "9router pod = $P9"
echo "litellm pod = $POD"

echo
echo "=== 1) 运行时 bundle 里现存的 decline 能力(判据=chunk 里的字符串字面量) ==="
sudo kubectl -n $NS exec -i $P9 -- bash -c '
CH=$(ls /app/.next/server/chunks/*.js 2>/dev/null | tr "\n" " ")
for M in NATIVE_TOOL_DECLINE createWebSearchDeclineResponse createFetchDeclineResponse \
         isNativeToolArg encodeNativeToolDecline Unhandled; do
  N=$(grep -lF "$M" $CH 2>/dev/null | wc -l | tr -d " ")
  if [ "$N" != "0" ]; then echo "  有   $M  (命中 $N 个 chunk)"; else echo "  没有 $M"; fi
done
echo "  --- image ---"
' < /dev/null 2>&1 | grep -v '^\[sudo\]'
sudo kubectl -n $NS get deploy 9router \
  -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}' < /dev/null

echo
echo "=== 2) 9router 日志:未接住的 interaction_query field / unsupported tool ==="
sudo kubectl -n $NS logs $P9 --tail=3000 2>/dev/null \
  | grep -iE 'unhandled|unsupported|interaction_query|native|decline|CURSOR AGENT' \
  | sed 's/\(.\{200\}\).*/\1../' | sort | uniq -c | sort -rn | head -25
echo "  (空 = 日志里没有,可能 CURSOR_STREAM_DEBUG 没开)"
sudo kubectl -n $NS get deploy 9router \
  -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}' < /dev/null \
  | grep -iE 'debug|stream' || echo "  CURSOR_STREAM_DEBUG 未设置"

echo
echo "=== 3) 402 卡在哪:LiteLLM 侧这两个 group 的最近失败原文 ==="
sudo kubectl -n $NS logs $POD --tail=1500 2>/dev/null \
  | grep -iE '402|fable|opus-5|9router' \
  | sed 's/\(.\{240\}\).*/\1../' | tail -15

echo
echo "=== 4) 对照:grok 那条腿的 litellm_params 形状(看它凭什么不碰这套协议) ==="
sudo kubectl -n $NS exec -i litellm-db-0 -- psql -U litellm -d litellm -c \
"select model_name, model_id,
        litellm_params->>'model' as lp_model,
        litellm_params->>'api_base' as api_base
 from \"LiteLLM_ProxyModelTable\"
 where model_name in ('claude-grok-4.6','claude-fable-5.1','claude-opus-5')
 order by model_name;" < /dev/null 2>&1 | grep -v '^\[sudo\]'
