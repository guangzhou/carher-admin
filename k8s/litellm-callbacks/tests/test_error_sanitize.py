"""test_error_sanitize.py — 对外报错脱敏 + 路由头假名化。

钉住 2026-08-08 那四条实测泄漏不再出网：
  1) RateLimitError.message 里的 cooldown_list=['chatgpt-acct-99-...']
  2) APIError.message 里的 api_base='http://chatgpt-acct-95....svc.cluster.local'
  3) 流式 SSE error 帧里的 str(e) + traceback（暴露 /app/*.py）
  4) 响应头 x-litellm-model-id / x-litellm-model-api-base

同时钉住三条**不能被脱敏顺手打掉**的东西：``status_code`` / ``type`` / ``code``
原样保留（客户端 SDK 重试语义 + 生产回归脚本 hidden_access_denied 靠 type），
以及 x-litellm-model-id 的假名必须"同 deployment 同值、不同则不同" —— 换成常量
占位符会让 probe-affinity.py 的 ``mid1 == mid2`` 恒真，把坏掉的 sticky router
报成 PASS。

用法::

    cd k8s/litellm-callbacks/tests
    .venv/bin/python -m unittest test_error_sanitize -v
"""
import asyncio
import json
import importlib.util
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))

# 线上实测的原始泄漏文本，逐字抄下来当输入。
LEAK_COOLDOWN = (
    "litellm.RateLimitError: No deployments available for selected model, "
    "Try again in 60 seconds. Passed model=chatgpt-gpt-5.6-terra. "
    "pre-call-checks=True, cooldown_list=['chatgpt-acct-99-gpt-5.6-terra', "
    "'chatgpt-acct-95-gpt-5.6-sol', 'chatgpt-acct-97-gpt-5.6-sol']"
)
LEAK_API_BASE = (
    "litellm.APIError: APIError: OpenAIException - "
    "api_base='http://chatgpt-acct-95.litellm-product.svc.cluster.local:4000'"
)
LEAK_TRACEBACK = (
    "Expecting value: line 1 column 3 (char 2)\n\n"
    'Traceback (most recent call last):\n'
    '  File "/app/streaming_output_backfill.py", line 188, in _escalate\n'
    "    raise RateLimitError(...)\n"
)
LEAK_KEY_HASH = (
    "Authentication Error, Invalid proxy server token passed. "
    "Received API Key = sk-...-key, Key Hash (Token) ="
    "d95ec1c8f99fcfecf40695d8b977fc0dbe937a0fa5d22ca9f1080911ba385877. "
    "Unable to find token in cache or `LiteLLM_VerificationTokenTable`"
)

# 任何一条出现在对外文案里就算漏。
FORBIDDEN = (
    "chatgpt-acct", "acct-", "svc.cluster.local", "litellm-product",
    "cooldown_list", "api_base", "Traceback", "/app/", "zerokey", "wangsu",
    "d95ec1c8f99fcfecf", "sk-",
)

REAL_MODEL_IDS = (
    "chatgpt-acct-109-gpt-5.6-luna",
    "chatgpt-acct-109-gpt-5.6-terra",
    "chatgpt-acct-121-gpt-5.6-luna",
    "deepseek-official/deepseek-v4-flash",
)


def _stub_litellm():
    if "litellm" in sys.modules:
        return
    litellm = types.ModuleType("litellm")
    integrations = types.ModuleType("litellm.integrations")
    custom_logger = types.ModuleType("litellm.integrations.custom_logger")

    class CustomLogger:  # noqa: D401 - stub
        pass

    custom_logger.CustomLogger = CustomLogger
    integrations.custom_logger = custom_logger
    litellm.integrations = integrations
    sys.modules["litellm"] = litellm
    sys.modules["litellm.integrations"] = integrations
    sys.modules["litellm.integrations.custom_logger"] = custom_logger

    # get_error_information 的补丁靶子。真实签名见 198 pod 内
    # litellm_logging.py:5391（staticmethod，kwargs 调用）。
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

    # get_custom_headers 的补丁靶子（198 上是
    # proxy/common_request_processing.py:734 的 staticmethod，返回纯 dict）。
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
    """每次重新 exec 模块，好让测试换 env 后重新走 import 期的 gate。"""
    _stub_litellm()
    path = os.path.join(HERE, "..", "error_sanitize.py")
    spec = importlib.util.spec_from_file_location("errsan", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


M = _load()

CALL_ID = "21a8dda7-7bb4-4ff0-98e3-cfdd77715e3b"
REQ = {"litellm_call_id": CALL_ID, "model": "gpt-5.5"}


# --- 被测的四种异常形状。照抄线上会走到的类形态，不引真 litellm。 ---

class FakeProxyException(Exception):
    """litellm ProxyException 的形状：message/type/param/code + psf。"""

    def __init__(self, message, type="rate_limit_error", param="None", code=429,
                 provider_specific_fields=None):
        self.message = str(message)
        super().__init__(self.message)
        self.type = type
        self.param = param
        self.code = str(code)
        self.status_code = code
        self.provider_specific_fields = provider_specific_fields


class FakeHTTPException(Exception):
    """fastapi HTTPException 的形状：status_code + detail，没有 .message。"""

    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"{status_code}: {detail}")


