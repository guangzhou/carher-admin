#!/bin/bash
# litellm-198-budget-notice-smoke.sh — 198 prod budget_notice 冒烟(③ 软拦截 + ④ 系列额度)
#
# 用法(在 198 host 上以 sudo 跑): sudo bash litellm-198-budget-notice-smoke.sh
#       指定车道(gray 零流量先验): sudo SVC=litellm-proxy-gray-nodeport bash litellm-198-budget-notice-smoke.sh
#
# 2026-10-20 加 S9~S11:② 改成总额/高配桶 80%/95% 双档 + 附 cye 按模型表 + 满后可用模型;
#   ④ 拦截文案列出可用模型。
#   🔴 本脚本只走 /v1/chat/completions。13/13 全绿时 /v1/messages 上的提醒曾一个字没注入
#   (桥接流不发 content_block_stop)。上线前后还要跑线型探针:
#   sudo [SVC=...] python3 /root/litellm-198-budget-notice-wire-probe.py   (repo scripts/budget-notice/wire-probe.py)
#
# 2026-09-24 基线:8/8 PASS。三个烧过的坑已内置:
#   - ③ 文案里的金额含预算预留层的**预估成本**(capacity patch),别断言精确
#     spend 值,只断限额值("$5.00")
#   - ④ 记账只算 response_cost>0 的请求:**免费入口(如 gpt-5.4-mini 计费 0)
#     永远测不出来**,必须用真实计费模型(wangsu7-gpt-5.6-sol ≈$0.02/次)
#   - ④ 落账是异步 logging worker,固定 sleep 不可靠 → 轮询 redis 直读
set -euo pipefail
NS=litellm-product
SVC="${SVC:-litellm-proxy}"
BASE="http://$(kubectl -n $NS get svc "${SVC}" -o jsonpath='{.spec.clusterIP}'):4000"
echo "target svc=${SVC} base=${BASE}"
MK=$(kubectl -n $NS get secret litellm-secrets-r2 -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
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
# 先列模型再把额度打满：超额 key 连 /v1/models 都被预算闸门 429（10-26 新旧三车道实测一致），反过来就拿不到模型名
st,b = call("/v1/models", key=k, method="GET")
model = json.loads(b)["data"][0]["id"]
call("/key/update", {"key":k,"spend":9.0})
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
             "models":[PMODEL, BMODEL],
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
check("S5b block lists available models (non-premium in allowlist)",
      "切换到下面任一个" in all_text(b) and BMODEL in all_text(b)
      and "查余额或者cye" in all_text(b), f"{all_text(b)[:400]}")

st,b = call("/v1/chat/completions",
            {"model":BMODEL,"messages":[{"role":"user","content":"只回一个字:好"}],"max_tokens":16}, key=kf)
check("S6 non-premium model not blocked (negative control)", st==200 and "额度已用完" not in all_text(b), f"{st} {b[:250]}")

st,b = call("/v1/chat/completions", {"model":BMODEL,"messages":[{"role":"user","content":"/查余额"}]}, key=kf)
check("S7 quota query shows families",
      st==200 and "今日用量" in all_text(b), f"{st} {b[:400]}")

# --- ② 双档提醒(流式注入)。总额 84% ⇒ 80 档;再打一发同档不重复;高配桶 redis 置 85% ⇒ 高配 80 档 ---
def redis(*args):
    return subprocess.run(["kubectl","-n","litellm-product","exec","litellm-redis-0","--",
                           "redis-cli",*args], capture_output=True, text=True).stdout.strip()

st,b = call("/key/generate", {"key_alias":"cursor-smokewarn-"+ts,"max_budget":5.0,
                               "budget_duration":"1d","models":[PMODEL, BMODEL],
                               "metadata":{"budget_family_overrides":{"premium":1.0}}})
kw=json.loads(b)["key"]; tw=hashlib.sha256(kw.encode()).hexdigest()
call("/key/update", {"key":kw,"spend":4.2})
time.sleep(3)
msg={"messages":[{"role":"user","content":"只回一个字:好"}],"max_tokens":16,"stream":True}
st,b = call("/v1/chat/completions", {"model":BMODEL, **msg}, key=kw)
t=all_text(b)
check("S9 total 80% tier injected", st==200 and "今日额度已用 84%" in t and "查余额或者cye" in t
      and "80%/95%" in t, f"{st} {t[-500:]}")
st,b = call("/v1/chat/completions", {"model":BMODEL, **msg}, key=kw)
check("S9b same tier not repeated", st==200 and "今日额度已用" not in all_text(b), f"{all_text(b)[-300:]}")

redis("SET", f"budget_notice:fam:premium:{tw}:{bj}", "0.85", "EX", "3600")
st,b = call("/v1/chat/completions", {"model":PMODEL, **msg}, key=kw)
t=all_text(b)
check("S10 premium 80% tier injected with fallback list",
      st==200 and "高配模型(Opus/Astra/Kimi-K3) 今日已用 85%" in t and "用完后仍可用" in t and BMODEL in t,
      f"{st} {t[-600:]}")
# S11:表里的名字 = 用户发的那个(这把 key 发的就是 PMODEL/BMODEL,它们本身是组名);
# 只看表的第一列(全文还有可用模型行等,不该参与判定)。
# 2026-10-20 起提醒里还有额度表、可用模型表,只切出「📈 今日按模型」那一张
sect=t.split("📈 今日按模型",1)[1] if "📈 今日按模型" in t else ""
names=[]
for ln in sect.splitlines()[1:]:
    if ln.startswith("| ") and not ln.startswith("| 模型"):
        names.append(ln.split("|")[1].strip())
    elif names and not ln.startswith("|"):
        break
check("S11 notice table rows use the names this key sent",
      bool(names) and set(names) <= {PMODEL, BMODEL}, f"{names}")
if not (names and set(names) <= {PMODEL, BMODEL}):
    print("---- S11 full notice ----\n" + t + "\n---- end ----", flush=True)
redis("DEL", f"budget_notice:fam:premium:{tw}:{bj}")
call("/key/delete", {"keys":[kw]})

st,b = call("/key/delete", {"keys":[kf]})
check("S8 cleanup", st==200, f"{st}")

print(f"\n{sum(results)}/{len(results)} PASS")
sys.exit(0 if all(results) else 1)
PY
