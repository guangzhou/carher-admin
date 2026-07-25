#!/bin/bash
# add-chatgpt-acct-198-devcode.sh — onboard 1 acct to 198 pool via MANUAL device-code
# (2026-07-20: fallback when CF Turnstile blocks patchright full-auto login).
#
# Usage: ./add-chatgpt-acct-198-devcode.sh <N> <email> <mail_pw>
#   Phase A: applies manifest (if missing) + issues a device code on 188 (direct,
#            plain-urllib + Originator header — passes CF where the browser can't).
#   Phase B: YOU open ONLY https://auth.openai.com/codex/device, log in with the
#            email + mail_pw, enter the code, Authorize, then CLOSE the browser.
#            *** NEVER open chatgpt.com afterwards — web login revokes the token
#                (token_invalidated). See memory feedback_chatgpt_oauth_no_web_login_verify.
#   Phase C: script polls 188 for auth.json → verifies /v1/me 200 → writes it into
#            the PVC via a hostPath busybox pod (NOT kubectl-cp/stdin, which hangs
#            over the jms tunnel) → resume_acct registers all 6 models → rollout → smoke.
#
# Env (defaults are the 198 prod values):
#   LITELLM_POOL_KEY_198  sub-proxy key   (default sk-chatgpt-198-...)
#   LITELLM_MK_198        prod master key (default sk-pro-litellm-...)
set -uo pipefail
N="${1:?usage: $0 <N> <email> <mail_pw>}"; EMAIL="${2:?email}"; MAIL_PW="${3:?mail_pw}"
NS=litellm-product
POOL_KEY="${LITELLM_POOL_KEY_198:-sk-chatgpt-198-d8a3f4e62b9c1057ef324918a7b6d3e0}"
MK="${LITELLM_MK_198:-sk-pro-litellm-ce077e2b0721bb419a633e4d}"
ENDPOINT=https://cc.auto-link.com.cn/pro
REPO="$(cd "$(dirname "$0")/.." && pwd)"
ACCT="acct-$N"; PORT=$((4000 + N))
log(){ echo "[$ACCT $(date +%H:%M:%S)] $*"; }
jr(){ local o; for t in 1 2 3 4 5 6; do o=$(jms ssh AIYJY-litellm "$1" 2>&1); echo "$o" | grep -q "Permission denied (password" || { echo "$o"; return 0; }; sleep 3; done; echo "$o"; return 1; }
j8(){ local o; for t in 1 2 3 4 5 6; do o=$(jms ssh JSZX-AI-03 "$1" 2>&1); echo "$o" | grep -q "Permission denied (password" || { echo "$o"; return 0; }; sleep 3; done; echo "$o"; return 1; }

# ── Phase A: ensure deploy + issue device code ─────────────────────────────
log "[A] ensure deploy chatgpt-$ACCT exists"
if ! jr "kubectl -n $NS get deploy chatgpt-$ACCT >/dev/null 2>&1 && echo yes" | grep -q yes; then
  TMPL=$(ls "$REPO"/k8s/chatgpt-acct-8?.yaml "$REPO"/k8s/chatgpt-acct-79.yaml 2>/dev/null | head -1)
  [ -n "$TMPL" ] || { echo "FATAL: no manifest template in $REPO/k8s"; exit 1; }
  TB=$(basename "$TMPL" .yaml | sed 's/chatgpt-acct-//')
  sed "s/acct-$TB/acct-$N/g; s/account: \"$TB\"/account: \"$N\"/g" "$TMPL" > "$REPO/k8s/chatgpt-acct-$N.yaml"
  grep -qE 'port: 4000' "$REPO/k8s/chatgpt-acct-$N.yaml" || { echo "FATAL: port sed corrupted"; exit 1; }
  cat "$REPO/k8s/chatgpt-acct-$N.yaml" | jms ssh AIYJY-litellm "kubectl apply -f -" | grep -vE "Permission denied"
fi

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

log "[C] stage token on 198 host + hostPath busybox cp into PVC (scale0→cp→scale1)"
jms ssh JSZX-AI-03 "cat /tmp/auth-$ACCT.json" > "/tmp/auth-$ACCT.json"
cat "/tmp/auth-$ACCT.json" | jms ssh AIYJY-litellm "cat > /tmp/${ACCT}stage.json"
jr "kubectl -n $NS scale deploy chatgpt-$ACCT --replicas=0; for i in \$(seq 1 20); do [ \"\$(kubectl -n $NS get pod -l app=chatgpt-$ACCT --no-headers 2>/dev/null|wc -l)\" = 0 ] && break; sleep 3; done"
NODE=$(jr "PV=\$(kubectl -n $NS get pvc chatgpt-$ACCT-auth -o jsonpath='{.spec.volumeName}'); kubectl get pv \$PV -o jsonpath='{.spec.nodeAffinity.required.nodeSelectorTerms[0].matchExpressions[0].values[0]}'" | tail -1)
OVR="{\"spec\":{\"nodeName\":\"$NODE\",\"restartPolicy\":\"Never\",\"volumes\":[{\"name\":\"a\",\"persistentVolumeClaim\":{\"claimName\":\"chatgpt-$ACCT-auth\"}},{\"name\":\"h\",\"hostPath\":{\"path\":\"/tmp/${ACCT}stage.json\",\"type\":\"File\"}}],\"containers\":[{\"name\":\"x\",\"image\":\"busybox\",\"command\":[\"sh\",\"-c\",\"cp /h/src /a/auth.json; echo RESULT=\$(wc -c < /a/auth.json)\"],\"volumeMounts\":[{\"name\":\"a\",\"mountPath\":\"/a\"},{\"name\":\"h\",\"mountPath\":\"/h/src\"}]}]}}"
jr "kubectl -n $NS run cp$N-host --restart=Never --image=busybox --overrides='$OVR' >/dev/null 2>&1; kubectl -n $NS wait --for=condition=Ready pod/cp$N-host --timeout=30s >/dev/null 2>&1; sleep 3; kubectl -n $NS logs cp$N-host 2>/dev/null | grep RESULT; kubectl -n $NS delete pod cp$N-host --force --grace-period=0 >/dev/null 2>&1"
jr "kubectl -n $NS scale deploy chatgpt-$ACCT --replicas=1; kubectl -n $NS rollout status deploy/chatgpt-$ACCT --timeout=150s"

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
