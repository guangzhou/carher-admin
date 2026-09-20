#!/usr/bin/env bash
# 已用数据排除的假设:
#   * token 过期 —— providerConnections updatedAt=09-16T09:40:38Z(北京17:40),才 3h4x,没到 24h
#   * 账号死/额度打满 —— full_stripe_profile HTTP 200: membershipType=pro,
#     individualMembershipType=ultra, subscriptionStatus=active, lastPaymentFailed=false
#
# 新事实,而且是矛盾点:
#   我直打 9router:20128 用 model=claude-fable-5-1-medium 拿到的是
#     HTTP 404 {"message":"No active credentials for provider: anthropic","code":"model_not_found"}
#   经 LiteLLM 打拿到的是 HTTP 402 "API 异常 (req: xxxx)"
#   两个错不一样 => LiteLLM 发的 model 串/路径跟我手打的不同,不能拿我这发替代它。
#   而且 "provider: anthropic" 本身可疑: usageHistory 里 12:48 那批
#   model=claude-fable-5-1-medium 的 provider 明明是 cursor。
#   说明 9router 是按模型名前缀猜 provider 的,名字对不上就落到 anthropic/openai 上去了。
#
# 本轮: 拿到 LiteLLM 侧真实的 model + api_base,原样重放,让两边的错对齐。
set -uo pipefail
NS=litellm-product
P9=9router-6755f9f964-6jl7v
POD=$(sudo kubectl -n $NS get pod -l app=litellm-proxy -o jsonpath='{.items[0].metadata.name}')

echo "=== 1) LiteLLM 侧这两个部署的真实 litellm_params(pod 内解密,凭据只打长度) ==="
DB_ROWS=$(sudo kubectl -n $NS exec -i litellm-db-0 -- psql -U litellm -d litellm -A -t -c \
"select json_agg(json_build_array(model_name, model_id, litellm_params::text))
 from \"LiteLLM_ProxyModelTable\"
 where model_id like '9router/%' or model_name in
   ('claude-fable-5-1-medium','claude-opus-5-medium','cursor-fc-fable-5.1','cursor-fc-opus-5');" \
 < /dev/null 2>/dev/null | tr -d '\n')

sudo kubectl -n $NS exec -i $POD -- env DB_ROWS="$DB_ROWS" python3 -c "
import os, json
import litellm.proxy.proxy_server as _ps
if not os.getenv('LITELLM_SALT_KEY'):
    _ps.master_key = os.environ['LITELLM_MASTER_KEY']
from litellm.proxy.common_utils.encrypt_decrypt_utils import decrypt_value_helper
rows = json.loads(os.environ['DB_ROWS'] or '[]') or []
for name, mid, lp in rows:
    d = json.loads(lp)
    out = {}
    for k, v in d.items():
        if isinstance(v, str) and k in ('api_key','api_base','model'):
            dec = decrypt_value_helper(v, k) or v
            out[k] = ('len=%d' % len(dec)) if k == 'api_key' else dec
        elif k != 'api_key':
            out[k] = v
    print(' model_name=%-24s model_id=%s' % (name, mid))
    print('   ', json.dumps(out, ensure_ascii=False)[:400])
" < /dev/null 2>&1 | grep -v sitecustomize

echo
echo "=== 2) 9router 的 providerNodes / combos(它怎么把模型名映射到 provider) ==="
sudo kubectl -n $NS exec -i $P9 -- node -e "
const D=require('/app/node_modules/better-sqlite3');
const db=new D('/app/data/db/data.sqlite',{readonly:true});
for(const t of ['providerNodes','combos']){
  console.log('--- '+t+' ---');
  for(const r of db.prepare('select * from '+t).all()){
    const o={};
    for(const [k,v] of Object.entries(r)){
      if(/key|token|secret/i.test(k)){o[k]=v?('len='+String(v).length):v;continue;}
      o[k]=(typeof v==='string'&&v.length>260)?String(v).slice(0,260)+'..':v;
    }
    console.log('  '+JSON.stringify(o));
  }
}
" < /dev/null 2>&1 | tail -25

echo
echo "=== 3) cursor 连接的 expiresAt / testStatus ==="
sudo kubectl -n $NS exec -i $P9 -- node -e "
const D=require('/app/node_modules/better-sqlite3');
const db=new D('/app/data/db/data.sqlite',{readonly:true});
const c=db.prepare(\"select * from providerConnections where provider='cursor'\").get();
const d=JSON.parse(c.data);
console.log('  isActive     =',c.isActive);
console.log('  updatedAt    =',c.updatedAt);
console.log('  expiresAt    =',d.expiresAt, d.expiresAt?('=> '+new Date(d.expiresAt).toISOString()):'');
console.log('  testStatus   =',JSON.stringify(d.testStatus));
console.log('  accessToken  = len='+String(d.accessToken||'').length);
console.log('  providerSpecificData =',JSON.stringify(d.providerSpecificData).slice(0,300));
console.log('  now          =',new Date().toISOString());
" < /dev/null 2>&1 | tail -12
