"""Direct gateway A/B: same request, system content as plain string vs list of text parts
(LiteLLM turns Anthropic system blocks into the list form)."""
import json, os, sys, time, urllib.request

GW, AK = os.environ["GW"], os.environ["AK"]; N = int(sys.argv[1]) if len(sys.argv) > 1 else 8
SYS = "You are an interactive CLI coding agent running on the user's machine. Use the tools to inspect the user's machine. cwd=/repo"
TOOLS = [{"type": "function", "function": {"name": "Bash", "description": "Executes a bash command and returns its output.",
          "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
         {"type": "function", "function": {"name": "Read", "description": "Reads a file from the local filesystem.",
          "parameters": {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}}}]
PROMPTS = ["看我本地的代码结构", "这个仓库里有哪些测试？", "帮我看下 README 写了什么"]
ARMS = {"sys_str": SYS, "sys_list": [{"type": "text", "text": SYS}]}
res = {a: [0, 0, 0] for a in ARMS}
for i in range(N):
    for arm, sysc in ARMS.items():
        body = {"model": "gemini-4-argon", "tools": TOOLS, "messages": [{"role": "system", "content": sysc}, {"role": "user", "content": PROMPTS[i % 3]}]}
        req = urllib.request.Request(GW + "/v1/chat/completions", data=json.dumps(body).encode(), headers={"Authorization": "Bearer " + AK, "Content-Type": "application/json"})
        t = time.time()
        try:
            with urllib.request.urlopen(req, timeout=240) as f:
                m = json.load(f)["choices"][0]["message"]
            ok = bool(m.get("tool_calls")); snip = (m.get("content") or "")[:50]
        except Exception as e:
            ok, snip = None, repr(e)[:60]
        res[arm][0 if ok else (1 if ok is False else 2)] += 1
        print(i, arm, {True: "TOOL", False: "TEXT", None: "ERR"}[ok], round(time.time() - t, 1), snip.replace("\n", " "), flush=True)
print("SUMMARY tool/text/err", json.dumps(res))
