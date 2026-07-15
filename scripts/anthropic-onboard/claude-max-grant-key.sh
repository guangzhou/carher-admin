#!/usr/bin/env bash
# Grant a LiteLLM virtual key access to the claude-max-* (CC Max) model family,
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
#       → adds claude-max-{opus,sonnet,haiku,sonnet-5}+fable5 to that key's models[]
#
#   ./claude-max-grant-key.sh claude-code-someuser-abcd --alias
#       → above + sets aliases: anthropic.claude-{opus-4-6/7/8,sonnet-4-6,haiku-4-5,
#         fable-5,sonnet-5} + claude-sonnet-5 transparently route to the ccmax pool.
#         Also strips stale pins (fable5→wangsu / reverse claude-max-*→anthropic.*).
#
#   ./claude-max-grant-key.sh claude-code-someuser-abcd --revoke
#       → removes claude-max-* from models[] AND the ccmax aliases
#         (requests fall back to native anthropic.claude-* groups)
#
# Standard map (2026-07-15 先锋 19 key 改造基准, memory:
# ccmax-pioneer-19-keys-primary-wangsu-fallback):
#   anthropic.claude-opus-4-{6,7,8} → claude-max-opus      (→ opus-4-8 @ Max)
#   anthropic.claude-sonnet-4-6     → claude-max-sonnet
#   anthropic.claude-haiku-4-5      → claude-max-haiku
#   anthropic.claude-fable-5        → fable5
#   anthropic.claude-sonnet-5 / claude-sonnet-5 → claude-max-sonnet-5
# router fallbacks: claude-max-* / fable5 → anthropic.wangsu.claude-* (网宿为辅)
set -euo pipefail

KEY_ALIAS="${1:-}"
MODE="${2:-grant}"

if [[ -z "$KEY_ALIAS" || "${KEY_ALIAS}" == "--help" ]]; then
  sed -n '2,32p' "$0"
  exit 1
fi

DIR="$(cd "$(dirname "$0")" && pwd)"
JMS="$DIR/../jms"
[[ -x "$JMS" ]] || { echo "missing $JMS"; exit 1; }

NS=litellm-product
DB_POD=litellm-db-0
PSQL="kubectl exec -n $NS $DB_POD -- psql -U litellm litellm"
FLUSH="kubectl exec -n $NS litellm-redis-0 -- redis-cli FLUSHDB"

GRANT_SQL="UPDATE \\\"LiteLLM_VerificationToken\\\"
  SET models = (SELECT array_agg(DISTINCT m) FROM unnest(models ||
    ARRAY['claude-max-opus','claude-max-sonnet','claude-max-haiku','claude-max-sonnet-5','fable5']) AS m)
  WHERE key_alias = '$KEY_ALIAS';"

case "$MODE" in
  grant|--grant)
    echo "[1/2] adding claude-max-{opus,sonnet,haiku,sonnet-5}+fable5 to models[]…"
    $JMS ssh AIYJY-litellm "$PSQL -c \"$GRANT_SQL\""
    echo "[2/2] FLUSHDB (direct SQL bypasses /key/update cache refresh)…"
    $JMS ssh AIYJY-litellm "$FLUSH"
    ;;
  --alias)
    echo "[1/3] adding claude-max-* to models[]…"
    $JMS ssh AIYJY-litellm "$PSQL -c \"$GRANT_SQL\""
    echo "[2/3] setting aliases (anthropic.* → ccmax pool, strip stale pins)…"
    $JMS ssh AIYJY-litellm "$PSQL -c \"
      UPDATE \\\"LiteLLM_VerificationToken\\\"
      SET aliases = (COALESCE(aliases,'{}'::jsonb)
                     - 'fable5' - 'claude-max-opus' - 'claude-max-sonnet' - 'claude-max-haiku')
                    || '{
        \\\"anthropic.claude-opus-4-6\\\": \\\"claude-max-opus\\\",
        \\\"anthropic.claude-opus-4-7\\\": \\\"claude-max-opus\\\",
        \\\"anthropic.claude-opus-4-8\\\": \\\"claude-max-opus\\\",
        \\\"anthropic.claude-sonnet-4-6\\\": \\\"claude-max-sonnet\\\",
        \\\"anthropic.claude-haiku-4-5\\\": \\\"claude-max-haiku\\\",
        \\\"anthropic.claude-fable-5\\\": \\\"fable5\\\",
        \\\"anthropic.claude-sonnet-5\\\": \\\"claude-max-sonnet-5\\\",
        \\\"claude-sonnet-5\\\": \\\"claude-max-sonnet-5\\\"
      }'::jsonb
      WHERE key_alias = '$KEY_ALIAS';\""
    echo "[3/3] FLUSHDB…"
    $JMS ssh AIYJY-litellm "$FLUSH"
    ;;
  revoke|--revoke)
    echo "[1/2] removing claude-max-* from models[] AND ccmax aliases…"
    $JMS ssh AIYJY-litellm "$PSQL -c \"
      UPDATE \\\"LiteLLM_VerificationToken\\\"
      SET models = array(SELECT m FROM unnest(models) m WHERE m NOT LIKE 'claude-max-%'),
          aliases = aliases - 'anthropic.claude-opus-4-6'
                            - 'anthropic.claude-opus-4-7'
                            - 'anthropic.claude-opus-4-8'
                            - 'anthropic.claude-sonnet-4-6'
                            - 'anthropic.claude-haiku-4-5'
                            - 'anthropic.claude-fable-5'
                            - 'anthropic.claude-sonnet-5'
                            - 'claude-sonnet-5'
      WHERE key_alias = '$KEY_ALIAS';\""
    echo "[2/2] FLUSHDB…"
    $JMS ssh AIYJY-litellm "$FLUSH"
    ;;
  *)
    echo "unknown mode: $MODE  (use grant / --alias / --revoke)"; exit 1
    ;;
esac

echo "[final] verify:"
$JMS ssh AIYJY-litellm "$PSQL -c \"
  SELECT key_alias,
         'claude-max-opus' = ANY(models) AS has_max,
         aliases->>'anthropic.claude-opus-4-7' AS alias_47,
         aliases->>'claude-sonnet-5' AS alias_s5
  FROM \\\"LiteLLM_VerificationToken\\\" WHERE key_alias = '$KEY_ALIAS';\""
