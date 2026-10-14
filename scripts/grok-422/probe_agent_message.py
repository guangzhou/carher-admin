#!/usr/bin/env python3
"""A/B 探针：x.ai 是否因 Responses input 里的 `agent_message` item 回 422。

在 litellm-proxy pod 内跑（用 pod 自带的 LITELLM_MASTER_KEY，打 127.0.0.1:4000），
走与用户相同的 `sa-grok-4.7` 路径，但关掉重试和兜底，让 422 原样冒出来：
  - num_retries=0、fallbacks=[]（只这一个请求）
  - 不带 tags：本集群开了 tag 过滤，带未知 tag 会被路由器直接拒（500「Not allowed to access model due to tags」），10-14 第一轮就这么全假红

每个 case 打 N 次（默认 3），只打印状态码和错误体前 300 字节，不打印模型输出。

cases：
  A  plain        : 一条 user message（阳性对照：必须 200，否则尺子坏）
  B  agent_text   : user message + agent_message(content=[input_text])
  C  agent_enc    : user message + agent_message(content=[input_text, encrypted_content])
                    —— 与 10-14 抓到的被拒请求同形（Codex Desktop 0.162 子代理消息）
  D  agent_strip  : C 经过 strip_agent_message() 之后（修复方案的回归：必须 200）
  E  cmp_trigger  : user message + 末尾 {"type":"compaction_trigger"}（Codex 压缩轮的唯一标记）
  F  cmp_rewrite  : E 把 compaction_trigger 换成一条显式「请总结」user message
"""
from __future__ import annotations

import copy
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("PROBE_BASE", "http://127.0.0.1:4000")
KEY = os.environ["LITELLM_MASTER_KEY"]
MODEL = os.environ.get("PROBE_MODEL", "sa-grok-4.7")
N = int(os.environ.get("PROBE_N", "3"))

# 形状照抄线上被拒请求（id 前缀、author、content 两段）；encrypted_content 用一个
# 格式正确但不属于任何会话的 Fernet 串，等价于「别家解码上下文产生的密文」。
FAKE_ENC = "gAAAAAB" + "A" * 200


def user(text: str) -> dict:
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def agent_message(with_enc: bool) -> dict:
    content = [{"type": "input_text", "text": "subagent finished: nothing to report."}]
    if with_enc:
        content.append({"type": "encrypted_content", "encrypted_content": FAKE_ENC})
    return {
        "id": "amsg_00000000-0000-0000-0000-000000000000",
        "type": "agent_message",
        "author": "/root",
        "recipient": "/root",
        "content": content,
    }


def strip_agent_message(body: dict) -> dict:
    """修复方案的纯函数版本（与 callback 里的逻辑一致，供回归 case D 用）。"""
    out = copy.deepcopy(body)
    items = []
    for it in out.get("input", []):
        if isinstance(it, dict) and it.get("type") == "agent_message":
            texts = [
                c.get("text", "")
                for c in it.get("content", [])
                if isinstance(c, dict) and c.get("type") in ("input_text", "output_text") and c.get("text")
            ]
            if texts:
                items.append({
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": f"[agent message from {it.get('author', 'agent')}]\n" + "\n".join(texts)}],
                })
            continue
        items.append(it)
    out["input"] = items
    return out


def body_for(case: str) -> dict:
    base = {
        "model": MODEL,
        "stream": False,
        "max_output_tokens": 16,
        "num_retries": 0,
        "fallbacks": [],
        "input": [user("Reply with the single word: ok")],
    }
    if case == "A":
        return base
    if case == "B":
        base["input"].insert(0, agent_message(with_enc=False))
        return base
    if case in ("E", "F"):
        if case == "E":
            base["input"].append({"type": "compaction_trigger"})
        else:
            base["input"].append(user("Summarize the conversation so far."))
        return base
    if case in ("C", "D"):
        base["input"].insert(0, agent_message(with_enc=True))
        return strip_agent_message(base) if case == "D" else base
    raise SystemExit(f"unknown case {case}")


def call(body: dict) -> tuple[int, str]:
    req = urllib.request.Request(
        BASE + "/v1/responses",
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, ""
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]
    except Exception as e:  # 网络/超时：单独标出来，别和 4xx 混
        return -1, f"{type(e).__name__}: {e}"[:300]


def main() -> int:
    cases = sys.argv[1:] or ["A", "B", "C", "D"]
    summary = {}
    for case in cases:
        codes = []
        for i in range(N):
            code, err = call(body_for(case))
            codes.append(code)
            print(f"case={case} try={i + 1} status={code}" + (f" err={err!r}" if err else ""), flush=True)
        summary[case] = codes
    print("SUMMARY " + json.dumps(summary))
    # 阳性对照不过 = 尺子坏，其他结论作废
    if "A" in summary and any(c != 200 for c in summary["A"]):
        print("RULER BROKEN: plain request did not return 200 every time")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
