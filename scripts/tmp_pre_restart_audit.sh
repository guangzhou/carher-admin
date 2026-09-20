#!/usr/bin/env bash
# 跑在 198 上，从磁盘执行(不能走 `ssh bash -s` 的 stdin —— 循环里的
# `kubectl exec -i` 会把外层 heredoc 的 stdin 吃掉,后半段静默不执行)。
#
# 重启前的体检,两件事:
#   A. 阳性对照: 用 carher-1 的形状打 grok 一发 hi。grok 跟本次改动无关,
#      它绿 = 探测手法本身没问题;它红 = 问题比我判断的大。
#   B. 补丁层盘点: rollout restart 只会丢"热塞进运行中容器"的文件。
#      镜像里烤进去的、ConfigMap/Secret 挂载进来的、DB 里存的,都不会丢。
#      所以逐个 pod 找 /app 下比容器启动时间更新的文件 —— 那些就是会丢的。
set -uo pipefail
NS=litellm-product
PODS=$(sudo kubectl -n $NS get pods -l app=litellm-proxy -o jsonpath='{range .items[*]}{.metadata.name}{" "}{end}')
FIRST=$(echo $PODS | awk '{print $1}')

echo "############ A. 阳性对照: grok 打 hi ############"
cat > /tmp/tmp_hi_m2.py <<'PY'
import json, os, time, urllib.request, urllib.error
BASE = "http://127.0.0.1:4000"; MK = os.environ["LITELLM_MASTER_KEY"]

def call(p, b=None, m="GET", key=None, t=300):
    d = json.dumps(b).encode() if b is not None else None
    r = urllib.request.Request(BASE+p, data=d, headers={
        "Authorization": "Bearer "+(key or MK),
        "Content-Type": "application/json"}, method=m)
    try:
        x = urllib.request.urlopen(r, timeout=t)
        return x.status, dict(x.headers), x.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode(errors="replace")

src = None
for page in range(1, 60):
    st, _, raw = call(f"/key/list?page={page}&size=100&return_full_object=true")
    ks = json.loads(raw).get("keys") or []
    if not ks: break
    for k in ks:
        if k.get("key_alias") == "carher-1": src = k; break
    if src: break

al = src.get("aliases") or {}
mods = src.get("models") or []
# carher-1 白名单里到底有没有 grok?
groks = [m for m in mods if "grok" in m.lower()] + [k for k in al if "grok" in k.lower()]
print("[shape] carher-1 白名单/alias 里带 grok 的:", groks or "(无)")

st, _, raw = call("/key/generate", {"key_alias": f"tmp-hi-{int(time.time())}",
    "aliases": al, "models": mods, "duration": "20m", "max_budget": 1.0}, "POST")
pk = json.loads(raw)["key"]

try:
    for m in (groks[:2] or []) + ["claude-grok-4.6"]:
        st, hdr, raw = call("/v1/chat/completions", {"model": m,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 24}, "POST", key=pk)
        mid = hdr.get("x-litellm-model-id") or "-"
        try:
            txt = json.loads(raw)["choices"][0]["message"]["content"] if st == 200 else raw[:160]
        except Exception:
            txt = raw[:160]
        print(f"[hi] {m:22} HTTP {st}  model-id={mid}  {json.dumps(txt)[:160]}")
finally:
    call("/key/delete", {"keys": [pk]}, "POST")
    print("[cleanup] 临时 key 已删")
PY
sudo kubectl -n $NS cp /tmp/tmp_hi_m2.py "$NS/$FIRST:/tmp/tmp_hi_m2.py" 2>/dev/null
sudo kubectl -n $NS exec -i "$FIRST" -c litellm -- python3 /tmp/tmp_hi_m2.py < /dev/null

echo
echo "############ B. 补丁层盘点 ############"
echo "=== 镜像(重启后不变) ==="
sudo kubectl -n $NS get deploy litellm-proxy -o jsonpath='{range .spec.template.spec.containers[*]}{.image}{"\n"}{end}'
echo
echo "=== 挂载(重启后照旧挂,不会丢) ==="
sudo kubectl -n $NS get deploy litellm-proxy -o jsonpath='{range .spec.template.spec.volumes[*]}{.name}{"  cm="}{.configMap.name}{"  secret="}{.secret.secretName}{"\n"}{end}'
echo
echo "=== 每个 pod: /app 下比容器启动更新的文件 = 重启会丢的 ==="
for P in $PODS; do
  echo "--- $P (started $(sudo kubectl -n $NS get pod $P -o jsonpath='{.status.startTime}')) ---"
  sudo kubectl -n $NS exec -i "$P" -c litellm -- sh -c '
    # 用 /proc/1 的启动时刻做基准,比 pod startTime 更贴近容器内真相
    find /app -newer /proc/1 -type f \
         ! -path "*/site-packages/*" ! -path "*/.venv/lib/*" \
         ! -name "*.pyc" ! -path "*/__pycache__/*" 2>/dev/null | head -25
    echo "(以上为空 = 该 pod 没有热塞进去的文件)"
  ' < /dev/null
done
