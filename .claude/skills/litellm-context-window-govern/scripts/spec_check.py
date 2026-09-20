#!/usr/bin/env python3
"""验证「官方口径」在真实 acct 上是否成立。
官方页(developers.openai.com)：1,050,000 context window + 128,000 max output tokens。

在 litellm-proxy pod 内运行，直连单个 acct，绕开 router 挑腿。
用法: python3 spec_check.py <acct_n> <model>
"""
import json, os, sys, time, urllib.request, urllib.error

n = sys.argv[1]; model = sys.argv[2]
if n == "self":
    URL = "http://127.0.0.1:4000/v1/chat/completions"
    K = os.environ["LITELLM_MASTER_KEY"]
else:
    URL = f"http://chatgpt-acct-{n}.carher.svc:4000/v1/chat/completions"
    K = os.environ["CHATGPT_POOL_KEY"]

def call(tag, filler_n, max_tokens, prompt=None, timeout=1200):
    body = ("hello " * filler_n + (prompt or "\nReply with exactly one word: pong")) if filler_n \
           else (prompt or "Reply with exactly one word: pong")
    payload = {"model": model, "messages": [{"role": "user", "content": body}]}
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    req = urllib.request.Request(URL, data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + K, "Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read()); u = d.get("usage", {}) or {}
            ch = d["choices"][0]
            txt = (ch["message"].get("content") or "")
            print(f"[{tag}] OK   {time.time()-t0:6.1f}s prompt={u.get('prompt_tokens')} "
                  f"completion={u.get('completion_tokens')} total={u.get('total_tokens')} "
                  f"finish={ch.get('finish_reason')} len(text)={len(txt)} head={txt[:60]!r}", flush=True)
            return u
    except urllib.error.HTTPError as e:
        print(f"[{tag}] FAIL {time.time()-t0:6.1f}s HTTP {e.code} {e.read().decode()[:300]}", flush=True)
    except Exception as e:
        print(f"[{tag}] ERR  {time.time()-t0:6.1f}s {type(e).__name__} {e}", flush=True)
    return None

print(f"=== acct-{n} / {model} ===", flush=True)

# 阳性对照 0：max_tokens 到底有没有被端到端尊重（drop_params:true 可能把它丢了）
call("ctl-mt16-longask", 0, 16,
     prompt="Write a 500 word essay about the sea. Do not stop early.")
call("ctl-nomt-longask", 0, None,
     prompt="Write a 500 word essay about the sea. Do not stop early.")

# 官方口径 A：max output = 128,000 —— 小 prompt 分别要 128,000 / 128,001 / 200,000
call("A-mt-128000", 0, 128000)
call("A-mt-128001", 0, 128001)
call("A-mt-200000", 0, 200000)

# 官方口径 B：总窗口 1,050,000 —— 输入 ~921,638 再加 128,000 输出配额（合计 1,049,638）
call("B-in921k-mt128000", 920000, 128000)

# 对照 C：输入 ~921,638 + 极小输出配额（已知 OK，用来证明 B 失败不是输入侧的问题）
call("C-in921k-mt16", 920000, 16)
