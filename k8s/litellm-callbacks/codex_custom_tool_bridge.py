"""codex_custom_tool_bridge.py — 让 chat-schema 目标能接住 codex 的 custom tool。

问题（2026-08-02 实测）
----------------------
codex（Codex CLI / Desktop，cursor-* 全部 key）走 Responses 协议，工具里带
``{"type": "custom", "name": "apply_patch", ...}``。LiteLLM 的
``transform_responses_api_tools_to_chat_completion_tools`` 只认 mcp /
web_search / function 三类，**其余走 else 原样透传**。透传到 chat 端点后没有
``function.name``，于是整单 400::

    'name' is a required property - 'tools.4.function'

两条 qwen 兜底都栽在这儿（compat 400；anthropic 网关 500 KeyError 'function'）。
实测网宿只接受"每个 tool 都是 function"这一种形状 —— chat 原生的
``{"type":"custom","custom":{...}}`` 同样 400。

修法必须双向对称
----------------
codex 声明的是 custom 工具，回来就必须是 ``custom_tool_call``；只改出站会让它
拿到一个没声明过的 ``function_call``，等于没修。所以：

* 出站：``custom`` -> function tool（单个 string 参数 ``input``），并把这些
  工具名记进 ContextVar。
* 入站：模型回的 tool_call 若 name 命中该集合，还原成 ``custom_tool_call``，
  ``input`` 取 JSON 参数里的 ``input`` 字段（拿不到就退回原始 arguments 串）。

作用域
------
这两个转换函数**只在 Responses→chat 桥接路径上跑**（chat schema 的部署），
ChatGPT 原生 Responses 目标结构上不经过。再加一道闸：只有请求真的声明了
custom 工具才动手。所以受影响的请求集合 = 今天本来就硬失败的那批。

未覆盖（诚实标注）
------------------
流式的 ``custom_tool_call`` 在真 API 里有自己的 delta 事件类型
（``response.custom_tool_call_input.delta``）。本模块只把 item 的 type/字段
还原对，沿用 function_call 的 delta 事件通道。codex 能否接受这种混搭**尚未用
真客户端验证**，需要闭环回归。
"""
from __future__ import annotations

import contextvars
import json
import logging
from typing import Any

_log = logging.getLogger("codex_custom_tool_bridge")

# 本请求声明过的 custom 工具名。空集合 = 本模块完全不介入。
_custom_tool_names: contextvars.ContextVar = contextvars.ContextVar(
    "codex_custom_tool_names", default=frozenset()
)

_INPUT_PARAM_SCHEMA = {
    "type": "object",
    "properties": {
        "input": {
            "type": "string",
            "description": "Raw text payload for this tool, passed through verbatim.",
        }
    },
    "required": ["input"],
    "additionalProperties": False,
}


def degrade_custom_tools(tools: Any) -> tuple[Any, frozenset]:
    """custom tool -> function tool(单 string 参数)。返回 (新 tools, 被降级的名字)。"""
    if not tools:
        return tools, frozenset()
    names: set = set()
    out: list = []
    changed = False
    for tool in tools:
        if isinstance(tool, dict) and tool.get("type") == "custom" and tool.get("name"):
            names.add(str(tool["name"]))
            out.append({
                "type": "function",
                "name": tool["name"],
                "description": tool.get("description") or "",
                "parameters": _INPUT_PARAM_SCHEMA,
            })
            changed = True
        else:
            out.append(tool)
    if not changed:
        return tools, frozenset()
    return out, frozenset(names)


def custom_tool_names(tools: Any) -> frozenset:
    """从原始请求的 tools 里挑出 custom 工具名。入站还原不依赖任何跨上下文状态。"""
    if not tools:
        return frozenset()
    names = set()
    for t in tools:
        if isinstance(t, dict) and t.get("type") == "custom" and t.get("name"):
            names.add(str(t["name"]))
    return frozenset(names)


