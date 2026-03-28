"""Migration tool: convert existing bare Pods + ConfigMaps → HerInstance CRDs.

Usage:
  # Dry-run (just show what would be created)
  python -m operator.migrate --dry-run

  # Actually migrate
  python -m operator.migrate

  # Migrate specific user
  python -m operator.migrate --uid=14

This scans the carher namespace for:
  1. carher-N-user-config ConfigMaps → extract app_id, name, model, etc.
  2. carher-N Pods → extract image tag, running state
  3. Creates HerInstance CRDs + Secrets for each discovered instance

After migration:
  - Operator takes over management of all instances
  - Bare Pods are adopted (operator won't recreate them until next update)
  - carher-admin switches to CRD mode
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import re
import sys

from kubernetes import client, config
from kubernetes.client.rest import ApiException

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("migrate")

NS = "carher"
CRD_GROUP = "carher.io"
CRD_VERSION = "v1alpha1"
CRD_PLURAL = "herinstances"


def main():
    parser = argparse.ArgumentParser(description="Migrate bare Pods to HerInstance CRDs")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be created")
    parser.add_argument("--uid", type=int, help="Migrate a specific user only")
    args = parser.parse_args()

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()

    v1 = client.CoreV1Api()
    crd_api = client.CustomObjectsApi()

    # Scan existing ConfigMaps
    configmaps = {}
    for cm in v1.list_namespaced_config_map(NS).items:
        m = re.match(r"carher-(\d+)-user-config", cm.metadata.name)
        if m:
            uid = int(m.group(1))
            data = (cm.data or {}).get("openclaw.json", "")
            try:
                configmaps[uid] = json.loads(data) if data else {}
            except json.JSONDecodeError:
                configmaps[uid] = {}

    # Scan existing Pods
    pods = {}
    for pod in v1.list_namespaced_pod(NS, label_selector="app=carher-user").items:
        uid_str = pod.metadata.labels.get("user-id", "")
        if uid_str and uid_str.isdigit():
            image = pod.spec.containers[0].image if pod.spec.containers else ""
            tag = image.split(":")[-1] if ":" in image else "v20260328"
            pods[int(uid_str)] = {
                "phase": pod.status.phase,
                "image_tag": tag,
            }

    all_uids = sorted(set(configmaps.keys()) | set(pods.keys()))
    if args.uid:
        all_uids = [args.uid]

    logger.info("Found %d instances to migrate", len(all_uids))
    created = 0
    skipped = 0
    errors = 0

    for uid in all_uids:
        cfg = configmaps.get(uid, {})
        pod_info = pods.get(uid, {})
        feishu = cfg.get("channels", {}).get("feishu", {})

        if not feishu.get("appId"):
            logger.warning("her-%d: no appId in config, skipping", uid)
            skipped += 1
            continue

        # Check if CRD already exists
        try:
            crd_api.get_namespaced_custom_object(CRD_GROUP, CRD_VERSION, NS, CRD_PLURAL, f"her-{uid}")
            logger.info("her-%d: CRD already exists, skipping", uid)
            skipped += 1
            continue
        except ApiException as e:
            if e.status != 404:
                raise

        # Extract data from config
        app_id = feishu.get("appId", "")
        app_secret = feishu.get("appSecret", "")
        name = feishu.get("name", "")
        bot_open_id = feishu.get("botOpenId", "")
        primary = cfg.get("agents", {}).get("defaults", {}).get("model", {}).get("primary", "")
        owners = feishu.get("dm", {}).get("allowFrom", [])

        # Detect model
        model = "gpt"
        for short, patterns in [("sonnet", ["sonnet"]), ("opus", ["opus"]), ("gpt", ["gpt-5"])]:
            if any(p in primary.lower() for p in patterns):
                model = short
                break

        # Detect provider
        provider = "anthropic" if primary.startswith("anthropic/") else "openrouter"

        # Detect prefix from OAuth URL
        prefix = "s1"
        oauth = feishu.get("oauthRedirectUri", "")
        pm = re.match(r"https://(s\d+)-u", oauth)
        if pm:
            prefix = pm.group(1)

        image_tag = pod_info.get("image_tag", "v20260328")
        is_running = pod_info.get("phase") == "Running"

        if args.dry_run:
            logger.info(
                "DRY-RUN her-%d: name=%s model=%s provider=%s prefix=%s image=%s running=%s",
                uid, name, model, provider, prefix, image_tag, is_running,
            )
            created += 1
            continue

        # Create K8s Secret for app_secret
        if app_secret:
            secret = client.V1Secret(
                metadata=client.V1ObjectMeta(
                    name=f"carher-{uid}-secret", namespace=NS,
                    labels={"app": "carher-user", "user-id": str(uid)},
                ),
                type="Opaque",
                data={"app_secret": base64.b64encode(app_secret.encode()).decode()},
            )
            try:
                v1.create_namespaced_secret(NS, secret)
            except ApiException as e:
                if e.status == 409:
                    v1.replace_namespaced_secret(f"carher-{uid}-secret", NS, secret)
                else:
                    logger.error("her-%d: failed to create secret: %s", uid, e)
                    errors += 1
                    continue

        # Create HerInstance CRD
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
                "name": name,
                "model": model,
                "appId": app_id,
                "appSecretRef": f"carher-{uid}-secret",
                "prefix": prefix,
                "owner": "|".join(owners),
                "provider": provider,
                "botOpenId": bot_open_id,
                "deployGroup": "stable",
                "image": image_tag,
                "paused": not is_running,
            },
        }

        try:
            crd_api.create_namespaced_custom_object(CRD_GROUP, CRD_VERSION, NS, CRD_PLURAL, body)
            logger.info("her-%d: CRD created (paused=%s)", uid, not is_running)
            created += 1
        except ApiException as e:
            logger.error("her-%d: failed to create CRD: %s", uid, e)
            errors += 1

    logger.info("Migration complete: %d created, %d skipped, %d errors", created, skipped, errors)


if __name__ == "__main__":
    main()
