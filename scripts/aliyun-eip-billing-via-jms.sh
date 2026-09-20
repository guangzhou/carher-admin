#!/usr/bin/env bash
# aliyun-eip-billing-via-jms.sh — 在阿里云新加坡 EIP 节点跑 ChatGPT 订阅续订/查订阅状态。
#
# 为什么走新加坡而不是 188:
#   188 出口 2026-07 起被 CF 收紧, patchright 登录停在 chatgpt.com/auth/login(密码框不渲染)
#   或跳 /api/auth/error; mail.com 取 OTP 也被弹到 Help Center(登录失败)。
#   阿里云新加坡 EIP 节点实测 CF_BLOCKED=False、mail.com OTP 可干净取(见 aliyun-eip-onboard-*.sh)。
#
# 与 aliyun-eip-onboard-via-jms.sh 的区别:
#   - action=inspect → oauth.py 带 BILLING_INSPECT=1 BILLING_DUMP=1(只读, 打印订阅状态)
#   - action=renew   → oauth.py 带 BILLING_RENEW=1(在 Billing 页点 Renew 恢复自动续订)
#   billing 分支在 oauth.py phase1.5 里 goto #settings/Billing 后 sys.exit(0), 不需要
#   device grant, 也不产出 auth.json —— 只回捞日志里的 [BILLING-*] 行。
#   默认 FORCE_OTP_LOGIN=1(卖号商密码常只对 mail.com 有效, 走验证码登录更稳)。
#
# 出口隔离铁律(与 onboard 相同): hostNetwork + 钉 EIP 节点, 别用普通 pod(会污染生产 NAT)。
#
# 用法:
#   bash scripts/aliyun-eip-billing-via-jms.sh <inspect|renew> <N> <email> <mail_pw> <chatgpt_pw> [totp]
#   例: bash scripts/aliyun-eip-billing-via-jms.sh inspect 99 Butterejd@mail.com Mail-99-dJUzNZG9 h8pPzwwZWpN2
#
# 跟进日志: bash scripts/aliyun-eip-billing-watch.sh <N>  (或见脚本末尾提示)
set -uo pipefail

ACTION="${1:?usage: $0 <inspect|renew> <N> <email> <mail_pw> <chatgpt_pw> [totp]}"
N="${2:?}"; EMAIL="${3:?}"; MPW="${4:?}"; GPW="${5:?}"; TSEC="${6:-}"
case "$ACTION" in inspect|renew) ;; *) echo "FATAL: action must be inspect|renew"; exit 1;; esac

NS=carher
REG=cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher
IMG="$REG:zerokey-capture-aliyun-20260707-otp3"     # 含 patchright + Xvfb + chromium
PVC=chatgpt-onboard-work
ASSET="${KVIA_ASSET:-k8s-work-226}"
JOB="cgpt-billing-${ACTION}-${N}"
DISPNUM="$((900 + N % 90))"                          # 唯一化 DISPLAY, 避开 onboard 的 :N
REPO="$(cd "$(dirname "$0")/.." && pwd)"
JMS="$REPO/scripts/jms"
OAUTH_PY="$REPO/scripts/chatgpt-onboard/chatgpt-litellm-oauth.py"
# billing 单会话不复用 cf_clearance, 节点选内存多的那个; EIP_NODE 可覆盖。
NODE="${EIP_NODE:-ap-southeast-1.172.16.0.86}"
SETTLE="${OTP_SETTLE_SEC:-90}"

kx(){ "$JMS" ssh "$ASSET" "$1" 2>&1; }
b64(){ printf '%s' "$1" | base64 | tr -d '\n'; }

[ -f "$OAUTH_PY" ] || { echo "FATAL: $OAUTH_PY not found"; exit 1; }
echo "[billing] acct-$N action=$ACTION node=$NODE asset=$ASSET settle=${SETTLE}s"

