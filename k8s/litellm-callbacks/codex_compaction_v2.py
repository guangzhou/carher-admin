"""codex_compaction_v2.py — 给「不认 compaction v2」的上游补 Codex 远程压缩。

治的病（2026-08-06 生产）
------------------------
Codex CLI 打 ``gpt-5.6-sol``（该 key 的 per-key alias 指向 ``zerokey-pool-*``）
触发 ``/compact``::

    Fatal error: remote compaction v2 expected exactly one compaction
    output item, got 0 from 1 output items

本地 codex 日志（``~/.codex/logs_2.sqlite``，op=Compact model=gpt-5.6-sol）里
那唯一一个 output item 是::

    {"type":"response.output_item.done","item":{"type":"message",...,
     "text":"我看到你这条消息是空的 🙂 ..."}}

即上游把 ``compaction_trigger`` 当成一条空的用户消息，正常闲聊回了。

为什么 zerokey 拿不到原生压缩（实测，非推测）
--------------------------------------------
1. **网页会话凭据够不着 codex 后端**。从 zero-108 的 ``/app/temp/users.json``
   取网页 ``authorization`` JWT，在同一个 pod 内打
   ``POST chatgpt.com/backend-api/codex/responses``，四组 header 变体
   （codex CLI 头集 / +cookie / 全套浏览器头 / 去掉 chatgpt-account-id）
   **全部 401 Unauthorized**；同一端点、同一 header、同一集群，换成
   chatgpt-acct-108 的 **Codex OAuth token → 200，原生 compaction item**。
   单变量落在 token 上：网页 token 的 ``scp`` 是
   ``model.request/model.read/organization.*``（ChatGPT 产品面），
   client_id 是网页 App；codex token 的 client_id 是 Codex CLI。
   （附证：zk-108 与 chatgpt-acct-108 的 ``chatgpt_account_id`` 完全相同，
   所以是「同一个号、两套凭据」，不是两个号。）
2. **codex-pool 直传路径线上没启用**。``responses.js:71`` 那条
   ``tools && hasTokens() -> handleCodex``（``:546-553`` 整条 SSE
   ``upstream.pipe(res)``，原生 compaction 会原样透传）需要
   ``CODEX_TOKEN_DIR``；实测 litellm-product 里 **0 个 deploy 设了该 env**，
   CM ``zerokey-codex-tokens`` 只有一个 ``.placeholder`` key。
3. **网页路径的代码里没有 compaction 这个概念**。``zk-image-patch`` 九个补丁
   文件全文 grep ``compaction`` 0 命中。两处关键：``responses.js:34-53``
   ``flattenInput()`` 按 ``${role}: ${text}`` 拼文本，trigger 没 role 没
   content → 拼出一行空的 ``"USER: "``；``:445-451`` 出站只造 message。

协议判据（读 openai/codex 源码，非推测）
--------------------------------------
* ``codex-rs/core/src/compact_remote_v2.rs`` —— 复用标准 Responses 流，只在
  ``input`` 末尾追加 ``{"type":"compaction_trigger"}``；只统计
  ``OutputItemDone`` 里的 ``Compaction`` 变体，``compaction_count != 1`` 就
  Fatal；``!saw_completed`` 则报 "stream closed before response.completed"。
* 同文件有测试 ``collect_compaction_output_accepts_additional_output_items``
  —— **额外的非 compaction item 是允许的**，只要 compaction 恰好 1 个。
  （之前误以为"必须唯一"，这里更正；本模块仍然丢 reasoning，理由是它带
  ``encrypted_content`` 会污染下一轮，不是协议要求。）
* ``codex-rs/protocol/src/models.rs`` —— ``Compaction { id: Option<..>,
  encrypted_content: String, ... }``，``encrypted_content`` **是普通 String**，
  Codex 当不透明字符串存、下一轮原样回传。**这是网关可以自造的决定性依据。**

为什么必须有信封 + 回程解码
--------------------------
客户端下一轮会把 compaction item 原样回传。实测两条路都把摘要吞了：

* deepseek 路：回传我们自造的 compaction item → 模型答「我们没聊过」，摘要丢失。
* 真 chatgpt 池：被我们自己的 ``chatgpt_responses_normalize.py:151``
  ``compaction_drop`` 整项删掉（那行比 compaction v2 协议还老）。

所以出站要打自有前缀 ``carher-cmp-v1:``，入站按前缀解回上下文；不是自己的
（官方密文 / 别的网关的信封）无法解，换成一条边界说明而不是静默删除 ——
静默删除的用户感知是「压缩完就失忆」。

竞品对照（三家独立收敛到同一设计）
--------------------------------
``AITabby/opencodex`` 前缀 ``ocmp1.``、``farion1231/cc-switch``
``cc-switch-compaction-v1:``、``JHXSMatthew/codex-localcompact``
``cpa-localcompact.v1.``（唯一真加密）。共同点：自有前缀 + base64 信封 +
只解自己的、外来的丢弃 + **摘要请求必须剥掉 tools/instructions**（否则模型
继续干活而不是做摘要）+ 截断的摘要一律拒收。本模块照抄这三条。

作用范围
--------
只对「api_base 指向 zero-N」的部署生效（见 ``_is_target``）。deepseek 那条路
的压缩仍由 ``deepseek_responses_adapt`` / ``deepseek_id_prefix`` 处理，本模块
不碰，避免双重包装；两边并存时也是幂等的（先跑的那个把 message 换成
compaction，后跑的找不到 message，自然 no-op）。

部署：litellm-callbacks ConfigMap + Deployment 里加一条 subPath volumeMount
（**光加 CM key 不够**，litellm 按 ``/app/<module>.py`` 找文件，缺挂载会
``Could not find module file`` 导致 pod 起不来）+ ``litellm_settings.callbacks``
里加一项，无需重建镜像。

**这一项必须排在 ``chatgpt_responses_normalize`` 之前**（2026-08-07 实测）：
排在它后面时，回程的 compaction item 已被 ``compaction_drop`` 删掉，本模块的
``pre_call`` 钩子看到的 input 里一个 compaction item 都没有（日志里
``pre_call sees`` 一条不出），摘要还原 4 发只跑到 1 发；挪到前面后 4/4 全跑到。
"""
from __future__ import annotations

