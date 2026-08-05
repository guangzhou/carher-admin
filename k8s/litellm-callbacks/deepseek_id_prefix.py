"""deepseek_id_prefix.py — 给 DeepSeek 的出站 item id 补 OpenAI 规范前缀。

问题（2026-08-06 生产）
----------------------
用户本地 Codex 打 ``deepseek-v4-flash-responses``，输出里出现裸文本::

    <｜｜DSML｜｜invoke name="exec_command">
    <｜｜DSML｜｜parameter name="cmd" string="true">ls -la</｜｜DSML｜｜parameter>

即工具调用**没被解析成结构化 function_call，降级成文本渲染**了。
上游全程 200、零错误 —— 纯静默故障，只看状态码永远发现不了。

判据是**对照组**（同一客户端、同一套工具、同一份配置，只换模型）::

                        GPT（好使）                DeepSeek（泄漏）
    function_call.id    fc_068c8f3f17af7f06...    f2104000-ca0f-4889-...
    reasoning.id        encitem_bGl0ZWxsbTp...    7a4e8e3b-7dcc-4101-...

DeepSeek 返回**裸 UUID**，没有 OpenAI Responses 规范要求的 ``fc_`` 前缀。
直连 api.deepseek.com 复测确认是上游本来就这样，不是我们网关改的。

Codex 的解析器按前缀判断这是不是一条工具调用项（同类实现见
Wei-Shaw/sub2api ``openai_codex_transform.go``：「id 必须以 "fc" 开头，
上游会校验 *Expected an ID that begins with 'fc'.*»）。前缀不对 ->
不认 -> 当成普通文本渲染。

连带解释另外两个现象：

* **模型"编造"不存在的工具名**（``exec`` / ``exec_command``，而客户端声明的
  是 ``ls`` / ``read_file`` / ``grep``）—— 它没编造。上一轮泄漏的文本被当成
  对话内容存进历史，模型照着学，越滚越离谱。
* **入站 ``reasoning`` 恒为 0**（实测 6 次采样，``typecount={'message': 25,
  'function_call': 654, 'function_call_output': 654}``）—— reasoning 的 id
  同样不合格，Codex 存不下，下一轮无从回传。于是
  ``deepseek_responses_adapt`` 给每轮补占位，一单插到 618 条。**那是症状，
  不是病根**（618 条占位本身已单独 A/B 过 12 次，与泄漏无因果）。

方案
----
只改**出站**：``function_call`` -> ``fc_``、``reasoning`` -> ``rs_``、
其余 tool call 类型 -> ``fc_``。

**入站不做还原** —— 实测 DeepSeek 对回传 id 的格式完全不挑::

    原样裸 UUID     -> 200
    全部加 fc_      -> 200
    fc_/rs_ 分类型  -> 200
    干脆不带 id     -> 200

省掉还原这一步，也就没有「还原错了导致配对失败」的风险。

``call_id`` **一个字节都不碰** —— DeepSeek 靠它做 call/output 配对
（见 [[feedback_deepseek_pairs_tool_calls_by_adjacency]]），改了会整单 400。
只动 ``id``。

作用域
------
``_is_deepseek()`` 只认 model 名里含 deepseek 的部署，其它模型第一行就 return。
幂等：已带前缀的 id 不会被二次加前缀。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

_log = logging.getLogger("deepseek_id_prefix")

# item type -> OpenAI 规范前缀。call_id 不在此列，永远不碰。
_PREFIX_BY_TYPE = {
    "reasoning": "rs_",
    "message": "msg_",
    "function_call": "fc_",
    "custom_tool_call": "fc_",
    "tool_call": "fc_",
    "local_shell_call": "fc_",
    "tool_search_call": "fc_",
    "mcp_tool_call": "fc_",
    "web_search_call": "ws_",
}

# 已经合规的前缀，遇到就跳过（幂等）。
_KNOWN_PREFIXES = ("fc_", "rs_", "msg_", "ws_", "encitem_", "resp_", "call_")

_SSE_DATA = re.compile(rb"^data: (.+)$", re.MULTILINE)


def _is_deepseek(model: Any) -> bool:
    return isinstance(model, str) and "deepseek" in model.lower()


def _needs_prefix(item_id: Any) -> bool:
    return isinstance(item_id, str) and bool(item_id) and not item_id.startswith(_KNOWN_PREFIXES)


_MISSING = object()


class _View:
    """把 pydantic v2 对象包装成能读写字段的 mapping 视图。

    2026-08-06 实测踩了两层坑：

    1. ``OutputItemAddedEvent.item`` 是 ``BaseLiteLLMOpenAIResponseObject``，
       字段**不在** ``__dict__`` 而在 ``__pydantic_extra__``（模型声明了
       ``extra="allow"``，上游多出来的 ``id`` / ``call_id`` 全落到 extra）。
       只读 ``__dict__`` 时 ``changed`` 恒为 False。
    2. 各类 delta 事件是 ``GenericEvent``，``__dict__`` 里只有 ``type``，
       ``item_id`` 同样在 extra 里。

    所以读要「先 extra 后 __dict__」，写要**写回它原本所在那一层**，
    否则改了个影子字段，序列化出去的还是旧值。
    """

    __slots__ = ("_obj", "_extra", "_dict")

    def __init__(self, obj: Any) -> None:
        self._obj = obj
        e = getattr(obj, "__pydantic_extra__", None)
        self._extra = e if isinstance(e, dict) else None
        d = getattr(obj, "__dict__", None)
        self._dict = d if isinstance(d, dict) else None

    def get(self, key: str, default: Any = None) -> Any:
        if self._extra is not None and key in self._extra:
            return self._extra[key]
        if self._dict is not None and key in self._dict:
            return self._dict[key]
        return getattr(self._obj, key, default)

    def keys(self):
        ks = set()
        if self._extra:
            ks |= set(self._extra)
        if self._dict:
            ks |= set(self._dict)
        return ks

    def __getitem__(self, key: str) -> Any:
        val = self.get(key, _MISSING)
        if val is _MISSING:
            raise KeyError(key)
        return val

    def __contains__(self, key: str) -> bool:
        return self.get(key, _MISSING) is not _MISSING

    def __setitem__(self, key: str, value: Any) -> None:
        # 写回字段原本所在那一层；都没有就走 setattr
        if self._extra is not None and key in self._extra:
            self._extra[key] = value
            return
        if self._dict is not None and key in self._dict:
            self._dict[key] = value
            return
        try:
            setattr(self._obj, key, value)
        except Exception:
            if self._extra is not None:
                self._extra[key] = value


def _as_mapping(obj: Any) -> Any:
    """返回能读写 type/id 的 mapping 视图；拿不到返回 None。"""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "__pydantic_extra__") or hasattr(obj, "__dict__"):
        return _View(obj)
    return None


def _fix_item(item: Any, counts: dict[str, int], id_map: dict | None = None) -> bool:
    """就地给单个 item 的 id 补前缀。返回是否改动。

    ``id_map`` 记录 旧id -> 新id，供后续只带 ``item_id`` 的 delta 事件对齐。
    """
    m = _as_mapping(item)
    if m is None:
        return False
    prefix = _PREFIX_BY_TYPE.get(m.get("type"))
    if prefix is None:
        return False
    raw = m.get("id")
    if not _needs_prefix(raw):
        return False
    new = prefix + str(raw).replace("-", "")
    m["id"] = new
    if id_map is not None:
        id_map[raw] = new
    counts[m["type"]] = counts.get(m["type"], 0) + 1
    return True


def _fix_response_obj(resp: Any, counts: dict[str, int], id_map: dict | None = None) -> bool:
    """给一个 response 对象里的所有 output item 补前缀。"""
    m = _as_mapping(resp)
    if m is None:
        return False
    changed = False
    for item in m.get("output") or []:
        changed |= _fix_item(item, counts, id_map)
    return changed


def _fix_event(evt: Any, counts: dict[str, int], id_map: dict | None = None) -> bool:
    """流式单个 SSE 事件。

    id 在三个地方出现，缺一个客户端就会看到前后不一致的 id：
      * ``response.output_item.added`` / ``.done`` 的 ``item``
      * ``response.completed`` / ``.incomplete`` 的 ``response.output[]``
      * 各类 delta 事件的 ``item_id``
    """
    e = _as_mapping(evt)
    if e is None:
        return False
    changed = False

    item = e.get("item")
    if item is not None and _fix_item(item, counts, id_map):
        changed = True
        # delta 事件用 item_id 关联，必须与 item.id 同步改
        im = _as_mapping(item)
        if im is not None and isinstance(e.get("item_id"), str):
            e["item_id"] = im["id"]

    resp = e.get("response")
    if resp is not None:
        changed |= _fix_response_obj(resp, counts, id_map)

    # 独立的 delta / done 事件只带 item_id，没有 item —— 也要跟着改，
    # 否则客户端拼不回同一条 item。
    raw_iid = e.get("item_id")
    if id_map and _needs_prefix(raw_iid):
        seen = id_map.get(raw_iid)
        if seen:
            e["item_id"] = seen
            changed = True

    return changed


class DeepSeekIdPrefix(CustomLogger):
    """出站补前缀。流式 / 非流式两个钩子都要挂。"""

    @staticmethod
    def _model_of(request_data: Any, response: Any = None) -> Any:
        if isinstance(request_data, dict):
            m = request_data.get("model")
            if m:
                return m
        if isinstance(response, dict):
            return response.get("model")
        return None

    async def async_post_call_success_hook(
        self, data: dict, user_api_key_dict: Any, response: Any
    ) -> Any:
        """非流式。"""
        try:
            if not _is_deepseek(self._model_of(data, response)):
                return response
            counts: dict[str, int] = {}
            target = response
            # litellm 可能给的是 pydantic 对象，取其 dict 视图就地改
            if not isinstance(target, dict) and hasattr(target, "output"):
                for item in getattr(target, "output", None) or []:
                    d = item if isinstance(item, dict) else getattr(item, "__dict__", None)
                    if d is not None:
                        _fix_item(d, counts)
            else:
                _fix_response_obj(target, counts)
            if counts:
                _log.warning("deepseek_id_prefix: nonstream counts=%s", counts)
        except Exception as exc:
            _log.warning("deepseek_id_prefix: post_call error: %r", exc)
        return response


def _install_process_chunk_patch() -> None:
    """在 ``BaseResponsesAPIStreamingIterator._process_chunk`` 上补前缀。

    **为什么不用 ``async_post_call_streaming_iterator_hook``**：那一层拿到的是
    ``ModelResponseStream``（``object='chat.completion.chunk'``），Chat
    Completions 形态；Responses 事件和它的 item id 是在**更下游**由
    ``_process_chunk`` 生成的。2026-08-06 实测：在钩子层改，非流式生效、
    流式完全不生效（日志只有 ``nonstream counts``），因为改的根本不是同一个对象。

    ``_process_chunk`` 是所有 Responses 流式路径（原生透传 / chat 转换）的
    单一收口，在这里改一次覆盖全部。
    """
    try:
        from litellm.responses.streaming_iterator import (
            BaseResponsesAPIStreamingIterator as _B,
        )
    except Exception as exc:
        _log.warning("deepseek_id_prefix: import failed, patch skipped: %r", exc)
        return

    if getattr(_B, "_dsidp_patched", False):
        return

    _orig = _B._process_chunk

    def _patched(self, chunk):
        evt = _orig(self, chunk)
        try:
            model = getattr(self, "model", None) or getattr(
                getattr(self, "logging_obj", None), "model", None
            )
            if _is_deepseek(model) and evt is not None:
                counts: dict[str, int] = {}
                id_map = getattr(self, "_dsidp_ids", None)
                if id_map is None:
                    id_map = {}
                    self._dsidp_ids = id_map
                _fix_event(evt, counts, id_map)
                if counts:
                    tot = getattr(self, "_dsidp_counts", None)
                    if tot is None:
                        tot = {}
                        self._dsidp_counts = tot
                    for k, v in counts.items():
                        tot[k] = tot.get(k, 0) + v
        except Exception as exc:
            _log.warning("deepseek_id_prefix: process_chunk error: %r", exc)
        return evt

    _B._process_chunk = _patched
    _B._dsidp_patched = True
    _log.warning("deepseek_id_prefix: _process_chunk patched")


_install_process_chunk_patch()


def _rewrite_chunk(chunk: Any, counts: dict[str, int]) -> Any:
    """按 chunk 的实际形态分派。litellm 这层可能给 bytes / str / 对象。"""
    if isinstance(chunk, (bytes, bytearray)):
        return _rewrite_sse_bytes(bytes(chunk), counts)
    if isinstance(chunk, str):
        out = _rewrite_sse_bytes(chunk.encode("utf-8"), counts)
        return out.decode("utf-8")
    # pydantic / dataclass 形态：就地改它的 dict 视图
    d = chunk if isinstance(chunk, dict) else getattr(chunk, "__dict__", None)
    if isinstance(d, dict):
        _fix_event(d, counts)
    return chunk


def _rewrite_sse_bytes(buf: bytes, counts: dict[str, int]) -> bytes:
    if b'"id"' not in buf:
        return buf

    def _sub(m: "re.Match[bytes]") -> bytes:
        payload = m.group(1)
        if payload.strip() == b"[DONE]":
            return m.group(0)
        try:
            evt = json.loads(payload)
        except Exception:
            return m.group(0)
        if not _fix_event(evt, counts):
            return m.group(0)
        return b"data: " + json.dumps(evt, ensure_ascii=False).encode("utf-8")

    return _SSE_DATA.sub(_sub, buf)


deepseek_id_prefix_instance = DeepSeekIdPrefix()
deepseek_id_prefix = deepseek_id_prefix_instance
