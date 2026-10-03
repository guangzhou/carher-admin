"""
LiteLLM callback module — force `provider` pin to reach OpenRouter for claude-sonnet-5.

Problem
-------
Litellm v1.89.4 openrouter chat transformer
(``/app/litellm/llms/openrouter/chat/transformation.py``) rewrites
``mapped_openai_params["extra_body"]`` from scratch, keeping only a whitelist
of {transforms, models, route}. Anything else the user puts under
``litellm_params.extra_body`` — including ``provider`` — gets silently dropped.
As a result, requests to OpenRouter's ``anthropic/claude-sonnet-5`` route
round-robin across all upstream providers, and 30-60% of them land on
non-Bedrock providers that reject our region → HTTP 403
``This model is not available in your region``.

Fix
---
On import, monkey-patch ``OpenRouterChatConfig.map_openai_params`` so it also
preserves an already-set ``extra_body.provider`` when it exists in either
``non_default_params`` or ``optional_params``. Behavior is scoped:
- Applied only when the resulting request targets ``anthropic/claude-sonnet-5``
  (model string check).
- Non-sonnet-5 traffic on OpenRouter is untouched.

We do the monkey-patch here (not a CustomLogger hook) because the transformer
runs after all pre-call hooks — a hook that sets ``extra_body.provider`` is
overwritten before the request leaves the process.

Registered in LiteLLM via ``litellm_settings.callbacks``:

    litellm_settings:
      callbacks: ["openrouter_provider_pin.openrouter_provider_pin"]
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Union

from litellm.integrations.custom_logger import CustomLogger
import litellm


_PATCH_APPLIED = False


def _install_transformer_patch() -> None:
    global _PATCH_APPLIED
    if _PATCH_APPLIED:
        return
    try:
        from litellm.llms.openrouter.chat.transformation import (
            OpenrouterConfig as _ORConfig,
        )
    except Exception:
        try:
            from litellm.llms.openrouter.chat.transformation import (
                OpenRouterChatConfig as _ORConfig,  # older/newer alias
            )
        except Exception as exc:
            litellm.print_verbose(
                f"[openrouter_provider_pin] failed to import OpenrouterConfig: {exc!r}"
            )
            return

    original = _ORConfig.map_openai_params

    def _patched(
        self,
        non_default_params: Dict[str, Any],
        optional_params: Dict[str, Any],
        model: str,
        drop_params: bool,
    ) -> Dict[str, Any]:
        # Snapshot provider pin from either side BEFORE upstream wipes extra_body.
        pin: Optional[Dict[str, Any]] = None
        for src in (non_default_params, optional_params):
            if not isinstance(src, dict):
                continue
            eb = src.get("extra_body")
            if isinstance(eb, dict) and isinstance(eb.get("provider"), dict):
                pin = eb["provider"]
                break
            prov = src.get("provider")
            if isinstance(prov, dict):
                pin = prov
                break

        mapped = original(self, non_default_params, optional_params, model, drop_params)

        # Only pin for claude-sonnet-5 upstream route.
        m = (model or "").lower()
        if pin is not None and ("claude-sonnet-5" in m or "claude-opus-5" in m):
            eb = mapped.get("extra_body")
            if not isinstance(eb, dict):
                eb = {}
            eb["provider"] = pin
            mapped["extra_body"] = eb
            try:
                litellm.print_verbose(
                    f"[openrouter_provider_pin] pinned provider={pin} for model={model!r}"
                )
            except Exception:
                pass
        return mapped

    _ORConfig.map_openai_params = _patched
    _PATCH_APPLIED = True
    try:
        litellm.print_verbose(
            "[openrouter_provider_pin] installed OpenrouterConfig.map_openai_params patch"
        )
    except Exception:
        pass


_install_transformer_patch()


class OpenRouterProviderPin(CustomLogger):
    """Placeholder CustomLogger — the real work is the import-time monkey-patch above.

    LiteLLM requires the ``callbacks`` list entries to resolve to CustomLogger
    instances; we register this instance so the module gets imported (and thus
    the patch above runs) at proxy startup.
    """

    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: Dict[str, Any],
        call_type: Any,
    ) -> Optional[Union[Exception, str, Dict[str, Any]]]:
        return data


openrouter_provider_pin = OpenRouterProviderPin()
