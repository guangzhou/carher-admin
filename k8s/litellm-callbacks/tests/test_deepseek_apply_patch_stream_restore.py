"""apply_patch 在 deepseek 上流式回成 function_call（2026-10-03）。

直打本地 deepseek /v1/responses 实测：custom ``apply_patch`` 进 → 上游回 ``function_call``。
deepseek_responses_adapt 不降级它也不登记，deepseek_id_prefix 的流式还原名单里就没有它。
修法只在出站取名单处补：客户端把 apply_patch 声明成 custom，就进还原名单。

用法::  python3 test_deepseek_apply_patch_stream_restore.py [deepseek_id_prefix.py 路径]
"""
import importlib.util
import os
import sys
import types

PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "deepseek_id_prefix.py")
if "litellm.integrations.custom_logger" not in sys.modules:
    m = types.ModuleType("litellm.integrations.custom_logger"); m.CustomLogger = type("CustomLogger", (), {})
    for n, mod in (("litellm", types.ModuleType("litellm")), ("litellm.integrations", types.ModuleType("litellm.integrations")),
                   ("litellm.integrations.custom_logger", m)):
        sys.modules.setdefault(n, mod)
spec = importlib.util.spec_from_file_location("dsidp_under_test", PATH)
M = importlib.util.module_from_spec(spec); spec.loader.exec_module(M)

R = []
def check(name, cond, det=""): R.append((name, bool(cond), det))

class _Lo:
    def __init__(self, tools=None, meta=None):
        self.model_call_details = {"tools": tools, "litellm_metadata": meta or {}}
        self.litellm_params = {}; self.optional_params = {}
class _It:
    def __init__(self, lo): self.logging_obj = lo

AP_CUSTOM = {"type": "custom", "name": "apply_patch", "format": {"type": "text"}}
# 阳性：生产那条形状（客户端 custom apply_patch，未登记降级）
check("custom apply_patch → 进还原名单", M._custom_names_from_iterator(_It(_Lo([AP_CUSTOM]))) == {"apply_patch"})
# 与已登记的降级名合并，不互相覆盖
check("和降级名 exec 合并", M._custom_names_from_iterator(_It(_Lo([AP_CUSTOM], {"deepseek_downgraded_custom_tools": ["exec"]}))) == {"apply_patch", "exec"})
# 阴性：客户端声明成 function 的 apply_patch，回 function_call 本来就对
check("function apply_patch 不进名单", M._custom_names_from_iterator(_It(_Lo([{"type": "function", "name": "apply_patch"}]))) == set())
# 阴性：其他 custom 工具名不靠这条路（仍只认登记）
check("custom exec 未登记时不进", M._custom_names_from_iterator(_It(_Lo([{"type": "custom", "name": "exec"}]))) == set())
# 原有行为不变
check("只有登记名时行为不变", M._custom_names_from_iterator(_It(_Lo(None, {"deepseek_downgraded_custom_tools": ["exec"]}))) == {"exec"})
check("无 logging_obj 不报错", M._custom_names_from_iterator(types.SimpleNamespace()) == set())
check("tools 非 list 不报错", M._custom_names_from_iterator(_It(_Lo("weird"))) == set())
# 下游还原函数本身对 apply_patch 生效（名单进去以后真的会被改回 custom）
obj = {"type": "response.output_item.done", "item": {"type": "function_call", "id": "fc_1", "name": "apply_patch", "arguments": "{\"input\": \"*** Begin Patch\"}"}}
cnt = {}; M._restore_custom_in_obj(obj, {"apply_patch"}, set(), cnt)
check("下游把 function_call 还原成 custom_tool_call", obj["item"]["type"] == "custom_tool_call" and obj["item"]["input"] == "*** Begin Patch", obj)

for n, ok, d in R: print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"  -> {d!r}"[:160]))
print(f"\n{sum(o for _, o, _ in R)}/{len(R)} passed"); sys.exit(0 if all(o for _, o, _ in R) else 1)
