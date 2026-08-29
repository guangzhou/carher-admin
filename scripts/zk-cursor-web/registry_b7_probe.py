#!/usr/bin/env python3
"""b7_probe.py — B线 ⟦call⟧/MCP 真往返验收。作废线:MCP 信封命中 <6/12 → B线作废。
命中定义:模型该轮发出的 function_call 名 ∈ {CallMcpTool, GetMcpTools}(走了 MCP 通道)。
--digest: 取一次 CallMcpTool 调用,喂回带独创标记的结果,验 turn-2 逐字消化。
"""
import base64, json, subprocess, sys, time, urllib.request

NS = "litellm-product"; MODEL = "cursor-web-fc-82-terra"
CAP = "/home/cltx/tooldiet/cap-6.json"; KEYF = "/home/cltx/tooldiet/b7_key.txt"

def sh(c): return subprocess.run(c, shell=True, capture_output=True, text=True).stdout
def proxy():
    ip = sh(f"kubectl -n {NS} get svc litellm-proxy -o jsonpath='{{.spec.clusterIP}}'").strip().strip("'")
    return f"http://{ip}:4000"
def master_key():
    d = json.loads(sh(f"kubectl -n {NS} get secret litellm-secrets -o json"))["data"]
    return base64.b64decode(d["LITELLM_MASTER_KEY"]).decode()
def post(base, key, path, body, timeout=300):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)
def get_key(base):
    try: return open(KEYF).read().strip()
    except FileNotFoundError:
        r = json.loads(post(base, master_key(), "/key/generate",
            {"models": [MODEL], "duration": "3h", "key_alias": f"b7-registry-{int(time.time())}"}).read())
        open(KEYF, "w").write(r["key"]); return r["key"]
def stream(base, key, body):
    r = {"completed": 0, "text": "", "calls": [], "err": None, "lat": None}
    t0 = time.time()
    try:
        resp = post(base, key, "/v1/responses", body)
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"): continue
            dd = line[5:].strip()
            if dd == "[DONE]": continue
            try: ev = json.loads(dd)
            except ValueError: continue
            t = ev.get("type", "")
            if t == "response.output_text.delta": r["text"] += ev.get("delta", "")
            elif t == "response.output_item.done":
                it = ev.get("item") or {}
                if it.get("type") == "function_call":
                    r["calls"].append({"name": it.get("name"), "call_id": it.get("call_id"), "arguments": it.get("arguments")})
            elif t == "response.completed": r["completed"] += 1
    except Exception as e: r["err"] = str(e)[:150]
    r["lat"] = round(time.time() - t0, 1)
    return r
def cap_base():
    cap = json.load(open(CAP)); return cap["instructions"], cap["tools"], cap["input"][0]
def uq(text):
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"<user_query>\n{text}\n</user_query>"}]}

TASKS = [
 "用浏览器打开 https://example.com,然后告诉我页面标题是什么",
 "看看我现在浏览器里开着哪些标签页,列给我",
 "用浏览器访问 https://www.baidu.com 并截一张图",
 "把当前这个聊天重命名为「B7验收测试」",
 "用浏览器打开 https://httpbin.org/get,把返回的 origin 字段读给我",
 "浏览器里当前页面往下滚动一屏,然后截图",
 "用浏览器查一下 https://example.org 的页面快照结构",
 "帮我在 Cursor 里新建一个叫 b7test 的项目",
 "用浏览器打开 https://www.qq.com,告诉我首页第一条新闻标题",
 "列出浏览器标签页,把第一个标签页截图",
 "用浏览器导航到 https://news.ycombinator.com,读前三条标题",
 "把浏览器当前标签页锁定,准备做自动化",
]

def main():
    base = proxy(); key = get_key(base)
    instructions, tools, item0 = cap_base()
    if sys.argv[1] == "--cleanup":
        try:
            post(base, master_key(), "/key/delete", {"keys": [open(KEYF).read().strip()]}); print("key deleted")
        except Exception as e: print("cleanup:", e)
        return
    if sys.argv[1] == "--run":
        hits = 0; mcp_call = None
        for i, task in enumerate(TASKS):
            r = stream(base, key, {"model": MODEL, "stream": True, "tool_choice": "auto",
                "instructions": instructions, "tools": tools, "input": [item0, uq(task + f" (B7-{i+1},{int(time.time())})")]})
            names = [c["name"] for c in r["calls"]]
            mcp = any(n in ("CallMcpTool", "GetMcpTools") for n in names)
            if mcp:
                hits += 1
                if not mcp_call:
                    for c in r["calls"]:
                        if c["name"] == "CallMcpTool": mcp_call = {"task": task, "call": c}; break
            print(json.dumps({"round": i+1, "mcp_hit": mcp, "calls": names, "lat": r["lat"],
                              "text_len": len(r["text"].strip()), "err": r["err"]}, ensure_ascii=False))
        print(f"HITS={hits}/12")
        if mcp_call: open("/home/cltx/tooldiet/b7_call.json", "w").write(json.dumps(mcp_call, ensure_ascii=False))
        return
    if sys.argv[1] == "--digest":
        saved = json.load(open("/home/cltx/tooldiet/b7_call.json"))
        c = saved["call"]; task = saved["task"]
        marker = "ZKB7_TITLE_MARKER_9X3Q"
        fc = {"type": "function_call", "call_id": c["call_id"], "name": c["name"], "arguments": c["arguments"]}
        fco = {"type": "function_call_output", "call_id": c["call_id"],
               "output": json.dumps({"content": [{"type": "text", "text": f"page loaded, title: {marker}"}], "isError": False})}
        r = stream(base, key, {"model": MODEL, "stream": True, "tool_choice": "auto",
            "instructions": instructions, "tools": tools, "input": [item0, uq(task), fc, fco]})
        ok = marker in r["text"]
        print(json.dumps({"digest_verbatim": ok, "text": r["text"][:200], "calls": [x["name"] for x in r["calls"]],
                          "lat": r["lat"], "err": r["err"]}, ensure_ascii=False))

if __name__ == "__main__": main()
