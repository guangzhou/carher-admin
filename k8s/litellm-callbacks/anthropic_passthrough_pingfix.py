"""
LiteLLM Anthropic pass-through streaming logging — JSONDecodeError tolerator.

Problem
-------
LiteLLM 1.82.6's anthropic pass-through endpoint
(``/app/litellm/proxy/pass_through_endpoints/streaming_handler.py``) collects
every raw byte chunk that flows from the upstream Anthropic-compatible
provider through the proxy to the client. After the stream ends, it splits
the buffer on ``\\n\\n`` boundaries and feeds each piece into
``ModelResponseIterator.convert_str_chunk_to_generic_chunk`` (in
``/app/litellm/llms/anthropic/chat/handler.py``), which contains::

    def convert_str_chunk_to_generic_chunk(self, chunk: str) -> ModelResponseStream:
        ...
        if str_line.startswith("data:"):
            data_json = json.loads(str_line[5:])      # ← unconditional
            return self.chunk_parser(chunk=data_json)
        else:
            return ModelResponseStream(id=self.response_id)

The unconditional ``json.loads`` does **zero validation** of the byte
sequence following the ``data:`` prefix. The SSE spec (RFC 8895) and
Anthropic's own streaming spec both allow ``data:`` lines that carry no
JSON payload — for keep-alive heartbeats, comment events, or upstream
proxy injections. In practice we observed OpenRouter emit at least four
shapes that explode this code path:

  * ``data:\\n``               — empty payload, no whitespace
  * ``data: \\n``              — payload is a single space
  * ``data:  \\n``             — payload is two spaces
  * ``data: <truncated>\\n``   — partial JSON when upstream cuts mid-event

When ``json.loads`` raises ``JSONDecodeError`` it propagates straight up
to ``streaming_handler.py:198``, which catches the exception, logs::

    ERROR streaming_handler.py:198 -
        Error in _route_streaming_logging_to_handler:
        Expecting value: line 1 column 3 (char 2)

…and then **silently drops the entire success-handler chain**. That chain
is what writes the SpendLog row — so any anthropic_messages stream whose
upstream emits even one such heartbeat ends up with HTTP 200 to the
client (correct) but **zero LiteLLM_SpendLogs entry** (incorrect — invisible
to billing, audit, the admin UI's Request Logs tab, and every per-key
spend metric).

In production this manifests selectively: requests that route to OpenRouter
(which emits the offending heartbeats during long prefills) lose their
SpendLog rows, while requests that route to Wangsu (whose CDN gateway
emits a different heartbeat shape) log normally. For the
``claude-code-liuguoxian-50gj`` test on 2026-04-28 we observed exactly
this split:

  * 3 POST /v1/messages -> 200 OK  (all three reached the proxy fine)
  * 1 SpendLog row written         (the Haiku request that hit Wangsu)
  * 2 SpendLog rows missing        (the Sonnet/Opus requests that hit OR)
  * 2 streaming_handler.py:198 errors in the proxy log (1:1 with the
    missing SpendLog rows)

Why "tolerate JSONDecodeError" rather than "skip empty data: lines"
-------------------------------------------------------------------
Initial hypothesis was the narrow case ``data:\\s*\\n``. Direct
falsification in the running pod showed the upstream's actual
JSONDecodeError positional info (``line 1 column 3 (char 2)``) does NOT
match what the empty-data hypothesis predicts (``line 2 column 1 (char N)``,
because ``str_line[5:]`` for ``data:  \\n`` is ``"  \\n"`` with a real
newline). The mismatch means the actual exploding chunk has *some other
shape* — most likely ``data:`` with leading whitespace then non-JSON text
(a partial event, a non-standard OR comment, or whitespace alone with the
trailing newline already stripped by the iterator's chunk-splitting). We
do not need to enumerate every possible weird shape: every shape produces
the same root failure mode (``json.loads`` on the byte sequence after
``data:``), so catching ``JSONDecodeError`` at exactly that call site
fixes all of them at once, present and future.

Fix
---
Wrap ``ModelResponseIterator.convert_str_chunk_to_generic_chunk`` so that
``JSONDecodeError`` from the inner ``json.loads`` is converted to the
same empty ``ModelResponseStream`` the upstream returns when a chunk
doesn't start with ``data:``. The downstream consumer
(``stream_chunk_builder``) is already tolerant of empty stream chunks:
it iterates over chunks and accumulates non-empty deltas, so dropping a
single un-parseable frame loses one heartbeat at most — heartbeats by
definition carry no token information, so SpendLog token counts are
unaffected.

Side effect summary:
  * Affected ConfigMap entry  — none (this hook contributes only an
    import-time monkey-patch; the class instance below is inert).
  * Affected request types    — only anthropic_messages (/v1/messages)
    pass-through streams. /chat/completions and /messages-without-stream
    paths are untouched.
  * Affected logging paths    — only the post-stream success chain that
    builds the StandardLoggingPayload from buffered raw bytes. The
    real-time iterator that yields chunks to the client (the LiteLLM
    streaming handler used by SDK clients) has its own parsing path and
    never reached the buggy line.
  * Lost telemetry            — for any chunk that *would* have raised
    here we lose its delta. By spec these are heartbeats, so output_tokens
    and content_text are unchanged.

Idempotent: the patch is guarded by an ``_anthropic_passthrough_pingfix_patched``
attribute on the patched function, so re-importing the module is a no-op.

Upstream tracking
-----------------
Will be filed at github.com/BerriAI/litellm/issues — the proper upstream
fix is in ``ModelResponseIterator.convert_str_chunk_to_generic_chunk``
itself (treat empty payload as a no-op event). Once an upstream release
ships the fix, remove this entry from
``litellm_settings.callbacks`` and delete this file.
"""

