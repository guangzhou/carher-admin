# 03 — /v1/responses 流式路径（LiteLLM v1.105.1 + 198 生产补丁）

只读源码研究，2026-10-20。所有结论都来自读代码；凡标「未验证」的，是没有现场日志或计数器支撑的推断。

路径缩写：
- `S/` = `.work/litellm-src-study-1020/src/litellm/`（v1.105.1 上游）
- `C/` = `.work/litellm-src-study-1020/live-cb/`（生产 callback 补丁）
- `P/` = `litellm-gray-rollout/patches/v1.105.1/`（镜像构建时的源码补丁）
- `SI` = `S/responses/streaming_iterator.py`；`PS` = `S/proxy/proxy_server.py`；`CRP` = `S/proxy/common_request_processing.py`

## 0. 先说结论（按风险从高到低）

1. **`streaming_output_backfill` 里的「非阻塞 _handle_failure」补丁现在既多余又有副作用**。1.105.1 上游已经自己改成非阻塞（`SI:886-918`，用 `_spawn_logging_task` 起后台 task）。我们的补丁（`C/streaming_output_backfill.py:603-644`）把它整个换掉，换完以后：(a) **sync `failure_handler` 不再跑**，而 router 的 cooldown/allowed_fails 计数 `deployment_callback_on_failure` 恰好就挂在 sync 的 `litellm.failure_callback` 上（`S/router.py:1179-1183`，`S/litellm_core_utils/logging_callback_manager.py:84-92`，`S/litellm_core_utils/litellm_logging.py:3722-3795`）；(b) 失败 task 没登记到 `_pending_logging_tasks`，所以上游 `__anext__` 在抛错前「等失败日志写完」那一步（`SI:1040-1045`）等的是空集合。按代码推，**Responses 流内失败（含我们自己升格的 capacity）不会让 router 给这个 deployment 记失败数、也不会触发 cooldown**；`streaming_output_backfill.py:12` 注释里「cooldown 由 __anext__ 既有的 _handle_failure 记账」这一前提已经不成立。现场没验证。
2. **`responses_aclose` 仍然需要，没有被上游取代**。v1.105.1 的 `SI` 全文 0 处 `aclose`。router 的 `stream_with_fallbacks` 收尾时只在 `hasattr(source_iterator, "aclose")` 成立时才关源流（`S/router.py:3408-3417`），没有我们这个补丁，这个分支就跳过了。另外 **chat→responses 桥接路径根本走不到它**：`BaseModelResponseIterator.aclose` 只关 `self.http_response`（`S/llms/base_llm/base_model_iterator.py:73-82`），桥接时这个属性是 None，里面那个 `ResponsesAPIStreamingIterator` 永远不会被关。
3. **上游现在会在「内容已经开始输出之后」继续做 midstream fallback**（续写提示 + 新的一轮生命周期事件），而我们的 backfill 注释说的是「已出内容就放行，没法 failover」（`C/streaming_output_backfill.py:260-265`）。现在的真实情况是：我们放行之后，上游的 `_maybe_raise_for_error_event` 会把同一个 `response.failed` 升格成 `MidStreamFallbackError(is_pre_first_chunk=False)`（`SI:650-671`），router 拿续写输入去重试（`S/router.py:3457-3461`），客户端会在同一条 SSE 里看到第二轮 `response.created…`。现场没验证。
4. **`encrypted_content_degrade_strip` 已经是死代码**：1.105.1 的 `async_filter_deployments` 不再抛异常，自己剥离后照常分发（`S/router_utils/pre_call_checks/encrypted_content_affinity_check.py:378-392`，全文没有 `raise`）。我们包的那层 `except (BadRequestError, RateLimitError, ServiceUnavailableError)`（`C/encrypted_content_degrade_strip.py:140`）进不去。
5. **`codex_compaction_v2` / `deepseek_id_prefix` 的 SSE 字节层改写依赖 contextvar 在同一个 task 里**。只要 keepalive 开着（`PS:9662-9666` 会用 `asyncio.create_task(aiter.__anext__())` 在子 task 里跑 `_process_chunk`），子 task 里 `.set()` 的值传不回负责格式化的那个生成器，两个补丁就会静默失效。keepalive 默认关（`S/__init__.py:267`），生产配置里有没有开没查到。
6. 客户端看到的 `model` 是加在**每个** Responses 事件顶层的额外字段（`PS:9443-9462`），嵌套的 `response.model` 从来不改。#44341 这个烘焙补丁对原生 Responses 路径的**计价没有影响**：费用在 iterator 的 `_process_chunk` 里就已经算好并盖章了（`SI:513`、`SI:1582-1598`），比 restamp 早。它只影响 chat 形态的流（包括 chat→responses 桥接）和 SpendLogs 里存的 response 正文。

## 1. 字节路径（上游 SSE → 客户端字节）

### 1.1 请求进入（只列和流式有关的部分）

| 步骤 | 位置 | 对流的影响 |
|---|---|---|
| `/v1/responses` 入口 | `S/proxy/response_api_endpoints/endpoints.py:212-386` | `native_data_generator = partial(select_data_generator, responses_stream_errors=True)`（`:264`），所以原生 Responses 路径会启用 `ResponsesStreamErrorState`。 |
| 进入 router | `S/router.py:6869-6873` → `_aresponses_with_streaming_fallbacks` `:5416-5428` → 每一跳的 attempt `:5430-5456` | 流式时把每一跳的 iterator 包成 `FallbackResponsesStreamWrapper`（`:3280-3420`）。 |
| `aresponses` | `S/responses/main.py:576-753` | 同步的 `responses()` 放在线程池里跑（`:730-732`）；拿到流式结果直接透传，不做 id 改写（id 改写只针对 `ResponsesAPIResponse`，`:740-748`）。 |
| HTTP 调用 | `S/llms/custom_httpx/llm_http_handler.py:2830-2863` | `post(stream=True)` 之后构造 `ResponsesAPIStreamingIterator(response=httpx.Response, …, request_data=request_context)`。`fake_stream` 时换成 `MockResponsesAPIStreamingIterator`（`:2841-2851`）。 |