class FakeRateLimitError(Exception):
    """litellm.RateLimitError 的形状：message + status_code，__str__ 走 args。"""

    def __init__(self, message, status_code=429):
        self.message = message
        self.status_code = status_code
        self.type = "rate_limit_error"
        super().__init__(message)


def _texts(exc):
    """下游可能拿去当对外文案的所有口子。"""
    out = [str(exc), repr(getattr(exc, "args", ()))]
    for attr in ("message", "detail", "provider_specific_fields"):
        out.append(repr(getattr(exc, attr, None)))
    return "\n".join(out)


def _run_failure(exc, mod=M, request_data=REQ):
    return asyncio.run(mod.error_sanitize.async_post_call_failure_hook(
        request_data=request_data, original_exception=exc,
        user_api_key_dict=None, traceback_str=None))


class MessageMaskedTest(unittest.TestCase):
    """四种异常形状，脱敏后所有对外口子都不含泄漏词。"""

    def _assert_clean(self, exc):
        blob = _texts(exc)
        for needle in FORBIDDEN:
            self.assertNotIn(needle, blob,
                             f"{needle!r} 仍在对外文案里: {blob[:400]}")
        self.assertIn("API 异常", blob)
        self.assertIn(CALL_ID[:8], blob, "关联 id 应出现在对外文案里")

    def test_proxy_exception_cooldown_list(self):
        e = FakeProxyException(LEAK_COOLDOWN,
                               provider_specific_fields={"error": LEAK_COOLDOWN})
        self.assertIsNone(_run_failure(e))
        self._assert_clean(e)

    def test_ratelimit_error_api_base(self):
        e = FakeRateLimitError(LEAK_API_BASE)
        _run_failure(e)
        self._assert_clean(e)

    def test_http_exception_key_hash(self):
        e = FakeHTTPException(401, LEAK_KEY_HASH)
        _run_failure(e)
        self._assert_clean(e)

    def test_bare_exception_traceback(self):
        """流式兜底分支用 str(e)+traceback，所以 .args 必须被换掉。"""
        e = Exception(LEAK_TRACEBACK)
        _run_failure(e)
        self._assert_clean(e)
        self.assertNotIn("Traceback", str(e))

    def test_request_id_absent_does_not_fabricate(self):
        e = FakeRateLimitError(LEAK_API_BASE)
        _run_failure(e, request_data={})
        self.assertIn("API 异常", str(e))
        self.assertIn("(req: -)", str(e), "取不到 call id 时给 '-'，不编造")


class PreservedFieldsTest(unittest.TestCase):
    """脱敏不能顺手打掉客户端 SDK 和生产回归脚本依赖的字段。"""

    def test_status_code_and_code_preserved(self):
        e = FakeProxyException(LEAK_COOLDOWN, code=429)
        _run_failure(e)
        self.assertEqual(e.status_code, 429)
        self.assertEqual(e.code, "429")

    def test_http_exception_status_preserved(self):
        e = FakeHTTPException(401, LEAK_KEY_HASH)
        _run_failure(e)
        self.assertEqual(e.status_code, 401)

    def test_clean_type_preserved(self):
        """rate_limit_error 不含敏感词 → 必须原样留着（429 可重试的语义）。"""
        e = FakeProxyException(LEAK_COOLDOWN, type="rate_limit_error")
        _run_failure(e)
        self.assertEqual(e.type, "rate_limit_error")

    def test_key_model_access_denied_type_preserved(self):
        """litellm-pro-gpt-products-regression.sh 的判据靠这个 type 字符串。"""
        e = FakeProxyException("allowlist: gpt-5.5, gpt-5.4",
                               type="key_model_access_denied", code=401)
        _run_failure(e)
        self.assertEqual(e.type, "key_model_access_denied")

    def test_dirty_type_scrubbed(self):
        """万一 provider 把机器名塞进 type，兜底清洗要接住。"""
        e = FakeProxyException(LEAK_COOLDOWN, type="chatgpt-acct-97 unavailable")
        _run_failure(e)
        self.assertEqual(e.type, "api_error")
        self.assertNotIn("chatgpt-acct", e.type)


