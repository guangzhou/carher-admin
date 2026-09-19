#!/bin/bash
# add-chatgpt-acct-198-devcode.sh — onboard 1 acct to 198 pool via MANUAL device-code
# (2026-07-20: fallback when CF Turnstile blocks patchright full-auto login).
#
# Usage: ./add-chatgpt-acct-198-devcode.sh <N> <email> <mail_pw> [node]
#   [node]: 198 (default) — pod on the 198 node, auth on local-path PVC.
#           standby       — pod pinned to aiyjy-litellm-standby (225 machine) on a
#                           static PV, to offload 198 memory. See memory
#                           project_new_acct_onboard_directly_on_225_standby.
#   Phase A: applies manifest (if missing) + issues a device code on 188 (direct,
#            plain-urllib + Originator header — passes CF where the browser can't).
#   Phase B: YOU open ONLY https://auth.openai.com/codex/device, log in with the
#            email + mail_pw, enter the code, Authorize, then CLOSE the browser.
#            *** NEVER open chatgpt.com afterwards — web login revokes the token
#                (token_invalidated). See memory feedback_chatgpt_oauth_no_web_login_verify.
#   Phase C: script polls 188 for auth.json → verifies /v1/me 200 → writes it into
#            the PVC (198: hostPath busybox cp into the local-path PVC; standby: a
#            writer pod with hostPath DirectoryOrCreate seeds auth.json onto the
#            standby node, then a static PV/PVC is bound) → resume_acct registers
#            all 6 models → rollout → smoke.
#
# Env (defaults are the 198 prod values):
#   LITELLM_POOL_KEY_198  sub-proxy key   (default sk-chatgpt-198-...)
#   LITELLM_MK_198        prod master key (default sk-pro-litellm-...)
#   JOIN_POOL             1 (default) = register the acct into the LiteLLM rotation
#                         pool (quota-rebalance POOL_ACCOUNTS + resume_acct 6 models +
#                         proxy rollout + smoke). 0 = onboard the pod ONLY (auth seeded,
#                         static PV/PVC bound, deploy Running) and STOP — the acct is a
#                         cold standby, invisible to routing until you join it later.
#   POOL_ONLY             1 = skip Phase A/B/C-staging (device code + auth seed + PV/PVC
#                         + deploy patch) and ONLY join the pool. Use this to promote an
#                         acct previously onboarded with JOIN_POOL=0 into rotation
#                         WITHOUT re-issuing a device code. Implies JOIN_POOL=1.
#                         Default 0.
#   STAGE_ONLY            1 = the auth.json is ALREADY on 188 at /tmp/auth-<acct>.json
#                         (e.g. produced by the automated aliyun-EIP patchright path).
#                         Skip Phase A2 (device-code issuance) — just ensure the deploy,
#                         stage the existing token (198 or standby), then honor JOIN_POOL.
#                         Enables FULLY-AUTOMATED cold-standby onboard. Default 0.
set -uo pipefail
N="${1:?usage: $0 <N> <email> <mail_pw> [node]}"; EMAIL="${2:?email}"; MAIL_PW="${3:?mail_pw}"
NODE_MODE="${4:-198}"   # 198 | standby
case "$NODE_MODE" in 198|standby) ;; *) echo "FATAL: node must be 198 or standby (got '$NODE_MODE')"; exit 1;; esac
JOIN_POOL="${JOIN_POOL:-1}"   # 1=join LiteLLM rotation pool | 0=onboard pod only (cold standby)
POOL_ONLY="${POOL_ONLY:-0}"   # 1=skip staging, only join the pool (acct already onboarded)
[ "$POOL_ONLY" = 1 ] && JOIN_POOL=1   # promoting an already-onboarded acct always joins
case "$JOIN_POOL" in 0|1) ;; *) echo "FATAL: JOIN_POOL must be 0 or 1 (got '$JOIN_POOL')"; exit 1;; esac
case "$POOL_ONLY" in 0|1) ;; *) echo "FATAL: POOL_ONLY must be 0 or 1 (got '$POOL_ONLY')"; exit 1;; esac
STAGE_ONLY="${STAGE_ONLY:-0}"   # 1=auth.json already on 188 /tmp/auth-<acct>.json; skip Phase A2 device code
case "$STAGE_ONLY" in 0|1) ;; *) echo "FATAL: STAGE_ONLY must be 0 or 1 (got '$STAGE_ONLY')"; exit 1;; esac
{ [ "$STAGE_ONLY" = 1 ] && [ "$POOL_ONLY" = 1 ]; } && { echo "FATAL: STAGE_ONLY and POOL_ONLY are mutually exclusive"; exit 1; }
STANDBY_NODE=aiyjy-litellm-standby
NS=litellm-product
POOL_KEY="${LITELLM_POOL_KEY_198:-sk-chatgpt-198-d8a3f4e62b9c1057ef324918a7b6d3e0}"
MK="${LITELLM_MK_198:?需要 export LITELLM_MK_198=<198 prod master key>；脚本不再内置默认值}"
ENDPOINT=https://cc.auto-link.com.cn/pro
# 2026-08-19: 新号 pod 镜像固定为 acct-226 的 build（cache-session-fix-v2）。
# 用户指令「以后增加acct都用这个image」。可用 env ACCT_IMAGE 覆盖（per-run）。
# ⚠️ 换镜像时：改这里 + 确保新 tag 已 push 到 198 与 225(standby) 两个节点的 127.0.0.1:5000。
ACCT_IMAGE="${ACCT_IMAGE:-127.0.0.1:5000/litellm-carher:vanilla-v1.90.2.cache-session-fix-v2-20260817-103630}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
ACCT="acct-$N"; PORT=$((4000 + N))
log(){ echo "[$ACCT $(date +%H:%M:%S)] $*"; }
jr(){ local o; for t in 1 2 3 4 5 6; do o=$(jms ssh AIYJY-litellm "$1" 2>&1); echo "$o" | grep -q "Permission denied (password" || { echo "$o"; return 0; }; sleep 3; done; echo "$o"; return 1; }
j8(){ local o; for t in 1 2 3 4 5 6; do o=$(jms ssh JSZX-AI-03 "$1" 2>&1); echo "$o" | grep -q "Permission denied (password" || { echo "$o"; return 0; }; sleep 3; done; echo "$o"; return 1; }

