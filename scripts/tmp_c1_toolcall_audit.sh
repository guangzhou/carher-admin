#!/usr/bin/env bash
# carher-1 刚才那 6 发 HTTP 都是 success,但用户说飞书文档没建出来。
# HTTP 200 不等于 toolcall 发出来了 —— 判据是响应里到底有没有 tool_calls。
#
# 三件事:
#   1) 请求里带了几个 tool(带没带 = 客户端有没有把工具表送上来)
#   2) 响应里有没有 tool_calls / finish_reason 是什么
#   3) 响应正文长什么样(空正文 + 无 tool_calls = 模型啥也没干)
set -uo pipefail
NS=litellm-product
KEY=a538f674b895678f417eea9e277f75a24bf94333f388b96b0546167a6d779bb7

sudo kubectl -n $NS exec -i litellm-db-0 -- psql -U litellm -d litellm -A -t -c \
"select json_agg(json_build_array(\"startTime\"::text, \"proxy_server_request\"::text, response::text))
 from (select * from \"LiteLLM_SpendLogs\"
       where \"api_key\"='$KEY' and \"startTime\" > now() - interval '30 minutes'
       order by \"startTime\" desc limit 8) t;" < /dev/null > /tmp/c1_rows.json 2>/dev/null

python3 - <<'PY'
import json
raw = open('/tmp/c1_rows.json').read().strip()
if not raw or raw == '\\N':
    print("没取到行"); raise SystemExit
rows = json.loads(raw)
for ts, req_s, resp_s in rows:
    print("="*78)
    print("时间", ts)
    try:
        req = json.loads(req_s) if req_s and req_s != 'null' else {}
    except Exception:
        req = {}
    body = req.get('body') if isinstance(req.get('body'), dict) else req
    tools = body.get('tools') or []
    names = []
    for t in tools:
        fn = (t.get('function') or {}) if isinstance(t, dict) else {}
        names.append(fn.get('name') or t.get('name') or '?')
    print(f"  请求: model={body.get('model')} stream={body.get('stream')} "
          f"tools={len(tools)} tool_choice={body.get('tool_choice')}")
    if names:
        print(f"        工具名: {names[:12]}{' ...' if len(names)>12 else ''}")
    msgs = body.get('messages') or []
    if msgs:
        last = msgs[-1]
        c = last.get('content')
        c = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
        print(f"        最后一条 {last.get('role')}: {(c or '')[:160]}")

    try:
        resp = json.loads(resp_s) if resp_s and resp_s != 'null' else {}
    except Exception:
        resp = {}
    chs = resp.get('choices') or []
    if not chs:
        print(f"  响应: 没有 choices, 原文前 200: {str(resp_s)[:200]}")
        continue
    for ch in chs:
        m = ch.get('message') or ch.get('delta') or {}
        tc = m.get('tool_calls') or []
        content = m.get('content')
        content = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        print(f"  响应: finish_reason={ch.get('finish_reason')} "
              f"tool_calls={len(tc)} content_len={len(content or '')}")
        for c0 in tc:
            fn = c0.get('function') or {}
            print(f"        -> 调用 {fn.get('name')} args={str(fn.get('arguments'))[:200]}")
        if content:
            print(f"        正文: {content[:400]}")
        elif not tc:
            print("        ⚠️ 既没有 tool_calls 也没有正文 —— 模型什么都没输出")
PY
