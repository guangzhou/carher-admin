"""
LiteLLM streaming bridge — SSE heartbeat + TTFT fix + httpx
read-timeout patch + progress watchdog + mid-stream stall recovery
for the ``anthropic_messages`` (/v1/messages) passthrough path.

Five independent problems this module addresses
-----------------------------------------------

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

4.  **600 s "fake-success" hangs after upstream Wangsu cheliantianxia
    network gateway stalls mid-stream.**

    LiteLLM's ``async_anthropic_messages_handler`` (used for the
    passthrough ``/v1/messages`` path) calls ``httpx.AsyncClient.post()``
    without ever passing the configured ``request_timeout`` /
    ``stream_timeout`` value. The httpx client is therefore
    constructed with its hardcoded default ``Timeout(read=600)``, which
    only fires after the connection has been silent for 600 wall-clock
    seconds. Combined with the heartbeat patch above (which keeps
    Cloudflare alive indefinitely), upstream Wangsu cheliantianxia
    stalls produced ``LiteLLM_SpendLogs`` rows with
    ``duration ≈ 600 s``, ``status = success``, ``output_tokens ≤ 18``
    -- "fake successes" where the client received only whitespace
    heartbeats and ultimately raised
    ``API returned an empty or malformed response (HTTP 200)``.

    Confirmed root cause via direct httpx introspection:
    ``litellm.LlmProviders.ANTHROPIC`` async httpx client has
    ``timeout = Timeout(connect=5, read=600, write=600, pool=600)``.
    OpenAI / OpenRouter clients have the same default; they simply do
    not exhibit the bug in production because their endpoints do not
    stall mid-stream.

5.  **Ping-only stalls bypass the httpx read-timeout entirely.**

    The httpx patch above (problem 4) catches the case where the
    upstream socket goes completely silent. But Wangsu's gateway
    keeps the socket alive during a stalled prefill by sending
    ``event: ping`` frames every ~10 s -- those bytes reset the
    httpx read clock without representing any actual generation
    progress. Production data (24 h, 27 prefill stalls all dragged
    out to ~600 s) confirmed the httpx 120 s patch never fired in
    these cases.

    Mid-stream stalls also bypass LiteLLM's router fallback chain,
    because ``acompletion()`` returned successfully the moment the
    upstream emitted HTTP 200 + headers; the router has already
    handed control to the iterator and nothing in the
    ``router_settings.fallbacks`` path will ever run.

Fix
---

This module does three things at import time + two things per request:

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

*   **(patch, global, env-gated)** Tightens the per-chunk read timeout
    on the cached ``litellm.LlmProviders.ANTHROPIC`` async httpx
    client from the hardcoded 600 s default to a configurable value
    (default 120 s when the env var
    ``STREAMING_BRIDGE_HTTPX_READ_TIMEOUT_SECONDS`` is set). Also
    wraps ``litellm.llms.custom_httpx.http_handler.get_async_httpx_client``
    so any future cache miss returns a tightened client. Only the
    Anthropic provider is touched -- OpenAI / OpenRouter / Vertex
    clients keep their 600 s defaults.

    With this patch in effect, an upstream stall surfaces as
    ``httpx.ReadTimeout`` after the configured interval instead of
    600 s. Pre-headers stalls then trigger LiteLLM Router's normal
    retry + fallback machinery; mid-stream stalls bubble up into
    this iterator hook, which converts them into a clean Anthropic
    error frame so the client SDK can retry.

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
    * If the upstream raises ``httpx.ReadTimeout`` (mid-stream stall
      surfaced by the global httpx patch), pass-through mode emits a
      synthetic ``event: error`` (``overloaded_error``) +
      ``event: message_stop`` SSE pair so the Anthropic SDK raises a
      clean ``APIError`` and clients with retry logic (claude-code CLI,
      Cursor) recover automatically. Collapse mode emits a synthetic
      Anthropic ``Message`` JSON whose ``content[0].text`` explains
      the stall (Pydantic-validated against ``anthropic.types.Message``
      in pre-deployment mock testing) so non-streaming clients see a
      parseable response instead of an
      ``APIResponseValidationError``.
    * **Progress watchdog** runs alongside the heartbeat. Two
      data-driven thresholds (default 120 s pre-message_start, 60 s
      post-message_start) bound how long upstream may stay silent on
      real progress events (everything except ``event: ping``). When
      tripped, the watchdog emits the same synthetic error frame /
      synthetic Message that the httpx-timeout path uses, so the
      client experience is identical regardless of whether the stall
      is "complete socket silence" (caught by httpx) or "ping-only
      keepalives drag out forever" (caught by watchdog). Thresholds
      tuned against 24 h production TTFT distributions; see
      ``_load_progress_thresholds`` and the comments next to the
      defaults for the data and reasoning. Per-arm kill switch via
      ``STREAMING_BRIDGE_PROGRESS_TIMEOUT_PRE_SECONDS`` /
      ``..._POST_SECONDS=999999`` (env-only; no pod restart needed
      beyond the next config rollout).

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
import json
import logging as _stdlib_logging
import os
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional, Set, Tuple

import httpx

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


# --------------------------- progress watchdog ------------------------
#
# Mid-stream stalls bypass LiteLLM's router fallback because acompletion()
# returns successfully the moment the upstream emits HTTP 200 + headers.
# Wangsu's gateway responds with HTTP 200 within ~5 s for the vast majority
# of requests; once that happens the iterator owns the connection and any
# further upstream silence (ping-only frames, or true silence under the
# httpx read-timeout patch's threshold) drags out to the client SDK's
# 600 s default timeout. The httpx-timeout patch above only fires on
# *complete* socket silence, which Wangsu avoids by sending periodic
# `event: ping` frames during prefill -- those bytes reset the
# socket-level read clock but carry zero generation progress.
#
# The watchdog detects "no real progress" by scanning the upstream byte
# stream for SSE event markers other than ping (per Anthropic's stream
# spec, ping is heartbeat, every other event carries actual state). It
# uses two thresholds tuned to 24 h production data:
#
# Pre-message_start (TTFT-equivalent window):
#     TTFT p99 across all input-token buckets ≤ 38 s, max 220 s.
#     120 s catches 99.5%+ of legitimate prefills; the 0.5% tail
#     between p99 and max gets routed through router fallback (which
#     adds ~30 s recovery latency, vs. 600 s of dead-wait without it).
#
# Post-message_start (generation phase):
#     content_block_delta / message_delta arrive token-by-token at
#     50-100 events/s during normal generation, including thinking blocks
#     (thinking_delta is a content_block_delta). 60 s of silence is
#     unambiguously a stall.
#
# False positive cost: per-request ~30 s extra latency due to fallback +
# SDK retry. False negative cost (status quo): 600 s dead-wait. Net
# expected effect on production: ~30 prefill stalls/day saved at cost of
# <30 false-positive retries/day in the >150k-input bucket.
_DEFAULT_PROGRESS_PRE_SECONDS: float = 120.0
_DEFAULT_PROGRESS_POST_SECONDS: float = 60.0

# SSE event markers that count as "upstream made real progress". Anthropic
# spec event names: message_start, message_delta, message_stop,
# content_block_start, content_block_delta, content_block_stop, error.
# Explicitly excluded: `event: ping` (keepalive only). We scan as raw
# bytes since the iterator hook receives un-decoded chunks; this avoids
# decoding overhead on the hot path.
_PROGRESS_MARKERS: Tuple[bytes, ...] = (
    b"event: message_start",
    b"event: message_delta",
    b"event: message_stop",
    b"event: content_block_start",
    b"event: content_block_delta",
    b"event: content_block_stop",
    b"event: error",
)
_MESSAGE_START_MARKER: bytes = b"event: message_start"


# --------------------------- collapse mode ----------------------------
#
# When ``force_stream`` flipped ``data["stream"]=True`` for a request
# whose client originally did NOT ask for streaming, we are in collapse
# mode: upstream sends SSE, but the client expects a single JSON body.
#
# In this mode the bridge:
#   - yields a single space (b" ") every HEARTBEAT_INTERVAL_SECONDS to
#     keep Cloudflare's idle timer alive. JSON parsers ignore leading
#     whitespace so the eventual JSON body still parses cleanly.
#   - buffers all upstream SSE bytes
#   - on EOF, parses the accumulated SSE events and reassembles them
#     into the single Anthropic Message JSON object that a non-streaming
#     call would have returned, then yields that as the final chunk.
#
# Output bytes go through ``return_sse_chunk`` and
# ``_process_chunk_with_cost_injection`` in
# ``async_streaming_data_generator``; both are no-ops for raw bytes
# that aren't shaped like SSE frames, so our whitespace and final JSON
# bytes pass through unmodified.

# Single-space heartbeat. This is the key trick of collapse mode:
# (a) JSON's grammar tolerates arbitrary leading whitespace,
#     so a stream like ``b"   {...}"`` parses identically to ``b"{...}"``
# (b) Cloudflare sees activity on the wire and resets its idle timer.
_COLLAPSE_HEARTBEAT_CHUNK: bytes = b" "


def _request_data_marks_collapse(request_data: Any) -> bool:
    """Was this request flipped to streaming by ``force_stream``?

    The flag travels via ``request_data["litellm_metadata"]
    ["_force_stream_collapse"]``. Returns False on any access error so
    a malformed metadata blob never crashes the iterator hook.
    """
    try:
        if not isinstance(request_data, dict):
            return False
        md = request_data.get("litellm_metadata")
        if not isinstance(md, dict):
            return False
        return bool(md.get("_force_stream_collapse"))
    except Exception:
        return False


def _parse_anthropic_sse_events(buf: bytes) -> List[Dict[str, Any]]:
    """Parse a raw Anthropic SSE byte buffer into a list of event dicts.

    Each Anthropic SSE event has the shape::

        event: <event_name>
        data: <json>

    separated by a blank line. A single TCP chunk often contains many
    events. The ``data`` JSON's ``"type"`` field also names the event,
    so for our reassembly we ignore the ``event:`` line and key off the
    parsed JSON's ``type``.

    Malformed events are skipped, not raised — partial / truncated
    upstream responses still produce as much output as we can.
    """
    events: List[Dict[str, Any]] = []
    if not buf:
        return events
    # SSE event boundary is a blank line. Normalize CRLF first.
    normalized = buf.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    for raw_event in normalized.split(b"\n\n"):
        raw_event = raw_event.strip()
        if not raw_event:
            continue
        data_lines: List[bytes] = []
        for line in raw_event.split(b"\n"):
            if line.startswith(b"data:"):
                data_lines.append(line[len(b"data:") :].lstrip())
            elif line.startswith(b"data: "):  # belt-and-suspenders
                data_lines.append(line[len(b"data: ") :])
        if not data_lines:
            continue
        try:
            payload = json.loads(b"\n".join(data_lines).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def _reassemble_anthropic_message(
    events: List[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """Walk parsed SSE events to rebuild the single Message JSON that
    a non-streaming Anthropic /v1/messages call would have returned.

    Anthropic's streaming protocol splits a Message into:

        message_start         -> seeds the message envelope (id, model,
                                 role, usage.input_tokens, empty content)
        content_block_start   -> initializes content[index] with a typed
                                 stub ({"type": "text", "text": ""},
                                 {"type": "tool_use", "input": {} from
                                 partial_json}, {"type": "thinking", ...})
        content_block_delta   -> incrementally fills the block
                                 (text_delta, thinking_delta,
                                 input_json_delta, signature_delta)
        content_block_stop    -> finalizes the block (for tool_use we
                                 must JSON-decode the accumulated
                                 partial_json into ``input``)
        message_delta         -> stop_reason, stop_sequence, usage
                                 deltas (output_tokens etc.)
        message_stop          -> end of message
        ping                  -> ignored
        error                 -> upstream error mid-stream; we surface
                                 it as a JSON error response so the
                                 client's non-streaming path can raise

    Returns ``None`` if no ``message_start`` was ever seen — caller
    surfaces that as an explicit error to the client.
    """
    msg: Optional[Dict[str, Any]] = None
    # Per-index accumulator for partial_json (tool_use input)
    partial_inputs: Dict[int, str] = {}

    for ev in events:
        ev_type = ev.get("type")

        if ev_type == "error":
            # Return Anthropic-shaped error envelope. Their non-streaming
            # error format matches this exactly.
            err_body = ev.get("error", ev)
            return {"type": "error", "error": err_body}

        if ev_type == "message_start":
            inner = ev.get("message")
            if isinstance(inner, dict):
                msg = dict(inner)
                if not isinstance(msg.get("content"), list):
                    msg["content"] = []
            continue

        if msg is None:
            # Got a non-message_start event before message_start. Skip.
            continue

        if ev_type == "content_block_start":
            idx = ev.get("index", 0)
            block = ev.get("content_block")
            if not isinstance(block, dict):
                continue
            block = dict(block)
            content_list = msg.setdefault("content", [])
            while len(content_list) <= idx:
                content_list.append({})
            content_list[idx] = block
            if block.get("type") == "tool_use":
                # tool_use blocks accumulate partial_json deltas; the
                # ``input`` field is filled at content_block_stop.
                partial_inputs[idx] = ""
                block.setdefault("input", {})
            continue

        if ev_type == "content_block_delta":
            idx = ev.get("index", 0)
            delta = ev.get("delta") or {}
            content_list = msg.get("content") or []
            if not (0 <= idx < len(content_list)):
                continue
            block = content_list[idx]
            if not isinstance(block, dict):
                continue
            d_type = delta.get("type")
            if d_type == "text_delta":
                block["text"] = (block.get("text") or "") + (delta.get("text") or "")
            elif d_type == "thinking_delta":
                block["thinking"] = (block.get("thinking") or "") + (
                    delta.get("thinking") or ""
                )
            elif d_type == "signature_delta":
                # signatures are sent whole, but be defensive about
                # multiple deltas just in case.
                sig = delta.get("signature") or ""
                block["signature"] = (block.get("signature") or "") + sig
            elif d_type == "input_json_delta":
                partial_inputs[idx] = partial_inputs.get(idx, "") + (
                    delta.get("partial_json") or ""
                )
            continue

        if ev_type == "content_block_stop":
            idx = ev.get("index", 0)
            if idx in partial_inputs:
                pj = partial_inputs.pop(idx)
                content_list = msg.get("content") or []
                if 0 <= idx < len(content_list) and isinstance(
                    content_list[idx], dict
                ):
                    try:
                        content_list[idx]["input"] = json.loads(pj) if pj else {}
                    except (json.JSONDecodeError, TypeError):
                        # Leave whatever we had; the client will see a
                        # malformed tool_use input but at least the rest
                        # of the message is intact.
                        content_list[idx].setdefault("input", {})
            continue

        if ev_type == "message_delta":
            delta = ev.get("delta") or {}
            if "stop_reason" in delta:
                msg["stop_reason"] = delta.get("stop_reason")
            if "stop_sequence" in delta:
                msg["stop_sequence"] = delta.get("stop_sequence")
            usage = ev.get("usage")
            if isinstance(usage, dict):
                existing = msg.setdefault("usage", {})
                if not isinstance(existing, dict):
                    existing = {}
                    msg["usage"] = existing
                existing.update(usage)
            continue

        # message_stop / ping / unknown -> no-op
    return msg


def _build_collapsed_synthetic_message(
    detail: str,
    model_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Construct a Pydantic-valid Anthropic ``Message`` that surfaces
    the error condition to the user inline.

    Background: Anthropic's Python / TypeScript SDK validates non-streaming
    response bodies against the ``Message`` model (``id``, ``role``,
    ``content``, ``stop_reason``, ``usage`` etc. are required). If we
    were to emit ``{"type": "error", "error": {...}}`` over a
    non-streaming HTTP 200 (which is what the collapsed path always
    returns -- LiteLLM has already committed to status=200 with
    ``Content-Type: text/event-stream`` by the time we yield), Pydantic
    rejects it and the SDK raises ``APIResponseValidationError`` with a
    confusing schema dump instead of a readable message.

    This synthesizer instead emits a real ``Message`` whose ``content[0]``
    text describes the failure. Shape-validated against
    ``anthropic.types.Message.model_validate`` in pre-deployment mock
    tests. ``stop_reason="end_turn"`` is the canonical "complete, no
    error" terminator; choosing it (vs. ``"max_tokens"`` etc.) avoids
    triggering any client-side retry-on-truncation heuristics.
    """
    return {
        "id": f"msg_carher_proxy_{uuid.uuid4().hex[:12]}",
        "type": "message",
        "role": "assistant",
        "content": [
            {
                "type": "text",
                "text": f"[CarHer LiteLLM] {detail}",
            }
        ],
        "model": model_name or "anthropic.claude",
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }


