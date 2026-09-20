"""Monkey-patch litellm streaming iterator to backfill response.completed output.

ChatGPT Codex backend sends response.completed with output:[] — actual content
arrives via preceding response.output_item.done SSE events. Without accumulation,
SpendLogs stores empty output.  This patch intercepts the raw SSE JSON before
_process_chunk, accumulates output_item.done payloads, and injects them into
response.completed before the original code stores + logs it.

另附 capacity 升格：上游 capacity 失败以 HTTP 200 + 流内 response.failed 送达，
不进 exception_type()，镜像层 capacity patch 完全拦不到（router 既不 cooldown 也不
fallback，错误直穿到 Codex）。此处在流内窄匹配后抛 MidStreamFallbackError，交还给
router.stream_with_fallbacks。cooldown 由 __anext__ 既有的 _handle_failure 记账，
本文件不再自己调 failure_handler（重复调会把失败计数放大 3 倍）。

⚠️ 流式路径与同步 400 路径的载体不同，别平移：
- 同步 400（镜像层 exception_mapping_utils patch）→ RateLimitError，走 exception_type()
- 流式（本文件）→ 必须 MidStreamFallbackError，router 的 except 只认这个

Ref: https://github.com/BerriAI/litellm/pull/31332
Ref: Wei-Shaw/sub2api#2481 (同步 400 路径) / #4384 (per-acct-model 冷却)
"""
from __future__ import annotations

import contextvars
import json
import logging
from typing import Any

from litellm.exceptions import MidStreamFallbackError as _MidStreamFallbackError
from litellm.exceptions import RateLimitError as _RateLimitError
from litellm.exceptions import InternalServerError as _InternalServerError
from litellm.integrations.custom_logger import CustomLogger

_log = logging.getLogger("streaming_output_backfill")


# === capacity 升格 ===
# 只窄匹配 capacity / overloaded。**不要**放宽成 status=="failed" 全匹配：
# 2026-07-15 sol 全组 cooldown 雪崩就是把不该升格的失败升格成 RateLimitError，
# 60s/3fails 把 54 个 deployment 全摘光（见 [[198-sol-cooldown-storm-2026-07-15]]）。
_CAPACITY_MARKERS = (
    "selected model is at capacity",
    "selected model isn't available",
    # 2026-07-31 新增。前两条已实测命中（Codex IDE 日志 2026-07-30 01:13:11，
    # 前端弹 "Selected model is at capacity"）。这条是后端量到 3220 次的变体，
    # 原先只能靠 code 命中；而 code 字段是否存在从未被证实（原始 SSE 帧从不落盘，
    # transformation.py:135 的 `error_message or raw_response.text` 短路丢弃）。
    # 只靠一个无法确认存在的字段 = 静默漏判，故补 message 判据兜底。
    "our servers are currently overloaded",
)
# code 分支保留：报文带 code 时更快命中，且不依赖上游文案措辞。
_CAPACITY_CODES = ("server_overloaded", "server_is_overloaded", "slow_down")

# 已向客户端下发过内容后不能再重试（流无法重放）——与 sub2api #4175 同一约束。
# 含 reasoning delta：宁可少救一例，也不引入"重试后 reasoning 重复渲染"的新故障。
_CONTENT_STARTED_EVENTS = (
    "response.output_text.delta",
    "response.output_text.done",
    "response.function_call_arguments.delta",
    "response.refusal.delta",
    "response.reasoning_summary_text.delta",
)


def _is_capacity_failure(resp: Any) -> bool:
    """判 response 对象是否 capacity 失败。窄匹配，宁漏不滥。"""
    if not isinstance(resp, dict):
        return False
    err = resp.get("error")
    if not isinstance(err, dict):
        return False
    code = str(err.get("code") or "").strip().lower()
    if code in _CAPACITY_CODES:
        return True
    msg = str(err.get("message") or "").lower()
    return any(m in msg for m in _CAPACITY_MARKERS)


