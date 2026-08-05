"""test_deepseek_reasoning_insert_pairing.py — 补 reasoning 不许劈开并行工具调用。

背景（2026-08-04 生产故障）
--------------------------
Codex 打 ``deepseek-v4-flash-responses``，第二轮起整单 400::

    litellm.BadRequestError: OpenAIException -
    {"error":{"message":"No tool output found for tool call
     call_00_0WAoGDLwNnmouIVTqXki0916.", ...}}

DeepSeek 用「**相邻**的 tool call 属于同一轮」给 call / output 配对。
``deepseek_responses_adapt`` 原本的判据是「前一项不是带 reasoning_text 的
reasoning 就补一条」—— 对同一轮里的第 2、3 条并行调用同样成立，于是它自己在
``fc1`` 和 ``fc2`` 之间插了一条 reasoning，把这一轮劈成两半，``fco1`` 就配不上
``fc1`` 了。

api.deepseek.com 单变量实测（只动中间那一项，call_id 全部配对齐全）：

    [msg, R, fc1, fc2, fco1, fco2]        -> 200 completed
    [msg, R, fc1, R, fc2, fco1, fco2]     -> 400 No tool output found   <- 原实现
    [msg, R, fc1, fco1, fc2, fco2]        -> 400 reasoning_text 必须回传
    [msg, R, fc1, fco1, R, fc2, fco2]     -> 200 completed

所以规则是「每一轮开头补一条，轮内一条都不能插」，而不是「每条调用前都补」。
DeepSeek 一轮确实会返回多条 ``function_call``（实测输出项
``['reasoning','function_call','function_call']``），这条形状是常态。

本文件钉住这个规则，防止有人把判据改回「每条 tool call 前都补」。用法::

    python3 -m unittest test_deepseek_reasoning_insert_pairing -v
"""
import importlib.util
import os
import sys
import types
import unittest

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "deepseek_responses_adapt.py")


