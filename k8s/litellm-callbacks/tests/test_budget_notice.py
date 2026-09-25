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

    # ③ 软拦截：patch 层判 type(e).__name__ == "BudgetExceededError"，pre_call
    #   非可 mock 分支 raise litellm.BudgetExceededError(current_cost=, max_budget=)。
    if not hasattr(litellm, "BudgetExceededError"):

        class BudgetExceededError(Exception):
            def __init__(self, current_cost=None, max_budget=None, message="", **kw):
                self.current_cost = current_cost
                self.max_budget = max_budget
                self.message = message or "Budget has been exceeded"
                super().__init__(self.message)

        litellm.BudgetExceededError = BudgetExceededError
        if not hasattr(exc_mod, "BudgetExceededError"):
            exc_mod.BudgetExceededError = BudgetExceededError

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

    # codex 停机分支的桩:_codex_usage_limit_exc 抛 ProxyException(带 headers)。
    if "litellm.proxy._types" not in sys.modules:
        proxy_mod = sys.modules.get("litellm.proxy")
        if proxy_mod is None:
            proxy_mod = types.ModuleType("litellm.proxy")
            litellm.proxy = proxy_mod
            sys.modules["litellm.proxy"] = proxy_mod
        types_mod = types.ModuleType("litellm.proxy._types")

        class ProxyException(Exception):
            def __init__(self, message, type, param, code=None, headers=None,
                         openai_code=None, provider_specific_fields=None):
                self.message = str(message)
                super().__init__(self.message)
                self.type = type
                self.param = param
                self.code = str(code)
                self.headers = headers or {}
                self.openai_code = openai_code or code
                self.provider_specific_fields = provider_specific_fields

        types_mod.ProxyException = ProxyException
        proxy_mod._types = types_mod
        sys.modules["litellm.proxy._types"] = types_mod

    # ③ 第二道预算检查的桩:_PROXY_MaxBudgetLimiter(budget_notice 模块加载时
    #   会 patch 它的 async_pre_call_hook)。原始实现:超预算抛 BudgetExceededError。
    if "litellm.proxy.hooks.max_budget_limiter" not in sys.modules:
        proxy_mod = sys.modules["litellm.proxy"]
        hooks_mod = types.ModuleType("litellm.proxy.hooks")
        mbl = types.ModuleType("litellm.proxy.hooks.max_budget_limiter")

        class _PROXY_MaxBudgetLimiter:
            async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
                spend = float(getattr(user_api_key_dict, "spend", 0) or 0)
                mx = getattr(user_api_key_dict, "max_budget", None)
                if mx and spend > mx:
                    raise litellm.BudgetExceededError(current_cost=spend, max_budget=mx)
                return None

        mbl._PROXY_MaxBudgetLimiter = _PROXY_MaxBudgetLimiter
        hooks_mod.max_budget_limiter = mbl
        proxy_mod.hooks = hooks_mod
        sys.modules["litellm.proxy.hooks"] = hooks_mod
        sys.modules["litellm.proxy.hooks.max_budget_limiter"] = mbl

    # ③ 第三道预算闸门的桩:capacity patch 的预算预留层。
    if "litellm.proxy.spend_tracking.budget_reservation" not in sys.modules:
        proxy_mod = sys.modules["litellm.proxy"]
        st_mod = types.ModuleType("litellm.proxy.spend_tracking")
        br = types.ModuleType("litellm.proxy.spend_tracking.budget_reservation")

        async def reserve_budget_for_request(**kwargs):
            vt = kwargs.get("valid_token")
            spend = float(getattr(vt, "spend", 0) or 0)
            mx = getattr(vt, "max_budget", None)
            if mx and spend + 0.16 > mx:  # 模拟含预估成本
                raise litellm.BudgetExceededError(
                    current_cost=spend + 0.16, max_budget=mx,
                    message=f"Budget has been exceeded! Key=x Current cost: {spend+0.16}, Max budget: {mx}")
            return {"reserved_cost": 0.16, "entries": [], "finalized": False}

        br.reserve_budget_for_request = reserve_budget_for_request
        st_mod.budget_reservation = br
        proxy_mod.spend_tracking = st_mod
        sys.modules["litellm.proxy.spend_tracking"] = st_mod
        sys.modules["litellm.proxy.spend_tracking.budget_reservation"] = br


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


