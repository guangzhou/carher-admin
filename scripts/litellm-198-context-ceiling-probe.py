#!/usr/bin/env python3
"""实测 198 上每个模型的**上游真实输入天花板**，用来给 `max_input_tokens` 定值。

为什么必须实测，不能抄
----------------------
`max_input_tokens` 在 198 上**是真闸门**（memory:
`project_198_max_input_tokens_10m_is_gate_off_2026_09_21` —— 有人填 `10000000`
把闸门整个关掉）。填大 = 闸门失效、上游才拒、用户看到的是上游的脏报错；
填小 = 我们自己砍用户的上下文。两个方向都有用户面半径，所以这个数字**不许猜**。

而 2026-09-25 实测：库里同族上游并存四套互不相容的数
  `gpt-5.5`=272,000  `gpt-6-*`=872,000  `gpt-5.6-*`/`cr-g-5.6`=922,000
  `gpt-5.2`/`gpt-5.4`=1,050,000
其中 272k/872k 对得上官方 codex 目录；**922,000 是我们自己算出来的**
（memory: `feedback_922000_was_never_the_real_ceiling`）；1,050,000 出处不明。
而 `cr-g-*` / `sa-*` / `chatgpt-*` 的上游是我们自己的代理，根本没有公开文档。
所以唯一可信的尺子就是**打到拒为止**。

不需要上游凭据
--------------
待填的那批模型现在 `max_input_tokens` 是 `None`，等于**我们这一侧没有闸门**，
请求会原样落到上游。所以从生产代理用 master key 打就能量到上游的边界，
不用去掏 sub2api / kiro / bpi 各自的 key。

对已经有值的模型（922k / 1,050k / 500k），探针只要发**小于该值**的输入就能
穿过我们的闸门打到上游 —— 这正好用来证伪那几个可疑的数。

🔴 2026-09-25 实测推翻了上面这一段，留着当反面教材
-------------------------------------------------
**闸门会挡住自己的量具。** 有闸门的模型上，这把尺子只量得到我们自己那道闸门，
量不到上游。当天三个 MEASURED 全是这个形状：

    sa-grok-4.6        接受≤492,941  拒绝≥500,750   闸门 500,000
    chatgpt-gpt-6-sol  接受≤859,944  拒绝≥875,562   闸门 872,000
    gpt-5.6-sol        接受≤906,796  拒绝≥922,413   闸门 922,000

三条括号全都精准地夹住**我们填的那个数**，一次都没碰到上游。原因是算术上必然的：
低于闸门 ⇒ 请求通过，看不到上游的边界在哪；到达闸门 ⇒ 我们先拒，上游根本没收到。
⇒ 这把尺子**只能从下方证伪**（上游在我们的闸门之下就拒 ⇒ 我们填高了，
`openrouter-glm-5.3-flash` 在 1,048,576 的闸门下方 1,000,500 就 400 正是这一例），
**不能证明"我们填高了没"**。要量上游真实天花板，必须先把该行的
`max_input_tokens` 摘成 `None`（= 打开闸门）再量，那是一次真的生产变更，
不是只读操作。

所以「922,000 是不是真的」这个问题，本轮**没有答案**；
本轮只证明了 500k/872k/922k 三道闸门**确实在拦**，且都不低于上游能收的量。

第 0 步永远是阳性对照
---------------------
每个模型先发一个**极小**请求。它失败 ⇒ 这条腿本来就是死的
（403 / 腿被 park / 号没订阅 / 目录里没这个 slug），这时候**任何**大请求的失败
都不构成"天花板"，必须记 `SKIP` 而不是记一个边界值。
没有阳性对照的红和绿同样不可信
（memory: `feedback_synthetic_red_is_as_untrusted_as_synthetic_green`）。

判据只认「接受 / 因过长被拒」，不认内容
---------------------------------------
⛔ 不拿 `content` 是否非空当判据：推理模型会把预算吃光返回 `content=None`
（memory: `feedback_reasoning_model_probe_needs_headroom_for_reasoning_tokens`）。
本探针一律 `max_tokens=1`，只看 HTTP 状态 + 错误文案分类：
  * 2xx                        → 这个长度**被接受**
  * 4xx 且文案含长度类关键词    → 这个长度**超了**（这才是天花板信号）
  * 其它 4xx/5xx/超时          → `UNKNOWN`，**不参与二分**，原样打印
把"其它失败"混进二分 = 拿一次限流/抖动把天花板判在随机位置。

成本
----
超长请求绝大多数会被上游**在处理前**就 400 掉，基本不烧 token；只有落在边界
下方的那几次是真实计费/配额消耗。二分每个模型约 8~10 次请求，其中真正被接受
（=真消耗）的约 4~5 次。`--dry-run` 只打印计划不发请求。

用法
----
    ./litellm-198-context-ceiling-probe.py --list            # 打印将要探的模型
    ./litellm-198-context-ceiling-probe.py --models cr-g-5.6-instant,sa-grok-4.5
    ./litellm-198-context-ceiling-probe.py --all-missing     # 只探两个字段都空的
    ./litellm-198-context-ceiling-probe.py --all-missing --out /tmp/ceiling.json

**只读，一个配置都不改。** 定值是下一步的事，本脚本只负责产出证据。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("LITELLM_198_BASE", "http://127.0.0.1:30402")
MK = os.environ.get("LITELLM_MK", "")

# 上游报"输入太长"的文案家族。宁可漏判成 UNKNOWN（会被打印出来人工看），
# 也不要把无关的 4xx 误当成天花板 —— 后者会静默把边界钉在错的位置。
_TOO_LONG_MARKERS = (
    "context_length_exceeded", "context length", "contextlength",
    "maximum context", "max_tokens", "too long", "too many tokens",
    "input is too large", "prompt is too long", "exceeds the maximum",
    "reduce the length", "string too long", "invalid_request_error: input",
    "request too large", "payload too large", "tokens > ",
    # 🔴 我们自己那道闸门返的是**中文**用户面文案，第一版只列了英文关键词，
    # 于是每一次"闸门正常拦住了"都被归成 UNKNOWN、退出二分、判 UNKNOWN。
    # 阳性对照就是这么抓出来的：已知边界的模型本该量出 272,000，结果没有结论。
    # 归类器只认自己见过的形状，没见过的一律 UNKNOWN —— 所以归类器必须先在
    # **真实的失败响应**上跑一遍（memory: `feedback_page_text_is_not_a_log_anchor`）。
    "上下文超出", "输入上限", "超出该模型",
    # 2026-09-25 第二轮实测补：kiro 上游（Anthropic 形状）说
    # "Context window is full. Reduce conversation history, system prompt, or tools."
    # 一个字都不落在上面任何一条里 ⇒ `kiro-qwen3-coder-next` 每个中点都判 UNKNOWN、
    # 四次后判 PARTIAL。而且它被 `error_sanitize` 打码成 `API 异常 (req: …)`，
    # 从探针这一侧**看不见原文**，是去生产车道 pod 日志 grep `masked req=` 才捞到的。
    # ⇒ 上游家族每多一个，就得去日志里捞一次真实文案，不能靠想。
    "context window is full",
)

# 我们自己的闸门会把上限直接写进文案（"上限 272,000"），可以直接读出来，
# 不必靠二分去逼近。但**上游**的拒绝没有这个格式，所以二分不能省。
_GATE_LIMIT_RE = r"上限\s*([0-9,]+)"

# 二分区间。上界取 2,000,000：比库里最大的 1,050,000 还高一档，
# 这样"库里那个数是不是吹的"才落在可测区间内，而不是顶在探针自己的上界上
# （memory: `feedback_sampling_interval_is_coupled_to_every_window_reading_it`
#  —— 区间必须按 MAX 标定，不能按当前值标定）。
LO_FLOOR = 1_000
HI_CEIL = 2_000_000
TOLERANCE = 0.03          # 相对精度，收敛到 3% 即停（再细没有产品意义）
FILLER = "token "          # 6 字符、约 1 token；真实比值由 calibrate() 实测

# 🔴 2026-09-25 第一版探针在**自己的阳性对照**上就翻车了，两个病都值得钉在这里：
#
# 病1「标定反了，大请求根本没发出去」：拿一个 50-token 的小请求去标定
# 字符/token 比值，但实测它 `prompt_tokens=1664` —— 这条路上有约 1600 token
# 的**固定开销**（注入的 system/cache）。用 `chars/used` 一除得出 0.12，
# 于是请求「2,000,000 token」时实际只发了 41,631 token，上游当然不拒，
# 探针据此宣布"没有天花板"。**这是假绿，而且是最贵的那种：它长得像结论。**
# 修法 = 两点法取**边际**比值并把固定开销单独解出来（见 calibrate()），
# 而且天花板一律用**回报的 `prompt_tokens`** 报，不用"我以为我发了多少"。
#
# 病2「用 master key 打裸名」：`glm-5.3-flash` 在 198 上没有裸名的真实组，
# 纯靠 per-key alias 活着，master key 打过去必 400。那不是腿死，是探针走错路
# （memory: `reference_carher_hangzhou_key_is_the_hangzhou_gw_upstream_credential`）。
# ⇒ 本脚本一律探**真实组名**（alias 的目标），并对公开名显式报错而不是记 SKIP。
CALIB_SMALL = 2_000
CALIB_LARGE = 30_000

# 上游把真实原因打码成这个形状时，光看文案分不出死活，必须拿 req id 去
# 生产车道 pod 日志 grep `masked req=`
# （memory: `feedback_acct_pool_db_row_needs_api_key_else_no_connected_db`）。
_MASKED_RE = "api 异常 (req:"


def api(path: str, payload: dict | None, timeout: int = 180):
    url = BASE.rstrip("/") + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Authorization": "Bearer " + MK,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            body = json.loads(body)
        except Exception:
            pass
        return e.code, body
    except Exception as e:                     # 超时 / 连不上
        return 0, "%s: %s" % (type(e).__name__, str(e)[:300])


def classify(status, body) -> str:
    """ACCEPT / TOO_LONG / UNKNOWN —— UNKNOWN 永远不参与二分。"""
    if 200 <= status < 300:
        return "ACCEPT"
    text = json.dumps(body, ensure_ascii=False).lower() if not isinstance(body, str) else body.lower()
    if status in (400, 413, 422) and any(m in text for m in _TOO_LONG_MARKERS):
        return "TOO_LONG"
    return "UNKNOWN"


def send(model: str, reps: int):
    """发 `reps` 份填充词，回 (归类, HTTP 状态, 实测 prompt_tokens, 原始响应)。"""
    body = {"model": model,
            "messages": [{"role": "user", "content": FILLER * reps}],
            "max_tokens": 1, "stream": False}
    t0 = time.time()
    status, resp = api("/v1/chat/completions", body)
    used = None
    if isinstance(resp, dict):
        used = (resp.get("usage") or {}).get("prompt_tokens")
    return classify(status, resp), status, used, resp, time.time() - t0


def calibrate(model: str):
    """两点法解出 (每份填充词的 token 数, 这条路的固定开销)。

    🔴 单点标定必错：这条路上有约 1600 token 的固定注入，用一个小请求去除
    会把比值算歪一个数量级（2026-09-25 实测 0.12 vs 真值 6.0），
    后果是"大请求"其实很小、上游不拒、探针宣布没有天花板 —— 假绿。
    """
    pts = []
    for reps in (CALIB_SMALL, CALIB_LARGE):
        kind, status, used, resp, _ = send(model, reps)
        if kind != "ACCEPT" or not used:
            return None, None, (kind, status, resp)
        pts.append((reps, used))
    (r1, u1), (r2, u2) = pts
    if r2 == r1 or u2 <= u1:
        return None, None, ("DEGENERATE", 0, pts)
    per_rep = (u2 - u1) / float(r2 - r1)       # 边际：不含固定开销
    overhead = u1 - r1 * per_rep
    return per_rep, overhead, None


def reps_for(target_tokens: int, per_rep: float, overhead: float) -> int:
    return max(1, int((target_tokens - overhead) / per_rep))


def probe(model: str, verbose=True) -> dict:
    """返回一个模型的实测结论。阳性对照不过一律 SKIP，不产出天花板。"""
    out = {"model": model, "trace": []}

    # --- 第 0 步：阳性对照 + 标定。它不过，后面所有红都不算数 ---
    per_rep, overhead, err = calibrate(model)
    if per_rep is None:
        kind, status, resp = err[0], err[1], err[2]
        out["verdict"] = "SKIP"
        sample = json.dumps(resp, ensure_ascii=False)[:400] if not isinstance(resp, str) else str(resp)[:400]
        out["sample_error"] = sample
        hint = ""
        if _MASKED_RE in sample.lower():
            hint = "（真实原因被打码，拿 req id 去生产车道 pod 日志 grep `masked req=`）"
        out["reason"] = ("阳性对照/标定没过（HTTP %s / %s）—— 这条腿对本探针本来就不通，"
                         "它对大请求报错不构成天花板证据%s" % (status, kind, hint))
        if verbose:
            print("  %-30s SKIP  %s" % (model, out["reason"]))
            print("      %s" % sample[:200])
        return out
    out["tokens_per_rep"] = round(per_rep, 3)
    out["fixed_overhead_tokens"] = int(overhead)
    if verbose:
        print("  %-30s 标定 %.2f token/份，固定开销 %d token"
              % (model, per_rep, overhead))

    # 先探上界：上界就被接受 ⇒ 上游天花板在可测区间之上
    kind, status, used, resp, _ = send(model, reps_for(HI_CEIL, per_rep, overhead))
    out["trace"].append({"target": HI_CEIL, "kind": kind, "status": status, "used": used})
    if kind == "ACCEPT":
        out["verdict"] = "NO_CEILING_BELOW"
        out["observed_prompt_tokens_max"] = used
        out["reason"] = ("请求 %s（上游实收 %s）仍被接受 —— 天花板在可测区间之上"
                         % (f"{HI_CEIL:,}", f"{used:,}" if used else "?"))
        if verbose:
            print("  %-30s ≥ %s（实收 %s，没拒）"
                  % (model, f"{HI_CEIL:,}", f"{used:,}" if used else "?"))
        return out
    if kind == "UNKNOWN":
        out["verdict"] = "UNKNOWN"
        out["reason"] = "上界请求返回了无法归类的失败（HTTP %s），不二分" % status
        out["sample_error"] = json.dumps(resp, ensure_ascii=False)[:400] if not isinstance(resp, str) else str(resp)[:400]
        if verbose:
            print("  %-30s UNKNOWN  HTTP %s  %s" % (model, status, out["sample_error"][:120]))
        return out

    # 我们自己那道闸门把上限直接写在文案里 ⇒ 读出来当**交叉校验**，
    # 但仍然走完二分：两者不一致本身就是要报出来的信号。
    stated = re.search(_GATE_LIMIT_RE,
                       json.dumps(resp, ensure_ascii=False) if not isinstance(resp, str) else resp)
    if stated:
        out["gate_says"] = int(stated.group(1).replace(",", ""))
        if verbose:
            print("  %-30s 拒绝文案自称上限 %s（我们这一侧的闸门）"
                  % (model, f"{out['gate_says']:,}"))

    lo, hi = LO_FLOOR, HI_CEIL
    best_ok, unknowns = 0, 0
    while (hi - lo) / float(hi) > TOLERANCE and unknowns < 4:
        mid = (lo + hi) // 2
        kind, status, used, resp, dt = send(model, reps_for(mid, per_rep, overhead))
        out["trace"].append({"target": mid, "kind": kind, "status": status,
                             "used": used, "sec": round(dt, 1)})
        if verbose:
            print("    %-28s %9s → %-8s (HTTP %s, 实收=%s)"
                  % (model, f"{mid:,}", kind, status, used))
        if kind == "ACCEPT":
            lo = mid
            if used:
                best_ok = max(best_ok, used)
        elif kind == "TOO_LONG":
            hi = mid
        else:
            unknowns += 1
            # UNKNOWN 不动区间：它可能是限流/抖动，拿它收缩边界会把结论钉歪。
            # 但必须把样本留下来 —— 第一版只在 SKIP/UNKNOWN 分支存 sample_error，
            # 于是判 PARTIAL 时屏幕上只有 "UNKNOWN"，原因一个字都没有，
            # 等于让人对着一个没有证据的红做判断。
            out.setdefault("unknown_samples", []).append({
                "target": mid, "status": status,
                "body": (json.dumps(resp, ensure_ascii=False)
                         if not isinstance(resp, str) else str(resp))[:400]})
            time.sleep(3)

    out["verdict"] = "MEASURED" if unknowns < 4 else "PARTIAL"
    out["accepted_max"] = lo
    out["rejected_min"] = hi
    # 🔴 报天花板只认**上游回报的** prompt_tokens，不认"我以为我发了多少"
    out["observed_prompt_tokens_max"] = best_ok
    if verbose:
        print("  %-30s 接受≤%s（实收峰值 %s）  拒绝≥%s  (%s)"
              % (model, f"{lo:,}", f"{best_ok:,}", f"{hi:,}", out["verdict"]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="实测 198 各模型上游真实输入天花板（只读）")
    ap.add_argument("--models", help="逗号分隔的模型名")
    ap.add_argument("--all-missing", action="store_true",
                    help="探所有 max_input_tokens 为空的模型（列表来自 --modelinfo）")
    ap.add_argument("--modelinfo", default="/tmp/modelinfo.json",
                    help="`/v1/model/info` 的落盘 JSON")
    ap.add_argument("--list", action="store_true", help="只打印将要探的模型，不发请求")
    ap.add_argument("--out", help="结论写到这个 JSON")
    a = ap.parse_args()

    if a.models:
        models = [m.strip() for m in a.models.split(",") if m.strip()]
    elif a.all_missing:
        data = json.load(open(a.modelinfo))["data"]
        seen, models = set(), []
        for r in data:
            name = r.get("model_name")
            info = r.get("model_info") or {}
            if name in seen:
                continue
            if info.get("max_input_tokens") is None:
                seen.add(name)
                models.append(name)
        models.sort()
    else:
        ap.error("要么 --models，要么 --all-missing")

    if a.list:
        print("\n".join(models))
        print("共 %d 个" % len(models))
        return 0
    if not MK:
        raise SystemExit("缺 LITELLM_MK —— 不猜 master key")

    print("== 实测上游输入天花板（%d 个模型，区间 %s~%s，精度 %d%%）=="
          % (len(models), f"{LO_FLOOR:,}", f"{HI_CEIL:,}", TOLERANCE * 100))
    results = []
    for m in models:
        results.append(probe(m))
    if a.out:
        json.dump(results, open(a.out, "w"), ensure_ascii=False, indent=1)
        print("\n结论 → %s" % a.out)

    # 阴性结论也要写成结论（memory: feedback_two_failure_faces_must_be_counted_separately）
    buckets = {}
    for r in results:
        buckets.setdefault(r["verdict"], []).append(r["model"])
    print("\n== 分桶 ==")
    for v in ("MEASURED", "NO_CEILING_BELOW", "PARTIAL", "UNKNOWN", "SKIP"):
        if v in buckets:
            print("  %-18s %d  %s" % (v, len(buckets[v]), ", ".join(buckets[v])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
