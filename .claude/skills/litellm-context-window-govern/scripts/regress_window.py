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
import json, os, re, sys, time, urllib.request, urllib.error
BASE = "http://127.0.0.1:4000"
MK = os.environ["LITELLM_MASTER_KEY"]
label = sys.argv[1]
# ===== CONFIG（都可用环境变量覆盖，别再改源码）=====
MODELS = os.environ.get("MODELS", "chatgpt-gpt-5.6-luna").split(",")
# 邻居冒烟。⛔ 名字必须是 198 上真实存在的组 —— 默认值里原来写着 `claude-opus-4-7`，
# 198 上**根本没有这个名**，它稳定返 400 ProxyModelNotFoundError，于是每轮回归都有一条
# 长期红腿，把"我改坏了"和"这个名字是编的"混成同一个形状。2026-09-25 换成实存的名。
NEIGH = os.environ.get("NEIGH", "chatgpt-gpt-5.5,claude-fable-5.1,wangsu-deepseek-v4-flash").split(",")
MATCH = tuple(os.environ.get("MATCH", "5.6,astra").split(","))
GATE_MODEL = os.environ.get("GATE_MODEL", "chatgpt-gpt-5.6-luna")
BUDGET = float(os.environ.get("BUDGET", "200"))   # 87万那发 ~ $12；$5 的 key 会让后续腿返 429
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
GATE_VAL = None
for m in info["data"]:
    n = m.get("model_name", "")
    mit = (m.get("model_info") or {}).get("max_input_tokens")
    if n == GATE_MODEL and mit:
        GATE_VAL = int(mit)
    if any(x in n for x in MATCH):
        dist.setdefault(str(mit), []).append(n)
print(f"[{label}] max_input_tokens 分布{MATCH}: " + "; ".join(f"{k} x{len(v)}" for k, v in dist.items()))

# 探针档位**从闸门实际值算出来**，不再写死。
# 2026-09-25 的坑：这里焊着 919000/923000（922,000 时代的两侧），闸门改到 872,000 之后
# 这两发全在阈值上方 ⇒ 两条腿都 400，"PASS 腿"永远亮不了，尺子横跨了一个已经不存在的阈值。
if GATE_VAL is None:
    print(f"[{label}] ABORT: /model/info 里 {GATE_MODEL} 没有 max_input_tokens，量不出闸门在哪")
    sys.exit(2)
print(f"[{label}] 闸门实测值={GATE_VAL:,}（放行腿/拦截腿的填充物条数由它算出）")

st, k = call("/key/generate", {"models": MODELS, "key_alias": f"tmp-window-{label}-{int(time.time())}",
                              "max_budget": BUDGET, "duration": "20m"})
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
    # 闸门边界。**拦截腿必须排在放行腿前面**：放行腿那一发要烧 ~$12，
    # 预算被它吃掉后紧随的拦截腿会返 429 —— 那不是"闸门拦住了"，是判据被顶掉。
    #
    # 🔴 填充物的条数 ≠ 闸门数出来的 token 数。2026-09-25 实测：`"hello "*869000` 被闸门
    # 数成 875,801（高 0.78%），于是"应该放行"的那一发也被 400 掉 —— 看着像闸门设低了，
    # 其实是尺子自己算错。闸门用的是估算器，上游 usage 是真实分词，两者本来就不等。
    # 所以放行腿**不信任任何固定比例**：被拦了就按它自己报的数回算一次再打。
    def gate_probe(reps, expect, retries=2):
        for _ in range(retries + 1):
            t0 = time.time()
            st, r = call("/v1/chat/completions", {"model": GATE_MODEL,
                "messages": [{"role": "user", "content": "hello " * reps + "\nreply with one word: pong"}]}, key=key)
            body = r if isinstance(r, str) else json.dumps(r)
            s = f"prompt={r.get('usage',{}).get('prompt_tokens')}" if st == 200 else body[:180]
            print(f"[{label}] gate ~{reps:,} reps (expect {expect}) HTTP {st} {time.time()-t0:5.1f}s {s}")
            if st == 200 or expect == "GATE":
                return st
            m = re.search(r"(\d[\d,]*)\s*token", body)   # "本次约 875,801 token，上限 872,000"
            if not m:
                return st
            counted = int(m.group(1).replace(",", ""))
            reps = int(reps * (GATE_VAL - 3000) / counted)
            print(f"[{label}]   ↑ 闸门数成 {counted:,}，按它回算填充物 -> {reps:,} reps 重打")
        return st

    gate_probe(GATE_VAL + 2000, "GATE")
    gate_probe(GATE_VAL - 3000, "PASS")
finally:
    call("/key/delete", {"keys": [key]})
    print(f"[{label}] temp key deleted")

for m in NEIGH:
    t0 = time.time()
    st, r = call("/v1/chat/completions", {"model": m,
        "messages": [{"role": "user", "content": "say pong"}], "max_tokens": 20}, timeout=180)
    print(f"[{label}] neigh {m:26s} HTTP {st} {time.time()-t0:5.1f}s " + ("" if st == 200 else str(r)[:120]))