def _load(path):
    """加载被测模块；litellm 不在时用 stub 顶掉，使本地也能跑回归。"""
    if "litellm.integrations.custom_logger" not in sys.modules:
        try:
            import litellm.integrations.custom_logger  # noqa: F401
        except Exception:
            litellm = types.ModuleType("litellm")
            integrations = types.ModuleType("litellm.integrations")
            custom_logger = types.ModuleType("litellm.integrations.custom_logger")

            class CustomLogger:  # minimal stub
                pass

            custom_logger.CustomLogger = CustomLogger
            integrations.custom_logger = custom_logger
            litellm.integrations = integrations
            sys.modules["litellm"] = litellm
            sys.modules["litellm.integrations"] = integrations
            sys.modules["litellm.integrations.custom_logger"] = custom_logger

    spec = importlib.util.spec_from_file_location("deepseek_responses_adapt", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MOD = _load(os.environ.get("DEEPSEEK_ADAPT_PATH", DEFAULT_PATH))

MSG = {"type": "message", "role": "user",
       "content": [{"type": "input_text", "text": "run ls and pwd"}]}
REAL_REASONING = {"type": "reasoning", "id": "8e40e132", "status": "completed",
                  "summary": [],
                  "content": [{"type": "reasoning_text", "text": "call the tool twice"}]}


def fc(n):
    return {"type": "function_call", "id": "uuid-%d" % n, "status": "completed",
            "arguments": '{"cmd": "ls"}', "call_id": "call_00_%04d" % n, "name": "shell"}


def fco(n):
    return {"type": "function_call_output", "call_id": "call_00_%04d" % n, "output": "ok"}


def types_of(items):
    return [i.get("type") for i in items]


def adapt(items):
    """跑一次完整出站转换，返回 (新 input, counts)。"""
    counts = {}
    out = MOD._adapt_input(items, counts)
    return out, counts


class ParallelToolCallsStayContiguous(unittest.TestCase):
    """同一轮的并行调用之间不许出现任何非 tool-call 的项。"""

    def _assert_no_split(self, items):
        seq = types_of(items)
        for i in range(len(seq) - 2):
            if seq[i] in MOD._TOOL_CALL_TYPES and seq[i + 2] in MOD._TOOL_CALL_TYPES:
                self.assertIn(
                    seq[i + 1], MOD._TOOL_CALL_TYPES,
                    "两条 tool call 之间插进了 %r，DeepSeek 会 400 "
                    "No tool output found：%r" % (seq[i + 1], seq))

    def test_parallel_calls_with_real_reasoning(self):
        out, counts = adapt([MSG, REAL_REASONING, fc(1), fc(2), fco(1), fco(2)])
        self.assertEqual(types_of(out), ["message", "reasoning", "function_call",
                                         "function_call", "function_call_output",
                                         "function_call_output"])
        self.assertEqual(counts, {}, "载荷本来就合规，不该有任何改写")
        self._assert_no_split(out)

    def test_parallel_calls_without_reasoning_get_exactly_one(self):
        """客户端没回传 reasoning：只在这一轮开头补一条，轮内不补。"""
        out, counts = adapt([MSG, fc(1), fc(2), fc(3), fco(1), fco(2), fco(3)])
        self.assertEqual(types_of(out),
                         ["message", "reasoning", "function_call", "function_call",
                          "function_call", "function_call_output",
                          "function_call_output", "function_call_output"])
        self.assertEqual(counts.get("reasoning_insert"), 1)
        self._assert_no_split(out)

    def test_client_supplied_reasoning_between_calls_is_dropped(self):
        """客户端自己把 reasoning 夹在两条调用之间（gpt-5.x 历史）也要拆掉。"""
        out, counts = adapt([MSG, REAL_REASONING, fc(1), REAL_REASONING, fc(2),
                             fco(1), fco(2)])
        self.assertEqual(counts.get("reasoning_between_calls_drop"), 1)
        self._assert_no_split(out)

    def test_sequential_rounds_each_get_their_own_reasoning(self):
        """轮与轮之间（output 之后再起一条调用）仍然必须补 —— 少补 DeepSeek 也 400。"""
        out, counts = adapt([MSG, fc(1), fco(1), fc(2), fco(2)])
        self.assertEqual(types_of(out),
                         ["message", "reasoning", "function_call", "function_call_output",
                          "reasoning", "function_call", "function_call_output"])
        self.assertEqual(counts.get("reasoning_insert"), 2)

    def test_custom_tool_call_counts_as_same_round(self):
        """custom_tool_call 与 function_call 相邻同样属于一轮，不许被劈开。"""
        ctc = {"type": "custom_tool_call", "id": "uuid-9", "call_id": "call_00_0009",
               "status": "completed", "name": "apply_patch", "input": "*** Begin Patch"}
        out, counts = adapt([MSG, REAL_REASONING, ctc, fc(1),
                             {"type": "custom_tool_call_output",
                              "call_id": "call_00_0009", "output": "ok"}, fco(1)])
        self.assertIsNone(counts.get("reasoning_insert"))
        self._assert_no_split(out)

    def test_non_deepseek_payload_untouched(self):
        """作用域没变：非 deepseek 部署第一行就 return。"""
        data = {"model": "gpt-5.6-sol", "input": [MSG, fc(1), fc(2), fco(1), fco(2)]}
        self.assertIs(MOD._adapt(data, "test"), data)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class MessageSplitsParallelBlock(unittest.TestCase):
    """2026-08-05 生产：assistant message 夹进并行块同样 400。

    起因：sunqiang 的 cursor key 打 deepseek-v4-flash-responses，3 小时内
    117 次 ``No tool output found``。日志判据显示是**网关自己插的**：36 条报错
    里 35 条 ``reasoning_insert`` 非零（只有 1 条 no-op），其中
    ``reasoning_insert: 1`` 出现 12 次 —— 只插一条就炸。

    根因是 2026-08-04 那版修复漏了一格：它只认「前一项紧邻是 tool call」，
    而 Codex 一轮的真实输出是 ``reasoning, message, fc, fc`` —— message 会夹进
    并行块，判据认不出来，于是把第 2 条并行调用当成新一轮、在它前面补
    reasoning，又一次劈开。

    api.deepseek.com 单变量实测（call_id 全配对，只动并行块中间那一项）::

        [R, fc1, fc2, fo1, fo2]        -> 200
        [R, fc1, MSG, fc2, fo1, fo2]   -> 400 No tool output found   <- 本次真凶
        [R, fc1, R,   fc2, fo1, fo2]   -> 400 No tool output found
        [R, MSG, fc1, fc2, fo1, fo2]   -> 200   （轮开头，无害）
        [MSG, R, fc1, fc2, fo1, fo2]   -> 200

    即 message 与 reasoning 同罪：落在并行块**中间**致命，落在轮**开头**无害。
    这一区别是实测出来的，不是推的 —— 所以下面两组断言必须同时成立。
    """

    R = {"type": "reasoning", "summary": [],
         "content": [{"type": "reasoning_text", "text": "t"}]}
    MSG = {"role": "assistant", "content": "Let me check."}

    @staticmethod
    def _fc(n):
        return {"type": "function_call", "call_id": f"c{n}", "name": "f", "arguments": "{}"}

    @staticmethod
    def _fo(n):
        return {"type": "function_call_output", "call_id": f"c{n}", "output": "ok"}

    def _types(self, items):
        return [i.get("type") or ("msg:" + i.get("role", "")) for i in items]

    def test_message_inside_parallel_block_is_dropped(self):
        """真凶格：并行块中间的 message 必须删掉，且不能反而补 reasoning。"""
        items = [{"role": "user", "content": "q"}, self.R,
                 self._fc(1), self.MSG, self._fc(2), self._fo(1), self._fo(2)]
        counts = {}
        out = MOD._adapt_input(items, counts)
        types = self._types(out)
        self.assertEqual(
            types,
            ["msg:user", "reasoning", "function_call", "function_call",
             "function_call_output", "function_call_output"])
        self.assertEqual(counts.get("message_between_calls_drop"), 1)
        self.assertNotIn("reasoning_insert", counts,
                         "并行块内部绝不能补 reasoning —— 那正是 400 的成因")

    def test_message_at_turn_start_is_kept(self):
        """反向对照：实测 200 的形状不许动。删过头会丢用户可见内容。"""
        items = [{"role": "user", "content": "q"}, self.R, self.MSG,
                 self._fc(1), self._fc(2), self._fo(1), self._fo(2)]
        counts = {}
        out = MOD._adapt_input(items, counts)
        self.assertIn("msg:assistant", self._types(out),
                      "轮开头的 message 实测无害（D/E 格 200），不能删")
        self.assertEqual(counts, {})

    def test_plain_conversation_untouched(self):
        """没有工具调用的纯对话，一个字节都不能动。"""
        items = [{"role": "user", "content": "q"}, self.MSG,
                 {"role": "user", "content": "q2"}, self.MSG]
        counts = {}
        out = MOD._adapt_input(items, counts)
        self.assertEqual(len(out), 4)
        self.assertEqual(counts, {})

    def test_three_way_parallel_with_two_messages(self):
        """三条并行 + 中间两条 message：全删，块保持连续。"""
        items = [{"role": "user", "content": "q"}, self.R,
                 self._fc(1), self.MSG, self._fc(2), self.MSG, self._fc(3),
                 self._fo(1), self._fo(2), self._fo(3)]
        counts = {}
        out = MOD._adapt_input(items, counts)
        self.assertEqual(
            self._types(out),
            ["msg:user", "reasoning", "function_call", "function_call", "function_call",
             "function_call_output", "function_call_output", "function_call_output"])
        self.assertEqual(counts.get("message_between_calls_drop"), 2)

    def test_message_and_reasoning_both_inside(self):
        """双夹心：message 和 reasoning 一起删。"""
        items = [{"role": "user", "content": "q"}, self.R,
                 self._fc(1), self.MSG, self.R, self._fc(2), self._fo(1), self._fo(2)]
        counts = {}
        out = MOD._adapt_input(items, counts)
        self.assertEqual(
            self._types(out),
            ["msg:user", "reasoning", "function_call", "function_call",
             "function_call_output", "function_call_output"])
        self.assertEqual(counts.get("message_between_calls_drop"), 1)
        self.assertEqual(counts.get("reasoning_between_calls_drop"), 1)
