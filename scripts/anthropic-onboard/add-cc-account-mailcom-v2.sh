#!/usr/bin/env bash
# add-cc-account-mailcom-v2.sh — claude.ai 新 verify-code 流程版 (2026-05-25)
#
# 用法:
#   ./scripts/anthropic-onboard/add-cc-account-mailcom-v2.sh acct-N
#
# 与 v1 区别:
#   - 4 段架构 (storage_state 路径,跨段共享 cookie)
#   - 适配 claude.ai 新 "Enter verification code" 中间页流程
#   - 多 mount 一个 /tmp/cc-state-acct-N.json
#
# .creds 字段同 v1: email + mail_pw

set -eo pipefail

ACCT="${1:?用法: $0 acct-N}"
if [[ ! "$ACCT" =~ ^acct-[0-9]+$ ]]; then
    echo "❌ ACCT 格式必须是 acct-N (如 acct-4),实际: $ACCT"; exit 1
fi
SSH_188="cltx@10.68.13.188"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_SCRIPT="$SCRIPT_DIR/cc-oauth-mailcom-v2.py"
[[ -f "$PY_SCRIPT" ]] || { echo "❌ 找不到 $PY_SCRIPT"; exit 1; }

CREDS_REMOTE="/Data/anthropic-auth/$ACCT/.creds"
ENV_REMOTE="/Data/anthropic-auth/$ACCT/.env"
SS_DIR="/tmp/cc-screenshots-$ACCT"
STATE_FILE="/tmp/cc-state-$ACCT.json"
LOG_REMOTE="/tmp/cc-oauth-$ACCT.log"
PY_REMOTE="/tmp/cc-oauth-mailcom-v2.py"

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  add-cc-account-mailcom-v2.sh  $ACCT"
echo "╚══════════════════════════════════════════════════════════════╝"

echo "==[1/5]== 检查 $CREDS_REMOTE + 上传 v2 脚本"
ssh "$SSH_188" "test -f $CREDS_REMOTE" || {
    echo "❌ $CREDS_REMOTE 不存在"
    exit 1
}
scp -q "$PY_SCRIPT" "$SSH_188:$PY_REMOTE"

echo "==[2/5]== 启 tmux 跑 claude setup-token,拿 OAuth URL"
OAUTH_URL=$(ssh "$SSH_188" "
    tmux kill-session -t cc-oauth-$ACCT 2>/dev/null
    rm -f $LOG_REMOTE $STATE_FILE
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

echo "==[3/5]== Docker patchright 跑 4 段 OAuth (~5-7min)"
CC_EMAIL=$(ssh "$SSH_188" "grep ^email= $CREDS_REMOTE | cut -d= -f2-")
MAIL_PW=$(ssh "$SSH_188" "grep ^mail_pw= $CREDS_REMOTE | cut -d= -f2-")
[[ -n "$MAIL_PW" ]] || { echo "❌ .creds 缺 mail_pw= 字段"; exit 1; }

CALLBACK=$(ssh "$SSH_188" "
    rm -rf $SS_DIR && mkdir -p $SS_DIR
    touch $STATE_FILE && chmod 666 $STATE_FILE
    docker run --rm \
      -v $PY_REMOTE:/work/script.py:ro \
      -v $SS_DIR:/work/screenshots \
      -v $STATE_FILE:/work/state.json \
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
    echo "    runner log: ssh $SSH_188 tail -40 /tmp/cc-oauth-$ACCT-runner.log"
    exit 2
fi
echo "  ✅ callback code: ${CALLBACK:0:30}..."

echo "==[4/5]== 粘 callback code 回 setup-token"
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
    echo "🎉 $ACCT 上线完成: $ENV_REMOTE"
    exit 0
else
    echo "  ❌ 探针失败: $RESP"
    exit 3
fi
