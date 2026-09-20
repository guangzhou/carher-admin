#!/usr/bin/env python3
"""窗口落地后的逐副本回归。在**每个** litellm-proxy pod 里各跑一遍：
  kubectl -n carher cp regress_window.py $P:/tmp/ && kubectl -n carher exec $P -- python3 /tmp/regress_window.py $P

五项判据（缺一不算过，尤其 GATE 那条 —— 只有 PASS 没有 GATE = 没证明闸门在工作）：
  1) /model/info 目标行 max_input_tokens 分布 == 期望值 x 期望条数
  2) 临时 scoped key（不是 master key）打每个目标模型 -> 200，finally 删 key
  3) 略低于阈值 -> 200
  4) 略高于阈值 -> 400 ContextWindowExceededError，<1s，不出网
  5) 邻居模型冒烟 -> 200（改前也跑一遍存基线，否则分不清本来就坏还是我搞坏的）
按本次任务改下面 CONFIG 段。"""
import json, os, sys, time, urllib.request, urllib.error
BASE = "http://127.0.0.1:4000"
MK = os.environ["LITELLM_MASTER_KEY"]
label = sys.argv[1]
# ===== CONFIG =====
MODELS = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]   # 本次改动的模型（短名，给临时 key 授权）
NEIGH = ["chatgpt-gpt-5.5", "claude-opus-4-7", "wangsu-deepseek-v4-flash"]  # 邻居冒烟
MATCH = ("5.6", "astra")        # /model/info 里统计分布的 model_name 关键字
GATE_MODEL = "gpt-5.6-luna"
PASS_TOK, GATE_TOK = 919000, 923000   # 阈值两侧；填充物 "hello "xN，acct 侧系统开销约 +1638
# ==================

def call(path, payload=None, key=None, timeout=900):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data,
        headers={"Authorization": "Bearer " + (key or MK), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]

st, info = call("/model/info")
dist = {}
for m in info["data"]:
    n = m.get("model_name", "")
    if any(x in n for x in MATCH):
        dist.setdefault(str((m.get("model_info") or {}).get("max_input_tokens")), []).append(n)
print(f"[{label}] max_input_tokens 分布{MATCH}: " + "; ".join(f"{k} x{len(v)}" for k, v in dist.items()))

st, k = call("/key/generate", {"models": MODELS, "key_alias": f"tmp-window-{label}-{int(time.time())}",
                              "max_budget": 5, "duration": "20m"})
assert st == 200, (st, k)
key = k["key"]
try:
    for m in MODELS:
        t0 = time.time()
        st, r = call("/v1/chat/completions", {"model": m,
            "messages": [{"role": "user", "content": "reply with the single word: pong"}]}, key=key)
        if st == 200:
            u = r.get("usage", {})
            print(f"[{label}] {m:16s} HTTP 200 {time.time()-t0:5.1f}s reply={(r['choices'][0]['message'].get('content') or '').strip()[:20]!r} prompt={u.get('prompt_tokens')}")
        else:
            print(f"[{label}] {m:16s} HTTP {st} {time.time()-t0:5.1f}s ERR {r}")
    # 闸门边界：921,000 应过（<922,000），923,000 应被入口 400 掉
    for n_tok, expect in ((PASS_TOK, "PASS"), (GATE_TOK, "GATE")):
        t0 = time.time()
        st, r = call("/v1/chat/completions", {"model": GATE_MODEL,
            "messages": [{"role": "user", "content": "hello " * n_tok + "\nreply with one word: pong"}]}, key=key)
        s = json.dumps(r)[:160] if st != 200 else f"prompt={r.get('usage',{}).get('prompt_tokens')}"
        print(f"[{label}] gate ~{n_tok:,} (expect {expect}) HTTP {st} {time.time()-t0:5.1f}s {s}")
finally:
    call("/key/delete", {"keys": [key]})
    print(f"[{label}] temp key deleted")

for m in NEIGH:
    t0 = time.time()
    st, r = call("/v1/chat/completions", {"model": m,
        "messages": [{"role": "user", "content": "say pong"}], "max_tokens": 20}, timeout=180)
    print(f"[{label}] neigh {m:26s} HTTP {st} {time.time()-t0:5.1f}s " + ("" if st == 200 else str(r)[:120]))
