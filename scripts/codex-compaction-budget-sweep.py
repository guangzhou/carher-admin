#!/usr/bin/env python3
"""codex-compaction-budget-sweep.py —— 同一份真实 payload，**只变 max_output_tokens**。

这把尺子是干嘛的
----------------
Codex 报 ``Error running remote compact task: ... Incomplete response
returned, reason: max_output_tokens`` 时，有两个说得通的机制：

  A. 输入太长撑爆了上下文
  B. **输出预算**太小，被落点模型的 reasoning 吃光，一个字摘要都没出来

这两个在客户端报错文案上长得一模一样。分辨它们唯一干净的办法：拿**同一份**
真实会话 payload，**只**改 ``max_output_tokens`` 这一个变量扫一遍。
输入一个字节都没变而结果从 incomplete 翻成 completed ⇒ 是 B，跟长度无关。

2026-09-23 用它定的案（input 98,238 tok，`gpt-5.5` → per-key alias
`sa-grok-4.7` → 兜底 `openrouter-deepseek-v4.1-flash`）::

    不发这个字段  → incomplete  output=4096 reasoning=4096  摘要 0 字
    4096          → 同上，一模一样
    16384         → completed   output=5400 reasoning=2024  摘要 8826 字

🔴 **``absent``（完全不发这个字段）是必扫的一格，而且是最重要的一格。**
Codex 的 ``ResponsesApiRequest``（`codex-rs/codex-api/src/common.rs`）里
**没有 max_output_tokens 字段**，它真实发出的形状就是 absent。只扫
4096/16384 会得到「4096 太小」这个正确但不完整的结论，漏掉「那 4096 是谁
塞进去的」——当时是我们自己的 callback 把 ``cur is None`` 也当成「低于下限」，
给一个本来无上限的请求凭空装了个 4096 的上限。

🔴 **判据先看 ``usage``，不是看 HTTP 码。** 推理模型预算不够时会返 200 +
空 content，形状跟成功一样（memory:
feedback_reasoning_model_probe_needs_headroom_for_reasoning_tokens）。
本脚本每一格都打 ``output_tokens`` / ``reasoning_tokens`` / 摘要字符数 /
compaction item 数四个数，就是为了不让那种假绿蒙混过去。

怎么搞到 payload
----------------
要一份**真实的长会话**，合成的小对话复现不出来（reasoning 吃不满小预算）。
从出事那个 Codex 会话的 rollout 文件里取 ``input`` 数组即可::

    ls -t ~/.codex/sessions/**/rollout-*.jsonl | head -1

存成 ``{"model": "...", "input": [...]}`` 的 JSON。脚本会自己在 input 末尾
追加 ``{"type": "compaction_trigger"}`` —— **这一项是压缩轮与普通轮的唯一
区别**，不加就只是在重放一次普通对话，跟这个 bug 毫无关系。

用法
----
    # 在 198 上跑（BASE 默认打生产车道 NodePort）
    KEY=<用户自己的那把 key> ./codex-compaction-budget-sweep.py \
        --payload /tmp/replay.json --model gpt-5.5

    # 只验一格
    KEY=... ./codex-compaction-budget-sweep.py --payload /tmp/replay.json --budgets absent

    # 对照组：不加 trigger 的普通轮，用来证明这个上限只在压缩轮出现
    KEY=... ./codex-compaction-budget-sweep.py --payload /tmp/replay.json --no-trigger

⚠️ ``KEY`` 必须是**用户那把 key**，不是 master key —— per-key alias 只挂在
用户 key 上，用 master key 打会走到另一个落点，得到的是线型假绿
（memory: feedback_dont_rewrite_a_ruler_to_bypass_a_param_check）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

# 生产车道 NodePort。判车道只认 Svc litellm-proxy-nodeport 的 selector，
# 别用 `-l app=litellm-proxy`（那是另一个不在服务的 pod）。
DEFAULT_BASE = "http://127.0.0.1:30402"
TRIGGER = {"type": "compaction_trigger"}


def run_one(base: str, key: str, body: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        base.rstrip("/") + "/v1/responses",
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json"})
    out = {"status": None, "incomplete": None, "text_chars": 0,
           "compaction_items": 0, "usage": [], "error": None}
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        out["error"] = "HTTP %s %s" % (e.code, e.read()[:300].decode("utf-8", "replace"))
        return out
    except Exception as e:  # noqa: BLE001 - 如实记下来，别吞
        out["error"] = repr(e)
        return out
    with resp as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if not chunk or chunk == "[DONE]":
                continue
            try:
                d = json.loads(chunk)
            except ValueError:
                continue
            if d.get("type") == "response.output_text.delta":
                out["text_chars"] += len(d.get("delta") or "")
            item = d.get("item") or {}
            if isinstance(item, dict) and item.get("type") in (
                    "compaction", "compaction_summary", "context_compaction"):
                out["compaction_items"] += 1
            r_ = d.get("response") or {}
            u = r_.get("usage")
            if u:
                out["usage"].append((
                    d.get("type"), u.get("input_tokens"), u.get("output_tokens"),
                    (u.get("output_tokens_details") or {}).get("reasoning_tokens")))
            if r_.get("status"):
                out["status"] = r_["status"]
                out["incomplete"] = r_.get("incomplete_details")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="同一份 payload 只变 max_output_tokens，扫出 incomplete 的真因")
    ap.add_argument("--payload", required=True, help='JSON，含 {"input": [...]}')
    ap.add_argument("--model", default="gpt-5.5")
    ap.add_argument("--base", default=os.environ.get("BASE", DEFAULT_BASE))
    ap.add_argument("--budgets", default="absent,4096,16384",
                    help='逗号分隔；`absent` = 完全不发这个字段（Codex 真实形状）')
    ap.add_argument("--no-trigger", action="store_true",
                    help="对照组：不加 compaction_trigger，证明上限只在压缩轮出现")
    ap.add_argument("--timeout", type=int, default=900)
    a = ap.parse_args()

    key = os.environ.get("KEY", "")
    if not key:
        print("⛔ 要 KEY 环境变量，且必须是**用户那把 key**，master key 会走到别的落点", file=sys.stderr)
        return 2

    src = json.load(open(a.payload, encoding="utf-8"))
    items = list(src["input"])
    if not a.no_trigger:
        items.append(dict(TRIGGER))

    rows = []
    for b in [x.strip() for x in a.budgets.split(",") if x.strip()]:
        body = {"model": a.model, "input": items, "stream": True, "store": False}
        if b != "absent":
            body["max_output_tokens"] = int(b)
        print("\n--- max_output_tokens=%s  items=%d  trigger=%s"
              % (b, len(items), not a.no_trigger), flush=True)
        r = run_one(a.base, key, body, a.timeout)
        if r["error"]:
            print("  错误：%s" % r["error"])
        for et, i, o, rt in r["usage"]:
            print("  %-34s input=%s output=%s reasoning=%s" % (et, i, o, rt))
        print("  status=%s incomplete=%s 摘要=%d字 compaction_items=%d"
              % (r["status"], r["incomplete"], r["text_chars"], r["compaction_items"]))
        rows.append((b, r))

    print("\n== 汇总 ==")
    print("  %-10s %-12s %-8s %-10s %s" % ("budget", "status", "摘要字数", "reasoning", "compaction"))
    for b, r in rows:
        rt = r["usage"][-1][3] if r["usage"] else None
        print("  %-10s %-12s %-8d %-10s %s"
              % (b, r["status"] or (r["error"] or "?")[:12], r["text_chars"], rt,
                 r["compaction_items"]))
    ok = [b for b, r in rows if r["status"] == "completed" and r["compaction_items"] == 1]
    bad = [b for b, r in rows if r["status"] == "incomplete"]
    if ok and bad:
        print("\n⇒ 输入一个字节没变，只有预算变了就从 incomplete 翻成 completed："
              "\n  这是**输出预算**问题，跟上下文长度无关。incomplete 的格子：%s；过的：%s"
              % (",".join(bad), ",".join(ok)))
        if "absent" in bad:
            print("  🔴 `absent` 也红 ⇒ 那个上限不是客户端发的，是链路上某一层自己塞的。"
                  "\n     去 callback 里找把 `None` 当成「低于下限」的那一支。")
    elif not bad:
        print("\n⇒ 每一格都没 incomplete ⇒ **证伪**：不是输出预算。"
              "别再往这个方向查，换查输入侧/上游。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