class ModelCatalogTest(unittest.TestCase):
    def setUp(self):
        _ensure_stubs()
        self.proxy_server = types.ModuleType("litellm.proxy.proxy_server")
        self.old_proxy_server = sys.modules.get("litellm.proxy.proxy_server")
        sys.modules["litellm.proxy.proxy_server"] = self.proxy_server

    def tearDown(self):
        if self.old_proxy_server is None:
            sys.modules.pop("litellm.proxy.proxy_server", None)
        else:
            sys.modules["litellm.proxy.proxy_server"] = self.old_proxy_server

    def test_quota_text_filters_allowlist_and_deduplicates_pool_legs(self):
        self.proxy_server.llm_router = types.SimpleNamespace(model_list=[
            {"model_name": "gpt-5.6-sol",
             "model_info": {"context_window": 1000000,
                             "max_input_tokens": 922000},
             "litellm_params": {"max_output_tokens": 128000}},
            {"model_name": "gpt-5.6-sol",
             "model_info": {"context_window": 1000000,
                             "max_input_tokens": 922000,
                             "max_output_tokens": 128000}},
            {"model_name": "hidden-model",
             "model_info": {"context_window": 123}},
        ])
        key = _Key(alias="cursor-u1")
        key.models = ["gpt-5.6-sol"]

        text = M._model_catalog_text(key)

        self.assertIn("gpt-5.6-sol", text)
        self.assertIn("922k  128k  gpt-5.6-sol", text)
        self.assertNotIn("hidden-model", text)
        self.assertEqual(text.count("gpt-5.6-sol"), 1)

    def test_quota_pre_call_includes_model_catalog(self):
        self.proxy_server.llm_router = types.SimpleNamespace(model_list=[
            {"model_name": "gpt-5.6-sol",
             "model_info": {"context_window": 1000000,
                             "max_input_tokens": 922000,
                             "max_output_tokens": 128000}},
        ])
        key = _Key(alias="cursor-u1")
        key.models = ["gpt-5.6-sol"]

        out = _pre(
            {"messages": [{"role": "user", "content": "查余额"}]},
            "acompletion",
            key=key,
        )

        self.assertIn("📋 模型 1 个，1 组上限", out["mock_response"])
        self.assertIn("922k  128k  gpt-5.6-sol", out["mock_response"])
        # `context_window` 即使部署行上有，也不该出现 —— 它不是闸门读的字段
        self.assertNotIn("1,000,000", out["mock_response"])
        self.assertNotIn("1M", out["mock_response"])

    def test_empty_allowlist_lists_all_models_and_marks_missing_limits(self):
        self.proxy_server.llm_router = types.SimpleNamespace(model_list=[
            {"model_name": "model-without-limits", "model_info": {}},
        ])
        key = _Key(alias="cursor-u1")
        key.models = []

        text = M._model_catalog_text(key)

        self.assertIn("model-without-limits", text)
        # 两栏（输入/输出）各一个占位符，不是三栏 —— `context_window` 那栏已删，
        # 见下面的回归护栏。⛔ 不能数占位符出现了几次：它是 ASCII `-`，
        # 分隔线和模型名里都有，数出来是 43 不是 3。判据只能是**那一行本身**。
        row = [ln for ln in text.splitlines() if "model-without-limits" in ln][0]
        self.assertEqual(row.split(), ["-", "-", "model-without-limits"])
        self.assertIn(f"{M._NO_LIMIT} = 未设上限", text)

    def test_no_hardcoded_product_limits_are_invented(self):
        """🔴 回归护栏：限制值只许来自部署行 / LiteLLM 自己的表，不许来自硬编。

        2026-09-25 前这里有一张 `_MODEL_LIMIT_DEFAULTS`，给 `cr-g-5.6-instant`
        之类没有配置的模型编出 `1,000,000/922,000/128,000`。两个问题：
        `context_window` 不是 LiteLLM 的字段（线上 41/41 为 None），
        而 922,000 是我们自己算的、从来不是真实天花板。
        兜底值改不动闸门，只改显示 ⇒「显示 1,000,000、实际拦在 922,000」
        能长期零症状共存。所以宁可显示"未设上限"，也不许把假设印成事实。
        """
        self.assertFalse(hasattr(M, "_MODEL_LIMIT_DEFAULTS"),
                         "硬编默认限制表又回来了")
        self.assertFalse(hasattr(M, "_model_limit_defaults"))

        self.proxy_server.llm_router = types.SimpleNamespace(model_list=[
            {"model_name": "cr-g-5.6-instant",
             "model_info": {},
             "litellm_params": {"model": "openai/gpt-5.6-instant"}},
        ])
        key = _Key(alias="cursor-u1")
        key.models = ["cr-g-5.6-instant"]

        text = M._model_catalog_text(key)

        self.assertIn("cr-g-5.6-instant", text)
        self.assertNotIn("922,000", text)
        self.assertNotIn("922k", text)
        row = [ln for ln in text.splitlines() if "cr-g-5.6-instant" in ln][0]
        self.assertEqual(row.split()[:2], [M._NO_LIMIT, M._NO_LIMIT])

    def test_table_uses_no_ambiguous_width_characters(self):
        """🔴 对齐的前提是每个字符宽度确定，而 `—`/`─` 的宽度**不确定**。

        East Asian Width = Ambiguous 的字符在非 CJK 环境占 1 格、CJK 环境占 2 格。
        表格里出现它 ⇒ 同一个字符串在两种客户端里宽度不同，任何一套 padding
        都只能对一半（实测：`—` 占位那一行整列歪 1 格）。
        表格的全部价值就是对齐，所以宁可用不好看的 ASCII。
        中文表头是 W（确定 2 格），不在此列。
        """
        import unicodedata

        self.proxy_server.llm_router = types.SimpleNamespace(model_list=[
            {"model_name": "m1", "model_info": {"max_input_tokens": 922000}},
            {"model_name": "m2", "model_info": {}},
        ])
        key = _Key(alias="cursor-u1")
        key.models = ["m1", "m2"]

        text = M._model_catalog_text(key)
        offenders = sorted({
            ch for ch in text
            if unicodedata.east_asian_width(ch) == "A" and not ch.isspace()
        })
        self.assertEqual(offenders, [], f"表格里有宽度不确定的字符：{offenders}")

    def test_catalog_drops_the_fabricated_context_window_column(self):
        """`context_window` 即使部署行上写了，也不再显示 —— 它不是闸门读的字段。"""
        self.proxy_server.llm_router = types.SimpleNamespace(model_list=[
            {"model_name": "m1",
             "model_info": {"context_window": 999999,
                             "max_input_tokens": 250000,
                             "max_output_tokens": 16384}},
        ])
        key = _Key(alias="cursor-u1")
        key.models = ["m1"]

        text = M._model_catalog_text(key)

        self.assertIn("250k  ~16k  m1", text)
        self.assertNotIn("999,999", text)
        self.assertNotIn("~1M", text)
        self.assertIn("输入", text)
        self.assertIn("输出", text)

    def test_catalog_uses_key_allowlist_and_per_key_aliases(self):
        self.proxy_server.llm_router = types.SimpleNamespace(model_list=[
            {"model_name": "kiro-qwen3-coder-next",
             "model_info": {"context_window": 262144,
                             "max_input_tokens": 250000,
                             "max_output_tokens": 16384}},
        ])
        key = _Key(alias="cursor-u1")
        key.models = ["qwen3-coder-next", "callback-only-model"]
        key.aliases = {"qwen3-coder-next": "kiro-qwen3-coder-next"}

        text = M._model_catalog_text(key)

        self.assertIn("📋 模型 2 个，2 组上限", text)
        self.assertIn("qwen3-coder-next", text)
        self.assertIn("250k", text)
        self.assertIn("callback-only-model", text)
        self.assertEqual(text.count("qwen3-coder-next"), 1)

    def test_rounded_labels_never_merge_two_distinct_gate_values(self):
        """🔴 四舍五入是显示层的取舍，不许变成分组层的丢数据。

        用户明确选了「1,048,576 显示成 ~1.05M」这种近似写法，代价是
        1,048,576 和 1,050,000 渲染出同一个字符串。如果分组 key 用的是
        **渲染后的字符串**，这两个不同的闸门就会被并成一行 —— 41 个模型里
        少掉一个真实配置值，而且一个报错都没有、看起来完全正常。
        所以分组 key 必须是**原始数值**：最坏情况是两行标签长得一样
        （可见的歧义，读者会去问），而不是少一行（静默丢数据）。
        """
        self.proxy_server.llm_router = types.SimpleNamespace(model_list=[
            {"model_name": "a-1048576",
             "model_info": {"max_input_tokens": 1048576, "max_output_tokens": 131072}},
            {"model_name": "b-1050000",
             "model_info": {"max_input_tokens": 1050000, "max_output_tokens": 131072}},
        ])
        key = _Key(alias="cursor-u1")
        key.models = ["a-1048576", "b-1050000"]

        text = M._model_catalog_text(key)

        self.assertEqual(text.count("~1.05M"), 2, "两个不同的闸门被并成一行了")
        self.assertIn("📋 模型 2 个，2 组上限", text)
        # 归一：`"1050000"`（字符串）和 `1050000.0` 必须和 `1050000` 同组，
        # 否则同一个闸门会因为写法不同被拆成三行。
        self.proxy_server.llm_router.model_list.append(
            {"model_name": "c-str",
             "model_info": {"max_input_tokens": "1050000", "max_output_tokens": 131072.0}})
        key.models = ["a-1048576", "b-1050000", "c-str"]
        self.assertIn("📋 模型 3 个，2 组上限", M._model_catalog_text(key))

    def test_limit_rows_are_sorted_by_numeric_value_not_by_label(self):
        """🔴 排序只认原始数值：字典序会把 `10M` 排到 `922k` 前面。"""
        self.proxy_server.llm_router = types.SimpleNamespace(model_list=[
            {"model_name": "big", "model_info": {"max_input_tokens": 10000000}},
            {"model_name": "mid", "model_info": {"max_input_tokens": 922000}},
            {"model_name": "small", "model_info": {"max_input_tokens": 200000}},
            {"model_name": "none-at-all", "model_info": {}},
        ])
        key = _Key(alias="cursor-u1")
        key.models = ["big", "mid", "small", "none-at-all"]

        text = M._model_catalog_text(key)
        order = [name for name in ("none-at-all", "small", "mid", "big")]
        positions = [text.index(name) for name in order]
        self.assertEqual(positions, sorted(positions),
                         "行序不是按输入上限升序（未设上限最前）")


