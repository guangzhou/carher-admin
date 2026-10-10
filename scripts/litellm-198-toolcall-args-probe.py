#!/usr/bin/env python3
"""并行工具调用「参数为空」三把尺子（2026-10-09 her-13 malformed tool call 排查沉淀）。

    # 1) 车道 A/B：同一把 key 交替打两个入口，chat 流式 6 路并行 + 单调用/纯文本/非流式对照
    #    在能到 198 NodePort 的地方跑（如 her 容器内，key 用容器 env）
    KEY=$CARHER_PROD_KEY python3 litellm-198-toolcall-args-probe.py lanes \
        --a http://10.68.13.198:30402 --b http://10.68.13.198:30405 --model gpt-6-astra --rounds 6

    # 2) 上游形状：在 chatgpt-acct 叶子 pod 里打 /v1/responses，数每个调用有没有 delta
    kubectl -n litellm-product exec -i deploy/chatgpt-acct-84 -- python - upstream \
        --model chatgpt-gpt-6-astra --rounds 5 < litellm-198-toolcall-args-probe.py

    # 3) 全网影响面：打印按天统计「客户端回传的并行 tool_calls 参数为空」的 SQL（在 litellm-db-0 psql 跑）
    python3 litellm-198-toolcall-args-probe.py sql --day 2026-10-09

判据纪律：
- 上游不发 delta 是**随机**的（同模型 3~4/5），每侧至少 6 发；单发绿/红都不算数。
- `lanes` 两侧交替打，摊平上游时段漂移；`single`/`text`/`nostream` 是阴性对照，两侧必须相同。
- 叶子 `/v1/chat/completions` 生产不走（母 router 以 `POST /responses` 打叶子），别拿它判生产。
- 叶子先确认活着：死号回 401 会被读成「复现不出」（10-09 acct-82）。
- `sql` 回传的参数 = 客户端当时**实际收到**的；判「以前就空被吞」还是「以前是满的」只认它。
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

TOOLS_CHAT = [{"type": "function", "function": {
    "name": n, "description": "lookup",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}
    for n in ("get_weather", "get_time")]
TOOLS_RESP = [{"type": "function", "name": t["function"]["name"], "description": "lookup",
               "parameters": t["function"]["parameters"]} for t in TOOLS_CHAT]
PROMPT = {
    "par6": "Call get_weather AND get_time for Tokyo, Paris, and Berlin. Issue all six tool calls in parallel in this single turn.",
    "single": "Call get_weather for Tokyo. Only one call.",
    "text": "Reply with exactly the word: pong",
}


def _nonce():
    # LiteLLM 响应缓存是开的：body 必须每发唯一
    return " #" + os.urandom(4).hex()


def _post(url, key, body, timeout=240):
    req = urllib.request.Request(url, json.dumps(body).encode(),
                                 {"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def chat_once(base, key, model, case):
    stream = case != "nostream"
    body = {"model": model, "stream": stream, "tools": TOOLS_CHAT,
            "messages": [{"role": "user", "content": PROMPT.get(case, PROMPT["par6"]) + _nonce()}]}
    t0 = time.time()
    try:
        r = _post(base.rstrip("/") + "/v1/chat/completions", key, body)
    except urllib.error.HTTPError as e:
        return {"err": f"HTTP{e.code}"}
    except Exception as e:  # noqa: BLE001
        return {"err": type(e).__name__}
    calls, text, fin = {}, "", None
    if stream:
        for line in r:
            line = line.decode().strip()
            if not line.startswith("data:") or line.endswith("[DONE]"):
                continue
            for c in json.loads(line[5:]).get("choices", []):
                fin = c.get("finish_reason") or fin
                d = c.get("delta") or {}
                text += d.get("content") or ""
                for tc in d.get("tool_calls") or []:
                    calls[tc["index"]] = calls.get(tc["index"], "") + ((tc.get("function") or {}).get("arguments") or "")
    else:
        c = json.load(r)["choices"][0]
        fin, text = c["finish_reason"], c["message"].get("content") or ""
        for i, tc in enumerate(c["message"].get("tool_calls") or []):
            calls[i] = tc["function"]["arguments"]
    bad = 0
    for v in calls.values():
        if v:
            try:
                json.loads(v)
            except Exception:  # noqa: BLE001
                bad += 1
    return {"calls": len(calls), "empty": sum(1 for v in calls.values() if not v), "badjson": bad,
            "finish": fin, "text": bool(text.strip()), "secs": round(time.time() - t0)}


def fmt(r):
    if "err" in r:
        return r["err"]
    return f"{r['calls']}c/{r['empty']}empty/{r['badjson']}bad/{r['finish']}/{'txt' if r['text'] else '-'}/{r['secs']}s"


def cmd_lanes(a):
    key = os.environ.get("KEY") or sys.exit("KEY env required")
    cases = ["par6"] * a.rounds + ["single", "text", "nostream"]
    tot = {"a": [0, 0], "b": [0, 0]}  # [par6 发数, 有空参数的发数]
    for case in cases:
        ra, rb = chat_once(a.a, key, a.model, case), chat_once(a.b, key, a.model, case)
        if case == "par6":
            for side, r in (("a", ra), ("b", rb)):
                if "err" not in r:
                    tot[side][0] += 1
                    tot[side][1] += r["empty"] > 0
        print(f"{case:8s} A={fmt(ra):34s} B={fmt(rb)}", flush=True)
    print(f"par6 有空参数的发数：A {tot['a'][1]}/{tot['a'][0]}  B {tot['b'][1]}/{tot['b'][0]}")


def cmd_upstream(a):
    key = os.environ.get("LITELLM_MASTER_KEY") or sys.exit("run inside a chatgpt-acct pod (LITELLM_MASTER_KEY)")
    for case in ["par6"] * a.rounds + ["single"] * 2:
        body = {"model": a.model, "stream": True, "tools": TOOLS_RESP,
                "input": [{"role": "user", "content": PROMPT[case] + _nonce()}]}
        try:
            r = _post(a.base.rstrip("/") + "/v1/responses", key, body)
        except urllib.error.HTTPError as e:
            hint = {401: "号死了(invalidated)，换号", 429: "号限流中，换号或等"}.get(e.code, "")
            print(case, f"HTTP{e.code} {hint} —— 这一发不计入判据")
            continue
        with_delta, done = set(), 0
        for line in r:
            line = line.decode().strip()
            if not line.startswith("data:"):
                continue
            try:
                d = json.loads(line[5:])
            except Exception:  # noqa: BLE001
                continue
            t = d.get("type")
            if t == "response.function_call_arguments.delta" and d.get("delta"):
                with_delta.add(d.get("output_index"))
            elif t == "response.function_call_arguments.done":
                done += 1
        print(f"{case:6s} calls={done} no_delta={done - len(with_delta)}", flush=True)


SQL = """set statement_timeout = '150s';
-- 客户端下一轮回传的 assistant.tool_calls = 它当时实际收到的参数（按天、单日窗口，别扫大窗）
select '{day}' d, count(*) echoed,
       count(*) filter (where coalesce(t->'function'->>'arguments','') in ('','{{}}')) empty_or_brace,
       count(distinct s.api_key) keys
