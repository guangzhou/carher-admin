"""budget_notice.py — 198 LiteLLM key 日额度可见性（2026-08-19）

治的病
------
key 日额度（claude-code-* $70/天、cursor-* $100/天，北京 0 点重置）用完前没有
任何预警；用完后的 429 又被 error_sanitize 整体置换成「API 异常」，用户第一次
知道自己超额就是一脸懵逼的报错。三件套：

① ``/查余额`` | ``/quota`` —— pre-call 短路，不打上游、零计费，直接返回本 key
   今日用量和当前 key 可访问的模型配置（context / 输入上限 / 输出上限）。chat /
   responses 路径用 ``data["mock_response"]``（mock_heartbeat
   同款机制，198 已在产验证）；anthropic ``/v1/messages`` 路径抛
   ``ModifyResponseException``（路由对它有原生 200 + stream 处理；
   ``litellm_params.mock_response`` 在该路径返回非流式对象，与 Claude Code 的
   stream:true 错配）。
② 已用 ≥ WARN_RATIO（默认 90%）且未超限 —— 把一段提醒注入**本次响应最后一个
   text block 内部**（其 content_block_stop 之前；Claude Code 只把最后一个
   text block 当结果展示，追加独立尾块会顶掉真正的答案，2026-08-19 canary
   实测）。每 key 每北京日最多一次（redis 去重，解析链照抄 weighted_affinity
   的四级 fallback；拿不到 redis 退化为 per-pod 内存去重）。该轮没有可注入的
   text block（如以 tool_use 结尾）时释放当日名额，下一请求再试。
③ 100% 超预算 —— **软拦截成 200 友好文案**（2026-08-21）。预算拒绝原本在
   auth 层抛 BudgetExceededError（429），但 **Cursor agent 收到 429 后只显示
   它自己的 ``exceeded retry limit`` 串、根本不渲染响应体**（2026-08-21 活体
   探针实证：proxy 返回的友好 429 body 用户一个字都看不到）。唯一能让 Cursor
   用户看到「额度用完」的办法是把它变成 200 的助手消息（和 ② 同款、prod 已验
   证会渲染）。做法：monkey-patch ``_virtual_key_max_budget_check``，gated key
   超预算时吞掉原生 429 + 打 mark（module dict，恢复时清、TTL 兜底），让请求
   走到 pre-call；pre-call 读 mark，可 mock 的推理路由返回 ``mock_response`` /
   ``ModifyResponseException`` 的「额度已用完」文案（零上游、零计费），非 mock
   的 call_type 兜底重抛 BudgetExceededError（绝不放行到上游）。
   error_sanitize.py 的 BudgetExceededError 友好 429 分支仍保留 —— 非 gated key
   （carher-* bot 等）和 ``BUDGET_FRIENDLY_MOCK_DISABLED=1`` 止血时走那条。
④ 模型系列日额度（2026-08-21，默认关）—— 每 key 每北京日按**系列**限额：
   Cursor key 使用 Claude 家族（Fable/Opus/Sonnet/Haiku）$100、其他所有模型
   （含 GPT-5.3、gpt/deepseek/glm…）共用 $500；其他 gated key 仍保留 GPT-5.3
   独立桶 $200（env 可调、key metadata 可按人覆盖）。
   litellm 原生 model_max_budget 精确匹配无系列桶且滚动 24h 窗，故自建：
   success 事件按 (family, token, 北京日) redis 累加，pre_call 查桶超限出
   200 mock「该系列额度已用完，其他系列不受影响」。/查余额 附带各系列用量。

匹配规则（①）
--------------
取最后一条 user 消息的文本，**按行 strip 后全等**命中 {"/查余额","查余额",
"/quota"}（quota 不分大小写）才触发。按行而非按整条消息，是因为 Cursor /
Claude Code 都会把用户输入包进模板（文件上下文、system-reminder 等），整条
全等永远不命中；按行全等又比子串包含安全 —— 正文里聊到「查余额」三个字不会
误触发（那得独占一行才行，且误触发的代价只是收到一条用量而非模型回答）。

Gate（env，改 deploy env 即生效，无需改代码）
---------------------------------------------
BUDGET_NOTICE_DISABLED=1                       总开关（紧急止血，含 ③④）
BUDGET_FRIENDLY_MOCK_DISABLED=1                关掉 ③ 的 200 软拦截（回退到
                                              error_sanitize 的友好 429 body；
                                              ① ② 不受影响）
BUDGET_FAMILY_ENABLED=1                        启用 ④ 系列额度（默认关——阿里云
                                              不设即零行为变化）
BUDGET_FAMILY_GPT53_USD=200                    非 Cursor gated key 的 GPT-5.3 限额
BUDGET_FAMILY_CLAUDE_USD=100                   ④ Claude 家族每日限额
BUDGET_FAMILY_OTHER_USD=500                    ④ 其他所有模型每日限额（Cursor 含 GPT-5.3）
BUDGET_NOTICE_KEY_ALIASES=a,b                  精确灰度名单
BUDGET_NOTICE_KEY_PREFIXES=claude-code-,cursor-  前缀放量
两个 env 都未设置时默认 canary：claude-code-liuguoxian-50gj。
BUDGET_NOTICE_WARN_RATIO=0.9                   ② 的阈值

keep two in sync：本文件与 198 CM ``litellm-callbacks`` 的 ``budget_notice.py``。
"""

from __future__ import annotations

import datetime
import asyncio
import json
import logging
import os
import re as _re
import time
from typing import Any, List, Optional, Tuple

from litellm.integrations.custom_logger import CustomLogger

_log = logging.getLogger("budget_notice")

_MODEL_CATALOG_CACHE_TTL = 300.0
_TTFT_CACHE_TTL = 60.0
_MODEL_CATALOG_CACHE: dict = {}
_TTFT_CACHE: dict = {}

# ---------------------------------------------------------------- env gates

_DEFAULT_ALIASES = frozenset({"claude-code-liuguoxian-50gj"})


def _disabled() -> bool:
    return os.environ.get("BUDGET_NOTICE_DISABLED") == "1"


def _friendly_mock_disabled() -> bool:
    """③ 软拦截（100% → 200 助手消息）的独立止血。开启后 patch 恢复原生 429，
    ① /查余额 与 ② 90% 提醒不受影响。"""
    return os.environ.get("BUDGET_FRIENDLY_MOCK_DISABLED") == "1"


def _gate_aliases() -> set:
    raw = os.environ.get("BUDGET_NOTICE_KEY_ALIASES")
    if raw is None:
        if os.environ.get("BUDGET_NOTICE_KEY_PREFIXES") is None:
            return set(_DEFAULT_ALIASES)
        return set()
    return {x.strip() for x in raw.split(",") if x.strip()}


def _gate_prefixes() -> Tuple[str, ...]:
    raw = os.environ.get("BUDGET_NOTICE_KEY_PREFIXES")
    if raw is None:
        return ()
    return tuple(x.strip() for x in raw.split(",") if x.strip())


def _warn_ratio() -> float:
    try:
        return float(os.environ.get("BUDGET_NOTICE_WARN_RATIO", "0.9"))
    except Exception:
        return 0.9


def _gated(user_api_key_dict: Any) -> bool:
    alias = getattr(user_api_key_dict, "key_alias", None) or ""
    if not alias:
        return False
    if alias in _gate_aliases():
        return True
    return any(alias.startswith(p) for p in _gate_prefixes())


# ------------------------------------------------------- request text 提取

_TRIGGERS_EXACT = ("/查余额", "查余额")
_TRIGGERS_CI = ("/quota",)
# Cursor 等客户端会把用户输入包成单行 <user_query>查余额</user_query>；剥掉
# XML 风格标签后整行仍须全等，精确度不降。
_TAG_RE = _re.compile(r"<[^<>]{1,64}>")


def _debug_aliases() -> set:
    raw = os.environ.get("BUDGET_NOTICE_DEBUG_LOG_ALIASES", "")
    return {x.strip() for x in raw.split(",") if x.strip()}


def _block_texts(content: Any) -> List[str]:
    """content 可能是 str，也可能是多段 block（chat/anthropic 的 text、
    responses 的 input_text）。返回全部文本段。"""
    if isinstance(content, str):
        return [content]
    out: List[str] = []
    if isinstance(content, list):
        for seg in content:
            if isinstance(seg, str):
                out.append(seg)
            elif isinstance(seg, dict):
                t = seg.get("text") or seg.get("input_text")
                if isinstance(t, str):
                    out.append(t)
    return out


def _last_user_text(data: dict) -> str:
    """从尾向前找第一条 role==user 的消息，返回其全部文本段（\\n 连接）。
    同时兼容 chat/anthropic 的 ``messages`` 与 responses 的 ``input``。"""
    for field in ("messages", "input"):
        seq = data.get(field)
        if isinstance(seq, str) and field == "input":
            return seq
        if not isinstance(seq, list):
            continue
        for item in reversed(seq):
            if isinstance(item, dict) and item.get("role") == "user":
                return "\n".join(_block_texts(item.get("content")))
    return ""


def _is_quota_query(text: str) -> bool:
    if not text or len(text) > 20000:
        return False
    for line in text.splitlines():
        s = line.strip()
        for cand in (s, _TAG_RE.sub("", s).strip()):
            if cand in _TRIGGERS_EXACT:
                return True
            if cand.lower() in _TRIGGERS_CI:
                return True
    return False


# ------------------------------------------------------------- 用量文案

