"""
LiteLLM pre-call hook — strip lone UTF-16 surrogates from embedding inputs.

Problem
-------
Node.js callers (e.g. the carher bot) sometimes emit text containing lone
UTF-16 surrogates (U+D800..U+DFFF) because of byte-level slicing that cuts
an emoji in half. Node.js HTTP clients silently replace such code points
with U+FFFD when serializing the request body, so directly-connected
OpenRouter calls appeared to work. LiteLLM forwards those requests through
Python httpx, which is strict: `str.encode('utf-8')` raises
`UnicodeEncodeError: surrogates not allowed` before the request is ever
sent upstream, bubbling up as a 500 (and eventually a 404 via the
`no fallback for bge-m3` path) to the bot.

Fix
---
On `embedding`/`aembedding` calls, sanitize the `input` field by dropping
any lone surrogate code points. Behavior now matches the lenient Node.js
path. The dropped bytes are never valid characters on their own, so there
is no real semantic loss.

This hook only touches embedding calls; all other call types pass through
unchanged.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional, Union

from litellm.integrations.custom_logger import CustomLogger
import litellm


# Matches any lone surrogate code unit (U+D800..U+DFFF). Properly paired
# surrogates are collapsed to a single Python str code point by CPython,
# so this regex only fires on broken ones.
_LONE_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def _sanitize(value: Any) -> Any:
    """Recursively strip lone surrogates from str / list[str] / dict values."""
    if isinstance(value, str):
        return _LONE_SURROGATE_RE.sub("", value)
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    if isinstance(value, dict):
        return {k: _sanitize(v) for k, v in value.items()}
    return value


class EmbeddingSanitize(CustomLogger):
    """Pre-call hook: strip lone surrogates from embedding inputs."""

    _EMBED_CALL_TYPES = frozenset({"embedding", "aembedding"})

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
            if call_type not in self._EMBED_CALL_TYPES:
                return data

            orig = data.get("input")
            if orig is None:
                return data

            cleaned = _sanitize(orig)
            if cleaned != orig:
                try:
                    litellm.print_verbose(
                        "[embedding_sanitize] stripped lone surrogates from input "
                        f"(call_type={call_type!r}, model={data.get('model')!r})"
                    )
                except Exception:
                    pass
                data["input"] = cleaned
        except Exception as exc:
            # Never block the request on this hook's failure: if sanitize
            # itself errors, just pass the original data through.
            try:
                litellm.print_verbose(f"[embedding_sanitize] ERROR: {exc!r}")
            except Exception:
                pass
        return data


embedding_sanitize = EmbeddingSanitize()