from "LiteLLM_SpendLogs" s, jsonb_array_elements(s.proxy_server_request->'messages') m,
     jsonb_array_elements(m->'tool_calls') t
where s."startTime" >= timestamp '{day}' - interval '8 hours' and s."startTime" < timestamp '{day}' + interval '16 hours'
  and s.call_type = 'responses' and jsonb_typeof(s.proxy_server_request->'messages') = 'array'
  and m->>'role' = 'assistant' and jsonb_typeof(m->'tool_calls') = 'array'
  and jsonb_array_length(m->'tool_calls') >= 2{extra};
"""


def cmd_sql(a):
    extra = f"\n  and s.api_key = '{a.api_key}'" if a.api_key else ""
    if a.api_key and not all(ch in "0123456789abcdef" for ch in a.api_key):
        sys.exit("--api-key must be the token sha256 hex")
    print(SQL.format(day=a.day, extra=extra))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)
    l = sp.add_parser("lanes"); l.add_argument("--a", required=True); l.add_argument("--b", required=True)
    l.add_argument("--model", default="gpt-6-astra"); l.add_argument("--rounds", type=int, default=6)
    u = sp.add_parser("upstream"); u.add_argument("--base", default="http://127.0.0.1:4000")
    u.add_argument("--model", default="chatgpt-gpt-6-astra"); u.add_argument("--rounds", type=int, default=5)
    s = sp.add_parser("sql"); s.add_argument("--day", required=True); s.add_argument("--api-key")
    a = p.parse_args()
    {"lanes": cmd_lanes, "upstream": cmd_upstream, "sql": cmd_sql}[a.cmd](a)


if __name__ == "__main__":
    main()
