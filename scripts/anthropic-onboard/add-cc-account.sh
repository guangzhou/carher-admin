#!/usr/bin/env bash
# add-cc-account.sh — Claude Code Max 账号一键 OAuth + 落盘
#
# 用法:
#   ./scripts/anthropic-onboard/add-cc-account.sh acct-N
#
# 前提:已在 188 上建好 /Data/anthropic-auth/acct-N/.creds:
#   email=xxx@gmail.com
#   gmail_pw=<gmail 密码>
#   gmail_totp=<Google 2FA TOTP secret>
#   # (optional) helper_email=xxx@gisellee.top  仅记录,流程不用
#   # (optional) country=US                       仅记录
#
# 流程 (~3-5min):
#   1. ssh 188 → tmux session 跑 `claude setup-token` 拿 OAuth URL
#   2. docker run patchright cc-oauth-full.py → 自动完成 Gmail 登录 + Authorize + 抓 code
#   3. 把 code 粘回 tmux session → setup-token 打印 sk-ant-oat token
#   4. 写入 /Data/anthropic-auth/acct-N/.env (ANTHROPIC_OAUTH_TOKEN=)
#   5. 用 Haiku 4.5 探针验证 token 有效 (Opus/Sonnet 可能被 Team 共享池打满,不用作探针)
#
# 非交互,退出码:
#   0 = OK 落盘 + 探针 200 OK
#   1 = 配置错误 (creds 缺失等)
#   2 = OAuth 流程失败 (看 /tmp/cc-screenshots-acct-N/)
#   3 = token 拿到但探针失败

set -eo pipefail

ACCT="${1:?用法: $0 acct-N}"
if [[ ! "$ACCT" =~ ^acct-[0-9]+$ ]]; then
    echo "❌ ACCT 格式必须是 acct-N (如 acct-1),实际: $ACCT"; exit 1
fi
N="${ACCT#acct-}"
SSH_188="cltx@10.68.13.188"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_SCRIPT="$SCRIPT_DIR/cc-oauth-full.py"
[[ -f "$PY_SCRIPT" ]] || { echo "❌ 找不到 $PY_SCRIPT"; exit 1; }

CREDS_REMOTE="/Data/anthropic-auth/$ACCT/.creds"
ENV_REMOTE="/Data/anthropic-auth/$ACCT/.env"
SS_DIR="/tmp/cc-screenshots-$ACCT"
LOG_REMOTE="/tmp/cc-oauth-$ACCT.log"
PY_REMOTE="/tmp/cc-oauth-full.py"

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  add-cc-account.sh  $ACCT"
echo "╚══════════════════════════════════════════════════════════════╝"

# ── 1/5: 检查 .creds + 上传脚本 ─────────────────────────────────────
echo "==[1/5]== 检查 $CREDS_REMOTE + 上传 patchright 脚本"
ssh "$SSH_188" "test -f $CREDS_REMOTE" || {
    echo "❌ $CREDS_REMOTE 不存在。请先建:"
    echo ""
    echo "  ssh $SSH_188 'cat > $CREDS_REMOTE <<EOF"
    echo "email=xxx@gmail.com"
    echo "gmail_pw=<gmail 密码>"
    echo "gmail_totp=<Google 2FA TOTP secret>"
    echo "EOF"
    echo "   chmod 600 $CREDS_REMOTE'"
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

# ── 3/5: patchright Docker 跑 OAuth + 抓 callback code ─────────────
echo "==[3/5]== Docker patchright 跑 OAuth (~2-3min)"
CC_EMAIL=$(ssh "$SSH_188" "grep ^email= $CREDS_REMOTE | cut -d= -f2-")
GMAIL_PW=$(ssh "$SSH_188" "grep ^gmail_pw= $CREDS_REMOTE | cut -d= -f2-")
GMAIL_TOTP=$(ssh "$SSH_188" "grep ^gmail_totp= $CREDS_REMOTE | cut -d= -f2-")

CALLBACK=$(ssh "$SSH_188" "
    rm -rf $SS_DIR && mkdir -p $SS_DIR
    docker run --rm \
      -v $PY_REMOTE:/work/script.py:ro \
      -v $SS_DIR:/work/screenshots \
      -e CC_EMAIL='$CC_EMAIL' \
      -e GMAIL_PW='$GMAIL_PW' \
      -e GMAIL_TOTP='$GMAIL_TOTP' \
      -e CC_OAUTH_URL='$OAUTH_URL' \
      -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright -e DISPLAY=:99 \
      mcr.microsoft.com/playwright/python:v1.59.0-noble \
      bash -c 'Xvfb :99 -screen 0 1280x800x24 >/dev/null 2>&1 & sleep 1 && \
               pip install patchright==1.59.1 pyotp -q --root-user-action=ignore 2>&1 | tail -1 && \
               python3 /work/script.py' 2>&1 | grep '^✅ CALLBACK_CODE=' | tail -1 | sed 's/.*CALLBACK_CODE=//'
")
[[ -n "$CALLBACK" ]] || { echo "❌ OAuth 失败,看 ssh $SSH_188 ls $SS_DIR"; exit 2; }
echo "  ✅ callback code: ${CALLBACK:0:30}..."

# ── 4/5: 粘 code 回 tmux,拿 sk-ant-oat token ───────────────────────
echo "==[4/5]== 粘 callback code 回 setup-token,等待 token..."
# 同时把 state 拼到 code 后面 (用 # 分隔,这是 claude setup-token 期望的格式)
# 但实测我们已经看到只粘 code 也行 (Enter 后会自动接受)
# 这里保守做法:抓 OAuth URL 里的 state 拼上
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

# 验证: 用 Haiku (Opus/Sonnet 可能 Team 共享池满)
RESP=$(ssh "$SSH_188" "curl -s https://api.anthropic.com/v1/messages \
  -H 'Authorization: Bearer $TOKEN' \
  -H 'anthropic-beta: oauth-2025-04-20' \
  -H 'anthropic-dangerous-direct-browser-access: true' \
  -H 'anthropic-version: 2023-06-01' \
  -H 'content-type: application/json' \
  -d '{\"model\":\"claude-haiku-4-5\",\"max_tokens\":20,\"messages\":[{\"role\":\"user\",\"content\":\"reply OK\"}]}'")
if echo "$RESP" | grep -q '"type":"message"'; then
    echo "  ✅ Haiku 探针 200 OK"
    # 也试 Opus, 失败不退出 (作信息提示)
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
    echo "   token: /Data/anthropic-auth/$ACCT/.env"
    echo "   下一步: 起 litellm-anthropic-$N 容器接入 198 / 阿里云 LiteLLM"
    exit 0
else
    echo "  ❌ 探针失败: $RESP"
    exit 3
fi
