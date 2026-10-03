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


# Positive allowlist of Responses ``input[]`` item types that ARE input messages.
# Kept as an allowlist, not an exclusion list: an exclusion list has to enumerate
# every non-message type or it silently treats an unknown type as a user message,
# which is exactly how ``additional_tools`` slipped through and re-broke the hook.
# Anything not named here fails closed (treated as NOT a user message), so a new
# Responses item type degrades to "keep looking" rather than "stop and return the
# wrong text".
_MESSAGE_TYPES = frozenset({
    "message",
    "input_text",
})


def _is_userish_item(item: dict) -> bool:
    """True for a user input message.

    Fails CLOSED on anything unrecognised. The previous version used
    ``_NON_MESSAGE_TYPES`` as an exclusion list and returned True for any type not
    on it, which is the same false-negative bug it was written to fix, merely
    displaced onto every type nobody enumerated. ``additional_tools`` -- the type
    Codex actually sends, and which this repo's own bridge reads in
    ``_req_uses_exec_tool`` -- was absent, so a heartbeat whose input ended in an
    additional_tools item walked onto it, found no text, and billed the ~50K-token
    poll upstream. Any future Responses item type would do the same.

    A role-less item is accepted ONLY when it is positively identifiable as an
    input message: either it declares ``type: message`` / ``input_text``, or it
    carries no ``type`` at all (the shape the role-less widening was added for).
    """
    role = item.get("role")
    if role is not None:
        return role == "user"
    itype = item.get("type")
    if itype is None:
        return True
    return isinstance(itype, str) and itype in _MESSAGE_TYPES


def _item_text(content: Any) -> str:
    """Flatten a message ``content`` (str or multi-part list) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                t = p.get("text") or p.get("input_text") or ""
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts)
    return ""


# OpenClaw (newer builds, e.g. carher-75/267) appends a metadata user message
# AFTER the heartbeat marker message:
#   input[-2] = user "[... ] [OpenClaw heartbeat poll]"
#   input[-1] = user "OpenClaw runtime context for the immediately preceding user message. ..."
# The reverse walk used to stop at that runtime-context blob, miss the marker,
# and bill the full-history poll upstream (measured 63K-157K prompt tokens per
# poll on carher-75, 2026-08-16). Runtime-context blobs are metadata, not user
# turns — skip a bounded number of them and evaluate the real last user message.
_RUNTIME_CONTEXT_PREFIX = "OpenClaw runtime context"
_MAX_CONTEXT_SKIPS = 3


def _last_user_text(data: dict) -> str:
    """Pull the last user-role text out of either Responses-API ``input``
    or chat-style ``messages``. Returns empty string on any structural
    mismatch — this hook must never raise."""
    # Responses API: data["input"] is either a string or a list of items.
    inp = data.get("input")
    if isinstance(inp, str):
        return inp
    if isinstance(inp, list) and inp:
        skips = 0
        # Walk back to find last user message
        for item in reversed(inp):
            if not isinstance(item, dict):
                continue
            if not _is_userish_item(item):
                continue
            text = _item_text(item.get("content"))
            if text.startswith(_RUNTIME_CONTEXT_PREFIX) and skips < _MAX_CONTEXT_SKIPS:
                skips += 1
                continue
            return text
    # Chat-style: data["messages"]
    msgs = data.get("messages")
    if isinstance(msgs, list) and msgs:
        skips = 0
        for m in reversed(msgs):
            if not isinstance(m, dict):
                continue
            if m.get("role") != "user":
                continue
            text = _item_text(m.get("content"))
            if text.startswith(_RUNTIME_CONTEXT_PREFIX) and skips < _MAX_CONTEXT_SKIPS:
                skips += 1
                continue
            return text
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
            # LiteLLM Responses mock_response is non-streaming; collapse
            # heartbeat mocks even when the client asked for stream=true.
            if data.get("stream") is True:
                data["stream"] = False
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