class TTFTTest(unittest.TestCase):
    def setUp(self):
        _ensure_stubs()
        self.proxy_server = types.ModuleType("litellm.proxy.proxy_server")
        self.old_proxy_server = sys.modules.get("litellm.proxy.proxy_server")
        sys.modules["litellm.proxy.proxy_server"] = self.proxy_server
        M._TTFT_CACHE.clear()

    def tearDown(self):
        M._TTFT_CACHE.clear()
        if self.old_proxy_server is None:
            sys.modules.pop("litellm.proxy.proxy_server", None)
        else:
            sys.modules["litellm.proxy.proxy_server"] = self.old_proxy_server

    def test_per_model_ttft_is_rendered_and_cached(self):
        calls = []

        class DB:
            async def query_raw(self, query, value):
                calls.append((query, value))
                return [
                    {"g": "gpt-5.6-sol", "n": 662, "p50": 1.25, "p90": 2.53},
                    {"g": "sa-grok-4.6", "n": 1, "p50": 17.51, "p90": 17.51},
                ]

        self.proxy_server.prisma_client = types.SimpleNamespace(db=DB())
        key = _Key(alias="cursor-u1", token="hashed-token")

        first = asyncio.run(M._ttft_text(key))
        second = asyncio.run(M._ttft_text(key))

        self.assertIn("gpt-5.6-sol   1.25   2.53   662", first)
        self.assertIn("sa-grok-4.6  17.51  17.51     1", first)
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "hashed-token")

    def test_ttft_sql_keeps_the_two_measured_performance_constraints(self):
        """把 2026-09-24 实测的两条硬约束钉住 —— 违反任一条都是 1000x 级退化。

        约束来自 198 prod 库实测（见 `_TTFT_SQL` 上方注释）：
          1. metadata->>'user_api_key_alias' 过滤 = 791,457 ms；api_key = 773 ms
          2. "startTime" 上套算术会让 startTime 索引失效，退化成 Seq Scan
        这两条不是风格问题：proxy 只有 1 条 DB 连接（connection_limit=1），
        一条 13 分钟的查询会把整个 proxy 的 DB 访问饿死。
        """
        sql = M._TTFT_SQL
        self.assertIn('"api_key" = $1', sql)
        self.assertNotIn("metadata", sql)
        # startTime 必须裸列参与比较，偏移挪到右边
        self.assertIn('"startTime" >=', sql)
        self.assertNotIn('"startTime" + INTERVAL', sql)
        # 分组列必须是请求名 model_group，不是落点名 model
        self.assertIn('"model_group"', sql)

    def test_ttft_query_failure_is_visible(self):
        class DB:
            async def query_raw(self, query, value):
                raise RuntimeError("db unavailable")

        self.proxy_server.prisma_client = types.SimpleNamespace(db=DB())
        text = asyncio.run(
            M._ttft_text(_Key(alias="cursor-u1", token="hashed-token")))
        self.assertIn("查询暂不可用", text)

    def test_ttft_no_rows_says_so_instead_of_vanishing(self):
        class DB:
            async def query_raw(self, query, value):
                return []

        self.proxy_server.prisma_client = types.SimpleNamespace(db=DB())
        text = asyncio.run(
            M._ttft_text(_Key(alias="cursor-u1", token="hashed-token")))
        self.assertIn("暂无有效样本", text)

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


class ReasoningTokensBackfillTest(unittest.TestCase):
    """mock usage 少 `reasoning_tokens` 会让 Codex 判 ResponseCompleted 解析失败,
    整轮重试(WS 5 次 + 降 HTTP 再 5 次),同一张余额卡刷 12 遍。"""

    @staticmethod
    def _resp(details):
        class _Usage:
            pass

        class ResponsesAPIResponse:
            pass

        u = _Usage()
        u.output_tokens_details = details
        r = ResponsesAPIResponse()
        r.usage = u
        return r

    def test_empty_details_object_gets_zero(self):
        class _Details:
            reasoning_tokens = None

        r = self._resp(_Details())
        M._ensure_reasoning_tokens(r)
        self.assertEqual(r.usage.output_tokens_details.reasoning_tokens, 0)

    def test_existing_value_is_not_clobbered(self):
        class _Details:
            reasoning_tokens = 42

        r = self._resp(_Details())
        M._ensure_reasoning_tokens(r)
        self.assertEqual(r.usage.output_tokens_details.reasoning_tokens, 42)

    def test_dict_details_gets_zero(self):
        r = self._resp({})
        M._ensure_reasoning_tokens(r)
        self.assertEqual(r.usage.output_tokens_details["reasoning_tokens"], 0)

    def test_no_usage_is_a_noop(self):
        class ResponsesAPIResponse:
            pass

        M._ensure_reasoning_tokens(ResponsesAPIResponse())  # 不抛即通过

    def test_stream_guard_backfills_before_wrapping(self):
        class _Details:
            reasoning_tokens = None

        r = self._resp(_Details())
        out = _run_stream_raw(r, _Key(token="tok-g-2"))
        self.assertEqual(out, ["EV1:ResponsesAPIResponse", "EV2:done"])
        self.assertEqual(r.usage.output_tokens_details.reasoning_tokens, 0)


def _run_stream_raw(response_obj, key):
    async def go():
        out = []
        gen = M.budget_notice.async_post_call_streaming_iterator_hook(
            key, response_obj, {})
        async for x in gen:
            out.append(x)
        return out
    return _with_env(PREFIX_ENV, lambda: asyncio.run(go()))


