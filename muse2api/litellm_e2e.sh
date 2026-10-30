#!/usr/bin/env bash
# Acceptance for muse-* through 198 LiteLLM, the way a user hits it. Run ON 198:
#   printf '%s' "$SK" | ssh cltx@10.68.13.198 'read -r STD_KEY; export STD_KEY; bash -s' < muse2api/litellm_e2e.sh
# STD_KEY = plaintext sk- of a key that was granted muse-* (never printed).
# Positive: /v1/models lists muse-*, chat + stream + image return content.
# Negative: a throwaway key without muse-* gets 403 key_model_access_denied (key deleted after).
set -uo pipefail
P=http://127.0.0.1:30402
: "${STD_KEY:?set STD_KEY}"
MK=$(sudo kubectl -n litellm-product get secret litellm-secrets -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
[ -n "$MK" ] || { echo "ALERT: empty master key"; exit 2; }

echo "== /v1/models"
curl -sS "$P/v1/models" -H "Authorization: Bearer $STD_KEY" \
  | python3 -c 'import sys,json;print([m["id"] for m in json.load(sys.stdin)["data"] if m["id"].startswith("muse")])'

echo "== chat"
curl -sS --max-time 300 "$P/v1/chat/completions" -H "Authorization: Bearer $STD_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"muse-spark","messages":[{"role":"user","content":"Reply with exactly: LITELLM-MUSE-OK"}]}' \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print("content:",(d.get("error") or d["choices"][0]["message"]["content"]))' 2>&1 | head -2

echo "== stream"
curl -sSN --max-time 300 "$P/v1/chat/completions" -H "Authorization: Bearer $STD_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"muse-spark","stream":true,"messages":[{"role":"user","content":"用一句话介绍你自己"}]}' | python3 -c '
import sys,json
n=0;t=""
for l in sys.stdin:
    l=l.strip()
    if not l.startswith("data:") or l[5:].strip()=="[DONE]": continue
    try: c=(json.loads(l[5:]).get("choices") or [{}])[0].get("delta",{}).get("content") or ""
    except ValueError: continue
    if c: n+=1; t+=c
print("chunks",n,"text",t[:160])'

echo "== image"
curl -sS --max-time 400 "$P/v1/images/generations" -H "Authorization: Bearer $STD_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"muse-image","prompt":"一只戴墨镜的橘猫，极简扁平插画","n":1}' \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);x=(d.get("data") or [{}])[0];print("items",len(d.get("data") or []),"url",(x.get("url") or "")[:70],"err",d.get("error"))'

echo "== negative control (throwaway key without muse-*)"
NEG=$(curl -sS "$P/key/generate" -H "Authorization: Bearer $MK" -H 'Content-Type: application/json' \
  -d '{"key_alias":"tmp-muse-negctl","models":["deepseek-v4.1-flash"],"duration":"10m","max_budget":0.01}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["key"])')
curl -sS --max-time 60 "$P/v1/chat/completions" -H "Authorization: Bearer $NEG" -H 'Content-Type: application/json' \
  -d '{"model":"muse-spark","messages":[{"role":"user","content":"hi"}]}' -o /dev/null -w 'neg HTTP=%{http_code} (want 403)\n'
curl -sS "$P/key/delete" -H "Authorization: Bearer $MK" -H 'Content-Type: application/json' -d "{\"keys\":[\"$NEG\"]}" >/dev/null
echo "neg key left: $(sudo kubectl -n litellm-product exec litellm-db-0 -- psql -U litellm -d litellm -At -c "select count(*) from \"LiteLLM_VerificationToken\" where key_alias='tmp-muse-negctl';")"

echo "== SpendLogs last 30 min"
sudo kubectl -n litellm-product exec -i litellm-db-0 -- psql -U litellm -d litellm -At -F ' | ' << 'SQL'
select model_group, status, count(*), round(sum(spend)::numeric,6) from "LiteLLM_SpendLogs"
where "startTime" > now() - interval '30 minutes' and model_group like 'muse%' group by 1,2 order by 1;
SQL
