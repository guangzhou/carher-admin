#!/usr/bin/env bash
# litellm-sticky-verify.sh — 验证 her 实例 sticky 是否生效
#
# 给定 her name，连续发 N 个 chatgpt-gpt-5.5 请求，
# 看每次返回的 x-litellm-model-id (deployment id) 是否全部一致。
#
# 用法:
#   ./scripts/litellm-sticky-verify.sh her-1000              # 默认 5 次
#   ./scripts/litellm-sticky-verify.sh her-1000 10           # 10 次
#   ./scripts/litellm-sticky-verify.sh her-1000 5 gpt-5.4    # 自定义 model
#
# 输出:
#   - 5 次请求的 deployment id
#   - PASS (全 sticky) / FAIL (出现不同 deployment)

set -euo pipefail

HER="${1:-}"
COUNT="${2:-5}"
MODEL="${3:-chatgpt-gpt-5.5}"

if [[ -z "$HER" ]]; then
  echo "usage: $0 <her-name> [count=5] [model=chatgpt-gpt-5.5]" >&2
  echo "       e.g. $0 her-1000" >&2
  exit 2
fi

# 找 carher Pod
POD=$(kubectl get pod -n carher -l user-id=${HER#her-} -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [[ -z "$POD" ]]; then
  echo "  ✗ carher Pod for ${HER} not found (user-id=${HER#her-})" >&2
  exit 3
fi

# 拿 vkey
VK=$(kubectl exec -n carher "$POD" -c carher -- sh -c "python3 -c \"import json; print(json.load(open('/data/.openclaw/openclaw.json'))['models']['providers']['litellm']['apiKey'])\"" 2>&1 || echo "")
if [[ -z "$VK" || "$VK" != sk-* ]]; then
  echo "  ✗ cannot read vkey from ${POD}" >&2
  echo "$VK" >&2
  exit 4
fi

echo "[litellm-sticky-verify] her=${HER} pod=${POD} model=${MODEL} count=${COUNT}"
echo "  vkey: ${VK:0:18}..."
echo

declare ACCT_RECORD=""
RUNNER="curl-stick-verify-$$"
for i in $(seq 1 "$COUNT"); do
  POD_NAME="${RUNNER}-${i}"
  RES=$(kubectl run "$POD_NAME" -n carher --image=curlimages/curl:8.5.0 --restart=Never --rm -i --quiet -- \
    sh -c "curl -sS -X POST http://litellm-proxy.carher.svc:4000/v1/chat/completions \
      -H 'Authorization: Bearer $VK' -H 'Content-Type: application/json' \
      -d '{\"model\":\"$MODEL\",\"stream\":false,\"messages\":[{\"role\":\"user\",\"content\":\"verify $i\"}]}' \
      --max-time 25 -D /tmp/h 2>&1; grep -i 'x-litellm-model-id' /tmp/h 2>/dev/null" 2>&1)
  ACCT=$(echo "$RES" | grep -i "x-litellm-model-id" | head -1 | sed -E 's/.*x-litellm-model-id: //' | tr -d '\r' || echo "<unknown>")
  echo "  req $i: $ACCT"
  ACCT_RECORD="${ACCT_RECORD}${ACCT}"$'\n'
done

echo
echo "--- 分布 ---"
printf '%s' "$ACCT_RECORD" | sort | uniq -c | awk '{printf "  %s  → %s 次\n", substr($0, index($0,$2)), $1}'

UNIQ=$(printf '%s' "$ACCT_RECORD" | sort -u | grep -v '^$' | wc -l | tr -d ' ')
echo
if [[ "$UNIQ" == "1" ]]; then
  echo "✅ PASS: ${COUNT} 次请求全部 sticky 同一 deployment"
else
  echo "❌ FAIL: ${COUNT} 次请求路由到 ${UNIQ} 个不同 deployment, sticky 未生效"
  echo "  可能原因: Redis 连不通 / TTL=600s 过期 / 副本之间 cache 不同步"
  echo "  排查: ./scripts/litellm-redis-health.sh"
  exit 1
fi
