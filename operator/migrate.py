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
  - Legacy bare Pods are replaced by operator-managed Deployments
  - carher-admin switches to CRD mode
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import re
import sys
import time

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
            owner_refs = pod.metadata.owner_references or []
            is_bare_pod = not any(ref.controller for ref in owner_refs)
            pods[int(uid_str)] = {
                "name": pod.metadata.name,
                "phase": pod.status.phase,
                "image_tag": tag,
                "is_bare_pod": is_bare_pod,
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
        primary_lower = primary.lower()
        owners = feishu.get("dm", {}).get("allowFrom", [])
        oauth_redirect_uri = feishu.get("oauthRedirectUri", "")

        # Detect model
        model = "gpt"
        for short, patterns in [
            ("sonnet", ["sonnet"]),
            ("opus", ["opus"]),
            ("gemini", ["gemini"]),
            ("gpt", ["gpt-5", "gpt-4", "gpt"]),
        ]:
            if any(p in primary_lower for p in patterns):
                model = short
                break

        # Detect provider
        if primary_lower.startswith("litellm/"):
            provider = "litellm"
        elif primary_lower.startswith("wangsu/"):
            provider = "wangsu"
        elif primary_lower.startswith("anthropic/"):
            provider = "anthropic"
        elif primary_lower.startswith("openrouter/"):
            provider = "openrouter"
        else:
            provider = "wangsu"

        # Detect prefix from OAuth URL
        prefix = "s1"
        oauth = feishu.get("oauthRedirectUri", "")
        pm = re.match(r"https://(s\d+)-u", oauth)
        if pm:
            prefix = pm.group(1)

        image_tag = pod_info.get("image_tag", "v20260328")
        is_running = pod_info.get("phase") == "Running"
        bare_pod_name = pod_info.get("name", "") if pod_info.get("is_bare_pod") else ""
        final_paused = not is_running
        initial_paused = final_paused or bool(bare_pod_name)

        if args.dry_run:
            logger.info(
                "DRY-RUN her-%d: name=%s model=%s provider=%s prefix=%s image=%s running=%s bare_pod=%s",
                uid, name, model, provider, prefix, image_tag, is_running, bool(bare_pod_name),
            )
            created += 1
            continue

        secret_name = f"carher-{uid}-secret"
        secret_ref = ""

        # Create or reuse K8s Secret for app_secret
        if app_secret:
            secret = client.V1Secret(
                metadata=client.V1ObjectMeta(
                    name=secret_name, namespace=NS,
                    labels={"app": "carher-user", "user-id": str(uid)},
                ),
                type="Opaque",
                data={"app_secret": base64.b64encode(app_secret.encode()).decode()},
            )
            try:
                v1.create_namespaced_secret(NS, secret)
            except ApiException as e:
                if e.status == 409:
                    v1.replace_namespaced_secret(secret_name, NS, secret)
                else:
                    logger.error("her-%d: failed to create secret: %s", uid, e)
                    errors += 1
                    continue
            secret_ref = secret_name
        else:
            try:
                v1.read_namespaced_secret(secret_name, NS)
                secret_ref = secret_name
            except ApiException as e:
                if e.status == 404:
                    logger.warning("her-%d: no appSecret in config and no Secret %s, skipping", uid, secret_name)
                    skipped += 1
                    continue
                raise

        # Create HerInstance CRD
        spec = {
            "userId": uid,
            "name": name,
            "model": model,
            "appId": app_id,
            "prefix": prefix,
            "owner": "|".join(owners),
            "provider": provider,
            "botOpenId": bot_open_id,
            "deployGroup": "stable",
            "image": image_tag,
            "paused": initial_paused,
        }
        if secret_ref:
            spec["appSecretRef"] = secret_ref
        if oauth_redirect_uri:
            spec["oauthRedirectUri"] = oauth_redirect_uri

        body = {
            "apiVersion": f"{CRD_GROUP}/{CRD_VERSION}",
            "kind": "HerInstance",
            "metadata": {
                "name": f"her-{uid}",
                "namespace": NS,
                "labels": {"app": "carher-user", "user-id": str(uid)},
            },
            "spec": spec,
        }

        try:
            crd_api.create_namespaced_custom_object(CRD_GROUP, CRD_VERSION, NS, CRD_PLURAL, body)
            logger.info("her-%d: CRD created (paused=%s)", uid, initial_paused)

            if bare_pod_name:
                logger.info("her-%d: deleting legacy bare pod %s before operator rollout", uid, bare_pod_name)
                v1.delete_namespaced_pod(bare_pod_name, NS, grace_period_seconds=10)
                for _ in range(30):
                    try:
                        v1.read_namespaced_pod(bare_pod_name, NS)
                    except ApiException as e:
                        if e.status == 404:
                            break
                        raise
                    time.sleep(1)
                else:
                    logger.error("her-%d: legacy bare pod %s did not terminate in time", uid, bare_pod_name)
                    errors += 1
                    continue

            if initial_paused != final_paused:
                crd_api.patch_namespaced_custom_object(
                    CRD_GROUP,
                    CRD_VERSION,
                    NS,
                    CRD_PLURAL,
                    f"her-{uid}",
                    {"spec": {"paused": final_paused}},
                )
                logger.info("her-%d: unpaused CRD after legacy pod removal", uid)
            created += 1
        except ApiException as e:
            logger.error("her-%d: failed to create CRD: %s", uid, e)
            errors += 1

    logger.info("Migration complete: %d created, %d skipped, %d errors", created, skipped, errors)


if __name__ == "__main__":
    main()
