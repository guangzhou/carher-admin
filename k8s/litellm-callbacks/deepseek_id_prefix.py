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

import contextvars
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


# ---- 出站把降级过的 custom tool 还原 ----
# DeepSeek 只接受 apply_patch 一个 custom tool（实测 custom/exec ->
# 400 "Unsupported custom tool: 'exec'. Only 'apply_patch' is supported."），
# 所以 deepseek_responses_adapt 把其它 custom 工具降级成了 function。
# 但**客户端声明的是 custom** —— Codex 收到 function_call 不认这条调用，
# 表现为「命令执行被中断」（2026-08-06 实测：counts 里
# custom_out_to_function 恒为 0，即客户端从没回传过 output）。
# 故出站要按请求里客户端的原始声明还原回 custom_tool_call。

def _is_compaction_request(it: Any) -> bool:
    """本请求是不是 compaction v2 —— 靠入站打在 litellm_metadata 的标记。"""
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
            if isinstance(meta, dict) and meta.get("deepseek_compaction_v2"):
                return True
    return False


def _custom_names_from_iterator(it: Any) -> set:
    """取「被从 custom 降级成 function 的工具名」。

    2026-08-06 依次排除了三条路，每条都有实测判据：

    * ``async_pre_call_hook`` 登记 + 按 ``litellm_call_id`` 取回 —— 该钩子在
      deepseek 这条路上不跑，登记表实测恒为 ``regsize=0``。
    * ``logging_obj.optional_params`` / ``litellm_params`` 的 ``tools`` ——
      拿到的是**转换后**的形态（``[('function','exec'), ...]``），
      客户端原本的 custom 声明已经没了。
    * ``model_call_details.input`` 里的 ``additional_tools`` —— 也已被 hoist
      掉，实测 ``inputtypes=['message'] additional_tools=None``。

    可靠通道只有 ``litellm_metadata``：``deepseek_responses_adapt`` 在降级那
    一刻写入，它跟着请求一路走到流式迭代器。
    """
    lo = getattr(it, "logging_obj", None)
    if lo is None:
        return set()
    for holder in (getattr(lo, "model_call_details", None),
                   getattr(lo, "litellm_params", None),
                   getattr(lo, "optional_params", None)):
        if not isinstance(holder, dict):
            continue
        for key in ("litellm_metadata", "metadata"):
            meta = holder.get(key)
            if isinstance(meta, dict):
                names = meta.get("deepseek_downgraded_custom_tools")
                if names:
                    return set(names)
    return set()


def _client_custom_tool_names(request_data: Any) -> set:
    """客户端**原始**声明为 custom 的工具名。

    注意要看 ``input`` 里的 additional_tools（Codex responses-lite 形态），
    不能只看顶层 ``tools`` —— 顶层那份已经被 hoist + 降级过了。
    """
    names: set = set()
    if not isinstance(request_data, dict):
        return names

    def _scan(tools: Any) -> None:
        if not isinstance(tools, list):
            return
        for t in tools:
            if not isinstance(t, dict):
                continue
            if t.get("type") == "namespace":
                _scan(t.get("tools"))
            elif t.get("type") == "custom" and t.get("name"):
                names.add(t["name"])

    _scan(request_data.get("tools"))
    for item in request_data.get("input") or []:
        if isinstance(item, dict) and item.get("type") == "additional_tools":
            _scan(item.get("tools"))
    return names


def _downgraded_names_from_data(data: Any) -> set:
    """从请求 dict 的 metadata 里取降级过的名字（非流式路径用）。"""
    if not isinstance(data, dict):
        return set()
    for key in ("litellm_metadata", "metadata"):
        meta = data.get(key)
        if isinstance(meta, dict):
            names = meta.get("deepseek_downgraded_custom_tools")
            if names:
                return set(names)
    return set()


def _restore_custom_call(item: Any, custom_names: set, counts: dict) -> bool:
    """``function_call`` -> ``custom_tool_call``（仅限客户端声明为 custom 的名字）。

    降级时参数被包成 ``{"input": "..."}``，还原时要把它摊回 custom 的
    ``input`` 字符串字段。
    """
    m = _as_mapping(item)
    if m is None or m.get("type") != "function_call":
        return False
    name = m.get("name")
    if name not in custom_names:
        return False
    raw = m.get("arguments")
    text = ""
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
            text = parsed.get("input", "") if isinstance(parsed, dict) else raw
        except Exception:
            text = raw
    m["type"] = "custom_tool_call"
    m["input"] = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    try:
        m["arguments"] = None
    except Exception:
        pass
    counts["custom_call_restored"] = counts.get("custom_call_restored", 0) + 1
    return True


