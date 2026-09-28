"""
Mid-stream fallback LOOP for the Responses API (198 pro).

Bug（2026-08-10 复现钉死，见 project_198_gpt56_midstream_failover_race_2026_08_10）
--------------------------------------------------------------------------------
`Router._aresponses_streaming_iterator` 的 `stream_with_fallbacks` 是**单发**结构：

    except MidStreamFallbackError as e:
        fallback_response = await common_utils(...)   # 同组重挑/跨组兜底
        async for item in fallback_response:          # ← 在 except 块里消费
            yield item

fallback 拿到的新流如果**又**在中途报 capacity（过载风暴期非常常见——挑到的
下一个账号同样过载），第二个 MidStreamFallbackError 抛在 except 处理器内部，
没有任何人再接：不再同组换号、不再跨组兜底，原始过载错误直接砸到客户端。
litellm-dev 复现（all-sol-dead 场景）：acct-0 失败 → 重挑 acct-1 → acct-1 又
中流失败 → luna 兜底一次没试，客户端收到裸 MidStreamFallbackError。

Fix
---
把单发改成 while 循环：每次 MidStreamFallbackError 都重新进入
`async_function_with_fallbacks_common_utils`（同组重挑 → 跨组链），
`meta["_failover_excluded_ids"]` 天然跨轮累积（嵌套 dict 引用共享），
已失败账号不会被重复挑中。上限 _MAX_MIDSTREAM_FAILOVERS 次（见该常量），防满池
过载时无限循环。

部署方式：litellm-callbacks ConfigMap 模块 import 副作用整体替换
`Router._aresponses_streaming_iterator`（同 streaming_output_backfill.py 的
Router 补丁模式，无需改镜像）。方法体从镜像
vanilla-v1.90.2.capacity.sse-fix-bare-20260711-122004 的 router.py:2492 拷贝，
仅 stream_with_fallbacks 内部改为循环。升级 litellm 镜像时必须重新比对原方法。
"""

from typing import Any, AsyncGenerator, Dict

from litellm.integrations.custom_logger import CustomLogger

try:
    from litellm._logging import verbose_router_logger
except Exception:  # pragma: no cover
    import logging

    verbose_router_logger = logging.getLogger("litellm.router")

# [2026-09-28] 5 → 10。5 次预算里前 3 次按 _MAX_SAME_GROUP_RETRIES 规定花在同组
# 换号上，留给 fallback 链的只有 2 次，而 chatgpt-gpt-6-astra 的链有 5 跳
# （sol → luna → 5.6-sol → sa-grok-4.20 → openrouter-deepseek-v4.1-flash）：
# 后 3 跳在预算上永远走不到。实测 cursor-buyitian-pwcf 30h 内 2406 次落主组、
# 22 次落第 1 跳 sol，后 4 跳 SpendLogs 行数全 0。
#
# 抬到 10 = 3 次同组 + 7 次给链（链最长 5 跳，留 2 次余量给 run_async_fallback
# 因 `already attempted` 跳过的重复项）。平时用不到：25min 窗口 36 次 failover 里
# attempt 1/5 占 32 次、2/5 占 4 次，3/5 及以上为 0 —— 绝大多数请求第一次换号即成，
# 抬上限只在过载风暴期才被消耗到，不改变常态行为。
_MAX_MIDSTREAM_FAILOVERS = 10

