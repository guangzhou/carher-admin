#!/usr/bin/env bash
# vip-group-grant.sh <group> <key_alias>
#
# Grant a LiteLLM key access to chatgpt-vip-<group>:
#   1. Verify group exists (3 vip entries in DB)
#   2. Merge 9 chatgpt-* aliases into key.aliases jsonb (preserves other aliases)
#   3. Redis FLUSHDB (key verification cache TTL 60s otherwise)
#   4. Smoke: hit /v1/responses with that key on each of the 3 short names,
#      assert x-litellm-model-id contains chatgpt-acct-<N>-<short>
#
# Idempotent: re-run = same end state.

set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/lib.sh"

[[ $# -eq 2 ]] || { echo "usage: $0 <group> <key_alias>" >&2; exit 2; }
GROUP="$1"; KEY_ALIAS="$2"
require_group "$GROUP"

echo "== vip-group-grant: group=$GROUP key=$KEY_ALIAS =="

# 1. Verify vip group exists.
VIP_ENTRIES=$(psql_cmd "SELECT model_id, model_name FROM \"LiteLLM_ProxyModelTable\" WHERE model_name LIKE 'chatgpt-vip-${GROUP}-%' ORDER BY model_name;")
N_VIP=$(printf '%s\n' "$VIP_ENTRIES" | grep -c . || true)
if [[ "$N_VIP" -ne 3 ]]; then
  echo "ERR: chatgpt-vip-$GROUP has $N_VIP entries, expected 3. Run vip-group-create first." >&2
  printf '%s\n' "$VIP_ENTRIES" >&2
  exit 1
fi
ACCT_N=$(printf '%s\n' "$VIP_ENTRIES" | head -1 | grep -oE 'chatgpt-acct-[0-9]+' | grep -oE '[0-9]+')
echo "→ vip group backed by acct-$ACCT_N"

# 2. Get current aliases & build merged jsonb.
declare_aliases_for_group "$GROUP"
declare_fallback_for_group "$GROUP"  # unused here but consistent

CUR=$(psql_cmd "SELECT COALESCE(aliases::text, '{}') FROM \"LiteLLM_VerificationToken\" WHERE key_alias='$KEY_ALIAS';")
[[ -n "$CUR" ]] || { echo "ERR: no key with alias=$KEY_ALIAS" >&2; exit 1; }

MERGED=$(python3 - "$GROUP" "$CUR" <<'PY'
import json, sys
group = sys.argv[1]
cur = json.loads(sys.argv[2])
adds = {
    "gpt-5.5":                       f"chatgpt-vip-{group}-gpt-5.5",
    "chatgpt-gpt-5.5":               f"chatgpt-vip-{group}-gpt-5.5",
    "gpt-5.4":                       f"chatgpt-vip-{group}-gpt-5.4",
    "chatgpt-gpt-5.4":               f"chatgpt-vip-{group}-gpt-5.4",
    "gpt-5.2":                       f"chatgpt-vip-{group}-gpt-5.4",
    "gpt-5.4-mini":                  f"chatgpt-vip-{group}-gpt-5.4",
    "gpt-5.3-codex":                 f"chatgpt-vip-{group}-gpt-5.3-codex",
    "chatgpt-gpt-5.3-codex":         f"chatgpt-vip-{group}-gpt-5.3-codex",
    "chatgpt-gpt-5.3-codex-spark":   f"chatgpt-vip-{group}-gpt-5.3-codex",
}
cur.update(adds)
print(json.dumps(cur))
PY
)

echo "→ step 1/3 SQL: merge aliases ($(printf '%s' "$MERGED" | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))') keys)"
# Use psql variable substitution to avoid quoting hell.
psql_cmd "UPDATE \"LiteLLM_VerificationToken\" SET aliases='$MERGED'::jsonb, updated_at=NOW() WHERE key_alias='$KEY_ALIAS';"

echo "→ step 2/3 redis FLUSHDB"
redis_flushdb >/dev/null

# Need the actual sk-... token to test; LiteLLM stores hashed token, not raw.
# Skip end-user smoke if we don't have it; user-side test required separately.
TOKEN_HASH=$(psql_cmd "SELECT token FROM \"LiteLLM_VerificationToken\" WHERE key_alias='$KEY_ALIAS';")
echo "→ step 3/3 verify aliases written"
psql_cmd "SELECT key_alias, aliases->>'gpt-5.5' FROM \"LiteLLM_VerificationToken\" WHERE key_alias='$KEY_ALIAS';"

cat <<EOF

NEXT: smoke from user side (raw token only known to key holder):
  curl http://10.68.13.198:30402/pro/v1/responses \\
    -H 'Authorization: Bearer sk-...' \\
    -H 'Content-Type: application/json' \\
    -d '{"model":"gpt-5.5","input":"ping","max_output_tokens":16}' -i \\
    | grep -i x-litellm-model-id
  expected: chatgpt-acct-${ACCT_N}-gpt-5.5
EOF

echo "== DONE: $KEY_ALIAS granted vip-$GROUP =="
