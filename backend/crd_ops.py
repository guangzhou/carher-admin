"""CRD operations: carher-admin creates/updates HerInstance CRDs,
the operator handles the actual resource management.

This replaces direct Pod/ConfigMap manipulation in k8s_ops.py
when running in operator mode.
"""

from __future__ import annotations

import logging
from typing import Any

from kubernetes import client, config
from kubernetes.client.rest import ApiException

logger = logging.getLogger("carher-admin")

NS = "carher"
CRD_GROUP = "carher.io"
CRD_VERSION = "v1alpha1"
CRD_PLURAL = "herinstances"


def init_k8s():
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


_crd_instance: client.CustomObjectsApi | None = None
_v1_instance: client.CoreV1Api | None = None


def _crd_api() -> client.CustomObjectsApi:
    global _crd_instance
    if _crd_instance is None:
        _crd_instance = client.CustomObjectsApi()
    return _crd_instance


def _v1() -> client.CoreV1Api:
    global _v1_instance
    if _v1_instance is None:
        _v1_instance = client.CoreV1Api()
    return _v1_instance


# ──────────────────────────────────────
# CRD CRUD
# ──────────────────────────────────────

def create_her_instance(data: dict) -> dict:
    """Create a HerInstance CRD + associated Secret for appSecret."""
    uid = data["id"]
    app_secret = data.get("app_secret", "")

    # Store appSecret in a K8s Secret (CRDs are not encrypted)
    if app_secret:
        _ensure_secret(uid, app_secret)

    body = {
        "apiVersion": f"{CRD_GROUP}/{CRD_VERSION}",
        "kind": "HerInstance",
        "metadata": {
            "name": f"her-{uid}",
            "namespace": NS,
            "labels": {"app": "carher-user", "user-id": str(uid)},
        },
        "spec": {
            "userId": uid,
            "name": data.get("name", ""),
            "model": data.get("model", "opus"),
            "appId": data.get("app_id", ""),
            "appSecretRef": f"carher-{uid}-secret",
            "prefix": data.get("prefix", "s1"),
            "owner": data.get("owner", ""),
            "provider": data.get("provider", "wangsu"),
            "litellmKey": data.get("litellm_key", ""),
            "botOpenId": data.get("bot_open_id", ""),
            "deployGroup": data.get("deploy_group", "stable"),
            "image": data.get("image_tag", "v20260328"),
            "paused": False,
        },
    }

    api = _crd_api()
    try:
        result = api.create_namespaced_custom_object(CRD_GROUP, CRD_VERSION, NS, CRD_PLURAL, body)
        logger.info("Created HerInstance her-%d", uid)
        return result
    except ApiException as e:
        if e.status == 409:
            return update_her_instance(uid, body["spec"])
        raise


def get_her_instance(uid: int) -> dict | None:
    api = _crd_api()
    try:
        return api.get_namespaced_custom_object(CRD_GROUP, CRD_VERSION, NS, CRD_PLURAL, f"her-{uid}")
    except ApiException as e:
        if e.status == 404:
            return None
        raise


def list_her_instances() -> list[dict]:
    api = _crd_api()
    result = api.list_namespaced_custom_object(CRD_GROUP, CRD_VERSION, NS, CRD_PLURAL)
    return result.get("items", [])


def update_her_instance(uid: int, spec_changes: dict) -> dict:
    """Patch HerInstance spec. Operator will reconcile."""
    api = _crd_api()
    patch_body = {"spec": spec_changes}
    return api.patch_namespaced_custom_object(
        CRD_GROUP, CRD_VERSION, NS, CRD_PLURAL, f"her-{uid}",
        patch_body,
    )


def pause_her_instance(uid: int) -> dict:
    """Pause = operator deletes Pod, keeps ConfigMap/PVC."""
    return update_her_instance(uid, {"paused": True})


def resume_her_instance(uid: int) -> dict:
    """Resume = operator recreates Pod."""
    return update_her_instance(uid, {"paused": False})


def delete_her_instance(uid: int, purge_data: bool = False):
    """Delete the CRD. Operator cleans up Pod + ConfigMap. PVC preserved by default."""
    api = _crd_api()
    try:
        api.delete_namespaced_custom_object(CRD_GROUP, CRD_VERSION, NS, CRD_PLURAL, f"her-{uid}")
    except ApiException as e:
        if e.status != 404:
            raise

    if purge_data:
        v1 = _v1()
        try:
            v1.delete_namespaced_persistent_volume_claim(f"carher-{uid}-data", NS)
        except ApiException:
            pass

    try:
        v1 = _v1()
        v1.delete_namespaced_secret(f"carher-{uid}-secret", NS)
    except (ApiException, Exception):
        pass


def set_image(uid: int, image_tag: str) -> dict:
    """Update image for deploy. Operator will recreate Pod."""
    return update_her_instance(uid, {"image": image_tag})


def set_deploy_group(uid: int, group: str) -> dict:
    return update_her_instance(uid, {"deployGroup": group})


# ──────────────────────────────────────
# Batch operations
# ──────────────────────────────────────

def batch_set_image(image_tag: str, deploy_group: str | None = None) -> int:
    """Set image for all (or filtered) instances. Returns count updated."""
    instances = list_her_instances()
    count = 0
    for inst in instances:
        spec = inst.get("spec", {})
        if spec.get("paused"):
            continue
        if deploy_group and spec.get("deployGroup") != deploy_group:
            continue
        try:
            set_image(spec["userId"], image_tag)
            count += 1
        except Exception as e:
            logger.error("Failed to set image for her-%d: %s", spec["userId"], e)
    return count


# ──────────────────────────────────────
# Status queries (read from CRD status)
# ──────────────────────────────────────

def get_instance_status(uid: int) -> dict:
    """Read status from CRD (updated by operator's health check timer)."""
    inst = get_her_instance(uid)
    if not inst:
        return {"phase": "NotFound"}
    return inst.get("status", {})


def get_all_statuses() -> dict[int, dict]:
    """Get status for all instances from CRDs."""
    result: dict[int, dict] = {}
    for inst in list_her_instances():
        uid = inst.get("spec", {}).get("userId", 0)
        if uid:
            result[uid] = {
                "spec": inst.get("spec", {}),
                "status": inst.get("status", {}),
            }
    return result


# ──────────────────────────────────────
# Logs (still direct K8s API, not through CRD)
# ──────────────────────────────────────

def get_logs(uid: int, tail: int = 200) -> str:
    v1 = _v1()
    pod_name = _find_pod(uid, v1)
    if not pod_name:
        return f"Error: No running pod found for carher-{uid}"
    try:
        return v1.read_namespaced_pod_log(pod_name, NS, tail_lines=tail, container="carher")
    except ApiException as e:
        return f"Error: {e.reason}"


def _find_pod(uid: int, v1=None) -> str | None:
    if v1 is None:
        v1 = _v1()
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


# ──────────────────────────────────────
# Secret management
# ──────────────────────────────────────

def _ensure_secret(uid: int, app_secret: str):
    """Store app_secret in a dedicated K8s Secret."""
    import base64
    v1 = _v1()
    secret_name = f"carher-{uid}-secret"
    secret = client.V1Secret(
        metadata=client.V1ObjectMeta(
            name=secret_name, namespace=NS,
            labels={"app": "carher-user", "user-id": str(uid)},
        ),
        type="Opaque",
        data={"app_secret": base64.b64encode(app_secret.encode()).decode()},
    )
    try:
        v1.replace_namespaced_secret(secret_name, NS, secret)
    except ApiException as e:
        if e.status == 404:
            v1.create_namespaced_secret(NS, secret)
        else:
            raise
