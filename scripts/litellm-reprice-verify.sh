#!/usr/bin/env bash
# litellm-reprice-verify: compare SpendLogs spend-per-input-token BEFORE vs
# AFTER a given timestamp for one model pattern. Confirms repricing actually
# took effect on the proxy.
#
# Why this script exists: rollout success != cost-calc change. The only
# ground-truth is what LiteLLM_SpendLogs records per request.
#
# Usage:
#   scripts/litellm-reprice-verify.sh <model_pattern> [rollout_ts] [window_minutes]
#
# Example (verify Claude *2 that rolled out around 16:42):
#   scripts/litellm-reprice-verify.sh '%claude-sonnet%' '2026-05-21 16:42' 30
#
# Default rollout_ts = NOW() - 5 minutes (= just rolled out)
# Default window    = 30 minutes
#
# Model pattern uses PostgreSQL LIKE; remember model field includes provider
# prefix: 'openai/chatgpt-gpt-5.5', 'anthropic/anthropic.claude-sonnet-4-6'.
set -euo pipefail

PATTERN="${1:-}"
ROLLOUT_TS="${2:-}"
WINDOW="${3:-30}"

if [ -z "$PATTERN" ]; then
  cat <<EOF >&2
usage: $0 <model_pattern> [rollout_ts] [window_minutes]
  model_pattern: PostgreSQL LIKE pattern, e.g. '%claude-sonnet%' or 'openai/chatgpt-gpt-5.5'
  rollout_ts:    ISO timestamp, default: 5 minutes ago
  window:        minutes to compare on each side, default 30
EOF
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

SQL_TS_EXPR="NOW() - INTERVAL '5 minutes'"
if [ -n "$ROLLOUT_TS" ]; then
  SQL_TS_EXPR="'${ROLLOUT_TS}'::timestamp"
fi

cat > /tmp/litellm-reprice-verify.sh.remote << REMOTE_EOF
set -e
kubectl exec -n carher litellm-db-0 -- psql -U litellm -d litellm -A -F"|" -c "
SELECT
  CASE WHEN \"startTime\" >= ${SQL_TS_EXPR} THEN 'after' ELSE 'before' END AS phase,
  COUNT(*)                                                       AS calls,
  SUM(prompt_tokens)                                             AS in_tok,
  SUM(completion_tokens)                                         AS out_tok,
  ROUND(SUM(spend)::numeric, 6)                                  AS total_spend_usd,
  ROUND((SUM(spend)::numeric / NULLIF(SUM(prompt_tokens),0)) * 1000000, 4) AS per_M_in,
  ROUND((SUM(spend)::numeric / NULLIF(SUM(completion_tokens),0)) * 1000000, 4) AS per_M_out
FROM \"LiteLLM_SpendLogs\"
WHERE model LIKE '${PATTERN}'
  AND \"startTime\" >= ${SQL_TS_EXPR} - INTERVAL '${WINDOW} minutes'
  AND \"startTime\" <  ${SQL_TS_EXPR} + INTERVAL '${WINDOW} minutes'
  AND spend > 0
GROUP BY phase
ORDER BY phase DESC;"
REMOTE_EOF

"$REPO_ROOT/scripts/jms" scp /tmp/litellm-reprice-verify.sh.remote k8s-work-226:/tmp/ >/dev/null
echo "model LIKE '${PATTERN}', rollout @ ${ROLLOUT_TS:-5min-ago}, window ±${WINDOW}m"
echo
"$REPO_ROOT/scripts/jms" ssh k8s-work-226 'bash /tmp/litellm-reprice-verify.sh.remote' 2>&1 | tail -10
echo
echo "Expected ratio = new_per_M_in / old_per_M_in should match your reprice factor."
echo "If 'after' has no rows: model has no traffic in the window — wait or pick another pattern."
echo "If 'before' rows look weird: SpendLogs.model includes provider prefix; verify pattern with:"
echo "  scripts/jms ssh k8s-work-226 'kubectl exec -n carher litellm-db-0 -- psql -U litellm -d litellm -c \"SELECT DISTINCT model FROM \\\"LiteLLM_SpendLogs\\\" WHERE \\\"startTime\\\" > NOW() - INTERVAL ${WINDOW} minutes ORDER BY model;\"'"
