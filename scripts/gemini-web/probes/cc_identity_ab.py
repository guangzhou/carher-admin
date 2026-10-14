"""Tool-call rate through LiteLLM /v1/messages, 3 arms x N:
  cc_stream    : system says 'You are Claude Code...' , stream=True   (what failed 3/5)
  neutral_stream: neutral coding-agent system,           stream=True
  cc_nostream  : Claude Code system,                     stream=False
Temp key cloned from cursor-liuguoxian-std, deleted in finally."""
import json, os, sys, time, urllib.request

B = "http://127.0.0.1:30402"; MK = os.environ["MK"]; N = int(sys.argv[1]) if len(sys.argv) > 1 else 8
H = {"anthropic-version": "2023-06-01"}
TOOLS = [{"name": "Bash", "description": "Executes a bash command and returns its output.",
          "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
         {"name": "Read", "description": "Reads a file from the local filesystem.",
          "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}}]
CC = [{"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude.", "cache_control": {"type": "ephemeral"}},
      {"type": "text", "text": "You are an interactive CLI tool. Use the tools to inspect the user's machine. cwd=/repo"}]
NEU = [{"type": "text", "text": "You are an interactive CLI coding agent running on the user's machine. Use the tools to inspect the user's machine. cwd=/repo"}]
ARMS = {"cc_stream": (CC, True), "neutral_stream": (NEU, True), "cc_nostream": (CC, False)}
PROMPTS = ["看我本地的代码结构", "这个仓库里有哪些测试？", "帮我看下 README 写了什么"]


def api(path, body, key, extra=None):
    h = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}; h.update(extra or {})
    req = urllib.request.Request(B + path, data=json.dumps(body).encode(), headers=h)
    try:
        with urllib.request.urlopen(req, timeout=240) as f:
            return f.status, f.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]
    except Exception as e:
        return -1, repr(e)[:120]


def has_tool(st, txt, stream):
    if st != 200:
        return None, txt[:80]
    if stream:
        tools, text = [], ""
        for line in txt.splitlines():
            if line.startswith("data: "):
                try: d = json.loads(line[6:])
                except Exception: continue
                if d.get("type") == "content_block_start" and d["content_block"].get("type") == "tool_use": tools.append(1)
                if d.get("type") == "content_block_delta" and d["delta"].get("type") == "text_delta": text += d["delta"]["text"]
        return bool(tools), text[:60]
    d = json.loads(txt)
    return any(c.get("type") == "tool_use" for c in d.get("content", [])), " ".join(c.get("text", "") for c in d.get("content", []))[:60]


q = urllib.request.Request(B + "/key/list?return_full_object=true&size=1&key_alias=cursor-liuguoxian-std", headers={"Authorization": "Bearer " + MK})
src = json.load(urllib.request.urlopen(q))["keys"][0]
st, k = api("/key/generate", {"key_alias": "tmp-cc-id-ab-%d" % time.time(), "models": src["models"], "aliases": src.get("aliases") or {}, "duration": "60m", "max_budget": 3.0}, MK)
tk = json.loads(k)["key"]; res = {a: [0, 0, 0] for a in ARMS}
try:
    for i in range(N):
        for arm, (system, stream) in ARMS.items():
            t = time.time()
            st, txt = api("/v1/messages", {"model": "gemini-4-argon", "max_tokens": 4096, "stream": stream, "system": system,
                                           "tools": TOOLS, "messages": [{"role": "user", "content": PROMPTS[i % 3]}]}, tk, H)
            ok, snip = has_tool(st, txt, stream)
            res[arm][0 if ok else (1 if ok is False else 2)] += 1
            print(i, arm, {True: "TOOL", False: "TEXT", None: "ERR"}[ok], round(time.time() - t, 1), snip.replace("\n", " "), flush=True)
finally:
    api("/key/delete", {"keys": [tk]}, MK); print("tmp key deleted", flush=True)
print("SUMMARY tool/text/err", json.dumps(res))
