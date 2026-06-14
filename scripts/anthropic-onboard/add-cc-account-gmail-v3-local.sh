#!/usr/bin/env bash
# add-cc-account-gmail-v3-local.sh — gmail-v3 但 patchright 跑在本地 Mac
#
# WHY: 188 IP 被 Gmail 风控 ("Couldn't sign you in"), 本机 Mac 不同 IP 可能不被 block
#
# 流程:
#   1. ssh 188 → tmux 跑 claude setup-token 拿 OAuth URL
#   2. **本地 docker** 跑 patchright 完成 Gmail 登录 + Claude verify-code, 抓 callback
#   3. ssh 188 → tmux send-keys 把 callback 粘回 setup-token, 拿 sk-ant-oat token
#   4. 写 /Data/anthropic-auth/acct-N/.env
#   5. Haiku 探针验证

set -eo pipefail

ACCT="${1:?用法: $0 acct-N}"
if [[ ! "$ACCT" =~ ^acct-[0-9]+$ ]]; then
    echo "❌ ACCT 格式必须是 acct-N (如 acct-1),实际: $ACCT"; exit 1
fi
N="${ACCT#acct-}"
SSH_188="cltx@10.68.13.188"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_SCRIPT="$SCRIPT_DIR/cc-oauth-gmail-v3.py"
[[ -f "$PY_SCRIPT" ]] || { echo "❌ 找不到 $PY_SCRIPT"; exit 1; }

CREDS_REMOTE="/Data/anthropic-auth/$ACCT/.creds"
ENV_REMOTE="/Data/anthropic-auth/$ACCT/.env"
LOG_REMOTE="/tmp/cc-oauth-$ACCT.log"
SS_LOCAL="/tmp/cc-screenshots-$ACCT-local"

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  add-cc-account-gmail-v3-local.sh  $ACCT  (local Mac IP)"
echo "╚══════════════════════════════════════════════════════════════╝"

# ── 1/5: 检查 .creds + 拉本地 ─────────────────────────────────────
echo "==[1/5]== 读 $CREDS_REMOTE (从 188)"
ssh "$SSH_188" "test -f $CREDS_REMOTE" || { echo "❌ $CREDS_REMOTE 不存在"; exit 1; }
CC_EMAIL=$(ssh "$SSH_188" "grep ^email= $CREDS_REMOTE | cut -d= -f2-")
GMAIL_PW=$(ssh "$SSH_188" "grep ^gmail_pw= $CREDS_REMOTE | cut -d= -f2-")
GMAIL_TOTP=$(ssh "$SSH_188" "grep ^gmail_totp= $CREDS_REMOTE | cut -d= -f2-")
echo "  email=$CC_EMAIL"

