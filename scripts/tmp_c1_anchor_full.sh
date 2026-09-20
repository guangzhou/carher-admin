#!/usr/bin/env bash
# 关键问题: 用户那句"随便创建一个飞书文档"到底有没有进 messages?
#
# SpendLogs 里 user 消息被截断了(litellm_truncated skipped 4796 chars),
# 所以"正文没送到"目前还只是模型自己的说法,不是我读出来的。必须拿全文。
#
# 全文来源: SpendLogs 的 messages 列(和 proxy_server_request 是两列,
# 截断策略可能不同),外加 request_tags / 直接在整行里搜关键词。
# 只要能在任意一列里搜到"飞书文档"或"创建",就说明正文其实送到了,
# 那结论就要反过来 —— 是模型没看见/没听懂,不是 carher 没送。
set -uo pipefail
NS=litellm-product
KEY=a538f674b895678f417eea9e277f75a24bf94333f388b96b0546167a6d779bb7

echo "=== 1) 整行里搜关键词(哪一列命中就说明那一列有正文) ==="
sudo kubectl -n $NS exec -i litellm-db-0 -- psql -U litellm -d litellm -c \
"select \"startTime\",
        (messages::text like '%飞书文档%') as msg_has_doc,
        (messages::text like '%创建%')     as msg_has_create,
        (\"proxy_server_request\"::text like '%飞书文档%') as req_has_doc,
        (\"proxy_server_request\"::text like '%创建%')     as req_has_create,
        length(messages::text) as msg_len,
        length(\"proxy_server_request\"::text) as req_len
 from \"LiteLLM_SpendLogs\"
 where \"api_key\"='$KEY' and \"startTime\" > now() - interval '40 minutes'
 order by \"startTime\" desc limit 8;" < /dev/null

echo
echo "=== 2) 最近一发: 把 messages 列里每条 user 的 HISTORY 段整段拉出来 ==="
sudo kubectl -n $NS exec -i litellm-db-0 -- psql -U litellm -d litellm -A -t -c \
"select messages::text from \"LiteLLM_SpendLogs\"
 where \"api_key\"='$KEY' and \"startTime\" > now() - interval '40 minutes'
 order by \"startTime\" desc limit 1;" < /dev/null > /tmp/c1_full_msgs.json 2>/dev/null

python3 - <<'PY'
import json
raw = open('/tmp/c1_full_msgs.json').read().strip()
print("messages 列长度:", len(raw))
if not raw or raw in ('\\N', 'null'):
    print("messages 列是空的 —— 只能靠 proxy_server_request")
    raise SystemExit
try:
    msgs = json.loads(raw)
except Exception as e:
    print("解析失败:", e); print(raw[:500]); raise SystemExit
if isinstance(msgs, dict):
    msgs = msgs.get('messages') or []
for i, m in enumerate(msgs):
    if m.get('role') != 'user':
        continue
    c = m.get('content')
    if isinstance(c, list):
        c = " | ".join(str(p.get('text') or p) if isinstance(p, dict) else str(p) for p in c)
    c = c or ''
    trunc = 'litellm_truncated' in c
    print(f"--- user[{i}] len={len(c)} 被截断={trunc}")
    # HISTORY 段之后才是真正的对话内容
    for kw in ('飞书文档', '创建', 'HISTORY'):
        p = c.find(kw)
        print(f"    '{kw}' 位置: {p}")
        if p >= 0:
            print(f"      上下文: ...{c[max(0,p-200):p+300]}...")
PY
