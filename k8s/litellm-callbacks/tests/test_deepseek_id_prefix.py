"""test_deepseek_id_prefix.py — 出站 item id 必须带 OpenAI 规范前缀。

背景（2026-08-06 生产）
----------------------
本地 Codex 打 ``deepseek-v4-flash-responses``，工具调用降级成裸文本::

    <｜｜DSML｜｜invoke name="exec_command">

上游全程 200、零错误。对照组（同客户端同工具，只换模型）逼出差异::

                        GPT（好使）                DeepSeek（泄漏）
    function_call.id    fc_068c8f3f17af7f06...    f2104000-ca0f-4889-...
    reasoning.id        encitem_bGl0ZWxsbTp...    7a4e8e3b-7dcc-4101-...

Codex 按 ``fc_`` 前缀识别工具调用项，裸 UUID 不认 -> 当文本渲染。

实测 DeepSeek 对**回传** id 格式完全不挑（裸 UUID / fc_ / fc_+rs_ / 不带
id，四种都 200），所以只改出站、不还原入站。

用法::

    python3 -m unittest test_deepseek_id_prefix -v
"""
import importlib.util
import json
import os
import sys
import types
import unittest

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "deepseek_id_prefix.py")


def _load_module():
    if "litellm" not in sys.modules:
        litellm = types.ModuleType("litellm")
        integrations = types.ModuleType("litellm.integrations")
        custom_logger = types.ModuleType("litellm.integrations.custom_logger")

        class CustomLogger:  # noqa: D401 - stub
            pass

        custom_logger.CustomLogger = CustomLogger
        integrations.custom_logger = custom_logger
        litellm.integrations = integrations
        sys.modules["litellm"] = litellm
        sys.modules["litellm.integrations"] = integrations
        sys.modules["litellm.integrations.custom_logger"] = custom_logger

    path = os.environ.get("DEEPSEEK_ID_PREFIX_PATH", DEFAULT_PATH)
    spec = importlib.util.spec_from_file_location("deepseek_id_prefix_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MOD = _load_module()

# 真实抓到的 DeepSeek 出站形态
REAL_FC_ID = "f2104000-ca0f-4889-b6d9-6ba665436c74"
REAL_RS_ID = "7a4e8e3b-7dcc-4101-b8d7-1d6d1e29489b"
REAL_CALL_ID = "call_00_pDEZMVxZsLENCTZucrmD7726"


class ItemIdPrefix(unittest.TestCase):
    def test_function_call_gets_fc_prefix(self):
        item = {"type": "function_call", "id": REAL_FC_ID,
                "call_id": REAL_CALL_ID, "name": "ls"}
        counts = {}
        self.assertTrue(MOD._fix_item(item, counts))
        self.assertTrue(item["id"].startswith("fc_"))
        self.assertEqual(counts, {"function_call": 1})

    def test_reasoning_gets_rs_prefix(self):
        item = {"type": "reasoning", "id": REAL_RS_ID, "summary": []}
        self.assertTrue(MOD._fix_item(item, {}))
        self.assertTrue(item["id"].startswith("rs_"))

    def test_call_id_is_never_touched(self):
        """call_id 是 DeepSeek 的 call/output 配对依据，改了整单 400。"""
        item = {"type": "function_call", "id": REAL_FC_ID,
                "call_id": REAL_CALL_ID, "name": "ls"}
        MOD._fix_item(item, {})
        self.assertEqual(item["call_id"], REAL_CALL_ID)

    def test_idempotent(self):
        """已带前缀的不许二次加 —— 重放 / 多钩子叠加时必须稳定。"""
        item = {"type": "function_call", "id": "fc_" + REAL_FC_ID.replace("-", "")}
        before = item["id"]
        counts = {}
        self.assertFalse(MOD._fix_item(item, counts))
        self.assertEqual(item["id"], before)
        self.assertEqual(counts, {})

    def test_openai_native_ids_untouched(self):
        """GPT 侧本来就合规的 id 一个字节都不能动。"""
        for typ, ident in (("function_call", "fc_068c8f3f17af7f06016a7"),
                           ("reasoning", "encitem_bGl0ZWxsbTptb2Rl")):
            item = {"type": typ, "id": ident}
            self.assertFalse(MOD._fix_item(item, {}))
            self.assertEqual(item["id"], ident)

    def test_unknown_item_type_untouched(self):
        item = {"type": "function_call_output", "call_id": REAL_CALL_ID, "output": "x"}
        self.assertFalse(MOD._fix_item(item, {}))
        self.assertNotIn("id", item)

    def test_missing_or_empty_id_is_safe(self):
        for item in ({"type": "function_call"},
                     {"type": "function_call", "id": ""},
                     {"type": "function_call", "id": None}):
            self.assertFalse(MOD._fix_item(item, {}))


class StreamingEvents(unittest.TestCase):
    """流式必须在 output_item.added 就改 —— 客户端是增量解析的。"""

    def test_output_item_added_rewritten(self):
        evt = {"type": "response.output_item.added", "output_index": 0,
               "item": {"type": "function_call", "id": REAL_FC_ID,
                        "call_id": REAL_CALL_ID, "name": "ls"}}
        self.assertTrue(MOD._fix_event(evt, {}))
        self.assertTrue(evt["item"]["id"].startswith("fc_"))

    def test_item_id_kept_in_sync(self):
        """delta 事件靠 item_id 关联，与 item.id 不同步客户端就拼不起来。"""
        evt = {"type": "response.output_item.done", "item_id": REAL_FC_ID,
               "item": {"type": "function_call", "id": REAL_FC_ID,
                        "call_id": REAL_CALL_ID, "name": "ls"}}
        MOD._fix_event(evt, {})
        self.assertEqual(evt["item_id"], evt["item"]["id"])

    def test_response_completed_output_rewritten(self):
        evt = {"type": "response.completed",
               "response": {"status": "completed", "output": [
                   {"type": "reasoning", "id": REAL_RS_ID, "summary": []},
                   {"type": "function_call", "id": REAL_FC_ID,
                    "call_id": REAL_CALL_ID, "name": "ls"}]}}
        counts = {}
        self.assertTrue(MOD._fix_event(evt, counts))
        out = evt["response"]["output"]
        self.assertTrue(out[0]["id"].startswith("rs_"))
        self.assertTrue(out[1]["id"].startswith("fc_"))
        self.assertEqual(counts, {"reasoning": 1, "function_call": 1})

    def test_sse_bytes_roundtrip(self):
        raw = (b'data: ' + json.dumps({
            "type": "response.output_item.added",
            "item": {"type": "function_call", "id": REAL_FC_ID,
                     "call_id": REAL_CALL_ID, "name": "ls"}}).encode() + b'\n\n')
        out = MOD._rewrite_sse_bytes(raw, {})
        evt = json.loads(out.split(b"data: ", 1)[1])
        self.assertTrue(evt["item"]["id"].startswith("fc_"))
        self.assertEqual(evt["item"]["call_id"], REAL_CALL_ID)

    def test_done_sentinel_untouched(self):
        raw = b"data: [DONE]\n\n"
        self.assertEqual(MOD._rewrite_sse_bytes(raw, {}), raw)

    def test_malformed_json_passes_through(self):
        """半个 chunk / 非 JSON 一律原样放行，绝不能吞流。"""
        raw = b'data: {"type": "response.out\n\n'
        self.assertEqual(MOD._rewrite_sse_bytes(raw, {}), raw)

    def test_chunk_without_id_short_circuits(self):
        raw = b'data: {"type":"response.function_call_arguments.delta","delta":"{"}\n\n'
        self.assertEqual(MOD._rewrite_sse_bytes(raw, {}), raw)

    def test_utf8_content_survives(self):
        """ensure_ascii=False —— 中文不能被转义成 \\uXXXX 改变字节长度语义。"""
        raw = (b'data: ' + json.dumps({
            "type": "response.output_item.added",
            "item": {"type": "message", "id": REAL_RS_ID,
                     "content": [{"type": "output_text", "text": "看看本地的代码结构"}]}},
            ensure_ascii=False).encode("utf-8") + b'\n\n')
        out = MOD._rewrite_sse_bytes(raw, {})
        self.assertIn("看看本地的代码结构".encode("utf-8"), out)


class Scope(unittest.TestCase):
    def test_is_deepseek_gate(self):
        for m in ("deepseek-v4-flash", "openai/deepseek-v4-flash",
                  "deepseek-v4-flash-responses"):
            self.assertTrue(MOD._is_deepseek(m))
        for m in ("chatgpt-gpt-5.6-sol", "anthropic.claude-opus-4-8", None, 123):
            self.assertFalse(MOD._is_deepseek(m))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class PydanticEventObjects(unittest.TestCase):
    """流式路径上 item 是 **pydantic 对象**，不是 dict。

    2026-08-06 实测：只判 ``isinstance(dict)`` 时，``OutputItemAddedEvent``
    明明进来了、``item`` 键也在，``counts`` 却恒为 ``{}`` —— 第一行就 return。
    表现是「非流式生效、流式完全不生效」，而 Codex 走的正是流式。
    """

    class _Obj:
        """最小 pydantic 替身：字段存在 __dict__ 里。"""
        def __init__(self, **kw):
            self.__dict__.update(kw)

    def test_pydantic_item_is_fixed(self):
        item = self._Obj(type="function_call", id=REAL_FC_ID,
                         call_id=REAL_CALL_ID, name="ls")
        counts = {}
        self.assertTrue(MOD._fix_item(item, counts))
        self.assertTrue(item.id.startswith("fc_"))
        self.assertEqual(item.call_id, REAL_CALL_ID)

    def test_pydantic_event_with_pydantic_item(self):
        evt = self._Obj(type="response.output_item.added", output_index=0,
                        item=self._Obj(type="function_call", id=REAL_FC_ID,
                                       call_id=REAL_CALL_ID, name="ls"))
        counts = {}
        self.assertTrue(MOD._fix_event(evt, counts))
        self.assertTrue(evt.item.id.startswith("fc_"))
        self.assertEqual(counts, {"function_call": 1})

    def test_pydantic_response_completed(self):
        evt = self._Obj(type="response.completed",
                        response=self._Obj(status="completed", output=[
                            self._Obj(type="reasoning", id=REAL_RS_ID, summary=[]),
                            self._Obj(type="function_call", id=REAL_FC_ID,
                                      call_id=REAL_CALL_ID, name="ls")]))
        counts = {}
        self.assertTrue(MOD._fix_event(evt, counts))
        self.assertTrue(evt.response.output[0].id.startswith("rs_"))
        self.assertTrue(evt.response.output[1].id.startswith("fc_"))

    def test_delta_event_item_id_realigned_via_map(self):
        """只带 item_id 的 delta 事件必须用同一条流的映射对齐。

        对不上客户端就把 arguments 增量拼到一条不存在的 item 上。
        """
        id_map = {}
        added = self._Obj(type="response.output_item.added",
                          item=self._Obj(type="function_call", id=REAL_FC_ID,
                                         call_id=REAL_CALL_ID, name="ls"))
        MOD._fix_event(added, {}, id_map)
        new_id = added.item.id

        delta = self._Obj(type="response.function_call_arguments.delta",
                          item_id=REAL_FC_ID, delta='{"p"')
        self.assertTrue(MOD._fix_event(delta, {}, id_map))
        self.assertEqual(delta.item_id, new_id)

    def test_id_map_isolated_per_stream(self):
        """没有映射就不动 item_id —— 防止跨请求串号。"""
        delta = self._Obj(type="response.function_call_arguments.delta",
                          item_id=REAL_FC_ID, delta="{")
        self.assertFalse(MOD._fix_event(delta, {}, {}))
        self.assertEqual(delta.item_id, REAL_FC_ID)


class RestoreCustomToolCall(unittest.TestCase):
    """出站把降级过的 custom tool 还原回 custom_tool_call。

    DeepSeek 只接受 ``apply_patch`` 一个 custom tool（实测
    ``custom/exec`` -> 400 "Unsupported custom tool: 'exec'. Only
    'apply_patch' is supported."），所以 ``deepseek_responses_adapt`` 把
    Codex 的 ``exec`` 降级成了 function —— 上游没得选。

    但**客户端声明的是 custom**：Codex 收到 ``function_call`` 不认这条调用，
    表现为「命令执行被中断，一条都跑不了」（2026-08-06 实测：46 次 hoist
    生效、模型正常多轮调用，但 ``custom_out_to_function`` 恒为 0 ——
    客户端从没回传过 output，即它根本没执行）。

    故出站要按客户端**原始**声明还原。
    """

    class _Obj:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    AT_ITEM = {"type": "additional_tools", "role": "developer", "tools": [
        {"type": "custom", "name": "exec",
         "format": {"type": "grammar", "syntax": "lark", "definition": "start: /.*/"}},
        {"type": "function", "name": "wait", "parameters": {"type": "object"}},
    ]}

    def test_custom_names_read_from_additional_tools(self):
        """必须从 input 的 additional_tools 里取 —— 顶层 tools 已被降级过。"""
        data = {"model": "deepseek-v4-flash", "tools": [],
                "input": [self.AT_ITEM, {"role": "user", "content": "hi"}]}
        self.assertEqual(MOD._client_custom_tool_names(data), {"exec"})

    def test_custom_names_from_top_level_tools(self):
        data = {"model": "deepseek-v4-flash", "input": [],
                "tools": [{"type": "custom", "name": "apply_patch"}]}
        self.assertEqual(MOD._client_custom_tool_names(data), {"apply_patch"})

    def test_namespace_nested_custom_found(self):
        data = {"model": "deepseek-v4-flash", "input": [], "tools": [
            {"type": "namespace", "name": "ns", "tools": [
                {"type": "custom", "name": "inner"}]}]}
        self.assertIn("inner", MOD._client_custom_tool_names(data))

    def test_function_call_restored_to_custom(self):
        """真凶格：exec 的 function_call 还原成 custom_tool_call。"""
        item = self._Obj(type="function_call", id="fc_x", call_id="call_1",
                         name="exec", arguments=json.dumps({"input": "ls -la"}))
        counts = {}
        self.assertTrue(MOD._restore_custom_call(item, {"exec"}, counts))
        self.assertEqual(item.type, "custom_tool_call")
        self.assertEqual(item.input, "ls -la")
        self.assertEqual(item.call_id, "call_1", "call_id 不能动")
        self.assertEqual(counts, {"custom_call_restored": 1})

    def test_non_custom_tool_untouched(self):
        """客户端声明为 function 的工具不能被改成 custom。"""
        item = self._Obj(type="function_call", id="fc_y", call_id="c2",
                         name="wait", arguments='{"ms":100}')
        self.assertFalse(MOD._restore_custom_call(item, {"exec"}, {}))
        self.assertEqual(item.type, "function_call")

    def test_malformed_arguments_fall_back_to_raw(self):
        """参数不是合法 JSON 时原样带过去，不能丢内容。"""
        item = self._Obj(type="function_call", id="fc_z", call_id="c3",
                         name="exec", arguments="not-json{")
        MOD._restore_custom_call(item, {"exec"}, {})
        self.assertEqual(item.input, "not-json{")

    def test_empty_custom_names_is_noop(self):
        item = self._Obj(type="function_call", id="fc_w", call_id="c4",
                         name="exec", arguments='{"input":"ls"}')
        self.assertFalse(MOD._restore_custom_call(item, set(), {}))
        self.assertEqual(item.type, "function_call")


class SSERestoreIsolation(unittest.TestCase):
    """SSE 字节层还原 —— 作用域必须严格限定在 deepseek。

    为什么改在字节层：事件对象是 pydantic 模型，改它的 ``type`` 会让序列化器
    失配，``response.completed`` 那一帧崩 ``PydanticSerializationError:
    'MockValSer' object is not an instance of 'SchemaSerializer'``，整条流断在
    最后一帧（2026-08-06 实测：无 response.completed、无 [DONE]，比原故障更严重）。

    为什么用 contextvar 而不是模块级 dict：198 上常态 40+ 并发，全局字典会把
    deepseek 的还原规则串到同时在跑的 chatgpt/anthropic 请求上。
    """

    FN_DELTA = ('data: {"type":"response.function_call_arguments.delta",'
                '"item_id":"fc_1","delta":"ls "}')

    def _added(self, name="exec", iid="fc_1"):
        return ('data: {"type":"response.output_item.added","output_index":1,'
                '"item":{"type":"function_call","id":"%s","call_id":"c1",'
                '"name":"%s","arguments":"{\\"input\\":\\"ls -la\\"}"}}' % (iid, name))

    def test_function_call_restored_in_sse(self):
        ids, counts = set(), {}
        out = MOD._restore_custom_in_sse(self._added(), {"exec"}, ids, counts)
        evt = json.loads(out.split("data: ", 1)[1])
        self.assertEqual(evt["item"]["type"], "custom_tool_call")
        self.assertEqual(evt["item"]["input"], "ls -la")
        self.assertNotIn("arguments", evt["item"])
        self.assertEqual(evt["item"]["call_id"], "c1", "call_id 不能动")

    def test_arg_delta_event_renamed(self):
        """item 成了 custom，delta 事件名必须跟着换，否则客户端拼不起来。"""
        ids, counts = set(), {}
        MOD._restore_custom_in_sse(self._added(), {"exec"}, ids, counts)
        out = MOD._restore_custom_in_sse(self.FN_DELTA, {"exec"}, ids, counts)
        evt = json.loads(out.split("data: ", 1)[1])
        self.assertEqual(evt["type"], "response.custom_tool_call_input.delta")

    def test_delta_of_other_item_untouched(self):
        """同一响应里可能同时有普通 function_call —— 不是被还原那条就别动。"""
        ids, counts = set(), {}
        MOD._restore_custom_in_sse(self._added(), {"exec"}, ids, counts)
        other = ('data: {"type":"response.function_call_arguments.delta",'
                 '"item_id":"fc_OTHER","delta":"x"}')
        out = MOD._restore_custom_in_sse(other, {"exec"}, ids, counts)
        self.assertEqual(json.loads(out.split("data: ", 1)[1])["type"],
                         "response.function_call_arguments.delta")

    def test_non_downgraded_tool_untouched(self):
        """客户端本来就声明为 function 的工具不能被改成 custom。"""
        ids, counts = set(), {}
        out = MOD._restore_custom_in_sse(self._added(name="wait"), {"exec"}, ids, counts)
        self.assertEqual(json.loads(out.split("data: ", 1)[1])["item"]["type"],
                         "function_call")

    def test_empty_names_is_bytewise_noop(self):
        """作用域对照组：非 deepseek 请求 names 为空,必须原样返回同一个对象。"""
        raw = self._added()
        self.assertIs(MOD._restore_custom_in_sse(raw, set(), set(), {}), raw)

    def test_done_sentinel_and_malformed_pass_through(self):
        for raw in ("data: [DONE]", 'data: {"broken', "event: ping", ""):
            self.assertEqual(MOD._restore_custom_in_sse(raw, {"exec"}, set(), {}), raw)

    def test_completed_frame_output_restored(self):
        """response.completed 里的 output[] 也要还原,否则前后不一致。"""
        raw = ('data: {"type":"response.completed","response":{"status":"completed",'
               '"output":[{"type":"function_call","id":"fc_9","call_id":"c9",'
               '"name":"exec","arguments":"{\\"input\\":\\"pwd\\"}"}]}}')
        out = MOD._restore_custom_in_sse(raw, {"exec"}, set(), {})
        evt = json.loads(out.split("data: ", 1)[1])
        self.assertEqual(evt["response"]["output"][0]["type"], "custom_tool_call")

    def test_contextvar_defaults_to_none(self):
        """默认必须是 None —— SSE patch 靠它判断「本请求不归我管」。"""
        self.assertIsNone(MOD._ACTIVE_CUSTOM.get())

    def test_utf8_preserved(self):
        raw = ('data: {"type":"response.output_item.added","item":{"type":"function_call",'
               '"id":"fc_2","call_id":"c2","name":"exec",'
               '"arguments":"{\\"input\\":\\"echo 中文\\"}"}}')
        out = MOD._restore_custom_in_sse(raw, {"exec"}, set(), {})
        self.assertIn("中文", out)