# === [2026-08-10] 失败分类：以成功形状为基准（allowlist），不再枚举错误形状 ===
# 流的合法终止 = response.completed / response.incomplete。凡是 200 里带出来的
# 非成功终止（response.failed / 裸 failed response / SSE error 事件），默认
# 都值得换账号重试；只有明确"换账号也没用"的确定性错误才放行透传。
# 与 7-15 雪崩的区别：那次是把确定性错误也升格成 429（立即冷却）；这里
# (a) 确定性错误显式放行，(b) 未知形状升格为 500 语义 —— 冷却走
# allowed_fails=3 阈值而非 429 的立即冷却，风暴有闸门。
_DETERMINISTIC_ERROR_TYPES = (
    "invalid_request_error",
    "invalid_prompt",
    "content_policy_violation",
    "content_filter",
)
_DETERMINISTIC_CODES = (
    "string_above_max_length",
    "context_length_exceeded",
    "invalid_prompt",
    "content_policy_violation",
    "invalid_value",
    "unsupported_parameter",
    "unsupported_country_region_territory",
    "billing_hard_limit_reached",
)


def _classify_stream_failure(resp: Any) -> str:
    """返回 "retry_capacity" | "retry_unknown" | "surface"。"""
    err = resp.get("error") if isinstance(resp, dict) else None
    if not isinstance(err, dict):
        # 非成功终止但连 error 对象都没有 —— 未知形状，默认重试
        return "retry_unknown"
    code = str(err.get("code") or "").strip().lower()
    etype = str(err.get("type") or "").strip().lower()
    msg = str(err.get("message") or "").lower()
    if (
        code in _CAPACITY_CODES
        or any(m in msg for m in _CAPACITY_MARKERS)
        or etype in ("rate_limit_error", "service_unavailable_error")
        or code == "rate_limit_exceeded"
    ):
        return "retry_capacity"
    if etype in _DETERMINISTIC_ERROR_TYPES or code in _DETERMINISTIC_CODES:
        return "surface"
    if code.isdigit():
        c = int(code)
        if 400 <= c < 500 and c not in (408, 429):
            return "surface"
    return "retry_unknown"


def _output_to_choices(output_items: list[dict]) -> list[dict]:
    """Convert Responses API output items to Chat Completions choices format.

    LiteLLM UI only reads response.choices[0].message — synthesise it so the
    admin dashboard can render Responses API logs in pretty view.
    """
    parts: list[str] = []
    tool_calls: list[dict] = []
    for item in output_items:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "message":
            for block in item.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "output_text":
                    parts.append(block.get("text", ""))
        elif item_type == "reasoning":
            for s in item.get("summary") or []:
                if isinstance(s, dict) and s.get("type") == "summary_text":
                    parts.append(f"[reasoning] {s.get('text', '')}")
        elif item_type == "function_call":
            tool_calls.append(
                {
                    "id": item.get("call_id", ""),
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", ""),
                    },
                }
            )
    if not parts and not tool_calls:
        return []
    msg: dict[str, Any] = {"role": "assistant"}
    if parts:
        msg["content"] = "\n\n".join(parts)
    if tool_calls:
        msg["tool_calls"] = tool_calls
        if not parts:
            msg["content"] = None
    return [
        {
            "index": 0,
            "message": msg,
            "finish_reason": "tool_calls" if tool_calls else "stop",
        }
    ]


