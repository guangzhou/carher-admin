"""
LiteLLM pre-call hook — inject prompt_cache_key for OpenAI/ChatGPT routes.

Background
----------
OpenAI ChatGPT prompt cache routing:
  https://platform.openai.com/docs/guides/prompt-caching

OpenAI's server-side prompt cache routes by `(prompt_prefix_hash + prompt_cache_key)`.
Without `prompt_cache_key`, all 217 her instances sharing the same system prompt
collapse to one routing key on each acct, and the per-key 15 req/min throughput
limit is shared. When traffic exceeds 15 req/min, OpenAI overflows to other
servers and cache hits drop.

Solution: inject `prompt_cache_key = vkey_alias` (e.g. `carher-1000`) so each
her routes on its own key, splitting load across 217 routing keys at OpenAI
servers and avoiding the 15 req/min cliff.

`prompt_cache_retention = "24h"` is added too — gpt-5.5+ defaults to this anyway
but explicit setting keeps gpt-4o / gpt-4.1 (5-10 min default) on extended cache
when the per-her vkey is the cache key.

This complements P1 deployment_affinity (LiteLLM-side sticky vkey→acct):
  - P1 sticky decides WHICH acct
  - This hook helps OpenAI's internal cache routing on that chosen acct

Scope
-----
Only fires when `data["model"]` looks like an OpenAI / ChatGPT model
(`openai/`, `chatgpt-`, `gpt-5`, `gpt-4`). Anthropic / OpenRouter / Wangsu
paths are untouched (they have their own cache_control_injection_points).

Idempotent: respects existing prompt_cache_key (won't override caller-provided).
"""

from __future__ import annotations

from typing import Any, Dict, Literal

from litellm.integrations.custom_logger import CustomLogger


_OPENAI_MODEL_PATTERNS = (
    "openai/",          # e.g. openai/chatgpt-gpt-5.5, openai/gpt-5.4
    "chatgpt-",         # e.g. chatgpt-gpt-5.5 (model_name on canary/prod ConfigMap)
    "chatgpt/",         # e.g. chatgpt/gpt-5.5 (in-process chatgpt provider, on chatgpt-acct-N pods)
    "gpt-5",            # e.g. gpt-5.4 / gpt-5.5
    "gpt-4",            # e.g. gpt-4.1 / gpt-4o / etc.
    "custom_openai/",   # wangsu OpenAI-compat path; harmless to inject
)


def _is_openai_model(model: str) -> bool:
    if not isinstance(model, str):
        return False
    m = model.lower()
    return any(p in m for p in _OPENAI_MODEL_PATTERNS)


class InjectPromptCacheKey(CustomLogger):
    """Pre-call hook that injects OpenAI prompt_cache_key + 24h retention.

    Hook surface: LiteLLM `async_pre_call_hook` runs after virtual-key auth
    but before the request is dispatched to the upstream provider, so the
    `user_api_key_dict.key_alias` is already populated with the carher-* alias.
    """

    async def async_pre_call_hook(
        self,
        user_api_key_dict,
        cache,
        data: Dict[str, Any],
        call_type: Literal[
            "completion",
            "text_completion",
            "embeddings",
            "image_generation",
            "moderation",
            "audio_transcription",
            "pass_through_endpoint",
            "rerank",
            "mcp_call",
            "responses",
            "anthropic_messages",
            "google_genai_messages",
            "ocr",
        ],
    ) -> Dict[str, Any]:
        # Only inject for chat/completion/responses paths; skip embeddings, moderation, etc.
        if call_type not in ("completion", "responses"):
            return data

        model = data.get("model", "")
        if not _is_openai_model(model):
            return data

        # Resolve cache key from vkey alias (carher-<N>) — falls back to a stable
        # value if alias missing so we never inject empty string.
        alias = getattr(user_api_key_dict, "key_alias", None) or "carher-default"

        # Inject via extra_body so LiteLLM forwards as native OpenAI params.
        extra_body = data.get("extra_body") or {}
        # Respect caller-provided values (idempotent).
        extra_body.setdefault("prompt_cache_key", alias)
        extra_body.setdefault("prompt_cache_retention", "24h")
        data["extra_body"] = extra_body

        return data


inject_prompt_cache_key = InjectPromptCacheKey()
