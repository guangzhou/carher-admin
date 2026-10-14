"""Regression tests for ``grok_agent_message_flatten.py``.

Background (2026-10-14, 198 litellm-product → sub2api → x.ai)
--------------------------------------------------------------
Codex Desktop 0.162 sends sub-agent replies as ``{"type": "agent_message", ...}``
items in Responses ``input``. x.ai rejects that item type with 422 on every
account; sub2api fails over across all 5 legs, then LiteLLM retries 6 times.
The A/B probe (``work-grok-422/probe_agent_message.py``) showed plain=200,
+agent_message=502 (with or without encrypted_content), flattened=200.

Run
---

    cd k8s/litellm-callbacks/tests
    python3 -m unittest test_grok_agent_message_flatten -v
"""

from __future__ import annotations

import asyncio
import copy
import enum
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from typing import Any


def _install_litellm_stubs() -> None:
    s = types.ModuleType("litellm")
    sys.modules["litellm"] = s
    integ = types.ModuleType("litellm.integrations")
    cl = types.ModuleType("litellm.integrations.custom_logger")

    class _CL:
        def __init__(self) -> None: ...

    cl.CustomLogger = _CL
    sys.modules["litellm.integrations"] = integ
    sys.modules["litellm.integrations.custom_logger"] = cl


_install_litellm_stubs()

_HOOK_PATH = Path(__file__).resolve().parent.parent / "grok_agent_message_flatten.py"
_spec = importlib.util.spec_from_file_location("grok_agent_message_flatten", _HOOK_PATH)
_mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_mod)

SUB2API = "http://sub2api.litellm-dev.svc.cluster.local:8080/v1"
DEEPSEEK = "https://openrouter.ai/api/v1"


class CallTypes(str, enum.Enum):  # 与 litellm 同形：str() 不等于 value
    aresponses = "aresponses"
    acompletion = "acompletion"


def _user(text: str) -> dict:
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def _agent(text: str | None, enc: bool = True) -> dict:
    content = []
    if text is not None:
        content.append({"type": "input_text", "text": text})
    if enc:
        content.append({"type": "encrypted_content", "encrypted_content": "gAAAAAB" + "A" * 40})
    return {"id": "amsg_x", "type": "agent_message", "author": "/root", "recipient": "/root", "content": content}


def _run(kwargs: dict, call_type: Any = CallTypes.aresponses) -> dict:
    hook = _mod.grok_agent_message_flatten
    return asyncio.run(hook.async_pre_call_deployment_hook(kwargs, call_type))


class TestFlatten(unittest.TestCase):
    def test_agent_message_becomes_user_message_in_place(self) -> None:
        out = _run({"api_base": SUB2API, "input": [_user("a"), _agent("sub done"), _user("b")]})
        self.assertEqual([i["type"] for i in out["input"]], ["message", "message", "message"])
        mid = out["input"][1]
        self.assertEqual(mid["role"], "user")
        self.assertEqual(mid["content"][0]["text"], "[agent message from /root]\nsub done")

    def test_encrypted_content_is_not_carried_over(self) -> None:
        out = _run({"api_base": SUB2API, "input": [_agent("x", enc=True)]})
        self.assertNotIn("encrypted_content", str(out["input"]))

    def test_agent_message_without_text_is_dropped(self) -> None:
        out = _run({"api_base": SUB2API, "input": [_user("a"), _agent(None, enc=True)]})
        self.assertEqual(len(out["input"]), 1)

    def test_other_items_untouched_byte_for_byte(self) -> None:
        others = [
            _user("a"),
            {"type": "reasoning", "encrypted_content": "keep-me", "summary": []},
            {"type": "function_call", "call_id": "c1", "name": "f", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": "r"},
            {"type": "compaction", "encrypted_content": "keep-me-too"},
        ]
        kw = {"api_base": SUB2API, "input": copy.deepcopy(others) + [_agent("z")]}
        out = _run(kw)
        self.assertEqual(out["input"][:-1], others)


class TestGates(unittest.TestCase):
    def test_non_sub2api_deployment_untouched(self) -> None:
        # 兜底到 deepseek 时 deepseek_responses_adapt 负责，本钩子不能改它
        kw = {"api_base": DEEPSEEK, "input": [_agent("x")]}
        out = _run(kw)
        self.assertEqual(out["input"][0]["type"], "agent_message")

    def test_api_base_in_nested_litellm_params(self) -> None:
        out = _run({"litellm_params": {"api_base": SUB2API}, "input": [_agent("x")]})
        self.assertEqual(out["input"][0]["type"], "message")

    def test_non_responses_call_untouched(self) -> None:
        out = _run({"api_base": SUB2API, "input": [_agent("x")]}, CallTypes.acompletion)
        self.assertEqual(out["input"][0]["type"], "agent_message")

    def test_plain_string_call_type_also_works(self) -> None:
        out = _run({"api_base": SUB2API, "input": [_agent("x")]}, "aresponses")
        self.assertEqual(out["input"][0]["type"], "message")

    def test_no_agent_message_returns_same_list_object(self) -> None:
        items = [_user("a")]
        out = _run({"api_base": SUB2API, "input": items})
        self.assertIs(out["input"], items)

    def test_string_input_untouched(self) -> None:
        out = _run({"api_base": SUB2API, "input": "hello"})
        self.assertEqual(out["input"], "hello")

    def test_garbage_never_raises(self) -> None:
        for kw in ({"api_base": SUB2API, "input": [None, 3, {"type": "agent_message", "content": "notalist"}]},
                   {"api_base": SUB2API}, "notadict"):
            _run(kw)  # must not raise


if __name__ == "__main__":
    unittest.main()
