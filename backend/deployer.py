"""Deploy orchestrator: canary → early → stable with health gates.

Supports two modes:
  1. Operator mode (default): updates CRD spec.image, operator handles Pod recreation
  2. Legacy mode: directly manages Pods via k8s_ops (fallback if CRD not installed)

Deploy flows:
  - normal:      canary → health gate → early → health gate → stable
  - fast:        all instances at once (skip canary gates, for hotfixes)
  - canary-only: deploy to canary group only (manual promotion later)
  - group:<name>: deploy to a specific named group only

Deploy statuses: pending → canary → rolling → complete | paused | failed | rolled_back

Instance sources: merges CRD-managed + DB-managed instances.
CRD instances take priority; DB instances are included only if not already in CRD.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any

from . import database as db

logger = logging.getLogger("carher-admin")

BATCH_SIZE = int(os.environ.get("DEPLOY_BATCH_SIZE", "50"))

HEALTH_WAIT_CANARY = int(os.environ.get("DEPLOY_HEALTH_WAIT_CANARY", "30"))
HEALTH_WAIT_DEFAULT = int(os.environ.get("DEPLOY_HEALTH_WAIT", "15"))

_active_task: asyncio.Task | None = None

_USE_CRD = os.environ.get("DEPLOY_USE_CRD", "true").lower() not in ("false", "0", "no")


def _get_crd_ops():
    if _USE_CRD:
        try:
            from . import crd_ops
            return crd_ops
        except ImportError:
            pass
    return None


def _get_k8s_ops():
    from . import k8s_ops
    return k8s_ops


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ──────────────────────────────────────
# Unified instance listing (CRD + DB)
# ──────────────────────────────────────

def _list_all_deployable() -> list[dict]:
    """Merge CRD-managed and DB-managed running instances.
    Each entry has at least: id, deploy_group, image_tag, source ('crd'|'db')."""
    result = []
    seen_uids: set[int] = set()

    crd_ops = _get_crd_ops()
    if crd_ops:
        try:
            for inst in crd_ops.list_her_instances():
                spec = inst.get("spec", {})
                uid = spec.get("userId", 0)
                if not uid or spec.get("paused"):
                    continue
                seen_uids.add(uid)
                result.append({
                    "id": uid,
                    "deploy_group": spec.get("deployGroup", "stable"),
                    "image_tag": spec.get("image", ""),
                    "prefix": spec.get("prefix", "s1"),
                    "source": "crd",
                })
        except Exception as e:
            logger.warning("CRD list failed in deployer: %s", e)

    for inst in db.list_all():
        if inst["id"] in seen_uids:
            continue
        if inst["status"] != "running":
            continue
        result.append({
            "id": inst["id"],
            "deploy_group": inst.get("deploy_group", "stable"),
            "image_tag": inst.get("image_tag", ""),
            "prefix": inst.get("prefix", "s1"),
            "source": "db",
        })

    return result


def _list_by_group(group: str) -> list[dict]:
    """List deployable instances in a specific deploy group."""
    return [i for i in _list_all_deployable() if i["deploy_group"] == group]


# ──────────────────────────────────────
# Start deploy
# ──────────────────────────────────────

async def start_deploy(image_tag: str, mode: str = "normal", force: bool = False) -> dict:
    """Initiate a new deploy.

    mode: "normal", "fast", "canary-only", or "group:<name>" for targeted deploy
    force: if True, skip the already_deployed check
    """
    global _active_task

    active = db.get_active_deploy()
    if active:
        if active["image_tag"] == image_tag:
            return {"status": "already_deploying", "deploy": active}
        return {"error": f"Deploy #{active['id']} in progress ({active['status']}). Abort it first."}

    if not force:
        last = db.list_deploys(limit=1)
        if last and last[0]["image_tag"] == image_tag and last[0]["status"] == "complete":
            return {"status": "already_deployed", "image_tag": image_tag}

    all_instances = _list_all_deployable()
    if mode.startswith("group:"):
        target_group = mode[6:]
        all_instances = [i for i in all_instances if i["deploy_group"] == target_group]

    total = len(all_instances)
    if total == 0:
        return {"error": "No running instances to deploy"}

    prev_tag = _get_current_image_tag(all_instances)
    deploy_id = db.create_deploy(image_tag, prev_tag, total, mode=mode)
    logger.info("Deploy #%d: %s → %s (%d instances, mode=%s)", deploy_id, prev_tag, image_tag, total, mode)

    _active_task = asyncio.create_task(_run_deploy(deploy_id, image_tag, mode))
    return db.get_deploy(deploy_id)


def _get_current_image_tag(instances: list[dict]) -> str:
    """Most common image tag across given instances."""
    counts: dict[str, int] = {}
    for i in instances:
        tag = i.get("image_tag", "")
        if tag:
            counts[tag] = counts.get(tag, 0) + 1
    if not counts:
        return db.get_current_image_tag()
    return max(counts, key=counts.get)


async def _run_deploy(deploy_id: int, image_tag: str, mode: str):
    """Execute the deploy pipeline."""
    try:
        wave_order = db.get_wave_order() or ["canary", "early", "stable"]

        if mode == "fast":
            await _deploy_fast(deploy_id, image_tag)
        elif mode == "canary-only":
            await _deploy_wave(deploy_id, image_tag, [wave_order[0]])
        elif mode.startswith("group:"):
            target_group = mode[6:]
            await _deploy_wave(deploy_id, image_tag, [target_group])
        else:
            await _deploy_wave(deploy_id, image_tag, wave_order)

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
    all_running = _list_all_deployable()

    for batch_start in range(0, len(all_running), BATCH_SIZE):
        batch = all_running[batch_start:batch_start + BATCH_SIZE]
        await _deploy_batch(deploy_id, batch, image_tag)

    db.update_deploy(deploy_id, status="complete", completed_at=_now(), current_wave="")
    await _notify_deploy_event(deploy_id, "complete", f"Fast deploy: {len(all_running)} instances → {image_tag}")


async def _deploy_wave(deploy_id: int, image_tag: str, waves: list[str]):
    """Deploy by waves with health gates between each."""
    for wave in waves:
        instances = _list_by_group(wave)
        if not instances:
            continue

        db.update_deploy(deploy_id, status="canary" if wave == waves[0] else "rolling", current_wave=wave)
        logger.info("Deploy #%d: wave '%s' (%d instances)", deploy_id, wave, len(instances))

        for batch_start in range(0, len(instances), BATCH_SIZE):
            batch = instances[batch_start:batch_start + BATCH_SIZE]

            deploy = db.get_deploy(deploy_id)
            if deploy and deploy["status"] in ("paused", "failed", "rolled_back"):
                return

            await _deploy_batch(deploy_id, batch, image_tag)

            wait = HEALTH_WAIT_CANARY if wave == waves[0] else HEALTH_WAIT_DEFAULT
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
    """Deploy a batch of instances concurrently."""
    crd_ops = _get_crd_ops()
    loop = asyncio.get_running_loop()

    async def _deploy_one(inst: dict):
        uid = inst["id"]
        try:
            if inst["source"] == "crd" and crd_ops:
                await loop.run_in_executor(None, crd_ops.set_image, uid, image_tag)
            elif crd_ops:
                try:
                    await loop.run_in_executor(None, crd_ops.set_image, uid, image_tag)
                except Exception:
                    await _deploy_one_legacy(inst, image_tag, loop)
            else:
                await _deploy_one_legacy(inst, image_tag, loop)

            try:
                db.set_image_tag(uid, image_tag)
            except Exception:
                pass
            with db.get_db() as conn:
                conn.execute("UPDATE deploys SET done = done + 1 WHERE id = ?", (deploy_id,))
        except Exception as e:
            logger.error("Deploy #%d: failed carher-%d: %s", deploy_id, uid, e)
            with db.get_db() as conn:
                conn.execute("UPDATE deploys SET failed = failed + 1 WHERE id = ?", (deploy_id,))

    await asyncio.gather(*[_deploy_one(inst) for inst in batch])


async def _deploy_one_legacy(inst: dict, image_tag: str, loop):
    """Legacy deploy path: ConfigMap + Pod recreation."""
    from . import config_gen, k8s_ops
    uid = inst["id"]
    db_inst = db.get_by_id(uid)
    if not db_inst:
        return
    config_json = config_gen.generate_json_string(db_inst)
    await loop.run_in_executor(None, k8s_ops.apply_configmap, uid, config_json)
    await loop.run_in_executor(None, k8s_ops.delete_pod, uid)
    await asyncio.sleep(2)
    await loop.run_in_executor(None, k8s_ops.create_pod, uid, inst.get("prefix", "s1"), image_tag)


async def _health_check_batch(batch: list[dict]) -> list[dict]:
    """Check health concurrently. Returns list of unhealthy instances."""
    crd_ops = _get_crd_ops()
    loop = asyncio.get_running_loop()

    async def _check_one(inst: dict) -> dict | None:
        uid = inst["id"]
        try:
            if crd_ops:
                status = await loop.run_in_executor(None, crd_ops.get_instance_status, uid)
                phase = status.get("phase", "Unknown")
                if phase not in ("Running", "Pending"):
                    return {"id": uid, "reason": f"Phase: {phase}"}
                if status.get("feishuWS") == "Disconnected":
                    return {"id": uid, "reason": "Feishu WS disconnected"}
            else:
                from . import k8s_ops
                pod = await loop.run_in_executor(None, k8s_ops.get_pod_status, uid)
                if not pod.get("pod_exists") or pod.get("phase") != "Running":
                    return {"id": uid, "reason": f"Pod: {pod.get('phase', '?')}"}
                health = await loop.run_in_executor(None, k8s_ops.check_pod_health, uid)
                if not health["feishu_ws"]:
                    return {"id": uid, "reason": "Feishu WS not connected"}
        except Exception as e:
            return {"id": uid, "reason": str(e)}
        return None

    results = await asyncio.gather(*[_check_one(inst) for inst in batch])
    return [r for r in results if r is not None]


# ──────────────────────────────────────
# Control: continue / rollback / abort
# ──────────────────────────────────────

async def continue_deploy() -> dict:
    """Resume a paused deploy from the wave that caused the pause."""
    global _active_task
    deploy = db.get_active_deploy()
    if not deploy:
        return {"error": "No active deploy"}
    if deploy["status"] != "paused":
        return {"error": f"Deploy is {deploy['status']}, not paused"}

    paused_wave = deploy.get("current_wave", "")
    db.update_deploy(deploy["id"], status="rolling", error="")

    async def _resume_deploy(deploy_id: int, image_tag: str, resume_from: str):
        try:
            wave_order = db.get_wave_order() or ["canary", "early", "stable"]
            start_idx = 0
            if resume_from and resume_from in wave_order:
                start_idx = wave_order.index(resume_from)
            remaining_waves = wave_order[start_idx:]
            await _deploy_wave(deploy_id, image_tag, remaining_waves)
        except asyncio.CancelledError:
            logger.info("Deploy #%d: cancelled", deploy_id)
            db.update_deploy(deploy_id, status="failed", error="Cancelled", completed_at=_now())
        except Exception as e:
            logger.error("Deploy #%d: unexpected error: %s", deploy_id, e)
            db.update_deploy(deploy_id, status="failed", error=str(e), completed_at=_now())
            await _notify_deploy_event(deploy_id, "failed", str(e))

    _active_task = asyncio.create_task(_resume_deploy(deploy["id"], deploy["image_tag"], paused_wave))
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

    crd_ops = _get_crd_ops()
    loop = asyncio.get_running_loop()
    rolled = 0

    all_instances = _list_all_deployable()
    for inst in all_instances:
        if inst["image_tag"] == deploy["image_tag"]:
            try:
                uid = inst["id"]
                if crd_ops:
                    await loop.run_in_executor(None, crd_ops.set_image, uid, prev_tag)
                else:
                    await _deploy_one_legacy(inst, prev_tag, loop)
                try:
                    db.set_image_tag(uid, prev_tag)
                except Exception:
                    pass
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
    wave_order = db.get_wave_order() or ["canary", "early", "stable"]

    group_stats: dict[str, int] = {}
    try:
        crd_ops = _get_crd_ops()
        if crd_ops:
            for inst in crd_ops.list_her_instances():
                spec = inst.get("spec", {})
                if not spec.get("paused"):
                    g = spec.get("deployGroup", "stable")
                    group_stats[g] = group_stats.get(g, 0) + 1
    except Exception:
        pass
    db_stats = db.get_deploy_group_stats()
    for g, c in db_stats.items():
        group_stats[g] = group_stats.get(g, 0) + c

    return {
        "active": True,
        "deploy": deploy,
        "progress_pct": pct,
        "waves": {g: group_stats.get(g, 0) for g in wave_order},
        "wave_order": wave_order,
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