import base64
import binascii
import contextvars
import json
import logging
import os
import secrets
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

_log = logging.getLogger("codex_compaction_v2")

_RESPONSE_CALL_TYPES = {"responses", "aresponses"}

# 目标部署的判据：api_base 里出现 "://zero-"。
#
# 为什么不按 model 名门控：客户端发的是 ``gpt-5.6-sol``，落到 zerokey 还是真
# chatgpt 池由 per-key alias / fallback 在**路由之后**才决定（同一个组名两种
# 落点）。按名字门控会在真池那条路上把原生 compaction 改坏。api_base 是路由
# 结果，只有 zerokey 部署长这样（``http://zero-108...:8200/v1``），
# chatgpt-acct 是 ``http://chatgpt-acct-151...:4000``。
_TARGET_API_BASE_MARK = os.environ.get("CODEX_COMPACTION_API_BASE_MARK", "://zero-")
_TARGET_MODEL_ID_PREFIX = os.environ.get("CODEX_COMPACTION_MODEL_ID_PREFIX", "zk-")

_TRIGGER_TYPES = {"compaction_trigger"}
_COMPACTION_TYPES = {"compaction", "compaction_summary", "context_compaction"}

# 自有信封前缀。只解自己的；换前缀等于宣布旧会话的压缩项不可解（会退化成边界
# 说明），改动它前先想清楚存量会话。
_ENVELOPE_PREFIX = "carher-cmp-v1:"

_COMPACT_INSTRUCTION = (
    "请把以上完整对话压缩成一份结构化摘要，供后续对话继续使用。要求：\n"
    "1. 保留所有关键结论、已确认的事实、代码/文件/路径等具体标识；\n"
    "2. 保留尚未完成的待办事项和用户明确的偏好约束；\n"
    "3. 丢弃寒暄、重复内容和已被推翻的中间结论；\n"
    "4. 直接输出摘要正文，不要任何前言、解释或 markdown 代码块包裹。"
)

# 压缩这一轮的 system 指令。必须换掉客户端原来的 ``instructions``（Codex 的
# coding agent 基础提示词）—— cc-switch 的注释写明：不换的话模型会继续干活而
# 不是做摘要。
_COMPACT_SYSTEM = (
    "You compress conversation history into a summary that will replace the "
    "history verbatim. Output ONLY the summary text. Do not answer the "
    "conversation, do not call tools, do not add commentary."
)

_SUMMARY_HEADING = "[前序对话摘要 / conversation summary]"
_FOREIGN_NOTICE = (
    "[前序对话曾被压缩，但那份压缩内容由另一个上游生成、本通道无法解开，"
    "因此未能结转。如需早前细节请重新说明。]"
)

