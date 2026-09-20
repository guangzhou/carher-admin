apiVersion: v1
kind: Secret
metadata: {name: cgpt-onboard-creds-$N, namespace: $NS}
type: Opaque
data:
  EMAIL: $EMAIL
  MAIL_PW: $MPW
  CHATGPT_PW: $GPW
  TOTP_SECRET: "$TSEC"
---
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
        - {name: src, configMap: {name: $SRC_CM}}
        - {name: work, persistentVolumeClaim: {claimName: $PVC}}
        - {name: shm, emptyDir: {medium: Memory, sizeLimit: 1Gi}}
      containers:
        - name: run
          image: $IMG
          # 必须显式给 requests/limits: 不给 → QoS=BestEffort → 节点回收内存时**第一个**被杀。
          # EIP 节点 memory limits 已 220% 超卖, chromium 一冲高就被 OOM kill, 症状是
          # patchright 报 TargetClosedError(browser has been closed) 且看不到 OOM 字样 ——
          # 因为死的是 chromium 子进程, 容器本身 exit=0(命令是 'python | tee', tee 成功)。
          # ⚠️ 本段在 unquoted heredoc 里, 注释禁用反引号: 会被本地 shell 当命令替换执行
          # (2026-08-02: 原文写作反引号 python | tee → 报 "python: command not found",
          #  STAGE 变量为空 → YAML 没落地 → Job 建不出来)。
          # 2026-08-02 实证: acct-101 连挂 2 次 / acct-106 挂 1 次, 加上 requests 后消失。
          resources:
            requests: {cpu: "1", memory: 3Gi}
            limits: {cpu: "4", memory: 6Gi}
          volumeMounts:
            - {name: src, mountPath: /src}
            - {name: work, mountPath: /work}
            - {name: shm, mountPath: /dev/shm}
          env:
            - {name: DISPLAY, value: "$DISP"}
            - {name: PLAYWRIGHT_BROWSERS_PATH, value: /ms-playwright}
            - {name: MAIL_OTP_PROVIDER, value: mailcom}
            - {name: SCREENSHOT_DIR, value: /work/ss-$N}
            - {name: AUTH_JSON_OUTPUT, value: /work/auth-acct-$N.json}
            - {name: MAIL_LOGIN_PW_FILE, value: /run/mail_pw}
            - {name: CHATGPT_PW_FILE, value: /run/chatgpt_pw}
            - {name: MAIL_PW_FILE, value: /run/mail_pw}
            $EXTRA_ENV
            - name: MAIL_USER
              valueFrom: {secretKeyRef: {name: cgpt-onboard-creds-$N, key: EMAIL}}
            - name: CHATGPT_EMAIL
              valueFrom: {secretKeyRef: {name: cgpt-onboard-creds-$N, key: EMAIL}}
            - name: TOTP_SECRET
              valueFrom: {secretKeyRef: {name: cgpt-onboard-creds-$N, key: TOTP_SECRET}}
            - name: _MPW
              valueFrom: {secretKeyRef: {name: cgpt-onboard-creds-$N, key: MAIL_PW}}
            - name: _GPW
              valueFrom: {secretKeyRef: {name: cgpt-onboard-creds-$N, key: CHATGPT_PW}}
          command:
            - bash
            - -c
            - |
              set -o pipefail
              mkdir -p /work/ss-$N
              rm -f /work/auth-acct-$N.json
              printf '%s' "$_MPW" > /run/mail_pw && chmod 600 /run/mail_pw
              printf '%s' "$_GPW" > /run/chatgpt_pw && chmod 600 /run/chatgpt_pw
              unset _MPW _GPW
              # 等 Xvfb 真的起来, 不能用固定 sleep 2: 节点负载高时 X server 还没就绪,
              # chromium 直接 "Missing X server or $DISPLAY" 然后 gracefully close,
              # patchright 报 TargetClosedError(与 OOM 症状同名但根因不同, 2026-08-02
              # acct-101 实证)。以 X socket 出现为就绪判据, 最多等 30s。
              Xvfb $DISP -screen 0 1440x1000x24 >/tmp/xvfb.log 2>&1 &
              for i in $(seq 1 30); do [ -S /tmp/.X11-unix/X$DISPNUM ] && break; sleep 1; done
              if [ ! -S /tmp/.X11-unix/X$DISPNUM ]; then
                echo "FATAL: Xvfb 30s 未就绪"; tail -20 /tmp/xvfb.log; exit 1
              fi
              echo "xvfb ready after ${i}s"
              python3 -c "import patchright; print('patchright ready')"
              $CMD 2>&1 | tee /work/log-$ACTION-$N.txt
              # auth.json 直接 base64 打进 job 日志: 取回就只是 kubectl logs, 不必再起
              # 挂 PVC 的 reader pod(carher ns 有零中断 hook 挡 delete pod, 少碰为妙)。
              if [ -s /work/auth-acct-$N.json ]; then
                echo B64BEGIN
                base64 /work/auth-acct-$N.json
                echo B64END
              fi
