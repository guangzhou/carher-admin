#!/bin/bash
# aliyun-grinder.sh — 全自动把一批 ChatGPT 订阅号接入 **阿里云 ACK carher ns** chatgpt 池
# ────────────────────────────────────────────────────────────────────────────
# 与 scripts/add-acct-198/grinder.sh 的关系:
#   相同: phase A OAuth(188 patchright + 3-IP CF 轮换 + 两步法 toggle + retry-until-success)
#   不同: finalize 落到阿里云 K8s(carher ns, NAS PVC, kubectl cp)而非 198 K3s hostPath;
#         注册走 prod+canary 两个 ConfigMap(不是 198 的 quota-rebalance POOL_ACCOUNTS)
#
# 由 acct-122..126 实战沉淀 (2026-07-25)。相比 aliyun-batch-add-accts.sh 的进步:
#   - 不再要求"OAuth 已完成"做前提, OAuth 段内建(含 CF 轮换重试), 端到端一条命令
#   - 支持 authenticator-app 2FA 号(CSV 第 5 列 = TOTP base32 seed)
#   - 双 CM patch 直接改 **live ConfigMap**(不改仓库 yaml, 避免把仓库里未部署的
#     callback 漂移一起推上去), 仓库 yaml 由 --write-repo 单独同步
#
# ── creds CSV(不硬编码明文进仓库) ──
#   每行: acct_num,email,mail_pw,chatgpt_pw[,totp_secret]
#   路径由 GRIND_CREDS 指定。'#' 开头行忽略。
#
# ── 用法 ──
#   GRIND_ACCTS="122 123" GRIND_CREDS=/tmp/grind-creds-aliyun.csv \
#     nohup bash scripts/aliyun-grinder.sh > /tmp/aliyun-grind.log 2>&1 &
#   tail -f /tmp/aliyun-grind.log      # 只看本地日志, 别穿隧道盯屏
#
# ── 环境要求 ──
#   - 本机 kubectl 通阿里云 (jms proxy k8s-work-226/227 16443 172.16.1.163 6443)
#   - 188 直连 ssh (SSH_188) + patchright 镜像 + /Data/chatgpt-auth/re-oauth.sh
#   - 188 /Data 有空间(≥20G;满了 OAuth 必失败 — docker image prune -f 回收)
# ────────────────────────────────────────────────────────────────────────────
set -uo pipefail

CREDS_CSV="${GRIND_CREDS:-/tmp/grind-creds-aliyun.csv}"
NS=carher
IMAGE=mcr.microsoft.com/playwright/python:v1.60.0-noble
SSH_188="${SSH_188:-cltx@10.68.13.188}"
REPO="${GRIND_REPO:-$HOME/codes/carher-admin}"
MAXATT="${GRIND_MAX_ATT:-10}"
SKIP_OAUTH="${GRIND_SKIP_OAUTH:-0}"     # 1 = 只做 finalize(auth.json 已在 188)
SKIP_CM="${GRIND_SKIP_CM:-0}"           # 1 = 不动 ConfigMap(只建 pod)

cd "$REPO" || { echo "FATAL: repo not found $REPO"; exit 1; }
[ -s "$CREDS_CSV" ] || { echo "FATAL: creds csv missing/empty: $CREDS_CSV"; exit 1; }

s8(){ ssh -o ConnectTimeout=20 -o StrictHostKeyChecking=no "$SSH_188" "$1" 2>&1; }
csv_field(){ awk -F, -v n="$1" -v c="$2" '$1==n{print $c}' "$CREDS_CSV" | head -1; }
# 3-IP egress 轮换: att%3==1→236:17890(美), ==2→236:17891(美), ==0→188直连(日)
egress_for(){ case $(( $1 % 3 )) in 1) echo 'socks5://10.68.13.236:17890';; 2) echo 'socks5://10.68.13.236:17891';; 0) echo '';; esac; }

# ── pre-check ──────────────────────────────────────────────────────────────
echo "==[pre-check]=="
kubectl get ns "$NS" >/dev/null 2>&1 || { echo "FATAL: kubectl 没连通阿里云 ACK"; exit 1; }
# 188 只在**真要用它跑 OAuth** 时才检查/同步。走阿里云 EIP 路径(SKIP_OAUTH=1 +
# 本机已有 auth.json)时 188 完全不参与, 不该因它磁盘满/掉线就 FATAL 退出。
NEED_188=1
if [ "$SKIP_OAUTH" = "1" ]; then
  NEED_188=0
  for N in ${GRIND_ACCTS:-}; do
    [ -s "/tmp/auth-acct-$N.json" ] || NEED_188=1
  done
