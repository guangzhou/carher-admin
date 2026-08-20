"""test_budget_notice.py — key 日额度可见性三件套之①②的回归。

① /查余额|/quota 触发判定（按行全等，不误伤子串）+ pre-call 短路：
   chat/responses 走 ``data["mock_response"]``，anthropic 路由抛
   ModifyResponseException（stream 有原生 200 处理）。
② ≥90% 流末注入：chat 对象流在 finish chunk 前插 delta；anthropic 字节流在
   ``event: message_delta`` 前插一个完整 content block —— **split-position
   穷举**：marker 在任意 chunk 边界被切开都必须注入成功且原字节一字不丢
   （参考 test_streaming_bridge_done_filter 的穷举纪律）。

③ 100% 友好 429 在 test_error_sanitize.py（BudgetFriendly429Test）。

用法::

    cd k8s/litellm-callbacks/tests
    .venv/bin/python -m unittest test_budget_notice -v
"""
import asyncio
import datetime
import importlib.util
import json
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def _ensure_stubs():
    """幂等补齐 litellm 桩。unittest discover 下本文件按字母序先于
    test_error_sanitize 加载，所以这里必须装**超集**（含 error_sanitize 需要的
    core_utils / proxy 桩），否则后者见 litellm 已存在会跳过自装。"""
    litellm = sys.modules.get("litellm")
    if litellm is None:
        litellm = types.ModuleType("litellm")
        sys.modules["litellm"] = litellm

    if "litellm.integrations.custom_logger" not in sys.modules:
        integrations = types.ModuleType("litellm.integrations")
        custom_logger = types.ModuleType("litellm.integrations.custom_logger")

        class CustomLogger:
            pass

        custom_logger.CustomLogger = CustomLogger
        integrations.custom_logger = custom_logger
        litellm.integrations = integrations
        sys.modules["litellm.integrations"] = integrations
        sys.modules["litellm.integrations.custom_logger"] = custom_logger

    exc_mod = sys.modules.get("litellm.exceptions")
    if exc_mod is None:
        exc_mod = types.ModuleType("litellm.exceptions")
        litellm.exceptions = exc_mod
        sys.modules["litellm.exceptions"] = exc_mod
    # 属性级幂等：discover 全量跑时其它测试文件可能整体替换过这个桩模块
    if not hasattr(exc_mod, "ModifyResponseException"):

        class ModifyResponseException(Exception):
            def __init__(self, message, model="", request_data=None, **kw):
                self.message = message
                self.model = model
                self.request_data = request_data or {}
                super().__init__(message)

        exc_mod.ModifyResponseException = ModifyResponseException

    if "litellm.litellm_core_utils.litellm_logging" not in sys.modules:
        core_utils = types.ModuleType("litellm.litellm_core_utils")
        logging_mod = types.ModuleType("litellm.litellm_core_utils.litellm_logging")

        class StandardLoggingPayloadSetup:
            @staticmethod
            def get_error_information(original_exception=None, traceback_str=None):
                return {
                    "error_class": type(original_exception).__name__,
                    "error_code": str(getattr(original_exception, "status_code", "")),
                    "error_message": str(getattr(original_exception, "message",
                                                 original_exception)),
                    "traceback": traceback_str or "",
                }

        logging_mod.StandardLoggingPayloadSetup = StandardLoggingPayloadSetup
        core_utils.litellm_logging = logging_mod
        litellm.litellm_core_utils = core_utils
        sys.modules["litellm.litellm_core_utils"] = core_utils
        sys.modules["litellm.litellm_core_utils.litellm_logging"] = logging_mod

    if "litellm.responses.streaming_iterator" not in sys.modules:
        responses_pkg = types.ModuleType("litellm.responses")
        si = types.ModuleType("litellm.responses.streaming_iterator")

        class CachedResponsesAPIStreamingIterator:
            def __init__(self, response, logging_obj=None, request_data=None,
                         call_type=None):
                self._events = ["EV1:" + type(response).__name__, "EV2:done"]

            def __aiter__(self):
                self._i = 0
                return self

            async def __anext__(self):
                if self._i >= len(self._events):
                    raise StopAsyncIteration
                self._i += 1
                return self._events[self._i - 1]

        si.CachedResponsesAPIStreamingIterator = CachedResponsesAPIStreamingIterator
        responses_pkg.streaming_iterator = si
        litellm.responses = responses_pkg
        sys.modules["litellm.responses"] = responses_pkg
        sys.modules["litellm.responses.streaming_iterator"] = si

    if "litellm.proxy.common_request_processing" not in sys.modules:
        proxy_mod = sys.modules.get("litellm.proxy")
        if proxy_mod is None:
            proxy_mod = types.ModuleType("litellm.proxy")
        crp = types.ModuleType("litellm.proxy.common_request_processing")

        class ProxyBaseLLMRequestProcessing:
            @staticmethod
            def get_custom_headers(**kwargs):
                return dict(kwargs.pop("_headers", {}))

        crp.ProxyBaseLLMRequestProcessing = ProxyBaseLLMRequestProcessing
        proxy_mod.common_request_processing = crp
        litellm.proxy = proxy_mod
        sys.modules["litellm.proxy"] = proxy_mod
        sys.modules["litellm.proxy.common_request_processing"] = crp