# ── Staging (Phase A/B/C) runs unless POOL_ONLY=1 (acct already onboarded). ──
# Body is intentionally NOT re-indented so the default 198 path stays byte-for-byte
# identical to the pre-gate version (see memory: 198 缺省=老路径逐字节不变).
if [ "$POOL_ONLY" != 1 ]; then

# ── Phase A: ensure deploy + issue device code ─────────────────────────────
log "[A] ensure deploy chatgpt-$ACCT exists"
if ! jr "kubectl -n $NS get deploy chatgpt-$ACCT >/dev/null 2>&1 && echo yes" | grep -q yes; then
  TMPL=$(ls "$REPO"/k8s/chatgpt-acct-8?.yaml "$REPO"/k8s/chatgpt-acct-79.yaml 2>/dev/null | head -1)
  [ -n "$TMPL" ] || { echo "FATAL: no manifest template in $REPO/k8s"; exit 1; }
  TB=$(basename "$TMPL" .yaml | sed 's/chatgpt-acct-//')
  sed "s/acct-$TB/acct-$N/g; s/account: \"$TB\"/account: \"$N\"/g" "$TMPL" \
    | sed "s#image: .*litellm-carher:.*#image: $ACCT_IMAGE#" \
    > "$REPO/k8s/chatgpt-acct-$N.yaml"
  grep -qE 'port: 4000' "$REPO/k8s/chatgpt-acct-$N.yaml" || { echo "FATAL: port sed corrupted"; exit 1; }
  grep -qF "image: $ACCT_IMAGE" "$REPO/k8s/chatgpt-acct-$N.yaml" || { echo "FATAL: image sed failed (expected $ACCT_IMAGE)"; exit 1; }
  cat "$REPO/k8s/chatgpt-acct-$N.yaml" | jms ssh AIYJY-litellm "kubectl apply -f -" | grep -vE "Permission denied"
fi

