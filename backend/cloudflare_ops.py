"""Cloudflare Tunnel management for CarHer.

Manages cloudflared config (ConfigMap) and DNS routes so that
new Her instances are automatically reachable via Cloudflare Tunnel.
"""

from __future__ import annotations

import logging
import re
import yaml

from kubernetes import client
from kubernetes.client.rest import ApiException

logger = logging.getLogger("carher-admin")

NS = "carher"
TUNNEL_UUID = "0e83a70f-93d9-4c17-86cc-7600f52696a2"
TUNNEL_NAME = "carher-k8s"
CONFIG_CM_NAME = "cloudflared-config"
CLOUDFLARED_DEPLOYMENT = "cloudflared"
DOMAIN = "carher.net"

_v1_instance: client.CoreV1Api | None = None
_apps_instance: client.AppsV1Api | None = None


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


def _get_svc_cluster_ip(svc_name: str) -> str | None:
    """Get ClusterIP of a service, or None if not found."""
    try:
        svc = _v1().read_namespaced_service(svc_name, NS)
        return svc.spec.cluster_ip
    except ApiException:
        return None


AUTH_PROXY_SVC = "auth-proxy"
ADMIN_SVC = "carher-admin-svc"
INSTANCE_SERVICE_RE = re.compile(r"^carher-(\d+)-svc$")


def _list_instance_uids() -> list[int]:
    """List K8s-backed CarHer instance IDs from Services."""
    try:
        services = _v1().list_namespaced_service(NS).items
    except ApiException as e:
        logger.error("Failed to list services for tunnel config generation: %s", e)
        return []

    uids: list[int] = []
    for svc in services:
        name = svc.metadata.name if svc.metadata else ""
        match = INSTANCE_SERVICE_RE.match(name)
        if match:
            uids.append(int(match.group(1)))
    return sorted(set(uids))


def generate_config() -> str:
    """Generate cloudflared config.yml using stable auth-proxy backends.

    Each K8s instance still gets explicit public hostnames so Cloudflare edge
    learns them, but they all point to the stable in-cluster auth-proxy
    instead of ephemeral per-instance Pod IPs.
    """
    ingress: list[dict[str, str]] = []

    admin_ip = _get_svc_cluster_ip(ADMIN_SVC)
    if admin_ip:
        ingress.append({"hostname": f"admin.{DOMAIN}", "service": f"http://{admin_ip}:8900"})

    proxy_ip = _get_svc_cluster_ip(AUTH_PROXY_SVC)
    if proxy_ip:
        proxy_service = f"http://{proxy_ip}:80"
        for uid in _list_instance_uids():
            ingress.extend(
                [
                    {"hostname": f"s1-u{uid}-auth.{DOMAIN}", "service": proxy_service},
                    {"hostname": f"s1-u{uid}-fe.{DOMAIN}", "service": proxy_service},
                    {"hostname": f"s1-u{uid}-proxy.{DOMAIN}", "service": proxy_service},
                ]
            )
        ingress.append({"hostname": f"*.{DOMAIN}", "service": f"http://{proxy_ip}:80"})
    else:
        logger.error("auth-proxy service not found, wildcard route will be missing!")

    ingress.append({"service": "http_status:404"})

    config_data = {
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


def sync_tunnel_config(wait_for_service: str | None = None, retries: int = 5):
    """Full sync: regenerate config, update ConfigMap, restart if changed.
    If wait_for_service is given, poll until that Service exists before generating config.
    """
    if wait_for_service:
        import time
        for i in range(retries):
            if _get_svc_cluster_ip(wait_for_service):
                break
            time.sleep(3)

    changed = update_configmap()
    if changed:
        restart_cloudflared()
    return changed


def register_dns_routes(uid: int, prefix: str = "s1"):
    """Register Cloudflare DNS CNAME routes for a new instance.

    Even though the tunnel ingress uses a stable wildcard -> auth-proxy route,
    new public hostnames still need explicit DNS records so Cloudflare can
    route external traffic to the K8s tunnel consistently.
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
                pod_name,
                NS,
                command=["cloudflared", "tunnel", "route", "dns", "--overwrite-dns", TUNNEL_NAME, hostname],
                stderr=True,
                stdout=True,
                stdin=False,
                tty=False,
            )
            results.append({"hostname": hostname, "ok": True, "output": resp})
            logger.info("DNS route created: %s", hostname)
        except Exception as e:
            results.append({"hostname": hostname, "ok": False, "error": str(e)})
            logger.error("DNS route failed for %s: %s", hostname, e)

    return results
