"""test_sec_redact.py — 网信脱敏规范第一版（sec_redact.py）回归。

钉住方案文档（飞书 SXegdiYvXoEdXnxClUWcpQ02nfi）定下的口径：
  * 请求方向：敏感串替换后再发，密钥也是替换（不拦截，否则历史里有密钥的会话每轮都被拦）
  * 响应方向：命中密钥 ⇒ 就地替换成 [密钥凭证脱敏]（10-29 起不再阻断）；其余掩码
  * 编程 agent key 只做密钥，IP/手机号原样
  * 流式：跨片段切开的敏感串也要命中；三种形态（chat 对象 / Responses 事件 / anthropic SSE 字节）
  * 日志里不出现敏感原文
  * 影子模式只记不改

用法::

    cd k8s/litellm-callbacks/tests
    .venv/bin/python -m unittest test_sec_redact -v
"""
import asyncio
import importlib.util
import json
import logging
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))

KEY = "sk-proj-" + "a1B2c3D4e5F6g7H8i9J0kLmNoPqRsTuV"  # 合成值，形状同 OpenAI project key
PHONE = "13812345678"
IP = "10.68.13.198"


def _id18():
    b = "11010519900307123"
    w = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    return b + "10X98765432"[sum(int(c) * x for c, x in zip(b, w)) % 11]


def _stub_litellm():
    if "litellm.integrations.custom_logger" in sys.modules:
        return
    litellm = types.ModuleType("litellm")
    integrations = types.ModuleType("litellm.integrations")
    custom_logger = types.ModuleType("litellm.integrations.custom_logger")

    class CustomLogger:
        pass

    custom_logger.CustomLogger = CustomLogger
    integrations.custom_logger = custom_logger
    litellm.integrations = integrations
    sys.modules.setdefault("litellm", litellm)
    sys.modules["litellm.integrations"] = integrations
    sys.modules["litellm.integrations.custom_logger"] = custom_logger


def _load(env=None):
    for k in ("SEC_REDACT_DISABLED", "SEC_REDACT_SHADOW", "SEC_REDACT_AGENT_PREFIXES"):
        os.environ.pop(k, None)
    os.environ.update(env or {})
    _stub_litellm()
    spec = importlib.util.spec_from_file_location("sec_redact_t", os.path.join(HERE, "..", "sec_redact.py"))
    m = importlib.util.module_from_spec(spec)
    # 故意**不**登记 sys.modules：和 litellm 加载 callback 的方式一致。
    # 10-20 登记过一次，把 @dataclass 在线上 import 期崩溃的 bug 盖住了，全链 T0 才抓到。
    spec.loader.exec_module(m)
    return m


M = _load()


class Who:
    def __init__(self, alias="carher-test", user_id="u1"):
        self.key_alias, self.user_id = alias, user_id


HER, AGENT = Who("carher-hermes-1"), Who("cursor-zhangsan")


def run(coro):
    return asyncio.run(coro)


async def _collect(agen):
    return [x async for x in agen]


async def _aiter(items):
    for x in items:
        yield x


class Obj:
    """模拟 pydantic 对象：属性读写。"""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def chat_chunk(text, finish=None, index=0):
    return Obj(choices=[Obj(index=index, delta=Obj(content=text), finish_reason=finish)])


def chat_text(chunks):
    return "".join((c.choices[0].delta.content or "") for c in chunks)


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class RulesTest(unittest.TestCase):
    def redact(self, t, rules=None):
        return M.apply_hits(t, M.scan(t, rules or M.RULES))

    def test_phone_mask_and_boundaries(self):
        self.assertEqual(self.redact(f"请联系 {PHONE} 处理"), "请联系 138****5678 处理")
        self.assertEqual(self.redact("ts=1729412345678123 订单 113812345678"),
                         "ts=1729412345678123 订单 113812345678")

    def test_id_card_checksum(self):
        good = _id18()
        self.assertEqual(self.redact(f"身份证 {good}"), "身份证 " + good[:6] + "*" * 12)
        bad = good[:-1] + ("0" if good[-1] != "0" else "1")
        self.assertEqual(self.redact(f"身份证 {bad}"), f"身份证 {bad}")

    def test_bank_card_luhn(self):
        self.assertEqual(self.redact("卡号 4111111111111111"), "卡号 ************1111")
        self.assertEqual(self.redact("卡号 4111111111111112"), "卡号 4111111111111112")

    def test_private_ip_only(self):
        self.assertEqual(self.redact(f"连 {IP}:30402 和 8.8.8.8"), "连 [内网IP脱敏]:30402 和 8.8.8.8")

    def test_mac_needs_context(self):
        self.assertEqual(self.redact("网卡 00:1A:2B:3C:4D:5E 在线"), "网卡 [设备硬件信息脱敏] 在线")
        self.assertEqual(self.redact("hash 00:1A:2B:3C:4D:5E"), "hash 00:1A:2B:3C:4D:5E")

    def test_example_whitelist_not_for_credentials(self):
        self.assertEqual(self.redact(f"示例手机号 {PHONE}"), f"示例手机号 {PHONE}")
        self.assertIn(M.CRED_LABEL, self.redact(f"示例 key={KEY}"))

    def test_multiple_hits_all_replaced(self):
        self.assertEqual(self.redact("13812345678 和 13987654321"), "138****5678 和 139****4321")

    def test_credential_shapes(self):
        cases = [
            KEY,
            "AKIA" + "ABCDEFGHIJKLMNOP",
            "LTAI" + "5tAbCdEfGhIjKlMn",
            "ghp_" + "a" * 18 + "B" * 18,
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
            "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\n-----END OPENSSH PRIVATE KEY-----",
            "password: Xk9#mP2qLw8zR4vT",
            "数据库密码：Qw3rTy9UiOp2AsDf",
        ]
        for c in cases:
            with self.subTest(c=c[:20]):
                self.assertTrue(any(h.rule.startswith("3-") for h in M.scan(c)), c)

    def test_credential_false_positives_spec_literal_would_hit(self):
        for c in ('{"max_tokens": 4096, "access_token": getToken()}',
                  "api_key = os.environ['OPENAI_API_KEY']",
                  "password: ${DB_PASSWORD}",
                  "SECRET_KEY=your_secret_key_here",
                  "token_count = 123456789012345678"):
            with self.subTest(c=c):
                self.assertFalse(M.scan(c), c)

    def test_assign_keeps_key_name(self):
        self.assertEqual(self.redact("password: Xk9#mP2qLw8zR4vT"), "password: " + M.CRED_LABEL)

    def test_agent_rules_credentials_only(self):
        t = f"{IP} {PHONE} {KEY}"
        out = self.redact(t, M._CRED_RULES)
        self.assertIn(IP, out)
        self.assertIn(PHONE, out)
        self.assertNotIn(KEY, out)

    def test_cross_line(self):
        self.assertIn("[内网IP脱敏]", self.redact(f"第一行\n第二行 {IP}\n"))