from __future__ import annotations

import functools
import json
import logging as _stdlib_logging
from typing import Any

import litellm
from litellm.integrations.custom_logger import CustomLogger

_log = _stdlib_logging.getLogger("anthropic_passthrough_pingfix")


def _patch_convert_str_chunk_to_generic_chunk() -> None:
    """Install the JSONDecodeError tolerator on
    ``ModelResponseIterator.convert_str_chunk_to_generic_chunk``.

    Idempotent via ``_anthropic_passthrough_pingfix_patched`` attribute on
    the patched function. Re-importing this module is a no-op.
    """
    try:
        from litellm.llms.anthropic.chat.handler import ModelResponseIterator
        from litellm.types.utils import ModelResponseStream
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning(
            "anthropic_passthrough_pingfix: cannot import target classes: %r",
            exc,
        )
        return

    original = ModelResponseIterator.convert_str_chunk_to_generic_chunk
    if getattr(original, "_anthropic_passthrough_pingfix_patched", False):
        _log.debug(
            "anthropic_passthrough_pingfix: already patched, skipping"
        )
        return

    @functools.wraps(original)
    def _safe_convert(self: Any, chunk: Any) -> Any:
        try:
            return original(self, chunk)
        except json.JSONDecodeError as exc:
            # Single-line debug log of the exploding chunk so we can
            # post-mortem the actual upstream byte pattern. Truncated
            # to 120 chars to avoid log spam if the chunk happens to
            # be a large truncated JSON event.
            try:
                preview = (
                    chunk.decode("utf-8", errors="replace")
                    if isinstance(chunk, (bytes, bytearray))
                    else str(chunk)
                )
                preview = preview.replace("\n", "\\n").replace("\r", "\\r")
                if len(preview) > 120:
                    preview = preview[:120] + "...(truncated)"
            except Exception:
                preview = "<unprintable>"
            _log.debug(
                "anthropic_passthrough_pingfix: tolerated JSONDecodeError "
                "on chunk %r (%s)",
                preview,
                exc,
            )
            return ModelResponseStream(id=getattr(self, "response_id", None))

    _safe_convert._anthropic_passthrough_pingfix_patched = True  # type: ignore[attr-defined]
    ModelResponseIterator.convert_str_chunk_to_generic_chunk = _safe_convert
    _log.info(
        "anthropic_passthrough_pingfix: patched "
        "ModelResponseIterator.convert_str_chunk_to_generic_chunk"
    )


_patch_convert_str_chunk_to_generic_chunk()


class AnthropicPassthroughPingFix(CustomLogger):
    """Inert CustomLogger marker.

    The real work is the import-time monkey-patch above. This class exists
    only because LiteLLM's callback loader requires a class instance to
    register; listing this module in ``litellm_settings.callbacks`` is
    what triggers the import (and therefore the patch). Adds zero runtime
    overhead per request.
    """

    def __init__(self) -> None:
        super().__init__()


anthropic_passthrough_pingfix = AnthropicPassthroughPingFix()
