#!/usr/bin/env bash
# litellm-vkey-acct-mapping.sh — 查 217 her vkey 实际 sticky 到哪个 chatgpt acct
#
# 数据源: 阿里云 carher litellm-db.LiteLLM_SpendLogs
# 原理: SpendLogs 每条 row 含 model_id=chatgpt-acct-N/chatgpt-gpt-5.5 +
#       metadata.user_api_key_alias=carher-<id>。按 vkey alias 分组聚合,
#       看每个 vkey 用过的 acct 列表。理想 sticky 生效 → 每 vkey 用 1 个 acct。
#
# 用法:
#   ./scripts/litellm-vkey-acct-mapping.sh         # 默认近 1h
#   ./scripts/litellm-vkey-acct-mapping.sh 2h
#   ./scripts/litellm-vkey-acct-mapping.sh 30m
#   ./scripts/litellm-vkey-acct-mapping.sh 24h --raw    # 含 vkey 详细列表
#
# 输出 3 张表:
#   1. 健康度摘要 (sticky 生效率 = 每 vkey 用 1 acct 的比例)
#   2. per-acct vkey 数 (看 5 acct 分布是否均匀)
#   3. 异常 vkey 清单 (用了 ≥2 acct, sticky 跨 TTL 切换或失败)
#
# Verbatim 输出 protocol: stdout 必须 verbatim 复制到 assistant text
# (memory feedback-chatgpt-quota-minimal-output)

set -euo pipefail

WINDOW="${1:-1h}"
CLUSTER="${2:-aliyun}"     # aliyun | 198
RAW="${3:-}"

case "$WINDOW" in
  *h) INTERVAL="${WINDOW%h} hours" ;;
  *m) INTERVAL="${WINDOW%m} minutes" ;;
  *d) INTERVAL="${WINDOW%d} days" ;;
  *) echo "usage: $0 [Nh|Nm|Nd] [aliyun|198] [--raw]" >&2; exit 2 ;;
esac

case "$CLUSTER" in
  aliyun) NS="carher" ;;
  198)    NS="litellm-product" ;;
  *) echo "usage: $0 [Nh|Nm|Nd] [aliyun|198] [--raw]" >&2; exit 2 ;;
esac

# kctl wrapper
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
JMS="$REPO_ROOT/scripts/jms"
kctl() {
  if [[ "$CLUSTER" == "aliyun" ]]; then kubectl "$@"
  else "$JMS" ssh AIYJY-litellm "kubectl $*"
  fi
}

# model_id 格式: aliyun 'chatgpt-acct-N/...' / 198 'chatgpt-acct-N-...'
if [[ "$CLUSTER" == "aliyun" ]]; then
  ACCT_REGEX="REGEXP_REPLACE(model_id, 'chatgpt-acct-([0-9]+)/.*', 'acct-\1')"
  MODEL_LIKE="model_id LIKE 'chatgpt-acct-%/%'"
else
  ACCT_REGEX="REGEXP_REPLACE(model_id, 'chatgpt-acct-([0-9]+)-.*', 'acct-\1')"
  MODEL_LIKE="model_id LIKE 'chatgpt-acct-%-%'"
fi

DB_URL=$(kctl get secret litellm-secrets -n "$NS" -o jsonpath='{.data.DATABASE_URL}' | base64 -d)
USER=$(echo "$DB_URL" | sed -E 's|.*://([^:]+):.*|\1|')
DB=$(echo "$DB_URL" | sed -E 's|.*/([^?]+).*|\1|')

cat > /tmp/vkey-acct.sql <<SQL
\set QUIET on
\pset border 2
\pset null '∅'
\unset QUIET

-- TEMP TABLE 跨 SELECT 共享 (CTE 只在单 SELECT 有效)
DROP TABLE IF EXISTS tmp_vkey_summary;
CREATE TEMP TABLE tmp_vkey_summary AS
SELECT
  metadata->>'user_api_key_alias' AS vkey_alias,
  COUNT(*) AS calls,
  COUNT(DISTINCT ${ACCT_REGEX}) AS distinct_accts,
  STRING_AGG(DISTINCT ${ACCT_REGEX}, ',') AS accts_used,
  MODE() WITHIN GROUP (ORDER BY ${ACCT_REGEX}) AS primary_acct