class RequestTest(unittest.TestCase):
    def test_chat_messages_replaced_in_place_including_history(self):
        data = {"model": "x", "litellm_call_id": "c1", "messages": [
            {"role": "user", "content": f"我的 key={KEY}"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": [{"type": "text", "text": f"电话 {PHONE} 机器 {IP}"}]},
        ]}
        out = run(M.sec_redact.async_pre_call_hook(HER, None, data, "acompletion"))
        self.assertIs(out, data)
        dumped = json.dumps(data, ensure_ascii=False)
        for raw in (KEY, PHONE, IP):
            self.assertNotIn(raw, dumped)
        self.assertIn("138****5678", dumped)

    def test_tool_result_and_tool_args(self):
        data = {"messages": [
            {"role": "assistant", "tool_calls": [{"id": "t1", "type": "function",
                                                  "function": {"name": "sh", "arguments": json.dumps({"cmd": f"export K={KEY}"})}}]},
            {"role": "tool", "tool_call_id": "t1", "content": f"OPENAI_API_KEY={KEY}"},
        ]}
        run(M.sec_redact.async_pre_call_hook(AGENT, None, data, "acompletion"))
        self.assertNotIn(KEY, json.dumps(data))

    def test_responses_input_and_anthropic_system(self):
        data = {"input": [{"type": "function_call_output", "call_id": "x", "output": f"key {KEY}"}],
                "instructions": f"内网 {IP}"}
        run(M.sec_redact.async_pre_call_hook(HER, None, data, "aresponses"))
        self.assertNotIn(KEY, json.dumps(data))
        self.assertNotIn(IP, json.dumps(data))
        data = {"system": [{"type": "text", "text": f"pw password={'Zx8#' * 5}"}],
                "messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": f"{KEY}"}]}]}
        run(M.sec_redact.async_pre_call_hook(AGENT, None, data, "anthropic_messages"))
        self.assertNotIn(KEY, json.dumps(data))

    def test_agent_keeps_ip_and_phone(self):
        data = {"messages": [{"role": "user", "content": f"连 {IP} 电话 {PHONE} key {KEY}"}]}
        run(M.sec_redact.async_pre_call_hook(AGENT, None, data, "acompletion"))
        c = data["messages"][0]["content"]
        self.assertIn(IP, c)
        self.assertIn(PHONE, c)
        self.assertNotIn(KEY, c)

    def test_metadata_and_ids_untouched(self):
        data = {"metadata": {"note": KEY}, "litellm_call_id": IP, "messages": [{"role": "user", "content": "hi"}]}
        run(M.sec_redact.async_pre_call_hook(HER, None, data, "acompletion"))
        self.assertEqual(data["metadata"]["note"], KEY)
        self.assertEqual(data["litellm_call_id"], IP)

    def test_embedding_skipped(self):
        data = {"input": [f"{IP}"]}
        run(M.sec_redact.async_pre_call_hook(HER, None, data, "aembedding"))
        self.assertEqual(data["input"], [IP])

    def test_callTypes_enum_value(self):
        class CT:
            value = "aembedding"
        data = {"input": [IP]}
        run(M.sec_redact.async_pre_call_hook(HER, None, data, CT()))
        self.assertEqual(data["input"], [IP])

    def test_cache_same_result_and_reused(self):
        long = ("正常代码行 x = 1\n" * 40) + f" key={KEY} 电话 {PHONE}"
        d1 = {"messages": [{"role": "user", "content": long}]}
        d2 = {"messages": [{"role": "user", "content": long}]}
        run(M.sec_redact.async_pre_call_hook(HER, None, d1, "acompletion"))
        n = len(M._SCAN_CACHE)
        run(M.sec_redact.async_pre_call_hook(HER, None, d2, "acompletion"))
        self.assertEqual(d1, d2)
        self.assertEqual(len(M._SCAN_CACHE), n)
        self.assertNotIn(KEY, d2["messages"][0]["content"])
        # 规则集不同（agent）不能复用 her 的结果
        d3 = {"messages": [{"role": "user", "content": long}]}
        run(M.sec_redact.async_pre_call_hook(AGENT, None, d3, "acompletion"))
        self.assertIn(PHONE, d3["messages"][0]["content"])

    def test_log_has_no_raw_secret(self):
        cap = LogCapture()
        M._log.addHandler(cap)
        try:
            run(M.sec_redact.async_pre_call_hook(HER, None, {"messages": [{"role": "user", "content": f"{KEY} {PHONE}"}]}, "acompletion"))
        finally:
            M._log.removeHandler(cap)
        joined = "\n".join(cap.lines)
        self.assertIn("sec_redact: hit", joined)
        self.assertIn("rule=3-平台Token", joined)
        self.assertNotIn(KEY, joined)
        self.assertNotIn(PHONE, joined)

    def test_audit_lines_capped_per_segment(self):
        # 10-28：一段代码里内网 IP 命中 461 次 ⇒ 曾逐条写 461 行 WARNING，耗时几乎全在写日志
        cap = LogCapture()
        M._log.addHandler(cap)
        try:
            text = f"连 {IP}\n" * 200
            run(M.sec_redact.async_pre_call_hook(HER, None, {"messages": [{"role": "user", "content": text}]}, "acompletion"))
        finally:
            M._log.removeHandler(cap)
        detail = [l for l in cap.lines if l.startswith("sec_redact: hit ")]
        summary = [l for l in cap.lines if l.startswith("sec_redact: hit-summary")]
        self.assertEqual(len(detail), M._AUDIT_DETAIL_MAX)
        self.assertEqual(len(summary), 1)
        self.assertIn("more=180", summary[0])
        self.assertIn("5.1-内网IP:180", summary[0])
        self.assertNotIn(IP, "\n".join(cap.lines))

    def test_internal_error_passes_through(self):
        orig = M._walk_replace
        M._walk_replace = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            data = {"messages": [{"role": "user", "content": KEY}]}
            out = run(M.sec_redact.async_pre_call_hook(HER, None, data, "acompletion"))
        finally:
            M._walk_replace = orig
        self.assertIs(out, data)


