from app.convert import (
    chat_to_responses,
    delta_event_to_chat_chunk,
    finish_chat_chunk,
    responses_completed_to_chat,
)


def test_chat_to_responses_simple_text():
    body = {
        "model": "chatgpt-pool",
        "messages": [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hi"},
        ],
        "stream": True,
        "max_tokens": 128,
        "temperature": 0.7,
    }
    out = chat_to_responses(body)
    assert out["model"] == "chatgpt-pool"
    assert out["stream"] is True
    assert out["max_output_tokens"] == 128
    assert out["temperature"] == 0.7
    assert out["input"] == [
        {"type": "message", "role": "system",
         "content": [{"type": "input_text", "text": "you are helpful"}]},
        {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "hi"}]},
    ]


def test_chat_to_responses_handles_part_array_content():
    body = {
        "model": "x",
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "part1 "},
                {"type": "input_text", "text": "part2"},
            ]}
        ],
    }
    out = chat_to_responses(body)
    assert out["input"][0]["content"][0]["text"] == "part1 part2"


def test_chat_to_responses_prefers_max_output_tokens():
    body = {"model": "x", "messages": [], "max_tokens": 100, "max_output_tokens": 200}
    out = chat_to_responses(body)
    assert out["max_output_tokens"] == 200  # 显式 max_output_tokens 不被 max_tokens 覆盖


def test_responses_completed_to_chat_assembles_text():
    items = [
        {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "Hello "}]},
        {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "world"}]},
    ]
    usage = {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7}
    out = responses_completed_to_chat("resp_1", "chatgpt-pool", items, usage)
    assert out["choices"][0]["message"]["content"] == "Hello world"
    assert out["usage"]["prompt_tokens"] == 5
    assert out["usage"]["completion_tokens"] == 2
    assert out["usage"]["total_tokens"] == 7
    assert out["id"] == "resp_1"
    assert out["choices"][0]["finish_reason"] == "stop"


def test_responses_completed_handles_missing_usage():
    out = responses_completed_to_chat(None, "m", [], None)
    assert out["usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    assert out["id"].startswith("chatcmpl-")


def test_delta_chunk_shape():
    chunk = delta_event_to_chat_chunk("hello", "m", "resp_1")
    assert chunk["object"] == "chat.completion.chunk"
    assert chunk["choices"][0]["delta"]["content"] == "hello"
    assert chunk["choices"][0]["finish_reason"] is None


def test_finish_chunk_shape():
    chunk = finish_chat_chunk("m", "resp_1")
    assert chunk["choices"][0]["finish_reason"] == "stop"
    assert chunk["choices"][0]["delta"] == {}
