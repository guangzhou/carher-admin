#!/bin/bash
# Phase A: paused → wait pod gone → clean orphan tmp → patch deploy 5Gi → unpause → wait ready
# Args: $1 = her id (e.g. 54)
# Side effects:
#   - 写日志 /tmp/her-rescue/her-$HID.log
#   - 把新 5Gi pod 名写到 /tmp/her-rescue/her-$HID-pod-5g.txt
#
# 关键: 删 tmp 必须等 pod 完全消失（fd 释放），所以用一次性 busybox pod 挂 PVC。

set -u
HID="${1:-}"
[ -z "$HID" ] && { echo "ERROR: need her id" >&2; exit 1; }
mkdir -p /tmp/her-rescue
LOG="/tmp/her-rescue/her-$HID.log"
exec >> "$LOG" 2>&1

echo
echo "===== [PHASE-A] her-$HID  $(date -u +%FT%TZ) ====="

# 0. snapshot before
echo "--- BEFORE snapshot ---"
POD_BEFORE=$(kubectl get pod -n carher 2>/dev/null \
  | awk -v p="^carher-$HID-" '$1 ~ p {print $1; exit}')
echo "pod=$POD_BEFORE"
if [ -n "$POD_BEFORE" ]; then
  kubectl exec -n carher "$POD_BEFORE" -c carher --request-timeout=10s -- sh -c '
ls -la /data/.openclaw/memory/main.sqlite* 2>&1
echo "---"
ls -la /proc/*/fd/ 2>/dev/null | grep "main.sqlite" | awk "{print \$NF}" | sort | uniq -c
' 2>&1 | sed 's/^/  /'
fi

# Step 1: paused=true
echo
echo "--- STEP 1: paused=true ---"
kubectl patch herinstance -n carher "her-$HID" --type=merge -p '{"spec":{"paused":true}}'

# Step 2: wait pod gone (max 90s)
echo
echo "--- STEP 2: wait pod exit (max 90s) ---"
for i in $(seq 1 45); do
  pod=$(kubectl get pod -n carher 2>/dev/null \
    | awk -v p="^carher-$HID-" '$1 ~ p {print $1; exit}')
  if [ -z "$pod" ]; then
    echo "  pod gone at t=$((i*2))s"
    break
  fi
  sleep 2
done

# Step 3: clean orphan tmp via temp busybox pod
echo
echo "--- STEP 3: clean orphan tmp via cleaner pod ---"
CLEANER_YAML="/tmp/her-rescue/cleaner-$HID.yaml"
cat > "$CLEANER_YAML" <<YAMLEOF
apiVersion: v1
kind: Pod
metadata:
  name: tmpcleaner-her-$HID
  namespace: carher
spec:
  restartPolicy: Never
  containers:
  - name: c
    image: busybox:latest
    command: ["sh", "-c"]
    args:
    - |
      echo BEFORE:
      ls -la /data/.openclaw/memory/main.sqlite* 2>&1
      echo ---
      rm -fv /data/.openclaw/memory/main.sqlite.tmp-* 2>&1
      echo ---
      echo AFTER:
      ls -la /data/.openclaw/memory/main.sqlite* 2>&1
      sleep 3
    volumeMounts:
    - name: data
      mountPath: /data/.openclaw
  volumes:
  - name: data
    persistentVolumeClaim:
      claimName: carher-$HID-data
YAMLEOF
kubectl apply -f "$CLEANER_YAML"
for i in $(seq 1 30); do
  ph=$(kubectl get pod -n carher "tmpcleaner-her-$HID" -o jsonpath='{.status.phase}' 2>/dev/null)
  if [ "$ph" = "Succeeded" ] || [ "$ph" = "Failed" ]; then break; fi
  sleep 2
done
kubectl logs -n carher "tmpcleaner-her-$HID" 2>&1 | sed 's/^/  /'
kubectl delete pod -n carher "tmpcleaner-her-$HID" --grace-period=0 --force 2>&1 | tail -1

# Step 4: patch deployment to 5Gi
echo
echo "--- STEP 4: patch deployment memory limit 5Gi ---"
kubectl patch deployment -n carher "carher-$HID" --type=json -p='[
  {"op":"replace","path":"/spec/template/spec/containers/0/resources/limits/memory","value":"5Gi"}
]'

# Step 5: paused=false + wait ready
echo
echo "--- STEP 5: paused=false + wait ready ---"
kubectl patch herinstance -n carher "her-$HID" --type=merge -p '{"spec":{"paused":false}}'
for i in $(seq 1 45); do
  pod=$(kubectl get pod -n carher 2>/dev/null \
    | awk -v p="^carher-$HID-" '$1 ~ p {print $1; exit}')
  if [ -n "$pod" ]; then
    ready=$(kubectl get pod -n carher "$pod" \
      -o jsonpath='{.status.containerStatuses[?(@.name=="carher")].ready}' 2>/dev/null)
    if [ "$ready" = "true" ]; then
      echo "  pod=$pod ready at t=$((i*2))s"
      echo "$pod" > "/tmp/her-rescue/her-$HID-pod-5g.txt"
      break
    fi
  fi
  sleep 2
done

echo
echo "===== [PHASE-A] her-$HID DONE  $(date -u +%FT%TZ) ====="
