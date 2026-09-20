#!/usr/bin/env bash
# aliyun-eip-appeal-vnc.sh — 半自动申诉: oauth.py 自动填表 → 保持浏览器打开 → 你经 VNC 手动过
#   Turnstile + 点 Submit Appeal。Turnstile 人机验证只能真人过, 程序不绕。
#
# 网络通路: 你的 Mac --(jms proxy)--> k8s-work-226 --(nc)--> EIP 节点内网 IP:VNC 端口
#   x11vnc 只绑节点内网 IP(172.16.16.x), 绝不绑 0.0.0.0(hostNetwork 会连带暴露公网 EIP)。
#   VNC 走一个短密码 + 内网 only + jms 隧道三层, 够用。
#
# 用法:
#   bash scripts/aliyun-eip-appeal-vnc.sh <N> <email> <why_text> <context_text>
#   然后按脚本末尾提示, 在另一个终端跑 jms proxy, 本地 VNC 连 localhost:<port>。
#
# 依赖已实证(2026-08-23): 镜像有 Xvfb+patchright, apt 可装 x11vnc, 226 有 nc 且可达 .16.122。
set -uo pipefail

N="${1:?usage: $0 <N> <email> <why_text> <context_text>}"
EMAIL="${2:?}"; WHY="${3:?}"; CTX="${4:?}"

NS=carher
REG=cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher
IMG="$REG:zerokey-capture-aliyun-20260707-otp3"
ASSET="${KVIA_ASSET:-k8s-work-226}"
NODE="${EIP_NODE:-ap-southeast-1.172.16.16.122}"
NODE_IP="${NODE_IP:-${NODE##ap-southeast-1.}}"   # x11vnc 绑定的内网 IP(节点名去掉区域前缀)
JOB="cgpt-appeal-vnc-$N"
DISPNUM="$((800 + N % 90))"
VNC_PORT="$((5900 + N % 90))"
SRC_CM="${SRC_CM:-cgpt-billing-src-shared}"
HOLD_MIN="${APPEAL_HOLD_MIN:-30}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
JMS="$REPO/scripts/jms"

# 随机 VNC 密码(VNC/RFB 上限 8 字符; 仅内网+隧道内有效)。避免 SIGPIPE 双输出。
if [ -z "${VNC_PW:-}" ]; then
  VNC_PW="$(LC_ALL=C tr -dc 'a-z0-9' </dev/urandom 2>/dev/null | dd bs=1 count=8 2>/dev/null)"
  [ -n "$VNC_PW" ] || VNC_PW="appeal12"
fi
VNC_PW="${VNC_PW:0:8}"

kx(){ for i in 1 2 3 4 5; do OUT="$("$JMS" ssh "$ASSET" "$1" </dev/null 2>&1)"; echo "$OUT" | grep -q "Permission denied (password,publickey)\|nodename nor servname" || { echo "$OUT"; return 0; }; sleep 3; done; echo "$OUT"; }
b64(){ printf '%s' "$1" | base64 | tr -d '\n'; }

echo "[appeal-vnc] acct-$N node=$NODE disp=:$DISPNUM vnc_port=$VNC_PORT hold=${HOLD_MIN}m"

WHY_B64="$(b64 "$WHY")"; CTX_B64="$(b64 "$CTX")"

