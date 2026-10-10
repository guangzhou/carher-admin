#!/usr/bin/env bash
# 198 prod: read-only audit for per-key model-family daily budget buckets.
set -euo pipefail

PATTERN='cursor-%'
ALIAS=''
DATE=''

usage() {
  cat <<'USAGE'
Audit 198 LiteLLM model-family daily budget buckets (read-only).

Usage:
  litellm-198-family-budget-audit.sh
  litellm-198-family-budget-audit.sh --alias cursor-foo-abcd
  litellm-198-family-budget-audit.sh --pattern 'cursor-%'
  litellm-198-family-budget-audit.sh --date 20260923

Defaults to all cursor-* keys and today's Beijing date. It reports all known
Redis family buckets and does not modify DB or Redis. For Cursor keys after
the retirement rollout, the gpt53 column should remain zero.
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --alias) ALIAS="${2:?missing alias}"; shift 2 ;;
    --pattern) PATTERN="${2:?missing SQL LIKE pattern}"; shift 2 ;;
    --date) DATE="${2:?missing YYYYMMDD}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [ -n "$DATE" ] && ! [[ "$DATE" =~ ^[0-9]{8}$ ]]; then
  echo "--date must be YYYYMMDD, got: $DATE" >&2
  exit 2
fi
if [ -n "$ALIAS" ] && ! [[ "$ALIAS" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "--alias contains unsupported characters" >&2
  exit 2
fi
if ! [[ "$PATTERN" =~ ^[A-Za-z0-9._%-]+$ ]]; then
  echo "--pattern may contain only letters, digits, '.', '_', '-', and SQL LIKE wildcards" >&2
  exit 2
fi

HOST="${LITELLM_198_HOST:-cltx@10.68.13.198}"

ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=15 "$HOST" \
  "PATTERN='$PATTERN' ALIAS='$ALIAS' AUDIT_DATE='$DATE' bash -s" <<'REMOTE'
set -euo pipefail
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

if [ -z "$AUDIT_DATE" ]; then
  AUDIT_DATE=$(TZ=Asia/Shanghai date +%Y%m%d)
fi

if [ -n "$ALIAS" ]; then
  WHERE="key_alias = '$ALIAS'"
  SCOPE="alias=$ALIAS"
else
  WHERE="key_alias LIKE '$PATTERN'"
  SCOPE="pattern=$PATTERN"
fi

ROWS=$(mktemp)
FAM_KEYS=$(mktemp)
FAM_VALS=$(mktemp)
FAM_ROWS=$(mktemp)
FAM_CHUNKS=$(mktemp -d)
trap 'rm -f "$ROWS" "$FAM_KEYS" "$FAM_VALS" "$FAM_ROWS"; rm -rf "$FAM_CHUNKS"' EXIT
PSQL "SELECT key_alias || '|' || token || '|' || coalesce(max_budget::text,'') || '|' || coalesce(spend::text,'')
       FROM \"LiteLLM_VerificationToken\" WHERE $WHERE ORDER BY key_alias;" > "$ROWS"

COUNT=$(wc -l < "$ROWS" | tr -d ' ')
[ "$COUNT" -gt 0 ] || { echo "no keys matched: $SCOPE" >&2; exit 1; }

printf '=== family budget audit: %s date=%s keys=%s ===\n' "$SCOPE" "$AUDIT_DATE" "$COUNT"
printf '%-42s %10s %10s %10s %12s\n' 'key_alias' 'gpt53' 'claude' 'other' 'total spend'

# One SCAN + one MGET keeps the all-Cursor audit bounded instead of doing
# three kubectl/redis round trips per key.
RCLI --scan --pattern "budget_notice:fam:*:${AUDIT_DATE}" | sort -u > "$FAM_KEYS"
if [ -s "$FAM_KEYS" ]; then
  split -l 400 "$FAM_KEYS" "$FAM_CHUNKS/chunk-"
  : > "$FAM_VALS"
  for chunk in "$FAM_CHUNKS"/chunk-*; do
    RCLI MGET $(cat "$chunk") >> "$FAM_VALS"
  done
else
  : > "$FAM_VALS"
fi
paste -d '|' "$FAM_KEYS" "$FAM_VALS" > "$FAM_ROWS"

awk -F'|' -v fam_file="$FAM_ROWS" '
  FILENAME == fam_file {
    split($1, p, ":")
    fam[p[4] "|" p[3]] = $2
    next
  }
  {
    g = fam[$2 "|gpt53"]; c = fam[$2 "|claude"]; o = fam[$2 "|other"]
    printf "%-42s %10s %10s %10s %7s/%s\n", $1, g ? g : 0, c ? c : 0, o ? o : 0, $4 ? $4 : 0, $3 ? $3 : "-"
  }
' "$FAM_ROWS" "$ROWS"

echo 'VERDICT: READ-ONLY (no DB or Redis writes)'
REMOTE