def _install_streaming_output_backfill() -> None:
    try:
        from litellm.responses.streaming_iterator import (
            BaseResponsesAPIStreamingIterator,
        )
    except ImportError:
        _log.warning("streaming_output_backfill: import failed, skip")
        return

    if getattr(BaseResponsesAPIStreamingIterator, "_output_backfill_patched", False):
        return

    _orig_init = BaseResponsesAPIStreamingIterator.__init__
    _orig_process = BaseResponsesAPIStreamingIterator._process_chunk

    def _patched_init(self, *args: Any, **kwargs: Any) -> None:
        _orig_init(self, *args, **kwargs)
        self._bp_output_items: dict[int, Any] = {}
        self._bp_content_started = False
        self._bp_saw_terminal = False

    def _patched_process_chunk(self, chunk: Any) -> Any:
        if not chunk or chunk == "[DONE]":
            return _orig_process(self, chunk)

        if not hasattr(self, "_bp_output_items"):
            self._bp_output_items = {}
        if not hasattr(self, "_bp_content_started"):
            self._bp_content_started = False
        if not hasattr(self, "_bp_saw_terminal"):
            self._bp_saw_terminal = False

        try:
            parsed = json.loads(chunk)
        except (json.JSONDecodeError, TypeError):
            return _orig_process(self, chunk)

        event_type = parsed.get("type") if isinstance(parsed, dict) else None

        if event_type in _CONTENT_STARTED_EVENTS:
            self._bp_content_started = True

        # [2026-08-10 形状4] 记录"见过成功终止"——response.completed/incomplete
        # 事件或裸 completed/incomplete response。流关闭时若从未见过成功终止
        # 且未下发过内容，由 __anext__ 包装层升格（空流形状，carher-75 实测：
        # 上游静默关流，零事件零入账，客户端只能拿到空 async-for）。
        if event_type in ("response.completed", "response.incomplete") or (
            event_type is None
            and isinstance(parsed, dict)
            and parsed.get("object") == "response"
            and parsed.get("status") in ("completed", "incomplete")
        ):
            self._bp_saw_terminal = True

        # capacity 升格。裸 response 对象（无 type、object=response）也覆盖 ——
        # 我们的 chatgpt-acct pod 就发这种形态（见 streaming_iterator 的 bare handler）。
        _failed_resp = None
        if event_type == "response.failed":
            _failed_resp = parsed.get("response")
        elif (
            event_type is None
            and isinstance(parsed, dict)
            and parsed.get("object") == "response"
            and parsed.get("status") == "failed"
        ):
            _failed_resp = parsed
        elif event_type == "error":
            # 第三种非成功形状：SSE `error` 事件（carher-14 2026-08-10 实测，
            # code=server_is_overloaded 以此形状透传）。error 字段可能平铺在
            # 事件顶层，也可能嵌在 error 子对象里，归一后交给分类器。
            _err_obj = parsed.get("error")
            if not isinstance(_err_obj, dict):
                _err_obj = {
                    "code": parsed.get("code"),
                    "message": parsed.get("message"),
                }
            _failed_resp = {"error": _err_obj}

        _fail_decision = (
            _classify_stream_failure(_failed_resp) if _failed_resp is not None else None
        )
        if _fail_decision in ("retry_capacity", "retry_unknown"):
            if self._bp_content_started:
                # 字节已出，重放不了，只能放行给客户端。
                _log.warning(
                    "streaming_output_backfill: capacity failure after content "
                    "already streamed — passing through, no failover possible"
                )
            else:
                _err = _failed_resp.get("error") or {}
                _msg = str(_err.get("message") or "model at capacity")
                _model = str(getattr(self, "model", "") or "")
                _provider = str(getattr(self, "custom_llm_provider", "") or "openai")
                _log.warning(
                    "streaming_output_backfill: escalating %s stream failure to "
                    "MidStreamFallbackError for failover (event_type=%s) — %s",
                    _fail_decision,
                    event_type,
                    _msg[:200],
                )
                # 载体必须是 MidStreamFallbackError，不能是 RateLimitError。
                # router.stream_with_fallbacks 的 except 只认前者；抛 RateLimitError
                # 会穿透整个 router 冒到 proxy_server.async_data_generator 的
                # except Exception，那里已经 yield 过 SSE header，只能把错误当正文
                # 下发 —— 客户端看到 HTTP 200 + body 里塞 error frame，既不 fallback
                # 也不 cooldown。2026-07-31 实测 6h 226 次升格全是空操作。
                if _fail_decision == "retry_capacity":
                    _rl: Any = _RateLimitError(
                        message=f"RateLimitError: (transient capacity, stream) - {_msg}",
                        model=_model,
                        llm_provider=_provider,
                    )
                else:
                    # 未知非成功形状：500 语义。冷却走 allowed_fails 阈值，
                    # 不像 429 那样单发即冷却 —— 防 7-15 式雪崩。
                    _rl = _InternalServerError(
                        message=f"InternalServerError: (non-success stream shape) - {_msg}",
                        model=_model,
                        llm_provider=_provider,
                    )
                # 不要在这里显式调 logging_obj.failure_handler！
                # 镜像层 streaming_handler.py 的 [carher-patch] 那样做是因为
                # CustomStreamWrapper 路径没有 _handle_failure；而本文件所在的
                # Responses 路径，__anext__ 的 except 已经会调 _handle_failure。
                # 2026-07-31 实测：额外显式调用会让单次 capacity 事件触发 3 次
                # failure handler，把 allowed_fails 计数放大 3 倍 —— 正是
                # 2026-07-15 sol 全组 cooldown 雪崩的成因方向。
                _exc = _MidStreamFallbackError(
                    message=f"MidStreamFallbackError: (transient capacity, stream) - {_msg}",
                    model=_model,
                    llm_provider=_provider,
                    original_exception=_rl,
                    # 这一分支的前提就是 _bp_content_started False（字节未出），
                    # 所以流可安全重放：不带续写 prompt，用原始 input 重试。
                    generated_content="",
                    is_pre_first_chunk=True,
                )
                # 盖上失败的 deployment id —— router._maybe_run_weighted_failover
                # 用它把这个 acct 从同组重挑里排除掉。**没有它同组重试根本不会发生**：
                # 该函数第一件事就是 `if not failed_id: return None`。
                # /v1/responses 走 _ageneric_api_call_with_fallbacks_helper，那条路
                # 不像 _acompletion 会调 _set_failed_deployment_id_on_exception，
                # 所以必须我们自己盖（2026-07-31 读代码确认）。
                _mid = None
                try:
                    _mid = (self._hidden_params or {}).get("model_id")
                except Exception:
                    _mid = None
                if not _mid:
                    _lm = getattr(self, "litellm_metadata", None) or {}
                    _mid = (_lm.get("model_info") or {}).get("id")
                if _mid:
                    _exc.failed_deployment_id = _mid
                else:
                    # 没盖上就退化成"直接跳组"，仍然比现状(啥都不做)好，但要能看见。
                    _log.warning(
                        "streaming_output_backfill: no model_id — same-group "
                        "retry will be skipped, falling straight to next group"
                    )
                raise _exc

        if event_type == "response.output_item.done":
            item = parsed.get("item")
            output_index = parsed.get(
                "output_index", max(self._bp_output_items, default=-1) + 1
            )
            if item is not None and isinstance(output_index, int):
                self._bp_output_items[output_index] = item

        elif event_type in ("response.completed", "response.incomplete"):
            resp = parsed.get("response")
            if isinstance(resp, dict):
                modified = False
                if not resp.get("output") and self._bp_output_items:
                    sorted_items = [
                        item
                        for _, item in sorted(self._bp_output_items.items())
                    ]
                    resp["output"] = sorted_items
                    modified = True
                    _log.info(
                        "streaming_output_backfill: backfilled %d output items into %s",
                        len(self._bp_output_items),
                        event_type,
                    )
                output = resp.get("output")
                if output and not resp.get("choices"):
                    resp["choices"] = _output_to_choices(output)
                    modified = True
                if modified:
                    chunk = json.dumps(parsed)

        return _orig_process(self, chunk)

    BaseResponsesAPIStreamingIterator.__init__ = _patched_init
    BaseResponsesAPIStreamingIterator._process_chunk = _patched_process_chunk
    BaseResponsesAPIStreamingIterator._output_backfill_patched = True
    _log.info("streaming_output_backfill: patched BaseResponsesAPIStreamingIterator")


