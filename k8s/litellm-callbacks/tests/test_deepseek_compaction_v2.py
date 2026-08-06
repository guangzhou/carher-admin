"""test_deepseek_compaction_v2.py — Codex remote compaction v2 支持。

背景（2026-08-06 生产）
----------------------
用户 Codex 长会话触发压缩时报::

    Fatal error: remote compaction v2 expected exactly one compaction
    output item, got 0 from 3 output items

**协议**（读 openai/codex 源码确认，非推测）：

* `codex-rs/core/src/compact_remote_v2.rs` —— compaction v2 **不打
  `/v1/responses/compact`**（那是 V1），复用标准 Responses 流，只在 ``input``
  末尾追加 ``{"type":"compaction_trigger"}``；计数只认 ``OutputItemDone`` 里的
  ``Compaction`` 变体，``compaction_count != 1`` 就 Fatal。
* `codex-rs/protocol/src/models.rs` —— ``Compaction { id: Option<..>,
  encrypted_content: String, ... }``，**``encrypted_content`` 是普通 String**，
  Codex 当不透明字符串存、下一轮原样回传，塞明文摘要即可。

**DeepSeek 实际行为**（真打 api.deepseek.com 单变量实测）：
带 trigger 与不带（对照组）输出**无差异**，都是「继续对话」——
即 DeepSeek 完全忽略这个标记；但显式要摘要时能产出真摘要。

故做法：入站换成显式摘要指令 → 出站包装成 compaction item + 丢掉 reasoning。

用法::

    python3 -m unittest test_deepseek_compaction_v2 -v
"""
import importlib.util
import json
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def _stub_litellm():
    if "litellm" in sys.modules:
        return
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


def _load(fname, modname):
    _stub_litellm()
    path = os.environ.get(f"{modname.upper()}_PATH",
                          os.path.join(HERE, "..", fname))
    spec = importlib.util.spec_from_file_location(modname, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ADAPT = _load("deepseek_responses_adapt.py", "dsa_compact")
IDP = _load("deepseek_id_prefix.py", "dsidp_compact")

TRIGGER = {"type": "compaction_trigger"}
CONV = [
    {"type": "message", "role": "user",
     "content": [{"type": "input_text", "text": "分析仓库结构"}]},
    {"type": "message", "role": "assistant",
     "content": [{"type": "output_text", "text": "分 backend/ frontend/ k8s/"}]},
]


class InboundRewrite(unittest.TestCase):
    """入站：compaction_trigger -> 显式摘要指令。"""

    def _run(self, items, model="deepseek-v4-flash"):
        data = {"model": model, "input": list(items)}
        return ADAPT._adapt(data, "test")

    def test_trigger_removed_and_instruction_appended(self):
        out = self._run(CONV + [TRIGGER])
        types_ = [i.get("type") for i in out["input"]]
        self.assertNotIn("compaction_trigger", types_,
                         "DeepSeek 不认这个 item type，留着等于当普通对话续写")
        last = out["input"][-1]
        self.assertEqual(last["role"], "user")
        self.assertIn("压缩", last["content"][0]["text"])

    def test_metadata_flag_set_for_outbound(self):
        """出站要靠这个标记决定包不包装。"""
        out = self._run(CONV + [TRIGGER])
        self.assertTrue(out["litellm_metadata"]["deepseek_compaction_v2"])

    def test_max_output_tokens_raised(self):
        """实测 300 会 incomplete、摘要被截断。"""
        data = {"model": "deepseek-v4-flash", "input": CONV + [TRIGGER],
                "max_output_tokens": 300}
        out = ADAPT._adapt(data, "test")
        self.assertGreaterEqual(out["max_output_tokens"], 4096)

    def test_no_trigger_is_noop(self):
        data = {"model": "deepseek-v4-flash", "input": list(CONV)}
        self.assertIs(ADAPT._adapt(data, "test"), data)

    def test_non_deepseek_untouched(self):
        """作用域对照组：打 gpt 时一个字节都不能动。"""
        data = {"model": "chatgpt-gpt-5.6-sol", "input": CONV + [TRIGGER]}
        out = ADAPT._adapt(data, "test")
        self.assertIn("compaction_trigger", [i.get("type") for i in out["input"]])

    def test_original_not_mutated(self):
        items = CONV + [TRIGGER]
        data = {"model": "deepseek-v4-flash", "input": items}
        ADAPT._adapt(data, "test")
        self.assertIn("compaction_trigger", [i.get("type") for i in data["input"]])


class OutboundWrap(unittest.TestCase):
    """出站：message -> compaction item，reasoning 丢掉。"""

    ADDED_MSG = ('data: {"type":"response.output_item.done","output_index":1,'
                 '"item":{"type":"message","role":"assistant","id":"msg_1",'
                 '"content":[{"type":"output_text","text":"摘要：仓库分三块"}]}}')
    ADDED_REASONING = ('data: {"type":"response.output_item.done","output_index":0,'
                       '"item":{"type":"reasoning","id":"rs_1","summary":[]}}')

    def test_message_becomes_compaction(self):
        counts = {}
        out = IDP._wrap_compaction_in_sse(self.ADDED_MSG, counts)
        evt = json.loads(out.split("data: ", 1)[1])
        self.assertEqual(evt["item"]["type"], "compaction")
        self.assertEqual(evt["item"]["encrypted_content"], "摘要：仓库分三块")
        self.assertEqual(counts.get("compaction_wrapped"), 1)

    def test_encrypted_content_is_plain_string(self):
        """官方 schema 里就是 String，不是加密格式 —— 塞明文是对的。"""
        out = IDP._wrap_compaction_in_sse(self.ADDED_MSG, {})
        evt = json.loads(out.split("data: ", 1)[1])
        self.assertIsInstance(evt["item"]["encrypted_content"], str)

    def test_reasoning_item_dropped(self):
        """reasoning 会占 output_item_count 且污染下一轮上下文。"""
        out = IDP._wrap_compaction_in_sse(self.ADDED_REASONING, {})
        self.assertNotIn("reasoning", out)

    def test_completed_frame_output_normalized(self):
        """response.completed 里也要只剩一个 compaction。"""
        raw = ('data: {"type":"response.completed","response":{"status":"completed",'
               '"output":[{"type":"reasoning","id":"rs_2","summary":[]},'
               '{"type":"message","role":"assistant","id":"msg_2",'
               '"content":[{"type":"output_text","text":"摘要正文"}]}]}}')
        counts = {}
        out = IDP._wrap_compaction_in_sse(raw, counts)
        evt = json.loads(out.split("data: ", 1)[1])
        items = evt["response"]["output"]
        self.assertEqual([i["type"] for i in items], ["compaction"],
                         "compaction_count 必须恰好 1，reasoning 要丢掉")

    def test_empty_text_not_wrapped(self):
        """空 message 不该变成空摘要的 compaction。"""
        raw = ('data: {"type":"response.output_item.done","item":'
               '{"type":"message","role":"assistant","content":[]}}')
        counts = {}
        IDP._wrap_compaction_in_sse(raw, counts)
        self.assertEqual(counts, {})

    def test_done_and_malformed_pass_through(self):
        for raw in ("data: [DONE]", 'data: {"broken', "event: ping"):
            self.assertEqual(IDP._wrap_compaction_in_sse(raw, {}), raw)

    def test_contextvar_defaults_false(self):
        """非 compaction 请求恒 False，SSE patch 不会进这条分支。"""
        self.assertFalse(IDP._ACTIVE_COMPACTION.get())


if __name__ == "__main__":
    unittest.main(verbosity=2)
