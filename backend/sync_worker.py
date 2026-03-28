"""Background sync worker: DB → ConfigMap reconciliation.

Responsibilities:
1. Retry failed ConfigMap syncs (sync_status='pending')
2. Periodic consistency check: DB vs actual K8s state
3. Periodic NAS backup
"""

from __future__ import annotations

import asyncio
import logging

from . import database as db
from . import config_gen
from . import k8s_ops

logger = logging.getLogger("carher-admin")

SYNC_INTERVAL = 60  # seconds
BACKUP_INTERVAL = 300  # 5 minutes


async def start_workers():
    """Launch background tasks."""
    asyncio.create_task(_sync_pending_loop())
    asyncio.create_task(_backup_loop())
    logger.info("Background workers started")


async def _sync_pending_loop():
    """Retry pending ConfigMap syncs every SYNC_INTERVAL seconds."""
    while True:
        await asyncio.sleep(SYNC_INTERVAL)
        try:
            pending = db.get_pending_sync()
            if not pending:
                continue
            logger.info("Syncing %d pending ConfigMaps", len(pending))
            for inst in pending:
                try:
                    sync_configmap(inst)
                except Exception as e:
                    logger.warning("Sync failed for %d: %s", inst["id"], e)
        except Exception as e:
            logger.error("Sync worker error: %s", e)


async def _backup_loop():
    """Periodic NAS backup."""
    while True:
        await asyncio.sleep(BACKUP_INTERVAL)
        try:
            db.backup_to_nas()
        except Exception as e:
            logger.warning("Backup worker error: %s", e)


def sync_configmap(instance: dict):
    """Generate config from DB row and write to K8s ConfigMap."""
    uid = instance["id"]
    config_json = config_gen.generate_json_string(instance)
    k8s_ops.apply_configmap(uid, config_json)
    db.set_sync_status(uid, "synced")
    logger.info("ConfigMap synced for carher-%d", uid)


def sync_all():
    """Force sync all active instances. Used for manual 'force sync' button."""
    instances = db.list_all()
    synced = 0
    failed = 0
    for inst in instances:
        if inst["status"] == "deleted":
            continue
        try:
            sync_configmap(inst)
            synced += 1
        except Exception as e:
            logger.warning("Force sync failed for %d: %s", inst["id"], e)
            failed += 1
    return {"synced": synced, "failed": failed}


def consistency_check() -> list[dict]:
    """Compare DB state vs actual K8s state. Returns list of discrepancies."""
    db_instances = {inst["id"]: inst for inst in db.list_all() if inst["status"] != "deleted"}
    pod_statuses = k8s_ops.get_all_pod_statuses()

    issues = []

    # DB says running but no Pod
    for uid, inst in db_instances.items():
        if inst["status"] == "running" and uid not in pod_statuses:
            issues.append({"id": uid, "issue": "db_running_no_pod", "detail": "DB says running but no Pod found"})

    # Pod exists but not in DB
    for uid in pod_statuses:
        if uid not in db_instances:
            issues.append({"id": uid, "issue": "pod_no_db", "detail": "Pod exists but not in DB"})

    # DB says stopped but Pod exists
    for uid, inst in db_instances.items():
        if inst["status"] == "stopped" and uid in pod_statuses:
            phase = pod_statuses[uid].get("phase", "")
            if phase == "Running":
                issues.append({"id": uid, "issue": "db_stopped_pod_running", "detail": "DB says stopped but Pod is Running"})

    # ConfigMap content mismatch
    for uid, inst in db_instances.items():
        if inst["sync_status"] == "pending":
            issues.append({"id": uid, "issue": "sync_pending", "detail": "ConfigMap sync pending"})

    return issues
