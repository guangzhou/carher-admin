"""
Degrade-strip for encrypted_content affinity (198 pro).

Background
----------
``EncryptedContentAffinityCheck.async_filter_deployments``
(litellm/router_utils/pre_call_checks/encrypted_content_affinity_check.py)
pins Responses-API follow-ups (input carrying ``encitem_`` / ``litellm_enc:``
encrypted reasoning) to the originating chatgpt-acct deployment. When that
deployment is unavailable (usage-limit cooldown / removed / no encryption-
boundary peer) it raises BadRequestError/RateLimitError/ServiceUnavailableError
and the request hard-fails WITHOUT reaching the configured wangsu fallback
(verified 2026-06-16: forged stale encitem -> HTTP 400, no fallback).

This patch wraps that method: on the unavailable-origin error, instead of
hard-failing, it strips the encrypted reasoning items (any ``encitem_`` IDs,
``previous_response_id``, and ``litellm_enc:...`` wrappers off the
``encrypted_content`` field) from the request input and returns the healthy
deployment list, so the request proceeds on another healthy chatgpt acct
(stays on GPT) or falls back to wangsu if the whole group is down. Trades one
turn's reasoning continuity for availability. Only affects requests that would
otherwise 400/429/503; happy-path pinned traffic is untouched.

Fallback-path note (2026-06-25, marker moved 2026-08-02)
--------------------------------------------------------
Router fallback re-invokes ``async_filter_deployments`` against the fallback
model_group (e.g. wangsu-gpt-5.4). Without an idempotency marker the second
strip is a no-op (already cleaned) and the wrapper re-raises -> fallback
itself 503s. So we remember "ever stripped in this request" and on re-entry
that alone keeps us returning ``healthy_deployments`` instead of re-raising.

The marker used to be ``request_kwargs["_degrade_stripped"] = True``. That dict
is the outgoing provider payload, so the key rode along into the SDK call --
silently dropped on the anthropic path, but fatal on ``custom_openai``
(``AsyncCompletions.create() got an unexpected keyword argument
'_degrade_stripped'``). It is now a ContextVar, so the payload is never touched;
the legacy key is read once and popped for self-healing during rollout.

Deployed via the litellm-callbacks ConfigMap (module import side-effect), no
image rebuild -- same pattern as chatgpt_responses_output_fallback.py /
responses_aclose.py.
"""
from litellm.integrations.custom_logger import CustomLogger

import contextvars

try:
    from litellm._logging import verbose_router_logger
except Exception:  # pragma: no cover
    import logging

    verbose_router_logger = logging.getLogger("litellm.router")


_LITELLM_ENC_PREFIX = "litellm_enc:"

# "Ever stripped in this request" — see the note at the except clause below for
# why this cannot be a key inside request_kwargs. Each request runs in its own
# asyncio task, so contexts do not bleed between concurrent requests; router
# fallback re-entry stays inside the same task and therefore sees the flag.
_degrade_stripped_var: contextvars.ContextVar = contextvars.ContextVar(
    "degrade_strip_ever_stripped", default=False
)


def _strip_encrypted_reasoning(request_kwargs) -> bool:
    """Remove encrypted reasoning items + affinity markers from the request.

    Returns True if anything was stripped.
    """
    if not isinstance(request_kwargs, dict):
        return False
    changed = False
    if request_kwargs.pop("previous_response_id", None) is not None:
        changed = True
    inp = request_kwargs.get("input")
    if isinstance(inp, list):
        kept = []
        for it in inp:
            if isinstance(it, dict):
                if it.get("type") in ("reasoning", "compaction"):
                    continue  # drop encrypted reasoning / compaction item entirely
                iid = it.get("id")
                if isinstance(iid, str) and iid.startswith("encitem_"):
                    continue  # drop any other affinity-encoded item
                enc = it.get("encrypted_content")
                if isinstance(enc, str) and enc.startswith(_LITELLM_ENC_PREFIX):
                    # Codex wraps as "litellm_enc:{b64};{original}"; keep the
                    # original encrypted_content payload (after the first ';')
                    # so the message stays well-formed; only strip the
                    # affinity wrapper so the affinity check can't pin again.
                    semi = enc.find(";")
                    if semi >= 0:
                        it["encrypted_content"] = enc[semi + 1 :]
                    else:
                        it.pop("encrypted_content", None)
                    changed = True
            kept.append(it)
        if len(kept) != len(inp):
            request_kwargs["input"] = kept
            changed = True
    return changed


def _install_degrade_strip() -> None:
    try:
        from litellm.router_utils.pre_call_checks.encrypted_content_affinity_check import (
            EncryptedContentAffinityCheck,
        )
        from litellm.exceptions import (
            BadRequestError,
            RateLimitError,
            ServiceUnavailableError,
        )
    except Exception as exc:  # pragma: no cover
        verbose_router_logger.warning("[degrade_strip] import failed, patch skipped: %s", repr(exc))
        return

    orig = EncryptedContentAffinityCheck.async_filter_deployments
    if getattr(orig, "_degrade_strip_patched", False):
        return

    async def patched(
        self,
        model,
        healthy_deployments,
        messages=None,
        request_kwargs=None,
        parent_otel_span=None,
    ):
        try:
            return await orig(
                self,
                model=model,
                healthy_deployments=healthy_deployments,
                messages=messages,
                request_kwargs=request_kwargs,
                parent_otel_span=parent_otel_span,
            )
        except (BadRequestError, RateLimitError, ServiceUnavailableError) as exc:
            # The idempotency marker must NOT live in request_kwargs: that dict is
            # the outgoing provider payload, so an unknown key rides along into the
            # SDK call. Harmless on the anthropic path (unknown kwargs dropped) but
            # fatal on custom_openai -> ``AsyncCompletions.create() got an
            # unexpected keyword argument '_degrade_stripped'`` (2026-08-02: broke
            # every request the moment a compat entry became a gpt fallback target).
            # A ContextVar carries the same "ever stripped in this request" fact
            # without touching the payload. The legacy key is still read once and
            # popped, so any in-flight payload self-heals.
            legacy = None
            if isinstance(request_kwargs, dict):
                legacy = request_kwargs.pop("_degrade_stripped", None)
            already_stripped = bool(legacy) or _degrade_stripped_var.get()
            did_strip = _strip_encrypted_reasoning(request_kwargs)
            if did_strip or already_stripped:
                if did_strip:
                    _degrade_stripped_var.set(True)
                try:
                    verbose_router_logger.warning(
                        "[degrade_strip] origin unavailable (%s) for model=%s; "
                        "%s, allowing %d healthy deployment(s)",
                        exc.__class__.__name__,
                        model,
                        "stripped encrypted reasoning"
                        if did_strip
                        else "fallback re-entry (already stripped)",
                        len(healthy_deployments or []),
                    )
                except Exception:
                    pass
                return healthy_deployments
            raise

    patched._degrade_strip_patched = True
    EncryptedContentAffinityCheck.async_filter_deployments = patched
    verbose_router_logger.warning("[degrade_strip] patched EncryptedContentAffinityCheck.async_filter_deployments")


_install_degrade_strip()


class EncryptedContentDegradeStrip(CustomLogger):
    """No-op callback; real effect is the module-import monkey-patch above."""

    pass


encrypted_content_degrade_strip = EncryptedContentDegradeStrip()
