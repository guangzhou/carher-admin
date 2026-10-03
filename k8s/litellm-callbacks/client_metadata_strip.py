"""
LiteLLM pre-call hook — strip non-standard client params (e.g. ``client_metadata``)
that crash providers using the OpenAI Python SDK.

Problem
-------
Newer Cursor clients send a top-level ``client_metadata`` field in the
chat/completions body. LiteLLM forwards unknown params as kwargs to the
provider handler. For models routed via the ``custom_openai`` provider
(the 188 chatgpt pool + wangsu direct), this reaches
``openai.AsyncCompletions.create(client_metadata=...)`` and raises::

    litellm.InternalServerError: Custom_openaiException -
    AsyncCompletions.create() got an unexpected keyword argument 'client_metadata'
    → HTTP 500 → deployment cooldown → fallback chain exhausts → 429 to user

``litellm_settings.drop_params``/``additional_drop_params`` only act on the
known-OpenAI optional-param translation layer and do not strip this
top-level kwarg for the custom provider, so we drop it explicitly here.

The OpenRouter ``openai/`` proxy path tolerates the field (upstream ignores
unknown JSON keys), which is why only the custom_openai-backed groups break.

Fix
---
Pop the offending keys from ``data`` in ``async_pre_call_hook`` for all call
types. These are client telemetry fields with no effect on inference, so
dropping them is lossless for the model call.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Union

from litellm.integrations.custom_logger import CustomLogger
import litellm

# Non-standard, inference-irrelevant client params that break SDK providers.
_STRIP_KEYS = ("client_metadata",)
_EMPTY_LIST_STRIP_KEYS = ("tools",)


class ClientMetadataStrip(CustomLogger):
    """Pre-call hook: drop non-standard client params before the upstream call."""

    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: Dict[str, Any],
        call_type: Any,
    ) -> Optional[Union[Exception, str, Dict[str, Any]]]:
        try:
            if isinstance(data, dict):
                for k in _STRIP_KEYS:
                    if k in data:
                        data.pop(k, None)
                for k in _EMPTY_LIST_STRIP_KEYS:
                    if data.get(k) == []:
                        data.pop(k, None)
                        try:
                            litellm.print_verbose(
                                f"[client_metadata_strip] dropped empty {k!r} "
                                f"(call_type={call_type!r}, model={data.get('model')!r})"
                            )
                        except Exception:
                            pass
                        try:
                            litellm.print_verbose(
                                f"[client_metadata_strip] dropped {k!r} "
                                f"(call_type={call_type!r}, model={data.get('model')!r})"
                            )
                        except Exception:
                            pass
        except Exception as exc:
            try:
                litellm.print_verbose(f"[client_metadata_strip] ERROR: {exc!r}")
            except Exception:
                pass
        return data


client_metadata_strip = ClientMetadataStrip()