### 1.2 Iterator 内部（`SI`，生产版 = 上游 + SSE lifecycle 补丁 + 3 层 `_process_chunk` 包装）

1. **`SSEDecoder().aiter_bytes(response.aiter_bytes())`**（`SI:1035`）。来自 openai SDK。会合并多行 `data:`，但只把 `sse.data` 往下传（`SI:1060`），**`event:` 这个字段名直接丢掉**，事件类型只认 JSON 里的 `type`。注释帧和空事件都没有。
2. **`ResponsesAPIStreamingIterator.__anext__`**。生产里外层是 `streaming_output_backfill._patched_anext`（`C/streaming_output_backfill.py:402-450`），它包着上游 `__anext__`（`SI:1040-1045`），上游 `__anext__` 再调 `_next_event`（`SI:1047-1092`）。打完 lifecycle 补丁后，`_next_event` 每次循环先从 `_pending_events` 里吐一个已经排队的事件（`P/litellm-patch-sse-lifecycle.py:59` async-loop-head）。
3. **`_process_chunk(sse.data)`**（`SI:363-532`），外面套了三层包装，见 §4。上游本体做这些事：
   - **丢弃**：空 data（`:365-366`）；`[DONE]` 只把 `finished` 置 True（`:374-376`）；JSON 解析不了的静默返回 None（`:525-527`）；JSON 不是对象的返回 None（`:524`）。
   - **转换**：`responses_api_provider_config.transform_streaming_response`（`:389-393`）。OpenAI 实现见 `S/llms/openai/responses/transformation.py:625-656`：按 `type` 选 pydantic 类（`:674-730`，没登记的类型一律 `GenericEvent`），`error.code: null` 补成 `"unknown_error"`（`:638-646`），校验失败就退到 `model_construct`（`:650-656`，不做校验，缺字段也不报错）。
   - **改写**：只要 JSON 里有 `response`，就把 `response.id` 改成 litellm 编码过的 id（`:398-408` → `S/responses/utils.py:331-372`）；affinity 开着的时候 item id 也一起改（`utils.py:369-372`）。`output_item.added/done`、`annotation`、`content_part.done` 里的 container_id 会被编码（`SI:420-456`）。affinity 开着的时候，`encrypted_content` 会被包成 `litellm_enc:{b64(model_id)};{原文}`（`SI:459-477`，`utils.py:421-431`）。
   - **累计**：文本 delta 累进 `_generated_content`，工具参数 delta 累进 `_generated_tool_arguments`（`SI:412-419`）。
   - **终止事件**（completed/incomplete/failed）：usage 缺失时用 token 估算补上（`:488-506`，`:1476-1490`）；在 `response.usage.cost` 上盖费用（`:513`，`:1582-1598`，**这个 cost 客户端能看到**）；`completed_response` 赋值（`:512`）；**在这里就派发日志**（`:515-518`），也就是事件还没交给客户端，日志已经开始写了。
4. **`_maybe_raise_for_error_event`**（`SI:650-671`）。遇到 `error` 或 `response.failed` 就抛异常。可回退的（5xx、429、无状态码、内容策略，`:281-285`）包成 `MidStreamFallbackError`，否则抛映射后的异常。**被抛的这个事件本身不会交给客户端。**
5. **`_call_post_streaming_deployment_hook`**（`SI:759-799`）。遍历 `litellm.callbacks`。`_HasPostStreamingDeploymentHook` 是 runtime_checkable 协议（`:135-137`），而 `CustomLogger` 基类本身就定义了这个方法（`S/integrations/custom_logger.py:351-361`），所以**每个 callback 对每个 chunk 都会被 await 一次**。异常一律吞掉（`:798-799`）。
6. **SSE lifecycle 补丁**（只在生产里）：把这次 hook 的结果交给 `_lifecycle_gap_filler.expand(hooked)`，再经过 `_carher_adopt_bare_terminal` 进 `_pending_events`（`P/…sse-lifecycle.py:60`），下一轮循环开头弹出并 `_note_yielded_event`（`:59`）。它会**注入**缺失的 `response.created` / `in_progress` / `output_item.added` / `content_part.added`，在终止事件前**注入**缺失的 teardown；遇到裸 response 对象（没有 `type`，但 `object=response`）会**改写**成 `response.<status>`（`:55` CLASS_BLOCK 的 `expand`）。

### 1.3 Router 外层（`S/router.py:3383-3420`）

`stream_with_fallbacks` 会把 pre-output 生命周期事件（`response.created/in_progress/queued`，`SI:288`）**暂扣**，最多 200 个（`router.py:612-620`），等第一个真正的输出出现才一起放出（`:3386-3393`）。收到 `MidStreamFallbackError` 时暂扣的事件**整批丢弃**，换 fallback 的流（`:3396-3403`；fallback 也失败时才把暂扣事件吐出来，`:3501-3503`）。`FallbackResponsesStreamWrapper.__anext__` 会从转发的 chunk 里嗅探终止事件来设 `completed_response`（`:3375-3376`）。

