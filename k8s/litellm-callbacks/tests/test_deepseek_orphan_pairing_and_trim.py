"""test_deepseek_orphan_pairing_and_trim.py — 孤儿 call/output 修复 + 超限裁剪。

背景（2026-08-15 生产）
----------------------
gpt 兜底落 deepseek 的请求 48h 内 82 次 400 "No tool output found for tool
call ..."、127 次 413（openresty HTML）。deepseek 是兜底链最后一跳，两类失败
都直吐客户端，用户感知 =「工具调用不了」。单变量实测判据见
``deepseek_responses_adapt._repair_tool_pairing`` docstring。

钉住：
1. 孤儿 call → 在块尾（下一个边界项之前 / 列表末尾）补占位 output，类型按
   call 类型映射（tool_search_call → tool_search_output 无 _call 后缀）
2. 孤儿 output → 删除
3. 配对完整 → 恒等（不制造 diff）
4. 并行块缺一条 output → 占位追加块尾（乱序被上游容忍，不重排）
5. 超限裁剪：从最老开始裁、保住末尾、产生的孤儿被 repair 收尾；
   预算内恒等；DEEPSEEK_INPUT_BYTE_BUDGET=0 停用
6. 端到端 _adapt：裁剪+修复的 counts 可观测

用法::

    python3 -m unittest test_deepseek_orphan_pairing_and_trim -v
"""
import importlib.util
import os
import sys
import types
import unittest

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "deepseek_responses_adapt.py")


def _load_module(name="dsra_pairing_under_test", env=None):
    if "litellm" not in sys.modules:
        litellm = types.ModuleType("litellm")
        integrations = types.ModuleType("litellm.integrations")
        custom_logger = types.ModuleType("litellm.integrations.custom_logger")

        class CustomLogger:  # noqa: D401 - stub
            pass

        custom_logger.CustomLogger = CustomLogger
        integrations.custom_logger = custom_logger
        litellm.integrations = integrations
        sys.modules["litellm"] = litellm
        sys.modules["litellm.integrations"] = integrations
        sys.modules["litellm.integrations.custom_logger"] = custom_logger
    saved = {}
    for k, v in (env or {}).items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v
    try:
        spec = importlib.util.spec_from_file_location(name, DEFAULT_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


MOD = _load_module()

U = {"role": "user", "content": [{"type": "input_text", "text": "run ls"}]}
U2 = {"role": "user", "content": [{"type": "input_text", "text": "summarize"}]}


def fc(cid, ctype="function_call"):
    return {"type": ctype, "call_id": cid, "name": "shell", "arguments": "{}"}


def fco(cid, otype="function_call_output"):
    return {"type": otype, "call_id": cid, "output": "ok"}


class PairingRepairTest(unittest.TestCase):
    def test_complete_pairs_are_identity(self):
        items = [U, fc("c1"), fco("c1"), U2]
        counts = {}
        out = MOD._repair_tool_pairing(items, counts)
        self.assertIs(out, items)
        self.assertEqual(counts, {})

    def test_orphan_call_gets_placeholder_before_boundary(self):
        counts = {}
        out = MOD._repair_tool_pairing([U, fc("c1"), U2], counts)
        self.assertEqual([i.get("type") for i in out],
                         [None, "function_call", "function_call_output", None])
        self.assertEqual(out[2]["call_id"], "c1")
        self.assertEqual(counts["orphan_call_output_inserted"], 1)

    def test_orphan_call_at_end_gets_placeholder(self):
        counts = {}
        out = MOD._repair_tool_pairing([U, fc("c1"), fco("c1"), fc("c2")], counts)
        self.assertEqual(out[-1]["type"], "function_call_output")
        self.assertEqual(out[-1]["call_id"], "c2")

    def test_orphan_output_dropped(self):
        counts = {}
        out = MOD._repair_tool_pairing([U, fco("ghost"), U2], counts)
        self.assertEqual(out, [U, U2])
        self.assertEqual(counts["orphan_output_dropped"], 1)

    def test_parallel_block_missing_one_output_appends_at_block_end(self):
        counts = {}
        out = MOD._repair_tool_pairing([U, fc("c1"), fc("c2"), fco("c2"), U2], counts)
        types_ = [i.get("type") for i in out]
        self.assertEqual(types_, [None, "function_call", "function_call",
                                  "function_call_output", "function_call_output", None])
        self.assertEqual(out[4]["call_id"], "c1")

    def test_placeholder_type_follows_call_type(self):
        counts = {}
        out = MOD._repair_tool_pairing(
            [U, fc("c1", "tool_search_call"), U2], counts)
        self.assertEqual(out[2]["type"], "tool_search_output")
        counts = {}
        out = MOD._repair_tool_pairing(
            [U, fc("c2", "custom_tool_call"), U2], counts)
        self.assertEqual(out[2]["type"], "custom_tool_call_output")

    def test_intra_turn_filler_does_not_trigger_flush(self):
        reasoning = {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "t"}]}
        items = [U, fc("c1"), reasoning, fco("c1"), U2]
        counts = {}
        out = MOD._repair_tool_pairing(items, counts)
        self.assertIs(out, items)
        self.assertEqual(counts, {})

    def test_late_output_after_boundary_gets_placeholder_and_dedup(self):
        # call 与 output 之间隔了用户消息（DeepSeek 反正配不上）：
        # 边界前补占位，迟到的真 output 删除，避免双 output
        counts = {}
        out = MOD._repair_tool_pairing([U, fc("c1"), U2, fco("c1")], counts)
        types_ = [i.get("type") for i in out]
        self.assertEqual(types_, [None, "function_call", "function_call_output", None])
        self.assertEqual(counts["orphan_call_output_inserted"], 1)
        self.assertEqual(counts["late_output_dropped"], 1)

    def test_non_list_passthrough(self):
        self.assertEqual(MOD._repair_tool_pairing("hello", {}), "hello")


