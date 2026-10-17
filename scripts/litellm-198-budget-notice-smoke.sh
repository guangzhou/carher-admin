#!/bin/bash
# litellm-198-budget-notice-smoke.sh — 198 prod budget_notice 冒烟(③ 软拦截 + ④ 系列额度)
#
# 用法(在 198 host 上以 sudo 跑): sudo bash litellm-198-budget-notice-smoke.sh
#
# 2026-09-24 基线:8/8 PASS。三个烧过的坑已内置:
#   - ③ 文案里的金额含预算预留层的**预估成本**(capacity patch),别断言精确
#     spend 值,只断限额值("$5.00")
#   - ④ 记账只算 response_cost>0 的请求:**免费入口(如 gpt-5.4-mini 计费 0)
#     永远测不出来**,必须用真实计费模型(wangsu7-gpt-5.6-sol ≈$0.02/次)
#   - ④ 落账是异步 logging worker,固定 sleep 不可靠 → 轮询 redis 直读
set -euo pipefail
NS=litellm-product
BASE="http://$(kubectl -n $NS get svc litellm-proxy -o jsonpath='{.spec.clusterIP}'):4000"
MK=$(kubectl -n $NS get secret litellm-secrets -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
BILLABLE_MODEL="wangsu7-gpt-5.6-sol"   # 非高配、真实计费、便宜
PREMIUM_MODEL="claude-opus-4.8"       # 高配桶(Opus)、真实计费 ~$0.0005/次

python3 - "$BASE" "$MK" "$BILLABLE_MODEL" "$PREMIUM_MODEL" <<'PY'
import datetime, hashlib, json, subprocess, sys, time, urllib.request, urllib.error
BASE, MK, BMODEL, PMODEL = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

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

# --- ④ 系列额度(2026-10-14 起 cursor-* 全走高配桶:只记 Opus/Astra,其余模型不记账不拦) ---
# 2026-10-17 改:旧版断言「cursor key 打 wangsu7-gpt-5.6-sol 进 other 桶被拦」,10-14 规则变了以后
# 新旧两个版本都同样红(S4~S6),是用例过期不是代码坏。现在:override premium 压小,用真实计费的
# opus 组打一发 ⇒ 落账 ⇒ 第二发被拦;非高配模型同一把 key 照常放行(阴性对照)。
st,b = call("/key/generate",
            {"key_alias":"cursor-smokefam-"+ts,"max_budget":5.0,"budget_duration":"1d",
             "metadata":{"budget_family_overrides":{"premium":0.000001}}})
kf=json.loads(b)["key"]
token = hashlib.sha256(kf.encode()).hexdigest()

st,b = call("/v1/chat/completions",
            {"model":PMODEL,"messages":[{"role":"user","content":"只回一个字:好"}],"max_tokens":16}, key=kf)
check("S3 premium first call passes", st==200 and "额度已用完" not in all_text(b), f"{st} {b[:250]}")

bj = (datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=8)).strftime("%Y%m%d")
rkey = f"budget_notice:fam:premium:{token}:{bj}"
val=None
for i in range(12):
    time.sleep(5)
    out = subprocess.run(["kubectl","-n","litellm-product","exec","litellm-redis-0","--",
                          "redis-cli","GET",rkey], capture_output=True, text=True).stdout.strip()
    if out:
        val=out; print(f"accounting landed ~{(i+1)*5}s: {out}", flush=True); break
check("S4 premium accounting landed in redis", val is not None, f"key={rkey}")

st,b = call("/v1/chat/completions", {"model":PMODEL,"messages":[{"role":"user","content":"hi"}]}, key=kf)
check("S5 premium second call blocked 200",
      st==200 and "额度已用完" in all_text(b) and "不受影响" in all_text(b), f"{st} {b[:350]}")

st,b = call("/v1/chat/completions",
            {"model":BMODEL,"messages":[{"role":"user","content":"只回一个字:好"}],"max_tokens":16}, key=kf)
check("S6 non-premium model not blocked (negative control)", st==200 and "额度已用完" not in all_text(b), f"{st} {b[:250]}")

st,b = call("/v1/chat/completions", {"model":BMODEL,"messages":[{"role":"user","content":"/查余额"}]}, key=kf)
check("S7 quota query shows families",
      st==200 and "今日用量" in all_text(b), f"{st} {b[:400]}")

st,b = call("/key/delete", {"keys":[kf]})
check("S8 cleanup", st==200, f"{st}")

print(f"\n{sum(results)}/{len(results)} PASS")
sys.exit(0 if all(results) else 1)
PY
