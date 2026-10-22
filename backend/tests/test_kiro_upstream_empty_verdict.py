"""门禁：kiro 空回复判据不许退回到「缺 meteringEvent 即空」，零文本工具轮不许判空。

10-22 实测：meteringEvent 只在请求带 origin 时出现 ⇒ 不带 origin 的正常回复全都没有它；
照社区（Kiro-Go #159）那样判会把正常回复打成空 ⇒ 重试换号 ⇒ IdC 号被全打死。
正确判据 = 0 字节，或 有 contextUsageEvent 且 assistantResponseEvent==0 且 toolUseEvent==0。
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import struct
import zlib
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name, fname):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, "scripts", fname))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


up = _load("kiro_upstream_probe", "kiro-upstream-probe.py")


def _frame(event_type: str, payload: dict) -> bytes:
    hdrs = b""
    for k, v in ((":event-type", event_type), (":content-type", "application/json"),
                 (":message-type", "event")):
        kb, vb = k.encode(), v.encode()
        hdrs += bytes([len(kb)]) + kb + b"\x07" + struct.pack(">H", len(vb)) + vb
    body = json.dumps(payload).encode()
    total = 12 + len(hdrs) + len(body) + 4
    prelude = struct.pack(">II", total, len(hdrs))
    prelude += struct.pack(">I", zlib.crc32(prelude))
    msg = prelude + hdrs + body
    return msg + struct.pack(">I", zlib.crc32(msg))


def _v(*frames):
    raw = b"".join(_frame(t, p) for t, p in frames)
    return up.verdict(raw, up.parse_frames(raw))


CTX = ("contextUsageEvent", {"contextUsagePercentage": 0.67})
MET = ("meteringEvent", {"usage": 0.1})
TXT = ("assistantResponseEvent", {"content": "是"})
TOOL = ("toolUseEvent", {"name": "get_time", "toolUseId": "t1", "input": ""})


def test_silent_empty_only_context_usage_is_empty():
    assert _v(CTX)["empty"] is True


def test_zero_bytes_is_empty():
    assert up.verdict(b"", [])["empty"] is True


def test_text_without_metering_is_not_empty():
    """不带 origin 的正常回复：只有文本帧，没有 metering —— 旧尺子在这里误判。"""
    v = _v(TXT, TXT)
    assert v["empty"] is False and v["metering"] is False


def test_tool_turn_with_zero_text_is_not_empty():
    v = _v(TOOL, TOOL, CTX, MET)
    assert v["empty"] is False and v["n_text"] == 0 and v["n_tool"] == 2


def test_normal_with_origin_is_not_empty():
    assert _v(TXT, CTX, MET)["empty"] is False


def test_parser_reads_text():
    assert _v(TXT, ("assistantResponseEvent", {"content": "好"}))["text"] == "是好"


def test_kiro_pool_summary_counts(monkeypatch):
    kp = _load("kiro_pool_for_summary", "kiro-pool.py")
    disk = [
        {"id": 1, "authMethod": "idc", "subscriptionTitle": "KIRO STUDENT"},
        {"id": 2, "authMethod": "idc", "subscriptionTitle": "KIRO STUDENT", "disabled": True},
        {"id": 3, "authMethod": "social", "subscriptionTitle": "KIRO PRO+", "disabled": True,
         "profileArn": "arn:x"},
    ]
    monkeypatch.setattr(kp, "disk_pool", lambda: disk)
    buf = io.StringIO()
    with redirect_stdout(buf):
        kp.summary()
    out = buf.getvalue()
    assert "盘上 3 个号" in out
    assert "启用 1：idc/KIRO STUDENT×1" in out
    assert "有ARN" in out and "无ARN" in out
