#!/usr/bin/env bash
# Wrapper: run cc-max-upstream-status.py on the active CC Max runtime host.
# All args forwarded.
#
# Usage:
#   ./cc-max-upstream-status.sh                  # snapshot
#   ./cc-max-upstream-status.sh --watch 60       # refresh every 60s
#   ./cc-max-upstream-status.sh --json           # JSON for scripting
#
# Mechanics:
#   1. Copy the .py to ${CC_MAX_QUOTA_ASSET:-AIYJY-litellm}:/tmp/cc-max-upstream-status.py via jms ssh stdin
#   2. Run `python3 /tmp/cc-max-upstream-status.py "$@"`
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
JMS="$DIR/../jms"
PY="$DIR/cc-max-upstream-status.py"
ASSET="${CC_MAX_QUOTA_ASSET:-AIYJY-litellm}"
[[ -f "$PY" ]] || { echo "missing $PY"; exit 1; }
[[ -x "$JMS" ]] || { echo "missing $JMS (run from carher-admin repo)"; exit 1; }

REMOTE_PATH="/tmp/cc-max-upstream-status.py"

# Step 1: upload script
"$JMS" ssh "$ASSET" "cat > $REMOTE_PATH" < "$PY"

# Step 2: run with args
if [[ $# -gt 0 ]]; then
  ARGS_QUOTED=$(printf '%q ' "$@")
  "$JMS" ssh "$ASSET" "python3 $REMOTE_PATH $ARGS_QUOTED"
else
  "$JMS" ssh "$ASSET" "python3 $REMOTE_PATH"
fi
