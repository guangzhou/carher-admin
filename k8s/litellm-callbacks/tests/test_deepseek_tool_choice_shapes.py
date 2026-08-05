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

_FN_TOOL = {"type": "function", "name": "get_time",
            "parameters": {"type": "object", "properties": {}}}
_WS_TOOL = {"type": "web_search"}


def _run(tool_choice, tools=None):
    """跑一遍 _adapt，返回 (tool_choice 结果, 完整输出)。缺字段返回 _ABSENT。"""
    data = {"model": "deepseek-v4-flash", "input": [{"role": "user", "content": "hi"}],
            "tools": list(tools) if tools is not None else [_FN_TOOL]}
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
        """thinking 模式对 function / custom 的强制一律不支持，只能删。

        翻译成另一种强制形状同样 400，只是换个错误文本 —— 见模块头形状表
        F/G/I/J/K/W1/W2 七格。
        """
        forcing = [
            "required",
            {"type": "required"},
            {"type": "tool"},
            {"type": "function", "name": "get_time"},
            {"type": "function", "function": {"name": "get_time"}},
            {"type": "allowed_tools", "mode": "auto",
             "tools": [{"type": "function", "name": "get_time"}]},
            # W1/W2：custom 出现在错误文本的 "expected one of" 里，但实测是死的。
            # 第一版补丁把它当"内建 tag"放行，Codex 真载荷当场 400。
            {"type": "custom", "name": "shell"},
            {"type": "custom", "name": "apply_patch"},
        ]
        for value in forcing:
            with self.subTest(tool_choice=value):
                got, _ = _run(value)
                self.assertIs(got, _ABSENT,
                              "强制类 tool_choice 必须整个删掉，不能翻译成别的强制形状")

    def test_web_search_forcing_kept_only_when_tool_present(self):
        """W3/W4/W5：web_search 是唯一活着的强制，但要 tools 里真有这工具。

        没依托时实测 400 "no web_search tool was specified in the 'tools'
        parameter" —— 所以不能无条件放行。
        """
        for tag in ("web_search", "web_search_2025_08_26"):
            with self.subTest(tag=tag, tools="with ws"):
                got, _ = _run({"type": tag}, tools=[_FN_TOOL, _WS_TOOL])
                self.assertEqual(got, {"type": tag}, "有 web_search 工具时必须保留强制")
            with self.subTest(tag=tag, tools="without ws"):
                got, _ = _run({"type": tag}, tools=[_FN_TOOL])
                self.assertIs(got, _ABSENT, "没有 web_search 工具时必须删掉，否则 400")

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


class CodexRealPayload(unittest.TestCase):
    """真 Codex 载荷形状 —— 第一版补丁就是在这一格上漏的。

    Codex 声明 custom tool（shell / ApplyPatch），且 tool_choice 发对象形态。
    2026-08-05 穿 198 proxy 实测：这一格当时 400，合成的 function-only 载荷
    全绿 —— 只测自己造的形状会骗过去。
    """

    def _codex_payload(self, tool_choice):
        return {
            "model": "deepseek-v4-flash",
            "instructions": "You are a coding agent.",
            "input": [{"role": "user", "content": "check the time"}],
            "tools": [
                {"type": "custom", "name": "shell", "description": "run a shell command"},
                {"type": "custom", "name": "ApplyPatch", "description": "apply a patch"},
                _FN_TOOL,
            ],
            "tool_choice": tool_choice,
            "parallel_tool_calls": True,
        }

    def test_codex_custom_tool_choice_is_dropped(self):
        out = MOD._adapt(self._codex_payload({"type": "custom", "name": "shell"}), "test")
        self.assertNotIn("tool_choice", out,
                         "Codex 的 tool_choice={'type':'custom',...} 必须删掉")

    def test_codex_obj_auto_still_unwrapped_alongside_custom_tools(self):
        """custom tool 改写与 tool_choice 归一必须同时生效，互不干扰。"""
        out = MOD._adapt(self._codex_payload({"type": "auto"}), "test")
        self.assertEqual(out["tool_choice"], "auto")
        names = [t.get("name") for t in out["tools"]]
        self.assertIn("apply_patch", names, "ApplyPatch 应已被改名（既有行为不能回归）")


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


