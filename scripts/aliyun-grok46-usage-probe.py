#!/usr/bin/env python3
"""阿里云 LiteLLM：grok-4.6(上游 sa-grok-4.6) 的 usage 返回验证探针（只读）。

起因
----
2026-09-19 同事报「sa-grok-4.6 每次请求返回没有 usage」。

先把两件会被混为一谈的事分开，否则量具一定会骗人：

  (1) 上游报给 litellm 的 usage  —— 落在 SpendLogs 的 prompt_tokens 等列。
      09-19 查过 198 侧 SpendLogs：8/8 成功行都有 usage，且
      prompt_tokens_details.cached_tokens / completion_tokens_details.reasoning_tokens
      都有值（这两个 litellm 本地算不出来，所以是真·上游报的）。
  (2) litellm 回给客户端的 usage —— 也就是 HTTP 响应体里那个 "usage" 字段。

同事说的是 (2)。(1) 真 + (2) 假是完全可能同时成立的，尤其流式：
OpenAI 兼容协议下，**流式不带 stream_options.include_usage 就本来不返回 usage**，
这是协议规定的行为，不是故障。所以脚本必须把下面三种分开测，
否则「没有 usage」这句话根本没有唯一含义：

  A 非流式                          -> 必须有 usage。没有 = 真 bug
  B 流式 + include_usage:true       -> 必须有末帧 usage。没有 = 真 bug
  C 流式 + 不带 include_usage       -> 没有 usage 是**协议正确行为**，不是 bug

链路（这次的关键）
------------------
阿里云这侧对外名是 `grok-4.6`，**没有**叫 `sa-grok-4.6` 的模型。
`sa-grok-4.6` 是它桥到 198 时的上游名：

  客户端 --> 阿里云 litellm (ns carher, 对外名 grok-4.6)
          --> custom_openai/sa-grok-4.6 @ https://cc.auto-link.com.cn/pro/v1  (=198)
          --> 198 那侧的真实上游

两跳，usage 可能在任何一跳丢。所以脚本默认**两跳都打**（--hop both）：
同一组用例分别打阿里云和 198，哪一跳开始丢一眼可见。
只打一跳是坏尺子 —— 阿里云丢和 198 丢的处置完全不同。

`custom_openai/` 这个 provider 前缀也值得留意：它的响应处理路径和原生
`openai/` 不同，是 usage 透传最可能出问题的地方。

判据
----
只认响应体里 usage 的**实际数值**，不认「字段存在」：
litellm 在拿不到上游 usage 时会回 prompt_tokens=0 的空壳，
那种形状对计费和对 OWUI 的 compaction 阈值一样是「没有 usage」。
所以 0 值单独列成 ZERO 一档，不和 OK 混。

用法
----
  scripts/aliyun-grok46-usage-probe.py                  # 两跳全测
  scripts/aliyun-grok46-usage-probe.py --hop aliyun     # 只测阿里云
  scripts/aliyun-grok46-usage-probe.py --hop 198        # 只测 198
  scripts/aliyun-grok46-usage-probe.py --json out.json  # 机器可读结果
  scripts/aliyun-grok46-usage-probe.py --model grok-4.6 --long

前置：本机 kubectl 通阿里云（jms 隧道）
  kubectl get ns >/dev/null 2>&1 \
    || nohup scripts/jms proxy laoyang 16443 172.16.1.163 6443 > /tmp/jms-proxy.log 2>&1 &

只读：不写 CM、不写 key、不 rollout、不改任何配置。唯一副作用是产生若干次
真实推理调用（会计费，故默认 max_tokens 很小）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

NS = "carher"
HER = "her-1000"
ALIYUN_SVC = "svc/litellm-proxy"
# 198 那侧的对外入口，和 CM 里 api_base 一致（去掉 /v1 由调用方拼）
HOP198_BASE = "https://cc.auto-link.com.cn/pro"
# 阿里云对外名 / 198 上游名
ALIYUN_MODEL = "grok-4.6"
UPSTREAM_MODEL = "sa-grok-4.6"


def run(cmd: list[str], check: bool = True, timeout: int = 60) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])}... failed: {p.stderr.strip()[:300]}")
    return p.stdout


def get_key() -> str:
    """从 HerInstance CRD 取 carher-1000 的 key。绝不回显值。"""
    key = run(
        ["kubectl", "--request-timeout=25s", "-n", NS, "get", "her", HER,
         "-o", "jsonpath={.spec.litellmKey}"]
    ).strip()
    if not key:
        raise RuntimeError(f"{HER} 未取到 litellmKey（隧道通了吗？）")
    return key


class PortForward:
    """到阿里云 litellm-proxy 的临时端口转发。本机默认没有 4000 监听。"""

    def __init__(self, port: int):
        self.port = port
        self.proc: subprocess.Popen | None = None

    def __enter__(self) -> str:
        self.proc = subprocess.Popen(
            ["kubectl", "-n", NS, "port-forward", ALIYUN_SVC, f"{self.port}:4000"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        base = f"http://127.0.0.1:{self.port}"
        for _ in range(40):
            try:
                urllib.request.urlopen(f"{base}/health/liveliness", timeout=2).read()
                return base
            except Exception:
                if self.proc.poll() is not None:
                    raise RuntimeError("port-forward 进程退出了")
                time.sleep(0.5)
        raise RuntimeError("port-forward 起来但 /health/liveliness 一直不通")

    def __exit__(self, *exc) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def post(base: str, path: str, key: str, payload: dict, timeout: int = 180):
    """返回 (http_code, raw_body_text, elapsed_s)。错误也要拿到 body。"""
    req = urllib.request.Request(
        f"{base}{path}",
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


def classify_usage(usage) -> tuple[str, str]:
    """把 usage 判成 OK / ZERO / MISSING / MALFORMED，并给一句人话。

    ZERO 单独成档是这个脚本的核心判据：litellm 在拿不到上游 usage 时会回一个
    prompt_tokens=0 的空壳。那种形状「字段存在」但对计费和对 OWUI 的
    compaction 阈值来说和没有一样 —— 把它算成 OK 就是自欺。
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
    （带 include_usage 时）。所以要扫所有 data: 行，取最后一个带 usage 的。
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


