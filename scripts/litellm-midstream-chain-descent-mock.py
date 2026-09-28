#!/usr/bin/env python3
"""
中流 fallback **逐层下降**的 mock 回归夹具（带阳性对照）。

它回答的唯一问题：一发请求在 astra 组中流失败后，重发是**全砸在同一层**，
还是**同层 N 次 → 之后每层各 1 次**？

为什么必须有对照
----------------
"新版本跑出 3+1+1+1+1" 这个读数**单独看没有意义** —— 夹具本身可能把
`fallbacks` 喂错、可能根本没触发 MidStreamFallbackError，两种情况都会
产出看着漂亮的数字。判据只能是**同一套夹具**下「改动前 vs 改动后」的差：

    改动前：10 发全落第 1 跳，抛错          ← 这就是 2026-09-29 要修的病
    改动后：3 发同层 + 每层各 1 发，命中即成功

所以 `--control` 不给就**不出结论**（见 [[feedback_synthetic_red_is_as_untrusted_as_synthetic_green]]）。
对照文件取改动前**线上在跑的那版**：

    git show 32a66b1:k8s/litellm-callbacks/midstream_fallback_loop.py > /tmp/prepatch.py
    # sha256 前 12 位应为 8bf25d663a10，且 _MAX_MIDSTREAM_FAILOVERS = 10

⚠️ **别取错一格**：`a62bac8`（sha `e72b6f395172`）是 09-28 抬预算**之前**的版本，
上限还是 5，拿它当对照会跑出「对照 5 发」而不是线上真实的 10 发 ——
对照的绝对数字变小，读上去像"病没那么严重"。选对照 commit 的判据是
**它的 `_MAX_MIDSTREAM_FAILOVERS` 必须与当时线上一致**，脚本会核这一条。

⚠️ 对照文件必须是**改动前**的内容。如果你已经把新版本推上生产，`/app/` 里那份
就是新版，拿它当对照会得到「对照==新版」的假绿 —— 脚本会检测到并报废该轮。

跑法（在 litellm proxy pod 里，容器里有 litellm，无 curl 也不影响）
------------------------------------------------------------------
    kubectl -n litellm-product cp scripts/litellm-midstream-chain-descent-mock.py \
        <pod>:/tmp/chain_mock.py
    kubectl -n litellm-product exec <pod> -- python3 /tmp/chain_mock.py descend \
        --module /app/midstream_fallback_loop.py --control /tmp/prepatch.py
    kubectl -n litellm-product exec <pod> -- python3 /tmp/chain_mock.py guard \
        --module /app/midstream_fallback_loop.py

为什么子进程用 `python3 -S`
---------------------------
`sitecustomize.py` 在 import 时就会把一堆 Router 补丁装上（含被测模块本身）。
`-S` 跳过它，拿到 vanilla Router，再 `site.main()` 把 site-packages 搜索路径找回来
（`-S` 把那个也一起关了）。同进程内**无法**卸载已装的补丁
（`mappingproxy` 不可写 + 原方法引用已被覆盖），所以一个场景一个子进程。

⚠️ 这里量的是**夹具里的 Router 行为**，不是生产 worker 的行为。
生产侧要验只能等真实中流失败，别为了验它去打坏上游
（[[feedback_198_mock_fallback_forcers_ignored]]：mock_testing_fallbacks 在 198 上是硬 400）。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

# 与 DB router_settings.fallbacks 里 astra 那一行保持一致（2026-09-29 实测值）。
# 形状 B：一行五个目标。⚠️ 不要拆成五行「每组指向下一组」—— 去重表会让它退化成 1 次重试。
START = "chatgpt-gpt-6-astra"
HOPS = [
    "chatgpt-gpt-6-sol",
    "chatgpt-gpt-6-luna",
    "chatgpt-gpt-5.6-sol",
    "sa-grok-4.20",
    "openrouter-deepseek-v4.1-flash",
]

# 场景：哪一层是活的。NONE = 全死（看重发形状），其余 = 看是否真的下降到那一层。
CASES = [
    ("全层皆死", "NONE"),
    ("只第5层活", HOPS[4]),
    ("只第2层活", HOPS[1]),
]

GUARD_BREAKS = [
    ("router", "硬拒装", "Router 方法体指纹对不上 → 补丁不能装（装了会改错行为）"),
    ("cls", "硬拒装", "AttemptedFallbackTargets 不存在 → 注入代码 NameError，必须拦在安装期"),
    ("fb", "装 + 告警", "只是源码文本读不到 → 量具坏 ≠ 上游变，拒装会关掉能用的补丁"),
]

# --------------------------------------------------------------------------
# 子进程 A：逐层下降。argv = <module_path> <alive_group|NONE>
# --------------------------------------------------------------------------
_CHILD_DESCEND = r'''
import asyncio, importlib.util, logging, sys, types
from collections import Counter
logging.basicConfig(level=logging.CRITICAL, stream=sys.stderr)
import site; site.main()      # -S 关掉了 site-packages 搜索路径，找回来；但不跑 sitecustomize
from litellm.router import Router
from litellm.exceptions import MidStreamFallbackError

# vanilla Router 的用量提取会对我们的假迭代器抛 AttributeError，绕开它（不影响被测逻辑）
_oe = Router._extract_partial_responses_usage
Router._extract_partial_responses_usage = staticmethod(
    lambda it: _oe(it) if hasattr(it, "completed_response") else None)

MODPATH, ALIVE = sys.argv[1], (None if sys.argv[2] == "NONE" else sys.argv[2])
START, HOPS = sys.argv[3], sys.argv[4].split(",")
SENT = []

class Fake:
    """先吐一个 delta（=已开始下发内容，构成"中流"），再抛 MidStreamFallbackError。"""
    def __init__(s, g, d):
        s.g, s.d, s._n, s.completed_response = g, d, 0, None
    def __aiter__(s): return s
    async def __anext__(s):
        s._n += 1
        if s._n == 1:
            return types.SimpleNamespace(type="response.output_text.delta", delta="t")
        e = MidStreamFallbackError(message="overloaded", model=s.g, llm_provider="openai")
        e.generated_content = "x"; e.is_pre_first_chunk = False; e.failed_deployment_id = s.d
        raise e

class Good:
    def __init__(s): s._n, s.completed_response = 0, None
    def __aiter__(s): return s
    async def __anext__(s):
        s._n += 1
        if s._n <= 2:
            return types.SimpleNamespace(type="response.output_text.delta", delta="o")
        raise StopAsyncIteration

spec = importlib.util.spec_from_file_location("mst", MODPATH)
m = importlib.util.module_from_spec(spec); sys.modules["mst"] = m
spec.loader.exec_module(m)          # import 副作用 = 装补丁
PATCHED = getattr(Router, "_carher_midstream_loop_patched", False)

async def main():
    ml = [{"model_name": g,
           "litellm_params": {"model": "openai/gpt-4o-mini", "api_key": "sk-x",
                              "api_base": "http://127.0.0.1:9000"},
           "model_info": {"id": g + "#0"}} for g in [START] + HOPS]
    r = Router(model_list=ml, fallbacks=[{START: list(HOPS)}], num_retries=2)

    async def send(*a, **kw):
        g = str(kw.get("model") or "?"); SENT.append(g)
        return Good() if (ALIVE and g == ALIVE) else Fake(g, g + "#0")
    # 钩在"真的要发给上游"那一层：记下每次重发落到哪个组
    r._ageneric_api_call_with_fallbacks_helper = send

    it = await Router._aresponses_streaming_iterator(
        r, Fake(START, START + "#0"),
        {"model": START, "input": "hi", "litellm_trace_id": "CHAINMOCK"})
    got, err = 0, None
    try:
        async for _ in it:
            got += 1
            if got > 400:
                break
    except Exception as ex:
        err = ex
    order = []
    for g in SENT:
        if not order or order[-1] != g:
            order.append(g)
    print("PATCH_INSTALLED=%s 重发=%d %s" % (PATCHED, len(SENT), "抛错" if err else "成功"))
    for g in order:
        print("      %-34s x%d" % (g, Counter(SENT)[g]))

asyncio.run(main())
'''

# --------------------------------------------------------------------------
# 子进程 B：守卫。破坏一个指纹，看补丁装没装。argv = <module_path> <which>
# --------------------------------------------------------------------------
_CHILD_GUARD = r'''
import importlib.util, sys, site; site.main()
MODPATH, WHICH = sys.argv[1], sys.argv[2]
from litellm.router import Router
import litellm.router_utils.fallback_event_handlers as F

if WHICH == "router":
    # 方法体换成别的东西 => 源码指纹对不上
    Router._aresponses_streaming_iterator = lambda self, a, b: None
elif WHICH == "fb":
    # 让 `kwargs.get("attempted_targets")` 这段文本从源码里消失（仅文本，不改行为）
    def run_async_fallback(*a, **k): return None
    F.run_async_fallback = run_async_fallback
elif WHICH == "cls":
    del F.AttemptedFallbackTargets

spec = importlib.util.spec_from_file_location("mst", MODPATH)
m = importlib.util.module_from_spec(spec); sys.modules["mst"] = m
try:
    spec.loader.exec_module(m)
except Exception as exc:
    print("exec_module 抛错: %s: %s" % (type(exc).__name__, exc))
print("PATCH_INSTALLED=%s" % getattr(Router, "_carher_midstream_loop_patched", False))
'''


def _run_child(code: str, argv: list[str]) -> str:
    """一个场景一个子进程。-S 跳过 sitecustomize，拿 vanilla Router。"""
    path = "/tmp/.chain_mock_child_%d.py" % os.getpid()
    with open(path, "w") as fh:
        fh.write(code)
    try:
        p = subprocess.run([sys.executable, "-S", path] + argv,
                           capture_output=True, text=True, timeout=180)
        out = (p.stdout or "").strip()
        if not out:
            return "（子进程无输出，returncode=%d）\n%s" % (
                p.returncode, (p.stderr or "")[-600:])
        return out
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def cmd_descend(args: argparse.Namespace) -> int:
    if not args.control:
        print("拒跑：没给 --control。")
        print("  只有新版本的读数说明不了任何事 —— 夹具喂错 fallbacks / 根本没触发")
        print("  中流失败，都会产出看着漂亮的数字。判据只能是「改动前 vs 改动后」的差。")
        print("  取对照：git show 32a66b1:k8s/litellm-callbacks/midstream_fallback_loop.py > /tmp/prepatch.py")
        return 2

    import hashlib
    import re
    shas, budgets = {}, {}
    for label, path in (("对照", args.control), ("新版", args.module)):
        with open(path, "rb") as fh:
            blob = fh.read()
        shas[label] = hashlib.sha256(blob).hexdigest()[:12]
        mo = re.search(rb"^_MAX_MIDSTREAM_FAILOVERS\s*=\s*(\d+)", blob, re.M)
        budgets[label] = int(mo.group(1)) if mo else None
        print("%s  %s  sha=%s  预算=%s" % (label, path, shas[label], budgets[label]))

    if shas["对照"] == shas["新版"]:
        print("\n⛔ 对照文件与被测文件内容相同 —— 这轮报废。")
        print("   常见原因：新版本已推上生产，`/app/` 里那份就是新版。")
        print("   对照必须取改动前那版（sha 应为 8bf25d663a10）。")
        return 2

    # 对照必须与被测同预算，否则"对照打几发"这个数字是错的。
    # 09-29 实际踩到：错取了抬预算前的 commit，对照跑出 5 发而非线上真实的 10 发。
    if budgets["对照"] != budgets["新版"]:
        print("\n⛔ 两边 _MAX_MIDSTREAM_FAILOVERS 不同（对照 %s vs 新版 %s）—— 这轮报废。"
              % (budgets["对照"], budgets["新版"]))
        print("   对照的重发次数上限必须与被测一致，否则量到的差里混着预算差，")
        print("   而不是「下降 vs 不下降」的差。换一个同预算的对照 commit。")
        return 2

    argvs = "%s|%s" % (START, ",".join(HOPS))
    print("\n链子：%s → %s" % (START, " → ".join(HOPS)))
    rows = []
    for title, alive in CASES:
        print("\n" + "=" * 64)
        print("场景：%s（活着的层 = %s）" % (title, alive))
        for label, path in (("对照(改动前)", args.control), ("新版(改动后)", args.module)):
            out = _run_child(_CHILD_DESCEND, [path, alive, START, ",".join(HOPS)])
            first = out.splitlines()[0] if out.splitlines() else "?"
            print("\n  -- %s --" % label)
            for line in out.splitlines():
                print("     " + line)
            rows.append((title, label, first))

    print("\n" + "=" * 64)
    print("汇总（判据：对照恒 10 发砸同一层，新版应为 3 发同层 + 每层各 1 发）")
    for title, label, first in rows:
        print("  %-12s %-14s %s" % (title, label, first))
    print("\n⚠️ 这只证明夹具里的 Router 行为变了。生产 worker 侧没验 —— 要验得让上游真中流失败。")
    _ = argvs
    return 0


def cmd_guard(args: argparse.Namespace) -> int:
    print("被测文件：%s" % args.module)
    print("\n守卫应当分两级：缺了就跑不起来的硬拒装，只是读不到文本的只告警。")
    print("理由见 [[feedback_guard_must_warn_not_refuse_when_its_ruler_breaks]] ——")
    print("2026-09-29 第一版把两者混为一谈，坏尺子把 4/4 生产副本的补丁关停了。\n")
    print("  先跑一遍不破坏的基线：")
    out = _run_child(_CHILD_GUARD, [args.module, "none"])
    print("     " + out.replace("\n", "\n     "))
    print("     期望 PATCH_INSTALLED=True\n")
    for which, expect, why in GUARD_BREAKS:
        print("  破坏 %-7s 期望=%-8s %s" % (which, expect, why))
        out = _run_child(_CHILD_GUARD, [args.module, which])
        print("     " + out.replace("\n", "\n     "))
    print("\n⚠️ 这里的 PASS 不能替代「上生产后逐副本 grep 日志」——")
    print("   exec 起的干净进程与 uvicorn worker 行为可以不一致（09-29 实测就不一致，真因未查明）。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("descend", help="逐层下降（必须带 --control 阳性对照）")
    d.add_argument("--module", default="/app/midstream_fallback_loop.py")
    d.add_argument("--control", help="改动前那版的路径（缺省则拒跑）")
    d.set_defaults(fn=cmd_descend)

    g = sub.add_parser("guard", help="两道指纹守卫的分级行为")
    g.add_argument("--module", default="/app/midstream_fallback_loop.py")
    g.set_defaults(fn=cmd_guard)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
