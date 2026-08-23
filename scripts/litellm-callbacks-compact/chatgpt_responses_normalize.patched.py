"""Normalize OpenAI Responses items for the ChatGPT account pool."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

_log = logging.getLogger("chatgpt_responses_normalize")
# Every model group served by the ChatGPT (codex) pool must be listed here: the
# pool speaks one dialect and the normalizations below are what make a spec-legal
# Responses request survive it. 2026-08-03: added gpt-5.4 / gpt-5.2 /
# gpt-5.3-codex after proving the omission was a defect, not a deliberate carve-out
# -- single-variable probe on the SAME acct (acct-82):
#   chatgpt-gpt-5.4 + previous_response_id -> primary fails, falls back
#   chatgpt-gpt-5.4 without it             -> 200 on acct-82-gpt-5.4
#   chatgpt-gpt-5.5 + previous_response_id -> 200 (allowlisted, so it gets dropped)
# i.e. the upstream rejects previous_response_id for ALL pool models; 5.5 only
# survived because normalization ran for it. Same story for a bare-string
# ``input`` (upstream 400 ``{"detail":"Input must be a list"}``).
# The "too big a blast radius" note that previously justified the narrow list is
# superseded: the two key-scoped transforms it worried about
# (_DROP_ALL_REASONING_ALIASES / _MESSAGE_ONLY_ALIASES) are both EMPTY sets and
# therefore never fire, and the rest are pool-protocol fixes 5.5/5.6 have run for
# months.
_TARGET_MODEL_PARTS = ("gpt-5.5", "chatgpt-gpt-5.5", "chatgpt-pool-gpt-5.5", "gpt-5.6", "chatgpt-gpt-5.6",
                       "gpt-5.4", "gpt-5.2", "gpt-5.3-codex", "image-2")
_TARGET_ROLES = {"system", "developer"}
_RESPONSE_CALL_TYPES = {"responses", "aresponses"}
_DROP_ALL_REASONING_ALIASES = set()
_MESSAGE_ONLY_ALIASES = set()
# ── encrypted_content 放行灰度（2026-08-22）──────────────────────────────
# 实测（直打 acct pod 探针 probe_enc2/enc4）：chatgpt 上游对跨号密文已是静默
# 容忍（单块/双外来块/A+B 混块/5.6-sol/high effort 全 200，解不开就忽略），
# 不再是 2026-06 的 invalid_encrypted 400。放行密文块拿回推理连续性
# （官方口径：工具调用 SWE-bench +3%、缓存利用率 40→80%）。
# 灰度按 key_alias：名单内的 key 不剥 encrypted_content；其余流量行为不变。
# 兜底：① 路由后阶段目标 deployment 非 chatgpt/ 渠道（wangsu fallback、
# zerokey web 混池）→ 强制剥，外渠道永不收密文；② fallback guard 对
# encrypted 类 400 放行 fallback（目标已被①剥净，wangsu 接得住）。
_KEEP_ENCRYPTED_ALIASES = {"enc-canary-01"}

# ---- gateway 压缩（治超巨会话，per-key 灰度）--------------------------------
# 依据 2026-08-23 实测：2-8MB 请求占 6.5% 数量/43% 字节、每轮计费 ~50 万 tokens
# （狂耗账号 7d 桶）；>16MiB 连 WS 增量都上不去。v1=inline 裁剪（参照 codex harness
# auto-compact 思路）：保首条锚点 + 尾部预算内最近项，中间折叠为一条标记消息。
# 上游本就把超上下文部分截断扔掉——裁剪只是把"扔"提前到网关，省双向带宽+计费 tokens。
_COMPACT_ALIASES = {"compact-canary-01"}
_COMPACT_MIN_BYTES = 2 * 1024 * 1024     # 触发：input 序列化 >2MB
_COMPACT_ASSISTANT_MAX_BYTES = 40 * 1024 # assistant 单条上限（≈官方10K tokens）


def _compact_input_items(items: list[Any]) -> tuple[list[Any], dict[str, int]]:
    """网关压缩（结构参照 codex harness，2026-08-23 源码级调研）：
    ① 绝不删项、绝不破坏工具调用配对——只把陈旧工具输出的 output **内容**替换为
       截断占位（同官方 trim_function_call_history_to_fit_context_window 思路）；
    ② 用户消息全保（官方 build_compacted_history：用户消息是任务定义，预算内倒序优先最新）；
    ③ 超长 assistant 消息截到 ~10K tokens 等价（官方 MAX_RETAINED_AGENT_MESSAGE_TOKENS）；
    ④ 从最老的工具输出开始改写，直到总量入预算；改写不足以达标时不再硬删（保结构安全）。
    幂等：已被本函数改写过的输出带占位前缀，跳过。失败原样返回。"""
    try:
        total = len(json.dumps(items, ensure_ascii=False))
    except Exception:
        return items, {}
    if total < _COMPACT_MIN_BYTES or len(items) < 8:
        return items, {}
    marker_prefix = "[gateway-truncated:"
    out = list(items)
    counts: dict[str, int] = {"compact_kb_before": total // 1024}
    cur = total
    # 确定性逐项规则（K5 修正）：触发后对**所有**超阈值项统一变换，绝不按预算提前停——
    # 预算驱动会让掏空前沿逐轮前移，同一 item 两轮内容不同 → acct pod WS 增量前缀账本
    # 必然 prefix_break → 压缩会话永远全量。恒同变换 ⇒ 前缀稳定 ⇒ 与增量兼容。
    for i, it in enumerate(out):
        if not isinstance(it, dict):
            continue
        t = str(it.get("type") or "")
        if t in ("function_call_output", "custom_tool_call_output", "local_shell_call_output"):
            o = it.get("output")
            osz = len(json.dumps(o, ensure_ascii=False)) if o is not None else 0
            if osz < 8192:
                continue
            ostr = o if isinstance(o, str) else json.dumps(o, ensure_ascii=False)
            if ostr.startswith(marker_prefix):
                continue  # 幂等
            new_it = dict(it)
            new_it["output"] = "%s %d chars omitted; head] %s" % (marker_prefix, osz, ostr[:512])
            out[i] = new_it
            cur -= osz - len(new_it["output"])
            counts["compact_tool_outputs_truncated"] = counts.get("compact_tool_outputs_truncated", 0) + 1
        elif t in ("message", "") and it.get("role") == "assistant":
            try:
                c = it.get("content")
                csz = len(json.dumps(c, ensure_ascii=False)) if c is not None else 0
            except Exception:
                continue
            if csz <= _COMPACT_ASSISTANT_MAX_BYTES:
                continue
            txt = None
            if isinstance(c, list) and c and isinstance(c[0], dict):
                txt = c[0].get("text")
            elif isinstance(c, str):
                txt = c
            if not isinstance(txt, str) or txt.startswith(marker_prefix):
                continue
            new_it = dict(it)
            head = txt[:_COMPACT_ASSISTANT_MAX_BYTES]
            new_txt = "%s assistant msg %d chars omitted; head] %s" % (marker_prefix, len(txt), head)
            if isinstance(c, str):
                new_it["content"] = new_txt
            else:
                nc = [dict(c[0]) if isinstance(c[0], dict) else c[0]] + list(c[1:])
                if isinstance(nc[0], dict):
                    nc[0]["text"] = new_txt
                new_it["content"] = nc
            out[i] = new_it
            cur -= csz - len(new_txt)
            counts["compact_assistant_truncated"] = counts.get("compact_assistant_truncated", 0) + 1
        # 用户消息/reasoning/调用项: 一律不动（任务定义与执行骨架）
    if len(counts) <= 1:
        return items, {}
    counts["compact_kb_after"] = max(0, cur) // 1024
    return out, counts


# Cross-provider tool-call contamination cleanup.
# A conversation history produced under an Anthropic model carries tool_use
# ids like ``toolu_...`` and function names containing characters outside
# ``^[a-zA-Z0-9_-]+$`` (e.g. Chinese / dots). Replaying such history to the
# ChatGPT (OpenAI Responses) pool makes the upstream reject the whole request
# with 400 ``invalid_request_error`` on ``input[N].id`` / ``input[N].name``,
# which then cools the entire pool. We rewrite those fields deterministically
# (same dirty value -> same clean value) so function_call<->output pairing and
# name<->tool-definition linkage survive.
_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")
_ID_STRIP_RE = re.compile(r"[^a-zA-Z0-9]")
_TOOL_CALL_TYPES = {"function_call", "custom_tool_call", "tool_call", "local_shell_call"}


def _sanitize_name(name: Any) -> tuple[Any, bool]:
    if not isinstance(name, str) or _NAME_RE.search(name) is None:
        return name, False
    base = _NAME_RE.sub("_", name)
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    fixed = ("%s_%s" % (base, digest))[:64]
    return fixed, True


def _remap_tool_id(val: Any, prefix: str) -> tuple[Any, bool]:
    if isinstance(val, str) and val.startswith("toolu_"):
        rest = _ID_STRIP_RE.sub("", val[len("toolu_"):]) or "0"
        return prefix + rest, True
    return val, False


# A prefix allowlist ("rewrite when it looks like toolu_") cannot hold: every
# non-ChatGPT fallback target stamps its own shape. Observed on 198 prod:
#   wangsu qwen3.7-plus via anthropic gw, non-streaming -> ``toolu_<24hex>``
#   wangsu qwen3.7-plus via anthropic gw, *streaming*   -> ``call_<24hex>``
#   any custom_openai/compat entry (glm-5.2, qwen3.7-plus) -> ``call_<24hex>``
#   kimi-k3                                            -> ``shell_0``
# Codex always streams, so the ``call_*`` shape is the common case and the old
# toolu_-only rule let it through untouched -> upstream 400
# ``Invalid 'input[N].id': 'call_...'. Expected an ID that begins with 'fc'.``
#
# The expected prefix is decided by the ITEM TYPE, not by one global constant:
# ``function_call`` wants ``fc_`` but ``custom_tool_call`` wants ``ctc_``. A first
# revision forced ``fc_`` on every tool-call type and broke codex custom tools
# with ``Expected an ID that begins with 'ctc'`` (70 hits in 15 min) — worse than
# the bug it fixed. Types whose expected prefix we have not confirmed upstream
# are deliberately left to the legacy ``toolu_``-only rule rather than guessed.
#
# Pairing rides on ``call_id``, never on ``id``, so rewriting ``id`` is safe; the
# rewrite is deterministic so a replayed history stays stable.
_TOOL_ID_EXPECTED_PREFIX = {
    "function_call": "fc_",
    "custom_tool_call": "ctc_",
}
_TOOL_ID_KNOWN_PREFIXES = ("toolu_", "call_", "ctc_", "fc_")


def _force_tool_id_prefix(val: Any, want: str) -> tuple[Any, bool]:
    if not isinstance(val, str) or val.startswith(want):
        return val, False
    body = val
    for prefix in _TOOL_ID_KNOWN_PREFIXES:
        if body.startswith(prefix):
            body = body[len(prefix):]
            break
    body = _ID_STRIP_RE.sub("", body) or "0"
    return want + body, True


def _is_target_model(model: Any) -> bool:
    return isinstance(model, str) and any(part in model for part in _TARGET_MODEL_PARTS)



def _content_to_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict):
            for key in ("text", "input_text", "output_text"):
                value = part.get(key)
                if isinstance(value, str):
                    parts.append(value)
                    break
    text = "\n".join(p for p in parts if p).strip()
    return text or None


def _reasoning_has_summary_text(item: dict[str, Any]) -> bool:
    summary = item.get("summary")
    if isinstance(summary, str):
        return bool(summary.strip())
    if not isinstance(summary, list):
        return False
    for part in summary:
        if isinstance(part, str) and part.strip():
            return True
        if isinstance(part, dict):
            for key in ("text", "summary_text"):
                value = part.get(key)
                if isinstance(value, str) and value.strip():
                    return True
    return False


def _normalize_item(item: Any, drop_all_reasoning: bool = False, keep_encrypted: bool = False) -> tuple[Any | None, str | None]:
    if not isinstance(item, dict):
        return item, None

    if drop_all_reasoning and item.get("type") == "reasoning":
        return None, "key_scoped_reasoning_drop"

    if item.get("type") == "compaction":
        return None, "compaction_drop"

    if item.get("type") == "message" and item.get("role") in _TARGET_ROLES:
        text = _content_to_text(item.get("content"))
        if text:
            out = dict(item)
            out.pop("type", None)
            out["role"] = "system"
            out["content"] = text
            return out, "system_message"
        return item, None

    changed: list[str] = []
    out = dict(item)

    has_encrypted_content = "encrypted_content" in out
    item_id = out.get("id")
    if isinstance(item_id, str) and (item_id.startswith("encitem_") or (item.get("type") == "reasoning" and has_encrypted_content)):
        out.pop("id", None)
        changed.append("encrypted_item_id_strip")

    if has_encrypted_content:
        if keep_encrypted:
            changed.append("encrypted_keep")
        else:
            out.pop("encrypted_content", None)
            changed.append("encrypted_content_strip")

    # Anthropic->OpenAI tool-call contamination cleanup (see module docstring).
    if item.get("type") in _TOOL_CALL_TYPES or isinstance(out.get("name"), str):
        new_name, name_changed = _sanitize_name(out.get("name"))
        if name_changed:
            out["name"] = new_name
            changed.append("func_name_sanitize")
    want = _TOOL_ID_EXPECTED_PREFIX.get(item.get("type"))
    if want:
        new_id, id_changed = _force_tool_id_prefix(out.get("id"), want)
        if id_changed:
            out["id"] = new_id
            changed.append("tool_id_prefix_force")
    else:
        new_id, id_changed = _remap_tool_id(out.get("id"), "fc_")
        if id_changed:
            out["id"] = new_id
            changed.append("toolu_id_remap")
    new_call_id, call_id_changed = _remap_tool_id(out.get("call_id"), "call_")
    if call_id_changed:
        out["call_id"] = new_call_id
        changed.append("toolu_call_id_remap")

    if item.get("type") == "reasoning" and not _reasoning_has_summary_text(out) and "encrypted_content" not in out:
        kind = "+".join(changed + ["empty_reasoning_drop"]) if changed else "empty_reasoning_drop"
        return None, kind

    if changed:
        return out, "+".join(changed)

    return item, None


def _force_tool_ids_only(data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Model-agnostic minimal pass: only make tool-call ``id``s ``fc_``-prefixed.

    ``_TARGET_MODEL_PARTS`` is a *name* allowlist and it misses model groups the
    same way the old ``toolu_``-only id rule missed id shapes: 2026-08-02 the
    ``fc``-prefix 400 kept firing for ``chatgpt-gpt-5.4`` / ``gpt-5.2`` after the
    id fix landed, because normalization never ran for those groups at all.

    Widening ``_TARGET_MODEL_PARTS`` would also switch on the heavy transforms
    (previous_response_id drop, encrypted_content strip, empty-reasoning drop,
    compaction drop) for traffic that has never seen them — too big a blast
    radius for this defect. So the id rewrite, and only the id rewrite, runs for
    every model; everything else stays gated as before.
    """
    items = data.get("input")
    if not isinstance(items, list):
        return data, 0
    forced = 0
    new_items: list[Any] = []
    for item in items:
        want = _TOOL_ID_EXPECTED_PREFIX.get(item.get("type")) if isinstance(item, dict) else None
        if want:
            new_id, changed = _force_tool_id_prefix(item.get("id"), want)
            if changed:
                item = dict(item)
                item["id"] = new_id
                forced += 1
        new_items.append(item)
    if forced:
        data = dict(data)
        data["input"] = new_items
    return data, forced