def raw_input_from_arguments(arguments: Any) -> str:
    """从 function 的 JSON arguments 里取回原始文本；取不到就原样返回。"""
    if not isinstance(arguments, str) or not arguments:
        return ""
    try:
        parsed = json.loads(arguments)
    except Exception:
        return arguments
    if isinstance(parsed, dict):
        val = parsed.get("input")
        if isinstance(val, str):
            return val
        # 单键 dict 时退回该键的值,避免把 {"patch": "..."} 丢掉
        if len(parsed) == 1:
            only = next(iter(parsed.values()))
            if isinstance(only, str):
                return only
        return arguments
    return arguments


def restore_custom_item(item: dict, names: frozenset) -> tuple[dict, bool]:
    """function_call item -> custom_tool_call item(若 name 命中)。"""
    if not isinstance(item, dict) or item.get("type") != "function_call":
        return item, False
    if str(item.get("name") or "") not in names:
        return item, False
    out = dict(item)
    out["type"] = "custom_tool_call"
    out["input"] = raw_input_from_arguments(item.get("arguments"))
    out.pop("arguments", None)
    item_id = out.get("id")
    if isinstance(item_id, str) and not item_id.startswith("ctc_"):
        body = item_id
        for prefix in ("fc_", "call_", "ctc_", "toolu_"):
            if body.startswith(prefix):
                body = body[len(prefix):]
                break
        out["id"] = "ctc_" + "".join(ch for ch in body if ch.isalnum())
    return out, True


def _as_response_item(d: dict) -> Any:
    """把 dict 包成 Responses 输出项。优先用 litellm 的开放字段容器,失败就退回裸 dict。"""
    try:
        from litellm.types.llms.openai import BaseLiteLLMOpenAIResponseObject
        return BaseLiteLLMOpenAIResponseObject(**d)
    except Exception:
        return d


def install() -> None:
    try:
        from litellm.responses.litellm_completion_transformation.transformation import (
            LiteLLMCompletionResponsesConfig as _Cfg,
        )
    except Exception as exc:  # pragma: no cover
        _log.warning("codex_custom_tool_bridge: import failed, skip: %r", exc)
        return

    if getattr(_Cfg, "_codex_custom_tool_patched", False):
        return

    _orig_tools = _Cfg.transform_responses_api_tools_to_chat_completion_tools

    def _patched_tools(tools=None, *args, **kwargs):
        try:
            new_tools, names = degrade_custom_tools(tools)
            if names:
                _custom_tool_names.set(names)
                tools = new_tools
        except Exception as exc:  # fail-open: 绝不因为本 hook 挡掉请求
            _log.warning("codex_custom_tool_bridge: outbound skipped: %r", exc)
        return _orig_tools(tools, *args, **kwargs)

    # 入站挂在"同时拿得到请求和响应"的那个函数上,名字直接从请求的 tools 里算。
    # 早先版本挂在内层 transform_chat_completion_tools_to_responses_tools 上并靠
    # ContextVar 传名字 —— 实测入站与出站不在同一个 async 上下文,names 永远是空,
    # 模型调了 apply_patch 回来的仍是 function_call。改成从请求现算,无传播风险。
    _orig_resp = _Cfg.transform_chat_completion_response_to_responses_api_response

    def _patched_resp(request_input=None, responses_api_request=None, *args, **kwargs):
        result = _orig_resp(request_input, responses_api_request, *args, **kwargs)
        try:
            req = responses_api_request or {}
            tools = req.get("tools") if isinstance(req, dict) else getattr(req, "tools", None)
            names = custom_tool_names(tools)
            if not names:
                return result
            output = getattr(result, "output", None)
            if output is None and isinstance(result, dict):
                output = result.get("output")
            if not output:
                return result
            n = 0
            for i, it in enumerate(output):
                d = it if isinstance(it, dict) else getattr(it, "__dict__", None)
                if not isinstance(d, dict):
                    continue
                new_d, did = restore_custom_item(dict(d), names)
                if not did:
                    continue
                # 不能原地改 pydantic 对象: ``type`` 是声明字段所以改得动,但 ``input``
                # 不在 ResponseFunctionToolCall 的 schema 里,序列化时会被丢掉(实测
                # type 变成 custom_tool_call 而 input 为空)。整条替换成能装任意字段
                # 的容器。
                output[i] = _as_response_item(new_d)
                n += 1
            if n:
                _log.warning("codex_custom_tool_bridge: restored %d custom_tool_call item(s)", n)
        except Exception as exc:
            _log.warning("codex_custom_tool_bridge: response rewrite skipped: %r", exc)
        return result

    _orig_out = _Cfg.transform_chat_completion_tools_to_responses_tools

    def _patched_out(*args, **kwargs):
        items = _orig_out(*args, **kwargs)
        try:
            names = _custom_tool_names.get()
            if not names or not items:
                return items
            fixed = []
            for it in items:
                as_dict = it if isinstance(it, dict) else getattr(it, "__dict__", None)
                if not isinstance(as_dict, dict):
                    fixed.append(it)
                    continue
                new_d, did = restore_custom_item(dict(as_dict), names)
                if not did:
                    fixed.append(it)
                    continue
                if isinstance(it, dict):
                    fixed.append(new_d)
                else:
                    as_dict.clear()
                    as_dict.update(new_d)
                    fixed.append(it)
            return fixed
        except Exception as exc:
            _log.warning("codex_custom_tool_bridge: inbound skipped: %r", exc)
            return items

    _Cfg.transform_responses_api_tools_to_chat_completion_tools = staticmethod(_patched_tools)
    _Cfg.transform_chat_completion_tools_to_responses_tools = staticmethod(_patched_out)
    _Cfg.transform_chat_completion_response_to_responses_api_response = staticmethod(_patched_resp)
    _Cfg._codex_custom_tool_patched = True
    _log.warning("codex_custom_tool_bridge: patched responses<->chat tool transforms")


