"""Regression tests for the OpenAI-style ``data: [DONE]`` SSE residue
filter in ``streaming_bridge.py``.

Background
----------
LiteLLM routes Claude Code traffic to OpenRouter via OpenRouter's
``/v1/messages`` Anthropic-compat endpoint. OpenRouter does not strictly
follow Anthropic's SSE protocol — at end-of-stream it leaks the
OpenAI-protocol terminator ``event: data\\ndata: [DONE]\\n\\n`` after
the legitimate ``message_stop`` frame. Strict Anthropic SDK clients
(``acpx``/``openclaw``) parse every ``data:`` line as JSON and abort
with::

    Could not parse Anthropic SSE event data:
    Unexpected token 'D', "[DONE]" is not valid JSON

The Claude Code official SDK silently ignores unknown SSE lines, so it
hides the bug — but the same wire bytes break stricter clients.

Design (post 2026-04-28 incident)
---------------------------------
``streaming_bridge.py`` filters ``data: [DONE]`` events on the egress
side via ``_strip_sse_done_lines(buf)``. Fast-path returns ``buf``
unchanged when the literal substring ``[DONE]`` is absent.

An earlier revision additionally maintained a 32-byte carry-over
buffer to heal ``[DONE]`` literals split across TCP chunk boundaries.
That carry-over interacted disastrously with the 25 s heartbeat
injection during long Anthropic thinking streams: the carry could
withhold the tail of an in-progress ``data:`` line; when upstream went
silent for >25 s and the bridge yielded ``: keepalive\\n\\n``, the
heartbeat bytes appended to the unterminated line on the wire and the
client SSE parser saw a corrupt ``data:`` payload (typically failing
with ``SyntaxError: Expected '}'``). The 2026-04-28 fix now buffers
only up to complete SSE event boundaries before yielding to the client.
That keeps heartbeats protocol-safe and lets the bridge strip the
OpenRouter ``event: data`` / ``data: [DONE]`` residue atomically.

This test file pins the post-fix behavior and is the regression net
if anyone later re-introduces carry-over.

Run
---

    cd k8s/litellm-callbacks/tests
    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    .venv/bin/python -m unittest test_streaming_bridge_done_filter -v
"""

from __future__ import annotations

import asyncio
import datetime
import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from typing import Any, AsyncIterator, List
from unittest.mock import MagicMock


# --------------------------------------------------------------------------
# Stub `litellm` deeply enough that streaming_bridge.py imports without a
# real LiteLLM install. The bridge's actual logic does not need real
# litellm objects for the tests below — it needs:
#   * litellm.LlmProviders.ANTHROPIC for the httpx-timeout patch's
#     conditional branch (gated off by env var in these tests).
#   * litellm.integrations.custom_logger.CustomLogger as a base class.
#   * litellm.llms.custom_httpx.http_handler.get_async_httpx_client to
#     return a mock client when the bridge wraps it (also gated off).
#   * BaseAnthropicMessagesStreamingIterator class to monkey-patch.
# Setting the env vars below disables the patches that would otherwise
# look up real LiteLLM internals at import time.
# --------------------------------------------------------------------------


class _LlmProvidersStub:
    ANTHROPIC = "anthropic"


