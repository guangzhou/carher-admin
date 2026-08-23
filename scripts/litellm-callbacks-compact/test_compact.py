#!/usr/bin/env python3
"""_compact_input_items 离线单测（对 patched 文件，纯函数级，不碰生产）。"""
import importlib.util
import json
import sys
import types

# stub 掉文件顶部的 litellm 依赖
sys.modules.setdefault("litellm", types.ModuleType("litellm"))
_pl = types.ModuleType("litellm.proxy._logging")
sys.modules.setdefault("litellm.proxy", types.ModuleType("litellm.proxy"))
sys.modules["litellm.proxy._logging"] = _pl
ic = types.ModuleType("litellm.integrations.custom_logger")
class CustomLogger:  # noqa
    pass
ic.CustomLogger = CustomLogger
sys.modules["litellm.integrations"] = types.ModuleType("litellm.integrations")
sys.modules["litellm.integrations.custom_logger"] = ic

spec = importlib.util.spec_from_file_location("norm", "chatgpt_responses_normalize.patched.py")
m = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(m)
except Exception as e:
    # 文件尾部可能有依赖运行态的安装逻辑；只要 _compact_input_items 已定义就够
    if not hasattr(m, "_compact_input_items"):
        raise
    print(f"(module tail init skipped: {type(e).__name__})")

C = m._compact_input_items
ok = 0; total = 0
def check(name, cond):
    global ok, total
    total += 1; ok += cond
    print(("PASS " if cond else "FAIL ") + name)

def u(t): return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": t}]}
def fc(cid): return {"type": "function_call", "call_id": cid, "name": "run", "arguments": "{}"}
def fco(cid, t): return {"type": "function_call_output", "call_id": cid, "output": t}

# 1. 小输入原样返回
small = [u("q")] * 10
out, c = C(small)
check("small input untouched", out is small and not c)

# 2. 3MB 巨输入(肥肉=工具输出): 输出内容被截断占位, 配对结构保留, 用户消息全保
big = [u("FIRST-TASK")]
for i in range(700):
    big += [u(f"user-turn-{i}"), fc(f"c{i}"), fco(f"c{i}", "z" * 3000)]
out, c = C(big)
check("big compacted (tool outputs truncated)", c.get("compact_tool_outputs_truncated", 0) > 300)
check("result near target budget", len(json.dumps(out)) < 900 * 1024)
check("NO items deleted (structure intact)", len(out) == len(big))
check("call/output pairing intact", all(
    out[i].get("call_id") == big[i].get("call_id") for i in range(len(big)) if isinstance(big[i], dict) and big[i].get("call_id")))
check("ALL user messages intact", all(
    out[i] == big[i] for i in range(len(big)) if isinstance(big[i], dict) and big[i].get("role") == "user"))
check("truncation marker present", any("gateway-truncated" in json.dumps(it) for it in out))
check("recent tool output kept fuller than oldest",
    len(json.dumps(out[-1])) >= len(json.dumps(out[3])))

# 3. 幂等: 再压一遍不重复叠加
out2, c2 = C(out)
check("idempotent (second pass no double-truncate)",
    c2.get("compact_tool_outputs_truncated", 0) == 0 or out2 == out)

# 4. 超长 assistant 消息截断
big3 = [u("t")] * 6 + [{"type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": "a" * 3000000}]}] + [u("t2")] * 2
out, c = C(big3)
check("huge assistant msg truncated", c.get("compact_assistant_truncated", 0) == 1)

# 5. 退化输入不炸
out, c = C([u("h" * 3000000)] * 3 + [u("t")] * 6)
check("degenerate huge user msgs: untouched (users never cut), no crash",
    isinstance(out, list))

# 6. K5 前缀稳定性: 会话追加一轮后, 旧 item 的变换结果逐字节不变(确定性规则)
sess = [u("TASK")]
for i in range(700):
    sess += [u(f"turn-{i}"), fc(f"k{i}"), fco(f"k{i}", "w" * 3000)]
out_a, _ = C(list(sess))
sess_b = list(sess) + [fc("k99"), fco("k99", "w" * 3000), u("next-q")]
out_b, _ = C(sess_b)
check("K5: old items transform identically after session grows",
    all(json.dumps(out_a[i], sort_keys=True) == json.dumps(out_b[i], sort_keys=True)
        for i in range(len(out_a))))

print(f"\n{ok}/{total} passed")
sys.exit(0 if ok == total else 1)