# ── Phase A2: issue device code — SKIPPED when STAGE_ONLY=1 (auth already on 188) ──
if [ "$STAGE_ONLY" != 1 ]; then
log "[A] sync device-manual to 188 + issue code"
cat "$REPO/scripts/chatgpt-onboard/chatgpt-device-manual.py" | jms ssh JSZX-AI-03 "cat > /tmp/chatgpt-device-manual.py"
LAUNCH="pkill -f chatgpt-device-manual.py 2>/dev/null; sleep 1; rm -f /tmp/oauth-$ACCT.log /tmp/auth-$ACCT.json; export AUTH_JSON_OUTPUT=/tmp/auth-$ACCT.json POLL_MINUTES=25; setsid python3 /tmp/chatgpt-device-manual.py > /tmp/oauth-$ACCT.log 2>&1 </dev/null & disown"
LB64=$(printf '%s' "$LAUNCH" | base64 | tr -d '\n')
jms ssh JSZX-AI-03 "echo $LB64 | base64 -d | bash" >/dev/null 2>&1
CODE=""
for i in $(seq 1 10); do
  sleep 4
  CODE=$(j8 "grep -oE '[A-Z0-9]{4}-[A-Z0-9]{5}' /tmp/oauth-$ACCT.log 2>/dev/null | head -1" | tr -d '[:space:]')
  [ -n "$CODE" ] && break
done
[ -n "$CODE" ] || { echo "FATAL: no device code (188 usercode CF? check /tmp/oauth-$ACCT.log)"; j8 "tail -5 /tmp/oauth-$ACCT.log"; exit 2; }

cat <<BANNER

  ╔══════════════════════════════════════════════════════════════╗
  ║  在浏览器只打开 https://auth.openai.com/codex/device            ║
  ║  CODE: $CODE
  ║  登录: $EMAIL  /  邮箱密码: $MAIL_PW
  ║  Authorize 后【直接关浏览器,绝不要打开 chatgpt.com】(否则 token 作废)║
  ╚══════════════════════════════════════════════════════════════╝

BANNER

fi   # end Phase A2 (device code); STAGE_ONLY reuses the auth.json already on 188

# ── Phase C: wait auth.json → verify → cp → register → smoke ───────────────
log "[C] polling 188 for auth.json (25min)..."
HAVE=0
for i in $(seq 1 150); do
  if j8 "test -s /tmp/auth-$ACCT.json && echo yes" | grep -q yes; then HAVE=1; break; fi
  sleep 10
done
[ "$HAVE" = 1 ] || { echo "FATAL: no auth.json in 25min"; exit 3; }

log "[C] verify token live (/v1/me)"
ME=$(j8 "TOK=\$(python3 -c 'import json;print(json.load(open(\"/tmp/auth-$ACCT.json\"))[\"access_token\"])'); curl -sS -m 20 -o /dev/null -w '%{http_code}' -H \"Authorization: Bearer \$TOK\" https://api.openai.com/v1/me" | tr -dc 0-9 | tail -c3)
[ "$ME" = 200 ] || { echo "FATAL: token /v1/me=$ME (invalidated? re-authorize, do NOT touch chatgpt.com)"; exit 4; }
log "  ✅ token live"

log "[C] stage token into PVC (node mode: $NODE_MODE)"
jms ssh JSZX-AI-03 "cat /tmp/auth-$ACCT.json" > "/tmp/auth-$ACCT.json"
python3 -c "import json;d=json.load(open('/tmp/auth-$ACCT.json'));assert d.get('access_token') and d.get('account_id')" || { echo "FATAL: local auth.json invalid"; exit 5; }

if [ "$NODE_MODE" = standby ]; then
  # ── standby: seed auth.json onto the standby node via a writer pod (no node SSH) ──
  # New acct has no auth.json on the node yet; local PV requires the path to pre-exist,
  # so a busybox writer pod with hostPath DirectoryOrCreate creates the dir, and the
  # token is piped in over kubectl exec stdin (small file, does not hang like kubectl cp).
  PVDIR=/Data/local-pv/chatgpt-$ACCT-auth
  PVNAME=$ACCT-auth-225
  PVC225=chatgpt-$ACCT-auth-225
  log "[C] standby: writer pod on $STANDBY_NODE seeds $PVDIR/auth.json"
  WOVR="{\"spec\":{\"nodeSelector\":{\"kubernetes.io/hostname\":\"$STANDBY_NODE\"},\"tolerations\":[{\"key\":\"dedicated\",\"operator\":\"Equal\",\"value\":\"standby\",\"effect\":\"NoSchedule\"}],\"restartPolicy\":\"Never\",\"volumes\":[{\"name\":\"a\",\"hostPath\":{\"path\":\"$PVDIR\",\"type\":\"DirectoryOrCreate\"}}],\"containers\":[{\"name\":\"x\",\"image\":\"busybox\",\"command\":[\"sh\",\"-c\",\"sleep 240\"],\"volumeMounts\":[{\"name\":\"a\",\"mountPath\":\"/a\"}]}]}}"
  jr "kubectl -n $NS delete pod w$N --force --grace-period=0 >/dev/null 2>&1; kubectl -n $NS run w$N --restart=Never --image=busybox --overrides='$WOVR' >/dev/null 2>&1; kubectl -n $NS wait --for=condition=Ready pod/w$N --timeout=40s" | tail -1
  cat "/tmp/auth-$ACCT.json" | jms ssh AIYJY-litellm "kubectl -n $NS exec -i w$N -- sh -c 'cat > /a/auth.json'" 2>&1 | grep -v "Permission denied" || true
  VERIFY=$(jr "kubectl -n $NS exec w$N -- sh -c 'wc -c < /a/auth.json; grep -c account_id /a/auth.json'")
  log "  writer verify (bytes / account_id count): $(echo "$VERIFY" | tr '\n' ' ')"
  jr "kubectl -n $NS delete pod w$N --force --grace-period=0 >/dev/null 2>&1"

  log "[C] standby: apply static PV + PVC ($PVC225 -> $PVDIR @ $STANDBY_NODE)"
  cat <<YAML | jms ssh AIYJY-litellm "kubectl apply -f -" 2>&1 | grep -v "Permission denied"
