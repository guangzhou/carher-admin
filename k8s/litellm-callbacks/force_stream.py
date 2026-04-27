"""
LiteLLM pre-call hook — force ``stream=True`` upstream for non-streaming
external Claude Code requests, so ``streaming_bridge`` can keep the
Cloudflare connection alive with whitespace heartbeats during the long
upstream wait, and finally collapse the upstream SSE back into a single
Anthropic Message JSON object for the client.

Why this exists
---------------

Cloudflare Tunnel applies a ~100 s "Proxy Read Timeout" between the CF
edge and the in-cluster origin. Streaming requests are protected by
``streaming_bridge`` which injects an SSE comment frame
(``: keepalive\\n\\n``) every 25 s during silence — that resets CF's
idle timer and Cloudflare never 524s.

Non-streaming requests get no such protection. Wangsu / Anthropic
buffer the entire response server-side and only send bytes once the
full Message has been generated. For Opus 4.7 / Sonnet 4.6 long
generations that's 100–700 s of complete silence on the wire → CF 524.

Empirically observed for ``claude-code-*`` keys (24 h):

    anthropic_messages with chatcmpl-* request_id, dur > 100 s : 43
    acompletion        with chatcmpl-* request_id, dur > 100 s :  2

These are all client-side requests that did NOT set ``stream=true``
(typically Cursor IDE Background Agent tasks).

How the fix works
-----------------

We split the problem in two layers:

1.  **This hook (pre-call)** — for matching keys / call types, if the
    client did *not* request streaming, we flip ``data["stream"] = True``
    so LiteLLM's upstream HTTP call will be a streaming SSE request.
    We also stamp ``litellm_metadata["_force_stream_collapse"] = True``
    so ``streaming_bridge`` can detect this is a "collapsed" request.

2.  **streaming_bridge (post-call iterator)** — when the flag is set,
    bridge buffers all upstream SSE bytes, yields a single space
    (``b" "``) every 25 s during silence (JSON parsers ignore leading
    whitespace, so this keeps Cloudflare happy without breaking the
    eventual JSON the client will parse), and at end-of-stream yields
    one final chunk: the reassembled Anthropic Message as a JSON
    object. Net effect for the client is identical to a normal
    non-streaming response, except we never go silent for > 25 s.

The HTTP ``Content-Type`` on the response stays ``text/event-stream``
(LiteLLM's anthropic_messages route hardcodes that). This is fine
because the Anthropic Python SDK and all other Claude Code clients
fall back to ``response.json()`` when ``stream=False`` was set on
the client side, regardless of the response Content-Type — see
``anthropic._response.APIResponse._parse``: when content type is not
JSON it tries ``response.json()`` first, and only raises if the body
is genuinely unparseable.

Gating
------

Two predicates must both match before we flip ``stream``:

*   ``user_api_key_dict.key_alias`` matches ``FORCE_STREAM_KEY_ALIASES``
    (exact, comma-separated) or ``FORCE_STREAM_KEY_PREFIXES`` (prefix,
    comma-separated). Default is the bundled
    ``claude-code-liuguoxian-50gj`` canary entry.
*   ``call_type`` is ``anthropic_messages``. We deliberately do not
    cover ``acompletion`` in the canary because the chat-completions
    streaming format is different and reassembling it back into a
    non-streaming OpenAI ``ChatCompletion`` would need a separate code
    path. acompletion accounts for only 2/45 of observed 524s; we
    will add it once the anthropic_messages canary passes.

Plus a third precondition: the client must NOT have set ``stream=True``
already. Legitimate streaming requests are passed through unchanged
and are already protected by the existing SSE-comment heartbeat.

Source of truth
---------------

This file lives at ``k8s/litellm-callbacks/force_stream.py`` and is
mounted into the LiteLLM proxy pod via ``ConfigMap litellm-callbacks``.
The same content is embedded inline in ``k8s/litellm-proxy.yaml``;
keep the two in sync when editing.

Registered in LiteLLM via ``litellm_settings.callbacks``:

    litellm_settings:
      callbacks: [..., "force_stream.force_stream", ...]

Order matters: ``force_stream.force_stream`` MUST run before
``streaming_bridge.streaming_bridge`` so the flag is in place by the
time the iterator hook fires.
"""
from __future__ import annotations

import logging as _stdlib_logging
import os
from typing import Any, Optional, Set, Tuple

from litellm.integrations.custom_logger import CustomLogger


_log = _stdlib_logging.getLogger("force_stream")


_DEFAULT_CANARY_KEY_ALIASES: frozenset = frozenset({"claude-code-liuguoxian-50gj"})
_DEFAULT_CANARY_KEY_PREFIXES: Tuple[str, ...] = ()

# Only anthropic_messages in the initial canary. acompletion will be
# added in a follow-up once we have a chat-completions reassembler.
_TARGET_CALL_TYPES: frozenset = frozenset({"anthropic_messages"})


def _load_canary_aliases() -> Set[str]:
    raw = os.environ.get("FORCE_STREAM_KEY_ALIASES")
    if raw is None:
        # Only use bundled default if neither alias nor prefix env is set.
        if os.environ.get("FORCE_STREAM_KEY_PREFIXES") is None:
            return set(_DEFAULT_CANARY_KEY_ALIASES)
        return set()
    return {item.strip() for item in raw.split(",") if item.strip()}


def _load_canary_prefixes() -> Tuple[str, ...]:
    raw = os.environ.get("FORCE_STREAM_KEY_PREFIXES")
    if raw is None:
        return _DEFAULT_CANARY_KEY_PREFIXES
    return tuple(item.strip() for item in raw.split(",") if item.strip())


class ForceStream(CustomLogger):
    """Pre-call hook: flip ``stream=True`` upstream and mark the request
    so ``streaming_bridge`` knows to collapse the SSE back to JSON."""

    def __init__(self) -> None:
        super().__init__()
        self._aliases: Set[str] = _load_canary_aliases()
        self._prefixes: Tuple[str, ...] = _load_canary_prefixes()

    def _alias_matches(self, key_alias: Optional[str]) -> bool:
        if not isinstance(key_alias, str):
            return False
        if key_alias in self._aliases:
            return True
        if self._prefixes and key_alias.startswith(self._prefixes):
            return True
        return False

    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: dict,
        call_type: str,
    ) -> Any:
        """LiteLLM contract: return modified data, or raise to block."""
        try:
            if call_type not in _TARGET_CALL_TYPES:
                return data
            if not isinstance(data, dict):
                return data
            key_alias = getattr(user_api_key_dict, "key_alias", None)
            if not self._alias_matches(key_alias):
                return data
            # Pass legitimate streaming requests through untouched.
            if data.get("stream") is True:
                return data

            data["stream"] = True
            md = data.setdefault("litellm_metadata", {})
            if isinstance(md, dict):
                md["_force_stream_collapse"] = True

            try:
                _log.info(
                    "force_stream: collapsing %s for key_alias=%s",
                    call_type,
                    key_alias,
                )
            except Exception:
                pass
        except Exception as exc:
            try:
                _log.warning("force_stream: pre_call_hook error: %r", exc)
            except Exception:
                pass
        return data


force_stream = ForceStream()
