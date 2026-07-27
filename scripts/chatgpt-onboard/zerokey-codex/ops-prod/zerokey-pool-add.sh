#!/usr/bin/env bash
# zerokey-pool-add.sh — 批量将 225 上健康的 zero-N pods 注册到 198 litellm 轮询池
#
# 用法:
#   bash zerokey-pool-add.sh              # 自动发现所有未注册的 zero-N
#   bash zerokey-pool-add.sh 1 27 28 56   # 只加指定编号
#   bash zerokey-pool-add.sh --dry-run    # 干跑，不实际注册
#
# 前提:
#   - 198 K3s 上已有 zero-N Deployment + Service (namespace litellm-product)
#   - 从本地 Mac 跑，需要 sshpass

set -uo pipefail

LITELLM_HOST="10.68.13.198"
LITELLM_PORT="30402"
# Secrets come from the environment or the local gitignored .carher-secrets.json
# (see scripts/lib/carher-secrets-init.sh). They used to be hardcoded here: two
# production sudo passwords and a LiteLLM master key, in a tracked file.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../lib" && pwd)/carher-secrets.sh"
LITELLM_MK="$(carher_secret LITELLM_MASTER_KEY)"
K8S_HOST="10.68.13.198"
K8S_PASS="$(carher_secret SUDO_PW)"
ZK_HOST="10.68.13.225"
ZK_PASS="$(carher_secret ZK_SUDO_PW)"

MODEL_NAME="chatgpt-gpt-5.5"
MODEL_ID="openai/gpt-5-5"
INPUT_COST="5e-06"
OUTPUT_COST="3e-05"
RPM=30
MODE="responses"

DRY_RUN=false
TARGETS=()

for arg in "$@"; do
    if [ "$arg" = "--dry-run" ]; then
        DRY_RUN=true
    else
        TARGETS+=("$arg")
    fi
done

ssh198() {
    sshpass -p "$K8S_PASS" ssh -o StrictHostKeyChecking=no -o PubkeyAuthentication=no \
        -o PreferredAuthentications=password cltx@"$K8S_HOST" "$@"
}

ssh225() {
    sshpass -p "$ZK_PASS" ssh -o StrictHostKeyChecking=no cltx@"$ZK_HOST" "$@"
}

