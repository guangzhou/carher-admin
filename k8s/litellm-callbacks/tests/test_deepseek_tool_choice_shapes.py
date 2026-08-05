"""test_deepseek_tool_choice_shapes.py — tool_choice 归一到 DeepSeek 真正接受的形态。

背景（2026-08-05 生产故障）
--------------------------
Codex 打 ``deepseek-v4-flash-responses``，24h 内 27 次 400，且只有这一种错::

    litellm.BadRequestError: OpenAIException -
    {"error":{"message":"Failed to deserialize the JSON body into the target type:
     tool_choice: unknown variant `auto`, expected one of `function`, `web_search`,
     `web_search_2025_08_26`, `custom` at line 1 column 543", ...}}

该 model group 没配 fallback（``No fallback model group found for
original model_group=deepseek-v4-flash-responses``），400 直吐用户。

DeepSeek 的 ``tool_choice`` 是 serde 双形态：``auto`` / ``none`` 只接受**裸字符串**，
写成对象就只走 tagged-enum 分支。错误里 "expected one of" 列的是**对象分支**的
合法 tag，不是全集 —— 照字面构造 ``{"type":"function"}`` 反而撞 Thinking mode 那堵墙。

api.deepseek.com 单变量实测（其余字段全同，2026-08-05）::

    (不带)                                    -> 200  function_call
    "auto"                                    -> 200  function_call
    "none"                                    -> 200  只有 reasoning
    "required"                                -> 400  Thinking mode does not support
    {"type":"auto"}                           -> 400  unknown variant   <- 生产真凶
    {"type":"none"}                           -> 400  unknown variant
    {"type":"tool"} / {"type":"required"}     -> 400  unknown variant
    {"type":"function","name":"X"}            -> 400  Thinking mode does not support
    {"type":"function","function":{"name":X}} -> 400  missing field `name`
    {"type":"allowed_tools",...}              -> 400  unknown variant

穿过 198 proxy 打同一组载荷：字符串 200 / 对象 400，错误文本与生产日志逐字一致
—— 网关原样透传，是客户端发的形状。

本文件钉住：thinking 模式下只有「裸 auto / 裸 none / 不带」三种活形态，任何
强制类形状都必须降级为不带，而不是翻译成另一种强制形状。用法::

    python3 -m unittest test_deepseek_tool_choice_shapes -v
"""
import importlib.util
import os
import sys
import types
import unittest

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "deepseek_responses_adapt.py")


def _load_module():
    """脱离 litellm 依赖加载被测模块（只 stub CustomLogger 基类）。"""
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

    path = os.environ.get("DEEPSEEK_ADAPT_PATH", DEFAULT_PATH)
    spec = importlib.util.spec_from_file_location("deepseek_responses_adapt_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MOD = _load_module()

_ABSENT = "<<absent>>"


def _run(tool_choice):
    """跑一遍 _adapt，返回 (tool_choice 结果, counts)。缺字段返回 _ABSENT。"""
    data = {"model": "deepseek-v4-flash", "input": [{"role": "user", "content": "hi"}]}
    if tool_choice is not _ABSENT:
        data["tool_choice"] = tool_choice
    out = MOD._adapt(data, "test")
    return out.get("tool_choice", _ABSENT), out


class ToolChoiceShapes(unittest.TestCase):
    def test_bare_auto_and_none_pass_through(self):
        """实测 200 的两种形态，一个字都不许改。"""
        for value in ("auto", "none"):
            with self.subTest(tool_choice=value):
                got, _ = _run(value)
                self.assertEqual(got, value)

    def test_wrapped_auto_is_unwrapped_to_bare_string(self):
        """生产真凶：{"type":"auto"} -> "auto"。"""
        got, _ = _run({"type": "auto"})
        self.assertEqual(got, "auto")

    def test_wrapped_none_is_unwrapped_to_bare_string(self):
        got, _ = _run({"type": "none"})
        self.assertEqual(got, "none")

    def test_forcing_shapes_are_dropped_not_translated(self):
        """thinking 模式没有「强制用工具」这档，只能删。

        翻译成另一种强制形状同样 400，只是换个错误文本 —— 见模块头形状表
        F/G/I/J/K 五格。
        """
        forcing = [
            "required",
            {"type": "required"},
            {"type": "tool"},
            {"type": "function", "name": "get_time"},
            {"type": "function", "function": {"name": "get_time"}},
            {"type": "allowed_tools", "mode": "auto",
             "tools": [{"type": "function", "name": "get_time"}]},
        ]
        for value in forcing:
            with self.subTest(tool_choice=value):
                got, _ = _run(value)
                self.assertIs(got, _ABSENT,
                              "强制类 tool_choice 必须整个删掉，不能翻译成别的强制形状")

    def test_native_builtin_tags_pass_through(self):
        """web_search / custom 是 DeepSeek 对象分支里真正认的 tag，别动。"""
        for value in ({"type": "web_search"},
                      {"type": "web_search_2025_08_26"},
                      {"type": "custom", "name": "apply_patch"}):
            with self.subTest(tool_choice=value):
                got, _ = _run(value)
                self.assertEqual(got, value)

    def test_absent_stays_absent(self):
        """不带就是不带 —— 不许好心补一个 "auto" 进去。"""
        got, _ = _run(_ABSENT)
        self.assertIs(got, _ABSENT)

    def test_non_deepseek_model_is_untouched(self):
        """作用域：非 deepseek 部署一个字节都不能改。

        对照组 —— 同一个坏形状打 gpt，必须原样留着，证明门控没漏。
        """
        data = {"model": "chatgpt-gpt-5.6-sol",
                "input": [{"role": "user", "content": "hi"}],
                "tool_choice": {"type": "auto"}}
        out = MOD._adapt(data, "test")
        self.assertEqual(out["tool_choice"], {"type": "auto"})

    def test_original_payload_not_mutated(self):
        """_adapt 不能就地改调用方的 dict（fallback 链上会复用同一份）。"""
        original = {"type": "auto"}
        data = {"model": "deepseek-v4-flash",
                "input": [{"role": "user", "content": "hi"}],
                "tool_choice": original}
        MOD._adapt(data, "test")
        self.assertEqual(data["tool_choice"], {"type": "auto"})
        self.assertEqual(original, {"type": "auto"})


class CountsAreObservable(unittest.TestCase):
    """counts 是排查时唯一能区分「没触发」和「触发了没事干」的信号。"""

    def test_unwrap_is_counted(self):
        _, out = _run({"type": "auto"})
        # counts 只进日志不进 payload，这里靠「payload 变了」反推它非空
        self.assertEqual(out.get("tool_choice"), "auto")

    def test_no_op_returns_same_object(self):
        data = {"model": "deepseek-v4-flash",
                "input": [{"role": "user", "content": "hi"}],
                "tool_choice": "auto"}
        out = MOD._adapt(data, "test")
        self.assertIs(out, data, "无事可做时必须原样返回，别造新对象")


if __name__ == "__main__":
    unittest.main(verbosity=2)