### 1.4 Proxy 外层

1. **`async_data_generator`**（`PS:9830-10079`）。逐个 chunk 处理：
   - keepalive 包装 `_iter_with_keepalive`（`PS:9889-9897`，`:9634-9682`）：间隔 ≤0 时直接 `await`（`:9653-9660`）；>0 时**每个 chunk 都在子 task 里取**（`:9662-9666`），并注入 `: ping`（`:9900-9902`）。
   - per-chunk hook（`:9904-9911`）可以**改写**。
   - **`_restamp_streaming_chunk_model`**（`:9934-9941`，`:9410-9473`）：见 §6。
   - `error_state.observe_chunk`（`:9952-9953`）。
   - BaseModel 走 `_serialize_streaming_chunk` → `model_dump_json(exclude_none=True, exclude_unset=True)`（`:9554-9560`）。**上游显式发来的 `null` 字段会被丢掉。**
   - **`_format_streaming_sse_chunk`**（`:9603-9606`）：`data: {json}\n\n`。生产里外面有两层包装（`codex_compaction_v2` 和 `deepseek_id_prefix`）做字节级改写，见 §4。调用点 `:9989-9991` 是在运行时按模块全局名取函数，所以 monkey-patch 能生效。
   - 正常结束追加 `data: [DONE]`（`:10017-10018`），**`/v1/responses` 也会加**。
   - 异常走 `post_call_failure_hook`（`:10033-10037`）。native 路径用 `error_state.format_failure` **注入**一个 `event: response.failed` 帧（前提是终止事件还没发过），然后 `[DONE]`（`:10043-10050`；`S/proxy/common_utils/responses_stream_errors.py:131-165`）。
2. **`_wrap_responses_stream_for_container_ownership`**（`CRP:3024-3072`）：透传，在终止 chunk 之前写 container 归属表。
3. **`create_response`**（`CRP:979-1118`）：用独立 task 先缓冲第一个 chunk，同时监听断连（`CRP:878-926`，`:896` 用的是 `ensure_future`）。第一帧是 `data: {"error":{"code":<int>}}` 时改成返回 JSON 错误（`:1020-1049`）。native 路径的错误帧以 `event:` 开头，`_parse_event_data_for_error` 要求以 `data: ` 开头（`CRP:740`），所以**不会被转成 HTTP 错误**，客户端拿到的是 200 + `response.failed`。最后返回 `_UpstreamClosingStreamingResponse`（`:1112-1118`），它的 `__call__` 在 finally 里关闭 body_iterator 和 upstream_generator（`:839-851`）。

## 2. 生命周期与日志计费

| 情形 | 上游 1.105.1 行为 | 生产补丁叠加后 |
|---|---|---|
| `response.created/in_progress` | 改写 id（`SI:398-408`）；router 暂扣（`router.py:3386-3389`） | 缺了就由 gap filler 注入，id 用随机的 `resp_<uuid>`（`P/…sse-lifecycle.py:57` INIT_BLOCK）。**和后面 completed 里那个编码过的 id 对不上。** |
| `output_item.added/done` | 编码 container / encrypted_content（`SI:421-477`） | backfill 按 `output_index` 收集 `output_item.done` 的原始 item（`C/streaming_output_backfill.py:339-345`）；deepseek 给 id 加前缀（`C/deepseek_id_prefix.py:211-245`） |
| `output_text.delta` | 累计 `_generated_content`（`SI:412-415`） | backfill 标记 `_bp_content_started`（`C/…backfill.py:217-218`） |
| `response.completed/incomplete` | 补估算 usage、盖 cost、`completed_response`、`_log_completed_response(is_async=True)` → `dispatch_success_handlers` 用 `asyncio.create_task` 投递（`SI:534-576`，`:560`）。**这个 task 没被登记**（不在 `_PENDING_LOGGING_TASKS` 里）。 | backfill 在上游解析之前把收集到的 items 塞进空的 `response.output`，并加 `response.choices`（`C/…backfill.py:347-368`），所以**客户端收到的 completed 里也会多出 `choices`** |
| `response.failed` | 记 usage/cost（`SI:627-645`）→ `_handle_failure`（`:611-625`）→ 返回事件 → `_maybe_raise_for_error_event` 抛异常（`:1066`，`:650-671`） | backfill 还没出内容、并且分类为 retry 时，**在上游之前**抛 `MidStreamFallbackError`，带 `failed_deployment_id`（`C/…backfill.py:256-337`）。这时上游的 `_handle_logging_failed_response` 不会跑，usage 不入账。 |
| `error` 事件 | 不算终止事件；`_maybe_raise_for_error_event` 抛异常，`_next_event` 的 except 里 `_handle_failure`（`SI:1089-1092`） | 同上，backfill 先截获（`C/…backfill.py:244-254`） |
| 上游**不发终止事件就结束**（EOF 或 `[DONE]`） | `_raise_if_ended_without_terminal_event`（`SI:979-986`）：先 `_handle_failure`；还没开始输出就抛 `MidStreamFallbackError`，否则抛 `APIConnectionError` | 我们的空流升格（`C/…backfill.py:402-450`）只接 `StopAsyncIteration`，**上游已经改成抛异常了，所以这段代码到不了**（见 §5）。裸终止对象由 `_carher_adopt_bare_terminal` 补记 `completed_response`，避免被误判（`P/…sse-lifecycle.py:63`）。 |
| 传输中断 `ReadError/RemoteProtocolError` | 已经有终止事件 → 当作正常结束（`SI:1079-1082`）；否则 `_raise_for_transport_error`（`:988-992`），没开始输出就可回退 | 不变 |
| **`ReadTimeout` 等其它 `httpx.HTTPError`** | `SI:1084-1088` 原样抛出，**不包成 `MidStreamFallbackError`**。router 走 `except Exception`，吐出暂扣事件后 re-raise（`router.py:3404-3407`），**不触发 fallback** | 不变 |
| **客户端断连** | `async_data_generator` 捕获 `GeneratorExit/CancelledError`（`PS:10019-10030`）→ `_finalize_streaming_generator_cleanup`（`CRP:3809-3864`）：先打 499 元数据（`CRP:293-343`），然后 `_bill_partial_streamed_spend_on_disconnect`。它要求 `response.chunks` 是列表（`CRP:426-428`），**Responses iterator 和 FallbackResponsesStreamWrapper 都没有这个属性** → 返回 False → 手动释放并发槽（`CRP:3842-3847`）→ `response.aclose()`（`CRP:3849-3856`）。**上游这里不写 SpendLogs 行**，除非终止事件在断连前已经处理过。 | `responses_aclose.aclose` 在 `completed_response is None` 并且还没记过失败时，补一条 `APIError(499,"AbortedStream…")` 失败行（`C/responses_aclose.py:157-184`）。**没有 usage，cost 是 0/None**，只是让这次请求在明细里看得见。前提是 router 收尾时真的调到了它（`router.py:3410-3412`）。 |