def _beijing_reset_str(user_api_key_dict: Any) -> str:
    """budget_reset_at 在 DB / auth 对象里是裸 UTC 的 naive datetime。"""
    reset = getattr(user_api_key_dict, "budget_reset_at", None)
    if not isinstance(reset, datetime.datetime):
        return "北京时间明日 00:00"
    bj = reset + datetime.timedelta(hours=8)
    return f"北京时间 {bj.month:02d}-{bj.day:02d} {bj.hour:02d}:{bj.minute:02d}"


def _usage_text(user_api_key_dict: Any) -> str:
    alias = getattr(user_api_key_dict, "key_alias", None) or "（无别名）"
    spend = float(getattr(user_api_key_dict, "spend", None) or 0.0)
    max_budget = getattr(user_api_key_dict, "max_budget", None)
    if not max_budget:
        return (
            f"📊 {alias} 用量\n"
            f"本 key 未设周期限额，累计已用 ${spend:.2f}。"
        )
    pct = spend / max_budget * 100.0
    remain = max(0.0, max_budget - spend)
    # 「今日用量」这四个字是 T0 活体冒烟（tests/t0_budget_notice.py A~D3、I、J）和
    # scripts/litellm-198-budget-notice-smoke.sh 的断言锚点，改文案时不许动它。
    return (
        f"📊 {alias} 今日用量\n"
        f"${spend:.2f} / ${max_budget:.2f}（{pct:.0f}%），剩余 ${remain:.2f}"
        f" · {_beijing_reset_str(user_api_key_dict)} 重置 · 数据约 1 分钟延迟"
    )


def _runtime_model_list() -> list:
    """Read the loaded Router catalog without making a network request."""
    try:
        from litellm.proxy.proxy_server import llm_router

        rows = getattr(llm_router, "model_list", None)
        return rows if isinstance(rows, list) else []
    except Exception as exc:
        _log.warning("budget_notice: model catalog unavailable %r", exc)
        return []


_MODEL_LIMIT_DEFAULTS = (
    ("deepseek-v4-flash", {"context_window": 1_000_000,
                            "max_input_tokens": 1_000_000,
                            "max_output_tokens": 393_216}),
    ("claude-sonnet-5", {"context_window": 1_000_000,
                          "max_input_tokens": 1_000_000,
                          "max_output_tokens": 128_000}),
    ("claude-opus-5", {"context_window": 1_000_000,
                        "max_input_tokens": 1_000_000,
                        "max_output_tokens": 128_000}),
    ("claude-haiku-4-5", {"context_window": 200_000,
                           "max_input_tokens": 200_000,
                           "max_output_tokens": 64_000}),
    ("gpt-image-2", {"context_window": 32_000,
                      "max_input_tokens": 32_000,
                      "max_output_tokens": 4_096}),
    ("gpt-5.6", {"context_window": 1_000_000,
                 "max_input_tokens": 922_000,
                 "max_output_tokens": 128_000}),
)


def _model_limit_defaults(model_name: Any, params: dict) -> dict:
    """Return explicit product limits for aliases absent from LiteLLM's price table."""
    haystack = " ".join(
        str(value).lower()
        for value in (model_name, params.get("model"))
        if value
    )
    for marker, limits in _MODEL_LIMIT_DEFAULTS:
        if marker in haystack:
            return limits
    return {}


def _model_limit_value(info: dict, params: dict, model_name: Any, *names: str) -> Any:
    """Prefer explicit values, then LiteLLM's effective metadata, then product defaults."""
    for source in (info, params):
        for name in names:
            if isinstance(source, dict) and source.get(name) is not None:
                return source[name]
    defaults = _model_limit_defaults(model_name, params)
    for name in names:
        if name in defaults:
            return defaults[name]
    try:
        import litellm

        model = str(params.get("model") or model_name or "")
        for candidate in (model, model.split("/", 1)[-1]):
            if not candidate:
                continue
            effective = litellm.get_model_info(candidate)
            for name in names:
                if effective.get(name) is not None:
                    return effective[name]
    except Exception:
        pass
    return None


def _format_model_limit(value: Any) -> str:
    if value is None:
        return "未配置"
    try:
        number = float(value)
        if number.is_integer():
            return f"{int(number):,}"
        return f"{number:,g}"
    except (TypeError, ValueError):
        return str(value)


def _model_catalog_text(user_api_key_dict: Any) -> str:
    """Render the key's configured models, enriched by loaded deployment data."""
    allowed = getattr(user_api_key_dict, "models", None)
    if isinstance(allowed, dict):
        allowed = allowed.keys()
    if allowed:
        # Preserve the key API order; it is more useful than Router insertion
        # order and includes callback-routed aliases absent from model_list.
        names = list(dict.fromkeys(str(model) for model in allowed))
    else:
        # LiteLLM uses an empty model list to mean unrestricted access.
        names = []

    aliases = getattr(user_api_key_dict, "aliases", None)
    if not isinstance(aliases, dict):
        aliases = {}
    rows_by_name = {}
    for row in _runtime_model_list():
        if not isinstance(row, dict) or not row.get("model_name"):
            continue
        rows_by_name.setdefault(str(row["model_name"]), []).append(row)
    if not names:
        names = list(rows_by_name)

    grouped = {}
    for name in names:
        # A per-key alias often points at the real deployment carrying the
        # official limits, while the public name itself is callback-routed.
        lookup_names = [name]
        target = aliases.get(name)
        if target and str(target) not in lookup_names:
            lookup_names.append(str(target))
        rows = [row for lookup in lookup_names for row in rows_by_name.get(lookup, [])]
        entry = grouped.setdefault(name, {
            "context_window": set(),
            "max_input_tokens": set(),
            "max_output_tokens": set(),
        })
        for row in rows:
            info = row.get("model_info") or {}
            params = row.get("litellm_params") or {}
            if not isinstance(info, dict):
                info = {}
            if not isinstance(params, dict):
                params = {}
            for field, value in (
                ("context_window", _model_limit_value(
                    info, params, name, "context_window")),
                ("max_input_tokens", _model_limit_value(
                    info, params, name, "max_input_tokens")),
                ("max_output_tokens", _model_limit_value(
                    info, params, name, "max_output_tokens", "max_tokens")),
            ):
                if value is not None:
                    entry[field].add(str(value))
        # Resolve aliases with no loaded row from LiteLLM's built-in price and
        # context metadata, then from the explicit product defaults below.
        for field, lookup_names in (
            ("context_window", ("context_window",)),
            ("max_input_tokens", ("max_input_tokens",)),
            ("max_output_tokens", ("max_output_tokens", "max_tokens")),
        ):
            if not entry[field]:
                value = _model_limit_value({}, {}, name, *lookup_names)
                if value is not None:
                    entry[field].add(str(value))

    if not grouped:
        return ""

    catalog_fingerprint = tuple(
        (name, tuple(sorted((field, tuple(sorted(values)))
                            for field, values in limits.items())))
        for name, limits in grouped.items()
    )
    cache_key = (getattr(user_api_key_dict, "key_alias", ""), catalog_fingerprint,
                 tuple(sorted((str(k), str(v)) for k, v in aliases.items())))
    cached = _MODEL_CATALOG_CACHE.get(cache_key)
    now = time.monotonic()
    if cached and now - cached[0] < _MODEL_CATALOG_CACHE_TTL:
        return cached[1]

    # 按「限制三元组」合并：41 个模型的 41 行实测塌成约 12 行，且一个公开模型名
    # 都不丢。标签（上下文/输入/输出）在表头声明一次，不在每行重复。
    by_limits = {}
    for name, limits in grouped.items():
        values = tuple(
            tuple(sorted(_format_model_limit(value) for value in limits[field]))
            for field in ("context_window", "max_input_tokens", "max_output_tokens")
        )
        by_limits.setdefault(values, []).append(name)

    lines = [f"\n📋 模型 {len(grouped)} 个（上下文/输入/输出 上限，按限制合并）:"]
    for values, names_in_group in by_limits.items():
        limits_text = "/".join(
            ("/".join(value) if value else "未配置") for value in values
        )
        lines.append(f"- {limits_text} → " + "、".join(names_in_group))
    text = "\n".join(lines)
    _MODEL_CATALOG_CACHE[cache_key] = (now, text)
    return text


# TTFT 一条查询取回「今天这把 key 用过的每个模型」的 P50/P90。上限只为防一把 key
# 一天里打了几十个模型时把消息撑爆；今日有流量的模型数远小于 key 的白名单长度
# （实测 cursor-liuguoxian-std：白名单 41 个，今日有流量 5 个）。
_TTFT_MAX_ROWS = 15

