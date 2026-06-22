#!/bin/bash
# add-chatgpt-acct-198-hotmail.sh — hotmail/outlook 专用变体 (no ChatGPT password)
#
# 跟 add-chatgpt-acct-198-full.sh 不同点:
#   - hotmail acct 没 ChatGPT 密码 (magic-link/OTP-only login)
#   - 跳过 [4] toggle phase (toggle 需登 chatgpt.com 改 Security setting, 需密码)
#     hotmail Plus/Pro 卖号通常 toggle 已预开; 失败再回头处理
#   - OAuth phase 设 MAIL_OTP_PROVIDER=outlook 走 outlook.live.com 取 OTP
#   - .creds chatgpt_pw 写占位 'N/A' (re-oauth.sh 校验非空, 但 outlook 路径不用)
#
# 用法:
#   ./add-chatgpt-acct-198-hotmail.sh <N> <hotmail_email> <hotmail_pw>
#
# 例:
#   ./add-chatgpt-acct-198-hotmail.sh 54 tonerpearlene10@hotmail.com '8FD9za4d6WFz'

set -euo pipefail

if [ $# -lt 3 ]; then
  cat >&2 <<USAGE
usage: $0 <N> <hotmail_email> <hotmail_pw>

example:
  $0 54 tonerpearlene10@hotmail.com '8FD9za4d6WFz'
USAGE
  exit 1
fi

N="$1"; EMAIL="$2"; MAIL_PW="$3"
ACCT="acct-$N"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
K8S_DIR="$REPO_DIR/k8s"
SCRIPTS_DIR="$REPO_DIR/scripts"
ONBOARD_DIR="$SCRIPTS_DIR/chatgpt-onboard"
NS=litellm-product

choose_template() {
  for ((i=N-1; i>=26; i--)); do
    if [ -f "$K8S_DIR/chatgpt-acct-$i.yaml" ]; then
      echo "$K8S_DIR/chatgpt-acct-$i.yaml"; return
    fi
  done
  [ -f "$K8S_DIR/chatgpt-acct-26-33.yaml" ] && { echo "$K8S_DIR/chatgpt-acct-26-33.yaml"; return; }
  echo "" >&2; return 1
}

log() { printf "[%s %s] %s\n" "$ACCT" "$(date +%H:%M:%S)" "$*"; }

# jms ssh wrapper: retry on TLS handshake timeout (常见 200-300s 一次)
# Usage: jms_retry <max_tries> <command...>
jms_retry() {
  local max=$1; shift
  local i=1
  while [ $i -le $max ]; do
    if "$@"; then return 0; fi
    log "  ⚠️  cmd try $i/$max failed: $*"
    sleep $((i*5))
    i=$((i+1))
  done
  return 1
}

[[ "$N" =~ ^[0-9]+$ ]] || { echo "FATAL: N 必须是数字" >&2; exit 1; }
[ "$N" -ge 26 ] || { echo "FATAL: N 必须 ≥ 26" >&2; exit 1; }

# 188 disk preflight
DISK_USE=$(jms ssh JSZX-AI-03 'df --output=pcent / | tail -1 | tr -dc 0-9' 2>/dev/null || echo "0")
if [ "${DISK_USE:-0}" -ge 95 ]; then
  echo "FATAL: 188 / ${DISK_USE}% ≥ 95%; clear /tmp + journal first" >&2; exit 1
fi
log "188 / ${DISK_USE}% (ok)"

if jms_retry 5 jms ssh AIYJY-litellm "kubectl -n $NS get deploy chatgpt-acct-$N >/dev/null 2>&1"; then
  log "⚠️  chatgpt-acct-$N 已存在 — skip manifest/apply"
  SKIP_APPLY=1
else
  SKIP_APPLY=0
fi

# ── [1] sed manifest (anchored) ─────────────────────────────────────────
if [ "$SKIP_APPLY" = 0 ]; then
  TPL=$(choose_template) || { echo "FATAL: no template" >&2; exit 2; }
  TPL_N=$(basename "$TPL" .yaml | sed 's/^chatgpt-acct-//')
  OUT="$K8S_DIR/chatgpt-acct-$N.yaml"
  log "[1] sed manifest: $TPL_N → $N"
  cp "$TPL" "$OUT"
  sed -i '' "s/acct-$TPL_N/acct-$N/g; s/account: \"$TPL_N\"/account: \"$N\"/g" "$OUT"
  grep -q "containerPort: 4000" "$OUT" || { echo "FATAL: containerPort != 4000" >&2; exit 3; }
  log "  ✅ manifest port 4000 ok"
fi

# ── [2] 写 .creds (chatgpt_pw 占位 'N/A') ────────────────────────────────
log "[2] 写 .creds (hotmail, chatgpt_pw=N/A)"
jms ssh JSZX-AI-03 "mkdir -p /Data/chatgpt-auth/$ACCT && cat > /Data/chatgpt-auth/$ACCT/.creds" <<CRED
email=$EMAIL
mail_pw='$MAIL_PW'
chatgpt_pw='N/A'
CRED
jms ssh JSZX-AI-03 "chmod 600 /Data/chatgpt-auth/$ACCT/.creds && wc -l /Data/chatgpt-auth/$ACCT/.creds"

# ── [3] kubectl apply (retry on JMS TLS handshake timeout) ──────────────
if [ "$SKIP_APPLY" = 0 ]; then
  log "[3] kubectl apply"
  APPLY_OK=0
  for try in 1 2 3 4 5; do
    cat "$K8S_DIR/chatgpt-acct-$N.yaml" | jms ssh AIYJY-litellm "kubectl apply -f -" > /tmp/apply-$ACCT.log 2>&1 || true
    # verify by reading actual deploy state (idempotent — unchanged also ok)
    if jms ssh AIYJY-litellm "kubectl -n $NS get deploy chatgpt-acct-$N -o jsonpath='{.metadata.name}'" 2>/dev/null | grep -q "chatgpt-acct-$N"; then
      APPLY_OK=1; break
    fi
    log "  ⚠️  apply try $try/5 (deploy not visible yet), retry in $((try*5))s"
    sleep $((try*5))
  done
  [ "$APPLY_OK" = 1 ] || { echo "FATAL: kubectl apply 5x retries all failed" >&2; cat /tmp/apply-$ACCT.log >&2; exit 4; }
  SVC_PORT=$(jms_retry 3 bash -c "jms ssh AIYJY-litellm 'kubectl -n $NS get svc chatgpt-acct-$N -o jsonpath=\"{.spec.ports[0].port}\"'" 2>/dev/null)
  [ "$SVC_PORT" = "4000" ] || { echo "FATAL: svc port=$SVC_PORT" >&2; exit 4; }
  log "  ✅ svc port=4000"
fi

# ── [4] 跳过 toggle (hotmail 无 chatgpt 密码) ────────────────────────────
log "[4] skip toggle (hotmail: no chatgpt password; Plus/Pro 卖号 toggle 通常预开)"

# ── [5] OAuth (MAIL_OTP_PROVIDER=outlook) ───────────────────────────────
log "[5] OAuth (MAIL_OTP_PROVIDER=outlook, patchright)"
OAUTH_LOG="/tmp/oauth-$ACCT.log"
jms ssh JSZX-AI-03 "rm -f $OAUTH_LOG /tmp/auth-$ACCT.json && setsid bash -c 'GEN_ONLY=1 MAIL_OTP_PROVIDER=outlook bash /Data/chatgpt-auth/re-oauth.sh $ACCT 2>&1 | stdbuf -oL tee $OAUTH_LOG' </dev/null >/dev/null 2>&1 & disown; sleep 2; echo 'oauth bg started'"

log "  等 OAuth (最多 10min)..."
DEADLINE=$(($(date +%s) + 600))
HAVE_AUTH=0
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  if jms ssh JSZX-AI-03 "test -s /tmp/auth-$ACCT.json && grep -q '🎉.*GEN_ONLY done' $OAUTH_LOG 2>/dev/null"; then
    HAVE_AUTH=1; break
  fi
  if jms ssh JSZX-AI-03 "grep -q '❌\|account is on hold\|consent Continue disabled' $OAUTH_LOG 2>/dev/null"; then
    log "  ❌ OAuth early fail"; jms ssh JSZX-AI-03 "tail -60 $OAUTH_LOG" >&2; exit 7
  fi
  sleep 15
done
[ "$HAVE_AUTH" = 1 ] || { echo "FATAL: OAuth 10min timeout" >&2; jms ssh JSZX-AI-03 "tail -60 $OAUTH_LOG" >&2; exit 7; }
log "  ✅ auth.json produced"

# ── [6] 拉回 + onboard 8 步 ─────────────────────────────────────────────
LOCAL_AUTH="/tmp/auth-$ACCT.json"
jms ssh JSZX-AI-03 "cat /tmp/auth-$ACCT.json" > "$LOCAL_AUTH"
[ -s "$LOCAL_AUTH" ] || { echo "FATAL: auth.json empty" >&2; exit 8; }
log "  ✅ auth.json $(wc -c < "$LOCAL_AUTH") bytes"

log "[6] onboard 8 steps"
"$SCRIPTS_DIR/onboard-chatgpt-acct.sh" "$N" "$LOCAL_AUTH"

log "🎉 $ACCT done"
