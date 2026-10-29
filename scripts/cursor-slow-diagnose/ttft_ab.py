#!/usr/bin/env python3
"""Cursor 线型 (/v1/chat/completions stream) vs Claude Code 线型 (/v1/messages stream) 首字/总时长 A/B。
同一把 key、同一模型、同一提示,交替打,只变线型。key 从 ~/.claude/settings.json 读,不打印。
用法: python3 ttft_ab.py <model> [rounds] [pad_kb]
"""
import json, os, sys, time, http.client, ssl, statistics

HOST = "cc.auto-link.com.cn"
env = json.load(open(os.path.expanduser("~/.claude/settings.json")))["env"]
KEY = env.get("ANTHROPIC_API_KEY") or env["ANTHROPIC_AUTH_TOKEN"]
model = sys.argv[1]; rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 4
pad_kb = int(sys.argv[3]) if len(sys.argv) > 3 else 0
pad = ("资料:" + "这是一段用于撑大上下文的无关文本。" * 60 + "\n") * max(0, pad_kb // 2) if pad_kb else ""
prompt = pad + "用一句话(30字左右)说说为什么天空是蓝色的。"


def run(path, body, headers, first_pred):
    t0 = time.time()
    c = http.client.HTTPSConnection(HOST, timeout=300, context=ssl.create_default_context())
    c.request("POST", path, json.dumps(body), dict(headers, **{"content-type": "application/json"}))
    r = c.getresponse(); th = time.time() - t0
    ttft = None; n = 0; buf = b""
    while True:
        ch = r.read1(65536) if hasattr(r, "read1") else r.read(1024)
        if not ch: break
        buf += ch
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"): continue
            d = line[5:].strip()
            if d == b"[DONE]": continue
            try: j = json.loads(d)
            except Exception: continue
            if first_pred(j):
                n += 1
                if ttft is None: ttft = time.time() - t0
    return r.status, th, ttft, time.time() - t0, n


def chat():
    return run("/pro/v1/chat/completions",
               {"model": model, "stream": True, "max_tokens": 200, "messages": [{"role": "user", "content": prompt}]},
               {"authorization": "Bearer " + KEY},
               lambda j: any((c.get("delta") or {}).get("content") for c in j.get("choices") or []))


def msgs():
    return run("/pro/v1/messages",
               {"model": model, "stream": True, "max_tokens": 200, "messages": [{"role": "user", "content": prompt}]},
               {"x-api-key": KEY, "anthropic-version": "2023-06-01"},
               lambda j: j.get("type") == "content_block_delta" and (j.get("delta") or {}).get("type") == "text_delta")


res = {"chat(Cursor)": [], "messages(Claude)": []}
for i in range(rounds):
    for name, fn in (("chat(Cursor)", chat), ("messages(Claude)", msgs)) if i % 2 == 0 else (("messages(Claude)", msgs), ("chat(Cursor)", chat)):
        st, th, tt, tot, n = fn()
        res[name].append((st, th, tt, tot, n))
        print(f"{i} {name:17s} http={st} hdr={th:5.2f}s first_token={tt if tt is None else round(tt,2)}s total={tot:5.2f}s chunks={n}", flush=True)
print(f"--- {model} pad={pad_kb}KB 中位数 ---")
for k, v in res.items():
    ok = [x for x in v if x[0] == 200 and x[2] is not None]
    if ok:
        print(f"{k:17s} n={len(ok)} hdr={statistics.median(x[1] for x in ok):.2f}s first_token={statistics.median(x[2] for x in ok):.2f}s total={statistics.median(x[3] for x in ok):.2f}s chunks={statistics.median(x[4] for x in ok)}")