def _load():
    _ensure_stubs()
    path = os.path.join(HERE, "..", "budget_notice.py")
    spec = importlib.util.spec_from_file_location("bn", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


M = _load()

PREFIX_ENV = {"BUDGET_NOTICE_KEY_PREFIXES": "claude-code-,cursor-"}


class _Key:
    def __init__(self, alias="claude-code-u1", spend=1.0, max_budget=70.0,
                 token="tok-default", reset_at=datetime.datetime(2026, 8, 19, 16, 0),
                 route="/v1/chat/completions"):
        self.key_alias = alias
        self.spend = spend
        self.max_budget = max_budget
        self.token = token
        self.budget_reset_at = reset_at
        self.budget_duration = "1d"
        self.request_route = route


class _CallType:
    """CallTypes 枚举形状：str() 是 'CallTypes.x'，只有 .value 是裸值。"""

    def __init__(self, value):
        self.value = value

    def __str__(self):
        return f"CallTypes.{self.value}"


def _with_env(env, fn):
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        return fn()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _pre(data, call_type, key=None):
    _ensure_stubs()  # discover 全量下其它文件可能替换过桩，取用前再补一次

    async def go():
        return await M.budget_notice.async_pre_call_hook(
            key or _Key(), None, data, _CallType(call_type))
    return _with_env(PREFIX_ENV, lambda: asyncio.run(go()))


class TriggerMatchTest(unittest.TestCase):
    def test_exact_lines_trigger(self):
        for t in ("/查余额", "查余额", "/quota", "/QUOTA", "  /查余额  "):
            self.assertTrue(M._is_quota_query(t), t)

    def test_wrapped_own_line_triggers(self):
        text = "<context>...</context>\n/查余额\n<system-reminder>x</system-reminder>"
        self.assertTrue(M._is_quota_query(text))

    def test_tag_wrapped_line_triggers(self):
        """Cursor 把用户输入包成单行 <user_query>…</user_query>。"""
        for t in ("<user_query>查余额</user_query>",
                  "<user_query>/查余额</user_query>",
                  "ctx\n<user_query>/quota</user_query>\nmore"):
            self.assertTrue(M._is_quota_query(t), t)

    def test_tag_wrapped_prose_does_not_trigger(self):
        for t in ("<user_query>帮我看下查余额的实现</user_query>",
                  "<user_query>quota 报表</user_query>"):
            self.assertFalse(M._is_quota_query(t), t)

    def test_substring_does_not_trigger(self):
        for t in ("帮我看下 /查余额 的实现", "查余额功能坏了", "quota", "/quotas",
                  "", "x" * 30000):
            self.assertFalse(M._is_quota_query(t), t[:40])


class LastUserTextTest(unittest.TestCase):
    def test_chat_string_content(self):
        d = {"messages": [{"role": "user", "content": "/quota"}]}
        self.assertEqual(M._last_user_text(d), "/quota")

    def test_takes_last_user_skipping_assistant(self):
        d = {"messages": [
            {"role": "user", "content": "/查余额"},
            {"role": "assistant", "content": "好的"},
            {"role": "user", "content": "正常问题"},
        ]}
        self.assertEqual(M._last_user_text(d), "正常问题")

    def test_anthropic_block_content(self):
        d = {"messages": [{"role": "user", "content": [
            {"type": "text", "text": "/查余额"},
            {"type": "text", "text": "<system-reminder>x</system-reminder>"},
        ]}]}
        self.assertTrue(M._is_quota_query(M._last_user_text(d)))

    def test_responses_input_string(self):
        self.assertEqual(M._last_user_text({"input": "/quota"}), "/quota")

    def test_responses_input_items(self):
        d = {"input": [{"role": "user",
                        "content": [{"type": "input_text", "text": "/quota"}]}]}
        self.assertEqual(M._last_user_text(d), "/quota")


class PreCallTest(unittest.TestCase):
    def setUp(self):
        _ensure_stubs()  # 其它测试文件可能在 discover 中途替换过 litellm 桩

    def test_chat_sets_mock_response(self):
        d = {"messages": [{"role": "user", "content": "/查余额"}]}
        out = _pre(d, "acompletion")
        self.assertIn("今日用量", out.get("mock_response", ""))
        self.assertIn("$70.00", out["mock_response"])

    def test_responses_sets_mock_response(self):
        d = {"input": "/quota"}
        out = _pre(d, "aresponses")
        self.assertIn("今日用量", out.get("mock_response", ""))

    def test_anthropic_raises_modify_response(self):
        from litellm.exceptions import ModifyResponseException

        d = {"model": "gpt-x", "messages": [{"role": "user", "content": "/查余额"}]}
        with self.assertRaises(ModifyResponseException) as ctx:
            _pre(d, "aanthropic_messages")
        self.assertIn("今日用量", ctx.exception.message)

    def test_ungated_alias_untouched(self):
        d = {"messages": [{"role": "user", "content": "/查余额"}]}
        out = _pre(d, "acompletion", key=_Key(alias="other-user"))
        self.assertNotIn("mock_response", out)

    def test_disabled_env_untouched(self):
        d = {"messages": [{"role": "user", "content": "/查余额"}]}

        def go():
            return _pre(d, "acompletion")

        out = _with_env({"BUDGET_NOTICE_DISABLED": "1"}, go)
        self.assertNotIn("mock_response", out)

    def test_normal_prompt_untouched(self):
        d = {"messages": [{"role": "user", "content": "hello"}]}
        out = _pre(d, "acompletion")
        self.assertNotIn("mock_response", out)

    def test_embedding_call_type_ignored(self):
        d = {"messages": [{"role": "user", "content": "/查余额"}]}
        out = _pre(d, "aembedding")
        self.assertNotIn("mock_response", out)

    def test_no_budget_key_reports_lifetime(self):
        d = {"messages": [{"role": "user", "content": "/quota"}]}
        out = _pre(d, "acompletion", key=_Key(max_budget=None, spend=12.3))
        self.assertIn("未设周期限额", out["mock_response"])


# ------------------------------------------------------------------ ② 注入

class _Delta:
    def __init__(self, content=None):
        self.content = content
        self.tool_calls = None
        self.function_call = None


class _Choice:
    def __init__(self, content=None, finish=None):
        self.delta = _Delta(content)
        self.finish_reason = finish


class _Chunk:
    def __init__(self, content=None, finish=None):
        self.choices = [_Choice(content, finish)]


async def _agen(items):
    for it in items:
        yield it


def _run_stream(items, key):
    async def go():
        out = []
        gen = M.budget_notice.async_post_call_streaming_iterator_hook(
            key, _agen(items), {})
        async for x in gen:
            out.append(x)
        return out
    return _with_env(PREFIX_ENV, lambda: asyncio.run(go()))


def _warn_key(token):
    return _Key(alias="cursor-u9", spend=65.0, max_budget=70.0, token=token)


class ChatInjectTest(unittest.TestCase):
    def test_inject_before_finish_chunk(self):
        items = [_Chunk("mock "), _Chunk("answer"), _Chunk(None, finish="stop")]
        out = _run_stream(items, _warn_key("tok-chat-1"))
        self.assertEqual(len(out), 4)
        notice = out[2]
        self.assertIn("今日额度已用 93%", notice.choices[0].delta.content)
        self.assertIsNone(notice.choices[0].finish_reason)
        self.assertEqual(out[3].choices[0].finish_reason, "stop")

    def test_daily_dedupe_local(self):
        items = lambda: [_Chunk("x"), _Chunk(None, finish="stop")]
        out1 = _run_stream(items(), _warn_key("tok-chat-2"))
        out2 = _run_stream(items(), _warn_key("tok-chat-2"))
        self.assertEqual(len(out1), 3)
        self.assertEqual(len(out2), 2)

    def test_below_ratio_no_inject(self):
        items = [_Chunk("x"), _Chunk(None, finish="stop")]
        key = _Key(alias="cursor-u9", spend=10.0, max_budget=70.0, token="tok-chat-3")
        out = _run_stream(items, key)
        self.assertEqual(len(out), 2)

    def test_ungated_no_inject(self):
        items = [_Chunk("x"), _Chunk(None, finish="stop")]
        key = _Key(alias="zerokey-9", spend=65.0, max_budget=70.0, token="tok-chat-4")
        out = _run_stream(items, key)
        self.assertEqual(len(out), 2)

    def test_responses_route_object_stream_not_touched(self):
        """responses 路径的 hook 层也是 chat 形状 —— 对象流只在明确
        chat/completions 路由时注入，其余透传并释放当日名额。"""
        items = [_Chunk("x"), _Chunk(None, finish="stop")]
        key = _Key(alias="cursor-u9", spend=65.0, max_budget=70.0,
                   token="tok-chat-5", route="/v1/responses")
        out = _run_stream(items, key)
        self.assertEqual(len(out), 2)
        # 名额已释放：换到 chat 路由立刻能注入
        key2 = _Key(alias="cursor-u9", spend=65.0, max_budget=70.0,
                    token="tok-chat-5")
        out2 = _run_stream([_Chunk("x"), _Chunk(None, finish="stop")], key2)
        self.assertEqual(len(out2), 3)


def _anthropic_wire(final_block="text"):
    """真实桥接流形态：text(空)@0 + thinking@1 + <final_block>@2 + 收尾。"""
    def ev(name, obj):
        return (f"event: {name}\ndata: " + json.dumps(obj) + "\n\n").encode()

    if final_block == "text":
        final = (
            ev("content_block_start", {"type": "content_block_start", "index": 2,
                                       "content_block": {"type": "text", "text": ""}})
            + ev("content_block_delta", {"type": "content_block_delta", "index": 2,
                                         "delta": {"type": "text_delta", "text": "白色"}})
            + ev("content_block_stop", {"type": "content_block_stop", "index": 2})
        )
    else:
        final = (
            ev("content_block_start", {"type": "content_block_start", "index": 2,
                                       "content_block": {"type": "tool_use",
                                                         "id": "t1", "name": "bash",
                                                         "input": {}}})
            + ev("content_block_stop", {"type": "content_block_stop", "index": 2})
        )
    return (
        ev("message_start", {"type": "message_start",
                             "message": {"id": "m1", "content": []}})
        + ev("content_block_start", {"type": "content_block_start", "index": 0,
                                     "content_block": {"type": "text", "text": ""}})
        + ev("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "text_delta", "text": ""}})
        + ev("content_block_stop", {"type": "content_block_stop", "index": 0})
        + ev("content_block_start", {"type": "content_block_start", "index": 1,
                                     "content_block": {"type": "thinking",
                                                       "thinking": ""}})
        + ev("content_block_delta", {"type": "content_block_delta", "index": 1,
                                     "delta": {"type": "thinking_delta",
                                               "thinking": "hm"}})
        + ev("content_block_stop", {"type": "content_block_stop", "index": 1})
        + final
        + ev("message_delta", {"type": "message_delta",
                               "delta": {"stop_reason": "end_turn"}})
        + ev("message_stop", {"type": "message_stop"})
    )


def _expected_injected(wire, key):
    """提醒 delta 必须插在 index=2 的 content_block_stop **之前**（同 block 内）。"""
    stop2 = ('event: content_block_stop\ndata: '
             + json.dumps({"type": "content_block_stop", "index": 2}) + "\n\n").encode()
    assert wire.count(stop2) == 1
    return wire.replace(stop2, M._notice_delta_bytes(M._warn_text(key), 2) + stop2, 1)


class AnthropicInjectTest(unittest.TestCase):
    def test_single_chunk_inject_into_last_text_block(self):
        wire = _anthropic_wire()
        key = _warn_key("tok-a-1")
        out = b"".join(_run_stream([wire], key))
        self.assertEqual(out.count("今日额度已用".encode()), 1)
        self.assertEqual(out, _expected_injected(wire, key))

    def test_split_position_sweep(self):
        """最后一个 text block 的 stop 与 message_delta 区域内任意切分，
        注入位置与字节完整性都不能变。"""
        wire = _anthropic_wire()
        anchor = wire.find(b'{"type": "content_block_stop", "index": 2}')
        end = wire.find(b"event: message_delta") + len(b"event: message_delta") + 2
        for split in range(max(0, anchor - 30), end):
            key = _warn_key(f"tok-sweep-{split}")
            out = b"".join(_run_stream([wire[:split], wire[split:]], key))
            self.assertEqual(out, _expected_injected(wire, key), f"split={split}")

    def test_extreme_fragmentation(self):
        wire = _anthropic_wire()
        for size in (1, 3, 7, 31):
            key = _warn_key(f"tok-frag-{size}")
            chunks = [wire[i:i + size] for i in range(0, len(wire), size)]
            out = b"".join(_run_stream(chunks, key))
            self.assertEqual(out, _expected_injected(wire, key), f"size={size}")

    def test_tool_use_final_block_skips_and_releases(self):
        """以 tool_use 收尾的轮次不注入、字节透传，且当日名额被释放 ——
        紧接着的 text 收尾轮次必须能注入。"""
        wire_tool = _anthropic_wire(final_block="tool_use")
        key = _warn_key("tok-tool-1")
        out = b"".join(_run_stream([wire_tool], key))
        self.assertEqual(out, wire_tool)
        wire_text = _anthropic_wire()
        out2 = b"".join(_run_stream([wire_text], _warn_key("tok-tool-1")))
        self.assertEqual(out2.count("今日额度已用".encode()), 1)

    def test_no_marker_passthrough_no_loss(self):
        wire = b"data: {\"foo\": 1}\n\ndata: [DONE]\n\n"
        out = b"".join(_run_stream([wire[:9], wire[9:]], _warn_key("tok-a-2")))
        self.assertEqual(out, wire)
        self.assertNotIn("今日额度已用".encode(), out)


class NonIterableGuardTest(unittest.TestCase):
    """responses mock 返回完整对象混进流式 hook 链时,守卫必须转成事件流。"""

    def test_wraps_plain_object_into_events(self):
        class ResponsesAPIResponse:
            pass

        out = _run_stream_raw(ResponsesAPIResponse(), _Key(token="tok-g-1"))
        self.assertEqual(out, ["EV1:ResponsesAPIResponse", "EV2:done"])


def _run_stream_raw(response_obj, key):
    async def go():
        out = []
        gen = M.budget_notice.async_post_call_streaming_iterator_hook(
            key, response_obj, {})
        async for x in gen:
            out.append(x)
        return out
    return _with_env(PREFIX_ENV, lambda: asyncio.run(go()))


class UsageTextTest(unittest.TestCase):
    def test_reset_time_is_beijing(self):
        key = _Key(reset_at=datetime.datetime(2026, 8, 19, 16, 0))
        self.assertIn("北京时间 08-20 00:00", M._usage_text(key))

    def test_percent_and_remain(self):
        key = _Key(spend=65.0, max_budget=70.0)
        t = M._usage_text(key)
        self.assertIn("93%", t)
        self.assertIn("$5.00", t)


if __name__ == "__main__":
    unittest.main(verbosity=2)
