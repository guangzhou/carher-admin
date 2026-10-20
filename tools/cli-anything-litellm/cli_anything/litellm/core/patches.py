"""Patch-layer knowledge: which of our patches owns which upstream symbol, on a live lane.

Two inputs, both from the real backend:
  * the in-pod probe (``probes/patch_targets.py``) -> who actually replaced each symbol
  * the catalog below -> what each patch is for, so the output is readable

The catalog is documentation, not a ruler: a target the probe reports as unpatched is
unpatched, whatever the catalog says.
"""
from __future__ import annotations

import json
from importlib import resources
from typing import Final

from cli_anything.litellm.utils import remote198

# Symbols patched at interpreter start (sitecustomize imports these before any callback),
# so the probe's per-callback diff can't see them; it reports them by sentinel instead.
EARLY_OWNERS: Final[dict[str, str]] = {
    "ResponsesAPIStreamingIterator.aclose": "responses_aclose (imported by sitecustomize)",
    "ProxyUpdateSpend.update_spend_logs": "sitecustomize (spendlog batch rescue)",
}

# What each patch does, in one line, verified by reading the live file + the 1.105.1 source.
PURPOSE: Final[dict[str, str]] = {
    "dead_deployment_retry": "400 'no healthy deployments' (dead ChatGPT leaf) -> retryable + cooldown; upstream doesn't cool 4xx except 401/404/408/429",
    "streaming_output_backfill": "Responses stream: backfill response.completed.output from output_item.done; non-blocking failure handling",
    "chatgpt_responses_normalize": "ChatGPT pool: normalize Responses input items; wraps run_async_fallback",
    "codex_compaction_v2": "Codex remote compaction for upstreams that don't speak compaction v2",
    "deepseek_id_prefix": "DeepSeek outbound item ids get fc_/rs_ prefixes; restore custom_tool_call at SSE layer",
    "budget_notice": "/查余额 short-circuit, 90% warning injected into stream, friendly over-budget text",
    "null_byte_sanitize": "strip NUL / lone surrogates from spend-log rows before create_many",
    "encrypted_content_degrade_strip": "encrypted_content affinity: strip reasoning the pinned account can't decrypt",
    "error_sanitize": "client-facing error text + routing headers pseudonymized",
    "chatgpt_responses_output_fallback": "ResponsesAPIResponse.model_construct always has .output",
    "responses_tool_args_done_backfill": "parallel tool calls with no arg deltas: emit args once at done",
    "pricing_overlay": "our price table over litellm.completion_cost / response_cost_calculator",
    "streaming_bridge": "SSE heartbeat (CF 524), TTFT stamp, httpx read timeout, strip stray [DONE]",
    "anthropic_passthrough_pingfix": "anthropic passthrough: tolerate JSONDecodeError on ping/empty chunks",
    "anthropic_bridge_error_surface": "/v1/messages bridge mid-stream failure -> Anthropic error event (<=1.104 only; 1.105 upstream does it)",
    "responses_aclose": "ResponsesAPIStreamingIterator.aclose so the outer generator returns the socket",
    "sitecustomize": "spend-log scrub / batch rescue / prisma transport misclassification / UI log render",
    "weighted_affinity": "async_filter_deployments: weight-based first pick + session pin in Redis (sliding TTL) for upstream prompt cache",
    "baiyu_image_route": "one key (cursor-baiyu-thga): requests with images go to chatgpt-gpt-5.5",
    "client_metadata_strip": "pre-call: drop non-standard client params (client_metadata) that crash OpenAI-SDK providers",
    "codex_ua_pool_route": "pre-call: pick pool by User-Agent (Codex desktop vs CLI) for listed key prefixes/aliases",
    "cursor_responses_shim": "pre-call: normalize Cursor's Responses-shaped /chat/completions into valid chat requests",
    "cursor_web_fc_sys_rewrite": "pre-call: system-prompt rewrite for cursor-web-fc-* models",
    "force_stream": "pre-call: force stream=True for non-streaming Claude Code requests so streaming_bridge can heartbeat",
    "grok_agent_message_flatten": "deployment hook (sub2api only): flatten Responses agent_message items to user messages",
    "grok_gif_to_png": "deployment hook (sub2api only): GIF -> PNG first frame (x.ai rejects GIF)",
    "grok_web_search_preview_rename": "deployment hook (sub2api only): web_search_preview -> web_search",
    "local_gpu_max_tokens_clamp": "deployment hook: clamp max_tokens for the self-hosted GPU box",
    "log_truncate": "logging filter: truncate log records to 4000 chars",
    "mock_heartbeat": "pre-call: answer OpenClaw heartbeat polls with a mock, no upstream call",
    "observability_attempt_pusher": "success/failure events -> stability dashboard ingest (batched, HMAC)",
    "openrouter_provider_pin": "pin OpenRouter provider for claude-sonnet-5 (map_openai_params)",
    "opus_47_fix": "pre-call: legacy thinking/reasoning_effort -> adaptive schema for opus-4-7+",
    "reasoning_effort_minimal_normalize": "deployment hook: reasoning_effort minimal -> low",
    "embedding_sanitize": "pre-call (embeddings): strip lone UTF-16 surrogates",
    "deepseek_responses_adapt": "pre-call + deployment hook: let DeepSeek take gpt/Codex-shaped Responses requests (tools, reasoning_content)",
    "codex_custom_tool_bridge": "chat-schema targets accept Codex custom tools (wraps responses<->chat tool transforms)",
}


def probe_lane(lane: str) -> dict:
    pod = remote198.lane_pod(lane)
    code = resources.files("cli_anything.litellm.probes").joinpath("patch_targets.py").read_text(encoding="utf-8")
    out = remote198.pod_python(pod, code, timeout=180)
    line = next((l for l in out.splitlines() if l.startswith("@@")), None)
    if line is None:
        raise remote198.RemoteError("patch probe printed no result line", stderr=out[-600:])
    data = json.loads(line[2:])
    data["lane"] = lane
    data["pod"] = pod
    data["live_version"] = remote198.live_version(pod)
    return data


def owner_of(key: str, target: dict) -> str:
    """Human verdict for one target. Order matters: missing > layer chain > replaced-by-callback > early > upstream.

    The layer chain (outermost first, read from closures in the pod) is the most complete answer:
    it also shows wrappers installed before any callback, e.g. sitecustomize under null_byte_sanitize.
    """
    if target["missing"]:
        return "-"
    # Printed in install order (innermost first), the same order as patched_by, so
    # "a -> b" always means b wraps a.
    layers = [m for m in target.get("layers") or [] if not m.startswith("litellm")]
    if len(layers) >= 2 or (layers and not target["patched_by"]):
        return " -> ".join(reversed(layers))
    if target["patched_by"]:
        return " -> ".join(target["patched_by"])
    if key in EARLY_OWNERS and (target["sentinels"] or target["wrapped_depth"]
                                or not str(target["baseline_owner"]).startswith("litellm")):
        return EARLY_OWNERS[key]
    return "upstream"


def compare_lanes(probes: list[dict]) -> list[dict]:
    keys = list(probes[0]["targets"])
    rows = []
    for key in keys:
        cells = {p["lane"]: owner_of(key, p["targets"][key]) for p in probes}
        rows.append({"symbol": key, "owners": cells, "same": len(set(cells.values())) == 1})
    return rows
