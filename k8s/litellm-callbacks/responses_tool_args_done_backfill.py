# responses_tool_args_done_backfill.py — 198 母 router（litellm-product，1.103.2）
#
# 修什么（2026-10-09 实测）
# -------------------------
# chat 客户端（her / Cursor）→ 母 router `/v1/chat/completions` → 母 router 以
# responses 打 chatgpt-acct 叶子 → 把 responses SSE 转回 chat SSE。转回这一步由
# ``OpenAiResponsesToChatCompletionStreamIterator.chunk_parser`` 完成，它只认
# ``response.function_call_arguments.delta``；``output_item.done`` 分支注释写着
# 「参数已经通过 delta 发过了」于是只吐空块，``function_call_arguments.done`` 根本不处理。
#
# ChatGPT 上游对并行工具调用**随机地**不发任何 delta，只在 ``.done`` 给整段参数
# （acct-84 实测 6 路并行：astra 3/4、6-sol 3/5、5.6-sol 4/5 无 delta；单调用 0/6）。
# 这时 chat 客户端收到的每个 tool_call 都是 ``arguments:""``：
#   - OpenClaw 2026.8.x 判 malformed → "LLM request failed."（her-13 报错）
#   - Cursor 等拿 ``{}`` 静默执行（SpendLogs 回传：10-08 23500 个并行调用 15758 个空）
# 上游 BerriAI/litellm #27144（05-05 起 open），修复 PR #45356 截至 10-09 未合并，
# 1.104.2 / main 代码未变。
#
# 怎么修（与 #45356 同思路）
# --------------------------
# 包一层 ``chunk_parser``，按流记录哪些 ``output_index`` 收到过非空 delta：
#   - ``*.arguments.done`` / ``custom_tool_call_input.done``：该 index 没收到过 delta 且
#     done 带非空参数 ⇒ 补发一个只含 arguments 的 tool_call delta（id/name 已由
#     ``output_item.added`` 发过，这里与原生 delta 块同形：id=None, name=None）。
#   - ``output_item.done``（function_call/custom_tool_call）：同条件且还没补过 ⇒ 用
#     ``item.arguments``/``item.input`` 兜底补发（防上游只发 item.done 不发 args.done）。
#   - 其它一切事件、以及已收到 delta 的调用：原样调原函数，逐字节不变 ⇒ 不会重复发参数。
# 补发块的 index 走原生 ``_sequential_tool_call_index``，与 added/delta 块共用同一张映射表。
#
# 不碰：无状态调用方（guardrail handler 直接调 ``translate_responses_chunk_to_openai_stream``，
# 不经 chunk_parser）、非流式路径、/v1/responses 直通路径。
#
# 守卫（按后果分两级，见 skill litellm-198-router-patch §1）
#   - 类/方法/枚举 import 不到 ⇒ 拒装 + error（补丁跑不起来）
#   - 源码指纹读不到或对不上 ⇒ warning + 照装（量具坏 ≠ 行为变；升级后人工 re-diff）
# 开关：env ``RESPONSES_TOOL_ARGS_BACKFILL_DISABLED=1`` ⇒ 不装（回滚无需换 CM）。
# 上游合并 #45356 并升级后删除本文件。

import logging
import os

_LOG = logging.getLogger("LiteLLM Router")
_TAG = "responses_tool_args_done_backfill"

# 只在 1.103.2 该方法源码里成对出现的两句；对不上只告警（见上）。
_FP_TRANSLATE = (
    "per-stream callers already received it via",
    "Chat provider: Invalid function argument delta",
)

_TOOL_ITEM_TYPES = ("function_call", "custom_tool_call")

# 以下由 _install() 填充；单测可直接调用 make_patched_chunk_parser。
_E = None


def _event_type(chunk):
    t = chunk.get("type") if isinstance(chunk, dict) else None
    v = getattr(t, "value", None)
    return v if isinstance(v, str) else t


def _done_payload(chunk, etype, ev):
    """返回 (output_index, args_str) —— 只对「终态带参数」的事件；否则 None。"""
    if etype in (ev["ARGS_DONE"], ev["CUSTOM_DONE"]):
        args = chunk.get("arguments") if etype == ev["ARGS_DONE"] else chunk.get("input")
        return chunk.get("output_index", 0), args
    if etype == ev["ITEM_DONE"]:
        item = chunk.get("item") or {}
        if isinstance(item, dict) and item.get("type") in _TOOL_ITEM_TYPES:
            args = item.get("input") if item.get("type") == "custom_tool_call" else item.get("arguments")
            return chunk.get("output_index", 0), args
    return None


