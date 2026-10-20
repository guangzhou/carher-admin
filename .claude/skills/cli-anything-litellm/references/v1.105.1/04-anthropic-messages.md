# 04 — Anthropic `/v1/messages` (native + bridged) and passthrough, LiteLLM v1.105.1

Read-only source study. Paths: `S/` = `.work/litellm-src-study-1020/src/litellm/`, `CB/` = `.work/litellm-src-study-1020/live-cb/`,
`OV/` = `litellm-gray-rollout/patches/v1.105.1/overlays/`. "Not verified" = only read in code, no runtime evidence.
Overlay diffs were re-checked with `diff` against the 1.105.1 source and match the `.diff` files exactly.

## TL;DR

1. Routing is decided in `anthropic_messages_handler` (`S/llms/anthropic/pass_through/messages/handler.py:564-632`): provider has an Anthropic-Messages config → **native passthrough**; else `model_info.supported_endpoints` contains `/v1/messages` → OpenAI-like passthrough; else provider `openai` → **Responses bridge**; everything else → **chat-completions bridge**. `kiro-claude-*` (`anthropic/…`) is native (`S/utils.py:8944-8945`).
2. 1.105.1 bridge wrapper turns any upstream exception into an in-stream Anthropic `error` event (`responses_adapters/streaming_iterator.py:468-479`). `anthropic_bridge_error_surface` correctly refuses: neither fingerprint string exists anywhere in the 1.105.1 tree (grep: 0 hits).
3. **Native path has no "ended without message_stop" guard.** The incomplete-stream error event exists only in the Bedrock-style `async_sse_wrapper` pump (`messages/streaming_iterator.py:657-664`). An early EOF from kiro is relayed as a silently truncated stream and billed as a success row.
4. Mapping status → Anthropic error type has no 503 entry: `MidStreamFallbackError` defaults to 503 (`S/exceptions.py:1139`) → `api_error`, not `overloaded_error` (`S/anthropic_interface/exceptions/exception_mapping_utils.py:17-26,37-39`).
5. Overlays: logging-handler overlay is very likely dead code on 1.105.1 (upstream already coerces dict `server_tool_use`). The core-streaming overlay still adds something (a synchronous cooldown before `MidStreamFallbackError`), but only on the chat bridge, and it may double-count failures. The passthrough-streaming overlay is still needed.
6. `anthropic_passthrough_pingfix` is redundant on 1.105.1 (the only caller now catches `JSONDecodeError`). `opus_47_fix` is a no-op for `*-5-5` models and mostly redundant on native paths.
7. `streaming_bridge`'s httpx read-timeout patch is **probably ineffective** on 1.105.1 (by-name import, loop-keyed + 1 h TTL client cache, explicit per-request timeout). Not verified at runtime.
8. **Risk (hypothesis, not verified):** in `force_stream` collapse mode, upstream's 15 s Anthropic keepalive wrapper (`common_request_processing.py:2795-2798`) can splice `event: ping` frames into what should be a plain JSON body.

---

## 1. Request routing and transformation

### 1.1 Entry
- `POST /v1/messages` → `anthropic_response` (`S/proxy/anthropic_endpoints/endpoints.py:120-181`) → `base_process_llm_request(route_type="anthropic_messages")` (`:151-168`). The logging object is created with `original_function=route_type` (`S/proxy/common_request_processing.py:2169-2176`), so `call_type == "anthropic_messages"`. Then pre-call hooks run with `call_type=route_type` (`:2210-2215`).
- The router dispatches `anthropic_messages` through `_aanthropic_messages_with_streaming_fallbacks` (`S/router.py:5793-5796`). Every streaming attempt is wrapped by `_aanthropic_messages_streaming_iterator` (`S/router.py:5773-5777`).
- `litellm.anthropic_messages` (`messages/handler.py:228-408`) sanitizes messages: empty blocks, tool-use ids, web-search results (`:265-269`). It also runs `AnthropicCacheControlHook.maybe_inject_cache_control` (`:275-277`), CustomLogger `async_pre_request_hook`s (`:285-322`), the websearch short-circuit (`:329-338`) and interceptors (`:343-363`). Then it calls `anthropic_messages_handler` in an executor (`:368-401`).

### 1.2 Native vs bridge decision — `anthropic_messages_handler` (`messages/handler.py:423-675`)
1. `litellm.get_llm_provider` strips the prefix (`:493-503`).
2. If the provider is a known `LlmProviders`, use `ProviderConfigManager.get_provider_anthropic_messages_config` (`:566-570`). That returns a native config for anthropic, bedrock, bedrock_mantle/vertex_ai/azure_ai (claude only), minimax, **deepseek**, tencent, github_copilot (claude), edenai and JSON providers (`S/utils.py:8939-9010`).
3. Otherwise, if `model_info.supported_endpoints` contains `"/v1/messages"`, use `OpenAILikeAnthropicMessagesConfig` (`:92-102,571-580`).
4. Otherwise: if `compaction` is absent and `_should_route_to_responses_api(...)`, go to the Responses bridge (`:583-606`). That is true only when the provider is `openai` (`:47,85-86`) or the deployment's `mode=responses` disappears once the prefix is stripped (`:57-70`). It can be disabled with `litellm.use_chat_completions_url_for_anthropic_messages` (`:83-84`).
5. Else go to the chat-completions bridge `LiteLLMMessagesToCompletionTransformationHandler` (`:612-632`). That bridge calls `litellm.acompletion` (`adapters/handler.py:667`).

