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
已失败账号不会被重复挑中。上限 _MAX_MIDSTREAM_FAILOVERS=3 次，防满池
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

_MAX_MIDSTREAM_FAILOVERS = 3


def _install_midstream_fallback_loop() -> None:
    try:
        import anyio
        from litellm.router import Router
        from litellm.exceptions import MidStreamFallbackError
        from litellm.responses.streaming_iterator import (
            BaseResponsesAPIStreamingIterator,
            _get_openai_response_types,
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
                                "exhausted, surfacing error",
                                attempts - 1,
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
                        verbose_router_logger.info(
                            "midstream_fallback_loop: mid-stream failover attempt "
                            "%d/%d for model_group=%s",
                            attempts,
                            _MAX_MIDSTREAM_FAILOVERS,
                            model_group,
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
