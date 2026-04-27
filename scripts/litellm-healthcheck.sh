#!/bin/bash
#
# litellm-healthcheck.sh - one-shot health check for litellm-proxy on K8s.
#
# Asserts that the latest live pod has:
#   - 1/1 Running
#   - prisma-migrate + wipe-db-config-rows initContainers completed cleanly
#   - streaming_bridge boot patches active (httpx 120s + iterator init)
#   - null_byte_sanitize boot patch active (PrismaClient.jsonify_object)
#   - 5 expected callbacks registered in runtime
#   - 13 fallback rules loaded
#   - opus-4.7 fallback chain starts with anthropic.openrouter.claude-opus-4-7
#   - no fresh update_spend exceptions in last 5 minutes
#
# Reference doc: .cursor/skills/litellm-ops/SKILL.md
#
# Exit code 0 = all green; 1 = at least one check failed.

set -uo pipefail

NS=carher
APP=litellm-proxy

POD=$(kubectl get po -n "$NS" -l "app=$APP" --sort-by=.metadata.creationTimestamp \
       -o jsonpath='{.items[-1].metadata.name}' 2>/dev/null)
if [ -z "$POD" ]; then
  echo "✗ no $APP pod found in ns=$NS"; exit 1
fi
echo "Latest pod: $POD"
echo

PASS=0; FAIL=0
check() {
  if eval "$2" >/dev/null 2>&1; then
    echo "  ✓ $1"; PASS=$((PASS+1))
  else
    echo "  ✗ $1"; FAIL=$((FAIL+1))
  fi
}

echo "[1] Pod readiness"
check "Pod 1/1 Running" \
  "kubectl get po $POD -n $NS -o jsonpath='{.status.containerStatuses[0].ready}' | grep -q true"

echo "[2] InitContainers"
check "wipe-db-config-rows ran" \
  "kubectl logs $POD -c wipe-db-config-rows -n $NS 2>&1 | grep -q 'LiteLLM_Config is clean'"
check "prisma-migrate done" \
  "kubectl logs $POD -c prisma-migrate -n $NS 2>&1 | grep -q 'Your database is now in sync'"

echo "[3] Boot-time monkey patches"
LOG=$(kubectl logs "$POD" -c litellm -n "$NS" 2>&1 | head -300)
check "streaming_bridge: iterator init patch" \
  "echo \"\$LOG\" | grep -q 'patched BaseAnthropicMessagesStreamingIterator.__init__'"
check "streaming_bridge: httpx Anthropic timeout patch (read=120s)" \
  "echo \"\$LOG\" | grep -q 'patched anthropic httpx client timeout (read=120'"
check "null_byte_sanitize: PrismaClient.jsonify_object patch" \
  "echo \"\$LOG\" | grep -q 'null_byte_sanitize: patched PrismaClient.jsonify_object'"

echo "[4] Runtime registry (callbacks + fallbacks)"
MK=$(kubectl get secret litellm-secrets -n "$NS" -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
kubectl port-forward "svc/$APP" 4000:4000 -n "$NS" >/dev/null 2>&1 &
PF=$!
trap "kill $PF 2>/dev/null; wait 2>/dev/null" EXIT
sleep 3
CFG=$(curl -sf -H "Authorization: Bearer $MK" http://127.0.0.1:4000/get/config/callbacks)
if [ -z "$CFG" ]; then
  echo "  ✗ failed to query /get/config/callbacks"; FAIL=$((FAIL+1))
else
  for cb in streaming_bridge.streaming_bridge \
            opus_47_fix.thinking_schema_fix \
            force_stream.force_stream \
            embedding_sanitize.embedding_sanitize \
            null_byte_sanitize.null_byte_sanitize; do
    check "callback $cb" "echo \"\$CFG\" | grep -q $cb"
  done
  FB_COUNT=$(echo "$CFG" | python3 -c \
    'import sys,json; print(len(json.load(sys.stdin)["router_settings"]["fallbacks"]))')
  check "fallback count = 13" "[ \"$FB_COUNT\" = \"13\" ]"
  OPUS47_HOP1=$(echo "$CFG" | python3 -c \
    'import sys,json; d=json.load(sys.stdin); fbs={list(f.keys())[0]: list(f.values())[0] for f in d["router_settings"]["fallbacks"]}; print(fbs.get("anthropic.claude-opus-4-7",[""])[0])')
  check "opus-4.7 fb hop1 = OR-4.7" \
    "[ \"$OPUS47_HOP1\" = \"anthropic.openrouter.claude-opus-4-7\" ]"
fi

echo "[5] Spend-log writer health (last 5m)"
SPEND_ERR=$(kubectl logs "$POD" -c litellm -n "$NS" --since=5m 2>&1 \
  | grep -cE 'update_spend.*Exception|unexpected end of hex|22P05|invalid byte sequence' \
  || true)
check "no update_spend exceptions in last 5m (got $SPEND_ERR)" \
  "[ \"$SPEND_ERR\" = \"0\" ]"

echo "[6] Cooldown collateral-damage protection"
check "router_settings.disable_cooldowns: true in YAML" \
  "grep -qE '^[[:space:]]+disable_cooldowns:[[:space:]]+true' k8s/litellm-proxy.yaml"

echo
echo "PASS=$PASS  FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