class _REvent:
    """responses pydantic 事件的形状替身:type + 任意属性。"""

    def __init__(self, type, **kw):
        self.type = type
        for k, v in kw.items():
            setattr(self, k, v)


class _RPart:
    def __init__(self, text):
        self.type = "output_text"
        self.text = text


def _responses_events(final="text"):
    evs = [
        _REvent("response.created"),
        _REvent("response.output_item.added", output_index=0),
        _REvent("response.content_part.added", output_index=0, content_index=0),
        _REvent("response.output_text.delta", delta="mock ", item_id="m1"),
        _REvent("response.output_text.delta", delta="answer", item_id="m1"),
        _REvent("response.output_text.done", text="mock answer", item_id="m1"),
        _REvent("response.content_part.done", part=_RPart("mock answer")),
        _REvent("response.output_item.done",
                item={"type": "message",
                      "content": [{"type": "output_text", "text": "mock answer"}]}),
    ]
    if final == "tool":
        evs += [
            _REvent("response.output_item.added", output_index=1),
            _REvent("response.output_item.done",
                    item={"type": "function_call", "name": "bash"}),
        ]
    evs.append(_REvent("response.completed",
                       response={"output": [
                           {"type": "message",
                            "content": [{"type": "output_text",
                                         "text": "mock answer"}]}]}))
    return evs


def _rkey(token):
    return _Key(alias="cursor-u9", spend=65.0, max_budget=70.0, token=token,
                route="/v1/responses")


class ResponsesInjectTest(unittest.TestCase):
    def test_inject_into_last_text_part(self):
        evs = _responses_events()
        key = _rkey("tok-r-1")
        out = _run_stream(list(evs), key)
        notice = M._warn_text(key)
        self.assertEqual(len(out), len(evs) + 1)
        # 注入的 delta 紧跟在最后一个真实 delta 之后、text.done 之前
        types = [M._ev_type(e) for e in out]
        inj_pos = 5
        self.assertEqual(types[inj_pos], "response.output_text.delta")
        self.assertEqual(out[inj_pos].delta, notice)
        self.assertEqual(types[inj_pos + 1], "response.output_text.done")
        # done/completed 四处全文都补上了
        self.assertTrue(out[inj_pos + 1].text.endswith(notice))
        self.assertTrue(out[inj_pos + 2].part.text.endswith(notice))
        self.assertTrue(out[inj_pos + 3].item["content"][0]["text"].endswith(notice))
        self.assertTrue(out[-1].response["output"][0]["content"][0]["text"]
                        .endswith(notice))

    def test_dedupe_second_stream_untouched(self):
        evs = _responses_events()
        _run_stream(list(evs), _rkey("tok-r-2"))
        out2 = _run_stream(list(_responses_events()), _rkey("tok-r-2"))
        self.assertEqual(len(out2), len(evs))

    def test_tool_final_skips_and_releases(self):
        evs = _responses_events(final="tool")
        out = _run_stream(list(evs), _rkey("tok-r-3"))
        self.assertEqual(len(out), len(evs))
        notice = M._warn_text(_rkey("tok-r-3"))
        self.assertFalse(any(getattr(e, "delta", None) == notice for e in out))
        # 名额已释放:下一个 text 收尾的流要能注入
        out2 = _run_stream(list(_responses_events()), _rkey("tok-r-3"))
        self.assertEqual(len(out2), len(_responses_events()) + 1)


class UsageTextTest(unittest.TestCase):
    def test_reset_time_is_beijing(self):
        key = _Key(reset_at=datetime.datetime(2026, 8, 19, 16, 0))
        self.assertIn("北京时间 08-20 00:00", M._usage_text(key))

    def test_percent_and_remain(self):
        key = _Key(spend=65.0, max_budget=70.0)
        t = M._usage_text(key)
        self.assertIn("93%", t)
        self.assertIn("$5.00", t)


# ------------------------------------------------ ③ 超预算软拦截成 200

