"""
LiteLLM pre-call hook — normalize Cursor's Responses-shaped /chat/completions
requests into spec-compliant Chat Completions requests.

Background
----------
Cursor (BYOK / "Override OpenAI Base URL", incl. GPT-5.5/5.6 Agent mode) always
POSTs to ``/v1/chat/completions`` but sends a **Responses-API-shaped body**:
  * ``input`` (string or item array) instead of ``messages``
  * flat / ``type:"custom"`` tools (e.g. ApplyPatch) instead of the nested
    ``{"type":"function","function":{...}}`` chat schema
  * Responses-only fields: ``store`` / ``previous_response_id`` / ``truncation``
    / ``reasoning`` / ``include`` / ``instructions``
  * ``max_output_tokens`` instead of ``max_tokens``

Our LiteLLM ``/chat/completions`` handler expects standard ``messages`` and
nested function tools, so it rejects Cursor's payload
(``missing messages`` / ``tools[i].custom``). LiteLLM's built-in
responses<->chat bridge only fires for requests that hit the ``/responses``
endpoint, so it does NOT cover this reverse mismatch. This hook fills the gap
by rewriting the request body in place before dispatch.

Scope / isolation (all three MUST hold, else pass through untouched)
--------------------------------------------------------------------
1. ``call_type in {"completion", "acompletion"}`` — HARD boundary. Codex CLI hits ``/v1/responses``
   (``call_type == "responses"``) so it never enters this hook, even though Codex
   and Cursor share the same ``cursor-*`` virtual keys.
2. Body carries Responses markers (``input`` present, or a ``type:"custom"`` /
   flat function tool, or ``previous_response_id``/``instructions`` with no
   ``messages``). Normal chat traffic (carher-* bots send ``messages``) is not
   touched.
3. ``key_alias`` matches the Cursor allowlist (default prefix ``cursor-``,
   overridable via env), or the request uses the dedicated ``cursor-gpt-*``
   model namespace. The latter lets a master-key smoke exercise the same route;
   virtual-key model permissions still limit the public models to the target
   Cursor key. This is NOT the Codex isolation lever (condition 1 is).

Downstream caveat (documented, not handled here)
------------------------------------------------
The ChatGPT account-pool gateway (chatgpt-acct-proxy/gateway/app/convert.py)
currently DROPS tools when converting chat->responses. So tool calls only reach
the model if Cursor targets a chat-native GPT (e.g. OpenRouter ``gpt-5.4``). If
Cursor targets a ``chatgpt-*`` responses-pool model, tools are dropped at the
gateway and only plain chat works — extending the gateway is a separate change.

Idempotent: a standard chat body (``messages`` present, no Responses markers)
returns unmodified.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from litellm.integrations.custom_logger import CustomLogger

_log = logging.getLogger("cursor_responses_shim")

# Condition 3 allowlist. Default: any key aliased ``cursor-*``. Env overrides:
#   CURSOR_SHIM_KEY_PREFIXES="cursor-,cursor-ide-"   (comma list)
#   CURSOR_SHIM_KEY_ALIASES="cursor-liuguoxian,..."  (exact comma list)
# Dedicated cursor-gpt-* models are also accepted so master-key probes execute
# the identical path; public access remains constrained by virtual-key models.
# If CURSOR_SHIM_DISABLE_KEY_GATE=1, condition 3 is skipped (rely on 1+2 only).
_DEFAULT_KEY_PREFIXES: Tuple[str, ...] = ("cursor-",)

_CHAT_COMPLETION_CALL_TYPES = frozenset({"completion", "acompletion"})
_CURSOR_MODEL_PREFIX = "cursor-gpt-"


def _call_type_value(call_type: Any) -> str:
    """Normalize LiteLLM's str Enum without losing its underlying value."""
    return str(getattr(call_type, "value", call_type))

# Responses-only fields that Chat Completions does not accept and must be
# stripped once their content (if any) has been folded into messages.
_STRIP_FIELDS: Tuple[str, ...] = (
    "input",
    "instructions",
    "previous_response_id",
    "store",
    "truncation",
    "reasoning",
    "include",
    "text",  # Responses output-format wrapper; not a Chat Completions field.
    "max_output_tokens",  # renamed to max_tokens below
)


