#!/usr/bin/env bash
# Shared bits for 198 pro chatgpt vip-pool ops.
#
# All mutations via:
#   - SQL UPDATE on LiteLLM_ProxyModelTable / LiteLLM_VerificationToken
#   - kubectl patch/get on litellm-config ConfigMap
#   - kubectl rollout restart deployment/litellm-proxy
#   - Redis FLUSHDB (cache invalidate for key.aliases)
#
# Smoke calls via 198 hop (jms JSZX-AI-03 -> ssh 198 -> curl 127.0.0.1:30402).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
JMS="$ROOT/scripts/jms"
HOST="AIYJY-litellm"
NS="litellm-product"
CM_NAME="litellm-config"
DEPLOY="litellm-proxy"
DB_POD="litellm-db-0"
REDIS_POD="litellm-redis-0"

# 198 hop for smoke
JUMP_198="cltx@10.68.13.198"
PROXY_URL_INTERNAL="http://10.68.13.198:30402/pro"
MASTER_KEY="sk-pro-litellm-ce077e2b0721bb419a633e4d"

SHORT_NAMES=(gpt-5.5 gpt-5.4 gpt-5.3-codex)

# Map: chatgpt-* alias short_name → which vip short to redirect to
# (cursor keys may carry these names; revoke removes only these.)
declare_aliases_for_group() {
  local group="$1"
  ALIAS_PAIRS=(
    "gpt-5.5"                       "chatgpt-vip-${group}-gpt-5.5"
    "chatgpt-gpt-5.5"               "chatgpt-vip-${group}-gpt-5.5"
    "gpt-5.4"                       "chatgpt-vip-${group}-gpt-5.4"
    "chatgpt-gpt-5.4"               "chatgpt-vip-${group}-gpt-5.4"
    "gpt-5.2"                       "chatgpt-vip-${group}-gpt-5.4"
    "gpt-5.4-mini"                  "chatgpt-vip-${group}-gpt-5.4"
    "gpt-5.3-codex"                 "chatgpt-vip-${group}-gpt-5.3-codex"
    "chatgpt-gpt-5.3-codex"         "chatgpt-vip-${group}-gpt-5.3-codex"
    "chatgpt-gpt-5.3-codex-spark"   "chatgpt-vip-${group}-gpt-5.3-codex"
  )
}

ALIAS_KEYS_TO_STRIP=(
  "gpt-5.5" "chatgpt-gpt-5.5"
  "gpt-5.4" "chatgpt-gpt-5.4" "gpt-5.2" "gpt-5.4-mini"
  "gpt-5.3-codex" "chatgpt-gpt-5.3-codex" "chatgpt-gpt-5.3-codex-spark"
)

# Fallback chain per short
declare_fallback_for_group() {
  local group="$1"
  FALLBACK_PAIRS=(
    "chatgpt-vip-${group}-gpt-5.5"        "chatgpt-gpt-5.5,wangsu-gpt-5.5"
    "chatgpt-vip-${group}-gpt-5.4"        "chatgpt-gpt-5.4,wangsu-gpt-5.4"
    "chatgpt-vip-${group}-gpt-5.3-codex"  "chatgpt-gpt-5.3-codex,wangsu7-gpt-5.3-codex"
  )
}

psql_stdin() {
  "$JMS" ssh "$HOST" "kubectl exec -i -n $NS $DB_POD -- psql -U litellm -d litellm -P pager=off -v ON_ERROR_STOP=1 -t -A -F '|'"
}

psql_cmd() {
  printf '%s\n' "$1" | psql_stdin
}

cm_get_yaml() {
  "$JMS" ssh "$HOST" "kubectl get cm $CM_NAME -n $NS -o yaml"
}

cm_apply_yaml_stdin() {
  # Read YAML from stdin and apply.
  "$JMS" ssh "$HOST" "kubectl apply -f - -n $NS"
}

rollout_restart() {
  "$JMS" ssh "$HOST" "kubectl rollout restart deploy/$DEPLOY -n $NS"
  "$JMS" ssh "$HOST" "kubectl rollout status deploy/$DEPLOY -n $NS --timeout=180s"
}

redis_flushdb() {
  "$JMS" ssh "$HOST" "kubectl exec -n $NS $REDIS_POD -- redis-cli FLUSHDB"
}

# Smoke via 198 hop, returns response body (stderr=headers if -i set).
proxy_curl() {
  local method="$1" path="$2" key="$3" body="${4:-}"
  local q="curl -fsS -X $method '$PROXY_URL_INTERNAL$path' \
    -H 'Authorization: Bearer $key' -H 'Content-Type: application/json' -i"
  if [[ -n "$body" ]]; then
    "$JMS" ssh JSZX-AI-03 "ssh -o StrictHostKeyChecking=no $JUMP_198 \"$q --data-binary @-\"" <<<"$body"
  else
    "$JMS" ssh JSZX-AI-03 "ssh -o StrictHostKeyChecking=no $JUMP_198 \"$q\""
  fi
}

require_group() {
  [[ "$1" =~ ^[a-z][a-z0-9_-]{1,30}$ ]] \
    || { echo "ERR: <group> must be ^[a-z][a-z0-9_-]{1,30}$ (got: $1)" >&2; exit 2; }
}

require_acct() {
  [[ "$1" =~ ^acct-[0-9]+$ ]] \
    || { echo "ERR: <acct-id> must look like acct-N (got: $1)" >&2; exit 2; }
}
