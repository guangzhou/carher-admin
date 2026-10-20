#!/usr/bin/env python3
"""
兜底行为契约回归：主组整组过载时，请求必须「同组最多换 N 个号 → 沿兜底链每跳各试一次 → 活的那跳接住」。

为什么有这条（2026-10-20）
--------------------------
1.105.1 升级下线 `midstream_fallback_loop` 时只核了「上游有没有多跳中流兜底」（有），
没核那个补丁提供的另外两条行为：同组换号上限 3、总预算 10。7 条回归全是健康流量，
一条都不会逼出「主组全挂 → 兜底」——于是转正 3 天后第一次 ChatGPT 大面积过载，
同组换号吃满默认 max_fallbacks=5，跨组兜底一次都没跳，群里大面积报「rate limit exceeded」。

所以这条查的是**行为**，不是「哪个文件装了」：谁提供都行（上游 / 我们的补丁 / 新写法），
行为不在就红。升级下线补丁、helm upgrade 冲掉带外补丁、有人改 env，都会被它抓到。

怎么量
------
在车道 pod 里起一个新进程，**按该车道 /app/config.yaml 的 callbacks 顺序 import 全部补丁**
（= 线上 worker 的补丁面），用 env 里的 ROUTER_MAX_FALLBACKS 建一个真 `litellm.Router`，
把 `litellm.aresponses` 换成假上游（不出网、零费用），按生产 5.6-luna 的形状：
主组 16 腿全部「开流即过载」(MidStreamFallbackError, 带 failed_deployment_id)，
兜底链 7 跳里只有最后一跳活。走真 `Router.aresponses(stream=True)`。

判据（三条都要）：
  1. 请求成功（最后一跳接住）
  2. 主组发出次数 ≤ CAP+1（首发 + 至多 CAP 次换号）
  3. 链上每一跳恰好 1 次

对照：同一个 pod、同一个夹具，`python3 -S` 不装任何补丁（vanilla）再跑一遍并打印。
1.105.1 上它是红的（主组 6 次后放弃）——这证明夹具真能逼出那个故障。
哪天新版 vanilla 也绿了 = 上游自带了这个行为，打 NOTE（补丁可评估下线），不算红。

用法（本机，需要 ssh cltx@10.68.13.198 + sudo -n）::

    python3 scripts/litellm-198-failover-contract-check.py                 # 三条车道
    python3 scripts/litellm-198-failover-contract-check.py --lane gray     # 只查 gray
    python3 /opt/gray-console/scripts/litellm-198-failover-contract-check.py --local   # 198 上 root（驾驶台这么调）

退出码：0 全部车道通过；1 有车道不通过；2 跑不起来 / 拿不到结果（尺子坏，不许当绿）。
"""
from __future__ import annotations

import argparse
import base64
import os
import subprocess
import sys

HOST = "cltx@10.68.13.198"
NS = "litellm-product"
KUBE = "sudo -n kubectl --kubeconfig /etc/rancher/k3s/k3s.yaml -n %s" % NS
LANES = {"prod": "litellm-proxy", "gray": "litellm-proxy-gray", "guarded-old": "litellm-proxy-guarded-old"}
CAP = 3  # 同组最多换号次数（09-17 定、09-28 维持、10-20 恢复）

MAIN = "chatgpt-gpt-5.6-luna"
CHAIN = ["chatgpt-gpt-5.6-terra", "chatgpt-gpt-5.6-sol", "sa-grok-4.6", "deepseek-v4-flash-responses",
         "deepseek-v4-pro-responses", "deepseek-v4.1-flash"]

