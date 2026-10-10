#!/usr/bin/env bash
# Reset all Claude keys belonging to one 198 user, without touching Cursor keys.
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "usage: $0 <username> [--apply] [reset options]" >&2
  exit 2
fi

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
USER_NAME="$1"
shift

# Restrict the fuzzy match to the Claude key namespace. The reset helper still
# performs a dry-run unless --apply is explicitly supplied.
exec "$SCRIPT_DIR/litellm-198-key-budget-reset.sh" "claude-code-$USER_NAME" --like "$@"
