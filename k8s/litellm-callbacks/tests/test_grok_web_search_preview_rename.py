"""test_grok_web_search_preview_rename.py — sub2api(x.ai) 的 Responses 请求里
``web_search_preview`` → ``web_search``（x.ai 静默忽略 preview，0 次搜索）。

用法::

    python3 -m unittest test_grok_web_search_preview_rename -v
"""
import asyncio
import importlib.util
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SUB2API = "http://sub2api.litellm-dev.svc.cluster.local:8080/v1"
KIRO = "http://10.43.109.5:8990"


def _stub_litellm():
    if "litellm.integrations.custom_logger" in sys.modules:
        return
    litellm = sys.modules.get("litellm") or types.ModuleType("litellm")
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


def _load():
    _stub_litellm()
    path = os.path.join(HERE, "..", "grok_web_search_preview_rename.py")
    spec = importlib.util.spec_from_file_location("grok_web_search_preview_rename", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load()


def run(kwargs):
    return asyncio.run(M.grok_web_search_preview_rename.async_pre_call_deployment_hook(kwargs, "aresponses"))


class RenameTools(unittest.TestCase):
    def test_cursor_shape_is_renamed(self):
        # 线上 Cursor WebSearch 原样形状（10-15 16:45 cursor-liuguoxian-std）
        kw = {"api_base": SUB2API, "model": "openai/grok-4.20", "tools": [{"type": "web_search_preview"}]}
        run(kw)
        self.assertEqual(kw["tools"], [{"type": "web_search"}])

    def test_preview_dated_variant_and_extra_fields_dropped(self):
        kw = {"api_base": SUB2API, "tools": [
            {"type": "web_search_preview_2025_03_11", "search_context_size": "high", "user_location": {"type": "approximate"}}]}
        run(kw)
        self.assertEqual(kw["tools"], [{"type": "web_search"}])

    def test_function_tools_untouched_and_order_kept(self):
        fn = {"type": "function", "name": "exec_command", "parameters": {"type": "object"}}
        kw = {"api_base": SUB2API, "tools": [fn, {"type": "web_search_preview"}, {"type": "function", "name": "b"}]}
        run(kw)
        self.assertEqual(kw["tools"], [fn, {"type": "web_search"}, {"type": "function", "name": "b"}])
        self.assertIs(kw["tools"][0], fn)

    def test_dedup_when_native_already_present(self):
        kw = {"api_base": SUB2API, "tools": [{"type": "web_search"}, {"type": "web_search_preview"}]}
        run(kw)
        self.assertEqual(kw["tools"], [{"type": "web_search"}])

    def test_native_web_search_alone_unchanged(self):
        tools = [{"type": "web_search", "filters": {"allowed_domains": ["a.com"]}}]
        kw = {"api_base": SUB2API, "tools": tools}
        run(kw)
        self.assertEqual(kw["tools"], [{"type": "web_search", "filters": {"allowed_domains": ["a.com"]}}])

    def test_gate_non_sub2api_untouched(self):
        # 阴性对照：kiro / 官方 OpenAI 等别的上游一律不动
        for base in (KIRO, "https://api.openai.com/v1", ""):
            kw = {"api_base": base, "tools": [{"type": "web_search_preview"}]}
            run(kw)
            self.assertEqual(kw["tools"], [{"type": "web_search_preview"}], base)

    def test_api_base_in_litellm_params(self):
        kw = {"litellm_params": {"api_base": SUB2API}, "tools": [{"type": "web_search_preview"}]}
        run(kw)
        self.assertEqual(kw["tools"], [{"type": "web_search"}])

    def test_no_tools_or_bad_shapes_never_raise(self):
        for kw in ({"api_base": SUB2API}, {"api_base": SUB2API, "tools": None},
                   {"api_base": SUB2API, "tools": "web_search_preview"},
                   {"api_base": SUB2API, "tools": [None, 3, "x"]}):
            out = run(dict(kw))
            self.assertIsInstance(out, dict)

    def test_tool_choice_named_web_search_becomes_required(self):
        # 线上原样形状（10-15 17:12 cursor-heyinchuan-qnv5，x.ai 422 ModelToolChoice）
        tools = [{"type": "web_search", "filters": {"allowed_domains": ["code.claude.com", "github.com"]}}]
        kw = {"api_base": SUB2API, "tools": tools, "tool_choice": {"type": "web_search"}}
        run(kw)
        self.assertEqual(kw["tool_choice"], "required")
        self.assertEqual(kw["tools"], [{"type": "web_search", "filters": {"allowed_domains": ["code.claude.com", "github.com"]}}])

    def test_tool_choice_named_preview_becomes_required_and_tool_renamed(self):
        kw = {"api_base": SUB2API, "tools": [{"type": "web_search_preview"}],
              "tool_choice": {"type": "web_search_preview"}}
        run(kw)
        self.assertEqual(kw["tool_choice"], "required")
        self.assertEqual(kw["tools"], [{"type": "web_search"}])

    def test_other_tool_choice_untouched(self):
        fn_choice = {"type": "function", "name": "exec_command"}
        for tc in ("auto", "required", "none", fn_choice, None):
            kw = {"api_base": SUB2API, "tools": [{"type": "web_search"}], "tool_choice": tc}
            run(kw)
            self.assertEqual(kw["tool_choice"], tc)

    def test_tool_choice_gate_non_sub2api_untouched(self):
        kw = {"api_base": "https://api.openai.com/v1", "tools": [{"type": "web_search"}],
              "tool_choice": {"type": "web_search"}}
        run(kw)
        self.assertEqual(kw["tool_choice"], {"type": "web_search"})

    def test_returns_same_kwargs_object(self):
        kw = {"api_base": SUB2API, "tools": [{"type": "web_search_preview"}]}
        self.assertIs(run(kw), kw)


if __name__ == "__main__":
    unittest.main()
