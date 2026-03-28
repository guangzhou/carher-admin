"""Central knownBots management.

Instead of embedding knownBots in every per-user ConfigMap independently,
the operator:
  1. Scans all HerInstance CRDs to collect bot identities
  2. Stores the result in a shared ConfigMap (carher-known-bots)
  3. Per-user ConfigMaps reference the same computed knownBots

This eliminates the O(N²) problem: adding a bot is O(N) regeneration,
not O(N) ConfigMap writes + O(N) Pod restarts.
"""

from __future__ import annotations

import json
import logging

from kubernetes import client

logger = logging.getLogger("carher-operator")

NS = "carher"
CRD_GROUP = "carher.io"
CRD_VERSION = "v1alpha1"
CRD_PLURAL = "herinstances"
KNOWN_BOTS_CM = "carher-known-bots"

_cache: dict = {"bots": {}, "open_ids": {}}


def rebuild_known_bots_configmap():
    """Scan all HerInstance CRDs and rebuild the shared knownBots ConfigMap."""
    crd_api = client.CustomObjectsApi()
    items = crd_api.list_namespaced_custom_object(CRD_GROUP, CRD_VERSION, NS, CRD_PLURAL)

    bots: dict[str, str] = {}
    open_ids: dict[str, str] = {}

    for item in items.get("items", []):
        spec = item.get("spec", {})
        app_id = spec.get("appId", "")
        name = spec.get("name", "")
        bot_open_id = spec.get("botOpenId", "")
        paused = spec.get("paused", False)

        if paused:
            continue
        if app_id and name:
            bots[app_id] = name
        if bot_open_id and app_id:
            open_ids[bot_open_id] = app_id

    # Update shared ConfigMap
    v1 = client.CoreV1Api()
    cm = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(
            name=KNOWN_BOTS_CM, namespace=NS,
            labels={"app": "carher", "managed-by": "carher-operator"},
        ),
        data={
            "knownBots.json": json.dumps(bots, ensure_ascii=False),
            "knownBotOpenIds.json": json.dumps(open_ids, ensure_ascii=False),
        },
    )
    try:
        v1.replace_namespaced_config_map(KNOWN_BOTS_CM, NS, cm)
    except client.rest.ApiException as e:
        if e.status == 404:
            v1.create_namespaced_config_map(NS, cm)
        else:
            raise

    _cache["bots"] = bots
    _cache["open_ids"] = open_ids
    logger.info("knownBots rebuilt: %d bots, %d open_ids", len(bots), len(open_ids))


def get_known_bots() -> tuple[dict[str, str], dict[str, str]]:
    """Get cached knownBots. Falls back to reading ConfigMap if cache empty."""
    if _cache["bots"]:
        return _cache["bots"], _cache["open_ids"]

    v1 = client.CoreV1Api()
    try:
        cm = v1.read_namespaced_config_map(KNOWN_BOTS_CM, NS)
        data = cm.data or {}
        bots = json.loads(data.get("knownBots.json", "{}"))
        open_ids = json.loads(data.get("knownBotOpenIds.json", "{}"))
        _cache["bots"] = bots
        _cache["open_ids"] = open_ids
        return bots, open_ids
    except Exception:
        return {}, {}