_install_streaming_output_backfill()


def _install_empty_stream_escalation() -> None:
    """[2026-08-10 形状4] 空流升格：流结束却从未出现成功终止事件且未下发内容。

    carher-75 (hermestest-75) 实测：过载风暴期上游账号 pod 打开流后静默关闭，
    零事件 → StopAsyncIteration → 客户端拿到空 async-for，SpendLogs 零入账，
    _process_chunk 级探测（形状1-3）无从触发。此处包 __anext__：这种结束
    改抛 MidStreamFallbackError（500 语义载体，冷却走 allowed_fails 阈值），
    交给既有 failover 循环换号。已见成功终止或已下发内容的正常结束原样放行。
    """
    try:
        from litellm.exceptions import MidStreamFallbackError
        from litellm.responses.streaming_iterator import ResponsesAPIStreamingIterator
    except ImportError:
        _log.warning("empty_stream_escalation: import failed, skip")
        return

    if getattr(ResponsesAPIStreamingIterator, "_carher_empty_stream_patched", False):
        return

    _orig_anext = ResponsesAPIStreamingIterator.__anext__

    async def _patched_anext(self: Any) -> Any:
        try:
            return await _orig_anext(self)
        except StopAsyncIteration:
            if (
                getattr(self, "_bp_saw_terminal", False)
                or getattr(self, "_bp_content_started", False)
                or getattr(self, "_bp_empty_stream_escalated", False)
            ):
                raise
            self._bp_empty_stream_escalated = True
            _model = str(getattr(self, "model", "") or "")
            _provider = str(getattr(self, "custom_llm_provider", "") or "openai")
            _log.warning(
                "streaming_output_backfill: stream ended with no terminal event "
                "and no content (empty stream) — escalating to "
                "MidStreamFallbackError for failover model=%s",
                _model,
            )
            _ise = _InternalServerError(
                message="InternalServerError: (empty stream, no terminal event) - upstream returned no output",
                model=_model,
                llm_provider=_provider,
            )
            _exc = MidStreamFallbackError(
                message="MidStreamFallbackError: (empty stream) - upstream returned no output",
                model=_model,
                llm_provider=_provider,
                original_exception=_ise,
                generated_content="",
                is_pre_first_chunk=True,
            )
            _mid = None
            try:
                _mid = (self._hidden_params or {}).get("model_id")
            except Exception:
                _mid = None
            if not _mid:
                _lm = getattr(self, "litellm_metadata", None) or {}
                _mid = (_lm.get("model_info") or {}).get("id")
            if _mid:
                _exc.failed_deployment_id = _mid
            # 与 _process_chunk 升格路径不同，这里不在 __anext__ 的 except 里，
            # 需要自己触发 failure 记账（冷却/统计）；失败不阻断升格。
            try:
                self._handle_failure(_exc)
            except Exception:
                pass
            raise _exc

    ResponsesAPIStreamingIterator.__anext__ = _patched_anext
    ResponsesAPIStreamingIterator._carher_empty_stream_patched = True
    _log.info("streaming_output_backfill: patched ResponsesAPIStreamingIterator.__anext__ (empty-stream escalation)")


