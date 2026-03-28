"""CarHer Admin Dashboard — FastAPI backend."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import k8s_ops
from .models import HerAddRequest, HerBatchAction, HerBatchImport, HerUpdateRequest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("carher-admin")

STATIC_DIR = Path(__file__).parent.parent / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    k8s_ops.init_k8s()
    logger.info("CarHer Admin started")
    yield

app = FastAPI(title="CarHer Admin", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── API Routes ──

@app.get("/api/instances")
def api_list_instances():
    return k8s_ops.list_instances()


@app.get("/api/instances/{uid}")
def api_get_instance(uid: int):
    return k8s_ops.get_instance(uid)


@app.post("/api/instances")
def api_add_instance(req: HerAddRequest):
    uid = req.id or k8s_ops.next_available_id()
    bots, bot_ids = k8s_ops.collect_known_bots()
    return k8s_ops.add_instance(
        uid=uid, name=req.name, model_short_name=req.model,
        app_id=req.app_id, app_secret=req.app_secret,
        prefix=req.prefix, owner=req.owner, provider=req.provider,
        known_bots=bots, known_bot_open_ids=bot_ids,
    )


@app.post("/api/instances/batch-import")
def api_batch_import(req: HerBatchImport):
    bots, bot_ids = k8s_ops.collect_known_bots()
    results = []
    for inst in req.instances:
        uid = inst.id or k8s_ops.next_available_id()
        try:
            r = k8s_ops.add_instance(
                uid=uid, name=inst.name, model_short_name=inst.model,
                app_id=inst.app_id, app_secret=inst.app_secret,
                prefix=inst.prefix, owner=inst.owner, provider=inst.provider,
                known_bots=bots, known_bot_open_ids=bot_ids,
            )
            # Update bots for next instance
            if inst.app_id and inst.name:
                bots[inst.app_id] = inst.name
            results.append(r)
        except Exception as e:
            results.append({"id": uid, "error": str(e)})
    return {"results": results}


@app.post("/api/instances/batch")
def api_batch_action(req: HerBatchAction):
    results = []
    for uid in req.ids:
        try:
            if req.action == "stop":
                results.append(k8s_ops.stop_instance(uid))
            elif req.action == "start":
                results.append(k8s_ops.start_instance(uid))
            elif req.action == "restart":
                results.append(k8s_ops.restart_instance(uid))
            elif req.action == "delete":
                purge = req.params and req.params.image == "purge"
                results.append(k8s_ops.delete_instance(uid, purge=bool(purge)))
            elif req.action == "update":
                if req.params:
                    results.append(k8s_ops.update_instance(uid, model=req.params.model, owner=req.params.owner))
            else:
                results.append({"id": uid, "error": f"Unknown action: {req.action}"})
        except Exception as e:
            results.append({"id": uid, "error": str(e)})
    return {"results": results}


@app.post("/api/instances/{uid}/stop")
def api_stop(uid: int):
    return k8s_ops.stop_instance(uid)


@app.post("/api/instances/{uid}/start")
def api_start(uid: int):
    return k8s_ops.start_instance(uid)


@app.post("/api/instances/{uid}/restart")
def api_restart(uid: int):
    return k8s_ops.restart_instance(uid)


@app.put("/api/instances/{uid}")
def api_update(uid: int, req: HerUpdateRequest):
    return k8s_ops.update_instance(uid, model=req.model, owner=req.owner)


@app.delete("/api/instances/{uid}")
def api_delete(uid: int, purge: bool = Query(False)):
    return k8s_ops.delete_instance(uid, purge=purge)


@app.get("/api/instances/{uid}/logs")
def api_logs(uid: int, tail: int = Query(200)):
    return {"logs": k8s_ops.get_logs(uid, tail=tail)}


@app.get("/api/status")
def api_status():
    return k8s_ops.cluster_status()


@app.get("/api/health")
def api_health():
    return k8s_ops.health_check()


@app.get("/api/next-id")
def api_next_id():
    return {"next_id": k8s_ops.next_available_id()}


# ── Serve frontend ──

if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def serve_frontend(full_path: str):
        file = STATIC_DIR / full_path
        if file.exists() and file.is_file():
            return FileResponse(file)
        return FileResponse(STATIC_DIR / "index.html")
