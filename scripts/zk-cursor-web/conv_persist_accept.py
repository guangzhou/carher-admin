#!/usr/bin/env python3
"""conv_persist_accept.py — ROI #1 conv 缓存持久化「重启穿越」验收探针(198 上跑)。

分两相,由 argv[1] = A|B 选择,定向打 82 lane(cursor-web-fc-82-terra):
  A: 原样发 replay_ls(input len 8)→ 首轮握手建 upstream conv → saveConvSession(count 8)
     → 落盘 /app/convcache/conv-cache.json。外层随后 rollout restart 82。
  B: 同一 input[0..7] + 追加一条 user 消息(len 9)→ 期望 findConvSession 命中「重启后从
     文件加载回来的」会话 → [conv] delta send 增量续发、且**不**再握手。

判据(由外层 shell grep pod 日志裁决):
  A 后:该 pod 日志有 [conv] saved;节点上 conv-cache.json 存在且含 1 条。
  重启后:新 pod boot 日志 [conv-persist] loaded 1。
  B 后:新 pod 日志出现 [conv] delta send;B 的请求窗口内无 [handshake]（穿越成功,免握手税）。

用法: python3 conv_persist_accept.py A|B
"""
import importlib.util
import json
import sys
import time

spec = importlib.util.spec_from_file_location("s3", "/home/cltx/s3_probe.py")
s3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s3)

MODEL = "cursor-web-fc-82-terra"
KEY_FILE = "/home/cltx/.convpersist_key"
PHASE = (sys.argv[1] if len(sys.argv) > 1 else "A").upper()


def drain(resp):
    out = {"completed": 0, "text_len": 0, "calls": 0, "err": None}
    try:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            dd = line[5:].strip()
            if dd == "[DONE]":
                continue
            try:
                ev = json.loads(dd)
            except ValueError:
                continue
            t = ev.get("type", "")
            if t == "response.completed":
                out["completed"] += 1
            elif t == "response.output_item.done":
                it = ev.get("item") or {}
                if it.get("type") == "message":
                    out["text_len"] += sum(len(c.get("text", "")) for c in (it.get("content") or []) if isinstance(c, dict))
                elif it.get("type") in ("function_call", "custom_tool_call"):
                    out["calls"] += 1
    except Exception as e:  # noqa
        out["err"] = f"{type(e).__name__}: {e}"
    return out


def main():
    base, mk = s3.proxy(), s3.master_key()
    if PHASE == "A":
        alias = f"convpersist-{int(time.time())}"
        kr = json.loads(s3.post(base, mk, "/key/generate",
                                {"models": [MODEL], "duration": "2h", "key_alias": alias}).read())
        key = kr["key"]
        open(KEY_FILE, "w").write(key)
        d = s3.load_fix("ls", "")
        d["model"] = MODEL
        base_n = len(d["input"])
        print(f"[A] input items={base_n} key={key[:12]}… sending…", flush=True)
        r = drain(s3.post(base, key, "/v1/responses", d))
        print(f"[A] done completed={r['completed']} calls={r['calls']} text_len={r['text_len']} err={r['err']}", flush=True)
    else:  # B
        key = open(KEY_FILE).read().strip()
        d = s3.load_fix("ls", "")
        d["model"] = MODEL
        d["input"] = d["input"] + [{"type": "message", "role": "user",
                                    "content": [{"type": "input_text", "text": "继续,再确认一下当前目录。"}]}]
        print(f"[B] input items={len(d['input'])} (prefix must match A's 8) key={key[:12]}… sending…", flush=True)
        r = drain(s3.post(base, key, "/v1/responses", d))
        print(f"[B] done completed={r['completed']} calls={r['calls']} text_len={r['text_len']} err={r['err']}", flush=True)
        # 用完即删
        try:
            s3.post(base, mk, "/key/delete", {"keys": [key]}).read()
            print("[B] scoped key deleted", flush=True)
        except Exception as e:  # noqa
            print(f"[B] key delete warn: {e}", flush=True)


if __name__ == "__main__":
    main()
