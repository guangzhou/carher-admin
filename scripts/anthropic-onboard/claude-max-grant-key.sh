#!/usr/bin/env bash
# Grant a LiteLLM virtual key access to the claude-max-* model family,
# and (optionally) install per-key aliases so the user can keep calling
# `anthropic.claude-opus-4-7` etc. unchanged but be silently routed to Max.
#
# Target cluster: 198 prod LiteLLM (litellm-product/litellm-db-0).
#
# Usage:
#   ./claude-max-grant-key.sh <key_alias> [--alias]
#   ./claude-max-grant-key.sh <key_alias> --revoke
#
# Examples:
#   ./claude-max-grant-key.sh claude-code-someuser-abcd
#       → adds claude-max-{opus,sonnet,haiku} to that key's models[]
#
#   ./claude-max-grant-key.sh claude-code-someuser-abcd --alias
#       → above + sets aliases so anthropic.claude-{opus-4-7,sonnet-4-6,haiku-4-5}
#         transparently route to claude-max-*  (user need not change config)
#
#   ./claude-max-grant-key.sh claude-code-someuser-abcd --revoke
#       → removes claude-max-* from models[] AND removes the 3 aliases
#
# Pre-req: claude-max-* must already be present in litellm-product config.yaml
# (use patch-litellm-claude-max.py prod once globally).
set -euo pipefail

KEY_ALIAS="${1:-}"
MODE="${2:-grant}"

if [[ -z "$KEY_ALIAS" || "${KEY_ALIAS}" == "--help" ]]; then
  sed -n '2,30p' "$0"
  exit 1
fi

DIR="$(cd "$(dirname "$0")" && pwd)"
JMS="$DIR/../jms"
[[ -x "$JMS" ]] || { echo "missing $JMS"; exit 1; }

NS=litellm-product
DB_POD=litellm-db-0
PSQL="kubectl exec -n $NS $DB_POD -- psql -U litellm litellm"

case "$MODE" in
  grant|--grant)
    echo "[1/2] adding claude-max-{opus,sonnet,haiku} to models[]…"
    $JMS ssh AIYJY-litellm "$PSQL -c \"
      UPDATE \\\"LiteLLM_VerificationToken\\\"
      SET models = array_cat(models, ARRAY['claude-max-opus','claude-max-sonnet','claude-max-haiku'])
      WHERE key_alias = '$KEY_ALIAS' AND NOT 'claude-max-opus' = ANY(models);\""
    ;;
  --alias)
    echo "[1/3] adding claude-max-* to models[]…"
    $JMS ssh AIYJY-litellm "$PSQL -c \"
      UPDATE \\\"LiteLLM_VerificationToken\\\"
      SET models = array_cat(models, ARRAY['claude-max-opus','claude-max-sonnet','claude-max-haiku'])
      WHERE key_alias = '$KEY_ALIAS' AND NOT 'claude-max-opus' = ANY(models);\""
    echo "[2/3] setting aliases (anthropic.* → claude-max-*)…"
    $JMS ssh AIYJY-litellm "$PSQL -c \"
      UPDATE \\\"LiteLLM_VerificationToken\\\"
      SET aliases = COALESCE(aliases,'{}'::jsonb) || '{
        \\\"anthropic.claude-opus-4-7\\\": \\\"claude-max-opus\\\",
        \\\"anthropic.claude-sonnet-4-6\\\": \\\"claude-max-sonnet\\\",
        \\\"anthropic.claude-haiku-4-5\\\": \\\"claude-max-haiku\\\"
      }'::jsonb
      WHERE key_alias = '$KEY_ALIAS';\""
    ;;
  revoke|--revoke)
    echo "[1/2] removing claude-max-* from models[] AND aliases…"
    $JMS ssh AIYJY-litellm "$PSQL -c \"
      UPDATE \\\"LiteLLM_VerificationToken\\\"
      SET models = array(SELECT m FROM unnest(models) m WHERE m NOT LIKE 'claude-max-%'),
          aliases = aliases - 'anthropic.claude-opus-4-7'
                            - 'anthropic.claude-sonnet-4-6'
                            - 'anthropic.claude-haiku-4-5'
      WHERE key_alias = '$KEY_ALIAS';\""
    ;;
  *)
    echo "unknown mode: $MODE  (use grant / --alias / --revoke)"; exit 1
    ;;
esac

echo "[final] verify:"
$JMS ssh AIYJY-litellm "$PSQL -c \"
  SELECT key_alias,
         'claude-max-opus' = ANY(models) AS has_max,
         aliases->>'anthropic.claude-opus-4-7' AS alias_47
  FROM \\\"LiteLLM_VerificationToken\\\" WHERE key_alias = '$KEY_ALIAS';\""

echo "(Note: LiteLLM key cache TTL is 60s — new traffic picks up within 1 min)"