# ---------------------------------------------------------------- 流式
# codex 只走流式。桥接迭代器不复用上面那两个转换函数,它在
# ``_queue_final_tool_call_done_events`` 里内联构造 ``"type": "function_call"``,
# 所以非流式修好后流式仍是旧形状(实测: response.completed 的 output 已是
# custom_tool_call,但 output_item.added/done 仍是 function_call)。
#
# 事件流必须自洽,不能只改 added/done 而留下 function_call_arguments.delta ——
# 严格解析器会因为"custom_tool_call 却收到 function 参数增量"而报错。做法:
#   added        -> 改成 custom_tool_call
#   args.delta   -> 吞掉(上游发的是 {"input": "..."} 的 JSON 片段,不是原文)
#   args.done    -> 吞掉,并注入一条带完整原文的 custom_tool_call_input.delta + done
#   item.done    -> 改成 custom_tool_call,input 取回原文
_ARGS_DELTA = "response.function_call_arguments.delta"
_ARGS_DONE = "response.function_call_arguments.done"
_ITEM_ADDED = "response.output_item.added"
_ITEM_DONE = "response.output_item.done"


def _ev_get(ev, key):
    if isinstance(ev, dict):
        return ev.get(key)
    return getattr(ev, key, None)


def _ev_type(ev) -> str:
    t = _ev_get(ev, "type")
    return t if isinstance(t, str) else str(getattr(t, "value", "") or "")


def _item_dict(ev):
    it = _ev_get(ev, "item")
    if isinstance(it, dict):
        return it
    d = getattr(it, "__dict__", None)
    return d if isinstance(d, dict) else None


def _mk_event(payload: dict):
    try:
        from litellm.types.llms.openai import BaseLiteLLMOpenAIResponseObject
        return BaseLiteLLMOpenAIResponseObject(**payload)
    except Exception:
        return payload