# Step 1: Get already-registered IDs
echo "=== Step 1: Querying litellm pool ==="
REGISTERED=$(curl -s --max-time 10 "http://${LITELLM_HOST}:${LITELLM_PORT}/model/info" \
    -H "Authorization: Bearer ${LITELLM_MK}" 2>/dev/null | \
    python3 -c "
import json, sys
d = json.load(sys.stdin)
ids = set()
for m in d.get('data', []):
    mid = m.get('model_info',{}).get('id','')
    if mid.startswith('zero-'):
        ids.add(mid)
print(' '.join(sorted(ids)))
" 2>/dev/null)
echo "  Already in pool: $REGISTERED"

# Step 2: Get all running zero-N pods on 225
echo "=== Step 2: Querying running pods ==="
RUNNING=$(ssh198 "echo $K8S_PASS | sudo -S kubectl get pods -n litellm-product -l pool=zerokey-codex --no-headers 2>/dev/null | grep '1/1.*Running' | awk '{print \$1}' | sed 's/-[a-z0-9]*-[a-z0-9]*$//' | sort" 2>/dev/null)
echo "  Running pods: $(echo $RUNNING | tr '\n' ' ')"

# Step 3: Determine candidates
echo "=== Step 3: Selecting candidates ==="
CANDIDATES=()
for pod in $RUNNING; do
    num=$(echo "$pod" | sed 's/zero-//')
    # Skip if already registered
    if echo " $REGISTERED " | grep -q " $pod "; then
        continue
    fi
    # If specific targets given, filter
    if [ ${#TARGETS[@]} -gt 0 ]; then
        found=false
        for t in "${TARGETS[@]}"; do
            if [ "$t" = "$num" ]; then found=true; break; fi
        done
        if ! $found; then continue; fi
    fi
    CANDIDATES+=("$num")
done

if [ ${#CANDIDATES[@]} -eq 0 ]; then
    echo "  No candidates to add."
    exit 0
fi
echo "  Candidates: ${CANDIDATES[*]}"

# Step 4: Health check each candidate via hostPort on 225
echo "=== Step 4: Health + functional check ==="
HEALTHY=()
for num in "${CANDIDATES[@]}"; do
    # Get hostPort from deployment (avoid nested single-quote issues)
    port=$(ssh198 "echo $K8S_PASS | sudo -S kubectl get deploy zero-${num} -n litellm-product -o json 2>/dev/null" 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['spec']['template']['spec']['containers'][0]['ports'][0]['hostPort'])" 2>/dev/null)
    if [ -z "$port" ] || [ "$port" = "None" ]; then
        echo "  zero-${num}: SKIP (no hostPort found)"
        continue
    fi

    health=$(ssh225 "curl -s -o /dev/null -w '%{http_code}' --connect-timeout 3 http://127.0.0.1:${port}/health" 2>/dev/null)
    if [ "$health" != "200" ]; then
        echo "  zero-${num} (port ${port}): SKIP (health=$health)"
        continue
    fi

    # DNS reachability from litellm pod (more reliable than hostPort SSE test)
    dns_ok=$(ssh198 "echo $K8S_PASS | sudo -S kubectl exec -n litellm-product \$(kubectl get pods -n litellm-product -l app=litellm-proxy --no-headers 2>/dev/null | grep Running | head -1 | awk '{print \$1}') -- python3 -c \"import urllib.request; r=urllib.request.urlopen('http://zero-${num}.litellm-product.svc.cluster.local:8200/health',timeout=5); print(r.getcode())\" 2>/dev/null" 2>/dev/null)
    if [ "${dns_ok:-0}" != "200" ]; then
        echo "  zero-${num} (port ${port}): SKIP (DNS unreachable from litellm pod)"
        continue
    fi

    echo "  zero-${num} (port ${port}): HEALTHY"
    HEALTHY+=("$num")
done

if [ ${#HEALTHY[@]} -eq 0 ]; then
    echo "  No healthy candidates."
    exit 0
fi
echo "  Healthy: ${HEALTHY[*]}"

if $DRY_RUN; then
    echo "=== DRY RUN — would register ${#HEALTHY[@]} pods ==="
    for num in "${HEALTHY[@]}"; do
        echo "  zero-${num} → POST /model/new (model_info.id=zero-${num})"
    done
    exit 0
fi

# Step 5: Register each in litellm
echo "=== Step 5: Registering in litellm ==="
ADDED=()
FAILED=()
for num in "${HEALTHY[@]}"; do
    api_base="http://zero-${num}.litellm-product.svc.cluster.local:8200/v1"

    result=$(curl -s --max-time 10 -X POST "http://${LITELLM_HOST}:${LITELLM_PORT}/model/new" \
        -H "Authorization: Bearer ${LITELLM_MK}" \
        -H "Content-Type: application/json" \
        -d "{
            \"model_name\": \"${MODEL_NAME}\",
            \"litellm_params\": {
                \"model\": \"${MODEL_ID}\",
                \"api_base\": \"${api_base}\",
                \"input_cost_per_token\": ${INPUT_COST},
                \"output_cost_per_token\": ${OUTPUT_COST},
                \"rpm\": ${RPM}
            },
            \"model_info\": {
                \"id\": \"zero-${num}\",
                \"mode\": \"${MODE}\"
            }
        }" 2>/dev/null)

    model_id=$(echo "$result" | python3 -c "import json,sys; print(json.load(sys.stdin).get('model_id',''))" 2>/dev/null)
    if [ "$model_id" = "zero-${num}" ]; then
        echo "  zero-${num}: REGISTERED"
        ADDED+=("$num")
    else
        echo "  zero-${num}: FAILED ($result)"
        FAILED+=("$num")
    fi
done

# Step 6: Verify DNS reachability from litellm pod
echo "=== Step 6: Verifying DNS from litellm pod ==="
LITELLM_POD=$(ssh198 "echo $K8S_PASS | sudo -S kubectl get pods -n litellm-product -l app=litellm-proxy --no-headers 2>/dev/null | grep Running | head -1 | awk '{print \$1}'" 2>/dev/null)
echo "  litellm pod: $LITELLM_POD"

VERIFIED=()
for num in "${ADDED[@]}"; do
    dns_ok=$(ssh198 "echo $K8S_PASS | sudo -S kubectl exec -n litellm-product $LITELLM_POD -- python3 -c \"
import urllib.request
r = urllib.request.urlopen('http://zero-${num}.litellm-product.svc.cluster.local:8200/health', timeout=5)
print(r.getcode())
\" 2>/dev/null" 2>/dev/null)
    if [ "${dns_ok:-0}" = "200" ]; then
        echo "  zero-${num}: DNS OK (200)"
        VERIFIED+=("$num")
    else
        echo "  zero-${num}: DNS FAIL"
    fi
done

# Summary
echo ""
echo "=========================================="
echo "SUMMARY"
echo "  Registered: ${#ADDED[@]}  (${ADDED[*]:-none})"
echo "  DNS verified: ${#VERIFIED[@]}  (${VERIFIED[*]:-none})"
echo "  Failed: ${#FAILED[@]}  (${FAILED[*]:-none})"
echo "=========================================="