def _install_litellm_stubs() -> None:
    s = types.ModuleType("litellm")
    s.LlmProviders = _LlmProvidersStub
    s.print_verbose = lambda *a, **kw: None
    sys.modules["litellm"] = s

    integ = types.ModuleType("litellm.integrations")
    cl = types.ModuleType("litellm.integrations.custom_logger")

    class _CL:
        def __init__(self) -> None: ...

    cl.CustomLogger = _CL
    sys.modules["litellm.integrations"] = integ
    sys.modules["litellm.integrations.custom_logger"] = cl

    llms = types.ModuleType("litellm.llms")
    cx = types.ModuleType("litellm.llms.custom_httpx")
    hh = types.ModuleType("litellm.llms.custom_httpx.http_handler")

    def _gah(*args: Any, **kwargs: Any) -> Any:
        c = MagicMock()
        c.timeout = MagicMock()
        return c

    hh.get_async_httpx_client = _gah
    sys.modules["litellm.llms"] = llms
    sys.modules["litellm.llms.custom_httpx"] = cx
    sys.modules["litellm.llms.custom_httpx.http_handler"] = hh

    a = types.ModuleType("litellm.llms.anthropic")
    e = types.ModuleType("litellm.llms.anthropic.experimental_pass_through")
    em = types.ModuleType(
        "litellm.llms.anthropic.experimental_pass_through.messages"
    )
    si = types.ModuleType(
        "litellm.llms.anthropic.experimental_pass_through.messages."
        "streaming_iterator"
    )

    class _Base:
        def __init__(self, lo: Any, rb: Any) -> None:
            self.litellm_logging_obj = lo
            self.request_body = rb
            self.start_time = datetime.datetime.now()

    si.BaseAnthropicMessagesStreamingIterator = _Base
    sys.modules["litellm.llms.anthropic"] = a
    sys.modules["litellm.llms.anthropic.experimental_pass_through"] = e
    sys.modules[
        "litellm.llms.anthropic.experimental_pass_through.messages"
    ] = em
    sys.modules[
        "litellm.llms.anthropic.experimental_pass_through.messages."
        "streaming_iterator"
    ] = si


_install_litellm_stubs()

# Configure env BEFORE importing streaming_bridge so the gate matches
# the test fixture's key alias and the optional global patches stay
# disabled (they would log warnings but otherwise are harmless).
os.environ["STREAMING_BRIDGE_KEY_PREFIXES"] = "claude-code-"
os.environ.pop("STREAMING_BRIDGE_KEY_ALIASES", None)
os.environ.pop("STREAMING_BRIDGE_HTTPX_READ_TIMEOUT_SECONDS", None)

# Disable the progress watchdog so byte-by-byte tests don't trip the
# stall detector on the synthetic asyncio.sleep(0) cadence. The
# heartbeat injector still runs but writes to a sink we strip out
# before assertions.
os.environ["STREAMING_BRIDGE_PROGRESS_TIMEOUT_PRE_SECONDS"] = "999999"
os.environ["STREAMING_BRIDGE_PROGRESS_TIMEOUT_POST_SECONDS"] = "999999"
os.environ["STREAMING_BRIDGE_HEARTBEAT_SECONDS"] = "90"


# --------------------------------------------------------------------------
# Locate the production source file relative to this test (works whether
# the test runs from repo root, the tests dir, or via `python -m
# unittest`).
# --------------------------------------------------------------------------
_THIS = Path(__file__).resolve()
_BRIDGE = _THIS.parent.parent / "streaming_bridge.py"
assert _BRIDGE.exists(), f"streaming_bridge.py not found at {_BRIDGE}"

_spec = importlib.util.spec_from_file_location("streaming_bridge", _BRIDGE)
sb = importlib.util.module_from_spec(_spec)
sys.modules["streaming_bridge"] = sb
assert _spec.loader is not None
_spec.loader.exec_module(sb)


# --------------------------------------------------------------------------
# Test fixtures
# --------------------------------------------------------------------------


class _FakeUserKey:
    def __init__(self, alias: str) -> None:
        self.key_alias = alias


class _FakeLoggingObj:
    def __init__(self) -> None:
        self.start_time = datetime.datetime.now()
        self.completion_start_time = None
        self.call_type = "anthropic_messages"

    def _update_completion_start_time(self, t: datetime.datetime) -> None:
        if self.completion_start_time is None:
            self.completion_start_time = t


async def _chunks_to_async_iter(chunks: List[bytes]) -> AsyncIterator[bytes]:
    for c in chunks:
        # Yield control between chunks so the hook's queue/wait_for
        # mechanics behave like a real streaming source.
        await asyncio.sleep(0)
        yield c