class OverBudgetSoftBlockTest(unittest.TestCase):
    """patch 层吞 BudgetExceededError→打 mark→pre_call 出 200 友好文案。"""

    def setUp(self):
        _ensure_stubs()
        M._OVER_BUDGET_MARKS.clear()

    def tearDown(self):
        M._OVER_BUDGET_MARKS.clear()

    # ---- mark 生命周期 ----
    def test_mark_set_take_persists(self):
        M._mark_over_budget("tok-ob", 9.0, 5.0)
        # 不 pop：同一 mark 覆盖预检+真实请求两跳，可重复取
        self.assertEqual(M._take_over_budget_mark("tok-ob"), (9.0, 5.0))
        self.assertEqual(M._take_over_budget_mark("tok-ob"), (9.0, 5.0))

    def test_mark_clear(self):
        M._mark_over_budget("tok-ob", 9.0, 5.0)
        M._clear_over_budget_mark("tok-ob")
        self.assertIsNone(M._take_over_budget_mark("tok-ob"))

    def test_mark_ttl_expiry(self):
        M._mark_over_budget("tok-ob", 9.0, 5.0)
        # 手动把时间戳挪到 TTL 之外
        ts, c, m = M._OVER_BUDGET_MARKS["tok-ob"]
        M._OVER_BUDGET_MARKS["tok-ob"] = (ts - M._OVER_BUDGET_TTL - 1.0, c, m)
        self.assertIsNone(M._take_over_budget_mark("tok-ob"))
        self.assertNotIn("tok-ob", M._OVER_BUDGET_MARKS)

    def test_take_missing_returns_none(self):
        self.assertIsNone(M._take_over_budget_mark("nope"))

    # ---- pre_call：mark 存在 → 200 友好文案 ----
    def test_over_budget_chat_mock(self):
        M._mark_over_budget("tok-default", 9.0, 5.0)
        d = {"messages": [{"role": "user", "content": "帮我写个函数"}]}
        out = _pre(d, "acompletion")
        self.assertIn("额度已用完", out.get("mock_response", ""))
        self.assertIn("$9.00", out["mock_response"])
        self.assertIn("$5.00", out["mock_response"])

    def test_over_budget_responses_mock(self):
        M._mark_over_budget("tok-default", 9.0, 5.0)
        out = _pre({"input": "继续"}, "aresponses")
        self.assertIn("额度已用完", out.get("mock_response", ""))

    def test_over_budget_anthropic_raises_modify(self):
        from litellm.exceptions import ModifyResponseException

        M._mark_over_budget("tok-default", 9.0, 5.0)
        d = {"model": "gpt-x", "messages": [{"role": "user", "content": "hi"}]}
        with self.assertRaises(ModifyResponseException) as ctx:
            _pre(d, "aanthropic_messages")
        self.assertIn("额度已用完", ctx.exception.message)

    def test_over_budget_nonmockable_reraises(self):
        import litellm

        M._mark_over_budget("tok-default", 9.0, 5.0)
        d = {"messages": [{"role": "user", "content": "hi"}]}
        with self.assertRaises(litellm.BudgetExceededError):
            _pre(d, "aembedding")

    def test_over_budget_priority_over_quota_query(self):
        """超预算优先级高于 /查余额：mark 在时即便发 /查余额也出「额度已用完」。"""
        M._mark_over_budget("tok-default", 9.0, 5.0)
        d = {"messages": [{"role": "user", "content": "/查余额"}]}
        out = _pre(d, "acompletion")
        self.assertIn("额度已用完", out.get("mock_response", ""))

    def test_friendly_mock_disabled_ignores_mark(self):
        M._mark_over_budget("tok-default", 9.0, 5.0)
        d = {"messages": [{"role": "user", "content": "hi"}]}

        def go():
            return _pre(d, "acompletion")

        out = _with_env({"BUDGET_FRIENDLY_MOCK_DISABLED": "1"}, go)
        self.assertNotIn("mock_response", out)

    def test_ungated_key_ignores_mark(self):
        M._mark_over_budget("tok-other", 9.0, 5.0)
        d = {"messages": [{"role": "user", "content": "hi"}]}
        out = _pre(d, "acompletion", key=_Key(alias="other-user", token="tok-other"))
        self.assertNotIn("mock_response", out)

    # ---- patch wrapper：吞 429 / 打 mark / 清 mark ----
    def _wrap_and_call(self, orig, key):
        wrapped = M._make_budget_soft_wrapper(orig)
        return _with_env(PREFIX_ENV, lambda: asyncio.run(wrapped(key)))

    def test_wrapper_gated_over_budget_marks_and_swallows(self):
        import litellm

        async def orig(vt, *a, **k):
            raise litellm.BudgetExceededError(current_cost=9.0, max_budget=5.0)

        key = _Key(token="tok-w1")
        res = self._wrap_and_call(orig, key)
        self.assertIsNone(res)
        self.assertEqual(M._take_over_budget_mark("tok-w1"), (9.0, 5.0))

    def test_wrapper_success_clears_mark(self):
        M._mark_over_budget("tok-w2", 9.0, 5.0)

        async def orig(vt, *a, **k):
            return "OK"

        key = _Key(token="tok-w2")
        res = self._wrap_and_call(orig, key)
        self.assertEqual(res, "OK")
        self.assertIsNone(M._take_over_budget_mark("tok-w2"))

    def test_wrapper_ungated_reraises(self):
        import litellm

        async def orig(vt, *a, **k):
            raise litellm.BudgetExceededError(current_cost=9.0, max_budget=5.0)

        key = _Key(alias="other-user", token="tok-w3")
        with self.assertRaises(litellm.BudgetExceededError):
            self._wrap_and_call(orig, key)
        self.assertIsNone(M._take_over_budget_mark("tok-w3"))

    def test_wrapper_disabled_reraises(self):
        import litellm

        async def orig(vt, *a, **k):
            raise litellm.BudgetExceededError(current_cost=9.0, max_budget=5.0)

        key = _Key(token="tok-w4")
        with self.assertRaises(litellm.BudgetExceededError):
            _with_env({"BUDGET_FRIENDLY_MOCK_DISABLED": "1", **PREFIX_ENV},
                      lambda: asyncio.run(M._make_budget_soft_wrapper(orig)(key)))
        self.assertIsNone(M._take_over_budget_mark("tok-w4"))

    def test_wrapper_non_budget_exception_propagates(self):
        async def orig(vt, *a, **k):
            raise ValueError("boom")

        key = _Key(token="tok-w5")
        with self.assertRaises(ValueError):
            self._wrap_and_call(orig, key)

    # ---- 第二道检查(_PROXY_MaxBudgetLimiter)的 patch ----
    def test_limiter_patch_swallows_and_marks_gated(self):
        from litellm.proxy.hooks.max_budget_limiter import _PROXY_MaxBudgetLimiter

        self.assertTrue(getattr(
            _PROXY_MaxBudgetLimiter.async_pre_call_hook, "_bn_budget_soft_patched", False))
        inst = _PROXY_MaxBudgetLimiter()
        key = _Key(spend=80.0, max_budget=70.0, token="tok-l1")
        res = _with_env(PREFIX_ENV, lambda: asyncio.run(
            inst.async_pre_call_hook(key, None, {}, _CallType("acompletion"))))
        self.assertIsNone(res)
        self.assertEqual(M._take_over_budget_mark("tok-l1"), (80.0, 70.0))

    def test_limiter_patch_reraises_ungated(self):
        import litellm

        from litellm.proxy.hooks.max_budget_limiter import _PROXY_MaxBudgetLimiter

        inst = _PROXY_MaxBudgetLimiter()
        key = _Key(alias="other-user", spend=80.0, max_budget=70.0, token="tok-l2")
        with self.assertRaises(litellm.BudgetExceededError):
            _with_env(PREFIX_ENV, lambda: asyncio.run(
                inst.async_pre_call_hook(key, None, {}, _CallType("acompletion"))))
        self.assertIsNone(M._take_over_budget_mark("tok-l2"))

    def test_limiter_patch_under_budget_passthrough(self):
        from litellm.proxy.hooks.max_budget_limiter import _PROXY_MaxBudgetLimiter

        inst = _PROXY_MaxBudgetLimiter()
        key = _Key(spend=10.0, max_budget=70.0, token="tok-l3")
        res = _with_env(PREFIX_ENV, lambda: asyncio.run(
            inst.async_pre_call_hook(key, None, {}, _CallType("acompletion"))))
        self.assertIsNone(res)
        self.assertIsNone(M._take_over_budget_mark("tok-l3"))

    # ---- 第三道闸门(capacity patch 预算预留)的 patch ----
    def test_reservation_patch_swallows_and_marks_gated(self):
        from litellm.proxy.spend_tracking.budget_reservation import (
            reserve_budget_for_request,
        )

        self.assertTrue(getattr(
            reserve_budget_for_request, "_bn_budget_soft_patched", False))
        key = _Key(spend=75.0, max_budget=70.0, token="tok-r1")
        res = _with_env(PREFIX_ENV, lambda: asyncio.run(
            reserve_budget_for_request(valid_token=key, request_body={}, route="/v1/chat/completions")))
        self.assertIsNone(res)
        cost, mx = M._take_over_budget_mark("tok-r1")
        self.assertAlmostEqual(cost, 75.16)
        self.assertEqual(mx, 70.0)

    def test_reservation_patch_reraises_ungated(self):
        import litellm

        from litellm.proxy.spend_tracking.budget_reservation import (
            reserve_budget_for_request,
        )

        key = _Key(alias="other-user", spend=75.0, max_budget=70.0, token="tok-r2")
        with self.assertRaises(litellm.BudgetExceededError):
            _with_env(PREFIX_ENV, lambda: asyncio.run(
                reserve_budget_for_request(valid_token=key, request_body={}, route="/x")))
        self.assertIsNone(M._take_over_budget_mark("tok-r2"))

    def test_reservation_patch_under_budget_passthrough(self):
        from litellm.proxy.spend_tracking.budget_reservation import (
            reserve_budget_for_request,
        )

        key = _Key(spend=10.0, max_budget=70.0, token="tok-r3")
        res = _with_env(PREFIX_ENV, lambda: asyncio.run(
            reserve_budget_for_request(valid_token=key, request_body={}, route="/x")))
        self.assertIsNotNone(res)
        self.assertIsNone(M._take_over_budget_mark("tok-r3"))