def _load_key_prefixes() -> Tuple[str, ...]:
    raw = os.environ.get("CURSOR_SHIM_KEY_PREFIXES")
    if raw is None:
        return _DEFAULT_KEY_PREFIXES
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def _load_key_aliases() -> frozenset:
    raw = os.environ.get("CURSOR_SHIM_KEY_ALIASES")
    if not raw:
        return frozenset()
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


def _content_to_text(content: Any) -> str:
    """Flatten a Responses content value (string or part-array) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for c in content:
            if isinstance(c, dict) and c.get("type") in (
                "input_text",
                "output_text",
                "text",
            ):
                parts.append(c.get("text", "") or "")
        return "".join(parts)
    return ""



def _tool_output_to_content(output: Any) -> Any:
    """Tool output -> chat tool-message content.

    A string stays a string. A list that contains an image becomes image
    blocks, because dumping it to JSON makes the context-window gate count
    the base64 payload as text. Anything else is dumped, as before.
    """
    if isinstance(output, str) or output is None:
        return output or ""
    if isinstance(output, list):
        blocks: List[Dict[str, Any]] = []
        saw_image = False
        for part in output:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type in ("input_text", "output_text", "text"):
                text = part.get("text")
                if isinstance(text, str) and text:
                    blocks.append({"type": "text", "text": text})
            elif part_type in ("input_image", "image_url"):
                image_url = part.get("image_url") or part.get("url")
                url = image_url.get("url") if isinstance(image_url, dict) else image_url
                if isinstance(url, str) and url:
                    image = {"url": url}
                    detail = part.get("detail")
                    if isinstance(detail, str) and detail:
                        image["detail"] = detail
                    blocks.append({"type": "image_url", "image_url": image})
                    saw_image = True
                else:
                    # a block claimed to be an image but carried no URL.
                    # dumping the whole output would count its bytes as text,
                    # which is the failure this function exists to avoid.
                    return output
        if saw_image and len(blocks) == len(output):
            return blocks
        # a part we do not recognise sat next to the image. keep the original
        # list rather than dump it: the outbound transform passes through
        # input_image / input_text, and a dump would be counted as text.
        if saw_image:
            return output
    return json.dumps(output)


def _input_to_messages(input_val: Any) -> List[Dict[str, Any]]:
    """Convert a Responses ``input`` value to Chat Completions ``messages``.

    Handles: bare string, message items (input_text/output_text), function_call
    items (-> assistant tool_calls), function_call_output items (-> tool role).
    Consecutive function_call items merge into one assistant message so the
    tool_calls / tool-result ordering stays valid. ``reasoning`` items are
    dropped (Chat Completions has no equivalent).
    """
    if isinstance(input_val, str):
        return [{"role": "user", "content": input_val}]
    if not isinstance(input_val, list):
        return []

    messages: List[Dict[str, Any]] = []
    pending_tool_calls: List[Dict[str, Any]] = []

    def _flush_tool_calls() -> None:
        if pending_tool_calls:
            messages.append(
                {"role": "assistant", "content": None, "tool_calls": list(pending_tool_calls)}
            )
            pending_tool_calls.clear()

    for item in input_val:
        if not isinstance(item, dict):
            continue
        itype = item.get("type", "message")

        if itype == "function_call":
            args = item.get("arguments", "")
            if not isinstance(args, str):
                args = json.dumps(args)
            pending_tool_calls.append(
                {
                    "id": item.get("call_id") or item.get("id") or "",
                    "type": "function",
                    "function": {"name": item.get("name", ""), "arguments": args},
                }
            )
            continue

        # Any non-function_call item ends the current assistant tool-call turn.
        _flush_tool_calls()

        if itype == "function_call_output":
            output = item.get("output", "")
            # A tool result that carries an image must stay an image block.
            # json.dumps of a data: URL makes the context-window gate count the
            # base64 as text: one screenshot counted 1,494,853 against an
            # 872,000 ceiling (SpendLogs request b0095d2c, 2026-09-28).
            content = _tool_output_to_content(output)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item.get("call_id") or item.get("id") or "",
                    "content": content,
                }
            )
        elif itype == "message":
            messages.append(
                {
                    "role": item.get("role", "user"),
                    "content": _content_to_text(item.get("content")),
                }
            )
        elif itype == "reasoning":
            continue  # no Chat Completions equivalent
        # else: unknown item type -> best-effort drop

    _flush_tool_calls()
    return messages


def _convert_tools(tools: Any) -> List[Dict[str, Any]]:
    """Normalize Responses-shaped tools to Chat Completions function tools.

    * already-nested ``{"type":"function","function":{...}}`` -> kept as-is
    * flat ``{"type":"function","name":..,"parameters":..}`` -> nested
    * freeform ``{"type":"custom","name":..,"format":..}`` (e.g. ApplyPatch)
      -> downgraded to a function taking a single ``input`` string; the original
      grammar/format is preserved in the description. Lossy but valid: Chat
      Completions has no freeform-custom-tool concept.
    """
    if not isinstance(tools, list):
        return tools
    out: List[Dict[str, Any]] = []
    for t in tools:
        if not isinstance(t, dict):
            out.append(t)
            continue
        # Standard nested chat tool: keep.
        if "function" in t and isinstance(t["function"], dict):
            out.append(t)
            continue
        ttype = t.get("type")
        if ttype == "custom":
            fmt = t.get("format")
            desc = t.get("description") or ""
            if fmt is not None:
                desc = (desc + "\n\nformat: " + json.dumps(fmt)).strip()
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": t.get("name", ""),
                        "description": desc,
                        "parameters": {
                            "type": "object",
                            "properties": {"input": {"type": "string"}},
                            "required": ["input"],
                        },
                    },
                }
            )
        elif ttype == "function":
            fn: Dict[str, Any] = {"name": t.get("name", "")}
            if t.get("description") is not None:
                fn["description"] = t["description"]
            fn["parameters"] = t.get("parameters") or {
                "type": "object",
                "properties": {},
            }
            out.append({"type": "function", "function": fn})
        else:
            out.append(t)  # unknown tool type, leave alone
    return out


def _convert_tool_choice(choice: Any) -> Any:
    """Responses ``tool_choice`` -> Chat Completions ``tool_choice``."""
    if isinstance(choice, str):
        return choice  # "auto" / "none" / "required" are shared
    if isinstance(choice, dict):
        # Responses: {"type":"function","name":"X"} or {"type":"custom","name":"X"}
        if choice.get("type") in ("function", "custom") and "name" in choice:
            return {"type": "function", "function": {"name": choice["name"]}}
    return choice


def _looks_like_responses_payload(data: Dict[str, Any]) -> bool:
    if data.get("input") is not None:
        return True
    tools = data.get("tools")
    if isinstance(tools, list):
        for t in tools:
            if not isinstance(t, dict):
                continue
            if t.get("type") == "custom":
                return True
            # flat responses function tool: type=function, no nested "function"
            if t.get("type") == "function" and "function" not in t and "name" in t:
                return True
    if "messages" not in data:
        if data.get("previous_response_id") is not None or data.get("instructions"):
            return True
    return False


def _rewrite(data: Dict[str, Any]) -> Dict[str, Any]:
    """Mutate ``data`` in place: Responses shape -> Chat Completions shape."""
    messages: List[Dict[str, Any]] = []

    instructions = data.get("instructions")
    if isinstance(instructions, str) and instructions and "messages" not in data:
        messages.append({"role": "system", "content": instructions})

    if data.get("input") is not None:
        messages.extend(_input_to_messages(data["input"]))
    elif "messages" in data and isinstance(data["messages"], list):
        # Mixed payload: keep provided messages, just prepend instructions.
        messages.extend(data["messages"])

    if messages:
        data["messages"] = messages

    if "tools" in data:
        data["tools"] = _convert_tools(data["tools"])
    if "tool_choice" in data:
        data["tool_choice"] = _convert_tool_choice(data["tool_choice"])

    if "max_output_tokens" in data and "max_tokens" not in data:
        mot = data.get("max_output_tokens")
        if mot is not None:
            data["max_tokens"] = mot

    for f in _STRIP_FIELDS:
        data.pop(f, None)

    return data


class CursorResponsesShim(CustomLogger):
    """Pre-call hook that normalizes Cursor's Responses-shaped chat requests."""

    def __init__(self) -> None:
        super().__init__()
        self._prefixes: Tuple[str, ...] = _load_key_prefixes()
        self._aliases: frozenset = _load_key_aliases()
        self._key_gate_disabled: bool = (
            os.environ.get("CURSOR_SHIM_DISABLE_KEY_GATE", "").strip() in ("1", "true", "True")
        )

    @staticmethod
    def _extract_key_alias(user_api_key_dict: Any) -> Optional[str]:
        """Read LiteLLM key alias from object-style or dict-style metadata."""
        if isinstance(user_api_key_dict, dict):
            val = (
                user_api_key_dict.get("key_alias")
                or user_api_key_dict.get("user_api_key_alias")
                or user_api_key_dict.get("alias")
            )
            return val if isinstance(val, str) else None
        val = getattr(user_api_key_dict, "key_alias", None)
        return val if isinstance(val, str) else None

    def _key_matches(self, key_alias: Optional[str]) -> bool:
        if self._key_gate_disabled:
            return True
        if not isinstance(key_alias, str):
            return False
        if key_alias in self._aliases:
            return True
        if self._prefixes and key_alias.startswith(self._prefixes):
            return True
        return False

    def _is_target_request(self, data: Dict[str, Any], key_alias: Optional[str]) -> bool:
        """Permit the exact Cursor virtual key or its isolated public models.

        LiteLLM's pre-call hook receives the authenticated key alias in normal
        client traffic. Admin-key smoke traffic has no Cursor alias, so the
        dedicated ``cursor-gpt-*`` model namespace is its only safe selector.
        """
        return self._key_matches(key_alias) or (
            isinstance(data.get("model"), str)
            and data["model"].startswith(_CURSOR_MODEL_PREFIX)
        )

    def _maybe_rewrite(self, data: Any, call_type: Any, key_alias: Optional[str], source: str) -> Any:
        try:
            # (1) HARD isolation: only chat completions. Codex uses "responses".
            if _call_type_value(call_type) not in _CHAT_COMPLETION_CALL_TYPES:
                return data
            if not isinstance(data, dict):
                return data
            # (2) Only act on Responses-shaped bodies; leave standard chat alone.
            if not _looks_like_responses_payload(data):
                return data
            # (3) Scope to Cursor keys (product requirement, not Codex gate).
            if not self._is_target_request(data, key_alias):
                return data

            _log.warning(
                "cursor_responses_shim: source=%s call_type=%s key_alias=%s data_keys=%s",
                source,
                call_type,
                key_alias,
                sorted(list(data.keys()))[:12],
            )
            return _rewrite(data)
        except Exception:  # never break a request on shim failure
            _log.exception("cursor_responses_shim: rewrite failed; passing through")
            return data

    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: Dict[str, Any],
        call_type: str,
    ) -> Dict[str, Any]:
        """LiteLLM contract: return (possibly-mutated) data, or raise to block."""
        key_alias = self._extract_key_alias(user_api_key_dict)
        return self._maybe_rewrite(data, call_type, key_alias, "pre_call")

    async def async_pre_call_deployment_hook(self, kwargs: Dict[str, Any], call_type: Any) -> Any:
        """Route-selected hook; catches LiteLLM async chat paths labelled acompletion."""
        key_alias = None
        if not isinstance(kwargs, dict):
            return kwargs

        meta = kwargs.get("litellm_metadata") or {}
        if isinstance(meta, dict):
            key_alias = meta.get("user_api_key_alias")
            user_key = meta.get("user_api_key_dict")
            if not key_alias and isinstance(user_key, dict):
                val = user_key.get("key_alias") or user_key.get("user_api_key_alias")
                key_alias = val if isinstance(val, str) else None

        # LiteLLM versions differ here: some provide the body as the router
        # kwargs directly, others nest it under ``data``. Rewrite either shape.
        request_data = kwargs.get("data")
        if isinstance(request_data, dict):
            kwargs["data"] = self._maybe_rewrite(
                request_data, call_type, key_alias, "pre_deployment.data"
            )
            return kwargs
        return self._maybe_rewrite(kwargs, call_type, key_alias, "pre_deployment")


cursor_responses_shim = CursorResponsesShim()
