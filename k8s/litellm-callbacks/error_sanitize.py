"""error_sanitize.py — 对外报错脱敏 + 路由头假名化。

治的病
------
198 生产 proxy 会把内部拓扑随报错和响应头直接交给客户端。2026-08-08 实测到
四条泄漏（前两条在 4 个 pod 的 12h 日志里持续出现）::

    1) RateLimitError.message:
       No deployments available for selected model, Try again in 60 seconds.
       Passed model=chatgpt-gpt-5.6-terra. pre-call-checks=True,
       cooldown_list=['chatgpt-acct-99-gpt-5.6-terra', 'chatgpt-acct-95-...', ...]

    2) APIError.message:
       api_base='http://chatgpt-acct-95.litellm-product.svc.cluster.local:4000'

    3) 流式中途异常 → SSE error 帧里是 str(e) + 完整 traceback，暴露 /app/*.py
       这些回调文件名（common_request_processing.py:2606 的 error_msg 兜底分支）

    4) 响应头（**成功请求也带**，不只报错）：
       x-litellm-model-id: chatgpt-acct-109-gpt-5.6-luna
       x-litellm-model-api-base: http://chatgpt-acct-109....svc.cluster.local:4000

对外只留一句 ``API 异常`` + 一个排障用的 request id。文案里不出现任何与账户、
号、池、上游厂商相关的字眼 —— message 是**整体置换**而非按词删改，所以不存在
"漏掉某个词"的可能；``type`` / ``param`` 这两个仍要保留的字段额外过一遍
``_LEAK_RE`` 清洗兜底。

为什么就地改写异常，而不是 return HTTPException
-----------------------------------------------
``proxy/utils.py:2056`` 的 ``post_call_failure_hook`` 支持回调 return/raise 一个
HTTPException 来替换对外错误，看着正是为此设计的。**但流式路径不能用**：

    # common_request_processing.py:2587 async_streaming_data_generator
    transformed_exception = await proxy_logging_obj.post_call_failure_hook(...)
    if transformed_exception is not None:
        e = transformed_exception
    if isinstance(e, HTTPException):
        raise e                       # ← 连接被硬断，客户端收不到 error 帧
    ...
    yield serialize_error(proxy_exception)   # ← 正常应该走这里

return HTTPException 会让流式请求从"收到一个 error 帧"退化成"连接无声中断"，
比不脱敏更糟。就地改写 ``.message`` 之后两条路径行为一致：非流式的
``_handle_llm_api_exception``（:2270）和流式的 generator 最终都用
``getattr(e, "message", ...)`` 构造对外的 ``ProxyException``。所以本模块改完
一律 ``return None``。

四个属性都得改，少一个就漏
--------------------------
下游取对外文案的口子不止 ``.message``，实测/读码确认这四条路都存在：

===========================  ==========================================
``.message``                 ProxyException / litellm 各 Error 类的主口
``.detail``                  HTTPException 分支走 getattr(e, "detail")
``.args``（即 ``str(e)``）   ``_handle_llm_api_exception`` 的
                             has_attribute_error 分支（:2394）和
                             ``auth_exception_handler.py:184`` 的
                             ``"Authentication Error, " + str(e)`` 都绕过
                             ``.message`` 直接用 str(e)
``.provider_specific_fields``实测 400 响应里它把 message 原样回显了一遍
===========================  ==========================================

保留 ``status_code`` / ``type`` / ``code``：客户端 SDK 的重试语义靠它们
（429 可重试 vs 401 别重试），生产回归脚本
``litellm-pro-gpt-products-regression.sh`` 的 ``hidden_access_denied()`` 也靠
``type`` 里的 ``key_model_access_denied``。这三个字段是 OpenAI 标准枚举，不含
拓扑信息。

排障能力没丢（这是本模块能上线的前提）
--------------------------------------
``_log_llm_api_exception(e)`` 和 ``verbose_proxy_logger.exception(...)`` 都在
本 hook **之前**执行；SpendLogs 的 ``metadata->error_information->error_message``
由 LLM 调用层的 failure handler 写入，更早。所以 pod 日志和 DB 里仍是原始报错，
只有出网那一份被换掉。

也不影响路由：``post_call_failure_hook`` 在 router 重试 + fallback 全部耗尽之后
才跑，proxy 内部按错误文本判断的逻辑（``streaming_output_backfill.py`` 的
capacity→MidStreamFallbackError 升级、``chatgpt_responses_normalize.py`` 的
retryable needles）都排在我们前面。**推论：绝不能把脱敏改成 exception 类的
monkey-patch 或 SSE 帧过滤器** —— 那会跑到它们前面，把同组 failover 打死。

为什么 x-litellm-model-id 用假名而不是固定占位符
-----------------------------------------------
这个头有两个自动化消费者把"两次调用的值是否相等"当判据：
``litellm-encrypted-content-affinity/scripts/probe-affinity.py:84``
（``verdict_pass = encitem_found and mid1 == mid2``）和
``scripts/litellm-sticky-verify.sh``。换成常量占位符会让等式**恒真** ——
一个坏掉的 sticky router 会被报成 PASS，比不脱敏更危险。

HMAC 假名保持「同 deployment → 同值，不同 → 不同」，这两个探针语义不变，对外
又看不出是哪台。salt 必须是**跨 pod 稳定的常量**：4 个 replica 各算各的话，
跨 pod 的粘连判断会失真。要反查真实 deployment 用
``scripts/litellm-model-id-resolve.py``（走 /model/info 管理端点，不经过本模块）。

覆盖不到的（不当作已解决）
--------------------------
* passthrough 路由：``pass_through_endpoints.py`` 调 failure hook 但**丢弃返回
  值**，且它把上游 body 原样透传。2026-08-08 的 12h 流量里没有这类路径。
  （它的响应头**有**走 ``post_call_response_headers_hook``，但**没**走
  ``get_custom_headers``，所以那条路上 ``llm_provider-*`` 删不掉，只有覆盖生效。）
* ``httpx.HTTPStatusError``：``_handle_llm_api_exception`` 有一条
  ``detail={"error": error_text}`` 的分支会原样透传上游 body（:2377）。它在
  isinstance 链里排在 HTTPException 之后、且早于我们能影响的通用分支，本 hook
  管不到。12h 日志里出现 **0** 次，故不为它写脆弱的 response._content 改写。
* 响应 **body** 里的 ``"model"`` 字段仍是真实模型名（客户要 gpt-5.5、body 回
  deepseek-v4-flash 就暴露了替换）。那是 OpenAI 协议的功能字段，客户端可能校验，
  改它风险另算，本模块不碰。
* nginx / uvicorn 层的 5xx（proxy 进程挂了、连接超时）不经过 LiteLLM 回调。
* ⚠️ ``litellm_settings.return_response_headers`` 是**死配置** —— 实查安装的
  litellm 源码，这个键零读取点。别指望关它能挡住什么。

Activation
----------
默认 **ON**（这是安全默认：漏出去比看不清更贵）。逃生门::

    ERROR_SANITIZE_DISABLED=1          整体停用（per-call 读，kubectl set env 秒级生效）
    ERROR_SANITIZE_HEADERS_DISABLED=1  只停响应头假名化，报错仍脱敏
    ERROR_SANITIZE_MESSAGE             自定义对外文案，支持 {rid} 占位
    ERROR_SANITIZE_ID_SALT             假名 salt（换了之后历史假名对不上，别随手改）
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import logging
import os
import re
from typing import Any, Dict, List, Optional

from litellm.integrations.custom_logger import CustomLogger

_log = logging.getLogger("error_sanitize")

# 198 proxy 只吐 WARNING 及以上（见 .cursor/skills/litellm-hook-dev/SKILL.md
# §"_log.info 在 198 proxy 上看不见"），所以可观测事件一律 warning。

# 对外文案。整体置换，不做按词删改 —— 用户要求"不要有任何账户池的字眼"，
# 置换是唯一能给出这个保证的做法（删改永远可能漏词）。
_DEFAULT_MESSAGE = "API 异常"
_MESSAGE_TMPL = os.environ.get("ERROR_SANITIZE_MESSAGE") or "API 异常 (req: {rid})"

# 假名 salt。**必须是跨 pod 稳定的常量**，理由见模块 docstring。
_ID_SALT = (os.environ.get("ERROR_SANITIZE_ID_SALT")
            or "carher-198-model-id-pseudonym-v1").encode("utf-8")

# 仍要保留的字段（type / param）的兜底清洗。命中任一即整体替换成通用值。
# 正常取值是 OpenAI 标准枚举（invalid_request_error / rate_limit_error /
# key_model_access_denied / budget_exceeded / token_not_found_in_db / None），
# 一个都不该命中；这里纯属防御 —— 万一某个 provider 把机器名塞进 type。
_LEAK_RE = re.compile(
    r"(?:"
    r"chatgpt-acct|zerokey|wangsu|openrouter|acct-\d+"
    r"|svc\.cluster\.local|litellm-(?:product|dev|staging)"
    r"|10\.68\.13\.\d+|\bapi_base\b|cooldown_list|deployment"
    # key 物料。第三条（裸 hex）是 2026-08-08 实测 401 body 里的 Key Hash；
    # 第一条要能吃下被 litellm 打码成 ``sk-...-key`` 的形态（带点号）。
    r"|sk-[A-Za-z0-9._-]{3,}"
    r"|Key Hash|API Key"
    r"|\b[0-9a-f]{32,}\b"
    r"|账[户号]|号池|池子"
    r")",
    re.IGNORECASE,
)

_GENERIC_TYPE = "api_error"

# 原文寄存在异常对象上的属性名。LiteLLM 不读它，只有本模块给
# get_error_information 打的补丁读（让入库那份保真，出网那份仍脱敏）。
_ORIGINAL_ATTR = "_error_sanitize_original"

# 要覆盖掉的路由头。值在 build_safe_headers() 里算。
_H_MODEL_ID = "x-litellm-model-id"
_H_API_BASE = "x-litellm-model-api-base"
_H_MODEL_GROUP = "x-litellm-model-group"
_API_BASE_PLACEHOLDER = "-"

# ── 响应头：整类删掉的 ──────────────────────────────────────────────
# `llm_provider-*` 是上游响应头被原样加前缀转发出来的（core_helpers.py:275 的
# else 分支：任何不认识的上游头都变成 llm_provider-<k>）。2026-08-08 实测一次
# 池子流量（chatgpt-gpt-5.5）的出网头，里面有：
#
#   llm_provider-x-codex-plan-type: pro
#   llm_provider-x-codex-primary-used-percent: 9
#   llm_provider-x-codex-primary-reset-at: 1786767383
#   llm_provider-x-codex-active-limit: premium
#   llm_provider-x-codex-credits-balance: 0
#   llm_provider-set-cookie: __oailb=<JWT>   ← OpenAI edge-gateway 会话 cookie
#   llm_provider-x-litellm-model-api-base: https://chatgpt.com/backend-api/codex
#   llm_provider-x-litellm-model-group: chatgpt-gpt-5.5
#   llm_provider-cf-ray / cf-cache-status
#
# 即：账号的订阅档位 + 配额消耗 + 重置时刻 + 一个上游 JWT cookie，全都在对外发。
# 注意最后两条 —— 池子上游本身就是一台 LiteLLM，它自己的 x-litellm-* 头会走
# else 分支被加上 llm_provider- 前缀转发，**绕过本模块对 x-litellm-model-id 的
# 假名化**。所以整个前缀必须删，不能只挑几个。
_DROP_HEADER_PREFIXES = ("llm_provider-",)

# 逐个点名删的：花费 / 利润率 / 预算 / 限额 / 版本 / 缓存键。
# `-margin-percent` 和 `-margin-amount` 是我们的加价率，本来就不该给客户看。
# 实查：这些头在 scripts/ backend/ frontend/ operator-go/ 和三个 skills 目录里
# **零消费者**，删掉不打断任何东西。
_DROP_HEADERS = frozenset({
    "x-litellm-response-cost",
    "x-litellm-response-cost-original",
    "x-litellm-response-cost-discount-amount",
    "x-litellm-response-cost-margin-amount",
    "x-litellm-response-cost-margin-percent",
    "x-litellm-key-spend",
    "x-litellm-key-max-budget",
    "x-litellm-key-tpm-limit",
    "x-litellm-key-rpm-limit",
    "x-litellm-cache-key",
    "x-litellm-version",
    "x-litellm-model-region",
})

# 明确保留（写出来是为了让"为什么没删这个"有据可查）：
#   x-litellm-call-id            报障关联 id，脱敏文案里的 rid 就是它
#   x-litellm-attempted-fallbacks / -retries
#                                quota-rebalance.py:1080 靠它决定 acct 暂停还是保留在线
#   x-ratelimit-*                OpenAI 标准限流头，客户端 SDK 退避靠它
#                                （它们走 OPENAI_RESPONSE_HEADERS 分支，不带 llm_provider- 前缀）
#   retry-after                  429 退避
#   x-litellm-*-duration-ms      纯性能计时，无拓扑信息


def should_drop_header(name: Any) -> bool:
    try:
        low = str(name).lower()
    except Exception:
        return False
    if low in _DROP_HEADERS:
        return True
    return any(low.startswith(p) for p in _DROP_HEADER_PREFIXES)


def sanitize_header_map(headers: Any) -> dict:
    """删掉泄漏拓扑的响应头，并把剩下的 model-id / model-group 换成假名。

    在 ``get_custom_headers`` 的返回值上跑（那是成功/失败两条路径构造响应头的
    唯一汇聚点），所以这里能**真删**；回调 hook 只能覆盖不能删，两者互补。
    """
    if not isinstance(headers, dict):
        return headers
    out = {}
    for k, v in headers.items():
        if should_drop_header(k):
            continue
        low = str(k).lower()
        if low == _H_MODEL_ID and v:
            out[k] = pseudonym(v)
        elif low == _H_MODEL_GROUP and v:
            # 组名本身就带池子字眼（chatgpt-gpt-5.5 / zerokey-pool-gpt-5.5），
            # 而且会暴露"客户要的 gpt-5.5 其实由谁承接"。同样走假名，
            # scripts/litellm-model-id-resolve.py 可反查。
            out[k] = pseudonym(v)
        elif low == _H_API_BASE:
            out[k] = _API_BASE_PLACEHOLDER
        else:
            out[k] = v
    return out


def _install_custom_headers_patch() -> bool:
    """在 get_custom_headers 出口处删头。幂等。"""
    try:
        from litellm.proxy.common_request_processing import (
            ProxyBaseLLMRequestProcessing as _P,
        )
    except Exception as exc:
        _log.warning("error_sanitize: cannot import ProxyBaseLLMRequestProcessing: %r",
                     exc)
        return False

    orig = getattr(_P, "get_custom_headers", None)
    if orig is None:
        _log.warning("error_sanitize: get_custom_headers not found, headers NOT dropped")
        return False
    if getattr(orig, "_error_sanitize_patched", False):
        return True

    def patched(*args: Any, **kwargs: Any) -> Any:
        headers = orig(*args, **kwargs)
        try:
            if disabled() or headers_disabled():
                return headers
            return sanitize_header_map(headers)
        except Exception:
            # 出口处的补丁绝不能把正常响应弄挂 —— 宁可这一次没删干净
            return headers

    patched._error_sanitize_patched = True  # type: ignore[attr-defined]
    try:
        _P.get_custom_headers = staticmethod(patched)  # type: ignore[assignment]
    except Exception as exc:
        _log.warning("error_sanitize: failed to patch get_custom_headers: %r", exc)
        return False
    return True


def disabled() -> bool:
    """整体 kill switch。per-call 读，所以 kubectl set env 立刻生效。"""
    return os.environ.get("ERROR_SANITIZE_DISABLED") == "1"


def headers_disabled() -> bool:
    return os.environ.get("ERROR_SANITIZE_HEADERS_DISABLED") == "1"


def request_id(request_data: Any) -> str:
    """8 位关联 id。

    取 ``litellm_call_id`` —— 与客户端已经收到的 ``x-litellm-call-id`` 响应头
    **同源同值**（``get_custom_headers`` 用的就是它），所以用户报这一串我们能在
    pod 日志里直接定位。取不到时给 ``-``，不编造。
    """
    try:
        if isinstance(request_data, dict):
            cid = request_data.get("litellm_call_id") or ""
        else:
            cid = ""
    except Exception:
        cid = ""
    return str(cid)[:8] or "-"


def public_message(rid: str) -> str:
    try:
        return _MESSAGE_TMPL.format(rid=rid)
    except Exception:
        # 自定义模板写坏了也不能让请求少一份对外文案
        return _DEFAULT_MESSAGE


def budget_public_message(exc: Any, user_api_key_dict: Any, rid: str) -> Optional[str]:
    """BudgetExceededError 的对外文案豁免分支（2026-08-19，budget_notice 三件套之③）。

    预算 429 在整体置换策略下变成了「API 异常」，用户不知道自己是限额用完了
    （更不知道明天自己会好）。这类异常是 litellm auth 层自己生成的，message 里
    只有 Spend/Limit 数字、没有任何内部拓扑，所以给它专属的友好中文文案是安全
    的 —— 文案由本函数从零拼出，不透传原文的任何片段。

    仅豁免文案，置换机制不变：返回的 msg 仍走 sanitize_exception 的四属性
    就地改写，429 状态码与 type=budget_exceeded 原样保留。

    注意 team/user/org 级 BudgetExceededError 也会走到这里；198 现网只用
    key 级预算，reset 时间取自 key 的 budget_reset_at，对其余 scope 略有
    偏差但语义仍对（都是"额度用完、周期后恢复"）。
    """
    try:
        if os.environ.get("BUDGET_FRIENDLY_429_DISABLED") == "1":
            return None
        if type(exc).__name__ != "BudgetExceededError":
            return None
        cost = getattr(exc, "current_cost", None)
        limit = getattr(exc, "max_budget", None)
        nums = (
            f"：已消费 ${float(cost):.2f} / 限额 ${float(limit):.2f}"
            if cost is not None and limit is not None else ""
        )
        reset = getattr(user_api_key_dict, "budget_reset_at", None)
        if isinstance(reset, datetime.datetime):  # DB 裸 UTC
            bj = reset + datetime.timedelta(hours=8)
            when = f"北京时间 {bj.month:02d}-{bj.day:02d} {bj.hour:02d}:{bj.minute:02d}"
        else:
            when = "额度周期结束后"
        return (
            f"今日 key 额度已用完{nums}，{when}自动重置。"
            f"可发送 /查余额 查询用量。(req: {rid})"
        )
    except Exception:
        return None


def context_window_public_message(exc: Any, rid: str) -> Optional[str]:
    """ContextWindowExceededError 的对外文案豁免分支（2026-09-19）。

    起因：wanglihua 09-18 11:25 在 OWUI 里把上下文堆到 547720 token，撞上
    grok-4.6 的 500000 输入上限。这个异常是 litellm router 自己在
    ``_pre_call_checks`` 里抛的（router.py:10762），**在选中任何 deployment
    之前** —— 所以它的 message 里只有 "Model=<对外模型名>, Max Input
    Tokens=<数字>, Got=<数字>"，没有 api_base、没有落点、没有 key 物料。
    但整体置换把它打成「API 异常」之后，用户只知道"坏了"，不知道是自己聊得
    太长了、更不知道开个新会话就能好，只会来问我们。

    和 budget_public_message 同一套规矩：文案由本函数从零拼出，**不透传原文
    的任何片段**，只用正则把两个纯数字抓出来（数字本身不是内部拓扑）。
    抓不到就退化成不带数字的版本，绝不把原 message 拼进去。

    仅豁免文案，置换机制不变：返回的 msg 仍走 sanitize_exception 的四属性
    就地改写，400 状态码与异常类型原样保留。

    文案用词避开 _LEAK_RE（账户/账号/池子/deployment 都会被 contains_leak
    命中，那样这句话自己就会被 scrub 成通用文案）。

    kill switch：CONTEXT_WINDOW_FRIENDLY_MSG_DISABLED=1
    """
    try:
        if os.environ.get("CONTEXT_WINDOW_FRIENDLY_MSG_DISABLED") == "1":
            return None
        if type(exc).__name__ != "ContextWindowExceededError":
            return None
        raw = ""
        try:
            raw = str(getattr(exc, "message", "") or "")
        except Exception:
            raw = ""
        got = re.search(r"Got\s*[=:]\s*(\d+)", raw, re.IGNORECASE)
        cap = re.search(r"Max\s*Input\s*Tokens\s*[=:]\s*(\d+)", raw, re.IGNORECASE)
        if got and cap:
            nums = f"（本次约 {int(got.group(1)):,} token，上限 {int(cap.group(1)):,}）"
        else:
            nums = ""
        return (
            f"当前对话的上下文超出该模型的输入上限{nums}。"
            f"请新建一个会话，或删掉前面的部分历史后重试。"
            f"(req: {rid})"
        )
    except Exception:
        return None


def contains_leak(value: Any) -> bool:
    try:
        return bool(_LEAK_RE.search(str(value)))
    except Exception:
        return False


def scrub_field(value: Any, generic: str) -> Any:
    """只在命中敏感词时替换，否则原样保留（保住 SDK 重试语义）。"""
    if value is None:
        return None
    return generic if contains_leak(value) else value


def pseudonym(model_id: Any) -> str:
    """deployment id → 12 位稳定假名。同 id 同值，不同 id 不同值，不可逆。"""
    try:
        raw = str(model_id)
    except Exception:
        return "-"
    if not raw:
        return "-"
    return hmac.new(_ID_SALT, raw.encode("utf-8", "replace"),
                    hashlib.sha256).hexdigest()[:12]


def original_text(exc: Any) -> str:
    """脱敏前的原始文案，取最长的那份（不同异常类的信息量不在同一个属性上）。"""
    cands = []
    for attr in ("message", "detail"):
        try:
            v = getattr(exc, attr, None)
            if v:
                cands.append(str(v))
        except Exception:
            pass
    try:
        cands.append(str(exc))
    except Exception:
        pass
    return max(cands, key=len) if cands else ""


def stash_original(exc: Any, original: str) -> bool:
    """把原文挂到异常对象上，供 ``get_error_information`` 补丁取回。

    为什么需要这一步：2026-08-08 实测，SpendLogs 的
    ``metadata->error_information->>error_message`` 是在本 hook **之后**才序列化的
    （``get_error_information(original_exception)`` 读到的是已被改写的对象），所以
    单纯就地改写会把 DB 里那一列也一起抹掉 —— 而 ``litellm-key-activity.sh``
    和 her 排障 SOP 都靠它定根因。改前那条 ``No api key passed in.`` 与改后整片
    ``API 异常`` 的对照就是证据。

    ⚠️ 走过一条死路，别再试：往 ``request_data["metadata"]`` 里塞
    ``error_sanitize_original`` **进不了 DB**。SpendLogs 的 metadata 列是按
    ``StandardLoggingMetadata`` 白名单构造的，不收自定义键（2026-08-08 实测，
    8/8 行 ``<MISSING>``）。
    """
    if not original:
        return False
    try:
        setattr(exc, _ORIGINAL_ATTR, original)
        return True
    except Exception:
        return False


def _install_error_information_patch() -> bool:
    """让入库那一份错误信息取回原文；出网那一份不受影响。

    只改 ``error_message`` 一个字段，``error_class`` / ``error_code`` /
    traceback 全部沿用原实现。幂等（函数属性 guard），照
    ``encrypted_content_degrade_strip.py`` 的既有写法。
    """
    try:
        from litellm.litellm_core_utils.litellm_logging import (
            StandardLoggingPayloadSetup as _Setup,
        )
    except Exception as exc:
        _log.warning("error_sanitize: cannot import StandardLoggingPayloadSetup: %r",
                     exc)
        return False

    orig = getattr(_Setup, "get_error_information", None)
    if orig is None:
        _log.warning("error_sanitize: get_error_information not found, DB keeps masked text")
        return False
    if getattr(orig, "_error_sanitize_patched", False):
        return True

    def patched(*args: Any, **kwargs: Any) -> Any:
        info = orig(*args, **kwargs)
        try:
            exc = kwargs.get("original_exception")
            if exc is None and args:
                # 兼容位置调用
                exc = args[0]
            stashed = getattr(exc, _ORIGINAL_ATTR, None)
            if stashed and isinstance(info, dict):
                info["error_message"] = stashed
        except Exception:
            pass
        return info

    patched._error_sanitize_patched = True  # type: ignore[attr-defined]
    try:
        _Setup.get_error_information = staticmethod(patched)  # type: ignore[assignment]
    except Exception as exc:
        _log.warning("error_sanitize: failed to patch get_error_information: %r", exc)
        return False
    return True


def sanitize_exception(exc: Any, msg: str) -> List[str]:
    """就地把 exc 的对外文案换成 msg。返回实际改掉的属性名（供日志/测试）。

    每个属性单独 try —— 异常对象可能是 __slots__ 的、可能重写了
    __setattr__，任何一个改不动都不该拖累其余三个。本函数**永不抛**。
    """
    changed: List[str] = []

    # 1) .message —— ProxyException / litellm 各 Error 类的主口
    try:
        setattr(exc, "message", msg)
        changed.append("message")
    except Exception:
        pass

    # 2) .args —— 决定 str(e)。has_attribute_error 分支和 auth 兜底分支绕过
    #    .message 直接用 str(e)，所以这条不是可选项。
    try:
        exc.args = (msg,)
        changed.append("args")
    except Exception:
        pass

    # 3) .detail —— 只在本来就有时改。HTTPException 分支读它；给一个没有 detail
    #    的异常凭空加上，反而可能让上游走进不该走的分支。
    try:
        if hasattr(exc, "detail"):
            exc.detail = msg
            changed.append("detail")
    except Exception:
        pass

    # 4) .provider_specific_fields —— 实测会把 message 原样回显
    try:
        if hasattr(exc, "provider_specific_fields"):
            exc.provider_specific_fields = None
            changed.append("provider_specific_fields")
    except Exception:
        pass

    # 保留但清洗：type / param。正常不该命中，命中说明有 provider 往里塞了东西。
    for attr, generic in (("type", _GENERIC_TYPE), ("param", None)):
        try:
            cur = getattr(exc, attr, None)
            if cur is not None and contains_leak(cur):
                setattr(exc, attr, generic)
                changed.append(attr)
        except Exception:
            pass

    return changed


def build_safe_headers(litellm_call_info: Optional[Dict[str, Any]],
                       data: Any = None) -> Dict[str, str]:
    """替换两个泄漏拓扑的路由头。

    hook 只能覆盖不能删除（``utils.py:2490`` 是 ``merged_headers.update(result)``），
    所以给占位符 / 假名而不是移除。
    """
    model_id: Any = None
    if isinstance(litellm_call_info, dict):
        model_id = litellm_call_info.get("model_id")
    if not model_id and isinstance(data, dict):
        # 拿不到 litellm_call_info 时的退路（旧签名 backwards-compat 分支）
        for meta_key in ("metadata", "litellm_metadata"):
            meta = data.get(meta_key)
            if isinstance(meta, dict):
                mi = meta.get("model_info")
                if isinstance(mi, dict) and mi.get("id"):
                    model_id = mi["id"]
                    break
    return {
        _H_MODEL_ID: pseudonym(model_id) if model_id else "-",
        _H_API_BASE: _API_BASE_PLACEHOLDER,
    }


class ErrorSanitize(CustomLogger):
    async def async_post_call_failure_hook(
        self,
        request_data: dict,
        original_exception: Exception,
        user_api_key_dict: Any = None,
        traceback_str: Optional[str] = None,
    ) -> None:
        """就地脱敏，永远 return None（理由见模块 docstring）。"""
        try:
            if disabled():
                return None
            # ModifyResponseException 是"合成 200 响应"的载体（guardrail 拦截、
            # budget_notice 的 /查余额 短路都靠它），message 由我们自己的 hook
            # 生成、随后会原样作为正文展示给用户 —— 整体置换会把功能文案打成
            # 「API 异常」（2026-08-19 T0 实测）。仅当文案命中泄漏正则时才照旧
            # 脱敏（防御未来某个 hook 把上游原文塞进 message）。
            if type(original_exception).__name__ == "ModifyResponseException" \
                    and not contains_leak(getattr(original_exception, "message", "")):
                return None
            rid = request_id(request_data)
            msg = (
                budget_public_message(original_exception, user_api_key_dict, rid)
                or context_window_public_message(original_exception, rid)
                or public_message(rid)
            )
            original = original_text(original_exception)
            stashed = stash_original(original_exception, original)
            changed = sanitize_exception(original_exception, msg)
            # 原文必须留一份可检索的。两条回溯路径，都用同一个 rid 串起来
            # （客户端拿到的 x-litellm-call-id 就是它）：这行 WARNING 日志，
            # 以及 SpendLogs 的 error_information.error_message（靠上面那个补丁保真）。
            _log.warning(
                "error_sanitize: masked req=%s cls=%s attrs=%s stash=%s original=%r",
                rid, type(original_exception).__name__,
                ",".join(changed) or "none", stashed, original[:600],
            )
        except Exception as exc:
            # 兜到这里说明脱敏本身炸了 —— 报错会以原文出网。必须显式喊出来，
            # 否则就是"静默失效"：看着装上了，其实一直在漏。
            try:
                _log.warning("error_sanitize: FAILED TO MASK, raw error goes out: %r",
                             exc)
            except Exception:
                pass
        return None

    async def async_post_call_response_headers_hook(
        self,
        data: dict,
        user_api_key_dict: Any = None,
        response: Any = None,
        request_headers: Optional[Dict[str, str]] = None,
        litellm_call_info: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, str]]:
        try:
            if disabled() or headers_disabled():
                return None
            return build_safe_headers(litellm_call_info, data)
        except Exception as exc:
            try:
                _log.warning("error_sanitize: headers hook error: %r", exc)
            except Exception:
                pass
        return None


error_sanitize = ErrorSanitize()
_ERRINFO_PATCHED = _install_error_information_patch()
_HEADERS_PATCHED = _install_custom_headers_patch()
_log.warning(
    "error_sanitize: loaded, msg_tmpl=%r salt_len=%d errinfo_patch=%s hdr_patch=%s "
    "(ERROR_SANITIZE_DISABLED=1 to disable)",
    _MESSAGE_TMPL, len(_ID_SALT), _ERRINFO_PATCHED, _HEADERS_PATCHED,
)