class OfficialCodexCatalogShape(unittest.TestCase):
    """官方 models.json 语义下 Codex 发的载荷，网关必须一个字节都不碰。

    官方接入（api-docs.deepseek.com/quick_start/agent_integrations/codex/）靠客户端
    模型目录声明形状，其中 ``apply_patch_tool_type: "freeform"`` 决定 Codex 发
    ``{"type":"custom","name":"apply_patch",...}``，配套 output 是
    ``custom_tool_call_output``。这一形状 DeepSeek 原生就吃，任何"适配"都是帮倒忙。

    2026-08-05 A/B 实测（直连 api.deepseek.com 作对照组，同一份载荷）：
    turn1 两边都 completed；turn2 直连原样保留，**穿 198 时
    counts={'custom_out_to_function': 1}** —— call 是 custom_tool_call、output
    却被转成 function_call_output，一对调用被拆成两种类型。
    """

    OFFICIAL_APPLY_PATCH = {
        "type": "custom", "name": "apply_patch",
        "description": "Use the `apply_patch` tool to edit files.",
        "format": {"type": "grammar", "syntax": "lark", "definition": "start: /(.|\\n)*/"},
    }
    OFFICIAL_SHELL = {
        "type": "function", "name": "shell", "description": "Runs a shell command.",
        "parameters": {"type": "object",
                       "properties": {"command": {"type": "array", "items": {"type": "string"}}},
                       "required": ["command"]},
        "strict": False,
    }

    def _payload(self, extra_input=()):
        return {
            "model": "deepseek-v4-flash",
            "instructions": "You are Codex, an agent based on GPT-5.",
            "input": [{"role": "user", "content": "create hello.txt"}] + list(extra_input),
            "tools": [self.OFFICIAL_APPLY_PATCH, self.OFFICIAL_SHELL],
            "parallel_tool_calls": True,
        }

    def test_official_apply_patch_tool_untouched(self):
        """freeform grammar format 必须原样保留，别退化成 function。"""
        out = MOD._adapt(self._payload(), "test")
        self.assertEqual(out["tools"][0], self.OFFICIAL_APPLY_PATCH)
        self.assertEqual(out["tools"][1], self.OFFICIAL_SHELL)

    def test_official_custom_output_stays_custom(self):
        """apply_patch 的 call 放行了，它的 output 就不能被转成 function 形态。"""
        turn = [
            {"type": "reasoning", "summary": [],
             "content": [{"type": "reasoning_text", "text": "thinking"}]},
            {"type": "custom_tool_call", "call_id": "call_1",
             "name": "apply_patch", "input": "*** Begin Patch"},
            {"type": "custom_tool_call_output", "call_id": "call_1", "output": "Done"},
        ]
        out = MOD._adapt(self._payload(turn), "test")
        types = [i.get("type") for i in out["input"]]
        self.assertIn("custom_tool_call", types)
        self.assertIn("custom_tool_call_output", types,
                      "官方形状的 custom output 被转成 function_call_output 就配不上它的 call")
        self.assertNotIn("function_call_output", types)

    def test_non_official_custom_call_and_output_convert_together(self):
        """反向对照：非 apply_patch 的 custom（兜底场景）call 和 output 必须一起转。

        单独放行任何一边都会造成类型不配对 —— 这一格钉住"成对转换"。
        """
        turn = [
            {"type": "reasoning", "summary": [],
             "content": [{"type": "reasoning_text", "text": "thinking"}]},
            {"type": "custom_tool_call", "call_id": "call_2",
             "name": "shell", "input": "ls"},
            {"type": "custom_tool_call_output", "call_id": "call_2", "output": "ok"},
        ]
        out = MOD._adapt(self._payload(turn), "test")
        types = [i.get("type") for i in out["input"]]
        self.assertIn("function_call", types)
        self.assertIn("function_call_output", types)
        self.assertNotIn("custom_tool_call", types)
        self.assertNotIn("custom_tool_call_output", types)

    def test_official_payload_needs_no_conversion_at_all(self):
        """整体判据：纯官方形状进来，_adapt 必须原样返回同一个对象（no-op）。"""
        turn = [
            {"type": "reasoning", "summary": [],
             "content": [{"type": "reasoning_text", "text": "thinking"}]},
            {"type": "custom_tool_call", "call_id": "call_1",
             "name": "apply_patch", "input": "*** Begin Patch"},
            {"type": "custom_tool_call_output", "call_id": "call_1", "output": "Done"},
        ]
        data = self._payload(turn)
        out = MOD._adapt(data, "test")
        self.assertIs(out, data,
                      "官方形状不该触发任何转换；counts 非空说明网关在帮倒忙")
