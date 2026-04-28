#!/bin/bash
# 单实例 memory limit 调整 + 触发 rolling update
# 用法: resize_her.sh <her_id> <new_limit>
#   e.g. resize_her.sh 166 4Gi
#        resize_her.sh 40  5Gi
#
# 工作流:
#   1. 记录当前 limit + utilization
#   2. kubectl patch deployment 改 carher 容器 (索引 0) 的 memory limit
#   3. K8s 自动触发 rolling update (ReadinessGate=feishu-ws-ready 保证零下线)
#   4. 等新 pod ready (timeout 240s)
#   5. 验证最终 limit 与 ws ready

set -u
NS=carher
HID="${1:-}"
NEW="${2:-4Gi}"
[ -z "$HID" ] && { echo "Usage: $0 <her_id> <new_limit>" >&2; exit 1; }

LOG=${LOG:-/tmp/her-triage/her-${HID}-resize-${NEW}.log}
mkdir -p "$(dirname "$LOG")"
exec > >(tee -a "$LOG") 2>&1
echo "===== resize her-$HID -> $NEW   $(date -u +%FT%TZ) ====="

# 0) before
POD_BEFORE=$(kubectl get pod -n $NS --no-headers 2>/dev/null \
  | grep "^carher-${HID}-" | head -n1 | awk '{print $1}')
if [ -z "$POD_BEFORE" ]; then
  echo "ERROR: no running pod for her-$HID"
  exit 1
fi

LIMIT_BEFORE=$(kubectl get deployment -n $NS carher-${HID} \
  -o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}' 2>/dev/null)
USED_BEFORE=$(kubectl top pod -n $NS "$POD_BEFORE" --containers --no-headers 2>/dev/null \
  | awk '$2=="carher"{print $4}')
echo "BEFORE: pod=$POD_BEFORE limit=$LIMIT_BEFORE used=$USED_BEFORE"

if [ "$LIMIT_BEFORE" = "$NEW" ]; then
  echo "limit already $NEW, no change needed"
  exit 0
fi

# 1) patch deployment (carher container is index 0)
echo
echo "step 1: patch deployment carher-${HID} memory limit -> $NEW"
PATCH='{"spec":{"template":{"spec":{"containers":[{"name":"carher","resources":{"limits":{"memory":"'$NEW'"}}}]}}}}'
kubectl patch deployment -n $NS "carher-${HID}" --type=strategic -p "$PATCH"
if [ $? -ne 0 ]; then
  echo "ERROR: patch failed"
  exit 1
fi

# 2) wait for new pod ready (rolling update should auto-trigger)
echo
echo "step 2: waiting for rolling update + new pod ready..."
END=$(($(date +%s) + 240))
NEW_POD=""
while [ $(date +%s) -lt $END ]; do
  sleep 5
  CUR_POD=$(kubectl get pod -n $NS --no-headers 2>/dev/null \
    | grep "^carher-${HID}-" \
    | awk '$2=="2/2" && $3=="Running" {print $1}' \
    | head -n1)
  if [ -n "$CUR_POD" ] && [ "$CUR_POD" != "$POD_BEFORE" ]; then
    NEW_POD="$CUR_POD"
    break
  fi
  printf "."
done
echo
if [ -z "$NEW_POD" ]; then
  echo "WARN: no new pod ready within 240s; current state:"
  kubectl get pod -n $NS -l carher.io/instance=her-${HID} 2>/dev/null
  exit 2
fi
echo "new pod ready: $NEW_POD"

# 3) verify
LIMIT_AFTER=$(kubectl get pod -n $NS "$NEW_POD" \
  -o jsonpath='{.spec.containers[0].resources.limits.memory}' 2>/dev/null)
WS_READY=$(kubectl get pod -n $NS "$NEW_POD" \
  -o jsonpath='{range .status.conditions[?(@.type=="carher.io/feishu-ws-ready")]}{.status}{end}' 2>/dev/null)
echo "AFTER:  pod=$NEW_POD limit=$LIMIT_AFTER ws_ready=$WS_READY"

if [ "$LIMIT_AFTER" = "$NEW" ] && [ "$WS_READY" = "True" ]; then
  echo "===== DONE her-$HID -> $NEW   $(date -u +%FT%TZ) ====="
  exit 0
else
  echo "===== INCOMPLETE her-$HID (manual check)   $(date -u +%FT%TZ) ====="
  exit 3
fi