fi
if [ "$NEED_188" = "0" ]; then
  echo "  ✓ 走 EIP 路径(本机 auth.json 齐备), 跳过 188 检查"
else
DATA_USE=$(s8 'df --output=pcent /Data | tail -1' | tr -dc 0-9)
echo "  188 /Data ${DATA_USE}%"
[ "${DATA_USE:-100}" -ge 95 ] && { echo "FATAL: 188 /Data ${DATA_USE}% ≥95% — 先 docker image prune -f"; exit 1; }
s8 "docker image inspect $IMAGE >/dev/null 2>&1 && echo image-ok" | grep -q image-ok || { echo "FATAL: 188 缺 patchright 镜像"; exit 1; }

# 同步最新 oauth.py / toggle.py / re-oauth.sh 到 188(TOTP 支持随之传播)
for f in chatgpt-litellm-oauth.py chatgpt-enable-codex-toggle.py; do
  cat "scripts/chatgpt-onboard/$f" | ssh -o ConnectTimeout=20 "$SSH_188" "cat > /tmp/$f" 2>/dev/null
done
cat scripts/chatgpt-onboard/re-oauth.sh | ssh -o ConnectTimeout=20 "$SSH_188" "cat > /Data/chatgpt-auth/re-oauth.sh && chmod +x /Data/chatgpt-auth/re-oauth.sh" 2>/dev/null
echo "  ✓ 脚本已同步到 188"
fi

ACCTS="${GRIND_ACCTS:-$(awk -F, '/^[0-9]/{print $1}' "$CREDS_CSV" | tr '\n' ' ')}"
OK=""

