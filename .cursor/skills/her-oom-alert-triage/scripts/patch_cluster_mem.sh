#!/bin/bash
# 集群范围批量调整所有 carher-* deployment 的 memory limit
# 用法: patch_cluster_mem.sh <new_limit> [from_limit]
#   e.g. patch_cluster_mem.sh 4Gi          # 把所有低于 4Gi 的升到 4Gi
#        patch_cluster_mem.sh 4Gi 3Gi      # 只把 3Gi 的改到 4Gi (推荐)
#        patch_cluster_mem.sh 5Gi 4Gi      # 进一步从 4Gi 升到 5Gi
#
# 工作流:
#   1. 列出符合条件的 deployment (filter by from_limit if provided)
#   2. 顺序 kubectl patch 每个 deployment
#   3. 每个 patch 触发自己的 rolling update (ReadinessGate 平滑切换)
#   4. 不并行：避免 API server 节流 + 同时大量 pod 重启
#
# 估算: 200 实例 * 每个 patch ~3s + rolling 自动后台进行 = ~10 分钟下完所有 patch
#       但实际全部 pod 替换完成约 30-40 分钟 (rolling 异步进行)

set -u
NS=carher
NEW="${1:-}"
FROM="${2:-}"
[ -z "$NEW" ] && { echo "Usage: $0 <new_limit> [from_limit]" >&2; exit 1; }

LOG=${LOG:-/tmp/her-triage/cluster-patch-${NEW}.log}
mkdir -p "$(dirname "$LOG")"
exec > >(tee -a "$LOG") 2>&1
echo "===== cluster mem patch -> $NEW (from=${FROM:-any}) $(date -u +%FT%TZ) ====="

OUTDIR=/tmp/her-triage
kubectl get deployment -n $NS -o json 2>/dev/null > "$OUTDIR/deploys.json"

TARGETS=$(python3 - <<PYEOF
import json
with open("$OUTDIR/deploys.json") as f: d = json.load(f)
out = []
for dep in d.get('items', []):
    name = dep['metadata']['name']
    if not name.startswith('carher-'): continue
    parts = name.split('-')
    if len(parts) < 2 or not parts[1].isdigit(): continue
    cs = dep['spec']['template']['spec']['containers']
    cur = None
    for c in cs:
        if c['name'] == 'carher':
            cur = c.get('resources', {}).get('limits', {}).get('memory')
            break
    if cur is None: continue
    if cur == "$NEW": continue  # skip already correct
    if "$FROM" and cur != "$FROM": continue
    out.append((name, cur))
for n, cur in out: print(f'{n} {cur}')
PYEOF
)

COUNT=$(echo "$TARGETS" | grep -c . || echo 0)
echo "found $COUNT deployments to patch"
if [ "$COUNT" -eq 0 ]; then
  echo "nothing to do"
  exit 0
fi

echo
echo "$TARGETS" | head -10
[ "$COUNT" -gt 10 ] && echo "... and $((COUNT-10)) more"
echo

# confirmation prompt
if [ -z "${YES:-}" ]; then
  echo "Set YES=1 env var to proceed (or pipe 'yes' to stdin)"
  read -t 10 -p "type 'yes' to continue: " ANS || ANS=""
  [ "$ANS" != "yes" ] && { echo "aborted"; exit 1; }
fi

PATCH='{"spec":{"template":{"spec":{"containers":[{"name":"carher","resources":{"limits":{"memory":"'$NEW'"}}}]}}}}'

OK=0; FAIL=0
echo "$TARGETS" | while read DEP CUR; do
  [ -z "$DEP" ] && continue
  if kubectl patch deployment -n $NS "$DEP" --type=strategic -p "$PATCH" 2>&1 | grep -q "patched"; then
    printf "."
    OK=$((OK+1))
  else
    echo
    echo "FAIL: $DEP"
    FAIL=$((FAIL+1))
  fi
  # 节流：API server 太忙时让 rolling 有时间排队
  sleep 0.3
done
echo
echo "===== patch loop done $(date -u +%FT%TZ) ====="
echo "OK=$OK FAIL=$FAIL (FAIL 计数因 subshell 可能不准，请用 verify_cluster_mem 验证)"
echo
echo "rolling updates run in background; new pods will be created over next 20-40 min."
echo "monitor with:"
echo "  watch 'kubectl get pod -n $NS | grep carher- | awk \"{print \$3}\" | sort | uniq -c'"
echo "verify final state with:"
echo "  $(dirname \"$0\")/verify_cluster_mem.sh $NEW"
