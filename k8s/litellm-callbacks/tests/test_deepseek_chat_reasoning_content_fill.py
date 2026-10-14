"""test_deepseek_chat_reasoning_content_fill.py — chat 路径给工具轮补 reasoning_content。

2026-10-14 生产：cursor-wangwenbin-cfzh 经 VS Code 打 deepseek-v4.1-flash
（custom_openai/deepseek-flash @ api.deepseek.com），带工具历史的请求 76 次 400::

    The `reasoning_content` in the thinking mode must be passed back to the API.

198 实测（tools 定义+历史）：每条带 tool_calls 的 assistant 都要有 reasoning_content，
两轮里漏任意一轮都 400；值为 " " 或 "" 都 200；普通 assistant 回复不需要。

    python3 -m unittest test_deepseek_chat_reasoning_content_fill -v
"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_deepseek_reasoning_insert_pairing import MOD  # noqa: E402

OFFICIAL = {"model": "custom_openai/deepseek-flash", "api_base": "https://api.deepseek.com/v1"}


def tc(i):
    return {"id": "call_%d" % i, "type": "function", "function": {"name": "w", "arguments": "{}"}}


def asst(i, **kw):
    m = {"role": "assistant", "content": "", "tool_calls": [tc(i)]}
    m.update(kw)
    return m


def tool(i):
    return {"role": "tool", "tool_call_id": "call_%d" % i, "content": "{}"}


U = {"role": "user", "content": "weather?"}


class _CT:
    def __init__(self, v):
        self.value = v


def run(kwargs, call_type="acompletion"):
    return asyncio.run(MOD.deepseek_responses_adapt.async_pre_call_deployment_hook(kwargs, _CT(call_type)))


class ChatReasoningContentFill(unittest.TestCase):
    def test_every_tool_round_filled(self):
        msgs = [U, asst(1), tool(1), asst(2), tool(2)]
        out = run(dict(OFFICIAL, messages=msgs))
        rcs = [m.get("reasoning_content") for m in out["messages"] if m.get("role") == "assistant"]
        self.assertEqual(rcs, [" ", " "])
        self.assertNotIn("reasoning_content", msgs[1], "must not mutate caller's messages")

    def test_existing_reasoning_kept(self):
        out = run(dict(OFFICIAL, messages=[U, asst(1, reasoning_content="real"), tool(1)]))
        self.assertEqual(out["messages"][1]["reasoning_content"], "real")

    def test_empty_string_counts_as_present(self):
        kw = dict(OFFICIAL, messages=[U, asst(1, reasoning_content=""), tool(1)])
        self.assertIs(run(kw), kw)

    def test_restore_from_provider_specific_fields(self):
        m = asst(1, provider_specific_fields={"reasoning_content": "saved"})
        out = run(dict(OFFICIAL, messages=[U, m, tool(1)]))
        self.assertEqual(out["messages"][1]["reasoning_content"], "saved")

    def test_plain_assistant_untouched(self):
        kw = dict(OFFICIAL, messages=[U, {"role": "assistant", "content": "hi"}, {"role": "user", "content": "x"}])
        self.assertIs(run(kw), kw)

    def test_non_official_deepseek_untouched(self):
        for base in ("http://36.151.241.10:8000/v1", "https://aigateway.edgecloudapp.com/v1/x", None):
            kw = {"model": "custom_openai/deepseek-v4-flash", "api_base": base, "messages": [U, asst(1), tool(1)]}
            self.assertIs(run(kw), kw, base)

    def test_non_deepseek_untouched(self):
        kw = {"model": "openai/gpt-5.5", "api_base": "https://api.deepseek.com/v1", "messages": [U, asst(1), tool(1)]}
        self.assertIs(run(kw), kw)

    def test_responses_path_not_routed_to_chat_fill(self):
        kw = dict(OFFICIAL, messages=[U, asst(1), tool(1)], input=[])
        out = run(kw, "aresponses")
        self.assertNotIn("reasoning_content", out["messages"][1])

    def test_idempotent(self):
        once = run(dict(OFFICIAL, messages=[U, asst(1), tool(1)]))
        self.assertIs(run(once), once)


if __name__ == "__main__":
    unittest.main()
