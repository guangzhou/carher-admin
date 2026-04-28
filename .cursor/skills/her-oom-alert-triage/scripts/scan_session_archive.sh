#!/bin/bash
# 集群所有 her 实例的 session archive 体积扫描
# 用法: scan_session_archive.sh [TIMEOUT_PER_POD] [PARALLELISM]
#   默认: TIMEOUT=60, PARALLEL=5
#
# 输出风险表：
#   - archive_total: 老归档 (.reset.* / .deleted.* / .bak-*) 总体积
#   - active_max:    最大的活动 session 文件
#   - active_total:  所有活动 session 文件总和
#
# 标记:
#   HIGH: archive_total >= 100M 或 max_archive >= 30M
#   MED:  archive_total >= 30M  或 max_archive >= 10M
#   ACTIVE_BIG: active_max >= 30M
#   ACTIVE_CRITICAL: active_max >= 100M (重启即 OOM 风险)

set -u
NS=carher
TIMEOUT=${1:-60}
PARALLEL=${2:-5}
OUTDIR=${OUTDIR:-/tmp/her-triage/archive-scan}
rm -rf "$OUTDIR"
mkdir -p "$OUTDIR"

PODS=$(kubectl get pod -n $NS --no-headers 2>/dev/null \
  | awk '$3=="Running"{print $1}' \
  | grep '^carher-[0-9]*-')
TOTAL=$(echo "$PODS" | wc -l | tr -d ' ')
echo "===== session archive scan ====="
echo "scanning $TOTAL pods (timeout=${TIMEOUT}s, parallel=${PARALLEL})..."

scan_one() {
  local pod="$1"
  local hid=$(echo "$pod" | sed 's/^carher-\([0-9]*\)-.*$/\1/')
  local out="$OUTDIR/raw-${hid}.txt"
  kubectl exec -n $NS "$pod" -c carher --request-timeout=${TIMEOUT}s -- sh -c '
    DIR=/data/.openclaw/agents/main/sessions
    if [ ! -d "$DIR" ]; then echo "NO_DIR"; exit 0; fi
    cd "$DIR"
    ARCH_LIST=$(ls -1 2>/dev/null | grep -E "\.(reset|deleted)\.|\.bak-" || true)
    ARCH_COUNT=0
    [ -n "$ARCH_LIST" ] && ARCH_COUNT=$(echo "$ARCH_LIST" | wc -l)
    if [ "$ARCH_COUNT" -gt 0 ]; then
      ARCH_TOTAL=$(echo "$ARCH_LIST" | xargs -I{} stat -c "%s" {} 2>/dev/null | awk "{s+=\$1} END{print s+0}")
      ARCH_MAX=$(echo "$ARCH_LIST" | xargs -I{} stat -c "%s" {} 2>/dev/null | sort -n | tail -1)
    else
      ARCH_TOTAL=0; ARCH_MAX=0
    fi
    ACT_LIST=$(ls -1 *.jsonl 2>/dev/null | grep -vE "\.(reset|deleted)\." || true)
    ACT_COUNT=0
    [ -n "$ACT_LIST" ] && ACT_COUNT=$(echo "$ACT_LIST" | wc -l)
    if [ "$ACT_COUNT" -gt 0 ]; then
      ACT_TOTAL=$(echo "$ACT_LIST" | xargs -I{} stat -c "%s" {} 2>/dev/null | awk "{s+=\$1} END{print s+0}")
      ACT_MAX=$(echo "$ACT_LIST" | xargs -I{} stat -c "%s" {} 2>/dev/null | sort -n | tail -1)
    else
      ACT_TOTAL=0; ACT_MAX=0
    fi
    echo "ARCH_TOTAL=$ARCH_TOTAL ARCH_COUNT=$ARCH_COUNT ARCH_MAX=$ARCH_MAX ACT_TOTAL=$ACT_TOTAL ACT_COUNT=$ACT_COUNT ACT_MAX=$ACT_MAX"
  ' 2>/dev/null > "$out" || echo "EXEC_FAIL" > "$out"
}

