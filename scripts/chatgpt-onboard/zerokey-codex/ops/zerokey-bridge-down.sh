#!/usr/bin/env bash
# Stop the local zerokey-codex Responses bridge.
set -euo pipefail
LISTEN="${ZK_LISTEN:-127.0.0.1:8788}"
PORT="${LISTEN##*:}"
if lsof -ti "tcp:${PORT}" >/dev/null 2>&1; then
  lsof -ti "tcp:${PORT}" | xargs kill 2>/dev/null || true
  echo "stopped bridge on ${PORT}"
else
  echo "no bridge listening on ${PORT}"
fi