def _is_upstream_stall_exception(exc: BaseException) -> bool:
    """Return True iff ``exc`` indicates an upstream mid-stream stall
    that should be surfaced as a clean Anthropic error frame rather
    than re-raised.

    The httpx-timeout patch turns a stalled upstream connection into
    ``httpx.ReadTimeout`` after the configured interval. A subset of
    LiteLLM internal paths re-wrap the underlying ``httpx`` exceptions
    in their own classes whose names contain ``Timeout``; we accept
    both shapes to be defensive against version drift.
    """
    if isinstance(exc, httpx.ReadTimeout):
        return True
    if isinstance(exc, httpx.ConnectTimeout):
        return True
    if isinstance(exc, asyncio.TimeoutError):
        return True
    cls_name = type(exc).__name__.lower()
    return "timeout" in cls_name


def _build_passthrough_stall_sse_frame() -> bytes:
    """Synthetic Anthropic SSE frame to terminate a stalled stream cleanly.

    Emits ``event: error`` (with Anthropic's ``overloaded_error`` type
    so SDKs that special-case throttling treat it as retryable) followed
    by ``event: message_stop`` so the SDK's stream parser cleanly
    resolves and the underlying ``Stream`` iterator raises ``APIError``
    instead of waiting on more bytes.
    """
    err_payload = {
        "type": "error",
        "error": {
            "type": "overloaded_error",
            "message": (
                "Upstream stalled mid-stream after first byte; "
                "please retry."
            ),
        },
    }
    stop_payload = {"type": "message_stop"}
    return (
        b"event: error\ndata: "
        + json.dumps(err_payload).encode("utf-8")
        + b"\n\n"
        + b"event: message_stop\ndata: "
        + json.dumps(stop_payload).encode("utf-8")
        + b"\n\n"
    )


