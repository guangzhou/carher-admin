"""
LiteLLM pre-call hook: rewrite old Anthropic "thinking" schema to the 2026
"adaptive" schema for models routed to the Wangsu cheliantianxia6 gateway
(anthropic.claude-opus-4-7 and any future opus-4-7+ model).

Why this exists
===============
LiteLLM v1.82.6 only knows how to emit the new "adaptive" thinking schema for
Claude 4.6 models (see litellm/llms/anthropic/common_utils.py _is_claude_4_6_model).
For every other model it falls back to the legacy:

    thinking = {"type": "enabled", "budget_tokens": N}

The Wangsu cheliantianxia6 gateway (which hosts opus-4-7) rejects that legacy
schema with HTTP 400:

    "thinking.type.enabled" is not supported for this model. Use
    "thinking.type.adaptive" and "output_config.effort" to control
    thinking behavior.

This hook runs BEFORE LiteLLM's provider-specific transformation, so we can:

  1. Pop the OpenAI-style ``reasoning_effort`` out of ``data`` so LiteLLM
     does NOT translate it to the legacy ``thinking.type=enabled`` schema.
  2. Directly rewrite any incoming legacy ``thinking.type=enabled`` payload
     (Anthropic-native /v1/messages entry used by Claude Code / Cursor) to
     the new adaptive schema.

Both entry points (carher's OpenAI ``completion`` path and Claude Code's
``anthropic_messages`` native path) are handled uniformly.

Registered in LiteLLM via ``litellm_settings.callbacks``:

    litellm_settings:
      callbacks: ["opus_47_fix.thinking_schema_fix"]

The module lives next to ``config.yaml`` (i.e. ``/app/opus_47_fix.py`` in the
litellm-proxy container), so ``litellm.proxy.types_utils.utils.get_instance_fn``
resolves it relative to the config file.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Union

from litellm.integrations.custom_logger import CustomLogger
import litellm


NEEDS_ADAPTIVE_MODELS = (
    "opus-4-7",
    "opus_4_7",
    "opus-4.7",
    "opus_4.7",
)


EFFORT_MAP_FROM_OPENAI = {
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "max",
}


def _budget_to_effort(budget_tokens: int) -> str:
    """Map legacy thinking.budget_tokens to the new output_config.effort level."""
    if budget_tokens <= 8192:
        return "low"
    if budget_tokens <= 32768:
        return "medium"
    return "high"


def _is_target_model(*model_candidates: Optional[str]) -> bool:
    for m in model_candidates:
        if not isinstance(m, str):
            continue
        ml = m.lower()
        if any(tag in ml for tag in NEEDS_ADAPTIVE_MODELS):
            return True
    return False


def _rewrite_thinking(data: Dict[str, Any]) -> bool:
    """Mutate ``data`` so thinking params conform to the 2026 adaptive schema.

    Returns True when a change was made.
    """
    changed = False

    reasoning_effort = data.pop("reasoning_effort", None)
    effort_from_re: Optional[str] = None
    if reasoning_effort is not None:
        effort_from_re = EFFORT_MAP_FROM_OPENAI.get(
            str(reasoning_effort).lower(), "medium"
        )

    thinking = data.get("thinking")
    effort_from_thinking: Optional[str] = None
    if isinstance(thinking, dict) and thinking.get("type") == "enabled":
        raw_budget = thinking.get("budget_tokens")
        try:
            budget = int(raw_budget) if raw_budget is not None else 4096
        except (TypeError, ValueError):
            budget = 4096
        effort_from_thinking = _budget_to_effort(budget)

    effort = effort_from_re or effort_from_thinking
    if effort is None and reasoning_effort is None:
        return changed

    final_effort = effort or "medium"

    existing_oc = data.get("output_config")
    oc: Dict[str, Any] = dict(existing_oc) if isinstance(existing_oc, dict) else {}
    oc.setdefault("effort", final_effort)
    data["output_config"] = oc

    data["thinking"] = {"type": "adaptive"}
    changed = True

    return changed


class ThinkingSchemaFix(CustomLogger):
    """Rewrite thinking schema for models that require the 2026 adaptive API."""

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

            model_group: Optional[str] = None
            meta = data.get("metadata") or data.get("litellm_metadata") or {}
            if isinstance(meta, dict):
                model_group = meta.get("model_group")

            if not _is_target_model(data.get("model"), model_group):
                return data

            if _rewrite_thinking(data):
                try:
                    litellm.print_verbose(
                        "[opus_47_fix] rewrote thinking schema "
                        f"model={data.get('model')!r} call_type={call_type!r} "
                        f"new_thinking={data.get('thinking')} "
                        f"output_config={data.get('output_config')}"
                    )
                except Exception:
                    pass
        except Exception as exc:
            try:
                litellm.print_verbose(f"[opus_47_fix] ERROR: {exc!r}")
            except Exception:
                pass
        return data


thinking_schema_fix = ThinkingSchemaFix()
