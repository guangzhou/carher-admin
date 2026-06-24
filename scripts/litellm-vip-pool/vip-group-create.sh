#!/usr/bin/env bash
# vip-group-create.sh <group> <acct-id>
#
# Carve out one chatgpt-acct from the shared pool and dedicate it to <group>.
# Idempotent: re-running on an already-carved acct is a no-op (UPDATE matches 0 rows).
#
# Steps:
#   1. SQL: rename 3 entries chatgpt-acct-<N>-gpt-5.X model_name -> chatgpt-vip-<group>-gpt-5.X
#   2. CM patch: append 3 fallback entries (vip -> main pool -> wangsu)
#   3. kubectl rollout restart litellm-proxy (CM has no reloader sidecar)
#   4. Smoke: master-key probe each vip model_name returns 200 with vip model_id
#
# Reverse: vip-group-destroy.sh <group>
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/lib.sh"

[[ $# -eq 2 ]] || { echo "usage: $0 <group> <acct-id>" >&2; exit 2; }
GROUP="$1"; ACCT="$2"
require_group "$GROUP"
require_acct "$ACCT"
N="${ACCT#acct-}"

echo "== vip-group-create: group=$GROUP acct=$ACCT =="

# Pre-check: 3 entries must exist for this acct, none already vip.
PRE=$(psql_cmd "SELECT model_id, model_name FROM \"LiteLLM_ProxyModelTable\" WHERE model_id LIKE 'chatgpt-acct-$N-%';")
COUNT=$(printf '%s\n' "$PRE" | grep -c . || true)
if [[ "$COUNT" -ne 3 ]]; then
  echo "ERR: expected 3 entries for $ACCT, found $COUNT:" >&2
  printf '%s\n' "$PRE" >&2
  exit 1
fi
if printf '%s\n' "$PRE" | grep -q 'chatgpt-vip-'; then
  echo "ERR: acct $ACCT already carved into a vip group:" >&2
  printf '%s\n' "$PRE" >&2
  exit 1
fi

echo "→ step 1/4 SQL: rename 3 entries"
for short in "${SHORT_NAMES[@]}"; do
  vip="chatgpt-vip-${GROUP}-${short}"
  psql_cmd "UPDATE \"LiteLLM_ProxyModelTable\" SET model_name='$vip' WHERE model_id='chatgpt-acct-${N}-${short}' AND model_name='chatgpt-${short}';"
done

echo "→ step 2/4 ConfigMap: append 3 fallbacks"
TMP_CM="$(mktemp)"; trap 'rm -f "$TMP_CM"' EXIT
cm_get_yaml > "$TMP_CM"
python3 "$DIR/cm_patch_fallback.py" "$TMP_CM" "$GROUP" add
cm_apply_yaml_stdin < "$TMP_CM"

echo "→ step 3/4 rollout restart litellm-proxy"
rollout_restart

echo "→ step 4/4 smoke (route-only, 4xx body OK as long as model_id header matches)"
for short in "${SHORT_NAMES[@]}"; do
  vip="chatgpt-vip-${GROUP}-${short}"
  echo "-- probe $vip"
  body=$(printf '{"model":"%s","input":"ping","max_output_tokens":16}' "$vip")
  resp=$(proxy_curl POST /v1/responses "$MASTER_KEY" "$body" 2>&1 | tr -d '\r' || true)
  model_id=$(printf '%s\n' "$resp" | grep -oE 'x-litellm-model-id: [^[:space:]]+' | head -1 | awk '{print $2}')
  if [[ "$model_id" == "chatgpt-acct-${N}-${short}" ]]; then
    echo "  OK route -> $model_id"
  else
    echo "  FAIL got model_id=$model_id (want chatgpt-acct-${N}-${short})" >&2
    printf '%s\n' "$resp" | head -25 >&2
    exit 1
  fi
done

echo "== DONE: vip-group=$GROUP backed by $ACCT (3 entries) =="
