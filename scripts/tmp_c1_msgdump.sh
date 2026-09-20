#!/usr/bin/env bash
# 模型六发里有四发都在说"没看到消息内容",而 toolcall 机制本身是好的
# (sessions_history 正常发出、finish_reason=tool_calls)。
# 所以要查的不是 toolcall,是"用户那句话有没有进 messages"。
# 把整条消息列表逐条摊开,看每个 role 各带了什么。
set -uo pipefail
NS=litellm-product
KEY=a538f674b895678f417eea9e277f75a24bf94333f388b96b0546167a6d779bb7

sudo kubectl -n $NS exec -i litellm-db-0 -- psql -U litellm -d litellm -A -t -c \
"select json_agg(json_build_array(\"startTime\"::text, \"proxy_server_request\"::text))
 from (select * from \"LiteLLM_SpendLogs\"
       where \"api_key\"='$KEY' and \"startTime\" > now() - interval '30 minutes'
       order by \"startTime\" desc limit 3) t;" < /dev/null > /tmp/c1_msg.json 2>/dev/null

python3 - <<'PY'
import json
rows = json.loads(open('/tmp/c1_msg.json').read().strip())
for ts, req_s in rows:
    print("="*78)
    print("时间", ts)
    req = json.loads(req_s)
    body = req.get('body') if isinstance(req.get('body'), dict) else req
    msgs = body.get('messages') or []
    print(f"  共 {len(msgs)} 条消息")
    for i, m in enumerate(msgs):
        role = m.get('role')
        c = m.get('content')
        if isinstance(c, list):
            parts = []
            for p in c:
                if isinstance(p, dict):
                    parts.append(p.get('text') or p.get('content')
                                 or f"<{p.get('type')}>")
                else:
                    parts.append(str(p))
            c = " | ".join(str(x) for x in parts)
        c = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
        tc = m.get('tool_calls') or []
        extra = f" tool_calls={[ (x.get('function') or {}).get('name') for x in tc ]}" if tc else ""
        # system 那条通常几千字,只看头尾;user/tool 全看
        if role == 'system' and len(c or '') > 300:
            shown = (c[:200] + "  ……<省略 %d 字>……  " % (len(c)-400) + c[-200:])
        else:
            shown = (c or '')[:900]
        print(f"  [{i}] {role}{extra} len={len(c or '')}")
        print(f"      {shown}")
PY
