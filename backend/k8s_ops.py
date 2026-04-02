"""K8s operations for CarHer admin — thin layer over kubernetes Python client.

This module only handles K8s API calls (Pod, ConfigMap, PVC).
Config generation and data storage are in config_gen.py and database.py.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream

logger = logging.getLogger("carher-admin")

NS = "carher"
ACR = "cltx-her-ck-registry.ap-southeast-1.cr.aliyuncs.com/her/carher"
DEFAULT_IMAGE_TAG = "v20260328"


def init_k8s():
    try:
        config.load_incluster_config()
        logger.info("Loaded in-cluster K8s config")
    except config.ConfigException:
        config.load_kube_config()
        logger.info("Loaded local kubeconfig")


_core_instance: client.CoreV1Api | None = None


def _core() -> client.CoreV1Api:
    global _core_instance
    if _core_instance is None:
        _core_instance = client.CoreV1Api()
    return _core_instance


def _age(creation: datetime | None) -> str:
    if not creation:
        return "?"
    delta = datetime.now(timezone.utc) - creation.replace(tzinfo=timezone.utc)
    if delta.days > 0:
        return f"{delta.days}d{delta.seconds // 3600}h"
    hours = delta.seconds // 3600
    mins = (delta.seconds % 3600) // 60
    return f"{hours}h{mins}m"


# ──────────────────────────────────────
# Pod status queries
# ──────────────────────────────────────

def get_pod_status(uid: int) -> dict:
    """Get runtime info for a single pod."""
    v1 = _core()
    try:
        pod = v1.read_namespaced_pod(f"carher-{uid}", NS)
        cs = pod.status.container_statuses or []
        return {
            "pod_exists": True,
            "phase": pod.status.phase or "Unknown",
            "pod_ip": pod.status.pod_ip or "",
            "node": pod.spec.node_name or "",
            "restarts": cs[0].restart_count if cs else 0,
            "age": _age(pod.metadata.creation_timestamp),
            "image": pod.spec.containers[0].image if pod.spec.containers else "",
        }
    except ApiException as e:
        if e.status == 404:
            return {"pod_exists": False, "phase": "Stopped"}
        raise


def get_all_pod_statuses() -> dict[int, dict]:
    """Get runtime status for all carher-user pods. Returns {uid: status_dict}."""
    v1 = _core()
    result: dict[int, dict] = {}
    pods = v1.list_namespaced_pod(NS, label_selector="app=carher-user")
    for pod in pods.items:
        uid_str = pod.metadata.labels.get("user-id", "")
        if not uid_str or not uid_str.isdigit():
            continue
        cs = pod.status.container_statuses or []
        result[int(uid_str)] = {
            "pod_exists": True,
            "phase": pod.status.phase or "Unknown",
            "pod_ip": pod.status.pod_ip or "",
            "node": pod.spec.node_name or "",
            "restarts": cs[0].restart_count if cs else 0,
            "age": _age(pod.metadata.creation_timestamp),
        }
    return result


def get_pvc_status(uid: int) -> str:
    v1 = _core()
    try:
        pvc = v1.read_namespaced_persistent_volume_claim(f"carher-{uid}-data", NS)
        return pvc.status.phase or "Unknown"
    except ApiException:
        return "None"


# ──────────────────────────────────────
# ConfigMap
# ──────────────────────────────────────

def apply_configmap(uid: int, config_json: str):
    """Write openclaw.json to a per-user ConfigMap."""
    v1 = _core()
    cm_name = f"carher-{uid}-user-config"
    cm = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(name=cm_name, namespace=NS),
        data={"openclaw.json": config_json},
    )
    try:
        v1.replace_namespaced_config_map(cm_name, NS, cm)
    except ApiException as e:
        if e.status == 404:
            v1.create_namespaced_config_map(NS, cm)
        else:
            raise


def get_configmap_data(uid: int) -> dict | None:
    """Read openclaw.json from ConfigMap. Returns parsed dict or None."""
    v1 = _core()
    try:
        cm = v1.read_namespaced_config_map(f"carher-{uid}-user-config", NS)
        data = (cm.data or {}).get("openclaw.json", "")
        return json.loads(data) if data else None
    except (ApiException, json.JSONDecodeError):
        return None


def delete_configmap(uid: int):
    v1 = _core()
    try:
        v1.delete_namespaced_config_map(f"carher-{uid}-user-config", NS)
    except ApiException:
        pass


# ──────────────────────────────────────
# PVC
# ──────────────────────────────────────

def ensure_pvc(uid: int):
    v1 = _core()
    try:
        v1.read_namespaced_persistent_volume_claim(f"carher-{uid}-data", NS)
    except ApiException as e:
        if e.status == 404:
            pvc = client.V1PersistentVolumeClaim(
                metadata=client.V1ObjectMeta(name=f"carher-{uid}-data", namespace=NS),
                spec=client.V1PersistentVolumeClaimSpec(
                    access_modes=["ReadWriteMany"],
                    storage_class_name="alibabacloud-cnfs-nas",
                    resources=client.V1VolumeResourceRequirements(requests={"storage": "5Gi"}),
                ),
            )
            v1.create_namespaced_persistent_volume_claim(NS, pvc)


def delete_pvc(uid: int):
    v1 = _core()
    try:
        v1.delete_namespaced_persistent_volume_claim(f"carher-{uid}-data", NS)
    except ApiException:
        pass


# ──────────────────────────────────────
# Pod lifecycle
# ──────────────────────────────────────

def create_pod(uid: int, prefix: str, image_tag: str = DEFAULT_IMAGE_TAG):
    """Create (or recreate) a carher user pod."""
    v1 = _core()
    pod_name = f"carher-{uid}"
    pfx = f"{prefix}-" if not prefix.endswith("-") else prefix

    # Delete existing pod first
    try:
        v1.delete_namespaced_pod(pod_name, NS)
        time.sleep(3)
    except ApiException:
        pass

    pod = client.V1Pod(
        metadata=client.V1ObjectMeta(
            name=pod_name, namespace=NS,
            labels={"app": "carher-user", "user-id": str(uid)},
        ),
        spec=client.V1PodSpec(
            image_pull_secrets=[client.V1LocalObjectReference(name="acr-secret")],
            restart_policy="Always",
            containers=[client.V1Container(
                name="carher",
                image=f"{ACR}:{image_tag}",
                ports=[
                    client.V1ContainerPort(container_port=18789, name="gateway"),
                    client.V1ContainerPort(container_port=18790, name="realtime"),
                    client.V1ContainerPort(container_port=8000, name="frontend"),
                    client.V1ContainerPort(container_port=8080, name="ws-proxy"),
                    client.V1ContainerPort(container_port=18891, name="oauth"),
                ],
                env=[
                    client.V1EnvVar(name="HOME", value="/data"),
                    client.V1EnvVar(name="OPENCLAW_INSTANCE_ID", value=f"carher-{uid}-k8s"),
                    client.V1EnvVar(name="NODE_OPTIONS", value="--max-old-space-size=1536"),
                    client.V1EnvVar(name="GOOGLE_APPLICATION_CREDENTIALS", value="/gcloud/application_default_credentials.json"),
                    client.V1EnvVar(name="VOICE_FE_HOST", value=f"{pfx}u{uid}-fe.carher.net"),
                    client.V1EnvVar(name="VOICE_PROXY_HOST", value=f"{pfx}u{uid}-proxy.carher.net"),
                ],
                env_from=[client.V1EnvFromSource(secret_ref=client.V1SecretEnvSource(name="carher-env-keys"))],
                resources=client.V1ResourceRequirements(
                    requests={"cpu": "500m", "memory": "1Gi"},
                    limits={"cpu": "2", "memory": "2Gi"},
                ),
                volume_mounts=[
                    client.V1VolumeMount(name="user-data", mount_path="/data/.openclaw"),
                    client.V1VolumeMount(name="user-config", mount_path="/data/.openclaw/openclaw.json", sub_path="openclaw.json"),
                    client.V1VolumeMount(name="base-config", mount_path="/data/.openclaw/carher-config.json", sub_path="carher-config.json"),
                    client.V1VolumeMount(name="base-config", mount_path="/data/.openclaw/shared-config.json5", sub_path="shared-config.json5"),
                    client.V1VolumeMount(name="gcloud-adc", mount_path="/gcloud/application_default_credentials.json", sub_path="application_default_credentials.json", read_only=True),
                    client.V1VolumeMount(name="shared-skills", mount_path="/data/.openclaw/skills", read_only=True),
                    client.V1VolumeMount(name="dept-skills", mount_path="/data/.agents/skills", read_only=True),
                    client.V1VolumeMount(name="user-sessions", mount_path="/data/.openclaw/sessions", sub_path=str(uid)),
                ],
            )],
            volumes=[
                client.V1Volume(name="user-data", persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(claim_name=f"carher-{uid}-data")),
                client.V1Volume(name="user-config", config_map=client.V1ConfigMapVolumeSource(name=f"carher-{uid}-user-config")),
                client.V1Volume(name="base-config", config_map=client.V1ConfigMapVolumeSource(name="carher-base-config")),
                client.V1Volume(name="gcloud-adc", secret=client.V1SecretVolumeSource(secret_name="carher-gcloud-adc")),
                client.V1Volume(name="shared-skills", persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(claim_name="carher-shared-skills", read_only=True)),
                client.V1Volume(name="dept-skills", persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(claim_name="carher-dept-skills", read_only=True)),
                client.V1Volume(name="user-sessions", persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(claim_name="carher-shared-sessions")),
            ],
        ),
    )
    v1.create_namespaced_pod(NS, pod)


def delete_pod(uid: int):
    v1 = _core()
    try:
        v1.delete_namespaced_pod(f"carher-{uid}", NS)
    except ApiException:
        pass


# ──────────────────────────────────────
# Logs & health
# ──────────────────────────────────────

def get_logs(uid: int, tail: int = 200) -> str:
    v1 = _core()
    pod_name = _find_pod(uid)
    if not pod_name:
        return f"Error: No running pod found for carher-{uid}"
    try:
        return v1.read_namespaced_pod_log(pod_name, NS, tail_lines=tail, container="carher")
    except ApiException as e:
        return f"Error: {e.reason}"


def _find_pod(uid: int) -> str | None:
    """Find the running pod name for a user instance by label selector."""
    v1 = _core()
    try:
        pods = v1.list_namespaced_pod(NS, label_selector=f"user-id={uid}")
        for pod in pods.items:
            if pod.status.phase in ("Running", "Pending"):
                return pod.metadata.name
        if pods.items:
            return pods.items[0].metadata.name
    except ApiException:
        pass
    return None


def check_pod_health(uid: int) -> dict:
    """Check Feishu WS, memory DB, model loading for a running pod."""
    v1 = _core()
    pod_name = _find_pod(uid)
    logs = ""
    if pod_name:
        try:
            logs = v1.read_namespaced_pod_log(pod_name, NS, tail_lines=200, container="carher")
        except ApiException:
            pass

    has_memory = False
    try:
        stream(
            v1.connect_get_namespaced_pod_exec,
            f"carher-{uid}", NS,
            command=["test", "-f", "/data/.openclaw/memory/main.sqlite"],
            stderr=True, stdin=False, stdout=True, tty=False,
        )
        has_memory = True
    except Exception:
        pass

    return {
        "feishu_ws": "ws client ready" in logs,
        "memory_db": has_memory,
        "model_ok": "agent model" in logs,
    }


def cluster_status() -> dict:
    """Cluster-level stats: node distribution, pod counts."""
    v1 = _core()
    pods = v1.list_namespaced_pod(NS, label_selector="app=carher-user")
    total = len(pods.items)
    running = sum(1 for p in pods.items if p.status.phase == "Running")

    node_dist: dict[str, int] = {}
    for p in pods.items:
        n = p.spec.node_name or "unscheduled"
        node_dist[n] = node_dist.get(n, 0) + 1

    return {
        "total_pods": total,
        "running": running,
        "nodes": [{"name": k, "pods": v} for k, v in sorted(node_dist.items())],
    }


# ──────────────────────────────────────
# Discovery: scan existing ConfigMaps (for migration)
# ──────────────────────────────────────

def discover_all_configmaps() -> list[tuple[int, dict]]:
    """Scan all carher-N-user-config ConfigMaps. Returns [(uid, parsed_config)]."""
    v1 = _core()
    results = []
    cms = v1.list_namespaced_config_map(NS)
    for cm in cms.items:
        m = re.match(r"carher-(\d+)-user-config", cm.metadata.name)
        if not m:
            continue
        uid = int(m.group(1))
        data = (cm.data or {}).get("openclaw.json", "")
        try:
            cfg = json.loads(data) if data else {}
        except json.JSONDecodeError:
            cfg = {}
        if cfg:
            results.append((uid, cfg))
    return results
