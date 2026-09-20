#!/usr/bin/env bash
# 上一轮定论: cursor-fc-fable-5.1 只有 1 条腿(9router/claude-fable-5-1-medium)。
# 12:02~12:06 那 5 次失败是 RouterRateLimitError —— 唯一那条腿在 cooldown 里,
# 无腿可挑,LiteLLM 把组名原样写进 model 列。所以"坏腿"不存在,要查的是
# **什么把腿打进了 cooldown**。
#
# 假设: 腿本身在更早的时刻连续失败(unsupported IDE tool / MidStreamFallbackError),
#       达到 allowed_fails 后被 cooldown 60s,期间后续请求直接 RouterRateLimitError。
# 证伪条件: 如果 cooldown 之前那条腿上没有失败行,则假设错,cooldown 来自别处
#           (例如上游 429/5xx 计入、或 pre-call-check 判定)。
# 数据: 取落在真实腿上(model=openai/cu/claude-fable-5-1-medium)的行,
#       按时间列出 status + 异常类,看 12:02 之前有没有连续红。
set -uo pipefail
NS=litellm-product
KEY=a538f674b895678f417eea9e277f75a24bf94333f388b96b0546167a6d779bb7

echo "=== 1) carher-1 这把 key 今天所有请求的时序(含状态与异常类) ==="
sudo kubectl -n $NS exec -i litellm-db-0 -- psql -U litellm -d litellm -A -F'|' -t -c \
"select to_char(\"startTime\",'HH24:MI:SS') as t,
        \"model_group\",
        \"model\",
        coalesce(metadata->>'status','?') as st,
        coalesce(metadata->'error_information'->>'error_class','-') as cls
 from \"LiteLLM_SpendLogs\"
 where \"api_key\"='$KEY' and \"startTime\" > now() - interval '18 hours'
 order by \"startTime\";" < /dev/null 2>&1 | grep -v '^\[sudo\]'

echo
echo "=== 2) 落在真实腿上的失败行,取异常消息(不是 traceback) ==="
sudo kubectl -n $NS exec -i litellm-db-0 -- psql -U litellm -d litellm -A -t -c \
"select to_char(\"startTime\",'HH24:MI:SS') || '  ' ||
        coalesce(metadata->'error_information'->>'error_class','-') || E'\n    ' ||
        left(coalesce(metadata->'error_information'->>'error_message','-'), 400)
 from \"LiteLLM_SpendLogs\"
 where \"model\"='openai/cu/claude-fable-5-1-medium'
   and coalesce(metadata->>'status','') <> 'success'
   and \"startTime\" > now() - interval '18 hours'
 order by \"startTime\" desc limit 6;" < /dev/null 2>&1 | grep -v '^\[sudo\]'

echo
echo "=== 3) 当前 router cooldown 相关设置(判 60s / allowed_fails 是哪来的) ==="
sudo kubectl -n $NS exec -i litellm-db-0 -- psql -U litellm -d litellm -A -t -c \
"select jsonb_pretty(param_value)
 from \"LiteLLM_Config\" where param_name='router_settings';" < /dev/null 2>&1 \
 | grep -v '^\[sudo\]' | grep -iE 'cooldown|allowed_fails|num_retries|fallback' | head -20

echo
echo "=== 4) 这两个组现在还有没有 fallback 链 ==="
sudo kubectl -n $NS exec -i litellm-db-0 -- psql -U litellm -d litellm -A -t -c \
"select jsonb_pretty(param_value->'fallbacks')
 from \"LiteLLM_Config\" where param_name='router_settings';" < /dev/null 2>&1 \
 | grep -v '^\[sudo\]' | grep -A3 -iE 'cursor-fc-fable|cursor-fc-opus' | head -20
echo "(上面为空 = 这两个组没有 fallback,单腿裸奔)"
