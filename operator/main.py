"""CarHer Kubernetes Operator — kopf-based.

Watches HerInstance CRDs and manages the full lifecycle:
  - ConfigMap (per-user openclaw.json)
  - PVC (user data)
  - Pod (OpenClaw gateway)
  - Shared knownBots ConfigMap
  - Health checks (periodic)

Reconciliation loop:
  spec changed → recompute config → apply ConfigMap → recreate Pod
  Pod died      → detected by timer → recreate
  knownBots changed → regenerate all ConfigMaps (debounced)
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone

import kopf
from kubernetes import client, config

from .config_gen import generate_openclaw_json
from .known_bots import rebuild_known_bots_configmap, get_known_bots

logger = logging.getLogger("carher-operator")

NS = "carher"
ACR = "cltx-her-ck-registry.ap-southeast-1.cr.aliyuncs.com/her/carher"
CRD_GROUP = "carher.io"
CRD_VERSION = "v1alpha1"
CRD_PLURAL = "herinstances"
USER_PVC_STORAGE_CLASS = "alibabacloud-cnfs-nas"
USER_PVC_STORAGE_REQUEST = "20Gi"

_QUANTITY_SUFFIXES = {
    None: 1,
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "Ei": 1024**6,
}


# ──────────────────────────────────────
# Startup
# ──────────────────────────────────────

@kopf.on.startup()
def on_startup(settings: kopf.OperatorSettings, **_):
    settings.persistence.finalizer = "carher.io/operator-finalizer"
    settings.posting.level = logging.WARNING
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    logger.info("CarHer operator started")


def _parse_storage_quantity(val) -> int:
    s = str(val or "").strip()
    m = re.fullmatch(r"(\d+)([KMGTE]i)?", s)
    if not m:
        logger.warning("Unknown storage quantity %r, treating as 0", s)
        return 0
    return int(m.group(1)) * _QUANTITY_SUFFIXES[m.group(2)]


# ──────────────────────────────────────
# Create: HerInstance created → ConfigMap + PVC + Pod
# ──────────────────────────────────────

@kopf.on.create(CRD_GROUP, CRD_VERSION, CRD_PLURAL)
def on_create(spec, name, namespace, patch, **_):
    uid = spec["userId"]
    logger.info("Creating her-%d (%s)", uid, spec.get("name", ""))

    _ensure_pvc(uid)
    config_hash = _apply_config(spec)
    if not spec.get("paused", False):
        _ensure_pod(spec)
        patch.status["phase"] = "Pending"
    else:
        patch.status["phase"] = "Paused"
    patch.status["configHash"] = config_hash
    patch.status["feishuWS"] = "Unknown"

    # Trigger knownBots rebuild (new bot added)
    _schedule_known_bots_rebuild()


# ──────────────────────────────────────
# Update: spec changed → reconcile
# ──────────────────────────────────────

@kopf.on.update(CRD_GROUP, CRD_VERSION, CRD_PLURAL, field="spec")
def on_update(spec, old, new, name, namespace, patch, **_):
    uid = spec["userId"]
    logger.info("Updating her-%d", uid)

    old_spec = old.get("spec", {})
    new_spec = new.get("spec", {})

    # Handle pause/unpause
    if new_spec.get("paused") and not old_spec.get("paused"):
        _delete_pod(uid)
        patch.status["phase"] = "Paused"
        return
    if not new_spec.get("paused") and old_spec.get("paused"):
        _apply_config(spec)
        _ensure_pod(spec)
        patch.status["phase"] = "Pending"
        return

    # Regenerate config
    config_hash = _apply_config(spec)

    # If image or config changed, recreate pod
    image_changed = old_spec.get("image") != new_spec.get("image")
    config_changed = patch.status.get("configHash") != config_hash

    if image_changed or config_changed or _config_affecting_fields_changed(old_spec, new_spec):
        if not spec.get("paused", False):
            _delete_pod(uid)
            time.sleep(2)
            _ensure_pod(spec)
            patch.status["phase"] = "Pending"
    patch.status["configHash"] = config_hash

    # If bot identity fields changed, rebuild knownBots for all instances
    if _bot_fields_changed(old_spec, new_spec):
        _schedule_known_bots_rebuild()


# ──────────────────────────────────────
# Delete: clean up resources
# ──────────────────────────────────────

@kopf.on.delete(CRD_GROUP, CRD_VERSION, CRD_PLURAL)
def on_delete(spec, name, namespace, **_):
    uid = spec["userId"]
    logger.info("Deleting her-%d", uid)
    _delete_pod(uid)
    _delete_configmap(uid)
    # PVC is intentionally NOT deleted (data preservation)
    logger.info("her-%d resources cleaned (PVC preserved)", uid)
    _schedule_known_bots_rebuild()


# ──────────────────────────────────────
# Timer: health check every 30s
# ──────────────────────────────────────

@kopf.timer(CRD_GROUP, CRD_VERSION, CRD_PLURAL, interval=30, initial_delay=15)
def health_check(spec, status, patch, **_):
    uid = spec["userId"]
    if spec.get("paused", False):
        patch.status["phase"] = "Paused"
        return

    v1 = client.CoreV1Api()
    pod_name = f"carher-{uid}"

    try:
        pod = v1.read_namespaced_pod(pod_name, NS)
    except client.rest.ApiException as e:
        if e.status == 404:
            # Pod missing — self-heal
            logger.warning("her-%d pod missing, recreating (self-heal)", uid)
            _ensure_pod(spec)
            patch.status["phase"] = "Pending"
            patch.status["message"] = "Pod recreated by self-heal"
            return
        raise

    phase = pod.status.phase or "Unknown"
    cs = pod.status.container_statuses or []

    patch.status["phase"] = phase
    patch.status["podIP"] = pod.status.pod_ip or ""
    patch.status["node"] = pod.spec.node_name or ""
    patch.status["restarts"] = cs[0].restart_count if cs else 0
    patch.status["lastHealthCheck"] = datetime.now(timezone.utc).isoformat()

    # Check Feishu WS from logs
    if phase == "Running":
        try:
            logs = v1.read_namespaced_pod_log(pod_name, NS, tail_lines=100)
            ws_ok = "ws client ready" in logs
            patch.status["feishuWS"] = "Connected" if ws_ok else "Disconnected"
        except Exception:
            patch.status["feishuWS"] = "Unknown"

    # CrashLoopBackOff detection
    if cs and cs[0].state and cs[0].state.waiting:
        reason = cs[0].state.waiting.reason or ""
        if "CrashLoopBackOff" in reason:
            patch.status["phase"] = "Failed"
            patch.status["message"] = f"CrashLoopBackOff (restarts: {cs[0].restart_count})"


# ──────────────────────────────────────
# Known bots global rebuild (debounced via timer)
# ──────────────────────────────────────

_known_bots_dirty = False


def _schedule_known_bots_rebuild():
    global _known_bots_dirty
    _known_bots_dirty = True


@kopf.daemon(CRD_GROUP, CRD_VERSION, CRD_PLURAL, cancellation_timeout=5)
def known_bots_watcher(stopped, **_):
    """Runs once per operator. Periodically rebuilds shared knownBots ConfigMap."""
    global _known_bots_dirty
    while not stopped:
        if _known_bots_dirty:
            _known_bots_dirty = False
            try:
                rebuild_known_bots_configmap()
                _regenerate_all_configs()
            except Exception as e:
                logger.error("knownBots rebuild failed: %s", e)
                _known_bots_dirty = True
        stopped.wait(10)


def _regenerate_all_configs():
    """Regenerate ConfigMaps for all instances after knownBots change."""
    crd_api = client.CustomObjectsApi()
    items = crd_api.list_namespaced_custom_object(CRD_GROUP, CRD_VERSION, NS, CRD_PLURAL)
    for item in items.get("items", []):
        spec = item.get("spec", {})
        if not spec.get("paused", False):
            _apply_config(spec)
    logger.info("Regenerated all ConfigMaps after knownBots change")


# ──────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────

def _config_affecting_fields_changed(old: dict, new: dict) -> bool:
    fields = ["model", "appId", "prefix", "owner", "provider", "botOpenId"]
    return any(old.get(f) != new.get(f) for f in fields)


def _bot_fields_changed(old: dict, new: dict) -> bool:
    fields = ["appId", "name", "botOpenId"]
    return any(old.get(f) != new.get(f) for f in fields)


def _apply_config(spec: dict) -> str:
    """Generate openclaw.json from CRD spec and apply as ConfigMap. Returns content hash."""
    uid = spec["userId"]

    # Read appSecret from K8s Secret
    app_secret = ""
    secret_name = spec.get("appSecretRef", f"carher-{uid}-secret")
    try:
        v1 = client.CoreV1Api()
        secret = v1.read_namespaced_secret(secret_name, NS)
        if secret.data and "app_secret" in secret.data:
            import base64
            app_secret = base64.b64decode(secret.data["app_secret"]).decode()
    except client.rest.ApiException:
        logger.warning("Secret %s not found for her-%d", secret_name, uid)

    known_bots, known_bot_open_ids = get_known_bots()

    instance = {
        "id": uid,
        "name": spec.get("name", ""),
        "model": spec.get("model", "gpt"),
        "app_id": spec.get("appId", ""),
        "app_secret": app_secret,
        "prefix": spec.get("prefix", "s1"),
        "owner": spec.get("owner", ""),
        "provider": spec.get("provider", "wangsu"),
        "litellm_key": spec.get("litellmKey", ""),
        "bot_open_id": spec.get("botOpenId", ""),
    }

    config_dict = generate_openclaw_json(instance, known_bots, known_bot_open_ids)
    config_json = json.dumps(config_dict, indent=2, ensure_ascii=False)
    config_hash = hashlib.md5(config_json.encode()).hexdigest()[:12]

    # Apply ConfigMap
    v1 = client.CoreV1Api()
    cm_name = f"carher-{uid}-user-config"
    cm = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(
            name=cm_name, namespace=NS,
            labels={"app": "carher-user", "user-id": str(uid), "managed-by": "carher-operator"},
        ),
        data={"openclaw.json": config_json},
    )
    try:
        v1.replace_namespaced_config_map(cm_name, NS, cm)
    except client.rest.ApiException as e:
        if e.status == 404:
            v1.create_namespaced_config_map(NS, cm)
        else:
            raise

    return config_hash


def _ensure_pvc(uid: int):
    v1 = client.CoreV1Api()
    pvc_name = f"carher-{uid}-data"
    try:
        pvc = v1.read_namespaced_persistent_volume_claim(pvc_name, NS)
    except client.rest.ApiException as e:
        if e.status != 404:
            raise
    else:
        current_storage = ((pvc.spec.resources.requests or {}) if pvc.spec and pvc.spec.resources else {}).get("storage")
        if _parse_storage_quantity(current_storage) >= _parse_storage_quantity(USER_PVC_STORAGE_REQUEST):
            return
        v1.patch_namespaced_persistent_volume_claim(
            pvc_name,
            NS,
            {"spec": {"resources": {"requests": {"storage": USER_PVC_STORAGE_REQUEST}}}},
        )
        logger.info("Expanded PVC %s from %s to %s", pvc_name, current_storage, USER_PVC_STORAGE_REQUEST)
        return

    pvc = client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(name=pvc_name, namespace=NS, labels={"managed-by": "carher-operator"}),
        spec=client.V1PersistentVolumeClaimSpec(
            access_modes=["ReadWriteMany"],
            storage_class_name=USER_PVC_STORAGE_CLASS,
            resources=client.V1VolumeResourceRequirements(requests={"storage": USER_PVC_STORAGE_REQUEST}),
        ),
    )
    v1.create_namespaced_persistent_volume_claim(NS, pvc)


def _ensure_pod(spec: dict):
    """Create pod for a HerInstance. If exists, delete first."""
    uid = spec["userId"]
    image_tag = spec.get("image", "skills-two-layer-8045eb9e")
    prefix = spec.get("prefix", "s1")
    pfx = f"{prefix}-" if not prefix.endswith("-") else prefix

    v1 = client.CoreV1Api()
    pod_name = f"carher-{uid}"

    try:
        v1.delete_namespaced_pod(pod_name, NS)
        time.sleep(3)
    except client.rest.ApiException:
        pass

    pod = client.V1Pod(
        metadata=client.V1ObjectMeta(
            name=pod_name, namespace=NS,
            labels={"app": "carher-user", "user-id": str(uid), "managed-by": "carher-operator"},
            annotations={"carher.io/deploy-group": spec.get("deployGroup", "stable")},
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
                ],
            )],
            volumes=[
                client.V1Volume(name="user-data", persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(claim_name=f"carher-{uid}-data")),
                client.V1Volume(name="user-config", config_map=client.V1ConfigMapVolumeSource(name=f"carher-{uid}-user-config")),
                client.V1Volume(name="base-config", config_map=client.V1ConfigMapVolumeSource(name="carher-base-config")),
                client.V1Volume(name="gcloud-adc", secret=client.V1SecretVolumeSource(secret_name="carher-gcloud-adc")),
                client.V1Volume(name="shared-skills", host_path=client.V1HostPathVolumeSource(path="/root/.openclaw/skills", type="DirectoryOrCreate")),
                client.V1Volume(name="dept-skills", host_path=client.V1HostPathVolumeSource(path="/root/.openclaw/dept-skills/default", type="DirectoryOrCreate")),
            ],
        ),
    )
    v1.create_namespaced_pod(NS, pod)


def _delete_pod(uid: int):
    v1 = client.CoreV1Api()
    try:
        v1.delete_namespaced_pod(f"carher-{uid}", NS)
    except client.rest.ApiException:
        pass


def _delete_configmap(uid: int):
    v1 = client.CoreV1Api()
    try:
        v1.delete_namespaced_config_map(f"carher-{uid}-user-config", NS)
    except client.rest.ApiException:
        pass