def _normalize_data(data: dict[str, Any], source: str) -> dict[str, Any]:
    model = data.get("model")
    if not _is_target_model(model):
        data, forced = _force_tool_ids_only(data)
        if forced:
            _log.info("chatgpt_responses_normalize[%s] model=%s tool_id_fc_force=%d (untargeted model)",
                      source, model, forced)
        return data


    counts: dict[str, int] = {}
    out_data = dict(data)
    md_in = data.get("litellm_metadata")
    key_alias = md_in.get("user_api_key_alias") if isinstance(md_in, dict) else None
    drop_all_reasoning = key_alias in _DROP_ALL_REASONING_ALIASES
    message_only = key_alias in _MESSAGE_ONLY_ALIASES
    keep_encrypted = key_alias in _KEEP_ENCRYPTED_ALIASES
    # 密文只许进 chatgpt/ 渠道：pre_deployment 阶段 model 是 provider 形态
    # （含"/"），目标不是 chatgpt/ 就强制剥——wangsu fallback、zerokey 混池
    # deployment 永不收密文。pre_call 阶段 model 是 group 名（无"/"）不受影响。
    if keep_encrypted and isinstance(model, str) and "/" in model and not model.startswith("chatgpt/"):
        keep_encrypted = False
        counts["foreign_channel_enc_gate"] = 1
    if out_data.pop("previous_response_id", None) is not None:
        counts["previous_response_id_drop"] = 1

    input_items = out_data.get("input")
    # The Responses API accepts ``input`` as EITHER a string or an item array,
    # but the ChatGPT (codex) upstream only accepts the array form and hard-400s
    # a bare string with ``{"detail":"Input must be a list"}`` (2026-08-02:
    # 31 hits / 20 min, all on chatgpt-gpt-5.5). Clients are within spec here,
    # so coerce to the canonical single user message instead of letting the whole
    # request fail over to a fallback target.
    if isinstance(input_items, str) and input_items:
        input_items = [{"role": "user", "content": [{"type": "input_text", "text": input_items}]}]
        out_data["input"] = input_items
        counts["string_input_to_list"] = 1
    if isinstance(input_items, list):
        if key_alias in _COMPACT_ALIASES:
            input_items, _ccounts = _compact_input_items(input_items)
            for _ck, _cv in _ccounts.items():
                counts[_ck] = counts.get(_ck, 0) + _cv
        new_items: list[Any] = []
        for item in input_items:
            if message_only and isinstance(item, dict) and item.get("type") not in (None, "message"):
                counts["key_scoped_non_message_drop"] = counts.get("key_scoped_non_message_drop", 0) + 1
                continue
            new_item, kind = _normalize_item(item, drop_all_reasoning=drop_all_reasoning, keep_encrypted=keep_encrypted)
            if kind:
                counts[kind] = counts.get(kind, 0) + 1
            if new_item is not None:
                new_items.append(new_item)
        out_data["input"] = new_items

    # Keep tool DEFINITION names in lockstep with the sanitized function_call
    # names in the history, else the upstream rejects the tool schema or the
    # model can no longer match a call to its definition.
    tools = out_data.get("tools")
    if isinstance(tools, list):
        new_tools: list[Any] = []
        tools_changed = False
        for tool in tools:
            if not isinstance(tool, dict):
                new_tools.append(tool)
                continue
            tool_out = dict(tool)
            new_name, name_changed = _sanitize_name(tool_out.get("name"))
            if name_changed:
                tool_out["name"] = new_name
                tools_changed = True
            fn = tool_out.get("function")
            if isinstance(fn, dict):
                fn_new_name, fn_changed = _sanitize_name(fn.get("name"))
                if fn_changed:
                    fn_out = dict(fn)
                    fn_out["name"] = fn_new_name
                    tool_out["function"] = fn_out
                    tools_changed = True
            new_tools.append(tool_out)
        if tools_changed:
            out_data["tools"] = new_tools
            counts["tool_def_name_sanitize"] = counts.get("tool_def_name_sanitize", 0) + 1

    if not counts:
        return data

    md = out_data.setdefault("litellm_metadata", {})
    if isinstance(md, dict):
        md["_chatgpt_responses_normalized"] = counts
    # WARNING 级：root logger 默认 WARNING，INFO 会被吞（2026-08-22 排障教训，
    # 本钩子曾因此"隐形"数周——同 CM 的 deepseek_responses_adapt 用 warning 才可见）。
    _log.warning("chatgpt_responses_normalize: source=%s model=%s counts=%s", source, model, counts)
    return out_data


