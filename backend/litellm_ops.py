"""LiteLLM virtual key management.

Generates per-instance keys for spend tracking via LiteLLM proxy API.
For CRD-managed instances, the authoritative mapping is stored in HerInstance
spec.litellmKey. Legacy DB-managed instances keep using her_instances.litellm_key.
"""

from __future__ import annotations

import logging
import os
import urllib.parse
import urllib.request
import json

logger = logging.getLogger("carher-admin")

LITELLM_PROXY_URL = os.getenv(
    "LITELLM_PROXY_URL", "http://litellm-proxy.carher.svc:4000"
)
LITELLM_MASTER_KEY = os.getenv(
    "LITELLM_MASTER_KEY", ""
)

ALL_MODELS = [
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "gpt-5.4",
    "gemini-3.1-pro-preview",
    "minimax-m2.7",
    "glm-5",
    "gpt-5.3-codex",
    "BAAI/bge-m3",
]


def generate_key(uid: int, name: str = "") -> str | None:
    """Create a LiteLLM virtual key for a her instance. Returns the key string."""
    if not LITELLM_MASTER_KEY:
        logger.error("LITELLM_MASTER_KEY is not configured")
        return None
    alias = f"carher-{uid}"
    body = json.dumps({
        "user_id": alias,
        "key_alias": alias,
        "metadata": {"instance": alias, "owner_name": name},
        "models": ALL_MODELS,
    }).encode()
    req = urllib.request.Request(
        f"{LITELLM_PROXY_URL}/key/generate",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {LITELLM_MASTER_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        key = data.get("key", "")
        if key:
            logger.info("Generated LiteLLM key for carher-%d: %s...%s", uid, key[:6], key[-4:])
        return key
    except Exception as e:
        logger.error("Failed to generate LiteLLM key for carher-%d: %s", uid, e)
        return None


def delete_key(key: str) -> bool:
    """Delete a LiteLLM virtual key."""
    if not key:
        return False
    if not LITELLM_MASTER_KEY:
        logger.error("LITELLM_MASTER_KEY is not configured")
        return False
    body = json.dumps({"keys": [key]}).encode()
    req = urllib.request.Request(
        f"{LITELLM_PROXY_URL}/key/delete",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {LITELLM_MASTER_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        logger.info("Deleted LiteLLM key: %s...%s", key[:6], key[-4:])
        return True
    except Exception as e:
        logger.error("Failed to delete LiteLLM key: %s", e)
        return False


def get_key_info(key: str) -> dict | None:
    """Get spend info for a key."""
    if not key:
        return None
    if not LITELLM_MASTER_KEY:
        logger.error("LITELLM_MASTER_KEY is not configured")
        return None
    req = urllib.request.Request(
        f"{LITELLM_PROXY_URL}/key/info?key={urllib.parse.quote(key, safe='')}",
        headers={"Authorization": f"Bearer {LITELLM_MASTER_KEY}"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read())
    except Exception as e:
        logger.error("Failed to get LiteLLM key info: %s", e)
        return None