# 🔴 这条 SQL 的两个写法都是 2026-09-24 在 198 prod 库（litellm-db-0，
# `LiteLLM_SpendLogs` 108 GB / 907,738 行）实测定下来的，改之前先看数据：
#
#   1. **过滤列只能是 `api_key`，绝不能是 `metadata->>'user_api_key_alias'`。**
#      同一天窗口、同一张表实测：
#          metadata->>'user_api_key_alias'  →  791,457 ms（13 分 11 秒）
#          api_key = $1                     →        773 ms
#      差三个数量级。`metadata` 是大 JSON 且被 TOAST 外存，`->>` 逼 PG 把 90 万行
#      的 metadata 逐行解出来；`api_key` 是普通 varchar，比就完了。
#
#   2. **`startTime` 上不许套算术。** 写成
#      `("startTime" + INTERVAL '8 hours') >= DATE_TRUNC(...)` 会让
#      `LiteLLM_SpendLogs_startTime_idx` 整个失效，退化成 Parallel Seq Scan；
#      把 8 小时偏移挪到比较式右边之后，EXPLAIN ANALYZE 实测走
#      Parallel Index Scan，621 ms → 343 ms。语义完全等价：
#      `DATE_TRUNC('day', NOW() + 8h) - 8h` 就是「北京今天 00:00」对应的 UTC 时刻，
#      而 `startTime` 是裸 UTC。
#
# 为什么这两条是硬约束、不是"优化"：proxy 的 DATABASE_URL 带
# `connection_limit=1&pool_timeout=60` —— 整个 proxy 进程只有 **1 条** DB 连接。
# 一条 13 分钟的查询会独占它 13 分钟，把所有其它 DB 使用方一起饿死；而 60s 的
# pool_timeout 又保证这条查询在有竞争时永远拿不到连接 → 抛异常 → 被 except 吞成
# 空串。用户面症状就是「查余额很慢，而且 TTFT 那段根本不出现」。PG 侧
# `statement_timeout = 0`，不会有人来把慢查询掐掉。
_TTFT_SQL = '''SELECT "model_group" AS g,
                      COUNT(*)::int AS n,
                      ROUND((PERCENTILE_CONT(0.50) WITHIN GROUP (
                        ORDER BY EXTRACT(EPOCH FROM ("completionStartTime" - "startTime"))
                      ))::numeric, 2) AS p50,
                      ROUND((PERCENTILE_CONT(0.90) WITHIN GROUP (
                        ORDER BY EXTRACT(EPOCH FROM ("completionStartTime" - "startTime"))
                      ))::numeric, 2) AS p90
               FROM "LiteLLM_SpendLogs"
               WHERE "api_key" = $1
                 AND "startTime" >=
                     DATE_TRUNC('day', NOW() + INTERVAL '8 hours') - INTERVAL '8 hours'
                 AND "completionStartTime" IS NOT NULL
                 AND "completionStartTime" > "startTime"
               GROUP BY 1
               ORDER BY n DESC
               LIMIT ''' + str(_TTFT_MAX_ROWS)


async def _ttft_text(user_api_key_dict: Any) -> str:
    """今日 per-model TTFT P50/P90，按 `model_group`（= 用户在菜单里看到的请求名）分组。

    `completionStartTime` 由流式桥在上游第一个内容事件时写入，所以
    `completionStartTime - startTime` 就是 TTFT。没有正区间的行（非流式、
    失败行）全部排除 —— 它们没有 TTFT 可言。

    分组列用 `model_group` 而不是 `model`：`model` 是落点名
    （`openai/chatgpt-gpt-5.6-sol`），`model_group` 才是请求名（`gpt-5.6-sol`），
    后者才是用户认得出、也是他在模型菜单里选的那个名字。

    过滤列和时间条件的写法有硬约束，见 `_TTFT_SQL` 上方注释。
    """
    token = getattr(user_api_key_dict, "token", None) or ""
    if not token:
        # 只认 api_key（= VerificationToken.token 的哈希）。alias 那条路走
        # metadata JSON，实测 13 分钟，宁可不显示也不能再挂回去。
        return "\n⚡ TTFT 今日：取不到本 key 标识，无法统计。"
    cached = _TTFT_CACHE.get(token)
    now = time.monotonic()
    if cached and now - cached[0] < _TTFT_CACHE_TTL:
        return cached[1]

    def _keep(text: str) -> str:
        _TTFT_CACHE[token] = (now, text)
        return text

    try:
        from litellm.proxy.proxy_server import prisma_client
        if prisma_client is None:
            return _keep("\n⚡ TTFT 今日：查询暂不可用，请稍后重试。")

        rows = await prisma_client.db.query_raw(_TTFT_SQL, token)
        if not rows:
            return _keep("\n⚡ TTFT 今日：暂无有效样本（今日尚无流式请求）。")

        lines = ["\n⚡ TTFT 今日 P50/P90（秒，仅今日有流量的模型）:"]
        for row in rows:
            name = row.get("g") or "（未知模型）"
            lines.append(
                f"- {name} {float(row['p50']):.2f}/{float(row['p90']):.2f}"
                f"（{row['n']} 次）"
            )
        return _keep("\n".join(lines))
    except Exception as exc:
        _log.warning("budget_notice: TTFT query unavailable %r", exc)
        return _keep("\n⚡ TTFT 今日：查询暂不可用，请稍后重试。")


def _warn_text(user_api_key_dict: Any) -> str:
    spend = float(getattr(user_api_key_dict, "spend", None) or 0.0)
    max_budget = float(getattr(user_api_key_dict, "max_budget", None) or 0.0)
    pct = spend / max_budget * 100.0 if max_budget else 0.0
    return (
        f"\n\n----\n⚠️ 你的 key 今日额度已用 {pct:.0f}%"
        f"（${spend:.2f}/${max_budget:.2f}），{_beijing_reset_str(user_api_key_dict)} 重置。"
        f"发送 /查余额 可随时查询。（本提醒每天最多一次）"
    )


def _over_budget_text(user_api_key_dict: Any, cost=None, mx=None) -> str:
    """③ 100% 超预算的 200 友好文案。cost/mx 优先取 BudgetExceededError 自带的
    current_cost/max_budget（auth 层算好的值），拿不到再退回 key 对象上的
    spend/max_budget。"""
    if cost is None:
        cost = float(getattr(user_api_key_dict, "spend", None) or 0.0)
    if mx is None:
        mx = getattr(user_api_key_dict, "max_budget", None)
    try:
        cost = float(cost)
    except Exception:
        cost = 0.0
    limit_str = f"限额 ${float(mx):.2f}" if mx else "今日限额"
    return (
        f"🚫 你的 key 今日额度已用完：已消费 ${cost:.2f} / {limit_str}。\n"
        f"{_beijing_reset_str(user_api_key_dict)} 自动重置，届时可继续使用。\n"
        f"（可随时发送 /查余额 查询当前用量）"
    )


# ------------------------------------------------ ③ 超预算 mark（patch→pre_call）
# _virtual_key_max_budget_check 抛 BudgetExceededError 时，patch 层吞掉 429、
# 在这里按 token 打一个短 TTL mark；随后请求走到 pre_call，pre_call 读 mark 决定
# 返回哪种 200 友好文案。TTL 只是兜底（正常路径下 pre_call 读完就由下一次
# auth 成功清除），避免 patch 打了 mark 但 pre_call 没跑（异常/短路）导致长期滞留。

_OVER_BUDGET_MARKS: dict = {}
_OVER_BUDGET_TTL = 120.0
_OVER_BUDGET_CAP = 50000


def _mark_over_budget(token: str, cost, mx) -> None:
    if not token:
        return
    if len(_OVER_BUDGET_MARKS) > _OVER_BUDGET_CAP:
        _OVER_BUDGET_MARKS.clear()
    _OVER_BUDGET_MARKS[token] = (time.monotonic(), cost, mx)


def _clear_over_budget_mark(token: str) -> None:
    if token:
        _OVER_BUDGET_MARKS.pop(token, None)


def _take_over_budget_mark(token: str):
    """返回 (cost, mx) 或 None。不 pop —— 同一 mark 可能覆盖预检+真实请求两跳；
    清除交给 patch 层的 auth-success 分支（TTL 只兜底过期）。"""
    if not token:
        return None
    ent = _OVER_BUDGET_MARKS.get(token)
    if not ent:
        return None
    ts, cost, mx = ent
    if time.monotonic() - ts > _OVER_BUDGET_TTL:
        _OVER_BUDGET_MARKS.pop(token, None)
        return None
    return cost, mx


# ------------------------------------------------ ④ 模型系列日额度（per key）
# 需求（2026-09-24）：Cursor key 每人每天 Claude 家族 $100、其他模型（含 GPT-5.3）$500；
# 其他 gated key 保留 GPT-5.3 独立 $200 桶，北京 0 点
# 重置。litellm 原生 model_max_budget 做不到：精确匹配无系列概念、每模型独立
# 记账不共享桶、滚动 24h 窗非日历日、超限裸 429 Cursor 不可见 —— 全在这里自建。
#
# 记账：async_log_success_event 读 standard_logging_payload.response_cost，按
# (family, token, 北京日) 在 redis INCRBYFLOAT 累加（键含日期天然 0 点换桶，
# TTL 仅清理）。执法：pre_call 查桶，>= 限额 → 200 mock「该系列额度已用完」。
# fail-open：记账/查询任何异常都放行并打 warning。
#
# 默认关（BUDGET_FAMILY_ENABLED=1 才启用）—— 本文件三份拷贝（repo/198 CM/阿里云
# CM）保持同源，阿里云不设该 env 即零行为变化。

_FAMILY_RULES = (
    # (key, 展示名, 匹配函数)。顺序即优先级，首个命中生效。
    ("gpt53", "GPT-5.3 系列",
     lambda m: _is_gpt53_model(m)),
    # Claude 单独成桶。只认 Claude 家族的型号词，而不是泛匹配 "claude"：
    # Fable/Opus/Sonnet/Haiku 的不同 provider、前缀和 dated alias 都覆盖；
    # claude-glm/deepseek/kimi/grok 等内部别名仍留在 other 桶。
    ("claude", "Claude 家族",
     lambda m: bool(_CLAUDE_MODEL_RE.search(m))),
    # 其他所有模型兜底进入 other 桶。
    ("other", "其他模型",
    lambda m: True),
)
_FAMILY_DEFAULT_USD = {"gpt53": 200.0, "claude": 100.0, "other": 500.0}

# Cursor 专属规则：GPT-5.3 不再单独计账，直接进入 other。
_CURSOR_FAMILY_RULES = tuple(rule for rule in _FAMILY_RULES if rule[0] != "gpt53")

