#!/usr/bin/env bash
# aliyun-eip-onboard-via-jms.sh — 同 aliyun-eip-onboard.sh(阿里云新加坡 EIP 节点跑
# patchright OAuth/toggle),区别只在**kubectl 在 226 节点上跑**,不依赖本地隧道。
#
# 为什么要这条备用通道:
#   aliyun-eip-onboard.sh 用本地 kubectl(127.0.0.1:16443 ← jms proxy)。而 jms proxy 走
#   KoKo **非 TTY** exec channel + 远端 nc 中转;2026-08-01 实测 k8s-work-226/227 的非 TTY
#   exec channel 整体挂死(裸 `jms ssh <asset> "echo ok"` 就永久 hang),proxy 起得来但
#   TLS handshake timeout,换 hop 无效 —— 与 memory feedback_jms_laoyang_must_use_tty 一致。
#   `jms ssh --tty` 正常,且 226/227 节点自带 /usr/bin/kubectl 且有集群凭证。
#   故本脚本把 manifest 用 heredoc 喂给节点上的 kubectl(已实测含引号/$ 的 YAML 不被撕碎)。
#
# 出口隔离铁律(与原脚本相同): hostNetwork + 钉 EIP 节点。
#   普通 pod 走共享 NAT 47.84.112.136 = 线上 codex acct 出口,在普通 pod 里撞 CF 会污染它。
#   偶数号钉 .86 / 奇数号钉 .122(cf_clearance 绑 IP,同号务必同节点)。
#
# 用法:
#   bash scripts/aliyun-eip-onboard-via-jms.sh <oauth|toggle> <N> [CSV]
#     CSV 默认 /tmp/grind-creds.csv,格式 N,email,mail_pw,chatgpt_pw[,totp_secret]
#
# ⚠️ 一批号要**一个一个提交**,等前一个 Job 到终态(S=1/F=1)再交下一个。脚本已按号唯一化
#   DISPLAY(消除 X 冲突)并加了同节点占用预检(ALLOW_PARALLEL=1 可跳过),但 EIP 节点 memory
#   已 220% 超卖,同节点两个 chromium 仍会互相 OOM。"两个 EIP 节点可以并行"这条只在
#   **每节点 1 个**时成立。
#
# 产物: auth.json 落工作区 PVC,脚本结束时取回本地 /tmp/auth-acct-<N>.json
#       (供 scripts/add-acct-198/grinder.sh 的"已有有效 token 跳过 OAuth"分支消费)
set -uo pipefail

ACTION="${1:?usage: $0 <oauth|toggle> <N> [creds.csv]}"
N="${2:?}"
CSV="${3:-/tmp/grind-creds.csv}"

ASSET="${KVIA_ASSET:-k8s-work-226}"      # 跑 kubectl 的节点(任一 worker 均可)
NS=carher
REG=cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher
IMG="$REG:zerokey-capture-aliyun-20260707-otp3"   # 含 patchright + Xvfb + chromium
PVC=chatgpt-onboard-work
# 复用已在集群里的脚本 CM(内容 == 仓库 HEAD 版 oauth.py/toggle.py,已 sha256 核对)。
# 若被清掉,用 aliyun-eip-onboard.sh 重建(它带 --from-file),或换一个还在的编号。
SRC_CM="${SRC_CM:-cgpt-onboard-src-131}"
JOB="cgpt-onboard-${ACTION}-${N}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
JMS="$REPO/scripts/jms"

if [ $((N % 2)) -eq 0 ]; then NODE=ap-southeast-1.172.16.0.86; else NODE=ap-southeast-1.172.16.16.122; fi

