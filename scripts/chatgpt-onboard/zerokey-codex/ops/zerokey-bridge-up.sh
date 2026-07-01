#!/usr/bin/env bash
# Start the local zerokey-codex Responses bridge on the Mac.
# It translates Codex /v1/responses <-> zerokey chat/completions (Bearer vscode)
# so `codex -p zkagent` gets the full Agent loop (exec_command: shell + apply_patch)
# on ChatGPT web quota via the 188 zerokey accounts.
#
#   ./zerokey-bridge-up.sh            # single upstream (default 8124)
#   ZK_UPSTREAMS=...  ./zerokey-bridge-up.sh   # custom pool (comma list)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
BRIDGE_PY="$HERE/../bridge/zerokey-codex-responses-bridge.py"

LISTEN="${ZK_LISTEN:-127.0.0.1:8788}"
# Pool of onboarded zerokey accounts on 188 (combined quota + failover).
# The bridge fails over to the next account on 5xx / empty responses.
#   8123 kristine  8124 timothy  8126 owp  8127 hgg  8128 dvo  (zyq=8125)
UPSTREAMS="${ZK_UPSTREAMS:-http://10.68.13.188:8124/v1,http://10.68.13.188:8126/v1,http://10.68.13.188:8128/v1,http://10.68.13.188:8123/v1}"
AUTH="${ZK_AUTH:-vscode}"
MODEL="${ZK_MODEL:-gpt-5-5}"
LOG="${ZK_LOG:-/tmp/zk_bridge.log}"

PORT="${LISTEN##*:}"
# stop any previous instance bound to the port
if lsof -ti "tcp:${PORT}" >/dev/null 2>&1; then
  echo "stopping existing listener on ${PORT}..."
  lsof -ti "tcp:${PORT}" | xargs kill 2>/dev/null || true
  sleep 1
fi

echo "starting bridge: ${LISTEN} -> ${UPSTREAMS} (Bearer ${AUTH}, model ${MODEL})"
BRIDGE_LISTEN="$LISTEN" BRIDGE_UPSTREAMS="$UPSTREAMS" BRIDGE_UPSTREAM_AUTH="$AUTH" \
  BRIDGE_MODEL="$MODEL" BRIDGE_LOG="$LOG" \
  nohup python3 "$BRIDGE_PY" >>"$LOG" 2>&1 &
echo "pid=$! log=$LOG"
sleep 1.5
echo -n "health: "; curl -sS -m 5 "http://${LISTEN}/health" || echo "NOT READY"
echo
echo "Codex:  codex -p zkagent   (or set model=zerokey-codex provider=zerokey_local)"
