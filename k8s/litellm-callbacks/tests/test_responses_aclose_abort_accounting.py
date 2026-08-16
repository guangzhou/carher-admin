"""test_responses_aclose_abort_accounting.py — 中断流补记账（P1，2026-08-16）。

背景
----
LiteLLM 只在解析到 RESPONSE_COMPLETED/RESPONSE_FAILED 时落 SpendLogs
（memory: project_198_litellm_spendlogs_disabled）。客户端中途断开
（Codex/Cursor 取消、超时）时上游已全额计量 input，而账上零记录。
198 撞顶实测新号只"看见"120-300M tok 就死 —— 无形消耗里这是主嫌。

修法：responses_aclose.py 的 aclose patch（客户端断开时 proxy 的
async_data_generator finally 必调）里，若迭代器从未见过终章 chunk 且
failure handler 未触发过 → 走 self._handle_failure() 落一条
status=failure 的账（带完整 request 元数据，token 可离线估算）。

钉住：
1. 中断流（completed_response=None）→ _handle_failure 被调一次，异常
   message 含 "AbortedStream"，status_code=499
2. 正常完结流（completed_response 非 None）→ 不触发
3. 已进过失败路径（_failure_handled=True）→ 不重复触发
4. 记账逻辑抛异常不阻断 socket 清理（stream_iterator/response 仍被关）

用法::

    python3 -m unittest test_responses_aclose_abort_accounting -v
"""
import asyncio
import importlib.util
import os
import sys
import types
import unittest

MODULE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "responses_aclose.py")


def _install_litellm_stubs():
    if "litellm" in sys.modules and hasattr(sys.modules["litellm"], "APIError"):
        return
    litellm = types.ModuleType("litellm")

    class APIError(Exception):
        def __init__(self, status_code=None, message="", llm_provider="", model=""):
            super().__init__(message)
            self.status_code = status_code
            self.message = message
            self.llm_provider = llm_provider
            self.model = model

    litellm.APIError = APIError

    integrations = types.ModuleType("litellm.integrations")
    custom_logger = types.ModuleType("litellm.integrations.custom_logger")

    class CustomLogger:
        pass

    custom_logger.CustomLogger = CustomLogger
    integrations.custom_logger = custom_logger
    litellm.integrations = integrations

    # responses_aclose import 时会去 patch 这个类；给一个可被 patch 的假类
    responses_mod = types.ModuleType("litellm.responses")
    streaming_mod = types.ModuleType("litellm.responses.streaming_iterator")

    class ResponsesAPIStreamingIterator:
        pass

    streaming_mod.ResponsesAPIStreamingIterator = ResponsesAPIStreamingIterator
    responses_mod.streaming_iterator = streaming_mod
    litellm.responses = responses_mod

    sys.modules.update({
        "litellm": litellm,
        "litellm.integrations": integrations,
        "litellm.integrations.custom_logger": custom_logger,
        "litellm.responses": responses_mod,
        "litellm.responses.streaming_iterator": streaming_mod,
    })


_install_litellm_stubs()

spec = importlib.util.spec_from_file_location("responses_aclose_under_test", MODULE_PATH)
MOD = importlib.util.module_from_spec(spec)
spec.loader.exec_module(MOD)

PatchedCls = sys.modules[
    "litellm.responses.streaming_iterator"].ResponsesAPIStreamingIterator


class _FakeCloseable:
    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


def _make_iterator(completed_response=None, failure_handled=False,
                   completed_logged=False, logging_obj=object()):
    it = PatchedCls()
    it.completed_response = completed_response
    it._completed_response_logged = completed_logged
    it._failure_handled = failure_handled
    it.logging_obj = logging_obj
    it.custom_llm_provider = "openai"
    it.model = "chatgpt-gpt-5.6-sol"
    it.stream_iterator = _FakeCloseable()
    it.response = _FakeCloseable()
    it._handle_failure_calls = []
    it._handle_failure = lambda exc: it._handle_failure_calls.append(exc)
    return it


class AbortAccountingTest(unittest.TestCase):
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def setUp(self):
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())

    def test_aborted_stream_records_failure_once(self):
        it = _make_iterator()
        self._run(it.aclose())
        self.assertEqual(len(it._handle_failure_calls), 1)
        exc = it._handle_failure_calls[0]
        self.assertIn("AbortedStream", str(exc))
        self.assertEqual(exc.status_code, 499)
        self.assertEqual(exc.model, "chatgpt-gpt-5.6-sol")

    def test_completed_stream_not_touched(self):
        it = _make_iterator(completed_response=object())
        self._run(it.aclose())
        self.assertEqual(it._handle_failure_calls, [])

    def test_already_failed_stream_not_double_counted(self):
        it = _make_iterator(failure_handled=True)
        self._run(it.aclose())
        self.assertEqual(it._handle_failure_calls, [])

    def test_already_logged_stream_not_touched(self):
        it = _make_iterator(completed_logged=True)
        self._run(it.aclose())
        self.assertEqual(it._handle_failure_calls, [])

    def test_no_logging_obj_skips_accounting(self):
        it = _make_iterator(logging_obj=None)
        self._run(it.aclose())
        self.assertEqual(it._handle_failure_calls, [])

    def test_accounting_error_does_not_block_socket_cleanup(self):
        it = _make_iterator()
        stream, resp = it.stream_iterator, it.response

        def _boom(exc):
            raise RuntimeError("accounting blew up")

        it._handle_failure = _boom
        self._run(it.aclose())
        self.assertTrue(stream.closed)
        self.assertTrue(resp.closed)

    def test_sockets_closed_on_normal_path_too(self):
        it = _make_iterator(completed_response=object())
        stream, resp = it.stream_iterator, it.response
        self._run(it.aclose())
        self.assertTrue(stream.closed)
        self.assertTrue(resp.closed)
        self.assertTrue(it.finished)


if __name__ == "__main__":
    unittest.main()
