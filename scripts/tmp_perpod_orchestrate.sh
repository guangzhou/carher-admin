#!/usr/bin/env bash
# 跑在 198 上,必须从磁盘执行,不能走 `jms ssh 'bash -s' <<EOS` 的 stdin ——
# 下面循环里的 `kubectl exec -i` 会把外层 heredoc 的 stdin 整段吃掉,
# 后半段静默不执行、exit 1 且零输出。每个 exec 都显式 `< /dev/null`。
#
# 只读探测: 建一把克隆 carher-1 形状的临时 key,逐个 pod 打,最后删掉。
set -uo pipefail
NS=litellm-product
ROUNDS=${ROUNDS:-4}
PODS=$(sudo kubectl -n $NS get pods -l app=litellm-proxy -o jsonpath='{range .items[*]}{.metadata.name}{" "}{end}')
FIRST=$(echo $PODS | awk '{print $1}')
echo "[pods] $PODS"

# --- 建临时 key(key_alias 带时间戳: 重名会 400) ---
cat > /tmp/tmp_mkkey_p1.py <<'PY'
import json, os, time, urllib.request, urllib.error
BASE="http://127.0.0.1:4000"; MK=os.environ["LITELLM_MASTER_KEY"]
def call(p,b=None,m="GET"):
    d=json.dumps(b).encode() if b is not None else None
    r=urllib.request.Request(BASE+p,data=d,headers={"Authorization":"Bearer "+MK,
      "Content-Type":"application/json"},method=m)
    try:
        x=urllib.request.urlopen(r,timeout=115); return x.status,x.read().decode(errors="replace")
    except urllib.error.HTTPError as e: return e.code,e.read().decode(errors="replace")
src=None
for page in range(1,60):
    st,raw=call(f"/key/list?page={page}&size=100&return_full_object=true")
    ks=json.loads(raw).get("keys") or []
    if not ks: break
    for k in ks:
        if k.get("key_alias")=="carher-1": src=k; break
    if src: break
if not src: raise SystemExit("carher-1 not found")
st,raw=call("/key/generate",{"key_alias":f"tmp-probe-perpod-{int(time.time())}",
  "aliases":src.get("aliases") or {}, "models":src.get("models") or [],
  "duration":"25m","max_budget":1.0},"POST")
if st!=200: raise SystemExit(f"key/generate HTTP {st}: {raw[:300]}")
print("__PK__"+json.loads(raw)["key"])
PY
sudo kubectl -n $NS cp /tmp/tmp_mkkey_p1.py "$NS/$FIRST:/tmp/tmp_mkkey_p1.py" 2>/dev/null
PK=$(sudo kubectl -n $NS exec -i "$FIRST" -c litellm -- python3 /tmp/tmp_mkkey_p1.py < /dev/null \
     2>/dev/null | sed -n 's/^__PK__//p')
if [ -z "$PK" ]; then echo "FATAL: no probe key"; exit 1; fi
echo "[key] probe key len=${#PK}"

cleanup() {
  sudo kubectl -n $NS exec -i "$FIRST" -c litellm -- python3 -c "
import json,os,urllib.request
r=urllib.request.Request('http://127.0.0.1:4000/key/delete',
  data=json.dumps({'keys':[os.environ['PK']]}).encode(),
  headers={'Authorization':'Bearer '+os.environ['LITELLM_MASTER_KEY'],
           'Content-Type':'application/json'},method='POST')
print('[cleanup] key/delete', urllib.request.urlopen(r,timeout=60).status)
" < /dev/null 2>/dev/null || echo "[cleanup] WARN: key/delete failed, key expires in 25m"
}
trap cleanup EXIT

# --- 逐 pod 探测 ---
for P in $PODS; do
  sudo kubectl -n $NS cp /tmp/tmp_perpod_probe.py "$NS/$P:/tmp/tmp_perpod_probe.py" 2>/dev/null
  sudo kubectl -n $NS exec -i "$P" -c litellm -- \
    env PROBE_KEY="$PK" ROUNDS="$ROUNDS" POD_NAME="${P##*-}" \
    python3 /tmp/tmp_perpod_probe.py < /dev/null 2>/dev/null
done
