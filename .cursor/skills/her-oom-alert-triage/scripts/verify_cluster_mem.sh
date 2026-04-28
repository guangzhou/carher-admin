#!/bin/bash
# 验证集群内 her 实例 memory limit 分布与 rolling update 完成情况
# 用法: verify_cluster_mem.sh [expected_limit]
#   e.g. verify_cluster_mem.sh 4Gi    # 期望全部 4Gi

set -u
NS=carher
EXPECTED="${1:-}"

OUTDIR=/tmp/her-triage
mkdir -p "$OUTDIR"

kubectl get pod -n $NS -o json > "$OUTDIR/pods.json" 2>/dev/null
kubectl get deployment -n $NS -o json > "$OUTDIR/deploys.json" 2>/dev/null

echo "===== cluster mem verify ====="

python3 - <<PYEOF
import json
from collections import defaultdict
OUTDIR = "$OUTDIR"
EXPECTED = "$EXPECTED"

with open(f"{OUTDIR}/pods.json") as f: pods = json.load(f)
with open(f"{OUTDIR}/deploys.json") as f: deps = json.load(f)

# pod limit dist
pod_dist = defaultdict(int)
not_ready = []
for pod in pods.get('items', []):
    name = pod['metadata']['name']
    if not name.startswith('carher-'): continue
    parts = name.split('-')
    if len(parts) < 2 or not parts[1].isdigit(): continue
    phase = pod['status'].get('phase', '?')
    if phase != 'Running':
        not_ready.append((name, phase))
        continue
    for c in pod['spec'].get('containers', []):
        if c['name'] != 'carher': continue
        lim = c.get('resources', {}).get('limits', {}).get('memory', '?')
        pod_dist[lim] += 1

# deployment limit dist
dep_dist = defaultdict(int)
mismatch = []
for dep in deps.get('items', []):
    name = dep['metadata']['name']
    if not name.startswith('carher-'): continue
    parts = name.split('-')
    if len(parts) < 2 or not parts[1].isdigit(): continue
    cs = dep['spec']['template']['spec']['containers']
    for c in cs:
        if c['name'] != 'carher': continue
        lim = c.get('resources', {}).get('limits', {}).get('memory', '?')
        dep_dist[lim] += 1
        if EXPECTED and lim != EXPECTED:
            mismatch.append((name, lim))

print()
print('--- deployment memory limit distribution ---')
for lim, n in sorted(dep_dist.items(), key=lambda r: -r[1]):
    print(f'  {lim:>8}: {n} deployments')

print()
print('--- pod (running) memory limit distribution ---')
for lim, n in sorted(pod_dist.items(), key=lambda r: -r[1]):
    print(f'  {lim:>8}: {n} pods')

print()
print(f'--- pods not Running: {len(not_ready)} ---')
for name, phase in not_ready[:10]:
    print(f'  {name}: {phase}')

if EXPECTED:
    print()
    if mismatch:
        print(f'\u26a0 {len(mismatch)} deployments still NOT at {EXPECTED}:')
        for name, lim in mismatch[:20]:
            print(f'    {name}: {lim}')
    else:
        print(f'\u2713 all deployments at {EXPECTED}')

    # pod still using old limit (rolling not finished)
    pod_old = pod_dist.copy()
    pod_old.pop(EXPECTED, None)
    pod_old_total = sum(pod_old.values())
    if pod_old_total:
        print(f'\u26a0 {pod_old_total} pods still on old limit (rolling update may still be in progress)')
        for lim, n in sorted(pod_old.items()):
            print(f'    {lim}: {n}')
    else:
        print(f'\u2713 all running pods at {EXPECTED}')
PYEOF
