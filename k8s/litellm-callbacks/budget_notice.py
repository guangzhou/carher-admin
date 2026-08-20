"""budget_notice.py — 198 LiteLLM key 日额度可见性（2026-08-19）

治的病
------
key 日额度（claude-code-* $70/天、cursor-* $100/天，北京 0 点重置）用完前没有
任何预警；用完后的 429 又被 error_sanitize 整体置换成「API 异常」，用户第一次
知道自己超额就是一脸懵逼的报错。三件套：

① ``/查余额`` | ``/quota`` —— pre-call 短路，不打上游、零计费，直接返回本 key
   今日用量。chat / responses 路径用 ``data["mock_response"]``（mock_heartbeat
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
③ 100% 的友好 429 不在本文件 —— 见 error_sanitize.py 的 BudgetExceededError
   分支（同批次改动）。预算拒绝发生在 auth 层，pre-call hook 根本不会执行，
   只能走 failure-hook 就地改写。

匹配规则（①）
--------------
取最后一条 user 消息的文本，**按行 strip 后全等**命中 {"/查余额","查余额",
"/quota"}（quota 不分大小写）才触发。按行而非按整条消息，是因为 Cursor /
Claude Code 都会把用户输入包进模板（文件上下文、system-reminder 等），整条
全等永远不命中；按行全等又比子串包含安全 —— 正文里聊到「查余额」三个字不会
误触发（那得独占一行才行，且误触发的代价只是收到一条用量而非模型回答）。

Gate（env，改 deploy env 即生效，无需改代码）
---------------------------------------------
BUDGET_NOTICE_DISABLED=1                       总开关（紧急止血）
BUDGET_NOTICE_KEY_ALIASES=a,b                  精确灰度名单
BUDGET_NOTICE_KEY_PREFIXES=claude-code-,cursor-  前缀放量
两个 env 都未设置时默认 canary：claude-code-liuguoxian-50gj。
BUDGET_NOTICE_WARN_RATIO=0.9                   ② 的阈值

keep two in sync：本文件与 198 CM ``litellm-callbacks`` 的 ``budget_notice.py``。
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re as _re
from typing import Any, List, Optional, Tuple

from litellm.integrations.custom_logger import CustomLogger

_log = logging.getLogger("budget_notice")

# ---------------------------------------------------------------- env gates

_DEFAULT_ALIASES = frozenset({"claude-code-liuguoxian-50gj"})


def _disabled() -> bool:
    return os.environ.get("BUDGET_NOTICE_DISABLED") == "1"


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
    return (
        f"📊 {alias} 今日用量\n"
        f"已用 ${spend:.2f} / 限额 ${max_budget:.2f}（{pct:.0f}%），"
        f"剩余 ${remain:.2f}\n"
        f"{_beijing_reset_str(user_api_key_dict)} 重置。"
        f"用量数据约有 1 分钟延迟。"
    )


def _warn_text(user_api_key_dict: Any) -> str:
    spend = float(getattr(user_api_key_dict, "spend", None) or 0.0)
    max_budget = float(getattr(user_api_key_dict, "max_budget", None) or 0.0)
    pct = spend / max_budget * 100.0 if max_budget else 0.0
    return (
        f"\n\n----\n⚠️ 你的 key 今日额度已用 {pct:.0f}%"
        f"（${spend:.2f}/${max_budget:.2f}），{_beijing_reset_str(user_api_key_dict)} 重置。"
        f"发送 /查余额 可随时查询。（本提醒每天最多一次）"
    )


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


class BudgetNotice(CustomLogger):
    # ---------------------------------------------------------------- ①
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

            text = _usage_text(user_api_key_dict)
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
            _log.warning("budget_notice: pre_call error %r", exc)
            return data
        if shortcut_exc is not None:
            raise shortcut_exc
        return data

    # ---------------------------------------------------------------- ②
    async def async_post_call_streaming_iterator_hook(
        self, user_api_key_dict, response, request_data,
    ):
        inject = False
        notice = ""
        token = ""
        route = getattr(user_api_key_dict, "request_route", "") or ""
        injectable_route = ("chat/completions" in route) or ("messages" in route)
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
            held_stop = None       # 持有中的 text block stop 事件（bytes）
            held_index = None
            block_types = {}       # index -> content_block type
            route = getattr(user_api_key_dict, "request_route", "") or ""
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

                # 非 bytes：chat 路径的 ModelResponseStream 对象。仅在明确是
                # chat/completions 路由时注入 —— responses 路径的 hook 层也是
                # chat 形状（见 litellm-hook-dev skill），贸然注入会污染
                # responses 事件转换。
                if not injected and chat_route and _finish_chunk(item):
                    synthetic = _make_notice_chunk(item, notice)
                    if synthetic is not None:
                        yield synthetic
                        injected = True
                yield item
            if held_stop is not None:
                yield held_stop
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
