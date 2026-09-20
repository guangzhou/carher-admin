#!/usr/bin/env bash
# A/B 两腿都吐 402 "API 异常" —— 这不是 A/B 结果(两腿相同,零判别力),
# 是**现在线上就在报错**。402 来自上游 9router/Cursor,不是 LiteLLM 自己的鉴权。
# 之前 12:11~12:27 那 34/34 全绿,所以这是新出现的状态。
#
# 三件事要分清:
#   1) 402 是只打 fable-5.1 还是两个模型都打(=账号级 vs 模型级)
#   2) 未经改动的对照组 grok 是否也 402(=判断病在 9router 还是全局)
#   3) SpendLogs 里线上真实流量什么时候开始出现失败
# 判据不看我造的探针成功率,看真实用户流量的成败分布。
set -uo pipefail
NS=litellm-product
POD=$(sudo kubectl -n $NS get pod -l app=litellm-proxy -o jsonpath='{.items[0].metadata.name}')
MK=$(sudo kubectl -n $NS exec -i $POD -- printenv LITELLM_MASTER_KEY < /dev/null 2>/dev/null | tr -d '\r\n')

echo "=== 1) 三个模型各发一个 hi(grok = 未改动对照组) ==="
for M in claude-fable-5.1 claude-opus-5 claude-grok-4.6; do
  printf '  %-20s ' "$M"
  sudo kubectl -n $NS exec -i $POD -- env MK="$MK" M="$M" python3 -c "
import os, httpx, json
try:
    r = httpx.post('http://127.0.0.1:4000/v1/chat/completions',
        json={'model':os.environ['M'],'max_tokens':20,
              'messages':[{'role':'user','content':'hi'}]},
        headers={'Authorization':'Bearer '+os.environ['MK']}, timeout=120)
    d = r.json()
    if 'error' in d:
        print('HTTP', r.status_code, '|', str(d['error'].get('message'))[:120],
              '| code=', d['error'].get('code'))
    else:
        print('HTTP', r.status_code, '| OK |',
              (d['choices'][0]['message'].get('content') or '')[:40].replace('\n',' '))
except Exception as e:
    print('EXC', type(e).__name__, str(e)[:100])
" < /dev/null 2>/dev/null
done

echo
echo "=== 2) carher-1 真实流量近 90 分钟成败分布(按 5 分钟桶) ==="
sudo kubectl -n $NS exec -i litellm-db-0 -- psql -U litellm -d litellm -c \
"select date_trunc('hour', \"startTime\")
        + interval '5 min' * floor(extract(minute from \"startTime\')::int / 5) as bucket,
        \"model_group\", count(*) as n
 from \"LiteLLM_SpendLogs\"
 where \"api_key\"='a538f674b895678f417eea9e277f75a24bf94333f388b96b0546167a6d779bb7'
   and \"startTime\" > now() - interval '90 minutes'
 group by 1,2 order by 1 desc,2;" < /dev/null

echo
echo "=== 3) 近 90 分钟 9router 相关错误(全 key,看是不是账号级) ==="
sudo kubectl -n $NS exec -i litellm-db-0 -- psql -U litellm -d litellm -c \
"select date_trunc('minute', \"startTime\") as t, \"model_group\", \"model\",
        left(coalesce(metadata->>'error_information', ''), 160) as err
 from \"LiteLLM_SpendLogs\"
 where \"startTime\" > now() - interval '90 minutes'
   and \"model\" like '9router/%'
   and coalesce(metadata->>'status','') <> 'success'
 order by t desc limit 20;" < /dev/null