# Model groups are not consistently prefixed: examples include
# ``claude-fable-5``, ``anthropic.claude-opus-4-8``, ``fable5.1`` and
# ``cursor-ultra-sonnet-4-6``. Boundary-aware matching avoids treating
# ``claude-glm``/``claude-kimi``/``claude-gpt`` as Claude-family models.
_CLAUDE_MODEL_RE = _re.compile(
    r"(?:^|[-_./])(?:fable|opus|sonnet|haiku)(?=$|[-_./0-9])"
)


def _is_gpt53_model(model: str) -> bool:
    """Keep Claude-prefixed internal aliases out of the GPT-5.3 bucket."""
    return "gpt" in model and "5.3" in model and "claude" not in model


def _family_enabled() -> bool:
    return os.environ.get("BUDGET_FAMILY_ENABLED") == "1" and not _disabled()


def _family_limit(fkey: str, user_api_key_dict: Any) -> float:
    """限额优先级：key metadata.budget_family_overrides[fkey] > env > 内置默认。
    metadata 覆盖既是灰度/冒烟手段，也留了按人定制口子。"""
    try:
        meta = getattr(user_api_key_dict, "metadata", None) or {}
        ov = meta.get("budget_family_overrides") or {}
        if fkey in ov:
            return float(ov[fkey])
    except Exception:
        pass
    env_name = {"gpt53": "BUDGET_FAMILY_GPT53_USD",
                "claude": "BUDGET_FAMILY_CLAUDE_USD",
                "other": "BUDGET_FAMILY_OTHER_USD"}.get(fkey, "")
    try:
        return float(os.environ.get(env_name, _FAMILY_DEFAULT_USD[fkey]))
    except Exception:
        return _FAMILY_DEFAULT_USD.get(fkey, 0.0)


def _family_rules_for_key(user_api_key_dict: Any = None):
    """Return the active family rules; only cursor-* retires gpt53."""
    if isinstance(user_api_key_dict, str):
        alias = user_api_key_dict
    else:
        alias = getattr(user_api_key_dict, "key_alias", "") if user_api_key_dict else ""
    if str(alias).startswith("cursor-"):
        return _CURSOR_FAMILY_RULES
    return _FAMILY_RULES


def _family_of(model: str, user_api_key_dict: Any = None):
    """返回 (fkey, label) 或 None。按 model_group 名匹配（执法用请求里的
    data["model"]，记账用 payload.model_group —— litellm 两处同名）。"""
    m = (model or "").lower()
    if not m:
        return None
    for fkey, label, pred in _family_rules_for_key(user_api_key_dict):
        try:
            if pred(m):
                return fkey, label
        except Exception:
            pass
    return None


_fam_local: dict = {}   # redis 不可用时的 per-pod 降级记账


def _family_spend_key(fkey: str, token: str) -> str:
    return f"budget_notice:fam:{fkey}:{token}:{_beijing_date()}"


async def _family_add_spend(token: str, fkey: str, cost: float) -> None:
    if not token or cost <= 0:
        return
    key = _family_spend_key(fkey, token)
    rc = _resolve_redis()
    if rc is not None:
        try:
            # RedisCache.async_increment = INCRBYFLOAT，多 pod 原子累加
            await rc.async_increment(key, cost, ttl=100000)
            return
        except Exception as exc:
            _log.warning("budget_notice: family incr redis error %r, local", exc)
    if len(_fam_local) > 50000:
        _fam_local.clear()
    _fam_local[key] = _fam_local.get(key, 0.0) + cost


async def _family_get_spend(token: str, fkey: str) -> float:
    key = _family_spend_key(fkey, token)
    rc = _resolve_redis()
    if rc is not None:
        try:
            v = await rc.async_get_cache(key)
            if v is not None:
                return float(v)
            return 0.0
        except Exception as exc:
            _log.warning("budget_notice: family get redis error %r, local", exc)
    return float(_fam_local.get(key, 0.0))


def _family_over_text(label: str, spent: float, limit: float) -> str:
    return (
        f"🚫 你的 key 今日 {label} 额度已用完：该系列已消费 ${spent:.2f} / "
        f"限额 ${limit:.2f}。\n"
        f"北京时间明日 00:00 自动重置；**其他系列的模型不受影响**，可切换使用。\n"
        f"（可发送 /查余额 查询各系列用量）"
    )


async def _family_usage_lines(token: str, user_api_key_dict: Any) -> str:
    """/查余额 附加的系列用量段。未启用返回空串。"""
    if not _family_enabled() or not token:
        return ""
    try:
        rules = _family_rules_for_key(user_api_key_dict)
        spends = await asyncio.gather(
            *(_family_get_spend(token, fkey) for fkey, _label, _pred in rules)
        )
        parts = []
        for (fkey, label, _pred), spent in zip(rules, spends):
            limit = _family_limit(fkey, user_api_key_dict)
            parts.append(f"{label} ${spent:.2f}/${limit:.2f}")
        return "\n系列额度：" + "，".join(parts) + "。"
    except Exception as exc:
        _log.warning("budget_notice: family usage lines error %r", exc)
        return ""


# ------------------------------------------------------------ codex 停机分支
# 背景：codex CLI 的 goal/目标模式把网关回的 HTTP 200 友好消息当成「本轮成功」，
# 会不停地再发新请求 → 同一句「额度已用完」刷屏(不是烧钱、是刷消息)。唯一能让
# goal 循环干净退出的响应形状,是 429 + body {"error":{"type":"usage_limit_reached"}}
# (codex api_bridge.rs:118-144 映射为 CodexErr::UsageLimitReached,不可重试、直接
# 上抛,goal 循环的 `?` 把它带出循环 —— core/tasks/regular.rs:85)。
#
# ⚠️ codex 忽略 body 里的 message,自己渲染文案(protocol/error.rs Display);自定义
# 文案只能经**响应头** `x-codex-promo-message` 到达用户屏幕,渲染为
# 「You've hit your usage limit. {promo}, or try again later.」。
# ⚠️ 该头走 http::HeaderValue::to_str()(rate_limits.rs:parse_header_str),**只认可见
# ASCII**;混入中文 → to_str() 失败 → promo 被丢 → 退回泛化文案。所以 promo 必须
# 全 ASCII、且自带用量数字/重置时间/切换提示(body 带不了 resets_at,因为
# ProxyException.to_dict() 只吐 message/type/param/code)。
#
# 只对 codex 客户端生效;Cursor 等其它 /v1/responses 客户端吞 429 body,继续走 200
# mock(见 feedback_cursor_swallows_429_response_body)。

# _FAMILY_RULES 的展示名是中文,进不了 ASCII 头;这里给一份 ASCII 别名。
_FAMILY_ASCII = {
    "gpt53": "gpt-5.3 (codex)",
    "claude": "Claude",
    "other": "main-models (all non-Claude models)",
}


def _codex_stop_disabled() -> bool:
    """codex 429 停机分支的逃生门(per-call 读,kubectl set env 秒级生效)。"""
    return os.environ.get("BUDGET_CODEX_STOP_DISABLED") == "1"


def _client_is_codex(data: Any) -> bool:
    """codex CLI/exec 客户端识别:UA 或 originator 头含 'codex'。
    codex exec → originator: codex_exec / UA codex_exec/*;交互式 codex →
    originator: codex_cli_rs —— 两者都含子串 'codex'。Cursor/CC/her bot 均不含,
    可安全区分。headers 在 pre_call 时已落到 data(litellm_pre_call_utils 1433/1462)。"""
    try:
        if not isinstance(data, dict):
            return False
        for mk in ("proxy_server_request", "metadata", "litellm_metadata"):
            src = data.get(mk)
            if not isinstance(src, dict):
                continue
            hdrs = src.get("headers")
            if not isinstance(hdrs, dict):
                continue
            for hk, hv in hdrs.items():
                if str(hk).lower() in ("user-agent", "originator"):
                    if "codex" in str(hv).lower():
                        return True
    except Exception:
        pass
    return False


def _family_other(fkey: str, rules=None):
    """返回一个可切换的替代桶，供 Codex ASCII promo 使用。"""
    rules = rules or _FAMILY_RULES
    # Cursor 是 Claude ↔ other；其他 gated key 仍可在 gpt53/claude/other 间切换。
    for wanted in ("other", "gpt53", "claude"):
        if wanted == fkey:
            continue
        for k, label, _p in rules:
            if k == wanted:
                return k, label
    return None


def _codex_promo_family(fkey: str, spent: float, limit: float,
                        ofkey: Optional[str], ospent: float, olimit: float) -> str:
    """④ 系列桶超额时给 codex 的 ASCII promo(进 x-codex-promo-message)。

    codex 把它嵌进固定包装渲染:
        You've hit your usage limit. {promo}, or try again later.
    所以 {promo} 要写成能在句号之后、逗号之前顺读的整句(首字母大写、不与
    "usage limit" 重复、句尾能接 ", or try again later")。

    Claude 桶满、other 桶还有余额时，Cursor 可以切到非 Claude 模型继续；
    Cursor 的 GPT-5.3 已归入 other，不再有独立可切换桶。按用户要求写成
    信息式（告知有余额 + 怎么切），用不用让用户自己决定，不强推。
    """
    blocked = _FAMILY_ASCII.get(fkey, fkey)
    orem = (olimit - ospent) if (olimit and olimit > 0) else 0.0
    if ofkey and orem > 0:
        other = _FAMILY_ASCII.get(ofkey, ofkey)
        return (f"On the {blocked} lane you've spent ${spent:.2f} of ${limit:.2f} "
                f"today; the {other} lane still has ${orem:.2f} left if you want "
                f"to switch (/model) - your call")
    return (f"On the {blocked} lane you've spent ${spent:.2f} of ${limit:.2f} "
            f"today; it resets 00:00 Beijing time")


