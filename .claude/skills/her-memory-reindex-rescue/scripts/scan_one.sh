#!/bin/bash
# 扫描单个 carher pod，输出 TSV 一行。
# 用法: scan_one.sh <pod_name>
# 输出列（tab 分隔）:
#   POD  UID_PLACEHOLDER  RESTARTS  LAST_OOM  MAIN_MB  MAIN_AGE_H  TMP_COUNT  TMP_ACTIVE  TMP_MB  MEM_MB  STATUS
#
# 注意: UID_PLACEHOLDER 这一列因为 $UID 在 zsh/bash 是只读内置变量被覆盖，
# 不可信；分析时从 POD 名 carher-<id>-... 自取真实 her id。
#
# STATUS 取值: OK / NO_DIR / EXEC_FAIL

set -u
POD="${1:-}"
[ -z "$POD" ] && { echo "ERROR: need pod name" >&2; exit 1; }

HID=$(echo "$POD" | sed -n 's/^carher-\([0-9]\+\)-.*/\1/p')

META=$(kubectl get pod -n carher "$POD" -o json 2>/dev/null)
if [ -z "$META" ]; then
  printf '%s\t%s\t-\t-\t-\t-\t-\t-\t-\t-\tEXEC_FAIL\n' "$POD" "$HID"
  exit 0
fi

RESTARTS=$(echo "$META" | python3 -c "
import sys, json
try:
  p = json.load(sys.stdin)
  for c in p.get('status', {}).get('containerStatuses', []):
    if c['name'] == 'carher':
      print(c.get('restartCount', 0)); break
  else:
    print(0)
except Exception:
  print(0)
")

LAST_OOM=$(echo "$META" | python3 -c "
import sys, json
try:
  p = json.load(sys.stdin)
  for c in p.get('status', {}).get('containerStatuses', []):
    if c['name'] == 'carher':
      ls = c.get('lastState', {}).get('terminated', {})
      print(ls.get('finishedAt', '-') if ls.get('reason') == 'OOMKilled' else '-')
      break
  else:
    print('-')
except Exception:
  print('-')
")

OUT=$(kubectl exec -n carher "$POD" -c carher --request-timeout=15s -- sh -c '
DIR=/data/.openclaw/memory
[ -d "$DIR" ] || { echo "STATUS=NO_DIR"; exit 0; }
cd "$DIR"
main_size=$(stat -c%s main.sqlite 2>/dev/null || echo 0)
main_mtime=$(stat -c%Y main.sqlite 2>/dev/null || echo 0)
tmp_count=0
tmp_active=0
tmp_size=0
for f in main.sqlite.tmp-*; do
  [ -e "$f" ] || continue
  tmp_count=$((tmp_count+1))
  s=$(stat -c%s "$f" 2>/dev/null || echo 0)
  tmp_size=$((tmp_size+s))
  fd=$(ls -la /proc/*/fd/ 2>/dev/null | grep -c "$f" || true)
  fd=${fd:-0}
  [ "$fd" != "0" ] && tmp_active=$((tmp_active+1))
done
mem=$(cat /sys/fs/cgroup/memory.current 2>/dev/null \
  || cat /sys/fs/cgroup/memory/memory.usage_in_bytes 2>/dev/null \
  || echo 0)
echo "STATUS=OK"
echo "MAIN_SIZE=$main_size"
echo "MAIN_MTIME=$main_mtime"
echo "TMP_COUNT=$tmp_count"
echo "TMP_ACTIVE=$tmp_active"
echo "TMP_SIZE=$tmp_size"
echo "MEM=$mem"
' 2>&1)

STATUS=$(echo "$OUT" | grep '^STATUS=' | cut -d= -f2)
if [ "$STATUS" != "OK" ]; then
  printf '%s\t%s\t%s\t%s\t-\t-\t-\t-\t-\t-\t%s\n' \
    "$POD" "$HID" "$RESTARTS" "$LAST_OOM" "${STATUS:-EXEC_FAIL}"
  exit 0
fi

MAIN_SIZE=$(echo "$OUT" | grep '^MAIN_SIZE=' | cut -d= -f2)
MAIN_MTIME=$(echo "$OUT" | grep '^MAIN_MTIME=' | cut -d= -f2)
TMP_COUNT=$(echo "$OUT" | grep '^TMP_COUNT=' | cut -d= -f2)
TMP_ACTIVE=$(echo "$OUT" | grep '^TMP_ACTIVE=' | cut -d= -f2)
TMP_SIZE=$(echo "$OUT" | grep '^TMP_SIZE=' | cut -d= -f2)
MEM=$(echo "$OUT" | grep '^MEM=' | cut -d= -f2)

NOW=$(date +%s)
AGE_H=$(awk -v now="$NOW" -v m="$MAIN_MTIME" \
  'BEGIN{ if(m==0){print "-"} else {printf "%.1f", (now-m)/3600} }')
MAIN_MB=$(awk -v s="$MAIN_SIZE" 'BEGIN{printf "%.0f", s/1048576}')
TMP_MB=$(awk -v s="$TMP_SIZE" 'BEGIN{printf "%.0f", s/1048576}')
MEM_MB=$(awk -v s="$MEM" 'BEGIN{printf "%.0f", s/1048576}')

printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\tOK\n' \
  "$POD" "$HID" "$RESTARTS" "$LAST_OOM" \
  "$MAIN_MB" "$AGE_H" "$TMP_COUNT" "$TMP_ACTIVE" "$TMP_MB" "$MEM_MB"
