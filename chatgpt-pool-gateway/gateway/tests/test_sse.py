from app.sse import ResponseAggregator, SSEBuffer, aggregate


def test_buffer_emits_complete_events():
    buf = SSEBuffer()
    out = buf.feed(b"event: response.created\ndata: {\"a\":1}\n\nevent: response.in_progress\ndata: {}\n\n")
    assert [e.event for e in out] == ["response.created", "response.in_progress"]
    assert out[0].json() == {"a": 1}


def test_buffer_holds_partial_chunk():
    buf = SSEBuffer()
    out1 = buf.feed("event: response.output_text.delta\ndata: {\"delta\":\"chu")
    assert out1 == []
    out2 = buf.feed("nk1\"}\n\n")
    assert len(out2) == 1
    assert out2[0].json() == {"delta": "chunk1"}


def test_buffer_handles_multibyte_split_in_data():
    # 中文跨 chunk 切分，decode errors=replace 兜底但 JSON 必须能整体解析
    chinese = "你好世界"
    raw = f'event: response.output_text.delta\ndata: {{"delta":"{chinese}"}}\n\n'.encode("utf-8")
    buf = SSEBuffer()
    out = buf.feed(raw[:10])
    out += buf.feed(raw[10:])
    assert len(out) == 1
    assert out[0].json() == {"delta": chinese}


def test_buffer_flush_drains_no_trailing_newline():
    buf = SSEBuffer()
    buf.feed("event: response.completed\ndata: {\"x\":1}")
    out = buf.flush()
    assert len(out) == 1
    assert out[0].event == "response.completed"


def test_aggregator_collects_output_item_done_not_completed_output():
    """response.completed 顶层 output 可能 null（openai-python#3312）；
    所以 output 累积必须来自 response.output_item.done。"""
    from app.sse import SSEEvent
    events = [
        SSEEvent(event="response.created", data='{"response":{"id":"r1","status":"in_progress"}}'),
        SSEEvent(event="response.output_item.done",
                 data='{"item":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"hello"}]}}'),
        # 注意 response.completed 故意没带 output 字段，模拟 bug
        SSEEvent(event="response.completed",
                 data='{"response":{"id":"r1","status":"completed","usage":{"input_tokens":3,"output_tokens":1,"total_tokens":4}}}'),
    ]
    agg = aggregate(events)
    assert agg.completed is True
    assert agg.status == "completed"
    assert agg.response_id == "r1"
    assert agg.usage == {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4}
    assert len(agg.items) == 1
    assert agg.items[0]["content"][0]["text"] == "hello"


def test_buffer_skips_comment_lines():
    buf = SSEBuffer()
    out = buf.feed(": heartbeat\nevent: response.created\ndata: {}\n\n")
    assert len(out) == 1
    assert out[0].event == "response.created"
