#!/usr/bin/env bash
# Run the single/few-key full budget reset directly on 198 without uploading a temp file.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
RESET_SCRIPT="$SCRIPT_DIR/litellm-key-budget-reset.py"
DIRECT_HOST="${LITELLM_198_HOST:-cltx@10.68.13.198}"
JMS_ASSET="${LITELLM_198_JMS_ASSET:-AIYJY-litellm}"
JMS_BIN="${JMS_BIN:-$SCRIPT_DIR/jms}"

if [ ! -f "$RESET_SCRIPT" ]; then
  echo "FATAL: reset script not found: $RESET_SCRIPT" >&2
  exit 2
fi

if [ "$#" -eq 0 ]; then
  echo "usage: $0 <key_alias> [<key_alias> ...] [--like] [--apply] [reset options]" >&2
  exit 2
fi

printf -v REMOTE_CMD '%q ' sudo -n python3 - "$@"

echo "connection: direct ssh $DIRECT_HOST"
set +e
ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=no \
  "$DIRECT_HOST" "$REMOTE_CMD" < "$RESET_SCRIPT"
RC=$?
set -e

if [ "$RC" -ne 255 ]; then
  exit "$RC"
fi

echo "direct ssh unavailable; fallback: jms $JMS_ASSET" >&2
"$JMS_BIN" ssh "$JMS_ASSET" "$REMOTE_CMD" < "$RESET_SCRIPT"