def handle_stream_event(state: dict, ev, names: frozenset):
    """返回 (要吐出的事件 或 None 表示吞掉, 需要在其后注入的事件列表)。"""
    et = _ev_type(ev)
    ids = state.setdefault("ids", set())
    buf = state.setdefault("args", {})

    if et == _ITEM_ADDED:
        d = _item_dict(ev)
        if d and d.get("type") == "function_call" and str(d.get("name") or "") in names:
            new_d, did = restore_custom_item(dict(d), names)
            if did:
                ids.add(str(d.get("id") or ""))
                ids.add(str(d.get("call_id") or ""))
                d.clear(); d.update(new_d)
        return ev, []

    if et == _ARGS_DELTA and str(_ev_get(ev, "item_id") or "") in ids:
        buf[str(_ev_get(ev, "item_id"))] = buf.get(str(_ev_get(ev, "item_id")), "") + str(_ev_get(ev, "delta") or "")
        return None, []

    if et == _ARGS_DONE and str(_ev_get(ev, "item_id") or "") in ids:
        iid = str(_ev_get(ev, "item_id"))
        full = str(_ev_get(ev, "arguments") or buf.get(iid, ""))
        raw = raw_input_from_arguments(full)
        seq = _ev_get(ev, "sequence_number") or 0
        common = {"item_id": iid, "output_index": _ev_get(ev, "output_index") or 0}
        return None, [
            _mk_event(dict(common, type="response.custom_tool_call_input.delta",
                           delta=raw, sequence_number=seq)),
            _mk_event(dict(common, type="response.custom_tool_call_input.done",
                           input=raw, sequence_number=seq)),
        ]

    if et == _ITEM_DONE:
        d = _item_dict(ev)
        if d and d.get("type") == "function_call" and str(d.get("name") or "") in names:
            new_d, did = restore_custom_item(dict(d), names)
            if did:
                d.clear(); d.update(new_d)
        return ev, []

    return ev, []


def install_streaming() -> None:
    try:
        from litellm.responses.litellm_completion_transformation.streaming_iterator import (
            LiteLLMCompletionStreamingIterator as _It,
        )
    except Exception as exc:  # pragma: no cover
        _log.warning("codex_custom_tool_bridge: stream import failed, skip: %r", exc)
        return
    if getattr(_It, "_codex_custom_tool_stream_patched", False):
        return

    _orig_anext = _It.__anext__

    async def _patched_anext(self):
        pending = getattr(self, "_ctb_pending", None)
        if pending:
            return pending.pop(0)
        while True:
            ev = await _orig_anext(self)
            try:
                names = getattr(self, "_ctb_names", None)
                if names is None:
                    req = getattr(self, "responses_api_request", None) or {}
                    names = custom_tool_names(req.get("tools") if isinstance(req, dict) else None)
                    self._ctb_names = names
                if not names:
                    return ev
                state = getattr(self, "_ctb_state", None)
                if state is None:
                    state = {}; self._ctb_state = state
                out, inject = handle_stream_event(state, ev, names)
                if inject:
                    self._ctb_pending = list(getattr(self, "_ctb_pending", [])) + inject
                if out is not None:
                    return out
                p = getattr(self, "_ctb_pending", None)
                if p:
                    return p.pop(0)
                continue
            except Exception as exc:  # fail-open
                _log.warning("codex_custom_tool_bridge: stream rewrite skipped: %r", exc)
                return ev

    _It.__anext__ = _patched_anext
    _It._codex_custom_tool_stream_patched = True
    _log.warning("codex_custom_tool_bridge: patched streaming iterator")


# LiteLLM 通过 litellm_settings.callbacks 的 "module.instance" 来 import 本模块;
# 实例本身不需要任何 hook —— import 的副作用(install())才是目的。这与
# encrypted_content_degrade_strip / streaming_output_backfill 同一套路。
try:
    from litellm.integrations.custom_logger import CustomLogger as _CustomLogger

    class CodexCustomToolBridge(_CustomLogger):
        pass

    codex_custom_tool_bridge = CodexCustomToolBridge()
except Exception:  # pragma: no cover
    codex_custom_tool_bridge = None

install()
install_streaming()
