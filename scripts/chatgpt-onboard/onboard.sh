#!/bin/bash
# onboard.sh — 一键给 ChatGPT Pro 账号做 OAuth device-code 自动绑定。
#
# 用法：./onboard.sh acct-3
#
# 前置条件（一次性）：
#   1. 188 上 /Data/chatgpt-auth/secrets.age 已写入（age 加密的 yaml，结构见 secrets.example.yaml）
#   2. 188 上已 docker pull <onboard-image>，或本目录跑过 `docker build -t chatgpt-onboard .`
#   3. 188 能直连 imap.mail.com:993 + auth.openai.com（已确认）
#
# 流程：
#   1. 188 上把 age 解密成 /tmp/secrets.<rand>.yaml（chmod 600，trap 退出删除）
#   2. 启动 litellm-onboard-tmp 临时容器跑 device-code → 解析出 user_code + verification_uri
#   3. 启动 chatgpt-onboard 临时容器跑 playwright + IMAP → 完成浏览器侧授权
#   4. 等 litellm-onboard-tmp 写出 auth.json → mv 到 /Data/chatgpt-auth/$ACCT/auth.json
#   5. 销毁两个临时容器
#   6. 调 ../add-chatgpt-account.sh $ACCT 接管"启容器 + 注册 198"
#
# 安全：明文 secrets 只在 /tmp 存活几十秒；trap EXIT 清理；不写 docker layer
set -euo pipefail

ACCT="${1:?用法: $0 acct-N}"
SSH_188="cltx@10.68.13.188"
WORK_DIR_188="/Data/chatgpt-auth/.onboard"

if [[ ! "$ACCT" =~ ^acct-[0-9]+$ ]]; then
  echo "ERROR: ACCT 格式必须是 acct-N，实际: $ACCT"; exit 1
fi

# ---- 0/6: 前置确认 ----
echo "==[0/6]== 前置确认"
ssh $SSH_188 "test -f /Data/chatgpt-auth/secrets.age" || {
  echo "ERROR: 188:/Data/chatgpt-auth/secrets.age 不存在"
  echo "  先一次性写入：age -e -p -o /Data/chatgpt-auth/secrets.age secrets.yaml"
  exit 1
}
ssh $SSH_188 "docker image inspect chatgpt-onboard:latest >/dev/null 2>&1" || {
  echo "ERROR: 188 上没 chatgpt-onboard 镜像"
  echo "  先：scp -r scripts/chatgpt-onboard $SSH_188:/tmp/ && ssh $SSH_188 'cd /tmp/chatgpt-onboard && docker build -t chatgpt-onboard:latest .'"
  exit 1
}

# ---- 1/6: 解密 secrets 到 188 临时目录 ----
echo "==[1/6]== 解密 secrets（明文只在 /tmp 存活）"
ssh $SSH_188 "mkdir -p $WORK_DIR_188 && chmod 700 $WORK_DIR_188"
SECRETS_TMP=$(ssh $SSH_188 "mktemp -p $WORK_DIR_188 secrets.XXXXXX.yaml")
ssh $SSH_188 "age -d /Data/chatgpt-auth/secrets.age > $SECRETS_TMP && chmod 600 $SECRETS_TMP"

cleanup() {
  ssh $SSH_188 "rm -f $SECRETS_TMP" 2>/dev/null || true
  ssh $SSH_188 "docker rm -f litellm-onboard-tmp chatgpt-onboard-tmp 2>/dev/null" || true
}
trap cleanup EXIT

# ---- 2/6: 触发 device code ----
echo "==[2/6]== 触发 OAuth device code（litellm 容器）"
ssh $SSH_188 "mkdir -p /Data/chatgpt-auth/$ACCT && chmod 700 /Data/chatgpt-auth/$ACCT"

