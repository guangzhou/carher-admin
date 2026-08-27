#!/usr/bin/env python3
"""contract_diet_accept.py — ROI #3.5 契约瘦身 82 canary 对照探针(198 上跑)。

打 82 lane(cursor-web-fc-82-terra),单进程两轮同会话:
  Turn 1: replay_ls(input len 8)→ 首轮握手建 upstream conv + 全份契约。
  Turn 2: 同 input[0..7] + 追加 user "统计一下刚才列出的文件数量,直接告诉我数字"
          → findConvSession 命中 → delta send → **DIET 开时本轮只带 783c mini 契约**。
判据:Turn 2 仍**服从**——要么发命令(calls>=1,即 emit ⟦cmd¦run⟧),要么给出实质自包含答案
  (text_len 有内容且非空壳),且**不翻车**(completed 且非 violation)。对照基线 = d050ac3a 全份契约
  的 Phase B(历史 delta send 正常)。外层再 grep pod 日志确认 Turn2 打了 [proto2] ... DIET。

用法: python3 contract_diet_accept.py
"""
import importlib.util
import json
import time

spec = importlib.util.spec_from_file_location("s3", "/home/cltx/s3_probe.py")
s3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s3)

MODEL = "cursor-web-fc-82-terra"


def drain(resp):
    out = {"completed": 0, "text_len": 0, "calls": 0, "err": None, "head": ""}
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
                    txt = "".join(c.get("text", "") for c in (it.get("content") or []) if isinstance(c, dict))
                    out["text_len"] += len(txt)
                    if not out["head"]:
                        out["head"] = txt[:120]
                elif it.get("type") in ("function_call", "custom_tool_call"):
                    out["calls"] += 1
    except Exception as e:  # noqa
        out["err"] = f"{type(e).__name__}: {e}"
    return out


def main():
    base, mk = s3.proxy(), s3.master_key()
    alias = f"contractdiet-{int(time.time())}"
    kr = json.loads(s3.post(base, mk, "/key/generate",
                            {"models": [MODEL], "duration": "1h", "key_alias": alias}).read())
    key = kr["key"]
    print(f"scoped key {key[:12]}… minted", flush=True)

    d = s3.load_fix("ls", "")
    d["model"] = MODEL
    n = len(d["input"])
    print(f"[T1] input items={n} sending (handshake + full contract expected)…", flush=True)
    r1 = drain(s3.post(base, key, "/v1/responses", d))
    print(f"[T1] completed={r1['completed']} calls={r1['calls']} text_len={r1['text_len']} err={r1['err']}", flush=True)

    time.sleep(2)
    d2 = s3.load_fix("ls", "")
    d2["model"] = MODEL
    d2["input"] = d2["input"] + [{"type": "message", "role": "user",
                                  "content": [{"type": "input_text",
                                               "text": "统计一下刚才列出的文件数量,直接告诉我数字。"}]}]
    print(f"[T2] input items={len(d2['input'])} (delta turn → DIET mini expected)…", flush=True)
    r2 = drain(s3.post(base, key, "/v1/responses", d2))
    print(f"[T2] completed={r2['completed']} calls={r2['calls']} text_len={r2['text_len']} err={r2['err']}", flush=True)
    print(f"[T2] head={json.dumps(r2['head'], ensure_ascii=False)}", flush=True)

    # 判据裁读
    complied = r2["completed"] >= 1 and (r2["calls"] >= 1 or r2["text_len"] >= 8)
    print(f"[VERDICT] T2 complied={complied} (completed>=1 且 (发命令 或 实质答案))", flush=True)

    try:
        s3.post(base, mk, "/key/delete", {"keys": [key]}).read()
        print("scoped key deleted", flush=True)
    except Exception as e:  # noqa
        print(f"key delete warn: {e}", flush=True)


if __name__ == "__main__":
    main()
