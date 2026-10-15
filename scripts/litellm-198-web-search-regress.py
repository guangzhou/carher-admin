#!/usr/bin/env python3
"""198 联网搜索回归：按各客户端真实线型打一遍，判「真搜了、没兜底、答对了」。

在 198 host 上跑（要 sudo kubectl 取 master key）::

    scp scripts/litellm-198-web-search-regress.py cltx@10.68.13.198:~/wsr/
    ssh cltx@10.68.13.198 'python3 ~/wsr/litellm-198-web-search-regress.py; rm -rf ~/wsr'
    # 只跑部分: --only kiro-opus55-messages-mixed,grok420-responses-preview

为什么要这个脚本（2026-10-14/15 一轮踩出来的坏尺子）
----------------------------------------------------
1. **HTTP 200 / 回答里有链接 ≠ 搜了。** grok 走 Messages、x.ai 收到 ``web_search_preview``、
   DeepSeek 官方 Responses 都会 200 并以「搜索结果」名义编一段，还附 nobelprize.org 通用链接。
   判据必须是**协议级证据**，且三家各报在不同字段（见 ``search_evidence``）——
   只看 Anthropic 的 ``usage.server_tool_use`` 会把 xAI 的真搜判成没搜（10-15 我就这么误判过一次）。
2. **问题必须是训练截止之后的事实**，否则模型凭记忆也答得对（[[feedback_search_probe_needs_post_cutoff_fact]]）。
   默认 2026 诺贝尔化学奖（10-07 公布，Kagan/Soai）。过几个月模型训练数据追上来就要换题：``--question/--expect``。
3. **kiro 车道必须 ``x-litellm-attempted-fallbacks: 0``**。兜底是静默的：
   kiro 400 后落 grok / wangsu 照样 200 出字，不看这个头分不出是谁答的。
4. **阴性对照**：同题不给搜索工具，协议级证据必须为 0，否则尺子坏了。

退出码：全部 PASS 为 0。
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

NS = "litellm-product"
QUESTION = "2026年诺贝尔化学奖得主是谁？请联网搜索后回答，并给出来源链接。"
EXPECT = ["Kagan", "卡冈", "卡甘", "Soai"]

FN_ANTHROPIC = {"name": "exec_command", "description": "Run a shell command",
                "input_schema": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}}
FN_RESPONSES = {"type": "function", "name": "exec_command", "description": "Run a shell command",
                "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}}
WS_ANTHROPIC = {"type": "web_search_20250305", "name": "web_search"}
CURSOR_SEARCH_PREFIX = "Search the web for the following query and return a concise answer with sources:\n"


def cases(q):
    """(名字, 端点, 请求体, 期望: search=是否必须有搜索证据, kiro=是否必须 attempted-fallbacks=0)"""
    msg = lambda model, tools, stream=True: {"model": model, "max_tokens": 2000, "stream": stream,
                                             "tools": tools, "messages": [{"role": "user", "content": q}]}
    return [
        # Codex CLI / multica-agent-sdk：Messages，函数工具 + 服务端搜索工具混在一起（kiro 搜索回合）
        ("kiro-opus55-messages-mixed", "messages", msg("kiro-claude-opus-5-5", [FN_ANTHROPIC, WS_ANTHROPIC]), True, True),
        ("kiro-sonnet55-messages-mixed", "messages", msg("kiro-claude-sonnet-5-5", [FN_ANTHROPIC, WS_ANTHROPIC]), True, True),
        # Cursor WebSearch：单独发一条 Responses「替我搜」，工具类型是 web_search_preview
        ("kiro-opus55-responses-cursor-search", "responses",
         {"model": "kiro-claude-opus-5-5", "input": CURSOR_SEARCH_PREFIX + "2026 Nobel Prize in Chemistry winner",
          "tools": [{"type": "web_search_preview"}]}, True, True),
        # Codex Desktop：Responses，函数工具 + web_search
        ("kiro-opus55-responses-codex-mixed", "responses",
         {"model": "kiro-claude-opus-5-5", "stream": False,
          "input": [{"role": "user", "content": [{"type": "input_text", "text": q}]}],
          "tools": [FN_RESPONSES, {"type": "web_search"}]}, True, True),
        # Claude Code 的 WebSearch 子请求（kiro 老路径，原样返回结果列表）
        ("kiro-opus55-claude-code-subrequest", "messages",
         {"model": "kiro-claude-opus-5-5", "max_tokens": 800, "stream": True, "tools": [WS_ANTHROPIC],
          "messages": [{"role": "user", "content": "Perform a web search for the query: 2026 Nobel Prize in Chemistry"}]},
         True, True),
        # grok 直选：Cursor 发 web_search_preview，靠 callback grok_web_search_preview_rename 改成 web_search
        ("grok420-responses-preview", "responses",
         {"model": "sa-grok-4.20", "input": CURSOR_SEARCH_PREFIX + "2026 Nobel Prize in Chemistry winner",
          "tools": [{"type": "web_search_preview"}]}, True, False),
        # 阴性对照：同题不给搜索工具 ⇒ 协议级搜索证据必须为 0
        ("control-kiro-opus55-no-search-tool", "messages", msg("kiro-claude-opus-5-5", [FN_ANTHROPIC]), False, True),
    ]


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()


def endpoint():
    ip = sh(["sudo", "kubectl", "-n", NS, "get", "svc", "litellm-proxy-nodeport", "-o", "jsonpath={.spec.clusterIP}"])
    port = sh(["sudo", "kubectl", "-n", NS, "get", "svc", "litellm-proxy-nodeport", "-o", "jsonpath={.spec.ports[0].port}"])
    key = sh(["sudo", "kubectl", "-n", NS, "exec", "deploy/litellm-proxy", "--", "printenv", "LITELLM_MASTER_KEY"])
    return f"http://{ip}:{port}", key


def post(base, key, ep, body, timeout):
    req = urllib.request.Request(f"{base}/v1/{ep}", data=json.dumps(body).encode(), headers={
        "authorization": f"Bearer {key}", "x-api-key": key, "anthropic-version": "2023-06-01",
        "content-type": "application/json"})
    t = time.time()
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, dict(r.headers), r.read().decode("utf8", "replace"), time.time() - t
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf8", "replace"), time.time() - t


def collect(raw):
    """JSON 或 SSE → (所有对象列表, 拼起来的文本)。不管是 Messages 还是 Responses。"""
    objs = []
    if raw.lstrip().startswith("{"):
        objs.append(json.loads(raw))
    else:
        for line in raw.splitlines():
            if line.startswith("data: ") and line[6:].strip() not in ("", "[DONE]"):
                try:
                    objs.append(json.loads(line[6:]))
                except ValueError:
                    pass
    text = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("text", "delta") and isinstance(v, str):
                    text.append(v)
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(objs)
    return objs, "".join(text)


def search_evidence(objs):
    """协议级搜索证据，三家字段各不相同，取并集：
    - Anthropic: server_tool_use / web_search_tool_result 块，usage.server_tool_use.web_search_requests
    - OpenAI Responses: output 里 web_search_call 项
    - xAI: usage.num_server_side_tools_used / server_side_tool_usage_details.web_search_calls
    """
    ev = {}

    def bump(k, n=1):
        ev[k] = ev.get(k, 0) + n

    def walk(o):
        if isinstance(o, dict):
            t = o.get("type")
            if t in ("server_tool_use", "web_search_tool_result", "web_search_call"):
                bump(t)
            stu = o.get("server_tool_use")
            if isinstance(stu, dict) and (stu.get("web_search_requests") or 0) > 0:
                bump("usage.web_search_requests", stu["web_search_requests"])
            if (o.get("num_server_side_tools_used") or 0) > 0:
                bump("xai.num_server_side_tools_used", o["num_server_side_tools_used"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(objs)
    return ev


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--question", default=QUESTION)
    ap.add_argument("--expect", default=",".join(EXPECT), help="答案里出现任一即算答对（逗号分隔）")
    ap.add_argument("--only", default="", help="逗号分隔的用例名")
    ap.add_argument("--timeout", type=int, default=300)
    a = ap.parse_args()
    expect = [x for x in a.expect.split(",") if x]
    only = {x for x in a.only.split(",") if x}
    base, key = endpoint()
    fails = 0
    for name, ep, body, need_search, need_kiro in cases(a.question):
        if only and name not in only:
            continue
        code, headers, raw, dt = post(base, key, ep, body, a.timeout)
        h = {k.lower(): v for k, v in headers.items()}
        fb = h.get("x-litellm-attempted-fallbacks", "?")
        problems = []
        if code != 200:
            problems.append(f"http={code} {raw[:200]!r}")
            ev, correct = {}, False
        else:
            objs, text = collect(raw)
            ev = search_evidence(objs)
            correct = any(x in text for x in expect)
            if need_search and not ev:
                problems.append("没有协议级搜索证据（200 出字不算）")
            if need_search and not correct:
                problems.append(f"答案里没有 {expect}")
            if not need_search and ev:
                problems.append(f"阴性对照出现搜索证据 {ev} ⇒ 尺子坏了")
        if need_kiro and fb != "0":
            problems.append(f"attempted-fallbacks={fb}（不是 kiro 自己接住的）")
        status = "PASS" if not problems else "FAIL"
        fails += bool(problems)
        print(f"{status} {name:38s} http={code} {dt:5.1f}s fallbacks={fb} evidence={ev} correct={correct}")
        for p in problems:
            print(f"     - {p}")
    print(f"\n{'ALL PASS' if not fails else f'{fails} FAIL'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
