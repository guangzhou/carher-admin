#!/usr/bin/env bash
# litellm-redis-health.sh — carher-redis 健康检查 + DualCache 内容 dump
#
# 用法:
#   ./scripts/litellm-redis-health.sh           # 健康度 + key 数量摘要
#   ./scripts/litellm-redis-health.sh --sample  # 抽样 dump 5 条 sticky mapping
#   ./scripts/litellm-redis-health.sh --vkey her-1000  # 查特定 her 的 sticky mapping
#
# 检查项:
#   1. Redis ping (从 litellm-proxy pod)
#   2. DualCache 各类 prefix 的 key 数量
#   3. TTL 抽样
#   4. (--sample) 5 条 mapping 内容
#   5. (--vkey) 指定 her 的 vkey → acct mapping

set -euo pipefail

MODE="${1:-summary}"
HER_NAME="${2:-}"
CLUSTER="${3:-aliyun}"     # aliyun | 198

case "$CLUSTER" in
  aliyun)
    NS="carher"
    REDIS_POD="carher-redis-0"
    REDIS_HOST="carher-redis.carher.svc.cluster.local"
    PROXY_DEPLOY="deploy/litellm-proxy"
    ;;
  198)
    NS="litellm-product"
    REDIS_POD="litellm-redis-0"
    REDIS_HOST="litellm-redis.litellm-product.svc.cluster.local"
    PROXY_DEPLOY="deploy/litellm-proxy"
    ;;
  *) echo "usage: $0 [--summary|--sample|--vkey <her>] [_] [aliyun|198]" >&2; exit 2 ;;
esac

REDIS_PORT="6379"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
JMS="$REPO_ROOT/scripts/jms"

kctl() {
  if [[ "$CLUSTER" == "aliyun" ]]; then kubectl "$@"
  else "$JMS" ssh AIYJY-litellm "kubectl $*"
  fi
}

redis_cli() {
  if [[ "$CLUSTER" == "aliyun" ]]; then
    kubectl exec -n "$NS" "$REDIS_POD" -- redis-cli "$@"
  else
    "$JMS" ssh AIYJY-litellm "kubectl exec -n ${NS} ${REDIS_POD} -- redis-cli $*"
  fi
}

echo "=== 1) Redis ping (cluster=${CLUSTER}, ns=${NS}) ==="
if [[ "$CLUSTER" == "aliyun" ]]; then
  PING=$(kubectl exec -n "$NS" "$PROXY_DEPLOY" -c litellm -- python3 -c "
import redis
r = redis.Redis(host='${REDIS_HOST}', port=${REDIS_PORT}, socket_connect_timeout=3)
print('PING:', r.ping())
print('keys:', r.dbsize())
" 2>&1 || echo "FAIL")
else
  PING=$("$JMS" ssh AIYJY-litellm "kubectl exec -n ${NS} ${REDIS_POD} -- redis-cli ping; kubectl exec -n ${NS} ${REDIS_POD} -- redis-cli dbsize" 2>&1)
fi
echo "$PING"
echo

echo "=== 2) DualCache prefix 统计 ==="
for PREFIX in "deployment_affinity:v1:*" "prompt_caching:*" "router_*" "*"; do
  CNT=$(redis_cli --scan --pattern "$PREFIX" 2>/dev/null | wc -l | tr -d ' ')
  echo "  ${PREFIX}: ${CNT} keys"
done
echo

if [[ "$MODE" == "--sample" ]]; then
  echo "=== 3) 抽样 5 条 deployment_affinity mapping ==="
  KEYS=$(redis_cli --scan --pattern "deployment_affinity:v1:*" 2>/dev/null | head -5)
  if [[ -z "$KEYS" ]]; then
    echo "  (no deployment_affinity keys yet)"
  else
    while IFS= read -r K; do
      [[ -z "$K" ]] && continue
      VAL=$(redis_cli get "$K" 2>/dev/null | head -1)
      TTL=$(redis_cli ttl "$K" 2>/dev/null | head -1)
      echo "  $K"
      echo "    val=$VAL  ttl=${TTL}s"
    done <<< "$KEYS"
  fi
elif [[ "$MODE" == "--vkey" ]]; then
  if [[ -z "$HER_NAME" ]]; then echo "usage: $0 --vkey <her-name> [aliyun|198]" >&2; exit 2; fi
  if [[ "$CLUSTER" != "aliyun" ]]; then
    echo "  (--vkey only supported for aliyun cluster; 198 has no her-* pods)" >&2
    exit 2
  fi
  POD=$(kubectl get pod -n carher -l user-id=${HER_NAME#her-} -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
  if [[ -z "$POD" ]]; then echo "  ✗ carher Pod for ${HER_NAME} not found" >&2; exit 3; fi
  VK=$(kubectl exec -n carher "$POD" -c carher -- sh -c "python3 -c \"import json; print(json.load(open('/data/.openclaw/openclaw.json'))['models']['providers']['litellm']['apiKey'])\"" 2>&1)
  HASH=$(echo -n "$VK" | sha256sum | awk '{print $1}')
  echo "=== ${HER_NAME} 的 sticky mapping ==="
  echo "  vkey: ${VK:0:18}..."
  echo "  sha256 hash: ${HASH:0:16}..."
  KEYS=$(redis_cli --scan --pattern "deployment_affinity:v1:user_key:${HASH}:*" 2>/dev/null)
  if [[ -z "$KEYS" ]]; then
    echo "  (no mapping found; vkey 还没发过请求或 TTL 已过期)"
  else
    while IFS= read -r K; do
      [[ -z "$K" ]] && continue
      VAL=$(redis_cli get "$K" 2>/dev/null | head -1)
      TTL=$(redis_cli ttl "$K" 2>/dev/null | head -1)
      MODEL_GROUP=$(echo "$K" | awk -F: '{print $NF}')
      echo "  ${MODEL_GROUP}: ${VAL} (ttl=${TTL}s)"
    done <<< "$KEYS"
  fi
fi