class KillSwitchTest(unittest.TestCase):
    """ERROR_SANITIZE_DISABLED=1 必须完全不改写 —— 一键回退的落点。"""

    def setUp(self):
        self._old = os.environ.get("ERROR_SANITIZE_DISABLED")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("ERROR_SANITIZE_DISABLED", None)
        else:
            os.environ["ERROR_SANITIZE_DISABLED"] = self._old

    def test_disabled_leaves_exception_untouched(self):
        os.environ["ERROR_SANITIZE_DISABLED"] = "1"
        e = FakeRateLimitError(LEAK_COOLDOWN)
        _run_failure(e)
        self.assertIn("chatgpt-acct-99", e.message)
        self.assertEqual(str(e), LEAK_COOLDOWN)

    def test_disabled_leaves_headers_untouched(self):
        os.environ["ERROR_SANITIZE_DISABLED"] = "1"
        got = asyncio.run(M.error_sanitize.async_post_call_response_headers_hook(
            data={}, litellm_call_info={"model_id": REAL_MODEL_IDS[0]}))
        self.assertIsNone(got)

    def test_headers_only_kill_switch(self):
        os.environ["ERROR_SANITIZE_HEADERS_DISABLED"] = "1"
        try:
            got = asyncio.run(M.error_sanitize.async_post_call_response_headers_hook(
                data={}, litellm_call_info={"model_id": REAL_MODEL_IDS[0]}))
            self.assertIsNone(got)
            # 报错脱敏仍应生效
            e = FakeRateLimitError(LEAK_COOLDOWN)
            _run_failure(e)
            self.assertNotIn("chatgpt-acct", e.message)
        finally:
            os.environ.pop("ERROR_SANITIZE_HEADERS_DISABLED", None)


class HeaderPseudonymTest(unittest.TestCase):
    def _hdrs(self, model_id, data=None):
        return asyncio.run(M.error_sanitize.async_post_call_response_headers_hook(
            data=data if data is not None else {},
            litellm_call_info={"model_id": model_id}))

    def test_model_id_is_pseudonymised(self):
        h = self._hdrs(REAL_MODEL_IDS[0])
        v = h["x-litellm-model-id"]
        for needle in ("acct", "chatgpt", "gpt-5", "luna"):
            self.assertNotIn(needle, v)
        self.assertEqual(len(v), 12)

    def test_api_base_replaced(self):
        h = self._hdrs(REAL_MODEL_IDS[0])
        self.assertEqual(h["x-litellm-model-api-base"], "-")

    def test_stable_across_calls(self):
        """probe-affinity.py / litellm-sticky-verify.sh 的 mid1==mid2 判据。"""
        a = self._hdrs(REAL_MODEL_IDS[0])["x-litellm-model-id"]
        b = self._hdrs(REAL_MODEL_IDS[0])["x-litellm-model-id"]
        self.assertEqual(a, b)

    def test_distinct_deployments_stay_distinct(self):
        """常量占位符会让这条失败 —— 那正是它不能用常量的原因。"""
        vals = {self._hdrs(m)["x-litellm-model-id"] for m in REAL_MODEL_IDS}
        self.assertEqual(len(vals), len(REAL_MODEL_IDS))

    def test_missing_model_id_falls_back(self):
        h = self._hdrs(None)
        self.assertEqual(h["x-litellm-model-id"], "-")

    def test_model_id_from_metadata_fallback(self):
        """旧签名 backwards-compat 分支：litellm_call_info 缺失时从 data 里捞。"""
        got = asyncio.run(M.error_sanitize.async_post_call_response_headers_hook(
            data={"metadata": {"model_info": {"id": REAL_MODEL_IDS[0]}}},
            litellm_call_info=None))
        self.assertEqual(got["x-litellm-model-id"],
                         M.pseudonym(REAL_MODEL_IDS[0]))


