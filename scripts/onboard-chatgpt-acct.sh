#!/bin/bash
# onboard-chatgpt-acct.sh — 把 1 个新 chatgpt 订阅账号端到端接入 198 K3s prod 池
#
# 用法:
#   ./onboard-chatgpt-acct.sh <N> <auth.json path>     # 跑全套
#   ./onboard-chatgpt-acct.sh <N> <auth.json> --from STEP   # 从指定 step 续跑
#   STEP: apply | wait | cp-auth | models | aclose | register | rollout | smoke | pool-state
#
# 前提:
#   1. k8s/chatgpt-acct-N.yaml 已存在（apply step 会用）— 否则手工把 chatgpt-acct-26-33.yaml 拆出来再跑
#   2. auth.json 在本机 path，OAuth 已跑通（access_token + refresh_token + id_token 齐全）
#   3. 198 master key 在脚本里硬编码（公开 cc.auto-link.com.cn/pro 的 sk-pro 不是机密）
#
# 幂等:
#   - apply 用 kubectl apply 天然幂等
#   - cp-auth 跑前先 docker stop 188 (如果有)
#   - models 用 delete+recreate（[[litellm_model_update_400_bug]]）
#   - pool-state 用 idempotent python script，重复跑无害

set -euo pipefail

ACCT="${1:-}"
AUTH_FILE="${2:-}"
FROM_STEP="apply"
if [ "${3:-}" = "--from" ]; then FROM_STEP="${4:-apply}"; fi

if [ -z "$ACCT" ] || [ -z "$AUTH_FILE" ]; then
  echo "usage: $0 <N> <auth.json> [--from STEP]" >&2
  exit 1
fi

if [ ! -f "$AUTH_FILE" ]; then
  echo "FATAL: auth.json not found: $AUTH_FILE" >&2
  exit 1
fi

# 强校验 auth.json 含三件套（防止上传错文件）
python3 -c "
import json, sys
d = json.load(open('$AUTH_FILE'))
tokens = d.get('tokens', d)
for k in ('access_token', 'refresh_token'):
    if not tokens.get(k):
        print(f'FATAL: auth.json missing tokens.{k}', file=sys.stderr)
        sys.exit(2)
print('auth.json ok: access_token + refresh_token 齐全')
"

NS=litellm-product
DEPLOY=chatgpt-acct-$ACCT
SVC_DNS="http://chatgpt-acct-$ACCT.litellm-product.svc.cluster.local:4000"
POOL_KEY=sk-chatgpt-198-d8a3f4e62b9c1057ef324918a7b6d3e0
PROD_MK=sk-pro-litellm-ce077e2b0721bb419a633e4d
PROD_ENDPOINT=https://cc.auto-link.com.cn/pro
SKILLS_DIR="$HOME/.claude/skills"
PATCH_ACLOSE="$SKILLS_DIR/chatgpt-acct-close-wait-restart/scripts/patch-aclose-198.sh"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
K8S_FILE="$REPO_DIR/k8s/chatgpt-acct-$ACCT.yaml"
# bundled 文件兜底（acct-26..33 都在同一文件里）
BUNDLED_FILE="$REPO_DIR/k8s/chatgpt-acct-26-33.yaml"

upstream_for() {
  case "$1" in
    gpt-5.5)       echo openai/chatgpt-gpt-5.5 ;;
    gpt-5.4)       echo openai/chatgpt-gpt-5.4 ;;
    gpt-5.3-codex) echo openai/chatgpt-gpt-5.3-codex-spark ;;
    *) echo "unknown short=$1" >&2; return 1 ;;
  esac
}

step_order=(apply wait cp-auth models aclose register rollout smoke pool-state)
should_run() {
  local step=$1
  local started=0
  for s in "${step_order[@]}"; do
    [ "$s" = "$FROM_STEP" ] && started=1
    [ "$s" = "$step" ] && { [ $started -eq 1 ] && return 0 || return 1; }
  done
  return 1
}

log() { echo "[acct-$ACCT $(date +%H:%M:%S)] $*"; }