# 摘要输出预算：按输入规模给，别让 287k 上下文的摘要挤进 4096。
# 由来：2026-08-06 一例 ``prompt_tokens=287,688`` 而 ``completion_tokens``
# 连续 6 次都是 4096，报 ``max_output_tokens``；那个 4096 是客户端自己发的，
# 所以只在「客户端给的比我们的下限还小」时抬高。
_MIN_OUTPUT_TOKENS = 8192
_MAX_OUTPUT_TOKENS = 65536

# 本请求是不是压缩轮。**必须 contextvar**：SSE 格式化是模块级自由函数，拿不到
# 请求上下文；用模块级变量在 198 常态 40+ 并发下会把包装规则串到别的请求上。
_ACTIVE: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "ccv2_active_compaction", default=False
)


# ── 信封 ────────────────────────────────────────────────────────

def encode_envelope(summary: str, src: str | None = None) -> str:
    payload = {"v": 1, "summary": summary}
    if src:
        payload["src"] = src
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return _ENVELOPE_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii")


def decode_envelope(blob: Any) -> str | None:
    """解自己的信封；不是自己的返回 None（调用方据此走边界说明分支）。"""
    if not isinstance(blob, str) or not blob.startswith(_ENVELOPE_PREFIX):
        return None
    body = blob[len(_ENVELOPE_PREFIX):]
    try:
        pad = "=" * (-len(body) % 4)
        raw = base64.urlsafe_b64decode(body + pad)
        obj = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    summary = obj.get("summary") if isinstance(obj, dict) else None
    return summary if isinstance(summary, str) and summary.strip() else None


# ── 门控 ────────────────────────────────────────────────────────

def _api_base_of(kwargs: dict[str, Any]) -> str:
    for holder in (kwargs, kwargs.get("litellm_params"), kwargs.get("optional_params")):
        if isinstance(holder, dict):
            base = holder.get("api_base")
            if isinstance(base, str) and base:
                return base
    return ""


def _model_id_of(kwargs: dict[str, Any]) -> str:
    """部署 id（``zk-108-gpt-5.6-sol`` / ``chatgpt-acct-151-gpt-5.6-sol``）。"""
    for holder in (kwargs, kwargs.get("litellm_params"), kwargs.get("optional_params")):
        if isinstance(holder, dict):
            info = holder.get("model_info")
            if isinstance(info, dict):
                mid = info.get("id")
                if isinstance(mid, str) and mid:
                    return mid
    return ""


def _is_target(kwargs: dict[str, Any]) -> bool:
    """两个独立信号任一命中即算 zerokey 落点。

    两个都取是因为「pre_deployment 钩子的 kwargs 里一定有 api_base」这条我没有
    实测证据（litellm ``utils.py:1817`` 是在 router 合并部署参数之后调用的，
    按理都在，但按理不算数）。所以再挂一个 ``model_info.id`` 前缀判据兜底，
    并在两者皆空时打一条诊断日志（只在压缩轮打，天然限频）。
    """
    if _TARGET_API_BASE_MARK in _api_base_of(kwargs):
        return True
    if _model_id_of(kwargs).startswith(_TARGET_MODEL_ID_PREFIX):
        return True
    return False


def _has_trigger(items: Any) -> bool:
    return isinstance(items, list) and any(
        isinstance(i, dict) and i.get("type") in _TRIGGER_TYPES for i in items)


def _text_item(text: str, role: str = "user") -> dict[str, Any]:
    return {"type": "message", "role": role,
            "content": [{"type": "input_text", "text": text}]}


# ── 入站 ────────────────────────────────────────────────────────

def _restore_compaction_items(items: list, counts: dict[str, int]) -> list:
    """回程：把 compaction item 解回普通消息。

    自己的信封 → 摘要正文；外来密文 → 边界说明。两种都不能原样留着：
    上游（zerokey 网页路径）不认这个 item type，``flattenInput`` 会把它拼成
    一行空的 ``"USER: "``，等于凭空多一条空消息。
    """
    out = []
    for it in items:
        if not (isinstance(it, dict) and it.get("type") in _COMPACTION_TYPES):
            out.append(it)
            continue
        summary = decode_envelope(it.get("encrypted_content"))
        if summary:
            out.append(_text_item(f"{_SUMMARY_HEADING}\n{summary}"))
            counts["compaction_restored"] = counts.get("compaction_restored", 0) + 1
        else:
            out.append(_text_item(_FOREIGN_NOTICE))
            counts["compaction_foreign_notice"] = counts.get("compaction_foreign_notice", 0) + 1
    return out


