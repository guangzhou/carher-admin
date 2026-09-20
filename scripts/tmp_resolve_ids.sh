#!/usr/bin/env bash
# 探针回来的 x-litellm-model-id 是 1519252f8874 / 30260603bbf6 这种 hash,
# 不是我注册时写的 9router/claude-fable-5-1-medium。组名会撒谎,
# 判落点只认 api_base + model,所以必须把 id 解析开。
# 顺带把上一轮 key/delete 失败的临时 key 收干净。
set -uo pipefail
NS=litellm-product
IDS=${IDS:-"9efc4f1e34fe 1519252f8874 30260603bbf6"}
P=$(sudo kubectl -n $NS get pods -l app=litellm-proxy -o jsonpath='{.items[0].metadata.name}')

cat > /tmp/tmp_resolve_q2.py <<'PY'
import json, os, urllib.request, urllib.error
BASE="http://127.0.0.1:4000"; MK=os.environ["LITELLM_MASTER_KEY"]
def call(p,b=None,m="GET"):
    d=json.dumps(b).encode() if b is not None else None
    r=urllib.request.Request(BASE+p,data=d,headers={"Authorization":"Bearer "+MK,
      "Content-Type":"application/json"},method=m)
    try:
        x=urllib.request.urlopen(r,timeout=115); return x.status,x.read().decode(errors="replace")
    except urllib.error.HTTPError as e: return e.code,e.read().decode(errors="replace")

want=set(os.environ["IDS"].split())
st,raw=call("/model/info")
rows=json.loads(raw)["data"]
print(f"[info] {len(rows)} deployments")
for x in rows:
    mi=x.get("model_info") or {}
    mid=str(mi.get("id"))
    if mid in want:
        lp=x.get("litellm_params") or {}
        print(f"  id={mid}  group={x.get('model_name')}")
        print(f"     model    = {lp.get('model')}")
        print(f"     api_base = {lp.get('api_base')}")
        print(f"     api_key  = {'set len='+str(len(lp['api_key'])) if lp.get('api_key') else '(absent from /model/info)'}")

# 我注册的那两个 id 现在长什么样(用来对比 hash id 是不是同一条)
for mid in ("9router/claude-fable-5-1-medium","9router/claude-opus-5-medium"):
    hit=[x for x in rows if str((x.get("model_info") or {}).get("id"))==mid]
    if hit:
        lp=hit[0].get("litellm_params") or {}
        print(f"  [mine] {mid}: group={hit[0].get('model_name')} model={lp.get('model')} base={lp.get('api_base')}")
    else:
        print(f"  [mine] {mid}: NOT FOUND")

# 收尾: 删掉所有 tmp-probe-perpod-* / tmp-hi-* 临时 key
kill=[]
for page in range(1,60):
    st,raw=call(f"/key/list?page={page}&size=100&return_full_object=true")
    ks=json.loads(raw).get("keys") or []
    if not ks: break
    for k in ks:
        a=k.get("key_alias") or ""
        if a.startswith("tmp-probe-perpod-") or a.startswith("tmp-hi-"):
            kill.append((a,k.get("token")))
print(f"[cleanup] 待删临时 key: {[a for a,_ in kill] or '(无)'}")
for a,t in kill:
    st,raw=call("/key/delete",{"keys":[t]},"POST")
    print(f"  delete {a}: HTTP {st} {raw[:120]}")
PY
sudo kubectl -n $NS cp /tmp/tmp_resolve_q2.py "$NS/$P:/tmp/tmp_resolve_q2.py" 2>/dev/null
sudo kubectl -n $NS exec -i "$P" -c litellm -- env IDS="$IDS" python3 /tmp/tmp_resolve_q2.py < /dev/null
