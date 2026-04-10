"""CarHer Admin Dashboard — FastAPI backend.

Architecture:
  DB (SQLite) → source of truth for user registry
  config_gen  → DB row → openclaw.json
  k8s_ops     → ConfigMap / Pod / PVC lifecycle
  sync_worker → background retry + consistency check
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import time
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Annotated

import jwt
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import json as _json

from kubernetes.client.rest import ApiException as K8sApiException

from . import database as db
from . import acr_client
from . import config_gen
from . import crd_ops
from . import k8s_ops
from . import metrics as metrics_mod
from . import sync_worker
from . import deployer
from . import cloudflare_ops
from . import litellm_ops
from .models import (
    HerAddRequest, HerBatchAction, HerBatchImport, HerUpdateRequest,
    DeployGroupCreate, DeployGroupUpdate, SetDeployGroupRequest,
    BatchSetDeployGroupRequest, DeployRequest, DeployWebhookRequest,
    BranchRuleCreate, BranchRuleUpdate, TriggerBuildRequest,
    AgentRequest, AgentResponse,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("carher-admin")

STATIC_DIR = Path(__file__).parent.parent / "frontend" / "dist"


_SECRET_KEYS = {"app_secret", "appSecret", "secret", "token", "password", "apiKey", "api_key"}


def _redact_secrets(d: dict, _depth: int = 0):
    """Recursively mask values whose key looks like a secret."""
    if _depth > 10:
        return
    for k, v in d.items():
        if isinstance(v, dict):
            _redact_secrets(v, _depth + 1)
        elif isinstance(v, str) and v and k in _SECRET_KEYS:
            d[k] = v[:4] + "****" if len(v) > 4 else "****"


def _k8s_error_detail(exc: K8sApiException) -> str:
    """Extract a human-readable message from a K8s ApiException."""
    try:
        body = _json.loads(exc.body) if exc.body else {}
        return body.get("message", exc.reason or str(exc))
    except Exception:
        return exc.reason or str(exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    k8s_ops.init_k8s()
    db.init_db()
    await sync_worker.start_workers()
    metrics_mod.start_sampler(db)
    logger.info("CarHer Admin started (DB + K8s + sync workers + metrics sampler)")
    yield

app = FastAPI(
    title="CarHer Admin",
    description="Enterprise management platform for 500+ CarHer (Feishu AI assistant) instances. "
                "Provides declarative lifecycle management, canary deployment, health monitoring, "
                "and an AI operations agent. All APIs return JSON.",
    version="1.0.0",
    lifespan=lifespan,
)

ALLOWED_ORIGINS = os.environ.get("CORS_ALLOW_ORIGINS", "https://admin.carher.net").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Authentication ──

ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
JWT_SECRET = os.environ.get("JWT_SECRET", ADMIN_API_KEY or ADMIN_PASSWORD)
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 24

AUTH_EXEMPT_PATHS = {"/api/auth/login", "/api/deploy/webhook"}


class LoginRequest(BaseModel):
    username: str
    password: str


def _create_jwt(username: str) -> str:
    if not JWT_SECRET:
        raise RuntimeError("JWT secret is not configured")
    payload = {
        "sub": username,
        "iat": int(time.time()),
        "exp": int(time.time()) + JWT_EXPIRE_HOURS * 3600,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _verify_jwt(token: str) -> dict | None:
    if not JWT_SECRET:
        return None
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


@app.post("/api/auth/login")
def api_auth_login(req: LoginRequest):
    """Authenticate with username/password. Returns JWT token (valid 24h)."""
    if not ADMIN_PASSWORD:
        raise HTTPException(503, "ADMIN_PASSWORD not configured on server")
    if not JWT_SECRET:
        raise HTTPException(503, "JWT secret is not configured on server")
    if req.username != ADMIN_USERNAME or not hmac.compare_digest(req.password, ADMIN_PASSWORD):
        raise HTTPException(401, "Invalid username or password")
    token = _create_jwt(req.username)
    return {"token": token, "expires_in": JWT_EXPIRE_HOURS * 3600, "username": req.username}


@app.get("/api/auth/me")
def api_auth_me(request: Request):
    """Check current auth status. Returns user info if authenticated."""
    claims = _extract_auth(request)
    if not claims:
        raise HTTPException(401, "Not authenticated")
    return {"username": claims.get("sub", ""), "authenticated": True}


def _extract_auth(request: Request) -> dict | None:
    """Extract authentication from JWT Bearer token or X-API-Key header."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        claims = _verify_jwt(token)
        if claims:
            return claims

    api_key = request.headers.get("X-API-Key", "") or request.query_params.get("api_key", "")
    if api_key and ADMIN_API_KEY and hmac.compare_digest(api_key, ADMIN_API_KEY):
        return {"sub": "api-key", "api_key": True}

    return None


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Protect all /api/* endpoints except login and webhook. Static files pass through."""
    path = request.url.path

    if not path.startswith("/api/"):
        return await call_next(request)
    if path in AUTH_EXEMPT_PATHS:
        return await call_next(request)
    if request.method == "OPTIONS":
        return await call_next(request)

    if not ADMIN_PASSWORD and not ADMIN_API_KEY:
        return JSONResponse(status_code=503, content={"detail": "Admin authentication is not configured"})

    claims = _extract_auth(request)
    if not claims:
        return JSONResponse(status_code=401, content={"detail": "Authentication required"})

    return await call_next(request)


async def verify_api_key(request: Request):
    """Legacy dependency: now handled by auth_middleware. Kept for endpoint-level Depends()."""
    pass


# ── knownBots cache ──

_known_bots_cache: dict | None = None
_known_bots_ts: float = 0
KNOWN_BOTS_TTL = 30  # seconds


def _get_known_bots_cached():
    """Cache knownBots for 30s to avoid O(N) DB query per request."""
    global _known_bots_cache, _known_bots_ts
    now = time.monotonic()
    if _known_bots_cache is None or now - _known_bots_ts > KNOWN_BOTS_TTL:
        bots, bot_open_ids = db.collect_known_bots()
        try:
            for inst in crd_ops.list_her_instances():
                spec = inst.get("spec", {})
                app_id = spec.get("appId", "")
                name = spec.get("name", "")
                if app_id and name:
                    bots[app_id] = name
                boi = spec.get("botOpenId", "")
                if boi and app_id:
                    bot_open_ids[boi] = app_id
        except Exception:
            pass
        _known_bots_cache = (bots, bot_open_ids)
        _known_bots_ts = now
    return _known_bots_cache


# ── Helpers ──

def _model_short(full: str) -> str:
    parts = full.rsplit("/", 1)
    return parts[-1] if parts else full


def _model_map_for_provider(provider: str) -> dict[str, str]:
    if provider == "litellm":
        return config_gen.MODEL_MAP_LITELLM
    if provider == "wangsu":
        return config_gen.MODEL_MAP_WANGSU
    if provider == "anthropic":
        return config_gen.MODEL_MAP_ANTHROPIC
    return config_gen.MODEL_MAP


from .crd_helpers import db_instances_excluding_crds as _db_instances_excluding_crds


def _enrich_with_runtime(instance: dict) -> dict:
    """Merge DB data with live K8s pod status."""
    uid = instance["id"]
    pod = k8s_ops.get_pod_status(uid)

    prefix = instance.get("prefix", "s1")
    pfx = f"{prefix}-" if not prefix.endswith("-") else prefix
    model_map = _model_map_for_provider(instance.get("provider", "wangsu"))
    model_full = model_map.get(instance.get("model", "gpt"), instance.get("model", "gpt"))

    return {
        "id": uid,
        "name": instance.get("name", ""),
        "model": model_full,
        "model_short": instance.get("model", ""),
        "status": pod["phase"] if pod.get("pod_exists") else ("Stopped" if instance.get("status") != "deleted" else "Deleted"),
        "pod_ip": pod.get("pod_ip", ""),
        "node": pod.get("node", ""),
        "age": pod.get("age", ""),
        "restarts": pod.get("restarts", 0),
        "app_id": instance.get("app_id", ""),
        "oauth_url": f"https://{pfx}u{uid}-auth.carher.net/feishu/oauth/callback" if instance.get("app_id") else "",
        "owner": instance.get("owner", ""),
        "provider": instance.get("provider", "wangsu"),
        "sync_status": instance.get("sync_status", ""),
    }


def _sync_and_deploy(instance: dict):
    """Generate config → write ConfigMap → create Pod."""
    uid = instance["id"]
    config_json = config_gen.generate_json_string(instance)
    k8s_ops.apply_configmap(uid, config_json)
    db.set_sync_status(uid, "synced")


def _update_legacy_cloudflare(uid: int, prefix: str):
    try:
        svc_name = f"carher-{uid}-svc"
        cloudflare_ops.sync_tunnel_config(wait_for_service=svc_name)
        cloudflare_ops.register_dns_routes(uid, prefix=prefix)
        cloudflare_ops.update_remote_ingress([(uid, prefix)])
    except Exception as cf_err:
        logger.warning("Cloudflare auto-config failed for legacy instance %d (non-fatal): %s", uid, cf_err)


# ── API: List / Get ──

@app.get("/api/instances")
def api_list_instances(
    offset: int = Query(0, ge=0, description="Pagination offset"),
    limit: int = Query(0, ge=0, le=5000, description="Max items (0 = all)"),
):
    results = []
    seen_uids: set[int] = set()

    # CRD-managed instances (primary source of truth when operator is running)
    try:
        for inst in crd_ops.list_her_instances():
            spec = inst.get("spec", {})
            status = inst.get("status", {})
            uid = spec.get("userId", 0)
            if not uid:
                continue
            seen_uids.add(uid)
            prefix = spec.get("prefix", "s1")
            pfx = f"{prefix}-" if not prefix.endswith("-") else prefix
            phase = status.get("phase", "Unknown")
            if spec.get("paused"):
                phase = "Paused"
            results.append({
                "id": uid,
                "name": spec.get("name", ""),
                "model": spec.get("model", ""),
                "model_short": spec.get("model", ""),
                "status": phase,
                "pod_ip": status.get("podIP", ""),
                "node": status.get("node", ""),
                "age": "",
                "restarts": status.get("restarts", 0),
                "app_id": spec.get("appId", ""),
                "oauth_url": f"https://{pfx}u{uid}-auth.carher.net/feishu/oauth/callback" if spec.get("appId") else "",
                "owner": spec.get("owner", ""),
                "provider": spec.get("provider", "wangsu"),
                "image": spec.get("image", ""),
                "paused": spec.get("paused", False),
                "feishu_ws": status.get("feishuWS", "Unknown"),
                "sync_status": "operator",
                "deploy_group": spec.get("deployGroup", "stable"),
                "managed_by": "operator",
            })
    except Exception as e:
        logger.warning("CRD list failed, falling back to DB-only: %s", e)

    # DB-managed instances (legacy, not yet migrated to CRD)
    instances = _db_instances_excluding_crds()
    pod_statuses = k8s_ops.get_all_pod_statuses()

    for inst in instances:
        if inst["status"] == "deleted":
            continue
        uid = inst["id"]
        if uid in seen_uids:
            continue
        pod = pod_statuses.get(uid, {})

        prefix = inst.get("prefix", "s1")
        pfx = f"{prefix}-" if not prefix.endswith("-") else prefix
        mm = _model_map_for_provider(inst.get("provider", "wangsu"))
        model_full = mm.get(inst.get("model", "gpt"), inst.get("model", "gpt"))

        results.append({
            "id": uid,
            "name": inst.get("name", ""),
            "model": model_full,
            "model_short": inst.get("model", ""),
            "status": pod.get("phase", "Stopped") if pod.get("pod_exists") else "Stopped",
            "pod_ip": pod.get("pod_ip", ""),
            "node": pod.get("node", ""),
            "age": pod.get("age", ""),
            "restarts": pod.get("restarts", 0),
            "app_id": inst.get("app_id", ""),
            "oauth_url": f"https://{pfx}u{uid}-auth.carher.net/feishu/oauth/callback" if inst.get("app_id") else "",
            "owner": inst.get("owner", ""),
            "sync_status": inst.get("sync_status", ""),
            "deploy_group": inst.get("deploy_group", "stable"),
        })

    results.sort(key=lambda x: x["id"])
    total = len(results)
    if limit > 0:
        results = results[offset:offset + limit]
    return {"total": total, "offset": offset, "limit": limit, "instances": results}


# ── API: Search / Filter (must be before {uid} route to avoid path conflict) ──

@app.get("/api/instances/search", tags=["instances"])
def api_search_instances(
    status: str | None = Query(None, description="Filter: Running/Stopped/Failed/Paused"),
    model: str | None = Query(None, description="Filter: gpt/sonnet/opus"),
    deploy_group: str | None = Query(None, description="Filter: group name"),
    owner: str | None = Query(None, description="Filter: owner contains open_id"),
    name: str | None = Query(None, description="Filter: name contains text"),
    feishu_ws: str | None = Query(None, description="Filter: Connected/Disconnected"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    limit: int = Query(200, ge=1, le=5000, description="Max results"),
):
    """Search instances with flexible filters. All filters are AND-combined.
    Searches both CRD-managed and DB-managed instances."""
    results = []
    seen_uids: set[int] = set()

    try:
        for inst in crd_ops.list_her_instances():
            spec = inst.get("spec", {})
            st = inst.get("status", {})
            uid = spec.get("userId", 0)
            if not uid:
                continue
            seen_uids.add(uid)
            phase = st.get("phase", "Unknown")
            if spec.get("paused"):
                phase = "Paused"

            if status and phase.lower() != status.lower():
                continue
            if model and spec.get("model", "") != model:
                continue
            if deploy_group and spec.get("deployGroup", "stable") != deploy_group:
                continue
            if owner and owner not in spec.get("owner", ""):
                continue
            if name and name.lower() not in spec.get("name", "").lower():
                continue
            if feishu_ws:
                ws = st.get("feishuWS", "Unknown")
                if feishu_ws.lower() != ws.lower():
                    continue

            results.append({
                "id": uid, "name": spec.get("name", ""),
                "model": spec.get("model", ""), "model_short": spec.get("model", ""),
                "status": phase, "pod_ip": st.get("podIP", ""),
                "node": st.get("node", ""), "restarts": st.get("restarts", 0),
                "deploy_group": spec.get("deployGroup", "stable"),
                "owner": spec.get("owner", ""), "provider": spec.get("provider", "wangsu"),
                "app_id": spec.get("appId", ""), "image": spec.get("image", ""),
                "paused": spec.get("paused", False),
                "feishu_ws": st.get("feishuWS", "Unknown"), "managed_by": "operator",
            })
    except Exception as e:
        logger.warning("CRD search failed: %s", e)

    instances = _db_instances_excluding_crds()
    pod_statuses = k8s_ops.get_all_pod_statuses()
    for inst in instances:
        if inst["status"] == "deleted":
            continue
        uid = inst["id"]
        if uid in seen_uids:
            continue
        pod = pod_statuses.get(uid, {})
        phase = pod.get("phase", "Stopped") if pod.get("pod_exists") else "Stopped"
        if status and phase.lower() != status.lower():
            continue
        if model and inst.get("model", "") != model:
            continue
        if deploy_group and inst.get("deploy_group", "stable") != deploy_group:
            continue
        if owner and owner not in inst.get("owner", ""):
            continue
        if name and name.lower() not in inst.get("name", "").lower():
            continue

        mm = _model_map_for_provider(inst.get("provider", "wangsu"))
        model_full = mm.get(inst.get("model", "gpt"), inst.get("model", "gpt"))
        entry = {
            "id": uid, "name": inst.get("name", ""),
            "model": model_full, "model_short": inst.get("model", ""),
            "status": phase, "pod_ip": pod.get("pod_ip", ""),
            "node": pod.get("node", ""), "restarts": pod.get("restarts", 0),
            "deploy_group": inst.get("deploy_group", "stable"),
            "owner": inst.get("owner", ""), "provider": inst.get("provider", "wangsu"),
            "image": inst.get("image_tag", ""),
        }
        if feishu_ws:
            health = k8s_ops.check_pod_health(uid) if pod.get("pod_exists") else {}
            ws = "Connected" if health.get("feishu_ws") else "Disconnected"
            if ws.lower() != feishu_ws.lower():
                continue
            entry["feishu_ws"] = ws
        results.append(entry)

    total = len(results)
    results = results[offset:offset + limit]
    return {"total": total, "offset": offset, "limit": limit, "instances": results}


@app.get("/api/instances/{uid}")
def api_get_instance(uid: int):
    # Try CRD first for operator-managed instances
    try:
        crd = crd_ops.get_her_instance(uid)
        if crd:
            spec = crd.get("spec", {})
            status = crd.get("status", {})
            prefix = spec.get("prefix", "s1")
            pfx = f"{prefix}-" if not prefix.endswith("-") else prefix
            model_short = spec.get("model", "gpt")
            return {
                "id": uid,
                "name": spec.get("name", ""),
                "model": model_short, "model_short": model_short,
                "status": status.get("phase", "Unknown"),
                "pod_ip": status.get("podIP", ""),
                "node": status.get("node", ""),
                "restarts": status.get("restarts", 0),
                "app_id": spec.get("appId", ""),
                "bot_open_id": spec.get("botOpenId", ""),
                "oauth_url": f"https://{pfx}u{uid}-auth.carher.net/feishu/oauth/callback",
                "owner": spec.get("owner", ""),
                "provider": spec.get("provider", "wangsu"),
                "deploy_group": spec.get("deployGroup", "stable"),
                "feishu_ws": status.get("feishuWS", "Unknown"),
                "config_hash": status.get("configHash", ""),
                "image": spec.get("image", ""),
                "paused": spec.get("paused", False),
                "managed_by": "operator",
                "pvc_status": k8s_ops.get_pvc_status(uid),
                "last_health_check": status.get("lastHealthCheck", ""),
                "message": status.get("message", ""),
            }
    except K8sApiException as e:
        raise HTTPException(e.status or 500, detail=_k8s_error_detail(e))

    inst = db.get_by_id(uid)
    if not inst:
        raise HTTPException(404, f"Instance {uid} not found")
    result = _enrich_with_runtime(inst)
    result["pvc_status"] = k8s_ops.get_pvc_status(uid)
    bots, _ = _get_known_bots_cached()
    result["known_bots_count"] = len(bots)
    return result


# ── CRD helper ──

def _has_crd(uid: int) -> bool:
    """Check if this instance is managed by a HerInstance CRD."""
    return crd_ops.get_her_instance(uid) is not None


def _should_fallback_to_legacy(exc: Exception) -> bool:
    """Only fall back to legacy when the CRD API is truly unavailable."""
    return isinstance(exc, K8sApiException) and exc.status == 404


# ── API: Create ──

@app.post("/api/instances", dependencies=[Depends(verify_api_key)])
def api_add_instance(req: HerAddRequest):
    if req.id:
        uid = req.id
    else:
        db_next = db.next_id()
        crd_max = 0
        try:
            for inst in crd_ops.list_her_instances():
                u = inst.get("spec", {}).get("userId", 0)
                if u > crd_max:
                    crd_max = u
        except Exception:
            pass
        uid = max(db_next, crd_max + 1)

    data = {
        "id": uid, "name": req.name, "model": req.model,
        "app_id": req.app_id, "app_secret": req.app_secret,
        "prefix": req.prefix, "owner": req.owner,
        "provider": req.provider, "bot_open_id": "",
        "status": "running", "deploy_group": req.deploy_group,
    }

    pfx = f"{req.prefix}-" if not req.prefix.endswith("-") else req.prefix

    if req.provider == "litellm":
        key = litellm_ops.generate_key(uid, name=req.name)
        if not key:
            raise HTTPException(502, f"Failed to generate LiteLLM key for her-{uid}")
        data["litellm_key"] = key

    # CRD path: create HerInstance CRD → operator handles everything
    try:
        crd_ops.create_her_instance(data)
        logger.info("Created HerInstance CRD for uid=%d", uid)
        # Auto-sync Cloudflare DNS + remote tunnel ingress for the new instance
        try:
            svc_name = f"carher-{uid}-svc"
            cloudflare_ops.sync_tunnel_config(wait_for_service=svc_name)
            cloudflare_ops.register_dns_routes(uid, prefix=req.prefix)
            cloudflare_ops.update_remote_ingress([(uid, req.prefix)])
        except Exception as cf_err:
            logger.warning("Cloudflare auto-config failed for %d (non-fatal): %s", uid, cf_err)
        return {
            "id": uid, "status": "created", "managed_by": "operator",
            "oauth_url": f"https://{pfx}u{uid}-auth.carher.net/feishu/oauth/callback",
        }
    except Exception as e:
        if not _should_fallback_to_legacy(e):
            if data.get("litellm_key"):
                litellm_ops.delete_key(data["litellm_key"])
            if isinstance(e, K8sApiException):
                raise HTTPException(e.status or 500, detail=_k8s_error_detail(e))
            raise HTTPException(500, f"Failed to create HerInstance for {uid}: {e}")
        logger.warning("CRD create failed for %d, falling back to legacy: %s", uid, e)

    # Legacy fallback
    try:
        inst = db.insert(data)
    except Exception:
        if data.get("litellm_key"):
            litellm_ops.delete_key(data["litellm_key"])
        raise
    # knownBots cache invalidation removed — bots now register dynamically via Redis.
    try:
        k8s_ops.ensure_pvc(uid)
        k8s_ops.ensure_service(uid)
        _sync_and_deploy(inst)
        k8s_ops.create_pod(uid, prefix=req.prefix)
    except Exception as e:
        logger.error("K8s deploy failed for %d: %s", uid, e)
        db.set_sync_status(uid, "pending")
    _update_legacy_cloudflare(uid, req.prefix)

    return {
        "id": uid, "status": "created",
        "oauth_url": f"https://{pfx}u{uid}-auth.carher.net/feishu/oauth/callback",
    }


@app.post("/api/instances/batch-import", dependencies=[Depends(verify_api_key)])
def api_batch_import(
    req: Annotated[
        list[HerAddRequest] | HerBatchImport,
        Body(
            openapi_examples={
                "wrapped": {
                    "summary": "Preferred wrapped request body",
                    "value": {
                        "instances": [
                            {
                                "name": "用户A",
                                "model": "gpt",
                                "provider": "wangsu",
                                "app_id": "cli_xxx",
                                "app_secret": "xxx",
                                "prefix": "s1",
                                "owner": "ou_xxx",
                            }
                        ]
                    },
                },
                "legacy_raw_array": {
                    "summary": "Legacy raw array body",
                    "value": [
                        {
                            "name": "用户A",
                            "model": "gpt",
                            "provider": "wangsu",
                            "app_id": "cli_xxx",
                            "app_secret": "xxx",
                            "prefix": "s1",
                            "owner": "ou_xxx",
                        }
                    ],
                },
            }
        ),
    ],
):
    """Batch import instances.

    Preferred request body is {"instances":[...]}. A legacy raw JSON array body
    is also accepted for backward compatibility with older scripts and skills.
    """
    results = []
    crd_created: list[tuple[int, str]] = []
    next_uid = db.next_id()
    items = req.instances if isinstance(req, HerBatchImport) else req
    try:
        crd_max = 0
        for inst in crd_ops.list_her_instances():
            u = inst.get("spec", {}).get("userId", 0)
            if u > crd_max:
                crd_max = u
        next_uid = max(next_uid, crd_max + 1)
    except Exception:
        pass
    for item in items:
        uid = item.id or next_uid
        if not item.id:
            next_uid += 1
        pfx = f"{item.prefix}-" if not item.prefix.endswith("-") else item.prefix
        persisted = False
        data: dict | None = None
        try:
            data = {
                "id": uid, "name": item.name, "model": item.model,
                "app_id": item.app_id, "app_secret": item.app_secret,
                "prefix": item.prefix, "owner": item.owner,
                "provider": item.provider, "bot_open_id": "",
                "deploy_group": item.deploy_group,
                "status": "running",
            }
            if item.provider == "litellm":
                key = litellm_ops.generate_key(uid, name=item.name)
                if not key:
                    results.append({"id": uid, "error": f"Failed to generate LiteLLM key for her-{uid}"})
                    continue
                data["litellm_key"] = key
            # CRD path
            try:
                crd_ops.create_her_instance(data)
                crd_created.append((uid, item.prefix))
                results.append({
                    "id": uid, "status": "created", "managed_by": "operator",
                    "oauth_url": f"https://{pfx}u{uid}-auth.carher.net/feishu/oauth/callback",
                })
                continue
            except Exception as crd_err:
                if not _should_fallback_to_legacy(crd_err):
                    if data.get("litellm_key"):
                        litellm_ops.delete_key(data["litellm_key"])
                    if isinstance(crd_err, K8sApiException):
                        results.append({"id": uid, "error": _k8s_error_detail(crd_err)})
                    else:
                        results.append({"id": uid, "error": str(crd_err)})
                    continue
                logger.warning("CRD create failed for %d in batch, fallback: %s", uid, crd_err)

            # Legacy fallback
            inst = db.insert(data)
            persisted = True
            k8s_ops.ensure_pvc(uid)
            k8s_ops.ensure_service(uid)
            _sync_and_deploy(inst)
            k8s_ops.create_pod(uid, prefix=item.prefix)
            _update_legacy_cloudflare(uid, item.prefix)
            results.append({
                "id": uid, "status": "created",
                "oauth_url": f"https://{pfx}u{uid}-auth.carher.net/feishu/oauth/callback",
            })
        except Exception as e:
            if not persisted and data and data.get("litellm_key"):
                litellm_ops.delete_key(data["litellm_key"])
            results.append({"id": uid, "error": str(e)})

    # Batch-register Cloudflare DNS + remote tunnel ingress for all new CRD instances
    if crd_created:
        try:
            cloudflare_ops.sync_tunnel_config()
            for uid, prefix in crd_created:
                cloudflare_ops.register_dns_routes(uid, prefix=prefix)
            cloudflare_ops.update_remote_ingress(crd_created)
        except Exception as cf_err:
            logger.warning("Cloudflare batch config failed (non-fatal): %s", cf_err)

    db.flush_backup()
    return {"results": results}


# ── API: Update ──

# CRD spec field names differ from DB field names
_DB_TO_CRD_FIELD = {
    "name": "name", "model": "model", "owner": "owner",
    "provider": "provider", "prefix": "prefix", "deploy_group": "deployGroup",
    "image": "image", "app_id": "appId", "bot_open_id": "botOpenId",
}

_UPDATE_FIELDS = (
    "name", "model", "app_id", "owner", "provider",
    "prefix", "bot_open_id", "image", "deploy_group",
)

@app.put("/api/instances/{uid}", tags=["instances"])
def api_update(uid: int, req: HerUpdateRequest):
    """Update instance fields. Only non-null fields are applied.
    CRD-managed instances are patched via the K8s API; operator reconciles.
    app_secret is stored in a dedicated K8s Secret (never in CRD spec)."""
    changes = {}
    for field in _UPDATE_FIELDS:
        val = getattr(req, field, None)
        if val is not None:
            changes[field] = val

    has_secret_change = req.app_secret is not None

    if not changes and not has_secret_change:
        return {"id": uid, "action": "updated", "changes": {}}

    try:
        has_crd = _has_crd(uid)
    except K8sApiException as e:
        raise HTTPException(e.status or 500, detail=_k8s_error_detail(e))

    # CRD path
    if has_crd:
        generated_key = ""
        old_key_to_delete = ""
        existing = None
        try:
            if has_secret_change:
                crd_ops._ensure_secret(uid, req.app_secret)
            if changes:
                crd_changes = {_DB_TO_CRD_FIELD[k]: v for k, v in changes.items() if k in _DB_TO_CRD_FIELD}
                existing = crd_ops.get_her_instance(uid)
                new_provider = changes.get("provider")
                if new_provider == "litellm":
                    existing_key = (existing or {}).get("spec", {}).get("litellmKey", "")
                    if not existing_key:
                        inst_name = changes.get("name") or (existing or {}).get("spec", {}).get("name", "")
                        generated_key = litellm_ops.generate_key(uid, name=inst_name) or ""
                        if not generated_key:
                            raise HTTPException(502, f"Failed to generate LiteLLM key for her-{uid}")
                        crd_changes["litellmKey"] = generated_key
                elif new_provider and new_provider != "litellm":
                    old_key = (existing or {}).get("spec", {}).get("litellmKey", "")
                    if old_key:
                        crd_changes["litellmKey"] = ""
                        old_key_to_delete = old_key
                crd_ops.update_her_instance(uid, crd_changes)
                if old_key_to_delete:
                    litellm_ops.delete_key(old_key_to_delete)
        except Exception as e:
            if generated_key:
                litellm_ops.delete_key(generated_key)
            if isinstance(e, HTTPException):
                raise
            if isinstance(e, K8sApiException):
                raise HTTPException(e.status or 500, detail=_k8s_error_detail(e))
            raise HTTPException(500, f"Failed to update HerInstance {uid}: {e}")
        reported = {**changes}
        if has_secret_change:
            reported["app_secret"] = "***updated***"
        return {"id": uid, "action": "updated", "managed_by": "operator", "changes": reported}

    # Legacy path
    inst = db.get_by_id(uid)
    if not inst:
        raise HTTPException(404, f"Instance {uid} not found")
    old_key_to_delete = ""
    db_changes = {k: v for k, v in changes.items() if k != "image"}
    if "image" in changes:
        db_changes["image_tag"] = changes["image"]
    if has_secret_change:
        db_changes["app_secret"] = req.app_secret
    new_provider = changes.get("provider")
    if new_provider == "litellm" and not inst.get("litellm_key"):
        generated_key = litellm_ops.generate_key(uid, name=changes.get("name") or inst.get("name", "")) or ""
        if not generated_key:
            raise HTTPException(502, f"Failed to generate LiteLLM key for her-{uid}")
        db_changes["litellm_key"] = generated_key
    elif new_provider and new_provider != "litellm" and inst.get("litellm_key"):
        db_changes["litellm_key"] = ""
        old_key_to_delete = inst.get("litellm_key", "")
    if db_changes:
        inst = db.update(uid, db_changes)
    if not inst:
        raise HTTPException(404, f"Instance {uid} not found")
    try:
        if "prefix" in changes or "image" in changes:
            k8s_ops.ensure_service(uid)
        _sync_and_deploy(inst)
        if "image" in changes or "prefix" in changes:
            k8s_ops.create_pod(
                uid,
                prefix=inst.get("prefix", "s1"),
                image_tag=changes.get("image") or inst.get("image_tag", "v20260328"),
            )
        if "prefix" in changes:
            _update_legacy_cloudflare(uid, inst.get("prefix", "s1"))
    except Exception as e:
        logger.warning("ConfigMap sync failed for %d after update: %s", uid, e)
    finally:
        if old_key_to_delete:
            try:
                litellm_ops.delete_key(old_key_to_delete)
            except Exception as e:
                logger.warning("Failed to delete old LiteLLM key for %d: %s", uid, e)

    return {"id": uid, "action": "updated", "changes": changes}


# ── API: Lifecycle ──

@app.post("/api/instances/{uid}/stop")
def api_stop(uid: int):
    """Stop (pause) an instance. CRD: sets paused=true; Operator deletes Pod."""
    try:
        has_crd = _has_crd(uid)
    except K8sApiException as e:
        raise HTTPException(e.status or 500, detail=_k8s_error_detail(e))
    if has_crd:
        try:
            crd_ops.pause_her_instance(uid)
        except K8sApiException as e:
            raise HTTPException(e.status or 500, detail=_k8s_error_detail(e))
        return {"id": uid, "action": "stopped", "managed_by": "operator"}

    k8s_ops.delete_pod(uid)
    db.set_status(uid, "stopped")
    return {"id": uid, "action": "stopped"}


@app.post("/api/instances/{uid}/start")
def api_start(uid: int):
    """Start (resume) an instance. CRD: sets paused=false; Operator creates Pod."""
    try:
        has_crd = _has_crd(uid)
    except K8sApiException as e:
        raise HTTPException(e.status or 500, detail=_k8s_error_detail(e))
    if has_crd:
        try:
            crd_ops.resume_her_instance(uid)
        except K8sApiException as e:
            raise HTTPException(e.status or 500, detail=_k8s_error_detail(e))
        return {"id": uid, "action": "started", "managed_by": "operator"}

    inst = db.get_by_id(uid)
    if not inst:
        raise HTTPException(404, f"Instance {uid} not found")
    k8s_ops.ensure_service(uid)
    _sync_and_deploy(inst)
    k8s_ops.create_pod(uid, prefix=inst.get("prefix", "s1"))
    db.set_status(uid, "running")
    _update_legacy_cloudflare(uid, inst.get("prefix", "s1"))
    return {"id": uid, "action": "started"}


@app.post("/api/instances/{uid}/restart")
async def api_restart(uid: int):
    """Restart an instance. CRD: deletes Pod, Operator self-heals and recreates."""
    try:
        has_crd = _has_crd(uid)
    except K8sApiException as e:
        raise HTTPException(e.status or 500, detail=_k8s_error_detail(e))
    if has_crd:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, k8s_ops.delete_pod, uid)
        return {"id": uid, "action": "restarted", "managed_by": "operator",
                "note": "Pod deleted; operator will recreate within 30s"}

    inst = db.get_by_id(uid)
    if not inst:
        raise HTTPException(404, f"Instance {uid} not found")
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, k8s_ops.delete_pod, uid)
    await asyncio.sleep(2)
    k8s_ops.ensure_service(uid)
    _sync_and_deploy(inst)
    k8s_ops.create_pod(uid, prefix=inst.get("prefix", "s1"))
    _update_legacy_cloudflare(uid, inst.get("prefix", "s1"))
    return {"id": uid, "action": "restarted"}


@app.delete("/api/instances/{uid}")
def api_delete(uid: int, purge: bool = Query(False)):
    """Delete an instance. CRD: deletes HerInstance; Operator cleans up Pod + ConfigMap."""
    try:
        has_crd = _has_crd(uid)
    except K8sApiException as e:
        raise HTTPException(e.status or 500, detail=_k8s_error_detail(e))
    if has_crd:
        crd = crd_ops.get_her_instance(uid)
        old_key = (crd or {}).get("spec", {}).get("litellmKey", "")
        try:
            crd_ops.delete_her_instance(uid, purge_data=purge)
        except K8sApiException as e:
            if e.status != 404:
                raise HTTPException(e.status or 500, detail=_k8s_error_detail(e))
        if old_key:
            litellm_ops.delete_key(old_key)
        try:
            if purge:
                db.purge_instance(uid)
            else:
                db.delete_instance(uid)
        except Exception:
            pass
        # knownBots cache invalidation removed — bots now register dynamically via Redis.
        try:
            cloudflare_ops.sync_tunnel_config()
            cloudflare_ops.update_remote_ingress()
        except Exception as cf_err:
            logger.warning("Cloudflare config sync failed after delete %d: %s", uid, cf_err)
        return {"id": uid, "action": "deleted", "managed_by": "operator", "purge": purge}

    inst = db.get_by_id(uid)
    old_key = (inst or {}).get("litellm_key", "") if inst else ""
    try:
        # Delete the Service first so a new RBAC/API failure does not leave
        # the Pod/ConfigMap gone while the DB row is still active.
        k8s_ops.delete_service(uid)
        k8s_ops.delete_pod(uid)
        k8s_ops.delete_configmap(uid)
        if purge:
            k8s_ops.delete_pvc(uid)
    except K8sApiException as e:
        raise HTTPException(e.status or 500, detail=_k8s_error_detail(e))
    if purge:
        db.purge_instance(uid)
    else:
        db.delete_instance(uid)
    if old_key:
        litellm_ops.delete_key(old_key)
    # knownBots cache invalidation removed — bots now register dynamically via Redis.
    try:
        cloudflare_ops.sync_tunnel_config()
        cloudflare_ops.update_remote_ingress()
    except Exception as cf_err:
        logger.warning("Cloudflare config sync failed after legacy delete %d: %s", uid, cf_err)
    return {"id": uid, "action": "deleted", "purge": purge}


# ── API: Batch ──

@app.post("/api/instances/batch", dependencies=[Depends(verify_api_key)])
async def api_batch_action(req: HerBatchAction):
    results = []
    for uid in req.ids:
        try:
            if req.action == "stop":
                results.append(api_stop(uid))
            elif req.action == "start":
                results.append(api_start(uid))
            elif req.action == "restart":
                results.append(await api_restart(uid))
            elif req.action == "delete":
                purge = req.params and req.params.image == "purge"
                results.append(api_delete(uid, purge=bool(purge)))
            elif req.action == "update":
                if req.params:
                    results.append(api_update(uid, req.params))
            else:
                results.append({"id": uid, "error": f"Unknown action: {req.action}"})
        except HTTPException as he:
            results.append({"id": uid, "error": he.detail or f"HTTP {he.status_code}"})
        except Exception as e:
            results.append({"id": uid, "error": str(e) or repr(e)})
    return {"results": results}


# ── API: Logs ──

@app.get("/api/instances/{uid}/logs")
def api_logs(uid: int, tail: int = Query(200)):
    return {"logs": k8s_ops.get_logs(uid, tail=tail)}


# ── API: Cluster status / Health / Sync ──

@app.get("/api/status")
def api_status():
    """Cluster status summary, merging CRD and DB counts."""
    k8s_status = k8s_ops.cluster_status()
    # Include CRD-managed paused instances as "stopped"
    crd_paused = 0
    try:
        for inst in crd_ops.list_her_instances():
            if inst.get("spec", {}).get("paused"):
                crd_paused += 1
    except Exception:
        pass
    db_counts = _db_instances_excluding_crds()
    stopped = sum(1 for i in db_counts if i["status"] == "stopped")
    k8s_status["stopped"] = stopped + crd_paused
    return k8s_status


@app.get("/api/health")
def api_health():
    """Health overview. Uses CRD status (maintained by Go operator) for O(1) list call,
    avoiding per-Pod log reads that don't scale past a few hundred instances."""
    try:
        crd_statuses = crd_ops.get_all_statuses()
        results = []
        for uid, data in crd_statuses.items():
            spec = data.get("spec", {})
            status = data.get("status", {})
            if spec.get("paused"):
                continue
            results.append({
                "id": uid,
                "name": spec.get("name", ""),
                "feishu_ws": status.get("feishuWS", "Unknown") == "Connected",
                "memory_db": True,  # not tracked by operator; assume ok if Pod running
                "model_ok": True,
                "status": status.get("phase", "Unknown"),
            })
        return sorted(results, key=lambda x: x["id"])
    except Exception:
        pass

    # Legacy fallback: only check pods that are Running (skip exec-based checks for scale)
    instances = _db_instances_excluding_crds()
    pod_statuses = k8s_ops.get_all_pod_statuses()
    results = []
    for inst in instances:
        uid = inst["id"]
        if uid not in pod_statuses or inst["status"] == "deleted":
            continue
        phase = pod_statuses[uid].get("phase", "?")
        results.append({
            "id": uid,
            "name": inst.get("name", ""),
            "feishu_ws": False,
            "memory_db": False,
            "model_ok": False,
            "status": phase,
        })
    return sorted(results, key=lambda x: x["id"])


@app.get("/api/next-id")
def api_next_id():
    """Next available ID, considering both DB and CRD instances."""
    db_next = db.next_id()
    crd_max = 0
    try:
        for inst in crd_ops.list_her_instances():
            uid = inst.get("spec", {}).get("userId", 0)
            if uid > crd_max:
                crd_max = uid
    except Exception:
        pass
    return {"next_id": max(db_next, crd_max + 1)}


@app.post("/api/sync/force")
def api_force_sync():
    """Manually trigger full ConfigMap sync for all instances."""
    return sync_worker.sync_all()


@app.get("/api/sync/check")
def api_consistency_check():
    """Check DB vs K8s consistency."""
    return sync_worker.consistency_check()


@app.get("/api/audit")
def api_audit_log(instance_id: int | None = Query(None), limit: int = Query(50)):
    return db.get_audit_log(instance_id=instance_id, limit=limit)


@app.post("/api/import-from-k8s")
def api_import_from_k8s():
    """One-time migration: scan existing ConfigMaps and import into DB."""
    configmaps = k8s_ops.discover_all_configmaps()
    imported = 0
    skipped = 0
    for uid, cfg in configmaps:
        existing = db.get_by_id(uid)
        if existing:
            skipped += 1
            continue
        try:
            db.import_from_configmap_data(uid, cfg)
            imported += 1
        except Exception as e:
            logger.warning("Import failed for %d: %s", uid, e)
    return {"imported": imported, "skipped": skipped, "total": len(configmaps)}


# ── API: Metrics ──

@app.get("/api/metrics/overview", tags=["metrics"])
def api_metrics_overview():
    """Cluster-wide resource overview: nodes CPU/Memory, Her totals, PVC storage."""
    return metrics_mod.get_cluster_overview()


@app.get("/api/metrics/nodes", tags=["metrics"])
def api_metrics_nodes():
    """Per-node CPU/Memory usage and capacity."""
    return metrics_mod.get_node_metrics()


@app.get("/api/metrics/pods", tags=["metrics"])
def api_metrics_pods():
    """Real-time CPU/Memory for all Her pods."""
    return metrics_mod.get_all_pod_metrics()


@app.get("/api/instances/{uid}/metrics", tags=["metrics"])
def api_instance_metrics(uid: int):
    """Real-time CPU/Memory for a specific instance."""
    return metrics_mod.get_pod_metrics(uid)


@app.get("/api/instances/{uid}/metrics/history", tags=["metrics"])
def api_instance_metrics_history(uid: int, hours: int = Query(24, ge=1, le=168)):
    """Historical CPU/Memory for a specific pod (default: 24h, max: 7 days)."""
    data = db.get_pod_metrics_history(uid, hours=hours)
    return {"uid": uid, "hours": hours, "samples": len(data), "data": data}


@app.get("/api/metrics/history/nodes", tags=["metrics"])
def api_node_metrics_history(hours: int = Query(24, ge=1, le=168)):
    """Historical aggregated node metrics (default: 24h, max: 7 days)."""
    data = db.get_node_metrics_history(hours=hours)
    return {"hours": hours, "samples": len(data), "data": data}


@app.get("/api/metrics/storage", tags=["metrics"])
def api_metrics_storage():
    """PVC storage status in carher namespace."""
    return metrics_mod.get_storage_info()


# ── API: Deploy Pipeline ──

@app.post("/api/deploy", tags=["deploy"], dependencies=[Depends(verify_api_key)])
async def api_start_deploy(req: DeployRequest):
    """Start a new deployment.

    Modes: normal (canary→early→stable), fast (all at once), canary-only, group:<name>.
    Set force=true to re-deploy even if the same image tag was already deployed.
    """
    valid_modes = ("normal", "fast", "canary-only")
    if req.mode not in valid_modes and not req.mode.startswith("group:"):
        raise HTTPException(400, f"mode must be {', '.join(valid_modes)}, or group:<name>")
    return await deployer.start_deploy(req.image_tag, mode=req.mode, force=req.force)


@app.get("/api/deploy/status")
def api_deploy_status():
    return deployer.get_deploy_status()


@app.post("/api/deploy/continue")
async def api_deploy_continue():
    return await deployer.continue_deploy()


@app.post("/api/deploy/rollback")
async def api_deploy_rollback():
    return await deployer.rollback_deploy()


@app.post("/api/deploy/abort")
def api_deploy_abort():
    return deployer.abort_deploy()


@app.get("/api/deploy/history")
def api_deploy_history(limit: int = Query(20)):
    return db.list_deploys(limit=limit)


@app.get("/api/image-tags", tags=["deploy"])
def api_list_image_tags(limit: int = Query(30)):
    """List available CarHer image tags from ACR sync + deploy history + instances."""
    return db.list_image_tags(limit=limit)


@app.post("/api/image-tags/sync", tags=["deploy"])
def api_sync_image_tags():
    """Sync the fixed her/carher repository tags from Alibaba Cloud ACR into SQLite."""
    settings = db.get_acr_settings()
    try:
        acr_settings = acr_client.build_settings(**settings)
        tags = acr_client.list_carher_tags(acr_settings)
    except acr_client.ACRConfigError as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        logger.exception("ACR tag sync failed")
        raise HTTPException(502, f"ACR sync failed: {e}")

    upserted = db.upsert_acr_image_tags([
        {
            "tag": item.tag,
            "repo_namespace": acr_client.REPO_NAMESPACE,
            "repo_name": acr_client.REPO_NAME,
            "digest": item.digest,
            "image_id": item.image_id,
            "image_size": item.image_size,
            "image_update_ms": item.image_update_ms,
            "updated_at": item.updated_at,
        }
        for item in tags
    ])
    return {
        "repo": f"{acr_client.REPO_NAMESPACE}/{acr_client.REPO_NAME}",
        "fetched": len(tags),
        "upserted": upserted,
        "latest_tags": [item.tag for item in tags[:10]],
    }


@app.put("/api/instances/{uid}/deploy-group", tags=["deploy-groups"])
def api_set_deploy_group(uid: int, req: SetDeployGroupRequest):
    """Move a single instance to a deploy group. Syncs both CRD and DB."""
    try:
        has_crd = _has_crd(uid)
    except K8sApiException as e:
        raise HTTPException(e.status or 500, detail=_k8s_error_detail(e))
    if has_crd:
        try:
            crd_ops.set_deploy_group(uid, req.group)
        except K8sApiException as e:
            raise HTTPException(e.status or 500, detail=_k8s_error_detail(e))
    try:
        db.set_deploy_group(uid, req.group)
    except Exception:
        logger.warning("DB set_deploy_group failed for uid=%d (CRD already updated)", uid, exc_info=True)
    managed = "operator" if has_crd else "db"
    return {"id": uid, "deploy_group": req.group, "managed_by": managed}


@app.post("/api/instances/batch-deploy-group", tags=["deploy-groups"])
def api_batch_set_deploy_group(req: BatchSetDeployGroupRequest):
    """Move multiple instances to a deploy group at once. Syncs both CRD and DB."""
    crd_count = 0
    errors = []
    all_ids = list(req.ids)
    for uid in all_ids:
        try:
            has_crd = _has_crd(uid)
        except K8sApiException as e:
            errors.append({"id": uid, "error": _k8s_error_detail(e)})
            continue
        if has_crd:
            try:
                crd_ops.set_deploy_group(uid, req.group)
                crd_count += 1
            except K8sApiException as e:
                errors.append({"id": uid, "error": _k8s_error_detail(e)})
    try:
        db.batch_set_deploy_group(all_ids, req.group)
    except Exception:
        logger.warning("DB batch_set_deploy_group failed (CRD already updated)", exc_info=True)
    result = {"action": "batch_set_deploy_group", "count": len(all_ids),
              "group": req.group, "crd_updated": crd_count, "db_synced": len(all_ids)}
    if errors:
        result["errors"] = errors
    return result


# ── API: Deploy Groups ──

@app.get("/api/deploy-groups")
def api_list_deploy_groups():
    groups = db.list_deploy_groups()
    stats = db.get_deploy_group_stats()
    # Merge CRD instance counts
    try:
        for inst in crd_ops.list_her_instances():
            g = inst.get("spec", {}).get("deployGroup", "stable")
            stats[g] = stats.get(g, 0) + 1
    except Exception:
        pass
    for g in groups:
        g["count"] = stats.get(g["name"], 0)
    return groups


@app.post("/api/deploy-groups", tags=["deploy-groups"])
def api_create_deploy_group(req: DeployGroupCreate):
    """Create a custom deploy group. Lower priority = deployed first."""
    name = req.name.strip().lower()
    if not name:
        raise HTTPException(400, "name is required")
    if not all(c.isalnum() or c in "-_" for c in name):
        raise HTTPException(400, "name must be alphanumeric (with - or _)")
    db.create_deploy_group(name, req.priority, req.description)
    return {"name": name, "priority": req.priority}


@app.put("/api/deploy-groups/{name}", tags=["deploy-groups"])
def api_update_deploy_group(name: str, req: DeployGroupUpdate):
    """Update priority or description of a deploy group."""
    db.update_deploy_group(name, priority=req.priority, description=req.description)
    return {"name": name, "updated": True}


@app.delete("/api/deploy-groups/{name}")
def api_delete_deploy_group(name: str):
    if name in ("stable",):
        raise HTTPException(400, "Cannot delete the default 'stable' group")
    moved = db.delete_deploy_group(name)
    return {"name": name, "deleted": True, "instances_moved_to_stable": moved}


@app.post("/api/deploy/webhook", tags=["deploy"])
async def api_deploy_webhook(req: DeployWebhookRequest):
    """GitHub Actions webhook with CI metadata. Auto-matches branch rules if mode is empty."""
    expected = db.get_webhook_secret()
    if not expected:
        raise HTTPException(503, "DEPLOY_WEBHOOK_SECRET not configured (set in Settings or env)")
    if req.secret != expected:
        raise HTTPException(403, "Invalid webhook secret")

    ci_meta = {
        "branch": req.branch, "commit_sha": req.commit_sha,
        "commit_msg": req.commit_msg, "author": req.author,
        "repo": req.repo, "run_url": req.run_url,
    }

    mode = req.mode
    if not mode and req.branch:
        rule = db.match_branch_rule(req.branch)
        if rule:
            mode = rule["deploy_mode"]
            if not rule["auto_deploy"]:
                return {"status": "build_only", "image_tag": req.image_tag,
                        "branch": req.branch, "matched_rule": rule["pattern"],
                        "message": "Branch rule matched but auto_deploy=false"}
    mode = mode or "normal"

    return await deployer.start_deploy(req.image_tag, mode=mode, ci_meta=ci_meta)


# ── API: Branch Rules ──

@app.get("/api/branch-rules", tags=["ci-cd"])
def api_list_branch_rules():
    """List all branch → deploy mode mapping rules."""
    return db.list_branch_rules()


@app.post("/api/branch-rules", tags=["ci-cd"])
def api_create_branch_rule(req: BranchRuleCreate):
    """Create a branch rule. Supports glob patterns: main, hotfix/*, feature/*."""
    rule_id = db.create_branch_rule(
        req.pattern, req.deploy_mode, req.target_group, req.auto_deploy, req.description)
    return {"id": rule_id, "pattern": req.pattern}


@app.put("/api/branch-rules/{rule_id}", tags=["ci-cd"])
def api_update_branch_rule(rule_id: int, req: BranchRuleUpdate):
    """Update an existing branch rule."""
    ok = db.update_branch_rule(rule_id, **req.model_dump(exclude_none=True))
    if not ok:
        raise HTTPException(404, "Rule not found or no changes")
    return {"id": rule_id, "updated": True}


@app.delete("/api/branch-rules/{rule_id}", tags=["ci-cd"])
def api_delete_branch_rule(rule_id: int):
    """Delete a branch rule."""
    db.delete_branch_rule(rule_id)
    return {"id": rule_id, "deleted": True}


@app.post("/api/branch-rules/test", tags=["ci-cd"])
def api_test_branch_rule(branch: str = Query(..., description="Branch name to test")):
    """Test which rule would match a given branch name."""
    rule = db.match_branch_rule(branch)
    return {"branch": branch, "matched_rule": dict(rule) if rule else None}


# ── API: Trigger GitHub Build ──

@app.post("/api/ci/trigger-build", tags=["ci-cd"])
async def api_trigger_build(req: TriggerBuildRequest):
    """Trigger a GitHub Actions workflow_dispatch build.

    Requires GITHUB_TOKEN (DB setting or env var) with workflow dispatch permission.
    """
    token = db.get_github_token()
    if not token:
        raise HTTPException(503, "GITHUB_TOKEN not configured (set in Settings or env)")

    import aiohttp
    url = f"https://api.github.com/repos/{req.repo}/actions/workflows/{req.workflow}/dispatches"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    body = {"ref": req.branch, "inputs": {"deploy_mode": req.deploy_mode}}

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 204:
                return {"status": "triggered", "repo": req.repo, "branch": req.branch,
                        "workflow": req.workflow, "deploy_mode": req.deploy_mode}
            text = await resp.text()
            raise HTTPException(resp.status, f"GitHub API error: {text}")


@app.get("/api/ci/workflows", tags=["ci-cd"])
async def api_list_workflows(repo: str = Query("guangzhou/CarHer", description="GitHub repo owner/name")):
    """List workflows that support workflow_dispatch for a repo."""
    token = db.get_github_token()
    headers = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"token {token}"

    import aiohttp
    import asyncio
    import base64
    url = f"https://api.github.com/repos/{repo}/actions/workflows?per_page=100"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return {"repo": repo, "workflows": [], "error": f"GitHub API {resp.status}"}
                data = await resp.json()
                active_workflows = [
                    w for w in data.get("workflows", [])
                    if w.get("state") == "active"
                ]

            async def check_dispatch(w):
                content_url = f"https://api.github.com/repos/{repo}/contents/{w['path']}"
                has_dispatch = None
                try:
                    async with session.get(content_url, headers=headers,
                                           timeout=aiohttp.ClientTimeout(total=10)) as r:
                        if r.status == 200:
                            cdata = await r.json()
                            content = base64.b64decode(cdata.get("content", "")).decode()
                            has_dispatch = "workflow_dispatch" in content
                except Exception:
                    pass
                return {"name": w["name"], "file": w["path"].split("/")[-1],
                        "state": w["state"], "has_dispatch": has_dispatch}

            workflows = await asyncio.gather(*[check_dispatch(w) for w in active_workflows])
            return {"repo": repo, "workflows": [w for w in workflows if w["has_dispatch"] is not False]}
    except Exception as e:
        return {"repo": repo, "workflows": [], "error": str(e)}


@app.get("/api/ci/branches", tags=["ci-cd"])
async def api_list_branches(repo: str = Query("guangzhou/CarHer", description="GitHub repo owner/name")):
    """List branches of a GitHub repo. Requires GITHUB_TOKEN for private repos."""
    token = db.get_github_token()
    headers = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"token {token}"

    import aiohttp
    url = f"https://api.github.com/repos/{repo}/branches?per_page=100"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return {"repo": repo, "branches": [], "error": f"GitHub API {resp.status}"}
                data = await resp.json()
                branches = [b["name"] for b in data]
                return {"repo": repo, "branches": branches}
    except Exception as e:
        return {"repo": repo, "branches": [], "error": str(e)}


# ── API: CI Runs (GitHub Actions) ──

@app.get("/api/ci/runs", tags=["ci-cd"])
async def api_list_runs(
    repo: str = Query("", description="GitHub repo (empty = all configured repos)"),
    per_page: int = Query(10, ge=1, le=30),
):
    """List recent GitHub Actions workflow runs. If repo is empty, fetches from all configured repos."""
    token = db.get_github_token()
    headers = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"token {token}"

    repos = [repo] if repo else db.get_github_repos()
    all_runs = []

    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            for r in repos:
                url = f"https://api.github.com/repos/{r}/actions/runs?per_page={per_page}"
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                    for run in data.get("workflow_runs", []):
                        all_runs.append({
                            "id": run["id"],
                            "repo": r,
                            "name": run.get("name", ""),
                            "branch": run.get("head_branch", ""),
                            "status": run.get("status", ""),
                            "conclusion": run.get("conclusion"),
                            "created_at": run.get("created_at", ""),
                            "updated_at": run.get("updated_at", ""),
                            "html_url": run.get("html_url", ""),
                            "run_number": run.get("run_number", 0),
                            "event": run.get("event", ""),
                            "actor": run.get("actor", {}).get("login", ""),
                        })
    except Exception as e:
        return {"runs": all_runs, "error": str(e)}

    all_runs.sort(key=lambda x: x["created_at"], reverse=True)
    return {"runs": all_runs[:per_page]}


# ── API: Settings ──

@app.get("/api/settings", tags=["settings"])
async def api_get_settings():
    """Get all settings. Secret values are masked."""
    return db.get_all_settings(include_secrets=False)


@app.put("/api/settings", tags=["settings"])
async def api_update_settings(updates: dict[str, str]):
    """Update settings. Only send keys you want to change.
    For secrets, send empty string to clear, or the new full value."""
    safe_keys = {"github_token", "github_repos", "webhook_secret",
                 "feishu_webhook", "agent_api_key", "acr_registry",
                 "acr_username", "acr_password"}
    filtered = {k: v for k, v in updates.items() if k in safe_keys}
    if not filtered:
        raise HTTPException(400, "No valid settings to update")
    # Skip masked values (frontend sends back "••••xxxx")
    filtered = {k: v for k, v in filtered.items() if not v.startswith("••••")}
    if filtered:
        db.update_settings(filtered)
    return db.get_all_settings(include_secrets=False)


@app.get("/api/settings/repos", tags=["settings"])
async def api_get_repos():
    """Get configured GitHub repos list."""
    return {"repos": db.get_github_repos()}


@app.post("/api/cloudflare/sync", tags=["settings"])
async def api_cloudflare_sync():
    """Regenerate cloudflared config from active CRDs and restart if changed.
    Also ensures infrastructure routes (e.g. litellm) exist in remote tunnel config.
    """
    try:
        changed = cloudflare_ops.sync_tunnel_config()
        cloudflare_ops.update_remote_ingress()
        return {"synced": True, "config_changed": changed}
    except Exception as e:
        raise HTTPException(500, f"Cloudflare sync failed: {e}")


# ── API: Config Preview ──

@app.get("/api/instances/{uid}/config-preview", tags=["instances"])
def api_config_preview(uid: int):
    """Preview the openclaw.json that would be generated, without applying it.
    For CRD-managed instances, synthesizes a DB-like dict from the CRD spec."""
    try:
        crd = crd_ops.get_her_instance(uid)
        if crd:
            spec = crd.get("spec", {})
            app_secret = ""
            secret_name = spec.get("appSecretRef") or f"carher-{uid}-secret"
            try:
                secret = k8s_ops._core().read_namespaced_secret(secret_name, "carher")
                import base64
                raw = (secret.data or {}).get("app_secret", "")
                if raw:
                    app_secret = base64.b64decode(raw).decode()
            except Exception:
                pass
            inst = {
                "id": uid, "name": spec.get("name", ""),
                "model": spec.get("model", "gpt"), "app_id": spec.get("appId", ""),
                "app_secret": app_secret, "prefix": spec.get("prefix", "s1"),
                "owner": spec.get("owner", ""), "provider": spec.get("provider", "wangsu"),
                "bot_open_id": spec.get("botOpenId", ""),
                "litellm_key": spec.get("litellmKey", ""),
            }
            result = config_gen.generate_openclaw_json(inst)
            if isinstance(result, dict):
                _redact_secrets(result)
            return result
    except Exception:
        pass

    inst = db.get_by_id(uid)
    if not inst:
        raise HTTPException(404, f"Instance {uid} not found")
    result = config_gen.generate_openclaw_json(inst)
    if isinstance(result, dict):
        _redact_secrets(result)
    return result


@app.get("/api/instances/{uid}/config-current", tags=["instances"])
def api_config_current(uid: int):
    """Get the currently applied ConfigMap content for an instance."""
    try:
        cm = k8s_ops._core().read_namespaced_config_map(f"carher-{uid}-user-config", "carher")
        import json
        return json.loads(cm.data.get("openclaw.json", "{}"))
    except Exception as e:
        raise HTTPException(404, f"ConfigMap not found: {e}")


# ── API: knownBots ──

@app.get("/api/known-bots", tags=["system"])
def api_known_bots():
    """Get the global knownBots registry (all bot app_id -> name mappings).
    Merges DB and CRD instances."""
    bots, bot_open_ids = db.collect_known_bots()
    try:
        for inst in crd_ops.list_her_instances():
            spec = inst.get("spec", {})
            app_id = spec.get("appId", "")
            name = spec.get("name", "")
            if app_id and name:
                bots[app_id] = name
            boi = spec.get("botOpenId", "")
            if boi and app_id:
                bot_open_ids[boi] = app_id
    except Exception:
        pass
    return {
        "known_bots": bots,
        "known_bot_open_ids": bot_open_ids,
        "total": len(bots),
    }


# ── API: LiteLLM Key Management ──

@app.post("/api/litellm/keys/generate", tags=["litellm"], dependencies=[Depends(verify_api_key)])
def api_litellm_generate_key(uid: int = Query(..., description="Instance ID")):
    """Generate a LiteLLM virtual key for a specific instance if missing."""
    if not litellm_ops.LITELLM_MASTER_KEY:
        raise HTTPException(503, "LITELLM_MASTER_KEY is not configured")

    # Try CRD first
    inst = crd_ops.get_her_instance(uid)
    if inst:
        if inst.get("spec", {}).get("provider") != "litellm":
            raise HTTPException(400, f"Instance her-{uid} is not using provider=litellm")
        old_key = inst.get("spec", {}).get("litellmKey", "")
        if old_key:
            return {"id": uid, "key": old_key, "status": "already_exists"}
        name = inst.get("spec", {}).get("name", "")
        key = litellm_ops.generate_key(uid, name=name)
        if not key:
            raise HTTPException(502, "Failed to generate LiteLLM key")
        try:
            crd_ops.update_her_instance(uid, {"litellmKey": key})
        except Exception as e:
            litellm_ops.delete_key(key)
            logger.warning("CRD patch litellmKey failed for %d: %s", uid, e)
            raise HTTPException(500, f"Failed to patch CRD with LiteLLM key: {e}")
        return {"id": uid, "key": key}

    # Legacy DB fallback
    db_inst = db.get_by_id(uid)
    if not db_inst:
        raise HTTPException(404, f"Instance her-{uid} not found")
    if db_inst.get("provider") != "litellm":
        raise HTTPException(400, f"Instance her-{uid} is not using provider=litellm")
    old_key = db_inst.get("litellm_key", "")
    if old_key:
        return {"id": uid, "key": old_key, "status": "already_exists"}
    key = litellm_ops.generate_key(uid, name=db_inst.get("name", ""))
    if not key:
        raise HTTPException(502, "Failed to generate LiteLLM key")
    try:
        updated = db.update(uid, {"litellm_key": key})
        if not updated:
            raise RuntimeError(f"Instance her-{uid} disappeared while storing LiteLLM key")
    except Exception as e:
        litellm_ops.delete_key(key)
        logger.warning("Legacy DB update litellm_key failed for %d: %s", uid, e)
        raise HTTPException(500, f"Failed to store LiteLLM key for her-{uid}: {e}")
    return {"id": uid, "key": key, "managed_by": "legacy"}


@app.post("/api/litellm/keys/generate-batch", tags=["litellm"], dependencies=[Depends(verify_api_key)])
def api_litellm_generate_batch():
    """Generate LiteLLM keys for ALL instances that use litellm provider but have no key yet."""
    if not litellm_ops.LITELLM_MASTER_KEY:
        raise HTTPException(503, "LITELLM_MASTER_KEY is not configured")
    results = []

    # CRD instances
    for inst in crd_ops.list_her_instances():
        spec = inst.get("spec", {})
        uid = spec.get("userId", 0)
        if not uid or spec.get("provider") != "litellm":
            continue
        if spec.get("litellmKey"):
            results.append({"id": uid, "status": "already_has_key"})
            continue
        key = litellm_ops.generate_key(uid, name=spec.get("name", ""))
        if key:
            try:
                crd_ops.update_her_instance(uid, {"litellmKey": key})
                results.append({"id": uid, "status": "generated"})
            except Exception as e:
                litellm_ops.delete_key(key)
                results.append({"id": uid, "status": "error", "error": str(e)})
        else:
            results.append({"id": uid, "status": "generation_failed"})

    # Legacy DB instances (not covered by CRD)
    handled_uids = {r["id"] for r in results}
    for inst in _db_instances_excluding_crds():
        uid = inst.get("id")
        if not uid or uid in handled_uids or inst.get("provider") != "litellm":
            continue
        if inst.get("litellm_key"):
            results.append({"id": uid, "status": "already_has_key", "managed_by": "legacy"})
            continue
        key = litellm_ops.generate_key(uid, name=inst.get("name", ""))
        if key:
            try:
                updated = db.update(uid, {"litellm_key": key})
                if not updated:
                    raise RuntimeError(f"Instance her-{uid} disappeared while storing LiteLLM key")
                results.append({"id": uid, "status": "generated", "managed_by": "legacy"})
            except Exception as e:
                litellm_ops.delete_key(key)
                results.append({"id": uid, "status": "error", "error": str(e), "managed_by": "legacy"})
        else:
            results.append({"id": uid, "status": "generation_failed", "managed_by": "legacy"})

    return {"results": results}


@app.get("/api/litellm/spend", tags=["litellm"], dependencies=[Depends(verify_api_key)])
def api_litellm_spend():
    """Get spend summary for all LiteLLM virtual keys (per-instance spend tracking)."""
    import urllib.request
    if not litellm_ops.LITELLM_MASTER_KEY:
        raise HTTPException(503, "LITELLM_MASTER_KEY is not configured")
    try:
        req = urllib.request.Request(
            f"{litellm_ops.LITELLM_PROXY_URL}/global/spend/keys?limit=500",
            headers={"Authorization": f"Bearer {litellm_ops.LITELLM_MASTER_KEY}"},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        data = _json.loads(resp.read())
        return {"spend_data": data}
    except Exception as e:
        raise HTTPException(502, f"Failed to fetch LiteLLM spend data: {e}")


# ── API: Statistics ──

@app.get("/api/stats", tags=["system"])
def api_stats():
    """Aggregated statistics for dashboard and monitoring.
    Merges CRD-managed and DB-managed instances."""
    model_dist: dict[str, int] = {}
    provider_dist: dict[str, int] = {}
    prefix_dist: dict[str, int] = {}
    group_dist: dict[str, int] = {}
    total = 0
    running = 0
    stopped = 0
    paused = 0
    seen_uids: set[int] = set()

    # CRD-managed instances
    try:
        for inst in crd_ops.list_her_instances():
            spec = inst.get("spec", {})
            status = inst.get("status", {})
            uid = spec.get("userId", 0)
            if not uid:
                continue
            seen_uids.add(uid)
            total += 1

            phase = status.get("phase", "Unknown")
            if spec.get("paused"):
                paused += 1
            elif phase == "Running":
                running += 1

            m = spec.get("model", "gpt")
            model_dist[m] = model_dist.get(m, 0) + 1
            p = spec.get("provider", "wangsu")
            provider_dist[p] = provider_dist.get(p, 0) + 1
            pfx = spec.get("prefix", "s1")
            prefix_dist[pfx] = prefix_dist.get(pfx, 0) + 1
            g = spec.get("deployGroup", "stable")
            group_dist[g] = group_dist.get(g, 0) + 1
    except Exception as e:
        logger.warning("CRD stats failed: %s", e)

    # DB-managed instances
    instances = _db_instances_excluding_crds()
    pod_statuses = k8s_ops.get_all_pod_statuses()
    for inst in instances:
        if inst["status"] == "deleted":
            continue
        uid = inst["id"]
        if uid in seen_uids:
            continue
        total += 1
        pod = pod_statuses.get(uid, {})
        if pod.get("pod_exists") and pod.get("phase") == "Running":
            running += 1
        if inst["status"] == "stopped":
            stopped += 1

        m = inst.get("model", "gpt")
        model_dist[m] = model_dist.get(m, 0) + 1
        p = inst.get("provider", "wangsu")
        provider_dist[p] = provider_dist.get(p, 0) + 1
        pfx = inst.get("prefix", "s1")
        prefix_dist[pfx] = prefix_dist.get(pfx, 0) + 1
        g = inst.get("deploy_group", "stable")
        group_dist[g] = group_dist.get(g, 0) + 1

    return {
        "total_instances": total,
        "running_pods": running,
        "stopped": stopped,
        "paused": paused,
        "model_distribution": model_dist,
        "provider_distribution": provider_dist,
        "prefix_distribution": prefix_dist,
        "deploy_group_distribution": group_dist,
        "deploy_groups": db.list_deploy_groups(),
        "wave_order": db.get_wave_order(),
        "current_image_tag": db.get_current_image_tag(),
    }


# ── API: K8s Events ──

@app.get("/api/instances/{uid}/events", tags=["instances"])
def api_instance_events(uid: int, limit: int = Query(20)):
    """Get K8s events related to an instance (Pod + PVC)."""
    try:
        v1 = k8s_ops._core()
        events = v1.list_namespaced_event("carher", field_selector=f"involvedObject.name=carher-{uid}")
        items = sorted(events.items, key=lambda e: e.last_timestamp or e.event_time or "", reverse=True)[:limit]
        return [{
            "type": e.type,
            "reason": e.reason,
            "message": e.message,
            "count": e.count,
            "first_seen": str(e.first_timestamp) if e.first_timestamp else "",
            "last_seen": str(e.last_timestamp) if e.last_timestamp else "",
            "source": e.source.component if e.source else "",
        } for e in items]
    except Exception as e:
        return {"error": str(e), "events": []}


# ── API: CRD Direct Query ──

@app.get("/api/crd/instances", tags=["crd"])
def api_crd_list():
    """List all HerInstance CRDs with spec + status (direct K8s API)."""
    try:
        instances = crd_ops.list_her_instances()
        return [{
            "name": i["metadata"]["name"],
            "uid": i["spec"].get("userId"),
            "spec": i.get("spec", {}),
            "status": i.get("status", {}),
        } for i in instances]
    except Exception as e:
        raise HTTPException(500, f"CRD query failed: {e}")


@app.get("/api/crd/instances/{uid}", tags=["crd"])
def api_crd_get(uid: int):
    """Get a single HerInstance CRD (spec + status)."""
    try:
        inst = crd_ops.get_her_instance(uid)
        if not inst:
            raise HTTPException(404, f"CRD her-{uid} not found")
        return {
            "name": inst["metadata"]["name"],
            "spec": inst.get("spec", {}),
            "status": inst.get("status", {}),
            "metadata": {
                "created": inst["metadata"].get("creationTimestamp"),
                "generation": inst["metadata"].get("generation"),
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ── API: Backup ──

@app.post("/api/backup", tags=["system"])
def api_trigger_backup():
    """Manually trigger SQLite backup to NAS."""
    db.backup_to_nas()
    return {"action": "backup", "status": "ok", "path": str(db.BACKUP_DIR)}


# ── API: Pod Exec (debug) ──

EXEC_ALLOWED_PREFIXES = [
    "ls", "cat ", "head ", "tail ", "grep ", "wc ", "df ", "du ",
    "ps ", "uptime", "env", "echo ", "test ", "stat ", "find ",
    "node --version", "npm --version", "openclaw ",
]


@app.post("/api/instances/{uid}/exec", tags=["instances"], dependencies=[Depends(verify_api_key)])
def api_pod_exec(uid: int, body: dict):
    """Execute a whitelisted command inside an instance's Pod (for debugging). Returns stdout."""
    command = body.get("command", "").strip()
    if not command:
        raise HTTPException(400, "command is required")
    if len(command) > 500:
        raise HTTPException(400, "command too long (max 500 chars)")
    if not any(command.startswith(pfx) or command == pfx.strip() for pfx in EXEC_ALLOWED_PREFIXES):
        raise HTTPException(403, f"Command not in allowlist. Allowed prefixes: {', '.join(p.strip() for p in EXEC_ALLOWED_PREFIXES)}")
    try:
        from kubernetes.stream import stream as k8s_stream
        v1 = k8s_ops._core()
        pod_name = k8s_ops._find_pod(uid)
        if not pod_name:
            raise HTTPException(404, f"No running pod found for carher-{uid}")
        resp = k8s_stream(
            v1.connect_get_namespaced_pod_exec,
            pod_name, "carher",
            container="carher",
            command=["/bin/sh", "-c", command],
            stderr=True, stdout=True, stdin=False, tty=False,
        )
        return {"id": uid, "command": command, "output": resp}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Exec failed: {e}")


# ── API: AI Agent ──

@app.post("/api/agent", tags=["agent"], response_model=AgentResponse, dependencies=[Depends(verify_api_key)])
async def api_agent(req: AgentRequest):
    """Natural language operations agent. Understands Chinese and English.

    Examples:
      - "重启所有飞书断连的实例"
      - "查看金丝雀组有哪些实例"
      - "把用户 14 移到 VIP 组"
      - "分析 carher-25 的日志，找出错误原因"
      - "当前集群状态怎么样"
      - "部署 v20260329 镜像到金丝雀组"
    """
    from . import agent
    return await agent.handle_message(req.message, context=req.context, dry_run=req.dry_run)


@app.get("/api/agent/capabilities", tags=["agent"])
def api_agent_capabilities():
    """List all capabilities the AI agent can perform."""
    return {
        "description": "CarHer AI operations agent. Accepts natural language (Chinese/English).",
        "capabilities": [
            {"name": "query", "examples": ["当前集群状态", "查看实例 14 详情", "有哪些飞书断连的"]},
            {"name": "lifecycle", "examples": ["重启实例 25", "停止所有 Failed 的实例", "启动 carher-14"]},
            {"name": "deploy", "examples": ["部署 v20260329 到金丝雀组", "查看当前部署状态"]},
            {"name": "group", "examples": ["把 14 移到 VIP 组", "创建 test 分组 优先级 5"]},
            {"name": "diagnose", "examples": ["分析 carher-25 的日志", "为什么 14 号飞书断连了"]},
            {"name": "stats", "examples": ["当前有多少实例在运行", "各模型使用分布"]},
        ],
        "model": os.environ.get("AGENT_MODEL", "gpt-4o"),
        "endpoint": "POST /api/agent",
    }


# ── Serve frontend ──

if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def serve_frontend(full_path: str):
        file = STATIC_DIR / full_path
        if file.exists() and file.is_file():
            return FileResponse(file)
        return FileResponse(STATIC_DIR / "index.html")