日志机制：
- 上游的 `_handle_failure` 在有事件循环时起后台 task，**先 async 再 sync**（`SI:898-907`，`:920-931`），并把 task 登记进 `_pending_logging_tasks`。`__anext__` 抛异常前会先等这些 task 完成（`SI:1040-1045`，`:948-952`）。
- **生产里 `ResponsesAPIStreamingIterator._handle_failure` 被替换了**（`C/…backfill.py:615-639`）：只起 async task，不登记，不跑 sync `failure_handler`。后果见 §0-1 和 §4.4。
- `has_run_logging` 对 stream 请求不写标记（`S/litellm_core_utils/litellm_logging.py:2269-2279`），所以同一请求内后续的失败日志不会被这个标记挡住。

## 3. 连接生命周期（谁关 httpx.Response）

- **正常 EOF**：httpx 的 `aiter_raw` 读完后自己 `await self.aclose()`（本机 httpx 0.28.1 `_models.py:1037-1063`；**生产镜像里的 httpx 版本没核对**）。
- **以 `[DONE]` 结束**（上游是 litellm acct pod 的话，`PS:10017-10018` 一定会发）：`_process_chunk` 只置 `finished`（`SI:374-376`），`_next_event` 直接抛 `StopAsyncIteration`（`SI:1062-1064`），**不再往下读到 EOF**，所以 `aiter_raw` 的 `aclose` 不会执行。上游 iterator **没有 `aclose`**（`SI` 全文 0 处）。只能靠：
  - router `stream_with_fallbacks` 的 finally：`if hasattr(source_iterator, "aclose")`（`router.py:3408-3417`）。**没有 `responses_aclose` 时这个条件为假。**
  - proxy 清理：`hasattr(response,"aclose")`（`CRP:3849`）。这里的 `response` 是 `FallbackResponsesStreamWrapper`，它确实有 `aclose`（`router.py:3379-3381`），但它只是关闭生成器，最终还是落到上面那个 `hasattr(source_iterator,…)`。
- **异常**：`SI` 不关 response，同样靠上面两层。
- **客户端断连**：`_UpstreamClosingStreamingResponse.__call__` 的 finally（`CRP:839-851`）加上 `async_data_generator` 的 finally（`PS:10070-10079`），最终也是落到 `source_iterator.aclose`。
- **结论：上游 1.105.1 仍然没有 `ResponsesAPIStreamingIterator.aclose`，`responses_aclose` 不是多余的。** 它的属性假设（`finished`、`completed_response`、`_completed_response_logged`、`_failure_handled`、`logging_obj`、`stream_iterator`、`response`、`_handle_failure`）在 1.105.1 里都还在（`SI:311-324`，`:1035`，`:886`）。它在 sitecustomize 里提前 import（`C/sitecustomize.py:67-72`），装载情况在 `eff-table.txt` 里看得到（`RespIter.aclose: 三车道: responses_aclose`）。
- **缺口：chat→responses 桥接路径用不上它。** 链路是：`responses_api_bridge.acompletion` 直接调 `litellm.aresponses`（`S/completion_extras/litellm_responses_transformation/handler.py:292-295`，**不经过 router 的 Responses 包装**），拿到裸的 `ResponsesAPIStreamingIterator`，包成 `OpenAiResponsesToChatCompletionStreamIterator`（`handler.py:342-346`；`transformation.py:983-989`），再包 `CustomStreamWrapper`（`handler.py:347-353`）。`CustomStreamWrapper.aclose` 关的是 `completion_stream`（`S/litellm_core_utils/streaming_handler.py:385-409`），即 `BaseModelResponseIterator.aclose`，而它**只关 `self.http_response`**（`S/llms/base_llm/base_model_iterator.py:73-82`）。全仓只有 `llm_http_handler.py:1078` 会设这个属性，桥接路径不设。所以**内层的 Responses iterator 和它的 httpx 连接在桥接路径上永远不会被显式关闭**，499 补记账也不会跑。GC 回收生成器能不能关掉连接：`aiter_raw` 的 `aclose` 不在 finally 里（httpx `_models.py:1055-1063`），**看起来关不掉**。未验证，建议到 198 上查 CLOSE_WAIT 按路由分布。

