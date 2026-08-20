"""
cursor_web_fc_sys_rewrite.py — LiteLLM pre-call hook, gated to ``cursor-web-fc-*`` aliases.

Why
---
``cursor-web-fc-*`` routes to the zerokey WEB-injection path (``zero-cursor-101``
``/chat/completions`` → ``raw.js``/``web-tools.js``), which fakes tool calling by
prompt injection (the web ChatGPT session has no native function calling). That
injection is reliable on small payloads but COLLAPSES under a real Cursor payload:
Cursor's full agent system prompt (``status_update_spec`` / ``summary_spec`` /
``flow`` / ``completion_spec`` — "narrate, write status updates, give summaries")
directly fights the injection's "reply with ONLY the JSON tool block", and at
~65KB the agent persona wins → the model refuses ("I can't run a shell command in
this chat, paste it here"). Measured: full sysprompt + 19 tools ≈ 33% success;
de-conflicted ≈ 100% (3/3, then 3/3). See memory
feedback_cursor_gpt_web_toolcall_collapses_under_full_payload.

Fix
---
For cursor-web-fc-* requests ONLY, and only when the request actually carries
tools, de-conflict the system prompt before it reaches the web path:
(1) prepend a strong execution directive that overrides narration,
(2) strip the narration-heavy XML sections from EVERY system/developer message.
Reuses the existing (now reliable) raw.js injection — does NOT touch the shared
zerokey files.

Isolation / safety
------------------
- Gated twice: model must start with ``cursor-web-fc-`` AND the request must have
  a non-empty ``tools`` list. Tool-less chat (Cursor Ask mode, plain Q&A) passes
  through byte-for-byte — the rewrite would otherwise flatten normal answers.
- Every other model (incl. the shared ``cursor-gpt-*``) is returned byte-for-byte.
- Shape-agnostic: rewrites the system prompt wherever it lives — Responses
  ``instructions`` (str), chat ``messages`` (role system/developer), Responses
  ``input`` items (role system/developer) — so callback ordering vs the
  responses→chat bridge / cursor_responses_shim does not matter.
- Strips ALL system/developer carriers (Cursor may split the prompt); the strong
  prefix is added exactly once, on the first carrier.
- Idempotent: a sentinel prefix prevents double application; the strip regex is
  a no-op on already-stripped text.
- Observable: logs one INFO line per rewrite via standard logging and stamps
  ``metadata.cursor_web_fc_rewrite`` so firing is visible in proxy logs and
  SpendLogs metadata without set_verbose.
- Never raises: any error returns ``data`` unchanged.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional, Union

from litellm.integrations.custom_logger import CustomLogger

# a bare getLogger has no handler under litellm's logging setup (verified: INFO
# lines never reached pod stdout); verbose_proxy_logger is the one that does.
try:
    from litellm._logging import verbose_proxy_logger as _logger
except Exception:  # fallback if litellm internals move
    _logger = logging.getLogger("cursor_web_fc_sys_rewrite")

_VERSION = "v2"

_TARGET_PREFIX = "cursor-web-fc-"

_SENTINEL = "[EXECUTION ENVIRONMENT]"

_STRONG_PREFIX = (
    _SENTINEL
    + " You have a live runtime that really executes every tool in the tool list; "
    "tool results are returned to you. To do anything, emit a tool call. Hard rules "
    "that OVERRIDE any conflicting guidance below: do NOT narrate, do NOT write status "
    "updates, plans, or summaries, do NOT ask the user to run or paste anything, and "
    "NEVER say a tool/shell/file is unavailable — it is available via the tools. When "
    "the task needs an action, your reply for this turn must be the tool call(s) and "
    "nothing else.\n\n"
)

# Narration-heavy Cursor sections proven to trigger the "I can't, paste it" refusal.
_STRIP = re.compile(
    r"<(status_update_spec|summary_spec|flow|completion_spec|todo_spec)>.*?</\1>\s*",
    re.DOTALL | re.IGNORECASE,
)

_TARGET_ROLES = ("system", "developer")


def _already_done(text: str) -> bool:
    return _SENTINEL in text[:200]


def _transform_text(text: Any, add_prefix: bool) -> Any:
    if not isinstance(text, str) or not text.strip():
        return text
    if _already_done(text):
        return text
    stripped = _STRIP.sub("", text)
    return (_STRONG_PREFIX + stripped) if add_prefix else stripped


def _transform_content(content: Any, add_prefix: bool) -> Any:
    """Handle both a plain string and a list-of-parts content.

    Strips every text part; the prefix (if requested) goes on the first part only.
    """
    if isinstance(content, str):
        return _transform_text(content, add_prefix)
    if isinstance(content, list):
        first = add_prefix
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                part["text"] = _transform_text(part["text"], first)
                first = False
    return content


class CursorWebFcSysRewrite(CustomLogger):
    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: Dict[str, Any],
        call_type: Any,
    ) -> Optional[Union[Exception, str, Dict[str, Any]]]:
        try:
            if not isinstance(data, dict):
                return data
            model = data.get("model") or ""
            if not isinstance(model, str) or not model.startswith(_TARGET_PREFIX):
                return data
            # Tool-less requests (Ask mode / plain chat) must not be flattened.
            tools = data.get("tools")
            if not isinstance(tools, list) or not tools:
                return data

            prefixed = False  # strong prefix goes on exactly one carrier
            touched = 0

            # 1) Responses `instructions` (string) — primary carrier pre-bridge
            instr = data.get("instructions")
            if isinstance(instr, str) and instr.strip():
                data["instructions"] = _transform_text(instr, not prefixed)
                prefixed = True
                touched += 1

            # 2) Chat `messages` — every system/developer message
            msgs = data.get("messages")
            if isinstance(msgs, list):
                for m in msgs:
                    if isinstance(m, dict) and m.get("role") in _TARGET_ROLES:
                        m["content"] = _transform_content(m.get("content"), not prefixed)
                        prefixed = True
                        touched += 1

            # 3) Responses `input` items — every system/developer item
            inp = data.get("input")
            if isinstance(inp, list):
                for it in inp:
                    if isinstance(it, dict) and it.get("role") in _TARGET_ROLES:
                        it["content"] = _transform_content(it.get("content"), not prefixed)
                        prefixed = True
                        touched += 1

            # 4) Fallback (chat shape only): no system carrier found → insert one
            if not touched and isinstance(msgs, list):
                msgs.insert(0, {"role": "system", "content": _STRONG_PREFIX.strip()})
                touched = 1

            if touched:
                try:
                    md = data.get("metadata")
                    if isinstance(md, dict):
                        md["cursor_web_fc_rewrite"] = _VERSION
                except Exception:
                    pass
                _logger.info(
                    "[cursor_web_fc_sys_rewrite %s] rewrote %d carrier(s) "
                    "(model=%r call_type=%r tools=%d)",
                    _VERSION, touched, model, call_type, len(tools),
                )
        except Exception:
            _logger.exception("[cursor_web_fc_sys_rewrite %s] failed open (passthrough)", _VERSION)
        return data


cursor_web_fc_sys_rewrite = CursorWebFcSysRewrite()