# 用例表。expect 是**协议应然**，不是「我希望」：
#   C 那条 expect=MISSING —— 流式不带 include_usage 本就不返回 usage。
#   如果 C 反而有 usage，那是 litellm 比协议更宽松，不算故障，但要如实标出。
CASES = [
    ("A", "非流式",                      False, None,  "OK"),
    ("B", "流式 + include_usage:true",    True,  True,  "OK"),
    ("C", "流式 + 不带 include_usage",    True,  None,  "MISSING"),
    ("D", "流式 + include_usage:false",   True,  False, "MISSING"),
]


def build_payload(model: str, stream: bool, include_usage, max_tokens: int, prompt: str) -> dict:
    p: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "stream": stream,
    }
    if stream and include_usage is not None:
        p["stream_options"] = {"include_usage": include_usage}
    return p


def probe_hop(hop: str, base: str, key: str, model: str, max_tokens: int,
              prompt: str, repeat: int) -> list[dict]:
    """对一跳跑完整组用例。返回结构化结果。"""
    results = []
    print(f"\n{'=' * 78}")
    print(f"HOP: {hop}   base={base}   model={model}")
    print(f"{'=' * 78}")
    for cid, label, stream, inc_usage, expect in CASES:
        for rep in range(1, repeat + 1):
            tag = f"{cid}{'' if repeat == 1 else f'.{rep}'}"
            payload = build_payload(model, stream, inc_usage, max_tokens, prompt)
            code, body, secs = post(base, "/v1/chat/completions", key, payload)
            rec = {
                "hop": hop, "case": cid, "rep": rep, "label": label,
                "stream": stream, "include_usage": inc_usage,
                "http": code, "elapsed_s": round(secs, 2), "expect": expect,
            }
            if code != 200:
                rec["verdict"] = "HTTP_ERR"
                rec["detail"] = body[:300].replace("\n", " ")
                print(f"[{tag}] {label:32s} HTTP {code}  {rec['detail'][:110]}")
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
                    print(f"[{tag}] {label:32s} {err}")
                    results.append(rec)
                    continue
            verdict, human = classify_usage(usage)
            rec["verdict"] = verdict
            rec["detail"] = human
            rec["usage"] = usage
            rec["matches_expect"] = (verdict == expect) or (
                # C/D 期望 MISSING，拿到 OK 是"比协议宽松"，如实标注但不算失败
                expect == "MISSING" and verdict == "OK"
            )
            flag = "" if rec["matches_expect"] else "  <== 与协议应然不符"
            extra = f" sse_chunks={rec['sse_chunks']}" if stream else ""
            print(f"[{tag}] {label:32s} HTTP 200  {verdict:8s} {human}{extra}{flag}")
            results.append(rec)
    return results


