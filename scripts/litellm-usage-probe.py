#!/usr/bin/env python3
"""LiteLLM「响应里没有 usage」通用诊断探针（只读，不改任何配置）。

这个脚本是给**任何一套 LiteLLM Proxy** 用的：只要你能拿到
`base_url` + 一把 key + 一个能跑的模型名，就能跑，不需要 kubectl、
不需要进集群、不读配置文件。

为什么不能只发一个请求看有没有 usage
------------------------------------
「没有 usage」这句话没有唯一含义。至少有三种完全不同的情况，
处置也完全不同：

  A 非流式                        -> 必须有 usage。没有 = 真 bug
  B 流式 + include_usage:true     -> 必须有末帧 usage。没有 = 真 bug
  C 流式 + 不带 include_usage     -> 没有 usage 是 **OpenAI 兼容协议的规定行为**，
                                     不是故障。绝大多数「每次都没有 usage」
                                     的报告都落在这一档。
  D 流式 + include_usage:false    -> 同 C

只打一发请求，你分不清自己在 A/B/C/D 的哪一档，于是必然把 C 误判成 bug，
或者把真 bug 当成"客户端没传参数"糊弄过去。所以这个脚本**四档一起打**。

怎么修 C
--------
两条路，选一条：
  1. 客户端侧：每次流式请求都带 `stream_options: {"include_usage": true}`。
     缺点是你管不住所有客户端（很多 SDK / 第三方工具根本不给你这个口子）。
  2. 服务端侧（推荐）：在 LiteLLM Proxy 的 config.yaml 里打开
        general_settings:
          always_include_stream_usage: true
     LiteLLM 会在每一次流式上游调用上自动注入该 flag，客户端什么都不用改。

改完之后**再跑一次这个脚本，加 `--mode after`**：那时候用例 C 必须从
MISSING 变成 OK —— 这是唯一的验收判据。配置里有那一行、pod 重启过、
日志没报错，这三件事都不证明它生效了。

判据说明（重要）
----------------
只认 usage 的**实际数值**，不认「字段存在」。LiteLLM 在拿不到上游 usage 时
会回一个 `prompt_tokens=0` 的空壳，那种形状对计费、对上下文窗口显示来说
和没有 usage 完全一样，所以单列成 ZERO 一档，不和 OK 混。

用法
----
  export LITELLM_KEY=sk-...
  scripts/litellm-usage-probe.py --base https://your-proxy.example.com --model gpt-4o
  scripts/litellm-usage-probe.py --base http://127.0.0.1:4000 --model gpt-4o --mode after
  scripts/litellm-usage-probe.py --selftest          # 只验尺子，不打任何真实请求
  scripts/litellm-usage-probe.py ... --json out.json # 机器可读结果

副作用：会产生 4~N 次真实推理调用（会计费）。故默认 max_tokens=16。
不写配置、不改 key、不 rollout。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request


# ---------------------------------------------------------------- HTTP

def post(base: str, path: str, key: str, payload: dict, timeout: int):
    """返回 (http_code, raw_body_text, elapsed_s)。错误响应也要拿到 body。"""
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace"), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), time.time() - t0
    except Exception as e:
        return 0, f"<transport error: {type(e).__name__}: {e}>", time.time() - t0


# ---------------------------------------------------------------- 判据

def classify_usage(usage) -> tuple[str, str]:
    """把 usage 判成 OK / ZERO / MISSING / MALFORMED，并给一句人话。

    ZERO 单独成档是这个脚本的核心判据：LiteLLM 在拿不到上游 usage 时会回一个
    prompt_tokens=0 的空壳。「字段存在」但计数是 0，对计费和对上下文窗口显示
    来说和没有一样 —— 把它算成 OK 就是自欺。
    """
    if usage is None:
        return "MISSING", "响应体里没有 usage 字段"
    if not isinstance(usage, dict):
        return "MALFORMED", f"usage 不是对象而是 {type(usage).__name__}"
    pt = usage.get("prompt_tokens")
    ct = usage.get("completion_tokens")
    tt = usage.get("total_tokens")
    if pt is None and ct is None and tt is None:
        return "MALFORMED", f"usage 存在但三个计数全缺: {sorted(usage)}"
    if not pt and not ct:
        return "ZERO", f"usage 是空壳 prompt={pt} completion={ct}（等于没有）"
    extra = []
    ptd = usage.get("prompt_tokens_details") or {}
    ctd = usage.get("completion_tokens_details") or {}
    if isinstance(ptd, dict) and ptd.get("cached_tokens") is not None:
        extra.append(f"cached={ptd['cached_tokens']}")
    if isinstance(ctd, dict) and ctd.get("reasoning_tokens") is not None:
        extra.append(f"reasoning={ctd['reasoning_tokens']}")
    tail = ("  [" + " ".join(extra) + "]") if extra else ""
    return "OK", f"prompt={pt} completion={ct} total={tt}{tail}"


def extract_nonstream_usage(body: str):
    try:
        return json.loads(body).get("usage"), None
    except Exception as e:
        return None, f"响应体不是合法 JSON: {e}"


def extract_stream_usage(body: str):
    """从 SSE 流里找 usage。

    OpenAI 兼容流式把 usage 放在**末尾**一个 choices 为空的 chunk 里
    （带 include_usage 时）。所以要扫所有 data: 行，取最后一个带 usage 的，
    不能只看第一帧、也不能只看最后一行（最后一行通常是 [DONE]）。
    """
    found = None
    chunks = 0
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            continue
        chunks += 1
        try:
            obj = json.loads(payload)
        except Exception:
            continue
        if isinstance(obj, dict) and obj.get("usage") is not None:
            found = obj["usage"]
    return found, chunks


# ---------------------------------------------------------------- 用例表
#
# expect 是**应然**，不是「我希望」。两种 mode 下 C/D 的应然不同，这正是
# 打开 always_include_stream_usage 前后的唯一可观测差别：
#
#   mode=before  C/D 没有 usage  -> 协议正确，不是故障
#   mode=after   C 必须有 usage  -> 服务端注入生效的唯一证据
#
# D（客户端显式写 include_usage:false）在 after 下期望值留空：LiteLLM 会不会
# 覆盖一个显式的 false，本脚本不做断言 —— 如实打印，由读的人判断。

CASES = [
    # id, 描述,                          stream, include_usage, expect_before, expect_after
    ("A", "非流式",                       False, None,  "OK",      "OK"),
    ("B", "流式 + include_usage:true",     True,  True,  "OK",      "OK"),
    ("C", "流式 + 不带 include_usage",     True,  None,  "MISSING", "OK"),
    ("D", "流式 + include_usage:false",    True,  False, "MISSING", None),
]

MISSING_KINDS = ("MISSING", "ZERO", "MALFORMED")


def build_payload(model, stream, include_usage, max_tokens, prompt) -> dict:
    p: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "stream": stream,
    }
    if stream and include_usage is not None:
        p["stream_options"] = {"include_usage": include_usage}
    return p


def run_cases(base, key, model, max_tokens, prompt, repeat, timeout, mode) -> list[dict]:
    results = []
    print(f"\n{'=' * 78}")
    print(f"base={base}   model={model}   mode={mode}")
    print(f"{'=' * 78}")
    for cid, label, stream, inc_usage, exp_b, exp_a in CASES:
        expect = exp_a if mode == "after" else exp_b
        for rep in range(1, repeat + 1):
            tag = f"{cid}{'' if repeat == 1 else f'.{rep}'}"
            payload = build_payload(model, stream, inc_usage, max_tokens, prompt)
            code, body, secs = post(base, "/v1/chat/completions", key, payload, timeout)
            rec = {
                "case": cid, "rep": rep, "label": label, "stream": stream,
                "include_usage": inc_usage, "http": code,
                "elapsed_s": round(secs, 2), "expect": expect,
            }
            if code != 200:
                rec["verdict"] = "HTTP_ERR"
                rec["detail"] = body[:400].replace("\n", " ")
                print(f"[{tag}] {label:30s} HTTP {code}  {rec['detail'][:110]}")
                results.append(rec)
                continue
            if stream:
                usage, chunks = extract_stream_usage(body)
                rec["sse_chunks"] = chunks
            else:
                usage, err = extract_nonstream_usage(body)
                if err:
                    rec["verdict"] = "MALFORMED"
                    rec["detail"] = err
                    print(f"[{tag}] {label:30s} {err}")
                    results.append(rec)
                    continue
            verdict, human = classify_usage(usage)
            rec.update(verdict=verdict, detail=human, usage=usage)
            extra = f"  sse_chunks={rec['sse_chunks']}" if stream else ""
            flag = ""
            if expect and verdict != expect:
                flag = f"   <== 应然是 {expect}"
            print(f"[{tag}] {label:30s} HTTP 200  {verdict:8s} {human}{extra}{flag}")
            results.append(rec)
    return results


def summarize(results: list[dict], mode: str) -> int:
    """打印结论。exit code: 0=通过, 1=发现真问题。"""
    print(f"\n{'=' * 78}")
    print("结论")
    print(f"{'=' * 78}")

    http_err = [r for r in results if r["verdict"] == "HTTP_ERR"]
    if http_err:
        print("\n有请求根本没打通（HTTP != 200），下面的结论全部不成立：")
        for r in http_err:
            print(f"  - 用例 {r['case']} HTTP {r['http']}: {r['detail'][:160]}")
        print("\n先把 base / key / model 名弄对再谈 usage。")
        return 1

    real_bugs, gate_fail = [], []
    for cid, label, _s, _iu, exp_b, exp_a in CASES:
        expect = exp_a if mode == "after" else exp_b
        got = [r for r in results if r["case"] == cid]
        if not got:
            continue
        vs = sorted({r["verdict"] for r in got})
        note = f"应然: {expect}" if expect else "应然: 不断言（如实打印）"
        line = f"  {cid} {label:30s} -> {','.join(vs):12s} ({note})"
        bad = any(r["verdict"] in MISSING_KINDS for r in got)
        if expect == "OK" and bad:
            if cid in ("A", "B"):
                line += "   <<< 真问题"
                real_bugs.append((cid, label, vs))
            else:
                line += "   <<< 验收未通过"
                gate_fail.append((cid, label, vs))
        print(line)

    print("\n" + "-" * 78)

    if real_bugs:
        print("判定：**真 bug**。应当无条件返回 usage 的用例没有返回：")
        for cid, label, vs in real_bugs:
            print(f"  - 用例 {cid}（{label}）-> {','.join(vs)}")
        print("\n这不是 include_usage 的问题（A/B 已经显式要 usage 了）。")
        print("往上游查：该 deployment 的 provider 是不是把 usage 吞了；")
        print("换一个同 proxy 上的别的模型对照，分清是模型问题还是 proxy 问题。")
        print("注意 ZERO 也算缺失：prompt_tokens=0 的空壳等于没有 usage。")
        return 1

    if mode == "after":
        if gate_fail:
            print("判定：**验收未通过**。already 改了配置，但用例 C（客户端不带")
            print("include_usage 的流式请求）仍然没有 usage：")
            for cid, label, vs in gate_fail:
                print(f"  - 用例 {cid}（{label}）-> {','.join(vs)}")
            print("\n配置里有那一行 ≠ 它生效了。按顺序核这三件事：")
            print("  1. 那一行是不是在 `general_settings:` 下面（不是 litellm_settings）")
            print("  2. 进程有没有真的重新加载到这份配置（重启/滚动更新完成没有）")
            print("  3. 容器里 /app/config.yaml（或你的挂载路径）的内容是不是你改的那份")
            print("     —— 判据是容器内实际文件，不是仓库里的文件、不是 ConfigMap 的 YAML")
            return 1
        print("判定：**验收通过**。客户端不带 stream_options 的流式请求（用例 C）")
        print("现在也返回了真实 usage —— 服务端注入已生效。")
        return 0

    # mode=before
    c_d_missing = [
        r for r in results
        if r["case"] in ("C", "D") and r["verdict"] in MISSING_KINDS
    ]
    print("判定：未发现真 bug。应当无条件返回 usage 的用例（A 非流式、")
    print("B 流式带 include_usage）都返回了真实计数。")
    if c_d_missing:
        print("\n「每次请求都没有 usage」这个报告，最可能的成因就是用例 C：")
        print("  流式请求没带 stream_options.include_usage。它确实没有 usage，")
        print("  但这是 OpenAI 兼容协议的规定行为，不是故障（用例 B 已经证明")
        print("  只要带上就有）。")
        print("\n要让客户端什么都不用改就有 usage —— 在 Proxy 的 config.yaml 里：")
        print("    general_settings:")
        print("      always_include_stream_usage: true")
        print("  改完重启 proxy，然后用 `--mode after` 重跑本脚本验收：")
        print("  那时候用例 C 必须变成 OK。")
    else:
        print("\n而且用例 C（不带 include_usage 的流式）也有 usage —— 说明这套")
        print("Proxy 已经在服务端注入了（always_include_stream_usage 已打开，")
        print("或上游无条件回 usage）。没有需要改的东西。")
    return 0


# ---------------------------------------------------------------- 量具自检

def selftest() -> int:
    """第 0 步阳性对照：证明这把尺子「能」抓到缺失。

    全绿的探测结果本身不能自证量具有效 —— 一个永远返回 OK 的 classify_usage
    也会给出同样漂亮的满分。所以先喂它已知的坏形状，看它是否报警。
    """
    ok = True
    print("=== 阳性对照：以下形状必须被判为缺失 ===")
    for label, shape in [
        ("完全没有 usage 字段", None),
        ("空壳 0/0（拿不到上游 usage 时的形状）",
         {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}),
        ("usage 不是对象", "nope"),
        ("usage 在但三个计数全缺", {"foo": 1}),
    ]:
        verdict, human = classify_usage(shape)
        caught = verdict in MISSING_KINDS
        ok &= caught
        print(f"  {label:40s} -> {verdict:10s} "
              f"{'✓ 抓到' if caught else '✗ 漏了'}  {human}")

    print("=== 阴性对照：真计数不许误报 ===")
    verdict, human = classify_usage({
        "prompt_tokens": 641, "completion_tokens": 12, "total_tokens": 653,
        "prompt_tokens_details": {"cached_tokens": 512},
    })
    ok &= verdict == "OK"
    print(f"  真实 usage{' ' * 30} -> {verdict:10s} "
          f"{'✓' if verdict == 'OK' else '✗ 误报'}  {human}")

    print("=== SSE 提取器：末帧有 usage 要抓到，没有要报 None ===")
    with_usage = ('data: {"choices":[{"delta":{"content":"O"}}]}\n\n'
                  'data: {"choices":[],"usage":{"prompt_tokens":5,'
                  '"completion_tokens":2}}\n\ndata: [DONE]\n')
    without = 'data: {"choices":[{"delta":{"content":"O"}}]}\n\ndata: [DONE]\n'
    u1, c1 = extract_stream_usage(with_usage)
    u2, c2 = extract_stream_usage(without)
    ok &= u1 is not None and u2 is None
    print(f"  带 usage 的流 -> {u1} chunks={c1}")
    print(f"  不带的流      -> {u2} chunks={c2}")

    print(f"\n量具自检: "
          f"{'PASS —— 这把尺子可信，可以拿去读真实结果' if ok else 'FAIL —— 尺子坏了，别信它的绿灯'}")
    return 0 if ok else 1


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(
        description="LiteLLM「响应里没有 usage」通用诊断探针（只读）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--base", help="LiteLLM Proxy 地址，如 http://127.0.0.1:4000")
    ap.add_argument("--key", default=None,
                    help="API key。不传则读环境变量 LITELLM_KEY / LITELLM_API_KEY / OPENAI_API_KEY")
    ap.add_argument("--model", help="模型名（必须是这套 proxy 上真实存在的对外名）")
    ap.add_argument("--mode", choices=["before", "after"], default="before",
                    help="before=诊断现状；after=改完配置后的验收（用例 C 必须变 OK）")
    ap.add_argument("--max-tokens", type=int, default=16)
    ap.add_argument("--repeat", type=int, default=1,
                    help='每个用例重复几次；对方说"每次"时用 >1 查是否间歇')
    ap.add_argument("--prompt", default="Say OK.")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--json", metavar="PATH", help="把结构化结果写到文件")
    ap.add_argument("--selftest", action="store_true",
                    help="只跑量具自检（阳性对照），不打任何真实请求")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if not args.base or not args.model:
        ap.error("--base 和 --model 是必填的（或用 --selftest 只验尺子）")

    key = args.key or os.environ.get("LITELLM_KEY") \
        or os.environ.get("LITELLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not key:
        print("没拿到 key：传 --key 或设 LITELLM_KEY / LITELLM_API_KEY / OPENAI_API_KEY",
              file=sys.stderr)
        return 2

    # 先验尺子再量东西。自检不过就别往下跑，免得拿一把坏尺子读出漂亮结果。
    if selftest() != 0:
        return 1

    results = run_cases(args.base, key, args.model, args.max_tokens,
                        args.prompt, args.repeat, args.timeout, args.mode)
    rc = summarize(results, args.mode)

    if args.json:
        with open(args.json, "w") as f:
            json.dump({
                "base": args.base, "model": args.model, "mode": args.mode,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "results": results, "exit_code": rc,
            }, f, ensure_ascii=False, indent=2)
        print(f"\n结构化结果已写入 {args.json}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
