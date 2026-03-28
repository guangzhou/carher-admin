"""CarHer Admin Dashboard — FastAPI backend.

Architecture:
  DB (SQLite) → source of truth for user registry
  config_gen  → DB row → openclaw.json
  k8s_ops     → ConfigMap / Pod / PVC lifecycle
  sync_worker → background retry + consistency check
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import database as db
from . import config_gen
from . import k8s_ops
from . import sync_worker
from . import deployer
from .models import HerAddRequest, HerBatchAction, HerBatchImport, HerUpdateRequest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("carher-admin")

STATIC_DIR = Path(__file__).parent.parent / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    k8s_ops.init_k8s()
    db.init_db()
    await sync_worker.start_workers()
    logger.info("CarHer Admin started (DB + K8s + sync workers)")
    yield

app = FastAPI(title="CarHer Admin", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Helpers ──

def _model_short(full: str) -> str:
    parts = full.rsplit("/", 1)
    return parts[-1] if parts else full


def _enrich_with_runtime(instance: dict) -> dict:
    """Merge DB data with live K8s pod status."""
    uid = instance["id"]
    pod = k8s_ops.get_pod_status(uid)

    prefix = instance.get("prefix", "s1")
    pfx = f"{prefix}-" if not prefix.endswith("-") else prefix
    model_map = config_gen.MODEL_MAP_ANTHROPIC if instance.get("provider") == "anthropic" else config_gen.MODEL_MAP
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
        "provider": instance.get("provider", "openrouter"),
        "sync_status": instance.get("sync_status", ""),
    }


def _sync_and_deploy(instance: dict):
    """Generate config → write ConfigMap → create Pod."""
    uid = instance["id"]
    config_json = config_gen.generate_json_string(instance)
    k8s_ops.apply_configmap(uid, config_json)
    db.set_sync_status(uid, "synced")


# ── API: List / Get ──

@app.get("/api/instances")
def api_list_instances():
    instances = db.list_all()
    pod_statuses = k8s_ops.get_all_pod_statuses()

    results = []
    for inst in instances:
        if inst["status"] == "deleted":
            continue
        uid = inst["id"]
        pod = pod_statuses.get(uid, {})

        prefix = inst.get("prefix", "s1")
        pfx = f"{prefix}-" if not prefix.endswith("-") else prefix
        mm = config_gen.MODEL_MAP_ANTHROPIC if inst.get("provider") == "anthropic" else config_gen.MODEL_MAP
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
        })
    return results


@app.get("/api/instances/{uid}")
def api_get_instance(uid: int):
    inst = db.get_by_id(uid)
    if not inst:
        raise HTTPException(404, f"Instance {uid} not found")
    result = _enrich_with_runtime(inst)
    result["pvc_status"] = k8s_ops.get_pvc_status(uid)
    result["known_bots_count"] = len(db.collect_known_bots()[0])
    return result


# ── API: Create ──

@app.post("/api/instances")
def api_add_instance(req: HerAddRequest):
    uid = req.id or db.next_id()

    # Insert into DB
    data = {
        "id": uid, "name": req.name, "model": req.model,
        "app_id": req.app_id, "app_secret": req.app_secret,
        "prefix": req.prefix, "owner": req.owner,
        "provider": req.provider, "bot_open_id": "",
        "status": "running",
    }
    inst = db.insert(data)

    # Deploy to K8s
    try:
        k8s_ops.ensure_pvc(uid)
        _sync_and_deploy(inst)
        k8s_ops.create_pod(uid, prefix=req.prefix)
    except Exception as e:
        logger.error("K8s deploy failed for %d: %s", uid, e)
        db.set_sync_status(uid, "pending")

    pfx = f"{req.prefix}-" if not req.prefix.endswith("-") else req.prefix
    return {
        "id": uid, "status": "created",
        "oauth_url": f"https://{pfx}u{uid}-auth.carher.net/feishu/oauth/callback",
    }


@app.post("/api/instances/batch-import")
def api_batch_import(req: HerBatchImport):
    results = []
    for item in req.instances:
        uid = item.id or db.next_id()
        try:
            data = {
                "id": uid, "name": item.name, "model": item.model,
                "app_id": item.app_id, "app_secret": item.app_secret,
                "prefix": item.prefix, "owner": item.owner,
                "provider": item.provider, "bot_open_id": "",
                "status": "running",
            }
            inst = db.insert(data)
            k8s_ops.ensure_pvc(uid)
            _sync_and_deploy(inst)
            k8s_ops.create_pod(uid, prefix=item.prefix)
            pfx = f"{item.prefix}-" if not item.prefix.endswith("-") else item.prefix
            results.append({
                "id": uid, "status": "created",
                "oauth_url": f"https://{pfx}u{uid}-auth.carher.net/feishu/oauth/callback",
            })
        except Exception as e:
            results.append({"id": uid, "error": str(e)})
    return {"results": results}


# ── API: Update ──

@app.put("/api/instances/{uid}")
def api_update(uid: int, req: HerUpdateRequest):
    inst = db.get_by_id(uid)
    if not inst:
        raise HTTPException(404, f"Instance {uid} not found")

    changes = {}
    if req.model is not None:
        changes["model"] = req.model
    if req.owner is not None:
        changes["owner"] = req.owner

    if changes:
        inst = db.update(uid, changes)
        try:
            _sync_and_deploy(inst)
        except Exception as e:
            logger.warning("ConfigMap sync failed for %d after update: %s", uid, e)

    return {"id": uid, "action": "updated", "needs_restart": True}


# ── API: Lifecycle ──

@app.post("/api/instances/{uid}/stop")
def api_stop(uid: int):
    k8s_ops.delete_pod(uid)
    db.set_status(uid, "stopped")
    return {"id": uid, "action": "stopped"}


@app.post("/api/instances/{uid}/start")
def api_start(uid: int):
    inst = db.get_by_id(uid)
    if not inst:
        raise HTTPException(404, f"Instance {uid} not found")
    _sync_and_deploy(inst)
    k8s_ops.create_pod(uid, prefix=inst.get("prefix", "s1"))
    db.set_status(uid, "running")
    return {"id": uid, "action": "started"}


@app.post("/api/instances/{uid}/restart")
def api_restart(uid: int):
    inst = db.get_by_id(uid)
    if not inst:
        raise HTTPException(404, f"Instance {uid} not found")
    k8s_ops.delete_pod(uid)
    import time; time.sleep(2)
    _sync_and_deploy(inst)
    k8s_ops.create_pod(uid, prefix=inst.get("prefix", "s1"))
    return {"id": uid, "action": "restarted"}


@app.delete("/api/instances/{uid}")
def api_delete(uid: int, purge: bool = Query(False)):
    k8s_ops.delete_pod(uid)
    k8s_ops.delete_configmap(uid)
    if purge:
        k8s_ops.delete_pvc(uid)
        db.purge_instance(uid)
    else:
        db.delete_instance(uid)
    return {"id": uid, "action": "deleted", "purge": purge}


# ── API: Batch ──

@app.post("/api/instances/batch")
def api_batch_action(req: HerBatchAction):
    results = []
    for uid in req.ids:
        try:
            if req.action == "stop":
                results.append(api_stop(uid))
            elif req.action == "start":
                results.append(api_start(uid))
            elif req.action == "restart":
                results.append(api_restart(uid))
            elif req.action == "delete":
                purge = req.params and req.params.image == "purge"
                results.append(api_delete(uid, purge=bool(purge)))
            elif req.action == "update":
                if req.params:
                    results.append(api_update(uid, req.params))
            else:
                results.append({"id": uid, "error": f"Unknown action: {req.action}"})
        except Exception as e:
            results.append({"id": uid, "error": str(e)})
    return {"results": results}


# ── API: Logs ──

@app.get("/api/instances/{uid}/logs")
def api_logs(uid: int, tail: int = Query(200)):
    return {"logs": k8s_ops.get_logs(uid, tail=tail)}


# ── API: Cluster status / Health / Sync ──

@app.get("/api/status")
def api_status():
    k8s_status = k8s_ops.cluster_status()
    db_counts = db.list_all()
    stopped = sum(1 for i in db_counts if i["status"] == "stopped")
    k8s_status["stopped"] = stopped
    return k8s_status


@app.get("/api/health")
def api_health():
    instances = db.list_all()
    pod_statuses = k8s_ops.get_all_pod_statuses()
    results = []
    for inst in instances:
        uid = inst["id"]
        if uid not in pod_statuses or inst["status"] == "deleted":
            continue
        health = k8s_ops.check_pod_health(uid)
        results.append({
            "id": uid,
            "name": inst.get("name", ""),
            "feishu_ws": health["feishu_ws"],
            "memory_db": health["memory_db"],
            "model_ok": health["model_ok"],
            "status": pod_statuses[uid].get("phase", "?"),
        })
    return sorted(results, key=lambda x: x["id"])


@app.get("/api/next-id")
def api_next_id():
    return {"next_id": db.next_id()}


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


# ── API: Deploy Pipeline ──

@app.post("/api/deploy")
async def api_start_deploy(body: dict):
    image_tag = body.get("image_tag")
    mode = body.get("mode", "normal")
    if not image_tag:
        raise HTTPException(400, "image_tag required")
    if mode not in ("normal", "fast", "canary-only"):
        raise HTTPException(400, "mode must be normal, fast, or canary-only")
    return await deployer.start_deploy(image_tag, mode=mode)


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


@app.put("/api/instances/{uid}/deploy-group")
def api_set_deploy_group(uid: int, body: dict):
    group = body.get("group")
    if group not in ("canary", "early", "stable"):
        raise HTTPException(400, "group must be canary, early, or stable")
    db.set_deploy_group(uid, group)
    return {"id": uid, "deploy_group": group}


@app.post("/api/deploy/webhook")
async def api_deploy_webhook(body: dict):
    """GitHub Actions webhook: auto-trigger deploy after image push."""
    secret = body.get("secret", "")
    expected = os.environ.get("DEPLOY_WEBHOOK_SECRET", "")
    if not expected:
        raise HTTPException(503, "DEPLOY_WEBHOOK_SECRET not configured")
    if secret != expected:
        raise HTTPException(403, "Invalid webhook secret")

    image_tag = body.get("image_tag")
    if not image_tag:
        raise HTTPException(400, "image_tag required")

    mode = body.get("mode", "normal")
    return await deployer.start_deploy(image_tag, mode=mode)


# ── Serve frontend ──

if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def serve_frontend(full_path: str):
        file = STATIC_DIR / full_path
        if file.exists() and file.is_file():
            return FileResponse(file)
        return FileResponse(STATIC_DIR / "index.html")
