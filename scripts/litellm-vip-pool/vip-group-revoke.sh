#!/usr/bin/env bash
# vip-group-revoke.sh <group> <key_alias>
#
# Strip the 9 chatgpt-* aliases that point to chatgpt-vip-<group>-* from a key.
# Other aliases (claude-glm-5.2, etc.) are preserved.
# Followed by Redis FLUSHDB so removal takes effect within ~1s.

set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/lib.sh"

[[ $# -eq 2 ]] || { echo "usage: $0 <group> <key_alias>" >&2; exit 2; }
GROUP="$1"; KEY_ALIAS="$2"
require_group "$GROUP"

echo "== vip-group-revoke: group=$GROUP key=$KEY_ALIAS =="

CUR=$(psql_cmd "SELECT COALESCE(aliases::text, '{}') FROM \"LiteLLM_VerificationToken\" WHERE key_alias='$KEY_ALIAS';")
[[ -n "$CUR" ]] || { echo "ERR: no key with alias=$KEY_ALIAS" >&2; exit 1; }

MERGED=$(python3 - "$GROUP" "$CUR" <<'PY'
import json, sys
group = sys.argv[1]
cur = json.loads(sys.argv[2])
strip = {
    "gpt-5.5","chatgpt-gpt-5.5",
    "gpt-5.4","chatgpt-gpt-5.4","gpt-5.2","gpt-5.4-mini",
    "gpt-5.3-codex","chatgpt-gpt-5.3-codex","chatgpt-gpt-5.3-codex-spark",
}
target_prefix = f"chatgpt-vip-{group}-"
out = {k: v for k, v in cur.items()
       if not (k in strip and isinstance(v, str) and v.startswith(target_prefix))}
print(json.dumps(out))
PY
)

echo "→ SQL: strip vip aliases"
psql_cmd "UPDATE \"LiteLLM_VerificationToken\" SET aliases='$MERGED'::jsonb, updated_at=NOW() WHERE key_alias='$KEY_ALIAS';"

echo "→ redis FLUSHDB"
redis_flushdb >/dev/null

echo "== DONE: $KEY_ALIAS revoked from vip-$GROUP =="
