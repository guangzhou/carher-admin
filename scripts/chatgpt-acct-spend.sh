#!/usr/bin/env bash
# chatgpt-acct-spend.sh — ChatGPT Pro 账户池下游消费分析（198 + 阿里云双源）
#
# 数据源分布（2026-06-09 acct-2/15/17 从 MY 迁入 187 后）：
#   198 (AIYJY-litellm / litellm-product)  : acct-1~6,12~17,22~25 → 团队 IDE / Codex
#   阿里云 (carher namespace / litellm-product)  : acct-7~11,18~21 → carher bot
#
# model_id 格式差异（SQL 已兼容两种）：
#   198 :     chatgpt-acct-N-<model>     (用 '-' 分隔)
#   阿里云 :   chatgpt-acct-N/<model>     (用 '/' 分隔)
#
# 用法：
#   ./scripts/chatgpt-acct-spend.sh                  # 默认 both，近 7d
#   ./scripts/chatgpt-acct-spend.sh both 2h
#   ./scripts/chatgpt-acct-spend.sh prod 24h         # 仅 198 prod
#   ./scripts/chatgpt-acct-spend.sh aliyun 2h        # 仅阿里云
#   ./scripts/chatgpt-acct-spend.sh dev 7d           # 仅 198 dev
#   ./scripts/chatgpt-acct-spend.sh both 7d --raw    # 额外输出 raw model_id 明细
#
# 输出 protocol（per memory feedback-chatgpt-quota-minimal-output）：
#   跑完后 stdout verbatim 复制到 assistant text 代码块（fast UI 下 Bash result 不显示）。

set -euo pipefail

ENV="${1:-both}"
WINDOW="${2:-7d}"
RAW="${3:-}"

case "$ENV" in
  prod|dev|aliyun|both) ;;
  *) echo "usage: $0 [prod|dev|aliyun|both] [Nh|Nd] [--raw]" >&2; exit 2 ;;
esac

case "$WINDOW" in
  *m) INTERVAL="${WINDOW%m} minutes" ;;
  *h) INTERVAL="${WINDOW%h} hours" ;;
  *d) INTERVAL="${WINDOW%d} days" ;;
  *)  echo "window 必须是 Nm / Nh / Nd，例如 15m / 24h / 7d" >&2; exit 2 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
JMS="$REPO_ROOT/scripts/jms"

# ── SQL 模板（兼容两种 model_id 格式）────────────────────────────────
# acct: 198 用 '-' 拆 / 阿里云用 '/' 拆，CASE 区分
# model: 同理
ACCT_EXPR="CASE WHEN model_id LIKE '%/%' THEN REPLACE(SPLIT_PART(model_id, '/', 1), 'chatgpt-', '') ELSE SPLIT_PART(model_id, '-', 2) || '-' || SPLIT_PART(model_id, '-', 3) END"
MODEL_EXPR="CASE WHEN model_id LIKE '%/%' THEN SPLIT_PART(model_id, '/', 2) ELSE REGEXP_REPLACE(model_id, '^chatgpt-acct-[0-9]+-', '') END"

# acct 范围生成 (label用)：根据数据源给一个期望的 acct 列表
# - 198: acct-1,2~6,12~17 (acct-1 legacy)
# - 阿里云: acct-7~11,18~21
gen_acct_range() {
  local src="$1"
  case "$src" in
    198) echo "ARRAY[1,2,3,4,5,6,12,13,14,15,16,17,22,23,24,25]" ;;
    aliyun) echo "ARRAY[7,8,9,10,11,18,19,20,21]" ;;
  esac
}