# ------------------------------------------------ ④ 模型系列日额度

FAM_ENV = {**PREFIX_ENV, "BUDGET_FAMILY_ENABLED": "1"}


class FamilyBudgetTest(unittest.TestCase):
    def setUp(self):
        _ensure_stubs()
        M._fam_local.clear()
        M._OVER_BUDGET_MARKS.clear()

    def tearDown(self):
        M._fam_local.clear()

    # ---- 匹配器 ----
    def test_family_matcher(self):
        cursor_key = _Key(alias="cursor-u1")
        legacy_key = _Key(alias="claude-code-u1")
        for m in ("claude-fable-5", "fable5.1", "claude-sonnet-5",
                  "kiro-claude-opus-5", "anthropic.claude-opus-4-8",
                  "cursor-ultra-haiku-4-5"):
            self.assertEqual(M._family_of(m, cursor_key)[0], "claude", m)
            self.assertEqual(M._family_of(m, legacy_key)[0], "claude", m)
        for m in ("gpt-5.3-codex", "chatgpt-gpt-5.3-codex",
                  "cursor-gpt-5.3-codex", "zerokey-pool-gpt-5.3-mini",
                  "openrouter-gpt-5.3-codex"):
            self.assertEqual(M._family_of(m, cursor_key)[0], "other", m)
            self.assertEqual(M._family_of(m, legacy_key)[0], "gpt53", m)
        for m in ("gpt-5.6-sol", "gpt-5.6-terra",
                  "gpt-5.5", "gpt-5.4-mini",
                  "chatgpt-gpt-5.6-luna", "sa-gpt-image-1", "claude-gpt-5.6-sol",
                  "claude-gpt-5.3",
                  "claude-glm-5.3", "claude-deepseek-v4-pro", "claude-kimi-k2.7-code",
                  "claude-grok-4.6", "deepseek-v4-flash", "glm-5.3"):
            self.assertEqual(M._family_of(m, cursor_key)[0], "other", m)
            self.assertEqual(M._family_of(m, legacy_key)[0], "other", m)
        self.assertIsNone(M._family_of(""), "empty model")

    def test_non_cursor_retains_gpt53_bucket(self):
        self.assertEqual(M._family_of("gpt-5.3-codex", _Key(alias="claude-code-u1"))[0],
                         "gpt53")

    # ---- 限额来源 ----
    def test_limit_default_env_metadata(self):
        k = _Key()
        self.assertEqual(M._family_limit("gpt53", k), 200.0)
        self.assertEqual(M._family_limit("claude", k), 100.0)
        self.assertEqual(M._family_limit("other", k), 500.0)
        out = _with_env({"BUDGET_FAMILY_CLAUDE_USD": "75"},
                        lambda: M._family_limit("claude", k))
        self.assertEqual(out, 75.0)
        out = _with_env({"BUDGET_FAMILY_OTHER_USD": "450"},
                        lambda: M._family_limit("other", k))
        self.assertEqual(out, 450.0)
        out = _with_env({"BUDGET_FAMILY_GPT53_USD": "50"},
                        lambda: M._family_limit("gpt53", k))
        self.assertEqual(out, 50.0)
        k.metadata = {"budget_family_overrides": {"claude": 0.5}}
        self.assertEqual(M._family_limit("claude", k), 0.5)
        k.metadata = {"budget_family_overrides": {"other": 0.5}}
        self.assertEqual(M._family_limit("other", k), 0.5)
        k.metadata = {"budget_family_overrides": {"gpt53": 0.5}}
        self.assertEqual(M._family_limit("gpt53", k), 0.5)

    # ---- 记账（local 降级路径）----
    def test_accounting_accumulates(self):
        async def go():
            await M._family_add_spend("tok-f1", "other", 1.5)
            await M._family_add_spend("tok-f1", "other", 2.5)
            return await M._family_get_spend("tok-f1", "other")
        self.assertEqual(asyncio.run(go()), 4.0)

    def test_log_event_gated_and_family_routed(self):
        slp = {"response_cost": 3.0, "model_group": "gpt-5.6-sol",
               "metadata": {"user_api_key_alias": "claude-code-u1",
                            "user_api_key_hash": "tok-f2"}}
        def go():
            return asyncio.run(M.budget_notice.async_log_success_event(
                {"standard_logging_object": slp}, None, None, None))
        _with_env(FAM_ENV, go)
        async def rd():
            return await M._family_get_spend("tok-f2", "other")
        self.assertEqual(asyncio.run(rd()), 3.0)

    def test_log_event_claude_family_routed(self):
        slp = {"response_cost": 4.0, "model_group": "kiro-claude-opus-5",
               "metadata": {"user_api_key_alias": "cursor-u1",
                            "user_api_key_hash": "tok-claude"}}
        _with_env(FAM_ENV, lambda: asyncio.run(
            M.budget_notice.async_log_success_event(
                {"standard_logging_object": slp}, None, None, None)))

        async def rd():
            return await M._family_get_spend("tok-claude", "claude")
        self.assertEqual(asyncio.run(rd()), 4.0)

    def test_log_event_ungated_or_disabled_noop(self):
        slp = {"response_cost": 3.0, "model_group": "gpt-5.6-sol",
               "metadata": {"user_api_key_alias": "other-user",
                            "user_api_key_hash": "tok-f3"}}
        _with_env(FAM_ENV, lambda: asyncio.run(
            M.budget_notice.async_log_success_event(
                {"standard_logging_object": slp}, None, None, None)))
        # 未启用 env 时 gated key 也不记
        slp2 = {**slp, "metadata": {"user_api_key_alias": "claude-code-u1",
                                    "user_api_key_hash": "tok-f4"}}
        _with_env(PREFIX_ENV, lambda: asyncio.run(
            M.budget_notice.async_log_success_event(
                {"standard_logging_object": slp2}, None, None, None)))
        async def rd():
            return (await M._family_get_spend("tok-f3", "other"),
                    await M._family_get_spend("tok-f4", "other"))
        self.assertEqual(asyncio.run(rd()), (0.0, 0.0))

    # ---- 执法 ----
    def _pre_fam(self, data, call_type, key=None):
        async def go():
            return await M.budget_notice.async_pre_call_hook(
                key or _Key(alias="cursor-u1"), None, data, _CallType(call_type))
        return _with_env(FAM_ENV, lambda: asyncio.run(go()))

    def _seed(self, token, fkey, amount):
        M._fam_local[M._family_spend_key(fkey, token)] = amount

    def test_family_block_chat_mock(self):
        self._seed("tok-default", "other", 500.0)
        d = {"model": "gpt-5.3-codex",
             "messages": [{"role": "user", "content": "写个函数"}]}
        out = self._pre_fam(d, "acompletion")
        self.assertIn("其他模型 额度已用完", out.get("mock_response", ""))
        self.assertIn("$500.00", out["mock_response"])
        self.assertIn("$500.00", out["mock_response"])
        self.assertIn("不受影响", out["mock_response"])

    def test_family_gpt53_uses_other_bucket(self):
        """GPT-5.3 不再有独立桶，直接使用 other。"""
        self._seed("tok-default", "other", 500.0)
        d = {"model": "gpt-5.6-sol",
             "messages": [{"role": "user", "content": "hi"}]}
        out = self._pre_fam(d, "acompletion")
        self.assertIn("额度已用完", out.get("mock_response", ""))

    def test_family_block_anthropic_raises(self):
        from litellm.exceptions import ModifyResponseException

        self._seed("tok-default", "claude", 100.01)
        d = {"model": "claude-opus-5",
             "messages": [{"role": "user", "content": "hi"}]}
        with self.assertRaises(ModifyResponseException) as ctx:
            self._pre_fam(d, "aanthropic_messages")
        self.assertIn("Claude 家族 额度已用完", ctx.exception.message)

    def test_family_claude_isolated_from_other_bucket(self):
        """Claude 超限时，other 桶仍可正常使用。"""
        self._seed("tok-default", "claude", 100.01)
        d = {"model": "claude-sonnet-5",
             "messages": [{"role": "user", "content": "hi"}]}
        out = self._pre_fam(d, "acompletion")
        self.assertIn("Claude 家族 额度已用完", out.get("mock_response", ""))

        d = {"model": "deepseek-v4-flash",
             "messages": [{"role": "user", "content": "hi"}]}
        out = self._pre_fam(d, "acompletion")
        self.assertNotIn("mock_response", out)

    def test_family_under_limit_passes(self):
        self._seed("tok-default", "other", 100.0)
        d = {"model": "gpt-5.3-codex",
             "messages": [{"role": "user", "content": "hi"}]}
        out = self._pre_fam(d, "acompletion")
        self.assertNotIn("mock_response", out)

    def test_family_nongpt_blocked_by_other_bucket(self):
        """非 GPT/Claude 模型仍进入 other 桶。"""
        self._seed("tok-default", "other", 600.0)
        d = {"model": "deepseek-v4-flash",
             "messages": [{"role": "user", "content": "hi"}]}
        out = self._pre_fam(d, "acompletion")
        self.assertIn("其他模型 额度已用完", out.get("mock_response", ""))

    def test_family_gpt53_blocked_by_other_bucket(self):
        self._seed("tok-default", "other", 600.0)
        d = {"model": "gpt-5.3-codex",
             "messages": [{"role": "user", "content": "hi"}]}
        out = self._pre_fam(d, "acompletion")
        self.assertIn("其他模型 额度已用完", out.get("mock_response", ""))

    def test_family_quota_query_bypasses_block(self):
        """系列爆了 /查余额 仍可用（零上游），且文案带系列用量。"""
        self._seed("tok-default", "other", 250.0)
        d = {"model": "gpt-5.3-codex",
             "messages": [{"role": "user", "content": "/查余额"}]}
        out = self._pre_fam(d, "acompletion")
        self.assertIn("今日用量", out.get("mock_response", ""))
        self.assertIn("系列额度", out["mock_response"])
        self.assertIn("其他模型 $250.00/$500.00", out["mock_response"])

        self._seed("tok-default", "claude", 100.01)
        d = {"model": "claude-opus-5",
             "messages": [{"role": "user", "content": "/查余额"}]}
        out = self._pre_fam(d, "acompletion")
        self.assertIn("Claude 家族 $100.01/$100.00", out["mock_response"])

    def test_family_disabled_by_default(self):
        self._seed("tok-default", "other", 250.0)
        d = {"model": "gpt-5.3-codex",
             "messages": [{"role": "user", "content": "hi"}]}
        out = _with_env(PREFIX_ENV, lambda: asyncio.run(
            M.budget_notice.async_pre_call_hook(
                _Key(), None, d, _CallType("acompletion"))))
        self.assertNotIn("mock_response", out)

    def test_family_metadata_override(self):
        k = _Key(alias="cursor-u1")
        k.metadata = {"budget_family_overrides": {"other": 0.5}}
        self._seed("tok-default", "other", 0.6)
        d = {"model": "gpt-5.3-codex",
             "messages": [{"role": "user", "content": "hi"}]}
        out = self._pre_fam(d, "acompletion", key=k)
        self.assertIn("额度已用完", out.get("mock_response", ""))

    def test_family_ungated_key_untouched(self):
        self._seed("tok-other", "other", 999.0)
        d = {"model": "gpt-5.3-codex",
             "messages": [{"role": "user", "content": "hi"}]}
        out = self._pre_fam(d, "acompletion",
                            key=_Key(alias="other-user", token="tok-other"))
        self.assertNotIn("mock_response", out)

    def test_family_legacy_gpt53_enforced_for_claude_code(self):
        key = _Key(alias="claude-code-u1", token="tok-legacy")
        self._seed("tok-legacy", "gpt53", 200.01)
        d = {"model": "gpt-5.3-codex",
             "messages": [{"role": "user", "content": "hi"}]}
        out = self._pre_fam(d, "acompletion", key=key)
        self.assertIn("GPT-5.3 系列 额度已用完", out.get("mock_response", ""))


