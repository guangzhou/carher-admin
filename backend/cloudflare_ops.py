"""Cloudflare Tunnel management for CarHer.

Manages cloudflared config (ConfigMap), DNS routes, and remote tunnel
ingress so that new Her instances are automatically reachable via
Cloudflare Tunnel.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request
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

CF_ACCOUNT_ID = "67e6618e6af7e4342cbd1de02536fa2f"
CF_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "")
CF_TUNNEL_CONFIG_URL = (
    f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}"
    f"/cfd_tunnel/{TUNNEL_UUID}/configurations"
)

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


def _wait_for_svc_cluster_ip(svc_name: str, retries: int = 5, delay_seconds: int = 3) -> str | None:
    """Poll for a Service ClusterIP for a short period."""
    for i in range(retries):
        svc_ip = _get_svc_cluster_ip(svc_name)
        if svc_ip:
            return svc_ip
        if i < retries - 1:
            time.sleep(delay_seconds)
    return None


AUTH_PROXY_SVC = "auth-proxy"
ADMIN_SVC = "carher-admin"
MANAGED_INSTANCE_HOST_RE = re.compile(rf"^[^.]+-u\d+-(auth|fe|proxy)\.{re.escape(DOMAIN)}$")

# Infrastructure services that need dedicated tunnel routes.
# These are inserted BEFORE the wildcard catch-all so they take priority.
# Format: (hostname_prefix, service_name, port)
INFRA_ROUTES: list[tuple[str, str, int]] = [
    ("litellm", "litellm-proxy", 8080),
]


def _normalize_prefix(prefix: str) -> str:
    return f"{prefix}-" if not prefix.endswith("-") else prefix


def _list_active_instances() -> list[tuple[int, str]]:
    """List active instances as (uid, prefix), preferring CRDs over DB rows."""
    from . import crd_ops, database as db

    seen_uids: set[int] = set()
    instances: list[tuple[int, str]] = []

    try:
        for inst in crd_ops.list_her_instances():
            spec = inst.get("spec", {})
            uid = spec.get("userId", 0)
            if not uid:
                continue
            seen_uids.add(uid)
            instances.append((uid, spec.get("prefix", "s1")))
    except Exception as e:
        logger.warning("Failed to list CRD instances for tunnel sync, falling back to DB-only: %s", e)

    try:
        for inst in db.list_all():
            uid = inst.get("id", 0)
            if not uid or uid in seen_uids or inst.get("status") == "deleted":
                continue
            instances.append((uid, inst.get("prefix", "s1")))
    except Exception as e:
        logger.warning("Failed to list DB instances for tunnel sync: %s", e)

    return sorted(set(instances), key=lambda item: item[0])


def _build_instance_hostnames(uid: int, prefix: str) -> list[str]:
    pfx = _normalize_prefix(prefix)
    return [
        f"{pfx}u{uid}-auth.{DOMAIN}",
        f"{pfx}u{uid}-fe.{DOMAIN}",
        f"{pfx}u{uid}-proxy.{DOMAIN}",
    ]


def _is_managed_remote_hostname(hostname: str) -> bool:
    if not hostname:
        return False
    if hostname in {f"{hostname_prefix}.{DOMAIN}" for hostname_prefix, _, _ in INFRA_ROUTES}:
        return True
    return bool(MANAGED_INSTANCE_HOST_RE.match(hostname))


def _build_infra_rules(*, include_origin_request: bool) -> list[dict[str, str] | dict[str, object]]:
    rules, _ = _resolve_infra_rules(include_origin_request=include_origin_request)
    return rules


def _resolve_infra_rules(*, include_origin_request: bool) -> tuple[list[dict[str, str] | dict[str, object]], set[str]]:
    rules: list[dict[str, str] | dict[str, object]] = []
    unresolved_hostnames: set[str] = set()
    for hostname_prefix, svc_name, port in INFRA_ROUTES:
        hostname = f"{hostname_prefix}.{DOMAIN}"
        ip = _get_svc_cluster_ip(svc_name)
        if not ip:
            logger.warning("Infra service %s not found, skipping %s route", svc_name, hostname)
            unresolved_hostnames.add(hostname)
            continue
        rule: dict[str, str] | dict[str, object] = {
            "hostname": hostname,
            "service": f"http://{ip}:{port}",
        }
        if include_origin_request:
            rule["originRequest"] = {}
        rules.append(rule)
    return rules, unresolved_hostnames


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

    ingress.extend(_build_infra_rules(include_origin_request=False))

    proxy_ip = _get_svc_cluster_ip(AUTH_PROXY_SVC)
    if proxy_ip:
        proxy_service = f"http://{proxy_ip}:80"
        for uid, prefix in _list_active_instances():
            auth_host, fe_host, proxy_host = _build_instance_hostnames(uid, prefix)
            ingress.extend(
                [
                    {"hostname": auth_host, "service": proxy_service},
                    {"hostname": fe_host, "service": proxy_service},
                    {"hostname": proxy_host, "service": proxy_service},
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
    """Register Cloudflare DNS CNAME routes for a new instance via cloudflared pod."""
    hostnames = _build_instance_hostnames(uid, prefix)

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


# ── Remote Tunnel Ingress (Cloudflare API) ──


def _cf_api(method: str, url: str, data: dict | None = None) -> dict:
    """Make authenticated request to Cloudflare API."""
    if not CF_TOKEN:
        raise RuntimeError("CLOUDFLARE_API_TOKEN is not configured")
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": f"Bearer {CF_TOKEN}",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _get_remote_ingress() -> tuple[dict, list[dict]]:
    """GET the current remote tunnel config. Returns (full_config, ingress_list)."""
    resp = _cf_api("GET", CF_TUNNEL_CONFIG_URL)
    config = resp["result"]["config"]
    ingress = config.get("ingress")
    if not isinstance(ingress, list) or not ingress:
        raise RuntimeError("Cloudflare remote tunnel config has no ingress rules")
    return config, ingress


def _put_remote_ingress(config: dict) -> bool:
    """PUT updated remote tunnel config. Returns success bool."""
    resp = _cf_api("PUT", CF_TUNNEL_CONFIG_URL, {"config": config})
    return resp.get("success", False)


def update_remote_ingress(
    instances: list[tuple[int, str]] | None = None,
    wait_for_service: bool = True,
):
    """Upsert remote tunnel ingress rules for instances and infra routes.

    Args:
        instances: list of (uid, prefix) tuples to reconcile. If None, reconcile
                   all active instances from CRD/DB.
        wait_for_service: if True, resolve each instance's ClusterIP for routing;
                          if Service not yet ready, skip that instance with a warning.

    The remote ingress is the authoritative routing config for the tunnel
    (the K8s ConfigMap is overridden by Cloudflare at runtime).
    """
    full_sync = instances is None
    target_instances = _list_active_instances() if full_sync else (instances or [])
    config, ingress = _get_remote_ingress()
    catch_all = ingress[-1]
    existing_rules = ingress[:-1]

    desired_rules: list[dict] = []
    managed_hostnames: set[str] = set()
    unresolved_hostnames: set[str] = set()
    unresolved_uids: list[int] = []
    for uid, prefix in target_instances:
        auth_host, fe_host, proxy_host = _build_instance_hostnames(uid, prefix)
        hostnames = [auth_host, fe_host, proxy_host]
        svc_name = f"carher-{uid}-svc"
        svc_ip = _wait_for_svc_cluster_ip(svc_name) if wait_for_service else _get_svc_cluster_ip(svc_name)
        if not svc_ip:
            logger.warning("Service %s not ready, skipping remote ingress for uid=%d", svc_name, uid)
            unresolved_uids.append(uid)
            if full_sync:
                unresolved_hostnames.update(hostnames)
            continue

        managed_hostnames.update(hostnames)
        desired_rules.extend([
            {"hostname": auth_host, "service": f"http://{svc_ip}:18891", "originRequest": {}},
            {"hostname": fe_host, "service": f"http://{svc_ip}:8000", "originRequest": {}},
            {"hostname": proxy_host, "service": f"http://{svc_ip}:8080", "originRequest": {}},
        ])

    infra_rules, unresolved_infra_hostnames = _resolve_infra_rules(include_origin_request=True)
    if full_sync:
        unresolved_hostnames.update(unresolved_infra_hostnames)
    managed_hostnames.update(
        str(rule["hostname"]) for rule in infra_rules if isinstance(rule.get("hostname"), str)
    )
    desired_rules.extend(infra_rules)

    if full_sync:
        preserved_rules = [
            rule
            for rule in existing_rules
            if not _is_managed_remote_hostname(rule.get("hostname", ""))
            or rule.get("hostname", "") in unresolved_hostnames
        ]
    else:
        preserved_rules = [rule for rule in existing_rules if rule.get("hostname", "") not in managed_hostnames]
    new_ingress = preserved_rules + desired_rules + [catch_all]

    if ingress == new_ingress:
        logger.info("Remote ingress already matches desired state")
        return {
            "updated": False,
            "updated_hosts": sorted(managed_hostnames),
            "unresolved_uids": unresolved_uids,
            "unresolved_hostnames": sorted(unresolved_hostnames),
        }

    config["ingress"] = new_ingress
    ok = _put_remote_ingress(config)
    updated_hosts = sorted(managed_hostnames)
    if ok:
        logger.info("Remote ingress updated for %s", updated_hosts)
        return {
            "updated": True,
            "updated_hosts": updated_hosts,
            "unresolved_uids": unresolved_uids,
            "unresolved_hostnames": sorted(unresolved_hostnames),
        }
    else:
        raise RuntimeError(f"Remote ingress PUT failed for {updated_hosts}")
