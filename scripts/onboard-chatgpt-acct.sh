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
POOL_KEY="${LITELLM_POOL_KEY_198:?LITELLM_POOL_KEY_198 must be set}"
PROD_MK="${LITELLM_MK_198:?LITELLM_MK_198 must be set}"
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
  log "STEP wait (60s soft — auth.json 还没注入时不会 Ready, 这是预期)"
  jms ssh AIYJY-litellm "kubectl -n $NS wait pod -l app=$DEPLOY --for=condition=Ready --timeout=60s" 2>/dev/null || \
    log "  ⚠️  Pod not Ready in 60s (PVC 空 → 首次 OAuth flow hang 是预期，cp-auth 后会 ready)"
fi

# ────────────────────────────────────────────────────────────
# Step 3: cp auth.json + rollout restart
# ────────────────────────────────────────────────────────────
if should_run cp-auth; then
  log "STEP cp-auth (scale=0 → busybox attach PVC → scale=1)"
  # 关键：必须 scale=0 先把 pod 杀掉，否则 LiteLLM 启动时（PVC 还空）会写
  # 48-byte `{"device_code_requested_at": ...}` 占位到 auth.json，
  # 跟我们 cp 进去的真 token race，赢家是它（实证 2026-06-29 acct-74）。
  # 走 busybox 直挂 PVC 写文件，pod 完全不在 PVC mount 期间触碰 auth.json。
  cat "$AUTH_FILE" | jms scp - AIYJY-litellm:/tmp/auth-acct-$ACCT.json
  LOCAL_MD5=$(md5sum "$AUTH_FILE" | awk '{print $1}')
  REMOTE_MD5=$(jms ssh AIYJY-litellm "md5sum /tmp/auth-acct-$ACCT.json | awk '{print \$1}'")
  if [ "$LOCAL_MD5" != "$REMOTE_MD5" ]; then
    echo "FATAL: scp md5 mismatch: $LOCAL_MD5 != $REMOTE_MD5" >&2
    exit 3
  fi
  log "  md5 ok: $LOCAL_MD5"

  jms ssh AIYJY-litellm "
    set -e
    # 1) scale=0 等 pod 真消失
    kubectl -n $NS scale deploy/$DEPLOY --replicas=0
    for i in \$(seq 1 30); do
      N=\$(kubectl -n $NS get pod -l app=$DEPLOY --no-headers 2>/dev/null | wc -l)
      [ \"\$N\" = 0 ] && break
      sleep 2
    done
    [ \"\$N\" = 0 ] || { echo 'FATAL: pod did not terminate in 60s' >&2; exit 1; }

    # 2) busybox attach PVC, stdin → /a/auth.json, 校验 md5
    cat > /tmp/cp-overrides-$ACCT.json <<JSON
{
  \"spec\": {
    \"containers\": [{
      \"name\": \"cp\",
      \"image\": \"busybox\",
      \"stdin\": true,
      \"stdinOnce\": true,
      \"tty\": false,
      \"command\": [\"sh\",\"-c\",\"cat > /a/auth.json && md5sum /a/auth.json\"],
      \"volumeMounts\": [{\"name\": \"pvc\", \"mountPath\": \"/a\"}]
    }],
    \"volumes\": [{\"name\": \"pvc\", \"persistentVolumeClaim\": {\"claimName\": \"$DEPLOY-auth\"}}],
    \"restartPolicy\": \"Never\"
  }
}
JSON
    PVC_MD5=\$(kubectl -n $NS run cp-$DEPLOY --rm -i --restart=Never --image=busybox \
      --overrides=\"\$(cat /tmp/cp-overrides-$ACCT.json)\" \
      < /tmp/auth-acct-$ACCT.json 2>&1 | grep -E '^[a-f0-9]{32}' | awk '{print \$1}')
    rm -f /tmp/cp-overrides-$ACCT.json /tmp/auth-acct-$ACCT.json
    [ \"\$PVC_MD5\" != \"$LOCAL_MD5\" ] && { echo \"FATAL: PVC md5 mismatch \$PVC_MD5 != $LOCAL_MD5\" >&2; exit 1; }
    echo \"  ✅ PVC md5 ok: \$PVC_MD5\"

    # 3) scale=1 等 1/1 ready
    kubectl -n $NS scale deploy/$DEPLOY --replicas=1
    for i in \$(seq 1 60); do
      R=\$(kubectl -n $NS get deploy $DEPLOY -o jsonpath='{.status.readyReplicas}/{.spec.replicas}' 2>/dev/null)
      [ \"\$R\" = '1/1' ] && { echo \"  ✅ $DEPLOY ready (\$R)\"; break; }
      sleep 5
    done
    [ \"\$R\" = '1/1' ] || { echo \"FATAL: $DEPLOY not 1/1 after 300s (last=\$R)\" >&2; exit 7; }
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
  # patch-aclose-198.sh 末尾 `[ -n "$FAILED_LIST" ]` / `[ "$APPLY" = 0 ]` 在 OK 路径会返非零
  # 不杀 onboard，让下面 verify block 决断
  jms ssh AIYJY-litellm "bash /root/patch-aclose-198.sh --apply --only $ACCT" || true
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
    # 4-replica rolling update 平均 3-5min, --timeout=180s 高发误判 + jms 隧道 TLS flake;
    # 改 until-loop polling直到 4/4 ready (最多 600s)
    for i in \$(seq 1 120); do
      R=\$(kubectl -n $NS get deploy litellm-proxy -o jsonpath='{.status.readyReplicas}/{.spec.replicas}' 2>/dev/null)
      [ \"\$R\" = '4/4' ] && { echo \"  ✅ litellm-proxy 4/4 ready\"; break; }
      sleep 5
    done
    [ \"\$R\" = '4/4' ] || { echo \"FATAL: litellm-proxy not 4/4 after 600s (last=\$R)\" >&2; exit 8; }
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
  # grep 没匹配返非零 + set -e 会中断脚本，必须 || true 兜底
  grep -iE 'x-litellm-(model-api-base|attempted-fallbacks|model-id)' "$RESP" | sed 's/^/    /' || true
  if grep -q "chatgpt-acct-$ACCT.litellm-product.svc" "$RESP"; then
    log "  ✅ api_base matches svc DNS"
  else
    log "  ⚠️  api_base 没有命中预期 svc DNS，可能 sticky 缓存中（10min 后再试）"
    log "  ⚠️  也可能是上游 token_invalidated（chatgpt.com web 同账号登入会 revoke OAuth session）"
    log "       排查: kubectl exec pod -- python3 -c 'import urllib.request,json;tok=json.load(open(\"/chatgpt-auth/auth.json\"))[\"access_token\"];print(urllib.request.urlopen(urllib.request.Request(\"https://api.openai.com/v1/me\",headers={\"Authorization\":\"Bearer \"+tok}),timeout=10).status)'"
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
