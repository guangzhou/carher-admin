"""
LiteLLM spend-log payload sanitizer — strips NUL bytes and lone UTF-16
surrogates that crash the ``update_spend_logs`` flush job.

Problem
-------
LiteLLM 1.82.6's ``update_spend_logs`` background job flushes accumulated
``LiteLLM_SpendLogs`` rows to PostgreSQL in batches of up to 1000 via
``prisma_client.db.litellm_spendlogs.create_many(...)``. Each row's dict
fields (``messages``, ``response``, ``metadata``, ``proxy_server_request``)
are first serialized via ``PrismaClient.jsonify_object`` (defined in
``litellm/proxy/utils.py``):

    def jsonify_object(self, data: dict) -> dict:
        db_data = copy.deepcopy(data)
        for k, v in db_data.items():
            if isinstance(v, dict):
                try:
                    db_data[k] = json.dumps(v)
                except Exception:
                    db_data[k] = "failed-to-serialize-json"
        return db_data

This implementation does **zero validation** of two byte patterns that
PostgreSQL or its prisma driver reject:

  1. **NUL bytes** (``\\x00`` and the literal escape ``\\u0000``).
     Commonly emitted by binary tool outputs — DOCX/PDF text extraction,
     raw byte slices, client-side ``\\u0000`` escapes in user content.
     ``json.dumps`` encodes ``\\x00`` as the literal six-character
     sequence ``\\u0000``, which PostgreSQL rejects in both ``text`` and
     ``jsonb``:

         ERROR: invalid byte sequence for encoding "UTF8": 0x00
         ERROR: unsupported Unicode escape sequence
         DETAIL: \\u0000 cannot be converted to text.   (SQLSTATE 22P05)

  2. **Lone UTF-16 surrogates** (``\\ud800``..``\\udfff`` codepoints
     not paired with a counterpart). Produced when Node.js clients
     byte-slice an emoji or otherwise truncate a string at a non-codepoint
     boundary. Python's ``str`` happily holds them, and CPython
     ``json.dumps(ensure_ascii=True)`` emits them as a 4-digit hex
     escape — but the prisma client's Rust ``serde_json`` parser
     enforces the JSON spec strictly and rejects any lone surrogate
     with::

         prisma.errors.DataError: ... is not a valid JSON String.
         Underlying error: unexpected end of hex escape at line 1 column N

     The ``embedding_sanitize`` pre-call hook strips lone surrogates
     from ``data["input"]`` before the upstream call, but the spend-log
     payload is built from independent copies — ``proxy_server_request``
     (raw client body), ``metadata`` (logging context), and the
     ``UserAPIKeyAuth`` repr — so lone surrogates can still reach the
     DB even when the embedding call itself succeeded.

In **either case**, when ``create_many`` (NUL byte → PG side) or its
prisma JSON encoder (lone surrogate → driver side) hits one bad row,
**the entire batch of up to 1000 rows is rejected and silently
discarded** by LiteLLM's flush loop:

    # litellm/proxy/utils.py — update_spend_logs
    for j in range(0, len(logs_to_process), BATCH_SIZE):
        batch = logs_to_process[j : j + BATCH_SIZE]
        batch_with_dates = [
            prisma_client.jsonify_object({**entry}) for entry in batch
        ]
        await prisma_client.db.litellm_spendlogs.create_many(
            data=batch_with_dates, skip_duplicates=True
        )
        # Items already removed from queue at start of function
        # (NO retry, NO single-row split, just drops the whole batch)

In production this manifests as 23–27 ``update_spend`` failures per
pod per 30 minutes, each erasing up to 1000 spend rows from the
billing / audit trail. Upstream issues with no fix merged:

  - https://github.com/BerriAI/litellm/issues/21290  (NUL bytes)
  - https://github.com/BerriAI/litellm/issues/24310  (NUL bytes)
  - https://github.com/BerriAI/litellm/issues/19847  (lone surrogates)

Fix strategy
------------
We monkey-patch ``PrismaClient.jsonify_object`` (and the module-level
``jsonify_object`` for defense-in-depth) at import time so that every
spend-log batch is recursively scrubbed of:

  * raw NUL byte ``\\x00`` and its JSON escape ``\\u0000``, and
  * any lone UTF-16 surrogate codepoint in U+D800..U+DFFF

**before** ``json.dumps`` is invoked. Once the bad bytes are gone,
PostgreSQL accepts the row and the prisma driver successfully encodes
the batch.

We also patch ``litellm_core_utils/safe_json_dumps.safe_dumps`` because
several other paths inside LiteLLM (logging payloads, exception
serialization) reach the DB through it, even though it is not on the
spend-log critical path today — cheap insurance against the next
upstream code reshuffle.

Why monkey-patch and not a CustomLogger pre-call hook
-----------------------------------------------------
The failure happens in a *background job* (``update_spend_logs``),
hundreds of milliseconds after the originating request has already
returned to the client. CustomLogger hooks (``async_pre_call_hook``,
``async_log_success_event``) run in the request lifecycle and **cannot
intercept the batch flush**. The only reliable interception point is
the serializer itself.

Trade-offs
----------
  * **Lossiness**: stripping ``\\x00`` from a string discards what was
    almost certainly garbage anyway (PG cannot store it, the LLM
    cannot tokenize it cleanly, and most tools that generate it are
    binary-mode artifacts). The literal text ``\\u0000`` *that the user
    typed* will also be stripped — extremely rare in practice and
    losing it is preferable to losing the entire 1000-row batch.

    Lone surrogates only exist in strings that are already broken
    UTF-16 fragments (e.g. half of an emoji). They cannot tokenize as
    valid Unicode, so dropping them is identical to what the upstream
    LLM would do anyway; we just do it earlier.

  * **Fail-open**: if ``_strip_nul`` itself raises, the original data is
    passed through unchanged. We never block a spend log on this hook.

  * **Idempotence**: each patch checks a private flag on the function
    object so that re-importing this module (e.g. during dev reloads)
    does not stack patches.

Removal criteria
----------------
Remove this entire module once an upstream LiteLLM release ships its
own NUL-byte sanitization in ``jsonify_object`` / ``safe_dumps``. At
that point the patch becomes a no-op cosmetic and can be deleted.

Verification (recorded 2026-04-27)
----------------------------------
  * pod ``litellm-proxy-554dc489d5-fh86v`` — direct INSERT into
    ``LiteLLM_SpendLogs`` with raw ``\\x00`` and literal ``\\u0000``
    payloads reproduces both PG error variants.
  * Same pod — monkey-patched ``PrismaClient.jsonify_object`` correctly
    scrubbed both byte forms, output JSON contained neither.
  * pod ``litellm-proxy-7758ccb55c-4qfb7`` (after rollout) — observed
    a fresh ``unexpected end of hex escape`` on a ``BAAI/bge-m3`` row
    whose ``proxy_server_request.input[]`` contained the lone surrogate
    ``\\ud83d`` (truncated emoji). Confirms that NUL stripping alone
    is insufficient and motivates the extension to surrogate stripping.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from litellm.integrations.custom_logger import CustomLogger


_log = logging.getLogger(__name__)


# ── core sanitizer ─────────────────────────────────────────────────────
# Strip both the raw NUL byte and its JSON-escaped literal form. The
# literal ``\\u0000`` arrives whenever upstream code already ran
# ``json.dumps`` on a NUL-containing value (which is exactly what
# ``jsonify_object`` does for nested dicts, see docstring above).
_NUL_RAW = "\x00"
_NUL_ESCAPED = "\\u0000"

# Lone (unpaired) UTF-16 surrogate code points. CPython auto-collapses
# correctly-paired surrogates to a single >U+FFFF codepoint, so this
# regex only matches genuinely broken fragments — typically half of an
# emoji that was byte-truncated upstream.
_LONE_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")

# JSON-escape *literal* form of a lone surrogate (6 ASCII chars: \uDxxx).
# Reaches the spend-log buffer when upstream code has already run
# ``json.dumps`` once on a value containing a lone surrogate codepoint,
# so by the time our patch sees it the codepoint is gone but the
# 6-character escape sequence remains. Prisma's Rust serde_json driver
# rejects this literal form just as harshly as the codepoint form
# ("unexpected end of hex escape").
#
# We must NOT strip *paired* surrogate literals like ``\\ud83d\\ude00`` —
# those are the JSON spec's official way to encode codepoints above
# U+FFFF (e.g. emoji) and prisma accepts them. Hence the lookahead /
# lookbehind to require the partner be missing.
_LITERAL_HIGH_LONE_RE = re.compile(
    # high surrogate `\uD800..\uDBFF` NOT followed by a low surrogate
    r"\\u[dD][89aAbB][0-9a-fA-F]{2}"
    r"(?!\\u[dD][c-fC-F][0-9a-fA-F]{2})"
)
_LITERAL_LOW_LONE_RE = re.compile(
    # low surrogate `\uDC00..\uDFFF` NOT preceded by a high surrogate
    r"(?<!\\u[dD][89aAbB][0-9a-fA-F]{2})"
    r"\\u[dD][c-fC-F][0-9a-fA-F]{2}"
)


def _scrub_str(s: str) -> str:
    """Strip every byte sequence that crashes our PG/prisma write path.

    Three independent checks; cheap-existence test first so the common
    case (clean string) allocates nothing.
    """
    if _NUL_RAW in s or _NUL_ESCAPED in s:
        s = s.replace(_NUL_RAW, "").replace(_NUL_ESCAPED, "")
    if _LONE_SURROGATE_RE.search(s):
        s = _LONE_SURROGATE_RE.sub("", s)
    # ``\\u`` only appears in strings that have been json-dumped at least
    # once, so this short-circuit avoids two re.sub passes on every
    # input string in the common case.
    if "\\u" in s:
        s = _LITERAL_HIGH_LONE_RE.sub("", s)
        s = _LITERAL_LOW_LONE_RE.sub("", s)
    return s


def _strip_nul(value: Any) -> Any:
    """Recursively scrub PG/prisma-unsafe bytes from
    ``str`` / ``list`` / ``tuple`` / ``dict`` values.

    Despite the historical name (``_strip_nul``), this also handles
    lone UTF-16 surrogates — kept for backwards-compat with any
    external reference to the symbol.

    Anything other than the above container/string types (int / float /
    bool / None / unknown objects) is returned untouched. The walk is
    O(N) on the encoded payload size with no allocation when the input
    is already clean.
    """
    if isinstance(value, str):
        return _scrub_str(value)
    if isinstance(value, dict):
        return {k: _strip_nul(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_nul(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_strip_nul(v) for v in value)
    return value


# ── patch points ───────────────────────────────────────────────────────
def _patch_prisma_jsonify_object() -> None:
    """Wrap ``PrismaClient.jsonify_object`` (the instance method actually
    used by ``update_spend_logs``) so every flushed batch is scrubbed
    before ``json.dumps``.

    Idempotent via ``_null_byte_sanitize_patched`` attribute.
    """
    try:
        from litellm.proxy import utils as _proxy_utils
    except Exception as exc:
        _log.error(
            "null_byte_sanitize: cannot import litellm.proxy.utils: %r",
            exc,
        )
        return

    PrismaClient = getattr(_proxy_utils, "PrismaClient", None)
    if PrismaClient is None:
        _log.error(
            "null_byte_sanitize: litellm.proxy.utils.PrismaClient missing; "
            "skipping jsonify_object patch"
        )
        return

    if getattr(PrismaClient.jsonify_object, "_null_byte_sanitize_patched", False):
        return

    original = PrismaClient.jsonify_object

    def patched_jsonify_object(self, data: dict) -> dict:  # type: ignore[no-untyped-def]
        try:
            cleaned = _strip_nul(data) if isinstance(data, dict) else data
        except Exception as exc:
            try:
                _log.warning(
                    "null_byte_sanitize: _strip_nul failed (%r); falling back to original data",
                    exc,
                )
            except Exception:
                pass
            cleaned = data
        return original(self, cleaned)

    patched_jsonify_object._null_byte_sanitize_patched = True  # type: ignore[attr-defined]
    PrismaClient.jsonify_object = patched_jsonify_object  # type: ignore[method-assign]
    _log.warning(
        "null_byte_sanitize: patched PrismaClient.jsonify_object "
        "(scrubs \\x00 and \\u0000 from spend-log payloads)"
    )


def _patch_module_level_jsonify_object() -> None:
    """Also patch the module-level ``litellm.proxy.utils.jsonify_object``
    function (defined at line 2264 in 1.82.6 alongside the instance
    method). It is currently used by credential / vector-store / org
    endpoints — not the spend-log critical path — but patching it is
    cheap insurance and mirrors the instance-method behavior.

    Idempotent via ``_null_byte_sanitize_patched`` attribute.
    """
    try:
        from litellm.proxy import utils as _proxy_utils
    except Exception:
        return

    fn = getattr(_proxy_utils, "jsonify_object", None)
    if fn is None or getattr(fn, "_null_byte_sanitize_patched", False):
        return

    def patched_module_jsonify_object(data: dict) -> dict:  # type: ignore[no-untyped-def]
        try:
            cleaned = _strip_nul(data) if isinstance(data, dict) else data
        except Exception:
            cleaned = data
        return fn(cleaned)

    patched_module_jsonify_object._null_byte_sanitize_patched = True  # type: ignore[attr-defined]
    _proxy_utils.jsonify_object = patched_module_jsonify_object  # type: ignore[assignment]


def _patch_safe_json_dumps() -> None:
    """Wrap ``safe_dumps`` so any future upstream path that routes
    through it on the way to PG also has NUL bytes scrubbed.

    ``safe_dumps`` is used by exception serialization, telemetry
    payloads, and some logging callbacks. Not currently on the
    spend-log critical path in 1.82.6, but upstream has been moving
    serialization in this direction.

    Idempotent via ``_null_byte_sanitize_patched`` attribute.
    """
    try:
        from litellm.litellm_core_utils import safe_json_dumps as _sjd
    except Exception:
        return

    fn = getattr(_sjd, "safe_dumps", None)
    if fn is None or getattr(fn, "_null_byte_sanitize_patched", False):
        return

    def patched_safe_dumps(data: Any, max_depth: int = 10) -> str:  # type: ignore[no-untyped-def]
        try:
            cleaned = _strip_nul(data)
        except Exception:
            cleaned = data
        return fn(cleaned, max_depth)

    patched_safe_dumps._null_byte_sanitize_patched = True  # type: ignore[attr-defined]
    _sjd.safe_dumps = patched_safe_dumps  # type: ignore[assignment]


# Apply all patches at import time so the very next ``update_spend_logs``
# tick scrubs whatever NUL-bearing rows are already sitting in the
# in-memory buffer.
_patch_prisma_jsonify_object()
_patch_module_level_jsonify_object()
_patch_safe_json_dumps()


class NullByteSanitize(CustomLogger):
    """Inert ``CustomLogger`` placeholder so this module can be referenced
    from ``litellm_settings.callbacks`` like the other callbacks in
    the deployment. All real work is done by the import-time patches
    above; this class is just the registration handle.
    """


null_byte_sanitize = NullByteSanitize()