class TrimTest(unittest.TestCase):
    def test_under_budget_identity(self):
        data = {"model": "deepseek-v4-flash-responses", "input": [U, U2]}
        counts = {}
        self.assertIs(MOD._trim_oversize_input(data, counts), data)
        self.assertEqual(counts, {})

    def test_over_budget_trims_oldest_keeps_tail(self):
        mod = _load_module("dsra_trim_small", env={"DEEPSEEK_INPUT_BYTE_BUDGET": "600"})
        big = {"role": "user", "content": [{"type": "input_text", "text": "x" * 300}]}
        tail = {"role": "user", "content": [{"type": "input_text", "text": "tail"}]}
        data = {"input": [big, big, big, tail]}
        counts = {}
        out = mod._trim_oversize_input(data, counts)
        self.assertEqual(out["input"][-1], tail)
        self.assertLess(len(out["input"]), 4)
        self.assertGreaterEqual(counts["oversize_items_trimmed"], 1)

    def test_trim_never_empties_input(self):
        mod = _load_module("dsra_trim_tiny", env={"DEEPSEEK_INPUT_BYTE_BUDGET": "1"})
        data = {"input": [U, U2]}
        out = mod._trim_oversize_input(data, {})
        self.assertEqual(len(out["input"]), 1)

    def test_budget_zero_disables(self):
        mod = _load_module("dsra_trim_off", env={"DEEPSEEK_INPUT_BYTE_BUDGET": "0"})
        big = {"role": "user", "content": [{"type": "input_text", "text": "x" * 10000}]}
        data = {"input": [big] * 50}
        self.assertIs(mod._trim_oversize_input(data, {}), data)


def _img_out(cid, n_bytes):
    url = "data:image/png;base64," + "A" * n_bytes
    return {"type": "function_call_output", "call_id": cid,
            "output": [{"type": "input_image", "image_url": url, "detail": "original"}]}


