#!/bin/bash
# 5-signal OOM 扫描：辨别真 OOM 事件 vs 阈值告警
# 用法: scan_oom_signals.sh [WINDOW_MIN]   默认 180 (= 最近 3 小时)
#
# 5 个信号:
#   A. pod.status.containerStatuses[].lastState.terminated.reason=OOMKilled
#   B. events reason=PodOOMKilling
#   C. events reason=Killing (preStop / SIGTERM, 不一定是 OOM 但值得看)
#   D. 任何 container.restartCount > 0 且最近 startedAt 在窗口内
#   E. 最近 WINDOW_MIN 内创建的 pod (pod replaced 后老 lastState 会丢)

set -u
NS=carher
WINDOW=${1:-180}
OUTDIR=${OUTDIR:-/tmp/her-triage}
mkdir -p "$OUTDIR"

echo "===== OOM signal scan (WINDOW=${WINDOW} min) ====="
echo "snapshot kubectl state to /tmp/her-triage/..."

kubectl get pod -n $NS -o json > "$OUTDIR/pods.json" 2>/dev/null
kubectl get events -n $NS --field-selector reason=PodOOMKilling -o json > "$OUTDIR/events-oom.json" 2>/dev/null
kubectl get events -n $NS --field-selector reason=Killing -o json > "$OUTDIR/events-killing.json" 2>/dev/null

python3 - <<PYEOF
import json, datetime, os
from collections import defaultdict

WINDOW = ${WINDOW}
OUTDIR = "$OUTDIR"
now = datetime.datetime.now(datetime.timezone.utc)

def parse(p):
    return datetime.datetime.fromisoformat(p.replace('Z','+00:00'))

def age(p):
    if not p: return None
    return (now - parse(p)).total_seconds() / 60.0

def hid_of(name):
    if not name.startswith('carher-'): return None
    parts = name.split('-')
    if len(parts) < 2 or not parts[1].isdigit(): return None
    return int(parts[1])

# Signal A: pod lastState OOMKilled
sig_a = []
# Signal D: any restart with recent started
sig_d = []
# Signal E: recently created pods
sig_e = []

with open(f"{OUTDIR}/pods.json") as f:
    pods = json.load(f)

for pod in pods.get('items', []):
    name = pod['metadata']['name']
    hid = hid_of(name)
    if hid is None: continue
    cre = pod['metadata'].get('creationTimestamp')
    cre_age = age(cre) if cre else None
    if cre_age is not None and cre_age <= WINDOW:
        sig_e.append((hid, name, cre_age))

    for c in pod['status'].get('containerStatuses', []):
        if c['name'] != 'carher': continue
        last = c.get('lastState', {}).get('terminated', {})
        if last.get('reason') == 'OOMKilled':
            fa = last.get('finishedAt')
            a = age(fa)
            if a is not None and a <= WINDOW:
                sig_a.append((hid, name, c['restartCount'], fa, a))
        rc = c.get('restartCount', 0)
        if rc > 0:
            cur = c.get('state', {}).get('running', {}).get('startedAt')
            a = age(cur)
            if a is not None and a <= WINDOW:
                sig_d.append((hid, name, rc, cur, a))

# Signal B: events PodOOMKilling
sig_b = []
with open(f"{OUTDIR}/events-oom.json") as f:
    evs = json.load(f)
for e in evs.get('items', []):
    obj = e.get('involvedObject', {}).get('name', '')
    hid = hid_of(obj)
    if hid is None: continue
    ts = e.get('lastTimestamp') or e.get('eventTime') or e.get('firstTimestamp')
    a = age(ts)
    if a is not None and a <= WINDOW:
        sig_b.append((hid, obj, ts, a, e.get('message', '')[:120]))

# Signal C: events Killing
sig_c = []
with open(f"{OUTDIR}/events-killing.json") as f:
    evs = json.load(f)
for e in evs.get('items', []):
    obj = e.get('involvedObject', {}).get('name', '')
    hid = hid_of(obj)
    if hid is None: continue
    ts = e.get('lastTimestamp') or e.get('eventTime') or e.get('firstTimestamp')
    a = age(ts)
    if a is not None and a <= WINDOW:
        sig_c.append((hid, obj, ts, a, e.get('message', '')[:120]))

print()
print(f"--- A. pod lastState=OOMKilled (real OOM, last {WINDOW}min) ---")
print(f"    count={len(sig_a)}")
for hid, n, rc, t, a in sorted(sig_a, key=lambda r: r[4]):
    print(f"    her-{hid:<5d} pod={n:35s} restarts={rc:<3d} OOM_at={t} ({a:.0f}min ago)")

print()
print(f"--- B. events PodOOMKilling (real OOM, last {WINDOW}min) ---")
print(f"    count={len(sig_b)}")
for hid, n, t, a, msg in sorted(sig_b, key=lambda r: r[3]):
    print(f"    her-{hid:<5d} obj={n:35s} at={t} ({a:.0f}min ago)")

print()
print(f"--- C. events Killing (preStop/SIGTERM, last {WINDOW}min, not necessarily OOM) ---")
print(f"    count={len(sig_c)}")
# Killing 太多就只列前 20
for hid, n, t, a, msg in sorted(sig_c, key=lambda r: r[3])[:20]:
    print(f"    her-{hid:<5d} obj={n:35s} at={t} ({a:.0f}min ago)")
if len(sig_c) > 20:
    print(f"    ... and {len(sig_c)-20} more (集群 patch / paused-toggle 都会触发，正常 noise)")

print()
print(f"--- D. container restarted recently (last {WINDOW}min, restartCount>0) ---")
print(f"    count={len(sig_d)}")
for hid, n, rc, t, a in sorted(sig_d, key=lambda r: r[4])[:15]:
    print(f"    her-{hid:<5d} pod={n:35s} restarts={rc:<3d} cur_started={t} ({a:.0f}min ago)")
if len(sig_d) > 15:
    print(f"    ... and {len(sig_d)-15} more")

print()
print(f"--- E. recently created pods (last {WINDOW}min) ---")
print(f"    count={len(sig_e)}")
# 大量是预期的 (rolling update)，只列 head
for hid, n, a in sorted(sig_e, key=lambda r: r[2])[:10]:
    print(f"    her-{hid:<5d} pod={n:35s} created {a:.0f}min ago")
if len(sig_e) > 10:
    print(f"    ... and {len(sig_e)-10} more")

print()
print("===== summary =====")
real_oom_ids = set(x[0] for x in sig_a) | set(x[0] for x in sig_b)
print(f"REAL OOM (signal A or B): {len(real_oom_ids)} unique her ids: {sorted(real_oom_ids)}")
restart_ids = set(x[0] for x in sig_d) - real_oom_ids
print(f"Restarted but not OOM (signal D only): {len(restart_ids)} unique: {sorted(restart_ids)[:30]}{'...' if len(restart_ids)>30 else ''}")
print()
if not real_oom_ids:
    print("\u2713 No real OOM events detected in window.")
    print("  If user reports 'OOM alarm', likely is Aliyun ACK threshold alert (mem utilization),")
    print("  not actual OOMKilled. Run scan_mem_usage.sh next.")
else:
    print(f"\u26a0 {len(real_oom_ids)} real OOM events detected. Targets to triage:")
    for hid in sorted(real_oom_ids):
        print(f"    her-{hid}")
PYEOF
