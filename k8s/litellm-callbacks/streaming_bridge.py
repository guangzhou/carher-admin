"""
LiteLLM streaming bridge — SSE heartbeat + TTFT fix for the
``anthropic_messages`` (/v1/messages) passthrough path.

Three independent problems this module addresses
------------------------------------------------

1.  **524 origin timeouts on Claude Code CLI clients.**

    Claude Code CLI users hit ``https://litellm.carher.net`` → Cloudflare
    Tunnel → in-cluster LiteLLM. Cloudflare free/Pro applies a
    ~100–120 s "Proxy Read Timeout" to the *origin* stream: if the
    tunneled response emits no bytes in that window, Cloudflare
    drops the connection and returns 524.

    When claude-opus-4-7 on the Wangsu cheliantianxia6 gateway enters
    extended ``thinking`` (e.g. large tool loop context), the upstream
    can stay silent well past 100 s before the first visible byte
    arrives. Internal carher bots are unaffected — they talk to
    ``litellm-proxy.carher.svc:4000`` directly and skip Cloudflare
    entirely — but external claude-code CLI traffic keeps 524ing.

2.  **``LiteLLM_SpendLogs.startTime`` is captured at the wrong moment
    for every ``anthropic_messages`` request (upstream bug).**

    In ``litellm/llms/anthropic/experimental_pass_through/messages/
    streaming_iterator.py``, ``BaseAnthropicMessagesStreamingIterator.
    __init__`` does:

        self.start_time = datetime.now()

    and that timestamp is what gets threaded all the way down to
    ``async_success_handler(start_time=...)`` → ``get_logging_payload`` →
    ``LiteLLM_SpendLogs.startTime``. But this ``__init__`` runs AFTER
    ``await async_httpx_client.post(stream=True)`` has returned, which
    means AFTER the upstream HTTP response status + headers have been
    received. In practice Wangsu buffers the first batch of SSE frames
    server-side and flushes headers together with
    ``message_start + content_block_start + ping +
    content_block_delta`` in a single ~600 B TCP chunk. So the
    iterator's ``self.start_time`` is essentially the same instant as
    the first content byte, and ``completionStartTime - startTime``
    collapses to ≈ 0.

    The ``acompletion`` path (/v1/chat/completions, what carher bots
    use) avoids this entirely because ``function_setup`` initializes
    ``Logging(start_time=<proxy entry time>)`` and ``CustomStreamWrapper``
    stamps ``completion_start_time`` on the first chunk. That is why
    carher's Opus 4.7 SpendLogs have correct TTFT and Duration while
    claude-code's Opus 4.7 SpendLogs do not — the route determines the
    code path, not the model.

3.  **``completionStartTime`` is never set on the passthrough path, so
    LiteLLM falls back to ``end_time``.**

    ``litellm_core_utils/litellm_logging.py`` has:

        if self.completion_start_time is None:
            self.completion_start_time = end_time

    which makes ``TTFT == Duration`` for half the ``anthropic_messages``
    rows (the ones whose requests ran long enough that our pre-call
    hook wasn't in effect). We need to explicitly stamp
    ``completion_start_time`` when the first user-visible token arrives.

Fix
---

This module does two things at import time + one thing per request:

*   **(patch, global)** Replaces
    ``BaseAnthropicMessagesStreamingIterator.__init__`` with a version
    that pulls the start time from
    ``litellm_logging_obj.start_time`` (the real proxy-entry time set
    by ``function_setup`` in
    ``litellm/proxy/common_request_processing.py``) whenever it is
    available. Falls back to ``datetime.now()`` for safety. Applied
    unconditionally to every ``anthropic_messages`` request because
    proxy-entry time is semantically the right value for
    ``LiteLLM_SpendLogs.startTime`` in every case — this removes the
    upstream bug's effect on Duration as well.

*   **(hook, gated)** Registers an
    ``async_post_call_streaming_iterator_hook`` callback. For requests
    matching the gate (``CANARY_KEY_ALIASES`` + call_type ==
    ``anthropic_messages``) we wrap the upstream byte iterator with a
    pre-fetching async generator that:

    * Scans the upstream bytes for the first ``content_block_delta``
      SSE event and at that moment calls
      ``logging_obj._update_completion_start_time(datetime.now())``.
      This is the first frame that carries user-visible token payload
      (``text_delta``, ``thinking_delta``, ``input_json_delta``). It
      suppresses the ``end_time`` fallback so
      ``LiteLLM_SpendLogs.completionStartTime`` reflects a real TTFT.
    * If ``HEARTBEAT_INTERVAL_SECONDS`` elapse without a chunk, emits an
      SSE comment frame (``b": keepalive\\n\\n"``). Per the EventSource
      spec, any compliant SSE client (incl. all Anthropic SDKs and
      claude-code CLI) silently ignores lines starting with ``:``.
      Cloudflare sees activity on the wire and does not time out.

Combined effect for canary requests
-----------------------------------

    startTime            = proxy entry (from logging_obj.start_time)
    completionStartTime  = first content_block_delta from upstream
    endTime              = end of request
    TTFT  = completionStartTime - startTime
          = time from client-perceived request start until upstream's
            first model-output token reached us — a number users can
            actually reason about.
    Duration = endTime - startTime
             = full request wall time.

For non-canary requests the hook is inactive, so completionStartTime
still falls back to endTime and TTFT still == Duration. That's no
worse than the existing behavior; it just doesn't benefit from the hook.
startTime is corrected for everyone.

Gating (canary strategy)
------------------------

Two narrow predicates must match before we wrap the iterator:

    * ``user_api_key_dict.key_alias`` matches either
      ``STREAMING_BRIDGE_KEY_ALIASES`` (exact, comma-separated) or
      ``STREAMING_BRIDGE_KEY_PREFIXES`` (prefix, comma-separated). At
      least one env var should be set in production; if neither is set
      the bundled default matches only ``claude-code-liuguoxian-50gj``.
    * ``logging_obj.call_type == "anthropic_messages"``.

The __init__ monkey-patch is NOT gated — it's a correctness fix that
makes LiteLLM_SpendLogs.startTime match what every other call_type
already stores.

Heartbeat interval is tunable via ``STREAMING_BRIDGE_HEARTBEAT_SECONDS``
(default 25 s — 4× safety margin under Cloudflare's ~100 s limit).

Registered in LiteLLM via ``litellm_settings.callbacks``:

    litellm_settings:
      callbacks: ["streaming_bridge.streaming_bridge"]
"""
from __future__ import annotations

