#!/usr/bin/env bash
# Manage daily budgets for 198 Pro claude-code-* LiteLLM keys.
#
# Defaults to the current team policy migration: $5/day and $10/day -> $2/day.
#
# Usage:
#   scripts/litellm-pro-claude-budget.sh --inspect
#   scripts/litellm-pro-claude-budget.sh --apply
#   scripts/litellm-pro-claude-budget.sh --apply --from 5,10 --to 2
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
JMS="$ROOT/scripts/jms"

HOST="AIYJY-litellm"
NS="litellm-product"
MODE="inspect"
FROM_BUDGETS="5,10"
TO_BUDGET="2"

usage() {
  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --inspect) MODE="inspect"; shift ;;
    --apply) MODE="apply"; shift ;;
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