def make_patched_chunk_parser(orig_chunk_parser, ev):
    """ev: 事件名字符串表 {DELTA, CUSTOM_DELTA, ARGS_DONE, CUSTOM_DONE, ITEM_ADDED, ITEM_DONE}。"""

    def chunk_parser(self, chunk):
        if not isinstance(chunk, dict):
            return orig_chunk_parser(self, chunk)
        try:
            synth = _plan(self, chunk)
        except Exception as exc:  # noqa: BLE001  fail-open：补丁自身出错 ⇒ 原样交给原函数
            _LOG.warning("%s: backfill skipped: %r", _TAG, exc)
            synth = None
        return orig_chunk_parser(self, synth if synth is not None else chunk)

    def _plan(self, chunk):
        """返回要替代原事件交给原函数的合成 delta 事件；None = 原样放行。"""
        st = self.__dict__.get("_carher_args_state")
        if st is None:
            # had_args: 已经给客户端发过非空参数的 output_index（delta 或 added 自带）
            st = self.__dict__["_carher_args_state"] = {"had_args": set()}
        etype = _event_type(chunk)
        oi = chunk.get("output_index", 0)

        if etype in (ev["DELTA"], ev["CUSTOM_DELTA"]):
            if chunk.get("delta"):
                st["had_args"].add(oi)
        elif etype == ev["ITEM_ADDED"]:
            item = chunk.get("item") or {}
            if isinstance(item, dict) and item.get("type") in _TOOL_ITEM_TYPES:
                pre = item.get("input") if item.get("type") == "custom_tool_call" else item.get("arguments")
                if pre or chunk.get("arguments"):
                    st["had_args"].add(oi)
        else:
            p = _done_payload(chunk, etype, ev)
            if p is not None:
                idx, args = p
                if idx not in st["had_args"] and isinstance(args, str) and args.strip():
                    st["had_args"].add(idx)
                    # 走原生 delta 分支：同一张 index 映射表、同一个流级 id，形状与真 delta 完全一致。
                    # 被替换掉的原返回值（item.done 的空 Delta / args.done 的空 content 块）
                    # 都不带内容也不带 finish_reason，替换不丢信息。
                    synth = {
                        "type": ev["CUSTOM_DELTA"] if etype == ev["CUSTOM_DONE"] else ev["DELTA"],
                        "output_index": idx,
                        "item_id": chunk.get("item_id") or (chunk.get("item") or {}).get("id"),
                        "delta": args,
                    }
                    return synth
        return None

    chunk_parser.__wrapped__ = orig_chunk_parser
    chunk_parser._carher_args_backfill = True
    return chunk_parser


def _install():
    if os.environ.get("RESPONSES_TOOL_ARGS_BACKFILL_DISABLED") == "1":
        _LOG.warning("%s: disabled by env, NOT patching", _TAG)
        return
    import inspect

    from litellm.completion_extras.litellm_responses_transformation.transformation import (
        OpenAiResponsesToChatCompletionStreamIterator as It,
    )
    from litellm.types.llms.openai import ResponsesAPIStreamEvents as E

    cur = It.__dict__.get("chunk_parser")
    if getattr(cur, "_carher_args_backfill", False):
        _LOG.info("%s: already installed, skip", _TAG)
        return
    if cur is None or not callable(cur):
        _LOG.error("%s: chunk_parser not found on iterator, NOT patching", _TAG)
        return

    ev = {
        "DELTA": E.FUNCTION_CALL_ARGUMENTS_DELTA.value,
        "CUSTOM_DELTA": E.CUSTOM_TOOL_CALL_INPUT_DELTA.value,
        "ARGS_DONE": E.FUNCTION_CALL_ARGUMENTS_DONE.value,
        "CUSTOM_DONE": E.CUSTOM_TOOL_CALL_INPUT_DONE.value,
        "ITEM_ADDED": E.OUTPUT_ITEM_ADDED.value,
        "ITEM_DONE": E.OUTPUT_ITEM_DONE.value,
    }

    try:
        src = inspect.getsource(It.translate_responses_chunk_to_openai_stream)
        missing = [fp for fp in _FP_TRANSLATE if fp not in src]
        if missing:
            _LOG.warning("%s: source does not show %r (litellm upgraded?) — installing anyway, re-diff", _TAG, missing)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("%s: cannot read translate source (%r) — installing anyway", _TAG, exc)

    It.chunk_parser = make_patched_chunk_parser(cur, ev)
    _LOG.warning("%s: installed (patched OpenAiResponsesToChatCompletionStreamIterator.chunk_parser)", _TAG)


try:
    _install()
except Exception as _exc:  # noqa: BLE001
    _LOG.error("%s: install failed, skipped: %r", _TAG, _exc)


# callbacks 挂载壳（逻辑全在 import 副作用里）
try:
    from litellm.integrations.custom_logger import CustomLogger

    class ResponsesToolArgsDoneBackfill(CustomLogger):
        pass

    responses_tool_args_done_backfill = ResponsesToolArgsDoneBackfill()
except Exception as _exc:  # noqa: BLE001
    _LOG.error("%s: callback shell unavailable: %r", _TAG, _exc)
