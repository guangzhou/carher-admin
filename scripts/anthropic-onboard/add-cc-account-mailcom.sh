#!/usr/bin/env bash
# add-cc-account-mailcom.sh — mail.com (1and1) webmail 变体, 平行 outlook/171mail
#
# 用法:
#   ./scripts/anthropic-onboard/add-cc-account-mailcom.sh acct-N
#
# 适用账号特征:
#   - email 域名是 mail.com 系 (@therapist.net / @gmx.com / @consultant.com / @engineer.com 等都属 1and1)
#   - 卖家成品号字段 `email----mail_pw----...` 第 2 段是 mail.com webmail 密码
#   - 卖家文案明示"邮箱登录地址:mail.com"
#   - **171mail relay token 已死或不存在,改走 webmail 密码登录路径**
#
# 前提:已在 188 上建好 /Data/anthropic-auth/acct-N/.creds:
#   email=xxx@therapist.net
#   mail_pw=<mail.com webmail 密码>
#   mail_provider=mailcom
#   # (optional) relay_token=...  # 仅记录,本脚本不读
#
# 退出码同其他变体: 0=OK, 1=配置, 2=OAuth 失败, 3=token 探针失败.

set -eo pipefail

ACCT="${1:?用法: $0 acct-N}"
if [[ ! "$ACCT" =~ ^acct-[0-9]+$ ]]; then
    echo "❌ ACCT 格式必须是 acct-N (如 acct-4),实际: $ACCT"; exit 1
fi
N="${ACCT#acct-}"
SSH_188="cltx@10.68.13.188"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_SCRIPT="$SCRIPT_DIR/cc-oauth-mailcom.py"
[[ -f "$PY_SCRIPT" ]] || { echo "❌ 找不到 $PY_SCRIPT"; exit 1; }

CREDS_REMOTE="/Data/anthropic-auth/$ACCT/.creds"
ENV_REMOTE="/Data/anthropic-auth/$ACCT/.env"
SS_DIR="/tmp/cc-screenshots-$ACCT"
LOG_REMOTE="/tmp/cc-oauth-$ACCT.log"
PY_REMOTE="/tmp/cc-oauth-mailcom.py"

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  add-cc-account-mailcom.sh  $ACCT"
echo "╚══════════════════════════════════════════════════════════════╝"

# ── 1/5: 检查 .creds + 上传脚本 ─────────────────────────────────────
echo "==[1/5]== 检查 $CREDS_REMOTE + 上传 patchright 脚本"
ssh "$SSH_188" "test -f $CREDS_REMOTE" || {
    echo "❌ $CREDS_REMOTE 不存在。请先建:"
    echo ""
    echo "  ssh $SSH_188 'cat > $CREDS_REMOTE <<EOF"
    echo "email=xxx@therapist.net"
    echo "mail_pw=<mail.com webmail 密码>"
    echo "mail_provider=mailcom"
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

# ── 3/5: patchright mail.com OAuth ─────────────────────────────────
echo "==[3/5]== Docker patchright 跑 mail.com OAuth (~3-5min)"
CC_EMAIL=$(ssh "$SSH_188" "grep ^email= $CREDS_REMOTE | cut -d= -f2-")
MAIL_PW=$(ssh "$SSH_188" "grep ^mail_pw= $CREDS_REMOTE | cut -d= -f2-")
[[ -n "$MAIL_PW" ]] || { echo "❌ .creds 缺 mail_pw= 字段"; exit 1; }

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
               pip install patchright==1.59.1 playwright==1.59.0 -q --root-user-action=ignore 2>&1 | tail -1 && \
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
    OPUS_RESP=$(ssh "$SSH_188" "curl -s https://api.anthropic.com/v1/messages \
      -H 'Authorization: Bearer $TOKEN' \
      -H 'anthropic-beta: oauth-2025-04-20' \
      -H 'anthropic-dangerous-direct-browser-access: true' \
      -H 'anthropic-version: 2023-06-01' \
      -H 'content-type: application/json' \
      -d '{\"model\":\"claude-opus-4-7\",\"max_tokens\":20,\"messages\":[{\"role\":\"user\",\"content\":\"reply OK\"}]}'")
    if echo "$OPUS_RESP" | grep -q '"type":"message"'; then
        echo "  ✅ Opus 4.7 探针也 200 OK — 完整可用"
    elif echo "$OPUS_RESP" | grep -q 'rate_limit'; then
        echo "  ⚠️ Opus 4.7 = rate_limit_error (Team 共享池打满,Haiku 还能用)"
    else
        echo "  ⚠️ Opus 4.7 response: $(echo $OPUS_RESP | head -c 200)"
    fi
    echo ""
    echo "🎉 $ACCT 上线完成"
    echo "   token: $ENV_REMOTE"
    echo "   下一步:"
    echo "     1. 把 $ACCT token 加进 188 claude-max-proxy 的 ACCT_TOKENS"
    echo "        ssh $SSH_188 \"sed -i.bak 's/^ACCT_TOKENS=.*/&,${ACCT}::$TOKEN/' /Data/claude-max-proxy/.env\""
    echo "     2. ssh $SSH_188 \"cd /Data/claude-max-proxy && docker compose up -d\""
    echo "     3. ./scripts/anthropic-onboard/cc-max-upstream-status.sh 验证"
    exit 0
else
    echo "  ❌ 探针失败: $RESP"
    exit 3
fi
