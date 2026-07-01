from app.compaction_drop import drop_compaction_items, apply_to_responses_body, _has_encrypted_content


def test_drop_reasoning_item_whole():
    items = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "let me think"}]},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]},
    ]
    out = drop_compaction_items(items)
    assert len(out) == 2
    assert all(i.get("type") != "reasoning" for i in out)


def test_drop_any_item_carrying_encrypted_content():
    items = [
        {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "hi", "encrypted_content": "blob"}]},
        {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "good"}]},
    ]
    out = drop_compaction_items(items)
    assert len(out) == 1
    assert out[0]["content"][0]["text"] == "good"


def test_top_level_encrypted_content_drops_item():
    items = [{"type": "message", "encrypted_content": "x", "role": "user"}]
    out = drop_compaction_items(items)
    assert out == []


def test_has_encrypted_content_recursive():
    assert _has_encrypted_content({"a": {"b": [{"encrypted_content": 1}]}}) is True
    assert _has_encrypted_content({"a": [1, 2, {"b": "c"}]}) is False


def test_apply_to_responses_body_preserves_other_fields():
    body = {
        "model": "x", "stream": True,
        "input": [
            {"type": "reasoning"},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        ],
    }
    out = apply_to_responses_body(body)
    assert out["model"] == "x"
    assert out["stream"] is True
    assert len(out["input"]) == 1
    # 原 body 不被改
    assert len(body["input"]) == 2


def test_apply_no_input_field_returns_same():
    body = {"model": "x", "messages": [{"role": "user", "content": "hi"}]}
    out = apply_to_responses_body(body)
    assert out is body
