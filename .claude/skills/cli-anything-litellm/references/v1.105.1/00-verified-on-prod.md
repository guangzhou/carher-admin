# Main-thread notes (verified by me, v1.105.1)

## Per-key router_settings skips the early "Model is blocked" 403
- `proxy/common_request_processing.py:2116-2128`: key/team router_settings (`_get_hierarchical_router_settings`) -> `data["router_settings_override"]`.
- `proxy/route_llm_request.py:516-538`: the override branch merges `fallbacks/context_window_fallbacks/content_policy_fallbacks/num_retries/timeout/model_group_retry_policy/routing_strategy/enable_tag_filtering` into kwargs **only if the request didn't set them** and calls the router directly.
- The plain branch (`route_llm_request.py:539-540`) calls `_raise_if_model_fully_blocked` first (403 "Model is blocked", `:58-73`); the override branch does not.
- Router still filters blocked deployments on every entry (`router.py:14495`, called at 8597/8628/13082/14220/14401) ⇒ not a bypass; a key with per-key router_settings (764 keys on prod) hitting a fully blocked model gets a "no deployments" error instead of the explicit 403.
- Per-key fallbacks **replace** global fallbacks for that request (kwarg wins over `self.fallbacks` in `async_function_with_fallbacks`: `kwargs.get("fallbacks", self.fallbacks)`). Not a merge.

## Defaults on prod
- `MAX_STRING_LENGTH_PROMPT_IN_DB` unset ⇒ 2048 (`constants.py:529`); `MAX_STRING_LENGTH_STDOUT_LOG` 4096 (`constants.py:119`) — that is why the prisma error text is cut.
- `PROXY_BATCH_POLLING_ENABLED=true` on prod pods.

## (main thread, verified) streaming_output_backfill drops the sync failure handler on /v1/responses streams
- live pod: `ResponsesAPIStreamingIterator._handle_failure` owner = `streaming_output_backfill._patched_handle_failure`
  (flag `_carher_nonblocking_failure_patched=True`); its body never calls `logging_obj.failure_handler` / `_submit_sync_failure_handler`.
- upstream 1.105.1 `responses/streaming_iterator.py:886-944`: with a running loop it spawns
  `_run_failure_handlers_in_order` = async handler **then** sync handler (`finally: _submit_sync_failure_handler` → executor) — i.e. upstream already fixed
  the event-loop deadlock our patch was written for (no `.result()` join on the loop thread).
- router cooldown lives only in the SYNC hook: `router.py:1180-1183` registers `deployment_callback_on_failure` on `litellm.failure_callback`;
  the async hook `async_deployment_callback_on_failure` only increments RPM.
- ⇒ (code + live ownership) a mid-stream /v1/responses failure handled by `_handle_failure` does not reach router cooldown on prod.
  NOT measured live: how often this path fires vs failures caught earlier in `_acompletion`/`_ageneric_api_call_with_fallbacks` (those still cool down).
- ⇒ the 09-14 block in streaming_output_backfill is now redundant with upstream and harmful; removal candidate (needs a gray test with a dead leg mid-stream).

## (main thread, verified) encrypted_content_degrade_strip is a no-op on 1.105.1
- upstream `router_utils/pre_call_checks/encrypted_content_affinity_check.py` has 0 `raise` statements; when the origin can't serve,
  it strips encrypted reasoning itself and returns the healthy pool (lines ~378-392).
- our wrapper only acts in `except (BadRequestError, RateLimitError, ServiceUnavailableError)` (live-cb line 140) ⇒ never fires. Harmless, removable.

## (main thread, verified) chatgpt_responses_normalize deployment hook never fires
- `utils.py:1482` passes `CallTypes(call_type)` (enum) to every `async_pre_call_deployment_hook`.
- live pod (py 3.13.16): `str(CallTypes.aresponses) == 'CallTypes.aresponses'` → not in `('aresponses','responses')`.
- `chatgpt_responses_normalize.py:523` gates on `str(call_type) in _RESPONSE_CALL_TYPES` ⇒ always False ⇒ never runs on fallback hops / per deployment.
  The same trap is documented in `deepseek_responses_adapt.py:193-200` (found 2026-08-03) and fixed there + in grok_*; this file was never fixed.
