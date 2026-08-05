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