def _load_anthropic_read_timeout() -> Optional[float]:
    """Read the desired anthropic httpx read timeout from the environment.

    Returns ``None`` when the env var is unset / blank, signaling that
    the global httpx-timeout patch must NOT be applied (preserves the
    upstream LiteLLM default of 600 s for a clean rollback path:
    unsetting the env var returns the proxy to pre-patch behavior on
    next pod restart).

    Clamped to ``[30, 600]`` seconds:

    * ``< 30 s`` would risk false positives on ordinary long thinking
      (the longest observed gap between live wire bytes during normal
      Opus 4.7 thinking is ~28 s -- 30 s is the hard floor below which
      we'd cause regressions).
    * ``> 600 s`` makes the patch a no-op vs. the LiteLLM default, so
      anything beyond is silently capped.
    """
    raw = os.environ.get("STREAMING_BRIDGE_HTTPX_READ_TIMEOUT_SECONDS")
    if not raw:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value < 30.0:
        return 30.0
    if value > 600.0:
        return 600.0
    return value


def _patch_anthropic_httpx_timeout() -> None:
    """Tighten the per-chunk read timeout on the cached anthropic
    async httpx client and on any future cache miss.

    LiteLLM's ``async_anthropic_messages_handler`` builds upstream
    requests via ``httpx_client.post(stream=True)`` without forwarding
    the configured ``request_timeout`` / ``stream_timeout``, so the
    underlying ``httpx.AsyncClient`` falls back to its hardcoded
    ``Timeout(read=600)`` default. That 600 s default is what produced
    the observed "fake-success" rows in production: upstream Wangsu
    cheliantianxia hangs mid-stream → httpx silently waits the full 600 s
    → the response body finally closes with ``message_stop`` only ever
    relayed as our keep-alive whitespace heartbeats → the client SDK
    eventually raises ``empty or malformed response``.

    Patch strategy:

    1. Look up the cached anthropic async client (it is a singleton
       within the LiteLLM process) and overwrite ``client.timeout``
       in-place. This catches the case where the client was
       constructed before this module imported, which happens during
       LiteLLM's normal startup sequence.
    2. Wrap ``http_handler.get_async_httpx_client`` so any future cache
       miss (rare; happens only if the singleton is evicted or a new
       provider variant is registered) returns a tightened client.

    Idempotent via the ``_streaming_bridge_patched`` attribute on the
    wrapped function. Failing open: any error in the patch logs a
    warning and leaves the client at its 600 s default.
    """
    new_read = _load_anthropic_read_timeout()
    if new_read is None:
        _log.warning(
            "streaming_bridge: anthropic httpx timeout patch DISABLED "
            "(STREAMING_BRIDGE_HTTPX_READ_TIMEOUT_SECONDS unset)"
        )
        return

    try:
        from litellm.llms.custom_httpx import http_handler as _hh
    except Exception as exc:
        _log.error(
            "streaming_bridge: cannot import http_handler for httpx patch: %r",
            exc,
        )
        return

    new_timeout = httpx.Timeout(
        connect=10.0,
        read=new_read,
        write=30.0,
        pool=600.0,
    )

    try:
        cached = _hh.get_async_httpx_client(litellm.LlmProviders.ANTHROPIC)
        cached.timeout = new_timeout
        cached.client.timeout = new_timeout
    except Exception as exc:
        _log.warning(
            "streaming_bridge: cannot tighten cached anthropic client timeout: %r",
            exc,
        )

    if getattr(_hh.get_async_httpx_client, "_streaming_bridge_patched", False):
        _log.warning(
            "streaming_bridge: get_async_httpx_client already patched; "
            "anthropic timeout=read %ss",
            new_read,
        )
        return

    _orig_get = _hh.get_async_httpx_client

    def _patched_get(*args, **kwargs):  # type: ignore[no-untyped-def]
        client = _orig_get(*args, **kwargs)
        # Determine the provider regardless of how the caller passed it.
        # LiteLLM's signature is
        # ``get_async_httpx_client(llm_provider, params=None)``; some call
        # sites use the kwarg form, others positional. Don't enforce a
        # signature here -- forward args verbatim and inspect.
        provider = kwargs.get("llm_provider")
        if provider is None and args:
            provider = args[0]
        if provider == litellm.LlmProviders.ANTHROPIC:
            try:
                client.timeout = new_timeout
                client.client.timeout = new_timeout
            except Exception:
                pass
        return client

    _patched_get._streaming_bridge_patched = True  # type: ignore[attr-defined]
    _hh.get_async_httpx_client = _patched_get
    _log.warning(
        "streaming_bridge: patched anthropic httpx client timeout "
        "(read=%ss; was hardcoded 600s in LiteLLM upstream)",
        new_read,
    )


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