class NonStreamResponseTest(unittest.TestCase):
    def test_chat_mask(self):
        resp = Obj(choices=[Obj(message=Obj(content=f"打 {PHONE}"))])
        run(M.sec_redact.async_post_call_success_hook({}, HER, resp))
        self.assertEqual(resp.choices[0].message.content, "打 138****5678")

    def test_chat_credential_replaced_not_blocked(self):
        resp = Obj(choices=[Obj(message=Obj(content=f"给你 {KEY} 拿去用"))])
        run(M.sec_redact.async_post_call_success_hook({}, AGENT, resp))
        self.assertEqual(resp.choices[0].message.content, f"给你 {M.CRED_LABEL} 拿去用")

    def test_no_block_message_anywhere(self):
        self.assertFalse(hasattr(M, "BLOCK_MESSAGE"))

    def test_responses_object_and_anthropic_dict(self):
        resp = Obj(output=[Obj(content=[Obj(text=f"key {KEY}")])])
        run(M.sec_redact.async_post_call_success_hook({}, HER, resp))
        self.assertEqual(resp.output[0].content[0].text, f"key {M.CRED_LABEL}")
        resp = {"type": "message", "content": [{"type": "text", "text": f"内网 {IP}"}]}
        run(M.sec_redact.async_post_call_success_hook({}, HER, resp))
        self.assertEqual(resp["content"][0]["text"], "内网 [内网IP脱敏]")


