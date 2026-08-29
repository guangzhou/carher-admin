#!/usr/bin/env python3
"""a6_probe.py — A线目录瘦身真载荷验收(198 上跑)。
同一份今天真抓包 cap-6(带 <mcp_server_catalog>),两臂单变量:
  --arm cat N     : 原样带目录首轮 N 发
  --arm nocat N   : 程序化摘除目录后的首轮 N 发(其余逐字节同)
scoped key 存 /home/cltx/tooldiet/a6_key.txt,--cleanup 删。master key 仅 mint/delete。
"""
import base64, json, re, subprocess, sys, time, urllib.request

NS = "litellm-product"
MODEL = "cursor-web-fc-82-terra"
CAP = "/home/cltx/tooldiet/cap-6.json"
KEYF = "/home/cltx/tooldiet/a6_key.txt"

def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout

def proxy():
    ip = sh(f"kubectl -n {NS} get svc litellm-proxy -o jsonpath='{{.spec.clusterIP}}'").strip().strip("'")
    return f"http://{ip}:4000"

def master_key():
    data = json.loads(sh(f"kubectl -n {NS} get secret litellm-secrets -o json"))["data"]
    return base64.b64decode(data["LITELLM_MASTER_KEY"]).decode()

def post(base, key, path, body, timeout=300):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)

def get_key(base):
    try:
        return open(KEYF).read().strip()
    except FileNotFoundError:
        r = json.loads(post(base, master_key(), "/key/generate",
                            {"models": [MODEL], "duration": "3h",
                             "key_alias": f"a6-tooldiet-{int(time.time())}"}).read())
        open(KEYF, "w").write(r["key"])
        return r["key"]

def stream(base, key, body):
    r = {"completed": 0, "text": "", "calls": 0, "err": None, "lat": None}
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
                if (ev.get("item") or {}).get("type") == "function_call": r["calls"] += 1
            elif t == "response.completed": r["completed"] += 1
    except Exception as e:
        r["err"] = str(e)[:150]
    r["lat"] = round(time.time() - t0, 1)
    return r

def load_cap(strip_catalog):
    cap = json.load(open(CAP))
    instructions, tools, item0 = cap["instructions"], cap["tools"], cap["input"][0]
    if strip_catalog:
        def strip_txt(t):
            return re.sub(r"<mcp_server_catalog>[\s\S]*?</mcp_server_catalog>\n?", "", t)
        c = item0.get("content")
        if isinstance(c, list):
            c = [dict(x, text=strip_txt(x.get("text",""))) if isinstance(x, dict) and "text" in x else x for x in c]
            item0 = dict(item0, content=c)
        instructions = strip_txt(instructions)
    return instructions, tools, item0

def uq_item(text):
    return {"type": "message", "role": "user",
            "content": [{"type": "input_text", "text": f"<user_query>\n{text}\n</user_query>"}]}

def main():
    base = proxy()
    if sys.argv[1] == "--cleanup":
        try:
            k = open(KEYF).read().strip()
            post(base, master_key(), "/key/delete", {"keys": [k]})
            print("key deleted")
        except Exception as e: print("cleanup:", e)
        return
    arm = sys.argv[2]; n = int(sys.argv[3])
    key = get_key(base)
    instructions, tools, item0 = load_cap(strip_catalog=(arm == "nocat"))
    empty = 0
    for i in range(n):
        uq = f"随便写个飞书文档，我只是说测试 (A6-{arm}-{i+1},忽略本括号,{int(time.time())})"
        r = stream(base, key, {"model": MODEL, "stream": True, "tool_choice": "auto",
                               "instructions": instructions, "tools": tools,
                               "input": [item0, uq_item(uq)]})
        is_empty = (not r["text"].strip()) and r["calls"] == 0 and r["completed"] > 0
        if is_empty or r["err"]: empty += 1
        print(json.dumps({"arm": arm, "round": i+1, "empty": is_empty, "err": r["err"],
                          "lat": r["lat"], "text_len": len(r["text"].strip()), "calls": r["calls"]},
                         ensure_ascii=False))
    print(f"ARM={arm} N={n} EMPTY={empty}")

if __name__ == "__main__":
    main()
