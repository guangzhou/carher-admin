#!/bin/bash
# add-chatgpt-acct-198-full.sh — 端到端把 1 个新 ChatGPT 订阅账号加进 198 K3s litellm-product pool
#
# 用法:
#   ./add-chatgpt-acct-198-full.sh <N> <email> <mail_pw> <chatgpt_pw>
#
#   <N>            acct 编号（如 44）。下一个可用编号自查:
#                    jms ssh AIYJY-litellm "kubectl -n litellm-product get deploy | grep chatgpt-acct"
#   <email>        ChatGPT 订阅邮箱，例：iheyvlrwfyiki@mail.com
#   <mail_pw>      mail.com webmail 密码（含特殊字符也别加单引号，脚本会写入 .creds 时单引号包裹）
#   <chatgpt_pw>   ChatGPT 账号密码
#
# 串起 4 个子动作 + fail-fast:
#   [1] 本地 sed manifest (anchored, 防 sed 把端口 4000→4N00)
#   [2] 188 上写 /Data/chatgpt-auth/acct-N/.creds (单引号包裹防 $ ! 展开)
#   [3] kubectl apply manifest
#   [4] 188 上跑 chatgpt-enable-codex-toggle.py 启用 Device code authorization
#       (新 acct 默认 toggle OFF, 不开则 OAuth consent Continue 是 disabled → ❌)
#   [5] 188 上跑 GEN_ONLY=1 re-oauth.sh 拿 auth.json
#   [6] kubectl cp auth.json → 跑 onboard-chatgpt-acct.sh 8 步
#
# 端到端时延 (acct-44 实测 2026-06-18):
#   sed+apply  ~30s
#   toggle     ~3min  (chatgpt.com login + Security toggle click)
#   OAuth      ~2min  (session 复用走 /choose-an-account, 0 OTP)
#   onboard    ~7min  (apply→wait→cp-auth→models→aclose→register→rollout→smoke→pool-state)
#   ───────
#   总计      ~13min/acct (串行批量 N 个 acct: ~13*N min)
#
# Idempotent: 每个 step 都可单独续跑。失败时按提示用 onboard-chatgpt-acct.sh --from STEP 续。

set -euo pipefail