async def _chunks_with_gaps_to_async_iter(
    chunks_and_gaps: List[Any],
) -> AsyncIterator[bytes]:
    """Like ``_chunks_to_async_iter`` but supports interleaved
    ``await asyncio.sleep(seconds)`` gaps so the test can deliberately
    let the bridge's heartbeat watchdog fire between chunks.

    Items in ``chunks_and_gaps`` are either ``bytes`` (yielded as-is)
    or ``float`` (interpreted as a sleep duration in seconds).
    """
    for item in chunks_and_gaps:
        if isinstance(item, (bytes, bytearray)):
            yield bytes(item)
        else:
            await asyncio.sleep(float(item))


async def _run_hook(chunks: List[bytes]) -> bytes:
    """Drive the bridge with the given chunk sequence, return what the
    client sees (heartbeat frames stripped for assertion clarity)."""
    bridge = sb.StreamingBridge()
    out = bytearray()
    async for chunk in bridge.async_post_call_streaming_iterator_hook(
        user_api_key_dict=_FakeUserKey("claude-code-buyitian"),
        response=_chunks_to_async_iter(chunks),
        request_data={"litellm_logging_obj": _FakeLoggingObj()},
    ):
        if isinstance(chunk, (bytes, bytearray)):
            out.extend(bytes(chunk))
    return bytes(out).replace(b": keepalive\n\n", b"")


async def _run_hook_with_gaps(chunks_and_gaps: List[Any]) -> bytes:
    """Like ``_run_hook`` but the upstream iterator can sleep between
    chunks so the heartbeat path actually fires. Heartbeat frames are
    NOT stripped in the returned bytes — caller must verify them
    explicitly. This is the scenario that crashed tenggeer's VS Code
    plugin with ``Expected '}'`` pre-fix."""
    bridge = sb.StreamingBridge()
    out = bytearray()
    async for chunk in bridge.async_post_call_streaming_iterator_hook(
        user_api_key_dict=_FakeUserKey("claude-code-buyitian"),
        response=_chunks_with_gaps_to_async_iter(chunks_and_gaps),
        request_data={"litellm_logging_obj": _FakeLoggingObj()},
    ):
        if isinstance(chunk, (bytes, bytearray)):
            out.extend(bytes(chunk))
    return bytes(out)


def _parse_sse_events(buf: bytes) -> List[dict]:
    """Minimal SSE parser: split on ``\\n\\n``, accumulate ``data:`` lines
    per event, JSON-parse them. Returns a list of decoded payloads.
    Comment lines (starting with ``:``) are ignored. Raises whatever
    JSON.parse would raise — including the production failure mode.
    """
    import json as _json

    events: List[dict] = []
    for raw in buf.split(b"\n\n"):
        if not raw.strip():
            continue
        data_lines: List[bytes] = []
        for line in raw.split(b"\n"):
            if not line:
                continue
            if line.startswith(b":"):
                # SSE comment frame (heartbeats land here). Skip.
                continue
            if line.startswith(b"data:"):
                # Strip leading "data: " (with optional space).
                payload = line[5:].lstrip(b" ")
                data_lines.append(payload)
            # ``event:`` lines we don't care about for JSON validity.
        if not data_lines:
            continue
        merged = b"\n".join(data_lines)
        if merged == b"[DONE]":
            # OpenAI-style terminator; not JSON. Permitted residue.
            continue
        events.append(_json.loads(merged.decode("utf-8")))
    return events


# A complete, well-formed Anthropic SSE stream. Used as the body
# upstream emits before the OpenRouter-style ``[DONE]`` tail.
_ANTHROPIC_PREFIX: bytes = (
    b'event: message_start\n'
    b'data: {"type":"message_start","message":'
    b'{"id":"msg_01","model":"x","role":"assistant","content":[],'
    b'"usage":{"input_tokens":1}}}\n\n'
    b'event: content_block_start\n'
    b'data: {"type":"content_block_start","index":0,'
    b'"content_block":{"type":"text","text":""}}\n\n'
    b'event: ping\ndata: {"type":"ping"}\n\n'
    b'event: content_block_delta\n'
    b'data: {"type":"content_block_delta","index":0,'
    b'"delta":{"type":"text_delta","text":"hi"}}\n\n'
    b'event: content_block_stop\n'
    b'data: {"type":"content_block_stop","index":0}\n\n'
    b'event: message_delta\n'
    b'data: {"type":"message_delta",'
    b'"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}\n\n'
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)

