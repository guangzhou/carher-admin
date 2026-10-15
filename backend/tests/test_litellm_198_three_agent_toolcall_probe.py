from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "litellm-198-three-agent-toolcall-probe.py"
SPEC = importlib.util.spec_from_file_location("three_agent_probe", SCRIPT)
assert SPEC and SPEC.loader
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def sse(*events):
    return "\n".join("data: " + json.dumps(e) for e in events) + "\ndata: [DONE]\n"


def test_claude_tool_use_with_path_is_tool():
    raw = sse({"type": "content_block_start", "content_block": {"type": "tool_use", "name": "read_file"}},
              {"type": "content_block_delta", "delta": {"partial_json": '{"path": "/etc/hostname"}'}})
    assert probe.judge_claude(raw) == "TOOL"


def test_claude_refusal_text_is_reported_verbatim():
    raw = sse({"type": "content_block_delta", "delta": {"text": "I can't discuss that."}})
    assert probe.judge_claude(raw) == "TEXT:I can't discuss that."


def test_codex_function_call_from_completed_event_counts():
    raw = sse({"type": "response.completed", "response": {"output": [
        {"type": "function_call", "arguments": '{"path":"/etc/hostname"}'}]}})
    assert probe.judge_codex(raw) == "TOOL"


def test_cursor_tool_call_without_path_is_noargs():
    raw = sse({"choices": [{"delta": {"tool_calls": [{"function": {"arguments": "{}"}}]}}]})
    assert probe.judge_cursor(raw) == "TOOL-NOARGS"


def test_empty_stream_is_empty():
    assert probe.judge_cursor(sse()) == "EMPTY"


def test_summary_counts_noargs_and_text_as_bad():
    rows = [("a", "claude", "TOOL", "0"), ("a", "codex", "TOOL-NOARGS", "0"),
            ("a", "cursor", "TEXT:no", "1"), ("b", "claude", "429", "0")]
    per, bad = probe.summarize(rows)
    assert [b[:3] for b in bad] == [("a", "codex", "TOOL-NOARGS"), ("a", "cursor", "TEXT:no"),
                                    ("b", "claude", "429")]
    assert per["a"]["cursor"] == ["TEXT:no/fb1"]


def test_requests_carry_tools_in_each_wire_shape():
    for shape, path in (("claude", "/v1/messages"), ("codex", "/v1/responses"),
                        ("cursor", "/v1/chat/completions")):
        p, body, _ = probe.request_for(shape, "m", "q")
        assert p == path and body["stream"] is True and body["tools"]