# ── 1. 用本地 HEAD oauth.py 重建 src CM(旧 CM 可能 stale, sha 已实证漂移) ──────
#   ⚠️ 145KB 文件 base64≈194KB, 一次性走 jms 单命令 arg 会被截断(CM 落成空文件, sha
#   = 空串哈希 e3b0c442...)。必须分块 append 再解码(2026-08-20 实证)。
#   批量续订时**同一个 oauth.py 对所有号都一样**, 没必要每号重推(~5 次 jms 往返/号, 是
#   jms 抖动主来源, 2026-08-20 acct-82 实证 "jms reset failed")。故:
#     SRC_CM=<name>       复用现成 CM(不设则默认 cgpt-billing-src-$N)
#     SKIP_SRC_PUSH=1     跳过分块推送(假定 SRC_CM 已由 caller 建好)
#     SKIP_JOB=1          只建 CM 后退出(供 batch 一次性预建共享 CM)
SRC_CM="${SRC_CM:-cgpt-billing-src-$N}"
if [ "${SKIP_SRC_PUSH:-0}" != 1 ]; then
  LOCAL_SHA=$(shasum -a256 "$OAUTH_PY" | cut -d' ' -f1)
  TMPB64=$(mktemp); base64 < "$OAUTH_PY" | tr -d '\n' > "$TMPB64"
  PARTDIR=$(mktemp -d); split -b 40000 "$TMPB64" "$PARTDIR/p."
  kx "rm -f /tmp/billing-oauth-$N.b64; echo reset-ok" | grep -q reset-ok || { echo "FATAL: jms reset failed"; exit 1; }
  for part in "$PARTDIR"/p.*; do
    CHUNK=$(cat "$part")
    kx "printf '%s' '$CHUNK' >> /tmp/billing-oauth-$N.b64" >/dev/null
  done
  rm -rf "$TMPB64" "$PARTDIR"
  REMOTE_SHA=$(kx "base64 -d /tmp/billing-oauth-$N.b64 > /tmp/billing-oauth-$N.py; sha256sum /tmp/billing-oauth-$N.py | cut -d' ' -f1" | tr -d '[:space:]' | tail -c 64)
  if [ "$REMOTE_SHA" != "$LOCAL_SHA" ]; then
    echo "FATAL: src push sha mismatch local=$LOCAL_SHA remote=$REMOTE_SHA"; exit 1
  fi
  kx "kubectl -n $NS create cm $SRC_CM --from-file=oauth.py=/tmp/billing-oauth-$N.py --dry-run=client -o yaml | kubectl apply -f - 2>&1 | tail -1" | grep -E "configmap/" | tail -1
  echo "[billing] src CM ok sha=$LOCAL_SHA cm=$SRC_CM"
else
  echo "[billing] reuse src CM=$SRC_CM (SKIP_SRC_PUSH)"
fi
if [ "${SKIP_JOB:-0}" = 1 ]; then echo "[billing] SKIP_JOB set — src CM ready, exiting"; exit 0; fi

# ── 2. billing env ───────────────────────────────────────────────────────────
if [ "$ACTION" = "renew" ]; then BILLING_ENV='- {name: BILLING_RENEW, value: "1"}'
else BILLING_ENV='- {name: BILLING_INSPECT, value: "1"}'; fi

