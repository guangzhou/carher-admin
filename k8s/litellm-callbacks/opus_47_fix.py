"""
LiteLLM pre-call hook — thinking schema rewrite (opus-4-7+).

Rewrites legacy ``thinking.type=enabled`` / OpenAI-style ``reasoning_effort``
to the 2026 ``thinking.type=adaptive`` + ``output_config.effort`` schema
required by the Wangsu cheliantianxia6 gateway for claude-opus-4-7.

NOTE: ``stream_options.include_usage`` injection is NOT done here. It must be
configured via ``general_settings.always_include_stream_usage: true`` in the
LiteLLM config — that path runs before ``function_setup``, which is the only
stage that reliably propagates the flag to the upstream call. Injection from a
pre-call hook happens too late and is silently ignored by some providers.

Registered in LiteLLM via ``litellm_settings.callbacks``:

    litellm_settings:
      callbacks: ["opus_47_fix.thinking_schema_fix"]
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
    """Pre-call hook: rewrite opus-4-7 thinking params to adaptive schema."""

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
