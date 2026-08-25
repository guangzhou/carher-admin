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
import urllib.error
import json
from typing import Any

logger = logging.getLogger("carher-admin")

LITELLM_PROXY_URL = os.getenv(
    "LITELLM_PROXY_URL", "http://litellm-proxy.carher.svc:4000"
)
LITELLM_MASTER_KEY = os.getenv(
    "LITELLM_MASTER_KEY", ""
)

DEFAULT_LITELLM_ROUTE_POLICY = "openrouter_first"
WANGSU_FIRST_LITELLM_ROUTE_POLICY = "wangsu_first"
VALID_LITELLM_ROUTE_POLICIES = {
    DEFAULT_LITELLM_ROUTE_POLICY,
    WANGSU_FIRST_LITELLM_ROUTE_POLICY,
}

# Sonnet/Opus: Wangsu Direct primary -> OpenRouter fallback
# GPT products: account pool primary -> local DeepSeek fallback
# Gemini:       OpenRouter primary -> Wangsu OpenAI-compat fallback
MODEL_FALLBACK_MAP = {
    "claude-opus-4-6": "openrouter-claude-opus-4-6",
    "claude-sonnet-4-6": "openrouter-claude-sonnet-4-6",
    "gpt-5.5": "deepseek-v4-flash",
    "chatgpt-gpt-5.5": "deepseek-v4-flash",
    "gpt-5.6-sol": "deepseek-v4-flash",
    "chatgpt-gpt-5.6-sol": "deepseek-v4-flash",
    "gpt-5.6-terra": "deepseek-v4-flash",
    "chatgpt-gpt-5.6-terra": "deepseek-v4-flash",
    "gpt-5.6-luna": "deepseek-v4-flash",
    "chatgpt-gpt-5.6-luna": "deepseek-v4-flash",
    "gpt-5.4": "deepseek-v4-flash",
    "chatgpt-gpt-5.4": "deepseek-v4-flash",
    "gpt-5.3-codex": "deepseek-v4-flash",
    "chatgpt-gpt-5.3-codex": "deepseek-v4-flash",
    "gpt-5.3-codex-spark": "deepseek-v4-flash",
    "chatgpt-gpt-5.3-codex-spark": "deepseek-v4-flash",
    "gemini-3.1-pro-preview": "wangsu-gemini-3.1-pro-preview",
}

_BASE_MODELS = [
    "claude-opus-4-6",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-haiku-4-5",
    "gpt-5.5",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.4",
    "gpt-5.3-codex-spark",
    "deepseek-v4-flash",
    "gemini-3.1-pro-preview",
    "minimax-m2.7",
    "glm-5",
    "gpt-5.3-codex",
    "BAAI/bge-m3",
    "wangsu-gpt-5.5",
    "wangsu-deepseek-v4-pro",
    "wangsu-deepseek-v4-flash",
    "wangsu-glm-5.1",
    "wangsu-gemini-3.5-flash",
    "wangsu-text-embedding-v3",
    "openrouter-claude-opus-4-7",
    "openrouter-claude-opus-4-8",
    "openrouter-claude-sonnet-4-6",
    "anthropic.claude-opus-4-7",
    "anthropic.claude-opus-4-8",
    "chatgpt-gpt-5.5",
    "chatgpt-gpt-5.6-sol",
    "chatgpt-gpt-5.6-terra",
    "chatgpt-gpt-5.6-luna",
    "chatgpt-gpt-5.4",
    "chatgpt-gpt-5.3-codex",
    "chatgpt-gpt-5.3-codex-spark",
]
ALL_MODELS = sorted({*_BASE_MODELS, *MODEL_FALLBACK_MAP.values()})


def normalize_route_policy(policy: str | None) -> str:
    if policy in VALID_LITELLM_ROUTE_POLICIES:
        return str(policy)
    return DEFAULT_LITELLM_ROUTE_POLICY


def _build_router_settings() -> dict[str, list[dict[str, list[str]]]]:
    fallbacks: list[dict[str, list[str]]] = []
    for primary, fallback in MODEL_FALLBACK_MAP.items():
        fallbacks.append({primary: [fallback]})
    return {"fallbacks": fallbacks}


def _build_key_payload(uid: int, name: str = "", route_policy: str | None = None) -> dict[str, Any]:
    alias = f"carher-{uid}"
    normalized_policy = normalize_route_policy(route_policy)
    return {
        "user_id": alias,
        "key_alias": alias,
        "metadata": {
            "instance": alias,
            "owner_name": name,
            "litellm_route_policy": normalized_policy,
        },
        "models": ALL_MODELS,
        "aliases": {},
        "router_settings": _build_router_settings(),
    }


def request_json(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: int = 15,
) -> Any:
    if not LITELLM_MASTER_KEY:
        raise RuntimeError("LITELLM_MASTER_KEY is not configured")
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{LITELLM_PROXY_URL}{path}",
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {LITELLM_MASTER_KEY}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    if not raw:
        return {}
    return json.loads(raw)


def request_json_with_headers(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: int = 15,
) -> tuple[Any, dict[str, str]]:
    if not LITELLM_MASTER_KEY:
        raise RuntimeError("LITELLM_MASTER_KEY is not configured")
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{LITELLM_PROXY_URL}{path}",
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {LITELLM_MASTER_KEY}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        headers = {key.lower(): value for key, value in resp.headers.items()}
    return (json.loads(raw) if raw else {}), headers


def _post_json(path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    if not LITELLM_MASTER_KEY:
        logger.error("LITELLM_MASTER_KEY is not configured")
        return None
    return request_json("POST", path, payload, timeout=10)


def generate_key(uid: int, name: str = "", route_policy: str | None = None) -> str | None:
    """Create a LiteLLM virtual key for a her instance. Returns the key string."""
    payload = _build_key_payload(uid, name=name, route_policy=route_policy)
    try:
        data = _post_json("/key/generate", payload)
        if data is None:
            return None
        key = data.get("key", "")
        if key:
            logger.info("Generated LiteLLM key for carher-%d: %s...%s", uid, key[:6], key[-4:])
        return key
    except Exception as e:
        logger.error("Failed to generate LiteLLM key for carher-%d: %s", uid, e)
        return None


def update_key(key: str, uid: int, name: str = "", route_policy: str | None = None) -> bool:
    """Update an existing LiteLLM virtual key in place."""
    if not key:
        return False
    payload = _build_key_payload(uid, name=name, route_policy=route_policy)
    payload["key"] = key
    try:
        _post_json("/key/update", payload)
        logger.info(
            "Updated LiteLLM key policy for carher-%d: %s -> %s",
            uid,
            key[:6] + "..." + key[-4:],
            normalize_route_policy(route_policy),
        )
        return True
    except Exception as e:
        logger.error("Failed to update LiteLLM key for carher-%d: %s", uid, e)
        return False


def delete_key(key: str) -> bool:
    """Delete a LiteLLM virtual key."""
    if not key:
        return False
    try:
        if _post_json("/key/delete", {"keys": [key]}) is None:
            return False
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