class StreamChatTest(unittest.TestCase):
    def stream(self, pieces, who=HER):
        chunks = [chat_chunk(p) for p in pieces] + [chat_chunk("", finish="stop")]
        return run(_collect(M.sec_redact.async_post_call_streaming_iterator_hook(who, _aiter(chunks), {})))

    def test_split_phone_masked(self):
        out = self.stream(["电话是 138", "1234", "5678 ，", "谢谢" + "。" * 200])
        self.assertEqual(chat_text(out), "电话是 138****5678 ，谢谢" + "。" * 200)

    def test_split_key_replaced_and_rest_kept(self):
        half = len(KEY) // 2
        out = self.stream(["这是你的密钥 ", KEY[:half], KEY[half:], " 后面还有正文" * 30])
        text = chat_text(out)
        self.assertNotIn(KEY[:half], text)
        self.assertEqual(text, "这是你的密钥 " + M.CRED_LABEL + " 后面还有正文" * 30)

    def test_stream_private_key_longer_than_holdback(self):
        # 私钥正文远超 _HOLDBACK：不能先把 BEGIN 后面的 base64 行放出去
        body = "\n".join("QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWZnaGlqa2xtbm9wcXJzdHV2d3h5ejAx" for _ in range(30))
        pem = "-----BEGIN OPENSSH PRIVATE KEY-----\n" + body + "\n-----END OPENSSH PRIVATE KEY-----"
        pieces = ["私钥如下：\n"] + [pem[i:i + 23] for i in range(0, len(pem), 23)] + ["\n用完删掉。"]
        text = chat_text(self.stream(pieces, who=AGENT))
        self.assertEqual(text, "私钥如下：\n" + M.CRED_LABEL + "\n用完删掉。")

    def test_stream_truncated_private_key_body_not_leaked(self):
        body = "\n".join("QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWZnaGlqa2xtbm9wcXJzdHV2d3h5ejAx" for _ in range(10))
        pem = "-----BEGIN RSA PRIVATE KEY-----\n" + body
        text = chat_text(self.stream([pem[i:i + 40] for i in range(0, len(pem), 40)], who=AGENT))
        self.assertNotIn("QUJDREVG", text)
        self.assertIn(M.CRED_LABEL, text)

    def test_clean_stream_is_identical(self):
        pieces = [f"第{i}段正常内容，" for i in range(80)]
        self.assertEqual(chat_text(self.stream(pieces)), "".join(pieces))

    def test_agent_stream_ip_untouched(self):
        self.assertEqual(chat_text(self.stream([f"改成 {IP} 即可"], who=AGENT)), f"改成 {IP} 即可")

    def test_shadow_mode_no_change(self):
        global M
        M = _load({"SEC_REDACT_SHADOW": "1"})
        try:
            out = self.stream([f"key {KEY} 电话 {PHONE}"])
            self.assertEqual(chat_text(out), f"key {KEY} 电话 {PHONE}")
            data = {"messages": [{"role": "user", "content": KEY}]}
            run(M.sec_redact.async_pre_call_hook(HER, None, data, "acompletion"))
            self.assertEqual(data["messages"][0]["content"], KEY)
        finally:
            M = _load()

    def test_shadow_stream_survives_scanner_crash(self):
        # 影子模式下扫描出错也必须原样放完整条流（对用户零影响）
        global M
        M = _load({"SEC_REDACT_SHADOW": "1"})
        try:
            orig = M.scan
            M.scan = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
            pieces = [f"key {KEY}", " 电话 ", PHONE, " 结束"]
            out = self.stream(pieces)
            self.assertEqual(chat_text(out), "".join(pieces))
            evs = [{"type": "response.output_text.delta", "item_id": "m", "content_index": 0, "delta": KEY},
                   {"type": "response.completed", "response": {"output": [{"content": [{"text": KEY}]}]}}]
            got = run(_collect(M.sec_redact.async_post_call_streaming_iterator_hook(HER, _aiter(evs), {})))
            self.assertEqual(got, evs)
        finally:
            M.scan = orig
            M = _load()

    def test_shadow_stream_logs_hits(self):
        global M
        M = _load({"SEC_REDACT_SHADOW": "1"})
        cap = LogCapture()
        M._log.addHandler(cap)
        try:
            half = len(KEY) // 2
            out = self.stream(["你的 ", KEY[:half], KEY[half:], " 好"])
            self.assertIn(KEY, chat_text(out))
        finally:
            M._log.removeHandler(cap)
            M = _load()
        joined = "\n".join(cap.lines)
        self.assertIn("dir=response action=shadow rule=3-平台Token", joined)
        self.assertNotIn(KEY, joined)

    def test_disabled(self):
        global M
        M = _load({"SEC_REDACT_DISABLED": "1"})
        try:
            self.assertEqual(chat_text(self.stream([KEY])), KEY)
        finally:
            M = _load()


