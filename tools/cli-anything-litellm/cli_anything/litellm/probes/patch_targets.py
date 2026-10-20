# Runs INSIDE a proxy pod (fed on stdin to `python -`). Prints one line: "@@<json>".
#
# For each upstream symbol our patches are known to replace, import every callback in
# config.yaml order and record which import swapped the object. This is the ruler for
# "is the patch actually installed" — import success alone is not (a fingerprint guard can
# refuse and the import still succeeds).
#
# Two traps this handles (both measured 2026-10-20):
#   * bound methods are fresh objects on every attribute read -> compare __func__ identity
#   * functools.wraps copies __module__ from the wrapped function -> never infer the owner
#     from __module__; detect the swap by identity, then report the callback that did it
# sitecustomize runs at interpreter start, before this probe, so whatever it patched is
# already the baseline; those symbols are reported via sentinels / __wrapped__ instead.
import importlib
import json
import logging
import sys

import yaml

logging.disable(logging.CRITICAL)

TARGETS = {
    "Router.should_retry_this_error": ("litellm.router", "Router.should_retry_this_error"),
    "Router.async_get_available_deployment": ("litellm.router", "Router.async_get_available_deployment"),
    "Router._ageneric_api_call_with_fallbacks_helper": ("litellm.router", "Router._ageneric_api_call_with_fallbacks_helper"),
    "cooldown_handlers._is_cooldown_required": ("litellm.router_utils.cooldown_handlers", "_is_cooldown_required"),
    "fallback_event_handlers.run_async_fallback": ("litellm.router_utils.fallback_event_handlers", "run_async_fallback"),
    "router.run_async_fallback": ("litellm.router", "run_async_fallback"),
    "BaseResponsesAPIStreamingIterator._process_chunk": ("litellm.responses.streaming_iterator", "BaseResponsesAPIStreamingIterator._process_chunk"),
    "BaseResponsesAPIStreamingIterator.__init__": ("litellm.responses.streaming_iterator", "BaseResponsesAPIStreamingIterator.__init__"),
    "ResponsesAPIStreamingIterator.aclose": ("litellm.responses.streaming_iterator", "ResponsesAPIStreamingIterator.aclose"),
    "ResponsesAPIStreamingIterator.__anext__": ("litellm.responses.streaming_iterator", "ResponsesAPIStreamingIterator.__anext__"),
    "proxy_server._format_streaming_sse_chunk": ("litellm.proxy.proxy_server", "_format_streaming_sse_chunk"),
    "AnthropicResponsesStreamWrapper.__anext__": ("litellm.llms.anthropic.pass_through.responses_adapters.streaming_iterator", "AnthropicResponsesStreamWrapper.__anext__"),
    "AnthropicResponsesStreamWrapper.__anext__ (<=1.104 path)": ("litellm.llms.anthropic.experimental_pass_through.responses_adapters.streaming_iterator", "AnthropicResponsesStreamWrapper.__anext__"),
    "ProxyLogging.async_post_call_streaming_iterator_hook": ("litellm.proxy.utils", "ProxyLogging.async_post_call_streaming_iterator_hook"),
    "PrismaClient.jsonify_object": ("litellm.proxy.utils", "PrismaClient.jsonify_object"),
    "ProxyUpdateSpend.update_spend_logs": ("litellm.proxy.utils", "ProxyUpdateSpend.update_spend_logs"),
    "budget_reservation.reserve_budget_for_request": ("litellm.proxy.spend_tracking.budget_reservation", "reserve_budget_for_request"),
    "EncryptedContentAffinityCheck.async_filter_deployments": ("litellm.router_utils.pre_call_checks.encrypted_content_affinity_check", "EncryptedContentAffinityCheck.async_filter_deployments"),
    "StandardLoggingPayloadSetup.get_error_information": ("litellm.litellm_core_utils.litellm_logging", "StandardLoggingPayloadSetup.get_error_information"),
    "ProxyBaseLLMRequestProcessing.get_custom_headers": ("litellm.proxy.common_request_processing", "ProxyBaseLLMRequestProcessing.get_custom_headers"),
    "ResponsesAPIResponse.model_construct": ("litellm.types.llms.openai", "ResponsesAPIResponse.model_construct"),
    "OpenAiResponsesToChatCompletionStreamIterator.chunk_parser": ("litellm.completion_extras.litellm_responses_transformation.transformation", "OpenAiResponsesToChatCompletionStreamIterator.chunk_parser"),
    "litellm.completion_cost": ("litellm", "completion_cost"),
    "litellm.response_cost_calculator": ("litellm", "response_cost_calculator"),
    "http_handler.get_async_httpx_client": ("litellm.llms.custom_httpx.http_handler", "get_async_httpx_client"),
    "safe_json_dumps.safe_dumps": ("litellm.litellm_core_utils.safe_json_dumps", "safe_dumps"),
    "anthropic.chat.ModelResponseIterator.convert_str_chunk_to_generic_chunk": ("litellm.llms.anthropic.chat.handler", "ModelResponseIterator.convert_str_chunk_to_generic_chunk"),
}


