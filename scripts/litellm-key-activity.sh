#!/usr/bin/env bash
# LiteLLM key activity profiler.
# Given a key_alias, dump a structured profile of what that key is doing:
# hourly cost split (CST), traffic class (IDE vs business agent vs other),
# tools used, failure pattern, cache effectiveness, sample event triggers,
# and Claude's actual decisions when acting as an agent backend.
#
# Usage:
#   ./scripts/litellm-key-activity.sh <key_alias> [env] [hours]
#     env:   pro (default) | dev
#     hours: lookback window in hours (default 24)
#
# Examples:
#   ./scripts/litellm-key-activity.sh claude-code-fangzhuang-m6z0
#   ./scripts/litellm-key-activity.sh claude-code-fangzhuang-m6z0 pro 48
#   ./scripts/litellm-key-activity.sh claude-code-cuifang-x3tq dev 6

set -euo pipefail

key_alias="${1:?usage: $0 <key_alias> [pro|dev] [hours]}"
env_name="${2:-pro}"
hours="${3:-24}"

case "$env_name" in
  pro|prod) ns="litellm-product" ;;
  dev)      ns="litellm-dev"     ;;
  *)        echo "env must be 'pro' or 'dev'" >&2; exit 2 ;;
esac

local_sql=$(mktemp -t litellm-key-activity-XXXXXX.sql)
remote_path="/tmp/$(basename "$local_sql")"
trap 'rm -f "$local_sql"' EXIT

cat > "$local_sql" <<SQL
\\pset format wrapped
\\pset border 1

\\set key_alias '${key_alias}'
\\set hours ${hours}

SELECT '== Key meta ==' AS section;
SELECT key_alias,
       blocked,
       ROUND(spend::numeric, 2) AS spend_total,
       max_budget,
       budget_duration,
       to_char(budget_reset_at + INTERVAL '8 hours', 'MM-DD HH24:MI CST') AS budget_reset_cst,
       to_char(created_at + INTERVAL '8 hours', 'YYYY-MM-DD HH24:MI CST') AS created_cst
  FROM "LiteLLM_VerificationToken"
 WHERE key_alias = :'key_alias';

SELECT format('== Last %s h: model summary ==', :hours) AS section;
SELECT model,
       COUNT(*) AS req,
       SUM(prompt_tokens) AS in_tok,
       SUM(completion_tokens) AS out_tok,
       ROUND(SUM(spend)::numeric, 4) AS spend_usd
  FROM "LiteLLM_SpendLogs"
 WHERE metadata->>'user_api_key_alias' = :'key_alias'
   AND "startTime" >= NOW() - (:hours || ' hours')::interval
 GROUP BY model
 ORDER BY spend_usd DESC NULLS LAST;

SELECT '== Traffic class (by system prompt fingerprint) ==' AS section;
SELECT CASE
         WHEN COALESCE(proxy_server_request->'system'->0->>'text',
                       proxy_server_request->>'system') ILIKE '%You are Claude Code%'
              OR COALESCE(proxy_server_request->'system'->0->>'text',
                          proxy_server_request->>'system') ILIKE '%You are an interactive%'
              OR COALESCE(proxy_server_request->'system'->0->>'text',
                          proxy_server_request->>'system') ILIKE '%cc_version=%'
              OR COALESCE(proxy_server_request->'system'->0->>'text',
                          proxy_server_request->>'system') ILIKE '%cc_entrypoint=cli%'
              THEN 'B. Claude Code IDE'
         WHEN COALESCE(proxy_server_request->'system'->0->>'text',
                       proxy_server_request->>'system') ILIKE 'You are Cursor%'
              OR COALESCE(proxy_server_request->'system'->0->>'text',
                          proxy_server_request->>'system') ILIKE '%cursor.com%'
              THEN 'B. Cursor IDE'
         WHEN proxy_server_request->'system' IS NULL
              THEN 'D. no-system (probe / count_tokens)'
         ELSE 'C. custom agent: ' || LEFT(
                COALESCE(proxy_server_request->'system'->0->>'text',
                         proxy_server_request->>'system'),
                60)
       END AS traffic_class,
       COUNT(*) AS req,
       ROUND(SUM(spend)::numeric, 2) AS spend_usd
  FROM "LiteLLM_SpendLogs"
 WHERE metadata->>'user_api_key_alias' = :'key_alias'
   AND "startTime" >= NOW() - (:hours || ' hours')::interval
 GROUP BY 1
 ORDER BY spend_usd DESC NULLS LAST
 LIMIT 12;

SELECT '== Hourly split (Beijing time) ==' AS section;
SELECT to_char("startTime" + INTERVAL '8 hours', 'MM-DD Dy HH24:00') AS hour_cst,
       COUNT(*) FILTER (WHERE total_tokens > 0) AS ok_req,
       ROUND(SUM(spend)::numeric, 2) AS spend_usd,
       COUNT(*) FILTER (WHERE status = 'failure') AS fail_req
  FROM "LiteLLM_SpendLogs"
 WHERE metadata->>'user_api_key_alias' = :'key_alias'
   AND "startTime" >= NOW() - (:hours || ' hours')::interval
 GROUP BY hour_cst
 ORDER BY hour_cst;

SELECT '== Top tools used ==' AS section;
SELECT t->>'name' AS tool_name,
       COUNT(*)   AS uses,
       LEFT(MAX(t->>'description'), 80) AS sample_desc
  FROM "LiteLLM_SpendLogs" s,
       LATERAL jsonb_array_elements(proxy_server_request->'tools') AS t
 WHERE metadata->>'user_api_key_alias' = :'key_alias'
   AND s."startTime" >= NOW() - (:hours || ' hours')::interval
 GROUP BY t->>'name'
 ORDER BY uses DESC
 LIMIT 25;