class StreamResponsesTest(unittest.TestCase):
    def events(self, pieces):
        evs = [{"type": "response.created"}]
        for p in pieces:
            evs.append({"type": "response.output_text.delta", "item_id": "m1", "content_index": 0, "delta": p})
        full = "".join(pieces)
        evs += [{"type": "response.output_text.done", "item_id": "m1", "content_index": 0, "text": full},
                {"type": "response.content_part.done", "part": {"type": "output_text", "text": full}},
                {"type": "response.completed", "response": {"output": [{"content": [{"type": "output_text", "text": full}]}]}}]
        return run(_collect(M.sec_redact.async_post_call_streaming_iterator_hook(HER, _aiter(evs), {})))

    def test_mask_across_deltas_and_done_copies(self):
        out = self.events(["机器 10.68.", "13.198 上线"])
        deltas = "".join(e["delta"] for e in out if e["type"] == "response.output_text.delta")
        self.assertEqual(deltas, "机器 [内网IP脱敏] 上线")
        done = next(e for e in out if e["type"] == "response.output_text.done")
        self.assertEqual(done["text"], "机器 [内网IP脱敏] 上线")
        self.assertNotIn(IP, json.dumps(out, ensure_ascii=False))

    def test_reasoning_and_done_copies_real_deepseek_shape(self):
        # 10-20 gray 真实 deepseek 上游的泄漏形态：密钥先出现在 reasoning，再出现在正文；
        # 阻断后仍从 reasoning_text.done.text / content_part.done.part.text / output_item.done.item.content[0].text 漏出
        evs = [
            Obj(type="response.reasoning_text.delta", item_id="r1", content_index=0, delta="拼接得到 " + KEY[:20]),
            Obj(type="response.reasoning_text.delta", item_id="r1", content_index=0, delta=KEY[20:]),
            Obj(type="response.reasoning_text.done", item_id="r1", content_index=0, text="拼接得到 " + KEY),
            Obj(type="response.output_text.delta", item_id="m1", content_index=0, delta=KEY),
            Obj(type="response.output_text.done", item_id="m1", content_index=0, text=KEY),
            Obj(type="response.content_part.done", item_id="m1", content_index=0, part=Obj(type="output_text", text=KEY)),
            Obj(type="response.output_item.done", item=Obj(type="message", content=[Obj(type="output_text", text=KEY)])),
            Obj(type="response.completed", response=Obj(output=[Obj(content=[Obj(text=KEY)])])),
        ]
        out = run(_collect(M.sec_redact.async_post_call_streaming_iterator_hook(AGENT, _aiter(evs), {})))

        def dump(o):
            if isinstance(o, Obj):
                return {k: dump(v) for k, v in o.__dict__.items()}
            if isinstance(o, list):
                return [dump(x) for x in o]
            return o
        s = json.dumps([dump(o) for o in out], ensure_ascii=False)
        self.assertNotIn(KEY, s)
        self.assertNotIn(KEY[:20], s)
        visible = "".join(o.delta for o in out if o.type == "response.output_text.delta")
        self.assertEqual(visible, M.CRED_LABEL)
        reasoning = "".join(o.delta for o in out if o.type == "response.reasoning_text.delta")
        self.assertEqual(reasoning, "拼接得到 " + M.CRED_LABEL)

    def test_tail_event_object_form_no_raw_copy(self):
        # 对象形态（pydantic）：尾巴事件若从 done 事件复制，会带出整段原文 text
        full = f"电话 {PHONE} 结束"
        evs = [Obj(type="response.output_text.delta", item_id="m1", content_index=0, delta=full),
               Obj(type="response.output_text.done", item_id="m1", content_index=0, text=full)]
        out = run(_collect(M.sec_redact.async_post_call_streaming_iterator_hook(HER, _aiter(evs), {})))
        self.assertNotIn(PHONE, json.dumps([o.__dict__ for o in out], ensure_ascii=False))
        self.assertEqual("".join(o.delta for o in out if o.type == "response.output_text.delta"), "电话 138****5678 结束")

    def test_delta_event_with_full_text_copy(self):
        evs = [{"type": "response.output_text.delta", "item_id": "m1", "content_index": 0,
                "delta": f"机器 {IP}", "text": f"机器 {IP}"}]
        out = run(_collect(M.sec_redact.async_post_call_streaming_iterator_hook(HER, _aiter(evs), {})))
        self.assertNotIn(IP, json.dumps(out, ensure_ascii=False))

    def test_completed_chat_shaped_copies(self):
        # 10-20 全链 T0：litellm 在 response.completed 里多塞两份 chat 形态副本，只改 output 会漏
        full = f"电话 {PHONE}"
        evs = [{"type": "response.output_text.delta", "item_id": "m1", "content_index": 0, "delta": full},
               {"type": "response.completed", "text": full,
                "response": {"output": [], "choices": [{"message": {"content": full}}]}}]
        out = run(_collect(M.sec_redact.async_post_call_streaming_iterator_hook(HER, _aiter(evs), {})))
        self.assertNotIn(PHONE, json.dumps(out, ensure_ascii=False))

    def test_credential_replaced_in_deltas_and_done_copies(self):
        out = self.events(["key: ", KEY, " 结束"])
        self.assertNotIn(KEY, json.dumps(out, ensure_ascii=False))
        deltas = "".join(e["delta"] for e in out if e["type"] == "response.output_text.delta")
        self.assertEqual(deltas, f"key: {M.CRED_LABEL} 结束")
        done = next(e for e in out if e["type"] == "response.output_text.done")
        self.assertEqual(done["text"], f"key: {M.CRED_LABEL} 结束")


