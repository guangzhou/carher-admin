"""gpt-6* 上网关自造 compaction 的还原（2026-10-03 cursor-huangminqi 239 次 invalid_encrypted_content）。

用法::  python3 test_chatgpt_responses_normalize_gpt6_compaction.py [被测文件路径]
"""
import base64
import copy
import json
import os
import sys

import importlib.util
import types


def _load(path):
    """同 test_chatgpt_responses_normalize_tool_id._load：无 litellm 时 stub 掉 CustomLogger。"""
    if "litellm.integrations.custom_logger" not in sys.modules:
        try:
            import litellm.integrations.custom_logger  # noqa: F401
        except Exception:
            m = types.ModuleType("litellm.integrations.custom_logger")
            m.CustomLogger = type("CustomLogger", (), {})
            for name, mod in (("litellm", types.ModuleType("litellm")),
                              ("litellm.integrations", types.ModuleType("litellm.integrations")),
                              ("litellm.integrations.custom_logger", m)):
                sys.modules[name] = mod
    spec = importlib.util.spec_from_file_location("chatgpt_responses_normalize", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

MOD = _load(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "chatgpt_responses_normalize.py"))
_results = []


def check(name, cond, detail=""):
    _results.append((name, bool(cond), detail))


def user(t):
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": t}]}


PLAIN = {"type": "compaction", "id": "cmp_01a0ff73", "encrypted_content": "结构化摘要\n1. 用户在调试脚本"}
ENV = {"type": "compaction", "id": "cmp_x", "encrypted_content": "carher-cmp-v1:" + base64.urlsafe_b64encode(
    json.dumps({"v": 1, "summary": "信封里的摘要"}, ensure_ascii=False).encode()).decode()}
NATIVE = {"type": "compaction", "id": "cmp_n", "encrypted_content": "gAAAAABo" + "Qk3xZ_" * 40 + "=="}

# 阳性：生产那条形状（明文）在 gpt-6.1-sol / gpt-6-sol 上被换成文本消息，摘要保留
for model in ("chatgpt-gpt-6.1-sol", "chatgpt-gpt-6-sol", "gpt-6-astra"):
    d = MOD._normalize_data({"model": model, "input": [copy.deepcopy(PLAIN), user("go on")]}, "test")
    it = d["input"][0]
    check(f"{model}: 明文 compaction → message", it.get("type") == "message" and "compaction" not in json.dumps(d["input"]), it)
    check(f"{model}: 摘要原文保留", "用户在调试脚本" in it["content"][0]["text"], it)
    check(f"{model}: 其余 input 不动", d["input"][1] == user("go on"), d["input"][1])

d = MOD._normalize_data({"model": "chatgpt-gpt-6-sol", "input": [copy.deepcopy(ENV)]}, "test")
check("自有信封 carher-cmp-v1 被解开", d["input"][0].get("type") == "message" and "信封里的摘要" in d["input"][0]["content"][0]["text"], d["input"][0])

# 阴性：原生密文不碰（删了=原生压缩用户失忆）
src = {"model": "chatgpt-gpt-6-sol", "input": [copy.deepcopy(NATIVE), user("x")]}
d = MOD._normalize_data(copy.deepcopy(src), "test")
check("原生 compaction 密文原样保留", d["input"] == src["input"], d["input"][0])

# 阴性：gpt-6 上其他重型清洗仍不开（不扩大爆炸半径）
src = {"model": "chatgpt-gpt-6-sol", "previous_response_id": "resp_keep",
       "input": [{"type": "reasoning", "id": "rs_1", "encrypted_content": "gAAAA", "summary": []},
                 {"type": "message", "role": "system", "content": [{"type": "input_text", "text": "sys"}]}]}
d = MOD._normalize_data(copy.deepcopy(src), "test")
check("gpt-6 不剥 reasoning 密文/不改 system/保留 previous_response_id",
      d["input"] == src["input"] and d.get("previous_response_id") == "resp_keep", d)

# 阴性：白名单内模型行为不变（仍走 compaction_drop）
d = MOD._normalize_data({"model": "chatgpt-gpt-5.6-sol", "input": [copy.deepcopy(PLAIN), user("x")]}, "test")
check("gpt-5.6 仍整项 compaction_drop", all(i.get("type") != "compaction" for i in d["input"]) and len(d["input"]) == 1, d["input"])

# 阴性：完全不相关的模型不碰 compaction
src = {"model": "sa-grok-4.7", "input": [copy.deepcopy(PLAIN)]}
d = MOD._normalize_data(copy.deepcopy(src), "test")
check("非 gpt 模型 compaction 不动", d["input"] == src["input"], d["input"])

# 无 compaction 时零改动零复制
src = {"model": "chatgpt-gpt-6-sol", "input": [user("a")]}
d = MOD._normalize_data(src, "test")
check("无 compaction 时返回同一对象", d is src)

for n, ok, det in _results:
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   -> {det!r}"[:200]))
print(f"\n{sum(ok for _, ok, _ in _results)}/{len(_results)} passed")
sys.exit(0 if all(ok for _, ok, _ in _results) else 1)
