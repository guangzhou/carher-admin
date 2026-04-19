#!/usr/bin/env bash
# Run her-cost-stats.js on a single pod and save JSON to OUT_DIR/uid-<uid>.json
# Usage: run_one_pod.sh <uid> <pod-name>
set -euo pipefail

UID_="${1:?uid required}"
POD="${2:?pod required}"
NS="${NAMESPACE:-carher}"
OUT_DIR="${OUT_DIR:-./out}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_B64_FILE="${SCRIPT_DIR}/her-cost-stats.js.b64"

[ -f "$SCRIPT_B64_FILE" ] || base64 -i "${SCRIPT_DIR}/her-cost-stats.js" > "$SCRIPT_B64_FILE"
SCRIPT_B64=$(cat "$SCRIPT_B64_FILE" | tr -d '\n')

mkdir -p "$OUT_DIR"
OUT="${OUT_DIR}/uid-${UID_}.json"
ERR="${OUT_DIR}/uid-${UID_}.err"

# Idempotent: skip if valid JSON exists
if [ -s "$OUT" ] && python3 -c "import json,sys;json.load(open('$OUT'))" 2>/dev/null; then
  echo "uid=${UID_} cached" >&2
  exit 0
fi

kubectl -n "$NS" exec "$POD" -c carher -- sh -c \
  "echo '$SCRIPT_B64' | base64 -d > /tmp/her-cost-stats.js && node /tmp/her-cost-stats.js --json" \
  > "$OUT" 2> "$ERR" || {
    echo "uid=${UID_} FAIL $(head -c 200 "$ERR")" >&2
    rm -f "$OUT"
    exit 1
}

echo "uid=${UID_} ok $(wc -c < "$OUT") bytes"
