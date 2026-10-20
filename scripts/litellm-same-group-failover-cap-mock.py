#!/usr/bin/env python3
"""
same_group_failover_cap 的 mock 对照（在 litellm proxy pod 里跑，不碰上游）。

场景照抄 10-20 现场：主组 6 条腿全部中流过载（MidStreamFallbackError），
fallbacks = 主组 -> [同家族也过载的组, 活组]。量每一发落到哪个组。

    kubectl cp scripts/litellm-same-group-failover-cap-mock.py <pod>:/tmp/cap_mock.py
    kubectl cp k8s/litellm-callbacks/same_group_failover_cap.py <pod>:/tmp/cap.py
    kubectl exec <pod> -- python3 /tmp/cap_mock.py /tmp/cap.py

每个场景一个 `python3 -S` 子进程（跳过 sitecustomize，拿 vanilla Router）。
对照：不装补丁 + max_fallbacks=5（= 现在线上），期望全砸主组、抛错。
"""
import os
import subprocess
import sys

CHILD = r'''
import asyncio, importlib.util, logging, os, sys, types
from collections import Counter
logging.basicConfig(level=logging.CRITICAL)
import site; site.main()
PATCH, MAXFB, PATH = sys.argv[1], int(sys.argv[2]), sys.argv[3]
import litellm
from litellm.router import Router
from litellm.exceptions import MidStreamFallbackError
_oe = Router._extract_partial_responses_usage
Router._extract_partial_responses_usage = staticmethod(
    lambda it: _oe(it) if hasattr(it, "completed_response") else None)
if PATCH != "none":
    spec = importlib.util.spec_from_file_location("cap", PATCH)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
PATCHED = getattr(Router, "_carher_same_group_cap_patched", False)

MAIN, SIBLING, ALIVE = "chatgpt-gpt-6-luna", "chatgpt-gpt-5.6-sol", "sa-grok-4.20"
GROUPS = {MAIN: 6, SIBLING: 6, ALIVE: 1}
SENT = []

from litellm.responses.streaming_iterator import BaseResponsesAPIStreamingIterator

class Fake(BaseResponsesAPIStreamingIterator):
    """上游开流后、首字节前报过载（= 10-20 现场形状），带 failed_deployment_id（生产由 streaming_output_backfill 盖）。"""
    def __init__(s, g, d): s.g, s.d, s.completed_response = g, d, None
    def __aiter__(s): return s
    async def __anext__(s):
        e = MidStreamFallbackError(message="Our servers are currently overloaded", model=s.g, llm_provider="openai",
                                   generated_content="", is_pre_first_chunk=True)
        e.failed_deployment_id = s.d
        raise e
    async def aclose(s): pass

class Good(BaseResponsesAPIStreamingIterator):
    def __init__(s): s._n, s.completed_response = 0, None
    def __aiter__(s): return s
    async def __anext__(s):
        s._n += 1
        if s._n <= 2: return types.SimpleNamespace(type="response.output_text.delta", delta="o")
        raise StopAsyncIteration
    async def aclose(s): pass

async def fake_aresponses(*a, **kw):
    # api_base 的 path 段 = deployment id（每条腿唯一），据此判落点
    did = str(kw.get("api_base", "")).rsplit("/", 1)[-1].replace("_", "#")
    SENT.append(did)
    g = did.split("#")[0]
    return Good() if g == ALIVE else Fake(g, did)

async def main():
    ml = [{"model_name": g, "litellm_params": {"model": "openai/x", "api_key": "sk-x",
                                               "api_base": "http://127.0.0.1:9/%s_%d" % (g, i)},
           "model_info": {"id": "%s#%d" % (g, i)}} for g, n in GROUPS.items() for i in range(n)]
    if PATH == "stream":
        litellm.aresponses = fake_aresponses   # Router 构造时绑定 original_generic_function，必须先换
    r = Router(model_list=ml, fallbacks=[{MAIN: [SIBLING, ALIVE]}, {SIBLING: [ALIVE]}],
               num_retries=0, enable_weighted_failover=True, max_fallbacks=MAXFB)

    if PATH == "stream":
        err = None
        try:
            it = await r.aresponses(model=MAIN, input="hi", stream=True)
            async for _ in it: pass
        except Exception as ex: err = ex
    else:
        # chat 非流式：每条腿 mock 一个 429；ALIVE 正常回
        for d in r.model_list:
            if d["model_name"] != ALIVE:
                d["litellm_params"]["mock_response"] = litellm.RateLimitError(message="overloaded", model="x", llm_provider="openai")
            else:
                d["litellm_params"]["mock_response"] = "ok"
        orig = Router._acompletion
        async def spy(self, model, messages, **kw):
            try:
                return await orig(self, model, messages, **kw)
            finally:
                SENT.append(model)
        Router._acompletion = spy
        err = None
        try: await r.acompletion(model=MAIN, messages=[{"role": "user", "content": "hi"}])
        except Exception as ex: err = ex

    per = Counter(s.split("#")[0] for s in SENT)
    print("patched=%s max_fallbacks=%d path=%s  发出=%d  结果=%s" % (
        PATCHED, MAXFB, PATH, len(SENT), "抛错 " + type(err).__name__ + ": " + str(err)[:300] if err else "成功"))
    print("   落点: " + "  ".join("%s x%d" % (g, per[g]) for g in GROUPS if per[g]))
    if PATH == "stream":
        print("   主组不同腿: %d / 发主组 %d 次" % (len({s for s in SENT if s.startswith(MAIN)}), per[MAIN]))
asyncio.run(main())
'''


def run(argv):
    path = "/tmp/.cap_child_%d.py" % os.getpid()
    with open(path, "w") as fh:
        fh.write(CHILD)
    try:
        p = subprocess.run([sys.executable, "-S", path] + argv, capture_output=True, text=True, timeout=180)
        return (p.stdout.strip() or "(无输出 rc=%d)\n%s" % (p.returncode, p.stderr[-1500:]))
    finally:
        os.unlink(path)


def main():
    patch = sys.argv[1] if len(sys.argv) > 1 else "/app/same_group_failover_cap.py"
    for path in ("stream", "chat"):
        print("=" * 70)
        for label, p, mf in (("对照(线上现状)", "none", 5), ("只抬上限", "none", 10),
                             ("补丁+上限5", patch, 5), ("补丁+上限10", patch, 10)):
            print("-- %s" % label)
            print("   " + run([p, str(mf), path]).replace("\n", "\n   "))


if __name__ == "__main__":
    main()
