#!/bin/bash
# run-xai-capture-any.sh — run-xai-capture.sh 的参数化版本(原脚本写死 mail135,保留不动)。
# 在阿里云 EIP 节点跑 x.ai 设备码 OAuth 捕获,产物落共享 PVC。
#
# 用法: XAI_EMAIL=... XAI_PW=... TAG=xai-mail125 DISP=125 NODE=... ./run-xai-capture-any.sh
set -uo pipefail

NS=carher
K="kubectl -n $NS"
IMG="cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher:zerokey-capture-aliyun-20260707-otp3"
PVC=chatgpt-onboard-work

EMAIL="${XAI_EMAIL:?need XAI_EMAIL}"
PW="${XAI_PW:?need XAI_PW}"
TAG="${TAG:?need TAG}"
DISP="${DISP:?need DISP (hostNetwork 下同节点必须唯一)}"
NODE="${NODE:-ap-southeast-1.172.16.0.86}"     # EIP 47.236.200.98;.122 = 47.84.85.100
OUTFILE="${OUTFILE:-/work/grok_oauth-${TAG}.json}"
JOB="xai-capture-${TAG}"
SRC="$(cd "$(dirname "$0")" && pwd)/xai_device_capture.py"

$K create cm ${JOB}-src --from-file=xai_device_capture.py="$SRC" \
  --dry-run=client -o yaml | $K apply -f - >/dev/null
$K create secret generic ${JOB}-creds \
  --from-literal=EMAIL="$EMAIL" --from-literal=XAI_PW="$PW" \
  --dry-run=client -o yaml | $K apply -f - >/dev/null
$K delete job $JOB --ignore-not-found >/dev/null 2>&1
sleep 2

$K apply -f - >/dev/null <<JOBEOF
apiVersion: batch/v1
kind: Job
metadata: {name: $JOB, namespace: $NS}
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 7200
  template:
    spec:
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet
      nodeName: $NODE
      restartPolicy: Never
      tolerations:
        - {key: node.kubernetes.io/disk-pressure, operator: Exists, effect: NoSchedule}
        - {key: node.kubernetes.io/unschedulable,  operator: Exists, effect: NoSchedule}
      volumes:
        - {name: src, configMap: {name: ${JOB}-src}}
        - {name: work, persistentVolumeClaim: {claimName: $PVC}}
        - {name: shm, emptyDir: {medium: Memory, sizeLimit: 1Gi}}
      containers:
        - name: run
          image: $IMG
          resources:
            requests: {cpu: "500m", memory: "1Gi", ephemeral-storage: "2Gi"}
            limits:   {cpu: "2",    memory: "3Gi", ephemeral-storage: "4Gi"}
          volumeMounts:
            - {name: src, mountPath: /src}
            - {name: work, mountPath: /work}
            - {name: shm, mountPath: /dev/shm}
          env:
            - {name: DISPLAY, value: ":$DISP"}
            - {name: PLAYWRIGHT_BROWSERS_PATH, value: /ms-playwright}
            - {name: XAI_PW_FILE, value: /run/xai_pw}
            - {name: SCREENSHOT_DIR, value: /work/ss-$TAG}
            - {name: OAUTH_OUTPUT, value: $OUTFILE}
            - {name: TAG, value: $TAG}
            - name: MAIL_USER
              valueFrom: {secretKeyRef: {name: ${JOB}-creds, key: EMAIL}}
            - name: _XPW
              valueFrom: {secretKeyRef: {name: ${JOB}-creds, key: XAI_PW}}
          command:
            - bash
            - -c
            - |
              set -o pipefail
              mkdir -p /work/ss-$TAG
              printf '%s' "\$_XPW" > /run/xai_pw && chmod 600 /run/xai_pw && unset _XPW
              Xvfb :$DISP -screen 0 1440x1000x24 >/tmp/xvfb.log 2>&1 &
              for i in \$(seq 1 30); do [ -S /tmp/.X11-unix/X$DISP ] && break; sleep 1; done
              [ -S /tmp/.X11-unix/X$DISP ] || { echo "FATAL: Xvfb not ready"; tail -20 /tmp/xvfb.log; exit 1; }
              python3 -c "import patchright; print('patchright ready')"
              python3 /src/xai_device_capture.py 2>&1 | tee /work/log-$TAG.txt
JOBEOF

echo "[xai-capture] job=$JOB node=$NODE disp=:$DISP out=$OUTFILE"
$K get job $JOB -o wide 2>&1 | tail -3
