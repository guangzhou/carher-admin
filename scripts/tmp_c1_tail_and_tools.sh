#!/usr/bin/env bash
# 上一轮已定位: 用户那句"随便创建一个飞书文档"出现在 idx=6 / idx=10 两条 user 消息里,
# 位置是 [CONTEXT ANCHOR v5] 的 HISTORY 段,行尾带 <<CURRENT 标记。
# 最后一条消息 idx=11 是 OpenClaw runtime context 包装,只有会话元数据,没有正文。
#
# 但 idx=10 的 content 长度正好 2293 且 truncated=t —— 我只看到了它的"头部",
# 尾部被 LiteLLM 写库前截掉了。所以"没有一条独立的活指令消息"这个话现在还不能说:
# 真实用户文本完全可能就跟在锚点后面、落在被截掉的那段里。
#
# 本轮只做两件不需要猜的事:
#   1) 把 idx=10 的 content 完整 2293 字打出来,看 litellm_truncated 标记
#      具体插在哪 —— 标记之前是什么、标记说跳过了多少字。这决定"看不见的那段有多长"。
#   2) 列出请求带的 tools 名字。如果压根没有建飞书文档的工具,
#      那"没创建成功"就还有第二个独立原因,跟 prompt 位置无关。
set -uo pipefail
NS=litellm-product
KEY=a538f674b895678f417eea9e277f75a24bf94333f388b96b0546167a6d779bb7
WHEN='2026-09-16 12:48:28.919'

echo "=== 1) idx=10 (最后一条锚点 user) content 完整内容 ==="
sudo kubectl -n $NS exec -i litellm-db-0 -- psql -U litellm -d litellm -A -t -c \
"with r as (
   select \"proxy_server_request\" as p from \"LiteLLM_SpendLogs\"
   where \"api_key\"='$KEY' and \"startTime\"='$WHEN'::timestamp limit 1
 ), m as (
   select idx, msg from r, jsonb_array_elements(
     case when p->'messages' is not null then p->'messages'
          else p->'body'->'messages' end) with ordinality t(msg, idx)
 )
 select msg->>'content' from m where idx=10;" < /dev/null

echo
echo "=== 2) 截断标记在各条消息里的偏移(离末尾多远 = 标记后面还剩多少字) ==="
sudo kubectl -n $NS exec -i litellm-db-0 -- psql -U litellm -d litellm -c \
"with r as (
   select \"proxy_server_request\" as p from \"LiteLLM_SpendLogs\"
   where \"api_key\"='$KEY' and \"startTime\"='$WHEN'::timestamp limit 1
 ), m as (
   select idx, msg from r, jsonb_array_elements(
     case when p->'messages' is not null then p->'messages'
          else p->'body'->'messages' end) with ordinality t(msg, idx)
 )
 select idx, msg->>'role' as role,
        length(msg->>'content') as clen,
        strpos(msg->>'content', 'litellm_truncated') as marker_pos,
        length(msg->>'content') - strpos(msg->>'content', 'litellm_truncated') as tail_after_marker
 from m where msg->>'content' like '%litellm_truncated%' order by idx;" < /dev/null

echo
echo "=== 3) 这一发带了哪些 tools(名字全列,看有没有建飞书文档的) ==="
sudo kubectl -n $NS exec -i litellm-db-0 -- psql -U litellm -d litellm -A -t -c \
"with r as (
   select \"proxy_server_request\" as p from \"LiteLLM_SpendLogs\"
   where \"api_key\"='$KEY' and \"startTime\"='$WHEN'::timestamp limit 1
 )
 select string_agg(t->'function'->>'name', ', ' order by t->'function'->>'name')
 from r, jsonb_array_elements(
   case when p->'tools' is not null then p->'tools'
        else p->'body'->'tools' end) t;" < /dev/null

echo
echo "=== 4) tool_choice / 工具数 ==="
sudo kubectl -n $NS exec -i litellm-db-0 -- psql -U litellm -d litellm -c \
"with r as (
   select \"proxy_server_request\" as p from \"LiteLLM_SpendLogs\"
   where \"api_key\"='$KEY' and \"startTime\"='$WHEN'::timestamp limit 1
 )
 select coalesce(p->>'tool_choice', p->'body'->>'tool_choice') as tool_choice,
        jsonb_array_length(case when p->'tools' is not null then p->'tools'
                                else p->'body'->'tools' end) as n_tools,
        coalesce(p->>'model', p->'body'->>'model') as model
 from r;" < /dev/null
