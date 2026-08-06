#!/bin/bash
# build one zerokey on 225 co-located capture. usage: build225.sh <N>
#
# 取码预算可覆盖(2026-08-06 加):
#   OTP_AUTO_MAX   取码轮询窗口秒数, 默认 240(= cap.py 默认)。
#     ⚠️ 原先这里写死 50, 是 acct-141/144 失败的直接原因 —— 邮件明明已在收件箱
#     (截图实证 noreply@tm.openai.com "Your temporary ChatGPT login code"),
#     但 get_otp 的 deadline 只有 50s, 而它每轮要 find_mail_frame(最多 25s)
#     + 逐行点开邮件读正文; 对照: 同日 chatgpt-litellm-oauth.py 在**同样这两个邮箱**
#     取码成功, 靠的是 ~120s 的 settle(等60s→刷新→再等60s)。50s 是硬约束不是号的问题。
#   OTP_AUTO_ONLY  1=纯自动无兜底(默认 1); 0=自动失败后等 /work/out/otp.txt 人工注入
#   OTP_FILE_WAIT  OTP_AUTO_ONLY=0 时等文件的秒数, 默认 0
#   LOGIN_MODE     otp(默认) | password。有 ChatGPT 密码时可试 password 绕开取码:
#     cap.py 的 mail.com 取码器读不到自己截图里的收件箱(列表在跨域 iframe,
#     frames 遍历 evaluate 抛异常被吞) → 45 轮判"inbox 没加载" + 取码 0 命中,
#     加大 OTP_AUTO_MAX 到 240 也无效(实证 acct-141)。对照: 同邮箱同日被
#     toggle.py/oauth.py 各成功取码一次 → 是这份实现的缺陷, 不是号/IP/预算问题。
set -u
N="$1"; K="sudo k3s kubectl -n litellm-product"
OTP_AUTO_MAX="${OTP_AUTO_MAX:-240}"
OTP_AUTO_ONLY="${OTP_AUTO_ONLY:-1}"
OTP_FILE_WAIT="${OTP_FILE_WAIT:-0}"
LOGIN_MODE="${LOGIN_MODE:-otp}"
echo "[$N] LOGIN_MODE=$LOGIN_MODE OTP_AUTO_MAX=$OTP_AUTO_MAX OTP_AUTO_ONLY=$OTP_AUTO_ONLY OTP_FILE_WAIT=$OTP_FILE_WAIT"
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
            - {name: LOGIN_MODE, value: "$LOGIN_MODE"}
            - {name: OTP_AUTO_ONLY, value: "$OTP_AUTO_ONLY"}
            - {name: OTP_AUTO_MAX, value: "$OTP_AUTO_MAX"}
            - {name: OTP_FILE_WAIT, value: "$OTP_FILE_WAIT"}
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