try:
    _install_empty_stream_escalation()
except Exception as _exc:  # pragma: no cover
    _log.error("empty_stream_escalation: install failed, skipped: %r", _exc)


# === responses 路径 failed_deployment_id 盖章 ===
# LiteLLM 只在 `_completion` / `_acompletion` 的 except 里调
# `_set_failed_deployment_id_on_exception`；`/v1/responses` 走的
# `_ageneric_api_call_with_fallbacks_helper` 没有这一步。于是
# `router._maybe_run_weighted_failover` 的第一道门（`if not failed_id: return None`）
# 把 responses 流量整个挡在同组重试之外 —— 198 实测 responses 占 88% 请求
# （479 vs chat/completions 65，2026-08-03 60min 窗口），等于
# `enable_weighted_failover` 对流量大头无效。
#
# 只对**可重试**错误盖章，判据直接复用 litellm 自己的 `_should_retry`
# （408/409/429/5xx 为真；400/401/403/404 为假）。确定性错误换个 acct 必然同样失败，
# 盖了只会多打一发无用上游调用 + 白冷却一个 acct 60s —— 198 实测 responses 路径上
# 400 有 56 次/12min，无差别盖章会把这批请求的上游负载翻倍。
#
# ⚠️ deployment 是 helper 的局部变量，wrapper 取不到，故用 ContextVar 从
# `async_get_available_deployment` 传递。**不要改用 kwargs 传**：那个 dict 会一路
# 流进 provider SDK，多一个未知键就是一次 TypeError（同 `_degrade_strip` 泄漏事故）。
_PICKED_DEPLOYMENT_ID: contextvars.ContextVar = contextvars.ContextVar(
    "carher_picked_deployment_id", default=None
)