# ── 构造 SQL ─────────────────────────────────────────────────────────
build_sql() {
  local src="$1"  # "198" or "aliyun"
  local label="$2"
  local acct_arr; acct_arr="$(gen_acct_range "$src")"

  cat <<SQL
\set QUIET on
\pset border 2
\pset null '∅'
\unset QUIET

\echo
\echo === [$label] 按账号聚合（近 ${WINDOW}）===
SELECT
  acct,
  calls,
  spend_usd,
  prompt_tok,
  completion_tok,
  CASE WHEN calls IS NULL THEN '∅ no data'
       WHEN calls = 0 THEN '❌ no traffic'
       WHEN calls < 5 THEN '⚠️  very low (token_invalidated?)'
       ELSE '✅' END AS health
FROM (
  SELECT 'acct-' || g::text AS acct FROM unnest(${acct_arr}) g
) accounts
LEFT JOIN (
  SELECT
    ${ACCT_EXPR} AS acct,
    COUNT(*) AS calls,
    ROUND(SUM(spend)::numeric, 2) AS spend_usd,
    SUM(prompt_tokens) AS prompt_tok,
    SUM(completion_tokens) AS completion_tok
  FROM "LiteLLM_SpendLogs"
  WHERE model_id LIKE 'chatgpt-acct-%'
    AND "startTime" > NOW() - INTERVAL '${INTERVAL}'
  GROUP BY 1
) agg USING (acct)
ORDER BY (CASE WHEN calls IS NULL THEN -1 ELSE calls END) DESC;

\echo
\echo === [$label] 按模型聚合（近 ${WINDOW}）===
SELECT
  ${MODEL_EXPR} AS model,
  COUNT(*) AS calls,
  ROUND(SUM(spend)::numeric, 2) AS spend_usd,
  SUM(prompt_tokens) AS prompt_tok,
  SUM(completion_tokens) AS completion_tok
FROM "LiteLLM_SpendLogs"
WHERE model_id LIKE 'chatgpt-acct-%'
  AND "startTime" > NOW() - INTERVAL '${INTERVAL}'
GROUP BY 1
ORDER BY calls DESC;

\echo
\echo === [$label] 总计（近 ${WINDOW}）===
SELECT
  COUNT(*) AS total_calls,
  ROUND(SUM(spend)::numeric, 2) AS total_spend_usd,
  SUM(prompt_tokens) AS total_prompt_tok,
  SUM(completion_tokens) AS total_completion_tok,
  COUNT(DISTINCT ${ACCT_EXPR}) AS active_accounts,
  MIN("startTime") AS earliest,
  MAX("startTime") AS latest
FROM "LiteLLM_SpendLogs"
WHERE model_id LIKE 'chatgpt-acct-%'
  AND "startTime" > NOW() - INTERVAL '${INTERVAL}';

\echo
\echo === [$label] 按账号 × 模型 pivot（近 ${WINDOW}）===
SELECT
  ${ACCT_EXPR} AS acct,
  SUM(CASE WHEN model_id LIKE '%gpt-5.5'             THEN 1 ELSE 0 END) AS calls_5_5,
  ROUND(SUM(CASE WHEN model_id LIKE '%gpt-5.5'             THEN spend ELSE 0 END)::numeric, 2) AS spend_5_5,
  SUM(CASE WHEN model_id LIKE '%gpt-5.4'             THEN 1 ELSE 0 END) AS calls_5_4,
  ROUND(SUM(CASE WHEN model_id LIKE '%gpt-5.4'             THEN spend ELSE 0 END)::numeric, 2) AS spend_5_4,
  SUM(CASE WHEN model_id LIKE '%gpt-5.3-codex'       AND model_id NOT LIKE '%spark%' THEN 1 ELSE 0 END) AS codex,
  ROUND(SUM(CASE WHEN model_id LIKE '%gpt-5.3-codex' AND model_id NOT LIKE '%spark%' THEN spend ELSE 0 END)::numeric, 2) AS sp_codex,
  SUM(CASE WHEN model_id LIKE '%gpt-5.3-codex-spark' THEN 1 ELSE 0 END) AS spark,
  ROUND(SUM(CASE WHEN model_id LIKE '%gpt-5.3-codex-spark' THEN spend ELSE 0 END)::numeric, 2) AS sp_spark,
  COUNT(*) AS total_calls,
  ROUND(SUM(spend)::numeric, 2) AS total_usd
FROM "LiteLLM_SpendLogs"
WHERE model_id LIKE 'chatgpt-acct-%'
  AND "startTime" > NOW() - INTERVAL '${INTERVAL}'
GROUP BY 1
ORDER BY total_usd DESC;
SQL

  if [[ "$RAW" == "--raw" ]]; then
    cat <<SQL

\echo
\echo === [$label] 原始 model_id 明细 ===
SELECT
  model_id,
  COUNT(*) AS calls,
  ROUND(SUM(spend)::numeric, 4) AS spend_usd,
  SUM(prompt_tokens) AS prompt_tok,
  SUM(completion_tokens) AS completion_tok
FROM "LiteLLM_SpendLogs"
WHERE model_id LIKE 'chatgpt-acct-%'
  AND "startTime" > NOW() - INTERVAL '${INTERVAL}'
GROUP BY 1
ORDER BY calls DESC;
SQL
  fi
}

