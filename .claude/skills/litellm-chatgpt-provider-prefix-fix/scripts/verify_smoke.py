#!/usr/bin/env python3
"""对 gpt-5.6 组做流式 smoke。必须 stream:true —— 非流式打 chatgpt 池会收 SSE
裸流报 APIError 后静默 fallback 到 deepseek-v4-flash 且 HTTP 200（假阳性）。

每个请求换 user，绕开 weighted-affinity 粘滞（ttl 120s），让 router 重新挑
deployment，覆盖尽量多的 acct。

用法（在 198 上 sudo 后）:
    export MK=$(kubectl -n litellm-product get secret litellm-secrets \
        -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
    python3 verify_smoke.py [次数=36] [proxy_base]

之后按日志核对目标 acct 被选中且无异常：
    kubectl -n litellm-product logs <各proxy pod> --since=10m \
      | grep -oE 'weighted-pick deployment=chatgpt-acct-[0-9]+-gpt-5\\.6-[a-z]+' \
      | sort | uniq -c
"""
import json
import os
import subprocess
import sys

MK = os.environ['MK']
N = int(sys.argv[1]) if len(sys.argv) > 1 else 36
BASE = (sys.argv[2] if len(sys.argv) > 2
        else 'http://127.0.0.1:30402/pro') + '/v1/responses'

ok = fail = 0
for i in range(N):
    v = ['sol', 'terra', 'luna'][i % 3]
    body = json.dumps({
        'model': f'gpt-5.6-{v}',
        'input': [{'role': 'user', 'content': 'say ok'}],
        'max_output_tokens': 16,
        'stream': True,
        'user': f'verify-smoke-{os.getpid()}-{i}',
    })
    p = subprocess.run(['curl', '-s', '-m', '90', '-X', 'POST', BASE,
                        '-H', 'Authorization: Bearer ' + MK,
                        '-H', 'Content-Type: application/json', '-d', body],
                       capture_output=True, text=True)
    if 'response.completed' in p.stdout:
        ok += 1
    else:
        fail += 1
        print(f'REQ {i} ({v}) FAIL:', p.stdout[:200].replace(chr(10), ' '))
print(f'stream smoke: ok={ok} fail={fail}')