def _is_retryable_failure(exc: Any) -> bool:
    """瞬时故障才值得换 acct 重试。宁漏不滥：判不出来就不盖。"""
    import litellm

    # 确定性失败先排除 —— 它们可能映射到可重试状态码，但换 acct 结果一样。
    for _name in ("ContextWindowExceededError", "ContentPolicyViolationError"):
        _klass = getattr(litellm, _name, None)
        if _klass is not None and isinstance(exc, _klass):
            return False

    # 超时 / 连不上：没有 status_code，但确实是瞬时故障。
    for _name in ("Timeout", "APIConnectionError"):
        _klass = getattr(litellm, _name, None)
        if _klass is not None and isinstance(exc, _klass):
            return True

    status = getattr(exc, "status_code", None)
    if status is None:
        return False
    try:
        return bool(litellm._should_retry(status_code=int(status)))
    except Exception:
        return False


def _install_responses_failed_deployment_stamp() -> None:
    try:
        from litellm.router import Router
    except ImportError:
        _log.warning("responses_failed_deployment_stamp: import failed, skip")
        return

    if getattr(Router, "_carher_responses_stamp_patched", False):
        return

    _orig_pick = Router.async_get_available_deployment
    _orig_helper = Router._ageneric_api_call_with_fallbacks_helper

    async def _patched_pick(self, *args: Any, **kwargs: Any) -> Any:
        deployment = await _orig_pick(self, *args, **kwargs)
        try:
            _PICKED_DEPLOYMENT_ID.set(
                ((deployment or {}).get("model_info") or {}).get("id")
            )
        except Exception:
            pass
        return deployment

    async def _patched_helper(self, *args: Any, **kwargs: Any) -> Any:
        # 先清空：同一个 task 内 fallback 链会重入本函数，不清会读到上一跳的 id，
        # 把"上一个 acct"当成"这次失败的 acct"排除掉。
        _PICKED_DEPLOYMENT_ID.set(None)
        try:
            return await _orig_helper(self, *args, **kwargs)
        except Exception as exc:
            # 已有值不覆盖：流式 capacity 那条路由本文件上面那段自己盖，
            # 且 litellm 的 `_set_failed_deployment_id_on_exception` 也是幂等语义。
            if not getattr(exc, "failed_deployment_id", None) and _is_retryable_failure(
                exc
            ):
                _dep_id = _PICKED_DEPLOYMENT_ID.get()
                if _dep_id:
                    try:
                        exc.failed_deployment_id = _dep_id
                    except Exception:
                        # 少数异常类禁止属性赋值 —— 放弃盖章，不能让它压掉原始异常。
                        pass
                    else:
                        _log.info(
                            "responses_failed_deployment_stamp: stamped %s on %s "
                            "(status=%s) for same-group failover",
                            _dep_id,
                            type(exc).__name__,
                            getattr(exc, "status_code", None),
                        )
            raise

    Router.async_get_available_deployment = _patched_pick
    Router._ageneric_api_call_with_fallbacks_helper = _patched_helper
    Router._carher_responses_stamp_patched = True
    _log.info(
        "responses_failed_deployment_stamp: patched Router "
        "(_ageneric_api_call_with_fallbacks_helper + async_get_available_deployment)"
    )


try:
    _install_responses_failed_deployment_stamp()
except Exception as _exc:  # pragma: no cover
    # 绝不能让这个增强把 callback 模块的 import 搞挂 —— 模块加载失败会连带
    # 上面那段 capacity 升格一起失效，甚至拖垮 proxy 启动（4 个 pod 同时崩）。
    # 宁可退化回"responses 路径没有同组重试"的现状，也不能扩大爆炸半径。
    _log.error("responses_failed_deployment_stamp: install failed, skipped: %r", _exc)


class StreamingOutputBackfillCallback(CustomLogger):
    pass


streaming_output_backfill = StreamingOutputBackfillCallback()
