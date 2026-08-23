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

# 2. 3MB 输入触发：首条保留 + 标记 + 尾部预算内
big = [u("FIRST-ANCHOR")] + [u("x" * 20000) for _ in range(160)] + [u("LAST")]
out, c = C(big)
check("big input compacted", c.get("compact_items_omitted", 0) > 100)
check("head anchor kept", out[0]["content"][0]["text"] == "FIRST-ANCHOR")
check("marker inserted", "gateway-compacted" in json.dumps(out[1]))
check("tail kept last item", out[-1]["content"][0]["text"] == "LAST")
check("result under budget", len(json.dumps(out)) < 700 * 1024)

# 3. 孤儿工具输出保护：裁剪边界恰好切在 fc/fco 之间 → 尾部开头的 fco 被丢
pad = [u("y" * 20000) for _ in range(160)]
# 让尾部预算刚好从 fco 开始：fco 很大占满预算窗口的开头
big2 = [u("A")] + pad + [fc("c1"), fco("c1", "z" * 400000), u("tail-q")]
out, c = C(big2)
types_seq = [it.get("type") for it in out]
check("orphan output dropped or call kept adjacent",
      ("function_call_output" not in types_seq) or
      (types_seq.index("function_call") < types_seq.index("function_call_output")))

# 4. 全是超大单项也不炸
big3 = [u("h" * 3000000)] * 3 + [u("t")] * 6
out, c = C(big3)
check("degenerate huge items no crash", isinstance(out, list))

print(f"\n{ok}/{total} passed")
sys.exit(0 if ok == total else 1)