import asyncio
import datetime
import logging as _stdlib_logging
import os
from typing import Any, AsyncIterator, Optional, Set, Tuple

from litellm.integrations.custom_logger import CustomLogger
import litellm


_log = _stdlib_logging.getLogger("streaming_bridge")


# Default canary: only liuguoxian's Claude Code key when no env is set.
# In production we expect ``STREAMING_BRIDGE_KEY_PREFIXES=claude-code-``
# so every Claude Code CLI user is covered without maintaining a list.
_DEFAULT_CANARY_KEY_ALIASES: frozenset = frozenset({"claude-code-liuguoxian-50gj"})
_DEFAULT_CANARY_KEY_PREFIXES: Tuple[str, ...] = ()

# Well below Cloudflare's ~100 s origin read timeout.
_DEFAULT_HEARTBEAT_SECONDS: float = 25.0

# Only the passthrough path has the TTFT bug; acompletion handles its
# own completion_start_time via CustomStreamWrapper.
_TARGET_CALL_TYPE: str = "anthropic_messages"

# SSE comment frame. Lines starting with ':' are ignored by compliant
# SSE clients, but the bytes on the wire reset Cloudflare's idle timer.
_HEARTBEAT_CHUNK: bytes = b": keepalive\n\n"

# First SSE event that carries actual model output (text_delta,
# thinking_delta, input_json_delta). Seeing this marker in the raw byte
# stream is what we treat as "first token produced" for TTFT purposes.
_CONTENT_DELTA_MARKER: bytes = b"content_block_delta"

