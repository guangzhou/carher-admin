#!/usr/bin/env python3
"""删除误注册的 chatgpt/ 条目并按 openai/ + api_base 模式重注册。

用法（在 198 上 sudo 后）:
    export MK=$(kubectl -n litellm-product get secret litellm-secrets \
        -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
    export PK=$(kubectl -n litellm-product exec deploy/chatgpt-acct-62 -- \
        printenv LITELLM_MASTER_KEY)   # POOL_KEY，任一在役 acct pod 都是同一把
    python3 fix_reregister.py 62,66,74 sol,terra,luna [proxy_base]

注册体五要素缺一不可：openai/ 前缀、svc DNS api_base、api_key=POOL_KEY、
显式 model_info.id、mode=responses。model_name 只用组名（chatgpt-gpt-5.6-*），
禁止同时注册裸名（alias inflation）。
"""
import json
import os
import sys
import urllib.request

MK = os.environ['MK']
PK = os.environ['PK']
ACCTS = sys.argv[1].split(',')
VARIANTS = sys.argv[2].split(',')
BASE = sys.argv[3] if len(sys.argv) > 3 else 'http://127.0.0.1:30402/pro'


def api(path, body=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Authorization': 'Bearer ' + MK,
                 'Content-Type': 'application/json'})
    try:
        return json.load(urllib.request.urlopen(req))
    except urllib.error.HTTPError as e:
        return {'HTTPError': e.code, 'body': e.read().decode()[:300]}


# 1) 先删（坏条目在主动卡死 proxy，止血优先于零中断惯例）
for a in ACCTS:
    for v in VARIANTS:
        mid = f'chatgpt-acct-{a}-gpt-5.6-{v}'
        print('DELETE', mid, '->', json.dumps(api('/model/delete', {'id': mid}))[:120])

# 2) 重注册
for a in ACCTS:
    for v in VARIANTS:
        mid = f'chatgpt-acct-{a}-gpt-5.6-{v}'
        body = {
            'model_name': f'chatgpt-gpt-5.6-{v}',
            'litellm_params': {
                'model': f'openai/chatgpt-gpt-5.6-{v}',
                'api_base': f'http://chatgpt-acct-{a}.litellm-product.svc.cluster.local:4000',
                'api_key': PK,
                'rpm': 30,
            },
            'model_info': {'id': mid, 'mode': 'responses'},
        }
        print('NEW', mid, '->', json.dumps(api('/model/new', body))[:120])

# 3) 校验：chatgpt/ 清零 + 新条目字段正确
data = api('/model/info')['data']
remaining = [m for m in data
             if m.get('litellm_params', {}).get('model', '').startswith('chatgpt/')]
print('remaining chatgpt/ entries:', len(remaining))
want = {f'chatgpt-acct-{a}-gpt-5.6-{v}' for a in ACCTS for v in VARIANTS}
seen = set()
for m in data:
    mi = m.get('model_info') or {}
    if mi.get('id') in want and mi.get('id') not in seen:
        seen.add(mi['id'])
        lp = m['litellm_params']
        print('VERIFY', mi['id'], '|', lp.get('model'), '|', lp.get('api_base'),
              '| mode=', mi.get('mode'))
missing = want - seen
if missing:
    print('!! MISSING after re-register:', sorted(missing))
