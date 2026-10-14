"""Repeated Claude Code-shaped regression for gemini-4-argon via LiteLLM /v1/messages.
Temp key cloned from cursor-liuguoxian-std (models+aliases), deleted in finally."""
import json, os, random, sys, time, urllib.request

B = "http://127.0.0.1:30402"
MK = os.environ["MK"]
ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 5
H = {"anthropic-version": "2023-06-01"}


def api(path, body, key, extra=None, stream=False):
    h = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    h.update(extra or {})
    req = urllib.request.Request(B + path, data=json.dumps(body).encode(), headers=h)
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=300) as f:
            return f.status, f.read().decode(), round(time.time() - t, 1)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300], round(time.time() - t, 1)
    except Exception as e:  # timeout etc.
        return -1, repr(e)[:200], round(time.time() - t, 1)


TOOLS = [
    {"name": "Bash", "description": "Executes a bash command and returns its output.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}, "description": {"type": "string"},
                      "timeout": {"type": "number"}}, "required": ["command"], "additionalProperties": False,
                      "$schema": "http://json-schema.org/draft-07/schema#"}},
    {"name": "Read", "description": "Reads a file from the local filesystem.",
     "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}, "offset": {"type": "integer"},
                      "limit": {"type": "integer"}}, "required": ["file_path"]}},
    {"name": "Edit", "description": "Performs exact string replacement in a file.",
     "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}, "old_string": {"type": "string"},
                      "new_string": {"type": "string"}, "replace_all": {"type": "boolean", "default": False}},
                      "required": ["file_path", "old_string", "new_string"]}},
]
SYSTEM = [{"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude.", "cache_control": {"type": "ephemeral"}},
          {"type": "text", "text": "You are an interactive CLI tool. Use the tools to inspect the user's machine. cwd=/repo"}]


def sse_summary(txt):
    kinds, tool_uses, text = [], [], ""
    for line in txt.splitlines():
        if line.startswith("data: "):
            try:
                d = json.loads(line[6:])
            except Exception:
                continue
            kinds.append(d.get("type"))
            if d.get("type") == "content_block_start" and d["content_block"].get("type") == "tool_use":
                tool_uses.append(d["content_block"]["name"])
            if d.get("type") == "content_block_delta" and d["delta"].get("type") == "text_delta":
                text += d["delta"]["text"]
            if d.get("type") == "error":
                return "error", d, ""
    return ("message_stop" in kinds), tool_uses, text


def main():
    q = urllib.request.Request(B + "/key/list?return_full_object=true&size=1&key_alias=cursor-liuguoxian-std",
                               headers={"Authorization": "Bearer " + MK})
    src = json.load(urllib.request.urlopen(q))["keys"][0]
    st, k, _ = api("/key/generate", {"key_alias": "tmp-cc-regress-%d" % time.time(), "models": src["models"],
                                     "aliases": src.get("aliases") or {}, "duration": "60m", "max_budget": 3.0}, MK)
    tk = json.loads(k)["key"]
    stats = {}

    def rec(name, ok, detail):
        stats.setdefault(name, [0, 0])
        stats[name][0 if ok else 1] += 1
        print(("  OK  " if ok else "  RED ") + name, detail, flush=True)

    try:
        for r in range(ROUNDS):
            print("round", r + 1, flush=True)
            n = "NONCE-%d" % random.randint(10 ** 8, 10 ** 9)
            # 1 streaming hi with tools present (what Claude Code sends first)
            st, txt, dt = api("/v1/messages", {"model": "gemini-4-argon", "max_tokens": 4096, "stream": True, "system": SYSTEM,
                                                "tools": TOOLS, "messages": [{"role": "user", "content": [{"type": "text", "text": "Reply with exactly: " + n}]}]}, tk, H)
            done, tu, text = sse_summary(txt) if st == 200 else (False, [], "")
            rec("stream+tools nonce", st == 200 and done is True and n in text, (st, dt, text[:60] if st == 200 else txt[:160]))
            # 2 streaming, model should call a tool
            st, txt, dt = api("/v1/messages", {"model": "gemini-4-argon", "max_tokens": 4096, "stream": True, "system": SYSTEM,
                                                "tools": TOOLS, "messages": [{"role": "user", "content": "看我本地的代码结构"}]}, tk, H)
            done, tu, text = sse_summary(txt) if st == 200 else (False, [], "")
            rec("stream tool_use", st == 200 and done is True and len(tu) > 0, (st, dt, tu, text[:60] if st == 200 else txt[:160]))
            # 3 non-stream tool round-trip with tool_result
            st, txt, dt = api("/v1/messages", {"model": "gemini-4-argon", "max_tokens": 4096, "system": SYSTEM, "tools": TOOLS,
                                                "messages": [{"role": "user", "content": "看我本地的代码结构"}]}, tk, H)
            ok = False
            if st == 200:
                d = json.loads(txt)
                tus = [c for c in d.get("content", []) if c.get("type") == "tool_use"]
                if tus:
                    tid = tus[0]["id"]
                    msgs = [{"role": "user", "content": "看我本地的代码结构"},
                            {"role": "assistant", "content": d["content"]},
                            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid,
                                                          "content": "README.md\nbackend/\nfrontend/\noperator-go/\nk8s/\nmarker-ZQX%d" % r}]}]
                    st2, txt2, dt2 = api("/v1/messages", {"model": "gemini-4-argon", "max_tokens": 4096, "stream": True, "system": SYSTEM,
                                                           "tools": TOOLS, "messages": msgs}, tk, H)
                    done2, tu2, text2 = sse_summary(txt2) if st2 == 200 else (False, [], "")
                    ok = st2 == 200 and done2 is True and (len(text2) > 0 or len(tu2) > 0)
                    rec("tool_result follow-up", ok, (st2, dt2, tu2, text2[:80] if st2 == 200 else txt2[:160]))
                else:
                    rec("tool_result follow-up", False, ("no tool_use in step1", txt[:160]))
            else:
                rec("tool_result follow-up", False, ("step1", st, txt[:160]))
    finally:
        api("/key/delete", {"keys": [tk]}, MK)
        print("tmp key deleted", flush=True)
    print("SUMMARY", json.dumps(stats, ensure_ascii=False))


main()
