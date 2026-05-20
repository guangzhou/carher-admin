#!/usr/bin/env bash
# litellm-cache-hit-rate.sh — carher LiteLLM cache 命中率监控
#
# 数据源：阿里云 carher litellm-db.LiteLLM_DailyUserSpend（聚合表，含 cache 列）
#
# 重要背景（2026-05-20）：
#   - LiteLLM 1.85.0 vanilla 的 SpendLogsPayload TypedDict 没声明
#     cache_read_input_tokens / cache_creation_input_tokens，
#     所以 LiteLLM_SpendLogs per-request 表 cache 列永远 0
#   - 真实 cache 数据写到聚合表（DailyUserSpend / DailyTagSpend 等）
#   - ChatGPT (openai/chatgpt-*) cache 是服务端隐式的，LiteLLM 拿不到
#     这里 cache_read=0 不是问题，是数据源限制
#
# 用法：
#   ./scripts/litellm-cache-hit-rate.sh             # 默认今天
#   ./scripts/litellm-cache-hit-rate.sh 2026-05-19  # 指定日期
#   ./scripts/litellm-cache-hit-rate.sh 7d          # 近 7 天
#
# 输出 protocol（per memory feedback-chatgpt-quota-minimal-output）：
#   跑完后 stdout verbatim 复制到 assistant text 代码块。

set -euo pipefail

ARG="${1:-today}"

case "$ARG" in
  today)
    WHERE='date = (CURRENT_DATE)::text'
    SPENDLOGS_WHERE='"startTime" >= CURRENT_DATE'
    LABEL='today'
    ;;
  *d)
    DAYS="${ARG%d}"
    WHERE="date::date > CURRENT_DATE - INTERVAL '${DAYS} days'"
    SPENDLOGS_WHERE="\"startTime\" > NOW() - INTERVAL '${DAYS} days'"
    LABEL="last ${DAYS}d"
    ;;
  2*-*-*)
    WHERE="date = '${ARG}'"
    SPENDLOGS_WHERE="\"startTime\"::date = '${ARG}'"
    LABEL="$ARG"
    ;;
  *)
    echo "usage: $0 [today | Nd | YYYY-MM-DD]" >&2
    exit 2
    ;;
esac

DB_URL=$(kubectl get secret litellm-secrets -n carher -o jsonpath='{.data.DATABASE_URL}' | base64 -d)
USER=$(echo "$DB_URL" | sed -E 's|.*://([^:]+):.*|\1|')
DB=$(echo "$DB_URL" | sed -E 's|.*/([^?]+).*|\1|')

cat > /tmp/cache-hit-rate.sql <<SQL
\set QUIET on
\pset border 2
\pset null '∅'
\unset QUIET

\echo
\echo === [$LABEL] 按模型 cache 命中率（含真金白银 + ChatGPT 隐式）===
SELECT model,
       SUM(api_requests) AS calls,
       ROUND(SUM(prompt_tokens)::numeric / 1e6, 1) AS prompt_M,
       ROUND(SUM(cache_read_input_tokens)::numeric / 1e6, 1) AS cache_read_M,
       ROUND(SUM(cache_creation_input_tokens)::numeric / 1e6, 1) AS cache_write_M,
       ROUND(100.0 * SUM(cache_read_input_tokens)::numeric / NULLIF(SUM(prompt_tokens),0), 1) AS hit_pct,
       ROUND(SUM(spend)::numeric, 2) AS spend_usd,
       CASE
         WHEN model LIKE 'anthropic/%' THEN 'Anthropic (wangsu)'
         WHEN model LIKE 'openai/chatgpt-%' THEN 'ChatGPT 池 (订阅制, cache 隐式)'
         WHEN model LIKE 'openai/%' OR model LIKE 'custom_openai/%' THEN 'OpenAI/Wangsu (cache 不可观察)'
         WHEN model LIKE 'openrouter/%' THEN 'OpenRouter (cache 不可观察)'
         ELSE 'other'
       END AS provider
FROM "LiteLLM_DailyUserSpend"
WHERE $WHERE AND prompt_tokens > 10000
GROUP BY 1
ORDER BY 3 DESC LIMIT 15;

\echo
\echo === [$LABEL] Anthropic Claude 真实 cache 节省估算 ===
\echo "  (按官方 list 价: input \$3/MTok sonnet / \$5/MTok opus, cache_read 0.1x)"
SELECT model,
       ROUND(SUM(cache_read_input_tokens)::numeric / 1e6, 1) AS cache_read_M,
       ROUND(100.0 * SUM(cache_read_input_tokens)::numeric / NULLIF(SUM(prompt_tokens),0), 1) AS hit_pct,
       CASE
         WHEN model LIKE '%sonnet%' THEN ROUND(SUM(cache_read_input_tokens)::numeric / 1e6 * 3 * 0.9, 0)
         WHEN model LIKE '%haiku%'  THEN ROUND(SUM(cache_read_input_tokens)::numeric / 1e6 * 1 * 0.9, 0)
         WHEN model LIKE '%opus%'   THEN ROUND(SUM(cache_read_input_tokens)::numeric / 1e6 * 5 * 0.9, 0)
       END AS estimated_saved_usd
FROM "LiteLLM_DailyUserSpend"
WHERE $WHERE AND model LIKE 'anthropic/%' AND cache_read_input_tokens > 0
GROUP BY 1
ORDER BY 4 DESC;

\echo
\echo === [$LABEL] ChatGPT 池 5 acct 流量分布 (sticky 后应该看到 vkey hash 分布) ===
\echo "  (cache 数据不可观察，但流量分布能看出 sticky 是否生效)"
SELECT
  REGEXP_REPLACE(model_id, 'chatgpt-acct-([0-9]+)/.*', 'acct-\1') AS acct,
  COUNT(*) AS calls,
  ROUND(SUM(spend)::numeric, 2) AS spend_usd
FROM "LiteLLM_SpendLogs"
WHERE ${SPENDLOGS_WHERE} AND model_id LIKE 'chatgpt-acct-%/%'
GROUP BY 1
ORDER BY 1;
SQL

kubectl cp /tmp/cache-hit-rate.sql carher/litellm-db-0:/tmp/cache-hit-rate.sql 2>/dev/null
kubectl exec -n carher litellm-db-0 -- psql -U "$USER" -d "$DB" -P pager=off -f /tmp/cache-hit-rate.sql
kubectl exec -n carher litellm-db-0 -- rm -f /tmp/cache-hit-rate.sql >/dev/null 2>&1 || true
