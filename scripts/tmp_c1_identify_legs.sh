#!/usr/bin/env bash
# 只做一件事: 把 claude-fable-5.1 / claude-opus-5 的每一行部署解密打出来,
# 认出哪条是 9router(留)、哪条是 copilot2api(摘)。
# 判据只认 api_base + model,**不认组名**(组名会撒谎)。
# 本轮纯只读,一个字都不写。
#
# 上一版跑挂了(输出 0 字节)。这版拆小: 不发探针、不进 9router pod,只查库 + 解密。
set -uo pipefail
NS=litellm-product
POD=$(sudo kubectl -n $NS get pod -l app=litellm-proxy -o jsonpath='{.items[0].metadata.name}')

DB=$(sudo kubectl -n $NS exec -i litellm-db-0 -- psql -U litellm -d litellm -A -t -c \
"select json_agg(json_build_array(model_name, model_id, litellm_params::text, created_at::text))
 from \"LiteLLM_ProxyModelTable\"
 where model_name in ('claude-fable-5.1','claude-opus-5');" < /dev/null 2>/dev/null | tr -d '\n')

sudo kubectl -n $NS exec -i $POD -- env DB_ROWS="$DB" python3 -c "
import os, json
import litellm.proxy.proxy_server as _ps
if not os.getenv('LITELLM_SALT_KEY'):
    _ps.master_key = os.environ['LITELLM_MASTER_KEY']
from litellm.proxy.common_utils.encrypt_decrypt_utils import decrypt_value_helper
rows = json.loads(os.environ['DB_ROWS'] or '[]') or []
def dec(v, k):
    if not v: return v
    try: return decrypt_value_helper(v, k) or v
    except Exception: return v
for name, mid, lp, created in sorted(rows):
    d = json.loads(lp)
    ab = dec(d.get('api_base',''), 'api_base')
    m  = dec(d.get('model',''), 'model')
    ak = dec(d.get('api_key',''), 'api_key') or ''
    verdict = '?'
    s = (ab or '') + ' ' + (m or '')
    if '9router' in s or ':20128' in s: verdict = '9router  <= 留'
    elif 'copilot' in s.lower():        verdict = 'copilot2api <= 摘'
    print('%-18s %s  created=%s' % (name, mid, created))
    print('    model    = %s' % m)
    print('    api_base = %s' % ab)
    print('    api_key  = len=%d' % len(ak))
    print('    判定     = %s' % verdict)
    print()
" < /dev/null 2>&1 | grep -v sitecustomize