# ── 执行 198 (jms ssh AIYJY-litellm) ──────────────────────────────────
run_198() {
  local ns="$1"   # litellm-product or litellm-dev
  local label="$2"
  local sql_local sql_remote
  sql_local="$(mktemp -t chatgpt-spend.XXXX.sql)"
  sql_remote="/tmp/$(basename "$sql_local")"
  trap "rm -f '$sql_local'" RETURN

  build_sql "198" "$label" > "$sql_local"

  if [[ ! -x "$JMS" ]]; then
    echo "找不到 $JMS（需 carher-admin/scripts/jms 包装器）" >&2
    return 1
  fi

  echo "[chatgpt-acct-spend] running 198 (ns=$ns, window=$WINDOW)..."
  "$JMS" scp "$sql_local" "AIYJY-litellm:$sql_remote" >/dev/null
  "$JMS" ssh AIYJY-litellm "kubectl cp $sql_remote $ns/litellm-db-0:$sql_remote \
    && kubectl exec -n $ns litellm-db-0 -- bash -c \
       'PGPASSWORD=\$POSTGRES_PASSWORD psql -U \$POSTGRES_USER -d \$POSTGRES_DB -f $sql_remote' \
    && rm -f $sql_remote"
}

# ── 执行 阿里云 (本地 kubectl) ────────────────────────────────────────
run_aliyun() {
  local label="$1"
  local sql_local sql_remote
  sql_local="$(mktemp -t chatgpt-spend-aliyun.XXXX.sql)"
  sql_remote="/tmp/$(basename "$sql_local")"
  trap "rm -f '$sql_local'" RETURN

  build_sql "aliyun" "$label" > "$sql_local"

  echo "[chatgpt-acct-spend] running aliyun (ns=carher, window=$WINDOW)..."

  # 拿 DATABASE_URL 解析出 user / db
  local db_url db_user db_name
  db_url=$(kubectl get secret litellm-secrets -n carher -o jsonpath='{.data.DATABASE_URL}' | base64 -d)
  db_user=$(echo "$db_url" | sed -E 's|.*://([^:]+):.*|\1|')
  db_name=$(echo "$db_url" | sed -E 's|.*/([^?]+).*|\1|')

  kubectl cp "$sql_local" "carher/litellm-db-0:$sql_remote" >/dev/null 2>&1
  kubectl exec -n carher litellm-db-0 -- psql -U "$db_user" -d "$db_name" -P pager=off -f "$sql_remote"
  kubectl exec -n carher litellm-db-0 -- rm -f "$sql_remote" >/dev/null 2>&1 || true
}

# ── 主流程 ───────────────────────────────────────────────────────────
case "$ENV" in
  prod)   run_198 "litellm-product" "198 prod (acct-1~6,12~17,22~25 团队 Codex)" ;;
  dev)    run_198 "litellm-dev"     "198 dev" ;;
  aliyun) run_aliyun "阿里云 carher (acct-7~11,18~21 carher bot)" ;;
  both)
    run_198 "litellm-product" "198 prod (acct-1~6,12~17,22~25 团队 Codex)"
    echo
    run_aliyun "阿里云 carher (acct-7~11,18~21 carher bot)"
    ;;
esac
