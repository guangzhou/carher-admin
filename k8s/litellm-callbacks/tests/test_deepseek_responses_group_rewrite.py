"""test_deepseek_responses_group_rewrite.py — /v1/responses 裸名 -> -responses 组改写。

背景（2026-08-14）
------------------
目标是把 DeepSeek 官方 Codex 接入的 ``~/.codex/models.json`` 挪到 litellm 侧：
无 catalog 的 Codex 用户直接选裸名 ``deepseek-v4-flash``，但该产品名是
mode:chat 组 —— /v1/responses 进来要走 responses→chat bridge，Codex 降级
元数据的工具形状会 400（codex 0.147 实测）或挂死（namespace 工具）。
同一客户端打 ``deepseek-v4-flash-responses``（官方原生透传）E2E 全通。

所以 ``deepseek_responses_adapt.async_pre_call_hook`` 在 responses 调用入口把
裸名改写到 -responses 组。本测试钉住：

1. responses 调用 + 裸名 -> 改写生效（flash / pro 各一条）
2. chat 调用（acompletion）不改写 —— Cursor / her 的 chat 流量不能被动
3. 已是 -responses 名 / 非 deepseek 名不改写
4. ``DEEPSEEK_RESPONSES_REWRITE=off`` 一键停用
5. 改写不就地污染调用方的 dict（litellm 内部可能复用原对象）

用法::

    python3 -m unittest test_deepseek_responses_group_rewrite -v
"""
import asyncio
import importlib.util
import os
import sys
import types
import unittest

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "deepseek_responses_adapt.py")


def _install_litellm_stubs():
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


def _load_module(name="dsra_rewrite_under_test"):
    _install_litellm_stubs()
    spec = importlib.util.spec_from_file_location(name, DEFAULT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class RewriteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("DEEPSEEK_RESPONSES_REWRITE", None)
        cls.mod = _load_module()
        cls.hook = cls.mod.DeepSeekResponsesAdapt()

    def _pre_call(self, data, call_type="aresponses"):
        return _run(self.hook.async_pre_call_hook(None, None, data, call_type))

    def test_flash_bare_name_rewritten_on_responses(self):
        out = self._pre_call({"model": "deepseek-v4-flash", "input": []})
        self.assertEqual(out["model"], "deepseek-v4-flash-responses")

    def test_pro_bare_name_rewritten_on_responses(self):
        out = self._pre_call({"model": "deepseek-v4-pro", "input": []})
        self.assertEqual(out["model"], "deepseek-v4-pro-responses")

    def test_chat_call_type_untouched(self):
        data = {"model": "deepseek-v4-flash", "messages": []}
        out = self._pre_call(data, call_type="acompletion")
        self.assertEqual(out["model"], "deepseek-v4-flash")

    def test_responses_group_name_untouched(self):
        out = self._pre_call({"model": "deepseek-v4-flash-responses", "input": []})
        self.assertEqual(out["model"], "deepseek-v4-flash-responses")

    def test_non_deepseek_untouched(self):
        data = {"model": "gpt-5.6-sol", "input": []}
        out = self._pre_call(data)
        self.assertEqual(out["model"], "gpt-5.6-sol")

    def test_caller_dict_not_mutated_in_place(self):
        data = {"model": "deepseek-v4-flash", "input": []}
        out = self._pre_call(data)
        self.assertEqual(data["model"], "deepseek-v4-flash")
        self.assertEqual(out["model"], "deepseek-v4-flash-responses")

    def test_kill_switch_off(self):
        os.environ["DEEPSEEK_RESPONSES_REWRITE"] = "off"
        try:
            mod = _load_module("dsra_rewrite_killswitch")
            hook = mod.DeepSeekResponsesAdapt()
            out = _run(hook.async_pre_call_hook(
                None, None, {"model": "deepseek-v4-flash", "input": []}, "aresponses"))
            self.assertEqual(out["model"], "deepseek-v4-flash")
        finally:
            os.environ.pop("DEEPSEEK_RESPONSES_REWRITE", None)


if __name__ == "__main__":
    unittest.main()