- `codex_compaction_v2.py:597` survives only through its extra `"responses" in str(call_type)` clause.
- Other deployment hooks checked: deepseek_responses_adapt, grok_* use `.value` (OK); local_gpu_clamp / reasoning_effort / grok_gif / web_search_preview / cursor_shim don't gate on call_type.

## (main thread, verified) async_pre_routing_hook on callbacks is dead code
- `router.py:13317/13451` call `self.async_pre_routing_hook` (Router's own method, `:13796`), which runs routing plugins / auto-router strategy —
  it does not iterate `litellm.callbacks`. So `async_pre_routing_hook` defined in chatgpt_responses_normalize / deepseek_responses_adapt never runs.

## (main thread, checked against prod DB) budget reset race — not observed
- Hypothesis (02-auth-budget.md §7): without a coordination-Redis lock every worker sweeps, overlapping sweeps subtract twice ⇒ spend < 0.
- Falsifier: keys with clearly negative spend.
- Data (07:45Z): 3 keys with spend<0, min = -5.7e-14 (float dust from subtract-old-spend, not a double reset). 340 keys past budget_reset_at, all 340 expired
  (reset skips expired keys) ⇒ reset job is running for live keys. ⇒ race not observed; keep as code-level risk only.
- Coordination Redis: 0 `coordination_redis` log lines in the prod pod, no REDIS_* env names (secrets not read) ⇒ "probably not configured", not verified.

## (main thread, prod data) kiro cost = full input price × 1.3, cache reads never reported
- 3 recent kiro-claude-opus-5-5 rows: spend / (prompt×4e-6 + completion×2e-5) = 1.3000 exactly ⇒ pricing_overlay ×1.3 applied once, on the full prompt.
- Last 6 h: 15257 kiro-claude-* rows, **0** with cache_read_input_tokens > 0 ⇒ the kiro upstream does not report cache usage, so the
  "cache billed at full vs $0" question (04-anthropic-messages.md §8) doesn't arise today: everything is billed as uncached input.
- Not checked: whether kiro actually caches upstream (only that it doesn't report it).

## (main thread, live pod) "reconcile appends string callbacks every 30s" — NOT happening
- Hypothesis (06-proxy-pipeline.md): each 30s reconcile appends YAML `module.instance` names into litellm.callbacks / success / failure lists.
- Falsifier: list lengths stable across >30s and no bare strings in success/failure lists.
- Data: /active/callbacks sampled 40s apart on a prod pod: lengths identical (callbacks 91, success 56, failure 56, async_success 56).
  litellm.callbacks has 36 bare strings (= the 36 configured names, each once, alongside their 55 objects); success/failure/async lists have 0 bare strings.
  ⇒ no growth, no thread-pool fan-out of string callbacks. Strings in litellm.callbacks are inert for dispatch (CustomLogger isinstance checks skip them).

## Prod rolled at 2026-10-20 07:15:37Z (someone else, not this session)
- New RS litellm-proxy-5c89687bd, callbacks CM -> litellm-product-proxy-callbacks-09a9f47873c0.
- Only diff vs previous CM: budget_notice.py (sha 7fcdf4c043 -> 092d554e82), byte-identical to the **uncommitted** working-tree
  k8s/litellm-callbacks/budget_notice.py (+500/-63). Probe after the roll: still 36 callbacks importable, patch map unchanged (25/27 same across lanes).

## (main thread, prod config) keepalive is OFF ⇒ finding 03 §8 reduced to the first-chunk case
- `_iter_with_keepalive` (proxy_server.py:9634) is entered whenever a router exists, but with interval <= 0 it calls `aiter.__anext__()` inline (:9653-9660),
  no child task. Interval sources: `litellm.sse_keepalive_ping_interval_seconds` (default None, `__init__.py:267`) or per-deployment `keepalive_seconds`.
- prod: litellm_settings has no sse_keepalive_ping_interval_seconds (yaml), no DB litellm_settings row, 0 yaml and 0 DB deployments with keepalive_seconds.
- ⇒ the "every chunk fetched in a child task, contextvar lost" case does not apply on prod. The first-chunk prefetch task (`common_request_processing.py:896`)
  case still applies by code; not measured live.