# --------------------------------------------------------------------------- pod 内
CHILD = r'''
import asyncio, json, os, sys, types
from collections import Counter
import logging; logging.disable(logging.CRITICAL)
MODE = sys.argv[1]            # live | vanilla
MAIN = sys.argv[2]; CHAIN = sys.argv[3].split(",")
if MODE == "vanilla":
    import site; site.main()  # -S 关掉了 site-packages；找回来但不跑 sitecustomize
import litellm
from litellm.router import Router
loaded, errs = [], []
if MODE == "live":
    import importlib, yaml
    sys.path.insert(0, "/app")
    cfg = yaml.safe_load(open("/app/config.yaml"))
    for cb in (cfg.get("litellm_settings") or {}).get("callbacks") or []:
        if "." not in cb:
            continue
        try:
            importlib.import_module(cb.rsplit(".", 1)[0]); loaded.append(cb)
        except Exception as e:
            errs.append("%s: %s" % (cb, type(e).__name__))
from litellm.exceptions import MidStreamFallbackError
from litellm.responses.streaming_iterator import BaseResponsesAPIStreamingIterator
ALIVE = CHAIN[-1]
GROUPS = dict([(MAIN, 16)] + [(c, 16 if c.startswith("chatgpt") else 1) for c in CHAIN])
SENT = []

# 真的上游流对象都带 _hidden_params；没有它，1.103.2 会在外面再包一层 HiddenParamsAsyncIteratorWrapper，
# isinstance(BaseResponsesAPIStreamingIterator) 失败 ⇒ 中流兜底根本不接 ⇒ 量出来的是夹具的假红。
class Dead(BaseResponsesAPIStreamingIterator):
    def __init__(s, g, d): s.g, s.d, s.completed_response, s._hidden_params = g, d, None, {"model_id": d}
    def __aiter__(s): return s
    async def __anext__(s):
        e = MidStreamFallbackError(message="Our servers are currently overloaded", model=s.g,
                                   llm_provider="openai", generated_content="", is_pre_first_chunk=True)
        e.failed_deployment_id = s.d
        raise e
    async def aclose(s): pass

class Good(BaseResponsesAPIStreamingIterator):
    def __init__(s): s.n, s.completed_response, s._hidden_params = 0, None, {}
    def __aiter__(s): return s
    async def __anext__(s):
        s.n += 1
        if s.n <= 2: return types.SimpleNamespace(type="response.output_text.delta", delta="o")
        raise StopAsyncIteration
    async def aclose(s): pass

async def fake(*a, **kw):
    did = str(kw.get("api_base", "")).rsplit("/", 1)[-1].replace("_", "#"); SENT.append(did)
    g = did.split("#")[0]
    return Good() if g == ALIVE else Dead(g, did)
litellm.aresponses = fake

async def main():
    ml = [{"model_name": g, "litellm_params": {"model": "openai/x", "api_key": "sk-x",
                                               "api_base": "http://127.0.0.1:9/%s_%d" % (g, i)},
           "model_info": {"id": "%s#%d" % (g, i)}} for g, n in GROUPS.items() for i in range(n)]
    # 生产 router_settings 的取值；max_fallbacks 不传 = 走 env ROUTER_MAX_FALLBACKS（和线上同一条路）
    r = Router(model_list=ml, fallbacks=[{MAIN: CHAIN}], routing_strategy="simple-shuffle", num_retries=5,
               retry_after=0, allowed_fails=3, cooldown_time=60, enable_weighted_failover=True)
    r._time_to_sleep_before_retry = lambda *a, **k: 0
    ok, err = False, None
    try:
        it = await r.aresponses(model=MAIN, input="hi", stream=True)
        async for _ in it: pass
        ok = True
    except Exception as e:
        err = type(e).__name__
    per = Counter(s.split("#")[0] for s in SENT)
    print(json.dumps({"mode": MODE, "ok": ok, "err": err, "max_fallbacks": r.max_fallbacks,
                      "main_sends": per[MAIN], "hops": {c: per[c] for c in CHAIN},
                      "callbacks_loaded": len(loaded), "import_errors": errs,
                      "litellm": getattr(litellm, "__version__", None) or __import__("importlib.metadata").metadata.version("litellm")}))
asyncio.run(main())
'''


def in_pod(args: argparse.Namespace) -> int:
    import json
    path = "/tmp/.failover_contract_%d.py" % os.getpid()
    with open(path, "w") as fh:
        fh.write(CHILD)
    out = {}
    try:
        for mode, flags in (("live", []), ("vanilla", ["-S"])):
            p = subprocess.run([sys.executable, *flags, path, mode, MAIN, ",".join(CHAIN)],
                               capture_output=True, text=True, timeout=300, cwd="/app")
            line = next((l for l in reversed(p.stdout.splitlines()) if l.startswith("{")), None)
            out[mode] = json.loads(line) if line else {"error": (p.stderr or "")[-400:]}
    finally:
        os.unlink(path)
    print("RESULT " + json.dumps(out))
    return 0


# --------------------------------------------------------------------------- 本机
LOCAL = False  # --local：已经在 198 上（驾驶台以 root 跑），直接 sh -c，不走 ssh


