#!/bin/bash
# Phase C: patch deploy 3Gi → paused=true → wait gone → paused=false → wait ready → verify
# Args: $1 = her id
# Side effects:
#   - 写日志到 /tmp/her-rescue/her-$HID.log（接 Phase A 同一文件）
#   - 把最终 3Gi pod 名写到 /tmp/her-rescue/her-$HID-pod-final.txt
#
# 这一步把 Phase A 临时设置的 5Gi 还原回 3Gi。
# 用 paused 而不是直接改 deploy spec，是因为修改 limits 也会触发 deployment rollingUpdate
# (双 pod 短暂共存 → SQLite 锁冲突风险，PVC 是 RWX NAS 但 SQLite 不支持多进程写)。

set -u
HID="${1:-}"
[ -z "$HID" ] && { echo "ERROR: need her id" >&2; exit 1; }
mkdir -p /tmp/her-rescue
LOG="/tmp/her-rescue/her-$HID.log"
exec >> "$LOG" 2>&1

echo
echo "===== [PHASE-C] her-$HID  $(date -u +%FT%TZ) ====="

# Step 7: patch deployment 3Gi
echo "--- STEP 7: patch deployment memory limit 3Gi ---"
kubectl patch deployment -n carher "carher-$HID" --type=json -p='[
  {"op":"replace","path":"/spec/template/spec/containers/0/resources/limits/memory","value":"3Gi"}
]'

# Step 8a: paused=true wait gone
echo "--- STEP 8a: paused=true (drop pod) ---"
kubectl patch herinstance -n carher "her-$HID" --type=merge -p '{"spec":{"paused":true}}'
for i in $(seq 1 45); do
  pod=$(kubectl get pod -n carher 2>/dev/null \
    | awk -v p="^carher-$HID-" '$1 ~ p {print $1; exit}')
  if [ -z "$pod" ]; then
    echo "  pod gone at t=$((i*2))s"; break
  fi
  sleep 2
done

# Step 8b: paused=false wait ready
echo "--- STEP 8b: paused=false (bring back at 3Gi) ---"
kubectl patch herinstance -n carher "her-$HID" --type=merge -p '{"spec":{"paused":false}}'
for i in $(seq 1 45); do
  pod=$(kubectl get pod -n carher 2>/dev/null \
    | awk -v p="^carher-$HID-" '$1 ~ p {print $1; exit}')
  if [ -n "$pod" ]; then
    ready=$(kubectl get pod -n carher "$pod" \
      -o jsonpath='{.status.containerStatuses[?(@.name=="carher")].ready}' 2>/dev/null)
    if [ "$ready" = "true" ]; then
      echo "  pod=$pod ready at t=$((i*2))s"
      echo "$pod" > "/tmp/her-rescue/her-$HID-pod-final.txt"
      break
    fi
  fi
  sleep 2
done

# Verify final state
echo "--- AFTER snapshot ---"
FINAL_POD=$(cat "/tmp/her-rescue/her-$HID-pod-final.txt" 2>/dev/null)
if [ -n "$FINAL_POD" ]; then
  echo -n "  memory_limit="
  kubectl get pod -n carher "$FINAL_POD" \
    -o jsonpath='{.spec.containers[?(@.name=="carher")].resources.limits.memory}'
  echo
  kubectl exec -n carher "$FINAL_POD" -c carher --request-timeout=10s -- sh -c '
ls -la /data/.openclaw/memory/main.sqlite* 2>&1
echo "---"
ls -la /proc/*/fd/ 2>/dev/null | grep "main.sqlite" | awk "{print \$NF}" | sort | uniq -c
' 2>&1 | sed 's/^/  /'
fi

echo
echo "===== [PHASE-C] her-$HID DONE  $(date -u +%FT%TZ) ====="