## 4. 补丁逐个拆

### 4.1 `_process_chunk` 三层包装：一个 chunk 进来的执行顺序

装载顺序是 ccv2 → backfill → deepseek（最外层最后装，`eff-table.txt`）。所以调用 `self._process_chunk(raw)` 时实际是这样跑的：

1. **deepseek 包装进入**（`C/deepseek_id_prefix.py:524-525`）：进入时不做事，直接调下一层。
2. **backfill 包装进入**（`C/streaming_output_backfill.py:199-370`）：**它是前置处理**，自己 `json.loads` 一遍原始字符串（`:211`）：
   - 打标记：`_bp_content_started`、`_bp_saw_terminal`（`:217-230`）。
   - **可能直接抛** `MidStreamFallbackError`（`:256-337`）。这时下面两层都不会跑，上游的 `_handle_logging_failed_response` 也不会跑，所以这次失败的 usage 不入账（对比 `SI:515-516`、`:627-645`）。
   - 收集 `output_item.done` 的**原始** item（`:339-345`）。
   - 终止事件的 output 为空时用收集到的 items 回填，并加 `choices`，然后重新 `json.dumps`（`:347-368`）。
3. **ccv2 包装**（`C/codex_compaction_v2.py:511-519`）：先调上游 `_process_chunk`，**后置**：如果这条流被标记为 compaction，就 `_ACTIVE.set(True)`。
4. **上游 `_process_chunk`**（`SI:363-532`）。
5. 回到 deepseek **后置**（`C/deepseek_id_prefix.py:526-568`）：模型名含 `deepseek` 时，按 type 给 `evt` 里的 `item.id`、`response.output[].id`、`item_id` 加前缀，**直接改 pydantic 的 extra 字段**（`:103-165`，`:211-245`），并设置 `_ACTIVE_CUSTOM` / `_ACTIVE_COMPACTION` 两个 contextvar（`:546-551`）。

顺序依赖：
- backfill 回填进 completed 的是**没加前缀的原始 id**，deepseek 后置会对 `response.output` 再处理一遍（`:232-234`），规则是确定的（`prefix + id.replace("-","")`，`:192`），所以最终和流里 `output_item.*` 的 id 一致。**这个顺序不能反**：如果 deepseek 跑在 backfill 前面，回填的 items 已经是 dict，`_fix_response_obj` 照样能处理，问题倒不大；但如果 backfill 改成后置，回填进去的就是已经改过的 pydantic 对象，行为会不同。
- 上游先算 `_logging_copy`（`SI:542`），deepseek 后改 id，所以**日志里存的是没前缀的 id，客户端看到的是有前缀的**。
- `_handle_logging_completed_response` 用 `create_task` 派发（`SI:560`），日志 task 拿到的是 copy，后面 restamp 和 deepseek 的修改不会污染它。

### 4.2 每层读取的 `self` 属性在 1.105.1 里还在不在

| 补丁 | 读取的属性 | 1.105.1 |
|---|---|---|
| backfill | `model`、`custom_llm_provider`、`_hidden_params`、`litellm_metadata`、`_bp_*` | 都在（`SI:312`、`:331`、`:341`、`:330`；`_bp_*` 由它自己替换的 `__init__` 设置，`C/…backfill.py:193-197`）。`FallbackResponsesStreamWrapper` 跳过了 base `__init__`（`router.py:3290`），但它的 `_process_chunk` 不会被调用，没有影响。 |
| backfill `_patched_anext` | 假设空流会以 `StopAsyncIteration` 冒出来 | **不成立了**。上游现在空流抛的是 `MidStreamFallbackError` 或 `APIConnectionError`（`SI:1054-1057` → `:979-986`）。`StopAsyncIteration` 只在 `completed_response` 非空时才出现，所以 `_bp_saw_terminal` 一定是 True，**这段升格代码到不了**。 |
| backfill `_patched_handle_failure` | `_failure_handled`、`logging_obj`、`start_time` | 都在。但它**替换掉了上游的新实现**，见 §0-1、§5。 |
| ccv2 | `logging_obj.model_call_details/litellm_params/optional_params` | 都在（`S/litellm_core_utils/litellm_logging.py:918-944`）。 |
| deepseek | `model`、`logging_obj.model`、`logging_obj.model_call_details["tools"]`、`_dsidp_*` | 都在。`tools` 是靠 `**self.optional_params` 展开进去的（`litellm_logging.py:944`）。返回值 `evt` 可能是 None，已处理（`:530`）。 |
| responses_aclose | 见 §3 | 都在。 |

### 4.3 `_format_streaming_sse_chunk` 两层包装（ccv2 → deepseek，最外层是 deepseek）

- 两层都先调原函数拿到 `data: …\n\n` 字符串，再按 contextvar 决定要不要做 JSON 往返改写：ccv2 在 `C/codex_compaction_v2.py:544-558`；deepseek 在 `C/deepseek_id_prefix.py:839-865`，compaction 优先，否则还原 custom tool。没命中 contextvar 时一个字节都不改。
- **contextvar 跨 task 的问题**（只基于代码推导，没有复现）：
  - (a) `create_response` 用 `ensure_future` 在**独立 task** 里取第一个 chunk（`CRP:896`）。
  - (b) router 会暂扣 `created/in_progress`，等第一个输出事件到了才放（`router.py:3386-3393`）。
  - 合起来：第一个输出事件（比如 `output_item.added`）的 `_process_chunk` 跑在首块 task 里，contextvar 也是在那里 set 的；但它是被暂扣事件放出之后才 yield，**格式化发生在 Starlette 的 task 里**，那里 contextvar 还是默认值。结果是**流里第一个输出事件不会被 compaction 包装，也不会被还原成 custom_tool_call**。从第二个事件开始，`_process_chunk` 在新 task 里重新 set，就正常了。
  - 如果 keepalive 间隔 >0（`PS:9662-9666`），**每个** chunk 的 `_process_chunk` 都跑在子 task 里，两个补丁会**整条流都失效**。
  - 生产有没有开 keepalive 没查到（仓库配置里 grep 不到 `sse_keepalive_ping_interval_seconds` / `keepalive_seconds`）。
