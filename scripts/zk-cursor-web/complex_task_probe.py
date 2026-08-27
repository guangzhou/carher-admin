#!/usr/bin/env python3
"""complex_probe — 用户复杂任务原话,新会话首轮验收:必须动手不许只宣布。scoped key用完即删。"""
import importlib.util, json, time
spec = importlib.util.spec_from_file_location("s3", "/home/cltx/s3_probe.py")
s3 = importlib.util.module_from_spec(spec); spec.loader.exec_module(s3)
TASK = "帮我梳理下这个工程的代码结构，写到飞书文档上，架构用飞书画板画图（不要mermaid 风格的图）go"
base, mk = s3.proxy(), s3.master_key()
r = json.loads(s3.post(base, mk, "/key/generate", {"models":["cursor-web-fc-82-terra"],"duration":"1h","key_alias":"cxp-%d"%int(time.time())}).read())
key = r["key"]
try:
    for run in (1,2):
        d = s3.load_fix("ls", "")
        d["model"] = "cursor-web-fc-82-terra"
        # 把最后一条 user 的 <user_query> 换成复杂任务原话
        for it in reversed(d.get("input", [])):
            if it.get("type") == "message" and it.get("role") == "user":
                for c in it.get("content", []):
                    if isinstance(c, dict) and "<user_query>" in str(c.get("text","")):
                        c["text"] = "<user_query>\n" + TASK + "\n</user_query>"
                break
        t0=time.time(); txt=""; calls=[]
        resp = s3.post(base, key, "/v1/responses", d, timeout=180)
        for raw in resp:
            line = raw.decode("utf-8","replace").strip()
            if not line.startswith("data:"): continue
            dd = line[5:].strip()
            if dd == "[DONE]": continue
            try: ev = json.loads(dd)
            except ValueError: continue
            if ev.get("type") == "response.output_item.done":
                it = ev.get("item") or {}
                if it.get("type") == "message":
                    txt += "".join(c.get("text","") for c in (it.get("content") or []) if isinstance(c,dict))
                if it.get("type") in ("function_call","custom_tool_call"):
                    calls.append(str(it.get("arguments") or it.get("input") or "")[:100])
        acted = bool(calls)
        print("[run%d] lat=%.1fs ACTED=%s prose=%r call=%s" % (run, time.time()-t0, acted, txt[:80], (calls[0] if calls else None)), flush=True)
        time.sleep(2)
finally:
    s3.post(base, mk, "/key/delete", {"keys":[key]}).read()
