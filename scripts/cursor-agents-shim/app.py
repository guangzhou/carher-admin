"""
Cursor Cloud Agents API -> OpenAI-compatible /v1/chat/completions shim.

Deploys on a server with US egress (188) to bypass Cursor region gate.
Receives standard OpenAI chat requests, translates them to Cursor async
agent API, streams back OpenAI-format SSE chunks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cursor-shim")

CURSOR_BASE = "https://api.cursor.com"
CURSOR_API_KEY = os.environ.get("CURSOR_API_KEY", "")
PORT = int(os.environ.get("PORT", "8901"))
AGENT_TIMEOUT = int(os.environ.get("AGENT_TIMEOUT", "300"))

NS_IDEMPOTENT = uuid.UUID("12345678-abcd-abcd-abcd-123456789abc")

app = FastAPI(title="cursor-agents-shim")

_models_cache: Optional[Dict[str, Any]] = None
_models_cache_ts: float = 0
MODELS_CACHE_TTL = 3600

MODEL_ALIASES: Dict[str, str] = {
    "cursor-opus-5": "claude-opus-5",
    "cursor-opus-4-8": "claude-opus-4-8",
    "cursor-opus-4-7": "claude-opus-4-7",
    "cursor-opus-4-6": "claude-opus-4-6",
    "cursor-sonnet-5": "claude-sonnet-5",
    "cursor-sonnet-4-6": "claude-sonnet-4-6",
    "cursor-haiku-4-5": "claude-haiku-4-5",
    "cursor-fable-5": "claude-fable-5",
    "cursor-gpt-5.6-sol": "gpt-5.6-sol",
    "cursor-gpt-5.6-terra": "gpt-5.6-terra",
    "cursor-gpt-5.6-luna": "gpt-5.6-luna",
    "cursor-gpt-5.5": "gpt-5.5",
    "cursor-gpt-5.4": "gpt-5.4",
    "cursor-gpt-5.3-codex": "gpt-5.3-codex",
    "cursor-grok-4.5": "grok-4.5",
    "cursor-composer-2.5": "composer-2.5",
    "cursor-kimi-k3": "kimi-k3",
    "cursor-glm-5.2": "glm-5.2",
    "cursor-gemini-3.1-pro": "gemini-3.1-pro",
}


def _client(api_key: str) -> httpx.AsyncClient:
    """Return an httpx.AsyncClient. Caller manages lifecycle (must call aclose)."""
    return httpx.AsyncClient(
        base_url=CURSOR_BASE,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=httpx.Timeout(AGENT_TIMEOUT, connect=30),
    )


def _resolve_key(request: Request) -> str:
    """Extract crsr_ token from Authorization header, fall back to env."""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer ") and auth[7:].startswith("crsr_"):
        return auth[7:]
    return CURSOR_API_KEY


def _resolve_model(model: str) -> str:
    """Look up MODEL_ALIASES; pass through if not found."""
    return MODEL_ALIASES.get(model, model)


def _flatten_messages(messages: List[Dict[str, Any]]) -> str:
    """Join chat messages into a single prompt string."""
    parts: List[str] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
            )
        if role == "system":
            parts.append(f"[System] {content}")
        elif role == "assistant":
            parts.append(f"[Assistant] {content}")
        else:
            parts.append(content)
    return "\n\n".join(parts)


def _make_chunk(chunk_id: str, model: str, delta: Dict[str, Any], finish_reason: Optional[str] = None) -> str:
    """Return a JSON string in OpenAI chat.completion.chunk format."""
    return json.dumps({
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": delta,
            "finish_reason": finish_reason,
        }],
    })


def _make_completion(comp_id: str, model: str, content: str) -> Dict[str, Any]:
    """Return a dict in OpenAI chat.completion format."""
    return {
        "id": comp_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


async def _create_agent(client: httpx.AsyncClient, prompt: str, model: str) -> Dict[str, Any]:
    """POST /v1/agents with idempotent agentId. Returns agent_id + run_id."""
    aid = "bc-" + str(uuid.uuid5(NS_IDEMPOTENT, f"{time.time_ns()}-{uuid.uuid4()}"))
    body: Dict[str, Any] = {"prompt": {"text": prompt}, "agentId": aid}
    if model and model != "default":
        body["model"] = {"id": model}
    resp = await client.post("/v1/agents", json=body)
    resp.raise_for_status()
    data = resp.json()
    if "agent" in data:
        return {"agent_id": data["agent"]["id"], "run_id": data["run"]["id"]}
    return {"agent_id": data["id"], "run_id": data.get("latestRunId", "")}


async def _delete_agent(client: httpx.AsyncClient, agent_id: str) -> None:
    """DELETE agent, swallow all errors."""
    try:
        await client.delete(f"/v1/agents/{agent_id}")
    except Exception:
        pass


# Strong refs to detached cleanup tasks: asyncio only keeps weak references to
# running tasks, so a fire-and-forget task can be garbage-collected mid-flight.
_bg_tasks: set = set()


async def _cleanup(client: httpx.AsyncClient, agent_id: str) -> None:
    """Delete the remote agent then close the client. Never raises."""
    try:
        await _delete_agent(client, agent_id)
        await client.aclose()
    except Exception as exc:
        log.warning("Cleanup failed for agent=%s: %s", agent_id, exc)


def _spawn_cleanup(client: httpx.AsyncClient, agent_id: str) -> None:
    """Detach cleanup onto the event loop so it survives request cancellation."""
    task = asyncio.create_task(_cleanup(client, agent_id))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def _stream_run(client: httpx.AsyncClient, agent_id: str, run_id: str, model: str) -> AsyncIterator[str]:
    """Async generator yielding OpenAI chunk JSON strings from Cursor SSE stream."""
    chunk_id = "chatcmpl-" + uuid.uuid4().hex[:12]
    yield _make_chunk(chunk_id, model, {"role": "assistant", "content": ""})

    url = f"/v1/agents/{agent_id}/runs/{run_id}/stream"
    async with client.stream("GET", url) as resp:
        resp.raise_for_status()
        event_type = ""
        data_buf = ""
        async for line in resp.aiter_lines():
            if line.startswith("event:"):
                event_type = line[6:].strip()
                continue
            if line.startswith("data:"):
                data_buf = line[5:].strip()
                try:
                    payload = json.loads(data_buf)
                except json.JSONDecodeError:
                    continue

                if event_type == "assistant":
                    text = payload.get("text", "")
                    if text:
                        yield _make_chunk(chunk_id, model, {"content": text})
                elif event_type == "thinking":
                    text = payload.get("text", "")
                    if text:
                        yield _make_chunk(chunk_id, model, {"content": "", "reasoning_content": text})
                elif event_type in ("result", "done"):
                    yield _make_chunk(chunk_id, model, {}, finish_reason="stop")
                    return
                elif event_type == "error":
                    msg = payload.get("message", "Cursor agent error")
                    yield _make_chunk(chunk_id, model, {"content": f"\n[ERROR] {msg}"}, finish_reason="stop")
                    return

                event_type = ""
                data_buf = ""

    yield _make_chunk(chunk_id, model, {}, finish_reason="stop")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/v1/models")
async def list_models(request: Request):
    """Fetch models from Cursor, cache for 1 hour, return in OpenAI format."""
    global _models_cache, _models_cache_ts
    now = time.time()
    if _models_cache and now - _models_cache_ts < MODELS_CACHE_TTL:
        return JSONResponse(_models_cache)

    api_key = _resolve_key(request)
    async with _client(api_key) as c:
        resp = await c.get("/v1/models")
        resp.raise_for_status()
        cursor_data = resp.json()

    models = []
    for item in cursor_data.get("items", []):
        mid = item.get("id", "")
        models.append({
            "id": mid,
            "object": "model",
            "created": int(now),
            "owned_by": "cursor",
        })
    result = {"object": "list", "data": models}
    _models_cache = result
    _models_cache_ts = now
    return JSONResponse(result)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """Main endpoint: translate OpenAI chat request to Cursor agent run."""
    body = await request.json()
    api_key = _resolve_key(request)
    raw_model = body.get("model", "default")
    cursor_model = _resolve_model(raw_model)
    stream = body.get("stream", False)
    messages = body.get("messages", [])
    prompt_text = _flatten_messages(messages)

    if not prompt_text:
        return JSONResponse(
            {"error": {"message": "Empty prompt", "type": "invalid_request_error"}},
            status_code=400,
        )

    client = _client(api_key)

    try:
        info = await _create_agent(client, prompt_text, cursor_model)
    except httpx.HTTPStatusError as e:
        body_text = e.response.text
        log.error("Agent creation failed: %s %s", e.response.status_code, body_text[:200])
        await client.aclose()
        return JSONResponse(
            {"error": {"message": f"Cursor API error: {body_text[:200]}", "type": "upstream_error"}},
            status_code=e.response.status_code,
        )
    except Exception as exc:
        log.error("Agent creation failed: %s", exc)
        await client.aclose()
        return JSONResponse(
            {"error": {"message": str(exc), "type": "upstream_error"}},
            status_code=502,
        )

    agent_id = info["agent_id"]
    run_id = info["run_id"]
    log.info("Created agent=%s run=%s model=%s", agent_id, run_id, cursor_model)

    if stream:
        async def event_generator():
            try:
                async for chunk in _stream_run(client, agent_id, run_id, raw_model):
                    yield {"data": chunk}
                yield {"data": "[DONE]"}
            # Fire-and-forget, NOT awaited: when the SSE response finishes or the
            # client disconnects, this generator is closed and GeneratorExit /
            # CancelledError is raised inside it. Any `await` here is cancelled
            # immediately, so the DELETE never goes out and we leak one paid
            # remote agent per streaming request. Observed in production: POST
            # /v1/agents 201 + GET .../stream 200 with no DELETE, leaving
            # orphaned agents in the account. Cleanup must outlive this context.
            finally:
                _spawn_cleanup(client, agent_id)

        return EventSourceResponse(event_generator(), media_type="text/event-stream")
    else:
        try:
            full_text = ""
            async for chunk_str in _stream_run(client, agent_id, run_id, raw_model):
                try:
                    chunk = json.loads(chunk_str)
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    full_text += delta.get("content", "")
                except (json.JSONDecodeError, IndexError):
                    pass
            return JSONResponse(_make_completion("chatcmpl-" + uuid.uuid4().hex[:12], raw_model, full_text))
        finally:
            await _delete_agent(client, agent_id)
            await client.aclose()


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