# ────────────────────────────────────────────────────────────
# Step 1: apply manifest
# ────────────────────────────────────────────────────────────
if should_run apply; then
  log "STEP apply"
  if [ -f "$K8S_FILE" ]; then
    log "  using $K8S_FILE"
    jms ssh AIYJY-litellm "kubectl apply -f -" < "$K8S_FILE"
  elif [ -f "$BUNDLED_FILE" ]; then
    log "  using bundled $BUNDLED_FILE (会一并 apply 26-33 同集合，幂等)"
    jms ssh AIYJY-litellm "kubectl apply -f -" < "$BUNDLED_FILE"
  else
    echo "FATAL: neither $K8S_FILE nor $BUNDLED_FILE exists" >&2
    exit 1
  fi
fi

# ────────────────────────────────────────────────────────────
# Step 2: wait Pod ready (但 OAuth pending 时不会 Ready，超时即继续)
# ────────────────────────────────────────────────────────────
if should_run wait; then
  log "STEP wait"
  jms ssh AIYJY-litellm "kubectl -n $NS wait pod -l app=$DEPLOY --for=condition=Ready --timeout=120s" || \
    log "  ⚠️  Pod not Ready in 120s (PVC 空 → 首次 OAuth flow hang 是正常的，cp-auth 后会 ready)"
fi

