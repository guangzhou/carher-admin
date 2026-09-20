#!/usr/bin/env python3
"""LiteLLM 多条道的首 token 延迟 + 成功率压测。

原型是 2026-09-12 给 kiro.rs 18 条道做回归时写的 `~cltx/kiro-bench/bench.py`，
在这里参数化成通用件（任何一组 LiteLLM model_name 都能跑）。

四条判据纪律，全部焊死在代码里，别绕
--------------------------------------
1. **TTFT 自己掐表量，不信 LiteLLM 的 `completionStartTime`。**
   那个字段不是首 token 时刻，拿它拆 TTFT 必得「生成耗时≈0」的假象。
     t_first_byte  = 收到第一个 SSE data 帧的时刻（含空 delta / role 帧）
     t_first_token = 收到第一个**非空文本 delta** 的时刻  ← 用户真正看到字的时刻
2. **成功判据不是 HTTP 200，是「拼回来的文本非空」。**
   模型不可能一个字没回；空回一定是量具或链路有问题，不能记成功。
3. **轮转顺序 = N 轮 × M 模型，不是每个模型连打 N 发。**
   把上游的时段漂移摊平到每个模型头上，
   避免把「我恰好跑在上游忙的那一分钟」读成「这个模型慢」。
4. **开跑前先打阴性对照**（一个不存在的模型名，必须失败）。
   量具连红都报不出来时，它报的绿一样不可信。合成绿与合成红同样不可信。
   `--no-negative-control` 能关，但关了就别拿结果当回归判据。

跑在哪
------
LiteLLM 的 ClusterIP（如 198 的 `10.43.149.225:4000`）只在集群内可达，
所以本脚本要 scp 到 198 上跑：

    scp scripts/litellm-lane-bench.py cltx@10.68.13.198:~/
    ssh cltx@10.68.13.198 'MK=sk-... python3 ~/litellm-lane-bench.py \
        --models kiro-auto,kiro-glm-5 --rounds 10 --out ~/bench/raw.jsonl'

⚠️ 成本：这会真打上游。开跑前先用 `--rounds 1` 确认道通，再放大轮数。
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import statistics
import sys
import time

NEGATIVE_CONTROL_MODEL = "NOSUCH-model-xyz-negative-control"


def one(host, port, key, model, prompt, max_tokens, timeout):
    """打一发流式请求，返回一条记录。异常不抛出，记进 err 字段。"""
    body = json.dumps({
        "model": model, "max_tokens": max_tokens, "stream": True,
        "stream_options": {"include_usage": True},
        "messages": [{"role": "user", "content": prompt}],
    })
    r = {"model": model, "code": None, "ttfb": None, "ttft": None,
         "total": None, "text": "", "in": None, "out": None, "err": None,
         "ts": time.time()}
    t0 = time.monotonic()
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("POST", "/v1/chat/completions", body,
                     {"Authorization": "Bearer " + key,
                      "Content-Type": "application/json",
                      "Accept": "text/event-stream"})
        resp = conn.getresponse()
        r["code"] = resp.status
        if resp.status != 200:
            r["err"] = resp.read(400).decode("utf8", "replace")
            r["total"] = time.monotonic() - t0
            return r
        buf = b""
        while True:
            chunk = resp.read1(4096) if hasattr(resp, "read1") else resp.read(1)
            if not chunk:
                break
            now = time.monotonic()
            if r["ttfb"] is None:                      # 首字节：含空 delta / role 帧
                r["ttfb"] = now - t0
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                payload = line[5:].strip()
                if payload == b"[DONE]":
                    continue
                try:
                    d = json.loads(payload)
                except Exception:
                    continue
                u = d.get("usage") or {}
                if u.get("prompt_tokens"):
                    r["in"] = u["prompt_tokens"]
                if u.get("completion_tokens"):
                    r["out"] = u["completion_tokens"]
                for c in d.get("choices") or []:
                    piece = (c.get("delta") or {}).get("content") or ""
                    if piece:
                        if r["ttft"] is None:          # 首 token：第一个非空文本 delta
                            r["ttft"] = now - t0
                        r["text"] += piece
    except Exception as e:
        r["err"] = "%s: %s" % (type(e).__name__, e)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    r["total"] = time.monotonic() - t0
    return r


def is_ok(rec):
    """成功 = 200 且文本非空。只看 200 判不出对错。"""
    return rec["code"] == 200 and bool(rec["text"].strip())


def pct(sorted_vals, q):
    if not sorted_vals:
        return None
    i = min(int(round(q * (len(sorted_vals) - 1))), len(sorted_vals) - 1)
    return sorted_vals[i]


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--models", required=True,
                   help="逗号分隔的对外 model_name；或 @file 每行一个")
    p.add_argument("--host", default="10.43.149.225")
    p.add_argument("--port", type=int, default=4000)
    p.add_argument("--rounds", type=int, default=10)
    p.add_argument("--prompt", default="hi")
    p.add_argument("--max-tokens", type=int, default=64)
    p.add_argument("--timeout", type=int, default=180)
    p.add_argument("--out", help="逐发记录追加到这个 jsonl")
    p.add_argument("--key-env", default="MK", help="读哪个环境变量拿 key")
    p.add_argument("--no-negative-control", action="store_true",
                   help="跳过阴性对照。关了就别拿结果当回归判据")
    a = p.parse_args()

    key = os.environ.get(a.key_env)
    if not key:
        sys.exit(f"环境变量 {a.key_env} 没设。别把 key 写进命令行（会进 shell history）。")

    spec = a.models
    if spec.startswith("@"):
        models = [l.strip() for l in open(spec[1:], encoding="utf8")
                  if l.strip() and not l.startswith("#")]
    else:
        models = [m.strip() for m in spec.split(",") if m.strip()]

    # ---- 第 0 步：阴性对照。量具报不出红，它报的绿就不可信 ----
    if not a.no_negative_control:
        nc = one(a.host, a.port, key, NEGATIVE_CONTROL_MODEL,
                 a.prompt, a.max_tokens, a.timeout)
        if is_ok(nc):
            sys.exit(f"❌ 阴性对照失败：不存在的模型 {NEGATIVE_CONTROL_MODEL} "
                     f"竟然返回了内容（code={nc['code']}）。量具坏了，结果不可信，不跑。")
        print("✅ 阴性对照通过：不存在的模型 code=%s，量具能报红。\n" % nc["code"],
              flush=True)

    recs = []
    for rd in range(1, a.rounds + 1):
        for m in models:                               # 轮转，不是连发
            rec = one(a.host, a.port, key, m, a.prompt, a.max_tokens, a.timeout)
            rec["round"] = rd
            recs.append(rec)
            print("r%-2d %-26s %s code=%-4s ttft=%-7s total=%-7s in=%-6s out=%-4s %s" % (
                rd, m, "OK " if is_ok(rec) else "BAD", rec["code"],
                ("%.2f" % rec["ttft"]) if rec["ttft"] else "-",
                ("%.2f" % rec["total"]) if rec["total"] else "-",
                rec["in"], rec["out"], (rec["err"] or "")[:60]), flush=True)
            if a.out:
                with open(a.out, "a", encoding="utf8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print("---- round %d done ----" % rd, flush=True)

    fmt = lambda v: ("%.2f" % v) if v else "-"
    print("\n%-26s %-7s %-9s %-9s %-9s %-9s %-8s" % (
        "model", "ok/n", "ttft_p50", "ttft_p95", "ttft_min", "ttft_max", "in_tok"))
    for m in models:
        rs = [x for x in recs if x["model"] == m]
        good = [x for x in rs if is_ok(x)]
        tt = sorted(x["ttft"] for x in good if x["ttft"])
        ins = [x["in"] for x in good if x["in"]]
        print("%-26s %-7s %-9s %-9s %-9s %-9s %-8s" % (
            m, "%d/%d" % (len(good), len(rs)),
            fmt(statistics.median(tt)) if tt else "-", fmt(pct(tt, 0.95)),
            fmt(tt[0] if tt else None), fmt(tt[-1] if tt else None),
            int(statistics.median(ins)) if ins else "-"))

    allgood = [x for x in recs if is_ok(x)]
    alltt = sorted(x["ttft"] for x in allgood if x["ttft"])
    print("\n全局 %d/%d 成功（%.1f%%），TTFT p50 %s / p95 %s" % (
        len(allgood), len(recs), 100.0 * len(allgood) / max(len(recs), 1),
        fmt(statistics.median(alltt)) if alltt else "-", fmt(pct(alltt, 0.95))))
    print("⚠️ 短输出负载下 TTFT≈总时延，别把它当「流式首字很快」的证据。")
    print("⚠️ 10 发样本判不了长尾，别拿 max 当 SLO。")
    # 有失败就非零退出，方便拿它当部署闸门
    sys.exit(0 if len(allgood) == len(recs) else 1)


if __name__ == "__main__":
    main()