export -f scan_one
export NS OUTDIR TIMEOUT

echo "$PODS" | xargs -P $PARALLEL -n 1 bash -c 'scan_one "$0"; printf "."' > /dev/null 2>&1
echo
echo "scan done"
echo

python3 - <<PYEOF
import os, re, glob
OUTDIR = "$OUTDIR"

rows = []; fails = []
for f in glob.glob(f'{OUTDIR}/raw-*.txt'):
    hid = int(re.search(r'raw-(\d+)\.txt', f).group(1))
    txt = open(f).read().strip()
    if 'EXEC_FAIL' in txt:
        fails.append((hid, 'EXEC_FAIL')); continue
    if 'NO_DIR' in txt:
        fails.append((hid, 'NO_DIR')); continue
    m = re.search(r'ARCH_TOTAL=(\d+) ARCH_COUNT=(\d+) ARCH_MAX=(\d+) ACT_TOTAL=(\d+) ACT_COUNT=(\d+) ACT_MAX=(\d+)', txt)
    if not m:
        if 'ARCH_TOTAL=0' in txt: rows.append((hid, 0, 0, 0, 0, 0, 0))
        else: fails.append((hid, f'PARSE: {txt[:60]}'))
        continue
    rows.append((hid, *(int(m.group(i)) for i in range(1,7))))

rows.sort(key=lambda r: -(r[1] + r[6]))

def hr(b):
    if b < 1024: return f'{b}B'
    if b < 1024**2: return f'{b/1024:.0f}K'
    if b < 1024**3: return f'{b/1024**2:.1f}M'
    return f'{b/1024**3:.2f}G'

print(f'{"her":<8} {"archive_total":>13} {"a_cnt":>6} {"a_max":>9} {"act_total":>10} {"act_cnt":>7} {"act_max":>9}  notes')
print('-'*110)
high=[]; med=[]; big=[]; crit=[]
for hid, at, ac, am, tt, tc, tm in rows:
    note = []
    if at >= 100*1024**2 or am >= 30*1024**2: note.append('HIGH')
    elif at >= 30*1024**2 or am >= 10*1024**2: note.append('MED')
    if tm >= 100*1024**2: note.append('ACTIVE_CRITICAL')
    elif tm >= 30*1024**2: note.append('ACTIVE_BIG')
    if note:
        print(f'her-{hid:<5d} {hr(at):>13} {ac:>6d} {hr(am):>9} {hr(tt):>10} {tc:>7d} {hr(tm):>9}  {",".join(note)}')
        if 'HIGH' in note: high.append(hid)
        elif 'MED' in note: med.append(hid)
        if 'ACTIVE_CRITICAL' in note: crit.append((hid, tm))
        elif 'ACTIVE_BIG' in note: big.append((hid, tm))

print()
print(f'TOTAL ok={len(rows)}, fails={len(fails)}')
print(f'HIGH risk: {len(high)}')
print(f'MED  risk: {len(med)}')
print(f'ACTIVE_CRITICAL (>=100M, restart=OOM risk): {len(crit)}')
for hid, sz in crit:
    print(f'    her-{hid}: active_max={hr(sz)}')
print(f'ACTIVE_BIG (30-100M): {len(big)}')
for hid, sz in big:
    print(f'    her-{hid}: active_max={hr(sz)}')

if fails:
    print()
    print(f'--- {len(fails)} scan failed (rerun with longer timeout) ---')
    for hid, kind in sorted(fails):
        print(f'  her-{hid:<5d}  {kind}')

print()
if high or crit or big:
    print('NEXT STEPS:')
    if crit:
        print('  1. ACTIVE_CRITICAL pods: increase memory limit to 4Gi/5Gi BEFORE any restart')
        print('     -> resize_her.sh <hid> 5Gi')
    if high:
        print('  2. HIGH archive accumulation: clean old archives')
        print('     -> clean_session_archives.sh <hid>           # dry-run')
        print('     -> clean_session_archives.sh <hid> yes       # actual delete')
    if med and not high and not crit:
        print('  - MED only: monitor; no immediate action needed')
PYEOF