# ── 3. Secret + Job manifest 落节点文件再 apply(不信任 PTY 输出, 独立 verify) ──
YAML="/tmp/cgpt-billing-$ACTION-$N.yaml"
STAGE=$(cat <<EOF
cat > $YAML <<'YAMLEOF'
apiVersion: v1
kind: Secret
metadata: {name: cgpt-billing-creds-$N, namespace: $NS}
type: Opaque
data:
  EMAIL: $(b64 "$EMAIL")
  MAIL_PW: $(b64 "$MPW")
  CHATGPT_PW: $(b64 "$GPW")
  TOTP_SECRET: "$(b64 "$TSEC")"
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
          resources:
            # cpu/memory request 都只是调度预留(不是实际占用)。EIP 节点常 CPU~99%/内存~89-100%
            # 请求满(几十个实例 pod), 但实测 top CPU 5-25%/内存 71-81%(物理富余 10~17Gi)。稍大
            # 请求就挤不进 → billing pod OutOfcpu/OutOfmemory(2026-08-21 实证 .0.86 与 .16.122
            # 两节点 acct-121~134)。降到 50m/256Mi 能塞进几乎任何余量; limit 仍 4 核/6Gi,
            # 浏览器真实 ~1.5-2Gi burst 由物理内存兜(节点没到内存压力, 不会 runtime OOM-kill)。
            requests: {cpu: "50m", memory: 256Mi}
            limits: {cpu: "4", memory: 6Gi}
          volumeMounts:
            - {name: src, mountPath: /src}
            - {name: work, mountPath: /work}
            - {name: shm, mountPath: /dev/shm}
          env:
            - {name: DISPLAY, value: ":$DISPNUM"}
            - {name: PLAYWRIGHT_BROWSERS_PATH, value: /ms-playwright}
            - {name: MAIL_OTP_PROVIDER, value: mailcom}
            - {name: SCREENSHOT_DIR, value: /work/bill-ss-$N}
            - {name: MAIL_LOGIN_PW_FILE, value: /run/mail_pw}
            - {name: CHATGPT_PW_FILE, value: /run/chatgpt_pw}
            - {name: MAIL_PW_FILE, value: /run/mail_pw}
            - {name: FORCE_OTP_LOGIN, value: "${FORCE_OTP:-1}"}
            - {name: PROBE_LOGIN_HTML, value: "${PROBE_LOGIN_HTML:-0}"}
            - {name: RESET_PW, value: "${RESET_PW:-0}"}
            - {name: RESET_PROBE, value: "${RESET_PROBE:-0}"}
            - {name: SUBSCRIBE_PROBE, value: "${SUBSCRIBE_PROBE:-0}"}
            - {name: MAIL_INSPECT, value: "${MAIL_INSPECT:-0}"}
            - {name: APPEAL_MODE, value: "${APPEAL_MODE:-}"}
            - {name: APPEAL_DO_CLICK, value: "${APPEAL_DO_CLICK:-0}"}
            - {name: APPEAL_WHY_B64, value: "$(b64 "${APPEAL_WHY:-}")"}
            - {name: APPEAL_CONTEXT_B64, value: "$(b64 "${APPEAL_CONTEXT:-}")"}
            - {name: BILLING_DUMP, value: "1"}
            - {name: OTP_SETTLE_SEC, value: "$SETTLE"}
            $BILLING_ENV
            - name: MAIL_USER
              valueFrom: {secretKeyRef: {name: cgpt-billing-creds-$N, key: EMAIL}}
            - name: CHATGPT_EMAIL
              valueFrom: {secretKeyRef: {name: cgpt-billing-creds-$N, key: EMAIL}}
            - name: TOTP_SECRET
              valueFrom: {secretKeyRef: {name: cgpt-billing-creds-$N, key: TOTP_SECRET}}
            - name: _MPW
              valueFrom: {secretKeyRef: {name: cgpt-billing-creds-$N, key: MAIL_PW}}
            - name: _GPW
              valueFrom: {secretKeyRef: {name: cgpt-billing-creds-$N, key: CHATGPT_PW}}
          command:
            - bash
            - -c
            - |
              set -o pipefail
              mkdir -p /work/bill-ss-$N
              printf '%s' "\$_MPW" > /run/mail_pw && chmod 600 /run/mail_pw
              printf '%s' "\$_GPW" > /run/chatgpt_pw && chmod 600 /run/chatgpt_pw
              unset _MPW _GPW
              Xvfb :$DISPNUM -screen 0 1440x1000x24 >/tmp/xvfb.log 2>&1 &
              for i in \$(seq 1 30); do [ -S /tmp/.X11-unix/X$DISPNUM ] && break; sleep 1; done
              if [ ! -S /tmp/.X11-unix/X$DISPNUM ]; then
                echo "FATAL: Xvfb 30s 未就绪(display :$DISPNUM)"; tail -20 /tmp/xvfb.log; exit 1
              fi
              echo "xvfb ready after \${i}s"
              python3 -c "import patchright; print('patchright ready')"
              python3 /src/oauth.py 2>&1 | tee /work/bill-log-$ACTION-$N.txt
YAMLEOF
echo STAGED_LINES=\$(wc -l < $YAML)
EOF
)
kx "$STAGE" | grep -E "STAGED_LINES=[0-9]+" | tail -1

kx "kubectl -n $NS delete job $JOB --ignore-not-found >/dev/null 2>&1
kubectl apply -f $YAML 2>&1 | tail -4" | grep -E "secret/|job.batch/|rror" | tail -4

# verify: 对象必须真的在
V=$(kx "kubectl -n $NS get job $JOB -o jsonpath='JOBOK={.metadata.name}' 2>/dev/null; echo; kubectl -n $NS get secret cgpt-billing-creds-$N -o go-template='SECKEYS={{len .data}}{{\"\\n\"}}' 2>/dev/null")
echo "$V" | grep -oE "JOBOK=$JOB|SECKEYS=[0-9]+" | sort -u
echo "$V" | grep -q "JOBOK=$JOB" || { echo "!!!! acct-$N Job 未建成"; exit 1; }
echo "$V" | grep -qE "SECKEYS=4" || { echo "!!!! acct-$N Secret 键数 != 4"; exit 1; }
echo "[billing] job submitted. 跟进:"
echo "  $JMS ssh $ASSET \"kubectl -n $NS logs -f job/$JOB\""
