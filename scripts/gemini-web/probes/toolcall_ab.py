"""A/B on gemini-web direct: does appending a short tool reminder to the LAST user turn raise the tool-call rate?
Arm A = request as Claude Code sends it (tool protocol only in the system prompt, which is how gemini-web injects it).
Arm B = same + reminder appended to the last user message (simulates a gateway patch, no deploy needed).
Usage: GW=... AK=... python3 toolcall_ab.py N"""
import json, os, sys, time, urllib.request

GW, AK = os.environ["GW"], os.environ["AK"]
N = int(sys.argv[1]) if len(sys.argv) > 1 else 10
REMINDER = ("\n\n[Reminder] Tools are available and you cannot see the user's machine without them. "
            "If answering needs files, commands or the environment, reply ONLY with a [ToolCalls] block as specified.")
SYSTEM = ("You are an interactive CLI coding agent. You operate on the user's local machine through tools. "
          "Use the tools to inspect files before answering. cwd=/repo\n" + "Guideline: read before editing, run tests before reporting. " * 300)
TOOLS = [
    {"type": "function", "function": {"name": "Bash", "description": "Executes a bash command and returns its output.",
     "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "Read", "description": "Reads a file from the local filesystem.",
     "parameters": {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}}},
]
PROMPTS = ["看我本地的代码结构", "这个仓库里有哪些测试？", "帮我看下 README 写了什么"]


def call(user):
    body = {"model": "gemini-4-argon", "tools": TOOLS,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]}
    req = urllib.request.Request(GW + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + AK, "Content-Type": "application/json"})
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=240) as f:
            m = json.load(f)["choices"][0]["message"]
        return bool(m.get("tool_calls")), round(time.time() - t, 1), (m.get("content") or "")[:50].replace("\n", " ")
    except Exception as e:
        return None, round(time.time() - t, 1), repr(e)[:80]


res = {"A": [0, 0, 0], "B": [0, 0, 0]}  # tool, text, error
for i in range(N):
    p = PROMPTS[i % len(PROMPTS)]
    for arm, user in (("A", p), ("B", p + REMINDER)):
        ok, dt, txt = call(user)
        res[arm][0 if ok else (1 if ok is False else 2)] += 1
        print(i, arm, {True: "TOOL", False: "TEXT", None: "ERR"}[ok], dt, txt, flush=True)
print("SUMMARY tool/text/err", json.dumps(res))