Consequence: `anthropic/claude-opus-5-5` at `api_base http://10.43.109.5:8990` is **native**. A `deepseek/…` deployment hit on `/v1/messages` is also native, to DeepSeek's Anthropic endpoint (`S/utils.py:8979-8984`). Only `openai/…` (and responses-mode) deployments use the Responses bridge. Everything else uses the chat bridge.

### 1.3 Native transformation (`S/llms/anthropic/pass_through/messages/transformation.py`)
- Body is essentially pass-through (`transform_anthropic_messages_request`, `:479-570`). `max_tokens` is required (`:493-498`).
- **Thinking**, in this order:
  - `reasoning_effort` → thinking/output_config (`:500-505`, `:286-345`).
  - `maybe_drop_disabled_thinking`: removes `thinking:{type:disabled}` on always-on models (`:507-511`; `common_utils.py:727-744`).
  - `translate_legacy_thinking_for_adaptive_model`: `enabled` → `adaptive` on 4.7+/5.x unless `supports_legacy_thinking` (`:513-517`; `common_utils.py:747-775`).
  - Adaptive → legacy for older models (`:519-524`, `:347-447`).
  - Drop `temperature≠1` under thinking for non-adaptive models (`:526-530`).
  - Encrypted-reasoning blocks are stripped from history (`:565`).
  - The cost map has `claude-opus-5-5`/`claude-sonnet-5-5` with `supports_adaptive_thinking=True` and `thinking_always_on=True` (`S/model_prices_and_context_window_backup.json`, checked by script).
- **System**: forwarded as-is. Billing-header blocks are kept for first-party anthropic (`should_strip_billing_metadata` → False, `:124-131`). `role:"system"` messages are **not** normalized on the first-party path (`:194-196`).
- **cache_control**: forwarded untouched. `scope` is stripped only by the Vertex/Azure subclasses (`:94-122`).
- **tool_use / tool_result**: forwarded untouched. Advisor blocks are stripped if no advisor tool (`:557-562`).
- **Beta headers**: client `anthropic-beta` reaches the call through `provider_specific_header`, scoped to anthropic/bedrock/bedrock_mantle/vertex_ai (`S/proxy/litellm_pre_call_utils.py:3478-3512`; header set `S/types/llms/anthropic.py:723-726`). Headers are merged forwarded < extra_headers < provider_specific (`S/llms/custom_httpx/llm_http_handler.py:2142-2152`). The proxy then auto-adds betas for features it detects (`transformation.py:610-709`). There is no filtering for provider `anthropic` (`:91-92`; `llm_http_handler.py:2166-2167`).
- **`?beta=true`**: the URL is always `{api_base}/v1/messages` (`transformation.py:232-244`), so the query string never reaches kiro. Auth copies query params into the parsed body with `setdefault` (`S/proxy/common_utils/http_parsing_utils.py:669-674`, called from `S/proxy/auth/user_api_key_auth.py:1381`). The endpoint's later `_read_request_body` returns only the keys cached on first parse (`http_parsing_utils.py:303-309,338`), so `beta` should not land in the provider body. **Not verified at runtime.**

### 1.4 Bridged transformation
**Responses bridge** (`responses_adapters/transformation.py`):
- `system` → `instructions`, as text blocks joined by `\n` (`:543-549`). If any system block carries `prompt_cache_breakpoint`, it becomes a leading `developer` message instead (`:520-534`).
- Mid-turn `role:system` → a system message item (`:234-244`).
- user `tool_result` → `function_call_output`; images inside it go into a separate user message (`:286-344`).
- assistant `thinking`/`redacted_thinking` → replayed `reasoning` items; `tool_use` → `function_call` (`:189-212,363-382`).
- `thinking` → `reasoning.effort`: adaptive uses `output_config.effort`, otherwise `medium`; enabled is bucketed by `budget_tokens` (`:460-496`).
- `include=["reasoning.encrypted_content"]` when the provider supports `include` (`:540-541`; `responses_adapters/handler.py:38-44`).
- `metadata.user_id` → `user` + `prompt_cache_key` (`:613-619`).
- `cache_control` is **dropped**: only `prompt_cache_breakpoint` is carried (`:267-284`).
- `web_search*` tools → `web_search_preview` (`:396-399`).
- The logging obj `call_type` is forced to `anthropic_messages` (`responses_adapters/handler.py:131-142`).

**Chat bridge** (`adapters/transformation.py`):
- `tool_result` → `role:tool` messages (`:526-533`).
- Assistant thinking → `thinking_blocks` + `reasoning_content` (`:549-634`).
- `thinking` → kept for Claude, else `reasoning_effort` (`:703-735`).
- `cache_control` is kept only if the target "consumes" it (Claude / Bedrock ARN / gemini) (`:382-412,699-700`).

## 2. Streaming

