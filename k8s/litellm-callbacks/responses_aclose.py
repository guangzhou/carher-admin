"""
LiteLLM ResponsesAPIStreamingIterator socket-leak fix.

Problem
-------
``litellm.responses.streaming_iterator.ResponsesAPIStreamingIterator`` is the
async iterator that wraps the upstream SSE stream for every chatgpt/
provider ``/v1/responses`` request (and every ``/v1/chat/completions`` request
bridged to Responses for chatgpt models — i.e. the entire gpt-5.5/5.4 path
on this proxy). The class has ``__aiter__`` and ``__anext__`` but **no
``aclose`` method**.

The outer wrapper in ``litellm/proxy/proxy_server.py`` (``async_data_generator``)
already has the right cleanup intent — its ``finally`` block does::

    with anyio.CancelScope(shield=True):
        if hasattr(response, "aclose"):
            try:
                await response.aclose()
            except BaseException as e:
                verbose_proxy_logger.debug(...)

…but the ``hasattr(response, "aclose")`` gate evaluates to False for
ResponsesAPIStreamingIterator instances, so the entire shielded close
block is silently skipped. The ``httpx.Response`` keep-alive socket that
the iterator holds (``self.response``) is never returned to the connection
pool. On every stream completion — including the happy path that ends
with StopAsyncIteration — one socket leaks.

Quantified impact (198 K3s, 2026-06-13)
---------------------------------------
On the chatgpt-acct pool, the highest-traffic Pod (acct-2) accumulates
~100KB of stuck Recv-Q bytes within 30–60 minutes of restart. State=08
(classic CLOSE_WAIT) is only part of the picture; the dominant pattern
is state=01 ESTABLISHED + Recv-Q > 0 (kernel buffer never drained). The
``chatgpt-acct-close-wait-restart`` skill currently mitigates this with
periodic rolling restarts; this patch eliminates the leak so that skill
becomes obsolete.

Asymmetry with chat-completions
-------------------------------
The chat-completions path uses ``CustomStreamWrapper`` (litellm/utils.py)
which has a proper ``async def aclose(self)`` method (PR #21213, merged).
On that path the same wrapper ``hasattr`` gate evaluates True and the
socket is closed. The Responses path was simply never given the same
treatment.

Upstream PRs #26273 and #26292 attempted to fix this by adding
``await self.response.aclose()`` inside the except branches of
``__anext__``, but the happy path (StopAsyncIteration on stream end)
does not traverse those branches, so the leak persisted. Both PRs were
closed unmerged. Issue #26250 remains open.

Fix
---
Monkey-patch ``ResponsesAPIStreamingIterator`` at module import time to
add an ``aclose`` method that:

  1. Closes ``self.stream_iterator`` (SSEDecoder.aiter_bytes / aiter_lines —
     v1.85 uses aiter_lines, v1.87 uses SSEDecoder, both have aclose).
  2. Closes ``self.response`` (the underlying httpx.Response).
  3. Marks ``self.finished = True`` so any subsequent ``__anext__`` exits
     fast.

Each close is wrapped in its own try/except so that one failing close
does not prevent the other from running. After the patch is in place
``async_data_generator``'s existing ``hasattr`` gate naturally evaluates
True and the existing shielded-close path runs.

This patch is identical in form to ``streaming_bridge._patch_anthropic_
httpx_timeout`` and ``anthropic_passthrough_pingfix._patch_convert_str_
chunk_to_generic_chunk`` — import-time monkey-patch on a LiteLLM
internal class, no source-tree changes, no image rebuild. Rollout is
pure ConfigMap subPath mount.

Idempotent: the patch is guarded by a ``_carher_aclose_patched`` class
attribute. Re-importing the module is a no-op.

Side effect summary
  * Affected ConfigMap entry  — adds ``responses_aclose.py`` to the
    ``litellm-callbacks`` ConfigMap; one subPath mount per Pod.
  * Affected request types    — every chatgpt/ provider streaming
    request (which on this proxy is every chatgpt-gpt-5.x model_group).
    Non-streaming chatgpt requests do not traverse this iterator and
    are unaffected. All other providers (anthropic, openai, wangsu,
    gemini) are unaffected.
  * Affected logging paths    — none. ``self.finished`` is the same
    flag the iterator's own except branches already set, so post-stream
    SpendLog construction sees identical state.
  * Performance               — one extra ``await response.aclose()``
    per stream completion. Already shielded from cancellation by the
    outer wrapper. No measurable per-request overhead.

Upstream tracking
-----------------
Issue: github.com/BerriAI/litellm/issues/26250 (open).
Once upstream ships a real fix in the iterator class itself, remove
this entry from ``litellm_settings.callbacks`` and delete this file.
"""

from __future__ import annotations

import logging as _stdlib_logging
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

_log = _stdlib_logging.getLogger("responses_aclose")


def _patch_responses_api_streaming_iterator() -> None:
    """Install ``aclose`` on ``ResponsesAPIStreamingIterator``.

    Idempotent via the ``_carher_aclose_patched`` class attribute.
    """
    try:
        from litellm.responses.streaming_iterator import (
            ResponsesAPIStreamingIterator,
        )
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning(
            "responses_aclose: cannot import ResponsesAPIStreamingIterator: %r",
            exc,
        )
        return

    if getattr(ResponsesAPIStreamingIterator, "_carher_aclose_patched", False):
        _log.debug("responses_aclose: already patched, skipping")
        return

    async def aclose(self: Any) -> None:
        """Close the underlying SSE iterator and httpx response.

        Safe to call multiple times. Safe to call after __anext__ has
        already raised StopAsyncIteration or an exception. Each close
        attempt is isolated so one failure does not block the other.
        """
        # Mark finished first so any in-flight __anext__ on another
        # task exits the inner while-True loop instead of trying to
        # read from a closed iterator.
        try:
            self.finished = True
        except Exception:
            pass

        stream_iterator = getattr(self, "stream_iterator", None)
        if stream_iterator is not None:
            self.stream_iterator = None
            close_fn = getattr(stream_iterator, "aclose", None)
            if close_fn is not None:
                try:
                    await close_fn()
                except BaseException as exc:
                    _log.debug(
                        "responses_aclose: stream_iterator.aclose raised: %r",
                        exc,
                    )

        response = getattr(self, "response", None)
        if response is not None:
            self.response = None
            close_fn = getattr(response, "aclose", None)
            if close_fn is not None:
                try:
                    await close_fn()
                except BaseException as exc:
                    _log.debug(
                        "responses_aclose: response.aclose raised: %r",
                        exc,
                    )

    ResponsesAPIStreamingIterator.aclose = aclose  # type: ignore[attr-defined]
    ResponsesAPIStreamingIterator._carher_aclose_patched = True  # type: ignore[attr-defined]
    _log.info(
        "responses_aclose: patched ResponsesAPIStreamingIterator.aclose "
        "(closes stream_iterator + response)"
    )


_patch_responses_api_streaming_iterator()


class ResponsesAcloseLogger(CustomLogger):
    """Inert CustomLogger marker.

    The real work is the import-time monkey-patch above. This class
    exists only because LiteLLM's callback loader requires a class
    instance to register; listing this module in
    ``litellm_settings.callbacks`` is what triggers the import (and
    therefore the patch). Adds zero runtime overhead per request.
    """

    def __init__(self) -> None:
        super().__init__()


responses_aclose = ResponsesAcloseLogger()
