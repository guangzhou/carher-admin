#!/usr/bin/env bash
# 已确认: proxy_server_request 里含"飞书文档"+"创建" => 正文送到了模型面前。
# 所以问题不是 carher 没送,是模型收到了还回"没看到具体的消息内容"。
#
# 现在要定位: 那句话落在 messages 数组的哪个下标、哪个 role、
# 是当轮 user 指令,还是被埋进 [CONTEXT ANCHOR v5] 的 HISTORY 段
# / sessions_history 工具返回里 —— 后者模型会读成"过去发生的事",
# 不会当成"现在要执行的指令"。这跟"没送到"是完全不同的病。
#
# req_len=114772,jms 这一跳会截断大 payload,所以定位全部用 SQL 在库里做,
# 只让下标/role/长度/短摘录过线。
set -uo pipefail
NS=litellm-product
KEY=a538f674b895678f417eea9e277f75a24bf94333f388b96b0546167a6d779bb7
WHEN='2026-09-16 12:48:28.919'

echo "=== 1) messages 数组逐条: 谁含'飞书文档' ==="
sudo kubectl -n $NS exec -i litellm-db-0 -- psql -U litellm -d litellm -c \
"with r as (
   select \"proxy_server_request\" as p from \"LiteLLM_SpendLogs\"
   where \"api_key\"='$KEY' and \"startTime\"='$WHEN'::timestamp limit 1
 ), m as (
   select idx, msg from r, jsonb_array_elements(
     case when p->'messages' is not null then p->'messages'
          else p->'body'->'messages' end) with ordinality t(msg, idx)
 )
 select idx,
        msg->>'role' as role,
        length(msg->>'content') as clen,
        (msg->>'content' like '%飞书文档%') as has_doc,
        (msg->>'content' like '%litellm_truncated%') as truncated,
        (msg->>'content' like '%CONTEXT ANCHOR%') as is_anchor,
        (msg->>'content' like '%OpenClaw runtime context%') as is_wrapper,
        left(regexp_replace(coalesce(msg->>'content',''), E'[\\\\n\\\\r]+', ' | ', 'g'), 90) as head
 from m order by idx;" < /dev/null

echo
echo "=== 2) 命中那条: '飞书文档' 前后各 400 字的上下文 ==="
sudo kubectl -n $NS exec -i litellm-db-0 -- psql -U litellm -d litellm -A -t -c \
"with r as (
   select \"proxy_server_request\" as p from \"LiteLLM_SpendLogs\"
   where \"api_key\"='$KEY' and \"startTime\"='$WHEN'::timestamp limit 1
 ), m as (
   select idx, msg from r, jsonb_array_elements(
     case when p->'messages' is not null then p->'messages'
          else p->'body'->'messages' end) with ordinality t(msg, idx)
 ), h as (
   select idx, msg->>'role' as role, msg->>'content' as c,
          strpos(msg->>'content', '飞书文档') as pos
   from m where msg->>'content' like '%飞书文档%'
 )
 select E'\n----- idx='||idx||' role='||role||' pos='||pos||E' -----\n'
        || substr(c, greatest(1, pos-400), 800)
 from h order by idx;" < /dev/null

echo
echo "=== 3) 最后一条 user 消息全文头部 2500 字(当轮指令应该在这里) ==="
sudo kubectl -n $NS exec -i litellm-db-0 -- psql -U litellm -d litellm -A -t -c \
"with r as (
   select \"proxy_server_request\" as p from \"LiteLLM_SpendLogs\"
   where \"api_key\"='$KEY' and \"startTime\"='$WHEN'::timestamp limit 1
 ), m as (
   select idx, msg from r, jsonb_array_elements(
     case when p->'messages' is not null then p->'messages'
          else p->'body'->'messages' end) with ordinality t(msg, idx)
 )
 select left(msg->>'content', 2500) from m
 where msg->>'role'='user' order by idx desc limit 1;" < /dev/null
