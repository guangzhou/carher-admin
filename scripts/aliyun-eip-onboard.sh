#!/bin/bash
# aliyun-eip-onboard.sh — 在**阿里云新加坡 EIP 节点**跑 Codex toggle + OAuth 拿 token
#
# 为什么走阿里云而不是 188:
#   188 出口被 CF 限流严重(toggle/OAuth 频繁撞 "正在进行安全验证" 页, 靠 3-IP 轮换硬撞,
#   成功率低且慢)。阿里云新加坡节点 EIP 出口**实测能干净打开 auth.openai.com/log-in**
#   (CF_BLOCKED=False, 2026-07-26 复验; 首次实证见 memory
#   project_aliyun_native_zerokey_cf_gate_passed_2026_07_07)。
#
# ⚠️ 出口隔离铁律: 必须 hostNetwork + 钉 EIP 节点。
#   普通 pod 走共享 NAT 47.84.112.136 —— 那是**线上 9 个 codex acct 的出口**,
#   在普通 pod 里跑浏览器撞 CF 会污染生产出口 IP。
#   EIP 节点: .86 → 47.236.200.98 / .122 → 47.84.85.100(dify 节点)
#
# 产物: auth.json 落共享 PVC, 再由 caller 取回喂给 198 或阿里云池。
#
# 用法:
#   ./scripts/aliyun-eip-onboard.sh <ACTION> <N> <email> <mail_pw> <chatgpt_pw> [totp_secret]
#     ACTION = toggle | oauth
#   例:
#     ./scripts/aliyun-eip-onboard.sh toggle 128 a@mail.com mpw gpw BASE32SEED
#     ./scripts/aliyun-eip-onboard.sh oauth  128 a@mail.com mpw gpw BASE32SEED
set -uo pipefail

ACTION="${1:?usage: $0 <toggle|oauth> <N> <email> <mail_pw> <chatgpt_pw> [totp]}"
N="${2:?}"; EMAIL="${3:?}"; MPW="${4:?}"; GPW="${5:?}"; TSEC="${6:-}"

NS=carher
REG=cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher
IMG="$REG:zerokey-capture-aliyun-20260707-otp3"     # 含 patchright + Xvfb
PVC=chatgpt-onboard-work                            # RWX 工作区(存 auth.json/截图)
# 偶数号钉 .86 / 奇数号钉 .122, 两个 EIP 分摊(cf_clearance 绑 IP, 同号务必同节点)
if [ $((N % 2)) -eq 0 ]; then NODE=ap-southeast-1.172.16.0.86; else NODE=ap-southeast-1.172.16.16.122; fi
# ⚠️ DISPLAY 按号唯一,不能固定 :99 —— hostNetwork 下 X 的抽象 unix socket
# (@/tmp/.X11-unix/X<n>)和 TCP 端口(6000+n)都在**宿主 netns**,同节点第二个 Job 起 :99
# 必然 "Cannot establish any listening sockets"(2026-08-05 acct-142/143/144 实证)。
# 见 memory feedback_xvfb_display99_hostnetwork_collides_same_node。
DISPNUM="$N"
JOB="cgpt-onboard-${ACTION}-${N}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"

# ── 工作区 PVC(幂等) ────────────────────────────────────────────────────────
kubectl -n $NS get pvc $PVC >/dev/null 2>&1 || kubectl apply -f - >/dev/null <<PVCEOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: $PVC, namespace: $NS}
spec:
  accessModes: [ReadWriteMany]
  storageClassName: alibabacloud-cnfs-nas
  resources: {requests: {storage: 5Gi}}
PVCEOF

# ── 脚本 + creds 走 ConfigMap/Secret 注入(不进镜像、不留明文在 Job spec) ──────
kubectl -n $NS create cm cgpt-onboard-src-$N \
  --from-file=oauth.py="$REPO/scripts/chatgpt-onboard/chatgpt-litellm-oauth.py" \
  --from-file=toggle.py="$REPO/scripts/chatgpt-onboard/chatgpt-enable-codex-toggle.py" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

kubectl -n $NS create secret generic cgpt-onboard-creds-$N \
  --from-literal=EMAIL="$EMAIL" --from-literal=MAIL_PW="$MPW" \
  --from-literal=CHATGPT_PW="$GPW" --from-literal=TOTP_SECRET="$TSEC" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

kubectl -n $NS delete job $JOB --ignore-not-found >/dev/null 2>&1
sleep 2

if [ "$ACTION" = "toggle" ]; then
  CMD='python3 /src/toggle.py'
  EXTRA_ENV='- {name: ACTION, value: enable-codex-toggle}'
else
  CMD='python3 /src/oauth.py'
  EXTRA_ENV='- {name: GEN_ONLY, value: "1"}'
fi

kubectl apply -f - >/dev/null <<JOBEOF
apiVersion: batch/v1
kind: Job
metadata: {name: $JOB, namespace: $NS}
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 3600
  template:
    spec:
      hostNetwork: true                      # ← 出口隔离: 走节点 EIP, 不碰共享 NAT
      dnsPolicy: ClusterFirstWithHostNet
      nodeName: $NODE
      restartPolicy: Never
      volumes:
        - {name: src, configMap: {name: cgpt-onboard-src-$N}}
        - {name: work, persistentVolumeClaim: {claimName: $PVC}}
        - {name: shm, emptyDir: {medium: Memory, sizeLimit: 1Gi}}
      containers:
        - name: run
          image: $IMG
          volumeMounts:
            - {name: src, mountPath: /src}
            - {name: work, mountPath: /work}
            - {name: shm, mountPath: /dev/shm}
          env:
            - {name: DISPLAY, value: ":$DISPNUM"}
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
              # 密码写文件(脚本按 *_FILE 读), 不落 argv 防 ps 泄露
              printf '%s' "\$_MPW" > /run/mail_pw && chmod 600 /run/mail_pw
              printf '%s' "\$_GPW" > /run/chatgpt_pw && chmod 600 /run/chatgpt_pw
              unset _MPW _GPW
              Xvfb :$DISPNUM -screen 0 1440x1000x24 >/tmp/xvfb.log 2>&1 &
              for i in \$(seq 1 30); do [ -S /tmp/.X11-unix/X$DISPNUM ] && break; sleep 1; done
              [ -S /tmp/.X11-unix/X$DISPNUM ] || {
                echo "FATAL: Xvfb 30s 未就绪(display :$DISPNUM)"; tail -20 /tmp/xvfb.log
                grep -q "already running" /tmp/xvfb.log && \
                  echo "HINT: 同节点已有 onboard Job 占着 X —— 串行跑, 别并行"
                exit 1; }
              # ⚠️ 别 pip install patchright: 镜像已自带且 chromium 是 1.60.0,
              # 装 1.60.1 会找不到 chromium-1223 → BrowserType.launch 失败。
              python3 -c "import patchright; print('patchright ready')"
              $CMD 2>&1 | tee /work/log-$ACTION-$N.txt
JOBEOF

echo "[eip-onboard] job=$JOB node=$NODE action=$ACTION acct=$N"
echo "  跟进: kubectl -n $NS logs -f job/$JOB"