### 2.1 Native (`kiro-claude-*`, deepseek-anthropic, …)
Chain, innermost first:
1. `httpx` post with `stream=True` (`llm_http_handler.py:2256-2273`).
2. `AnthropicMessagesConfig.get_async_streaming_response_iterator` → `PassThroughStreamingHandler.chunk_processor` (`messages/transformation.py:587-608` → `messages/streaming_iterator.py:431-451`). This yields **raw upstream transport bytes verbatim** (`proxy/pass_through_endpoints/streaming_handler.py:196-209`).
3. `AnthropicMessagesStreamingResponse` (`llm_http_handler.py:2293-2302`). If an agentic completion hook is registered, `AgenticAnthropicStreamingIterator` goes in between (`:2304-2328`).
4. Router `FallbackAwareAnthropicMessagesStream` (`router.py:5458-5625`).
5. Proxy `async_post_call_streaming_iterator_hook` chain, where our `streaming_bridge` lives (`common_request_processing.py:3912-3916`).
6. `AnthropicStreamModelRestamper` (`common_request_processing.py:4040`; `anthropic_endpoints/streaming_model_restamp.py:94-174`) rewrites `message_start.message.model` to the client-requested name.
7. `wrap_sse_stream_with_keepalive_pings`, interval `litellm.anthropic_sse_ping_interval_seconds = 15.0` (`S/__init__.py:266`; `common_request_processing.py:2795-2798`). It writes `event: ping\ndata: {"type": "ping"}\n\n` (`S/constants.py:599`) whenever the stream is idle 15 s (`proxy/common_utils/sse_keepalive.py:63-79`).

So every SSE event (message_start, content_block_*, message_delta, message_stop, ping, error) is **produced by the upstream**. LiteLLM does not synthesize them on the native path, except:
- 15 s keepalive pings;
- the restamped `message_start.model`;
- the router's buffering of lifecycle frames before the first `content_block_delta`, done while a fallback could still take over (`router.py:5547-5606`; ping forwarded live, `:528-534`; buffer cap 200, `:525`).

**Native mid-stream failure:**
- Upstream `event: error` frame (e.g. `overloaded_error`) before any content: the router parses it (`router.py:5562`), maps type → status via `ANTHROPIC_ERROR_STATUS_CODE_MAP` (`S/llms/anthropic/common_utils.py:63-74`: invalid_request 400, authentication 401, permission 403, not_found 404, rate_limit 429, api 500, overloaded 503, timeout 504), and raises `MidStreamFallbackError` if 429/≥500 (`router.py:537-538,5586-5600`) → fallback chain (`:5657-5734`). After content, the frame is forwarded verbatim (`:5601-5604`).
- Transport exception inside `chunk_processor`: failure logging with partial usage is scheduled (`streaming_handler.py:243-259`) and re-raised. The router falls back only if no content has been sent and the status is retriable or absent (`router.py:576-593,5607-5616`). Otherwise the exception reaches `async_streaming_data_generator`. That function runs `post_call_failure_hook` and yields `anthropic_error_sse_frame(status, message)` (`common_request_processing.py:3979-4007,4047-4050`). It first yields a seal (`\n` + ping frame) when the last bytes were mid-frame (`sse_keepalive.py:171-175`). If this is the very first chunk, `create_response` turns it into a JSON error with the real status (`common_request_processing.py:1009-1018`).
- **Upstream ends early (EOF without `message_stop`)**: on this path nothing checks for a terminal event. `chunk_processor` just ends (`streaming_handler.py:196-242`), and success logging runs from the `finally` (`:268-275`). The client gets a 200 stream that stops mid-message. The `api_error` "Provider stream ended before emitting a message_stop event" exists **only** in `BaseAnthropicMessagesStreamingIterator._pump_upstream_to_queue` (`messages/streaming_iterator.py:657-664`). That pump is used by Bedrock invoke (`S/llms/bedrock/messages/invoke_transformations/anthropic_claude3_transformation.py:846`), not by first-party anthropic. Logging does set `prompt_cache_response_complete=False` in that case (`streaming_handler.py:304-314`) and re-tokenizes the output (`anthropic_passthrough_logging_handler.py:363-403`).
- **Client disconnect**: `GeneratorExit` propagates down. `chunk_processor`'s `finally` enqueues success logging on the rooted worker with whatever bytes it has (`streaming_handler.py:260-275`). The upstream is **not** drained further (unlike the Bedrock pump, `messages/streaming_iterator.py:610-667`). So `message_delta.usage` is lost, and output tokens are re-estimated by tokenizing the received text (`anthropic_passthrough_logging_handler.py:363-403`). The outer generator marks the disconnect and releases budget only if nothing was delivered (`common_request_processing.py:3962-3978,3818-3847`).

### 2.2 Bridged — Responses target (`AnthropicResponsesStreamWrapper`, `responses_adapters/streaming_iterator.py:101-494`)

Event mapping (`:105-115`, implementation `:209-431`):

| Responses event | Anthropic event |
|---|---|
| `response.created` | `message_start` (all usage zeros, `:140-158`) |
| `output_item.added` (message / function_call) | `content_block_start` text / tool_use |
| `output_text.delta` | `text_delta` (opens a text block if none, `:276-280`) |
| `reasoning_summary_text.delta` | `thinking_delta` |
| `function_call_arguments.delta` | `input_json_delta` |
| `output_item.done` | `content_block_stop`; reasoning items add a `signature_delta` (encrypted_content) or a `redacted_thinking` block (`:181-207`) |
| `completed` / `incomplete` | `message_delta` (stop_reason `max_tokens` / `refusal` / `tool_use` / `end_turn`, usage translated, `:384-431`) + `message_stop` |
| `response.failed` | error event via `stream_error_status_and_message` (`:369-381`) |

