"""Deploy orchestrator: canary → early → stable with health gates.

Deploy flow:
  1. POST /api/deploy {image_tag: "v20260329"}
  2. Wave 1 (canary): deploy to canary group, wait, health check
  3. Wave 2 (early):  deploy to early group in batches, health check each batch
  4. Wave 3 (stable): deploy to stable group in batches, health check each batch
  5. If any wave fails → pause, notify, allow manual continue/rollback

Deploy statuses: pending → canary → rolling → complete | paused | failed | rolled_back
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from . import database as db
from . import config_gen
from . import k8s_ops

logger = logging.getLogger("carher-admin")

WAVE_ORDER = ["canary", "early", "stable"]
HEALTH_WAIT_SECONDS = 15
BATCH_SIZE = 10

_active_task: asyncio.Task | None = None


async def start_deploy(image_tag: str) -> dict:
    """Initiate a new deploy. Returns deploy record."""
    global _active_task

    active = db.get_active_deploy()
    if active:
        return {"error": f"Deploy #{active['id']} already in progress (status: {active['status']})"}

    prev_tag = db.get_current_image_tag()
    all_running = [i for i in db.list_all() if i["status"] == "running"]
    total = len(all_running)

    if total == 0:
        return {"error": "No running instances to deploy"}

    deploy_id = db.create_deploy(image_tag, prev_tag, total)
    logger.info("Deploy #%d created: %s → %s (%d instances)", deploy_id, prev_tag, image_tag, total)

    _active_task = asyncio.create_task(_run_deploy(deploy_id, image_tag))
    return db.get_deploy(deploy_id)


async def _run_deploy(deploy_id: int, image_tag: str):
    """Execute the deploy pipeline: canary → early → stable."""
    try:
        for wave in WAVE_ORDER:
            instances = db.list_by_deploy_group(wave)
            if not instances:
                continue

            db.update_deploy(deploy_id, status=wave if wave == "canary" else "rolling", current_wave=wave)
            logger.info("Deploy #%d: wave '%s' (%d instances)", deploy_id, wave, len(instances))

            # Process in batches
            for batch_start in range(0, len(instances), BATCH_SIZE):
                batch = instances[batch_start:batch_start + BATCH_SIZE]

                # Check if paused/aborted
                deploy = db.get_deploy(deploy_id)
                if deploy["status"] in ("paused", "failed", "rolled_back"):
                    logger.info("Deploy #%d: stopped (status=%s)", deploy_id, deploy["status"])
                    return

                # Deploy batch
                for inst in batch:
                    try:
                        _deploy_one(inst, image_tag)
                        db.update_deploy(deploy_id, done=db.get_deploy(deploy_id)["done"] + 1)
                    except Exception as e:
                        logger.error("Deploy #%d: failed on carher-%d: %s", deploy_id, inst["id"], e)
                        db.update_deploy(deploy_id, failed=db.get_deploy(deploy_id)["failed"] + 1)

                # Health gate: wait then check
                await asyncio.sleep(HEALTH_WAIT_SECONDS)
                failures = _health_check_batch(batch)

                if failures:
                    fail_ids = [f["id"] for f in failures]
                    logger.warning("Deploy #%d: health check failed for %s, pausing", deploy_id, fail_ids)
                    db.update_deploy(
                        deploy_id, status="paused",
                        error=f"Health check failed: {fail_ids}. Use /api/deploy/continue or /api/deploy/rollback",
                    )
                    await _notify_deploy_event(deploy_id, "paused", f"Health check failed for {fail_ids}")
                    return

            logger.info("Deploy #%d: wave '%s' complete", deploy_id, wave)
            await _notify_deploy_event(deploy_id, f"wave_{wave}_complete", f"{len(instances)} instances updated")

        # All waves complete
        db.update_deploy(deploy_id, status="complete", completed_at=db._now(), current_wave="")
        logger.info("Deploy #%d: complete", deploy_id)
        await _notify_deploy_event(deploy_id, "complete", f"All instances updated to {image_tag}")

    except Exception as e:
        logger.error("Deploy #%d: unexpected error: %s", deploy_id, e)
        db.update_deploy(deploy_id, status="failed", error=str(e))
        await _notify_deploy_event(deploy_id, "failed", str(e))


def _deploy_one(instance: dict, image_tag: str):
    """Deploy a single instance: sync config → recreate pod with new image."""
    uid = instance["id"]
    prefix = instance.get("prefix", "s1")

    # Regenerate config (picks up any DB changes)
    config_json = config_gen.generate_json_string(instance)
    k8s_ops.apply_configmap(uid, config_json)
    db.set_sync_status(uid, "synced")

    # Recreate pod with new image
    k8s_ops.delete_pod(uid)
    time.sleep(2)
    k8s_ops.create_pod(uid, prefix=prefix, image_tag=image_tag)
    db.set_image_tag(uid, image_tag)


def _health_check_batch(instances: list[dict]) -> list[dict]:
    """Check health of a batch. Returns list of unhealthy instances."""
    failures = []
    for inst in instances:
        uid = inst["id"]
        pod = k8s_ops.get_pod_status(uid)
        if not pod.get("pod_exists") or pod.get("phase") != "Running":
            failures.append({"id": uid, "reason": f"Pod not running: {pod.get('phase', '?')}"})
            continue

        health = k8s_ops.check_pod_health(uid)
        if not health["feishu_ws"]:
            failures.append({"id": uid, "reason": "Feishu WS not connected"})
    return failures


# ──────────────────────────────────────
# Control: continue / rollback / abort
# ──────────────────────────────────────

async def continue_deploy() -> dict:
    """Resume a paused deploy."""
    global _active_task
    deploy = db.get_active_deploy()
    if not deploy:
        return {"error": "No active deploy"}
    if deploy["status"] != "paused":
        return {"error": f"Deploy is {deploy['status']}, not paused"}

    db.update_deploy(deploy["id"], status="rolling", error="")
    _active_task = asyncio.create_task(_run_deploy(deploy["id"], deploy["image_tag"]))
    return db.get_deploy(deploy["id"])


async def rollback_deploy() -> dict:
    """Rollback: revert all instances that were updated to the previous image."""
    deploy = db.get_active_deploy()
    if not deploy:
        return {"error": "No active deploy to rollback"}

    prev_tag = deploy["prev_image_tag"]
    if not prev_tag:
        return {"error": "No previous image tag recorded"}

    db.update_deploy(deploy["id"], status="rolled_back", completed_at=db._now())
    logger.info("Deploy #%d: rolling back to %s", deploy["id"], prev_tag)

    # Find instances that were updated to the new tag
    all_instances = db.list_all()
    rolled = 0
    for inst in all_instances:
        if inst["image_tag"] == deploy["image_tag"] and inst["status"] == "running":
            try:
                _deploy_one(inst, prev_tag)
                rolled += 1
            except Exception as e:
                logger.error("Rollback failed for carher-%d: %s", inst["id"], e)

    await _notify_deploy_event(deploy["id"], "rolled_back", f"Reverted {rolled} instances to {prev_tag}")
    return {"action": "rolled_back", "reverted": rolled, "to": prev_tag}


def abort_deploy() -> dict:
    """Abort without rollback — just stop the pipeline."""
    deploy = db.get_active_deploy()
    if not deploy:
        return {"error": "No active deploy"}
    db.update_deploy(deploy["id"], status="failed", error="Manually aborted", completed_at=db._now())
    return {"action": "aborted", "deploy_id": deploy["id"]}


def get_deploy_status() -> dict:
    """Current deploy status + progress."""
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

_FEISHU_WEBHOOK_URL: str | None = None


def set_webhook_url(url: str):
    global _FEISHU_WEBHOOK_URL
    _FEISHU_WEBHOOK_URL = url


async def _notify_deploy_event(deploy_id: int, event: str, detail: str):
    """Send deploy event to Feishu webhook."""
    if not _FEISHU_WEBHOOK_URL:
        return
    try:
        import aiohttp
        deploy = db.get_deploy(deploy_id)
        tag = deploy["image_tag"] if deploy else "?"
        status_emoji = {"complete": "✅", "paused": "⚠️", "failed": "❌", "rolled_back": "🔄"}.get(event, "📦")

        card = {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": f"{status_emoji} CarHer Deploy #{deploy_id}"}},
                "elements": [
                    {"tag": "div", "text": {"tag": "plain_text", "content": f"镜像: {tag}\n事件: {event}\n详情: {detail}"}},
                    {"tag": "div", "text": {"tag": "plain_text", "content": f"进度: {deploy['done']}/{deploy['total']}" if deploy else ""}},
                ],
            },
        }

        async with aiohttp.ClientSession() as session:
            await session.post(_FEISHU_WEBHOOK_URL, json=card, timeout=aiohttp.ClientTimeout(total=10))
    except Exception as e:
        logger.warning("Feishu notification failed: %s", e)
