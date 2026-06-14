#!/usr/bin/env bash
# add-cc-account-outlook.sh — Outlook (live.com) 变体, 平行 add-cc-account.sh (Gmail)
#
# 用法:
#   ./scripts/anthropic-onboard/add-cc-account-outlook.sh acct-N
#
# 前提:已在 188 上建好 /Data/anthropic-auth/acct-N/.creds:
#   email=xxx@outlook.com
#   mail_pw=<Outlook 登录密码>
#   mail_provider=outlook   # 仅记录,本脚本不读
#   # (optional) totp 之类 — Outlook 账号若开了 2FA, 自动化大概率挂, 走手工
#
# 跟 add-cc-account.sh 唯一区别:
#   - PY_SCRIPT 用 cc-oauth-outlook.py (login.live.com + outlook.live.com inbox)
#   - 环境变量 GMAIL_PW/GMAIL_TOTP → MAIL_PW (无 TOTP)
#
# 退出码同 Gmail 版本: 0=OK, 1=配置, 2=OAuth 失败, 3=token 探针失败.
# Outlook 反自动化更严, 若挂 OUTLOOK_CHALLENGE 看 /tmp/cc-screenshots-acct-N/ 手动救场.

set -eo pipefail

ACCT="${1:?用法: $0 acct-N}"
if [[ ! "$ACCT" =~ ^acct-[0-9]+$ ]]; then
    echo "❌ ACCT 格式必须是 acct-N (如 acct-3),实际: $ACCT"; exit 1
fi
N="${ACCT#acct-}"
SSH_188="cltx@10.68.13.188"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_SCRIPT="$SCRIPT_DIR/cc-oauth-outlook.py"
[[ -f "$PY_SCRIPT" ]] || { echo "❌ 找不到 $PY_SCRIPT"; exit 1; }

CREDS_REMOTE="/Data/anthropic-auth/$ACCT/.creds"
ENV_REMOTE="/Data/anthropic-auth/$ACCT/.env"
SS_DIR="/tmp/cc-screenshots-$ACCT"
LOG_REMOTE="/tmp/cc-oauth-$ACCT.log"
PY_REMOTE="/tmp/cc-oauth-outlook.py"

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  add-cc-account-outlook.sh  $ACCT"
echo "╚══════════════════════════════════════════════════════════════╝"

# ── 1/5: 检查 .creds + 上传脚本 ─────────────────────────────────────
echo "==[1/5]== 检查 $CREDS_REMOTE + 上传 patchright 脚本"
ssh "$SSH_188" "test -f $CREDS_REMOTE" || {
    echo "❌ $CREDS_REMOTE 不存在。请先建:"
    echo ""
    echo "  ssh $SSH_188 'cat > $CREDS_REMOTE <<EOF"
    echo "email=xxx@outlook.com"
    echo "mail_pw=<Outlook 密码>"
    echo "mail_provider=outlook"
    echo "EOF"
    echo "   chmod 600 $CREDS_REMOTE'"
    exit 1
}
scp -q "$PY_SCRIPT" "$SSH_188:$PY_REMOTE"

# ── 2/5: tmux 启 setup-token ────────────────────────────────────────
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

# ── 3/5: patchright Outlook OAuth ──────────────────────────────────
echo "==[3/5]== Docker patchright 跑 Outlook OAuth (~3-5min)"
CC_EMAIL=$(ssh "$SSH_188" "grep ^email= $CREDS_REMOTE | cut -d= -f2-")
MAIL_PW=$(ssh "$SSH_188" "grep ^mail_pw= $CREDS_REMOTE | cut -d= -f2-")

CALLBACK=$(ssh "$SSH_188" "
    rm -rf $SS_DIR && mkdir -p $SS_DIR
    docker run --rm \
      -v $PY_REMOTE:/work/script.py:ro \
      -v $SS_DIR:/work/screenshots \
      -e CC_EMAIL='$CC_EMAIL' \
      -e MAIL_PW='$MAIL_PW' \
      -e CC_OAUTH_URL='$OAUTH_URL' \
      -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright -e DISPLAY=:99 \
      mcr.microsoft.com/playwright/python:v1.59.0-noble \
      bash -c 'Xvfb :99 -screen 0 1280x800x24 >/dev/null 2>&1 & sleep 1 && \
               pip install patchright==1.59.1 -q --root-user-action=ignore 2>&1 | tail -1 && \
               python3 /work/script.py' 2>&1 | tee /tmp/cc-oauth-$ACCT-runner.log | grep '^✅ CALLBACK_CODE=' | tail -1 | sed 's/.*CALLBACK_CODE=//'
")
if [[ -z "$CALLBACK" ]]; then
    echo "❌ OAuth 失败. 截图: ssh $SSH_188 ls $SS_DIR"
    echo "    runner log: ssh $SSH_188 tail -30 /tmp/cc-oauth-$ACCT-runner.log"
    exit 2
fi
echo "  ✅ callback code: ${CALLBACK:0:30}..."

# ── 4/5: 粘 code 回 tmux ───────────────────────────────────────────
echo "==[4/5]== 粘 callback code 回 setup-token,等 token..."
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
    echo "❌ 没拿到 token. setup-token log:"
    ssh "$SSH_188" "tail -40 $LOG_REMOTE"
    exit 2
}
echo "  ✅ token: ${TOKEN:0:30}... (len=${#TOKEN})"

# ── 5/5: 写 .env + Haiku 探针 ──────────────────────────────────────
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
    echo "   token: $ENV_REMOTE"
    echo "   下一步:"
    echo "     1. 把 $ACCT token 加进 188 claude-max-proxy 的 ACCT_TOKENS"
    echo "        sed -i.bak 's/^ACCT_TOKENS=.*/&,${ACCT}::$TOKEN/' /Data/claude-max-proxy/.env"
    echo "     2. docker compose -f /Data/claude-max-proxy/docker-compose.yml up -d"
    echo "     3. ./scripts/anthropic-onboard/cc-max-upstream-status.sh 验证"
    exit 0
else
    echo "  ❌ 探针失败: $RESP"
    exit 3
fi