def verdict_summary(results: list[dict]) -> int:
    """打印结论。返回 exit code：0=没发现 usage 缺失，1=发现真问题。"""
    print(f"\n{'=' * 78}")
    print("结论")
    print(f"{'=' * 78}")

    hops = sorted({r["hop"] for r in results})
    real_bugs = []
    for hop in hops:
        rs = [r for r in results if r["hop"] == hop]
        print(f"\n[{hop}]")
        for cid, label, _s, _iu, expect in CASES:
            got = [r for r in rs if r["case"] == cid]
            if not got:
                continue
            vs = sorted({r["verdict"] for r in got})
            note = "协议应然: " + expect
            line = f"  {cid} {label:32s} -> {','.join(vs):12s} ({note})"
            # 只有 A/B 缺 usage 才是真 bug；C/D 缺是协议正确
            if expect == "OK" and any(
                r["verdict"] in ("MISSING", "ZERO", "MALFORMED") for r in got
            ):
                line += "   <<< 真问题"
                real_bugs.append((hop, cid, label, vs))
            print(line)

    print("\n" + "-" * 78)
    if real_bugs:
        print("判定：确认存在 usage 缺失。应当返回 usage 的用例没有返回：")
        for hop, cid, label, vs in real_bugs:
            print(f"  - {hop} / 用例 {cid}（{label}）-> {','.join(vs)}")
        print("\n注意 ZERO 也算缺失：prompt_tokens=0 的空壳对计费和 OWUI 的")
        print("compaction 阈值来说和没有 usage 完全一样。")
        return 1

    print("判定：未发现 usage 缺失。应当返回 usage 的用例（A 非流式、")
    print("B 流式带 include_usage）都返回了真实计数。")
    missing_nostream = [
        r for r in results
        if r["case"] in ("C", "D") and r["verdict"] == "MISSING"
    ]
    if missing_nostream:
        print("\n关于「每次请求返回没有 usage」这个报告，最可能的成因是：")
        print("  流式请求没带 stream_options.include_usage —— 见用例 C/D，")
        print("  它们确实没有 usage，但这是 OpenAI 兼容协议的规定行为，不是故障。")
        print("  客户端只要带上 stream_options={\"include_usage\": true} 就有了（用例 B 已证）。")
    return 0


