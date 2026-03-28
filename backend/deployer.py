"""Deploy orchestrator: canary → early → stable with health gates.

Supports two modes:
  1. Operator mode (default): updates CRD spec.image, operator handles Pod recreation
  2. Legacy mode: directly manages Pods via k8s_ops (fallback if CRD not installed)

Deploy flows:
  - normal:    canary → health gate → early → health gate → stable
  - fast:      all instances at once (skip canary gates, for hotfixes)
  - canary-only: deploy to canary group only (manual promotion later)

Deploy statuses: pending → canary → rolling → complete | paused | failed | rolled_back
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any

from . import database as db

logger = logging.getLogger("carher-admin")

WAVE_ORDER = ["canary", "early", "stable"]
BATCH_SIZE = 10

# Configurable via env
HEALTH_WAIT_CANARY = int(os.environ.get("DEPLOY_HEALTH_WAIT_CANARY", "30"))
HEALTH_WAIT_DEFAULT = int(os.environ.get("DEPLOY_HEALTH_WAIT", "15"))

_active_task: asyncio.Task | None = None

_USE_CRD = True


def _get_backend():
    """Return crd_ops if operator mode, else k8s_ops."""
    if _USE_CRD:
        try:
            from . import crd_ops
            return crd_ops
        except ImportError:
            pass
    from . import k8s_ops
    return k8s_ops


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ──────────────────────────────────────
# Start deploy
# ──────────────────────────────────────

async def start_deploy(image_tag: str, mode: str = "normal") -> dict:
    """Initiate a new deploy.

    mode: "normal" (canary→early→stable), "fast" (all at once), "canary-only"
    """
    global _active_task

    # Idempotency: if same tag already active or last completed, skip
    active = db.get_active_deploy()
    if active:
        if active["image_tag"] == image_tag:
            return {"status": "already_deploying", "deploy": active}
        return {"error": f"Deploy #{active['id']} in progress ({active['status']}). Abort it first."}

    last = db.list_deploys(limit=1)
    if last and last[0]["image_tag"] == image_tag and last[0]["status"] == "complete":
        return {"status": "already_deployed", "image_tag": image_tag}

    prev_tag = db.get_current_image_tag()
    all_running = [i for i in db.list_all() if i["status"] == "running"]
    total = len(all_running)

    if total == 0:
        return {"error": "No running instances to deploy"}

    deploy_id = db.create_deploy(image_tag, prev_tag, total)
    logger.info("Deploy #%d: %s → %s (%d instances, mode=%s)", deploy_id, prev_tag, image_tag, total, mode)

    _active_task = asyncio.create_task(_run_deploy(deploy_id, image_tag, mode))
    return db.get_deploy(deploy_id)


async def _run_deploy(deploy_id: int, image_tag: str, mode: str):
    """Execute the deploy pipeline."""
    try:
        if mode == "fast":
            await _deploy_fast(deploy_id, image_tag)
        elif mode == "canary-only":
            await _deploy_wave(deploy_id, image_tag, ["canary"])
        else:
            await _deploy_wave(deploy_id, image_tag, WAVE_ORDER)

    except asyncio.CancelledError:
        logger.info("Deploy #%d: cancelled", deploy_id)
        db.update_deploy(deploy_id, status="failed", error="Cancelled", completed_at=_now())
    except Exception as e:
        logger.error("Deploy #%d: unexpected error: %s", deploy_id, e)
        db.update_deploy(deploy_id, status="failed", error=str(e), completed_at=_now())
        await _notify_deploy_event(deploy_id, "failed", str(e))


async def _deploy_fast(deploy_id: int, image_tag: str):
    """Deploy all running instances at once, skip canary gates."""
    db.update_deploy(deploy_id, status="rolling", current_wave="all")
    all_running = [i for i in db.list_all() if i["status"] == "running"]

    for batch_start in range(0, len(all_running), BATCH_SIZE):
        batch = all_running[batch_start:batch_start + BATCH_SIZE]
        await _deploy_batch(deploy_id, batch, image_tag)

    db.update_deploy(deploy_id, status="complete", completed_at=_now(), current_wave="")
    await _notify_deploy_event(deploy_id, "complete", f"Fast deploy: {len(all_running)} instances → {image_tag}")


async def _deploy_wave(deploy_id: int, image_tag: str, waves: list[str]):
    """Deploy by waves with health gates between each."""
    for wave in waves:
        instances = db.list_by_deploy_group(wave)
        if not instances:
            continue

        db.update_deploy(deploy_id, status="canary" if wave == "canary" else "rolling", current_wave=wave)
        logger.info("Deploy #%d: wave '%s' (%d instances)", deploy_id, wave, len(instances))

        for batch_start in range(0, len(instances), BATCH_SIZE):
            batch = instances[batch_start:batch_start + BATCH_SIZE]

            # Abort check
            deploy = db.get_deploy(deploy_id)
            if deploy and deploy["status"] in ("paused", "failed", "rolled_back"):
                return

            await _deploy_batch(deploy_id, batch, image_tag)

            # Health gate
            wait = HEALTH_WAIT_CANARY if wave == "canary" else HEALTH_WAIT_DEFAULT
            await asyncio.sleep(wait)
            failures = await _health_check_batch(batch)

            if failures:
                fail_ids = [f["id"] for f in failures]
                logger.warning("Deploy #%d: health check failed: %s", deploy_id, fail_ids)
                db.update_deploy(
                    deploy_id, status="paused",
                    error=f"Health failed: {fail_ids}. /api/deploy/continue or /api/deploy/rollback",
                )
                await _notify_deploy_event(deploy_id, "paused", f"Health check failed: {fail_ids}")
                return

        await _notify_deploy_event(deploy_id, f"wave_{wave}_done", f"{len(instances)} updated")

    db.update_deploy(deploy_id, status="complete", completed_at=_now(), current_wave="")
    await _notify_deploy_event(deploy_id, "complete", f"All waves done → {image_tag}")


async def _deploy_batch(deploy_id: int, batch: list[dict], image_tag: str):
    """Deploy a batch of instances. Uses CRD or direct k8s_ops."""
    backend = _get_backend()
    loop = asyncio.get_event_loop()

    for inst in batch:
        uid = inst["id"]
        try:
            if _USE_CRD:
                # Operator mode: just update CRD spec.image → operator reconciles
                await loop.run_in_executor(None, backend.set_image, uid, image_tag)
            else:
                # Legacy: direct Pod management
                from . import config_gen
                config_json = config_gen.generate_json_string(inst)
                await loop.run_in_executor(None, backend.apply_configmap, uid, config_json)
                await loop.run_in_executor(None, backend.delete_pod, uid)
                await asyncio.sleep(2)
                prefix = inst.get("prefix", "s1")
                await loop.run_in_executor(None, backend.create_pod, uid, prefix, image_tag)

            db.set_image_tag(uid, image_tag)
            # Atomic increment
            with db.get_db() as conn:
                conn.execute("UPDATE deploys SET done = done + 1 WHERE id = ?", (deploy_id,))
        except Exception as e:
            logger.error("Deploy #%d: failed carher-%d: %s", deploy_id, uid, e)
            with db.get_db() as conn:
                conn.execute("UPDATE deploys SET failed = failed + 1 WHERE id = ?", (deploy_id,))


async def _health_check_batch(batch: list[dict]) -> list[dict]:
    """Check health. Returns list of unhealthy instances."""
    backend = _get_backend()
    loop = asyncio.get_event_loop()
    failures = []

    for inst in batch:
        uid = inst["id"]
        try:
            if _USE_CRD:
                status = await loop.run_in_executor(None, backend.get_instance_status, uid)
                phase = status.get("phase", "Unknown")
                if phase not in ("Running", "Pending"):
                    failures.append({"id": uid, "reason": f"Phase: {phase}"})
                elif status.get("feishuWS") == "Disconnected":
                    failures.append({"id": uid, "reason": "Feishu WS disconnected"})
            else:
                from . import k8s_ops
                pod = await loop.run_in_executor(None, k8s_ops.get_pod_status, uid)
                if not pod.get("pod_exists") or pod.get("phase") != "Running":
                    failures.append({"id": uid, "reason": f"Pod: {pod.get('phase', '?')}"})
                    continue
                health = await loop.run_in_executor(None, k8s_ops.check_pod_health, uid)
                if not health["feishu_ws"]:
                    failures.append({"id": uid, "reason": "Feishu WS not connected"})
        except Exception as e:
            failures.append({"id": uid, "reason": str(e)})

    return failures


# ──────────────────────────────────────
# Control: continue / rollback / abort
# ──────────────────────────────────────

async def continue_deploy() -> dict:
    global _active_task
    deploy = db.get_active_deploy()
    if not deploy:
        return {"error": "No active deploy"}
    if deploy["status"] != "paused":
        return {"error": f"Deploy is {deploy['status']}, not paused"}

    db.update_deploy(deploy["id"], status="rolling", error="")
    _active_task = asyncio.create_task(_run_deploy(deploy["id"], deploy["image_tag"], "normal"))
    return db.get_deploy(deploy["id"])


async def rollback_deploy() -> dict:
    """Rollback: revert updated instances to previous image."""
    deploy = db.get_active_deploy()
    if not deploy:
        return {"error": "No active deploy to rollback"}

    prev_tag = deploy["prev_image_tag"]
    if not prev_tag:
        return {"error": "No previous image tag recorded"}

    db.update_deploy(deploy["id"], status="rolled_back", completed_at=_now())
    logger.info("Deploy #%d: rollback → %s", deploy["id"], prev_tag)

    backend = _get_backend()
    loop = asyncio.get_event_loop()
    all_instances = db.list_all()
    rolled = 0

    for inst in all_instances:
        if inst["image_tag"] == deploy["image_tag"] and inst["status"] == "running":
            try:
                uid = inst["id"]
                if _USE_CRD:
                    await loop.run_in_executor(None, backend.set_image, uid, prev_tag)
                else:
                    from . import config_gen, k8s_ops
                    config_json = config_gen.generate_json_string(inst)
                    await loop.run_in_executor(None, k8s_ops.apply_configmap, uid, config_json)
                    await loop.run_in_executor(None, k8s_ops.delete_pod, uid)
                    await asyncio.sleep(2)
                    await loop.run_in_executor(None, k8s_ops.create_pod, uid, inst.get("prefix", "s1"), prev_tag)
                db.set_image_tag(uid, prev_tag)
                rolled += 1
            except Exception as e:
                logger.error("Rollback failed carher-%d: %s", inst["id"], e)

    await _notify_deploy_event(deploy["id"], "rolled_back", f"Reverted {rolled} → {prev_tag}")
    return {"action": "rolled_back", "reverted": rolled, "to": prev_tag}


def abort_deploy() -> dict:
    deploy = db.get_active_deploy()
    if not deploy:
        return {"error": "No active deploy"}
    db.update_deploy(deploy["id"], status="failed", error="Manually aborted", completed_at=_now())
    global _active_task
    if _active_task and not _active_task.done():
        _active_task.cancel()
    return {"action": "aborted", "deploy_id": deploy["id"]}


def get_deploy_status() -> dict:
    deploy = db.get_active_deploy()
    if not deploy:
        last = db.list_deploys(limit=1)
        return {"active": False, "last": last[0] if last else None}

    total = deploy["total"]
    done = deploy["done"]
    pct = round(done / total * 100) if total > 0 else 0
    return {
        "active": True,
        "deploy": deploy,
        "progress_pct": pct,
        "waves": {g: len(db.list_by_deploy_group(g)) for g in WAVE_ORDER},
    }


# ──────────────────────────────────────
# Notifications (Feishu webhook)
# ──────────────────────────────────────

_FEISHU_WEBHOOK_URL = os.environ.get("FEISHU_DEPLOY_WEBHOOK", "")


async def _notify_deploy_event(deploy_id: int, event: str, detail: str):
    if not _FEISHU_WEBHOOK_URL:
        return
    try:
        import aiohttp
        deploy = db.get_deploy(deploy_id)
        tag = deploy["image_tag"] if deploy else "?"
        emoji = {"complete": "✅", "paused": "⚠️", "failed": "❌", "rolled_back": "🔄"}.get(event, "📦")

        card = {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": f"{emoji} CarHer Deploy #{deploy_id}"}},
                "elements": [
                    {"tag": "div", "text": {"tag": "plain_text", "content": f"镜像: {tag}\n事件: {event}\n详情: {detail}"}},
                    {"tag": "div", "text": {"tag": "plain_text", "content": f"进度: {deploy['done']}/{deploy['total']}" if deploy else ""}},
                ],
            },
        }
        async with aiohttp.ClientSession() as session:
            await session.post(_FEISHU_WEBHOOK_URL, json=card, timeout=aiohttp.ClientTimeout(total=10))
    except Exception as e:
        logger.warning("Feishu notify failed: %s", e)
