#!/bin/bash
# build one zerokey on 225 co-located capture. usage: build225.sh <N>
set -u
N="$1"; K="sudo k3s kubectl -n litellm-product"
# 1. capture Job (co-located 225, patched script, write to hostPath)
$K delete job zk-cap225-$N --ignore-not-found >/dev/null 2>&1
cat <<Y | $K apply -f - >/dev/null 2>&1
apiVersion: batch/v1
kind: Job
metadata: {name: zk-cap225-$N, namespace: litellm-product, labels: {app: zk-cap225, account: "$N"}}
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 1200
  template:
    metadata: {labels: {app: zk-cap225, account: "$N"}}
    spec:
      restartPolicy: Never
      nodeName: aiyjy-litellm-standby
      tolerations: [{effect: NoSchedule, key: dedicated, value: standby}]
      dnsPolicy: None
      dnsConfig: {nameservers: ["1.1.1.1","8.8.8.8"]}
      imagePullSecrets: [{name: acr-vpc-secret}]
      containers:
        - name: capture
          image: docker.io/library/zerokey-capture:latest
          imagePullPolicy: Never
          command: ["sh","-c","xvfb-run -a python3 /script/cap.py && cp /work/out/zerokey-users.json /work/users.json && echo PROMOTED"]
          env:
            - {name: MAIL_USER, valueFrom: {secretKeyRef: {name: zerokey-acct-$N-creds, key: MAIL_USER}}}
            - {name: MAIL_LOGIN_PW_FILE, value: /run/creds/mail_pw}
            - {name: CHATGPT_PW_FILE, value: /run/creds/chatgpt_pw}
            - {name: OUT_JSON, value: /work/out/zerokey-users.json}
            - {name: ZK_USER, value: acct$N}
            - {name: SCREENSHOT_DIR, value: /work/screenshots}
            - {name: PROFILE_DIR, value: /work/profile}
            - {name: FORCE_LOGIN, value: "1"}
            - {name: LOGIN_MODE, value: "otp"}
            - {name: OTP_AUTO_ONLY, value: "1"}
            - {name: OTP_AUTO_MAX, value: "50"}
            - {name: OTP_FILE_WAIT, value: "0"}
          volumeMounts:
            - {name: script, mountPath: /script, readOnly: true}
            - {name: creds, mountPath: /run/creds, readOnly: true}
            - {name: work, mountPath: /work}
      volumes:
        - {name: script, configMap: {name: zerokey-capture-src}}
        - name: creds
          secret: {secretName: zerokey-acct-$N-creds, items: [{key: MAIL_PW, path: mail_pw},{key: CHATGPT_PW, path: chatgpt_pw}]}
        - {name: work, hostPath: {path: /Data/zerokey-sessions/zero-$N, type: DirectoryOrCreate}}
Y
# 2. 等 capture Job 完成(最多 ~18min)
for i in $(seq 1 90); do
  ph=$($K get pod -l app=zk-cap225,account=$N -o jsonpath="{.items[0].status.phase}" 2>/dev/null)
  [ "$ph" = "Succeeded" ] && { echo "[$N] capture OK"; break; }
  [ "$ph" = "Failed" ] && { echo "[$N] capture FAILED"; return 1 2>/dev/null; exit 1; }
  sleep 12
done
# 3. 确保 serve deploy 存在 + scale 1 + rollout
bash /tmp/gen_zero.sh $N | $K apply -f - >/dev/null 2>&1
$K scale deploy/zero-$N --replicas=1 >/dev/null 2>&1
$K rollout restart deploy/zero-$N >/dev/null 2>&1
# 4. 验证 1/1
for i in $(seq 1 20); do
  r=$($K get pod -l app=zero-$N --field-selector=status.phase=Running -o jsonpath="{.items[0].status.containerStatuses[0].ready}" 2>/dev/null)
  [ "$r" = "true" ] && { echo "[$N] serve READY"; exit 0; }
  sleep 6
done
echo "[$N] serve NOT-READY"; exit 1