# OpenRouter's terminator residue.
_DONE_TAIL: bytes = b"event: data\ndata: [DONE]\n\n"


_REQUIRED_EVENTS = (
    b"event: message_start",
    b"event: content_block_start",
    b"event: ping",
    b"event: content_block_delta",
    b"event: content_block_stop",
    b"event: message_delta",
    b"event: message_stop",
)


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


class StripDoneLinesUnitTests(unittest.TestCase):
    """Pure-function tests for ``_strip_sse_done_lines`` (no async)."""

    def test_normal_event_passes_through_unchanged(self) -> None:
        chunk = (
            b'event: content_block_delta\n'
            b'data: {"type":"content_block_delta","index":0,'
            b'"delta":{"type":"text_delta","text":"hello"}}\n\n'
        )
        # Identity-equal: fast path returns the same bytes object.
        self.assertIs(sb._strip_sse_done_lines(chunk), chunk)

    def test_done_with_event_data_prefix_is_stripped(self) -> None:
        chunk = b"event: data\ndata: [DONE]\n\n"
        self.assertEqual(sb._strip_sse_done_lines(chunk), b"")

    def test_done_alone_at_chunk_start_is_stripped(self) -> None:
        chunk = b"data: [DONE]\n\n"
        self.assertEqual(sb._strip_sse_done_lines(chunk), b"")

    def test_done_no_space_after_colon_is_stripped(self) -> None:
        chunk = b"data:[DONE]\n\n"
        self.assertEqual(sb._strip_sse_done_lines(chunk), b"")

    def test_message_stop_then_done_keeps_message_stop(self) -> None:
        chunk = (
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
            b"event: data\ndata: [DONE]\n\n"
        )
        self.assertEqual(
            sb._strip_sse_done_lines(chunk),
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        )

    def test_done_inside_text_delta_is_NOT_dropped(self) -> None:
        # Substring [DONE] inside a JSON string must not be matched —
        # the regex requires [DONE] to be the entire data: payload.
        chunk = (
            b'event: content_block_delta\n'
            b'data: {"type":"content_block_delta","delta":'
            b'{"text":"with [DONE] in middle"}}\n\n'
        )
        self.assertEqual(sb._strip_sse_done_lines(chunk), chunk)

    def test_crlf_line_endings_are_stripped(self) -> None:
        chunk = b"event: data\r\ndata: [DONE]\r\n\r\n"
        self.assertEqual(sb._strip_sse_done_lines(chunk), b"")


class HookScenarioTests(unittest.TestCase):
    """End-to-end scenarios driving the egress hook with realistic
    chunk sequences."""

    def _assert_anthropic_events_intact(self, out: bytes) -> None:
        for marker in _REQUIRED_EVENTS:
            self.assertIn(marker, out, f"missing {marker!r}")

    def _assert_no_done_residue(self, out: bytes) -> None:
        self.assertNotIn(b"[DONE]", out, "[DONE] leaked to client output")

    def test_single_chunk_with_done_tail(self) -> None:
        out = asyncio.run(_run_hook([_ANTHROPIC_PREFIX + _DONE_TAIL]))
        self._assert_anthropic_events_intact(out)
        self._assert_no_done_residue(out)

    def test_done_in_separate_final_chunk(self) -> None:
        out = asyncio.run(_run_hook([_ANTHROPIC_PREFIX, _DONE_TAIL]))
        self._assert_anthropic_events_intact(out)
        self._assert_no_done_residue(out)

    def test_no_done_anywhere_is_byte_identical_passthrough(self) -> None:
        out = asyncio.run(_run_hook([_ANTHROPIC_PREFIX]))
        self.assertEqual(out, _ANTHROPIC_PREFIX)

    def test_fused_events_and_done_tail_in_same_chunks(self) -> None:
        half = len(_ANTHROPIC_PREFIX) // 2
        chunks = [
            _ANTHROPIC_PREFIX[:half],
            _ANTHROPIC_PREFIX[half:] + _DONE_TAIL,
        ]
        out = asyncio.run(_run_hook(chunks))
        self._assert_anthropic_events_intact(out)
        self._assert_no_done_residue(out)