for N in $ACCTS; do
  EMAIL=$(csv_field "$N" 2); MPW=$(csv_field "$N" 3); GPW=$(csv_field "$N" 4); TSEC=$(csv_field "$N" 5)
  [ -n "$EMAIL" ] || { echo "!!!! acct-$N no creds in $CREDS_CSV, skip"; continue; }
  echo "════════ acct-$N $EMAIL $([ -n "$TSEC" ] && echo '(2FA)') ════════"

  # ── A. 写 .creds(单引号包裹防 shell 元字符撕碎密码) ──────────────────
  {
    printf "email='%s'\nmail_pw='%s'\nchatgpt_pw='%s'\n" "$EMAIL" "$MPW" "$GPW"
    [ -n "$TSEC" ] && printf "totp_secret='%s'\n" "$TSEC"
  } > /tmp/creds-$N.txt
  cat /tmp/creds-$N.txt | ssh -o ConnectTimeout=20 "$SSH_188" \
    "mkdir -p /Data/chatgpt-auth/acct-$N && cat > /Data/chatgpt-auth/acct-$N/.creds && chmod 600 /Data/chatgpt-auth/acct-$N/.creds && wc -l < /Data/chatgpt-auth/acct-$N/.creds" 2>/dev/null

  # ── B. OAuth (retry-until-success, 3-IP 轮换) ─────────────────────────
  GOT=0
  PRE=$(s8 "python3 -c 'import json;print(1 if len(json.load(open(\"/tmp/auth-acct-$N.json\")).get(\"access_token\",\"\"))>1000 else 0)' 2>/dev/null" | tr -dc 0-9)
  if [ "${PRE:-0}" = "1" ]; then echo "  ✓ acct-$N 已有有效 token → 跳过 OAuth"; GOT=1; fi
  [ "$SKIP_OAUTH" = "1" ] && GOT=1

  TOGGLE_TRIED=0
  for att in $(seq 1 "$MAXATT"); do
    [ "$GOT" = "1" ] && break
    PX=$(egress_for $att); LBL=$([ -n "$PX" ] && echo "$PX" || echo "188-JP")
    echo "===== acct-$N attempt $att egress=$LBL $(date +%H:%M:%S) ====="
    s8 "docker ps --filter ancestor=$IMAGE -q | xargs -r docker kill >/dev/null 2>&1"
    # 归档上一轮日志/截图再清空(否则失败证据被下一轮 rm 毁掉)
    s8 "[ -s /tmp/oauth-acct-$N.log ] && cp -f /tmp/oauth-acct-$N.log /tmp/oauth-acct-$N.log.prev 2>/dev/null; [ -d /tmp/screenshots-acct-$N ] && { rm -rf /tmp/screenshots-acct-$N.prev; cp -a /tmp/screenshots-acct-$N /tmp/screenshots-acct-$N.prev 2>/dev/null; }; docker run --rm -v /tmp:/t busybox rm -rf /t/auth-acct-$N.json >/dev/null 2>&1; rm -f /tmp/oauth-acct-$N.log /tmp/auth-acct-$N.json; rm -rf /tmp/screenshots-acct-$N; setsid bash -c 'OAUTH_PROXY=$PX MAIL_OTP_PROVIDER=mailcom GEN_ONLY=1 bash /Data/chatgpt-auth/re-oauth.sh acct-$N 2>&1 | stdbuf -oL tee /tmp/oauth-acct-$N.log' </dev/null >/dev/null 2>&1 & disown; sleep 2; echo launched"
    V=0
    for w in $(seq 1 54); do
      sleep 10
      V=$(s8 "python3 -c 'import json;print(1 if len(json.load(open(\"/tmp/auth-acct-$N.json\")).get(\"access_token\",\"\"))>1000 else 0)' 2>/dev/null" | tr -dc 0-9)
      [ "${V:-0}" = "1" ] && break
      CT=$(s8 "docker ps --filter ancestor=$IMAGE -q | wc -l" | tr -dc 0-9)
      [ "${CT:-0}" = "0" ] && [ "$w" -gt 3 ] && break
    done
    echo "  -> acct-$N att $att egress=$LBL auth_valid=${V:-0}"
    [ "${V:-0}" = "1" ] && { GOT=1; break; }
    # 账号级死路: deactivated 重试无意义, 立即放弃该号
    if s8 "grep -qE 'ACCOUNT_DEACTIVATED|account is on hold' /tmp/oauth-acct-$N.log 2>/dev/null && echo Y" | grep -q Y; then
      echo "  !!!! acct-$N 账号级失效(deactivated/hold) — 重试无意义, skip"; break
    fi
    # 两步法: 连续 ≥2 次失败且没开过 toggle → 疑似 Codex toggle off, 先开 toggle
    if [ "$att" -ge 2 ] && [ "$TOGGLE_TRIED" = 0 ]; then
      if s8 "grep -qE 'toggle: false . false|aria_disabled=true|Enable device code|consent Continue disabled' /tmp/oauth-acct-$N.log 2>/dev/null && echo Y" | grep -q Y; then
        echo "  [两步法] acct-$N 疑似 Codex toggle off → 独立跑 toggle enable"
        for tg in 1 2 3; do
          TGPX=$(egress_for $tg)
          echo "  [两步法] toggle attempt $tg egress=$([ -n "$TGPX" ] && echo "$TGPX" || echo 188-JP)"
          TGOUT=$(s8 "docker ps --filter ancestor=$IMAGE -q | xargs -r docker kill >/dev/null 2>&1
sd=\$(mktemp -d /tmp/tgl-$N-XXXX)
unq(){ grep -E \"^\$1=\" /Data/chatgpt-auth/acct-$N/.creds|head -1|cut -d= -f2-|sed -E \"s/^'(.*)'\\\$/\\\\1/\"; }
pw=\$(unq chatgpt_pw); mpw=\$(unq mail_pw); em=\$(unq email)
printf '%s' \"\\\$pw\">\$sd/p.txt; printf '%s' \"\\\$mpw\">\$sd/m.txt
docker run --rm -v /tmp/chatgpt-enable-codex-toggle.py:/work/script.py:ro \
  -v \$sd/p.txt:/run/chatgpt_pw.txt:ro -v \$sd/m.txt:/run/mail_pw.txt:ro -v \$sd:/work/screenshots \
  -e CHATGPT_EMAIL=\$em -e CHATGPT_PW_FILE=/run/chatgpt_pw.txt -e MAIL_PW_FILE=/run/mail_pw.txt \
  -e SCREENSHOT_DIR=/work/screenshots -e ACTION=enable-codex-toggle -e MAIL_OTP_PROVIDER=mailcom \
  -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright -e OAUTH_PROXY=$TGPX -e DISPLAY=:99 -e TOTP_SECRET='$TSEC' $IMAGE \
  bash -c 'Xvfb :99 -screen 0 1440x1000x24 >/dev/null 2>&1 & sleep 1 && pip install patchright==1.60.0 -q --root-user-action=ignore >/dev/null 2>&1 && python3 /work/script.py' 2>&1 | grep -E 'RESULT|ENABLED|FAILED|aria-checked=true'
rm -rf \$sd")
          echo "$TGOUT" | sed 's/^/    tgl> /'
          echo "$TGOUT" | grep -qE 'RESULT=ENABLED|aria-checked=true' && { echo "  [两步法] toggle 开启成功 (att $tg)"; break; }
          sleep 3
        done
        TOGGLE_TRIED=1
      fi
    fi
    sleep 3
  done
  [ "$GOT" != "1" ] && { echo "!!!! acct-$N OAuth FAILED — skip(不做半接入)"; continue; }

  # ── C. 建阿里云资源 (PVC + Deployment + Service), 以 acct-75 为活模板 ──
  echo "===== acct-$N 建阿里云资源 ====="
  if ! kubectl -n $NS get deploy chatgpt-acct-$N >/dev/null 2>&1; then
    ACCT_N=$N python3 - > /tmp/aliyun-acct-$N.yaml <<'PY'
import os
n = os.environ["ACCT_N"]
print(f"""---
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {{name: chatgpt-acct-{n}-auth, namespace: carher, labels: {{pool: chatgpt-acct, account: "{n}"}}}}
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: alibabacloud-cnfs-nas
  resources: {{requests: {{storage: 1Gi}}}}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: chatgpt-acct-{n}
  namespace: carher
  labels: {{app: chatgpt-acct-{n}, pool: chatgpt-acct, account: "{n}"}}
spec:
  replicas: 1
  strategy: {{type: Recreate}}
  selector:
    matchLabels: {{app: chatgpt-acct-{n}}}
  template:
    metadata:
      labels: {{app: chatgpt-acct-{n}, pool: chatgpt-acct, account: "{n}"}}
    spec:
      containers:
        - name: litellm
          image: ghcr.io/berriai/litellm:v1.90.2
          imagePullPolicy: IfNotPresent
          args: ["--config", "/app/config.yaml", "--port", "4000"]
          ports: [{{containerPort: 4000}}]
          env:
            - {{name: CHATGPT_TOKEN_DIR, value: /chatgpt-auth}}
            - name: LITELLM_MASTER_KEY
              valueFrom:
                secretKeyRef: {{name: chatgpt-pool-master-key, key: LITELLM_MASTER_KEY}}
          resources:
            requests: {{cpu: 100m, memory: 256Mi}}
            limits: {{cpu: "1", memory: 2Gi}}
          readinessProbe:
            httpGet: {{path: /health/readiness, port: 4000}}
            initialDelaySeconds: 30
            periodSeconds: 10
            failureThreshold: 12
          volumeMounts:
            - {{name: config, mountPath: /app/config.yaml, subPath: config.yaml, readOnly: true}}
            - {{name: auth, mountPath: /chatgpt-auth}}
            - {{name: responses-patch, subPath: transformation.py,
                mountPath: /app/.venv/lib/python3.13/site-packages/litellm/llms/chatgpt/responses/transformation.py}}
      volumes:
        - {{name: config, configMap: {{name: chatgpt-pool-config, defaultMode: 420}}}}
        - {{name: auth, persistentVolumeClaim: {{claimName: chatgpt-acct-{n}-auth}}}}
        - {{name: responses-patch, configMap: {{name: chatgpt-responses-patch, defaultMode: 420}}}}
---
apiVersion: v1
kind: Service
metadata:
  name: chatgpt-acct-{n}
  namespace: carher
  labels: {{app: chatgpt-acct-{n}, pool: chatgpt-acct, account: "{n}"}}
spec:
  type: ClusterIP
  selector: {{app: chatgpt-acct-{n}}}
  ports: [{{port: 4000, targetPort: 4000}}]""")
PY
    # apply 带重试 + presence 校验(隧道抖动会静默丢 deploy → 半接入)
    APPLIED=0
    for t in 1 2 3 4 5; do
      kubectl apply -f /tmp/aliyun-acct-$N.yaml >/dev/null 2>&1
      kubectl -n $NS get deploy chatgpt-acct-$N >/dev/null 2>&1 && { APPLIED=1; break; }
      sleep 3
    done
    [ "$APPLIED" = 1 ] || { echo "!!!! acct-$N apply 失败(隧道抖动?) — skip 防半接入"; continue; }
    echo "  ✓ PVC+Deployment+Service 已建"
  else
    echo "  ✓ deploy 已存在"
  fi

  # ── D. auth.json 落 PVC, 空壳兜底校验 ──────────────────────────────────
  # 来源二选一:
  #   走阿里云 EIP 路径(首选): auth.json 已由 aliyun-eip-onboard.sh 取回到本机
  #     /tmp/auth-acct-N.json → 直接用, 不去 188 拉(188 上根本没有这文件)。
  #   走 188 路径(fallback): 从 188 /tmp 拉回。
  if [ -s "/tmp/auth-acct-$N.json" ] && python3 -c "import json,sys;sys.exit(0 if len(json.load(open('/tmp/auth-acct-$N.json')).get('access_token',''))>1000 else 1)" 2>/dev/null; then
    echo "  ✓ acct-$N 用本机已有 auth.json (EIP 路径)"
  else
    s8 "cat /tmp/auth-acct-$N.json" > /tmp/auth-acct-$N.json
  fi
  python3 -c "import json,sys; d=json.load(open('/tmp/auth-acct-$N.json')); sys.exit(0 if len(d.get('access_token',''))>1000 else 1)" \
    || { echo "!!!! acct-$N 本机 auth.json 无效 — skip"; continue; }

  for vfix in 1 2 3; do
    POD=""
    for i in $(seq 1 30); do
      POD=$(kubectl -n $NS get pod -l app=chatgpt-acct-$N -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
      [ -n "$POD" ] && [ "$(kubectl -n $NS get pod $POD -o jsonpath='{.status.phase}' 2>/dev/null)" = "Running" ] && break
      sleep 4
    done
    [ -n "$POD" ] || { echo "  ⚠ acct-$N pod 未起, 等待重试"; sleep 10; continue; }
    kubectl cp /tmp/auth-acct-$N.json $NS/$POD:/chatgpt-auth/auth.json -c litellm >/dev/null 2>&1
    kubectl -n $NS rollout restart deploy/chatgpt-acct-$N >/dev/null 2>&1
    kubectl -n $NS rollout status deploy/chatgpt-acct-$N --timeout=180s 2>&1 | tail -1
    sleep 10
    VPOD=$(kubectl -n $NS get pod -l app=chatgpt-acct-$N -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
    PODTOK=$(kubectl -n $NS exec $VPOD -c litellm -- python3 -c \
      "import json;print(len(json.load(open('/chatgpt-auth/auth.json')).get('access_token','')))" 2>/dev/null | tr -dc 0-9)
    if [ "${PODTOK:-0}" -gt 1000 ]; then echo "  ✓ acct-$N pod auth 有效 (access_len=$PODTOK)"; break; fi
    echo "  ⚠ acct-$N pod auth 空壳 (access_len=${PODTOK:-0}) — 第 $vfix 轮重写"
  done
  [ "${PODTOK:-0}" -gt 1000 ] || { echo "!!!! acct-$N auth 落盘失败 — skip 注册"; continue; }
  OK="$OK $N"
done

[ -z "$OK" ] && { echo "GRINDER DONE ok=(none) $(date +%H:%M:%S)"; exit 0; }

# ── E. 双 CM patch (prod litellm-config + canary litellm-config-canary) ────
# 必须两个都改: her 实例默认走 prod, 只改 canary = 新 acct idle 不入轮询。
if [ "$SKIP_CM" = "0" ]; then
  echo "===== 双 ConfigMap patch (prod + canary) ====="
  for CM in litellm-config litellm-config-canary; do
    kubectl -n $NS get cm $CM -o jsonpath='{.data.config\.yaml}' > /tmp/$CM.cur 2>/dev/null
    [ -s /tmp/$CM.cur ] || { echo "  ⚠ $CM 读不到, skip"; continue; }
    python3 scripts/aliyun-cm-add-acct.py /tmp/$CM.cur /tmp/$CM.new $OK
    # 校验: 新 CM 必须是合法 yaml, 且新 acct 的 entry 数 == 该 CM 里参照 acct 的 entry 数。
    # ⚠ 不能硬编码 7: prod 有 7 个 chatgpt model 组, 但 canary 只有 4 个
    # (缺 5.6-sol/terra/luna), 写死 7 会让 canary 永远校验失败(2026-07-25 实证)。
    # 以同 CM 内的存量 acct 为基线, 谁多谁少都能自适应, 也能抓出"漏加组"。
    python3 - /tmp/$CM.new "$OK" <<'PY'
import sys, yaml, re
from collections import Counter
cfg = open(sys.argv[1]).read()
d = yaml.safe_load(cfg)            # 语法坏了这里就炸, 不会 apply 上去
assert isinstance(d.get("model_list"), list), "model_list missing"
new_accts = set(sys.argv[2].split())
cnt = Counter()
for e in d["model_list"]:
    if not isinstance(e, dict):
        continue
    m = re.fullmatch(r"chatgpt-acct-(\d+)/.+", str((e.get("model_info") or {}).get("id", "")))
    if m:
        cnt[m.group(1)] += 1
base = [c for a, c in cnt.items() if a not in new_accts]
assert base, "该 CM 内没有存量 chatgpt-acct 参照"
expect = max(set(base), key=base.count)   # 存量 acct 的众数即该 CM 的组数
for n in sorted(new_accts):
    print(f"    acct-{n}: {cnt[n]} entries (baseline {expect})")
    assert cnt[n] == expect, f"acct-{n} 应 {expect} entries, 实际 {cnt[n]}"
print(f"  ✓ yaml 合法 + entry 数与基线一致 ({expect})")
PY
    [ $? -eq 0 ] || { echo "  ❌ $CM 校验失败, 不 apply"; continue; }
    kubectl -n $NS create cm $CM --from-file=config.yaml=/tmp/$CM.new --dry-run=client -o yaml \
      | kubectl -n $NS apply -f - >/dev/null 2>&1 && echo "  ✓ $CM applied"
  done

  echo "  rollout prod + canary..."
  kubectl -n $NS rollout restart deploy/litellm-proxy >/dev/null 2>&1
  kubectl -n $NS rollout restart deploy/litellm-proxy-canary >/dev/null 2>&1
  kubectl -n $NS rollout status deploy/litellm-proxy --timeout=600s 2>&1 | tail -1
  kubectl -n $NS rollout status deploy/litellm-proxy-canary --timeout=300s 2>&1 | tail -1
fi

# ── F. smoke: 每个新 acct 直打自己的 entry (id 精确定位, 不靠 LB 抽奖) ────
echo "===== smoke ====="
PPOD=$(kubectl -n $NS get pod -l app=litellm-proxy -o jsonpath='{range .items[*]}{.metadata.name} {.status.containerStatuses[0].ready}{"\n"}{end}' 2>/dev/null | awk '$2=="true"{print $1; exit}')
MK=$(kubectl -n $NS exec $PPOD -c litellm -- printenv LITELLM_MASTER_KEY 2>/dev/null | tr -d '\r\n')
for N in $OK; do
  APOD=$(kubectl -n $NS get pod -l app=chatgpt-acct-$N -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
  # 直打该 acct 自己的 litellm(绕过 proxy LB), 确认上游 token 真能用。
  # ⚠ 必须用 python3 而不是 curl: ghcr litellm 镜像里**没有 curl**, 用 curl 会
  # 一律返回空 → 报 HTTP ERR, 看着像账号坏了, 其实是探针坏了(2026-07-25 实证)。
  # ⚠ 必须流式(stream:true): chatgpt responses 上游非流式会空 output。
  R=$(kubectl -n $NS exec $APOD -c litellm -- python3 -c "
import json,os,urllib.request
r=urllib.request.Request('http://localhost:4000/v1/chat/completions',
 data=json.dumps({'model':'chatgpt-gpt-5.5','messages':[{'role':'user','content':'hi'}],
                  'max_tokens':8,'stream':True}).encode(),
 headers={'Authorization':'Bearer '+os.environ['LITELLM_MASTER_KEY'],
          'Content-Type':'application/json'})
try:
    print(urllib.request.urlopen(r,timeout=90).status)
except Exception as e:
    print(getattr(e,'code','ERR'))
" 2>/dev/null | tr -dc 0-9)
  echo "  acct-$N direct chatgpt-gpt-5.5 -> HTTP ${R:-ERR}"
done
echo "GRINDER DONE ok=$OK $(date +%H:%M:%S)"