SELECT '== Cache effectiveness (per workflow) ==' AS section;
SELECT CASE
         WHEN COALESCE(proxy_server_request->'system'->0->>'text',
                       proxy_server_request->>'system') ILIKE '%You are Claude Code%'
              OR COALESCE(proxy_server_request->'system'->0->>'text',
                          proxy_server_request->>'system') ILIKE '%You are an interactive%'
              OR COALESCE(proxy_server_request->'system'->0->>'text',
                          proxy_server_request->>'system') ILIKE '%cc_version=%'
              OR COALESCE(proxy_server_request->'system'->0->>'text',
                          proxy_server_request->>'system') ILIKE '%cc_entrypoint=cli%'
              THEN 'B. IDE'
         WHEN proxy_server_request->'system' IS NULL THEN 'D. no-system'
         ELSE 'C. custom agent'
       END AS workflow,
       COUNT(*) AS req,
       SUM(prompt_tokens) AS total_in,
       SUM((metadata->'usage_object'->>'cache_read_input_tokens')::int)     AS cache_read,
       SUM((metadata->'usage_object'->>'cache_creation_input_tokens')::int) AS cache_write,
       ROUND(100.0 * SUM((metadata->'usage_object'->>'cache_read_input_tokens')::int)
             / NULLIF(SUM(prompt_tokens), 0), 1) AS cache_hit_pct,
       ROUND(SUM(spend)::numeric, 2) AS spend_usd
  FROM "LiteLLM_SpendLogs"
 WHERE metadata->>'user_api_key_alias' = :'key_alias'
   AND "startTime" >= NOW() - (:hours || ' hours')::interval
   AND total_tokens > 0
 GROUP BY workflow
 ORDER BY spend_usd DESC NULLS LAST;

SELECT '== Failure breakdown ==' AS section;
SELECT metadata->'error_information'->>'error_class'   AS error_class,
       metadata->'error_information'->>'error_code'    AS code,
       LEFT(metadata->'error_information'->>'error_message', 90) AS msg,
       COUNT(*) AS n,
       to_char(MIN("startTime") + INTERVAL '8 hours', 'MM-DD HH24:MI CST') AS first_seen,
       to_char(MAX("startTime") + INTERVAL '8 hours', 'MM-DD HH24:MI CST') AS last_seen
  FROM "LiteLLM_SpendLogs"
 WHERE metadata->>'user_api_key_alias' = :'key_alias'
   AND "startTime" >= NOW() - (:hours || ' hours')::interval
   AND status = 'failure'
 GROUP BY 1, 2, 3
 ORDER BY n DESC
 LIMIT 10;

SELECT '== First user message of latest 5 custom-agent calls ==' AS section;
SELECT to_char(s."startTime" + INTERVAL '8 hours', 'MM-DD HH24:MI:SS') AS t_cst,
       LEFT(
         CASE
           WHEN jsonb_typeof((proxy_server_request->'messages'->0)->'content') = 'string'
                THEN (proxy_server_request->'messages'->0)->>'content'
           WHEN jsonb_typeof((proxy_server_request->'messages'->0)->'content') = 'array'
                THEN (proxy_server_request->'messages'->0)->'content'->0->>'text'
         END, 600) AS first_user_msg
  FROM "LiteLLM_SpendLogs" s
 WHERE metadata->>'user_api_key_alias' = :'key_alias'
   AND s."startTime" >= NOW() - (:hours || ' hours')::interval
   AND COALESCE(proxy_server_request->'system'->0->>'text',
                proxy_server_request->>'system') NOT ILIKE '%You are Claude Code%'
   AND COALESCE(proxy_server_request->'system'->0->>'text',
                proxy_server_request->>'system') NOT ILIKE '%You are an interactive%'
   AND COALESCE(proxy_server_request->'system'->0->>'text',
                proxy_server_request->>'system') NOT ILIKE '%cc_version=%'
   AND COALESCE(proxy_server_request->'system'->0->>'text',
                proxy_server_request->>'system') NOT ILIKE '%cc_entrypoint=cli%'
   AND proxy_server_request->'messages' IS NOT NULL
 ORDER BY s."startTime" DESC
 LIMIT 5;

SELECT '== Sample assistant tool_use decisions (custom agents) ==' AS section;
SELECT to_char(s."startTime" + INTERVAL '8 hours', 'MM-DD HH24:MI:SS') AS t_cst,
       c->>'name' AS tool_called,
       LEFT((c->'input')::text, 400) AS args
  FROM "LiteLLM_SpendLogs" s,
       LATERAL jsonb_array_elements(response->'content') AS c
 WHERE metadata->>'user_api_key_alias' = :'key_alias'
   AND s."startTime" >= NOW() - (:hours || ' hours')::interval
   AND c->>'type' = 'tool_use'
   AND COALESCE(proxy_server_request->'system'->0->>'text',
                proxy_server_request->>'system') NOT ILIKE '%You are Claude Code%'
   AND COALESCE(proxy_server_request->'system'->0->>'text',
                proxy_server_request->>'system') NOT ILIKE '%cc_version=%'
 ORDER BY s."startTime" DESC
 LIMIT 5;
SQL

# Upload SQL → copy into db pod → run with psql -f.
# This avoids quoting hell of jms ssh + kubectl exec + bash -c + psql -c.
jms scp "$local_sql" "AIYJY-litellm:$remote_path" >/dev/null
jms ssh AIYJY-litellm \
  "kubectl cp $remote_path $ns/litellm-db-0:$remote_path \
   && kubectl exec -n $ns litellm-db-0 -- bash -c \
     'PGPASSWORD=\$POSTGRES_PASSWORD psql -U \$POSTGRES_USER \$POSTGRES_DB -f $remote_path' \
   ; rm -f $remote_path"