def ssh(cmd: str, stdin: str | None = None, timeout: int = 900) -> subprocess.CompletedProcess:
    argv = ["sh", "-c", cmd] if LOCAL else ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", HOST, cmd]
    return subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout)


def judge(r: dict) -> tuple[bool, list[str]]:
    why = []
    if not r.get("ok"):
        why.append("请求失败(%s)：最后一跳活着却没接住" % r.get("err"))
    if r.get("main_sends", 99) > CAP + 1:
        why.append("主组发了 %s 次 > %d：同组换号没封顶" % (r.get("main_sends"), CAP + 1))
    bad = {k: v for k, v in (r.get("hops") or {}).items() if v != 1}
    if bad:
        why.append("兜底链没有逐跳各 1 次：%s" % bad)
    if r.get("import_errors"):
        why.append("callback import 失败：%s" % r["import_errors"])
    return not why, why


def check_lane(lane: str, me: str) -> int:
    import json
    dep = LANES[lane]
    p = ssh("%s get po -l app=%s --field-selector=status.phase=Running --no-headers "
            "-o custom-columns=N:.metadata.name,DEL:.metadata.deletionTimestamp" % (KUBE, dep))
    # 正在 terminating 的旧 pod 跑的是旧配置，量它 = 量错对象
    pods = [l.split()[0] for l in p.stdout.splitlines() if l.strip() and l.split()[-1] == "<none>"]
    if not pods:
        print("STOP %-11s 找不到非 terminating 的 Running pod（%s）" % (lane, p.stderr.strip()[:200]))
        return 2
    pod = pods[0]
    b64 = base64.b64encode(me.encode()).decode()
    r = ssh("echo %s | base64 -d | %s exec -i %s -- sh -c 'cat > /tmp/.fcc.py' && "
            "%s exec %s -- sh -c 'cd /app && python3 /tmp/.fcc.py --in-pod; rc=$?; rm -f /tmp/.fcc.py; exit $rc'"
            % (b64, KUBE, pod, KUBE, pod))
    line = next((l for l in r.stdout.splitlines() if l.startswith("RESULT ")), None)
    if not line:
        print("STOP %-11s %s 没拿到结果 rc=%d %s" % (lane, pod, r.returncode, (r.stderr or r.stdout)[-300:]))
        return 2
    res = json.loads(line[7:])
    live, van = res.get("live", {}), res.get("vanilla", {})
    if "error" in live or "error" in van:
        print("STOP %-11s %s 子进程挂了：%s" % (lane, pod, (live.get("error") or van.get("error"))[:300]))
        return 2
    van_ok, _ = judge(van)
    live_ok, why = judge(live)
    desc = lambda x: "ok=%s 主组=%s 链=%s max_fallbacks=%s" % (
        x.get("ok"), x.get("main_sends"), "/".join(str(v) for v in x.get("hops", {}).values()), x.get("max_fallbacks"))
    print("     %-11s %s litellm=%s callbacks=%s" % (lane, pod, live.get("litellm"), live.get("callbacks_loaded")))
    print("       线上补丁面: %s" % desc(live))
    print("       阳性对照(vanilla): %s" % desc(van))
    if van_ok:
        # vanilla 也绿 = 上游自己已经满足契约（不是坏事），但这一轮量不出补丁有没有用——如实报，不算红
        print("NOTE %-11s vanilla 也满足契约：上游已自带该行为，补丁可评估下线" % lane)
    if not live_ok:
        print("FAIL %-11s %s" % (lane, "；".join(why)))
        return 1
    print("OK   %-11s 主组≤%d 次后逐跳兜底，最后一跳接住" % (lane, CAP + 1))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lane", action="append", choices=list(LANES))
    ap.add_argument("--in-pod", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--local", action="store_true", help="已在 198 上运行（驾驶台），不走 ssh")
    a = ap.parse_args()
    if a.in_pod:
        return in_pod(a)
    global LOCAL
    LOCAL = a.local
    me = open(os.path.abspath(__file__), encoding="utf-8").read()
    lanes = a.lane or list(LANES)
    rcs = {lane: check_lane(lane, me) for lane in lanes}
    worst = max(rcs.values())
    print("SUMMARY %s" % " ".join("%s=%s" % (k, {0: "OK", 1: "FAIL", 2: "STOP"}[v]) for k, v in rcs.items()))
    return worst


if __name__ == "__main__":
    sys.exit(main())