def resolve(mod, chain):
    try:
        obj = importlib.import_module(mod)
    except Exception as exc:  # noqa: BLE001 - a missing module is a finding, not a crash
        return None, "no-module:" + type(exc).__name__
    for part in chain.split("."):
        if part in getattr(obj, "__dict__", {}):
            obj = obj.__dict__[part]
        elif hasattr(obj, part):
            obj = getattr(obj, part)
        else:
            return None, "no-attr:" + part
    return obj, None


def func_of(obj):
    return getattr(obj, "__func__", obj)


def ident(obj):
    f = func_of(obj)
    return "%s.%s" % (getattr(f, "__module__", "?"), getattr(f, "__qualname__", type(f).__name__))


def wrapped_depth(obj):
    f, n = func_of(obj), 0
    while hasattr(f, "__wrapped__") and n < 10:
        f, n = f.__wrapped__, n + 1
    return n


def sentinels(obj):
    f = func_of(obj)
    return sorted(a for a in dir(f) if a.endswith("_patched") and getattr(f, a, False) is True)


def layer_chain(obj, limit=8):
    """Modules of the wrappers stacked on this symbol, outermost first, ending at the original.

    Follows __wrapped__ first (functools.wraps), else the first callable in __closure__ — that is
    how every one of our wrappers holds the function it replaced. Stops at a litellm.* function.
    """
    f, chain, seen = func_of(obj), [], set()
    for _ in range(limit):
        if id(f) in seen:
            break
        seen.add(id(f))
        mod = getattr(f, "__module__", "?") or "?"
        chain.append(mod)
        if mod.startswith("litellm"):
            break
        nxt = getattr(f, "__wrapped__", None)
        if nxt is None:
            cells = [c.cell_contents for c in (getattr(f, "__closure__", None) or ())
                     if callable(getattr(c, "cell_contents", None)) and not isinstance(c.cell_contents, type)]
            nxt = next((c for c in cells if getattr(c, "__name__", "") == getattr(f, "__name__", "")
                        or getattr(c, "__name__", "").lstrip("_") in getattr(f, "__name__", "")), None) \
                or next(iter(cells), None)
        if nxt is None:
            break
        f = func_of(nxt)
    return chain


def main():
    baseline = {}
    for key, (mod, chain) in TARGETS.items():
        obj, err = resolve(mod, chain)
        baseline[key] = (err, ident(obj) if obj is not None else None)

    cfg = yaml.safe_load(open("/app/config.yaml"))
    callbacks = [c for c in (cfg.get("litellm_settings") or {}).get("callbacks") or [] if "." in c]
    owners = {k: [] for k in TARGETS}
    import_errors = []
    for cb in callbacks:
        mod_name = cb.rsplit(".", 1)[0]
        before = {}
        for key, (mod, chain) in TARGETS.items():
            obj, _ = resolve(mod, chain)
            before[key] = id(func_of(obj)) if obj is not None else None
        try:
            importlib.import_module(mod_name)
        except Exception as exc:  # noqa: BLE001
            import_errors.append({"callback": cb, "error": type(exc).__name__ + ": " + str(exc)[:160]})
            continue
        for key, (mod, chain) in TARGETS.items():
            obj, _ = resolve(mod, chain)
            if obj is not None and id(func_of(obj)) != before[key]:
                owners[key].append(mod_name)

    targets = {}
    for key, (mod, chain) in TARGETS.items():
        err, base_ident = baseline[key]
        obj, _ = resolve(mod, chain)
        targets[key] = {
            "missing": err,
            "baseline_owner": base_ident,
            "final_owner": ident(obj) if obj is not None else None,
            "patched_by": owners[key],
            "sentinels": sentinels(obj) if obj is not None else [],
            "wrapped_depth": wrapped_depth(obj) if obj is not None else 0,
            "layers": layer_chain(obj) if obj is not None else [],
        }
    print("@@" + json.dumps({
        "sitecustomize_preloaded": "sitecustomize" in sys.modules,
        "callbacks": callbacks,
        "import_errors": import_errors,
        "targets": targets,
    }))


main()