# ⚠️ DISPLAY 必须**按号唯一**,不能固定 :99 —— Job 是 hostNetwork,X server 的抽象 unix
# socket(@/tmp/.X11-unix/X<n>)和 TCP 端口(6000+n)都活在**宿主机 network namespace**,
# 同一节点上第二个 Job 起 Xvfb :99 必然 "Cannot establish any listening sockets -
# Make sure an X server isn't already running" → FATAL: Xvfb 30s 未就绪(容器里 /tmp 是
# 私有的, 所以文件系统那半边看不出冲突, 只有抽象 socket/TCP 会撞)。
# 2026-08-05 实证: 并行提交 140~144 五个 toggle, 落同节点的 142/143/144 全挂在这里
# (loglines=6, 没走到网络 → 零成本可重跑)。见 memory
# feedback_xvfb_display99_hostnetwork_collides_same_node。
DISPNUM="$N"

# kx <timeout> <remote-script>  — 在节点上跑一段 shell(PTY 通道)
kx(){ local to="$1"; shift; "$JMS" ssh --tty --timeout "$to" "$ASSET" "$1" 2>&1; }

# ── 同节点占用预检 ────────────────────────────────────────────────────────────
# DISPLAY 唯一化已经消除了 X 冲突, 但同节点并行仍会抢 CPU/内存(EIP 节点 memory limits
# 已 220% 超卖, chromium 一冲高就被 OOM kill, 症状是 TargetClosedError)。所以默认仍然
# **一个节点同时只跑一个号**; 确知节点有余量时用 ALLOW_PARALLEL=1 跳过。
if [ "${ALLOW_PARALLEL:-0}" != "1" ]; then
  BUSY=$(kx 120 "kubectl -n $NS get pod -l job-name --field-selector spec.nodeName=$NODE,status.phase=Running -o name 2>/dev/null | grep cgpt-onboard- | grep -v '\\-${ACTION}-${N}-' | head -3")
  BUSY=$(echo "$BUSY" | grep -oE 'pod/cgpt-onboard-[a-z]+-[0-9]+-[a-z0-9]+' | head -3)
  if [ -n "$BUSY" ]; then
    echo "!!!! 节点 $NODE 上已有 onboard pod 在跑,先等它结束(串行是硬要求,见脚本注释):"
    echo "$BUSY" | sed 's/^/       /'
    echo "     确知节点有余量要强行并行: ALLOW_PARALLEL=1 $0 $ACTION $N $CSV"
    exit 1
  fi
fi

# ── creds(本地 CSV → base64,走 Secret data 避免所有引号/元字符问题) ──────────
row=$(awk -F, -v n="$N" '$1==n{print;exit}' "$CSV")
[ -n "$row" ] || { echo "FATAL: acct-$N not in $CSV"; exit 1; }
EMAIL=$(echo "$row" | cut -d, -f2)
MPW=$(echo "$row"   | cut -d, -f3)
GPW=$(echo "$row"   | cut -d, -f4)
TSEC=$(echo "$row"  | cut -d, -f5)
[ -n "$EMAIL" ] && [ -n "$MPW" ] && [ -n "$GPW" ] || { echo "FATAL: acct-$N creds incomplete"; exit 1; }
b64(){ printf '%s' "$1" | base64 | tr -d '\n'; }

if [ "$ACTION" = "toggle" ]; then
  CMD='python3 /src/toggle.py'
  EXTRA_ENV='- {name: ACTION, value: enable-codex-toggle}'
else
  CMD='python3 /src/oauth.py'
  EXTRA_ENV='- {name: GEN_ONLY, value: "1"}'
fi

echo "[via-jms] acct-$N action=$ACTION node=$NODE src_cm=$SRC_CM asset=$ASSET"

