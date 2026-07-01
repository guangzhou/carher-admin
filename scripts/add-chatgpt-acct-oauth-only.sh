#!/bin/bash
# add-chatgpt-acct-oauth-only.sh — 188 OAuth-only: 写 creds + toggle + 产 auth.json
#
# 用法:
#   ./add-chatgpt-acct-oauth-only.sh <N> <email> <mail_pw> <chatgpt_pw>
#
# 产物:
#   - 188:/Data/chatgpt-auth/acct-N/.creds
#   - 188:/Data/chatgpt-auth/acct-N/auth.json (toggle ENABLED + GEN_ONLY OAuth)
#   - 188:/tmp/auth-acct-N.json (供 aliyun-batch-add-accts.sh scp 拉)
#
# 完成后跑: ./scripts/aliyun-batch-add-accts.sh N1 N2 N3 ... 上阿里云 carher pool
#
# 不做: 198 K3s deploy / 198 prod LiteLLM 注册 / onboard-chatgpt-acct.sh 7 步
# 抠自 add-chatgpt-acct-198-full.sh 的 [2][4][5] 段 + set +e 包裹防 jms transient 杀脚本
#
# 端到端 ~6-9min/acct:
#   creds   ~5s
#   toggle  ~3min  (新 acct) / 即时 (toggle log 已 ENABLED)
#   OAuth   ~3-6min (session 复用走 /choose-an-account 30-60s, 否则 password+OTP ~3-6min)

set -eo pipefail

if [ $# -lt 4 ]; then
  cat >&2 <<USAGE
usage: $0 <N> <email> <mail_pw> <chatgpt_pw>

example:
  $0 69 yocwtcwvgkpke@mail.com 'demequ1eXd5d' 'udgknzj2fS!q'
USAGE
  exit 1
fi

N="$1"; EMAIL="$2"; MAIL_PW="$3"; CHATGPT_PW="$4"
ACCT="acct-$N"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPTS_DIR="$REPO_DIR/scripts"
ONBOARD_DIR="$SCRIPTS_DIR/chatgpt-onboard"

[[ "$N" =~ ^[0-9]+$ ]] || { echo "FATAL: N 必须数字, 实际: $N" >&2; exit 1; }

log() { printf "[%s %s] %s\n" "$ACCT" "$(date +%H:%M:%S)" "$*"; }

# ── 188 disk preflight ──────────────────────────────────────────────────
DISK_INFO=$(jms ssh JSZX-AI-03 'df --output=pcent,target /Data / 2>/dev/null | tail -n +2' 2>/dev/null || echo "")
DATA_USE=$(echo "$DISK_INFO" | awk '$2=="/Data"{gsub(/%/,"",$1); print $1}')
ROOT_USE=$(echo "$DISK_INFO" | awk '$2=="/"{gsub(/%/,"",$1); print $1}')
if [ "${DATA_USE:-0}" -ge 95 ]; then
  echo "FATAL: 188 /Data ${DATA_USE}% (≥95%)" >&2
  exit 1
fi
[ "${ROOT_USE:-0}" -ge 97 ] && log "⚠️  188 / ${ROOT_USE}% (软警告)"
log "188 /Data ${DATA_USE}% / ${ROOT_USE}% (ok)"

# ── 已有 auth.json (历史成功)? 跳过 ─────────────────────────────────────
if jms ssh JSZX-AI-03 "test -s /tmp/auth-$ACCT.json" 2>/dev/null; then
  AUTH_BYTES=$(jms ssh JSZX-AI-03 "wc -c < /tmp/auth-$ACCT.json" 2>/dev/null | tr -d '[:space:]')
  if [ "${AUTH_BYTES:-0}" -gt 1000 ]; then
    log "  ✅ 188:/tmp/auth-$ACCT.json 已存在 ($AUTH_BYTES bytes) — 跳过全部, OAuth 已就位"
    exit 0
  fi
fi

# ── [2] 188 写 .creds (单引号包裹防 shell 展开) ─────────────────────────
log "[2] 写 188:/Data/chatgpt-auth/$ACCT/.creds"
jms ssh JSZX-AI-03 "mkdir -p /Data/chatgpt-auth/$ACCT && cat > /Data/chatgpt-auth/$ACCT/.creds" <<CRED
email=$EMAIL
mail_pw='$MAIL_PW'
chatgpt_pw='$CHATGPT_PW'
CRED
jms ssh JSZX-AI-03 "chmod 600 /Data/chatgpt-auth/$ACCT/.creds && wc -l /Data/chatgpt-auth/$ACCT/.creds"

# ── [4] toggle Device code authorization (整段 set +e 包裹) ──────────────
log "[4] 启用 Device code authorization toggle"
set +e

LAUNCHER_LOCAL="$ONBOARD_DIR/run-enable-codex-toggle-188.sh"
SCRIPT_LOCAL="$ONBOARD_DIR/chatgpt-enable-codex-toggle.py"
[ -f "$LAUNCHER_LOCAL" ] || { echo "FATAL: 缺 $LAUNCHER_LOCAL" >&2; set -e; exit 5; }
[ -f "$SCRIPT_LOCAL" ] || { echo "FATAL: 缺 $SCRIPT_LOCAL" >&2; set -e; exit 5; }
cat "$LAUNCHER_LOCAL" | jms ssh JSZX-AI-03 "cat > /tmp/run-enable-codex-toggle-188.sh && chmod +x /tmp/run-enable-codex-toggle-188.sh" 2>&1 | head -3 || true
cat "$SCRIPT_LOCAL" | jms ssh JSZX-AI-03 "cat > /tmp/chatgpt-enable-codex-toggle.py" 2>&1 | head -3 || true

TOGGLE_LOG="/tmp/toggle-$ACCT.log"
EXISTING=$(jms ssh JSZX-AI-03 "sed -n 's/^$ACCT *[^ ]* *//p' $TOGGLE_LOG 2>/dev/null | head -1" 2>/dev/null | tr -d '[:space:]')
if [[ "$EXISTING" =~ ^(ENABLED|ALREADY_ENABLED)$ ]]; then
  log "  ✅ toggle 已完成 (existing log: $EXISTING) — 跳过 188 重跑"
else
  log "  启 188 toggle docker (bg)..."
  jms ssh JSZX-AI-03 "rm -f $TOGGLE_LOG && setsid bash -c 'bash /tmp/run-enable-codex-toggle-188.sh $ACCT 2>&1 | stdbuf -oL tee $TOGGLE_LOG' </dev/null >/dev/null 2>&1 & disown; sleep 2; echo started" 2>&1 | head -3
  TOGGLE_RC=$?
  log "  toggle bg ssh rc=$TOGGLE_RC (polling 阶段判定真伪)"
fi

# 等结果 (最多 12min: 新 acct 撞 mail.com OTP 全流程 4-8min, jms ssh hang 还要算上 2-3min)
log "  等 toggle 完成 (最多 12min)..."
DEADLINE=$(($(date +%s) + 720))
RESULT=""
ITER=0
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  ITER=$((ITER+1))
  RAW=$(jms ssh JSZX-AI-03 "sed -n 's/^$ACCT *[^ ]* *//p' $TOGGLE_LOG 2>/dev/null | head -1" 2>/dev/null)
  RC=$?
  RESULT=$(echo "$RAW" | tr -d '[:space:]')
  case "$RESULT" in
    ENABLED|ALREADY_ENABLED) log "  ✅ toggle: $RESULT (iter=$ITER, rc=$RC)"; break ;;
    FAILED_*|MISSING_CREDS|INVALID_ACCT|BAD_CREDS_FIELDS|ERROR*)
      log "  ❌ toggle: $RESULT (iter=$ITER)"
      jms ssh JSZX-AI-03 "tail -20 $TOGGLE_LOG" >&2 || true
      set -e; exit 6
      ;;
    *)
      if [ $((ITER % 6)) = 0 ]; then
        log "  ... polling iter=$ITER rc=$RC RESULT='${RESULT:0:40}'"
      fi
      sleep 10
      ;;
  esac