**No `ping` is emitted by the wrapper**; pings come only from the proxy's 15 s keepalive.

**Mid-stream failure in 1.105.1 (quoted, `responses_adapters/streaming_iterator.py:451-484`):**
```python
        try:
            if hasattr(self.responses_stream, "__aiter__"):
                async for event in self.responses_stream:
                    self._process_event(event)
                    if self._chunk_queue:
                        return self._chunk_queue.popleft()
            ...
        except StopAsyncIteration:
            pass
        except Exception as e:  # noqa: BLE001  # every upstream failure becomes a client error event
            verbose_logger.exception(
                "AnthropicResponsesStreamWrapper: upstream Responses stream for %s failed", self.model
            )
            self._fail_stream(*_failure_status_and_message(e))

        if not self._chunk_queue and not self._sent_message_stop and not self._stream_failed:
            verbose_logger.error(
                "AnthropicResponsesStreamWrapper: upstream Responses stream for %s ended without a terminal event",
                self.model,
            )
            self._fail_stream(500, INCOMPLETE_STREAM_ERROR_MESSAGE)
```
- `_failure_status_and_message` unwraps the `MidStreamFallbackError` chain (`:71-85`). The status is the original's `status_code` if 400-599, else 500. The message is redacted (`:88-98`; `S/_logging.py:120-125`).
- `transform_to_anthropic_error` maps status → type with `ANTHROPIC_ERROR_TYPE_MAP` = {400 invalid_request_error, 401 authentication_error, 403 permission_error, 404 not_found_error, 413 request_too_large, 429 rate_limit_error, 500 api_error, 529 overloaded_error}, default `api_error` (`S/anthropic_interface/exceptions/exception_mapping_utils.py:17-26,37-39`). **503/502/504 → `api_error`, not `overloaded_error`.** If the raw message is already an Anthropic error JSON, it is passed through as-is (`:152-157`).
- For `response.failed`, status comes from `code`/`type` (`S/responses/streaming_iterator.py:192-255`): `rate_limit*`/`insufficient_quota` → 429, `server_error` → 500, invalid_* → 400, numeric codes kept, default 500.
- After failure: `_stream_failed` → `StopAsyncIteration`, no `message_stop` (`:443-444`).
- **Early end**: `api_error` "Provider stream ended before emitting a message_stop event…" (`:474-479`; message text `messages/utils.py:18-21`).
- **Client disconnect**: the wrapper has no `aclose`/finally. Teardown is the outer generator's `response.aclose()` (`common_request_processing.py:3849-3856`) through the router wrapper. Whether the inner `ResponsesAPIStreamingIterator` socket is closed depends on our `responses_aclose` patch (`CB/responses_aclose.py:1-40`). Not verified for this path.

### 2.3 Bridged — chat target (`AnthropicStreamWrapper`, `adapters/streaming_iterator.py`)
- `CustomStreamWrapper` chunks → Anthropic dict events (`:783-1011`). On end: holds `stop_reason`, emits `content_block_stop` → `message_delta` → `message_stop` (`:986-1011`).
- Exceptions are converted at the SSE boundary: `async_anthropic_sse_wrapper` does `except Exception: yield _mid_stream_error_sse_event(e)` (`:1029-1044`). Status comes from `BaseLLMException`/`MidStreamFallbackError.status_code`, else 500 (`:65-77`).
- Because `CustomStreamWrapper` turns any 5xx/429/unknown-status mid-stream error into `MidStreamFallbackError` (`S/litellm_core_utils/streaming_handler.py:2310-2325`), and that class defaults to 503 (`S/exceptions.py:1138-1139`), many such errors become `api_error`.
- **Ordering caveat**: the error frame is produced *inside* the bridge, so the router wrapper sees an SSE error frame rather than a raised exception. Pre-content it still triggers a fallback through `parse_anthropic_error_event` (`router.py:5562-5600`). Post-content it is forwarded.
- **Early end** on the chat bridge: `httpx.ReadError`/`RemoteProtocolError` before a finish_reason is treated as a failure (`streaming_handler.py:2094-2097`). A clean EOF without finish_reason still synthesizes `message_stop` (`adapters/streaming_iterator.py:1008-1010`). So a truncated chat upstream looks like a normal end to the client. Not verified at runtime.
- **Disconnect**: `AnthropicSSEStream` exposes `chunks/messages/model` (`adapters/streaming_iterator.py:1204-1233`), so `_bill_partial_streamed_spend_on_disconnect` rebuilds partial usage from the chunks (`common_request_processing.py:396-445`).

## 3. Overlays vs upstream 1.105.1

`diff` of each `OV/*.py` against `S/` reproduces the `.diff` files exactly: +15, +8, +18/−4 lines. Everything else in the three files is byte-identical to 1.105.1.

