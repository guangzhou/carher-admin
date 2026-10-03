"""
Anthropic /v1/messages bridge: surface mid-stream failures as Anthropic
``error`` SSE events instead of silently truncating the stream (198 pro).

Bug（2026-08-10 实测钉死，claude-code 走桥接 25% 整轮失败）
----------------------------------------------------------------
Claude Code 走 ``/v1/messages`` → responses_adapters 桥接 →
``litellm.aresponses``（模块级直调，**不经过** ``Router.aresponses``，所以
midstream_fallback_loop 那把 Router 补丁覆盖不到这条流）。上游中流过载时
streaming_output_backfill 正确升格为 MidStreamFallbackError，但
``AnthropicResponsesStreamWrapper.__anext__`` 的 ``except Exception`` 只打一行
日志就 ``StopAsyncIteration``：客户端收到 HTTP 200 + 只有 message_start 的空流
（实测恒 724 字节）。Claude Code 对空流不重试，整轮 agentic loop 直接中止。

对照数据（2026-08-10）：同一过载风暴下
  /v1/responses（三刀全覆盖）   30/37 次升格被接住
  /v1/chat/completions          抛 500 可重试，1/40 失败
  /v1/messages 桥接             5/20 静默截断，0 次被接住

Fix
---
桥接层没有 router 上下文（deployment 在上层已 pin 死），做不了真 failover。
正解是把异常翻成 Anthropic 协议的标准流内 ``error`` 事件：

    event: error
    data: {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}

Claude Code / Anthropic SDK 对 overloaded_error 有内建重试；重发的请求重新
走 router 选号（WA + cooldown），等效于把 failover 外包给客户端。

分类哲学与第三刀一致（以成功形状为基准，见
project_198_gpt56_midstream_failover_race_2026_08_10 第三刀）：
  - 确定性客户端错误（4xx 语义）→ invalid_request_error，不诱导重试；
  - 其余一律 → overloaded_error（"Overloaded" 为 Anthropic 官方文案），
    未知形状默认可重试，杜绝枚举漏网。
对外 message 用固定字面量，不带上游异常文本（对外脱敏红线，见
project_198_outward_error_sanitize_2026_08_08）；完整异常 + traceback 落
服务端日志，marker=``anthropic_bridge_error_surface:``。

部署方式：litellm-callbacks CM 新增本文件 + config.yaml callbacks 挂
``anthropic_bridge_error_surface.anthropic_bridge_error_surface`` +
deployment volumeMount subPath（缺一不可，见
feedback_litellm_callback_needs_volumemount_not_just_cm_key）。

方法体拷贝自镜像 vanilla-v1.90.2.capacity.sse-fix-bare-20260711-122004 的
``responses_adapters/streaming_iterator.py`` ``__anext__``（仅改 except 分支
与断流护栏）。带源码指纹守卫：升级镜像后指纹不符自动拒装，重新比对后更新。
"""

import traceback

from litellm.integrations.custom_logger import CustomLogger

try:
    from litellm import verbose_logger
except Exception:  # pragma: no cover
    import logging

    verbose_logger = logging.getLogger("litellm")

_MARKER = "anthropic_bridge_error_surface"


def _classify_exception(exc):
    """确定性客户端错误 → invalid_request_error；其余默认 overloaded_error。

    默认档必须是可重试形状——检测器判"坏"永远枚举不完，判"不是确定性 4xx"
    才收敛（同第三刀）。
    """
    try:
        import litellm as _l

        deterministic = tuple(
            t
            for t in (
                getattr(_l.exceptions, "BadRequestError", None),
                getattr(_l.exceptions, "AuthenticationError", None),
                getattr(_l.exceptions, "PermissionDeniedError", None),
                getattr(_l.exceptions, "NotFoundError", None),
                getattr(_l.exceptions, "UnprocessableEntityError", None),
            )
            if t is not None
        )
        if deterministic and isinstance(exc, deterministic):
            return ("invalid_request_error", "Upstream rejected the request.")
    except Exception:  # pragma: no cover — 分类失败也必须走默认可重试档
        pass
    return ("overloaded_error", "Overloaded")


