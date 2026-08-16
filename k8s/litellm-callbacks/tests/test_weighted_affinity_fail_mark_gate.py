"""test_weighted_affinity_fail_mark_gate.py — fail-mark 判定的请求级失败排除。

背景（2026-08-15 生产）
----------------------
gpt 长会话兜底落 deepseek 时，超大 body 被 api.deepseek.com 边缘 openresty
拒收（413 HTML），litellm 包成 ``litellm.APIError`` —— 命中 v4 fail-mark 的
TRANSIENT "APIError" 关键词，把 deepseek deployment 误标 180s，殃及其他
用户的兜底（48h 内多次 ``fail-marked deployment=deepseek-official/...``
与 413 同刻）。修法：NEVER_MARK 增加请求级失败短语（NEVER 先于 TRANSIENT
判定）。

钉住：
1. 413 HTML 包装成 APIError -> 不标记
2. ContextWindowExceeded（含/不含 BadRequestError 文本）-> 不标记
3. 纯传输类（Timeout / APIConnection / 5xx APIError）-> 仍标记（别把
   排除做宽把真故障也放过）

用法::

    python3 -m unittest test_weighted_affinity_fail_mark_gate -v
"""
import importlib.util
import os
import sys
import types
import unittest

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "weighted_affinity.py")


def _load_module():
    if "litellm._logging" not in sys.modules:
        litellm = sys.modules.get("litellm") or types.ModuleType("litellm")
        logging_mod = types.ModuleType("litellm._logging")

        class _L:
            def info(self, *a, **k): pass
            def warning(self, *a, **k): pass
            def debug(self, *a, **k): pass

        logging_mod.verbose_router_logger = _L()
        integrations = getattr(litellm, "integrations", None) or types.ModuleType("litellm.integrations")
        custom_logger = types.ModuleType("litellm.integrations.custom_logger")

        class CustomLogger:  # noqa: D401 - stub
            pass

        custom_logger.CustomLogger = CustomLogger
        custom_logger.Span = object
        integrations.custom_logger = custom_logger
        litellm.integrations = integrations
        litellm._logging = logging_mod
        types_mod = types.ModuleType("litellm.types")
        llms_mod = types.ModuleType("litellm.types.llms")
        openai_mod = types.ModuleType("litellm.types.llms.openai")
        openai_mod.AllMessageValues = dict
        llms_mod.openai = openai_mod
        types_mod.llms = llms_mod
        litellm.types = types_mod
        sys.modules.update({
            "litellm": litellm,
            "litellm._logging": logging_mod,
            "litellm.integrations": integrations,
            "litellm.integrations.custom_logger": custom_logger,
            "litellm.types": types_mod,
            "litellm.types.llms": llms_mod,
            "litellm.types.llms.openai": openai_mod,
        })
    spec = importlib.util.spec_from_file_location("wa_under_test", DEFAULT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load_module()
# 类名不锁死：取带 _failure_should_mark 的那个类实例化判定
_CLS = next(
    getattr(MOD, name) for name in dir(MOD)
    if isinstance(getattr(MOD, name), type) and hasattr(getattr(MOD, name), "_failure_should_mark")
)


class _APIError(Exception):
    pass


class _Timeout(Exception):
    pass


class _ContextWindowExceededError(Exception):
    pass


class FailMarkGateTest(unittest.TestCase):
    def _mark(self, exc):
        return _CLS._failure_should_mark(exc)

    def test_413_html_wrapped_as_apierror_not_marked(self):
        exc = _APIError(
            'APIError: OpenAIException - <html>\r\n<head><title>413 Request Entity '
            'Too Large</title></head>\r\n<body>openresty</body></html>')
        self.assertFalse(self._mark(exc))

    def test_context_window_exceeded_not_marked(self):
        self.assertFalse(self._mark(_ContextWindowExceededError(
            "litellm.BadRequestError: maximum context length is 1048576 tokens")))
        # 即使未来包装丢了 BadRequestError 文本，类名/短语仍然兜住
        self.assertFalse(self._mark(_ContextWindowExceededError("upstream rejected")))
        self.assertFalse(self._mark(_APIError(
            "This model's maximum context length is 1048576 tokens")))

    def test_real_transport_failures_still_marked(self):
        self.assertTrue(self._mark(_Timeout("Request timed out")))
        self.assertTrue(self._mark(_APIError("APIConnectionError: peer reset")))
        self.assertTrue(self._mark(_APIError("InternalServerError: 502 upstream")))

    def test_plain_4xx_still_not_marked(self):
        class BadRequestError(Exception):
            pass
        self.assertFalse(self._mark(BadRequestError("No tool output found")))


class QuotaCapMarkTTLTest(unittest.TestCase):
    """[2026-08-16] 配额撞顶 429 → 长 TTL 标记（到官方 reset），修 fallback 风暴。

    真实 body（acct-194 实测 2026-08-16）:
      {"error":{"type":"usage_limit_reached","message":"The usage limit has
       been reached","plan_type":"pro","resets_at":1787387541,
       "resets_in_seconds":534428}}
    """

    def setUp(self):
        self.inst = _CLS()

    def _ttl(self, exc):
        return self.inst._failure_mark_ttl(exc)

    def test_quota_cap_with_resets_uses_official_reset_plus_cushion(self):
        exc = _APIError(
            'RateLimitError: OpenAIException - {"error":{"type":"usage_limit_reached",'
            '"message":"The usage limit has been reached","plan_type":"pro",'
            '"resets_at":1787387541,"eligible_promo":null,"resets_in_seconds":534428}}')
        self.assertEqual(self._ttl(exc), (534428 + 60, "quota-cap"))

    def test_quota_cap_without_resets_falls_back_to_default(self):
        exc = _APIError('429 usage_limit_reached upstream said no')
        self.assertEqual(self._ttl(exc), (self.inst.quota_mark_ttl_default, "quota-cap"))

    def test_quota_cap_ttl_capped_at_7d(self):
        exc = _APIError(
            '{"error":{"type":"usage_limit_reached","resets_in_seconds":99999999}}')
        self.assertEqual(self._ttl(exc), (7 * 86400, "quota-cap"))

    def test_transient_keeps_short_ttl(self):
        self.assertEqual(self._ttl(_Timeout("Request timed out")),
                         (self.inst.fail_mark_ttl, "transient"))
        # 普通 TPM 抖动型 RateLimit（无 usage_limit_reached 签名）仍是短标记
        self.assertEqual(self._ttl(_APIError("RateLimitError: slow down")),
                         (self.inst.fail_mark_ttl, "transient"))

    def test_never_mark_still_none(self):
        self.assertIsNone(self._ttl(_APIError(
            "413 Request Entity Too Large openresty")))
        self.assertIsNone(self._ttl(None))


if __name__ == "__main__":
    unittest.main()
