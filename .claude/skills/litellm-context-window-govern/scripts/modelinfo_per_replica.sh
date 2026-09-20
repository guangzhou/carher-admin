#!/usr/bin/env bash
# 逐副本读 /model/info 里目标行的窗口分布 —— DB 改完等轮询收敛用，也是回归判据 1。
# 多副本必须全查：只查一个 = 另一个可能还在跑旧值（2026-09-06 就出现过 3/4 先收敛）。
set -uo pipefail
NS=litellm-product
for P in $(kubectl -n $NS get po -l app=litellm-proxy --no-headers -o name | cut -d/ -f2); do
  echo -n "$P: "
  kubectl -n $NS exec $P -- python3 -c "
import os,json,urllib.request
r=urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:4000/model/info',
    headers={'Authorization':'Bearer '+os.environ['LITELLM_MASTER_KEY']}),timeout=30)
d=json.load(r)['data']
from collections import Counter
c=Counter(str((m.get('model_info') or {}).get('max_input_tokens'))
          for m in d if m.get('model_name')=='chatgpt-gpt-6-astra')
print('astra:',dict(c))
" 2>/dev/null | grep astra
done
