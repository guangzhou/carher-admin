#!/usr/bin/env bash
# 上一轮把我自己的假设证伪了: cooldown 在 12:00 就已生效,而 carher-1 这把 key 上
# 第一次腿失败是 12:06。cooldown 按 deployment id 记、全 router 共享、不分 key,
# 所以打进 cooldown 的失败来自**别的 key**。
#
# 假设: 11:50~12:06 有别的 key 在 9router/claude-fable-5-1-medium 上连续失败 >=3 次。
# 证伪条件: 如果这个窗口里该腿上除 carher-1 外没有失败行,假设错,cooldown 另有来源
#           (例如 pre-call-check、或 9router 侧 5xx 未落 SpendLogs)。
# 数据: 按 key 分组列出该腿 11:30~12:15 的全部行与异常类。
#
# 注: 判失败不许用 metadata->>'status' <> 'success' —— 大量行 status 为 null,
#     会把成功行捞进来(上一轮已踩)。改用 error_information 是否存在做判据。
set -uo pipefail
NS=litellm-product

echo "=== 1) 该腿 11:30~12:15 全部行(按时间,含 key 别名与异常类) ==="
sudo kubectl -n $NS exec -i litellm-db-0 -- psql -U litellm -d litellm -A -F'|' -t -c \
"select to_char(s.\"startTime\",'HH24:MI:SS'),
        coalesce(nullif(s.metadata->>'user_api_key_alias',''), left(s.\"api_key\",8)) as who,
        case when s.metadata->'error_information' is null then 'OK' else 'FAIL' end as r,
        coalesce(s.metadata->'error_information'->>'error_class','-') as cls
 from \"LiteLLM_SpendLogs\" s
 where s.\"model\"='openai/cu/claude-fable-5-1-medium'
   and s.\"startTime\" between '2026-09-16 11:30'::timestamp and '2026-09-16 12:15'::timestamp
 order by s.\"startTime\";" < /dev/null 2>&1 | grep -v '^\[sudo\]'

echo
echo "=== 2) 该腿今天所有失败行,按 key 汇总(看是谁在持续弄坏它) ==="
sudo kubectl -n $NS exec -i litellm-db-0 -- psql -U litellm -d litellm -A -F'|' -t -c \
"select coalesce(nullif(metadata->>'user_api_key_alias',''), left(\"api_key\",8)) as who,
        coalesce(metadata->'error_information'->>'error_class','-') as cls,
        count(*) as n,
        to_char(min(\"startTime\"),'HH24:MI:SS') || ' ~ ' || to_char(max(\"startTime\"),'HH24:MI:SS') as span
 from \"LiteLLM_SpendLogs\"
 where \"model\"='openai/cu/claude-fable-5-1-medium'
   and metadata->'error_information' is not null
   and \"startTime\" > now() - interval '18 hours'
 group by 1,2 order by n desc;" < /dev/null 2>&1 | grep -v '^\[sudo\]'

echo
echo "=== 3) MidStreamFallbackError 的异常消息全文(判是不是 unsupported IDE tool) ==="
sudo kubectl -n $NS exec -i litellm-db-0 -- psql -U litellm -d litellm -A -t -c \
"select to_char(\"startTime\",'HH24:MI:SS') || E'\n    ' ||
        left(coalesce(metadata->'error_information'->>'error_message','(空)'), 500)
 from \"LiteLLM_SpendLogs\"
 where \"model\"='openai/cu/claude-fable-5-1-medium'
   and metadata->'error_information'->>'error_class'='MidStreamFallbackError'
   and \"startTime\" > now() - interval '18 hours'
 order by \"startTime\" desc limit 4;" < /dev/null 2>&1 | grep -v '^\[sudo\]'
