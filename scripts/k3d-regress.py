#!/usr/bin/env python3
"""Regression for the cursor kimi-k3 / deepseek-v4-flash -> OpenRouter remap.

Replays the request shapes real users actually send (measured from
LiteLLM_SpendLogs.proxy_server_request over 14 days), not simplified probes:

  deepseek-v4-flash    4600/4996 calls = stream + tools, max_tokens 32000,
                       27-28 tools, 8-14 turns, some via /v1/messages
                       (thinking / system / context_management present)
  sa-kimi-k3-responses  960 stream+tools, 248 stream no-tools

Judged on the streamed body, not just the HTTP status: a 200 that yields no
content chunk is a failure here.
"""
import json
import os
import random
import sys
import urllib.request

BASE = os.environ.get("LITELLM_BASE", "http://127.0.0.1:30402")
KEY = os.environ["PROBE_KEY"]


def nonce(tag):
    return f"NONCE-{tag}-{random.randint(10**8, 10**9)}"


def post(path, body, stream):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, repr(e)


def judge_stream(raw):
    """Return (n_chunks, got_text, got_toolcall, saw_done, err)."""
    n = got_text = got_tool = 0
    done = False
    err = None
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            done = True
            continue
        try:
            d = json.loads(payload)
        except Exception:
            continue
        n += 1
        if isinstance(d, dict) and d.get("error"):
            err = str(d["error"])[:200]
        for ch in d.get("choices") or []:
            delta = ch.get("delta") or {}
            if delta.get("content"):
                got_text += 1
            if delta.get("tool_calls"):
                got_tool += 1
        # anthropic /v1/messages shape
        t = d.get("type") or ""
        if t == "content_block_delta":
            got_text += 1
        # openai /v1/responses shape: text arrives as response.output_text.delta,
        # NOT choices[].delta.content. Judging that path by the chat shape reads
        # a perfectly good stream as empty (this cost one false red).
        if t == "response.output_text.delta" and d.get("delta"):
            got_text += 1
        if t in ("response.function_call_arguments.delta", "response.output_item.added") and (
            (d.get("item") or {}).get("type") == "function_call"
        ):
            got_tool += 1
        if t == "response.completed":
            done = True
        if t == "response.failed" or t == "response.incomplete":
            err = json.dumps(d.get("response", {}).get("incomplete_details") or d)[:200]
    return n, got_text, got_tool, done, err


TOOLS_N = 27


def many_tools(n=TOOLS_N):
    return [
        {
            "type": "function",
            "function": {
                "name": f"tool_{i}",
                "description": f"test tool {i}",
                "parameters": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                },
            },
        }
        for i in range(n)
    ]


def multiturn(tag, turns=11):
    msgs = [{"role": "system", "content": "You are a coding assistant."}]
    for i in range(turns // 2):
        msgs.append({"role": "user", "content": f"step {i}: describe a list in one word"})
        msgs.append({"role": "assistant", "content": f"list{i}"})
    msgs.append({"role": "user", "content": f"Reply exactly: {tag}"})
    return msgs


def run(label, path, body, stream=True):
    tag = None
    code, raw = post(path, body, stream)
    if stream:
        n, text, tool, done, err = judge_stream(raw)
        ok = code == 200 and n > 0 and (text > 0 or tool > 0)
        detail = f"chunks={n} text={text} toolcalls={tool} done={done}"
        if err:
            detail += f" err={err}"
        if not ok and n == 0:
            detail += f" body={raw[:200]!r}"
    else:
        ok = code == 200
        detail = ""
        try:
            d = json.loads(raw)
            msg = (d.get("choices") or [{}])[0].get("message") or {}
            content = msg.get("content")
            tc = msg.get("tool_calls")
            ok = ok and bool(content or tc)
            detail = f"provider={d.get('provider')} len={len(content or '')} toolcalls={len(tc or [])}"
        except Exception:
            detail = f"body={raw[:200]!r}"
            ok = False
    print(f"[{'PASS' if ok else 'FAIL'}] {label}: HTTP={code} {detail}", flush=True)
    return ok


def main():
    results = []
    for model in ("kimi-k3", "deepseek-v4-flash"):
        t = nonce("ST")
        # 1. dominant real shape: stream + many tools + max_tokens 32000 + multiturn
        results.append(
            run(
                f"{model} chat stream+{TOOLS_N}tools mt=32000 multiturn",
                "/v1/chat/completions",
                {
                    "model": model,
                    "messages": multiturn(t),
                    "tools": many_tools(),
                    "tool_choice": "auto",
                    "stream": True,
                    "stream_options": {"include_usage": True},
                    "max_tokens": 32000,
                },
            )
        )
        # 2. stream, no tools (248 kimi calls look like this)
        t = nonce("SN")
        results.append(
            run(
                f"{model} chat stream no-tools",
                "/v1/chat/completions",
                {
                    "model": model,
                    "messages": [{"role": "user", "content": f"Reply exactly: {t}"}],
                    "stream": True,
                    "stream_options": {"include_usage": True},
                },
            )
        )
        # 3. non-stream control
        t = nonce("NS")
        results.append(
            run(
                f"{model} chat non-stream",
                "/v1/chat/completions",
                {
                    "model": model,
                    "messages": [{"role": "user", "content": f"Reply exactly: {t}"}],
                    "max_tokens": 700,
                },
                stream=False,
            )
        )
        # 4. /v1/responses streaming (codex clients)
        t = nonce("RS")
        results.append(
            run(
                f"{model} responses stream",
                "/v1/responses",
                {"model": model, "input": f"Reply exactly: {t}", "stream": True,
                 "max_output_tokens": 2000},
            )
        )
        # 5. /v1/messages anthropic shape with thinking + tools (seen in real logs)
        t = nonce("AM")
        results.append(
            run(
                f"{model} messages stream+tools+thinking",
                "/v1/messages",
                {
                    "model": model,
                    "system": "You are a coding assistant.",
                    "messages": [{"role": "user", "content": f"Reply exactly: {t}"}],
                    "tools": [
                        {
                            "name": f"tool_{i}",
                            "description": f"test tool {i}",
                            "input_schema": {
                                "type": "object",
                                "properties": {"q": {"type": "string"}},
                            },
                        }
                        for i in range(TOOLS_N)
                    ],
                    "stream": True,
                    "max_tokens": 32000,
                },
            )
        )
    print(f"\nTOTAL pass={sum(results)}/{len(results)}")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