class NeverRaisesTest(unittest.TestCase):
    """hook 自己炸掉也不能影响请求 —— utils.py 会吞掉非 HTTPException，
    但我们不指望它，本模块自己兜住。"""

    def test_slotted_exception_survives(self):
        class Slotted(Exception):
            __slots__ = ()

        e = Slotted(LEAK_COOLDOWN)
        self.assertIsNone(_run_failure(e))

    def test_setattr_blocking_exception_survives(self):
        class Hostile(Exception):
            def __setattr__(self, k, v):
                raise RuntimeError("nope")

        self.assertIsNone(_run_failure(Hostile("boom")))

    def test_non_dict_request_data_survives(self):
        e = FakeRateLimitError(LEAK_COOLDOWN)
        self.assertIsNone(_run_failure(e, request_data="not-a-dict"))
        self.assertNotIn("chatgpt-acct", e.message)

    def test_headers_hook_with_garbage_call_info(self):
        got = asyncio.run(M.error_sanitize.async_post_call_response_headers_hook(
            data="not-a-dict", litellm_call_info="not-a-dict"))
        self.assertEqual(got["x-litellm-model-id"], "-")


class OriginalPreservedTest(unittest.TestCase):
    """出网那份被换掉之后，入库那份必须仍是原文。

    2026-08-08 实测：SpendLogs 的 error_information.error_message 是在本 hook
    之后才序列化的，所以就地改写会把 DB 那一列也抹掉。改前那条
    ``No api key passed in.`` 和改后整片 ``API 异常`` 就是对照。
    """

    def _errinfo(self, exc):
        from litellm.litellm_core_utils.litellm_logging import (
            StandardLoggingPayloadSetup as S,
        )
        return S.get_error_information(original_exception=exc)

    def test_db_payload_keeps_original_message(self):
        e = FakeRateLimitError(LEAK_COOLDOWN)
        _run_failure(e)
        # 出网：脱敏
        self.assertNotIn("chatgpt-acct", e.message)
        # 入库：原文
        self.assertIn("chatgpt-acct-99", self._errinfo(e)["error_message"])

    def test_db_payload_keeps_class_and_code(self):
        e = FakeRateLimitError(LEAK_COOLDOWN, status_code=429)
        _run_failure(e)
        info = self._errinfo(e)
        self.assertEqual(info["error_class"], "FakeRateLimitError")
        self.assertEqual(info["error_code"], "429")

    def test_unstashed_exception_untouched_by_patch(self):
        """没经过本 hook 的异常，补丁不能改它的 error_message。"""
        e = FakeRateLimitError("some unrelated failure")
        self.assertEqual(self._errinfo(e)["error_message"],
                         "some unrelated failure")

    def test_patch_is_idempotent(self):
        self.assertTrue(M._install_error_information_patch())
        self.assertTrue(M._install_error_information_patch())
        e = FakeRateLimitError(LEAK_COOLDOWN)
        _run_failure(e)
        self.assertIn("chatgpt-acct-99", self._errinfo(e)["error_message"])

    def test_stash_sets_attribute(self):
        e = FakeRateLimitError(LEAK_COOLDOWN)
        self.assertTrue(M.stash_original(e, "real text"))
        self.assertEqual(getattr(e, "_error_sanitize_original"), "real text")

    def test_stash_noop_on_empty(self):
        e = FakeRateLimitError(LEAK_COOLDOWN)
        self.assertFalse(M.stash_original(e, ""))

    def test_original_text_picks_longest_source(self):
        e = FakeHTTPException(401, LEAK_KEY_HASH)
        self.assertIn("d95ec1c8f99fcfecf", M.original_text(e))