# ── 2/5: tmux 启 setup-token,拿 OAuth URL ──────────────────────────
echo "==[2/5]== 启 tmux 跑 claude setup-token,拿 OAuth URL"
OAUTH_URL=$(ssh "$SSH_188" "
    tmux kill-session -t cc-oauth-$ACCT 2>/dev/null
    rm -f $LOG_REMOTE
    export PATH=\$HOME/.local/bin:\$PATH
    tmux new-session -d -s cc-oauth-$ACCT \"claude setup-token 2>&1 | tee $LOG_REMOTE\"
    for i in {1..30}; do
        URL=\$(python3 -c 'import re,sys; t=open(sys.argv[1], errors=\"ignore\").read().replace(\"\r\", \"\"); f=\"\".join(t.splitlines()); m=re.search(r\"https://claude\\.com/cai/oauth/authorize\\?[^ \t]*?state=[A-Za-z0-9_-]+\", f); print(m.group(0) if m else \"\")' $LOG_REMOTE)
        [ -n \"\$URL\" ] && { printf \"%s\n\" \"\$URL\"; break; }
        sleep 1
    done
")
[[ -n "$OAUTH_URL" ]] || { echo "❌ 没拿到 OAuth URL"; exit 2; }
echo "  ✅ URL: ${OAUTH_URL:0:80}..."

# ── 3/5: 本地 Docker 跑 OAuth ─────────────────────────────────────
echo "==[3/5]== 本地 Docker patchright 跑 OAuth (我家 IP)"
rm -rf "$SS_LOCAL" && mkdir -p "$SS_LOCAL"

set +e
RAW_OUT=$(docker run --rm \
  -v "$PY_SCRIPT":/work/script.py:ro \
  -v "$SS_LOCAL":/work/screenshots \
  -e CC_EMAIL="$CC_EMAIL" \
  -e GMAIL_PW="$GMAIL_PW" \
  -e GMAIL_TOTP="$GMAIL_TOTP" \
  -e CC_OAUTH_URL="$OAUTH_URL" \
  -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright -e DISPLAY=:99 \
  mcr.microsoft.com/playwright/python:v1.59.0-noble \
  bash -c 'Xvfb :99 -screen 0 1280x800x24 >/dev/null 2>&1 & sleep 1 && \
           pip install patchright==1.59.1 pyotp -q --root-user-action=ignore 2>&1 | tail -1 && \
           python3 /work/script.py' 2>&1)
DOCKER_RC=$?
set -e
echo "  docker rc=$DOCKER_RC"
CALLBACK=$(echo "$RAW_OUT" | grep '^✅ CALLBACK_CODE=' | tail -1 | sed 's/.*CALLBACK_CODE=//' || true)

if [[ -z "$CALLBACK" ]]; then
    FAIL=$(echo "$RAW_OUT" | grep -oE 'GMAIL_BLOCKED_[A-Z_]+|NO_VERIFY_CODE|NO_SECURE_LINK|CLAUDE_PAGE_STALE|CLAUDE_NO_VERIFY_PAGE' | head -1 || true)
    echo "❌ OAuth 失败: ${FAIL:-UNKNOWN}"
    echo "  截图: ls $SS_LOCAL"
    echo "  最后 30 行 output:"
    echo "$RAW_OUT" | tail -30 | sed 's/^/    /'
    exit 2
fi
echo "  ✅ callback code: ${CALLBACK:0:30}..."

# ── 4/5: 粘 code 回 tmux,拿 sk-ant-oat token ───────────────────────
echo "==[4/5]== 粘 callback code 回 setup-token (188 tmux)"
STATE=$(echo "$OAUTH_URL" | grep -oE 'state=[^&]+' | head -1 | cut -d= -f2)
FULL_INPUT="${CALLBACK}#${STATE}"

ssh "$SSH_188" "
    tmux send-keys -t cc-oauth-$ACCT -l '$FULL_INPUT'
    sleep 1
    tmux send-keys -t cc-oauth-$ACCT Enter
    sleep 10
"
TOKEN=$(ssh "$SSH_188" "grep -oE 'sk-ant-oat[a-zA-Z0-9_-]+' $LOG_REMOTE | tail -1")
[[ -n "$TOKEN" ]] || {
    echo "❌ 没拿到 token,setup-token log:"
    ssh "$SSH_188" "tail -40 $LOG_REMOTE"
    exit 2
}
echo "  ✅ token: ${TOKEN:0:30}... (len=${#TOKEN})"

# ── 5/5: 写 .env + Haiku 探针 ─────────────────────────────────────
echo "==[5/5]== 写 $ENV_REMOTE + Haiku 4.5 探针"
ssh "$SSH_188" "
    echo 'ANTHROPIC_OAUTH_TOKEN=$TOKEN' > $ENV_REMOTE
    chmod 600 $ENV_REMOTE
    ls -la $ENV_REMOTE
"
RESP=$(ssh "$SSH_188" "curl -s https://api.anthropic.com/v1/messages \
  -H 'Authorization: Bearer $TOKEN' \
  -H 'anthropic-beta: oauth-2025-04-20' \
  -H 'anthropic-dangerous-direct-browser-access: true' \
  -H 'anthropic-version: 2023-06-01' \
  -H 'content-type: application/json' \
  -d '{\"model\":\"claude-haiku-4-5\",\"max_tokens\":20,\"messages\":[{\"role\":\"user\",\"content\":\"reply OK\"}]}'")
if echo "$RESP" | grep -q '"type":"message"'; then
    echo "  ✅ Haiku 探针 200 OK"
    echo ""
    echo "🎉 $ACCT 上线完成"
    echo "   token: /Data/anthropic-auth/$ACCT/.env"
    echo "   下一步: cd /Data/claude-max-proxy && docker compose restart"
    exit 0
else
    echo "  ❌ 探针失败: $RESP"
    exit 3
fi