def _load_progress_thresholds() -> Tuple[float, float]:
    """Read pre/post-message_start watchdog thresholds from env.

    Setting either to a very large value (e.g. 999999) effectively
    disables that arm of the watchdog without removing the code -- this
    is the kill switch for fast rollback if the watchdog turns out to
    cause regressions.
    """
    def _read(name: str, default: float, lo: float, hi: float = 600.0) -> float:
        raw = os.environ.get(name)
        if not raw:
            return default
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return default
        if value < lo:
            return lo
        if value > hi:
            return hi
        return value

    pre = _read(
        "STREAMING_BRIDGE_PROGRESS_TIMEOUT_PRE_SECONDS",
        _DEFAULT_PROGRESS_PRE_SECONDS,
        lo=30.0,
        hi=999999.0,
    )
    post = _read(
        "STREAMING_BRIDGE_PROGRESS_TIMEOUT_POST_SECONDS",
        _DEFAULT_PROGRESS_POST_SECONDS,
        lo=15.0,
        hi=999999.0,
    )
    return pre, post


def _scan_progress_marker(view: bytes) -> Tuple[bool, bool]:
    """Scan ``view`` for any SSE event marker that signals real upstream
    progress (anything except ``event: ping``).

    A single TCP read often contains multiple SSE events; checking via
    ``bytes.__contains__`` is O(n*m) but n is bounded by the chunk size
    (httpx default 16 KB) and m is small (we have 7 markers), so this
    is well below 1 ms per chunk in practice and runs only when a chunk
    actually arrives -- not on the heartbeat tick.

    Returns ``(any_progress, saw_message_start)``. ``saw_message_start``
    is the signal that flips the watchdog from the pre-threshold (TTFT
    headroom) to the post-threshold (generation-phase tolerance).
    """
    saw_msg_start = _MESSAGE_START_MARKER in view
    if saw_msg_start:
        return True, True
    for marker in _PROGRESS_MARKERS:
        if marker in view:
            return True, False
    return False, False


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
    _log.warning(
        "streaming_bridge: patched BaseAnthropicMessagesStreamingIterator.__init__"
        " to use logging_obj.start_time"
    )