# litellm_call_id -> 客户端声明为 custom 的工具名集合。
# 流式迭代器拿不到原始请求，只能靠 pre_call 钩子登记、_process_chunk 时按
# call id 取回。带上限，防止长跑进程无界增长。
_CUSTOM_NAMES_BY_CALL: "dict[str, set]" = {}
_CUSTOM_NAMES_MAX = 512


def _remember_custom_names(request_data: Any) -> None:
    if not isinstance(request_data, dict):
        return
    cid = request_data.get("litellm_call_id")
    if not cid:
        return
    names = _client_custom_tool_names(request_data)
    if not names:
        return
    if len(_CUSTOM_NAMES_BY_CALL) >= _CUSTOM_NAMES_MAX:
        _CUSTOM_NAMES_BY_CALL.clear()
    _CUSTOM_NAMES_BY_CALL[str(cid)] = names


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

    async def async_pre_call_hook(
        self, user_api_key_dict: Any, cache: Any, data: dict, call_type: str
    ) -> Any:
        """只做登记：记下客户端原始声明为 custom 的工具名，供出站还原用。

        必须在这里取 —— 到了流式迭代器那层，``tools`` 已经被
        ``deepseek_responses_adapt`` hoist + 降级过，看不到原始 custom 声明了。
        """
        try:
            # **不能按 model 门控** —— pre_call 时 model 还是原始 model group
            # （兜底场景下是 gpt-5.6-sol 之类），2026-08-06 实测门控在这里
            # 一次都不放行，登记表恒为空。改为无条件登记：只记「客户端声明为
            # custom 的工具名」，对非 deepseek 请求也无副作用（出站还原那侧
            # 仍由 _is_deepseek 门控）。
            _remember_custom_names(data)
        except Exception as exc:
            _log.warning("deepseek_id_prefix: pre_call error: %r", exc)
        return data

    async def async_post_call_success_hook(
        self, data: dict, user_api_key_dict: Any, response: Any
    ) -> Any:
        """非流式。"""
        try:
            if not _is_deepseek(self._model_of(data, response)):
                return response
            counts: dict[str, int] = {}
            # 非流式同样要还原 custom 形态（流式那侧在 _process_chunk 里做）。
            cn = _client_custom_tool_names(data) or _downgraded_names_from_data(data)
            if cn:
                out_items = None
                if isinstance(response, dict):
                    out_items = response.get("output")
                elif hasattr(response, "output"):
                    out_items = getattr(response, "output", None)
                for _o in out_items or []:
                    # 只在**纯 dict** item 上还原。改 pydantic 对象的 type 会让
                    # 它的序列化器失配：非流式响应体走 model_dump 时崩
                    # TypeError: 'MockValSer' object is not an instance of
                    # 'SchemaSerializer'（2026-08-06 实测 500）。pydantic 的
                    # serializer 按声明类型缓存，改 type 字段等于换了模型。
                    # 流式那侧事件本身就是 dict 视图，不受影响。
                    if isinstance(_o, dict):
                        _restore_custom_call(_o, cn, counts)
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
                # 先还原 custom 形态,再补前缀 —— 顺序不能反,
                # 还原会改 type,而前缀是按 type 选的
                cn = getattr(self, "_dsidp_custom_names", None)
                if cn is None:
                    cn = _custom_names_from_iterator(self)
                    self._dsidp_custom_names = cn
                # 把「本请求要还原哪些名字」放进 contextvar，供下游 SSE
                # 格式化函数取用。**只在 _is_deepseek 门控内设置** ——
                # 其它模型的请求这里根本不会执行到，contextvar 保持 None，
                # SSE patch 第一行就 return，一个字节都不碰。
                if cn:
                    active = _ACTIVE_CUSTOM.get()
                    if active is None or active[0] != cn:
                        _ACTIVE_CUSTOM.set((cn, set()))
                if not _ACTIVE_COMPACTION.get() and _is_compaction_request(self):
                    _ACTIVE_COMPACTION.set(True)
                # **不在这里改 type** —— 事件对象是 pydantic 模型，改了 type
                # 会让它的序列化器失配，`response.completed` 那一帧序列化时崩
                # PydanticSerializationError: 'MockValSer' object is not an
                # instance of 'SchemaSerializer'，整条流断在最后一帧
                # （2026-08-06 实测：无 response.completed / 无 [DONE]）。
                # custom 形态的还原改在 SSE 字节层做，见 _restore_in_sse_bytes。
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


