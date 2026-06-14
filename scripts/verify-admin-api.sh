#!/bin/bash
# verify-admin-api.sh — 198 prod LiteLLM admin API 5 项验证
#
# 已在 2026-05-17 实测通过，零线上影响（用唯一 model_name `chatgpt-noop-verify-*`，
# 无任何 cursor/carher key 路由到这个 model_name）。
#
# 用法：./scripts/verify-admin-api.sh
#
# 验证项：
#   T1: POST /model/new        (创建 deployment)
#   T2: PATCH rpm=50           (硬限速)
#   T3: PATCH rpm=999999       (HEALTHY 档)
#   T4: PATCH rpm=0            (OFFLINE 档)
#   T5: POST /model/delete     (清理)
#
# 关键发现（已写入文档 §3.5.1 / §5.1）：
#   - PATCH rpm=null 不工作 (保留上次值)，HEALTHY 档必须用 999999
#   - GET /model/info 与 DB 不同步，监控查 DB 不查 GET API

set -euo pipefail

LB="http://localhost:30402"
TS=$(date +%s)
TID="chatgpt-noop-verify-id-${TS}"
MNAME="chatgpt-noop-verify-${TS}"

ok()   { echo "  ✅ $1"; }
fail() { echo "  ❌ $1"; FAILED=1; }
FAILED=0

# 必须从 jms 跳板机执行（直连 198 内网）
if ! command -v jms >/dev/null 2>&1; then
  echo "ERROR: jms CLI not found. 本脚本必须在能访问 198 prod 的环境执行（通常本地 Mac）"
  exit 1
fi

MK=$(jms ssh AIYJY-litellm "kubectl get secret litellm-secrets -n litellm-product \
  -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d")
[[ -z "$MK" ]] && { echo "ERROR: 拿不到 LITELLM_MASTER_KEY"; exit 1; }

echo "=== T1: POST /model/new — 创建测试 deployment（独立 model_name 零路由风险）==="
RESP=$(jms ssh AIYJY-litellm "curl -sS -w '\\n|HTTP_%{http_code}' -X POST $LB/model/new \
  -H 'Authorization: Bearer $MK' -H 'Content-Type: application/json' \
  -d '{
    \"model_name\": \"$MNAME\",
    \"litellm_params\": {
      \"model\": \"openai/chatgpt-gpt-5.5\",
      \"api_base\": \"http://10.68.13.188:9999\",
      \"api_key\": \"sk-noop\"
    },
    \"model_info\": {\"id\":\"$TID\",\"mode\":\"responses\"}
  }'")
echo "$RESP" | tail -1 | grep -q "HTTP_200" && ok "/model/new HTTP 200" || fail "/model/new failed"

echo ""
echo "=== T2: PATCH rpm=50 + 直查 DB 验证 ==="
jms ssh AIYJY-litellm "curl -sS -X PATCH $LB/model/$TID/update \
  -H 'Authorization: Bearer $MK' -H 'Content-Type: application/json' \
  -d '{\"model_id\":\"$TID\",\"litellm_params\":{\"rpm\":50}}'" >/dev/null
DB_RPM=$(jms ssh AIYJY-litellm "kubectl exec -n litellm-product litellm-db-0 -- \
  psql -U litellm -d litellm -t -c \
  \"SELECT litellm_params->'rpm' FROM \\\"LiteLLM_ProxyModelTable\\\" WHERE model_info->>'id'='$TID';\"" | xargs)
[[ "$DB_RPM" == "50" ]] && ok "DB rpm=50" || fail "DB rpm=$DB_RPM (期望 50)"

echo ""
echo "=== T3: PATCH rpm=999999 (HEALTHY 档) ==="
jms ssh AIYJY-litellm "curl -sS -X PATCH $LB/model/$TID/update \
  -H 'Authorization: Bearer $MK' -H 'Content-Type: application/json' \
  -d '{\"model_id\":\"$TID\",\"litellm_params\":{\"rpm\":999999}}'" >/dev/null
DB_RPM=$(jms ssh AIYJY-litellm "kubectl exec -n litellm-product litellm-db-0 -- \
  psql -U litellm -d litellm -t -c \
  \"SELECT litellm_params->'rpm' FROM \\\"LiteLLM_ProxyModelTable\\\" WHERE model_info->>'id'='$TID';\"" | xargs)
[[ "$DB_RPM" == "999999" ]] && ok "DB rpm=999999" || fail "DB rpm=$DB_RPM (期望 999999)"

echo ""
echo "=== T4: PATCH rpm=0 (OFFLINE 档) ==="
jms ssh AIYJY-litellm "curl -sS -X PATCH $LB/model/$TID/update \
  -H 'Authorization: Bearer $MK' -H 'Content-Type: application/json' \
  -d '{\"model_id\":\"$TID\",\"litellm_params\":{\"rpm\":0}}'" >/dev/null
DB_RPM=$(jms ssh AIYJY-litellm "kubectl exec -n litellm-product litellm-db-0 -- \
  psql -U litellm -d litellm -t -c \
  \"SELECT litellm_params->'rpm' FROM \\\"LiteLLM_ProxyModelTable\\\" WHERE model_info->>'id'='$TID';\"" | xargs)
[[ "$DB_RPM" == "0" ]] && ok "DB rpm=0 (OFFLINE)" || fail "DB rpm=$DB_RPM (期望 0)"

echo ""
echo "=== T5: POST /model/delete — 清理测试 deployment ==="
RESP=$(jms ssh AIYJY-litellm "curl -sS -w '\\n|HTTP_%{http_code}' -X POST $LB/model/delete \
  -H 'Authorization: Bearer $MK' -H 'Content-Type: application/json' \
  -d '{\"id\":\"$TID\"}'")
echo "$RESP" | tail -1 | grep -q "HTTP_200" && ok "/model/delete HTTP 200" || fail "/model/delete failed"

ROWS=$(jms ssh AIYJY-litellm "kubectl exec -n litellm-product litellm-db-0 -- \
  psql -U litellm -d litellm -t -c \
  \"SELECT count(*) FROM \\\"LiteLLM_ProxyModelTable\\\" WHERE model_info->>'id'='$TID';\"" | xargs)
[[ "$ROWS" == "0" ]] && ok "DB 已清理 (0 rows)" || fail "DB 仍残留 $ROWS 行"

echo ""
echo "=========================================="
[[ $FAILED -eq 0 ]] && {
  echo "🎉 全部通过 — admin API 五件套全可用"
  echo ""
  echo "✅ 可以推进 Day 0 ConfigMap → DB 迁移（scripts/migrate-cm-to-db.sh）"
} || {
  echo "💥 有失败项 — 调度方案需重新评估"
  echo ""
  echo "❌ 不能推进 Day 0；先调查失败原因（看 LiteLLM Pod logs / OpenAPI spec 变化）"
  exit 1
}
echo "=========================================="