apiVersion: v1
kind: PersistentVolume
metadata:
  name: $PVNAME
spec:
  capacity: {storage: 1Gi}
  accessModes: [ReadWriteOnce]
  persistentVolumeReclaimPolicy: Retain
  storageClassName: manual
  volumeMode: Filesystem
  local: {path: $PVDIR}
  claimRef: {namespace: $NS, name: $PVC225}
  nodeAffinity:
    required:
      nodeSelectorTerms:
      - matchExpressions:
        - {key: kubernetes.io/hostname, operator: In, values: [$STANDBY_NODE]}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: $PVC225
  namespace: $NS
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: manual
  volumeName: $PVNAME
  resources: {requests: {storage: 1Gi}}
YAML

  log "[C] standby: patch deploy (nodeSelector + toleration + claimName=$PVC225)"
  SPATCH="{\"spec\":{\"template\":{\"spec\":{\"nodeSelector\":{\"kubernetes.io/hostname\":\"$STANDBY_NODE\"},\"tolerations\":[{\"key\":\"dedicated\",\"operator\":\"Equal\",\"value\":\"standby\",\"effect\":\"NoSchedule\"}],\"volumes\":[{\"name\":\"auth\",\"persistentVolumeClaim\":{\"claimName\":\"$PVC225\"}}]}}}}"
  jr "kubectl -n $NS patch deploy chatgpt-$ACCT --type=strategic -p '$SPATCH'" | tail -1
  jr "kubectl -n $NS rollout status deploy/chatgpt-$ACCT --timeout=150s" | tail -1
  jr "kubectl -n $NS get pod -l app=chatgpt-$ACCT -o custom-columns=N:.metadata.name,READY:.status.containerStatuses[0].ready,NODE:.spec.nodeName --no-headers" | tail -2
  # drop the stale local-path PVC the manifest created on 198 (auth now on static PV)
  jr "kubectl -n $NS delete pvc chatgpt-$ACCT-auth --wait=false >/dev/null 2>&1" || true
