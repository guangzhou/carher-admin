#!/usr/bin/env bash
# litellm-198-max-input-tokens-audit.sh
#
# 背景：LiteLLM 入口的 _pre_call_checks 读 model_info.max_input_tokens。
# 该字段为空时，LiteLLM 会按 litellm_params.model 的 slug 去它**内置价格表**
# 里匹配一个值继承下来。zerokey 自建池的 slug（openai/gpt-5.6-terra 等）恰好
# 命中内置表的 1,050,000，于是长会话在入口被 400 掉，而我们从没配过这个数。
#
# 这个脚本把"没配就静默继承"变成一件会告警的事：
#   任何一行 zerokey 支撑的 deployment，如果 max_input_tokens 为空 → 退出码 1。
#
# 用法（在 198 上，或任何能 kubectl 到 litellm-product 的地方）：
#   ./litellm-198-max-input-tokens-audit.sh              # 审计 zerokey-cursor%
#   ./litellm-198-max-input-tokens-audit.sh 'zerokey-%'  # 自定义 id 前缀
#
# 退出码： 0=全部已显式配置   1=存在未配置行   2=环境/连接错误
set -uo pipefail

NS="${NS:-litellm-product}"
DB_POD="${DB_POD:-litellm-db-0}"
PG_USER="${PG_USER:-litellm}"
PG_DB="${PG_DB:-litellm}"
PATTERN="${1:-zerokey-cursor%}"

# KUBECTL 可以是多词（例如 "sudo k3s kubectl"），所以只在调用方没显式给时才自动探测
if [[ -z "${KUBECTL:-}" ]]; then
  if command -v kubectl >/dev/null 2>&1; then KUBECTL="kubectl"; else KUBECTL="k3s kubectl"; fi
fi

sql() {
  # 走 base64 传 SQL，避开引号地狱（见 memory: SQL 走 base64 避引号地狱）
  local b64
  b64=$(printf '%s' "$1" | base64 | tr -d '\n')
  $KUBECTL exec -n "$NS" "$DB_POD" -- bash -c \
    "echo $b64 | base64 -d | psql -U $PG_USER -d $PG_DB -tAF'|' -v ON_ERROR_STOP=1" 2>/dev/null
}

Q="SELECT model_info->>'id',
          model_name,
          COALESCE(model_info->>'max_input_tokens','')
     FROM \"LiteLLM_ProxyModelTable\"
    WHERE model_info->>'id' LIKE '${PATTERN}'
    ORDER BY 1;"

ROWS=$(sql "$Q")
if [[ -z "$ROWS" ]]; then
  echo "AUDIT ERROR: 查不到任何匹配 '${PATTERN}' 的行（连接失败或前缀写错）" >&2
  exit 2
fi

total=0; missing=0
while IFS='|' read -r id name mit; do
  [[ -z "$id" ]] && continue
  total=$((total+1))
  if [[ -z "$mit" ]]; then
    missing=$((missing+1))
    printf 'MISSING  %-40s %-32s max_input_tokens=<未配置，将从内置价格表静默继承>\n' "$id" "$name"
  fi
done <<< "$ROWS"

echo "----"
echo "AUDIT pattern=${PATTERN}  total=${total}  missing=${missing}"

if (( missing > 0 )); then
  cat >&2 <<EOF

修复方式：
  UPDATE "LiteLLM_ProxyModelTable"
     SET model_info = model_info || '{"max_input_tokens": 10000000}'::jsonb,
         updated_at = now(), updated_by = 'mit-audit'
   WHERE model_info->>'id' LIKE '${PATTERN}'
     AND model_info->>'max_input_tokens' IS NULL;
改完必须 rollout restart deploy/litellm-proxy 并做 scoped-key 回归（基准线 cursor-web-fc-82-terra）。
EOF
  exit 1
fi

echo "AUDIT OK: 全部行都显式配置了 max_input_tokens，无静默继承。"
exit 0