# ────────────────────────────────────────────────────────────
# Step 3: cp auth.json + rollout restart
# ────────────────────────────────────────────────────────────
if should_run cp-auth; then
  log "STEP cp-auth"
  # mac bash 3.2 + jms scp 文件路径限制，用 stdin 传
  cat "$AUTH_FILE" | jms scp - AIYJY-litellm:/tmp/auth-acct-$ACCT.json
  LOCAL_MD5=$(md5sum "$AUTH_FILE" | awk '{print $1}')
  REMOTE_MD5=$(jms ssh AIYJY-litellm "md5sum /tmp/auth-acct-$ACCT.json | awk '{print \$1}'")
  if [ "$LOCAL_MD5" != "$REMOTE_MD5" ]; then
    echo "FATAL: scp md5 mismatch: $LOCAL_MD5 != $REMOTE_MD5" >&2
    exit 3
  fi
  log "  md5 ok: $LOCAL_MD5"

  jms ssh AIYJY-litellm "
    POD=\$(kubectl -n $NS get pod -l app=$DEPLOY -o jsonpath='{.items[0].metadata.name}')
    [ -z \"\$POD\" ] && { echo 'FATAL: no pod for $DEPLOY' >&2; exit 1; }
    kubectl -n $NS cp /tmp/auth-acct-$ACCT.json \$POD:/chatgpt-auth/auth.json
    # 校验 in-pod md5
    POD_MD5=\$(kubectl -n $NS exec \$POD -- md5sum /chatgpt-auth/auth.json | awk '{print \$1}')
    [ \"\$POD_MD5\" != \"$LOCAL_MD5\" ] && { echo \"FATAL: pod md5 mismatch \$POD_MD5\" >&2; exit 1; }
    echo \"  in-pod md5 ok\"
    rm /tmp/auth-acct-$ACCT.json
    kubectl -n $NS rollout restart deploy/$DEPLOY
    kubectl -n $NS rollout status deploy/$DEPLOY --timeout=180s
  "
fi

# ────────────────────────────────────────────────────────────
# Step 4: in-pod /v1/models 自测应返 7 条
# ────────────────────────────────────────────────────────────
if should_run models; then
  log "STEP models (in-pod /v1/models)"
  jms ssh AIYJY-litellm "
    POD=\$(kubectl -n $NS get pod -l app=$DEPLOY -o jsonpath='{.items[0].metadata.name}')
    N=\$(kubectl -n $NS exec \$POD -- python3 -c '
import urllib.request, json
req = urllib.request.Request(\"http://localhost:4000/v1/models\", headers={\"Authorization\":\"Bearer $POOL_KEY\"})
r = urllib.request.urlopen(req, timeout=10)
print(len(json.load(r)[\"data\"]))
')
    echo \"  /v1/models returned \$N entries (expect 7)\"
    [ \"\$N\" = \"7\" ] || { echo 'FATAL: expected 7 models' >&2; exit 4; }
  "
fi

# ────────────────────────────────────────────────────────────
# Step 5: aclose patch (responses_aclose monkey-patch)
# ────────────────────────────────────────────────────────────
if should_run aclose; then
  log "STEP aclose"
  # patch-aclose-198.sh 直接 kubectl，必须在 198 host 上跑；198 上已有 /root/patch-aclose-198.sh
  jms ssh AIYJY-litellm "bash /root/patch-aclose-198.sh --apply --only $ACCT"
  # verify
  jms ssh AIYJY-litellm "
    POD=\$(kubectl -n $NS get pod -l app=$DEPLOY -o jsonpath='{.items[0].metadata.name}')
    OK=\$(kubectl -n $NS exec \$POD -- python3 -c '
from litellm.responses.streaming_iterator import ResponsesAPIStreamingIterator as C
print(\"yes\" if hasattr(C, \"_carher_aclose_patched\") else \"no\")
')
    echo \"  aclose patched: \$OK\"
    [ \"\$OK\" = \"yes\" ] || { echo 'FATAL: aclose patch missing' >&2; exit 5; }
  "
fi

# ────────────────────────────────────────────────────────────
# Step 6: register 3 model entries to prod LiteLLM (delete+recreate)
# ────────────────────────────────────────────────────────────
if should_run register; then
  log "STEP register"
  for SHORT in gpt-5.5 gpt-5.4 gpt-5.3-codex; do
    MID="chatgpt-acct-$ACCT-$SHORT"
    REAL=$(upstream_for "$SHORT")
    MNAME="chatgpt-$SHORT"
    log "  $MID -> $REAL via $SVC_DNS"
    curl -sS -X POST "$PROD_ENDPOINT/model/delete" \
      -H "Authorization: Bearer $PROD_MK" -H "Content-Type: application/json" \
      -d "{\"id\":\"$MID\"}" -o /dev/null || true
    HTTP=$(curl -sS -o /tmp/onboard-reg-$$.json -w '%{http_code}' \
      -X POST "$PROD_ENDPOINT/model/new" \
      -H "Authorization: Bearer $PROD_MK" -H "Content-Type: application/json" \
      -d "{
        \"model_name\":\"$MNAME\",
        \"litellm_params\":{\"model\":\"$REAL\",\"api_base\":\"$SVC_DNS\",\"api_key\":\"$POOL_KEY\"},
        \"model_info\":{\"id\":\"$MID\",\"mode\":\"responses\"}
      }")
    if [ "$HTTP" != "200" ]; then
      echo "FATAL: /model/new failed HTTP=$HTTP" >&2
      cat /tmp/onboard-reg-$$.json >&2
      rm -f /tmp/onboard-reg-$$.json
      exit 6
    fi
  done
  rm -f /tmp/onboard-reg-$$.json
fi

# ────────────────────────────────────────────────────────────
# Step 7: rollout restart litellm-proxy (router 重读 DB)
# ────────────────────────────────────────────────────────────
if should_run rollout; then
  log "STEP rollout (litellm-proxy)"
  jms ssh AIYJY-litellm "
    kubectl -n $NS rollout restart deploy/litellm-proxy
    kubectl -n $NS rollout status deploy/litellm-proxy --timeout=180s
  "
fi

# ────────────────────────────────────────────────────────────
# Step 8: force-route smoke (绕 sticky 验证 api_base)
# ────────────────────────────────────────────────────────────
if should_run smoke; then
  log "STEP smoke"
  RESP=$(mktemp)
  curl -sS -i -m 30 "$PROD_ENDPOINT/v1/chat/completions" \
    -H "Authorization: Bearer $PROD_MK" -H "Content-Type: application/json" \
    -d "{\"model\":\"chatgpt-acct-$ACCT-gpt-5.5\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"stream\":true,\"max_tokens\":5}" \
    > "$RESP" || true
  log "  response headers:"
  grep -iE 'x-litellm-(model-api-base|attempted-fallbacks|model-id)' "$RESP" | sed 's/^/    /'
  if grep -q "chatgpt-acct-$ACCT.litellm-product.svc" "$RESP"; then
    log "  ✅ api_base matches svc DNS"
  else
    log "  ⚠️  api_base 没有命中预期 svc DNS，可能 sticky 缓存中（10min 后再试）"
  fi
  rm -f "$RESP"
fi

# ────────────────────────────────────────────────────────────
# Step 9: 更新 188 quota-rebalance.py 的 POOL_ACCOUNTS + state.json
# ────────────────────────────────────────────────────────────
if should_run pool-state; then
  log "STEP pool-state"
  PORT=$((4000 + ACCT))
  python3 "$REPO_DIR/scripts/chatgpt-pool-account-add.py" "$ACCT" "$PORT"
fi

log "✅ acct-$ACCT onboard complete"
