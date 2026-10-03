"""LiteLLM → 通衢 API 稳定性看板：attempt 事件推送方（custom callback）。

背景
====
稳定性看板的「兜底成功率」「上游异常率」「重试恢复率」依赖 *尝试级* 事件
（每次上游调用：原始尝试、retry、fallback），而上游 /spend/logs/v2 原始日志
只有每条请求的最终状态，无法还原 fallback 过程。因此需要 LiteLLM Proxy 侧
在每个 attempt 完成时，把 attempt 事件推送到看板采集接口
``POST /api/internal/observability/events``。

本模块是 LiteLLM 的 ``CustomLogger`` 子类，通过 config.yaml 注册：

.. code-block:: yaml

    litellm_settings:
      callbacks:
        - observability_attempt_pusher.attempt_pusher_instance

模块文件放在 config.yaml 同目录（LiteLLM 通过 ``get_instance_fn`` 按
``config_file_path`` 目录加载 ``<module>.py``），或放进 Python path 可导入的位置。

行为约定
========
- 每次 attempt（成功/失败）异步入队，后台批量推送，绝不阻塞/影响代理主流程；
- 事件字段严格限制在看板白名单内，绝不发送 prompt/response/key 等敏感字段；
- 错误消息在发送前脱敏并截断；
- HMAC 签名算法与看板 ``internal_observability_events`` 完全一致：
  ``signature = sha256_hmac(secret, f"{timestamp}.{sha256(body)}")``，头
  ``x-observability-timestamp`` / ``x-observability-signature: sha256=<hex>``；
- eventId 在 backend_id + call_id + attempt_index 维度唯一，看板侧幂等去重；
- 任一配置缺失时推送方自动禁用（记录日志），不影响代理运行。

环境变量（在 LiteLLM Proxy 部署中配置）
========================================
OBSERVABILITY_INGEST_URL        采集接口地址（默认 https://myai.carher.net/api/internal/observability/events）
OBSERVABILITY_INGEST_HMAC_SECRET 与看板 OBSERVABILITY_INGEST_HMAC_SECRET 相同的密钥（必填，否则禁用）
OBSERVABILITY_BACKEND_ID        本 LiteLLM 后端的标识，必须与看板 usage_backend_ids()（或
                                USAGE_SNAPSHOT_BACKEND_IDS）中的 id 一致（必填）
OBSERVABILITY_INGEST_BATCH_MAX  每批事件数上限，1-500（默认 100）
OBSERVABILITY_INGEST_FLUSH_SECONDS 后台冲刷间隔秒（默认 3）
OBSERVABILITY_INGEST_TIMEOUT_SECONDS 单次 HTTP 超时秒（默认 3）
OBSERVABILITY_INGEST_DISABLED   置为 "true" 强制禁用（默认按配置自动判断）
OBSERVABILITY_INGEST_MAX_QUEUE  内存队列上限，超出丢弃最旧事件（默认 5000）
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

try:  # LiteLLM proxy 环境必定有 httpx
    import httpx
except Exception:  # pragma: no cover - 仅在代理外导入时可能缺失
    httpx = None  # type: ignore[assignment]

from litellm.integrations.custom_logger import CustomLogger

logger = logging.getLogger("observability.attempt_pusher")

# 与看板 main.py _OBSERVABILITY_EVENT_FIELDS 保持一致（camelCase，看板端自动转 snake_case）。
_ALLOWED_FIELDS = frozenset(
    {
        "eventId", "backendId", "traceId", "requestId", "attemptId",
        "attemptIndex", "requestedModelGroup", "actualModel", "route",
        "provider", "eventType", "status", "errorCode", "errorClass",
        "errorCategory", "errorMessage", "scenario", "scenarioVersion",
        "startedAt", "endedAt", "eventTime", "collectedAt", "ttftMs",
        "durationMs", "retryIndex", "isRetry", "isFallback", "routeName",
        "fallbackFrom", "fallbackTo",
    }
)
_FORBIDDEN_SUBSTRINGS = (
    "prompt", "messages", "response", "completion", "content", "body",
    "choices", "api_key", "apiKey", "authorization", "token", "secret",
    "traceback", "exception",
)

_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]+"),
    re.compile(r"Bearer\s+\S+", re.IGNORECASE),
    re.compile(r"(?i)\b(api[_ -]?key|token|password|passwd|secret|authorization|access[_ -]?token)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"(?i)([?&](?:api[_-]?key|token|password|secret|authorization)=)[^&\s]+"),
    re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"),
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    re.compile(r"https?://[^\s]+"),
    re.compile(r"\b[A-Za-z0-9_-]{32,}\b"),
)


def _redact_message(value: Any) -> str:
    """脱敏并截断错误消息：去除密钥/邮箱/IP/长串，且绝不含请求正文。"""
    text = str(value or "").strip()
    if not text:
        return ""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("<redacted>", text)
    return text[:300]


def _as_float(value: Any) -> Optional[float]:
    try:
        result = float(value)
        return result if result == result and abs(result) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int:
    try:
        return max(0, int(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def _text(value: Any, limit: int = 256) -> str:
    return str(value or "").strip()[:limit]


def _epoch(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, datetime):
            return value.timestamp()
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.timestamp()
    except (TypeError, ValueError):
        return None


def _iso_utc(value: Any) -> Optional[str]:
    epoch = _epoch(value)
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


class ObservabilityAttemptPusher(CustomLogger):
    """把每次 attempt（成功/失败）批量推送到通衢 API 稳定性看板。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._enabled = False
        self._url = ""
        self._secret = ""
        self._backend_id = ""
        self._batch_max = 100
        self._flush_seconds = 3.0
        self._timeout_seconds = 3.0
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._flush_task: asyncio.Task[Any] | None = None
        self._http_client: httpx.AsyncClient | None = None
        self._attempt_counters: dict[str, int] = {}
        self._setup()

    # ------------------------------------------------------------------ setup

    def _setup(self) -> None:
        if str(os.getenv("OBSERVABILITY_INGEST_DISABLED", "")).strip().lower() in {
            "1", "true", "yes", "on",
        }:
            logger.info("observability attempt pusher disabled by OBSERVABILITY_INGEST_DISABLED")
            return
        if httpx is None:
            logger.warning("observability attempt pusher disabled: httpx unavailable")
            return
        secret = os.getenv("OBSERVABILITY_INGEST_HMAC_SECRET", "").strip()
        backend_id = os.getenv("OBSERVABILITY_BACKEND_ID", "").strip()
        url = os.getenv(
            "OBSERVABILITY_INGEST_URL",
            "https://myai.carher.net/api/internal/observability/events",
        ).strip()
        if not secret or not backend_id or not url:
            logger.warning(
                "observability attempt pusher disabled: missing OBSERVABILITY_INGEST_HMAC_SECRET / "
                "OBSERVABILITY_BACKEND_ID / OBSERVABILITY_INGEST_URL"
            )
            return
        self._enabled = True
        self._url = url
        self._secret = secret
        self._backend_id = backend_id
        self._batch_max = max(1, min(500, _as_int(os.getenv("OBSERVABILITY_INGEST_BATCH_MAX", "100"))))
        self._flush_seconds = max(0.5, _as_float(os.getenv("OBSERVABILITY_INGEST_FLUSH_SECONDS", "3")) or 3.0)
        self._timeout_seconds = max(0.5, _as_float(os.getenv("OBSERVABILITY_INGEST_TIMEOUT_SECONDS", "3")) or 3.0)
        self._queue = asyncio.Queue(maxsize=max(100, _as_int(os.getenv("OBSERVABILITY_INGEST_MAX_QUEUE", "5000"))))
        self._http_client = httpx.AsyncClient(timeout=httpx.Timeout(self._timeout_seconds))
        logger.info(
            "observability attempt pusher enabled backend=%s url=%s batch_max=%s flush=%ss",
            backend_id, url, self._batch_max, self._flush_seconds,
        )

    def _ensure_flush_task(self) -> None:
        if not self._enabled or self._queue is None:
            return
        if self._flush_task is None or self._flush_task.done():
            loop = asyncio.get_running_loop()
            self._flush_task = loop.create_task(self._flush_loop(), name="observability-attempt-pusher")

    # ------------------------------------------------------------ event build

    def _build_attempt(
        self,
        kwargs: dict[str, Any],
        response_obj: Any,
        start_time: Any,
        end_time: Any,
        *,
        status: str,
        exception: Optional[BaseException] = None,
    ) -> dict[str, Any]:
        litellm_params = kwargs.get("litellm_params") or {}
        metadata = litellm_params.get("metadata") or kwargs.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}

        call_id = _text(
            kwargs.get("litellm_call_id")
            or litellm_params.get("litellm_call_id")
            or metadata.get("litellm_call_id")
        )
        request_id = _text(
            kwargs.get("request_id")
            or litellm_params.get("request_id")
            or metadata.get("request_id")
            or call_id
        )
        trace_id = _text(metadata.get("trace_id") or metadata.get("traceId") or call_id or request_id)
        model_group = _text(
            metadata.get("model_group")
            or kwargs.get("model")
            or litellm_params.get("model")
            or litellm_params.get("model_group")
        )
        actual_model = _text(litellm_params.get("model") or metadata.get("actual_model") or model_group)
        provider = _text(
            litellm_params.get("custom_llm_provider")
            or litellm_params.get("provider")
            or kwargs.get("custom_llm_provider")
        )
        attempted_retries = _as_int(metadata.get("attempted_retries"))
        max_retries = _as_int(metadata.get("max_retries"))
        retry_index = attempted_retries if attempted_retries > 0 else 0
        attempt_index = attempted_retries  # Router 每次 retry 递增；首次尝试为 0

        # fallback 信号：成功响应携带 x-litellm-attempted-fallbacks 头（写入 hidden params）。
        attempted_fallbacks = self._read_fallback_header(response_obj)

        start_epoch = _epoch(start_time)
        end_epoch = _epoch(end_time)
        duration_ms = None
        if start_epoch is not None and end_epoch is not None:
            duration_ms = round(max(0.0, (end_epoch - start_epoch) * 1000), 2)

        event: dict[str, Any] = {
            "eventId": f"{self._backend_id}:{call_id or request_id}:{attempt_index}",
            "backendId": self._backend_id,
            "traceId": trace_id,
            "requestId": request_id,
            "attemptId": call_id or request_id,
            "attemptIndex": attempt_index,
            "retryIndex": retry_index,
            "requestedModelGroup": model_group,
            "actualModel": actual_model,
            "routeName": _text(litellm_params.get("model_group") or model_group),
            "provider": provider,
            "eventType": status,
            "status": status,
            "startedAt": _iso_utc(start_time),
            "endedAt": _iso_utc(end_time),
            "durationMs": duration_ms,
            "isRetry": retry_index > 0,
            # fallback 信号只能从成功响应头 x-litellm-attempted-fallbacks 读到：
            # 经历过兜底且最终成功的请求会标记 isFallback，最终仍失败的请求
            # 只有 failure 事件（不计入 recovered，仍计入 fallback_triggered）。
            "isFallback": attempted_fallbacks is not None and attempted_fallbacks > 0,
        }
        if attempted_fallbacks and attempted_fallbacks > 0:
            event["fallbackFrom"] = model_group
            event["fallbackTo"] = actual_model or "unknown"

        if status == "failure" and exception is not None:
            error_code = _text(getattr(exception, "status_code", "") or getattr(exception, "code", ""), 120)
            error_class = _text(type(exception).__name__, 160)
            message = _redact_message(str(exception))
            if error_code:
                event["errorCode"] = error_code
            if error_class:
                event["errorClass"] = error_class
            if message:
                event["errorMessage"] = message
        elif status == "failure":
            # 从 kwargs 携带的错误信息兜底（无 exception 对象时）。
            error_info = kwargs.get("error_information") or {}
            if isinstance(error_info, dict):
                event["errorCode"] = _text(error_info.get("error_code") or error_info.get("errorCode"), 120)
                event["errorClass"] = _text(error_info.get("error_class") or error_info.get("errorClass"), 160)
                event["errorMessage"] = _redact_message(error_info.get("error_message") or error_info.get("errorMessage"))

        # 只保留白名单字段，杜绝敏感字段外泄。
        return {key: value for key, value in event.items() if key in _ALLOWED_FIELDS and value is not None and value != ""}

    @staticmethod
    def _read_fallback_header(response_obj: Any) -> Optional[int]:
        try:
            hidden = getattr(response_obj, "_hidden_params", None) or {}
            if not isinstance(hidden, dict):
                return None
            additional = hidden.get("additional_headers") or {}
            if not isinstance(additional, dict):
                return None
            value = additional.get("x-litellm-attempted-fallbacks")
            parsed = _as_int(value)
            return parsed if value is not None else None
        except Exception:
            return None

    # ---------------------------------------------------------------- hooks

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time) -> None:
        if not self._enabled:
            return
        try:
            event = self._build_attempt(
                kwargs, response_obj, start_time, end_time, status="success"
            )
            await self._enqueue(event)
        except Exception:
            logger.exception("observability pusher failed to build success event")

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time) -> None:
        if not self._enabled:
            return
        try:
            exception = kwargs.get("exception")
            event = self._build_attempt(
                kwargs, response_obj, start_time, end_time,
                status="failure", exception=exception,
            )
            await self._enqueue(event)
        except Exception:
            logger.exception("observability pusher failed to build failure event")

    async def _enqueue(self, event: dict[str, Any]) -> None:
        if self._queue is None:
            return
        self._ensure_flush_task()
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(event)
            except Exception:
                pass  # 队列不可用时静默丢弃，绝不影响代理主流程

    # ---------------------------------------------------------------- flush

    async def _flush_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._flush_seconds)
                await self._flush_once()
            except asyncio.CancelledError:
                await self._flush_once()
                raise
            except Exception:
                logger.exception("observability pusher flush loop error")

    async def _flush_once(self) -> None:
        if self._queue is None or self._http_client is None:
            return
        batch: list[dict[str, Any]] = []
        while len(batch) < self._batch_max:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if not batch:
            return
        body = json.dumps({"events": batch}, ensure_ascii=False).encode("utf-8")
        timestamp = str(int(time.time()))
        digest = hashlib.sha256(body).hexdigest()
        signature = hmac.new(
            self._secret.encode("utf-8"),
            f"{timestamp}.{digest}".encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        try:
            response = await self._http_client.post(
                self._url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "x-observability-timestamp": timestamp,
                    "x-observability-signature": f"sha256={signature}",
                },
            )
            if response.status_code >= 300:
                logger.warning(
                    "observability pusher ingest rejected status=%s body=%s",
                    response.status_code, _redact_message(response.text)[:200],
                )
                return
            logger.debug("observability pusher flushed events=%s", len(batch))
        except Exception as exc:
            # 推送失败：降级丢弃本批（幂等 eventId 保证重推不会重复）。
            logger.warning("observability pusher ingest failed: %s", exc.__class__.__name__)

    # ---------------------------------------------------------------- teardown

    async def async_log_audit_log_event(self, audit_log: Any) -> None:
        """Proxy 关闭时冲刷剩余队列（挂载为 audit hook 由代理生命周期调用）。"""
        try:
            await self._flush_once()
        except Exception:
            pass


attempt_pusher_instance = ObservabilityAttemptPusher()