class StreamAnthropicTest(unittest.TestCase):
    @staticmethod
    def sse(obj, name):
        return f"event: {name}\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()

    def run_bytes(self, pieces, with_stop=True, split_bytes=True):
        evs = [self.sse({"type": "message_start", "message": {}}, "message_start"),
               self.sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}, "content_block_start")]
        for p in pieces:
            evs.append(self.sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": p}}, "content_block_delta"))
        if with_stop:
            evs.append(self.sse({"type": "content_block_stop", "index": 0}, "content_block_stop"))
        evs += [self.sse({"type": "message_delta", "delta": {"stop_reason": "end_turn"}}, "message_delta"),
                self.sse({"type": "message_stop"}, "message_stop")]
        raw = b"".join(evs)
        chunks = [raw[i:i + 37] for i in range(0, len(raw), 37)] if split_bytes else evs
        out = b"".join(run(_collect(M.sec_redact.async_post_call_streaming_iterator_hook(AGENT, _aiter(chunks), {}))))
        text = ""
        for ev in out.split(b"\n\n"):
            for line in ev.split(b"\n"):
                if line.startswith(b"data:"):
                    o = json.loads(line[5:])
                    if o.get("type") == "content_block_delta":
                        text += o["delta"]["text"]
        return out, text

    def test_replace_with_byte_splits(self):
        out, text = self.run_bytes(["密钥 ", KEY[:10], KEY[10:], " 后续"])
        self.assertNotIn(KEY.encode(), out)
        self.assertEqual(text, f"密钥 {M.CRED_LABEL} 后续")
        self.assertIn(b"message_stop", out)

    def test_clean_text_identical_without_block_stop(self):
        pieces = ["你好，", "这是一段", "正常回复。"]
        _, text = self.run_bytes(pieces, with_stop=False)
        self.assertEqual(text, "".join(pieces))

    def test_tail_flushed_before_block_stop(self):
        out, text = self.run_bytes(["短句"])
        self.assertEqual(text, "短句")
        self.assertLess(out.index("短句".encode()), out.index(b"content_block_stop"))


class StreamAnthropicNoTerminalEventTest(unittest.TestCase):
    """流没走到收尾事件就结束 ⇒ _HOLDBACK 扣着的正文必须放干（2026-11-05）。

    `_HOLDBACK` 让每路正文永远扣住最后 160 字符，原先只在 content_block_stop /
    message_delta / message_stop 上放。短于 160 字符的回复整句都在缓冲里，
    一旦流没走到这三种事件（上游断开、末尾残留不完整事件、收尾事件 JSON 坏掉），
    用户就看到半句或空回复。

    上面的 StreamAnthropicTest.run_bytes 永远补发 message_delta + message_stop，
    所以兜底始终生效 —— 这几个场景是它覆盖不到的。
    """

    SHORT = "等一下，这组凭据有个重要发现——先核"

    @staticmethod
    def sse(obj, name):
        return f"event: {name}\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()

    def _head(self, text, seg=6):
        evs = [self.sse({"type": "message_start", "message": {}}, "message_start"),
               self.sse({"type": "content_block_start", "index": 0,
                         "content_block": {"type": "text", "text": ""}}, "content_block_start")]
        for i in range(0, len(text), seg):
            evs.append(self.sse({"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": text[i:i + seg]}},
                                "content_block_delta"))
        return evs

    def _text_of(self, out: bytes) -> str:
        text = ""
        for ev in out.split(b"\n\n"):
            for line in ev.split(b"\n"):
                if line.startswith(b"data:"):
                    try:
                        o = json.loads(line[5:])
                    except Exception:
                        continue
                    if o.get("type") == "content_block_delta":
                        text += o.get("delta", {}).get("text", "")
        return text

    def _drive(self, chunks, raise_at_end=False):
        async def up():
            for c in chunks:
                yield c
            if raise_at_end:
                raise RuntimeError("upstream closed")

        async def collect():
            got = []
            try:
                async for item in M.sec_redact.async_post_call_streaming_iterator_hook(
                        AGENT, up(), {}):
                    if isinstance(item, (bytes, bytearray)):
                        got.append(bytes(item))
            except RuntimeError:
                pass  # 断流本身照旧向上抛，这里只验证放干的内容
            return b"".join(got)

        return run(collect())

    def test_no_terminal_event_at_all(self):
        out = self._drive(self._head(self.SHORT))
        self.assertEqual(self._text_of(out), self.SHORT, "没有任何收尾事件时正文不能被吞")

    def test_upstream_raises_before_terminal_event(self):
        out = self._drive(self._head(self.SHORT), raise_at_end=True)
        self.assertEqual(self._text_of(out), self.SHORT, "上游断开前扣着的正文必须放干")

    def test_trailing_incomplete_event(self):
        chunks = self._head(self.SHORT) + [b'event: message_stop\ndata: {"type": "message_s']
        out = self._drive(chunks)
        self.assertEqual(self._text_of(out), self.SHORT, "末尾残留不完整事件时正文不能被吞")

    def test_terminal_event_broken_json(self):
        chunks = self._head(self.SHORT) + ["event: message_stop\ndata: {坏JSON\n\n".encode()]
        out = self._drive(chunks)
        self.assertEqual(self._text_of(out), self.SHORT, "收尾事件 JSON 坏掉时正文不能被吞")

    def test_secret_still_redacted_when_stream_breaks(self):
        """阳性对照：放干不能把脱敏放过去 —— 断流时密钥照样要被替换。"""
        out = self._drive(self._head(f"key={KEY} 完毕"), raise_at_end=True)
        self.assertNotIn(KEY.encode(), out, "断流放干时密钥绝不能原样出网")
        self.assertIn(M.CRED_LABEL, self._text_of(out))


