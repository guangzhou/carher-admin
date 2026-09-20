#!/bin/bash
# litellm-198-budget-notice-smoke.sh — 198 prod budget_notice 冒烟(③ 软拦截 + ④ 系列额度)
#
# 用法(在 198 host 上以 sudo 跑): sudo bash litellm-198-budget-notice-smoke.sh
#
# 2026-08-22 基线:8/8 PASS。三个烧过的坑已内置:
#   - ③ 文案里的金额含预算预留层的**预估成本**(capacity patch),别断言精确
#     spend 值,只断限额值("$5.00")
#   - ④ 记账只算 response_cost>0 的请求:**免费入口(如 gpt-5.4-mini 计费 0)
#     永远测不出来**,必须用真实计费模型(wangsu7-gpt-5.6-sol ≈$0.02/次)
#   - ④ 落账是异步 logging worker,固定 sleep 不可靠 → 轮询 redis 直读
set -euo pipefail
NS=litellm-product
BASE="http://$(kubectl -n $NS get svc litellm-proxy -o jsonpath='{.spec.clusterIP}'):4000"
MK=$(kubectl -n $NS get secret litellm-secrets -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
BILLABLE_MODEL="wangsu7-gpt-5.6-sol"   # other 族、真实计费、便宜

python3 - "$BASE" "$MK" "$BILLABLE_MODEL" <<'PY'
import datetime, hashlib, json, subprocess, sys, time, urllib.request, urllib.error
BASE, MK, BMODEL = sys.argv[1], sys.argv[2], sys.argv[3]

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
    print(("PASS " if cond else "FAIL ")+name+("" if cond else " :: "+detail[:350]), flush=True)

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

# --- ③ key 总额度软拦截(三道闸门都要被吞;金额只断限额) ---
st,b = call("/key/generate", {"key_alias":"claude-code-smoke-"+ts,"max_budget":5.0,"budget_duration":"1d"})
k=json.loads(b)["key"]
call("/key/update", {"key":k,"spend":9.0})
st,b = call("/v1/models", key=k, method="GET")
model = json.loads(b)["data"][0]["id"]
st,b = call("/v1/chat/completions", {"model":model,"messages":[{"role":"user","content":"hello"}]}, key=k)
check("S1 key over-budget 200", st==200 and "额度已用完" in all_text(b) and "$5.00" in all_text(b), f"{st} {b[:300]}")
st,b = call("/v1/responses", {"model":model,"input":"hello","stream":True}, key=k)
check("S2 key over-budget responses stream 200", st==200 and "额度已用完" in all_text(b), f"{st} {b[:300]}")
call("/key/delete", {"keys":[k]})

# --- ④ 系列额度:override 压小 gpt_other,真实计费模型,轮询 redis 等落账 ---
st,b = call("/key/generate",
            {"key_alias":"claude-code-smokefam-"+ts,"max_budget":5.0,"budget_duration":"1d",
             "metadata":{"budget_family_overrides":{"other":0.000001}}})
kf=json.loads(b)["key"]
token = hashlib.sha256(kf.encode()).hexdigest()

st,b = call("/v1/chat/completions",
            {"model":BMODEL,"messages":[{"role":"user","content":"只回一个字:好"}],"max_tokens":16}, key=kf)
check("S3 family first call passes", st==200 and "额度已用完" not in all_text(b), f"{st} {b[:250]}")

bj = (datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=8)).strftime("%Y%m%d")
rkey = f"budget_notice:fam:other:{token}:{bj}"
val=None
for i in range(12):
    time.sleep(5)
    out = subprocess.run(["kubectl","-n","litellm-product","exec","litellm-redis-0","--",
                          "redis-cli","GET",rkey], capture_output=True, text=True).stdout.strip()
    if out:
        val=out; print(f"accounting landed ~{(i+1)*5}s: {out}", flush=True); break
check("S4 accounting landed in redis", val is not None, f"key={rkey}")

st,b = call("/v1/chat/completions", {"model":BMODEL,"messages":[{"role":"user","content":"hi"}]}, key=kf)
check("S5 family second call blocked 200",
      st==200 and "其他模型 额度已用完" in all_text(b) and "不受影响" in all_text(b), f"{st} {b[:350]}")

st,b = call("/v1/chat/completions",
            {"model":"gpt-5.3-codex","messages":[{"role":"user","content":"只回一个字:好"}],"max_tokens":16}, key=kf)
check("S6 gpt53 family unaffected", st==200 and "额度已用完" not in all_text(b), f"{st} {b[:250]}")

# 非 gpt 模型也共用 other 桶(08-22 起):挑一个 deepseek/claude/glm 组,应同样被拦
st,b = call("/v1/models", key=kf, method="GET")
models=[m["id"] for m in json.loads(b)["data"]]
nongpt = next((m for m in models if any(x in m.lower() for x in ("deepseek","claude","glm")) and "gpt" not in m.lower()), None)
if nongpt:
    st,b = call("/v1/chat/completions", {"model":nongpt,"messages":[{"role":"user","content":"hi"}]}, key=kf)
    check("S6b non-gpt shares other bucket", st==200 and "其他模型 额度已用完" in all_text(b), f"{nongpt} {st} {b[:250]}")

st,b = call("/v1/chat/completions", {"model":BMODEL,"messages":[{"role":"user","content":"/查余额"}]}, key=kf)
check("S7 quota query shows families",
      st==200 and "今日用量" in all_text(b) and "系列额度" in all_text(b), f"{st} {b[:400]}")

st,b = call("/key/delete", {"keys":[kf]})
check("S8 cleanup", st==200, f"{st}")

print(f"\n{sum(results)}/{len(results)} PASS")
sys.exit(0 if all(results) else 1)
PY