- `deepseek_id_prefix._rewrite_chunk` / `_rewrite_sse_bytes`（`:578-608`）没有任何调用方，是死代码。

### 4.4 `streaming_output_backfill` 其它两段

- **`responses_failed_deployment_stamp`**（`:480-577`）：包了 `_ageneric_api_call_with_fallbacks_helper`。但 **1.105.1 上游 helper 已经自己盖 `failed_deployment_id` 了**（`S/router.py:5406-5414` → `:3899-3907`，幂等），所以这一层多余，不过无害（它不覆盖已有值，`:542`）。注意：helper 只覆盖「建流时」抛的异常。**流中途的异常**里，只有 backfill 自己升格的那种带 id（`:321-330`）；上游 `_maybe_raise_for_error_event` / `_fallback_error` 构造的 `MidStreamFallbackError` **不带** `failed_deployment_id`（`SI:664-671`，`:959-967`），于是 `_maybe_run_weighted_failover` 直接返回 None（`router.py:7209-7211`），**不会在同组里换号重试，直接跳到跨组 fallback**。
- **非阻塞 `_handle_failure`**（`:600-651`）：见 §0-1。补一点：如果 router 的路由策略里带 weighted_affinity，它的 `async_log_failure_event` 会给 deployment 打 fail-mark（`C/weighted_affinity.py:543-571`；`MidStreamFallback` 在 TRANSIENT 列表里，`:147-156`）。这条走的是 async 路径，所以**流内失败时号还是会被临时避开，只是不经过 router 的 allowed_fails/cooldown**。线上是不是用的 weighted_affinity 策略，没核对。

### 4.5 `responses_tool_args_done_backfill`（只在 chat→responses 桥接路径上）

- 包装 `OpenAiResponsesToChatCompletionStreamIterator.chunk_parser`（`C/responses_tool_args_done_backfill.py:79-127`）。上游 1.105.1 仍然不处理 `*.arguments.done`，`output_item.done` 也只吐空 Delta（`S/completion_extras/litellm_responses_transformation/transformation.py:1499-1536`）。补丁的两个源码指纹都还在（`:1498`、`:1506`），所以**仍然需要，并且能装上**。
- 依赖的 `self._tool_call_index_map`（`transformation.py:1338`）和 `_sequential_tool_call_index`（`:1357-1373`）都还在。

### 4.6 `chatgpt_responses_output_fallback`

- `ResponsesAPIResponse.output` 仍然是必填、没有默认值（`S/types/llms/openai.py:1397-1408`），上游也仍然会走 `model_construct`：事件校验失败时（`S/llms/openai/responses/transformation.py:656`），以及终止事件的 response 是 dict 时（`SI:1488`）。所以这个补丁**仍然有用**，并且在这条流式路径上。

### 4.7 `encrypted_content_degrade_strip`

- **死代码**。1.103.2 里 affinity check 会抛异常（`.research-litellm/litellm-1.103.2/litellm/router_utils/pre_call_checks/encrypted_content_affinity_check.py:393-429`）；1.105.1 改成自己剥离后正常分发（`S/router_utils/pre_call_checks/encrypted_content_affinity_check.py:378-392`），整个文件没有 `raise`。
- 语义也不一样：上游只去掉带 `encrypted_content` 的 reasoning，有可读 summary 的保留下来（`S/responses/utils.py:559-595`）；我们的版本还会删 `previous_response_id`、compaction item、`encitem_` id（`C/encrypted_content_degrade_strip.py:66-102`）。现在线上实际跑的是上游语义。

### 4.8 `streaming_bridge`

- 和本路径无关。只处理 `call_type == "anthropic_messages"`（`C/streaming_bridge.py:260`、`:1074`）；httpx 超时补丁只对 ANTHROPIC provider 生效（`:842`）。`get_async_httpx_client` 外面那层包装对所有 provider 都会被调用，但只是透传。

## 5. 哪些补丁和 1.105.1 上游已有的东西重叠

PR 编号（#43270、#43133 等）和代码段的对应关系是根据行为推的，源码里没有引用 PR 号，**对应关系没有核实**。