# Apply the __init__ patch at import time so every subsequent
# /v1/messages request uses the corrected start_time.
_patch_base_anthropic_streaming_iterator()

# Apply the anthropic httpx read-timeout patch at import time so the
# very next /v1/messages call uses the tightened timeout. Env-gated;
# unsetting STREAMING_BRIDGE_HTTPX_READ_TIMEOUT_SECONDS reverts to
# LiteLLM's 600 s default on next pod restart (clean rollback).
_patch_anthropic_httpx_timeout()


class StreamingBridge(CustomLogger):
    """SSE heartbeat + TTFT stamping + progress watchdog for
    anthropic_messages streams."""

    def __init__(self) -> None:
        super().__init__()
        self._canary_aliases: Set[str] = _load_canary_aliases()
        self._canary_prefixes: Tuple[str, ...] = _load_canary_prefixes()
        self._heartbeat_seconds: float = _load_heartbeat_seconds()
        pre, post = _load_progress_thresholds()
        self._progress_timeout_pre: float = pre
        self._progress_timeout_post: float = post

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

        collapse_mode = _request_data_marks_collapse(request_data)

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

        # ---------- collapse mode ----------
        # Buffer everything from upstream; never relay SSE bytes to the
        # client. Yield a single space (b" ") on a wall-clock cadence
        # so the client TCP connection always sees activity, even when
        # upstream is busy emitting SSE events that we are silently
        # buffering. JSON parsers ignore leading whitespace so the
        # eventual JSON body still parses cleanly.
        #
        # Critical detail: the heartbeat MUST fire on wall-clock
        # cadence, not on "no upstream chunk in N seconds". If we keyed
        # off upstream silence, a chatty SSE stream (ping every 1 s,
        # content_block_delta every 200 ms) would buffer everything
        # internally for 100+ s without yielding anything to the
        # client, and Cloudflare would 524 the connection. By
        # tracking time-since-last-yield-to-client we guarantee the
        # client wire sees a byte at least every ``_heartbeat_seconds``
        # regardless of how busy upstream is.
        if collapse_mode:
            collapse_buf = bytearray()
            stamped = False
            loop = asyncio.get_event_loop()
            last_yield_at: float = loop.time()
            # Watchdog state mirrors the pass-through path.
            last_progress_at: float = loop.time()
            seen_message_start: bool = False
            upstream_done = False
            upstream_error: Optional[BaseException] = None

            async def _drain_one(timeout: float) -> bool:
                """Pull one item from the queue with a timeout.

                Returns True if EOF was reached, False if a chunk was
                buffered, raises asyncio.TimeoutError on timeout.

                Also updates the enclosing watchdog state
                (``last_progress_at`` / ``seen_message_start``) on real
                upstream events so the collapse path can detect a stall
                even though it never relays SSE bytes to the client.
                """
                nonlocal stamped, last_progress_at, seen_message_start
                item = await asyncio.wait_for(queue.get(), timeout=timeout)
                if item is eof_sentinel:
                    return True
                if (
                    isinstance(item, tuple)
                    and len(item) == 2
                    and item[0] is err_sentinel
                ):
                    raise item[1]
                view = _chunk_bytes_view(item)
                if view is not None:
                    collapse_buf.extend(view)
                    any_progress, saw_msg_start = _scan_progress_marker(view)
                    if any_progress:
                        last_progress_at = loop.time()
                    if saw_msg_start and not seen_message_start:
                        seen_message_start = True
                    if (
                        not stamped
                        and _CONTENT_DELTA_MARKER in collapse_buf
                        and not getattr(
                            logging_obj, "completion_start_time", None
                        )
                    ):
                        self._stamp_completion_start(logging_obj)
                        stamped = True
                return False

            try:
                while not upstream_done:
                    now = loop.time()
                    heartbeat_remaining = (
                        self._heartbeat_seconds - (now - last_yield_at)
                    )
                    if heartbeat_remaining <= 0:
                        # Heartbeat is overdue. Yield right now and
                        # reset the wall-clock timer.
                        yield _COLLAPSE_HEARTBEAT_CHUNK
                        last_yield_at = loop.time()
                        continue

                    # Watchdog deadline: if upstream goes too long
                    # without a non-ping SSE event, trip a synthetic
                    # stall and bail out of collapse mode with a
                    # parseable Message body explaining the failure.
                    threshold = (
                        self._progress_timeout_post
                        if seen_message_start
                        else self._progress_timeout_pre
                    )
                    stall_remaining = (last_progress_at + threshold) - now
                    if stall_remaining <= 0:
                        try:
                            _log.warning(
                                "streaming_bridge: collapse watchdog stall "
                                "(seen_message_start=%s, threshold=%ss); "
                                "yielding synthetic Message",
                                seen_message_start, threshold,
                            )
                        except Exception:
                            pass
                        msg = _build_collapsed_synthetic_message(
                            "Upstream stalled with only keepalive frames "
                            "and no progress events; please retry."
                        )
                        yield json.dumps(msg, ensure_ascii=False).encode("utf-8")
                        return

                    wait_seconds = max(
                        0.05, min(heartbeat_remaining, stall_remaining)
                    )

                    try:
                        upstream_done = await _drain_one(timeout=wait_seconds)
                    except asyncio.TimeoutError:
                        # Either heartbeat or watchdog deadline tick.
                        # We yield a heartbeat unconditionally — the
                        # next iteration will re-evaluate the watchdog
                        # against the (now refreshed) clock.
                        yield _COLLAPSE_HEARTBEAT_CHUNK
                        last_yield_at = loop.time()
                    except Exception as exc:
                        upstream_error = exc
                        break

                if upstream_error is not None:
                    # Upstream errored mid-stream. Anthropic SDK validates
                    # the non-streaming response body against the Message
                    # Pydantic model, so emitting a raw error envelope
                    # would surface as APIResponseValidationError on the
                    # client. Yield a synthetic Message instead -- the
                    # client sees a parseable response whose text body
                    # explains the failure, which is strictly better UX
                    # than an opaque schema dump.
                    if _is_upstream_stall_exception(upstream_error):
                        detail = (
                            "Upstream stalled mid-stream after first byte "
                            "(httpx read timeout); please retry."
                        )
                    else:
                        detail = (
                            f"upstream stream interrupted: "
                            f"{type(upstream_error).__name__}"
                        )
                    msg = _build_collapsed_synthetic_message(detail)
                    yield json.dumps(msg, ensure_ascii=False).encode("utf-8")
                    return

                # Upstream closed cleanly. Reassemble + emit one JSON.
                events = _parse_anthropic_sse_events(bytes(collapse_buf))
                msg = _reassemble_anthropic_message(events)
                if msg is None or msg.get("type") != "message":
                    # Either no message_start was ever produced, or the
                    # upstream emitted only an `error` event mid-stream
                    # (which _reassemble surfaces as type=error). In both
                    # cases emit a synthetic Message so the client SDK
                    # parses the body cleanly.
                    msg = _build_collapsed_synthetic_message(
                        "Upstream did not produce a complete message; "
                        "please retry.",
                    )
                yield json.dumps(msg, ensure_ascii=False).encode("utf-8")
                return
            finally:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass

        # ---------- pass-through mode ----------
        # Scan buffer for first `content_block_delta` marker across chunk
        # boundaries (httpx may split a single SSE frame across TCP reads).
        # We keep up to ``_SCAN_BUFFER_LIMIT`` bytes and slide the window
        # so that a marker split exactly at the edge is still detected.
        scan_buf = bytearray()
        marker_len = len(_CONTENT_DELTA_MARKER)
        first_content_seen = False

        # Progress watchdog state. ``last_progress_at`` advances every
        # time we see a non-ping SSE event from upstream; it does NOT
        # advance on heartbeats we inject ourselves (those are bytes we
        # produced, not upstream progress). ``seen_message_start`` flips
        # the watchdog from the generous pre-prefill threshold to the
        # tight generation-phase threshold.
        loop = asyncio.get_event_loop()
        last_progress_at: float = loop.time()
        seen_message_start: bool = False

        try:
            while True:
                # Compute deadlines for this iteration. The watchdog
                # threshold is selected by message_start state; the loop
                # waits for the sooner of (heartbeat tick, watchdog
                # deadline). Heartbeat refreshes Cloudflare; watchdog
                # detects upstream stall.
                threshold = (
                    self._progress_timeout_post
                    if seen_message_start
                    else self._progress_timeout_pre
                )
                now = loop.time()
                stall_remaining = (last_progress_at + threshold) - now
                if stall_remaining <= 0:
                    # Upstream has emitted no non-ping SSE event for
                    # ``threshold`` seconds. Convert this mid-stream
                    # stall into a clean synthetic error frame so the
                    # SDK raises ``APIError`` immediately and any
                    # retry-on-overloaded client (Anthropic SDK,
                    # claude-code CLI, Cursor) recovers automatically.
                    # Without this, the request drags out to the
                    # client's own ~600 s read timeout for no benefit.
                    try:
                        _log.warning(
                            "streaming_bridge: watchdog stall "
                            "(seen_message_start=%s, threshold=%ss); "
                            "emitting synthetic error frame",
                            seen_message_start, threshold,
                        )
                    except Exception:
                        pass
                    try:
                        yield _build_passthrough_stall_sse_frame()
                    except Exception:
                        pass
                    return

                # Always wait at least 0.05 s so we never busy-spin even
                # if rounding pushes stall_remaining to a tiny positive.
                wait_seconds = max(
                    0.05, min(self._heartbeat_seconds, stall_remaining)
                )

                try:
                    item = await asyncio.wait_for(
                        queue.get(), timeout=wait_seconds
                    )
                except asyncio.TimeoutError:
                    # Either heartbeat tick or watchdog tick fired with
                    # no upstream chunk. Send a heartbeat and loop —
                    # the watchdog re-evaluates on the next iteration.
                    yield _HEARTBEAT_CHUNK
                    continue

                if item is eof_sentinel:
                    return
                if (
                    isinstance(item, tuple)
                    and len(item) == 2
                    and item[0] is err_sentinel
                ):
                    upstream_exc = item[1]
                    # Mid-stream stalls show up here as httpx.ReadTimeout
                    # (the global httpx-timeout patch fires after the
                    # configured interval). Translating that into a clean
                    # Anthropic SSE error frame lets the client SDK raise
                    # a parseable APIError instead of silently waiting on
                    # a stream that already terminated. Clients with
                    # retry-on-overloaded behavior (claude-code CLI,
                    # Cursor, Anthropic SDK with default backoff) recover
                    # automatically.
                    if _is_upstream_stall_exception(upstream_exc):
                        try:
                            _log.warning(
                                "streaming_bridge: passthrough mid-stream stall, "
                                "emitting synthetic error frame: %r",
                                upstream_exc,
                            )
                        except Exception:
                            pass
                        try:
                            yield _build_passthrough_stall_sse_frame()
                        except Exception:
                            pass
                        return
                    # Non-stall exception (real upstream error, network
                    # reset, etc.) — surface unchanged so LiteLLM's
                    # outer handlers / spend-log writer see the real
                    # exception class.
                    raise upstream_exc

                view = _chunk_bytes_view(item)
                if view is not None:
                    # Watchdog bookkeeping: any non-ping SSE event in
                    # this chunk resets the stall clock. ping-only
                    # chunks intentionally do not reset, so a Wangsu
                    # gateway that only sends pings during a stalled
                    # prefill will eventually trip the watchdog.
                    any_progress, saw_msg_start = _scan_progress_marker(view)
                    if any_progress:
                        last_progress_at = loop.time()
                    if saw_msg_start and not seen_message_start:
                        seen_message_start = True

                    # TTFT stamping (independent of the watchdog).
                    if not first_content_seen:
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