if [ $# -lt 4 ]; then
  cat >&2 <<USAGE
usage: $0 <N> <email> <mail_pw> <chatgpt_pw>

example:
  $0 44 iheyvlrwfyiki@mail.com 'O@4kbi9qzunt' 'yqtt3.l7672F'

注意 mail_pw / chatgpt_pw 用单引号包裹避免 shell 展开 \$ ! 等字符。
USAGE
  exit 1
fi

N="$1"; EMAIL="$2"; MAIL_PW="$3"; CHATGPT_PW="$4"
ACCT="acct-$N"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
K8S_DIR="$REPO_DIR/k8s"
SCRIPTS_DIR="$REPO_DIR/scripts"
ONBOARD_DIR="$SCRIPTS_DIR/chatgpt-onboard"
NS=litellm-product

# 选最新已存在的 manifest 当模板（编号最大的，但小于当前 N）
# 不一定是 N-1: 可能跳号
choose_template() {
  local tpl=""
  for ((i=N-1; i>=26; i--)); do
    if [ -f "$K8S_DIR/chatgpt-acct-$i.yaml" ]; then
      echo "$K8S_DIR/chatgpt-acct-$i.yaml"
      return
    fi
  done
  # bundled file 兜底
  [ -f "$K8S_DIR/chatgpt-acct-26-33.yaml" ] && { echo "$K8S_DIR/chatgpt-acct-26-33.yaml"; return; }
  echo "" >&2; return 1
}

log() { printf "[%s %s] %s\n" "$ACCT" "$(date +%H:%M:%S)" "$*"; }

# ── 前置校验 ─────────────────────────────────────────────────────────────
[[ "$N" =~ ^[0-9]+$ ]] || { echo "FATAL: N 必须是数字, 实际: $N" >&2; exit 1; }
[ "$N" -ge 26 ] || { echo "FATAL: N 必须 ≥ 26 (1-25 是旧路径), 实际: $N" >&2; exit 1; }

# 188 disk preflight (踩坑 #25 v2 2026-06-25):
#   硬阻塞看 /Data (docker root + patchright 容器 + auth.json 副本真实写入处, 492G)
#   / (vda3 39G) 是 OS 稳态 88-94% 永远贴线但 onboard 不写 /, 改软警告 ≥97%
#   pool-state cp ... .bak-N-TS 落在 /home/cltx (=/, 36KB/份), 但 / 还有 4G+ free 时根本不是瓶颈
DISK_INFO=$(jms ssh JSZX-AI-03 'df --output=pcent,target /Data / 2>/dev/null | tail -n +2' 2>/dev/null || echo "")
DATA_USE=$(echo "$DISK_INFO" | awk '$2=="/Data"{gsub(/%/,"",$1); print $1}')
ROOT_USE=$(echo "$DISK_INFO" | awk '$2=="/"{gsub(/%/,"",$1); print $1}')
if [ "${DATA_USE:-0}" -ge 95 ]; then
  cat >&2 <<DISKFULL
FATAL: 188 /Data 使用率 ${DATA_USE}% (≥95% 阈值)

/Data 是 docker root + patchright 容器 layers + auth.json 副本真实写入处.
Onboard 真正的写入瓶颈在这里, 不是 / (vda3 39G OS 稳态).

排查 /Data:
  jms ssh JSZX-AI-03 'sudo du -h -d1 /Data 2>/dev/null | sort -h | tail -10'
  jms ssh JSZX-AI-03 'docker system df'                  # 看 dangling images / 退役容器

释放 (安全):
  jms ssh JSZX-AI-03 'docker container prune -f'         # 退役 patchright 容器
  jms ssh JSZX-AI-03 'docker image prune -a -f --filter "until=72h"'

详见 add-chatgpt-acct-198 SKILL §25
DISKFULL
  exit 1
fi
[ "${ROOT_USE:-0}" -ge 97 ] && log "⚠️  188 / ${ROOT_USE}% (软警告: OS 稳态 /var+/usr+/home, onboard 不真写 /, 可继续)"
log "188 /Data ${DATA_USE}% / ${ROOT_USE}% (<95% 阈值, ok)"

if jms ssh AIYJY-litellm "kubectl -n $NS get deploy chatgpt-acct-$N >/dev/null 2>&1"; then
  log "⚠️  chatgpt-acct-$N 已存在 — 跳过 manifest/apply, 直接续跑 OAuth + onboard"
  SKIP_APPLY=1
else
  SKIP_APPLY=0
fi

# ── [1] 本地 sed manifest (anchored) ─────────────────────────────────────
if [ "$SKIP_APPLY" = 0 ]; then
  TPL=$(choose_template) || { echo "FATAL: 找不到 chatgpt-acct-*.yaml 模板" >&2; exit 2; }
  TPL_N=$(basename "$TPL" .yaml | sed 's/^chatgpt-acct-//')
  OUT="$K8S_DIR/chatgpt-acct-$N.yaml"
  log "[1] sed manifest: $TPL_N → $N → $OUT"
  cp "$TPL" "$OUT"
  # CRITICAL: anchored — 只动 acct-X / "X" 字面量, 不动数字 X (防 X=40 时把 4000 撞成 4N00)
  sed -i '' "s/acct-$TPL_N/acct-$N/g; s/account: \"$TPL_N\"/account: \"$N\"/g" "$OUT"

  # 校验端口（必须 4000，不是 4N00）
  if ! grep -q "containerPort: 4000" "$OUT"; then
    echo "FATAL: manifest containerPort 不是 4000 (sed 撞了)" >&2
    grep -E "Port|port" "$OUT" >&2
    exit 3
  fi
  log "  ✅ manifest 端口 4000 正常, $(grep -c "acct-$N" "$OUT") 处 acct-$N 引用"
fi

# ── [2] 188 写 .creds (单引号包裹防 shell 展开) ─────────────────────────
log "[2] 写 188:/Data/chatgpt-auth/$ACCT/.creds"
# 单引号包裹必须用 cat <<EOF 而非 base64（base64 增加误码）
jms ssh JSZX-AI-03 "mkdir -p /Data/chatgpt-auth/$ACCT && cat > /Data/chatgpt-auth/$ACCT/.creds" <<CRED
email=$EMAIL
mail_pw='$MAIL_PW'
chatgpt_pw='$CHATGPT_PW'
CRED
jms ssh JSZX-AI-03 "chmod 600 /Data/chatgpt-auth/$ACCT/.creds && wc -l /Data/chatgpt-auth/$ACCT/.creds"

# ── [3] kubectl apply ───────────────────────────────────────────────────
if [ "$SKIP_APPLY" = 0 ]; then
  log "[3] kubectl apply manifest"
  cat "$K8S_DIR/chatgpt-acct-$N.yaml" | jms ssh AIYJY-litellm "kubectl apply -f -"
  # 校验 svc port=4000（再次防 sed 撞）
  SVC_PORT=$(jms ssh AIYJY-litellm "kubectl -n $NS get svc chatgpt-acct-$N -o jsonpath='{.spec.ports[0].port}'")
  [ "$SVC_PORT" = "4000" ] || { echo "FATAL: svc port=$SVC_PORT (应为 4000)" >&2; exit 4; }
  log "  ✅ svc port=4000 ok"
fi

# ── [4] 启用 Device code authorization toggle ──────────────────────────
log "[4] 启用 Device code authorization toggle (新 acct 默认 OFF, 不开 OAuth consent Continue disabled)"

# [4] 整段 set +e 包裹: jms ssh transient (Permission denied / channel close) 不杀脚本
# 真失败由 polling 段 RESULT 判定 / timeout / FAILED_* 显式 exit 6
set +e

# 同步 launcher + script 到 188 (确保最新, idempotent)
LAUNCHER_LOCAL="$ONBOARD_DIR/run-enable-codex-toggle-188.sh"
SCRIPT_LOCAL="$ONBOARD_DIR/chatgpt-enable-codex-toggle.py"
[ -f "$LAUNCHER_LOCAL" ] || { echo "FATAL: 缺 $LAUNCHER_LOCAL" >&2; set -e; exit 5; }
[ -f "$SCRIPT_LOCAL" ] || { echo "FATAL: 缺 $SCRIPT_LOCAL" >&2; set -e; exit 5; }
cat "$LAUNCHER_LOCAL" | jms ssh JSZX-AI-03 "cat > /tmp/run-enable-codex-toggle-188.sh && chmod +x /tmp/run-enable-codex-toggle-188.sh" 2>&1 | head -5 || true
cat "$SCRIPT_LOCAL" | jms ssh JSZX-AI-03 "cat > /tmp/chatgpt-enable-codex-toggle.py" 2>&1 | head -5 || true

# 后台跑 + tee 日志 (jms ssh 断也不死)
TOGGLE_LOG="/tmp/toggle-$ACCT.log"
# 注意: 若 toggle log 已有 ENABLED (上次 driver 死在 toggle 后但 188 toggle 实跑完), 不重跑
EXISTING=$(jms ssh JSZX-AI-03 "sed -n 's/^$ACCT *[^ ]* *//p' $TOGGLE_LOG 2>/dev/null | head -1" 2>/dev/null | tr -d '[:space:]')
if [[ "$EXISTING" =~ ^(ENABLED|ALREADY_ENABLED)$ ]]; then
  log "  ✅ toggle 已完成 (existing log: $EXISTING) — 跳过 188 重跑"
else
  jms ssh JSZX-AI-03 "rm -f $TOGGLE_LOG && setsid bash -c 'bash /tmp/run-enable-codex-toggle-188.sh $ACCT 2>&1 | stdbuf -oL tee $TOGGLE_LOG' </dev/null >/dev/null 2>&1 & disown; sleep 2; echo 'toggle bg started'" 2>&1 || log "  ⚠️  toggle bg ssh rc=$? (transient, polling 阶段判定真伪)"
fi

# 等结果（最多 6min: chatgpt.com login + mail OTP + Security 点 toggle）
log "  等 toggle 完成 (最多 6min)..."
DEADLINE=$(($(date +%s) + 360))
RESULT=""
ITER=0
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  ITER=$((ITER+1))
  # 容错: jms ssh transient 错误或 stdout 被污染时, 只认 schema 内的 RESULT 值
  RAW=$(jms ssh JSZX-AI-03 "sed -n 's/^$ACCT *[^ ]* *//p' $TOGGLE_LOG 2>/dev/null | head -1" 2>/dev/null)
  RC=$?
  RESULT=$(echo "$RAW" | tr -d '[:space:]')
  case "$RESULT" in
    ENABLED|ALREADY_ENABLED) log "  ✅ toggle result: $RESULT (iter=$ITER, rc=$RC)"; break ;;
    FAILED_*|MISSING_CREDS|INVALID_ACCT|BAD_CREDS_FIELDS)
      log "  ❌ toggle result: $RESULT (iter=$ITER)"
      jms ssh JSZX-AI-03 "tail -20 $TOGGLE_LOG" >&2 || true
      set -e
      exit 6
      ;;
    *)
      # 每 6 次 (~60s) 打一次心跳
      if [ $((ITER % 6)) = 0 ]; then
        log "  ... polling iter=$ITER rc=$RC RESULT='${RESULT:0:40}'"
      fi
      sleep 10
      ;;
  esac