class CodexStopTest(unittest.TestCase):
    """codex goal 模式停机分支:超额时对 codex 客户端回 429 + usage_limit_reached +
    x-codex-promo-message(ASCII),而非 200 mock。非 codex 客户端(Cursor 等)仍 200。

    注:头是否真到 wire、codex 是否真退出 goal 循环,由 198 全链 T0 验证(scripts/
    litellm-198-t0-fullchain.sh + 真 codex 二进制);本处只锁 raise 形状与 promo 内容。"""

    def setUp(self):
        _ensure_stubs()
        M._fam_local.clear()
        M._OVER_BUDGET_MARKS.clear()

    def tearDown(self):
        M._fam_local.clear()
        M._OVER_BUDGET_MARKS.clear()

    def _seed(self, token, fkey, amount):
        M._fam_local[M._family_spend_key(fkey, token)] = amount

    def _codex_data(self, model, where="proxy_server_request", ua="codex_exec/0.5.0"):
        d = {"model": model, "messages": [{"role": "user", "content": "写个函数"}]}
        d[where] = {"headers": {"user-agent": ua}}
        return d

    def _pre_fam(self, data, call_type, key=None, extra_env=None):
        env = {**FAM_ENV, **(extra_env or {})}

        async def go():
            return await M.budget_notice.async_pre_call_hook(
                key or _Key(alias="cursor-u1"), None, data, _CallType(call_type))
        return _with_env(env, lambda: asyncio.run(go()))

    # ---- 客户端识别 ----
    def test_client_is_codex_detects_ua_and_originator(self):
        self.assertTrue(M._client_is_codex(
            {"proxy_server_request": {"headers": {"user-agent": "codex_exec/0.5"}}}))
        self.assertTrue(M._client_is_codex(
            {"metadata": {"headers": {"originator": "codex_cli_rs"}}}))
        self.assertTrue(M._client_is_codex(
            {"litellm_metadata": {"headers": {"User-Agent": "codex/1.0"}}}))
        self.assertFalse(M._client_is_codex(
            {"proxy_server_request": {"headers": {"user-agent": "cursor/0.42"}}}))
        self.assertFalse(M._client_is_codex({"messages": []}))

    # ---- ④ 系列桶:codex → 429 usage_limit_reached + promo 头 ----
    def test_family_codex_raises_usage_limit_with_promo(self):
        from litellm.proxy._types import ProxyException

        # other 桶爆($500.44/$500),Claude 桶还剩($50/$100)
        self._seed("tok-default", "other", 500.44)
        self._seed("tok-default", "claude", 50.0)
        d = self._codex_data("gpt-5.6-sol")
        with self.assertRaises(ProxyException) as ctx:
            self._pre_fam(d, "aresponses")
        e = ctx.exception
        self.assertEqual(e.type, "usage_limit_reached")
        self.assertEqual(str(e.code), "429")
        promo = e.headers.get("x-codex-promo-message", "")
        # 用量数字(超额系列) + 另一系列剩余 + 切换提示
        self.assertIn("500.44", promo)
        self.assertIn("500.00", promo)
        self.assertIn("50.00", promo)          # Claude 剩余 100-50
        self.assertIn("Claude", promo)
        self.assertIn("/model", promo)
        # ASCII-only(HTTP 头 to_str() 只认可见 ASCII)
        self.assertEqual(promo, promo.encode("ascii", "ignore").decode("ascii"))

    def test_family_codex_both_exhausted_no_switch_hint(self):
        from litellm.proxy._types import ProxyException

        self._seed("tok-default", "other", 500.10)
        self._seed("tok-default", "claude", 100.10)   # Claude 也爆
        d = self._codex_data("gpt-5.6-sol")
        with self.assertRaises(ProxyException) as ctx:
            self._pre_fam(d, "aresponses")
        promo = ctx.exception.headers.get("x-codex-promo-message", "")
        self.assertIn("500.10", promo)
        self.assertNotIn("switch model", promo)
        self.assertIn("resets", promo)

    def test_family_codex_blocked_on_claude_hints_other(self):
        from litellm.proxy._types import ProxyException

        self._seed("tok-default", "claude", 100.0)    # Claude 爆
        self._seed("tok-default", "other", 10.0)     # other 还剩很多
        d = self._codex_data("claude-opus-5")
        with self.assertRaises(ProxyException) as ctx:
            self._pre_fam(d, "aresponses")
        promo = ctx.exception.headers.get("x-codex-promo-message", "")
        # 钉的是「这句话必须说清哪几件事」,不钉具体措辞 —— 文案改过一次
        self.assertIn("Claude", promo)               # 爆的是哪条 lane
        self.assertIn("100.00", promo)               # 花了多少
        self.assertIn("100.00", promo)               # 上限多少
        self.assertIn("main-models", promo)          # 还能走哪条
        self.assertIn("490.00", promo)               # 那条还剩多少(500-10)
        self.assertIn("/model", promo)               # 怎么切
        self.assertEqual(promo, promo.encode("ascii", "ignore").decode("ascii"))

    # ---- 非 codex 客户端保持 200 mock(Cursor 吞 429 body) ----
    def test_family_non_codex_still_200_mock(self):
        self._seed("tok-default", "other", 600.0)
        d = {"model": "gpt-5.6-sol",
             "proxy_server_request": {"headers": {"user-agent": "cursor/0.42"}},
             "messages": [{"role": "user", "content": "hi"}]}
        out = self._pre_fam(d, "aresponses")
        self.assertIn("额度已用完", out.get("mock_response", ""))

    # ---- 逃生门 ----
    def test_family_codex_kill_switch_falls_back_to_mock(self):
        self._seed("tok-default", "other", 600.0)
        d = self._codex_data("gpt-5.6-sol")
        out = self._pre_fam(d, "aresponses",
                            extra_env={"BUDGET_CODEX_STOP_DISABLED": "1"})
        self.assertIn("额度已用完", out.get("mock_response", ""))

    # ---- ③ 总额度:codex → 429 total promo ----
    def test_over_budget_codex_raises_usage_limit(self):
        from litellm.proxy._types import ProxyException

        M._mark_over_budget("tok-default", 9.0, 5.0)
        d = self._codex_data("gpt-5.6-sol")
        with self.assertRaises(ProxyException) as ctx:
            self._pre_fam(d, "aresponses")
        e = ctx.exception
        self.assertEqual(e.type, "usage_limit_reached")
        promo = e.headers.get("x-codex-promo-message", "")
        self.assertIn("9.00", promo)
        self.assertIn("5.00", promo)
        # ③ 是 key 总额度,覆盖所有模型 ⇒ 这条分支**不许**暗示"切到 gpt-5.3 就能接着跑"
        # (budget_notice pre_call 只看 over_budget_mark 不看 model,切了照样撞墙)。
        # 所以这里钉两件事:说清它管所有模型 + 不给切模型的 CTA。
        # 原来钉的字面 "total budget" 已被改写,钉字面钉不住这个语义。
        self.assertIn("every model", promo)
        self.assertNotIn("/model", promo)
        self.assertEqual(promo, promo.encode("ascii", "ignore").decode("ascii"))

    def test_over_budget_non_codex_still_200_mock(self):
        M._mark_over_budget("tok-default", 9.0, 5.0)
        d = {"proxy_server_request": {"headers": {"user-agent": "cursor/0.42"}},
             "input": "继续"}
        out = self._pre_fam(d, "aresponses")
        self.assertIn("额度已用完", out.get("mock_response", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
