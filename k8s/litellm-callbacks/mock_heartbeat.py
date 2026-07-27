"""
LiteLLM pre-call hook — short-circuit OpenClaw heartbeat polls.

OpenClaw runtime periodically sends a "[OpenClaw heartbeat poll]" turn to
its model so the agent can wake up and run autonomous actions. With 270+
carher instances, each heartbeat ships ~50K prompt tokens (full chat
history + tool catalog) and costs ~$0.22 on gpt-5.5. The vast majority
return no tool_call — pure waste.

This hook detects the heartbeat marker in the request body and sets
``data["mock_response"] = "ok"``. LiteLLM's Responses API path
(``litellm/responses/main.py::aresponses``) checks ``mock_response`` BEFORE
provider resolution and returns a synthetic ``ResponsesAPIResponse`` with
status=completed, output=[{type:"message", content:[{type:"output_text",
text:"ok"}]}]. No upstream call, no tokens billed.

Activation
----------

Default: ON for all keys. Disable per-call by setting
``MOCK_HEARTBEAT_DISABLED=1`` env var, or per-key by adding
``litellm_metadata._skip_mock_heartbeat: true``.

The detection is conservative: only short-circuits when the LAST user-role
message contains the literal marker ``[OpenClaw heartbeat poll]``. Responses
API payloads sometimes omit ``role`` on the final input item, so role-less
final items are also inspected. Any other content (even mentions of heartbeats
in conversation history) passes through.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

_log = logging.getLogger("mock_heartbeat")

_MARKER = "[OpenClaw heartbeat poll]"
_TARGET_CALL_TYPES = frozenset({"responses", "aresponses", "acompletion", "completion"})


# Role-less items in a Responses ``input[]`` are mostly NOT user messages —
# function_call_output / reasoning / custom_tool_call_output all lack ``role``.
# Accepting them broke the hook in both directions (measured 2026-07-27):
#   - false negative: input=[{role:user, content:MARKER}, {type:"reasoning", …}]
#     walked back onto the reasoning item, returned its text, missed the marker,
#     and billed the ~50K-token heartbeat upstream — the exact payload shape the
#     role-less widening was added for.
#   - false positive: a role-less function_call_output whose content echoed the
#     marker short-circuited a REAL request to "ok".
# So accept a missing role only on an actual input message.
_NON_MESSAGE_TYPES = frozenset({
    "function_call",
    "function_call_output",
    "custom_tool_call",
    "custom_tool_call_output",
    "reasoning",
    "computer_call",
    "computer_call_output",
    "file_search_call",
    "web_search_call",
    "code_interpreter_call",
    "image_generation_call",
    "local_shell_call",
    "local_shell_call_output",
    "mcp_call",
    "mcp_list_tools",
    "mcp_approval_request",
    "mcp_approval_response",
    "item_reference",
})


def _is_userish_item(item: dict) -> bool:
    """True for a user input message. Responses API sometimes omits ``role`` on
    input-message items, so a missing role is accepted — but only when the item is
    not one of the non-message types above."""
    role = item.get("role")
    if role is not None:
        return role == "user"
    itype = item.get("type")
    if isinstance(itype, str) and itype in _NON_MESSAGE_TYPES:
        return False
    return True


def _last_user_text(data: dict) -> str:
    """Pull the last user-role text out of either Responses-API ``input``
    or chat-style ``messages``. Returns empty string on any structural
    mismatch — this hook must never raise."""
    # Responses API: data["input"] is either a string or a list of items.
    inp = data.get("input")
    if isinstance(inp, str):
        return inp
    if isinstance(inp, list) and inp:
        # Walk back to find last user message
        for item in reversed(inp):
            if not isinstance(item, dict):
                continue
            if not _is_userish_item(item):
                continue
            content = item.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                # multi-part: concat text parts
                parts = []
                for p in content:
                    if isinstance(p, dict):
                        t = p.get("text") or p.get("input_text") or ""
                        if isinstance(t, str):
                            parts.append(t)
                if parts:
                    return "\n".join(parts)
            break
    # Chat-style: data["messages"]
    msgs = data.get("messages")
    if isinstance(msgs, list) and msgs:
        for m in reversed(msgs):
            if not isinstance(m, dict):
                continue
            if m.get("role") != "user":
                continue
            c = m.get("content")
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                parts = []
                for p in c:
                    if isinstance(p, dict):
                        t = p.get("text") or ""
                        if isinstance(t, str):
                            parts.append(t)
                if parts:
                    return "\n".join(parts)
            break
    return ""


class MockHeartbeat(CustomLogger):
    def __init__(self) -> None:
        super().__init__()

    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: dict,
        call_type: str,
    ) -> Any:
        try:
            if os.environ.get("MOCK_HEARTBEAT_DISABLED") == "1":
                return data
            if call_type not in _TARGET_CALL_TYPES:
                return data
            if not isinstance(data, dict):
                return data

            md = data.get("litellm_metadata")
            if isinstance(md, dict) and md.get("_skip_mock_heartbeat"):
                return data

            text = _last_user_text(data)
            if _MARKER not in text:
                return data

            data["mock_response"] = "ok"
            md = data.setdefault("litellm_metadata", {})
            if isinstance(md, dict):
                md["_mock_heartbeat"] = True
                md["_mock_heartbeat_marker"] = _MARKER
            try:
                key_alias = getattr(user_api_key_dict, "key_alias", None)
                _log.info(
                    "mock_heartbeat: short-circuit call_type=%s key_alias=%s",
                    call_type,
                    key_alias,
                )
            except Exception:
                pass
        except Exception as exc:
            try:
                _log.warning("mock_heartbeat: pre_call_hook error: %r", exc)
            except Exception:
                pass
        return data


mock_heartbeat = MockHeartbeat()
