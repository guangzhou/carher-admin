#!/bin/bash
# litellm-aliyun-budget-notice-smoke.sh — 阿里云 carher ns budget_notice 冒烟(①②+③关态)
#
# 用法(在 226 上跑): bash litellm-aliyun-budget-notice-smoke.sh
#
# 2026-08-21 上线基线:7/7 PASS(canary 12 项的收敛版)。三个烧过的坑已内置:
#   - 真实调用必须用接纯 chat 的模型:wangsu-deepseek-v4-pro 会 400
#     "field messages is required" → 用 wangsu-glm-5.2(便宜稳定)
#   - /key/update 改 spend 后 auth 缓存 ~60s 才失效 → 改完必 sleep 70 再断言
#     (①/查余额 不受影响——mock 不读 spend 门槛;②预警/③预算门受影响)
#   - 流式 body 是 SSE 帧+中文 \uXXXX 转义 → 断言前 all_text() 重组
# 花费:约 $0.01(Q4/Q4b/Q6 三次真实小请求,其余零上游)。
set -euo pipefail
NS=carher
BASE="http://$(kubectl -n $NS get svc litellm-proxy -o jsonpath='{.spec.clusterIP}'):4000"
MK=$(kubectl -n $NS get secret litellm-secrets -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
test -n "$MK"

python3 - "$BASE" "$MK" <<'PY'
import json, sys, time, urllib.request, urllib.error
BASE, MK = sys.argv[1], sys.argv[2]
MODEL = "wangsu-glm-5.2"

def call(path, payload=None, key=None, method="POST", timeout=120):
    req = urllib.request.Request(BASE+path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization":"Bearer "+(key or MK),"Content-Type":"application/json"},
        method=method)
    try:
        r = urllib.request.urlopen(req, timeout=timeout); return r.status, r.read().decode()
    except urllib.error.HTTPError as e: return e.code, e.read().decode()

results=[]
def check(name, cond, detail=""):
    results.append(cond)
    print(("PASS " if cond else "FAIL ")+name+("" if cond else " :: "+detail[:300]), flush=True)

def all_text(body):
    texts=[]
    def walk(o):
        if isinstance(o, dict):
            for kk,v in o.items():
                if kk in ("content","text","delta") and isinstance(v,str): texts.append(v)
                else: walk(v)
        elif isinstance(o, list):
            for x in o: walk(x)
    for line in body.splitlines():
        line=line.strip()
        if line.startswith("data:"): line=line[5:].strip()
        if not line or line=="[DONE]": continue
        try: walk(json.loads(line))
        except Exception: texts.append(line)
    return "".join(texts)

ts=str(int(time.time()))
st,b = call("/key/generate", {"key_alias":"carher-smoke-"+ts,"max_budget":10.0,"budget_duration":"1d"})
assert st==200, b
k=json.loads(b)["key"]

# ① 三形态(零上游零计费)
st,b = call("/v1/chat/completions", {"model":MODEL,"messages":[{"role":"user","content":"/查余额"}]}, key=k)
check("Q1 quota chat nonstream", st==200 and "今日用量" in b and "$10.00" in b, f"{st} {b[:300]}")
st,b = call("/v1/chat/completions", {"model":MODEL,"messages":[{"role":"user","content":"/quota"}],"stream":True}, key=k)
check("Q2 quota chat stream", st==200 and "今日用量" in all_text(b), f"{st} {b[:300]}")
st,b = call("/v1/messages", {"model":MODEL,"max_tokens":64,"messages":[{"role":"user","content":"查余额"}],"stream":True}, key=k)
check("Q3 quota messages stream", st==200 and "今日用量" in all_text(b), f"{st} {b[:400]}")

# ② 90% 预警 + 去重(spend 改后等缓存)
st,_ = call("/key/update", {"key":k,"spend":9.5}); assert st==200
print("spend=9.5, waiting 70s for auth cache...", flush=True); time.sleep(70)
st,b = call("/v1/chat/completions", {"model":MODEL,"messages":[{"role":"user","content":"用一个词回答:天空是什么颜色"}],"stream":True,"max_tokens":30}, key=k)
check("Q4 warn inject 95%", st==200 and "今日额度已用" in all_text(b), f"{st} {b[:500]}")
st,b2 = call("/v1/chat/completions", {"model":MODEL,"messages":[{"role":"user","content":"用一个词回答:草是什么颜色"}],"stream":True,"max_tokens":30}, key=k)
check("Q4b warn dedupe", st==200 and "今日额度已用" not in all_text(b2), f"{st} {b2[:300]}")

# ③ 阿里云关着:超额仍原生 429(若解开 FRIENDLY_MOCK_DISABLED 此项应改断 200+额度已用完)
st,_ = call("/key/update", {"key":k,"spend":15.0}); assert st==200
print("spend=15, waiting 70s...", flush=True); time.sleep(70)
st,b = call("/v1/chat/completions", {"model":MODEL,"messages":[{"role":"user","content":"hi"}]}, key=k)
check("Q5 over-budget stays 429 (mock disabled)", st==429, f"{st} {b[:250]}")

# 非 gated 不受影响
st,b = call("/key/generate", {"key_alias":"other-smoke-"+ts,"max_budget":10.0,"budget_duration":"1d"})
k2=json.loads(b)["key"]
st,b = call("/v1/chat/completions", {"model":MODEL,"messages":[{"role":"user","content":"/查余额"}],"stream":True,"max_tokens":30}, key=k2)
check("Q6 ungated no shortcut", st==200 and "今日用量" not in all_text(b), f"{st} {b[:300]}")

st,b = call("/key/delete", {"keys":[k,k2]})
check("Q7 cleanup", st==200, f"{st} {b[:150]}")

print(f"\n{sum(results)}/{len(results)} PASS")
sys.exit(0 if all(results) else 1)
PY
