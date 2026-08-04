#!/usr/bin/env bash
# aliyun-acct-join-pool-on226.sh — 在 k8s-work-226 上把新 chatgpt-acct 加进阿里云
# litellm 轮询池:prod(litellm-config) + canary(litellm-config-canary) 双 CM patch
# → rollout → 复核 entry 数 → 入口流式 smoke。
#
# 前提:/tmp/aliyun-cm-add-acct.py 已推到本节点;各 acct 的 Deploy/Svc 已就绪且直连 200。
# 用法:bash /tmp/aliyun-acct-join-pool.sh 132 133 134
set -uo pipefail
NS=carher
ACCTS="$*"
STAMP=$(date +%Y%m%d-%H%M%S)

for CM in litellm-config litellm-config-canary; do
  echo "===== $CM ====="
  kubectl -n $NS get cm $CM -o jsonpath='{.data.config\.yaml}' > /tmp/$CM.cur 2>/dev/null
  [ -s /tmp/$CM.cur ] || { echo "  ⚠ $CM 读不到, skip"; continue; }
  cp /tmp/$CM.cur /tmp/cmbak-$CM-$STAMP.yaml
  echo "  backup: /tmp/cmbak-$CM-$STAMP.yaml ($(wc -l < /tmp/$CM.cur) 行)"
  python3 /tmp/aliyun-cm-add-acct.py /tmp/$CM.cur /tmp/$CM.new $ACCTS || { echo "  ❌ patch 失败"; continue; }
  python3 - /tmp/$CM.new "$ACCTS" <<'PY'
import sys, yaml, re
from collections import Counter
d = yaml.safe_load(open(sys.argv[1]))
assert isinstance(d.get("model_list"), list), "model_list missing"
new = set(sys.argv[2].split())
cnt = Counter()
for e in d["model_list"]:
    if not isinstance(e, dict):
        continue
    m = re.fullmatch(r"chatgpt-acct-(\d+)/.+", str((e.get("model_info") or {}).get("id", "")))
    if m:
        cnt[m.group(1)] += 1
base = [c for a, c in cnt.items() if a not in new]
assert base, "该 CM 内没有存量 chatgpt-acct 参照"
expect = max(set(base), key=base.count)
for n in sorted(new):
    print(f"    acct-{n}: {cnt[n]} entries (baseline {expect})")
    assert cnt[n] == expect, f"acct-{n} 应 {expect} entries, 实际 {cnt[n]}"
print(f"  ✓ yaml 合法 + entry 数与基线一致 ({expect})")
PY
  [ $? -eq 0 ] || { echo "  ❌ $CM 校验失败, 不 apply"; continue; }
  kubectl -n $NS create cm $CM --from-file=config.yaml=/tmp/$CM.new --dry-run=client -o yaml \
    | kubectl -n $NS apply -f - >/dev/null 2>&1 && echo "  ✓ $CM applied"
done

echo "===== rollout (prod + canary) ====="
kubectl -n $NS rollout restart deploy/litellm-proxy >/dev/null 2>&1
kubectl -n $NS rollout restart deploy/litellm-proxy-canary >/dev/null 2>&1
kubectl -n $NS rollout status deploy/litellm-proxy --timeout=600s 2>&1 | tail -1
kubectl -n $NS rollout status deploy/litellm-proxy-canary --timeout=300s 2>&1 | tail -1

echo "===== 复核: 线上 CM 里的 entry 数 ====="
for CM in litellm-config litellm-config-canary; do
  echo -n "  $CM: "
  kubectl -n $NS get cm $CM -o jsonpath='{.data.config\.yaml}' 2>/dev/null \
    | grep -oE "chatgpt-acct-[0-9]+/[a-z0-9.-]+" | cut -d/ -f1 | sort | uniq -c | tr '\n' ' '
  echo
done

echo "===== 复核: proxy 路由表(/v1/model/info)里的新 acct id ====="
PPOD=$(kubectl -n $NS get pod -l app=litellm-proxy -o jsonpath='{range .items[*]}{.metadata.name} {.status.containerStatuses[0].ready}{"\n"}{end}' 2>/dev/null | awk '$2=="true"{print $1; exit}')
echo "  proxy pod: $PPOD"
kubectl -n $NS exec $PPOD -c litellm -- python3 -c "
import json,os,urllib.request,re,collections
r=urllib.request.Request('http://localhost:4000/v1/model/info',
 headers={'Authorization':'Bearer '+os.environ['LITELLM_MASTER_KEY']})
d=json.loads(urllib.request.urlopen(r,timeout=60).read())
ids=[ (m.get('model_info') or {}).get('id','') for m in d.get('data',[]) ]
c=collections.Counter(re.match(r'chatgpt-acct-(\d+)/',i).group(1) for i in ids if re.match(r'chatgpt-acct-(\d+)/',i))
print('  router chatgpt-acct entries:', dict(sorted(c.items(), key=lambda kv:int(kv[0]))))
" 2>&1 | tr -d '\r' | tail -2

echo "===== 入口 smoke: gpt-5.5 流式 x8, 看落哪些成员 ====="
kubectl -n $NS exec $PPOD -c litellm -- python3 -c "
import json,os,urllib.request,collections
hit=collections.Counter(); codes=collections.Counter()
for i in range(8):
    r=urllib.request.Request('http://localhost:4000/v1/chat/completions',
      data=json.dumps({'model':'gpt-5.5','messages':[{'role':'user','content':'say OK %d'%i}],
                       'max_tokens':16,'stream':True}).encode(),
      headers={'Authorization':'Bearer '+os.environ['LITELLM_MASTER_KEY'],'Content-Type':'application/json'})
    try:
        resp=urllib.request.urlopen(r,timeout=120); resp.read(2000)
        codes[resp.status]+=1; hit[resp.headers.get('x-litellm-model-id','?')]+=1
    except Exception as e:
        codes[getattr(e,'code','ERR')]+=1
print('  codes',dict(codes))
print('  hits',dict(hit))
" 2>&1 | tr -d '\r' | tail -3
echo "JOINPOOL_DONE accts=$ACCTS"