class CrossChunkSplitHealedByCompleteEventBufferTest(unittest.TestCase):
    """The 2026-04-28 fix replaced the unsafe 32-byte carry-over with
    a complete-event buffer (yield only up to the last ``\\n\\n``, hold
    the trailing partial SSE event). A welcome side-effect: cross-chunk
    splits inside ``data: [DONE]`` are now healed for free, because
    ``[DONE]`` lives inside one SSE event and the buffer reassembles
    partial events before the regex runs.

    These tests pin that property. If a future refactor regresses
    the complete-event buffer — for instance, by yielding upstream
    chunks verbatim with no buffering — these tests will fail and
    flag the regression.
    """

    def test_every_split_position_inside_done_tail_clears_residue(
        self,
    ) -> None:
        combined = _ANTHROPIC_PREFIX + _DONE_TAIL
        done_start = len(_ANTHROPIC_PREFIX)
        done_end = len(combined)

        leaks: List[int] = []
        for split in range(done_start, done_end):
            chunks = [combined[:split], combined[split:]]
            out = asyncio.run(_run_hook(chunks))
            if b"[DONE]" in out:
                leaks.append(split)

        self.assertEqual(
            leaks,
            [],
            f"{len(leaks)} split positions leak [DONE]: {leaks} — "
            "the complete-event buffer is no longer healing cross-chunk "
            "splits, which means the bridge may also be yielding "
            "mid-line bytes (re-opening the heartbeat × Expected '}' bug).",
        )

    def test_pathological_byte_fragmentation_clears_done(self) -> None:
        """Stress: split the wire into 1-, 3-, 7-byte chunks. Even at
        worst-case TCP fragmentation, the complete-event buffer must
        deliver all events to the client cleanly with no ``[DONE]``
        literal in the output (and all required Anthropic events
        intact).

        The optional ``event: data\\n`` prefix of ``_DONE_TAIL`` must
        not leak either. Strict clients may treat an event-only frame
        as an empty/malformed response, so the complete-event buffer
        strips the OpenRouter residue atomically.
        """
        wire = _ANTHROPIC_PREFIX + _DONE_TAIL
        for chunk_size in (1, 3, 7, 31, 33):
            chunks = [
                wire[i : i + chunk_size]
                for i in range(0, len(wire), chunk_size)
            ]
            out = asyncio.run(_run_hook(chunks))
            self.assertNotIn(
                b"[DONE]",
                out,
                f"chunk_size={chunk_size}: [DONE] leaked",
            )
            for ev in _REQUIRED_EVENTS:
                self.assertIn(
                    ev, out,
                    f"chunk_size={chunk_size}: missing {ev!r}",
                )
            # Anthropic prefix bytes (everything up to and including
            # message_stop) must arrive verbatim. OpenRouter's entire
            # ``event: data`` / ``data: [DONE]`` residue is removed.
            self.assertTrue(
                out.startswith(_ANTHROPIC_PREFIX),
                f"chunk_size={chunk_size}: legitimate prefix corrupted",
            )
            tail = out[len(_ANTHROPIC_PREFIX):]
            self.assertEqual(
                tail,
                b"",
                f"chunk_size={chunk_size}: unexpected residue {tail!r}",
            )


