#!/usr/bin/env bash
# 198 prod: remove the retired gpt53 family bucket for cursor-* tokens.
#
# This only deletes Redis bookkeeping keys. It does not change model routing,
# key spend/max_budget, metadata, allowlists, or key state. GPT-5.3 requests
# are handled by the callback's `other` bucket after the retirement rollout.
set -uo pipefail

APPLY=0
PAT='cursor-%'
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --pattern) PAT="${2:?missing SQL LIKE pattern}"; shift 2 ;;
    -h|--help)
      cat <<'USAGE'
Remove the retired gpt53 Redis family bucket for Cursor keys on 198.

Usage:
  litellm-198-cursor-remove-gpt53-bucket.sh              # dry-run
  litellm-198-cursor-remove-gpt53-bucket.sh --apply      # delete matching keys
  litellm-198-cursor-remove-gpt53-bucket.sh --pattern 'cursor-%' --apply
USAGE
      exit 0
      ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if ! [[ "$PAT" =~ ^[A-Za-z0-9._%-]+$ ]]; then
  echo "--pattern may contain only letters, digits, '.', '_', '-', and SQL LIKE wildcards" >&2
  exit 2
fi

HOST="${LITELLM_198_HOST:-cltx@10.68.13.198}"
ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=15 "$HOST" \
  "APPLY='$APPLY' PAT='$PAT' bash -s" <<'REMOTE'
set -uo pipefail
NS=litellm-product
DB_POD=litellm-db-0
REDIS_POD=litellm-redis-0

DB_URL=$(sudo -n kubectl -n "$NS" exec deploy/litellm-proxy -- env </dev/null 2>/dev/null \
  | grep -m1 '^DATABASE_URL=')
PG_PW=$(echo "$DB_URL" | sed -E 's|.*://[^:]+:([^@]+)@.*|\1|')
[ -n "$PG_PW" ] || { echo "FATAL: cannot derive database password" >&2; exit 2; }
PSQL() {
  sudo -n kubectl -n "$NS" exec "$DB_POD" -- env PGPASSWORD="$PG_PW" \
    psql -U litellm -d litellm -h localhost -tAqc "$1" </dev/null
}
RCLI() {
  sudo -n kubectl -n "$NS" exec "$REDIS_POD" -- redis-cli --raw "$@" </dev/null
}

TOKENS=$(mktemp)
ALL_KEYS=$(mktemp)
TARGET_KEYS=$(mktemp)
trap 'rm -f "$TOKENS" "$ALL_KEYS" "$TARGET_KEYS"' EXIT

PSQL "SELECT token FROM \"LiteLLM_VerificationToken\" WHERE key_alias LIKE '$PAT';" \
  | sort -u > "$TOKENS"
NTOK=$(wc -l < "$TOKENS" | tr -d ' ')

# Scan all dates, then filter by the exact token set. Never touch other users'
# gpt53 keys, and never use FLUSHDB.
RCLI --scan --pattern 'budget_notice:fam:gpt53:*' | sort -u > "$ALL_KEYS"
awk -F: 'NR==FNR{t[$1]=1; next} ($4 in t){print}' "$TOKENS" "$ALL_KEYS" > "$TARGET_KEYS"
NALL=$(wc -l < "$ALL_KEYS" | tr -d ' ')
NTARGET=$(wc -l < "$TARGET_KEYS" | tr -d ' ')

echo "=== remove retired cursor gpt53 bucket (pattern='$PAT') ==="
echo "cursor tokens: $NTOK   gpt53 buckets(all): $NALL   cursor gpt53 buckets: $NTARGET"
if [ "$APPLY" != "1" ]; then
  echo "VERDICT: DRY-RUN. Add --apply to delete only the listed cursor gpt53 keys."
  exit 0
fi

DEL=0
if [ "$NTARGET" -gt 0 ]; then
  OUT=$(xargs -n 400 sudo -n kubectl -n "$NS" exec "$REDIS_POD" -- redis-cli DEL \
    < "$TARGET_KEYS" 2>/dev/null)
  DEL=$(echo "$OUT" | awk '{s+=$1} END{print s+0}')
fi
LEFT=$(RCLI --scan --pattern 'budget_notice:fam:gpt53:*' \
  | awk -F: 'NR==FNR{t[$1]=1; next} ($4 in t){n++} END{print n+0}' "$TOKENS" -)
echo "redis DEL hit: $DEL/$NTARGET"
echo "cursor gpt53 buckets remaining: $LEFT"
[ "$LEFT" = "0" ] && echo "VERDICT: OK (retired cursor gpt53 bucket removed)" \
                   || echo "VERDICT: WARN (some cursor gpt53 buckets remain)"
REMOTE
