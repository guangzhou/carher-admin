"""test_observability_attempt_pusher.py — attempt 事件里的兜底识别（observability_attempt_pusher.py）。

钉住 2026-11-01 生产实测的两个丢兜底根因：
1. 只从成功响应的 hidden header 读 ``x-litellm-attempted-fallbacks``：anthropic_messages 成功事件里
   header 是 None（1.106.4 pod 内实测），失败的兜底跳也永远没有 header。
   ⇒ 先读 ``litellm_params.metadata`` / ``litellm_metadata`` 的 ``attempted_fallbacks``（Router 每跳都写）。
2. 兜底跳和主腿共用同一个 ``litellm_call_id``、``attempted_retries`` 从 0 重数 ⇒ 旧 eventId
   ``<backend>:<call>:0`` 撞车，看板 ``ON CONFLICT DO NOTHING`` 丢掉后到的兜底成功事件
   （生产 81/81 兜底请求在 243 只剩主腿 failure）。

kwargs 形状照抄 1.106.4 gray pod 里真 Router（主组 127.0.0.1:9 死、兜底组 mock）抓到的值，
见 .work/myai-billing-plan/39-pusher-fb/probe_fb_kwargs.py。

用法::

    cd k8s/litellm-callbacks/tests
    .venv/bin/python -m unittest test_observability_attempt_pusher -v
"""
import asyncio
import importlib.util
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _install_litellm_stubs():
    litellm = types.ModuleType("litellm")
    integrations = types.ModuleType("litellm.integrations")
    custom_logger = types.ModuleType("litellm.integrations.custom_logger")

    class CustomLogger:  # noqa: D401 - 最小桩
        def __init__(self, **kwargs):
            pass

    custom_logger.CustomLogger = CustomLogger
    integrations.custom_logger = custom_logger
    litellm.integrations = integrations
    sys.modules.setdefault("litellm", litellm)
    sys.modules.setdefault("litellm.integrations", integrations)
    sys.modules.setdefault("litellm.integrations.custom_logger", custom_logger)