# Upper bound on how many bytes we retain while scanning for the marker.
# In practice message_start + content_block_start + a few pings stay
# well under 4 KB; 64 KB leaves plenty of headroom while still capping
# worst-case buffering for a request that never emits a delta.
_SCAN_BUFFER_LIMIT: int = 64 * 1024


def _load_canary_aliases() -> Set[str]:
    raw = os.environ.get("STREAMING_BRIDGE_KEY_ALIASES")
    if raw is None:
        # Only fall back to the bundled default when NO env var is
        # configured at all. If the operator explicitly sets
        # STREAMING_BRIDGE_KEY_PREFIXES (even empty), that signals
        # prefix-only gating and we should not silently add the default.
        if os.environ.get("STREAMING_BRIDGE_KEY_PREFIXES") is None:
            return set(_DEFAULT_CANARY_KEY_ALIASES)
        return set()
    return {item.strip() for item in raw.split(",") if item.strip()}


def _load_canary_prefixes() -> Tuple[str, ...]:
    raw = os.environ.get("STREAMING_BRIDGE_KEY_PREFIXES")
    if raw is None:
        return _DEFAULT_CANARY_KEY_PREFIXES
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _load_heartbeat_seconds() -> float:
    raw = os.environ.get("STREAMING_BRIDGE_HEARTBEAT_SECONDS")
    if not raw:
        return _DEFAULT_HEARTBEAT_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_HEARTBEAT_SECONDS
    if value < 1.0:
        return 1.0
    if value > 90.0:
        return 90.0
    return value


def _chunk_bytes_view(chunk: Any) -> Optional[bytes]:
    """Return a bytes view of the chunk iff it can be scanned for the marker.

    The anthropic_messages passthrough always yields bytes. Anything
    else (dict / str / None) we skip — we do not want to stamp TTFT on
    unexpected chunk shapes.
    """
    if isinstance(chunk, (bytes, bytearray)):
        return bytes(chunk) if chunk else None
    return None


def _patch_base_anthropic_streaming_iterator() -> None:
    """Pin ``BaseAnthropicMessagesStreamingIterator.start_time`` to
    ``litellm_logging_obj.start_time`` (proxy entry time).

    The upstream class captures ``self.start_time = datetime.now()``
    inside ``__init__`` which runs AFTER the upstream HTTP response
    headers have arrived. Wangsu/Anthropic flushes headers together with
    the first SSE events, so this timestamp is essentially identical
    to the moment the first content token reaches us, collapsing TTFT
    to ≈ 0. The correct value is ``logging_obj.start_time``, set by
    ``function_setup`` the instant the request entered the proxy.

    The patch is idempotent and fails open (falls back to
    ``datetime.now()``) if ``litellm_logging_obj`` ever lacks a usable
    start_time.
    """
    try:
        from litellm.llms.anthropic.experimental_pass_through.messages.streaming_iterator import (
            BaseAnthropicMessagesStreamingIterator,
        )
    except Exception as exc:
        _log.warning(
            "streaming_bridge: cannot import BaseAnthropicMessagesStreamingIterator: %r",
            exc,
        )
        return

    original_init = BaseAnthropicMessagesStreamingIterator.__init__
    if getattr(original_init, "_streaming_bridge_patched", False):
        return  # already patched (e.g. module imported twice)

    def patched_init(self, litellm_logging_obj, request_body):  # type: ignore[no-untyped-def]
        self.litellm_logging_obj = litellm_logging_obj
        self.request_body = request_body

        proxy_entry_time = getattr(litellm_logging_obj, "start_time", None)
        if isinstance(proxy_entry_time, datetime.datetime):
            self.start_time = proxy_entry_time
        else:
            self.start_time = datetime.datetime.now()

    patched_init._streaming_bridge_patched = True  # type: ignore[attr-defined]
    BaseAnthropicMessagesStreamingIterator.__init__ = patched_init
    _log.info(
        "streaming_bridge: patched BaseAnthropicMessagesStreamingIterator.__init__"
        " to use logging_obj.start_time"
    )


# Apply the __init__ patch at import time so every subsequent
# /v1/messages request uses the corrected start_time.
_patch_base_anthropic_streaming_iterator()


