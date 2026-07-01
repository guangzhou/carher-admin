#!/bin/bash
# oauth-fetch-acct.sh — 拿 1 个新 ChatGPT 订阅的 auth.json（device-code 人工辅助）
#
# 用法:
#   ./oauth-fetch-acct.sh <N>                  # 拿 acct-N 的 auth.json → /tmp/auth-acct-N.json
#   ./oauth-fetch-acct.sh <N> /custom/path     # 指定输出路径
#
# 为啥这条路径:
#   - Mac 直连 auth.openai.com → 403 unsupported_country_region (CN geo-block)
#   - 188 docker plain urllib → CF 429 "Just a moment" (bot fingerprint 升级 2026-06)
#   - 198 host plain urllib + Originator:codex_cli_rs → 200 OK (实测 2026-06-15)
#
# 流程:
#   1. scp chatgpt-device-manual.py 到 198:/tmp
#   2. 在 198 后台 setsid 起 poll，写 /tmp/oauth-acct-N.log
#   3. tail 等到 USER_CODE 行出现 → 打印给用户
#   4. 提示用户浏览器完成，回车继续
#   5. 后台 poll 拿到 authorization_code → auth.json
#   6. scp 拉回本地 + md5 校验 + 清理 198 临时文件

set -euo pipefail
ACCT="${1:?usage: $0 <N> [out_path]}"
OUT="${2:-/tmp/auth-acct-${ACCT}.json}"
LOG_198=/tmp/oauth-acct-${ACCT}.log
SCRIPT_198=/tmp/chatgpt-device-manual.py
SCRIPT_SRC="$(cd "$(dirname "$0")/.." && pwd)/scripts/chatgpt-onboard/chatgpt-device-manual.py"
POLL_MIN=15

[ -f "$SCRIPT_SRC" ] || { echo "FATAL: device-manual script not found: $SCRIPT_SRC" >&2; exit 1; }

echo "==[1/6]== 复制 device-code 脚本到 198 (ssh+tee, 绕 jms scp 189 拒)"
LOCAL_MD5=$(md5sum "$SCRIPT_SRC" | awk '{print $1}')
cat "$SCRIPT_SRC" | jms ssh AIYJY-litellm "cat > $SCRIPT_198"
REMOTE_MD5=$(jms ssh AIYJY-litellm "md5sum $SCRIPT_198 | awk '{print \$1}'")
if [ "$LOCAL_MD5" != "$REMOTE_MD5" ]; then
  echo "FATAL: device-manual upload md5 mismatch $LOCAL_MD5 vs $REMOTE_MD5" >&2
  exit 5
fi
echo "  md5 ok: $LOCAL_MD5"

echo "==[2/6]== 198 上后台起 poll（${POLL_MIN}min 窗口）"
# jms ssh 多行 heredoc 经常被折成单行导致语句错乱；用单行 ; 分隔，确保 setsid 一定执行
SPAWN_CMD="pkill -f chatgpt-device-manual.py 2>/dev/null; sleep 1; rm -f $LOG_198 /tmp/auth-acct-${ACCT}.json; touch $LOG_198; nohup setsid bash -c 'AUTH_JSON_OUTPUT=/tmp/auth-acct-${ACCT}.json POLL_MINUTES=${POLL_MIN} python3 ${SCRIPT_198}' < /dev/null > $LOG_198 2>&1 & sleep 5; pgrep -af chatgpt-device-manual.py; wc -c $LOG_198"
jms ssh AIYJY-litellm "$SPAWN_CMD" || true

echo "==[3/6]== 等 user_code 出现（最多 30s）"
for i in $(seq 1 15); do
  CODE_LINE=$(jms ssh AIYJY-litellm "grep 'USER_CODE' $LOG_198 2>/dev/null | head -1" || true)
  if [ -n "$CODE_LINE" ]; then break; fi
  sleep 2
done
if [ -z "$CODE_LINE" ]; then
  echo "FATAL: 30s 内没拿到 user_code，看 198:$LOG_198" >&2
  jms ssh AIYJY-litellm "tail -20 $LOG_198" >&2
  exit 2
fi

USER_CODE=$(echo "$CODE_LINE" | awk -F': ' '{print $2}' | tr -d ' ')
echo
echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║  acct-${ACCT}: 在你的浏览器里完成"
echo "╠═══════════════════════════════════════════════════════════════╣"
echo "║  URL:       https://auth.openai.com/codex/device"
echo "║  USER_CODE: $USER_CODE"
echo "╚═══════════════════════════════════════════════════════════════╝"
echo "  → 登录 auth.openai.com → 输入 code → Authorize"
echo "  ⚠️  Authorize 完成后不要去 chatgpt.com 登入验证账号!"
echo "       web 端登入会立即 revoke 刚拿到的 OAuth token"
echo "       (token_invalidated 故障模式, 必须重新 OAuth)"
echo "  → 我会持续 poll 198，拿到 token 自动继续"

echo
echo "==[4/6]== poll 198 等 auth.json（最多 ${POLL_MIN}min）"
DEADLINE=$(( $(date +%s) + POLL_MIN * 60 ))
while [ $(date +%s) -lt $DEADLINE ]; do
  STATE=$(jms ssh AIYJY-litellm "
    if [ -f /tmp/auth-acct-${ACCT}.json ]; then
      echo READY
    elif grep -q 'authorization_code' $LOG_198 2>/dev/null; then
      echo CODE_GOT
    elif grep -q 'access_token' $LOG_198 2>/dev/null; then
      echo TOKEN_GOT
    else
      grep -oE 'attempt [0-9]+' $LOG_198 2>/dev/null | tail -1 || echo PENDING
    fi
  " 2>/dev/null || echo "")
  case "$STATE" in
    READY) echo "  ✅ auth.json ready"; break ;;
    *) echo "  $(date +%H:%M:%S) state=$STATE"; sleep 10 ;;
  esac
done

if [ "$STATE" != "READY" ]; then
  echo "FATAL: ${POLL_MIN}min 超时，最后 20 行日志：" >&2
  jms ssh AIYJY-litellm "tail -20 $LOG_198" >&2
  exit 3
fi

echo "==[5/6]== 拉回 Mac + md5 双向校验"
jms ssh AIYJY-litellm "cat /tmp/auth-acct-${ACCT}.json" > "$OUT"
LOCAL_MD5=$(md5sum "$OUT" | awk '{print $1}')
REMOTE_MD5=$(jms ssh AIYJY-litellm "md5sum /tmp/auth-acct-${ACCT}.json | awk '{print \$1}'")
if [ "$LOCAL_MD5" != "$REMOTE_MD5" ]; then
  echo "FATAL: scp md5 mismatch: $LOCAL_MD5 vs $REMOTE_MD5" >&2
  exit 4
fi
echo "  md5 ok: $LOCAL_MD5"

echo "==[6/6]== 198 清理 + 摘要"
jms ssh AIYJY-litellm "rm -f $SCRIPT_198 $LOG_198 /tmp/auth-acct-${ACCT}.json"

python3 -c "
import json, datetime
d = json.load(open('$OUT'))
exp = d.get('expires_at', 0)
print('  account_id :', d.get('account_id', '?'))
print('  expires_at :', exp, '('+str(datetime.datetime.fromtimestamp(exp))+')')
print('  token_len  :', len(d.get('access_token', '')))
"

echo
echo "✅ acct-${ACCT} auth.json → $OUT"
echo "   下一步: ~/codes/carher-admin/scripts/onboard-chatgpt-acct.sh ${ACCT} $OUT"
