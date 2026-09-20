#!/usr/bin/env bash
# 独立问题(与 toolcall 无关,与飞书文档无关): cursor-fc-fable-5.1 这个组里
#   成功的落点 model = openai/cu/claude-fable-5-1-medium   (10 行)
#   失败的落点 model = cursor-fc-fable-5.1                  (5 行, 12:02~12:06)
# 失败那批的 model 等于组名本身,说明**没解析成上游腿**。
# 两种形状要分开,别猜:
#   (a) 组里真有第二条腿,那条腿配置坏 => 摘它
#   (b) 只有一条腿,失败发生在解析之前(鉴权/入参/超时),LiteLLM 就把组名填进 model
#       => 那不是"坏腿",摘无可摘,得看报错原文
# 上一轮 metadata 只吐 traceback 开头,被截断了,没看到最后那行异常。所以这轮
# 直接取 traceback 尾部 + exception_type,那里才写着真因。
set -uo pipefail
NS=litellm-product
POD=$(sudo kubectl -n $NS get pod -l app=litellm-proxy -o jsonpath='{.items[0].metadata.name}')
KEY=a538f674b895678f417eea9e277f75a24bf94333f388b96b0546167a6d779bb7

echo "=== 1) cursor-fc-* 两个组各有几条腿(判 a 还是 b) ==="
DB=$(sudo kubectl -n $NS exec -i litellm-db-0 -- psql -U litellm -d litellm -A -t -c \
"select json_agg(json_build_array(model_name, model_id, litellm_params::text))
 from \"LiteLLM_ProxyModelTable\"
 where model_name like 'cursor-fc-%';" < /dev/null 2>/dev/null | tr -d '\n')
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
rows=json.loads(os.environ['DB_ROWS'] or '[]') or []
from collections import Counter
c=Counter(r[0] for r in rows)
for name,n in sorted(c.items()): print('  %-24s 腿数=%d' % (name,n))
print()
for name, mid, lp in sorted(rows):
    d=json.loads(lp)
    print('  %-24s %s' % (name, mid))
    print('      model    = %s' % dec(d.get('model',''),'model'))
    print('      api_base = %s' % dec(d.get('api_base',''),'api_base'))
    print('      api_key  = len=%d' % len(dec(d.get('api_key',''),'api_key') or ''))
" < /dev/null 2>&1 | grep -v sitecustomize

echo
echo "=== 2) 失败那批的真因(取 traceback 尾部,那里才是异常行) ==="
sudo kubectl -n $NS exec -i litellm-db-0 -- psql -U litellm -d litellm -A -t -c \
"select \"startTime\" || E'\n    ' || right(metadata->>'error_information', 600)
 from \"LiteLLM_SpendLogs\"
 where \"api_key\"='$KEY' and \"model_group\"='cursor-fc-fable-5.1'
   and \"model\"='cursor-fc-fable-5.1'
 order by \"startTime\" desc limit 3;" < /dev/null 2>&1 | grep -v '^\[sudo\]'

echo
echo "=== 3) metadata 顶层有哪些键(找专门存异常类型的字段) ==="
sudo kubectl -n $NS exec -i litellm-db-0 -- psql -U litellm -d litellm -A -t -c \
"select jsonb_object_keys(metadata)
 from \"LiteLLM_SpendLogs\"
 where \"api_key\"='$KEY' and \"model_group\"='cursor-fc-fable-5.1'
   and \"model\"='cursor-fc-fable-5.1'
 order by 1;" < /dev/null 2>&1 | grep -v '^\[sudo\]' | sort -u | tr '\n' ' '
echo
