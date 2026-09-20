# ── carher patch 2026-09-14: 失败日志不许阻塞事件循环 ─────────────────────
#
# 归档说明（本文件是 CM 里那段补丁的**唯一仓库副本**）：
#   线上位置 = ConfigMap `litellm-callbacks` 的 key `streaming_output_backfill.py` **末尾**，
#   subPath 挂到 litellm-proxy 容器的 /app/streaming_output_backfill.py。
#   它依赖该文件里已有的模块级 `_log`，**必须作为追加段存在**，不能单独当模块加载。
#   追加方式见 scripts/litellm-cm-append-patch.py，SOP 见 skill litellm-proxy-freeze-triage。
#
# 现场证据（py-spy dump，两个 worker 的 MainThread 栈逐帧相同）：
#   threading.py:359          Condition.wait(timeout=None)
#   futures/_base.py:455      Future.result(timeout=None)
#   asyncify.py:112           run_async_function
#   streaming_iterator.py:991 _handle_failure(exception=MidStreamFallbackError)
#   streaming_iterator.py:1089 __anext__      ← 跑在 asyncio 事件循环线程上
# 上游 asyncify.run_async_function 在"已有运行中事件循环"时的实现是
#   with ThreadPoolExecutor(max_workers=1) as ex: return ex.submit(fn).result()
# 既没有 timeout，with 退出时还要 shutdown(wait=True)。
# ⇒ **只给 future.result() 加 timeout 修不了**，with 退出照样 join。
# ⇒ async_failure_handler 里任何一个回调挂住 = 该 worker 事件循环永久死锁
# ⇒ /health/liveliness 恒超时 ⇒ kubelet 10×30s 后 SIGKILL(137) ⇒ 崩溃循环。
# 修法：有运行中的 loop 就 create_task 后台跑，绝不 join；没有 loop 时保持上游原行为。
_CARHER_BG_FAILURE_TASKS = set()


def _install_nonblocking_responses_failure_handler():
    import asyncio as _aio
    import traceback as _tb
    from datetime import datetime as _dt
    from litellm.responses.streaming_iterator import (
        ResponsesAPIStreamingIterator as _RSI,
    )

    if getattr(_RSI, "_carher_nonblocking_failure_patched", False):
        return
    _orig_handle_failure = _RSI._handle_failure

    def _patched_handle_failure(self, exception):
        if getattr(self, "_failure_handled", False):
            return
        try:
            loop = _aio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            return _orig_handle_failure(self, exception)
        self._failure_handled = True
        traceback_exception = _tb.format_exc()
        try:
            coro = self.logging_obj.async_failure_handler(
                exception=exception,
                traceback_exception=traceback_exception,
                start_time=self.start_time,
                end_time=_dt.now(),
            )
            # 强引用住，否则 asyncio 只持弱引用，任务可能被 GC 掉
            task = loop.create_task(coro)
            _CARHER_BG_FAILURE_TASKS.add(task)
            task.add_done_callback(_CARHER_BG_FAILURE_TASKS.discard)
        except Exception:
            pass

    _RSI._handle_failure = _patched_handle_failure
    _RSI._carher_nonblocking_failure_patched = True
    _log.info(
        "nonblocking_responses_failure_handler: patched "
        "ResponsesAPIStreamingIterator._handle_failure (不再 join 线程池)"
    )


try:
    _install_nonblocking_responses_failure_handler()
except Exception as _exc:  # pragma: no cover
    _log.error(
        "nonblocking_responses_failure_handler: install failed, skipped: %r", _exc
    )