def _codex_promo_total(cost: float, mx: float) -> str:
    """③ key 总额度超额时给 codex 的 ASCII promo。

    数字口径同 ④(used/limit)。总额度覆盖所有模型，且 ③ 分支 model-agnostic
    (budget_notice pre_call 只看 over_budget_mark、不看 model),所以 ③ 一旦触发,
    切换模型也不能绕过它。
    """
    return (f"You've spent ${cost:.2f} of your ${mx:.2f} daily budget, which "
            f"covers every model (gpt-5.3 included); it resets 00:00 Beijing "
            f"time. Choose a model family only after the daily total resets")


def _codex_usage_limit_exc(promo: str):
    """构造让 codex goal 循环退出的异常:429 + type=usage_limit_reached +
    x-codex-promo-message 头。经 FastAPI @app.exception_handler(ProxyException)
    (proxy_server.py:1300)直出 JSONResponse(status=429, {"error":to_dict()},
    headers=exc.headers)。error_sanitize 不碰 .headers、且 type 不命中泄漏正则,
    故 promo 头与 type 都存活;它只会把 body message 置换成「API 异常」(codex 无视
    body message,无影响)。"""
    from litellm.proxy._types import ProxyException

    safe = (promo or "").encode("ascii", "ignore").decode("ascii")
    return ProxyException(
        message="usage limit reached",
        type="usage_limit_reached",
        param=None,
        code=429,
        headers={"x-codex-promo-message": safe},
    )


def _codex_promo_upstream() -> str:
    """上游号池撞额度(账号自身配额打满,非本 key 预算)时给 codex 的 ASCII promo。

    这类 429 不是用户 key 的预算问题,而是承接 codex 的账号池临时全被打满(litellm
    重试/兜底耗尽后才走到失败 hook),所以给不出 used/limit 数字,只能给「稍后重试 /
    换到别的模型系列」这类真实可操作指引。"""
    return ("Codex capacity is busy right now; retry shortly, or switch model "
            "with /model to use a different lane")


def _is_upstream_usage_limit(exc: Any) -> bool:
    """判定是否上游透传的 usage_limit 429(而非我们 pre_call 自抛的 ③/④)。

    主判据用 ``.type``(error_sanitize 只在命中泄漏正则时才改 type,
    usage_limit_reached 不含泄漏词 → 会存活,跨 callback 顺序都可靠);``.message``
    可能已被 error_sanitize 置换成「API 异常」,故不作主判据。宽松匹配,只在 codex
    客户端路径上用,误配代价低(最多给一条无害的 retry 指引)。"""
    try:
        code = getattr(exc, "status_code", None)
        if code is None:
            code = getattr(exc, "code", None)
        try:
            code = int(code)
        except Exception:
            code = None
        typ = str(getattr(exc, "type", "") or "").lower()
        blob = typ
        for a in ("message",):
            try:
                blob += " " + str(getattr(exc, a, "") or "").lower()
            except Exception:
                pass
        try:
            blob += " " + str(exc).lower()
        except Exception:
            pass
        has_ul = ("usage_limit" in blob) or ("usage limit" in blob)
        return has_ul and (code in (429, None))
    except Exception:
        return False


# ------------------------------------------------------------ redis 去重
# 解析链照抄 weighted_affinity：优先 router 的 RedisCache（跨 pod/worker），
# 拿不到就退化为本模块级 set（每 pod 各去重一次，可接受的降级）。

_local_seen: set = set()
_redis_cache = None
_redis_resolved = False


def _resolve_redis():
    global _redis_cache, _redis_resolved
    if _redis_resolved:
        return _redis_cache
    _redis_resolved = True
    try:
        from litellm.proxy.proxy_server import llm_router

        router_cache = getattr(llm_router, "cache", None)
        rc = getattr(router_cache, "redis_cache", None)
        if rc is not None:
            _redis_cache = rc
            _log.warning("budget_notice: dedupe backend=redis")
            return _redis_cache
    except Exception:
        pass
    _log.warning("budget_notice: dedupe backend=local (redis unavailable)")
    return None


def _beijing_date() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)
    ).strftime("%Y%m%d")


async def _acquire_daily(token: str) -> bool:
    """今天第一次见到该 key 返回 True。get→set 存在跨 pod 竞态窗口，
    最坏后果是同一瞬间并发请求各注入一次提醒 —— 可接受，不为此上锁。"""
    key = f"budget_notice:90:{token}:{_beijing_date()}"
    rc = _resolve_redis()
    if rc is not None:
        try:
            cur = await rc.async_get_cache(key)
            if cur is not None:
                return False
            await rc.async_set_cache(key, "1", ttl=100000)
            return True
        except Exception as exc:
            _log.warning("budget_notice: redis dedupe error %r, fallback local", exc)
    if key in _local_seen:
        return False
    _local_seen.add(key)
    if len(_local_seen) > 20000:
        _local_seen.clear()
    return True


async def _release_daily(token: str) -> None:
    """注入没有真正落到流里（比如该轮以 tool_use 结尾、没有 text block）时
    释放当日名额，让下一个请求再试。best effort，失败不抛。"""
    key = f"budget_notice:90:{token}:{_beijing_date()}"
    _local_seen.discard(key)
    rc = _resolve_redis()
    if rc is not None:
        try:
            await rc.async_delete_cache(key)
        except Exception:
            pass


# ---------------------------------------------------------- 流注入（②）
#
# anthropic 路径必须把提醒 delta 注入到**最后一个 text block 内部**（其
# content_block_stop 之前），不能追加独立 block —— Claude Code 只把最后一个
# text block 当结果展示，独立尾块会顶掉真正的答案（2026-08-19 canary 实测）。
# 做法：事件级状态机 —— text block 的 stop 事件先持有，看到下一个事件是
# message_delta/message_stop 才注入 + 放行；是别的事件（还有后续 block）则原样
# 放行。字节流按 \n\n 事件边界重组，天然覆盖任意 chunk 切分。

_EV_SEP = b"\n\n"


def _classify_event(ev: bytes):
    """返回 (etype, index, block_type)；解析失败一律 (None, None, None) 透传。"""
    try:
        for line in ev.split(b"\n"):
            if line.startswith(b"data:"):
                obj = json.loads(line[5:].strip())
                etype = obj.get("type")
                idx = obj.get("index")
                btype = None
                if etype == "content_block_start":
                    cb = obj.get("content_block") or {}
                    btype = cb.get("type")
                return etype, idx, btype
    except Exception:
        pass
    return None, None, None


def _notice_delta_bytes(notice: str, index) -> bytes:
    obj = {"type": "content_block_delta", "index": index,
           "delta": {"type": "text_delta", "text": notice}}
    return (b"event: content_block_delta\ndata: "
            + json.dumps(obj, ensure_ascii=False).encode() + b"\n\n")


# ---- responses 事件流注入(Codex / Cursor agent 走 /v1/responses)----
# hook 层拿到的是 pydantic 事件对象(ResponseCreatedEvent 等,2026-08-20 T0
# 实测)。与 anthropic 同款课题:提醒必须进最后一个 output_text part 内部,
# 且 done/completed 事件里的全文要同步补上(客户端可能从任一源渲染)。
# 红线:绝不改事件的 type 字段(序列化器按类型缓存);注入的 delta 事件用
# deepcopy 流里真实 delta 改文本,类型/ID 天然正确。

def _ev_type(ev) -> str:
    t = getattr(ev, "type", None)
    return str(getattr(t, "value", t) or "")


def _append_text_attr(obj, attr: str, notice: str) -> None:
    try:
        cur = getattr(obj, attr, None)
        if isinstance(cur, str):
            setattr(obj, attr, cur + notice)
    except Exception:
        pass


def _patch_done_event(ev, notice: str) -> None:
    """text.done / part.done / item.done 三种事件里补全文,best effort。"""
    try:
        v = _ev_type(ev)
        if v == "response.output_text.done":
            _append_text_attr(ev, "text", notice)
        elif v == "response.content_part.done":
            part = getattr(ev, "part", None)
            if part is not None:
                _append_text_attr(part, "text", notice)
        elif v == "response.output_item.done":
            item = getattr(ev, "item", None)
            _patch_message_item(item, notice)
    except Exception:
        pass


def _patch_message_item(item, notice: str) -> None:
    try:
        content = item.get("content") if isinstance(item, dict) \
            else getattr(item, "content", None)
        if not isinstance(content, list):
            return
        for part in reversed(content):
            ptype = part.get("type") if isinstance(part, dict) \
                else getattr(part, "type", None)
            if str(getattr(ptype, "value", ptype)) == "output_text":
                if isinstance(part, dict):
                    if isinstance(part.get("text"), str):
                        part["text"] = part["text"] + notice
                else:
                    _append_text_attr(part, "text", notice)
                return
    except Exception:
        pass


def _patch_completed_event(ev, notice: str) -> None:
    try:
        resp = getattr(ev, "response", None)
        output = resp.get("output") if isinstance(resp, dict) \
            else getattr(resp, "output", None)
        if not isinstance(output, list):
            return
        for item in reversed(output):
            itype = item.get("type") if isinstance(item, dict) \
                else getattr(item, "type", None)
            if str(getattr(itype, "value", itype)) == "message":
                _patch_message_item(item, notice)
                return
    except Exception:
        pass