def selftest() -> int:
    """第 0 步阳性对照：证明这把尺子「能」抓到缺失。

    全绿的探测结果本身不能自证量具有效 —— 一个永远返回 OK 的 classify_usage
    也会给出同样漂亮的 12/12。所以先喂它已知的坏形状，看它是否报警。
    """
    ok = True
    print("=== 阳性对照：以下形状必须被判为缺失 ===")
    bad_shapes = [
        ("完全没有 usage 字段", None),
        ("空壳 0/0（litellm 拿不到上游 usage 时的形状）",
         {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}),
        ("usage 不是对象", "nope"),
        ("usage 在但三个计数全缺", {"foo": 1}),
    ]
    for label, shape in bad_shapes:
        verdict, human = classify_usage(shape)
        caught = verdict in ("MISSING", "ZERO", "MALFORMED")
        ok &= caught
        print(f"  {label:44s} -> {verdict:10s} "
              f"{'✓ 抓到' if caught else '✗ 漏了'}  {human}")

    print("=== 阴性对照：真计数不许误报 ===")
    verdict, human = classify_usage({
        "prompt_tokens": 641, "completion_tokens": 1, "total_tokens": 719,
        "prompt_tokens_details": {"cached_tokens": 512},
    })
    ok &= verdict == "OK"
    print(f"  真实 usage{' ' * 34} -> {verdict:10s} "
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

    print(f"\n量具自检: {'PASS —— 这把尺子可信，可以拿去读真实结果' if ok else 'FAIL —— 尺子坏了，别信它的绿灯'}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="阿里云 grok-4.6 (上游 sa-grok-4.6) 的 usage 返回验证探针（只读）"
    )
    ap.add_argument("--hop", choices=["aliyun", "198", "both"], default="both",
                    help="测哪一跳。默认 both —— 只测一跳分不清是哪跳丢的")
    ap.add_argument("--model", default=None,
                    help=f"覆盖模型名（阿里云默认 {ALIYUN_MODEL}，198 默认 {UPSTREAM_MODEL}）")
    ap.add_argument("--max-tokens", type=int, default=16)
    ap.add_argument("--repeat", type=int, default=1,
                    help='每个用例重复几次。同事说"每次"，>1 可查是否间歇')
    ap.add_argument("--long", action="store_true",
                    help="用较长 prompt（探 cached_tokens 是否也透传）")
    ap.add_argument("--port", type=int, default=4111, help="本地 port-forward 端口")
    ap.add_argument("--json", metavar="PATH", help="把结构化结果写到文件")
    ap.add_argument("--selftest", action="store_true",
                    help="只跑量具自检（阳性对照），不打任何真实请求")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    prompt = "Reply with exactly OK."
    if args.long:
        prompt = ("Below is filler text; reply with exactly OK.\n"
                  + ("The quick brown fox jumps over the lazy dog. " * 400))

    key = get_key()
    print(f"key: {HER} 的 litellmKey 已取到（长度 {len(key)}，不回显）")

    results: list[dict] = []
    rc = 0
    try:
        if args.hop in ("aliyun", "both"):
            with PortForward(args.port) as base:
                results += probe_hop("aliyun (ns carher)", base, key,
                                     args.model or ALIYUN_MODEL,
                                     args.max_tokens, prompt, args.repeat)
        if args.hop in ("198", "both"):
            # 198 侧用阿里云 CM 里同一个 api_base；凭据是桥 key，不是 carher key，
            # 所以这一跳需要 PRO198_BRIDGE_API_KEY。拿不到就明说跳过，不假装测过。
            bridge = os.environ.get("PRO198_BRIDGE_API_KEY", "").strip()
            if not bridge:
                print(f"\n{'=' * 78}")
                print("HOP: 198 —— 跳过")
                print(f"{'=' * 78}")
                print("这一跳的凭据是桥 key（PRO198_BRIDGE_API_KEY），不是 carher key。")
                print("没有它就打不了，跳过而不是假装测过。要测就先导出该环境变量：")
                print("  export PRO198_BRIDGE_API_KEY=...   # 取自阿里云 litellm 的 Secret")
            else:
                results += probe_hop("198 (cc.auto-link.com.cn/pro)",
                                     f"{HOP198_BASE}", bridge,
                                     args.model or UPSTREAM_MODEL,
                                     args.max_tokens, prompt, args.repeat)
    finally:
        if results:
            rc = verdict_summary(results)
            if args.json:
                with open(args.json, "w", encoding="utf-8") as fh:
                    json.dump(results, fh, ensure_ascii=False, indent=1)
                print(f"\n结构化结果已写入 {args.json}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