| 我们的补丁 / 功能 | 上游 1.105.1 对应代码 | 判定 |
|---|---|---|
| backfill 的非阻塞 `_handle_failure` | `SI:898-907`：`running_loop = _running_loop(); if running_loop is not None: self._record_pending_logging_task(_spawn_logging_task(running_loop, self._run_failure_handlers_in_order(...)))`；`SI:920-931` 先 async，然后在 finally 里提交 sync。1.103.2 是阻塞的 `run_async_function`（`.research-litellm/litellm-1.103.2/litellm/responses/streaming_iterator.py:855-875`）。 | **上游已修，而且修得更好。我们的补丁是倒退**：丢了 sync failure_handler（router cooldown 用的就是它）和 pending 登记。建议删掉 `_install_nonblocking_responses_failure_handler`。 |
| backfill 的空流升格（`_patched_anext`） | `SI:979-986`：`if self.completed_response is not None: return … self._handle_failure(error); if self._output_started: raise error; raise self._fallback_error(error) from error` | **上游已覆盖，我们这段到不了**。差别：上游的错误不带 `failed_deployment_id`。 |
| backfill 的 capacity / 未知失败升格 | `SI:650-671` + `:281-285`：`return not isinstance(status_code, int) or status_code >= 500 or status_code == 429`；`:246-250` 状态码映射，未知 code 当 500 | **大部分重叠**。我们独有的：①盖 `failed_deployment_id`，让同组换号能触发（`router.py:7209-7211`）；②capacity 映射成 429 语义；③在上游记 failed usage 之前抛出。还需要保留它的理由只剩 ①，可以考虑只保留盖章、去掉重复分类。 |
| backfill 的 output 回填 | 流式 iterator 里**没有**；上游只在非流式的 SSE 解析里回填（`S/llms/chatgpt/responses/transformation.py:203-211`：`if not response_payload.get("output") and streamed_output_items: response_payload["output"] = …`）和桥接的非流式恢复里回填（`S/completion_extras/…/transformation.py:840-870`） | **流式回填仍然需要**（SpendLogs 要有输出）。`choices` 注入是我们加的非规范字段。 |
| `responses_failed_deployment_stamp` | `S/router.py:5410-5414`：`if deployment is not None: self._stamp_failed_deployment_id_with_effective_model_info(e, deployment, kwargs)` | **上游已有，冗余但无害**。 |
| `responses_aclose` | `SI` 里没有 `aclose`；`router.py:3410`：`if hasattr(source_iterator, "aclose"):` | **仍然需要**。499 补记账也仍然需要：上游的断连部分计费依赖 `response.chunks`（`CRP:426-428`），Responses 这条路没有这个属性。 |
| midstream fallback（Responses） | `router.py:3232-3420`、`:3422-3516`；`_note_yielded_event` / `_output_started`（`SI:954-957`） | 上游自带，我们没有补丁和它重叠。§0-3 的行为变化要注意。 |
| SSE lifecycle（gap filler + 裸 response） | 上游 `SI` 里没有裸 `object=response` 的处理（全文 `"object"` 只在 WebSocket 段 `:2719` 出现） | **仍然需要**。 |
| `encrypted_content_degrade_strip` | `encrypted_content_affinity_check.py:378-392`：`ResponsesAPIRequestUtils.strip_encrypted_reasoning_from_input(request_input) … return typed_healthy_deployments` | **上游已取代，我们的是死代码**。 |
| #44341（stream alias 计价） | 1.105.1 的 `PS:9457-9462` 仍然原地改：`chunk["model"] = target_model` / `chunk.model = target_model` | 补丁有意义，但只对 chat 形态的流有意义（见 §6）。 |

## 6. Model 盖章：客户端看到的 vs 计价用的

- **客户端看到的**：`_restamp_streaming_chunk_model`（`PS:9410-9473`）把每个 chunk 的**顶层** `model` 设成 `_litellm_client_requested_model`（`CRP:2702-2703`，`PS:9344-9356`）；发生过 fallback 时设成 `x-litellm-model-group`（`PS:9382-9386`，`:9422`）。Responses 事件类没有声明 `model` 字段，但 `extra="allow"`（`S/types/llms/base.py:29-30`），所以 `getattr(chunk,"model",None)` 拿到 None，跟 target 不相等，于是**每个 Responses 事件都会多出一个顶层 `"model":"<别名>"`**（本机 pydantic 2.13.5 实测：setattr 和 model_copy 都会序列化出来）。嵌套的 `response.model`（created/completed 里那个）**不改**，保持上游原值。`ResponsesStreamErrorState` 合成失败帧时用的也是嵌套那个值（`responses_stream_errors.py:122-124`）。
- **烘焙补丁 #44341**（`P/litellm-patch-stream-alias-pricing.py:91-118`）：改成 `return chunk.model_copy(update={"model": target_model})`，原 chunk 保持不动，同时 `logging_obj.client_facing_stream_model = target_model`。日志侧（`:27-88`）在**算完费用之后**，把存档用的 response 换成 `model=别名` 的拷贝。
- **原生 Responses 路径上，restamp 和计价没关系**：
  - 费用在 `_process_chunk` 处理终止事件时就算了：`_stamp_responses_usage_cost(_billed_response, self.logging_obj)`（`SI:513`），里面 `logging_obj._response_cost_calculator(result=response_obj)`（`SI:1594`）用的模型是 `litellm_model_name or self.model`（logging obj 上的部署模型，`litellm_logging.py:1887`），不看 chunk。
  - 成功日志拿到的是 `_logging_copy` 产生的拷贝（`SI:542`）；`_get_assembled_streaming_response` 把 `usage.cost` 写进 hidden params（`litellm_logging.py:4125-4126`），计价器看到就直接用（`:1857-1866`）。
  - 这些都发生在 restamp（`PS:9934`）之前。
  - 所以 **#44341 在原生 Responses 路径上唯一的效果**，是 SpendLogs 里存的 response 正文的 `model` 变成别名（通过 `client_facing_stream_model`，第一个 chunk restamp 时就已经设好）。
- **chat→responses 桥接路径**：chunk 是 `ModelResponseStream`，`CustomStreamWrapper` 用同一批对象拼装再计价。这是 #44341 要修的场景，补丁在这里有实际作用。
- 注意：被 restamp 的那个对象，就是 `_wrap_responses_stream_for_container_ownership` 和 `ResponsesStreamErrorState` 读的那个，上游版和补丁版里它们读到的都是 restamp 之后的值，没有冲突。

