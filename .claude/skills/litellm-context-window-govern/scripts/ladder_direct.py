#!/usr/bin/env python3
"""直连某个 acct pod 量真实窗口（绕开 router 的随机挑腿）。
用法: python3 ladder_direct.py <acct_n|self> <model> <size1,size2,...>
填充物 "hello "xN => prompt_tokens ~= N + 系统开销(约1638)。边界要打两次确认可重复。
"""
import json, os, sys, time, urllib.request, urllib.error
n, model = sys.argv[1], sys.argv[2]
sizes = [int(x) for x in sys.argv[3].split(",")]
# n="self" => 在 acct pod 内部直打自己（用 LITELLM_MASTER_KEY）
# n=<数字> => 在 litellm-proxy pod 内打 acct svc（用 CHATGPT_POOL_KEY）
NS_SVC = os.environ.get("ACCT_SVC_NS", "carher")   # 198 上是 litellm-product
if n == "self":
    URL = "http://127.0.0.1:4000/v1/chat/completions"
    K = os.environ["LITELLM_MASTER_KEY"]
else:
    URL = f"http://chatgpt-acct-{n}.{NS_SVC}.svc:4000/v1/chat/completions"
    K = os.environ["CHATGPT_POOL_KEY"]
for N in sizes:
    body = "hello " * N + "\nReply with exactly one word: pong"
    req = urllib.request.Request(URL, data=json.dumps(
        {"model": model, "messages": [{"role": "user", "content": body}], "max_tokens": 16}).encode(),
        headers={"Authorization": "Bearer " + K, "Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            d = json.loads(r.read()); u = d.get("usage", {})
            print(f"acct-{n} {model:22s} ~{N:>9,d} OK   {time.time()-t0:5.1f}s prompt_tokens={u.get('prompt_tokens')} "
                  f"reply={(d['choices'][0]['message'].get('content') or '').strip()[:20]!r}", flush=True)
    except urllib.error.HTTPError as e:
        print(f"acct-{n} {model:22s} ~{N:>9,d} FAIL {time.time()-t0:5.1f}s HTTP {e.code} {e.read().decode()[:200]}", flush=True)
        break