# ---------------- SSE 字节层还原 custom_tool_call ----------------
# 为什么必须在字节层做：事件对象是 pydantic 模型，改它的 ``type`` 字段会让
# 序列化器失配 —— ``response.completed`` 那一帧序列化时崩
# ``PydanticSerializationError: 'MockValSer' object is not an instance of
# 'SchemaSerializer'``，整条流断在最后一帧（2026-08-06 实测：无
# response.completed、无 [DONE]，比原故障更严重）。
# 到了 SSE 字节这一层已经是纯 JSON 文本，改它不碰任何 pydantic 机制。

_FN_TO_CUSTOM_EVENT = {
    "response.function_call_arguments.delta": "response.custom_tool_call_input.delta",
    "response.function_call_arguments.done": "response.custom_tool_call_input.done",
}


def _restore_custom_in_obj(obj: Any, names: set, ids: set, counts: dict) -> bool:
    """在**纯 dict** 的事件对象上把 function_call 还原成 custom_tool_call。"""
    if not isinstance(obj, dict):
        return False
    changed = False

    def _fix_one(it: Any) -> bool:
        if not isinstance(it, dict) or it.get("type") != "function_call":
            return False
        if it.get("name") not in names:
            return False
        raw = it.get("arguments")
        text = ""
        if isinstance(raw, str) and raw:
            try:
                parsed = json.loads(raw)
                text = parsed.get("input", "") if isinstance(parsed, dict) else raw
            except Exception:
                text = raw
        it["type"] = "custom_tool_call"
        it["input"] = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
        it.pop("arguments", None)
        if it.get("id"):
            ids.add(it["id"])
        counts["custom_call_restored"] = counts.get("custom_call_restored", 0) + 1
        return True

    item = obj.get("item")
    if _fix_one(item):
        changed = True

    resp = obj.get("response")
    if isinstance(resp, dict):
        for o in resp.get("output") or []:
            if _fix_one(o):
                changed = True

    # 参数增量事件跟着换名 —— item 成了 custom，delta 还叫 function_call_*
    # 的话客户端拼不起来
    t = obj.get("type")
    new_t = _FN_TO_CUSTOM_EVENT.get(str(t))
    if new_t and obj.get("item_id") in ids:
        obj["type"] = new_t
        if "delta" in obj:
            pass  # 两种事件的增量字段同名，都是 delta
        if "arguments" in obj:
            obj["input"] = obj.pop("arguments")
        counts["arg_event_renamed"] = counts.get("arg_event_renamed", 0) + 1
        changed = True

    return changed


def _restore_custom_in_sse(text: str, names: set, ids: set, counts: dict) -> str:
    """逐 SSE 帧还原。解析不了的原样放行，绝不吞流。"""
    if not names or "data: " not in text:
        return text
    out_lines = []
    changed_any = False
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
        except Exception:
            out_lines.append(line)
            continue
        if _restore_custom_in_obj(obj, names, ids, counts):
            changed_any = True
            out_lines.append("data: " + json.dumps(obj, ensure_ascii=False))
        else:
            out_lines.append(line)
    return "\n".join(out_lines) if changed_any else text


# ============ Codex compaction v2 出站包装 ============
#
# 入站侧（deepseek_responses_adapt._rewrite_compaction_request）已把
# compaction_trigger 换成显式摘要指令，并在 litellm_metadata 打了
# deepseek_compaction_v2 标记。这里负责把上游返回的摘要包装成 Codex 要的形状。
#
# 官方 schema（openai/codex protocol/src/models.rs）::
#
#     Compaction { id: Option<..>, encrypted_content: String, ... }
#
# 判据（compact_remote_v2.rs）：只认 OutputItemDone 里的 Compaction 变体，
# 且 compaction_count **必须恰好 1**，否则 Fatal。
#
# 所以出站要做两件事：
#   1. 把 message item 改写成 compaction item（摘要文本塞 encrypted_content）
#   2. **丢掉 reasoning item** —— 它会占 output_item_count，且对压缩语义无用
#      （Codex 存的是 compaction，reasoning 留着只会污染下一轮上下文）

_COMPACTION_ITEM_TYPE = "compaction"


def _extract_text(item: Any) -> str:
    """从 message item 里取纯文本。"""
    m = _as_mapping(item)
    if m is None:
        return ""
    parts = m.get("content")
    if isinstance(parts, str):
        return parts
    if not isinstance(parts, list):
        return ""
    buf = []
    for p in parts:
        pm = _as_mapping(p)
        if pm is None:
            continue
        t = pm.get("text")
        if isinstance(t, str):
            buf.append(t)
    return "".join(buf)


