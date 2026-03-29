"""Cloudflare Tunnel management for CarHer.

Manages cloudflared config (ConfigMap) and DNS routes so that
new Her instances are automatically reachable via Cloudflare Tunnel.
"""

from __future__ import annotations

import logging
import yaml
from typing import Any

from kubernetes import client
from kubernetes.client.rest import ApiException

logger = logging.getLogger("carher-admin")

NS = "carher"
TUNNEL_UUID = "0e83a70f-93d9-4c17-86cc-7600f52696a2"
TUNNEL_NAME = "carher-k8s"
CRED_SECRET_NAME = "cloudflared-credentials"
CONFIG_CM_NAME = "cloudflared-config"
CLOUDFLARED_DEPLOYMENT = "cloudflared"
DOMAIN = "carher.net"

_v1_instance: client.CoreV1Api | None = None
_apps_instance: client.AppsV1Api | None = None
_custom_instance: client.CustomObjectsApi | None = None


def _v1() -> client.CoreV1Api:
    global _v1_instance
    if _v1_instance is None:
        _v1_instance = client.CoreV1Api()
    return _v1_instance


def _apps() -> client.AppsV1Api:
    global _apps_instance
    if _apps_instance is None:
        _apps_instance = client.AppsV1Api()
    return _apps_instance


def _crd_api() -> client.CustomObjectsApi:
    global _custom_instance
    if _custom_instance is None:
        _custom_instance = client.CustomObjectsApi()
    return _custom_instance


def _get_svc_cluster_ip(svc_name: str) -> str | None:
    """Get ClusterIP of a service, or None if not found."""
    try:
        svc = _v1().read_namespaced_service(svc_name, NS)
        return svc.spec.cluster_ip
    except ApiException:
        return None


def generate_config() -> str:
    """Generate cloudflared config.yml from all active HerInstance CRDs + admin."""
    ingress: list[dict[str, str]] = []

    # Admin service
    admin_ip = _get_svc_cluster_ip("carher-admin-svc")
    if admin_ip:
        ingress.append({"hostname": f"admin.{DOMAIN}", "service": f"http://{admin_ip}:8900"})

    # Her instances from CRDs
    api = _crd_api()
    try:
        items = api.list_namespaced_custom_object(
            "carher.io", "v1alpha1", NS, "herinstances"
        ).get("items", [])
    except ApiException:
        items = []

    for inst in items:
        spec = inst.get("spec", {})
        uid = spec.get("userId", 0)
        prefix = spec.get("prefix", "s1")
        paused = spec.get("paused", False)
        if not uid or paused:
            continue

        svc_ip = _get_svc_cluster_ip(f"carher-{uid}-svc")
        if not svc_ip:
            logger.warning("No Service for carher-%d, skipping cloudflare config", uid)
            continue

        pfx = f"{prefix}-" if not prefix.endswith("-") else prefix
        ingress.append({"hostname": f"{pfx}u{uid}-auth.{DOMAIN}", "service": f"http://{svc_ip}:18891"})
        ingress.append({"hostname": f"{pfx}u{uid}-fe.{DOMAIN}", "service": f"http://{svc_ip}:8000"})
        ingress.append({"hostname": f"{pfx}u{uid}-proxy.{DOMAIN}", "service": f"http://{svc_ip}:8080"})

    # Catch-all must be last
    ingress.append({"service": "http_status:404"})

    config_data = {
        "tunnel": TUNNEL_UUID,
        "credentials-file": "/etc/cloudflared/credentials.json",
        "ingress": ingress,
    }
    return yaml.dump(config_data, default_flow_style=False, allow_unicode=True)


def update_configmap() -> bool:
    """Regenerate cloudflared config and update the ConfigMap. Returns True if changed."""
    new_config = generate_config()
    v1 = _v1()

    try:
        cm = v1.read_namespaced_config_map(CONFIG_CM_NAME, NS)
        old = cm.data.get("config.yml", "") if cm.data else ""
        if old == new_config:
            return False
        cm.data = {"config.yml": new_config}
        v1.replace_namespaced_config_map(CONFIG_CM_NAME, NS, cm)
        logger.info("Updated cloudflared ConfigMap")
        return True
    except ApiException as e:
        if e.status == 404:
            cm = client.V1ConfigMap(
                metadata=client.V1ObjectMeta(name=CONFIG_CM_NAME, namespace=NS),
                data={"config.yml": new_config},
            )
            v1.create_namespaced_config_map(NS, cm)
            logger.info("Created cloudflared ConfigMap")
            return True
        raise


def restart_cloudflared():
    """Restart cloudflared by patching a restart annotation on the Deployment."""
    import datetime
    apps = _apps()
    try:
        patch = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "carher.io/restartedAt": datetime.datetime.now(datetime.timezone.utc).isoformat()
                        }
                    }
                }
            }
        }
        apps.patch_namespaced_deployment(CLOUDFLARED_DEPLOYMENT, NS, patch)
        logger.info("Triggered cloudflared restart")
    except ApiException as e:
        logger.error("Failed to restart cloudflared: %s", e)


def sync_tunnel_config():
    """Full sync: regenerate config, update ConfigMap, restart if changed."""
    changed = update_configmap()
    if changed:
        restart_cloudflared()
    return changed


def register_dns_routes(uid: int, prefix: str = "s1"):
    """Register Cloudflare DNS CNAME routes for a new instance.
    Runs 'cloudflared tunnel route dns' via exec into the cloudflared pod.
    """
    pfx = f"{prefix}-" if not prefix.endswith("-") else prefix
    hostnames = [
        f"{pfx}u{uid}-auth.{DOMAIN}",
        f"{pfx}u{uid}-fe.{DOMAIN}",
        f"{pfx}u{uid}-proxy.{DOMAIN}",
    ]

    v1 = _v1()
    try:
        pods = v1.list_namespaced_pod(NS, label_selector=f"app={CLOUDFLARED_DEPLOYMENT}")
        if not pods.items:
            logger.warning("No cloudflared pod found; DNS routes must be registered manually")
            return []
        pod_name = pods.items[0].metadata.name
    except ApiException:
        logger.error("Failed to find cloudflared pod for DNS registration")
        return []

    results = []
    from kubernetes.stream import stream
    for hostname in hostnames:
        try:
            resp = stream(
                v1.connect_get_namespaced_pod_exec,
                pod_name, NS,
                command=["cloudflared", "tunnel", "route", "dns", "--overwrite-dns", TUNNEL_NAME, hostname],
                stderr=True, stdout=True, stdin=False, tty=False,
            )
            results.append({"hostname": hostname, "ok": True, "output": resp})
            logger.info("DNS route created: %s", hostname)
        except Exception as e:
            results.append({"hostname": hostname, "ok": False, "error": str(e)})
            logger.error("DNS route failed for %s: %s", hostname, e)

    return results