def _install_bridge_error_surface() -> None:
    try:
        import inspect

        from litellm.llms.anthropic.experimental_pass_through.responses_adapters.streaming_iterator import (  # noqa: E501
            AnthropicResponsesStreamWrapper,
        )
    except ImportError as exc:
        verbose_logger.warning("%s: import failed, skip: %r", _MARKER, exc)
        return

    if getattr(AnthropicResponsesStreamWrapper, "_carher_error_surface_patched", False):
        return

    # 版本守卫：方法体拷贝自 vanilla-v1.90.2。原方法丢失"吞异常 + Drain"结构
    # 说明镜像升级过，拒装并保留原行为——装错方法体比不装危险。
    try:
        _orig_src = inspect.getsource(AnthropicResponsesStreamWrapper.__anext__)
    except Exception as exc:
        verbose_logger.error(
            "%s: cannot read original source, skip: %r", _MARKER, exc
        )
        return
    if (
        "AnthropicResponsesStreamWrapper error:" not in _orig_src
        or "Drain any remaining queued chunks" not in _orig_src
    ):
        verbose_logger.error(
            "%s: original __anext__ fingerprint mismatch (image upgraded?), "
            "NOT patching — re-diff responses_adapters/streaming_iterator.py",
            _MARKER,
        )
        return

    async def _patched_anext(self):
        # ── 以下结构拷贝自 v1.90.2 __anext__，仅 except 分支与断流护栏有改动 ──
        # Return any queued chunks first
        if self._chunk_queue:
            return self._chunk_queue.popleft()

        # 护栏：上游已炸且 error 事件已发完，不再触碰坏流（重进 async for
        # 可能再次抛错 → 重复排队 error 事件）。
        if getattr(self, "_carher_stream_dead", False):
            raise StopAsyncIteration

        # Emit message_start if not yet done (fallback if response.created wasn't fired)
        if not self._sent_message_start:
            self._sent_message_start = True
            self._chunk_queue.append(self._make_message_start())
            return self._chunk_queue.popleft()

        # Consume the upstream stream
        try:
            async for event in self.responses_stream:
                self._process_event(event)
                if self._chunk_queue:
                    return self._chunk_queue.popleft()
        except StopAsyncIteration:
            pass
        except Exception as e:
            # 原版在这里吞掉异常 → 客户端收到 200 + 截断流（Claude Code 整轮
            # 中止且不重试）。改为翻成 Anthropic 流内 error 事件；对外 message
            # 用固定字面量（脱敏），完整异常落服务端日志。
            err_type, err_msg = _classify_exception(e)
            verbose_logger.error(
                "%s: surfacing %s as in-stream '%s' to client\n%s",
                _MARKER,
                type(e).__name__,
                err_type,
                traceback.format_exc(),
            )
            self._carher_stream_dead = True
            self._chunk_queue.append(
                {
                    "type": "error",
                    "error": {"type": err_type, "message": err_msg},
                }
            )

        # Drain any remaining queued chunks
        if self._chunk_queue:
            return self._chunk_queue.popleft()

        raise StopAsyncIteration

    AnthropicResponsesStreamWrapper.__anext__ = _patched_anext
    AnthropicResponsesStreamWrapper._carher_error_surface_patched = True
    verbose_logger.info(
        "%s: patched AnthropicResponsesStreamWrapper.__anext__ "
        "(swallowed exceptions -> in-stream Anthropic error events)",
        _MARKER,
    )


try:
    _install_bridge_error_surface()
except Exception as _exc:  # pragma: no cover — 装载失败退回原行为，不扩大爆炸半径
    verbose_logger.error("%s: install failed, skipped: %r", _MARKER, _exc)


class _BridgeErrorSurfaceLogger(CustomLogger):
    """空 logger——本模块的全部工作在 import 副作用里完成。

    挂在 litellm_settings.callbacks 只是为了让 proxy 每个 worker 都确定性
    import 本模块（与 midstream_fallback_loop 同模式）。
    """


anthropic_bridge_error_surface = _BridgeErrorSurfaceLogger()