class StreamChatNoFinishReasonTest(unittest.TestCase):
    """chat 形态同一个洞：缺 finish_reason 或上游断开 ⇒ 扣着的正文必须放干。

    StreamChatTest.stream 永远补发 ``chat_chunk("", finish="stop")``，兜底始终生效，
    所以那组测试覆盖不到这里。
    """

    SHORT = "等一下，这组凭据有个重要发现——先核"

    def _drive(self, pieces, raise_at_end=False, finish=False, who=AGENT):
        chunks = [chat_chunk(p) for p in pieces]
        if finish:
            chunks.append(chat_chunk("", finish="stop"))

        async def up():
            for c in chunks:
                yield c
            if raise_at_end:
                raise RuntimeError("upstream closed")

        async def collect():
            got = []
            try:
                async for item in M.sec_redact.async_post_call_streaming_iterator_hook(who, up(), {}):
                    got.append(item)
            except RuntimeError:
                pass
            return got

        return run(collect())

    def _segs(self, text, n=6):
        return [text[i:i + n] for i in range(0, len(text), n)]

    def test_no_finish_reason(self):
        out = self._drive(self._segs(self.SHORT))
        self.assertEqual(chat_text(out), self.SHORT, "缺 finish_reason 时正文不能被吞")

    def test_upstream_raises(self):
        out = self._drive(self._segs(self.SHORT), raise_at_end=True)
        self.assertEqual(chat_text(out), self.SHORT, "上游断开时扣着的正文必须放干")

    def test_secret_still_redacted_on_break(self):
        out = self._drive(self._segs(f"key={KEY} 完毕", 7), raise_at_end=True)
        text = chat_text(out)
        self.assertNotIn(KEY, text, "chat 放干时密钥绝不能原样出网")
        self.assertIn(M.CRED_LABEL, text)

    def test_no_duplicate_when_finish_reason_present(self):
        out = self._drive(self._segs(self.SHORT), finish=True)
        self.assertEqual(chat_text(out), self.SHORT, "有 finish_reason 时放干不应重复下发")


class StreamResponsesNoDoneTest(unittest.TestCase):
    """Responses 形态同一个洞：缺 ``*.done`` 或上游断开 ⇒ 扣着的正文必须放干。"""

    SHORT = "等一下，这组凭据有个重要发现——先核"

    def _drive(self, pieces, raise_at_end=False, with_done=False, who=AGENT):
        evs = [{"type": "response.output_text.delta", "item_id": "m1",
                "content_index": 0, "delta": p} for p in pieces]
        if with_done:
            evs.append({"type": "response.output_text.done", "item_id": "m1",
                        "content_index": 0, "text": "".join(pieces)})

        async def up():
            for e in evs:
                yield e
            if raise_at_end:
                raise RuntimeError("upstream closed")

        async def collect():
            got = []
            try:
                async for item in M.sec_redact.async_post_call_streaming_iterator_hook(who, up(), {}):
                    got.append(item)
            except RuntimeError:
                pass
            return got

        return run(collect())

    @staticmethod
    def _text(out):
        return "".join(e.get("delta") or "" for e in out
                       if isinstance(e, dict) and e.get("type") == "response.output_text.delta")

    def _segs(self, text, n=6):
        return [text[i:i + n] for i in range(0, len(text), n)]

    def test_no_done_event(self):
        out = self._drive(self._segs(self.SHORT))
        self.assertEqual(self._text(out), self.SHORT, "缺 .done 时正文不能被吞")

    def test_upstream_raises(self):
        out = self._drive(self._segs(self.SHORT), raise_at_end=True)
        self.assertEqual(self._text(out), self.SHORT, "上游断开时扣着的正文必须放干")

    def test_secret_still_redacted_on_break(self):
        out = self._drive(self._segs(f"key={KEY} 完毕", 7), raise_at_end=True)
        text = self._text(out)
        self.assertNotIn(KEY, text, "Responses 放干时密钥绝不能原样出网")
        self.assertIn(M.CRED_LABEL, text)

    def test_no_duplicate_when_done_present(self):
        out = self._drive(self._segs(self.SHORT), with_done=True)
        self.assertEqual(self._text(out), self.SHORT, "有 .done 时放干不应重复下发")


