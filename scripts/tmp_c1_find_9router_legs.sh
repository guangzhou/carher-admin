#!/usr/bin/env bash
# 我上一条说错了,得纠正: 我说 "各有 2 行,一条 9router 一条 copilot,轮询打到旧腿就 402"。
# 数据不是这样:
#   claude-fable-5.1 两行 = copilot2api-4 / copilot2api-5
#   claude-opus-5    两行 = copilot2api-4 / copilot2api-5
# **四条腿全是 copilot2api,一条 9router 的都没有。**
# copilot 已经用完 => 402 是必然,不是随机。
# 而且这说明 09-17 我那轮"repoint 到 9router"根本没落到这两个组上。
#
# 但 SpendLogs 之前明明显示落点是 9router/claude-fable-5-1-medium，
# 说明 9router 那两行是**注册在别的 model_name 下**(很可能是
# cursor-fc-fable-5.1 / cursor-fc-opus-5)，carher-1 打的 claude-fable-5.1
# 走的是 alias 或另一个组。alias 无条件压过同名真实组 —— 这条得查清楚,
# 否则我会摘错东西。
#
# 本轮纯只读,查三件:
#   1) 全表里所有 9router 腿挂在哪些 model_name 下
#   2) claude-fable-5.1 / claude-opus-5 有没有 alias 指向别处
#   3) SpendLogs 里 carher-1 的 model_group -> model 真实落点
set -uo pipefail
NS=litellm-product
POD=$(sudo kubectl -n $NS get pod -l app=litellm-proxy -o jsonpath='{.items[0].metadata.name}')

echo "=== 1) 所有 9router 腿(按 api_base 认,不认组名) ==="
DB=$(sudo kubectl -n $NS exec -i litellm-db-0 -- psql -U litellm -d litellm -A -t -c \
"select json_agg(json_build_array(model_name, model_id, litellm_params::text))
 from \"LiteLLM_ProxyModelTable\";" < /dev/null 2>/dev/null | tr -d '\n')
sudo kubectl -n $NS exec -i $POD -- env DB_ROWS="$DB" python3 -c "
import os, json
import litellm.proxy.proxy_server as _ps
if not os.getenv('LITELLM_SALT_KEY'):
    _ps.master_key = os.environ['LITELLM_MASTER_KEY']
from litellm.proxy.common_utils.encrypt_decrypt_utils import decrypt_value_helper
def dec(v,k):
    if not v: return v
    try: return decrypt_value_helper(v,k) or v
    except Exception: return v
rows = json.loads(os.environ['DB_ROWS'] or '[]') or []
print('  ProxyModelTable 总行数 =', len(rows))
hit=0
for name, mid, lp in sorted(rows):
    d=json.loads(lp)
    ab=dec(d.get('api_base',''),'api_base') or ''
    m =dec(d.get('model',''),'model') or ''
    if '9router' in (ab+m) or ':20128' in ab:
        hit+=1
        print('  %-26s %s' % (name, mid))
        print('      model=%s' % m)
        print('      base =%s' % ab)
print('  9router 腿总数 =', hit)
" < /dev/null 2>&1 | grep -v sitecustomize

echo
echo "=== 2) alias / model_group_alias 里有没有改写 claude-fable-5.1 / claude-opus-5 ==="
sudo kubectl -n $NS exec -i litellm-db-0 -- psql -U litellm -d litellm -A -t -c \
"select param_name, left(param_value::text, 900)
 from \"LiteLLM_Config\"
 where param_name in ('router_settings','model_group_alias','litellm_settings');" \
 < /dev/null 2>&1 | grep -v '^\[sudo\]' | head -20

echo
echo "=== 3) carher-1 近 3 天真实落点: model_group -> model ==="
sudo kubectl -n $NS exec -i litellm-db-0 -- psql -U litellm -d litellm -c \
"select \"model_group\", \"model\", count(*) as n,
        max(\"startTime\") as last_seen
 from \"LiteLLM_SpendLogs\"
 where \"api_key\"='a538f674b895678f417eea9e277f75a24bf94333f388b96b0546167a6d779bb7'
   and \"startTime\" > now() - interval '3 days'
 group by 1,2 order by 4 desc limit 15;" < /dev/null 2>&1 | grep -v '^\[sudo\]'
