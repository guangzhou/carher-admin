#!/usr/bin/env bash
# Set Claude-family daily limits for claude-code-* keys on 198.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SCRIPT="$SCRIPT_DIR/litellm-198-cursor-family-limit.py"
HOST="${LITELLM_198_HOST:-cltx@10.68.13.198}"

if [ ! -f "$SCRIPT" ]; then
  echo "FATAL: helper not found: $SCRIPT" >&2
  exit 2
fi
if [ "$#" -eq 0 ]; then
  echo "usage: $0 (--all | --owner NAME | --alias ALIAS | --contains TEXT) --limit USD [--max-budget USD] [--apply]" >&2
  exit 2
fi

printf -v REMOTE_CMD '%q ' sudo -n python3 - --key-prefix claude-code- "$@"
echo "connection: direct ssh $HOST"
set +e
ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=no \
  "$HOST" "$REMOTE_CMD" < "$SCRIPT"
rc=$?
set -e
if [ "$rc" -ne 255 ]; then
  exit "$rc"
fi

JMS_ASSET="${LITELLM_198_JMS_ASSET:-AIYJY-litellm}"
JMS_BIN="${JMS_BIN:-$SCRIPT_DIR/jms}"
echo "direct ssh unavailable; fallback: jms $JMS_ASSET" >&2
"$JMS_BIN" ssh "$JMS_ASSET" "$REMOTE_CMD" < "$SCRIPT"