def _output_budget(items: list) -> int:
    """按输入规模给摘要预算（粗估 1 token ≈ 2.5 字符）。"""
    chars = len(json.dumps(items, ensure_ascii=False)) if items else 0
    want = max(_MIN_OUTPUT_TOKENS, min(_MAX_OUTPUT_TOKENS, chars // 40))
    return want


def _rewrite_trigger(data: dict[str, Any], counts: dict[str, int]) -> None:
    """就地把 trigger 换成显式摘要指令，并把这一轮改造成「纯摘要请求」。"""
    items = [i for i in data.get("input") or []
             if not (isinstance(i, dict) and i.get("type") in _TRIGGER_TYPES)]
    items.append(_text_item(_COMPACT_INSTRUCTION))
    data["input"] = items

    # 剥掉工具面 —— 留着模型会继续干活而不是做摘要（竞品 cc-switch 同结论）。
    for key in ("tools", "tool_choice", "parallel_tool_calls", "response_format"):
        if data.pop(key, None) is not None:
            counts[f"stripped_{key}"] = 1
    data["instructions"] = _COMPACT_SYSTEM

    cur = data.get("max_output_tokens")
    want = _output_budget(items)
    if not isinstance(cur, int) or cur < want:
        data["max_output_tokens"] = want
        counts["output_budget"] = want

    meta = data.get("litellm_metadata")
    meta = dict(meta) if isinstance(meta, dict) else {}
    meta["codex_compaction_v2"] = True
    data["litellm_metadata"] = meta
    counts["trigger_rewritten"] = counts.get("trigger_rewritten", 0) + 1


def _count_compaction(items: Any) -> int:
    if not isinstance(items, list):
        return 0
    return sum(1 for i in items
               if isinstance(i, dict) and i.get("type") in _COMPACTION_TYPES)


def restore_own_envelopes(data: dict[str, Any], source: str) -> dict[str, Any]:
    """**不设门控**地把「我们自己的信封」解回消息。

    2026-08-07 实测（4 发相同请求只有 1 发跑到了带门控的还原，且恰好就是唯一
    答对暗号的那一发）：``pre_deployment`` 那层的门控信号在这条路上并不稳定。
    而「解自己的信封」根本不需要知道落点 —— ``carher-cmp-v1:`` 前缀只可能是
    本模块发出去的，谁收到都该解。所以这一步提到最早的钩子、去掉门控。

    外来密文（官方 ``litellm_enc:``/别家信封）在这里**一律不碰** —— 那是真
    chatgpt 池的原生上下文，动它就越界了；它的边界说明仍留在带门控的
    ``adapt_inbound`` 里，只对 zerokey 落点生效。
    """
    items = data.get("input")
    if not isinstance(items, list):
        return data
    counts: dict[str, int] = {}
    out = []
    for it in items:
        if isinstance(it, dict) and it.get("type") in _COMPACTION_TYPES:
            summary = decode_envelope(it.get("encrypted_content"))
            if summary:
                out.append(_text_item(f"{_SUMMARY_HEADING}\n{summary}", role="system"))
                counts["own_restored"] = counts.get("own_restored", 0) + 1
                continue
        out.append(it)
    if counts:
        data["input"] = out
        _log.warning("codex_compaction_v2: source=%s counts=%s", source, counts)
    return data


def adapt_inbound(data: dict[str, Any], source: str) -> dict[str, Any]:
    """入站总入口。返回同一个 dict（就地改），非目标部署原样返回。"""
    if not isinstance(data, dict):
        return data
    if not _is_target(data):
        # 压缩轮却没命中门控 —— 要么落点不是 zerokey（正常），要么两个信号在这
        # 个钩子里都取不到（门控失效，静默空转）。打出来才能分清，且只在压缩轮
        # 打，天然限频。
        n_comp = _count_compaction(data.get("input"))
        if _has_trigger(data.get("input")) or n_comp:
            _log.warning(
                "codex_compaction_v2: gate MISS (source=%s trigger=%s compaction=%d "
                "model=%r api_base=%r model_id=%r keys=%s)",
                source, _has_trigger(data.get("input")), n_comp, data.get("model"),
                _api_base_of(data), _model_id_of(data),
                sorted(k for k in data if k in
                       ("api_base", "model_info", "litellm_params", "optional_params",
                        "metadata", "litellm_metadata")))
        return data
    counts: dict[str, int] = {}
    items = data.get("input")
    if isinstance(items, list) and any(
            isinstance(i, dict) and i.get("type") in _COMPACTION_TYPES for i in items):
        data["input"] = _restore_compaction_items(items, counts)
    if _has_trigger(data.get("input")):
        _rewrite_trigger(data, counts)
        # 尽力而为：这里的置位在流式路径上传不到 SSE 层（见
        # _install_process_chunk_patch 的注释），真正生效的是迭代器那一层；
        # 非流式路径还得靠它。
        _ACTIVE.set(True)
    if counts:
        _log.warning("codex_compaction_v2: source=%s counts=%s", source, counts)
    return data


# ── 出站（SSE 字节层）────────────────────────────────────────────
#
# 为什么在字节层做：改 pydantic 事件对象的 ``type`` 会让序列化器失配，
# ``response.completed`` 那一帧崩 PydanticSerializationError，整条流断在最后
# 一帧（2026-08-06 deepseek 侧实测：无 response.completed / 无 [DONE]）。

def _new_cmp_id() -> str:
    return "cmp_" + secrets.token_hex(24)


def _item_text(item: dict) -> str:
    parts = item.get("content")
    if isinstance(parts, str):
        return parts
    if not isinstance(parts, list):
        return ""
    buf = []
    for p in parts:
        if isinstance(p, dict) and isinstance(p.get("text"), str):
            buf.append(p["text"])
    return "".join(buf)


def _to_compaction(item: dict, counts: dict[str, int]) -> bool:
    if not isinstance(item, dict) or item.get("type") != "message":
        return False
    text = _item_text(item)
    if not text.strip():
        return False
    item.clear()
    item["id"] = _new_cmp_id()
    item["type"] = "compaction"
    item["encrypted_content"] = encode_envelope(text)
    counts["wrapped"] = counts.get("wrapped", 0) + 1
    return True


def wrap_sse(text: str, counts: dict[str, int]) -> str:
    """把这一轮的 message 换成 compaction item，并丢掉 reasoning 相关帧。"""
    if "data: " not in text:
        return text
    out_lines: list[str] = []
    changed = False
    drop_ids: set = set()
    for line in text.split("\n"):
        if not line.startswith("data: "):
            out_lines.append(line)
            continue
        payload = line[6:].strip()
        if not payload or payload == "[DONE]":
            out_lines.append(line)
            continue
        try:
            obj = json.loads(payload)
        except ValueError:
            out_lines.append(line)
            continue

        etype = str(obj.get("type") or "")
        item = obj.get("item")

        if isinstance(item, dict) and item.get("type") == "reasoning":
            if item.get("id"):
                drop_ids.add(item["id"])
            changed = True
            continue
        if obj.get("item_id") in drop_ids or "reasoning" in etype:
            changed = True
            continue

        if isinstance(item, dict) and _to_compaction(item, counts):
            changed = True

        resp = obj.get("response")
        if isinstance(resp, dict):
            kept = []
            for o in resp.get("output") or []:
                if isinstance(o, dict) and o.get("type") == "reasoning":
                    changed = True
                    continue
                if isinstance(o, dict) and _to_compaction(o, counts):
                    changed = True
                kept.append(o)
            if kept != (resp.get("output") or []):
                resp["output"] = kept
                changed = True

        out_lines.append("data: " + json.dumps(obj, ensure_ascii=False)
                         if changed else line)
    return "\n".join(out_lines) if changed else text


def is_flagged(it: Any) -> bool:
    """这条流是不是压缩轮 —— 读入站在 ``litellm_metadata`` 打的标记。"""
    lo = getattr(it, "logging_obj", None)
    if lo is None:
        return False
    for holder in (getattr(lo, "model_call_details", None),
                   getattr(lo, "litellm_params", None),
                   getattr(lo, "optional_params", None)):
        if not isinstance(holder, dict):
            continue
        for key in ("litellm_metadata", "metadata"):
            meta = holder.get(key)
            if isinstance(meta, dict) and meta.get("codex_compaction_v2"):
                return True
    return False


def _install_process_chunk_patch() -> None:
    """在流式迭代器里置位 contextvar —— **不能在入站钩子里置位**。

    2026-08-07 实测（第一版就是栽在这里）：入站 ``pre_deployment`` 钩子里
    ``_ACTIVE.set(True)`` 之后，SSE 格式化函数读到的仍是 False，出站一个字节
    没改（日志里入站 ``trigger_rewritten=1`` 有、出站 ``wrapped`` 没有）。
    原因是那个钩子跑在 litellm 的调用协程里，contextvars 的 Context 在建
    task 时拷贝，置位传不回后来做流式响应的那个上下文。

    ``BaseResponsesAPIStreamingIterator._process_chunk`` 与 SSE 格式化在同一
    条流里，deepseek_id_prefix 走的就是这条成例。
    """
    try:
        from litellm.responses.streaming_iterator import (
            BaseResponsesAPIStreamingIterator as _B,
        )
    except Exception as exc:  # pragma: no cover - 环境缺失
        _log.warning("codex_compaction_v2: iterator import failed: %r", exc)
        return
    if getattr(_B, "_ccv2_patched", False):
        return
    _orig = _B._process_chunk

    def _patched(self, chunk):
        evt = _orig(self, chunk)
        try:
            if not _ACTIVE.get() and is_flagged(self):
                _ACTIVE.set(True)
                _log.warning("codex_compaction_v2: stream flagged as compaction")
        except Exception as exc:
            _log.warning("codex_compaction_v2: process_chunk error: %r", exc)
        return evt

    _B._process_chunk = _patched
    _B._ccv2_patched = True
    _log.warning("codex_compaction_v2: _process_chunk patched")


def _install_sse_patch() -> None:
    """链式 patch ``proxy_server._format_streaming_sse_chunk``（SSE 单一收口）。

    deepseek_id_prefix 也 patch 同一个函数；链式包装互不冲突，各自靠自己的
    contextvar 门控，未命中的请求一个字节都不碰。
    """
    try:
        from litellm.proxy import proxy_server as _ps
    except Exception as exc:  # pragma: no cover - 环境缺失
        _log.warning("codex_compaction_v2: sse patch import failed: %r", exc)
        return
    if getattr(_ps, "_ccv2_sse_patched", False):
        return
    _orig = getattr(_ps, "_format_streaming_sse_chunk", None)
    if _orig is None:
        _log.warning("codex_compaction_v2: _format_streaming_sse_chunk missing, patch skipped")
        return

    def _patched(chunk):
        out = _orig(chunk)
        try:
            if not _ACTIVE.get():
                return out
            counts: dict[str, int] = {}
            if isinstance(out, bytes):
                out = wrap_sse(out.decode("utf-8", "replace"), counts).encode("utf-8")
            elif isinstance(out, str):
                out = wrap_sse(out, counts)
            if counts:
                _log.warning("codex_compaction_v2: sse %s", counts)
        except Exception as exc:
            _log.warning("codex_compaction_v2: sse patch error: %r", exc)
        return out

    _ps._format_streaming_sse_chunk = _patched
    _ps._ccv2_sse_patched = True
    _log.warning("codex_compaction_v2: _format_streaming_sse_chunk patched")


_install_process_chunk_patch()
_install_sse_patch()


class CodexCompactionV2(CustomLogger):
    """两层入站：

    * ``async_pre_call_hook``（路由前）—— 只做「解自己的信封」，不需要门控，
      而且必须尽早：链上还有别的回调会把 compaction item 整项删掉
      （``chatgpt_responses_normalize:151`` 的 ``compaction_drop``）。
    * ``async_pre_call_deployment_hook``（路由后）—— trigger 改写、外来密文的
      边界说明，这些必须知道落点是不是 zerokey 才能做。
    """

    async def async_pre_call_hook(self, user_api_key_dict: Any, cache: Any,
                                  data: dict, call_type: str) -> Any:
        try:
            if not isinstance(data, dict):
                return None
            if str(call_type) not in _RESPONSE_CALL_TYPES:
                return None
            n = _count_compaction(data.get("input"))
            if n:
                _log.warning("codex_compaction_v2: pre_call sees %d compaction item(s) "
                             "model=%r", n, data.get("model"))
            return restore_own_envelopes(data, "pre_call:%s" % call_type)
        except Exception as exc:
            _log.warning("codex_compaction_v2: pre_call error: %r", exc)
        return None

    async def async_pre_call_deployment_hook(self, kwargs: dict[str, Any], call_type: Any) -> Any:
        try:
            if str(call_type) in _RESPONSE_CALL_TYPES or "responses" in str(call_type):
                if isinstance(kwargs, dict):
                    return adapt_inbound(kwargs, "pre_deployment:%s" % call_type)
        except Exception as exc:
            _log.warning("codex_compaction_v2: pre_deployment error: %r", exc)
        return None


codex_compaction_v2 = CodexCompactionV2()
