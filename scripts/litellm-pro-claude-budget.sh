#!/usr/bin/env bash
# Manage daily budgets for 198 Pro claude-code-* LiteLLM keys.
#
# Modes:
#   --inspect                  show budget distribution for claude-code-* keys
#   --apply [--from A,B --to C]  migrate keys whose max_budget IN (A,B) -> C
#                              (defaults: 5,10 -> 2; only touches budget_duration='1d' keys)
#   --set N --keys a,b,c       NORMALIZE the named keys to $N/day: max_budget=N,
#                              budget_duration='1d', spend=0 (catches up lagging daily
#                              reset), budget_reset_at=next UTC midnight. Use for e.g.
#                              先锋 pioneer keys standard ($200/day, 2026-07-16).
#
# Why --set resets spend: keys with NULL budget_duration accumulate spend for life
# and lock at max_budget forever; keys whose reset job lagged keep yesterday's spend.
# Both present as budget_exceeded 429 even after raising the cap.
#
# Usage:
#   scripts/litellm-pro-claude-budget.sh --inspect
#   scripts/litellm-pro-claude-budget.sh --apply
#   scripts/litellm-pro-claude-budget.sh --apply --from 5,10 --to 2
#   scripts/litellm-pro-claude-budget.sh --set 200 --keys claude-code-a-1234,claude-code-b-5678
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
JMS="$ROOT/scripts/jms"

HOST="AIYJY-litellm"
NS="litellm-product"
MODE="inspect"
FROM_BUDGETS="5,10"
TO_BUDGET="2"
SET_BUDGET=""
KEY_LIST=""

usage() {
  sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --inspect) MODE="inspect"; shift ;;
    --apply) MODE="apply"; shift ;;
    --set) MODE="set"; SET_BUDGET="${2:?missing value for --set}"; shift 2 ;;
    --keys) KEY_LIST="${2:?missing value for --keys}"; shift 2 ;;
    --from) FROM_BUDGETS="${2:?missing value for --from}"; shift 2 ;;
    --to) TO_BUDGET="${2:?missing value for --to}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -x "$JMS" ]] || { echo "missing executable $JMS" >&2; exit 1; }
[[ "$FROM_BUDGETS" =~ ^[0-9]+([.][0-9]+)?(,[0-9]+([.][0-9]+)?)*$ ]] || {
  echo "--from must be a comma-separated numeric list, got: $FROM_BUDGETS" >&2
  exit 2
}
[[ "$TO_BUDGET" =~ ^[0-9]+([.][0-9]+)?$ ]] || {
  echo "--to must be numeric, got: $TO_BUDGET" >&2
  exit 2
}

FROM_SQL="${FROM_BUDGETS//,/::numeric,}::numeric"

run_psql() {
  "$JMS" ssh "$HOST" "kubectl exec -i -n $NS litellm-db-0 -- psql -U litellm -d litellm -v ON_ERROR_STOP=1 -P pager=off" "$@"
}

inspect_sql() {
  cat <<SQL
SELECT max_budget, budget_duration, count(*) AS keys
FROM "LiteLLM_VerificationToken"
WHERE key_alias LIKE 'claude-code-%'
GROUP BY 1,2
ORDER BY 1,2;

SELECT max_budget AS old_budget, count(*) AS matching_keys
FROM "LiteLLM_VerificationToken"
WHERE key_alias LIKE 'claude-code-%'
  AND budget_duration = '1d'
  AND max_budget IN ($FROM_SQL)
GROUP BY 1
ORDER BY 1;
SQL
}

if [[ "$MODE" == "inspect" ]]; then
  inspect_sql | run_psql
  exit 0
fi

if [[ "$MODE" == "set" ]]; then
  [[ "$SET_BUDGET" =~ ^[0-9]+([.][0-9]+)?$ ]] || { echo "--set must be numeric, got: $SET_BUDGET" >&2; exit 2; }
  [[ -n "$KEY_LIST" ]] || { echo "--set requires --keys a,b,c (explicit key_alias list)" >&2; exit 2; }
  [[ "$KEY_LIST" =~ ^[A-Za-z0-9._,-]+$ ]] || { echo "--keys contains invalid characters: $KEY_LIST" >&2; exit 2; }
  KEYS_SQL="'${KEY_LIST//,/\',\'}'"
  RESET_AT="$(date -u -v+1d '+%Y-%m-%d 00:00:00' 2>/dev/null || date -u -d 'tomorrow' '+%Y-%m-%d 00:00:00')"
  cat <<SQL | run_psql
BEGIN;
UPDATE "LiteLLM_VerificationToken"
SET max_budget = $SET_BUDGET,
    budget_duration = '1d',
    spend = 0,
    budget_reset_at = '$RESET_AT'
WHERE key_alias IN ($KEYS_SQL);
COMMIT;

SELECT key_alias, spend, max_budget, budget_duration, budget_reset_at
FROM "LiteLLM_VerificationToken"
WHERE key_alias IN ($KEYS_SQL)
ORDER BY key_alias;
SQL
  "$JMS" ssh "$HOST" "kubectl exec -n $NS litellm-redis-0 -- redis-cli FLUSHDB"
  exit 0
fi

cat <<SQL | run_psql
BEGIN;
UPDATE "LiteLLM_VerificationToken"
SET max_budget = $TO_BUDGET
WHERE key_alias LIKE 'claude-code-%'
  AND budget_duration = '1d'
  AND max_budget IN ($FROM_SQL);
COMMIT;

SELECT max_budget, budget_duration, count(*) AS keys
FROM "LiteLLM_VerificationToken"
WHERE key_alias LIKE 'claude-code-%'
GROUP BY 1,2
ORDER BY 1,2;
SQL

"$JMS" ssh "$HOST" "kubectl exec -n $NS litellm-redis-0 -- redis-cli FLUSHDB"
