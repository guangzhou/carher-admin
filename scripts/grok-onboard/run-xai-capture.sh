#!/bin/bash
# run-xai-capture.sh — 在 226 上执行: 注入 x.ai 捕获脚本到 baked patchright 镜像,
# hostNetwork 钉 EIP 节点 .86, 一次干净尝试拿 Grok OAuth bundle。
# 对齐 scripts/aliyun-eip-onboard.sh 的证明过的注入/隔离模式。
set -uo pipefail
CTX="203299974580141085-c215e116fb0a7414287f4be1c31bb4ebc"
K="kubectl --context $CTX -n carher"
NS=carher
IMG="cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher:zerokey-capture-aliyun-20260707-otp3"
PVC=chatgpt-onboard-work
NODE=ap-southeast-1.172.16.0.86      # EIP 47.236.200.98
DISP=77
JOB=xai-capture
EMAIL="vmendoza1808@mail.com"
XAI_PW="h7auMEre"

$K create cm xai-capture-src --from-file=xai_device_capture.py=/tmp/xai_device_capture.py \
  --dry-run=client -o yaml | $K apply -f - >/dev/null
$K create secret generic xai-capture-creds \
  --from-literal=EMAIL="$EMAIL" --from-literal=XAI_PW="$XAI_PW" \
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
      volumes:
        - {name: src, configMap: {name: xai-capture-src}}
        - {name: work, persistentVolumeClaim: {claimName: $PVC}}
        - {name: shm, emptyDir: {medium: Memory, sizeLimit: 1Gi}}
      containers:
        - name: run
          image: $IMG
          resources:
            requests: {cpu: "500m", memory: "1Gi"}
            limits: {cpu: "2", memory: "3Gi"}
          volumeMounts:
            - {name: src, mountPath: /src}
            - {name: work, mountPath: /work}
            - {name: shm, mountPath: /dev/shm}
          env:
            - {name: DISPLAY, value: ":$DISP"}
            - {name: PLAYWRIGHT_BROWSERS_PATH, value: /ms-playwright}
            - {name: XAI_PW_FILE, value: /run/xai_pw}
            - {name: SCREENSHOT_DIR, value: /work/ss-xai}
            - {name: OAUTH_OUTPUT, value: /work/grok_oauth-mail135.json}
            - {name: TAG, value: xai-mail135}
            - name: MAIL_USER
              valueFrom: {secretKeyRef: {name: xai-capture-creds, key: EMAIL}}
            - name: _XPW
              valueFrom: {secretKeyRef: {name: xai-capture-creds, key: XAI_PW}}
          command:
            - bash
            - -c
            - |
              set -o pipefail
              mkdir -p /work/ss-xai
              printf '%s' "\$_XPW" > /run/xai_pw && chmod 600 /run/xai_pw && unset _XPW
              Xvfb :$DISP -screen 0 1440x1000x24 >/tmp/xvfb.log 2>&1 &
              for i in \$(seq 1 30); do [ -S /tmp/.X11-unix/X$DISP ] && break; sleep 1; done
              [ -S /tmp/.X11-unix/X$DISP ] || { echo "FATAL: Xvfb not ready"; tail -20 /tmp/xvfb.log; exit 1; }
              python3 -c "import patchright; print('patchright ready')"
              python3 /src/xai_device_capture.py 2>&1 | tee /work/log-xai-capture.txt
JOBEOF

echo "[xai-capture] job=$JOB node=$NODE disp=:$DISP"
$K get job $JOB -o wide 2>&1 | tail -3