# litellm 的 device flow 在容器内 stdout 打 user_code + verification_uri
# 这一步精确格式取决于 litellm 内部，需要先在 188 上空跑一次拿日志
# 占位：这里先用一个 echo 做 placeholder，落地时替换为真实命令
USER_CODE_FILE=$(ssh $SSH_188 "mktemp -p $WORK_DIR_188 user_code.XXXXXX")
ssh $SSH_188 "docker run -d --name litellm-onboard-tmp \
  -v /Data/chatgpt-auth/$ACCT:/chatgpt-auth \
  -e CHATGPT_TOKEN_DIR=/chatgpt-auth \
  ghcr.io/berriai/litellm:main-stable \
  --device-code-flow > $USER_CODE_FILE.cid"   # ⚠️ 真实命令需调研 litellm 入口

# 等容器打出 user_code（≤30s）
echo "  等 litellm 容器打出 user_code"
USER_CODE=""
for i in $(seq 1 15); do
  USER_CODE=$(ssh $SSH_188 "docker logs litellm-onboard-tmp 2>&1 | grep -oE 'user_code[: ]+[A-Z0-9-]{8,12}' | head -1 | grep -oE '[A-Z0-9-]{8,12}'" || true)
  [[ -n "$USER_CODE" ]] && break
  sleep 2
done
if [[ -z "$USER_CODE" ]]; then
  echo "ERROR: 30s 内没拿到 user_code"
  ssh $SSH_188 "docker logs litellm-onboard-tmp"
  exit 1
fi
echo "  user_code = $USER_CODE"

# ---- 3/6: 跑 playwright 完成浏览器侧授权 ----
echo "==[3/6]== headless chromium + IMAP（最长 3 min）"
ssh $SSH_188 "docker run --rm --name chatgpt-onboard-tmp \
  -v $SECRETS_TMP:/run/secrets.yaml:ro \
  chatgpt-onboard:latest \
  --acct $ACCT \
  --user-code $USER_CODE"

# ---- 4/6: 等 litellm 容器写出 auth.json ----
echo "==[4/6]== 等 litellm 容器把 auth.json 写到 PVC"
for i in $(seq 1 30); do
  if ssh $SSH_188 "test -s /Data/chatgpt-auth/$ACCT/auth.json"; then
    echo "  ✅ auth.json 已落盘"
    break
  fi
  sleep 2
done
ssh $SSH_188 "test -s /Data/chatgpt-auth/$ACCT/auth.json" || {
  echo "ERROR: 60s 内 auth.json 未生成"
  ssh $SSH_188 "docker logs litellm-onboard-tmp --tail 30"
  exit 1
}

# ---- 5/6: 验证 token 有效（plan_type=pro）----
echo "==[5/6]== 验证 token 有效（plan_type=pro）"
ssh $SSH_188 "cat /Data/chatgpt-auth/$ACCT/auth.json" | python3 -c '
import json, base64, urllib.request, sys
auth = json.load(sys.stdin)
tok = auth["access_token"]
aid = auth.get("account_id") or json.loads(base64.urlsafe_b64decode(
    auth["id_token"].split(".")[1] + "=="))["https://api.openai.com/auth"]["chatgpt_account_id"]
req = urllib.request.Request("https://chatgpt.com/backend-api/codex/usage",
    headers={"Authorization": f"Bearer {tok}", "chatgpt-account-id": aid,
             "Originator": "codex_cli_rs",
             "User-Agent": "codex_cli_rs/0.30.0 (Linux; x86_64)"})
u = json.loads(urllib.request.urlopen(req, timeout=15).read())
assert u["plan_type"] == "pro", f"plan_type={u[\"plan_type\"]}"
print(f"  ✅ plan=pro 5h={u[\"rate_limit\"][\"primary_window\"][\"used_percent\"]}%")
'

# ---- 6/6: 接管现有 add-chatgpt-account.sh 流程 ----
echo "==[6/6]== 调用 add-chatgpt-account.sh 完成 198 注册"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
"$SCRIPT_DIR/add-chatgpt-account.sh" "$ACCT" "/dev/null"  # auth.json 已在 188 上，第 2 参数会被 add 脚本忽略

echo ""
echo "🎉 $ACCT OAuth 自动绑定 + 198 注册完成（无人工介入）"