FROM "LiteLLM_SpendLogs"
WHERE "startTime" > NOW() - INTERVAL '${INTERVAL}'
  AND ${MODEL_LIKE}
  AND metadata->>'user_api_key_alias' IS NOT NULL
GROUP BY 1;

\echo
\echo === [近 ${WINDOW}] Sticky 健康度摘要 ===
\echo "  注: TTL=600s, 长时间窗口 (1h+) 内 vkey 自然过期重 LB,"
\echo "  partial/broken 不一定是故障，更可能是稀疏调用 + 多次 TTL 过期"
SELECT
  COUNT(*) AS total_active_vkeys,
  COUNT(*) FILTER (WHERE distinct_accts = 1) AS sticky_ok,
  COUNT(*) FILTER (WHERE distinct_accts = 2) AS sticky_partial,
  COUNT(*) FILTER (WHERE distinct_accts >= 3) AS sticky_broken,
  ROUND(100.0 * COUNT(*) FILTER (WHERE distinct_accts = 1) / NULLIF(COUNT(*),0), 1) AS sticky_pct
FROM tmp_vkey_summary;

\echo
\echo === [近 ${WINDOW}] per-acct her 实例数分布 (按 primary_acct = MODE) ===
SELECT
  primary_acct AS acct,
  COUNT(*) AS her_count,
  ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct
FROM tmp_vkey_summary
WHERE primary_acct IS NOT NULL
GROUP BY 1
ORDER BY 1;

\echo
\echo === [近 ${WINDOW}] 异常 vkey 清单 (用 ≥3 acct, 高优排查) ===
SELECT
  vkey_alias,
  calls,
  distinct_accts,
  accts_used
FROM tmp_vkey_summary
WHERE distinct_accts >= 3
ORDER BY distinct_accts DESC, calls DESC
LIMIT 30;

DROP TABLE IF EXISTS tmp_vkey_summary;
SQL

if [[ "$RAW" == "--raw" ]]; then
cat >> /tmp/vkey-acct.sql <<SQL

\echo
\echo === RAW: 全部 vkey × acct 用量 (--raw) ===
SELECT
  metadata->>'user_api_key_alias' AS vkey_alias,
  ${ACCT_REGEX} AS acct,
  COUNT(*) AS calls
FROM "LiteLLM_SpendLogs"
WHERE "startTime" > NOW() - INTERVAL '${INTERVAL}'
  AND ${MODEL_LIKE}
  AND metadata->>'user_api_key_alias' IS NOT NULL
GROUP BY 1, 2
ORDER BY 1, 3 DESC;
SQL
fi

if [[ "$CLUSTER" == "aliyun" ]]; then
  kubectl cp /tmp/vkey-acct.sql ${NS}/litellm-db-0:/tmp/vkey-acct.sql 2>/dev/null
  kubectl exec -n "$NS" litellm-db-0 -- psql -U "$USER" -d "$DB" -P pager=off -f /tmp/vkey-acct.sql
  kubectl exec -n "$NS" litellm-db-0 -- rm -f /tmp/vkey-acct.sql >/dev/null 2>&1 || true
else
  "$JMS" scp /tmp/vkey-acct.sql AIYJY-litellm:/tmp/vkey-acct.sql >/dev/null
  "$JMS" ssh AIYJY-litellm "kubectl cp /tmp/vkey-acct.sql ${NS}/litellm-db-0:/tmp/vkey-acct.sql 2>/dev/null && kubectl exec -n ${NS} litellm-db-0 -- psql -U ${USER} -d ${DB} -P pager=off -f /tmp/vkey-acct.sql && kubectl exec -n ${NS} litellm-db-0 -- rm -f /tmp/vkey-acct.sql"
fi
