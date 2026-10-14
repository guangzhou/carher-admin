"""Same request through gateway direct vs LiteLLM /v1/chat/completions; payload sizes 1KB/50KB/120KB (Claude Code is ~60-120KB)."""
import json, os, time, urllib.request

GW = os.environ["GW"]; AK = os.environ["AK"]; LL = "http://127.0.0.1:30402"; MK = os.environ["MK"]


def api(base, path, key, body):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as f:
        return json.load(f)


def stream(base, key, body):
    body = dict(body, stream=True)
    req = urllib.request.Request(base + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    t = time.time(); first = None
    with urllib.request.urlopen(req, timeout=300) as f:
        for line in f:
            if first is None and line.startswith(b"data: ") and (b'"content":"' in line or b"tool_calls" in line):
                first = time.time() - t
    return round(first or -1, 1), round(time.time() - t, 1)


q = urllib.request.Request(LL + "/key/list?return_full_object=true&size=1&key_alias=cursor-liuguoxian-std", headers={"Authorization": "Bearer " + MK})
src = json.load(urllib.request.urlopen(q))["keys"][0]
tk = api(LL, "/key/generate", MK, {"key_alias": "tmp-lat-%d" % time.time(), "models": src["models"], "aliases": src.get("aliases") or {}, "duration": "30m", "max_budget": 2.0})["key"]
tools = [{"type": "function", "function": {"name": "tool_%d" % i, "description": ("Tool %d. " % i) + "Detailed usage notes. " * 60,
          "parameters": {"type": "object", "properties": {"a": {"type": "string", "description": "x" * 200}}}}} for i in range(20)]
try:
    for kb in (1, 50, 120):
        sysmsg = "You are a coding assistant.\n" + ("Guideline: be careful, read before editing, run tests. " * (kb * 1024 // 55))
        body = {"model": "gemini-4-argon", "messages": [{"role": "system", "content": sysmsg}, {"role": "user", "content": "Say hi in one word."}],
                "tools": tools if kb > 1 else None}
        body = {k: v for k, v in body.items() if v is not None}
        size = len(json.dumps(body)) // 1024
        for i in range(2):
            g = stream(GW, AK, body); l = stream(LL, tk, body)
            print("payload=%3dKB  gateway first/total=%s  litellm first/total=%s" % (size, g, l), flush=True)
finally:
    api(LL, "/key/delete", MK, {"keys": [tk]}); print("tmp key deleted")
