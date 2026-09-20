#!/usr/bin/env python3
"""扫描 litellm proxy DB 里误用 chatgpt/ 前缀或缺 api_base 的 model 条目。

DB 的 litellm_params 加密存储，psql LIKE 查询是假阴性 —— 必须走 /model/info。
用法（在 198 上 sudo 后）:
    export MK=$(kubectl -n litellm-product get secret litellm-secrets \
        -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
    python3 scan_bad_models.py [proxy_base]   # 默认 http://127.0.0.1:30402/pro
"""
import json
import os
import sys
import urllib.request

MK = os.environ['MK']
BASE = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:30402/pro'

req = urllib.request.Request(BASE + '/model/info',
                             headers={'Authorization': 'Bearer ' + MK})
data = json.load(urllib.request.urlopen(req))['data']

# /model/info 会按 model_group_alias 把同一条目展开成组名+裸名两行，按唯一 id 去重
bad = {}
for m in data:
    lp = m.get('litellm_params', {})
    mi = m.get('model_info') or {}
    mdl = lp.get('model', '')
    if mdl.startswith('chatgpt/') or '/' not in mdl:
        bad[mi.get('id')] = (m.get('model_name'), mdl, str(lp.get('api_base')),
                             str(mi.get('mode')))

print(f'total rows: {len(data)}, bad unique ids: {len(bad)}')
for k, v in sorted(bad.items(), key=lambda x: str(x[0])):
    print(' ', k, '|', ' | '.join(map(str, v)))