done
set -e
if [ -z "$RESULT" ] || ! [[ "$RESULT" =~ ^(ENABLED|ALREADY_ENABLED)$ ]]; then
  echo "FATAL: toggle 6min timeout (iter=$ITER, last RESULT='$RESULT')" >&2
  jms ssh JSZX-AI-03 "tail -30 $TOGGLE_LOG" >&2 || true
  exit 6
fi

# ── [5] OAuth (GEN_ONLY=1 → 只产 auth.json) ─────────────────────────────
log "[5] OAuth (GEN_ONLY=1 patchright)"
OAUTH_LOG="/tmp/oauth-$ACCT.log"
OTP_PROV="${MAIL_OTP_PROVIDER:-mailcom}"
log "  MAIL_OTP_PROVIDER=$OTP_PROV (pass-through to re-oauth.sh)"
jms ssh JSZX-AI-03 "rm -f $OAUTH_LOG /tmp/auth-$ACCT.json && setsid bash -c 'MAIL_OTP_PROVIDER=$OTP_PROV GEN_ONLY=1 bash /Data/chatgpt-auth/re-oauth.sh $ACCT 2>&1 | stdbuf -oL tee $OAUTH_LOG' </dev/null >/dev/null 2>&1 & disown; sleep 2; echo 'oauth bg started'"

