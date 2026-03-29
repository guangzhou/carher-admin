"""Metrics collection from K8s Metrics API.

Reads Pod and Node resource usage from metrics-server (already deployed on ACK).
No additional components needed — metrics-server caches data every 30s,
we just read it via the standard /apis/metrics.k8s.io/v1beta1 API.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from kubernetes import client, config
from kubernetes.client.rest import ApiException

logger = logging.getLogger("carher-admin")

NS = "carher"

_custom_api: client.CustomObjectsApi | None = None
_core_api: client.CoreV1Api | None = None


def _custom() -> client.CustomObjectsApi:
    global _custom_api
    if _custom_api is None:
        _custom_api = client.CustomObjectsApi()
    return _custom_api


def _core() -> client.CoreV1Api:
    global _core_api
    if _core_api is None:
        _core_api = client.CoreV1Api()
    return _core_api


def _parse_cpu(val: str) -> float:
    """Parse K8s CPU quantity to millicores (float)."""
    if val.endswith("n"):
        return int(val[:-1]) / 1_000_000
    if val.endswith("u"):
        return int(val[:-1]) / 1_000
    if val.endswith("m"):
        return float(val[:-1])
    return float(val) * 1000


def _parse_memory_mi(val: str) -> float:
    """Parse K8s memory quantity to MiB (float)."""
    if val.endswith("Ki"):
        return int(val[:-2]) / 1024
    if val.endswith("Mi"):
        return float(val[:-2])
    if val.endswith("Gi"):
        return float(val[:-2]) * 1024
    if val.endswith("Ti"):
        return float(val[:-2]) * 1024 * 1024
    return int(val) / (1024 * 1024)


# ──────────────────────────────────────
# Pod metrics
# ──────────────────────────────────────

def get_pod_metrics(uid: int) -> dict:
    """Get CPU/Memory for a specific carher Pod."""
    try:
        data = _custom().get_namespaced_custom_object(
            "metrics.k8s.io", "v1beta1", NS, "pods", f"carher-{uid}",
        )
        containers = data.get("containers", [])
        if not containers:
            return {"cpu_m": 0, "memory_mi": 0}
        usage = containers[0].get("usage", {})
        return {
            "cpu_m": round(_parse_cpu(usage.get("cpu", "0")), 2),
            "memory_mi": round(_parse_memory_mi(usage.get("memory", "0")), 1),
        }
    except ApiException as e:
        if e.status == 404:
            return {"cpu_m": 0, "memory_mi": 0, "error": "Pod not running"}
        raise


def get_all_pod_metrics() -> dict[int, dict]:
    """Get CPU/Memory for all carher Pods. Returns {uid: {cpu_m, memory_mi}}."""
    result: dict[int, dict] = {}
    try:
        data = _custom().list_namespaced_custom_object(
            "metrics.k8s.io", "v1beta1", NS, "pods",
        )
        for item in data.get("items", []):
            name = item.get("metadata", {}).get("name", "")
            if not name.startswith("carher-") or not name[7:].isdigit():
                continue
            uid = int(name[7:])
            containers = item.get("containers", [])
            if not containers:
                continue
            usage = containers[0].get("usage", {})
            result[uid] = {
                "cpu_m": round(_parse_cpu(usage.get("cpu", "0")), 2),
                "memory_mi": round(_parse_memory_mi(usage.get("memory", "0")), 1),
            }
    except Exception as e:
        logger.warning("Failed to get pod metrics: %s", e)
    return result


# ──────────────────────────────────────
# Node metrics
# ──────────────────────────────────────

def get_node_metrics() -> list[dict]:
    """Get CPU/Memory usage and capacity for all nodes."""
    nodes_usage: dict[str, dict] = {}
    try:
        data = _custom().list_cluster_custom_object("metrics.k8s.io", "v1beta1", "nodes")
        for item in data.get("items", []):
            name = item.get("metadata", {}).get("name", "")
            usage = item.get("usage", {})
            nodes_usage[name] = {
                "cpu_m": round(_parse_cpu(usage.get("cpu", "0")), 1),
                "memory_mi": round(_parse_memory_mi(usage.get("memory", "0")), 1),
            }
    except Exception as e:
        logger.warning("Failed to get node metrics: %s", e)
        return []

    result = []
    try:
        node_list = _core().list_node()
        for node in node_list.items:
            name = node.metadata.name
            cap = node.status.capacity or {}
            alloc = node.status.allocatable or {}
            cpu_cap = _parse_cpu(cap.get("cpu", "0"))
            mem_cap = _parse_memory_mi(cap.get("memory", "0"))
            usage = nodes_usage.get(name, {})
            cpu_used = usage.get("cpu_m", 0)
            mem_used = usage.get("memory_mi", 0)

            pod_count = 0
            try:
                pods = _core().list_namespaced_pod(
                    NS, field_selector=f"spec.nodeName={name},status.phase=Running"
                )
                pod_count = len(pods.items)
            except Exception:
                pass

            result.append({
                "name": name,
                "cpu_capacity_m": round(cpu_cap, 0),
                "cpu_used_m": round(cpu_used, 1),
                "cpu_percent": round(cpu_used / cpu_cap * 100, 1) if cpu_cap else 0,
                "memory_capacity_mi": round(mem_cap, 0),
                "memory_used_mi": round(mem_used, 1),
                "memory_percent": round(mem_used / mem_cap * 100, 1) if mem_cap else 0,
                "pod_count": pod_count,
            })
    except Exception as e:
        logger.warning("Failed to get node capacity: %s", e)

    return result


# ──────────────────────────────────────
# NAS storage (PVC-based)
# ──────────────────────────────────────

def get_storage_info() -> dict:
    """Get PVC usage summary in carher namespace."""
    result = {"total_pvcs": 0, "bound": 0, "pending": 0, "pvcs": []}
    try:
        pvcs = _core().list_namespaced_persistent_volume_claim(NS)
        for pvc in pvcs.items:
            phase = pvc.status.phase if pvc.status else "Unknown"
            cap = pvc.status.capacity.get("storage", "0") if pvc.status and pvc.status.capacity else "0"
            result["total_pvcs"] += 1
            if phase == "Bound":
                result["bound"] += 1
            else:
                result["pending"] += 1
            result["pvcs"].append({
                "name": pvc.metadata.name,
                "status": phase,
                "capacity": cap,
                "storage_class": pvc.spec.storage_class_name or "",
            })
    except Exception as e:
        logger.warning("Failed to get PVC info: %s", e)
    return result


# ──────────────────────────────────────
# Cluster overview (aggregated)
# ──────────────────────────────────────

def get_cluster_overview() -> dict:
    """All-in-one cluster metrics: nodes + pods + storage."""
    nodes = get_node_metrics()
    pod_metrics = get_all_pod_metrics()
    storage = get_storage_info()

    total_cpu_cap = sum(n["cpu_capacity_m"] for n in nodes)
    total_cpu_used = sum(n["cpu_used_m"] for n in nodes)
    total_mem_cap = sum(n["memory_capacity_mi"] for n in nodes)
    total_mem_used = sum(n["memory_used_mi"] for n in nodes)

    her_cpu = sum(p["cpu_m"] for p in pod_metrics.values())
    her_mem = sum(p["memory_mi"] for p in pod_metrics.values())

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cluster": {
            "cpu_capacity_m": round(total_cpu_cap, 0),
            "cpu_used_m": round(total_cpu_used, 1),
            "cpu_percent": round(total_cpu_used / total_cpu_cap * 100, 1) if total_cpu_cap else 0,
            "memory_capacity_mi": round(total_mem_cap, 0),
            "memory_used_mi": round(total_mem_used, 1),
            "memory_percent": round(total_mem_used / total_mem_cap * 100, 1) if total_mem_cap else 0,
            "node_count": len(nodes),
        },
        "her_totals": {
            "instance_count": len(pod_metrics),
            "cpu_m": round(her_cpu, 1),
            "memory_mi": round(her_mem, 1),
            "avg_cpu_m": round(her_cpu / len(pod_metrics), 2) if pod_metrics else 0,
            "avg_memory_mi": round(her_mem / len(pod_metrics), 1) if pod_metrics else 0,
        },
        "nodes": nodes,
        "storage": storage,
    }


# ──────────────────────────────────────
# Background sampler thread
# ──────────────────────────────────────

_sampler_thread: threading.Thread | None = None
_sampler_running = False
SAMPLE_INTERVAL = 60


def start_sampler(db_module):
    """Start background thread that samples pod metrics every 60s into SQLite."""
    global _sampler_thread, _sampler_running
    if _sampler_running:
        return
    _sampler_running = True
    _sampler_thread = threading.Thread(target=_sampler_loop, args=(db_module,), daemon=True)
    _sampler_thread.start()
    logger.info("Metrics sampler started (interval=%ds)", SAMPLE_INTERVAL)


def _sampler_loop(db_module):
    global _sampler_running
    cleanup_counter = 0
    while _sampler_running:
        try:
            pod_metrics = get_all_pod_metrics()
            node_metrics = get_node_metrics()
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

            rows = []
            for uid, m in pod_metrics.items():
                rows.append((ts, "pod", uid, m["cpu_m"], m["memory_mi"]))
            for n in node_metrics:
                rows.append((ts, "node", 0, n["cpu_used_m"], n["memory_used_mi"]))

            if rows:
                db_module.insert_metrics_batch(rows)

            cleanup_counter += 1
            if cleanup_counter >= 1440:
                db_module.cleanup_old_metrics(days=7)
                cleanup_counter = 0

        except Exception as e:
            logger.warning("Metrics sampler error: %s", e)

        time.sleep(SAMPLE_INTERVAL)