class BudgetNotice(CustomLogger):
    # ---------------------------------------------------------------- ①③
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        # 先在受保护区内决策，唯一的 raise 放在 try 之外 —— 短路信号绝不能被
        # 自己的兜底 except 吞掉。
        shortcut_exc = None
        try:
            if _disabled() or not isinstance(data, dict):
                return data
            if not _gated(user_api_key_dict):
                return data
            v = str(getattr(call_type, "value", call_type))  # 枚举必须取 .value

            # ③ 超预算软拦截：patch 层已吞掉 429 并打 mark。这里必须先于 ①
            #   的路由白名单/文案判断 —— mark 一旦存在就要出 200 友好消息，
            #   否则请求会带着「auth 放行」继续打到上游（真实计费）。
            token = getattr(user_api_key_dict, "token", "") or ""
            ob = _take_over_budget_mark(token) if token else None
            if ob is not None and not _friendly_mock_disabled():
                cost, mx = ob
                ob_text = _over_budget_text(user_api_key_dict, cost, mx)
                alias = getattr(user_api_key_dict, "key_alias", "") or ""
                if v in (
                    "completion", "acompletion",
                    "responses", "aresponses", "_aresponses_websocket",
                ):
                    if _client_is_codex(data) and not _codex_stop_disabled():
                        promo = _codex_promo_total(
                            cost if cost is not None else 0.0,
                            mx if mx is not None else 0.0)
                        _log.warning(
                            "budget_notice: over-budget CODEX 429 alias=%s route=%s "
                            "promo=%r", alias, v, promo)
                        raise _codex_usage_limit_exc(promo)
                    _log.warning(
                        "budget_notice: over-budget mock alias=%s route=%s", alias, v)
                    data["mock_response"] = ob_text
                    return data
                if v in ("anthropic_messages", "aanthropic_messages"):
                    from litellm.exceptions import ModifyResponseException

                    _log.warning(
                        "budget_notice: over-budget mock alias=%s route=anthropic", alias)
                    raise ModifyResponseException(
                        message=ob_text, model=str(data.get("model") or ""),
                        request_data=data,
                    )
                # 非可 mock 的 call_type（embeddings 等）：绝不放行到上游，
                # 重抛原生预算异常，由 error_sanitize 出友好 429。
                _log.warning(
                    "budget_notice: over-budget non-mockable route=%s, re-raise", v)
                import litellm as _litellm

                raise _litellm.BudgetExceededError(
                    current_cost=cost if cost is not None else 0.0,
                    max_budget=mx if mx is not None else 0.0,
                )

            # ④ 模型系列日额度（先于 ① —— 系列超限时连 /查余额 之外的推理
            #   都不放行；/查余额 本身不打上游、不受系列额度限制，放它先走）
            if _family_enabled() and token:
                fam = _family_of(str(data.get("model") or ""), user_api_key_dict)
                if fam is not None and not _is_quota_query(_last_user_text(data)):
                    fkey, flabel = fam
                    spent = await _family_get_spend(token, fkey)
                    limit = _family_limit(fkey, user_api_key_dict)
                    if limit > 0 and spent >= limit:
                        alias = getattr(user_api_key_dict, "key_alias", "") or ""
                        fam_text = _family_over_text(flabel, spent, limit)
                        if v in (
                            "completion", "acompletion",
                            "responses", "aresponses", "_aresponses_websocket",
                        ):
                            if _client_is_codex(data) and not _codex_stop_disabled():
                                ofk = _family_other(
                                    fkey, _family_rules_for_key(user_api_key_dict))
                                if ofk:
                                    ofkey = ofk[0]
                                    ospent = await _family_get_spend(token, ofkey)
                                    olimit = _family_limit(ofkey, user_api_key_dict)
                                else:
                                    ofkey, ospent, olimit = None, 0.0, 0.0
                                promo = _codex_promo_family(
                                    fkey, spent, limit, ofkey, ospent, olimit)
                                _log.warning(
                                    "budget_notice: family block CODEX 429 alias=%s "
                                    "fam=%s spent=%.2f/%.2f route=%s promo=%r",
                                    alias, fkey, spent, limit, v, promo)
                                raise _codex_usage_limit_exc(promo)
                            _log.warning(
                                "budget_notice: family block alias=%s fam=%s "
                                "spent=%.2f/%.2f route=%s", alias, fkey, spent, limit, v)
                            data["mock_response"] = fam_text
                            return data
                        if v in ("anthropic_messages", "aanthropic_messages"):
                            from litellm.exceptions import ModifyResponseException

                            _log.warning(
                                "budget_notice: family block alias=%s fam=%s "
                                "spent=%.2f/%.2f route=anthropic", alias, fkey, spent, limit)
                            raise ModifyResponseException(
                                message=fam_text,
                                model=str(data.get("model") or ""), request_data=data,
                            )
                        _log.warning(
                            "budget_notice: family block non-mockable route=%s, re-raise", v)
                        import litellm as _litellm

                        raise _litellm.BudgetExceededError(
                            current_cost=spent, max_budget=limit,
                        )

            if v not in (
                "completion", "acompletion",
                "responses", "aresponses", "_aresponses_websocket",
                "anthropic_messages", "aanthropic_messages",
            ):
                return data
            alias = getattr(user_api_key_dict, "key_alias", "") or ""
            text = _last_user_text(data)
            if alias in _debug_aliases():
                _log.warning(
                    "budget_notice: debug last_user_text alias=%s ct=%s head=%r",
                    alias, v, text[:300])
            if not _is_quota_query(text):
                return data

            family_text, ttft_text = await asyncio.gather(
                _family_usage_lines(token, user_api_key_dict),
                _ttft_text(user_api_key_dict),
            )
            # 顺序：额度 → 系列 → TTFT → 模型清单。TTFT 是用户主动要的那段，放在
            # 12 行模型清单**前面**，不然它被挤到消息尾部要翻屏才看得到。
            text = (_usage_text(user_api_key_dict) + family_text + ttft_text
                    + _model_catalog_text(user_api_key_dict))
            if v in ("anthropic_messages", "aanthropic_messages"):
                from litellm.exceptions import ModifyResponseException

                _log.warning("budget_notice: quota query alias=%s route=anthropic", alias)
                shortcut_exc = ModifyResponseException(
                    message=text, model=str(data.get("model") or ""), request_data=data,
                )
            else:
                _log.warning("budget_notice: quota query alias=%s route=%s", alias, v)
                data["mock_response"] = text
        except Exception as exc:
            # ModifyResponseException / BudgetExceededError / ProxyException 是我们
            # 主动抛的短路信号(200 合成响应 / 友好 429 / codex 停机 429),绝不能被
            # 这层兜底吞掉。
            cls = type(exc).__name__
            if cls in ("ModifyResponseException", "BudgetExceededError",
                       "ProxyException"):
                raise
            _log.warning("budget_notice: pre_call error %r", exc)
            return data
        if shortcut_exc is not None:
            raise shortcut_exc
        return data

    # ------------------------------------------ part1: 上游号池 usage_limit 补头
    async def async_post_call_failure_hook(
        self,
        request_data: dict,
        original_exception: Exception,
        user_api_key_dict: Any = None,
        traceback_str: Optional[str] = None,
    ) -> None:
        """codex 客户端撞上游号池 usage_limit 429 时补 x-codex-promo-message。

        病理:上游 chatgpt 账号撞自身配额回 429,它自己的 x-codex-* 头被 litellm
        加 ``llm_provider-`` 前缀转发(core_helpers.py:275),再被 error_sanitize
        整段删掉 → codex 收不到任何 x-codex-* → 渲染泛化「You've hit your usage
        limit. Try again later.」。这里给这类外发 429 补一条我们自己的 promo 头,
        让 codex 至少给出可操作指引。

        **只补头、永远 return None**:就地 setattr(exc, 'headers', ...)。绝不返
        HTTPException —— 那会把流式请求从「收到 error 帧」退化成「连接无声中断」
        (见 error_sanitize.py docstring 的实证)。补头能否真到达客户端由 T0 全链
        验证(pre_call 的 ProxyException(headers=) 已证可达,失败路径此前未验)。
        """
        try:
            if _disabled() or _codex_stop_disabled():
                return None
            if not isinstance(request_data, dict) or not _client_is_codex(request_data):
                return None
            exc = original_exception
            hdrs = getattr(exc, "headers", None)
            # 已带 promo 头(我们 pre_call 自抛的 ③/④ 429)→ 不重复注入
            if isinstance(hdrs, dict) and any(
                    str(k).lower() == "x-codex-promo-message" for k in hdrs):
                return None
            if not _is_upstream_usage_limit(exc):
                # T0 spike:非命中也记一行形状,方便摸清上游异常长相后收紧判据
                if os.environ.get("BUDGET_UPSTREAM_INJECT_DEBUG") == "1":
                    _log.warning(
                        "budget_notice: upstream-inject SKIP cls=%s code=%r type=%r "
                        "msg=%r has_headers=%s",
                        type(exc).__name__,
                        getattr(exc, "status_code", None) or getattr(exc, "code", None),
                        getattr(exc, "type", None),
                        str(getattr(exc, "message", ""))[:160],
                        isinstance(hdrs, dict))
                return None
            promo = _codex_promo_upstream()
            safe = promo.encode("ascii", "ignore").decode("ascii")
            newh = dict(hdrs) if isinstance(hdrs, dict) else {}
            newh["x-codex-promo-message"] = safe
            ok = False
            try:
                exc.headers = newh
                ok = True
            except Exception:
                pass
            # 归一 body type 到 usage_limit_reached:litellm 把上游 429 映射成
            # RateLimitError(client 侧 body error.type=throttling_error),codex 会
            # 当普通可重试 429 退避重试、既不渲染 promo 也不退 goal 循环。③/④ 已
            # 证实「error.type=usage_limit_reached + 429」这一形状才会让 codex 映射
            # 非重试 UsageLimitReached 退循环。此处补齐同形(usage_limit_reached
            # 不命中 error_sanitize 的泄漏正则,会被保留)。T0 全链验证 client 侧
            # body type 最终落到 usage_limit_reached。
            type_ok = False
            try:
                exc.type = "usage_limit_reached"
                type_ok = True
            except Exception:
                pass
            _log.warning(
                "budget_notice: upstream-usage-limit CODEX inject cls=%s set=%s "
                "type_set=%s promo=%r", type(exc).__name__, ok, type_ok, safe)
        except Exception as e:
            _log.warning("budget_notice: post_call_failure_hook error %r", e)
        return None

    # ---------------------------------------------------------------- ④ 记账
    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        """按 (family, token, 北京日) 累加 response_cost。fail-open。"""
        try:
            if not _family_enabled():
                return
            slp = kwargs.get("standard_logging_object") or {}
            if not isinstance(slp, dict):
                return
            cost = float(slp.get("response_cost") or 0.0)
            if cost <= 0:
                return
            meta = slp.get("metadata") or {}
            alias = meta.get("user_api_key_alias") or ""
            token = meta.get("user_api_key_hash") or ""
            if not token or not alias:
                return
            # 与 _gated 同门（family 只对 gated key 记账/执法）
            if not (alias in _gate_aliases()
                    or any(alias.startswith(p) for p in _gate_prefixes())):
                return
            model = slp.get("model_group") or slp.get("model") or ""
            # Classification is key-scoped: Cursor has no separate gpt53 bucket,
            # while other gated prefixes retain the legacy family split.
            fam = _family_of(str(model), alias)
            if fam is None:
                return
            await _family_add_spend(token, fam[0], cost)
        except Exception as exc:
            _log.warning("budget_notice: family accounting error %r", exc)

    # ---------------------------------------------------------------- ②
    async def async_post_call_streaming_iterator_hook(
        self, user_api_key_dict, response, request_data,
    ):
        # 非可迭代守卫(必须最先跑):responses 的 mock 分支无视 stream 参数、
        # 返回完整 ResponsesAPIResponse 对象(v1.90.2 responses/main.py MOCK
        # RESPONSE LOGIC),它进入流式 hook 链会让第一个 async-for 的 hook 500
        # (2026-08-20 Cursor /查余额 实测)。用 litellm 自己的 cache-hit 流式
        # 包装器把对象转成合法事件流;转换失败退化为单项透传,绝不抛。
        if not hasattr(response, "__aiter__"):
            events = None
            _ensure_reasoning_tokens(response)
            try:
                from litellm.responses.streaming_iterator import (
                    CachedResponsesAPIStreamingIterator,
                )

                rd = request_data if isinstance(request_data, dict) else {}
                wrapped = CachedResponsesAPIStreamingIterator(
                    response=response,
                    logging_obj=rd.get("litellm_logging_obj"),
                    request_data=rd,
                )
                events = [e async for e in wrapped]
                _log.warning(
                    "budget_notice: wrapped non-iterable %s into %d events",
                    type(response).__name__, len(events))
            except Exception as exc:
                _log.warning("budget_notice: non-iterable wrap failed %r", exc)
            if events:
                for e in events:
                    yield e
            else:
                yield response
            return

        inject = False
        notice = ""
        token = ""
        route = getattr(user_api_key_dict, "request_route", "") or ""
        injectable_route = ("chat/completions" in route) or ("messages" in route) \
            or ("responses" in route)
        if getattr(user_api_key_dict, "key_alias", "") in _debug_aliases():
            # 形态侦察:responses 路由注入前先搞清 hook 层 item 到底长什么样
            seen = []
            async for item in response:
                if len(seen) < 4:
                    t = type(item).__name__
                    extra = getattr(item, "type", None) or getattr(
                        getattr(item, "choices", [None])[0] if getattr(item, "choices", None) else None,
                        "finish_reason", None)
                    seen.append(f"{t}:{extra}")
                yield item
            _log.warning("budget_notice: debug stream shapes route=%s items=%s",
                         route, seen)
            return
        try:
            if injectable_route and not _disabled() and _gated(user_api_key_dict):
                spend = float(getattr(user_api_key_dict, "spend", None) or 0.0)
                max_budget = getattr(user_api_key_dict, "max_budget", None)
                if max_budget and spend / float(max_budget) >= _warn_ratio() \
                        and spend < float(max_budget):
                    token = getattr(user_api_key_dict, "token", "") or ""
                    if token and await _acquire_daily(token):
                        inject = True
                        notice = _warn_text(user_api_key_dict)
                        _log.warning(
                            "budget_notice: warn inject alias=%s spend=%.2f/%.2f route=%s",
                            getattr(user_api_key_dict, "key_alias", ""),
                            spend, float(max_budget),
                            getattr(user_api_key_dict, "request_route", ""),
                        )
        except Exception as exc:
            _log.warning("budget_notice: warn gate error %r", exc)

        if not inject:
            async for item in response:
                yield item
            return

        # --- 需要注入：按 item 形态分两路。任何异常都退化为透传剩余流；
        #     没能真正注入时释放当日去重名额，下一个请求再试。 ---
        injected = False
        try:
            buf = b""
            held_stop = None       # anthropic:持有中的 text block stop 事件
            held_index = None
            block_types = {}       # index -> content_block type
            r_holding = False      # responses:持有 text.done/part.done/item.done
            r_held = []
            r_last_delta = None    # responses:最近一个真实 output_text.delta 事件
            chat_route = "chat/completions" in route
            async for item in response:
                if isinstance(item, (bytes, bytearray)):
                    buf += bytes(item)
                    while _EV_SEP in buf:
                        ev, buf = buf.split(_EV_SEP, 1)
                        ev_full = ev + _EV_SEP
                        etype, idx, btype = _classify_event(ev_full)
                        if held_stop is not None:
                            if not injected and etype in ("message_delta",
                                                          "message_stop"):
                                yield _notice_delta_bytes(notice, held_index)
                                injected = True
                            yield held_stop
                            held_stop = None
                        if etype == "content_block_start":
                            block_types[idx] = btype
                            yield ev_full
                        elif (etype == "content_block_stop" and not injected
                              and block_types.get(idx) == "text"):
                            held_stop, held_index = ev_full, idx
                        else:
                            yield ev_full
                    continue

                # ---- responses 事件对象(type 形如 response.*)----
                v = _ev_type(item)
                if v.startswith("response."):
                    if v == "response.output_text.delta":
                        r_last_delta = item
                        if r_holding:  # 不该发生,防御性放行
                            for h in r_held:
                                yield h
                            r_held, r_holding = [], False
                        yield item
                        continue
                    if (v == "response.output_text.done" and not injected
                            and r_last_delta is not None and not r_holding):
                        r_held, r_holding = [item], True
                        continue
                    if r_holding and v in ("response.content_part.done",
                                           "response.output_item.done"):
                        r_held.append(item)
                        continue
                    if r_holding and v == "response.completed":
                        import copy

                        d = copy.deepcopy(r_last_delta)
                        try:
                            d.delta = notice
                        except Exception:
                            setattr(d, "delta", notice)
                        yield d
                        injected = True
                        for h in r_held:
                            _patch_done_event(h, notice)
                            yield h
                        _patch_completed_event(item, notice)
                        r_held, r_holding = [], False
                        yield item
                        continue
                    if r_holding:
                        # 后面还有别的事件(新 item 等)→ 刚才那个不是最后的
                        # text part,原样放行,继续找
                        for h in r_held:
                            yield h
                        r_held, r_holding = [], False
                        yield item
                        continue
                    yield item
                    continue

                # ---- chat 路径的 ModelResponseStream 对象 ----
                if not injected and chat_route and _finish_chunk(item):
                    synthetic = _make_notice_chunk(item, notice)
                    if synthetic is not None:
                        yield synthetic
                        injected = True
                yield item
            if held_stop is not None:
                yield held_stop
            for h in r_held:
                yield h
            if buf:
                yield buf
        except GeneratorExit:
            raise
        except Exception as exc:
            _log.warning("budget_notice: inject error %r, passthrough rest", exc)
            async for item in response:
                yield item
        finally:
            if inject and not injected and token:
                await _release_daily(token)
                _log.warning("budget_notice: warn inject missed, dedupe released")