class HeaderDropTest(unittest.TestCase):
    """整类删掉泄漏拓扑的响应头。

    输入照抄 2026-08-08 从 198 上一次池子流量（chatgpt-gpt-5.5）实测到的出网头。
    """

    LIVE = {
        # 上游 ChatGPT 账号的订阅档位 + 配额消耗 + 重置时刻
        "llm_provider-x-codex-plan-type": "pro",
        "llm_provider-x-codex-active-limit": "premium",
        "llm_provider-x-codex-primary-used-percent": "9",
        "llm_provider-x-codex-primary-reset-at": "1786767383",
        "llm_provider-x-codex-credits-balance": "0",
        # OpenAI edge-gateway 会话 cookie（JWT）
        "llm_provider-set-cookie": "__oailb=eyJhbGciOiJFUzI1NiIsImtpZCI6Im9haWxiLXYxIn0.x.y",
        # 内层 LiteLLM 自己的头被加前缀转发 —— 绕过 x-litellm-model-id 的假名化
        "llm_provider-x-litellm-model-api-base": "https://chatgpt.com/backend-api/codex",
        "llm_provider-x-litellm-model-group": "chatgpt-gpt-5.5",
        "llm_provider-x-litellm-key-spend": "0.0",
        "llm_provider-cf-ray": "a27fc5a2b9a5d3e6-KIX",
        # 花费 / 利润率 / 预算 / 限额 / 版本
        "x-litellm-response-cost": "1.33e-05",
        "x-litellm-response-cost-margin-percent": "0.0",
        "x-litellm-response-cost-margin-amount": "0.0",
        "x-litellm-key-spend": "1.33e-05",
        "x-litellm-key-max-budget": "1000.0",
        "x-litellm-key-tpm-limit": "None",
        "x-litellm-cache-key": "abc123",
        "x-litellm-version": "1.90.2",
        "x-litellm-model-region": "",
        # 要假名化的
        "x-litellm-model-id": "chatgpt-acct-109-gpt-5.6-luna",
        "x-litellm-model-group": "chatgpt-gpt-5.5",
        "x-litellm-model-api-base": "http://chatgpt-acct-109.litellm-product.svc.cluster.local:4000",
        # 必须原样保留的
        "x-litellm-call-id": CALL_ID,
        "x-litellm-attempted-fallbacks": "0",
        "x-litellm-attempted-retries": "0",
        "x-litellm-response-duration-ms": "842.769",
        "x-ratelimit-remaining-requests": "99",
        "x-ratelimit-reset-tokens": "60s",
        "retry-after": "60",
    }

    def setUp(self):
        self.out = M.sanitize_header_map(dict(self.LIVE))

    def test_no_llm_provider_header_survives(self):
        leaked = [k for k in self.out if k.startswith("llm_provider-")]
        self.assertEqual(leaked, [], f"llm_provider-* 没删干净: {leaked}")

    def test_account_quota_state_gone(self):
        blob = json.dumps(self.out)
        for needle in ("codex-plan-type", "premium", "1786767383", "__oailb",
                       "backend-api/codex", "cf-ray"):
            self.assertNotIn(needle, blob, f"{needle!r} 仍在出网头里")

    def test_cost_and_margin_gone(self):
        for k in ("x-litellm-response-cost", "x-litellm-response-cost-margin-percent",
                  "x-litellm-response-cost-margin-amount", "x-litellm-key-spend",
                  "x-litellm-key-max-budget", "x-litellm-key-tpm-limit",
                  "x-litellm-cache-key", "x-litellm-version",
                  "x-litellm-model-region"):
            self.assertNotIn(k, self.out, f"{k} 应被删掉")

    def test_model_id_and_group_pseudonymised(self):
        self.assertEqual(self.out["x-litellm-model-id"],
                         M.pseudonym("chatgpt-acct-109-gpt-5.6-luna"))
        self.assertEqual(self.out["x-litellm-model-group"],
                         M.pseudonym("chatgpt-gpt-5.5"))
        blob = json.dumps(self.out)
        for needle in ("acct", "chatgpt", "zerokey", "svc.cluster.local"):
            self.assertNotIn(needle, blob)

    def test_api_base_placeholder(self):
        self.assertEqual(self.out["x-litellm-model-api-base"], "-")

    def test_keeps_what_ops_and_sdks_need(self):
        """删过头会打断 quota-rebalance 的暂停判断和客户端 SDK 退避。"""
        self.assertEqual(self.out["x-litellm-call-id"], CALL_ID)
        for k in ("x-litellm-attempted-fallbacks", "x-litellm-attempted-retries",
                  "x-litellm-response-duration-ms", "retry-after",
                  "x-ratelimit-remaining-requests", "x-ratelimit-reset-tokens"):
            self.assertIn(k, self.out, f"{k} 不该被删")

    def test_case_insensitive(self):
        out = M.sanitize_header_map({"LLM_Provider-Set-Cookie": "x",
                                     "X-LiteLLM-Key-Spend": "1.0"})
        self.assertEqual(out, {})

    def test_non_dict_passthrough(self):
        self.assertEqual(M.sanitize_header_map("nope"), "nope")

    def test_patch_applied_and_idempotent(self):
        from litellm.proxy.common_request_processing import (
            ProxyBaseLLMRequestProcessing as P,
        )
        self.assertTrue(M._install_custom_headers_patch())
        self.assertTrue(M._install_custom_headers_patch())
        got = P.get_custom_headers(_headers=dict(self.LIVE))
        self.assertNotIn("llm_provider-set-cookie", got)
        self.assertNotIn("x-litellm-key-spend", got)
        self.assertIn("x-litellm-call-id", got)

    def test_kill_switch_restores_raw_headers(self):
        from litellm.proxy.common_request_processing import (
            ProxyBaseLLMRequestProcessing as P,
        )
        M._install_custom_headers_patch()
        os.environ["ERROR_SANITIZE_DISABLED"] = "1"
        try:
            got = P.get_custom_headers(_headers=dict(self.LIVE))
            self.assertIn("llm_provider-set-cookie", got)
        finally:
            os.environ.pop("ERROR_SANITIZE_DISABLED", None)


