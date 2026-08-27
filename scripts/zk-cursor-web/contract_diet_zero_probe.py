#!/usr/bin/env python3
"""contract_diet_zero_probe.py — DIET 零档(ZK_CONTRACT_DIET=2)多轮服从探针(198 上跑)。

问题:增量轮完全不带任何契约提醒(协议只靠首轮全份+握手),模型多轮后还服不服?
设计:同一会话 10 轮,任务/闲聊/知识题交替——任务轮该动手(发 ⟦cmd¦run⟧→function_call)
或给实质自包含答案;闲聊/知识轮该纯 prose 且**不该**乱发命令(IF AND ONLY IF 条款)。
每轮判形状,最后汇总:complied 率 + 形状错误明细。外层再 grep pod 日志对账
(每增量轮应打 [proto2] v2 contract DIET-ZERO,verdict 无 violation)。

用法: python3 contract_diet_zero_probe.py
"""
import importlib.util
import json
import time

spec = importlib.util.spec_from_file_location("s3", "/home/cltx/s3_probe.py")
s3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s3)

MODEL = "cursor-web-fc-82-terra"

# (文本, 类型) 类型: task=该动手或实质答  chat=纯prose不动手  know=纯prose不动手
TURNS = [
    ("统计一下刚才列出的文件数量,直接告诉我数字。", "task"),
    ("hi", "chat"),
    ("TCP 三次握手是哪三步?一句话说完。", "know"),
    ("看看当前目录下有没有 .git 目录。", "task"),
    ("谢谢", "chat"),
    ("python 里 list 和 tuple 的区别,一句话。", "know"),
    ("把系统当前时间打出来。", "task"),
    ("hello", "chat"),
    ("查一下磁盘使用率。", "task"),
]


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
                        out["head"] = txt[:100]
                elif it.get("type") in ("function_call", "custom_tool_call"):
                    out["calls"] += 1
    except Exception as e:  # noqa
        out["err"] = f"{type(e).__name__}: {e}"
    return out


def judge(kind, r):
    """返回 (ok, why)。task=动手或实质答;chat/know=有prose且不乱动手。"""
    if r["err"] or r["completed"] < 1:
        return False, f"transport/incomplete err={r['err']} completed={r['completed']}"
    if kind == "task":
        if r["calls"] >= 1 or r["text_len"] >= 8:
            return True, "acted-or-answered"
        return False, "task turn: no cmd, no substantive text"
    # chat / know
    if r["calls"] >= 1:
        return False, f"{kind} turn emitted a command (IF-AND-ONLY-IF violated)"
    if r["text_len"] >= 4:
        return True, "prose-only"
    return False, f"{kind} turn: empty deliverable (len={r['text_len']})"


def main():
    base, mk = s3.proxy(), s3.master_key()
    alias = f"dietzero-{int(time.time())}"
    kr = json.loads(s3.post(base, mk, "/key/generate",
                            {"models": [MODEL], "duration": "1h", "key_alias": alias}).read())
    key = kr["key"]
    print(f"scoped key {key[:12]}… minted", flush=True)

    d = s3.load_fix("ls", "")
    d["model"] = MODEL
    print(f"[T1/task] handshake+full contract, input items={len(d['input'])}…", flush=True)
    r = drain(s3.post(base, key, "/v1/responses", d))
    ok, why = judge("task", r)
    results = [("T1", "task", ok, why, r)]
    print(f"[T1] completed={r['completed']} calls={r['calls']} text={r['text_len']} → {'OK' if ok else 'BAD'} ({why})", flush=True)

    inp = list(d["input"])
    for i, (text, kind) in enumerate(TURNS, start=2):
        time.sleep(2)
        inp = inp + [{"type": "message", "role": "user",
                      "content": [{"type": "input_text", "text": text}]}]
        d2 = s3.load_fix("ls", "")
        d2["model"] = MODEL
        d2["input"] = inp
        r = drain(s3.post(base, key, "/v1/responses", d2))
        ok, why = judge(kind, r)
        results.append((f"T{i}", kind, ok, why, r))
        print(f"[T{i}/{kind}] completed={r['completed']} calls={r['calls']} text={r['text_len']} "
              f"→ {'OK' if ok else 'BAD'} ({why}) head={json.dumps(r['head'][:60], ensure_ascii=False)}", flush=True)

    n_ok = sum(1 for _, _, ok, _, _ in results if ok)
    print(f"\n[VERDICT] {n_ok}/{len(results)} complied", flush=True)
    for tid, kind, ok, why, _ in results:
        if not ok:
            print(f"  [BAD] {tid}/{kind}: {why}", flush=True)

    try:
        s3.post(base, mk, "/key/delete", {"keys": [key]}).read()
        print("scoped key deleted", flush=True)
    except Exception as e:  # noqa
        print(f"key delete warn: {e}", flush=True)


if __name__ == "__main__":
    main()