def _finish_chunk(item: Any) -> bool:
    try:
        choices = getattr(item, "choices", None)
        return bool(choices) and getattr(choices[0], "finish_reason", None) is not None
    except Exception:
        return False


def _make_notice_chunk(finish_chunk: Any, notice: str) -> Optional[Any]:
    """从 finish chunk 复制一个 delta.content=notice 的前置 chunk。"""
    try:
        import copy

        c = copy.deepcopy(finish_chunk)
        c.choices[0].finish_reason = None
        delta = c.choices[0].delta
        delta.content = notice
        for attr in ("tool_calls", "function_call"):
            try:
                setattr(delta, attr, None)
            except Exception:
                pass
        return c
    except Exception as exc:
        _log.warning("budget_notice: make chunk failed %r", exc)
        return None


budget_notice = BudgetNotice()


# ------------------------------------------------------- 模块级 monkey-patch
# responses 的 mock 分支无视 stream 参数、返回完整 ResponsesAPIResponse 对象
# (v1.90.2 responses/main.py MOCK RESPONSE LOGIC)。它进入流式 hook 链后,
# **链条最内层**(第一个注册的流式 hook 或 ProxyLogging 自己的快路径)一执行
# `async for` 就 TypeError 500 —— 在本 hook 里加守卫没用,我们不在最内层
# (2026-08-20 prod 实测:T0 两 callback 链守卫有效,prod 27 callback 链照崩)。
# 唯一收口是 ProxyLogging.async_post_call_streaming_iterator_hook:进链之前
# 用 litellm 自己的 cache-hit 包装器把对象转成合法事件流。


