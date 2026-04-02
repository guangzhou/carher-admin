#!/usr/bin/env bash
#
# Zero-downtime canary: switch 20 instances to wangsu/opus without pod restart.
#
# How it works:
#   1. Batch-update model+provider on the CRD (via admin API)
#   2. Operator detects config-only change → updates ConfigMap, skips Deployment rollout
#   3. config-reloader sidecar detects ConfigMap volume change → writes merged config
#   4. CarHer process picks up new config on next request — zero WebSocket disruption
#
# Usage: ADMIN_API_KEY=xxx ./scripts/canary-wangsu-opus.sh
#
set -euo pipefail

API="${CARHER_API:-https://admin.carher.net/api}"
AUTH_HEADER="X-API-Key: ${ADMIN_API_KEY:?Set ADMIN_API_KEY}"
COUNT="${CANARY_COUNT:-20}"

echo "==> Fetching running instances..."
ALL_IDS=$(curl -sf -H "$AUTH_HEADER" "$API/instances" \
  | python3 -c "
import json, sys
data = json.load(sys.stdin)
ids = [d['id'] for d in data if d.get('status','').lower() == 'running']
ids.sort()
print(' '.join(str(i) for i in ids))
")

IDS_ARRAY=($ALL_IDS)
TOTAL=${#IDS_ARRAY[@]}
echo "==> Found $TOTAL running instances"

if [ "$TOTAL" -lt "$COUNT" ]; then
  echo "WARNING: Only $TOTAL running instances, using all of them"
  COUNT=$TOTAL
fi

CANARY_IDS=("${IDS_ARRAY[@]:0:$COUNT}")
JSON_IDS=$(python3 -c "import json; print(json.dumps([int(x) for x in '${CANARY_IDS[*]}'.split()]))")

echo "==> Moving $COUNT instances to canary group: ${CANARY_IDS[*]}"
curl -sf -X POST -H "$AUTH_HEADER" -H "Content-Type: application/json" \
  "$API/instances/batch-deploy-group" \
  -d "{\"ids\":$JSON_IDS,\"group\":\"canary\"}" | python3 -m json.tool

echo ""
echo "==> Updating $COUNT instances to model=opus, provider=wangsu (no restart)..."
curl -sf -X POST -H "$AUTH_HEADER" -H "Content-Type: application/json" \
  "$API/instances/batch" \
  -d "{\"ids\":$JSON_IDS,\"action\":\"update\",\"params\":{\"model\":\"opus\",\"provider\":\"wangsu\"}}" \
  | python3 -m json.tool

echo ""
echo "Done! $COUNT instances switched to wangsu/opus via hot-reload."
echo "  - Operator updates ConfigMap only (no Deployment rollout)"
echo "  - config-reloader sidecar syncs new config to running pod"
echo "  - NO pod restart, NO WebSocket disconnect"
echo ""
echo "Monitor: curl -sH 'X-API-Key: ...' '$API/instances/search?deploy_group=canary' | jq"
echo "Rollback: re-run with CANARY_COUNT=0 or update model/provider back"
