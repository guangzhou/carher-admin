#!/usr/bin/env bash
# 198 上跑:按「key 当前绑在哪个 kiro 槽」× 客户端(UA)分组,看 aws-* Claude 模型近 N 小时耗时。
# 证伪用:同一个槽里 Cursor 和 Claude Code 应当一样慢/一样快;不同槽差得多 ⇒ 慢在槽不在客户端。
set -euo pipefail
HRS=${1:-6}
NS=litellm-product
P=$(sudo -n kubectl -n kiro-rs get pod -l app=kiro-rs-slots -o name | head -1)
K=$(sudo -n kubectl -n kiro-rs exec "$P" -c kiro-rs -- cat /app/config/config.json | python3 -c 'import json,sys;print(json.load(sys.stdin)["adminApiKey"])')
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
curl -s -H "x-api-key: $K" http://127.0.0.1:30891/api/admin/slots > "$T/slots.json"
for n in $(python3 -c 'import json,sys;[print(x["name"]) for x in json.load(open(sys.argv[1]))["slots"]]' "$T/slots.json"); do
  curl -s -H "x-api-key: $K" "http://127.0.0.1:30891/api/admin/slots/users?slot=$n"; echo
done > "$T/users.jsonl"
# alias → slot 映射写成 VALUES 列表
VALUES=$(python3 - "$T/users.jsonl" <<'PY'
import json, sys
rows = []
for l in open(sys.argv[1]):
    l = l.strip()
    if not l: continue
    for it in json.loads(l)["items"]:
        a = (it.get("keyAlias") or "").replace("'", "")
        if a: rows.append("('%s','%s')" % (a, it["slot"]))
print(",".join(rows))
PY
)
# 10-29:secret 被 key 轮换改名(litellm-secrets → litellm-secrets-r2)⇒ 不写死,挑带 DATABASE_URL 的那个
SEC=$(sudo kubectl -n $NS get secret -o name | grep -E '^secret/litellm-secrets' | while read s; do
  sudo kubectl -n $NS get "$s" -o jsonpath='{.data.DATABASE_URL}' 2>/dev/null | grep -q . && echo "$s"; done | tail -1)
[ -n "$SEC" ] || { echo "FATAL 找不到带 DATABASE_URL 的 litellm-secrets*" >&2; exit 2; }
DU=$(sudo kubectl -n $NS get "$SEC" -o jsonpath='{.data.DATABASE_URL}' | base64 -d)
U=$(echo "$DU"|sed -E 's#^[a-z+]+://([^:]+):.*#\1#'); PW=$(echo "$DU"|sed -E 's#^[a-z+]+://[^:]+:([^@]+)@.*#\1#'); D=$(echo "$DU"|sed -E 's#.*/([^/?]+)(\?.*)?$#\1#')
sudo kubectl -n $NS exec litellm-db-0 -- env PGPASSWORD="$PW" psql -h 127.0.0.1 -U "$U" -d "$D" -c "SET statement_timeout='250s';
WITH m(alias,slot) AS (VALUES $VALUES),
s AS (SELECT m.slot, CASE WHEN l.request_tags::text LIKE '%User-Agent: Cursor%' THEN 'cursor'
                          WHEN l.request_tags::text LIKE '%User-Agent: claude-cli%' THEN 'claude' ELSE 'other' END cli,
        l.request_duration_ms d, extract(epoch from l.\"completionStartTime\"-l.\"startTime\") ttft
      FROM \"LiteLLM_SpendLogs\" l JOIN m ON m.alias = l.metadata->>'user_api_key_alias'
      WHERE l.\"startTime\">now()-interval '${HRS} hours' AND l.status='success'
        AND l.model_group IN ('aws-claude-opus-5-5','aws-claude-sonnet-5-5','aws-claude-opus-4-8','aws-claude-opus-5'))
SELECT slot, cli, count(*) n,
  round(percentile_cont(0.5) WITHIN GROUP (ORDER BY ttft)::numeric,1) ttft_p50,
  round(percentile_cont(0.9) WITHIN GROUP (ORDER BY ttft)::numeric,1) ttft_p90,
  round(percentile_cont(0.5) WITHIN GROUP (ORDER BY d)::numeric/1000,1) total_p50,
  round(100.0*count(*) FILTER (WHERE ttft>60)/count(*)) ttft_gt60_pct
FROM s WHERE cli<>'other' GROUP BY 1,2 HAVING count(*)>=10 ORDER BY 1,2;"