class CompleteEventBufferDeliversAllUpstreamBytesTest(unittest.TestCase):
    """Pin the post-fix invariant: every upstream byte eventually
    reaches the client (modulo [DONE] residue removal), in order,
    with no duplication. The complete-event buffer is allowed to
    *delay* bytes (hold them until an SSE event terminator arrives), but never
    drop, reorder, or duplicate them.
    """

    def test_arbitrary_mid_line_slicing_preserves_total_bytes(
        self,
    ) -> None:
        # Slice the prefix at arbitrary boundaries (some mid-line, some
        # mid-event). Each slice may end inside a ``data:`` line; the
        # buffer must hold the partial tail until the next slice
        # supplies the closing ``\n``.
        slices = [
            _ANTHROPIC_PREFIX[:10],
            _ANTHROPIC_PREFIX[10:50],
            _ANTHROPIC_PREFIX[50:120],
            _ANTHROPIC_PREFIX[120:200],
            _ANTHROPIC_PREFIX[200:],
        ]
        out = asyncio.run(_run_hook(slices))
        self.assertEqual(
            out,
            _ANTHROPIC_PREFIX,
            "bridge dropped, reordered, or duplicated upstream bytes",
        )

    def test_done_residue_in_separate_chunks_stripped_cleanly(
        self,
    ) -> None:
        # [DONE] tail in its own final chunk. Output must be the
        # prefix verbatim with the tail removed.
        slices = [
            _ANTHROPIC_PREFIX[:10],
            _ANTHROPIC_PREFIX[10:120],
            _ANTHROPIC_PREFIX[120:],
            _DONE_TAIL,
        ]
        out = asyncio.run(_run_hook(slices))
        self.assertEqual(out, _ANTHROPIC_PREFIX)