## 7. 意外、风险和容易误解的地方

1. **母 router 实际走的是 OpenAI transformation，不是 ChatGPT 的**。按 skill `litellm-chatgpt-provider-prefix-fix` 的说法，正确的注册形态是 `openai/chatgpt-gpt-5.x` 加上 acct svc 的 `api_base`（`.claude/skills/litellm-chatgpt-provider-prefix-fix/SKILL.md:58`）。所以母 router 用的是 `OpenAIResponsesAPIConfig.transform_streaming_response`。`ChatGPTResponsesAPIConfig`（`S/llms/chatgpt/responses/transformation.py`）只会在 acct pod 进程里跑（强制 `stream=True`、只保留白名单字段，`:90-111`）。DB 里的 provider 我没现量。
2. **`/v1/responses` 也会以 `data: [DONE]` 结束**（`PS:10017-10018`）。OpenAI 官方不发这一帧。下游如果是另一个 litellm（acct pod → 母 router），`[DONE]` 会让 `_next_event` 提前停止读取（`SI:1062-1064`），连接就只能靠 `aclose` 来释放，这正是 `responses_aclose` 存在的原因。
3. **客户端断连时上游不计费**：Responses 路径没有 `chunks` 可以拼成部分响应（`CRP:426-428`）。我们补的 499 行 cost 为空，**被 chatgpt 消耗掉的 input token 在账上是 0**。
4. **内容开始后的 midstream fallback 会重放生命周期**（§0-3）。fallback 的事件流会带着自己的 `response.created`（router 只在没有 fallback 成功时才吐暂扣事件，`router.py:3501-3503`）。Codex 收到两个 created 会怎么处理，没验证。
5. **backfill 抢在上游之前抛 `MidStreamFallbackError`，导致失败 usage 不入账**（`C/…backfill.py:337` 早于 `SI:627-645`）。按代码推，非成功终止且被判为 retry 的那一跳，其 input token 不会进明细；fallback 成功那一跳的 usage 里会合并 `partial_usage`，但这个值是从 `completed_response` 取的（`router.py:3056-3090`），而我们这里没设，所以是 None。
6. **`ReadTimeout`（及其它 `httpx.HTTPError`）不触发 midstream fallback**（`SI:1084-1088`）。流卡住时客户端直接收到 `response.failed`。
7. **成功日志用的是无引用的 `asyncio.create_task`**（`SI:560`）。Python 文档写明这种 task 可能在跑完前被 GC 回收。失败路径用的是 `_spawn_logging_task`，会持有强引用（`SI:182-189`），成功路径没有。这和 `finding-spendlog-drop.md` 那个 1.3% 缺口有没有关系：**还没查，现在只是一个假设**。证伪方法：如果缺的行全都能对应到 prisma 拒收日志（那份文件说 84 个 id 都对上了），这个假设就不成立。
8. **每个 chunk 都要 await 一遍所有 callback 的 `async_post_call_streaming_deployment_hook`**（`SI:782-794`）。生产有 40 个左右的 CustomLogger。我们的补丁没有一个实现这个方法（live-cb grep 0 命中），所以全是空 await，开销没测。
9. **同一个 chunk 被反复 JSON 序列化**：backfill 解析 1 次（`:211`），终止事件再 dumps 1 次（`:368`）；上游 `json.loads` 1 次（`SI:383`）；序列化 1 次（`PS:9560`）；ccv2 / deepseek 命中时又各做一次 loads+dumps（`C/codex_compaction_v2.py:432`、`C/deepseek_id_prefix.py:697`）。大的 completed 帧（output 回填之后）代价最高。没测。
10. **日志里存的 id 和客户端看到的 id 不一样**（deepseek 路径）：日志拷贝在 deepseek 加前缀之前就做了（`SI:542`），所以 SpendLogs 里是裸 UUID，客户端看到的是 `fc_…`。拿 id 跨系统排查时容易误判。
11. **gap filler 合成的 `response.created` 用的是随机 `resp_<uuid>`**（`P/…sse-lifecycle.py:57`），和后面 completed 里那个 litellm 编码过的 id 不一样。只在上游漏发 created 时才会出现。
12. **`deepseek_id_prefix` 里有一段死代码**（`_rewrite_chunk`、`_rewrite_sse_bytes`，`:578-608`）。backfill 的空流升格段在 1.105.1 上也到不了（§4.2）。删的时候建议同时删掉对应注释，免得以后有人以为它们还在生效。

## 8. 没验证 / 下一步查什么

- 生产 `litellm_settings.sse_keepalive_ping_interval_seconds` 和 deployment 级的 `keepalive_seconds`：决定 §4.3 的 contextvar 问题是「只影响第一个事件」还是「整条流失效」。
- 在 gray 车道做阳性对照：构造一个流内 `response.failed`（server_error），看 router 的 `deployment_callback_on_failure` 有没有给那个号 +1 失败（比如 cooldown 列表 / `increment_deployment_failures_for_current_minute` 的计数）。对照组是去掉 `_install_nonblocking_responses_failure_handler` 后再跑一次。用来证实或推翻 §0-1。
- 198 上 chat→responses 桥接路径的 CLOSE_WAIT / Recv-Q：按连接对端 acct pod 统计，区分 `/v1/responses` 和 `/v1/chat/completions` 的来源（§3 缺口）。
- 生产镜像里的 httpx 版本（§3 关于 `aiter_raw` 的结论来自本机 0.28.1）。
- 线上路由策略是不是 weighted_affinity（§4.4 fail-mark 的说法依赖这一点）。