# mock 分支产出的 usage 里 `output_tokens_details` 是空对象。Codex 的
# ResponseCompleted 反序列化把 `reasoning_tokens` 当必填,少这一栏整条流判成
# "stream disconnected before completion: failed to parse ResponseCompleted:
# missing field `reasoning_tokens`" —— 卡片文本(delta)已经渲染出来了,死在最后
# 一个事件上,于是 WS 重试 5 次 → 熔断降 HTTP → 再重试 5 次,同一张余额卡刷 12
# 遍才放弃(2026-09-21 /查余额 实测,客户端日志 codex_core::responses_retry)。
# 真上游永远带这一栏,所以只有我们短路出的 mock 会踩。


def _ensure_reasoning_tokens(response) -> None:
    """给 mock 出来的 responses usage 补 `output_tokens_details.reasoning_tokens`。

    只补缺失的那一栏,已有值不动;任何异常都吞掉——补不上最多退回原状,
    不能让可见性功能把请求本身弄挂。"""
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        details = getattr(usage, "output_tokens_details", None)
        if details is None:
            try:
                from litellm.types.llms.openai import OutputTokensDetails

                usage.output_tokens_details = OutputTokensDetails(
                    reasoning_tokens=0)
            except Exception as exc:
                _log.warning("budget_notice: usage details build failed %r", exc)
            return
        if isinstance(details, dict):
            details.setdefault("reasoning_tokens", 0)
        elif getattr(details, "reasoning_tokens", None) is None:
            details.reasoning_tokens = 0
    except Exception as exc:
        _log.warning("budget_notice: usage normalize failed %r", exc)


def _wrap_nonstream_responses_obj(response, request_data):
    try:
        from litellm.responses.streaming_iterator import (
            CachedResponsesAPIStreamingIterator,
        )

        rd = request_data if isinstance(request_data, dict) else {}
        return CachedResponsesAPIStreamingIterator(
            response=response,
            logging_obj=rd.get("litellm_logging_obj"),
            request_data=rd,
        )
    except Exception as exc:
        _log.warning("budget_notice: choke wrap failed %r", exc)
        return None


def _patch_proxy_streaming_choke():
    try:
        from litellm.proxy.utils import ProxyLogging
    except Exception:
        return  # 单测/精简环境没有 proxy.utils,静默跳过
    orig = ProxyLogging.async_post_call_streaming_iterator_hook
    if getattr(orig, "_bn_nonstream_mock_patched", False):
        return

    async def patched(self, response, user_api_key_dict, request_data):
        if response is not None and not hasattr(response, "__aiter__") \
                and type(response).__name__ == "ResponsesAPIResponse":
            _ensure_reasoning_tokens(response)
            wrapped = _wrap_nonstream_responses_obj(response, request_data)
            if wrapped is not None:
                _log.warning(
                    "budget_notice: choke wrapped ResponsesAPIResponse for streaming")
                response = wrapped
        async for chunk in orig(self, response=response,
                                user_api_key_dict=user_api_key_dict,
                                request_data=request_data):
            yield chunk

    patched._bn_nonstream_mock_patched = True
    ProxyLogging.async_post_call_streaming_iterator_hook = patched


# --------------------------------------- ③ 预算软拦截：patch _virtual_key_max_budget_check
# auth 层的 per-key 预算检查 `_virtual_key_max_budget_check`（litellm/proxy/auth/
# auth_checks.py）在 spend>=max_budget 时抛 BudgetExceededError（→429）。它被
# user_api_key_auth.py **按名字 import**（`from ...auth_checks import
# _virtual_key_max_budget_check`），调用点在调用时从**调用方模块的 globals**
# 解析 —— 所以必须 patch `user_api_key_auth` 命名空间里那个名字（只 patch
# auth_checks 模块属性无效）。同时 belt-and-suspenders patch auth_checks 源头。
#
# gated key 超预算时：吞掉 429、打 mark、返回 None（auth 放行），请求走到
# pre_call 由 pre_call 出 200 友好文案；auth 成功（未超预算）则清 mark。
# 非 gated key / BUDGET_NOTICE_DISABLED / BUDGET_FRIENDLY_MOCK_DISABLED 时原样
# 重抛 429，error_sanitize 出友好 429 body。


def _make_budget_soft_wrapper(orig):
    async def patched(valid_token, *args, **kwargs):
        token = getattr(valid_token, "token", "") or ""
        try:
            result = await orig(valid_token, *args, **kwargs)
        except Exception as e:
            if type(e).__name__ != "BudgetExceededError":
                raise
            if _disabled() or _friendly_mock_disabled() or not _gated(valid_token):
                raise
            _mark_over_budget(
                token, getattr(e, "current_cost", None), getattr(e, "max_budget", None))
            _log.warning(
                "budget_notice: soft-block over-budget alias=%s cost=%r max=%r",
                getattr(valid_token, "key_alias", ""),
                getattr(e, "current_cost", None), getattr(e, "max_budget", None))
            return None
        # 未超预算 —— 清掉可能残留的 mark（额度已重置 / 误标）
        _clear_over_budget_mark(token)
        return result

    patched._bn_budget_soft_patched = True
    return patched


def _patch_virtual_key_budget():
    for modpath in (
        "litellm.proxy.auth.user_api_key_auth",   # 调用方命名空间（关键）
        "litellm.proxy.auth.auth_checks",         # 源头（belt-and-suspenders）
    ):
        try:
            mod = __import__(modpath, fromlist=["_virtual_key_max_budget_check"])
        except Exception:
            continue
        orig = getattr(mod, "_virtual_key_max_budget_check", None)
        if orig is None or getattr(orig, "_bn_budget_soft_patched", False):
            continue
        setattr(mod, "_virtual_key_max_budget_check", _make_budget_soft_wrapper(orig))
        _log.warning("budget_notice: patched _virtual_key_max_budget_check in %s", modpath)


def _patch_proxy_max_budget_limiter():
    """litellm 有**第二道** key 预算检查：默认 pre-call hook
    _PROXY_MaxBudgetLimiter（读 f"{token}_spend" 缓存）。它在 hook 链里排在
    本模块 pre_call 之前，一抛 BudgetExceededError 链条即断、mock 无机会跑
    （2026-08-22 全链 T0 实证：auth 层 patch 已吞并打 mark，仍 429——零价格
    mock 模型的旧 T0 里该缓存为空所以从未暴露）。同款处置：gated key 吞掉、
    打 mark、返回 None 让链继续，本模块 pre_call 出 200。"""
    try:
        from litellm.proxy.hooks.max_budget_limiter import _PROXY_MaxBudgetLimiter
    except Exception:
        return
    orig = _PROXY_MaxBudgetLimiter.async_pre_call_hook
    if getattr(orig, "_bn_budget_soft_patched", False):
        return

    async def patched(self, user_api_key_dict, cache, data, call_type):
        try:
            return await orig(self, user_api_key_dict, cache, data, call_type)
        except Exception as e:
            if type(e).__name__ != "BudgetExceededError":
                raise
            if _disabled() or _friendly_mock_disabled() \
                    or not _gated(user_api_key_dict):
                raise
            token = getattr(user_api_key_dict, "token", "") or ""
            _mark_over_budget(
                token, getattr(e, "current_cost", None), getattr(e, "max_budget", None))
            _log.warning(
                "budget_notice: soft-block hook-layer over-budget alias=%s cost=%r max=%r",
                getattr(user_api_key_dict, "key_alias", ""),
                getattr(e, "current_cost", None), getattr(e, "max_budget", None))
            return None

    patched._bn_budget_soft_patched = True
    _PROXY_MaxBudgetLimiter.async_pre_call_hook = patched
    _log.warning("budget_notice: patched _PROXY_MaxBudgetLimiter.async_pre_call_hook")


def _patch_budget_reservation():
    """capacity patch 镜像还有**第三道**预算闸门：auth 期的乐观预算预留
    reserve_budget_for_request（litellm/proxy/spend_tracking/budget_reservation.py，
    连本次请求的预估成本一起算，零成本模型跳过 —— 旧 T0 mock 零价格所以从未
    暴露；2026-08-22 全链 T0 + 有价 mock 实证）。调用点是函数内 local import，
    patch 模块属性即生效。gated key 超预算：吞掉、打 mark、返回 None（等价
    disable_budget_reservation 的无预留路径，请求继续走到 pre_call 出 200 mock，
    mock 零上游成本、无需预留）。社区版镜像无此模块，静默跳过。"""
    try:
        from litellm.proxy.spend_tracking import budget_reservation as _br
    except Exception:
        return
    orig = getattr(_br, "reserve_budget_for_request", None)
    if orig is None or getattr(orig, "_bn_budget_soft_patched", False):
        return

    async def patched(*args, **kwargs):
        vt = kwargs.get("valid_token")
        try:
            return await orig(*args, **kwargs)
        except Exception as e:
            if type(e).__name__ != "BudgetExceededError":
                raise
            if vt is None or _disabled() or _friendly_mock_disabled() \
                    or not _gated(vt):
                raise
            token = getattr(vt, "token", "") or ""
            _mark_over_budget(
                token, getattr(e, "current_cost", None), getattr(e, "max_budget", None))
            _log.warning(
                "budget_notice: soft-block reservation over-budget alias=%s cost=%r max=%r",
                getattr(vt, "key_alias", ""),
                getattr(e, "current_cost", None), getattr(e, "max_budget", None))
            return None

    patched._bn_budget_soft_patched = True
    _br.reserve_budget_for_request = patched
    _log.warning("budget_notice: patched reserve_budget_for_request")


_patch_proxy_streaming_choke()
_patch_virtual_key_budget()
_patch_proxy_max_budget_limiter()
_patch_budget_reservation()
