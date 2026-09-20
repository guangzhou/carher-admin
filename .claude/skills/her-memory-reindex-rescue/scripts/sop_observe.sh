#!/bin/bash
# 5 分钟观察 her 实例 reindex 是否触发 + main.sqlite mtime 是否推进 + 内存是否平稳
# Args: $1 = her id
# 输出: /tmp/her-rescue/her-$HID-observe.log
#
# 期望结果（清孤儿后通常如此）:
#   - tmp_count 全程为 0
#   - main.sqlite size/mtime 不变
#   - mem 稳定在 300-600 MB
#
# 若 5 分钟内出现 tmp 文件且 main.sqlite mtime 推进，说明 reindex 触发并能成功 swap，
# 继续观察直到 tmp 消失再做 Phase C；
# 若出现 tmp 但 main.sqlite 不推进，可能又陷入循环，需要排查内存/embedding 服务。

set -u
HID="${1:-}"
[ -z "$HID" ] && { echo "ERROR: need her id" >&2; exit 1; }
mkdir -p /tmp/her-rescue
LOG="/tmp/her-rescue/her-$HID-observe.log"
exec >> "$LOG" 2>&1

DURATION="${DURATION_SEC:-300}"
INTERVAL="${INTERVAL_SEC:-30}"

echo
echo "===== [OBSERVE] her-$HID start $(date -u +%FT%TZ) (duration=${DURATION}s, interval=${INTERVAL}s) ====="
END=$(( $(date +%s) + DURATION ))

while [ "$(date +%s)" -lt "$END" ]; do
  ts=$(date -u +%FT%TZ)
  pod=$(kubectl get pod -n carher 2>/dev/null \
    | awk -v p="^carher-$HID-" '$1 ~ p {print $1; exit}')
  if [ -z "$pod" ]; then
    echo "[$ts] NO POD"
    sleep "$INTERVAL"; continue
  fi
  out=$(kubectl exec -n carher "$pod" -c carher --request-timeout=10s -- sh -c '
ms=$(stat -c "%s|%y" /data/.openclaw/memory/main.sqlite 2>/dev/null)
echo "main: $ms"
tmp_count=0; tmp_active=0; tmp_size=0
for f in /data/.openclaw/memory/main.sqlite.tmp-*; do
  [ -e "$f" ] || continue
  tmp_count=$((tmp_count+1))
  s=$(stat -c%s "$f" 2>/dev/null || echo 0)
  tmp_size=$((tmp_size+s))
  fd=$(ls -la /proc/*/fd/ 2>/dev/null | grep -c "$(basename "$f")" || true)
  fd=${fd:-0}
  [ "$fd" != "0" ] && tmp_active=$((tmp_active+1))
done
echo "tmp_count=$tmp_count active=$tmp_active total_size=$tmp_size"
echo "mem=$(cat /sys/fs/cgroup/memory.current 2>/dev/null || cat /sys/fs/cgroup/memory/memory.usage_in_bytes 2>/dev/null)"
' 2>&1)
  printf "[%s pod=%s]\n%s\n----\n" "$ts" "$pod" "$out"
  sleep "$INTERVAL"
done

echo "===== [OBSERVE] her-$HID done $(date -u +%FT%TZ) ====="
