#!/bin/bash
# add-chatgpt-account.sh — 全自动上线新 ChatGPT Pro 账号
#
# 两种用法:
#   (A) 已有 auth.json:     ./scripts/add-chatgpt-account.sh acct-N /path/to/auth.json
#   (B) 用 .creds 自动 OAuth: ./scripts/add-chatgpt-account.sh acct-N --oauth
#       前提: 已在 188:/Data/chatgpt-auth/acct-N/.creds 写入 email/mail_pw/chatgpt_pw
#
# 流程 (~3-5min):
#   1/7 (仅 --oauth) 调 re-oauth.sh 在 188 上跑 patchright OAuth → auth.json
#   1/7 (传 auth.json) scp 上 188:/Data/chatgpt-auth/acct-N/auth.json
#   2/7 启 docker 容器 litellm-chatgpt-N (端口 4000+N)
#   3/7 健康门控 30s
#   4/7 /codex/usage 探针 (验证 plan=pro)
#   5/7 注册 4 个 deployment 到 198 prod admin API
#   6/7 触发 quota-cron
#   7/7 chatgpt-acct-status.py 验证 🟢 HEALTHY
#
# 命名约定: model_info.id = chatgpt-acct-N-gpt-5.x

set -euo pipefail

ACCT="${1:?用法: $0 acct-N [/path/to/auth.json|--oauth]}"
AUTH_SRC="${2:?需提供 /path/to/auth.json 或 --oauth}"

if [[ ! "$ACCT" =~ ^acct-[0-9]+$ ]]; then
  echo "❌ ACCT 格式必须是 acct-N (如 acct-12), 实际: $ACCT"; exit 1
fi

N="${ACCT#acct-}"
PORT=$((4000 + N))
SSH_188="cltx@10.68.13.188"
MASTER_KEY="${CHATGPT_188_MASTER_KEY:?set CHATGPT_188_MASTER_KEY}"