### 3.1 `anthropic_passthrough_logging_handler.py` (+15, in `_compute_response_cost` before `completion_cost`)
- **What it does**: if `usage.server_tool_use` is a plain dict, replace it with `anthropic.types.server_tool_usage.ServerToolUsage(web_search_requests=…)`. If that throws, set it to `None`.
- **Still needed? Very likely not.** On 1.105.1 every producer of the usage object already coerces a dict into litellm's own `ServerToolUse`:
  - `Usage.__init__` (`S/types/utils.py:1962-1966`);
  - `stream_chunk_builder` (`S/litellm_core_utils/streaming_chunk_builder_utils.py:918-928`);
  - `AnthropicConfig.calculate_usage` builds `ServerToolUse` itself (`S/llms/anthropic/chat/transformation.py:2455-2462`).
  
  The cost code also reads it tolerantly, dict or attribute (`S/litellm_core_utils/llm_cost_calc/utils.py:126-153`). The branch therefore should never fire. Not verified at runtime: no counter exists.
- **Latent hazard**: in the anthropic SDK I could inspect locally, `ServerToolUsage` requires both `web_fetch_requests` and `web_search_requests`. Constructing it with only `web_search_requests` would then raise and fall to `None`, which drops web-search pricing. The SDK version inside the image is not verified.
- **Upgrade risk**: this is a whole-file replacement of 1209 lines. It was clean only because upstream did not touch the file between 1.103.2 and 1.105.1 (measured: 0 changed lines). Upstream fixes to interrupted-stream recovery or cache usage in this file would be silently reverted. **Recommendation: drop it,** after a gray run that logs whether the dict branch ever fires.

