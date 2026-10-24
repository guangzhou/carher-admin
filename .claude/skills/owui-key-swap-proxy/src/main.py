"""key-swap-proxy: OpenAI-compatible reverse proxy in front of LiteLLM.

Logic:
  1. Read X-OpenWebUI-User-Email header (forwarded by OWUI when
     ENABLE_FORWARD_USER_INFO_HEADERS=true).
  2. Extract email local-part (zhangsan@cltx.com -> zhangsan).
  3. Gate: check LiteLLM /user/info for user_id IN (cursor-X, claude-code-X).
     If neither exists -> 401 with apply_url JSON for OWUI to render.
  4. Forward to LiteLLM using master key as Authorization.
     For chat/completions endpoints inject body.user = email so spend logs
     get end_user grouping.
  5. Block Claude models (禁 Claude is a hard rule for OWUI users):
     - Filter /v1/models response (drop entries whose .id matches claude-*).
     - Reject /v1/chat/completions when body.model matches claude-*.

Layered cache for the gate check (10 min TTL).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

LITELLM_URL = os.environ["LITELLM_URL"].rstrip("/")
LITELLM_MASTER_KEY = os.environ["LITELLM_MASTER_KEY"]
APPLY_URL = os.environ.get("APPLY_URL", "https://example.com/apply")
CACHE_TTL = int(os.environ.get("CACHE_TTL", "600"))
ALLOWED_EMAIL_DOMAIN = os.environ.get("ALLOWED_EMAIL_DOMAIN", "")  # e.g. auto-link.com.cn; empty = no restriction
# ---- OWUI 只开放这一个模型（2026-09-15，全员一视同仁，含管理员）----
# 原来这里是「禁 Claude」黑名单。改成白名单：不在名单里的一个都不放。
# 环境变量可覆盖，改名单不用重新发版：ALLOWED_MODELS=a,b,c
ALLOWED_MODELS = {
    m.strip().lower()
    for m in os.environ.get("ALLOWED_MODELS", "sa-grok-4.6").split(",")
    if m.strip()
}


def _model_allowed(model: str | None) -> bool:
    return bool(model) and model.strip().lower() in ALLOWED_MODELS


def _model_blocked_response(model: str | None) -> JSONResponse:
    names = ", ".join(sorted(ALLOWED_MODELS))
    return JSONResponse(
        status_code=403,
        content={
            "error": {
                "message": (
                    f"Open WebUI 现在只提供 {names}，请在模型里选它。"
                    f"（你这次发的是 {model or '空'}）历史会话里的旧模型需要手动切换。"
                ),
                "type": "model_not_allowed",
                "code": "model_not_allowed",
            }
        },
    )
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("key-swap-proxy")

app = FastAPI(title="OWUI key-swap-proxy", version="0.1.0")

# email -> (allowed: bool, expires_at: float)
_gate_cache: dict[str, tuple[bool, float]] = {}
_cache_lock = asyncio.Lock()

# Shared httpx client (created on startup).
_client: httpx.AsyncClient | None = None


@app.on_event("startup")
async def _startup() -> None:
    global _client
    _client = httpx.AsyncClient(timeout=httpx.Timeout(connect=5, read=600, write=10, pool=5))
    log.info("startup: litellm_url=%s apply_url=%s cache_ttl=%ds", LITELLM_URL, APPLY_URL, CACHE_TTL)


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _client is not None:
        await _client.aclose()


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True, "cache_size": len(_gate_cache)}


async def _check_user_in_litellm(local_part: str) -> bool:
    """Return True if either cursor-<local> or claude-code-<local> user exists.

    A user record is auto-created by LiteLLM whenever a key is issued for it,
    so this is a reliable allowlist signal.
    """
    assert _client is not None
    headers = {"Authorization": f"Bearer {LITELLM_MASTER_KEY}"}
    for prefix in ("cursor-", "claude-code-"):
        user_id = f"{prefix}{local_part}"
        try:
            r = await _client.get(
                f"{LITELLM_URL}/user/info",
                params={"user_id": user_id},
                headers=headers,
                timeout=5,
            )
        except httpx.HTTPError as exc:
            log.warning("gate check upstream error user=%s: %s", user_id, exc)
            continue
        if r.status_code == 200:
            data = r.json()
            keys = data.get("keys") or []
            if keys:
                return True
    return False


async def _gate(email: str) -> bool:
    now = time.time()
    async with _cache_lock:
        entry = _gate_cache.get(email)
    if entry and entry[1] > now:
        return entry[0]
    local = email.split("@", 1)[0].lower()
    allowed = await _check_user_in_litellm(local)
    async with _cache_lock:
        _gate_cache[email] = (allowed, now + CACHE_TTL)
    return allowed


def _no_key_response(email: str) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={
            "error": {
                "message": (
                    f"你 ({email}) 还没在 LiteLLM 申请过 key, 无法使用 Open WebUI. "
                    f"请去 {APPLY_URL} 申请 cursor 或 claude-code 账号, 通过后回这里即可."
                ),
                "type": "no_litellm_key",
                "code": "no_litellm_key",
                "apply_url": APPLY_URL,
            }
        },
    )




async def _forward_models(request: Request, email: str) -> StreamingResponse | JSONResponse:
    """GET /v1/models — fetch from LiteLLM, filter out Claude entries."""
    assert _client is not None
    headers = {"Authorization": f"Bearer {LITELLM_MASTER_KEY}"}
    r = await _client.get(f"{LITELLM_URL}/v1/models", headers=headers)
    if r.status_code != 200:
        return JSONResponse(status_code=r.status_code, content=r.json())
    body = r.json()
    if isinstance(body.get("data"), list):
        before = len(body["data"])
        body["data"] = [m for m in body["data"] if _model_allowed(m.get("id"))]
        log.debug("models filter: email=%s before=%d after=%d", email, before, len(body["data"]))
    return JSONResponse(content=body)


async def _forward_streaming(request: Request, email: str, target_path: str) -> StreamingResponse | JSONResponse:
    """Proxy chat/completions and similar endpoints, injecting body.user=email.

    For Claude models we reject up-front (禁 Claude).
    """
    assert _client is not None
    raw_body = await request.body()
    if raw_body:
        try:
            body_json = json.loads(raw_body)
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content={"error": {"message": "invalid JSON body"}})
        if not _model_allowed(body_json.get("model")):
            return _model_blocked_response(body_json.get("model"))
        body_json["user"] = email  # override / inject
        body_bytes = json.dumps(body_json).encode()
    else:
        body_bytes = b""

    headers = {
        "Authorization": f"Bearer {LITELLM_MASTER_KEY}",
        "Content-Type": request.headers.get("content-type", "application/json"),
        # 下面 aiter_raw 透传原始字节但丢了 content-encoding ⇒ 上游若 gzip, 客户端拿到裸 gzip 解不开
        # (OWUI v0.12 非流式 'utf-8' codec can't decode byte 0x8b)。要明文最省事。
        "Accept-Encoding": "identity",
    }
    upstream_url = f"{LITELLM_URL}/{target_path}"
    req = _client.build_request(
        request.method,
        upstream_url,
        headers=headers,
        content=body_bytes,
        params=request.query_params,
    )
    upstream = await _client.send(req, stream=True)

    async def _iter():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    drop = {"content-encoding", "content-length", "transfer-encoding", "connection"}
    out_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in drop}
    return StreamingResponse(
        _iter(),
        status_code=upstream.status_code,
        headers=out_headers,
        media_type=upstream.headers.get("content-type"),
    )


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"], response_model=None)
async def catch_all(path: str, request: Request) -> StreamingResponse | JSONResponse:
    if path in ("healthz", "metrics"):
        return JSONResponse(content={"ok": True})

    email = request.headers.get("x-openwebui-user-email")
    if not email:
        return JSONResponse(
            status_code=401,
            content={
                "error": {
                    "message": "missing X-OpenWebUI-User-Email header. is ENABLE_FORWARD_USER_INFO_HEADERS=true on Open WebUI?",
                    "type": "missing_user_header",
                }
            },
        )

    if ALLOWED_EMAIL_DOMAIN and not email.lower().endswith("@" + ALLOWED_EMAIL_DOMAIN):
        log.info("domain deny email=%s (not @%s)", email, ALLOWED_EMAIL_DOMAIN)
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "message": f"只允许 @{ALLOWED_EMAIL_DOMAIN} 邮箱使用此服务，你的邮箱 {email} 不在白名单内。",
                    "type": "domain_not_allowed",
                    "code": "domain_not_allowed",
                }
            },
        )

    if not await _gate(email):
        log.info("gate deny email=%s", email)
        return _no_key_response(email)

    if path.rstrip("/") in ("v1/models", "openai/v1/models", "models"):
        return await _forward_models(request, email)

    return await _forward_streaming(request, email, path)