class LeakRegexTest(unittest.TestCase):
    def test_detects_all_measured_leaks(self):
        for text in (LEAK_COOLDOWN, LEAK_API_BASE, LEAK_KEY_HASH):
            self.assertTrue(M.contains_leak(text), text[:60])

    def test_does_not_flag_clean_openai_enums(self):
        for t in ("invalid_request_error", "authentication_error",
                  "rate_limit_error", "key_model_access_denied",
                  "budget_exceeded", "token_not_found_in_db", "None"):
            self.assertFalse(M.contains_leak(t), t)



# --- budget_notice 三件套之③：BudgetExceededError 友好文案 + ModifyResponseException 豁免 ---

class BudgetExceededError(Exception):
    """litellm.BudgetExceededError 形状（error_sanitize 按 __name__ 识别）。"""

    def __init__(self, current_cost, max_budget, message=None):
        self.current_cost = current_cost
        self.max_budget = max_budget
        self.status_code = 429
        self.message = message or (
            f"Budget has been exceeded! Current cost: {current_cost}, "
            f"Max budget: {max_budget}")
        super().__init__(self.message)


class ModifyResponseException(Exception):
    """litellm.ModifyResponseException 形状：message 是要展示给用户的正文。"""

    def __init__(self, message):
        self.message = message
        super().__init__(message)


class _KeyDict:
    def __init__(self, reset_at=None):
        self.budget_reset_at = reset_at


class BudgetFriendly429Test(unittest.TestCase):
    def _run(self, exc, key=None):
        return asyncio.run(M.error_sanitize.async_post_call_failure_hook(
            request_data=REQ, original_exception=exc,
            user_api_key_dict=key, traceback_str=None))

    def test_budget_exceeded_message_is_friendly(self):
        import datetime as dt
        exc = BudgetExceededError(75.0, 70.0)
        self._run(exc, key=_KeyDict(dt.datetime(2026, 8, 19, 16, 0)))
        for want in ("额度已用完", "$75.00", "$70.00", "/查余额",
                     "北京时间 08-20 00:00", CALL_ID[:8]):
            self.assertIn(want, exc.message, exc.message)
        # 四属性口径一致（args 决定 str(e)，auth 兜底分支走它）
        self.assertIn("额度已用完", str(exc))
        self.assertEqual(exc.status_code, 429)

    def test_budget_exceeded_without_reset_at(self):
        exc = BudgetExceededError(75.0, 70.0)
        self._run(exc, key=None)
        self.assertIn("额度已用完", exc.message)
        self.assertIn("额度周期结束后", exc.message)

    def test_budget_friendly_kill_switch(self):
        os.environ["BUDGET_FRIENDLY_429_DISABLED"] = "1"
        try:
            exc = BudgetExceededError(75.0, 70.0)
            self._run(exc)
            self.assertNotIn("额度已用完", exc.message)
            self.assertIn("API 异常", exc.message)
        finally:
            os.environ.pop("BUDGET_FRIENDLY_429_DISABLED", None)

    def test_modify_response_exception_untouched(self):
        msg = "📊 claude-code-x 今日用量\n已用 $1.00 / 限额 $70.00（1%）"
        exc = ModifyResponseException(msg)
        got = self._run(exc)
        self.assertIsNone(got)
        self.assertEqual(exc.message, msg)

    def test_modify_response_exception_with_leak_still_masked(self):
        exc = ModifyResponseException(
            "violation from chatgpt-acct-99.litellm-product.svc.cluster.local")
        self._run(exc)
        self.assertNotIn("chatgpt-acct", exc.message)
        self.assertIn("API 异常", exc.message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
