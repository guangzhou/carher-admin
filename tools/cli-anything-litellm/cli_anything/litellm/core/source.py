"""Read the upstream LiteLLM source a lane is actually built from, and the request path map.

The source of truth is the tag in 198's ``/root/litellm-fork`` that matches the live
version — never a local checkout (``~/codes/litellm`` stopped in May) and never ``main``.
"""
from __future__ import annotations

import re
import shlex
from typing import Final

from cli_anything.litellm.utils import remote198

# The request path through 1.105.1, verified by reading the tag (2026-10-20). Each step names
# the function that owns it and the patches that hook into that step on our lanes.
REQUEST_PATH: Final[tuple[dict[str, object], ...]] = (
    {"step": "endpoint", "where": "proxy/proxy_server.py:chat_completion | response_api_endpoints/endpoints.py:responses_api | anthropic_endpoints/endpoints.py:anthropic_response",
     "does": "FastAPI route -> ProxyBaseLLMRequestProcessing.base_process_llm_request", "hooks": ()},
    {"step": "auth", "where": "proxy/auth/user_api_key_auth.py:user_api_key_auth -> auth_checks.common_checks / can_key_call_model / _virtual_key_max_budget_check",
     "does": "key lookup (cache/DB), model access, budget; key aliases narrow auth since 1.104 (#43049)",
     "hooks": ("baked:auth_checks.py (alias-auth revert)", "budget_notice (reserve_budget_for_request)")},
    {"step": "pre-call", "where": "proxy/common_request_processing.py:common_processing_pre_call_logic",
     "does": "add_litellm_data_to_request, model_alias_map, key aliases, per-request model_group_alias, function_setup, then ProxyLogging.pre_call_hook",
     "hooks": ("async_pre_call_hook: mock_heartbeat, opus_47_fix, force_stream, budget_notice, cursor_*, codex_ua_pool_route, ...",)},
    {"step": "route", "where": "proxy/route_llm_request.py:route_request -> Router.acompletion / aresponses / anthropic messages",
     "does": "during_call_hook runs in parallel with the LLM call (asyncio.gather)", "hooks": ()},
    {"step": "fallback+retry", "where": "router.py:async_function_with_fallbacks -> async_function_with_retries -> should_retry_this_error",
     "does": "retries inside a group (num_retries), then fallbacks across groups (router_settings.fallbacks, owned by DB)",
     "hooks": ("dead_deployment_retry (should_retry_this_error)", "chatgpt_responses_normalize (run_async_fallback)")},
    {"step": "pick deployment", "where": "router.py:async_get_available_deployment -> async_get_healthy_deployments",
     "does": "team/web-search filter -> health -> cooldown filter -> blocked -> async_callback_filter_deployments -> pre_call_checks -> tags -> order -> strategy (simple-shuffle)",
     "hooks": ("weighted_affinity (async_filter_deployments: session pin)", "encrypted_content_degrade_strip", "streaming_output_backfill (async_get_available_deployment)")},
    {"step": "cooldown", "where": "router_utils/cooldown_handlers.py:_is_cooldown_required / _set_cooldown_deployments",
     "does": "4xx don't cool down except 401/404/408/429; 5xx and unknown do", "hooks": ("dead_deployment_retry (_is_cooldown_required, DEAD_ARM_COOLDOWN_HOOK=1)",)},
    {"step": "provider call", "where": "llms/<provider>/... ; deployment-level hooks via async_pre_call_deployment_hook",
     "does": "per-deployment request rewrite (also runs on fallback hops)",
     "hooks": ("deepseek_responses_adapt", "grok_*", "local_gpu_max_tokens_clamp", "reasoning_effort_minimal_normalize", "codex_compaction_v2")},
    {"step": "stream out", "where": "responses/streaming_iterator.py:BaseResponsesAPIStreamingIterator._process_chunk ; proxy_server._format_streaming_sse_chunk",
     "does": "upstream SSE -> ResponsesAPIStreamingResponse -> client SSE",
     "hooks": ("_process_chunk chain: deepseek_id_prefix -> streaming_output_backfill -> codex_compaction_v2 -> upstream",
               "baked:responses/streaming_iterator.py (SSE lifecycle)", "streaming_bridge (heartbeat)", "budget_notice (90% notice)")},
    {"step": "spend", "where": "hooks/proxy_track_cost_callback.py -> db/db_spend_update_writer.py:update_database -> proxy/utils.py:update_spend_logs",
     "does": "queue SpendLogs row + daily-table transactions; flush every PROXY_BATCH_WRITE_AT (10s) via create_many, poison rows bisected and dropped",
     "hooks": ("sitecustomize + null_byte_sanitize (jsonify_object, 2 layers)", "sitecustomize (update_spend_logs rescue)", "baked:litellm_logging.py + proxy_server.py (#44341 alias pricing)")},
)

