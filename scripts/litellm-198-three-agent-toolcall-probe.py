#!/usr/bin/env python3
"""每个模型名 × 三个 coding agent 的线型，判「能不能真干活」= 有没有发出工具调用。

为什么要这把尺子（2026-10-12~14 一再用到）
---------------------------------------------
「200 出字」证明不了 agent 能用：agent 读文件、改代码全靠工具调用。三个客户端走三个端点，
同一个组在某个端点上会坏，所以三条都要实打：

  claude  POST /v1/messages          anthropic tools, stream    (Claude Code)
  codex   POST /v1/responses         function tools, stream     (Codex CLI / App)
  cursor  POST /v1/chat/completions  function tools, stream     (Cursor BYOK)

每格判定：TOOL（发出 read_file 且参数里有路径）/ TOOL-NOARGS / TEXT:<前 90 字> / EMPTY / <HTTP 码>。
TEXT 一定要看原文：10-12 第一版提示词写成命令式「你必须调用、不许文字回答」，5 个模型
当成提示注入直接拒（"won't follow injected instructions"）—— 那是尺子坏了不是模型坏了。
现在的提示是编码 agent 的自然问法 + 系统提示。

纪律
----
* **禁 master key 直打**：master 不过 per-key 白名单闸门，必然假绿。这里克隆目标 key 的
  models + aliases 建一把临时 key（30 分钟过期，跑完删）。
* 每格唯一 nonce（响应缓存开着，逐字节相同的请求到不了上游）。
* 阴性对照：打一个形状里没有的名字必须 403，否则这轮读数作废（退出码 3）。
* 出图名（只走 /v1/images/generations）默认跳过。

用法（在 198 上跑）::

    MK=<master> python3 litellm-198-three-agent-toolcall-probe.py --clone cursor-liuguoxian-std
    MK=<master> python3 litellm-198-three-agent-toolcall-probe.py --clone <key> --only gpt-5.5,grok-4.7 --reps 3
    MK=<master> python3 litellm-198-three-agent-toolcall-probe.py --shape-file shape.json   # {"models":[..],"aliases":{..}}

已知既存现象（不是新故障）：kiro 的 claude-opus-4-7 对系统文件类问题回 "I can't discuss that."；
gpt-5.5 / gpt-5.6-sol 偶发空内容；grok-4.20-multi-agent 带 tools 必兜到 deepseek（fb=1）。
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = os.environ.get("LITELLM_BASE", "http://127.0.0.1:30402")
SYSTEM = ("You are a coding agent working in the user's repository. Use the provided tools "
          "to inspect files instead of guessing.")
PROMPT = "What is the hostname of this machine? Read the file /etc/hostname to find out. (ref %s)"
SCHEMA = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
IMAGE_NAMES = {"gpt-image-2", "image-2", "grok-imagine", "grok-imagine-image", "grok-imagine-image-2.0",
               "grok-imagine-image-quality", "muse-image", "gemini-image"}
SHAPES = ("claude", "codex", "cursor")
NEG_NAME = "zz-probe-name-that-does-not-exist"


def post(path, body, key, timeout=180):
    req = urllib.request.Request(BASE + path, json.dumps(body).encode(),
                                 {"Authorization": "Bearer " + key, "Content-Type": "application/json",
                                  "anthropic-version": "2023-06-01"})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, r.read().decode("utf8", "replace"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf8", "replace")[:300], dict(e.headers or {})
    except Exception as e:  # noqa: BLE001 - 传输层断了也要看得见
        return -1, "%s: %s" % (type(e).__name__, str(e)[:200]), {}


def sse_events(raw):
    for line in raw.splitlines():
        if line.startswith("data:"):
            chunk = line[5:].strip()
            if chunk and chunk != "[DONE]":
                try:
                    yield json.loads(chunk)
                except Exception:  # noqa: BLE001
                    pass


def verdict(tool, args, text):
    if tool:
        return "TOOL" if "hostname" in args else "TOOL-NOARGS"
    text = text.strip().replace("\n", " ")
    return ("TEXT:" + text[:90]) if text else "EMPTY"


def judge_claude(raw):
    tool, args, text = False, "", ""
    for e in sse_events(raw):
        t = e.get("type")
        if t == "content_block_start" and (e.get("content_block") or {}).get("type") == "tool_use":
            tool = True
        elif t == "content_block_delta":
            d = e.get("delta") or {}
            args += d.get("partial_json") or ""
            text += d.get("text") or ""
    return verdict(tool, args, text)


def judge_codex(raw):
    tool, args, text = False, "", ""
    for e in sse_events(raw):
        t = e.get("type") or ""
        if t == "response.output_item.added" and (e.get("item") or {}).get("type") == "function_call":
            tool = True
        elif t == "response.function_call_arguments.delta":
            args += e.get("delta") or ""
        elif t == "response.function_call_arguments.done" and not args:
            args = e.get("arguments") or ""
        elif t == "response.output_text.delta":
            text += e.get("delta") or ""
        elif t == "response.completed":
            for it in (e.get("response") or {}).get("output") or []:
                if it.get("type") == "function_call":
                    tool = True
                    args = args or it.get("arguments") or ""
    return verdict(tool, args, text)


def judge_cursor(raw):
    tool, args, text = False, "", ""
    for e in sse_events(raw):
        for ch in e.get("choices") or []:
            d = ch.get("delta") or {}
            text += d.get("content") or ""
            for tc in d.get("tool_calls") or []:
                tool = True
                args += (tc.get("function") or {}).get("arguments") or ""
    return verdict(tool, args, text)


def request_for(shape, name, prompt):
    if shape == "claude":
        return "/v1/messages", {
            "model": name, "max_tokens": 2048, "stream": True, "system": SYSTEM,
            "tools": [{"name": "read_file", "description": "read a file", "input_schema": SCHEMA}],
            "messages": [{"role": "user", "content": prompt}]}, judge_claude
    if shape == "codex":
        return "/v1/responses", {
            "model": name, "max_output_tokens": 2048, "stream": True, "instructions": SYSTEM,
            "tools": [{"type": "function", "name": "read_file", "description": "read a file",
                       "parameters": SCHEMA}],
            "input": [{"role": "user", "content": prompt}]}, judge_codex
    return "/v1/chat/completions", {
        "model": name, "max_tokens": 2048, "stream": True,
        "tools": [{"type": "function", "function": {"name": "read_file", "description": "read a file",
                                                    "parameters": SCHEMA}}],
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]}, judge_cursor


def run_cell(name, shape, key, nonce, rep):
    path, body, judge = request_for(shape, name, PROMPT % ("%s-%s-%s-%d" % (nonce, name, shape, rep)))
    st, raw, h = post(path, body, key)
    fb = h.get("x-litellm-attempted-fallbacks") or "0"
    return name, shape, (judge(raw) if st == 200 else str(st)), fb


def read_shape(args):
    if args.shape_file:
        d = json.load(open(args.shape_file))
        return list(d.get("models") or []), dict(d.get("aliases") or {})
    sql = ("select json_build_object('models',models,'aliases',aliases) from \"LiteLLM_VerificationToken\" "
           "where key_alias='%s';" % args.clone.replace("'", "''"))
    out = subprocess.run(["sudo", "kubectl", "-n", "litellm-product", "exec", "-i", "litellm-db-0", "--",
                          "psql", "-U", "litellm", "-d", "litellm", "-t", "-A"],
                         input=sql, capture_output=True, text=True).stdout.strip()
    if not out.startswith("{"):
        raise SystemExit("key %r not found" % args.clone)
    d = json.loads(out)
    return list(d.get("models") or []), dict(d.get("aliases") or {})


def summarize(rows):
    """rows: [(name, shape, verdict, fb)] -> (per_name {name: {shape: [verdicts]}}, non_tool list)."""
    per = collections.defaultdict(lambda: collections.defaultdict(list))
    bad = []
    for name, shape, v, fb in rows:
        per[name][shape].append(v + ("/fb" + fb if fb != "0" else ""))
        if not v.startswith("TOOL") or v == "TOOL-NOARGS":
            bad.append((name, shape, v, fb))
    return per, bad


def main():
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--clone", metavar="KEY_ALIAS", help="克隆这把 key 的 models+aliases")
    src.add_argument("--shape-file", metavar="PATH", help='{"models":[...],"aliases":{...}}')
    ap.add_argument("--only", help="逗号分隔，只测这些名")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()
    mk = os.environ.get("MK") or os.environ.get("LITELLM_MASTER_KEY")
    if not mk:
        raise SystemExit("set MK (master key) — used only to create/delete the throwaway key")

    models, aliases = read_shape(args)
    if not models:
        raise SystemExit("shape has models=[] (unrestricted) — probing it bypasses the allowlist gate")
    names = [m for m in models if m not in IMAGE_NAMES]
    if args.only:
        want = [x.strip() for x in args.only.split(",") if x.strip()]
        missing = [x for x in want if x not in models]
        if missing:
            raise SystemExit("--only names not in the shape: %s" % missing)
        names = [n for n in names if n in want]
    nonce = "%d%04d" % (time.time(), random.randint(0, 9999))
    st, gen, _ = post("/key/generate", {"key_alias": "zz-probe-3agent-" + nonce, "models": models,
                                        "aliases": aliases, "duration": "30m"}, mk)
    if st != 200:
        raise SystemExit("key/generate failed %s %s" % (st, gen[:200]))
    key = json.loads(gen)["key"]
    try:
        jobs = [(n, s, r) for n in names for s in SHAPES for r in range(args.reps)]
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            rows = list(ex.map(lambda j: run_cell(j[0], j[1], key, nonce, j[2]), jobs))
        neg = run_cell(NEG_NAME, "cursor", key, nonce, 0)
    finally:
        post("/key/delete", {"keys": [key]}, mk)

    per, bad = summarize(rows)
    w = max([len(n) for n in per] + [6])
    print("%-*s  %-18s %-18s %-18s" % (w, "name", *SHAPES))
    for n in names:
        cells = []
        for s in SHAPES:
            vs = per[n][s]
            ok = sum(1 for v in vs if v.startswith("TOOL") and not v.startswith("TOOL-NOARGS"))
            cells.append("%d/%d%s" % (ok, len(vs), " fb" if any("/fb" in v for v in vs) else ""))
        print("%-*s  %-18s %-18s %-18s" % (w, n, *cells))
    total = len(rows)
    print("\ncells=%d TOOL=%d non-TOOL=%d" % (total, total - len(bad), len(bad)))
    for name, shape, v, fb in bad:
        print("  %-*s %-7s %s%s" % (w, name, shape, v, "  fb=" + fb if fb != "0" else ""))
    print("NEG %s -> %s" % (NEG_NAME, neg[2]))
    if neg[2] == "TOOL" or neg[2].startswith("TEXT") or neg[2] == "200":
        print("RULER-BROKEN: a name outside the shape was served; readings are void")
        return 3
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
