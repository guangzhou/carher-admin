#!/usr/bin/env bash
# add-cc-account-gmail-v3.sh — Gmail 版,适配 claude.ai 新 verify-code 流程
#
# 同 add-cc-account.sh 接口,只替换 patchright 脚本为 cc-oauth-gmail-v3.py
#
# 用法:
#   ./scripts/anthropic-onboard/add-cc-account-gmail-v3.sh acct-N
#
# 前提:已在 188 上建好 /Data/anthropic-auth/acct-N/.creds:
#   email=xxx@gmail.com
#   gmail_pw=<gmail 密码>
#   gmail_totp=<Google 2FA TOTP secret>
#
# 退出码:
#   0 = OK
#   1 = 配置错误
#   2 = OAuth 失败 (看 /tmp/cc-screenshots-acct-N/)
#       特殊子状态: GMAIL_BLOCKED_VERIFY_ITS_YOU / GMAIL_BLOCKED_RECAPTCHA / NO_VERIFY_CODE
#       建议: 等 Google 风控解 / 走 sessionkey 流程 / 手工登 Gmail 后重跑
#   3 = token 拿到但探针失败

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
SS_DIR="/tmp/cc-screenshots-$ACCT"
LOG_REMOTE="/tmp/cc-oauth-$ACCT.log"
PY_REMOTE="/tmp/cc-oauth-gmail-v3.py"
DOCKER_ENV_REMOTE="/tmp/cc-oauth-$ACCT.env"

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  add-cc-account-gmail-v3.sh  $ACCT  (verify-code flow)"
echo "╚══════════════════════════════════════════════════════════════╝"

# ── 1/5: 检查 .creds + 上传脚本 ─────────────────────────────────────
echo "==[1/5]== 检查 $CREDS_REMOTE + 上传 patchright 脚本"
ssh "$SSH_188" "test -f $CREDS_REMOTE" || {
    echo "❌ $CREDS_REMOTE 不存在"
    exit 1
}
scp -q "$PY_SCRIPT" "$SSH_188:$PY_REMOTE"

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

# ── 3/5: patchright Docker 跑 OAuth ────────────────────────────────
echo "==[3/5]== Docker patchright 跑 OAuth (~3-4min, verify-code 流程)"
CC_EMAIL=$(ssh "$SSH_188" "grep ^email= $CREDS_REMOTE | cut -d= -f2-")
GMAIL_PW=$(ssh "$SSH_188" "grep ^gmail_pw= $CREDS_REMOTE | cut -d= -f2-")
GMAIL_TOTP=$(ssh "$SSH_188" "grep ^gmail_totp= $CREDS_REMOTE | cut -d= -f2-")
trap 'ssh "$SSH_188" "rm -f $DOCKER_ENV_REMOTE" >/dev/null 2>&1 || true' EXIT
ssh "$SSH_188" "umask 077; cat > $DOCKER_ENV_REMOTE" <<ENVEOF
CC_EMAIL=$CC_EMAIL
GMAIL_PW=$GMAIL_PW
GMAIL_TOTP=$GMAIL_TOTP
CC_OAUTH_URL=$OAUTH_URL
PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
DISPLAY=:99
ENVEOF

# 同时抓 callback 和异常状态码
set +e
RAW_OUT=$(ssh "$SSH_188" "
    rm -rf $SS_DIR && mkdir -p $SS_DIR
    docker run --rm \
      -v $PY_REMOTE:/work/script.py:ro \
      -v $SS_DIR:/work/screenshots \
      --env-file $DOCKER_ENV_REMOTE \
      mcr.microsoft.com/playwright/python:v1.59.0-noble \
      bash -c 'Xvfb :99 -screen 0 1280x800x24 >/dev/null 2>&1 & sleep 1 && \
               pip install patchright==1.59.1 pyotp -q --root-user-action=ignore 2>&1 | tail -1 && \
               python3 /work/script.py' 2>&1
    RC=\$?
    rm -f $DOCKER_ENV_REMOTE
    exit \$RC
")
DOCKER_RC=$?
set -e
echo "  docker rc=$DOCKER_RC"
CALLBACK=$(echo "$RAW_OUT" | grep '^✅ CALLBACK_CODE=' | tail -1 | sed 's/.*CALLBACK_CODE=//' || true)

if [[ -z "$CALLBACK" ]]; then
    FAIL=$(echo "$RAW_OUT" | grep -oE 'GMAIL_BLOCKED_[A-Z_]+|NO_VERIFY_CODE|NO_SECURE_LINK|CLAUDE_PAGE_STALE|CLAUDE_NO_VERIFY_PAGE' | head -1 || true)
    echo "❌ OAuth 失败: ${FAIL:-UNKNOWN}"
    echo "  截图: ssh $SSH_188 ls $SS_DIR"
    echo "  最后 30 行 output:"
    echo "$RAW_OUT" | tail -30 \
      | sed -E 's/sk-ant-[A-Za-z0-9_-]+/[REDACTED]/g; s/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+/[EMAIL]/g; s#https://[^[:space:]]+#https://[REDACTED]#g; s/(TOTP = )[0-9]+/\1[REDACTED]/g; s/(Gmail TOTP = )[0-9]+/\1[REDACTED]/g' \
      | sed 's/^/    /'
    exit 2
fi
echo "  ✅ callback code: ${CALLBACK:0:30}..."

# ── 4/5: 粘 code 回 tmux,拿 sk-ant-oat token ───────────────────────
echo "==[4/5]== 粘 callback code 回 setup-token,等待 token..."
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

# ── 5/5: 写 .env + Haiku 探针验证 ───────────────────────────────────
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
    echo "   下一步: claude-max-proxy 自动 pick up (env_file 重新读),"
    echo "          或 docker compose -f /Data/claude-max-proxy/docker-compose.yml restart"
    exit 0
else
    echo "  ❌ 探针失败: $RESP"
    exit 3
fi