class ChatGPTResponsesNormalize(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict: Any, cache: Any, data: dict, call_type: str) -> Any:
        try:
            if str(call_type) in _RESPONSE_CALL_TYPES and isinstance(data, dict):
                return _normalize_data(data, "pre_call:%s" % call_type)
        except Exception as exc:
            _log.warning("chatgpt_responses_normalize: pre_call error: %r", exc)
        return data

    async def async_pre_call_deployment_hook(self, kwargs: dict[str, Any], call_type: Any) -> Any:
        try:
            if str(call_type) in _RESPONSE_CALL_TYPES and isinstance(kwargs, dict):
                return _normalize_data(kwargs, "pre_deployment:%s" % call_type)
        except Exception as exc:
            _log.warning("chatgpt_responses_normalize: pre_deployment error: %r", exc)
        return kwargs

    async def async_pre_routing_hook(self, model: str, request_kwargs: dict, messages: Any = None, input: Any = None, specific_deployment: bool = False) -> Any:
        try:
            if _is_target_model(model) and isinstance(request_kwargs, dict):
                request_kwargs = dict(request_kwargs)
                request_kwargs.setdefault("model", model)
                if "input" not in request_kwargs and input is not None:
                    request_kwargs["input"] = input
                return _normalize_data(request_kwargs, "pre_routing")
        except Exception as exc:
            _log.warning("chatgpt_responses_normalize: pre_routing error: %r", exc)
        return request_kwargs


