"""K8s operations for CarHer admin — wraps kubernetes Python client."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream

logger = logging.getLogger("carher-admin")

NS = "carher"
ACR = "cltx-her-ck-registry.ap-southeast-1.cr.aliyuncs.com/her/carher"
DEFAULT_IMAGE_TAG = "v20260328"
GEMINI_PROJECT = "gen-lang-client-0519229117"
GEMINI_MODEL = "gemini-live-2.5-flash-native-audio"

MODEL_MAP = {
    "sonnet": "openrouter/anthropic/claude-sonnet-4.6",
    "opus": "openrouter/anthropic/claude-opus-4.6",
    "gpt": "openrouter/openai/gpt-5.4",
}
MODEL_MAP_ANTHROPIC = {
    "sonnet": "anthropic/claude-sonnet-4-6",
    "opus": "anthropic/claude-opus-4-6",
    "gpt": "openrouter/openai/gpt-5.4",
}

STANDARD_MODELS = {
    "openrouter/anthropic/claude-opus-4.6": {"alias": "opus"},
    "openrouter/anthropic/claude-sonnet-4.6": {"alias": "sonnet"},
    "anthropic/claude-opus-4-6": {"alias": "or-opus"},
    "anthropic/claude-sonnet-4-6": {"alias": "or-sonnet"},
    "openrouter/google/gemini-3.1-pro-preview": {"alias": "gemini"},
    "openrouter/minimax/minimax-m2.5": {"alias": "minimax"},
    "openrouter/z-ai/glm-5": {"alias": "glm"},
    "openrouter/openai/gpt-5.4": {"alias": "gpt"},
    "openrouter/openai/gpt-5.3-codex": {"alias": "codex"},
}


def init_k8s():
    """Load kubeconfig: in-cluster first, fallback to local ~/.kube/config."""
    try:
        config.load_incluster_config()
        logger.info("Loaded in-cluster K8s config")
    except config.ConfigException:
        config.load_kube_config()
        logger.info("Loaded local kubeconfig")


def _core() -> client.CoreV1Api:
    return client.CoreV1Api()


def _age(creation: datetime | None) -> str:
    if not creation:
        return "?"
    delta = datetime.now(timezone.utc) - creation.replace(tzinfo=timezone.utc)
    if delta.days > 0:
        return f"{delta.days}d{delta.seconds // 3600}h"
    hours = delta.seconds // 3600
    mins = (delta.seconds % 3600) // 60
    return f"{hours}h{mins}m"


def _parse_config(cm_data: str) -> dict[str, Any]:
    try:
        return json.loads(cm_data)
    except (json.JSONDecodeError, TypeError):
        return {}


def _extract_prefix_from_config(cfg: dict) -> str:
    url = cfg.get("channels", {}).get("feishu", {}).get("oauthRedirectUri", "")
    m = re.match(r"https://(s\d+-)u", url)
    return m.group(1) if m else "s1-"


def _model_short(full: str) -> str:
    """Extract short model name from full model path."""
    parts = full.rsplit("/", 1)
    return parts[-1] if parts else full


# ──────────────────────────────────────
# List
# ──────────────────────────────────────
def list_instances() -> list[dict]:
    v1 = _core()
    result: dict[str, dict] = {}

    # Running/Pending Pods
    pods = v1.list_namespaced_pod(NS, label_selector="app=carher-user")
    pod_ids: set[str] = set()
    for pod in pods.items:
        uid = pod.metadata.labels.get("user-id", "")
        if not uid:
            continue
        pod_ids.add(uid)
        cs = pod.status.container_statuses or []
        restarts = cs[0].restart_count if cs else 0

        cfg = _get_config_data(v1, uid)
        feishu = cfg.get("channels", {}).get("feishu", {})
        primary = cfg.get("agents", {}).get("defaults", {}).get("model", {}).get("primary", "")

        result[uid] = {
            "id": int(uid),
            "name": feishu.get("name", ""),
            "model": primary,
            "model_short": _model_short(primary),
            "status": pod.status.phase or "Unknown",
            "pod_ip": pod.status.pod_ip or "",
            "node": pod.spec.node_name or "",
            "age": _age(pod.metadata.creation_timestamp),
            "restarts": restarts,
            "app_id": feishu.get("appId", ""),
            "oauth_url": feishu.get("oauthRedirectUri", ""),
            "owner": ",".join(feishu.get("dm", {}).get("allowFrom", [])),
        }

    # Stopped: have ConfigMap but no Pod
    cms = v1.list_namespaced_config_map(NS)
    for cm in cms.items:
        m = re.match(r"carher-(\d+)-user-config", cm.metadata.name)
        if not m:
            continue
        uid = m.group(1)
        if uid in pod_ids:
            continue
        cfg = _parse_config((cm.data or {}).get("openclaw.json", "{}"))
        feishu = cfg.get("channels", {}).get("feishu", {})
        primary = cfg.get("agents", {}).get("defaults", {}).get("model", {}).get("primary", "")
        result[uid] = {
            "id": int(uid),
            "name": feishu.get("name", ""),
            "model": primary,
            "model_short": _model_short(primary),
            "status": "Stopped",
            "pod_ip": "",
            "node": "",
            "age": "",
            "restarts": 0,
            "app_id": feishu.get("appId", ""),
            "oauth_url": feishu.get("oauthRedirectUri", ""),
            "owner": ",".join(feishu.get("dm", {}).get("allowFrom", [])),
        }

    return sorted(result.values(), key=lambda x: x["id"])


# ──────────────────────────────────────
# Get details
# ──────────────────────────────────────
def get_instance(uid: int) -> dict:
    v1 = _core()
    info: dict[str, Any] = {"id": uid}

    # Pod
    try:
        pod = v1.read_namespaced_pod(f"carher-{uid}", NS)
        cs = pod.status.container_statuses or []
        info["status"] = pod.status.phase or "Unknown"
        info["pod_ip"] = pod.status.pod_ip or ""
        info["node"] = pod.spec.node_name or ""
        info["restarts"] = cs[0].restart_count if cs else 0
        info["age"] = _age(pod.metadata.creation_timestamp)
        info["image"] = pod.spec.containers[0].image if pod.spec.containers else ""
    except ApiException as e:
        if e.status == 404:
            info["status"] = "Stopped"
        else:
            raise

    # ConfigMap
    cfg = _get_config_data(v1, str(uid))
    feishu = cfg.get("channels", {}).get("feishu", {})
    info["name"] = feishu.get("name", "")
    info["model"] = cfg.get("agents", {}).get("defaults", {}).get("model", {}).get("primary", "")
    info["model_short"] = _model_short(info.get("model", ""))
    info["app_id"] = feishu.get("appId", "")
    info["oauth_url"] = feishu.get("oauthRedirectUri", "")
    info["owner"] = ",".join(feishu.get("dm", {}).get("allowFrom", []))
    info["known_bots_count"] = len(feishu.get("knownBots", {}))
    info["config"] = cfg

    # PVC
    try:
        pvc = v1.read_namespaced_persistent_volume_claim(f"carher-{uid}-data", NS)
        info["pvc_status"] = pvc.status.phase or "Unknown"
    except ApiException:
        info["pvc_status"] = "None"

    return info


def _get_config_data(v1: client.CoreV1Api, uid: str) -> dict:
    try:
        cm = v1.read_namespaced_config_map(f"carher-{uid}-user-config", NS)
        return _parse_config((cm.data or {}).get("openclaw.json", "{}"))
    except ApiException:
        return {}


# ──────────────────────────────────────
# Add
# ──────────────────────────────────────
def add_instance(
    uid: int,
    name: str,
    model_short_name: str,
    app_id: str,
    app_secret: str,
    prefix: str,
    owner: str = "",
    provider: str = "openrouter",
    image_tag: str = DEFAULT_IMAGE_TAG,
    known_bots: dict | None = None,
    known_bot_open_ids: dict | None = None,
) -> dict:
    v1 = _core()
    pfx = f"{prefix}-" if not prefix.endswith("-") else prefix

    # Resolve model
    mm = MODEL_MAP_ANTHROPIC if provider == "anthropic" else MODEL_MAP
    model_full = mm.get(model_short_name, model_short_name)

    # Build config
    cfg = _build_config(
        uid, name, model_full, app_id, app_secret, pfx, owner, provider,
        known_bots or {}, known_bot_open_ids or {},
    )

    # 1. PVC
    _ensure_pvc(v1, uid)

    # 2. ConfigMap
    _apply_configmap(v1, uid, cfg)

    # 3. Pod
    _apply_pod(v1, uid, pfx, image_tag)

    oauth_url = f"https://{pfx}u{uid}-auth.carher.net/feishu/oauth/callback"
    return {"id": uid, "status": "created", "oauth_url": oauth_url}


def _build_config(
    uid: int, name: str, model_full: str, app_id: str, app_secret: str,
    prefix: str, owner: str, provider: str,
    known_bots: dict, known_bot_open_ids: dict,
) -> dict:
    if provider == "anthropic":
        models = {
            "anthropic/claude-opus-4-6": {"alias": "opus"},
            "anthropic/claude-sonnet-4-6": {"alias": "sonnet"},
            "openrouter/anthropic/claude-opus-4.6": {"alias": "or-opus"},
            "openrouter/anthropic/claude-sonnet-4.6": {"alias": "or-sonnet"},
        }
    else:
        models = {
            "openrouter/anthropic/claude-opus-4.6": {"alias": "opus"},
            "openrouter/anthropic/claude-sonnet-4.6": {"alias": "sonnet"},
            "anthropic/claude-opus-4-6": {"alias": "or-opus"},
            "anthropic/claude-sonnet-4-6": {"alias": "or-sonnet"},
        }
    models.update({
        "openrouter/google/gemini-3.1-pro-preview": {"alias": "gemini"},
        "openrouter/minimax/minimax-m2.5": {"alias": "minimax"},
        "openrouter/z-ai/glm-5": {"alias": "glm"},
        "openrouter/openai/gpt-5.4": {"alias": "gpt"},
        "openrouter/openai/gpt-5.3-codex": {"alias": "codex"},
    })

    cfg: dict[str, Any] = {
        "$include": "./carher-config.json",
        "agents": {"defaults": {"model": {"primary": model_full}, "models": models}},
        "plugins": {"entries": {"realtime": {"config": {"gemini": {
            "projectId": GEMINI_PROJECT, "model": GEMINI_MODEL,
        }}}}},
    }

    if app_id and app_secret:
        feishu: dict[str, Any] = {
            "enabled": True, "appId": app_id, "appSecret": app_secret,
            "name": name, "groups": {"enabled": True, "archive": True},
            "oauthRedirectUri": f"https://{prefix}u{uid}-auth.carher.net/feishu/oauth/callback",
        }
        if known_bots:
            feishu["knownBots"] = known_bots
        if known_bot_open_ids:
            feishu["knownBotOpenIds"] = known_bot_open_ids
        if owner:
            feishu["dm"] = {"allowFrom": [o.strip() for o in owner.split("|") if o.strip()]}
        cfg["channels"] = {"feishu": feishu}

    if owner:
        cfg["commands"] = {"ownerAllowFrom": [o.strip() for o in owner.split("|") if o.strip()]}

    return cfg


# ──────────────────────────────────────
# Stop / Start / Restart / Delete
# ──────────────────────────────────────
def stop_instance(uid: int) -> dict:
    v1 = _core()
    try:
        v1.delete_namespaced_pod(f"carher-{uid}", NS)
        return {"id": uid, "action": "stopped"}
    except ApiException as e:
        if e.status == 404:
            return {"id": uid, "action": "already_stopped"}
        raise


def start_instance(uid: int, image_tag: str = DEFAULT_IMAGE_TAG) -> dict:
    v1 = _core()

    # Must have ConfigMap
    cfg = _get_config_data(v1, str(uid))
    if not cfg:
        return {"id": uid, "error": "ConfigMap not found, cannot start"}

    prefix = _extract_prefix_from_config(cfg)
    _apply_pod(v1, uid, prefix, image_tag)
    return {"id": uid, "action": "started"}


def restart_instance(uid: int, image_tag: str = DEFAULT_IMAGE_TAG) -> dict:
    stop_instance(uid)
    import time
    time.sleep(2)
    return start_instance(uid, image_tag)


def delete_instance(uid: int, purge: bool = False) -> dict:
    v1 = _core()
    deleted = []
    try:
        v1.delete_namespaced_pod(f"carher-{uid}", NS)
        deleted.append("pod")
    except ApiException:
        pass
    try:
        v1.delete_namespaced_config_map(f"carher-{uid}-user-config", NS)
        deleted.append("configmap")
    except ApiException:
        pass
    if purge:
        try:
            v1.delete_namespaced_persistent_volume_claim(f"carher-{uid}-data", NS)
            deleted.append("pvc")
        except ApiException:
            pass
    return {"id": uid, "action": "deleted", "purge": purge, "deleted": deleted}


# ──────────────────────────────────────
# Update config
# ──────────────────────────────────────
def update_instance(uid: int, model: str | None = None, owner: str | None = None) -> dict:
    v1 = _core()
    cfg = _get_config_data(v1, str(uid))
    if not cfg:
        return {"id": uid, "error": "ConfigMap not found"}

    if model:
        model_full = MODEL_MAP.get(model, model)
        cfg.setdefault("agents", {}).setdefault("defaults", {}).setdefault("model", {})["primary"] = model_full

    if owner is not None:
        owners = [o.strip() for o in owner.split("|") if o.strip()]
        cfg.setdefault("channels", {}).setdefault("feishu", {}).setdefault("dm", {})["allowFrom"] = owners
        cfg.setdefault("commands", {})["ownerAllowFrom"] = owners

    _apply_configmap(v1, uid, cfg)
    return {"id": uid, "action": "updated", "needs_restart": True}


# ──────────────────────────────────────
# Logs
# ──────────────────────────────────────
def get_logs(uid: int, tail: int = 200) -> str:
    v1 = _core()
    try:
        return v1.read_namespaced_pod_log(f"carher-{uid}", NS, tail_lines=tail)
    except ApiException as e:
        return f"Error: {e.reason}"


# ──────────────────────────────────────
# Health check
# ──────────────────────────────────────
def health_check() -> list[dict]:
    v1 = _core()
    results = []
    pods = v1.list_namespaced_pod(NS, label_selector="app=carher-user", field_selector="status.phase=Running")

    for pod in pods.items:
        uid = pod.metadata.labels.get("user-id", "")
        if not uid:
            continue

        cfg = _get_config_data(v1, uid)
        feishu = cfg.get("channels", {}).get("feishu", {})

        logs = ""
        try:
            logs = v1.read_namespaced_pod_log(f"carher-{uid}", NS, tail_lines=200)
        except ApiException:
            pass

        ws_ok = "ws client ready" in logs
        model_ok = "agent model" in logs

        # Memory DB check via exec
        has_memory = False
        try:
            resp = stream(
                v1.connect_get_namespaced_pod_exec,
                f"carher-{uid}", NS,
                command=["test", "-f", "/data/.openclaw/memory/main.sqlite"],
                stderr=True, stdin=False, stdout=True, tty=False,
            )
            has_memory = True
        except Exception:
            pass

        results.append({
            "id": int(uid),
            "name": feishu.get("name", ""),
            "feishu_ws": ws_ok,
            "memory_db": has_memory,
            "model_ok": model_ok,
            "status": pod.status.phase,
        })

    return sorted(results, key=lambda x: x["id"])


# ──────────────────────────────────────
# Cluster status
# ──────────────────────────────────────
def cluster_status() -> dict:
    v1 = _core()
    pods = v1.list_namespaced_pod(NS, label_selector="app=carher-user")
    total = len(pods.items)
    running = sum(1 for p in pods.items if p.status.phase == "Running")

    # Stopped = ConfigMaps without active Pods
    pod_ids = {p.metadata.labels.get("user-id") for p in pods.items}
    cms = v1.list_namespaced_config_map(NS)
    stopped = 0
    for cm in cms.items:
        m = re.match(r"carher-(\d+)-user-config", cm.metadata.name)
        if m and m.group(1) not in pod_ids:
            stopped += 1

    # Node distribution
    node_dist: dict[str, int] = {}
    for p in pods.items:
        n = p.spec.node_name or "unscheduled"
        node_dist[n] = node_dist.get(n, 0) + 1

    return {
        "total_pods": total,
        "running": running,
        "stopped": stopped,
        "nodes": [{"name": k, "pods": v} for k, v in sorted(node_dist.items())],
    }


# ──────────────────────────────────────
# Next available ID
# ──────────────────────────────────────
def next_available_id() -> int:
    v1 = _core()
    pods = v1.list_namespaced_pod(NS, label_selector="app=carher-user")
    ids = []
    for pod in pods.items:
        uid = pod.metadata.labels.get("user-id", "")
        if uid.isdigit():
            ids.append(int(uid))
    # Also check ConfigMaps for stopped instances
    cms = v1.list_namespaced_config_map(NS)
    for cm in cms.items:
        m = re.match(r"carher-(\d+)-user-config", cm.metadata.name)
        if m:
            ids.append(int(m.group(1)))
    return max(ids, default=0) + 1


# ──────────────────────────────────────
# Collect all knownBots from existing ConfigMaps
# ──────────────────────────────────────
def collect_known_bots() -> tuple[dict, dict]:
    v1 = _core()
    bots: dict[str, str] = {}
    bot_ids: dict[str, str] = {}

    cms = v1.list_namespaced_config_map(NS)
    for cm in cms.items:
        if not re.match(r"carher-\d+-user-config", cm.metadata.name):
            continue
        cfg = _parse_config((cm.data or {}).get("openclaw.json", "{}"))
        feishu = cfg.get("channels", {}).get("feishu", {})
        app_id = feishu.get("appId", "")
        name = feishu.get("name", "")
        if app_id and name:
            bots[app_id] = name
        for oid, aid in feishu.get("knownBotOpenIds", {}).items():
            bot_ids[oid] = aid

    return bots, bot_ids


# ──────────────────────────────────────
# Internal helpers: PVC, ConfigMap, Pod
# ──────────────────────────────────────
def _ensure_pvc(v1: client.CoreV1Api, uid: int):
    try:
        v1.read_namespaced_persistent_volume_claim(f"carher-{uid}-data", NS)
    except ApiException as e:
        if e.status == 404:
            pvc = client.V1PersistentVolumeClaim(
                metadata=client.V1ObjectMeta(name=f"carher-{uid}-data", namespace=NS),
                spec=client.V1PersistentVolumeClaimSpec(
                    access_modes=["ReadWriteMany"],
                    storage_class_name="alibabacloud-cnfs-nas",
                    resources=client.V1VolumeResourceRequirements(
                        requests={"storage": "5Gi"},
                    ),
                ),
            )
            v1.create_namespaced_persistent_volume_claim(NS, pvc)


def _apply_configmap(v1: client.CoreV1Api, uid: int, cfg: dict):
    cm_name = f"carher-{uid}-user-config"
    cm = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(name=cm_name, namespace=NS),
        data={"openclaw.json": json.dumps(cfg, indent=2, ensure_ascii=False)},
    )
    try:
        v1.replace_namespaced_config_map(cm_name, NS, cm)
    except ApiException as e:
        if e.status == 404:
            v1.create_namespaced_config_map(NS, cm)
        else:
            raise


def _apply_pod(v1: client.CoreV1Api, uid: int, prefix: str, image_tag: str):
    pod_name = f"carher-{uid}"

    # Delete existing
    try:
        v1.delete_namespaced_pod(pod_name, NS)
        import time
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
                    client.V1EnvVar(name="VOICE_FE_HOST", value=f"{prefix}u{uid}-fe.carher.net"),
                    client.V1EnvVar(name="VOICE_PROXY_HOST", value=f"{prefix}u{uid}-proxy.carher.net"),
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