class IdentityTest(unittest.TestCase):
    """响应里模型自称 Kiro ⇒ 换掉（2026-11-05 用户硬要求）。

    样本取自 198 SpendLogs 真实回复；合法提及取自本仓真实文件名/说法。
    """

    REAL = [
        "我是 Kiro，一个 AI 驱动的开发环境。",
        "Hi! 我是 Kiro，在 carher-admin 仓库里待命。",
        "Hi, I'm Kiro. Your message came through",
        "Hi! I'm Kiro, your AI dev environment",
        "I am Kiro, an AI assistant",
        "我是Kiro，一个AI编程助手",
        "This is Kiro speaking",
        "I’m Kiro!",
    ]
    LEGIT = [
        "运行 scripts/kiro-pool.py show 看池子",
        "kiro-rs-slots 只有 1 个副本",
        "我是 kiro-rs 的维护者吗？不是",
        "I'm Kiro-curious, let me read kiro_common.py",
        "我们的 Kiro 账号池现在 947 个号",
        "上游是 Kiro（AWS），我们改不了它的系统提示",
        "This is kiro.rs upstream",
        "this is kiro/src/main.rs",
        "I am kiro_common's author",
    ]

    def setUp(self):
        os.environ.pop("SEC_REDACT_IDENTITY_DISABLED", None)

    def tearDown(self):
        os.environ.pop("SEC_REDACT_IDENTITY_DISABLED", None)

    def rules(self, who=AGENT):
        return M._response_rules_for(who)

    def test_real_self_identification_replaced(self):
        for s in self.REAL:
            out = M.apply_hits(s, M.scan(s, self.rules()))
            self.assertNotRegex(out, r"(?i)(我是|I'm|I’m|I am|This is)\s*Kiro", s)

    def test_legit_mentions_untouched(self):
        for s in self.LEGIT:
            self.assertEqual(M.apply_hits(s, M.scan(s, self.rules())), s, s)

    def test_rest_of_sentence_kept(self):
        s = "我是 Kiro，一个 AI 驱动的开发环境。"
        self.assertEqual(M.apply_hits(s, M.scan(s, self.rules())), f"我是 {M.IDENTITY_LABEL}，一个 AI 驱动的开发环境。")
        s = "Hi, I'm Kiro. Your message came through"
        self.assertEqual(M.apply_hits(s, M.scan(s, self.rules())), "Hi, I'm an AI coding assistant. Your message came through")

    def test_applies_to_all_key_types(self):
        s = "我是 Kiro，一个 AI 编程助手"
        for who in (AGENT, HER, Who("claude-code-x"), Who("codex-x"), Who("whatever")):
            self.assertNotIn("Kiro", M.apply_hits(s, M.scan(s, M._response_rules_for(who))), who.key_alias)

    def test_request_direction_untouched(self):
        """员工自己说「我是 Kiro」或历史里有 ⇒ 不改请求，免得模型对不上上下文。"""
        data = {"messages": [{"role": "user", "content": "上次你说「我是 Kiro」，为什么？"}]}
        run(M.sec_redact.async_pre_call_hook(AGENT, None, data, "acompletion"))
        self.assertEqual(data["messages"][0]["content"], "上次你说「我是 Kiro」，为什么？")

    def test_kill_switch(self):
        os.environ["SEC_REDACT_IDENTITY_DISABLED"] = "1"
        s = "我是 Kiro，一个 AI 编程助手"
        self.assertEqual(M.apply_hits(s, M.scan(s, M._response_rules_for(AGENT))), s)

    def _chat_stream(self, pieces, finish=True):
        chunks = [chat_chunk(p) for p in pieces] + ([chat_chunk("", finish="stop")] if finish else [])
        return chat_text(run(_collect(M.sec_redact.async_post_call_streaming_iterator_hook(AGENT, _aiter(chunks), {}))))

    def test_stream_chat_split_across_chunks(self):
        # 「我是 Ki」「ro，一个」切开 ⇒ 缓冲必须拼上再换
        text = self._chat_stream(["你好！我是 Ki", "ro，一个 AI ", "驱动的开发环境。"])
        self.assertNotIn("Kiro", text)
        self.assertIn(M.IDENTITY_LABEL, text)

    def test_stream_chat_no_finish_reason(self):
        text = self._chat_stream(["我是 Kiro，", "一个 AI 编程助手"], finish=False)
        self.assertNotIn("Kiro", text)

    def test_stream_anthropic_split(self):
        def sse(o, n):
            return f"event: {n}\ndata: {json.dumps(o, ensure_ascii=False)}\n\n".encode()
        evs = [sse({"type": "message_start", "message": {}}, "message_start"),
               sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}, "content_block_start")]
        for p in ["Hi! I'm Ki", "ro, your AI dev ", "environment."]:
            evs.append(sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": p}}, "content_block_delta"))
        evs += [sse({"type": "content_block_stop", "index": 0}, "content_block_stop"),
                sse({"type": "message_stop"}, "message_stop")]
        out = b"".join(run(_collect(M.sec_redact.async_post_call_streaming_iterator_hook(AGENT, _aiter(evs), {}))))
        text = ""
        for ev in out.split(b"\n\n"):
            for line in ev.split(b"\n"):
                if line.startswith(b"data:"):
                    o = json.loads(line[5:])
                    if o.get("type") == "content_block_delta":
                        text += o["delta"]["text"]
        self.assertEqual(text, "Hi! I'm an AI coding assistant, your AI dev environment.")

    def test_stream_responses_and_done_copies(self):
        evs = [{"type": "response.output_text.delta", "item_id": "m1", "content_index": 0, "delta": "我是 Kiro，"},
               {"type": "response.output_text.delta", "item_id": "m1", "content_index": 0, "delta": "一个开发助手"},
               {"type": "response.output_text.done", "item_id": "m1", "content_index": 0, "text": "我是 Kiro，一个开发助手"},
               {"type": "response.completed", "response": {"output": [{"content": [{"type": "output_text", "text": "我是 Kiro，一个开发助手"}]}]}}]
        out = run(_collect(M.sec_redact.async_post_call_streaming_iterator_hook(AGENT, _aiter(evs), {})))
        self.assertNotIn("我是 Kiro", json.dumps(out, ensure_ascii=False), "delta 和 done/completed 里的整段副本都要换")

    def test_non_stream_anthropic_dict(self):
        resp = {"type": "message", "content": [{"type": "text", "text": "我是 Kiro，一个 AI 开发助手。"}]}
        run(M.sec_redact.async_post_call_success_hook({}, AGENT, resp))
        self.assertNotIn("Kiro", resp["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