def _load():
    _install_litellm_stubs()
    os.environ["OBSERVABILITY_INGEST_HMAC_SECRET"] = "unit-test-only"
    os.environ["OBSERVABILITY_BACKEND_ID"] = "primary"
    os.environ["OBSERVABILITY_INGEST_URL"] = "http://127.0.0.1:9/events"
    spec = importlib.util.spec_from_file_location(
        "observability_attempt_pusher", os.path.join(ROOT, "observability_attempt_pusher.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load()


class _Resp:
    def __init__(self, header=None):
        self._hidden_params = {"additional_headers": {}}
        if header is not None:
            self._hidden_params["additional_headers"]["x-litellm-attempted-fallbacks"] = header


def _kwargs(*, call_id="call-1", model_group="grpA", attempted_fallbacks=0, original="grpA",
            bucket="metadata", attempted_retries=0, model="dead"):
    md = {"model_group": model_group, "attempted_retries": attempted_retries}
    if attempted_fallbacks is not None:
        md["attempted_fallbacks"] = attempted_fallbacks
        md["original_model_group"] = original
    lp = {"model": None, "custom_llm_provider": "openai", "metadata": {"model_group": model_group}}
    if bucket == "metadata":
        lp["metadata"] = md
    else:  # responses / anthropic：两个桶都有，litellm_metadata 更全
        lp["metadata"] = dict(md)
        lp["litellm_metadata"] = md
    return {"model": model, "litellm_call_id": call_id, "litellm_params": lp}


def _pusher():
    p = MOD.ObservabilityAttemptPusher()
    assert p._enabled
    return p


def _build(p, kwargs, resp=None, status="success"):
    exc = RuntimeError("boom") if status == "failure" else None
    return p._build_attempt(kwargs, resp, 1.0, 2.0, status=status, exception=exc)


class FallbackDetection(unittest.TestCase):
    def setUp(self):
        self.p = _pusher()

    def test_chat_fallback_success_from_metadata(self):
        ev = _build(self.p, _kwargs(model_group="grpB", attempted_fallbacks=1, original="grpA"), _Resp(1))
        self.assertTrue(ev["isFallback"])
        self.assertEqual(ev["fallbackFrom"], "grpA")
        self.assertEqual(ev["fallbackTo"], "grpB")
        self.assertEqual(ev["requestedModelGroup"], "grpB")

    def test_anthropic_success_has_no_header_but_metadata_wins(self):
        # 1.106.4 实测：aanthropic_messages 兜底成功事件 hidden header = None
        ev = _build(self.p, _kwargs(model_group="grpB", attempted_fallbacks=1, original="grpA",
                                    bucket="litellm_metadata"), _Resp(None))
        self.assertTrue(ev["isFallback"])
        self.assertEqual((ev["fallbackFrom"], ev["fallbackTo"]), ("grpA", "grpB"))

    def test_old_reader_misses_anthropic_positive_control(self):
        # 阳性对照：只读 header（旧实现）在同一份输入上读不到 ⇒ 证明上一条测的是新增路径
        self.assertIsNone(MOD.ObservabilityAttemptPusher._read_fallback_header(_Resp(None)))

    def test_failed_fallback_hop_is_marked(self):
        ev = _build(self.p, _kwargs(model_group="grpC", attempted_fallbacks=2, original="grpA"), None, "failure")
        self.assertTrue(ev["isFallback"])
        self.assertEqual(ev["status"], "failure")
        self.assertEqual((ev["fallbackFrom"], ev["fallbackTo"]), ("grpA", "grpC"))

    def test_primary_leg_not_fallback(self):
        for status in ("success", "failure"):
            ev = _build(self.p, _kwargs(attempted_fallbacks=0), _Resp(0), status)
            self.assertFalse(ev["isFallback"], status)
            self.assertNotIn("fallbackFrom", ev)
            self.assertNotIn("fallbackTo", ev)

    def test_header_fallback_when_metadata_absent(self):
        ev = _build(self.p, _kwargs(model_group="grpB", attempted_fallbacks=None), _Resp(1))
        self.assertTrue(ev["isFallback"])
        self.assertEqual(ev["fallbackFrom"], "unknown")
        self.assertEqual(ev["fallbackTo"], "grpB")

    def test_neither_source_means_not_fallback(self):
        ev = _build(self.p, _kwargs(attempted_fallbacks=None), _Resp(None))
        self.assertFalse(ev["isFallback"])

    def test_header_ignored_on_failure_without_metadata(self):
        ev = _build(self.p, _kwargs(attempted_fallbacks=None), _Resp(3), "failure")
        self.assertFalse(ev["isFallback"])

    def test_malformed_metadata_does_not_raise(self):
        kw = {"litellm_call_id": "c", "litellm_params": {"metadata": "not-a-dict", "litellm_metadata": 5}}
        ev = _build(self.p, kw, _Resp(None))
        self.assertFalse(ev["isFallback"])
        self.assertEqual(MOD.ObservabilityAttemptPusher._read_fallback_metadata(None), (None, ""))


class EventIdUniqueness(unittest.TestCase):
    def setUp(self):
        self.p = _pusher()

    def test_primary_event_id_format_unchanged(self):
        ev = _build(self.p, _kwargs(call_id="abc", attempted_fallbacks=0), None, "failure")
        self.assertEqual(ev["eventId"], "primary:abc:0")

    def test_fallback_hops_do_not_collide_with_primary(self):
        # 同一 call_id：主腿失败 → 兜底 1 失败 → 兜底 2 成功（生产形状，attempted_retries 都是 0）
        ids = [
            _build(self.p, _kwargs(call_id="abc", attempted_fallbacks=0), None, "failure")["eventId"],
            _build(self.p, _kwargs(call_id="abc", model_group="grpB", attempted_fallbacks=1), None, "failure")["eventId"],
            _build(self.p, _kwargs(call_id="abc", model_group="grpC", attempted_fallbacks=2), _Resp(2))["eventId"],
        ]
        self.assertEqual(len(set(ids)), 3, ids)
        self.assertEqual(ids[2], "primary:abc:fb2:grpC:0")

    def test_fallback_hop_has_own_attempt_id_but_shares_trace(self):
        # 看板按 (trace, attemptId) 去重：兜底跳必须是独立 attempt，同时留在同一个 trace 里
        a = _build(self.p, _kwargs(call_id="abc", attempted_fallbacks=0), None, "failure")
        b = _build(self.p, _kwargs(call_id="abc", model_group="grpB", attempted_fallbacks=1), _Resp(1))
        self.assertNotEqual(a["attemptId"], b["attemptId"])
        self.assertEqual(a["traceId"], b["traceId"])
        self.assertEqual(a["requestId"], b["requestId"])
        self.assertEqual(a["attemptId"], "abc")

    def test_event_id_bounded(self):
        ev = _build(self.p, _kwargs(call_id="x" * 300, model_group="g" * 300, attempted_fallbacks=1), _Resp(1))
        self.assertLessEqual(len(ev["eventId"]), 256)


class Whitelist(unittest.TestCase):
    def test_only_allowed_fields(self):
        p = _pusher()
        ev = _build(p, _kwargs(model_group="grpB", attempted_fallbacks=1), _Resp(1))
        self.assertLessEqual(set(ev), MOD._ALLOWED_FIELDS)

    def test_async_hook_enqueues(self):
        p = _pusher()

        async def run():
            await p.async_log_success_event(_kwargs(model_group="grpB", attempted_fallbacks=1), _Resp(None), 1.0, 2.0)
            if p._flush_task:
                p._flush_task.cancel()
            return p._queue.get_nowait()

        ev = asyncio.run(run())
        self.assertTrue(ev["isFallback"])


if __name__ == "__main__":
    unittest.main()