# 等 auth.json 落盘 (最多 8min)
log "  等 OAuth 完成 (最多 8min)..."
DEADLINE=$(($(date +%s) + 480))
HAVE_AUTH=0
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  if jms ssh JSZX-AI-03 "test -s /tmp/auth-$ACCT.json && grep -q '🎉.*GEN_ONLY done' $OAUTH_LOG 2>/dev/null"; then
    HAVE_AUTH=1; break
  fi
  # 早期 fail 信号
  if jms ssh JSZX-AI-03 "grep -q '❌\|account is on hold\|consent Continue disabled' $OAUTH_LOG 2>/dev/null"; then
    log "  ❌ OAuth 早期失败"
    jms ssh JSZX-AI-03 "tail -50 $OAUTH_LOG" >&2
    exit 7
  fi
  sleep 15
done
[ "$HAVE_AUTH" = 1 ] || { echo "FATAL: OAuth 8min timeout" >&2; jms ssh JSZX-AI-03 "tail -50 $OAUTH_LOG" >&2; exit 7; }
log "  ✅ auth.json 已产出"

# ── [6] 拉回本地 + 跑 onboard 8 步 ──────────────────────────────────────
LOCAL_AUTH="/tmp/auth-$ACCT.json"
jms ssh JSZX-AI-03 "cat /tmp/auth-$ACCT.json" > "$LOCAL_AUTH"
[ -s "$LOCAL_AUTH" ] || { echo "FATAL: 拉回 auth.json 为空" >&2; exit 8; }
log "  ✅ auth.json 拉回 $LOCAL_AUTH ($(wc -c < "$LOCAL_AUTH") bytes)"

log "[6] 跑 onboard-chatgpt-acct.sh 8 步"
"$SCRIPTS_DIR/onboard-chatgpt-acct.sh" "$N" "$LOCAL_AUTH"

log "🎉 $ACCT 端到端完成"