class StreamingBridge(CustomLogger):
    """SSE heartbeat + TTFT stamping for anthropic_messages streams."""

    def __init__(self) -> None:
        super().__init__()
        self._canary_aliases: Set[str] = _load_canary_aliases()
        self._canary_prefixes: Tuple[str, ...] = _load_canary_prefixes()
        self._heartbeat_seconds: float = _load_heartbeat_seconds()

    def _alias_matches(self, key_alias: Optional[str]) -> bool:
        if not isinstance(key_alias, str):
            return False
        if key_alias in self._canary_aliases:
            return True
        if self._canary_prefixes and key_alias.startswith(self._canary_prefixes):
            return True
        return False

    def _should_bridge(
        self, user_api_key_dict: Any, request_data: Any
    ) -> Optional[Any]:
        """Return the logging_obj iff this request should be bridged.

        Returning None means "pass the upstream iterator through untouched".
        """
        try:
            key_alias = getattr(user_api_key_dict, "key_alias", None)
            if not self._alias_matches(key_alias):
                return None

            if not isinstance(request_data, dict):
                return None

            logging_obj = request_data.get("litellm_logging_obj")
            if logging_obj is None:
                return None

            if getattr(logging_obj, "call_type", None) != _TARGET_CALL_TYPE:
                return None

            return logging_obj
        except Exception:
            return None

    @staticmethod
    def _stamp_completion_start(logging_obj: Any) -> None:
        try:
            logging_obj._update_completion_start_time(
                datetime.datetime.now()
            )
        except Exception as exc:
            try:
                litellm.print_verbose(
                    f"[streaming_bridge] TTFT stamp failed: {exc!r}"
                )
            except Exception:
                pass

    async def async_post_call_streaming_iterator_hook(
        self,
        user_api_key_dict: Any,
        response: AsyncIterator[Any],
        request_data: dict,
    ) -> AsyncIterator[Any]:
        logging_obj = self._should_bridge(user_api_key_dict, request_data)
        if logging_obj is None:
            async for chunk in response:
                yield chunk
            return

        # Prefetcher pushes upstream chunks into a queue; the main loop
        # reads with wait_for so we can inject heartbeats on silence.
        queue: asyncio.Queue = asyncio.Queue()
        eof_sentinel = object()
        err_sentinel = object()

        async def _prefetch() -> None:
            try:
                async for chunk in response:
                    await queue.put(chunk)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await queue.put((err_sentinel, exc))
            finally:
                await queue.put(eof_sentinel)

        task = asyncio.create_task(_prefetch())
        # Scan buffer for first `content_block_delta` marker across chunk
        # boundaries (httpx may split a single SSE frame across TCP reads).
        # We keep up to ``_SCAN_BUFFER_LIMIT`` bytes and slide the window
        # so that a marker split exactly at the edge is still detected.
        scan_buf = bytearray()
        marker_len = len(_CONTENT_DELTA_MARKER)
        first_content_seen = False

        try:
            while True:
                try:
                    item = await asyncio.wait_for(
                        queue.get(), timeout=self._heartbeat_seconds
                    )
                except asyncio.TimeoutError:
                    # Upstream has been silent long enough that Cloudflare
                    # would soon 524. Inject an SSE comment — clients
                    # ignore it, Cloudflare sees activity.
                    yield _HEARTBEAT_CHUNK
                    continue

                if item is eof_sentinel:
                    return
                if (
                    isinstance(item, tuple)
                    and len(item) == 2
                    and item[0] is err_sentinel
                ):
                    raise item[1]

                if not first_content_seen:
                    view = _chunk_bytes_view(item)
                    if view is not None:
                        scan_buf.extend(view)
                        if _CONTENT_DELTA_MARKER in scan_buf:
                            first_content_seen = True
                            self._stamp_completion_start(logging_obj)
                            scan_buf = bytearray()  # free memory
                        elif len(scan_buf) > _SCAN_BUFFER_LIMIT:
                            # Retain only the tail so a marker straddling
                            # the boundary is still findable, but stop
                            # the buffer from growing unboundedly.
                            del scan_buf[: -(marker_len - 1)]
                yield item
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass


streaming_bridge = StreamingBridge()