# Dev guard: do not route ChatGPT account-pool request-shape errors to Wangsu.
def _install_chatgpt_fallback_guard() -> None:
    try:
        import litellm.router as router_mod
        import litellm.router_utils.fallback_event_handlers as fallback_mod
    except Exception as exc:
        _log.warning("chatgpt_responses_normalize: fallback guard import failed: %r", exc)
        return

    current = getattr(fallback_mod, "run_async_fallback", None)
    if getattr(current, "_chatgpt_responses_guard", False):
        return
    original = current

    def _targets_chatgpt_to_wangsu(original_model_group: Any, fallback_model_group: Any) -> bool:
        if not _is_target_model(original_model_group):
            return False
        groups = fallback_model_group if isinstance(fallback_model_group, list) else [fallback_model_group]
        return any(isinstance(group, str) and "wangsu" in group and "gpt-5.5" in group for group in groups)

    def _is_non_retryable_request_error(exc: Exception) -> bool:
        status = getattr(exc, "status_code", None)
        text = str(exc)

        # encrypted 类错误放行 fallback（2026-08-22）：灰度 key 放行密文后若上游
        # 罕见地返 encrypted 400，fallback 目标在 pre_deployment 已被剥净密文，
        # wangsu 能正常接住 → 用户拿降级回答而不是硬 400。存量流量密文在门口
        # 已剥、不会产生此类错误，对存量零影响。
        _enc_needles = ("encrypted_content", "encrypted content", "encryption boundary")
        if any(needle in text for needle in _enc_needles):
            _log.warning("chatgpt_responses_normalize: encrypted-4xx allowed to fallback: %s", text[:200])
            return False

        # Availability / capacity errors are allowed to use the configured
        # Wangsu fallback. Payload/schema errors are not.
        retryable_needles = (
            "rate limit",
            "rate_limit",
            "quota",
            "quota_exceeded",
            "timeout",
            "timed out",
            "connect",
            "connection",
            "upstream unavailable",
            "service unavailable",
            "temporarily unavailable",
            "Too Many Requests",
        )
        if isinstance(status, int):
            if status in (408, 429) or status >= 500:
                return False
            if status in (400, 401, 403, 404, 422):
                return True
        if any(needle.lower() in text.lower() for needle in retryable_needles):
            return False

        non_retryable_needles = (
            "BadRequestError",
            "invalid_request_error",
            "missing required",
            "Missing required",
            "System messages are not allowed",
            "aresponses() missing",
            "schema",
            "unsupported",
            "content_filter",
            "policy",
        )
        return any(needle in text for needle in non_retryable_needles)

    async def guarded_run_async_fallback(*args: Any, **kwargs: Any) -> Any:
        original_model_group = kwargs.get("original_model_group")
        fallback_model_group = kwargs.get("fallback_model_group")
        original_exception = kwargs.get("original_exception")
        if (
            isinstance(original_exception, Exception)
            and _targets_chatgpt_to_wangsu(original_model_group, fallback_model_group)
            and _is_non_retryable_request_error(original_exception)
        ):
            _log.warning(
                "chatgpt_responses_normalize: blocked non-retryable fallback original_model_group=%s fallback_model_group=%s error=%s",
                original_model_group,
                fallback_model_group,
                type(original_exception).__name__,
            )
            raise original_exception
        return await original(*args, **kwargs)

    guarded_run_async_fallback._chatgpt_responses_guard = True
    fallback_mod.run_async_fallback = guarded_run_async_fallback
    router_mod.run_async_fallback = guarded_run_async_fallback
    _log.info("chatgpt_responses_normalize: installed ChatGPT Responses fallback guard")


_install_chatgpt_fallback_guard()


chatgpt_responses_normalize = ChatGPTResponsesNormalize()