class TenggeeerExpectedBraceReproductionTest(unittest.TestCase):
    """Exact reproduction of the failure that aborted tenggeer's VS
    Code extension on 2026-04-28: a chunk whose tail lands inside an
    unterminated ``data:`` line, followed by a long upstream silence
    that fires the bridge's heartbeat injector. Pre-fix the heartbeat
    bytes would land on the unterminated line and the client SSE
    parser would JSON-parse the merged payload, raising
    ``SyntaxError: Expected '}'``. Post-fix the bridge does not
    withhold the line tail (no carry-over), so the heartbeat is
    emitted as a comment frame between SSE events and the JSON parse
    succeeds.

    This test temporarily overrides ``STREAMING_BRIDGE_HEARTBEAT_SECONDS``
    to a tiny value so the heartbeat fires on a sub-second cadence; we
    then deliberately sleep between chunks to drive multiple heartbeat
    ticks while the bridge holds (or pre-fix held) the line tail.
    """

    def setUp(self) -> None:
        # Force aggressive heartbeat cadence inside the bridge. We
        # rebuild the bridge's per-instance ``_heartbeat_seconds``
        # value via a fresh import path because the existing module
        # already snapshotted the env at import time. The simplest
        # reliable approach: monkey-patch the module-level
        # ``_load_heartbeat_seconds`` to return our test value, then
        # any new ``StreamingBridge()`` picks it up in __init__.
        self._orig = sb._load_heartbeat_seconds
        sb._load_heartbeat_seconds = lambda: 0.05  # type: ignore

    def tearDown(self) -> None:
        sb._load_heartbeat_seconds = self._orig  # type: ignore

    def test_heartbeat_during_midline_carry_does_not_corrupt_json(
        self,
    ) -> None:
        # Construct a wire that splits a content_block_delta event
        # across chunks: the first chunk ends mid-JSON-string (no
        # terminating ``\n``), then a long upstream silence (during
        # which the heartbeat will fire repeatedly), then the rest of
        # the event arrives. This is exactly the topology of the long
        # Opus thinking streams that crashed tenggeer's client.
        complete_event = (
            b'event: content_block_delta\n'
            b'data: {"type":"content_block_delta","index":0,'
            b'"delta":{"type":"text_delta","text":"hello world"}}\n\n'
        )
        # Split position chosen mid-text-value (between ``hel`` and
        # ``lo world``). The remaining JSON closes the brace.
        split = complete_event.index(b"hel") + len(b"hel")
        first_half = complete_event[:split]   # ends with ``...hel``
        second_half = complete_event[split:]  # starts with ``lo world"...``

        # Build the upstream sequence: prefix → mid-line chunk → 0.3 s
        # silence (≥ 6 heartbeat intervals at 0.05 s each) → tail.
        sequence: List[Any] = [
            _ANTHROPIC_PREFIX[: _ANTHROPIC_PREFIX.index(b"event: content_block_delta")],
            first_half,
            0.3,  # upstream silence — heartbeat fires here pre-fix
            second_half,
            _ANTHROPIC_PREFIX[
                _ANTHROPIC_PREFIX.index(b"event: content_block_stop"):
            ],
        ]

        out = asyncio.run(_run_hook_with_gaps(sequence))

        # Sanity: heartbeat actually fired (otherwise the test isn't
        # exercising the failure mode).
        self.assertIn(
            b": keepalive\n\n",
            out,
            "heartbeat never fired — test setup is broken; the bug "
            "this test reproduces requires heartbeat injection during "
            "the midline gap.",
        )

        # The actual regression check: client-side SSE parse must
        # succeed end-to-end. Pre-fix this raised
        # json.decoder.JSONDecodeError("Expecting ',' delimiter" /
        # "Expecting property name" / "Expected '}'") because the
        # heartbeat bytes corrupted the in-progress data: line.
        try:
            events = _parse_sse_events(out)
        except Exception as e:  # pragma: no cover — assertion below
            self.fail(
                f"client-side SSE parse failed (pre-fix bug regressed): "
                f"{type(e).__name__}: {e}\n\nWire bytes:\n{out!r}"
            )

        # Verify the content_block_delta event survived intact.
        deltas = [
            ev for ev in events
            if ev.get("type") == "content_block_delta"
        ]
        self.assertTrue(deltas, "content_block_delta event lost")
        self.assertEqual(
            deltas[0]["delta"]["text"],
            "hello world",
            "text_delta payload corrupted",
        )

    def test_many_midline_chunks_with_heartbeats_all_parse_clean(
        self,
    ) -> None:
        """Fire many heartbeats across many mid-line chunk boundaries
        in a single stream — simulates a long Opus thinking session
        where every other gap exceeds the heartbeat interval."""
        # Build a long stream with multiple text_delta events, slice
        # each at a mid-string position, sleep between halves.
        sequence: List[Any] = [b"event: message_start\n"]
        sequence.append(
            b'data: {"type":"message_start","message":'
            b'{"id":"msg_x","model":"opus","role":"assistant","content":[],'
            b'"usage":{"input_tokens":1}}}\n\n'
        )
        sequence.append(
            b"event: content_block_start\n"
            b'data: {"type":"content_block_start","index":0,'
            b'"content_block":{"type":"text","text":""}}\n\n'
        )
        for i in range(8):
            full = (
                b"event: content_block_delta\n"
                b'data: {"type":"content_block_delta","index":0,'
                b'"delta":{"type":"text_delta","text":"chunk-'
                + str(i).encode("ascii")
                + b' payload"}}\n\n'
            )
            mid = len(full) // 2
            sequence.append(full[:mid])
            sequence.append(0.08)  # > 0.05 s heartbeat → fires
            sequence.append(full[mid:])
        sequence.append(
            b"event: content_block_stop\n"
            b'data: {"type":"content_block_stop","index":0}\n\n'
        )
        sequence.append(
            b"event: message_delta\n"
            b'data: {"type":"message_delta",'
            b'"delta":{"stop_reason":"end_turn"},'
            b'"usage":{"output_tokens":10}}\n\n'
        )
        sequence.append(
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        )

        out = asyncio.run(_run_hook_with_gaps(sequence))

        # Heartbeats should have fired at least a few times.
        hb_count = out.count(b": keepalive\n\n")
        self.assertGreaterEqual(
            hb_count, 4,
            f"expected several heartbeats; got {hb_count}",
        )

        try:
            events = _parse_sse_events(out)
        except Exception as e:
            self.fail(
                f"long-stream SSE parse failed: {type(e).__name__}: {e}"
            )

        deltas = [
            ev for ev in events
            if ev.get("type") == "content_block_delta"
        ]
        self.assertEqual(len(deltas), 8, "lost or duplicated deltas")
        for i, ev in enumerate(deltas):
            self.assertEqual(
                ev["delta"]["text"],
                f"chunk-{i} payload",
                f"delta {i} corrupted",
            )

        # Stream terminated cleanly.
        self.assertEqual(events[-1]["type"], "message_stop")


if __name__ == "__main__":
    unittest.main(verbosity=2)
