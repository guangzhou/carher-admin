"""test_codex_compaction_v2.py — zerokey 路径的 Codex remote compaction v2。

复现的现场（2026-08-06，本地 codex 日志 ``~/.codex/logs_2.sqlite``）::

    op=Compact model=gpt-5.6-sol
    response.output_item.done -> item.type = "message"
    text = "我看到你这条消息是空的 🙂 ..."
    → Fatal error: remote compaction v2 expected exactly one compaction
      output item, got 0 from 1 output items

即上游（zerokey 网页路径，``responses.js:34-53`` 把 trigger 拼成一行空的
``"USER: "``）把压缩轮当普通闲聊回了。

本测试按 Codex 客户端的判据验收（``compact_remote_v2.rs``：只数
``response.output_item.done`` 里 type=compaction 的 item，必须恰好 1 个），
另外覆盖回程解码 —— 那是「压缩不报错但下一句就失忆」的根因。

用法::

    python3 -m unittest test_codex_compaction_v2 -v
"""
import importlib.util
import json
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))

ZK_BASE = "http://zero-108.litellm-product.svc.cluster.local:8200/v1"
ACCT_BASE = "http://chatgpt-acct-151.litellm-product.svc.cluster.local:4000"


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


def _load():
    _stub_litellm()
    path = os.path.join(HERE, "..", "codex_compaction_v2.py")
    spec = importlib.util.spec_from_file_location("ccv2", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


M = _load()

TRIGGER = {"type": "compaction_trigger"}
CONV = [
    {"type": "message", "role": "user",
     "content": [{"type": "input_text", "text": "暗号是紫色犀牛42"}]},
    {"type": "message", "role": "assistant",
     "content": [{"type": "output_text", "text": "记住了：紫色犀牛42"}]},
]


def _req(items, base=ZK_BASE, **extra):
    d = {"model": "gpt-5.6-sol", "api_base": base, "input": list(items)}
    d.update(extra)
    return d


def _sse(*events):
    return "\n".join("data: " + json.dumps(e, ensure_ascii=False) for e in events) + "\n"


def _codex_count(sse_text):
    """按 Codex 的判据数：output_item.done 里的 compaction item 个数 + item 总数。"""
    comp = total = 0
    for line in sse_text.split("\n"):
        if not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if not payload or payload == "[DONE]":
            continue
        obj = json.loads(payload)
        if obj.get("type") == "response.output_item.done":
            total += 1
            if (obj.get("item") or {}).get("type") == "compaction":
                comp += 1
    return comp, total


class Gate(unittest.TestCase):
    """只对 zerokey 部署生效 —— 真 chatgpt 池原生支持，动它就是改坏。"""

    def test_chatgpt_acct_deployment_untouched(self):
        d = _req(CONV + [TRIGGER], base=ACCT_BASE, tools=[{"type": "function", "name": "shell"}])
        out = M.adapt_inbound(d, "test")
        self.assertEqual([i.get("type") for i in out["input"]][-1], "compaction_trigger")
        self.assertIn("tools", out, "真池那条路的 tools 不许剥")

    def test_zerokey_deployment_hit(self):
        out = M.adapt_inbound(_req(CONV + [TRIGGER]), "test")
        self.assertNotIn("compaction_trigger", [i.get("type") for i in out["input"]])

    def test_no_api_base_is_noop(self):
        d = {"model": "gpt-5.6-sol", "input": CONV + [TRIGGER]}
        self.assertEqual([i.get("type") for i in M.adapt_inbound(d, "test")["input"]][-1],
                         "compaction_trigger")


class InboundTrigger(unittest.TestCase):

    def setUp(self):
        self.out = M.adapt_inbound(
            _req(CONV + [TRIGGER],
                 tools=[{"type": "function", "name": "shell"}],
                 tool_choice="auto",
                 parallel_tool_calls=True,
                 instructions="You are Codex, an agent based on GPT-5."),
            "test")

    def test_trigger_replaced_with_summary_instruction(self):
        self.assertNotIn("compaction_trigger", [i.get("type") for i in self.out["input"]])
        self.assertIn("压缩", self.out["input"][-1]["content"][0]["text"])

    def test_tool_surface_stripped(self):
        """留着 tools/instructions 模型会继续干活而不是做摘要（竞品 cc-switch 同结论）。"""
        for key in ("tools", "tool_choice", "parallel_tool_calls"):
            self.assertNotIn(key, self.out)
        self.assertNotIn("agent", self.out["instructions"].lower())

    def test_output_budget_raised_above_client_cap(self):
        """2026-08-06 一例：客户端自己发 4096，287k 上下文的摘要写不进去。"""
        out = M.adapt_inbound(_req(CONV + [TRIGGER], max_output_tokens=4096), "test")
        self.assertGreaterEqual(out["max_output_tokens"], 8192)

    def test_metadata_flag(self):
        self.assertTrue(self.out["litellm_metadata"]["codex_compaction_v2"])


class Envelope(unittest.TestCase):

    def test_roundtrip(self):
        blob = M.encode_envelope("暗号 = 紫色犀牛42")
        self.assertTrue(blob.startswith("carher-cmp-v1:"))
        self.assertEqual(M.decode_envelope(blob), "暗号 = 紫色犀牛42")

    def test_foreign_blobs_rejected(self):
        """官方密文 / 别家网关的信封都不是我们能解的。"""
        for foreign in ("litellm_enc:bW9kZWxfaWQ=;gAAAAABqdKoe",
                        "gAAAAABqdKoebaC3tWOtoMnDDFsdfFoX",
                        "ocmp1.eyJ2IjoxfQ==",
                        "cc-switch-compaction-v1:eyJ2IjoxfQ",
                        "", None, 42):
            self.assertIsNone(M.decode_envelope(foreign), foreign)

    def test_corrupt_own_envelope_rejected(self):
        self.assertIsNone(M.decode_envelope("carher-cmp-v1:not-base64!!"))


class InboundReturnTrip(unittest.TestCase):
    """回程：客户端把 compaction item 原样回传，必须解回上下文。

    不做这一步的实测后果：deepseek 侧回传后模型答「我们没聊过」；真池侧被
    chatgpt_responses_normalize.py:151 的 compaction_drop 整项删掉。
    两者的用户感知都是「压缩完就失忆」。
    """

    def test_own_envelope_restored_as_message(self):
        item = {"type": "compaction", "encrypted_content": M.encode_envelope("暗号=紫色犀牛42")}
        out = M.adapt_inbound(_req([item] + CONV), "test")
        first = out["input"][0]
        self.assertEqual(first["type"], "message")
        self.assertIn("紫色犀牛42", first["content"][0]["text"])
        self.assertNotIn("compaction", [i.get("type") for i in out["input"]])

    def test_foreign_blob_becomes_boundary_notice(self):
        """外来密文解不开也不能静默删 —— 静默删就是无声失忆。"""
        item = {"type": "compaction", "encrypted_content": "litellm_enc:xx;gAAAA"}
        out = M.adapt_inbound(_req([item] + CONV), "test")
        first = out["input"][0]
        self.assertEqual(first["type"], "message")
        self.assertIn("无法解开", first["content"][0]["text"])

    def test_context_compaction_alias_handled(self):
        item = {"type": "context_compaction", "encrypted_content": M.encode_envelope("A=1")}
        out = M.adapt_inbound(_req([item]), "test")
        self.assertIn("A=1", out["input"][0]["content"][0]["text"])

    def test_restore_without_trigger_does_not_flag_compaction(self):
        """普通轮次带着历史压缩项进来，不能被当成压缩轮去包装输出。"""
        item = {"type": "compaction", "encrypted_content": M.encode_envelope("A=1")}
        out = M.adapt_inbound(_req([item] + CONV), "test")
        self.assertNotIn("codex_compaction_v2", out.get("litellm_metadata") or {})


class UngatedOwnRestore(unittest.TestCase):
    """解自己的信封不设门控 —— 2026-08-07 实测：带门控那层 4 发只跑到 1 发，
    而链上更早的回调（chatgpt_responses_normalize:151）会把 compaction item
    整项删掉，所以这一步必须提到最早的钩子且不依赖落点判断。"""

    def test_restores_without_any_gate_signal(self):
        d = {"model": "gpt-5.6-sol",  # 无 api_base / 无 model_info
             "input": [{"type": "compaction",
                        "encrypted_content": M.encode_envelope("暗号=紫色犀牛42")}]}
        out = M.restore_own_envelopes(d, "t")
        self.assertEqual(out["input"][0]["type"], "message")
        self.assertEqual(out["input"][0]["role"], "system")
        self.assertIn("紫色犀牛42", out["input"][0]["content"][0]["text"])

    def test_foreign_blob_left_alone_here(self):
        """外来密文是真池的原生上下文，这一层不许碰（边界说明只在带门控那层做）。"""
        item = {"type": "compaction", "encrypted_content": "litellm_enc:xx;gAAAA"}
        d = {"model": "chatgpt-gpt-5.6-sol", "input": [dict(item)]}
        self.assertEqual(M.restore_own_envelopes(d, "t")["input"][0], item)


class OutboundSSE(unittest.TestCase):
    """出站按 Codex 的判据验收。"""

    def _live_stream(self):
        """线上实测的那条流（zero-108 直连，1 个 message item）。"""
        msg = {"type": "message", "id": "msg_1b18", "role": "assistant",
               "content": [{"type": "output_text", "text": "## 已确认事实\n- 暗号：紫色犀牛42"}],
               "status": "completed"}
        return _sse(
            {"type": "response.created"},
            {"type": "response.output_item.added", "output_index": 0, "item": dict(msg, content=[])},
            {"type": "response.output_item.done", "output_index": 0, "item": dict(msg)},
            {"type": "response.completed", "response": {"id": "resp_1", "output": [dict(msg)]}},
        )

    def test_the_reported_failure_is_fixed(self):
        before = _codex_count(self._live_stream())
        self.assertEqual(before, (0, 1), "复现原始故障：0 个 compaction / 1 个 item")
        after = _codex_count(M.wrap_sse(self._live_stream(), {}))
        self.assertEqual(after, (1, 1), "Codex 要求 compaction 恰好 1 个")

    def test_wrapped_item_shape(self):
        out = M.wrap_sse(self._live_stream(), {})
        item = None
        for line in out.split("\n"):
            if line.startswith("data: "):
                obj = json.loads(line[6:])
                if obj.get("type") == "response.output_item.done":
                    item = obj["item"]
        self.assertEqual(item["type"], "compaction")
        self.assertTrue(item["id"].startswith("cmp_"))
        self.assertEqual(M.decode_envelope(item["encrypted_content"]),
                         "## 已确认事实\n- 暗号：紫色犀牛42")

    def test_completed_output_also_rewritten(self):
        out = M.wrap_sse(self._live_stream(), {})
        for line in out.split("\n"):
            if line.startswith("data: "):
                obj = json.loads(line[6:])
                if obj.get("type") == "response.completed":
                    self.assertEqual([o["type"] for o in obj["response"]["output"]], ["compaction"])

    def test_reasoning_dropped(self):
        """reasoning 带 encrypted_content，留着污染下一轮上下文。"""
        stream = _sse(
            {"type": "response.output_item.done", "output_index": 0,
             "item": {"type": "reasoning", "id": "rs_1", "encrypted_content": "x"}},
            {"type": "response.output_item.done", "output_index": 1,
             "item": {"type": "message", "id": "m1", "role": "assistant",
                      "content": [{"type": "output_text", "text": "摘要"}]}},
        )
        self.assertEqual(_codex_count(M.wrap_sse(stream, {})), (1, 1))

    def test_empty_message_not_fabricated(self):
        """空摘要不许硬包成 compaction —— 那等于把上下文换成空气。"""
        stream = _sse({"type": "response.output_item.done", "output_index": 0,
                       "item": {"type": "message", "id": "m1", "role": "assistant",
                                "content": [{"type": "output_text", "text": "   "}]}})
        self.assertEqual(_codex_count(M.wrap_sse(stream, {})), (0, 1))

    def test_idempotent_on_already_compaction(self):
        """deepseek 侧的包装先跑过一遍时不能再包一次。"""
        stream = _sse({"type": "response.output_item.done", "output_index": 0,
                       "item": {"type": "compaction", "id": "cmp_x",
                                "encrypted_content": M.encode_envelope("A")}})
        self.assertEqual(_codex_count(M.wrap_sse(stream, {})), (1, 1))

    def test_non_sse_passthrough(self):
        self.assertEqual(M.wrap_sse("ping\n", {}), "ping\n")


class FullRoundTrip(unittest.TestCase):
    """压缩 → 客户端回传 → 摘要真的回到上下文里。"""

    def test_summary_survives_the_round_trip(self):
        # 1. 压缩轮
        M.adapt_inbound(_req(CONV + [TRIGGER]), "test")
        sse = M.wrap_sse(_sse({"type": "response.output_item.done", "output_index": 0,
                               "item": {"type": "message", "id": "m1", "role": "assistant",
                                        "content": [{"type": "output_text",
                                                     "text": "暗号=紫色犀牛42"}]}}), {})
        item = None
        for line in sse.split("\n"):
            if line.startswith("data: "):
                obj = json.loads(line[6:])
                if obj.get("type") == "response.output_item.done":
                    item = obj["item"]
        # 2. 客户端下一轮原样回传
        nxt = M.adapt_inbound(_req([item, CONV[0]]), "test")
        flat = json.dumps(nxt["input"], ensure_ascii=False)
        self.assertIn("暗号=紫色犀牛42", flat, "摘要必须回到上下文，否则压缩完就失忆")
        self.assertNotIn("carher-cmp-v1", flat, "信封不许原样喂给上游")


if __name__ == "__main__":
    unittest.main()