# ── 三段式: 先把 manifest 落到节点文件, 再 apply, 再独立 verify ─────────────────
# 为什么不一把 heredoc | kubectl apply: PTY 通道的输出捕获会截断(jms _run_tty 读到
# sentinel 即 kill ssh),apply 的成败行经常收不到 → 会把失败读成成功。所以**永不信任
# 捕获到的输出**,一律回查对象是否存在(2026-08-01 实证: Secret 建成/Job 没建成却无报错)。
YAML="/tmp/cgpt-onboard-$ACTION-$N.yaml"
STAGE=$(cat <<EOF
cat > $YAML <<'YAMLEOF'
apiVersion: v1
kind: Secret
metadata: {name: cgpt-onboard-creds-$N, namespace: $NS}
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
              rm -f /work/auth-acct-$N.json
              printf '%s' "\$_MPW" > /run/mail_pw && chmod 600 /run/mail_pw
              printf '%s' "\$_GPW" > /run/chatgpt_pw && chmod 600 /run/chatgpt_pw
              unset _MPW _GPW
              # 等 Xvfb 真的起来, 不能用固定 sleep 2: 节点负载高时 X server 还没就绪,
              # chromium 直接 "Missing X server or \$DISPLAY" 然后 gracefully close,
              # patchright 报 TargetClosedError(与 OOM 症状同名但根因不同, 2026-08-02
              # acct-101 实证)。以 X socket 出现为就绪判据, 最多等 30s。
              # DISPLAY 按号唯一(:$DISPNUM), 不是 :99 —— hostNetwork 下 X 的 socket/端口
              # 在宿主 netns, 固定 :99 会让同节点第二个 Job 必挂(见脚本头部注释)。
              Xvfb :$DISPNUM -screen 0 1440x1000x24 >/tmp/xvfb.log 2>&1 &
              for i in \$(seq 1 30); do [ -S /tmp/.X11-unix/X$DISPNUM ] && break; sleep 1; done
              if [ ! -S /tmp/.X11-unix/X$DISPNUM ]; then
                echo "FATAL: Xvfb 30s 未就绪(display :$DISPNUM)"; tail -20 /tmp/xvfb.log
                grep -q "already running" /tmp/xvfb.log && \
                  echo "HINT: display :$DISPNUM 被同节点另一个 Job 占了 —— 串行跑, 别并行"
                exit 1
              fi
              echo "xvfb ready after \${i}s"
              python3 -c "import patchright; print('patchright ready')"
              $CMD 2>&1 | tee /work/log-$ACTION-$N.txt
              # auth.json 直接 base64 打进 job 日志: 取回就只是 kubectl logs, 不必再起
              # 挂 PVC 的 reader pod(carher ns 有零中断 hook 挡 delete pod, 少碰为妙)。
              if [ -s /work/auth-acct-$N.json ]; then
                echo B64BEGIN
                base64 /work/auth-acct-$N.json
                echo B64END
              fi
YAMLEOF
echo STAGED_LINES=\$(wc -l < $YAML)
EOF
)
kx 120 "$STAGE" | grep -E "STAGED_LINES=[0-9]+" | tail -1

kx 240 "kubectl -n $NS delete job $JOB --ignore-not-found >/dev/null 2>&1
kubectl apply -f $YAML 2>&1 | tail -4
echo APPLY_RC=\$?" | grep -E "secret/|job.batch/|APPLY_RC|rror" | tail -4

# verify: 对象必须真的在(不信任上面的输出)
V=$(kx 120 "kubectl -n $NS get job $JOB -o jsonpath='JOBOK={.metadata.name}' 2>/dev/null; echo; kubectl -n $NS get secret cgpt-onboard-creds-$N -o go-template='SECKEYS={{len .data}}{{\"\\n\"}}' 2>/dev/null")
echo "$V" | grep -oE "JOBOK=$JOB|SECKEYS=[0-9]+" | sort -u
echo "$V" | grep -q "JOBOK=$JOB" || { echo "!!!! acct-$N Job 未建成 — 别当成功往下走"; exit 1; }
echo "$V" | grep -qE "SECKEYS=4" || { echo "!!!! acct-$N Secret 键数 != 4(TOTP_SECRET 空会被丢 → secretKeyRef 起不来 pod)"; exit 1; }
echo "[via-jms] job submitted; 跟进/取回: bash scripts/aliyun-eip-onboard-watch.sh $N $ACTION"