### 3.2 `litellm_core_utils/streaming_handler.py` (+8, in `_handle_stream_fallback_error` before `raise MidStreamFallbackError`)
- **What it does**: calls `self.logging_obj.failure_handler(mapped_exception, "]]]]END]]]" + response_uptil_now)` synchronously. The goal is that the router's sync `deployment_callback_on_failure`, which handles cooldown (`S/router.py:1179-1183,8242`), runs *before* the fallback picks a deployment.
- **Applies only where `CustomStreamWrapper` is used**: the `/v1/messages` chat bridge and `/v1/chat/completions`. It does not apply to the native or Responses-bridge `/v1/messages` paths.
- **Upstream already runs failure handling, just asynchronously**:
  - async path: `asyncio.create_task(dispatch_failure_handlers(...))` (`streaming_handler.py:2087-2093,2208-2216`);
  - sync path: a thread (`:1957-1960`).
  
  `dispatch_failure_handlers` submits the sync `failure_handler` to an executor whenever a non-internal sync failure callback exists (`S/litellm_core_utils/litellm_logging.py:2238-2245,4019-4035,4077-4091`). The router callback is not filtered out. So the overlay's only real gain is ordering: the cooldown lands before the fallback is chosen. Whether 1.105.1's native multi-hop midstream (README: #42283/#43133/#43238) makes this unnecessary is not verified.
- **Side effects (per code, not verified at runtime)**:
  1. Possible double-count toward `allowed_fails`. `has_run_logging` never sets the dedup flag for streams (`litellm_logging.py:2269-2279`), so both the overlay call and the upstream executor call reach `deployment_callback_on_failure`.
  2. It runs all sync failure callbacks inline on the event loop.
  3. It writes the generated text into `traceback_exception` (`litellm_logging.py:3619-3621`). That field can then show up wherever tracebacks are logged.
- **Upgrade risk**: whole-file, 2502 lines. Upstream changed 18 lines between 1.103.2 and 1.105.1 (service_tier stamping, `_stamp_served_service_tier`). An overlay that was not rebuilt would silently drop that.

### 3.3 `proxy/pass_through_endpoints/streaming_handler.py` (+18/−4)
- **What it does**: `_stamp_first_chunk_if_needed(chunk, …)` skips empty chunks and chunks whose first non-blank byte is `:`. Upstream stamps `completion_start_time` on the first transport chunk of any kind (`S/proxy/pass_through_endpoints/streaming_handler.py:64-67,198,219`). That is still true in 1.105.1, so the overlay still changes behavior.
- **Limits**:
  - It inspects **upstream** bytes only. Our own `: keepalive` heartbeats are injected downstream and never pass through here. It helps only if the upstream itself sends `:` comment frames; whether kiro at `10.43.109.5:8990` does is not verified.
  - An upstream `event: ping` still stamps.
  - For `claude-code-*` keys, `streaming_bridge` overwrites `completion_start_time` anyway at the first `content_block_delta` (`CB/streaming_bridge.py:1501-1505`; `_update_completion_start_time` overwrites unconditionally, `litellm_logging.py:2305-2307`). The overlay therefore only affects non-`claude-code-` keys, e.g. `cursor-*` on `/v1/messages`.
- **Upgrade risk**: highest of the three. Upstream changed 62 lines in this file between 1.103.2 and 1.105.1 (TinyFish billing, `PassThroughStreamContext`, partial-usage failure logging). An overlay replayed from an old base would revert the 1.105 failure-billing code.
- **Lower-risk alternative**: monkey-patch only `PassThroughStreamingHandler._stamp_first_chunk_if_needed`. It is a staticmethod, but the call sites don't pass the chunk, so this would need a small call-site patch instead.

## 4. Callback patches: assumptions vs 1.105.1

| Patch | Intercepts | Assumptions on 1.105.1 | Verdict |
|---|---|---|---|
| `streaming_bridge` (a) `__init__` patch | `BaseAnthropicMessagesStreamingIterator.__init__` (`CB/streaming_bridge.py:966-1019`) | Import path `pass_through` exists (`S/llms/anthropic/pass_through/messages/streaming_iterator.py:382`). Signature `(self, litellm_logging_obj, request_body)` matches (`:388-392`). Upstream still sets `start_time=datetime.now()` (`:395`) and the iterator is still built *after* the HTTP post returns (`llm_http_handler.py:2256` then `:2285`). Its `start_time` is what feeds logging (`messages/streaming_iterator.py:448`). | **Holds, still needed** (native path only) |
| `streaming_bridge` (b) httpx read-timeout | mutates the cached ANTHROPIC client and rebinds `http_handler.get_async_httpx_client` (`CB/streaming_bridge.py:758-856`) | `llm_http_handler` imports the function **by name** (`S/llms/custom_httpx/llm_http_handler.py:104-109`, used at `:2127`), so the rebinding is invisible to it. The client cache key includes the event-loop id (`S/caching/llm_caching_handler.py:46-63`) and entries expire after 3600 s (`S/constants.py:298`; `http_handler.py:1758-1763`). 1.105.1 also passes an explicit per-request timeout when `stream_timeout`/`timeout`/`request_timeout` are configured (`llm_http_handler.py:2081-2104,2268-2272`), and that wins over the client default (`http_handler.py:814-815`). The docstring premise "LiteLLM doesn't forward request_timeout" is **false on 1.105.1**. The canary yaml sets the env to `600` (`k8s/litellm-proxy-canary.yaml:150-151`), which makes this a no-op anyway. Prod value not verified. | **Likely ineffective/redundant** |
| `streaming_bridge` (c) iterator hook | `async_post_call_streaming_iterator_hook` (`CB/streaming_bridge.py:1095-1555`) | Gate `logging_obj.call_type == "anthropic_messages"` holds on all three paths (`common_request_processing.py:2169-2176`; the Responses bridge forces it, `responses_adapters/handler.py:141`). Chunks are bytes on all three paths. The 25 s heartbeat is now mostly redundant: upstream sends `event: ping` every 15 s outside the hook (`common_request_processing.py:2795-2798`). The `[DONE]` egress filter still matters (raw bytes are relayed). The stall frame sends `error` **followed by `message_stop`** (`:696-723`), which is not Anthropic-spec, and it swallows the exception, so `post_call_failure_hook` doesn't run. | Mixed. See §6 for the collapse-mode risk |
| `anthropic_passthrough_pingfix` | wraps `ModelResponseIterator.convert_str_chunk_to_generic_chunk` (`CB/anthropic_passthrough_pingfix.py:129-186`) | Class and method exist (`S/llms/anthropic/chat/handler.py:545,1267-1290`) and are still the class the logging handler uses (`anthropic_passthrough_logging_handler.py:19-21,717-741`). But that only caller now catches `json.JSONDecodeError` itself (`:745-752`) and skips `data: [DONE]` (`:735-736`). The fast path returns `None` on bad JSON (`:649-652`) and any assembly exception falls back to usage-only cost (`:187-201`). The bug it fixed (all success logging dropped) cannot happen on 1.105.1. | **Redundant**. Removable (low risk either way) |
| `anthropic_bridge_error_surface` | would replace `AnthropicResponsesStreamWrapper.__anext__` | Refuses because the fingerprint check `"AnthropicResponsesStreamWrapper error:" in src and "Drain any remaining queued chunks" in src` (`CB/anthropic_bridge_error_surface.py:121-130`) fails. grep over the whole 1.105.1 tree: **0 hits for either string**. The 1.105.1 `__anext__` (quoted in §2.2, `responses_adapters/streaming_iterator.py:440-484`) already turns exceptions into an error event, stops after failure (`:443-444`), and flags early EOF (`:474-479`). | **Correct refusal**. Behavior difference: upstream sends `api_error` for 500/503 and `rate_limit_error` for 429, with a redacted upstream message. Ours always sent `overloaded_error` + `"Overloaded"`. Whether Claude Code retries `api_error` the same way is not verified. |
| `force_stream` | `async_pre_call_hook` (`CB/force_stream.py:151-189`) | Signature matches (`S/integrations/custom_logger.py:413-421`). Called with `call_type=route_type="anthropic_messages"` (`common_request_processing.py:2210-2215`). Sets `data["stream"]=True` and `litellm_metadata._force_stream_collapse`. | Holds. See §6 risk |
| `opus_47_fix` | `async_pre_call_hook` (`CB/opus_47_fix.py:104-141`) | Matches only names containing `opus-4-7`/`opus_4_7`/`opus-4.7`/`opus_4.7` (`:27-32`), so it is a **no-op for `*-5-5`**. On the native anthropic path 1.105.1 already does enabled→adaptive and reasoning_effort→output_config (`messages/transformation.py:500-517`; `common_utils.py:747-775`). It can still matter on the chat bridge, where Claude `thinking` is forwarded verbatim (`adapters/transformation.py:703-735`). It reads `data["metadata"]` before `litellm_metadata` (`:119`). On `/v1/messages`, `metadata` is the client's Anthropic `{user_id}`, so `model_group` is never found. | Mostly redundant. Narrow scope |

## 5. Spend and usage for `/v1/messages` streams

### 5.1 Native path (kiro)
- **Collection**: raw transport bytes are accumulated in `chunk_processor` (`proxy/pass_through_endpoints/streaming_handler.py:152,197`). At end or teardown, `_route_streaming_logging_to_handler` → `_build_passthrough_logging_result` runs (in a worker thread) → `AnthropicPassthroughLoggingHandler._handle_logging_anthropic_collected_chunks` (`streaming_handler.py:277-469`; handler `:469-514`).
- **Assembly**:
  - The pure-text fast path collapses text deltas (`anthropic_passthrough_logging_handler.py:541-697`). Otherwise the legacy path runs `ModelResponseIterator.convert_str_chunk_to_generic_chunk` → `chunk_parser` per event, then `stream_chunk_builder` (`:700-759`).
  - Usage comes from `message_start.message.usage` and `message_delta.usage`, merged cumulatively (`chat/handler.py:635-646,987-1012,1096-1115`).
  - Then `AnthropicConfig.calculate_usage` runs (`chat/transformation.py:2363-2467`): `prompt_tokens = input_tokens + cache_creation_input_tokens + cache_read_input_tokens` (`:2399-2405`); `prompt_tokens_details.cached_tokens = cache_read`, `cache_creation_tokens = cache_creation`, `text_tokens = raw input` (`:2430-2436`); the 5m/1h split goes to `cache_creation_token_details` (`:2378`).
- **Fallback**: if assembly fails, `_build_usage_only_response_from_chunks` reads the same fields straight from SSE (`:777-894`, incl. `cache_creation.ephemeral_5m/1h`, `:818-821,851-853`).
- **Cost**: `_compute_response_cost` (`:272-293`) → `litellm.completion_cost(model="anthropic/<model>", custom_pricing=use_custom_pricing_for_model(litellm_params), router_model_id=model_info.id)`.
  - `use_custom_pricing_for_model` is True when the deployment's `model_info` (under `litellm_metadata`) has any custom-pricing key (`S/litellm_core_utils/litellm_logging.py:5567-5596`).
  - With custom pricing, the model name used is `router_model_id`, if that entry prices anything (`S/cost_calculator.py:850-858`). The router registers each deployment's `model_info` under its id (`S/router.py:10108-10113`).
  - So a kiro deployment with `input_cost_per_token`/`output_cost_per_token` in model_info is priced from its own id entry, not from `claude-opus-5-5`'s built-in price ($4e-6/token input in the built-in map, checked by script).
  - The cost goes into `kwargs["response_cost"]` and `model_call_details["response_cost"]` (`:436-440`). The success handler keeps a non-zero existing cost (`litellm_logging.py:2419-2423`).
  - Our `pricing_overlay` wraps `litellm.completion_cost` with ×1.3 (`CB/pricing_overlay.py:93-118`). This path calls `litellm.completion_cost` by attribute (`anthropic_passthrough_logging_handler.py:285`), so ×1.3 applies. Whether `stream_chunk_builder`'s internal `_response_cost_calculator` also stamps a cost that later wins, and whether that is multiplied too, is not verified.
- **Cache pricing caveat (not verified)**: when a custom-priced deployment's `model_info` has no `cache_read_input_token_cost`/`cache_creation_input_token_cost`, the cost code falls back to the **full input rate** for cache tokens (`S/litellm_core_utils/llm_cost_calc/utils.py:763-771`). `register_model` warns these "will default to 0" when no built-in entry matches (`S/utils.py:3386-3397`). Which one wins for a UUID-keyed kiro entry is not verified. If kiro rows lack cache prices, cache reads could be billed 10× too high (full input) or at $0. **Check: `/model/info` for kiro deployments + one SpendLogs row with cache_read>0.**

### 5.2 Where usage is lost or distorted
1. **Client disconnect on native**: upstream draining stops. Only bytes already received are billed (`streaming_handler.py:260-275`). With no `message_delta`, output tokens are re-estimated from the received text with `litellm.token_counter` (`anthropic_passthrough_logging_handler.py:363-403`). Thinking/tool-JSON tokens are only partly counted, because `get_content_from_model_response` reads content + tool_calls (`S/litellm_core_utils/prompt_templates/common_utils.py:305-326`). Input + cache tokens from `message_start` are kept. **This is under-billing (not zero), and kiro still generates and is billed for the rest upstream.**
2. **Upstream EOF without `message_stop` (native)**: logged as a *success* row with partial usage. No error is surfaced (§2.1).
3. **Native first-party anthropic does not use the detached-drain pump.** The Bedrock-style `async_sse_wrapper` (drain to the end on disconnect, `messages/streaming_iterator.py:468-525,610-667`) is not on our path.
4. **Upstream ≥400 response**: `chunk_processor` skips success logging (`streaming_handler.py:235-239,268`). Failure is logged by the caller. Fine.
5. **Responses bridge**: usage comes only from the terminal `response.completed/incomplete` (`responses_adapters/streaming_iterator.py:408-427`). `message_start` carries zeros (`:151-156`). On disconnect the bridge yields no partial cost of its own. Billing depends on `ResponsesAPIStreamingIterator`'s own terminal/partial logging (`S/responses/streaming_iterator.py:512-518,2018-2031`). `_bill_partial_streamed_spend_on_disconnect` needs `response.chunks`, which the Responses wrapper doesn't expose (`common_request_processing.py:425-427`). **Likely zero-or-partial on disconnect. Not verified.**
6. **Logging worker timeouts**: success/failure logging coroutines on `GLOBAL_LOGGING_WORKER` are cancelled after `LOGGING_WORKER_MAX_TIME_PER_COROUTINE`, default 20 s (`S/constants.py:635`; `S/litellm_core_utils/logging_worker.py:168-187`). The re-tokenize path runs inside it (`streaming_handler.py:350-360`). Very large Claude Code histories could hit it. Not verified. This is a candidate to cross-check against the SpendLogs-drop finding (`finding-spendlog-drop.md`), which currently points at prisma rejects instead.
7. **Our `streaming_bridge` stall/collapse paths** `return` instead of raising (`CB/streaming_bridge.py:1405-1408,1476-1480,1229-1234,1262-1274`). The upstream iterator is cancelled in `finally` (`:1549-1555`), which closes `chunk_processor` with `GeneratorExit` → partial success logging. The client sees an error or synthetic message, but SpendLogs shows status=success. That is consistent with the "fake success" rows the bridge docstring itself describes.

## 6. Surprising / risky / likely misunderstood

1. **Collapse mode × upstream keepalive (hypothesis, not verified).** `force_stream` flips non-streaming requests to stream. `streaming_bridge` collapse mode emits `b" "` heartbeats and then one JSON body (`CB/streaming_bridge.py:1145-1290`). But on 1.105.1 the outer generator is wrapped by `wrap_sse_stream_with_keepalive_pings` (15 s) **outside** our hook (`common_request_processing.py:2795-2798`; `sse_keepalive.py:63-79`). Our 25 s heartbeat is a separate clock. If 15 s pass with no yield, upstream writes `event: ping\ndata: {"type": "ping"}\n\n` into the body. For a non-streaming client expecting JSON, the body would then begin with an SSE frame. The `b" "` yields happen at most every 25 s, so a 15 s gap is expected during long thinking.
   - Falsification test: a non-streaming `/v1/messages` with a `claude-code-*` key and a prompt with >15 s TTFT. Check whether the raw body contains `event: ping`. Set `litellm_settings.anthropic_sse_ping_interval_seconds: null` or lower `STREAMING_BRIDGE_HEARTBEAT_SECONDS` below 15 if confirmed.
   - The same wrapper existed in 1.103.2 (`.research-litellm/litellm-1.103.2/litellm/proxy/common_request_processing.py:2707`), so if real, this is not new in 1.105.
2. **`streaming_bridge` pass-through stall frame** emits `event: error` followed by `event: message_stop` (`CB/streaming_bridge.py:696-723`). Upstream deliberately ends after an error without `message_stop` (`responses_adapters/streaming_iterator.py:114,443-444`). Some strict SDKs may treat the `message_stop` as a successful end. Not verified with Claude Code.
3. **The native path does no truncation detection.** That gap is upstream's, not ours: only Bedrock gets `INCOMPLETE_STREAM_ERROR_MESSAGE`. It matters for kiro, whose upstream (`10.43.109.5:8990`) is a non-Anthropic gateway.
4. **503 → `api_error`.** Anthropic's `overloaded_error` is 529 in the HTTP→type map (`exception_mapping_utils.py:25`) but 503 in the reverse map (`common_utils.py:71`). The round-trip is lossy: an upstream `overloaded_error` turned into a status and back becomes `api_error`. Claude Code's retry policy for `api_error` vs `overloaded_error` is not verified.
5. **"httpx read-timeout tightened to 120 s" is likely not in effect** (§4). 1.105.1 honors `stream_timeout` per request anyway. If prod relies on the env, measure: a stalled kiro stream should die at `stream_timeout`, not at the env value.
6. **Heartbeats are double**: upstream 15 s `event: ping` plus ours at 25 s. Ours is now redundant for Cloudflare except in collapse mode.
7. **TTFT**: on the native path `completion_start_time` is stamped by `chunk_processor` on the first transport chunk (`streaming_handler.py:198,219`), possibly a ping. `streaming_bridge` overwrites it for `claude-code-*` keys at the first `content_block_delta`. The two sources measure different things, so TTFT across key families is not comparable.
8. **`opus_47_fix` reads `data["metadata"]` first** (`CB/opus_47_fix.py:119`). On `/v1/messages` that is the client's Anthropic metadata, so the `model_group` match never works there. It only catches models named literally `*opus-4-7*`.
9. **Overlay upgrade hazard summary**: `streaming_handler` (passthrough) is the most volatile (62 lines of upstream churn in one minor). The logging-handler overlay protects nothing on 1.105.1 and is pure liability. Prefer function-level patches with fingerprint guards (like `anthropic_bridge_error_surface`) over whole-file overlays.

## Not verified (explicitly)
- Prod values of `STREAMING_BRIDGE_*`, `FORCE_STREAM_*`, `anthropic_sse_ping_interval_seconds`, `stream_timeout`. I only saw the canary yaml in git (`k8s/litellm-proxy-canary.yaml:140-155`, `k8s/litellm-proxy-canary-config.yaml:1335-1336,1423`).
- Whether kiro sends `:` comment frames or `event: ping`, and whether it ever emits `event: error` mid-stream.
- The anthropic SDK version in the image (`ServerToolUsage` required fields).
- Whether `?beta=true` ever reaches the provider body (code says no).
- Cache-token pricing for custom-priced kiro deployments.