# [2026-09-17] 同组重挑配额。
#
# 上游 `_maybe_run_weighted_failover`(router.py:6392) 在 fallback 链**之前**执行，
# 且只要同组还剩一个非 cooldown 的腿就 return 结果 —— 永不 return None 放行到链上。
# `gpt-5.6-sol` 挂 39 条腿，原来 3 次预算 100% 消耗在同一个 ChatGPT 池内换号，
# 链上的 sa-grok-4.6 / openrouter-deepseek-v4.1-flash 一次都走不到（实测 09-17
# 05:07~05:08 日志里三次 attempt 的 model_group 完全相同）。池子整体过载时换号
# 等于换了个同样堵的门，调大上限只是把 39 个堵门轮一遍。
#
# 修法：本请求同组重挑满 _MAX_SAME_GROUP_RETRIES 次后，擦掉异常上的
# `failed_deployment_id` —— 那是 `_maybe_run_weighted_failover` 的入口闸
# (`if not failed_id: return None`)，闸一关它立即放行到 fallback 链。
# 只作用于当前这条流的这一次异常，不碰全局 enable_weighted_failover，
# 其他 model group 的同组重试行为完全不变。
#
# 黏性无影响：首发选腿由 weighted_affinity.py 的 async_filter_deployments 决定，
# 那个 hook 通篇不读 enable_weighted_failover，本改动也不碰它。
#
# [2026-09-28] 维持 3 不动。_MAX_MIDSTREAM_FAILOVERS 抬到 10 之后，本常量的语义
# 从「同组占掉总预算的 60%」变成「前 3 次同组，之后 7 次全部放行到链」——
# 用户要的形状（同组换 3 次 + 每个备胎各试一次）由这两个常量的差值表达，
# 不需要改本行。
#
# [2026-09-29] 上面那句「由两个常量的差值表达」当时**只是设计意图，没有生效**。
# 09-28 抬到 10 之后实测：10 发全砸在链子第 1 跳，后 4 跳一次都没碰到 ——
# 光关闸门（擦 failed_deployment_id）不够，每次重发还会把 model 读回原组、
# 去表里重新命中第 1 个目标。真正让链子下降的是下方 attempted_targets 注入，
# 两处必须同时在才成立。本常量现在还额外决定注入时机（见那段注释的不等号）。
_MAX_SAME_GROUP_RETRIES = 3


