#!/usr/bin/env bash
# Grant a user's cursor key access to all chatgpt-* models in the 198 dev env.
#
# Behavior (idempotent on result, not on token):
#   1. Find existing cursor-<name>-* key in litellm-dev DB
#   2. Delete it (if spend == 0) and recreate with same alias + extended allowlist
#      (workaround: /key/regenerate is enterprise-gated in OSS LiteLLM)
#   3. Smoke-test the new key against chatgpt-gpt-5.5 via /chat/completions
#
# Usage:
#   ./scripts/dev-chatgpt-grant-cursor.sh <name>
#   ./scripts/dev-chatgpt-grant-cursor.sh guran
#   ./scripts/dev-chatgpt-grant-cursor.sh liuguoxian

set -euo pipefail

NAME="${1:-}"
if [[ -z "$NAME" ]]; then
  echo "Usage: $0 <name>  (e.g. guran, liuguoxian)" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JMS="$SCRIPT_DIR/jms"

DEV_MASTER="${DEV_LITELLM_MASTER_KEY:?set DEV_LITELLM_MASTER_KEY}"
DEV_BASE_INTERNAL='http://localhost:30400/dev'
DEV_BASE_DISPLAY='http://10.68.13.198:30400/dev/v1'

# Dev cursor allowlist — 11 个有真实后端的模型 + 5 个 OpenAI 原生 / wangsu 兼容别名（router-level）。
# 别名 (gpt-5.5 / gpt-5.4-mini / gpt-5.2 / wangsu-gpt-5.4 / wangsu-gpt-5.5 等) 在
# router_settings.model_group_alias 里映射到 chatgpt-* 后端：
#   gpt-5.5         -> chatgpt-gpt-5.5
#   gpt-5.4-mini    -> chatgpt-gpt-5.3-codex-spark
#   gpt-5.2         -> chatgpt-gpt-5.4
#   wangsu-gpt-5.4  -> chatgpt-gpt-5.4   (2026-05-16 兼容老 key)
#   wangsu-gpt-5.5  -> chatgpt-gpt-5.5   (同上)
#   gpt-5.4         -> chatgpt-gpt-5.4   (修死引用)
#   gpt-5.3-codex   -> chatgpt-gpt-5.3-codex   (修死引用)
# 别名都解决 Codex Desktop App 硬编码 picker bug (openai/codex#19694)。
ALLOWLIST='["wangsu-gemini-3.1-pro-preview","anthropic.claude-opus-4-7","chatgpt-gpt-5.5","chatgpt-gpt-5.4","chatgpt-gpt-5.3-codex","chatgpt-gpt-5.3-codex-spark","chatgpt-gpt-5.3-instant","chatgpt-gpt-5.3-chat-latest","gpt-5.5","gpt-5.4-mini","gpt-5.2","gpt-5.4","gpt-5.3-codex","wangsu-gpt-5.4","wangsu-gpt-5.5"]'

# 1. Locate existing alias + check spend
LOOKUP_SQL=$(cat <<SQL
SELECT key_alias || '|' || COALESCE(spend::text,'0')
FROM "LiteLLM_VerificationToken"
WHERE key_alias LIKE 'cursor-${NAME}-%'
LIMIT 1;
SQL
)
TMP_SQL=$(mktemp)
echo "$LOOKUP_SQL" > "$TMP_SQL"
"$JMS" scp "$TMP_SQL" AIYJY-litellm:/tmp/_lookup_cursor.sql >/dev/null
LOOKUP=$("$JMS" ssh AIYJY-litellm \
  "kubectl cp /tmp/_lookup_cursor.sql litellm-dev/litellm-db-0:/tmp/_lookup_cursor.sql >/dev/null && \
   kubectl exec -n litellm-dev litellm-db-0 -- bash -c \
     'PGPASSWORD=\$POSTGRES_PASSWORD psql -U \$POSTGRES_USER -d \$POSTGRES_DB -t -A -f /tmp/_lookup_cursor.sql'" \
  2>/dev/null | tr -d '\r' | grep -E '^cursor-' | head -1)
rm -f "$TMP_SQL"

if [[ -z "$LOOKUP" ]]; then
  echo "ERROR: no cursor-${NAME}-* key in litellm-dev DB. Create one first via the admin UI." >&2
  exit 1
fi

ALIAS="${LOOKUP%|*}"
SPEND="${LOOKUP#*|}"
SPEND_INT="${SPEND%%.*}"

if [[ "${SPEND_INT:-0}" -gt 0 ]]; then
  echo "ERROR: $ALIAS has non-zero spend (\$$SPEND). Refusing to delete + recreate." >&2
  echo "Reset spend manually first, then re-run." >&2
  exit 1
fi

USER_ID="${ALIAS%-*}"   # cursor-guran-v2sb -> cursor-guran
echo "alias  : $ALIAS"
echo "user_id: $USER_ID"

# 2. Delete + recreate
"$JMS" ssh AIYJY-litellm \
  "curl -sS -X POST $DEV_BASE_INTERNAL/key/delete \
    -H 'Authorization: Bearer $DEV_MASTER' \
    -H 'Content-Type: application/json' \
    -d '{\"key_aliases\":[\"$ALIAS\"]}'" >/dev/null

NEW_KEY_JSON=$("$JMS" ssh AIYJY-litellm \
  "curl -sS -X POST $DEV_BASE_INTERNAL/key/generate \
    -H 'Authorization: Bearer $DEV_MASTER' \
    -H 'Content-Type: application/json' \
    -d '{\"key_alias\":\"$ALIAS\",\"user_id\":\"$USER_ID\",\"models\":$ALLOWLIST,\"max_budget\":100,\"budget_duration\":\"1d\"}'")

NEW_KEY=$(echo "$NEW_KEY_JSON" | python3 -c 'import sys,json; print(json.load(sys.stdin)["key"])')

# 3. Smoke test
SMOKE=$("$JMS" ssh AIYJY-litellm \
  "curl -sN -X POST $DEV_BASE_INTERNAL/v1/chat/completions \
    -H 'Authorization: Bearer $NEW_KEY' \
    -H 'Content-Type: application/json' \
    -d '{\"model\":\"chatgpt-gpt-5.5\",\"messages\":[{\"role\":\"user\",\"content\":\"reply OK only\"}],\"stream\":true}' \
    --max-time 25" 2>&1 | grep -oE '"content":"[^"]*"' | head -1)

cat <<OUT

== $ALIAS ==
  endpoint  : $DEV_BASE_DISPLAY
  api_key   : $NEW_KEY
  budget    : \$100/day
  models    : 15 (1 wangsu-gemini + 1 claude-opus-4-7 + 6 chatgpt-* + 7 OpenAI/wangsu aliases)
  smoke     : ${SMOKE:-FAILED — check proxy logs}

Cursor:
  Settings -> Models -> OpenAI Base URL = $DEV_BASE_DISPLAY
  OpenAI API Key = $NEW_KEY
  Add models: chatgpt-gpt-5.5 / -5.4 / -5.3-codex / -5.3-codex-spark / -5.3-instant / -5.3-chat-latest

Codex IDE (Desktop App):
  ~/.codex/config.toml — set openai_base_url = "$DEV_BASE_DISPLAY"
  ~/.codex/auth.json   — OPENAI_API_KEY = $NEW_KEY
  IDE model picker can use gpt-5.5 / gpt-5.4-mini / gpt-5.2 directly
  (routed via model_group_alias to ChatGPT Pro backend)

Codex CLI:
  ~/.codex/config.toml — see chatgpt-pro-litellm SKILL.md "Codex CLI 接入步骤"
OUT
