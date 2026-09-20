#!/usr/bin/env bash
# 上一轮两个必须先解决的发现:
#
# (A) claude-fable-5.1 / claude-opus-5 各有 **2 行部署**,不是 1 行。
#     我 09-17 那次 repoint 是 add 新腿,旧腿(copilot2api,已用完)**还留在组里**。
#     这解释了 402: 轮询打到旧腿就 402,打到新腿就 200。
#     => 402 跟 toolcall 是两件独立的事,且这件是我自己那轮没做完的收尾
#        (改名/换映射是加法: add→verify→cutover→**最后 remove**,我停在了 verify)。
#     本轮先把两条腿分别直打,确认"402 只出自旧腿",再谈删。
#
# (B) 9router 已经是 fc-20260913h,bundle 里有没有那三个 decline 还没验出来
#     (pod 里没有 bash,上一版 `exec -- bash -c` 直接失败了)。用 sh 重来。
#     日志里 interaction_update 出现了 **field 13 / 25 / 8**,skill 里那张表只到 fetch=9,
#     且 field 25 的 dump 是坏的("Received type number") —— 这是解码器的 bug,不是新通道。
#     必须先分清: 13/25 是"没接住的新通道"还是"已知的正常帧",不许直接照 fetch 抄一个 decline。
set -uo pipefail
NS=litellm-product
P9=$(sudo kubectl -n $NS get pod -l app=9router -o jsonpath='{.items[0].metadata.name}')

echo "=== A1) 两个 group 各自的腿:哪条是 9router 哪条是旧 copilot2api ==="
DB=$(sudo kubectl -n $NS exec -i litellm-db-0 -- psql -U litellm -d litellm -A -t -c \
"select json_agg(json_build_array(model_name, model_id, litellm_params::text))
 from \"LiteLLM_ProxyModelTable\"
 where model_name in ('claude-fable-5.1','claude-opus-5');" < /dev/null 2>/dev/null | tr -d '\n')
POD=$(sudo kubectl -n $NS get pod -l app=litellm-proxy -o jsonpath='{.items[0].metadata.name}')
sudo kubectl -n $NS exec -i $POD -- env DB_ROWS="$DB" python3 -c "
import os, json
import litellm.proxy.proxy_server as _ps
if not os.getenv('LITELLM_SALT_KEY'):
    _ps.master_key = os.environ['LITELLM_MASTER_KEY']
from litellm.proxy.common_utils.encrypt_decrypt_utils import decrypt_value_helper
for name, mid, lp in json.loads(os.environ['DB_ROWS'] or '[]') or []:
    d = json.loads(lp)
    m  = decrypt_value_helper(d.get('model',''), 'model') or d.get('model')
    ab = decrypt_value_helper(d.get('api_base',''), 'api_base') or d.get('api_base')
    ak = decrypt_value_helper(d.get('api_key',''), 'api_key') or ''
    print('  %-18s %s' % (name, mid))
    print('     model    = %s' % m)
    print('     api_base = %s' % ab)
    print('     api_key  = len=%d' % len(ak))
" < /dev/null 2>&1 | grep -v sitecustomize

echo
echo "=== A2) 按 model_id 逐腿直打(证明 402 只出自哪条腿) ==="
sudo kubectl -n $NS exec -i $POD -- python3 -c "
import os, httpx, json
MK=os.environ['LITELLM_MASTER_KEY']
ids=json.loads(os.environ.get('IDS','[]'))
for name in ['claude-fable-5.1','claude-opus-5']:
    for i in range(4):
        r=httpx.post('http://127.0.0.1:4000/v1/chat/completions',
          json={'model':name,'max_tokens':10,'messages':[{'role':'user','content':'hi'}]},
          headers={'Authorization':'Bearer '+MK}, timeout=120)
        mid=r.headers.get('x-litellm-model-id','?')
        try: e=r.json().get('error',{}).get('code','')
        except Exception: e=''
        print('  %-18s round%d HTTP %s  model-id=%s %s' % (name,i+1,r.status_code,mid,e))
" < /dev/null 2>&1 | grep -v sitecustomize

echo
echo "=== B1) bundle 里的 decline 能力(pod 无 bash,改用 sh) ==="
sudo kubectl -n $NS exec -i $P9 -- sh -c '
CH=$(ls /app/.next/server/chunks/*.js 2>/dev/null)
for M in NATIVE_TOOL_DECLINE createWebSearchDeclineResponse createFetchDeclineResponse isNativeToolArg; do
  N=$(grep -lF "$M" $CH 2>/dev/null | wc -l | tr -d " ")
  [ "$N" != "0" ] && echo "  有   $M ($N chunk)" || echo "  没有 $M"
done' < /dev/null 2>&1 | grep -v '^\[sudo\]'

echo
echo "=== B2) unsupported IDE tool 到底从哪来: 9router 还是 LiteLLM 自己造的词 ==="
sudo kubectl -n $NS exec -i $P9 -- sh -c '
grep -rlF "unsupported IDE tool" /app/.next/server/chunks/ 2>/dev/null | head -3
grep -roF "unsupported IDE tool" /app/.next/server/chunks/ 2>/dev/null | head -3' < /dev/null 2>&1
echo "  ^ 有命中 = 这句话是 9router 造的; 空 = 来自 Cursor 上游或 LiteLLM"

echo
echo "=== B3) SpendLogs 里 unsupported IDE tool 的历史分布(判断是否高频) ==="
sudo kubectl -n $NS exec -i litellm-db-0 -- psql -U litellm -d litellm -c \
"select date_trunc('hour',\"startTime\") as h, \"model_group\", count(*)
 from \"LiteLLM_SpendLogs\"
 where \"startTime\" > now() - interval '3 days'
   and metadata::text like '%unsupported IDE tool%'
 group by 1,2 order by 1 desc limit 20;" < /dev/null 2>&1 | grep -v '^\[sudo\]'