_DEF_RE: Final = re.compile(r"^(\s*)(async\s+)?def\s+(\w+)\s*\(|^(\s*)class\s+(\w+)\b")


def resolve_tag(lane: str | None, tag: str | None) -> tuple[str, str | None]:
    """Return (tag, live_version). With a lane, the tag must equal the lane's live version."""
    live = None
    if lane is not None:
        live = remote198.live_version(remote198.lane_pod(lane))
        if tag is None:
            tag = "v" + live
        elif tag.lstrip("v") != live:
            raise remote198.RemoteError(f"--tag {tag} != live version {live} on lane {lane}")
    if tag is None:
        raise remote198.RemoteError("give --tag or --lane")
    if not remote198.fork_has_tag(tag):
        raise remote198.RemoteError(f"tag {tag} not in {remote198.FORK}; fetch it from https://www.github.com/BerriAI/litellm.git")
    return tag, live


def find_symbol(tag: str, symbol: str, path: str | None = None) -> list[dict[str, object]]:
    """Locate `def symbol` / `class symbol` (optionally `Class.method`) in the tag. Uses git grep, no checkout."""
    cls, _, name = symbol.rpartition(".")
    pattern = rf"^\s*(async\s+)?def {name}\b|^\s*class {name}\b"
    scope = f" -- {shlex.quote(path)}" if path else " -- 'litellm/*.py'"
    out = remote198.run(f"git -C {remote198.FORK} grep -nE {shlex.quote(pattern)} {shlex.quote(tag)}{scope} || true")
    hits = []
    for line in out.splitlines():
        # format: <tag>:<path>:<line>:<text>
        parts = line.split(":", 3)
        if len(parts) < 4:
            continue
        _, fpath, lineno, text = parts
        hits.append({"path": fpath, "line": int(lineno), "text": text.strip()})
    if cls and hits:
        hits = [h for h in hits if _inside_class(tag, h, cls)] or hits
    return hits


def _inside_class(tag: str, hit: dict[str, object], cls: str) -> bool:
    src = remote198.fork_show(tag, str(hit["path"])).splitlines()
    i = int(hit["line"]) - 1
    indent = len(src[i]) - len(src[i].lstrip())
    for j in range(i - 1, -1, -1):
        line = src[j]
        if line.strip() and (len(line) - len(line.lstrip())) < indent:
            m = re.match(r"\s*class\s+(\w+)", line)
            if m:
                return m.group(1) == cls
            if not line.lstrip().startswith(("@", "#")):
                indent = len(line) - len(line.lstrip())
    return False


def show_symbol(tag: str, path: str, line: int, max_lines: int = 200) -> str:
    """The body of the def/class starting at `line` (ends at the next line with <= indent)."""
    src = remote198.fork_show(tag, path).splitlines()
    i = line - 1
    if i < 0 or i >= len(src):
        raise remote198.RemoteError(f"{path}:{line} out of range")
    indent = len(src[i]) - len(src[i].lstrip())
    j = i - 1
    while j >= 0 and src[j].lstrip().startswith("@") and (len(src[j]) - len(src[j].lstrip())) == indent:
        j -= 1
    start = j + 1
    end = i + 1
    while end < len(src) and end - start < max_lines:
        s = src[end]
        if s.strip() and (len(s) - len(s.lstrip())) <= indent and not s.lstrip().startswith((")", "]", "}")):
            break
        end += 1
    return "\n".join(f"{n + 1:6d}  {src[n]}" for n in range(start, end))
