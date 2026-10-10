#!/usr/bin/env python3
"""探针：198 上 **acct 池（chatgpt-acct-*，走 ChatGPT 订阅份额）** 提供的 GPT 系模型，
在 **Cursor 的真实调用形状** 下能不能用。

和 `litellm-198-cursor-newnames-probe.py` 的区别：那把只量「200 且出字」。
Cursor 作为 agent IDE，光出字不够 —— 它靠原生 function calling 干活，
而 acct 网关（`chatgpt-acct-proxy/gateway/app/convert.py:33`）在 chat→responses
转换时**显式丢弃 tools**。所以本探针对每个名字发两次：

- 变体 A `plain`：`stream:true`，**不带 tools** —— 量「纯对话能不能用」
- 变体 B `tools`：`stream:true` + 一个 `read_file` 函数，且 prompt 明确要求调它
  —— 量「Cursor agent 模式能不能用」。判据是响应里**有没有 tool_calls delta**，
  不是「有没有 200」。200 + 纯文本 = agent 模式实际不可用。

判据纪律（沿用同族）：
- ⛔ 禁 master key（不过 per-key 白名单闸门，必然假绿）。复刻真 cursor key 的
  `models`+`aliases` 建临时 key，跑完删。
- 阴性对照：不存在的名字同发一遍，读数必须不同。
- 每发唯一 nonce（响应缓存开着）。
- 落点只认 `x-litellm-model-id` 响应头 —— 组名会撒谎，且这把 key 的 aliases
  会把 `gpt-5.4`/`gpt-5.2`/`chatgpt-gpt-5.4` 改写去 `sa-grok-4.6`，
  那三个名字量的不是 acct。

用法（198 上跑）：
    sudo kubectl -n litellm-product exec litellm-db-0 -- psql -U litellm -d litellm \
      -t -A -c "select json_build_object('models',models,'aliases',aliases)::text
                from \"LiteLLM_VerificationToken\"
                where key_alias='cursor-efae1ca4-unro'" > /tmp/shape.json
    LITELLM_MASTER_KEY=... python3 litellm-198-acct-gpt-cursor-probe.py --clone-file /tmp/shape.json
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

# acct 池（mode=responses, api_base=chatgpt-acct-*:4000）对外的 GPT 名。
# 有意排除 gpt-5.2 / gpt-5.4 / chatgpt-gpt-5.4 —— 这把 key 的 aliases 把它们
# 改写去 sa-grok-4.6 了，量它们等于量 grok，不是量 acct。
NAMES = (
    "gpt-5.5",
    "gpt-5.6-terra",
    "gpt-5.6-sol",
    "gpt-5.6-luna",
    "gpt-5.3-codex",
    "gpt-5.4-mini",
    "chatgpt-gpt-5.5",
    "chatgpt-gpt-5.6-terra",
)

TOOLS = [{
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a file from the workspace",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "file path"}},
            "required": ["path"],
        },
    },
}]


def call(base, path, payload, key, timeout=300, stream=False):
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Authorization": "Bearer " + key}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        if stream:
            return r.status, r.read().decode("utf8", "replace"), dict(r.headers)
        return r.status, json.load(r), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf8", "replace")[:500], dict(e.headers or {})
    except Exception as e:  # noqa: BLE001 - 传输层断了也要看得见，禁 2>/dev/null
        return -1, "%s: %s" % (type(e).__name__, str(e)[:250]), {}


def parse_sse(body: str):
    """返回 (text, n_toolcall_deltas, finish_reasons)。
    裸流式没有 usage 是协议规定不是故障，所以这里不读 usage。"""
    text, ntool, finish = [], 0, []
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        chunk = line[6:].strip()
        if chunk == "[DONE]":
            continue
        try:
            d = json.loads(chunk)
        except Exception:  # noqa: BLE001
            continue
        for ch in d.get("choices") or []:
            delta = ch.get("delta") or {}
            if delta.get("content"):
                text.append(delta["content"])
            if delta.get("tool_calls"):
                ntool += len(delta["tool_calls"])
            if ch.get("finish_reason"):
                finish.append(ch["finish_reason"])
    return "".join(text), ntool, finish


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:30402")
    ap.add_argument("--clone-file", required=True)
    args = ap.parse_args()

    master = os.environ.get("LITELLM_MASTER_KEY")
    if not master:
        print("LITELLM_MASTER_KEY 未设置", file=sys.stderr)
        return 2

    row = json.load(open(args.clone_file))
    models = list(row.get("models") or [])
    aliases = dict(row.get("aliases") or {})
    if not models:
        print("形状文件 models 为空 = 全放行，等于绕开白名单闸门，必然假绿。", file=sys.stderr)
        return 2
    missing = [n for n in NAMES if n not in models]
    if missing:
        print("⚠️ 这把 key 白名单里没有：%s（探针会照发，预期 400/401）" % missing)

    nonce = "%d-%d" % (time.time(), random.randint(1000, 9999))
    print("clone=%s models=%d aliases=%d nonce=%s" % (args.clone_file, len(models), len(aliases), nonce))
    aliased = [n for n in NAMES if n in aliases]
    if aliased:
        print("⚠️ 以下名字被这把 key 的 alias 改写，读数不代表 acct：%s" % aliased)

    st, gen, _ = call(args.base, "/key/generate",
                      {"key_alias": "zz-probe-acctgpt-" + nonce, "models": models,
                       "aliases": aliases, "duration": "25m"}, master)
    if st != 200:
        print("key/generate 失败 %s %s" % (st, gen))
        return 1
    probe = gen["key"]

    rows = []
    try:
        for n in NAMES:
            for variant in ("plain", "tools"):
                body_req = {
                    "model": n, "stream": True, "max_tokens": 200,
                    "messages": [{"role": "user", "content":
                                  ("nonce %s: call the read_file tool on path README.md" % nonce)
                                  if variant == "tools" else
                                  ("nonce %s: reply with the single word OK" % nonce)}],
                }
                if variant == "tools":
                    body_req["tools"] = TOOLS
                t0 = time.time()
                st, body, h = call(args.base, "/v1/chat/completions", body_req, probe, stream=True)
                dt = time.time() - t0
                if st == 200:
                    txt, ntool, finish = parse_sse(body)
                else:
                    txt, ntool, finish = str(body)[:200], 0, []
                mid = h.get("x-litellm-model-id") or "-"
                rows.append((n, variant, st, mid, ntool, txt, dt))
                print("  %-22s %-5s %-4s %6.1fs mid=%-26s tool_deltas=%-3d finish=%-12s out=%r"
                      % (n, variant, st, dt, mid[:26], ntool, ",".join(finish)[:12], txt[:60]))

        st, body, _ = call(args.base, "/v1/chat/completions",
                           {"model": "acct-does-not-exist-" + nonce, "stream": True,
                            "messages": [{"role": "user", "content": "x"}], "max_tokens": 8},
                           probe, stream=True)
        print("  NEG  (bogus name)      %-4s %s" % (st, str(body)[:110]))
        if st == 200:
            print("RULER-BROKEN: 不存在的名字也 200，这轮读数作废")
            return 3
    finally:
        st, body, _ = call(args.base, "/key/delete", {"keys": [probe]}, master)
        print("  cleanup /key/delete -> %s %s" % (st, str(body)[:80]))

    plain_ok = [r[0] for r in rows if r[1] == "plain" and r[2] == 200 and r[5].strip()]
    tools_200 = [r[0] for r in rows if r[1] == "tools" and r[2] == 200]
    tools_fc = [r[0] for r in rows if r[1] == "tools" and r[2] == 200 and r[4] > 0]
    print()
    print("SUMMARY  names=%d" % len(NAMES))
    print("  纯对话可用      : %d/%d %s" % (len(plain_ok), len(NAMES), plain_ok))
    print("  带 tools 返 200 : %d/%d %s" % (len(tools_200), len(NAMES), tools_200))
    print("  真出 tool_calls : %d/%d %s" % (len(tools_fc), len(NAMES), tools_fc))
    print("  ⇒ Cursor agent 模式可用 = 第三行；只有第二行绿 = 200 但工具被丢，agent 跑不动")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
