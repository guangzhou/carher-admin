#!/bin/bash
# 集群内存利用率扫描，按 (used / limit) 倒序
# 用法: scan_mem_usage.sh
#
# 合并 kubectl top + deployment limit
# 输出: HER  POD  LIMIT  USED  PCT  STATUS

set -u
NS=carher
OUTDIR=${OUTDIR:-/tmp/her-triage}
mkdir -p "$OUTDIR"

echo "===== mem usage scan ====="
echo "snapshot..."

kubectl top pod -n $NS --containers --no-headers 2>/dev/null \
  | awk '$2=="carher"{print $1, $4}' \
  | sed 's/Mi$//' \
  > "$OUTDIR/top.txt"

# pod -> limit
kubectl get pod -n $NS -o json > "$OUTDIR/pods.json" 2>/dev/null

python3 - <<PYEOF
import json
OUTDIR = "$OUTDIR"

# read top
top = {}
try:
    with open(f"{OUTDIR}/top.txt") as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) < 2: continue
            pod, mem = parts[0], parts[1]
            try: top[pod] = int(mem)
            except: pass
except FileNotFoundError:
    pass

with open(f"{OUTDIR}/pods.json") as f:
    pods = json.load(f)

rows = []
for pod in pods.get('items', []):
    name = pod['metadata']['name']
    if not name.startswith('carher-'): continue
    parts = name.split('-')
    if len(parts) < 2 or not parts[1].isdigit(): continue
    hid = int(parts[1])
    phase = pod['status'].get('phase', '?')
    if phase != 'Running': continue
    limit_mi = None
    for c in pod['spec'].get('containers', []):
        if c['name'] != 'carher': continue
        lim = c.get('resources', {}).get('limits', {}).get('memory', '')
        if lim.endswith('Gi'): limit_mi = int(float(lim[:-2]) * 1024)
        elif lim.endswith('Mi'): limit_mi = int(lim[:-2])
        elif lim.endswith('M'): limit_mi = int(lim[:-1])
    used = top.get(name)
    if used is None or limit_mi is None: continue
    pct = used * 100 / limit_mi
    rows.append((hid, name, limit_mi, used, pct))

rows.sort(key=lambda r: -r[4])
print()
print(f"{'her':<8} {'pod':<35} {'limit':>8} {'used':>8} {'pct':>6}")
print('-' * 75)
for hid, name, lim, used, pct in rows[:30]:
    flag = ''
    if pct >= 80: flag = ' \u26a0 HIGH'
    elif pct >= 60: flag = ' \u26a0 ALERT'
    print(f"her-{hid:<5d} {name:<35} {lim:>5d}Mi {used:>5d}Mi {pct:>5.1f}%{flag}")

print()
print(f"... ({len(rows)} total scanned, showing top 30)")

high = sum(1 for r in rows if r[4] >= 80)
alert = sum(1 for r in rows if 60 <= r[4] < 80)
print()
print(f"summary: HIGH (\u226580%): {high}, ALERT (60-80%): {alert}, total scanned: {len(rows)}")
print()
if high >= 5:
    print(f"\u26a0 \u26a0 {high} instances at >=80% utilization. Consider cluster-wide patch_cluster_mem.sh 4Gi")
elif alert >= 10:
    print(f"\u26a0 {alert} instances at 60-80%. Watch closely; if growing, schedule cluster patch.")
else:
    print("\u2713 cluster mem pressure normal. Threshold alerts (if any) likely from page cache or transient spikes.")

# limit 分布
limit_dist = {}
for hid, name, lim, used, pct in rows:
    limit_dist[lim] = limit_dist.get(lim, 0) + 1
print()
print(f"limit distribution: {dict(sorted(limit_dist.items(), reverse=True))}")
PYEOF
