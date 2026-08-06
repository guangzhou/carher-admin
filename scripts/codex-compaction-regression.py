#!/usr/bin/env python3
"""codex-compaction-regression.py — zerokey 补 compaction 的前后回归对照。

用法::

    python3 scripts/codex-compaction-regression.py before   # 上线前基线
    python3 scripts/codex-compaction-regression.py after    # 上线后复测
    python3 scripts/codex-compaction-regression.py diff     # 对照

设计原则：**每条用例都要能单独回答"这次改动有没有碰它"**。

- zerokey 面：compaction 轮是要被修的（0 -> 1）；普通轮 / 别的 zerokey 组
  必须**逐字段不变**。
- 非 zerokey 面（真 chatgpt 池、deepseek、chat completions）全部是对照组，
  任何一格变了都说明门控漏了。

比对的是**形状**（HTTP 状态 / 落点部署族 / output item 类型序列 /
compaction 计数），不比自由文本 —— 文本本来就每次不同，拿它当判据会得到
一堆假阳性。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("LITELLM_BASE", "https://cc.auto-link.com.cn/pro/v1")
USER_KEY = os.environ.get("OPENAI_API_KEY", "")
MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
OUT_DIR = os.environ.get("REG_OUT_DIR", "/tmp")

CONV = [
    {"type": "message", "role": "user",
     "content": [{"type": "input_text", "text": "暗号是紫色犀牛42，记住它。"}]},
    {"type": "message", "role": "assistant",
     "content": [{"type": "output_text", "text": "好的，暗号：紫色犀牛42。"}]},
]
TRIGGER = {"type": "compaction_trigger"}
ASK = {"type": "message", "role": "user",
       "content": [{"type": "input_text", "text": "暗号是什么？只回答暗号本身。"}]}
# 回程用例里塞的压缩项。用固定明文，好判断摘要有没有真的进上下文。
PLAIN_SUMMARY = "- 用户设定的暗号是：紫色犀牛42\n- 无未完成待办"


def _key(which: str) -> str:
    return USER_KEY if which == "user" else MASTER_KEY


def _responses(model, items, key, tools=False):
    body = {"model": model, "instructions": "You are Codex.",
            "input": items, "stream": True, "store": False}
    if tools:
        body["tools"] = [{"type": "function", "name": "shell",
                          "description": "run a shell command",
                          "parameters": {"type": "object",
                                         "properties": {"cmd": {"type": "string"}},
                                         "required": ["cmd"]}}]
    req = urllib.request.Request(
        BASE + "/responses", data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    rec = {"status": None, "model_id": None, "events": {}, "items": [],
           "compaction": 0, "text": ""}
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            rec["status"] = r.status
            rec["model_id"] = r.headers.get("x-litellm-model-id")
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                p = line[5:].strip()
                if not p or p == "[DONE]":
                    continue
                try:
                    ev = json.loads(p)
                except ValueError:
                    continue
                t = ev.get("type")
                rec["events"][t] = rec["events"].get(t, 0) + 1
                if t == "response.output_item.done":
                    it = ev.get("item") or {}
                    rec["items"].append(it.get("type"))
                    if it.get("type") == "compaction":
                        rec["compaction"] += 1
                        rec["enc_prefix"] = str(it.get("encrypted_content"))[:14]
                    txt = ""
                    for part in it.get("content") or []:
                        if isinstance(part, dict) and isinstance(part.get("text"), str):
                            txt += part["text"]
                    if txt:
                        rec["text"] += txt
    except urllib.error.HTTPError as e:
        rec["status"] = e.code
        rec["text"] = e.read().decode("utf-8", "replace")[:300]
    except Exception as e:  # noqa: BLE001
        rec["status"] = "ERR"
        rec["text"] = repr(e)[:300]
    rec["text"] = rec["text"][:400]
    return rec


def _chat(model, key):
    body = {"model": model, "messages": [{"role": "user", "content": "回答一个字：好"}],
            "stream": False}
    req = urllib.request.Request(
        BASE + "/chat/completions", data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.loads(r.read())
            return {"status": r.status, "model_id": r.headers.get("x-litellm-model-id"),
                    "finish": (d.get("choices") or [{}])[0].get("finish_reason"),
                    "has_content": bool((d.get("choices") or [{}])[0]
                                        .get("message", {}).get("content"))}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "text": e.read().decode()[:200]}
    except Exception as e:  # noqa: BLE001
        return {"status": "ERR", "text": repr(e)[:200]}


# (case_id, 说明, callable) —— 说明里写清楚这一格「预期变 / 预期不变」
CASES = [
    ("zk.compact", "zerokey 压缩轮 —— 本次要修的那一格（预期 0 -> 1）",
     lambda: _responses("gpt-5.6-sol", CONV + [TRIGGER], _key("user"), tools=True)),
    ("zk.normal", "zerokey 普通轮 —— 预期完全不变",
     lambda: _responses("gpt-5.6-sol", CONV + [ASK], _key("user"), tools=True)),
    ("zk.replay", "zerokey 回程（带 compaction item）—— 预期摘要能进上下文",
     lambda: _responses("gpt-5.6-sol",
                        [{"type": "compaction", "encrypted_content": PLAIN_SUMMARY}, ASK],
                        _key("user"), tools=True)),
    ("zk.other_group", "zerokey 另一个组的普通轮 —— 预期完全不变",
     lambda: _responses("zerokey-pool-gpt-5.5", CONV + [ASK], _key("master"))),
    ("acct.compact", "真 chatgpt 池压缩轮（原生）—— 预期完全不变",
     lambda: _responses("chatgpt-gpt-5.6-sol", CONV + [TRIGGER], _key("master"))),
    ("acct.normal", "真 chatgpt 池普通轮 —— 预期完全不变",
     lambda: _responses("chatgpt-gpt-5.6-sol", CONV + [ASK], _key("master"))),
    ("ds.compact", "deepseek 压缩轮（已有实现）—— 预期完全不变",
     lambda: _responses("gpt-5.6-luna", CONV + [TRIGGER], _key("user"))),
    ("ds.normal", "deepseek 普通轮 —— 预期完全不变",
     lambda: _responses("gpt-5.6-luna", CONV + [ASK], _key("user"))),
    ("chat.control", "chat completions 面 —— 预期完全不变",
     lambda: _chat("deepseek-v4-flash", _key("user"))),
]

# 形状比对只看这些字段；自由文本不入判据。
SHAPE_KEYS = ("status", "items", "compaction", "finish", "has_content")

# 事件**计数**随生成文本长短天然浮动（delta 帧数），拿它当判据全是假阳性。
# 只比出现过哪些事件类型；delta 类连类型也不比（有没有 delta 取决于内容）。
_NOISY_EVENTS = ("delta",)


def _shape(rec):
    out = {k: rec[k] for k in SHAPE_KEYS if k in rec}
    if "events" in rec:
        out["event_types"] = sorted(
            t for t in rec["events"] if not any(n in t for n in _NOISY_EVENTS))
    mid = rec.get("model_id") or ""
    # 落点每次可能换账号（weighted affinity），只比"族"
    out["model_family"] = (mid.split("-gpt-")[0].rstrip("0123456789-") or mid)[:24]
    return out


def run(tag):
    res = {}
    for cid, desc, fn in CASES:
        rec = fn()
        res[cid] = {"desc": desc, **rec}
        mark = "compaction=%s" % rec.get("compaction") if "compaction" in rec else ""
        print(f"  {cid:16s} status={str(rec.get('status')):4s} "
              f"items={rec.get('items')} {mark} id={rec.get('model_id')}")
    path = os.path.join(OUT_DIR, f"codex-compaction-regression.{tag}.json")
    with open(path, "w") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print(f"\n写入 {path}")
    return res


def diff():
    a = json.load(open(os.path.join(OUT_DIR, "codex-compaction-regression.before.json")))
    b = json.load(open(os.path.join(OUT_DIR, "codex-compaction-regression.after.json")))
    print(f"{'case':16s} {'before':38s} {'after':38s} 判定")
    changed, same = [], []
    for cid, _desc, _fn in CASES:
        sa, sb = _shape(a[cid]), _shape(b[cid])
        verdict = "CHANGED" if sa != sb else "same"
        (changed if sa != sb else same).append(cid)
        print(f"{cid:16s} {json.dumps(sa, ensure_ascii=False)[:38]:38s} "
              f"{json.dumps(sb, ensure_ascii=False)[:38]:38s} {verdict}")
    print(f"\n变了: {changed or '（无）'}")
    print(f"没变: {same}")
    print("\n预期只有 zk.compact / zk.replay 变；其余任何一格变了都是门控漏了。")


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "before"
    if what == "diff":
        diff()
    else:
        if not USER_KEY or not MASTER_KEY:
            sys.exit("需要 OPENAI_API_KEY（用户 key）和 LITELLM_MASTER_KEY")
        print(f"== {what} ==")
        run(what)
