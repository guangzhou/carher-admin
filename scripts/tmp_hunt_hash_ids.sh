#!/usr/bin/env bash
# x-litellm-model-id 回来的是 9efc4f1e34fe / 1519252f8874 / 30260603bbf6,
# 但 /model/info 里没有任何 model_info.id 等于它们。
# 在断言"打到了 9router"之前必须先搞清这三个 hash 是什么 ——
# 组名会撒谎,id 对不上就等于落点未知。
set -uo pipefail
NS=litellm-product
P=$(sudo kubectl -n $NS get pods -l app=litellm-proxy -o jsonpath='{.items[0].metadata.name}')

cat > /tmp/tmp_hunt_r3.py <<'PY'
import json, os, urllib.request, urllib.error
BASE="http://127.0.0.1:4000"; MK=os.environ["LITELLM_MASTER_KEY"]
def call(p,b=None,m="GET"):
    d=json.dumps(b).encode() if b is not None else None
    r=urllib.request.Request(BASE+p,data=d,headers={"Authorization":"Bearer "+MK,
      "Content-Type":"application/json"},method=m)
    try:
        x=urllib.request.urlopen(r,timeout=115); return x.status,x.read().decode(errors="replace")
    except urllib.error.HTTPError as e: return e.code,e.read().decode(errors="replace")

HASHES=["9efc4f1e34fe","1519252f8874","30260603bbf6"]
st,raw=call("/model/info")
rows=json.loads(raw)["data"]

print("=== 1) 整行 JSON 里做子串搜索 ===")
for h in HASHES:
    hit=[x for x in rows if h in json.dumps(x)]
    if not hit:
        print(f"  {h}: /model/info 里完全找不到")
    for x in hit[:3]:
        mi=x.get("model_info") or {}; lp=x.get("litellm_params") or {}
        print(f"  {h}: group={x.get('model_name')} id={mi.get('id')} "
              f"model={lp.get('model')} base={lp.get('api_base')}")

print("=== 2) 这三个组现在有几条腿、分别是谁 ===")
for g in ("claude-grok-4.6","cursor-fc-fable-5.1","cursor-fc-opus-5",
          "claude-fable-5.1","claude-opus-5"):
    legs=[x for x in rows if x.get("model_name")==g]
    print(f"  {g}: {len(legs)} 条腿")
    for x in legs:
        mi=x.get("model_info") or {}; lp=x.get("litellm_params") or {}
        print(f"     id={mi.get('id')}  model={lp.get('model')}  base={lp.get('api_base')}")

print("=== 3) SpendLogs: 刚才那 24 发到底记在哪个 model/model_group/model_id ===")
st,raw=call("/spend/logs?limit=40")
try:
    logs=json.loads(raw)
    logs=logs if isinstance(logs,list) else logs.get("data") or []
    for r in logs[:24]:
        if (r.get("model_group") or "") in ("claude-grok-4.6","claude-fable-5.1","claude-opus-5"):
            print(f"  group={r.get('model_group')}  model={r.get('model')}  "
                  f"model_id={r.get('model_id')}  status={r.get('status')}  {r.get('startTime')}")
except Exception as e:
    print("  spend/logs 读不出来:", type(e).__name__, str(e)[:200], raw[:200])
PY
sudo kubectl -n $NS cp /tmp/tmp_hunt_r3.py "$NS/$P:/tmp/tmp_hunt_r3.py" 2>/dev/null
sudo kubectl -n $NS exec -i "$P" -c litellm -- python3 /tmp/tmp_hunt_r3.py < /dev/null
