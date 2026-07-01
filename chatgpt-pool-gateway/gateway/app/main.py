"""FastAPI app entry。

主入口路径:
  POST /v1/chat/completions   (LiteLLM 调这里; 内部转 chat→responses 再调上游)
  GET  /health/readiness      (K8s readinessProbe)
  GET  /health/liveness       (K8s livenessProbe)
  GET  /metrics               (Prometheus scrape)
  /admin/*                    (admin.py 挂载)

请求路径决策树 (chat completions):
  1. auth header 校验 (Bearer == INTERNAL_API_KEY)
  2. 读 body + headers → extract conv_id
  3. affinity.get(conv_id) → 命中 acct 走 sticky; miss 走 picker
  4. picker 没 routable → 503 (LiteLLM 应配 fallback)
  5. compaction_drop apply 到 input items
  6. chat_to_responses → POST 上游 /backend-api/codex/responses (curl_cffi SSE)
  7. 流式: 透传 delta_event → chat.completion.chunk; agg.completed 后写 finish chunk
  8. 非流式: 聚合 → responses_completed_to_chat
  9. affinity.set(conv_id, acct) (成功 first byte 后)
  10. finally: 必须 await response.aclose()

fail-fast at connect:
  连接前/HTTP 头未到前出 5xx → 不更新 affinity, FIRST_BYTE_5XX 计数, 让 LiteLLM 走 fallback。
  连接后 (已开始 stream) 出 5xx → 不重试不换 acct, 把 error 透传给 LiteLLM。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from . import admin as admin_module
from .affinity import AffinityMap, extract_conv_id
from .compaction_drop import apply_to_responses_body
from .config import (
    AUTH_FILES_DIR,
    GATEWAY_LISTEN_HOST,
    GATEWAY_LISTEN_PORT,
    INTERNAL_API_KEY,
    REGISTRY_DB_PATH,
    UPSTREAM_BASE,
    UPSTREAM_RESPONSES_PATH,
)
from .convert import (
    chat_to_responses,
    delta_event_to_chat_chunk,
    finish_chat_chunk,
    responses_completed_to_chat,
)
from .metrics import (
    AFFINITY,
    COMPACTION_DROPS,
    DURATION,
    FIRST_BYTE_5XX,
    PICKER,
    REQUESTS,
)
from .picker import pick
from .refresh import load_bundle, refresh_if_needed
from .registry import Registry
from .sse import ResponseAggregator, SSEBuffer
from .upstream import cf_impersonating_session, make_httpx_internal_client

log = logging.getLogger("gateway.main")

CLIENT_ID = os.environ.get("OPENAI_CLIENT_ID", "app_EMoamEEZ73f0CkXaXp7hrann")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("gateway lifespan start: db=%s auth_dir=%s upstream=%s",
             REGISTRY_DB_PATH, AUTH_FILES_DIR, UPSTREAM_BASE)
    reg = Registry(REGISTRY_DB_PATH)
    app.state.registry = reg
    app.state.affinity = AffinityMap()
    app.state.httpx_internal = make_httpx_internal_client()
    app.state.cf_session_factory = cf_impersonating_session
    # 后台 probe loop
    from .probe import probe_loop
    app.state.probe_task = asyncio.create_task(
        probe_loop(reg, cf_impersonating_session)
    )
    try:
        yield
    finally:
        app.state.probe_task.cancel()
        try:
            await app.state.probe_task
        except (asyncio.CancelledError, Exception):
            pass
        await app.state.httpx_internal.aclose()


app = FastAPI(title="chatgpt-pool-gateway", lifespan=lifespan)
app.include_router(admin_module.router)


def _check_internal_auth(authorization: str | None) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer")
    if authorization[7:] != INTERNAL_API_KEY:
        raise HTTPException(403, "bad token")


@app.get("/health/liveness")
async def liveness() -> dict[str, Any]:
    return {"ok": True, "ts": time.time()}


@app.get("/health/readiness")
async def readiness(request: Request) -> dict[str, Any]:
    reg: Registry = request.app.state.registry
    accts = list(reg.all())
    healthy = sum(1 for a in accts if a.state.value == "healthy")
    return {"ok": healthy > 0, "total": len(accts), "healthy": healthy}


@app.get("/metrics")
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/v1/models")
async def list_models(authorization: str | None = Header(None)) -> dict[str, Any]:
    _check_internal_auth(authorization)
    return {
        "object": "list",
        "data": [
            {"id": "chatgpt-pool", "object": "model"},
            {"id": "chatgpt-gpt-5.5", "object": "model"},
            {"id": "chatgpt-gpt-5.4", "object": "model"},
            {"id": "chatgpt-gpt-5.3-codex", "object": "model"},
        ],
    }


def _select_acct(
    request: Request, body: dict[str, Any]
) -> tuple[str | None, str, str | None]:
    """returns (acct_name, reason, conv_id)。"""
    affinity: AffinityMap = request.app.state.affinity
    reg: Registry = request.app.state.registry
    conv_id = extract_conv_id(body, dict(request.headers))
    sticky = affinity.get(conv_id)
    if sticky and reg.get(sticky) and reg.get(sticky).state.value == "healthy":
        AFFINITY.labels(result="hit").inc()
        return sticky, f"affinity:{conv_id}", conv_id
    if sticky:
        AFFINITY.labels(result="miss").inc()  # 黏的 acct 已不健康
    elif conv_id:
        AFFINITY.labels(result="miss").inc()
    result = pick(reg.all())
    if result.account is None:
        PICKER.labels(result="miss", reason=result.reason).inc()
        return None, result.reason, conv_id
    PICKER.labels(result="hit", reason=result.reason).inc()
    return result.account.name, result.reason, conv_id


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    started = time.time()
    authorization = request.headers.get("authorization") or request.headers.get("Authorization")
    _check_internal_auth(authorization)
    body = await request.json()
    if not isinstance(body, dict) or "messages" not in body:
        raise HTTPException(400, "missing messages")
    streaming = bool(body.get("stream"))
    model = body.get("model") or "chatgpt-pool"

    reg: Registry = request.app.state.registry
    affinity: AffinityMap = request.app.state.affinity

    acct_name, reason, conv_id = _select_acct(request, body)
    if acct_name is None:
        REQUESTS.labels(path="/v1/chat/completions", code="503").inc()
        DURATION.labels(path="/v1/chat/completions").observe(time.time() - started)
        return JSONResponse(status_code=503, content={
            "error": {"message": f"no_routable_account: {reason}", "type": "gateway_unavailable"}
        })

    # 给上游用 /v1/responses 形态
    upstream_body = chat_to_responses(body)
    if "input" in upstream_body and isinstance(upstream_body["input"], list):
        before = len(upstream_body["input"])
        upstream_body = apply_to_responses_body(upstream_body)
        dropped = before - len(upstream_body["input"])
        if dropped > 0:
            COMPACTION_DROPS.inc(dropped)
    upstream_body["model"] = upstream_body.get("model") or model

    bundle = load_bundle(reg, acct_name)
    headers = {
        "Authorization": f"Bearer {bundle.access_token}",
        "ChatGPT-Account-ID": bundle.account_id or "",
        "OpenAI-Beta": "responses=experimental",
        "Content-Type": "application/json",
        "originator": "codex_cli_rs",
    }
    target_url = UPSTREAM_BASE.rstrip("/") + UPSTREAM_RESPONSES_PATH

    if streaming:
        return await _stream_chat(request, acct_name, conv_id, target_url, headers, upstream_body, model, started)
    return await _non_stream_chat(request, acct_name, conv_id, target_url, headers, upstream_body, model, started)


async def _stream_chat(
    request: Request,
    acct_name: str,
    conv_id: str | None,
    url: str,
    headers: dict[str, str],
    upstream_body: dict[str, Any],
    model: str,
    started: float,
) -> Response:
    affinity: AffinityMap = request.app.state.affinity
    response_id_holder: dict[str, str | None] = {"id": None}
    first_byte_seen = False

    async def gen() -> AsyncIterator[bytes]:
        nonlocal first_byte_seen
        buf = SSEBuffer()
        agg = ResponseAggregator()
        resp = None
        try:
            async with cf_impersonating_session() as session:
                req_payload = json.dumps({**upstream_body, "stream": True}).encode()
                try:
                    resp = await session.post(
                        url, data=req_payload, headers=headers, timeout=30, stream=True,
                    )
                except Exception as e:
                    FIRST_BYTE_5XX.inc()
                    log.warning("upstream connect fail acct=%s: %r", acct_name, e)
                    err = {"error": {"message": f"upstream_connect_fail:{e!r}", "type": "gateway_upstream"}}
                    yield (f"data: {json.dumps(err)}\n\ndata: [DONE]\n\n").encode()
                    return
                status = getattr(resp, "status_code", None) or getattr(resp, "status", None)
                if status and status >= 500:
                    FIRST_BYTE_5XX.inc()
                    err = {"error": {"message": f"upstream_http_{status}", "type": "gateway_upstream"}}
                    yield (f"data: {json.dumps(err)}\n\ndata: [DONE]\n\n").encode()
                    return
                if status and status >= 400:
                    body_text = ""
                    try:
                        body_text = await resp.atext() if hasattr(resp, "atext") else resp.text
                    except Exception:
                        pass
                    err = {"error": {"message": f"upstream_http_{status}", "type": "gateway_upstream",
                                     "body": body_text[:512]}}
                    yield (f"data: {json.dumps(err)}\n\ndata: [DONE]\n\n").encode()
                    return

                first_byte_seen = True
                if conv_id:
                    affinity.set(conv_id, acct_name)

                iter_method = (getattr(resp, "aiter_content", None)
                               or getattr(resp, "iter_content", None)
                               or getattr(resp, "aiter_raw", None))
                if iter_method is None:
                    err = {"error": {"message": "no_iter_method", "type": "gateway_internal"}}
                    yield (f"data: {json.dumps(err)}\n\ndata: [DONE]\n\n").encode()
                    return
                async for chunk in iter_method(1024):
                    if not chunk:
                        continue
                    for ev in buf.feed(chunk):
                        agg.consume(ev)
                        if ev.event == "response.output_text.delta":
                            d = ev.json() or {}
                            text = d.get("delta") or ""
                            if agg.response_id and not response_id_holder["id"]:
                                response_id_holder["id"] = agg.response_id
                            chunk_msg = delta_event_to_chat_chunk(text, model, agg.response_id)
                            yield (f"data: {json.dumps(chunk_msg)}\n\n").encode()
                        elif ev.event == "response.completed":
                            chunk_msg = finish_chat_chunk(model, agg.response_id)
                            yield (f"data: {json.dumps(chunk_msg)}\n\n").encode()
                            yield b"data: [DONE]\n\n"
                            return
                for ev in buf.flush():
                    agg.consume(ev)
                if not agg.completed:
                    chunk_msg = finish_chat_chunk(model, agg.response_id)
                    yield (f"data: {json.dumps(chunk_msg)}\n\n").encode()
                    yield b"data: [DONE]\n\n"
        finally:
            if resp is not None and hasattr(resp, "aclose"):
                try:
                    await resp.aclose()
                except Exception:
                    pass
            REQUESTS.labels(
                path="/v1/chat/completions",
                code="200" if first_byte_seen else "502",
            ).inc()
            DURATION.labels(path="/v1/chat/completions").observe(time.time() - started)

    return StreamingResponse(gen(), media_type="text/event-stream")


async def _non_stream_chat(
    request: Request,
    acct_name: str,
    conv_id: str | None,
    url: str,
    headers: dict[str, str],
    upstream_body: dict[str, Any],
    model: str,
    started: float,
) -> Response:
    """非流式: 上游强制流式 + 服务端聚合 (chatgpt /v1/responses 不支持非流式)。"""
    affinity: AffinityMap = request.app.state.affinity
    buf = SSEBuffer()
    agg = ResponseAggregator()
    first_byte = False
    resp = None
    try:
        async with cf_impersonating_session() as session:
            req_payload = json.dumps({**upstream_body, "stream": True}).encode()
            try:
                resp = await session.post(
                    url, data=req_payload, headers=headers, timeout=30, stream=True,
                )
            except Exception as e:
                FIRST_BYTE_5XX.inc()
                REQUESTS.labels(path="/v1/chat/completions", code="502").inc()
                DURATION.labels(path="/v1/chat/completions").observe(time.time() - started)
                return JSONResponse(status_code=502, content={
                    "error": {"message": f"upstream_connect_fail:{e!r}", "type": "gateway_upstream"}
                })
            status = getattr(resp, "status_code", None) or getattr(resp, "status", None)
            if status and status >= 500:
                FIRST_BYTE_5XX.inc()
                REQUESTS.labels(path="/v1/chat/completions", code=str(status)).inc()
                DURATION.labels(path="/v1/chat/completions").observe(time.time() - started)
                return JSONResponse(status_code=status, content={
                    "error": {"message": f"upstream_http_{status}", "type": "gateway_upstream"}
                })
            first_byte = True
            iter_method = (getattr(resp, "aiter_content", None)
                           or getattr(resp, "iter_content", None)
                           or getattr(resp, "aiter_raw", None))
            if iter_method is None:
                REQUESTS.labels(path="/v1/chat/completions", code="500").inc()
                return JSONResponse(status_code=500, content={"error": {"message": "no_iter_method"}})
            async for chunk in iter_method(1024):
                if not chunk:
                    continue
                for ev in buf.feed(chunk):
                    agg.consume(ev)
                if agg.completed:
                    break
            for ev in buf.flush():
                agg.consume(ev)
    finally:
        if resp is not None and hasattr(resp, "aclose"):
            try:
                await resp.aclose()
            except Exception:
                pass

    if conv_id and first_byte:
        affinity.set(conv_id, acct_name)

    out = responses_completed_to_chat(agg.response_id, model, agg.items, agg.usage)
    REQUESTS.labels(path="/v1/chat/completions", code="200").inc()
    DURATION.labels(path="/v1/chat/completions").observe(time.time() - started)
    return JSONResponse(content=out)
