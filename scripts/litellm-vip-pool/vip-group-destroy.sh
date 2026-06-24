#!/usr/bin/env bash
# vip-group-destroy.sh <group>
#
# Reverse of vip-group-create:
#   1. Refuse if any keys still have aliases pointing at this group
#   2. SQL: rename 3 vip entries back to chatgpt-gpt-5.X
#   3. CM patch: remove 3 fallback entries
#   4. rollout restart litellm-proxy
#
# Use this when deprecating a vip group; running keys' aliases should be revoked first.

set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/lib.sh"

[[ $# -eq 1 ]] || { echo "usage: $0 <group>" >&2; exit 2; }
GROUP="$1"
require_group "$GROUP"

echo "== vip-group-destroy: group=$GROUP =="

# Refuse if any key still references this group.
USERS=$(psql_cmd "SELECT key_alias FROM \"LiteLLM_VerificationToken\" WHERE aliases::text LIKE '%chatgpt-vip-${GROUP}-%';")
if [[ -n "$USERS" ]]; then
  echo "ERR: these keys still alias chatgpt-vip-$GROUP-*:" >&2
  printf '%s\n' "$USERS" >&2
  echo "Run vip-group-revoke first." >&2
  exit 1
fi

# Read entries, derive acct-N, sanity-check 3 entries match.
ENTRIES=$(psql_cmd "SELECT model_id, model_name FROM \"LiteLLM_ProxyModelTable\" WHERE model_name LIKE 'chatgpt-vip-${GROUP}-%' ORDER BY model_name;")
N=$(printf '%s\n' "$ENTRIES" | grep -c . || true)
if [[ "$N" -ne 3 ]]; then
  echo "ERR: expected 3 vip entries for $GROUP, found $N:" >&2
  printf '%s\n' "$ENTRIES" >&2
  exit 1
fi

echo "→ step 1/3 SQL: restore 3 entries to main pool"
for short in "${SHORT_NAMES[@]}"; do
  vip="chatgpt-vip-${GROUP}-${short}"
  main="chatgpt-${short}"
  psql_cmd "UPDATE \"LiteLLM_ProxyModelTable\" SET model_name='$main' WHERE model_name='$vip';"
done

echo "→ step 2/3 CM: drop 3 fallback entries"
TMP_CM="$(mktemp)"; trap 'rm -f "$TMP_CM"' EXIT
cm_get_yaml > "$TMP_CM"
python3 "$DIR/cm_patch_fallback.py" "$TMP_CM" "$GROUP" remove
cm_apply_yaml_stdin < "$TMP_CM"

echo "→ step 3/3 rollout restart litellm-proxy"
rollout_restart

echo "== DONE: chatgpt-vip-$GROUP destroyed =="