YAML="/tmp/$JOB.yaml"
STAGE=$(cat <<EOF
cat > $YAML <<'YAMLEOF'
apiVersion: v1
kind: Secret
metadata: {name: cgpt-appeal-creds-$N, namespace: $NS}
type: Opaque
data:
  EMAIL: $(b64 "$EMAIL")
  VNC_PW: $(b64 "$VNC_PW")
---
apiVersion: batch/v1
kind: Job
metadata: {name: $JOB, namespace: $NS}
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 3600
  template:
    spec:
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet
      nodeName: $NODE
      restartPolicy: Never
      volumes:
        - {name: src, configMap: {name: $SRC_CM}}
        - {name: work, persistentVolumeClaim: {claimName: chatgpt-onboard-work}}
        - {name: shm, emptyDir: {medium: Memory, sizeLimit: 1Gi}}
      containers:
        - name: run
          image: $IMG
          resources:
            requests: {cpu: "100m", memory: 512Mi}
            limits: {cpu: "4", memory: 6Gi}
          volumeMounts:
            - {name: src, mountPath: /src}
            - {name: work, mountPath: /work}
            - {name: shm, mountPath: /dev/shm}
          env:
            - {name: DISPLAY, value: ":$DISPNUM"}
            - {name: PLAYWRIGHT_BROWSERS_PATH, value: /ms-playwright}
            - {name: SCREENSHOT_DIR, value: /work/appeal-ss-$N}
            - {name: APPEAL_MODE, value: "submit"}
            - {name: APPEAL_DO_CLICK, value: "0"}
            - {name: APPEAL_HOLD, value: "1"}
            - {name: APPEAL_HOLD_MIN, value: "$HOLD_MIN"}
            - {name: APPEAL_WHY_B64, value: "$WHY_B64"}
            - {name: APPEAL_CONTEXT_B64, value: "$CTX_B64"}
            - {name: MAIL_USER,     valueFrom: {secretKeyRef: {name: cgpt-appeal-creds-$N, key: EMAIL}}}
            - {name: CHATGPT_EMAIL, valueFrom: {secretKeyRef: {name: cgpt-appeal-creds-$N, key: EMAIL}}}
            - {name: _VNCPW,        valueFrom: {secretKeyRef: {name: cgpt-appeal-creds-$N, key: VNC_PW}}}
          command:
            - bash
            - -c
            - |
              set -o pipefail
              mkdir -p /work/appeal-ss-$N
              echo "[pod] installing x11vnc ..."
              (apt-get update -qq && apt-get install -y -qq x11vnc) >/tmp/apt.log 2>&1 || { echo "APT-FAIL"; tail -5 /tmp/apt.log; exit 1; }
              command -v x11vnc || { echo "x11vnc MISSING after install"; exit 1; }
              Xvfb :$DISPNUM -screen 0 1440x1000x24 >/tmp/xvfb.log 2>&1 &
              for i in \$(seq 1 30); do [ -S /tmp/.X11-unix/X$DISPNUM ] && break; sleep 1; done
              [ -S /tmp/.X11-unix/X$DISPNUM ] || { echo "FATAL Xvfb"; tail /tmp/xvfb.log; exit 1; }
              echo "xvfb ready"
              mkdir -p /root/.vnc
              x11vnc -storepasswd "\$_VNCPW" /root/.vnc/passwd >/dev/null 2>&1
              # 只绑内网 IP, 禁绑 0.0.0.0 (公网 EIP 不暴露)
              x11vnc -display :$DISPNUM -rfbauth /root/.vnc/passwd -listen $NODE_IP -rfbport $VNC_PORT \
                     -forever -shared -noxdamage -bg -o /tmp/x11vnc.log
              sleep 2; echo "[pod] x11vnc listening on $NODE_IP:$VNC_PORT"
              python3 -c "import patchright; print('patchright ready')"
              python3 /src/oauth.py 2>&1 | tee /work/appeal-log-$N.txt
YAMLEOF
echo STAGED=\$(wc -l < $YAML)
EOF
)
kx "$STAGE" | grep -E "STAGED=[0-9]+" | tail -1

kx "kubectl -n $NS delete job $JOB --ignore-not-found >/dev/null 2>&1; kubectl -n $NS delete secret cgpt-appeal-creds-$N --ignore-not-found >/dev/null 2>&1
kubectl apply -f $YAML 2>&1 | tail -3" | grep -E "secret/|job.batch/|rror" | tail -3

V=$(kx "kubectl -n $NS get job $JOB -o jsonpath='JOBOK={.metadata.name}' 2>/dev/null")
echo "$V" | grep -q "JOBOK=$JOB" || { echo "!!!! Job 未建成"; exit 1; }

cat <<TIP

[appeal-vnc] Job 已提交。等 ~40s 让 pod 装 x11vnc + 起 Xvfb + 填表(看日志):
  $JMS ssh $ASSET "kubectl -n $NS logs -f job/$JOB"
  等到出现 "x11vnc listening" 和 "[appeal] HOLD mode" 再连 VNC。

连接 VNC(另开一个终端, 保持前台运行):
  $JMS proxy $ASSET $VNC_PORT $NODE_IP $VNC_PORT

然后本地 VNC viewer 连:
  localhost:$VNC_PORT     (macOS: 打开"屏幕共享" 或 Finder→前往→连接服务器 vnc://localhost:$VNC_PORT)
  VNC 密码: $VNC_PW

进去后: 页面已填好, 你只需过 Cloudflare Turnstile(勾一下), 再点 "Submit Appeal"。
提交成功后 oauth.py 会自动检测回执文本并落图 appeal-04-receipt.png, HOLD 结束退出。
TIP