# ── 1/7: 拿 auth.json ────────────────────────────────────────────────
if [[ "$AUTH_SRC" == "--oauth" ]]; then
  echo "==[1/7]== 调 re-oauth.sh 在 188 上跑 patchright OAuth (~3-5min)..."
  ssh "$SSH_188" "test -f /Data/chatgpt-auth/$ACCT/.creds" || {
    echo "❌ 需要先建 /Data/chatgpt-auth/$ACCT/.creds:"
    echo "   ssh $SSH_188 'cat > /Data/chatgpt-auth/$ACCT/.creds <<EOF"
    echo "email=xxx@mail.com"
    echo "mail_pw=<webmail 字段A>"
    echo "chatgpt_pw=<ChatGPT 字段B>"
    echo "EOF"
    echo "   chmod 600 /Data/chatgpt-auth/$ACCT/.creds'"
    exit 1
  }
  ssh "$SSH_188" "test -f /Data/chatgpt-auth/re-oauth.sh" || {
    echo "❌ /Data/chatgpt-auth/re-oauth.sh 不存在 — scp 上 188 先"
    exit 1
  }
  # re-oauth.sh 内部已经做了 docker cp + restart + verify, 跑完即上线 6/7 中 1-3 步
  # 但 re-oauth.sh 假设容器已存在; 新账号容器还不存在, 所以这里只用它产 auth.json
  # ── 改用直接调 chatgpt-litellm-oauth.py 拿 auth.json
  ssh "$SSH_188" "test -f /tmp/chatgpt-litellm-oauth.py" || {
    echo "❌ /tmp/chatgpt-litellm-oauth.py 不存在 — 先 scp 上 188"
    exit 1
  }
  OAUTH_RUN_ID="$(date +%s)"
  AUTH_PATH_188="/tmp/auth-${ACCT}-${OAUTH_RUN_ID}.json"
  AUTH_FILE_NAME="$(basename "$AUTH_PATH_188")"
  SCREENSHOT_DIR_188="/tmp/screenshots-${ACCT}-${OAUTH_RUN_ID}"
  ssh "$SSH_188" "
    set -euo pipefail
    source <(grep -E '^(email|mail_pw|chatgpt_pw)=' /Data/chatgpt-auth/$ACCT/.creds | sed 's/^/declare /')
    SECRET_DIR=\$(mktemp -d /tmp/add-acct-${ACCT}-XXXXXX)
    trap 'rm -rf \$SECRET_DIR' EXIT
    printf '%s' \"\$mail_pw\" > \$SECRET_DIR/mail_pw.txt
    printf '%s' \"\$chatgpt_pw\" > \$SECRET_DIR/chatgpt_pw.txt
    chmod 600 \$SECRET_DIR/*
    mkdir -p '$SCREENSHOT_DIR_188'
    docker run --rm \
      -v /tmp/chatgpt-litellm-oauth.py:/work/chatgpt-litellm-oauth.py:ro \
      -v \$SECRET_DIR/mail_pw.txt:/run/mail_pw.txt:ro \
      -v \$SECRET_DIR/chatgpt_pw.txt:/run/chatgpt_pw.txt:ro \
      -v '$SCREENSHOT_DIR_188':/work/screenshots \
      -v /tmp:/work/out \
      -e MAIL_USER=\"\$email\" \
      -e MAIL_LOGIN_PW_FILE=/run/mail_pw.txt \
      -e CHATGPT_PW_FILE=/run/chatgpt_pw.txt \
      -e AUTH_JSON_OUTPUT=/work/out/$AUTH_FILE_NAME \
      -e SCREENSHOT_DIR=/work/screenshots \
      -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
      -e DISPLAY=:99 \
      mcr.microsoft.com/playwright/python:v1.60.0-noble \
      bash -c 'Xvfb :99 -screen 0 1280x800x24 >/dev/null 2>&1 & sleep 1 && pip install patchright -q --root-user-action=ignore 2>&1 | tail -1 && python3 /work/chatgpt-litellm-oauth.py' 2>&1 | grep -v '^The XKEY\|^> Warning\|^Errors from\|^\[notice\]' | tail -150
    test -s '$AUTH_PATH_188' || { echo '❌ OAuth 失败 — 见 $SCREENSHOT_DIR_188/'; exit 1; }
    echo '  ✅ auth.json 已产生'
  "
  # 把 188 上的 auth.json 落到目标 dir
  ssh "$SSH_188" "mkdir -p /Data/chatgpt-auth/$ACCT && chmod 700 /Data/chatgpt-auth/$ACCT && cp '$AUTH_PATH_188' /Data/chatgpt-auth/$ACCT/auth.json && chmod 600 /Data/chatgpt-auth/$ACCT/auth.json"
  echo "  ✅ /Data/chatgpt-auth/$ACCT/auth.json"
else
  echo "==[1/7]== 上传本地 auth.json 到 188:/Data/chatgpt-auth/$ACCT/"
  if [[ ! -f "$AUTH_SRC" ]]; then
    echo "❌ $AUTH_SRC not found"; exit 1
  fi
  ssh "$SSH_188" "mkdir -p /Data/chatgpt-auth/$ACCT && chmod 700 /Data/chatgpt-auth/$ACCT"
  scp -q "$AUTH_SRC" "$SSH_188:/Data/chatgpt-auth/$ACCT/auth.json"
  ssh "$SSH_188" "chmod 600 /Data/chatgpt-auth/$ACCT/auth.json"
  echo "  ✅ uploaded"
fi

# ── 2/7: 启 docker 容器 ──────────────────────────────────────────────
echo "==[2/7]== 启 docker 容器 litellm-chatgpt-$N (端口 $PORT)"
ssh "$SSH_188" "cd /Data/chatgpt-auth && docker compose up -d litellm-chatgpt-$N"

# ── 3/7: 健康门控 ────────────────────────────────────────────────────
echo "==[3/7]== 健康门控 (最长 30s)"
HEALTHY=0
for i in $(seq 1 15); do
  if ssh "$SSH_188" "curl -fsS http://localhost:$PORT/health/liveliness" >/dev/null 2>&1; then
    echo "  ✅ port $PORT healthy"; HEALTHY=1; break
  fi
  sleep 2
done
[[ $HEALTHY -eq 1 ]] || { echo "  ❌ port $PORT 30s 内未健康"; echo "  ssh $SSH_188 'docker logs litellm-chatgpt-$N --tail 50'"; exit 1; }

# ── 4/7: /codex/usage 探针 ───────────────────────────────────────────
echo "==[4/7]== 验证 /codex/usage (plan=pro)"
ssh "$SSH_188" "docker exec litellm-chatgpt-$N cat /chatgpt-auth/auth.json" | python3 -c '
import json, base64, urllib.request, sys
auth = json.load(sys.stdin)
tok = auth["access_token"]
aid = auth.get("account_id") or json.loads(base64.urlsafe_b64decode(
    auth["id_token"].split(".")[1] + "=="))["https://api.openai.com/auth"]["chatgpt_account_id"]
req = urllib.request.Request(
    "https://chatgpt.com/backend-api/codex/usage",
    headers={"Authorization": "Bearer " + tok, "chatgpt-account-id": aid,
             "Originator": "codex_cli_rs",
             "User-Agent": "codex_cli_rs/0.30.0 (Linux; x86_64)"})
u = json.loads(urllib.request.urlopen(req, timeout=15).read())
plan = u["plan_type"]
rl = u["rate_limit"]
pct5h = rl["primary_window"]["used_percent"]
pctwk = rl["secondary_window"]["used_percent"]
assert plan == "pro", "plan_type=" + str(plan) + " (not pro)"
print("  ✅ plan=" + plan + " 5h=" + str(pct5h) + "% week=" + str(pctwk) + "% acct=" + aid)
'

# ── 5/7: 注册到 198 prod LiteLLM ─────────────────────────────────────
echo "==[5/7]== 注册到 198 prod LiteLLM (admin API)"
MK=$(jms ssh AIYJY-litellm "kubectl get secret litellm-secrets -n litellm-product -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d")
for model in chatgpt-gpt-5.5 chatgpt-gpt-5.4 chatgpt-gpt-5.3-codex chatgpt-gpt-5.3-codex-spark; do
  MID="chatgpt-${ACCT}-${model#chatgpt-}"
  echo "  注册 model_name=$model id=$MID"
  jms ssh AIYJY-litellm "curl -fsS -X POST http://localhost:30402/model/new \
    -H 'Authorization: Bearer $MK' -H 'Content-Type: application/json' \
    -d '{
      \"model_name\": \"$model\",
      \"litellm_params\": {
        \"model\": \"openai/$model\",
        \"api_base\": \"http://10.68.13.188:$PORT\",
        \"api_key\": \"$MASTER_KEY\"
      },
      \"model_info\": {\"id\":\"$MID\",\"mode\":\"responses\"}
    }'" >/dev/null
done
echo "  ✅ 4 个 deployment 已注册"

# ── 6/7: 触发 quota-cron ─────────────────────────────────────────────
echo "==[6/7]== 触发 quota-cron"
ssh "$SSH_188" "sudo systemctl start chatgpt-quota.service" 2>/dev/null || echo "  ⚠️ chatgpt-quota.service 不存在,跳过"
sleep 3

# ── 7/7: 状态验证 ────────────────────────────────────────────────────
echo "==[7/7]== chatgpt-acct-status.py 验证"
if ssh "$SSH_188" "test -f /tmp/chatgpt-acct-status.py"; then
  STATUS=$(ssh "$SSH_188" "python3 /tmp/chatgpt-acct-status.py 2>/dev/null | grep -E '^${ACCT}\\b' | tail -1")
  echo "  $STATUS"
  if echo "$STATUS" | grep -q 'HEALTHY'; then
    echo ""
    echo "🎉 $ACCT 上线完成 — 🟢 HEALTHY"
    exit 0
  fi
fi
echo ""
echo "🎉 $ACCT 上线完成 (状态验证不完全 — 手动 check)"
echo "   后续任务:"
echo "   - git: 把 4 个 deployment 写进 30-cm-litellm-config.yaml 声明式备份"
echo "   - 飞书 wiki: 记录 $ACCT → 邮箱映射"