done
set -e
if [ -z "$RESULT" ] || ! [[ "$RESULT" =~ ^(ENABLED|ALREADY_ENABLED)$ ]]; then
  echo "FATAL: toggle 12min timeout (iter=$ITER, last RESULT='$RESULT')" >&2
  jms ssh JSZX-AI-03 "tail -30 $TOGGLE_LOG" >&2 || true
  exit 6
fi

# ── [5] OAuth GEN_ONLY=1 (整段 set +e 包裹) ─────────────────────────────
log "[5] OAuth (GEN_ONLY=1 patchright)"
set +e

OAUTH_LOG="/tmp/oauth-$ACCT.log"
log "  启 188 OAuth docker (bg)..."
jms ssh JSZX-AI-03 "rm -f $OAUTH_LOG /tmp/auth-$ACCT.json && setsid bash -c 'GEN_ONLY=1 bash /Data/chatgpt-auth/re-oauth.sh $ACCT 2>&1 | stdbuf -oL tee $OAUTH_LOG' </dev/null >/dev/null 2>&1 & disown; sleep 2; echo started" 2>&1 | head -3
OAUTH_RC=$?
log "  oauth bg ssh rc=$OAUTH_RC"

# 等 auth.json 落盘 (最多 8min)
log "  等 OAuth 完成 (最多 8min)..."
DEADLINE=$(($(date +%s) + 480))
HAVE_AUTH=0
ITER=0
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  ITER=$((ITER+1))
  if jms ssh JSZX-AI-03 "test -s /tmp/auth-$ACCT.json && grep -q '🎉.*GEN_ONLY done' $OAUTH_LOG 2>/dev/null"; then
    HAVE_AUTH=1; break
  fi
  if jms ssh JSZX-AI-03 "grep -qE '❌|account is on hold|consent Continue disabled' $OAUTH_LOG 2>/dev/null"; then
    log "  ❌ OAuth 早期失败 (iter=$ITER)"
    jms ssh JSZX-AI-03 "tail -50 $OAUTH_LOG" >&2 || true
    set -e; exit 7
  fi
  if [ $((ITER % 4)) = 0 ]; then
    log "  ... polling iter=$ITER"
  fi
  sleep 15
done
set -e
if [ "$HAVE_AUTH" = 0 ]; then
  echo "FATAL: OAuth 8min timeout (iter=$ITER)" >&2
  jms ssh JSZX-AI-03 "tail -50 $OAUTH_LOG" >&2 || true
  exit 7
fi
log "  ✅ 188:/tmp/auth-$ACCT.json 已产出"

# ── 同步副本到 /Data/chatgpt-auth/acct-N/auth.json (aliyun script 看这个路径) ──
jms ssh JSZX-AI-03 "cp /tmp/auth-$ACCT.json /Data/chatgpt-auth/$ACCT/auth.json && ls -la /Data/chatgpt-auth/$ACCT/auth.json"

log "🎉 $ACCT OAuth done (auth.json on 188 ready for aliyun-batch-add-accts.sh)"