def _to_compaction_item(item: dict, counts: dict) -> bool:
    """message -> compaction（就地改，只在纯 dict 上调用）。"""
    if not isinstance(item, dict) or item.get("type") != "message":
        return False
    text = _extract_text(item)
    if not text.strip():
        return False
    item.clear()
    item["type"] = _COMPACTION_ITEM_TYPE
    item["encrypted_content"] = text
    counts["compaction_wrapped"] = counts.get("compaction_wrapped", 0) + 1
    return True


def _wrap_compaction_in_sse(text: str, counts: dict) -> str:
    """SSE 帧层：message -> compaction，并丢掉 reasoning item。

    与 custom tool 还原同理，必须在字节层做 —— 改 pydantic 事件对象的 type
    会炸 response.completed 那一帧的序列化。
    """
    if "data: " not in text:
        return text
    out_lines = []
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
        except Exception:
            out_lines.append(line)
            continue

        etype = str(obj.get("type") or "")
        item = obj.get("item")

        # reasoning 相关的事件整条丢掉
        if isinstance(item, dict) and item.get("type") == "reasoning":
            if item.get("id"):
                drop_ids.add(item["id"])
            changed = True
            continue
        if obj.get("item_id") in drop_ids or "reasoning" in etype:
            changed = True
            continue

        if isinstance(item, dict) and _to_compaction_item(item, counts):
            changed = True

        resp = obj.get("response")
        if isinstance(resp, dict):
            kept = []
            for o in resp.get("output") or []:
                if isinstance(o, dict) and o.get("type") == "reasoning":
                    changed = True
                    continue
                if isinstance(o, dict) and _to_compaction_item(o, counts):
                    changed = True
                kept.append(o)
            if kept != (resp.get("output") or []):
                resp["output"] = kept
                changed = True

        out_lines.append("data: " + json.dumps(obj, ensure_ascii=False)
                         if changed else line)
    return "\n".join(out_lines) if changed else text


def _install_sse_format_patch() -> None:
    """patch ``proxy_server._format_streaming_sse_chunk`` —— SSE 的单一收口。"""
    try:
        from litellm.proxy import proxy_server as _ps
    except Exception as exc:
        _log.warning("deepseek_id_prefix: sse patch import failed: %r", exc)
        return
    if getattr(_ps, "_dsidp_sse_patched", False):
        return
    _orig = getattr(_ps, "_format_streaming_sse_chunk", None)
    if _orig is None:
        _log.warning("deepseek_id_prefix: _format_streaming_sse_chunk missing, sse patch skipped")
        return

    def _patched(chunk):
        out = _orig(chunk)
        try:
            if _ACTIVE_COMPACTION.get():
                counts: dict = {}
                if isinstance(out, bytes):
                    out = _wrap_compaction_in_sse(
                        out.decode("utf-8", "replace"), counts).encode("utf-8")
                elif isinstance(out, str):
                    out = _wrap_compaction_in_sse(out, counts)
                if counts:
                    _log.warning("deepseek_id_prefix: compaction %s", counts)
                return out
            active = _ACTIVE_CUSTOM.get()
            if active:
                names, ids = active
                counts: dict = {}
                if isinstance(out, bytes):
                    new = _restore_custom_in_sse(out.decode("utf-8", "replace"), names, ids, counts)
                    out = new.encode("utf-8")
                elif isinstance(out, str):
                    out = _restore_custom_in_sse(out, names, ids, counts)
                if counts:
                    _log.warning("deepseek_id_prefix: sse restore %s", counts)
        except Exception as exc:
            _log.warning("deepseek_id_prefix: sse patch error: %r", exc)
        return out

    _ps._format_streaming_sse_chunk = _patched
    _ps._dsidp_sse_patched = True
    _log.warning("deepseek_id_prefix: _format_streaming_sse_chunk patched")


# 当前请求里「被降级过的 custom 工具名」。
#
# **必须用 contextvar，不能用模块级 dict** —— SSE 格式化函数是模块级自由函数，
# 拿不到请求上下文；若用全局字典，高并发下会把 deepseek 的还原规则串到同时在
# 跑的 chatgpt / anthropic 请求上（198 上常态 40+ 并发）。ContextVar 按协程
# 隔离，一请求一份，天生不串。
#
# 存 (names, ids)：names = 被降级的工具名；ids = 已还原 item 的 id，
# 供后续只带 item_id 的 delta 事件对齐。
_ACTIVE_CUSTOM: "contextvars.ContextVar[tuple | None]" = contextvars.ContextVar(
    "dsidp_active_custom", default=None
)

# 本请求是不是 Codex compaction v2（入站已把 trigger 换成摘要指令）。
# 同样用 contextvar 按协程隔离，非 deepseek 请求恒为 False。
_ACTIVE_COMPACTION: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "dsidp_active_compaction", default=False
)


_install_sse_format_patch()

