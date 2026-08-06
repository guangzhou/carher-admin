"""test_deepseek_additional_tools_hoist.py — Codex responses-lite 的工具必须提到顶层。

背景（2026-08-06 生产，**这条是真凶**）
--------------------------------------
用户本地 Codex 打 ``deepseek-v4-flash-responses``，输出里出现裸文本::

    <｜｜DSML｜｜tool_calls>
    <｜｜DSML｜｜invoke name="exec">

抓包判据 —— 同一时段 39 个 deepseek 请求，38 个正常、只有他那条异常::

    正常:  tools=['function/execute_shell_command', ...]   tool_choice=None
    异常:  tools=[]   tool_choice='auto'   seq_in=['additional_tools', ...]
           DSAT itemkeys=['role','tools','type'] n=3
                names=['custom/exec','function/wait','function/request_user_input']

即 Codex 的 "responses lite" 线路把工具塞在 ``input`` 里，顶层 ``tools`` 是空的。
LiteLLM 1.90.2 全库 grep ``additional_tools`` = 0 处，原样透传；DeepSeek 也不认，
于是**收到零个工具**。

单变量实测（只改 tools 是否为空，其余全同，真打 api.deepseek.com）::

    tools=[]        -> 3 次里第 2 次吐出 <｜｜DSML｜｜invoke name="list_files">
    tools=[真工具]  -> 5/5 干净，全部结构化 function_call

「模型编造工具名」也是这么来的 —— 没有声明可依据，只能瞎编。

对齐上游 PR #33228（bedrock_mantle 侧同一问题）。上游**只在 bedrock_mantle
做了**，openai provider 没有，所以升级 LiteLLM 修不了这条。

用法::

    python3 -m unittest test_deepseek_additional_tools_hoist -v
"""
import importlib.util
import os
import sys
import types
import unittest

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "deepseek_responses_adapt.py")


def _load_module():
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
    spec = importlib.util.spec_from_file_location("dsa_hoist_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MOD = _load_module()

# 真实抓到的形状
REAL_AT_ITEM = {
    "type": "additional_tools", "role": "developer",
    "tools": [
        {"type": "custom", "name": "exec",
         "format": {"type": "grammar", "syntax": "lark", "definition": "start: /.*/"}},
        {"type": "function", "name": "wait",
         "parameters": {"type": "object", "properties": {}}},
        {"type": "function", "name": "request_user_input",
         "parameters": {"type": "object", "properties": {}}},
    ],
}
USER_MSG = {"type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "看看本地的代码结构"}]}


def _payload(input_items, tools=None):
    d = {"model": "deepseek-v4-flash", "input": list(input_items), "tool_choice": "auto"}
    if tools is not None:
        d["tools"] = tools
    return d


class HoistAdditionalTools(unittest.TestCase):
    def test_real_shape_hoisted_and_item_removed(self):
        """真凶格：工具提到顶层，additional_tools 项从 input 删掉。"""
        out = MOD._adapt(_payload([REAL_AT_ITEM, USER_MSG], tools=[]), "test")
        names = [t.get("name") for t in out["tools"]]
        self.assertEqual(names, ["exec", "wait", "request_user_input"])
        self.assertNotIn("additional_tools",
                         [i.get("type") for i in out["input"]],
                         "DeepSeek 不认这个 input item type，必须删掉")
        self.assertEqual(len(out["input"]), 1)

    def test_tools_no_longer_empty(self):
        """核心断言：转换后 tools 非空 —— 空 tools 正是文本泄漏的成因。"""
        out = MOD._adapt(_payload([REAL_AT_ITEM, USER_MSG], tools=[]), "test")
        self.assertTrue(out["tools"], "tools 为空时模型会把工具调用写成文本")

    def test_namespace_tools_flattened(self):
        """namespace 是嵌套容器，不摊平就整包丢给 DeepSeek（上游 PR 同款）。"""
        item = {"type": "additional_tools", "role": "developer", "tools": [
            {"type": "function", "name": "wait", "parameters": {"type": "object"}},
            {"type": "namespace", "name": "collaboration", "tools": [
                {"type": "function", "name": "spawn_agent",
                 "parameters": {"type": "object"}}]},
        ]}
        out = MOD._adapt(_payload([item, USER_MSG], tools=[]), "test")
        names = [t.get("name") for t in out["tools"]]
        self.assertIn("spawn_agent", names)
        self.assertNotIn("collaboration", names)
        self.assertNotIn("namespace", [t.get("type") for t in out["tools"]])

    def test_merges_with_existing_top_level_tools(self):
        """两种形态混发时合并，且不重复。"""
        existing = [{"type": "function", "name": "ls",
                     "parameters": {"type": "object", "properties": {}}}]
        out = MOD._adapt(_payload([REAL_AT_ITEM, USER_MSG], tools=existing), "test")
        names = [t.get("name") for t in out["tools"]]
        self.assertEqual(names[0], "ls")
        self.assertIn("exec", names)

    def test_duplicate_name_not_added_twice(self):
        existing = [{"type": "function", "name": "wait",
                     "parameters": {"type": "object", "properties": {}}}]
        out = MOD._adapt(_payload([REAL_AT_ITEM, USER_MSG], tools=existing), "test")
        self.assertEqual([t.get("name") for t in out["tools"]].count("wait"), 1)

    def test_empty_additional_tools_item_still_removed(self):
        """空的 additional_tools 项也要删 —— DeepSeek 不认这个 item type。"""
        item = {"type": "additional_tools", "role": "developer", "tools": []}
        out = MOD._adapt(_payload([item, USER_MSG]), "test")
        self.assertNotIn("additional_tools", [i.get("type") for i in out["input"]])

    def test_no_additional_tools_is_noop(self):
        """没有该项时一个字节都不动（38/39 的常规请求走这条）。"""
        data = _payload([USER_MSG], tools=[{"type": "function", "name": "ls",
                                            "parameters": {"type": "object"}}])
        out = MOD._adapt(data, "test")
        self.assertIs(out, data)

    def test_non_deepseek_untouched(self):
        """作用域对照组：打 gpt 时不能动。"""
        data = {"model": "chatgpt-gpt-5.6-sol",
                "input": [REAL_AT_ITEM, USER_MSG], "tools": []}
        out = MOD._adapt(data, "test")
        self.assertIn("additional_tools", [i.get("type") for i in out["input"]])
        self.assertEqual(out["tools"], [])

    def test_original_payload_not_mutated(self):
        """fallback 链上会复用同一份，不能就地改。"""
        items = [REAL_AT_ITEM, USER_MSG]
        data = _payload(items, tools=[])
        MOD._adapt(data, "test")
        self.assertEqual(len(data["input"]), 2)
        self.assertEqual(data["tools"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
