-- litellm-cache-hit-spendlogs.sql — ChatGPT 上游缓存命中率查询集（SpendLogs 口径）
--
-- 口径说明（2026-08-12/13 确立）:
--   * 真实命中在 metadata->'usage_object'->'prompt_tokens_details'->>'cached_tokens'
--     （上游每次响应报的 input_tokens_details.cached_tokens，litellm 原样入库）
--   * 订阅号在官网看不到缓存数据（chatgpt.com / codex /status 只有配额），本表是唯一来源
--   * 对照必须剔除缓存不友好 workload（5.3-codex / codex-auto-review：上游只缓存
--     ~3.4k 固定指令前缀，正文不缓存），否则稀释均值
--   * 前后对照用「同钟点窗口」（如今天 vs 昨天 00:00-02:10 UTC），排除昼夜流量结构差异
--   * 只看 acct 落地（model_id LIKE 'chatgpt-acct-%'），deepseek 兜底落地没有 ChatGPT 缓存
--
-- 用法: kubectl cp 本文件到 litellm-db-0:/tmp/x.sql && kubectl exec ... psql -U litellm -d litellm -f /tmp/x.sql
-- （198: litellm-product/litellm-db-0；改窗口直接编辑下面的时间条件）

\echo ===1. 今日 per-acct 命中率===
SELECT split_part(model_id, '-gpt', 1) AS acct,
  count(*) AS n, sum(prompt_tokens) AS prompt_tok,
  sum(coalesce((metadata->'usage_object'->'prompt_tokens_details'->>'cached_tokens')::bigint,0)) AS cached_tok,
  round(100.0*sum(coalesce((metadata->'usage_object'->'prompt_tokens_details'->>'cached_tokens')::bigint,0))/nullif(sum(prompt_tokens),0),1) AS pct
FROM "LiteLLM_SpendLogs"
WHERE "startTime" >= CURRENT_DATE
  AND model_id LIKE 'chatgpt-acct-%' AND model_id NOT ILIKE '%5.3-codex%'
GROUP BY 1 HAVING count(*) >= 20 ORDER BY n DESC LIMIT 15;

\echo ===2. 今日 per-key（accts 列=钉台健康度: 并发 session 数量级; 几十=亲和坏了）===
SELECT substring(api_key,1,8) AS key8, count(*) AS n,
  count(DISTINCT model_id) AS accts,
  round(100.0*sum(coalesce((metadata->'usage_object'->'prompt_tokens_details'->>'cached_tokens')::bigint,0))/nullif(sum(prompt_tokens),0),1) AS pct
FROM "LiteLLM_SpendLogs"
WHERE "startTime" >= CURRENT_DATE
  AND model_id LIKE 'chatgpt-acct-%' AND model_id NOT ILIKE '%5.3-codex%'
GROUP BY 1 HAVING count(*) >= 10 ORDER BY n DESC LIMIT 12;

\echo ===3. 前后对照模板（改两组时间窗）===
SELECT CASE WHEN "startTime" >= timestamp '2026-08-13 00:00' THEN 'A_post' ELSE 'B_pre' END AS win,
  count(*) AS n,
  round(100.0*sum(coalesce((metadata->'usage_object'->'prompt_tokens_details'->>'cached_tokens')::bigint,0))/nullif(sum(prompt_tokens),0),1) AS pct
FROM "LiteLLM_SpendLogs"
WHERE (("startTime" BETWEEN timestamp '2026-08-13 00:00' AND timestamp '2026-08-13 02:10')
    OR ("startTime" BETWEEN timestamp '2026-08-12 00:00' AND timestamp '2026-08-12 02:10'))
  AND model_id LIKE 'chatgpt-acct-%' AND model_id NOT ILIKE '%5.3-codex%'
GROUP BY 1 ORDER BY 1;