def _install_midstream_fallback_loop() -> None:
    try:
        import anyio
        from litellm.router import Router
        from litellm.exceptions import MidStreamFallbackError
        from litellm.responses.streaming_iterator import (
            BaseResponsesAPIStreamingIterator,
            _get_openai_response_types,
        )

        # 链子逐层下降依赖这个去重表（见下方 attempted_targets 注释）。
        # 它在 v1.90.2 的 fallback_event_handlers 里。这一条是**硬拒装**：
        # 类不存在则下面的注入代码根本跑不起来（NameError），必须拦在安装期。
        # 与它相邻的「源码文本守卫」不同 —— 那条只告警，理由见该处注释。
        from litellm.router_utils.fallback_event_handlers import (
            AttemptedFallbackTargets as _AttemptedFallbackTargets,
        )
    except ImportError as exc:
        verbose_router_logger.warning(
            "midstream_fallback_loop: import failed, skip: %r", exc
        )
        return

    if getattr(Router, "_carher_midstream_loop_patched", False):
        return

    # 版本守卫：方法体拷贝自 vanilla-v1.90.2 镜像。若运行中的 router 源码不含
    # 预期标记（升级到 v1.92+ 或方法被改），拒绝安装、保持原单发行为 ——
    # 装错版本的方法体比不装危险得多。升级镜像时重新比对 router.py:2492 后
    # 更新本文件与此指纹。
    import inspect

    try:
        _orig_src = inspect.getsource(Router._aresponses_streaming_iterator)
    except Exception as exc:
        verbose_router_logger.error(
            "midstream_fallback_loop: cannot read original source, skip: %r", exc
        )
        return
    if "stream_with_fallbacks(aresponses): error closing source" not in _orig_src:
        verbose_router_logger.error(
            "midstream_fallback_loop: original method fingerprint mismatch "
            "(image upgraded?), NOT patching — re-diff router.py before enabling"
        )
        return

    # 第二道守卫：我们往 kwargs 塞 attempted_targets，靠的是 run_async_fallback
    # 会把它**接过去**（`carried_targets = kwargs.get("attempted_targets")`）。
    # 上游哪天不再读这个 key，注入就变成一句无害的死代码 —— 表现是链子静默
    # 退回「10 发全砸第一层」，没有任何报错。所以要在安装期留下痕迹。
    #
    # [2026-09-29] 本守卫**只告警、不拒装**。第一版写成了 `return`（拒装），
    # 上生产后 4/4 副本全部拒装 —— 线上退回完全不打补丁的原单发行为，
    # 比打补丁前更差（连同组重挑 3 次都没了）。而同一套判据在 exec 起的
    # 干净进程里怎么测都是通过（冷/热 import、带不带 sitecustomize，全 True），
    # 差异复现不出来。
    #
    # 「为什么 worker 里读不到」这一项**我没查出来，不下结论**。已排除的：
    # 源码只有一份（find 全盘一个 .py + 一个 .pyc）、needle 在第 384 行确实是
    # 双引号、函数无 __wrapped__、模块路径与 __module__ 一致。没排除的：
    # uvicorn 多 worker 下 inspect/linecache 的行为。
    #
    # 但无论那个原因是什么，**拒装都是错的响应**：守卫的作用是「上游变了要让我
    # 知道」，不该是「读不到就把能用的补丁关掉」。inspect.getsource 读不到源码
    # 是**量具坏了**，不是**上游改了行为**，这两件事我第一版混为一谈了。
    # 真出问题的降级是安全的（链子退回单层，和补丁前一样），而拒装的降级不安全。
    try:
        from litellm.router_utils.fallback_event_handlers import run_async_fallback

        _fb_src = inspect.getsource(run_async_fallback)
    except Exception as exc:
        verbose_router_logger.warning(
            "midstream_fallback_loop: cannot read run_async_fallback source "
            "(%r) — proceeding with attempted_targets injection unverified",
            exc,
        )
    else:
        if 'kwargs.get("attempted_targets")' not in _fb_src:
            verbose_router_logger.warning(
                "midstream_fallback_loop: run_async_fallback source does not "
                "show kwargs.get(\"attempted_targets\") — if upstream really "
                "dropped it, chain descent degrades to same-group-only; "
                "re-diff fallback_event_handlers.py on the next image bump"
            )

    async def _patched_aresponses_streaming_iterator(
        self,
        response: "BaseResponsesAPIStreamingIterator",
        initial_kwargs: Dict[str, Any],
    ) -> "BaseResponsesAPIStreamingIterator":
        source_iterator = response

        _openai_types = _get_openai_response_types()
        _RESPONSES_TERMINAL_EVENT_TYPES = (
            _openai_types.ResponsesAPIStreamEvents.RESPONSE_COMPLETED,
            _openai_types.ResponsesAPIStreamEvents.RESPONSE_INCOMPLETE,
            _openai_types.ResponsesAPIStreamEvents.RESPONSE_FAILED,
        )

        # ---- 拷贝自原方法：仅为 isinstance 兼容的透传壳 ----
        class FallbackResponsesStreamWrapper(BaseResponsesAPIStreamingIterator):
            def __init__(self, async_generator: AsyncGenerator):
                import time
                from datetime import datetime

                self._async_generator = async_generator
                self.response = getattr(source_iterator, "response", None)  # type: ignore[assignment]
                self.model = getattr(source_iterator, "model", None)  # type: ignore[assignment]
                self.logging_obj = getattr(  # type: ignore[assignment]
                    source_iterator,
                    "logging_obj",
                    getattr(source_iterator, "litellm_logging_obj", None),
                )
                self.finished = False
                self.responses_api_provider_config = getattr(
                    source_iterator, "responses_api_provider_config", None
                )
                self.completed_response = None
                self.start_time = getattr(source_iterator, "start_time", datetime.now())
                self._failure_handled = False
                self._completed_response_cached = False
                self._completed_response_logged = False
                self._completed_response_cache_hit = None
                self._persist_completed_response_before_logging = True
                self._stream_created_time = time.time()
                self.litellm_metadata = getattr(
                    source_iterator, "litellm_metadata", None
                )
                self.custom_llm_provider = getattr(
                    source_iterator, "custom_llm_provider", None
                )
                self.request_data = getattr(source_iterator, "request_data", {}) or {}
                self.call_type = getattr(source_iterator, "call_type", None)
                self._hidden_params = dict(
                    getattr(source_iterator, "_hidden_params", None) or {}
                )

            def __aiter__(self):
                return self

            async def __anext__(self):
                chunk = await self._async_generator.__anext__()
                if (
                    self.completed_response is None
                    and getattr(chunk, "type", None) in _RESPONSES_TERMINAL_EVENT_TYPES
                ):
                    self.completed_response = chunk
                return chunk

            async def aclose(self):
                await self._async_generator.aclose()

        async def stream_with_fallbacks():
            current_iterator: Any = source_iterator
            opened_streams: list = []  # 循环中打开的 fallback 流，finally 统一关
            partial_usage = None
            attempts = 0
            try:
                while True:
                    try:
                        async for item in current_iterator:
                            if partial_usage is not None:
                                Router._combine_responses_fallback_usage(
                                    item, partial_usage
                                )
                            yield item
                        return
                    except MidStreamFallbackError as e:
                        attempts += 1
                        if attempts > _MAX_MIDSTREAM_FAILOVERS:
                            verbose_router_logger.error(
                                "midstream_fallback_loop: %d mid-stream failovers "
                                "exhausted, surfacing error (model_group=%s trace=%s)",
                                attempts - 1,
                                initial_kwargs.get("model"),
                                initial_kwargs.get("litellm_trace_id") or "-",
                            )
                            raise
                        _pu = Router._extract_partial_responses_usage(current_iterator)
                        if _pu is not None:
                            partial_usage = _pu
                        model_group = initial_kwargs.get("model")
                        fallbacks = initial_kwargs.get("fallbacks", self.fallbacks)
                        context_window_fallbacks = initial_kwargs.get(
                            "context_window_fallbacks", self.context_window_fallbacks
                        )
                        content_policy_fallbacks = initial_kwargs.get(
                            "content_policy_fallbacks", self.content_policy_fallbacks
                        )
                        initial_kwargs["original_function"] = (
                            self._ageneric_api_call_with_fallbacks_helper
                        )
                        if e.is_pre_first_chunk or not e.generated_content:
                            pass
                        else:
                            initial_kwargs["input"] = (
                                Router._build_responses_continuation_input(
                                    initial_kwargs.get("input"),
                                    e.generated_content,
                                )
                            )
                        self._update_kwargs_before_fallbacks(
                            model=model_group,
                            kwargs=initial_kwargs,
                            metadata_variable_name="litellm_metadata",
                        )
                        # 同组配额用完 → 关掉 weighted-failover 的入口闸，强制走链。
                        # 见文件头 _MAX_SAME_GROUP_RETRIES 注释。
                        _cross_group = attempts > _MAX_SAME_GROUP_RETRIES
                        # [2026-09-29] 让链子真的能往下走。
                        #
                        # 病灶：`run_async_fallback` 的签名是
                        #   async def run_async_fallback(*args, litellm_router,
                        #       fallback_model_group, original_model_group, **kwargs)
                        # 那个 `**kwargs` 收集成一个**新 dict**，`common_utils` 里的
                        # `input_kwargs` 同样是新 dict。所以它内部两处写入
                        #   kwargs["model"] = mg
                        #   kwargs["attempted_targets"] = attempted
                        # 全落在副本上，本循环的 initial_kwargs 一个字节都收不到。
                        # 后果：每次重发都重新读到**原组**，去表里查永远命中第 1 个
                        # 目标 —— 09-28 实测 10 行日志全是
                        # `model_group=chatgpt-gpt-6-astra`、10 发全砸在第一层备胎，
                        # 后面四层一次都没碰到。
                        #
                        # 修法：我们自己在 kwargs 上放一个持久的 attempted_targets。
                        # `run_async_fallback` 认这个 key（`if attempt_key in
                        # attempted: continue`），于是试过的目标会被记住并跳过，
                        # 链子逐层往下。只建一次，之后由 LiteLLM 往里累加。
                        #
                        # 时机为什么是 `>= _MAX_SAME_GROUP_RETRIES` 而不是
                        # `> `：晚一发注入，则第 4 发时闸门已关但去重表刚建成空的，
                        # `run_async_fallback` 会重挑第 1 个目标 —— 同层白打 4 发。
                        # 早一发注入，第 3 发在同层内消化时顺手把第 1 层记进表，
                        # 第 4 发才正好跳到第 2 层。mock 实测落点序列：
                        #   6-sol ×3 → 6-luna → 5.6-sol → grok-4.20 → deepseek
                        # 即同层恰好 3 发、之后每层各 1 发。改这个不等号会破掉它。
                        if attempts >= _MAX_SAME_GROUP_RETRIES and not isinstance(
                            initial_kwargs.get("attempted_targets"),
                            _AttemptedFallbackTargets,
                        ):
                            initial_kwargs["attempted_targets"] = (
                                _AttemptedFallbackTargets()
                            )
                        if _cross_group and getattr(e, "failed_deployment_id", None):
                            try:
                                e.failed_deployment_id = None
                            except Exception as _exc:  # pragma: no cover
                                verbose_router_logger.debug(
                                    "midstream_fallback_loop: cannot clear "
                                    "failed_deployment_id: %r",
                                    _exc,
                                )
                        # [2026-09-28] 带上 trace id。原来这行只有 attempt/组名，
                        # 并发请求的日志交错在一起**无法归属到同一条流** ——
                        # 09-28 诊断 cursor-buyitian-pwcf 时我据此把别人的
                        # `attempt 1/5 for model_group=chatgpt-gpt-6-sol`
                        # （6h 内 43 把 key 直接点名发 sol，3334 次）读成了
                        # 「跳到备胎后计数器归零」，得出错误结论。
                        # trace id 是判「这几行是不是同一条流」的唯一依据。
                        _trace = initial_kwargs.get("litellm_trace_id") or "-"
                        verbose_router_logger.info(
                            "midstream_fallback_loop: mid-stream failover attempt "
                            "%d/%d for model_group=%s (mode=%s) trace=%s",
                            attempts,
                            _MAX_MIDSTREAM_FAILOVERS,
                            model_group,
                            "cross-group" if _cross_group else "same-group",
                            _trace,
                        )
                        new_response = (
                            await self.async_function_with_fallbacks_common_utils(
                                e=e,
                                disable_fallbacks=False,
                                fallbacks=fallbacks,
                                context_window_fallbacks=context_window_fallbacks,
                                content_policy_fallbacks=content_policy_fallbacks,
                                model_group=model_group,
                                args=(),
                                kwargs=initial_kwargs,
                            )
                        )
                        if hasattr(new_response, "__aiter__"):
                            opened_streams.append(new_response)
                            current_iterator = new_response
                            continue  # ← 循环点：嵌套中流失败会再次进入本 except
                        yield new_response
                        return
            except MidStreamFallbackError:
                raise
            except Exception as fallback_error:
                verbose_router_logger.error(
                    "Responses streaming fallback also failed: %s", fallback_error
                )
                raise
            finally:
                with anyio.CancelScope(shield=True):
                    for _stream in [source_iterator, *opened_streams]:
                        if hasattr(_stream, "aclose"):
                            try:
                                await _stream.aclose()  # type: ignore[func-returns-value]
                            except BaseException as exc:
                                verbose_router_logger.debug(
                                    "midstream_fallback_loop: error closing stream: %s",
                                    exc,
                                )

        return FallbackResponsesStreamWrapper(stream_with_fallbacks())

    Router._aresponses_streaming_iterator = _patched_aresponses_streaming_iterator
    Router._carher_midstream_loop_patched = True
    verbose_router_logger.info(
        "midstream_fallback_loop: patched Router._aresponses_streaming_iterator "
        "(loop, max %d mid-stream failovers)",
        _MAX_MIDSTREAM_FAILOVERS,
    )


try:
    _install_midstream_fallback_loop()
except Exception as _exc:  # pragma: no cover
    # 补丁装不上就保持原状（单发 fallback），绝不能拖垮 proxy 启动。
    verbose_router_logger.error(
        "midstream_fallback_loop: install failed, skipped: %r", _exc
    )


class MidstreamFallbackLoopCallback(CustomLogger):
    """空 callback 壳：litellm_settings.callbacks 挂载入口，逻辑全在 import 副作用。"""


midstream_fallback_loop = MidstreamFallbackLoopCallback()