class ImageBudgetTest(unittest.TestCase):
    """2026-10-22 cursor-tusuhai：view_image 截图 3.5MB ⇒ 旧逻辑按字节删成 input=[]。"""

    R = {"type": "reasoning", "summary": [], "content": [{"type": "reasoning_text", "text": "r"}]}

    def _view(self, cid):
        return {"type": "function_call", "call_id": cid, "name": "view_image",
                "arguments": '{"path":"a.png","detail":"original"}'}

    def test_big_screenshot_is_not_trimmed(self):
        # 线上同形：[user, reasoning, view_image call, 3.5MB 带图 output]
        items = [U, self.R, self._view("c1"), _img_out("c1", 3_500_000)]
        data = {"model": "deepseek-v4.1-flash-responses", "input": items}
        out = MOD._adapt(dict(data), "unit")
        self.assertEqual(len(out["input"]), 4)
        self.assertEqual(out["input"][-1], items[-1])  # 图原样保留

    def test_four_screenshots_9mb_untouched(self):
        items = [U, self.R] + [self._view("c%d" % k) for k in range(4)] \
            + [_img_out("c%d" % k, 2_200_000) for k in range(4)]
        out = MOD._adapt({"model": "deepseek-v4.1-flash-responses", "input": items}, "unit")
        self.assertEqual(len(out["input"]), len(items))

    def test_text_still_trimmed_by_bytes(self):
        # 预算管的是文本：4MB 纯文本工具输出照旧按条裁（上游 1M token 墙是真的）
        mod = _load_module("dsra_trim_text", env={"DEEPSEEK_INPUT_BYTE_BUDGET": "3000000"})
        old = {"type": "function_call_output", "call_id": "c0", "output": "x" * 3_500_000}
        items = [U, fc("c0"), old, U2, fc("c1"), fco("c1"), U2]
        out = mod._adapt({"model": "deepseek-v4.1-flash-responses", "input": items}, "unit")
        self.assertNotIn(old, out["input"])
        self.assertEqual(out["input"][-1], U2)

    def test_cut_never_lands_inside_call_block(self):
        mod = _load_module("dsra_trim_block", env={"DEEPSEEK_INPUT_BYTE_BUDGET": "300"})
        filler = {"role": "user", "content": [{"type": "input_text", "text": "z" * 1000}]}
        data = {"input": [filler, fc("b1"), fc("b2"), fco("b1"), fco("b2"), U2]}
        out = mod._trim_oversize_input(data, {})
        types_ = [i.get("type") for i in out["input"]]
        self.assertEqual(types_[:2], ["function_call", "function_call"])

    def test_never_empty_after_adapt_when_last_block_is_text_huge(self):
        # 最后一块本身就超预算：宁可原样发（上游给可读的超长报错），也不删成空
        mod = _load_module("dsra_trim_lastblock", env={"DEEPSEEK_INPUT_BYTE_BUDGET": "1000"})
        items = [U, fc("c9"), {"type": "function_call_output", "call_id": "c9", "output": "y" * 5000}]
        out = mod._adapt({"model": "deepseek-v4.1-flash-responses", "input": items}, "unit")
        self.assertTrue(out["input"])
        self.assertIn("c9", [i.get("call_id") for i in out["input"] if i.get("type") == "function_call_output"])


class AdaptEndToEndTest(unittest.TestCase):
    def test_adapt_repairs_orphans_and_reports_counts(self):
        mod = _load_module("dsra_e2e_small", env={"DEEPSEEK_INPUT_BYTE_BUDGET": "1200"})
        big = {"role": "user", "content": [{"type": "input_text", "text": "y" * 1000}]}
        # 裁剪吃掉 [big, fc, big]，留下 fco 成孤儿 -> repair 删掉
        data = {
            "model": "deepseek-v4-flash-responses",
            "input": [big, fc("c9"), big, fco("c9"), U2],
        }
        out = mod._adapt(dict(data), "unit")
        cids = [i.get("call_id") for i in out["input"] if isinstance(i, dict)]
        self.assertNotIn("c9", cids)  # orphan output removed after trim
        self.assertEqual(out["input"][-1], U2)

    def test_adapt_non_deepseek_untouched(self):
        data = {"model": "gpt-5.6-sol", "input": [U, fco("ghost")]}
        out = MOD._adapt(dict(data), "unit")
        self.assertEqual(out["input"], [U, fco("ghost")])


if __name__ == "__main__":
    unittest.main()