else
  # ── default 198: hostPath busybox cp into the existing local-path PVC ──
  cat "/tmp/auth-$ACCT.json" | jms ssh AIYJY-litellm "cat > /tmp/${ACCT}stage.json"
  jr "kubectl -n $NS scale deploy chatgpt-$ACCT --replicas=0; for i in \$(seq 1 20); do [ \"\$(kubectl -n $NS get pod -l app=chatgpt-$ACCT --no-headers 2>/dev/null|wc -l)\" = 0 ] && break; sleep 3; done"
  NODE=$(jr "PV=\$(kubectl -n $NS get pvc chatgpt-$ACCT-auth -o jsonpath='{.spec.volumeName}'); kubectl get pv \$PV -o jsonpath='{.spec.nodeAffinity.required.nodeSelectorTerms[0].matchExpressions[0].values[0]}'" | tail -1)
  OVR="{\"spec\":{\"nodeName\":\"$NODE\",\"restartPolicy\":\"Never\",\"volumes\":[{\"name\":\"a\",\"persistentVolumeClaim\":{\"claimName\":\"chatgpt-$ACCT-auth\"}},{\"name\":\"h\",\"hostPath\":{\"path\":\"/tmp/${ACCT}stage.json\",\"type\":\"File\"}}],\"containers\":[{\"name\":\"x\",\"image\":\"busybox\",\"command\":[\"sh\",\"-c\",\"cp /h/src /a/auth.json; echo RESULT=\$(wc -c < /a/auth.json)\"],\"volumeMounts\":[{\"name\":\"a\",\"mountPath\":\"/a\"},{\"name\":\"h\",\"mountPath\":\"/h/src\"}]}]}}"
  jr "kubectl -n $NS run cp$N-host --restart=Never --image=busybox --overrides='$OVR' >/dev/null 2>&1; kubectl -n $NS wait --for=condition=Ready pod/cp$N-host --timeout=30s >/dev/null 2>&1; sleep 3; kubectl -n $NS logs cp$N-host 2>/dev/null | grep RESULT; kubectl -n $NS delete pod cp$N-host --force --grace-period=0 >/dev/null 2>&1"
  jr "kubectl -n $NS scale deploy chatgpt-$ACCT --replicas=1; kubectl -n $NS rollout status deploy/chatgpt-$ACCT --timeout=150s"
fi

fi   # end staging (POOL_ONLY != 1)

if [ "$JOIN_POOL" != 1 ]; then
  cat <<DONE
[$ACCT] ✅ onboarded as COLD STANDBY on node '$NODE_MODE' — pod Running, NOT in the LiteLLM rotation pool.
       Routing does not see it yet (no POOL_ACCOUNTS entry, no resume_acct, proxy not rolled).
       To join the rotation pool later WITHOUT re-auth, re-run with POOL_ONLY=1:
           POOL_ONLY=1 $0 $N '$EMAIL' '<mail_pw>' $NODE_MODE
DONE
  exit 0
fi

log "[C] pool-state (surgical sed) + resume_acct (6 models)"
j8 "grep -q '\"$ACCT\":' /home/cltx/quota-rebalance.py || { cp /home/cltx/quota-rebalance.py /home/cltx/quota-rebalance.py.bak-$N-\$(date +%s); sed -i '/\"acct-79\": {\"port\": 4079/a\\    \"$ACCT\": {\"port\": $PORT, \"location\": \"198\"},' /home/cltx/quota-rebalance.py; }"
j8 "python3 -c 'import json,pathlib,time;p=pathlib.Path(\"/home/cltx/.chatgpt-quota/state/state.json\");d=json.loads(p.read_text());a=d.setdefault(\"$ACCT\",{});a.update({\"tier\":\"HEALTHY\",\"paused\":False,\"manual_offline\":False,\"consecutive_401\":0,\"consecutive_probe_err\":0,\"probe_err_alerted\":False,\"restore_at\":0,\"cause\":None,\"ts\":int(time.time())});p.write_text(json.dumps(d,indent=2,ensure_ascii=False))'"
j8 "set -a; source /home/cltx/.chatgpt-quota/env; set +a; python3 -c \"import importlib.util,sys;spec=importlib.util.spec_from_file_location('qr','/home/cltx/quota-rebalance.py');qr=importlib.util.module_from_spec(spec);sys.modules['qr']=qr;spec.loader.exec_module(qr);print('resume:',qr.resume_acct('$ACCT',qr.POOL_ACCOUNTS['$ACCT']))\"" | grep -E "resume|resumed"

log "[C] rollout litellm-proxy + smoke 6 models"
jr "kubectl -n $NS rollout restart deploy/litellm-proxy; for i in \$(seq 1 100); do R=\$(kubectl -n $NS get deploy litellm-proxy -o jsonpath='{.status.readyReplicas}/{.spec.replicas}' 2>/dev/null); [ \"\$R\" = 4/4 ] && break; sleep 5; done" >/dev/null
for M in gpt-5.5 gpt-5.4 gpt-5.3-codex gpt-5.6-sol gpt-5.6-terra gpt-5.6-luna; do
  R=$(jr "curl -sS -m 40 -o /dev/null -w '%{http_code}' $ENDPOINT/v1/chat/completions -H 'Authorization: Bearer $MK' -H 'Content-Type: application/json' -d '{\"model\":\"chatgpt-$ACCT-$M\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":5}'" | grep -oE '^[0-9]{3}$')
  echo "  chatgpt-$ACCT-$M -> HTTP $R"
done
log "✅ $ACCT done"
