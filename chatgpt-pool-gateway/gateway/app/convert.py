"""chat/completions <-> responses schema 转换。

设计原则：
- gateway 对 LiteLLM 只暴露 /v1/chat/completions（litellm chatgpt provider /v1/responses 仍有
  顶层 output null bug，project_litellm_chatgpt_responses_empty_output_bug 在跟）。
- gateway 内部调上游 /v1/responses（chatgpt.com 唯一受支持的路径）。
- 转换器纯函数，golden test 覆盖。

仅实现 MVP 必需字段；非常见字段（logprobs/seed/temperature）直接透传。
"""
from __future__ import annotations

import time
import uuid
from typing import Any


def chat_to_responses(body: dict[str, Any]) -> dict[str, Any]:
    """chat/completions request → /v1/responses request。"""
    messages = body.get("messages") or []
    out: dict[str, Any] = {
        "model": body.get("model"),
        "input": _messages_to_input(messages),
        "stream": bool(body.get("stream")),
    }
    # MVP：只搬几个明确字段，其余依赖 model defaults
    for k in ("max_output_tokens", "temperature", "top_p", "metadata"):
        if k in body and body[k] is not None:
            out[k] = body[k]
    # chat/completions 的 max_tokens -> responses 的 max_output_tokens
    if "max_tokens" in body and "max_output_tokens" not in out:
        out["max_output_tokens"] = body["max_tokens"]
    # tools 不展开（MVP 不支持函数调用），直接放弃；后续再做
    return out


def _messages_to_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI chat messages -> responses input items。仅处理 text 内容。"""
    items: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts: list[str] = []
            for c in content:
                if isinstance(c, dict) and c.get("type") in ("text", "input_text"):
                    parts.append(c.get("text", ""))
            text = "".join(parts)
        else:
            text = ""
        items.append({
            "type": "message",
            "role": role,
            "content": [{"type": "input_text", "text": text}],
        })
    return items


def responses_completed_to_chat(
    response_id: str | None,
    model: str,
    items: list[dict[str, Any]],
    usage: dict[str, Any] | None,
) -> dict[str, Any]:
    """聚合完成的 /v1/responses 输出 → chat/completions response。"""
    text = _items_to_text(items)
    completion_tokens = (usage or {}).get("output_tokens", 0) or 0
    prompt_tokens = (usage or {}).get("input_tokens", 0) or 0
    total = (usage or {}).get("total_tokens", prompt_tokens + completion_tokens)
    return {
        "id": response_id or f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total,
        },
    }


def _items_to_text(items: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in items:
        if item.get("type") != "message":
            continue
        for c in item.get("content") or []:
            if c.get("type") in ("output_text", "text"):
                parts.append(c.get("text", ""))
    return "".join(parts)


def delta_event_to_chat_chunk(delta_text: str, model: str, response_id: str | None) -> dict[str, Any]:
    """SSE 流转发：单个 output_text.delta -> chat.completion.chunk。"""
    return {
        "id": response_id or f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {"content": delta_text},
            "finish_reason": None,
        }],
    }


def finish_chat_chunk(model: str, response_id: str | None) -> dict[str, Any]:
    return {
        "id": response_id or f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
