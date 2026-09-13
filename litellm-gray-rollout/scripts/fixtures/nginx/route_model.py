"""Offline reference model for the 198 gray routing state machine.

This module intentionally has no network or nginx dependency. The fixture
runner uses it for fast model checks, but a missing nginx binary is never
reported as a runtime pass.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any


INFERENCE_PATHS = (
    "/v1/messages",
    "/v1/responses",
    "/v1/chat/completions",
    "/v1/completions",
    "/v1/embeddings",
    "/v1/images/generations",
    "/messages",
    "/responses",
    "/chat/completions",
    "/completions",
    "/embeddings",
    "/images/generations",
)
CONTROL_PREFIXES = (
    "/model",
    "/key",
    "/team",
    "/budget",
    "/user",
    "/organization",
    "/customer",
)


def canonical_key(headers: dict[str, str]) -> str:
    """Mirror the documented fail-closed Authorization/x-api-key rules."""

    authorization = headers.get("authorization", "")
    x_api_key = headers.get("x-api-key", "")
    if authorization:
        match = re.fullmatch(r"\s*Bearer\s+([^\s]+)\s*", authorization, re.I)
        candidate = match.group(1) if match else ""
        return candidate if is_valid_key(candidate) else ""
    candidate = x_api_key.strip()
    return candidate if is_valid_key(candidate) else ""


def is_valid_key(value: str) -> bool:
    return bool(re.fullmatch(r"sk-[A-Za-z0-9._~-]{8,512}", value))


def normalize_path(path: str) -> str:
    """Convert the public /pro path to the post-rewrite URI used by nginx maps."""

    if path == "/pro":
        return "/"
    if path.startswith("/pro/"):
        return path[4:]
    return path


def is_control_path(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in CONTROL_PREFIXES)


def is_inference_path(path: str) -> bool:
    return any(path == value or path.startswith(value + "/") for value in INFERENCE_PATHS)


def bucket_for(key: str, split_percent: int) -> str:
    if not key or split_percent <= 0:
        return "prod"
    # nginx 1.18 split_clients uses ngx_murmur_hash2 and compares the
    # uint32 hash with a fixed-point percentage threshold.
    data = key.encode()
    length = len(data)
    h = length
    index = 0
    multiplier = 0x5BD1E995
    while length >= 4:
        k = data[index] | (data[index + 1] << 8) | (data[index + 2] << 16) | (data[index + 3] << 24)
        k = (k * multiplier) & 0xFFFFFFFF
        k ^= k >> 24
        k = (k * multiplier) & 0xFFFFFFFF
        h = ((h * multiplier) & 0xFFFFFFFF) ^ k
        index += 4
        length -= 4
    if length == 3:
        h ^= data[index + 2] << 16
    if length >= 2:
        h ^= data[index + 1] << 8
    if length >= 1:
        h ^= data[index]
        h = (h * multiplier) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * multiplier) & 0xFFFFFFFF
    h ^= h >> 15
    threshold = (split_percent * 0xFFFFFFFF) // 100
    return "gray" if h < threshold else "prod"


def evaluate(
    *,
    path: str,
    headers: dict[str, str] | None = None,
    protected_prod: set[str] | None = None,
    force_prod: set[str] | None = None,
    force_gray: set[str] | None = None,
    split_percent: int = 0,
    convergence_mode: bool = False,
    bridge_override: bool = False,
) -> dict[str, Any]:
    headers = {key.lower(): value for key, value in (headers or {}).items()}
    key = canonical_key(headers)
    protected_prod = protected_prod or set()
    force_prod = force_prod or set()
    force_gray = force_gray or set()
    path = normalize_path(path)

    # Only enumerated inference endpoints enter the key state machine. Unknown
    # or control-plane paths stay stable in normal mode even for force-gray keys.
    if not is_inference_path(path) and not convergence_mode and not bridge_override:
        pool = "prod"
        reason = "default_path"
    elif bridge_override:
        pool = "guarded-old"
        reason = "bridge_override"
    elif convergence_mode:
        pool = "gray"
        reason = "convergence_mode"
    elif is_control_path(path):
        pool = "prod"
        reason = "control_path"
    elif key in protected_prod:
        pool = "prod"
        reason = "protected_prod"
    elif key in force_prod:
        pool = "prod"
        reason = "force_prod"
    elif key in force_gray:
        pool = "gray"
        reason = "force_gray"
    elif is_inference_path(path):
        pool = bucket_for(key, split_percent)
        reason = "split" if key else "missing_key"
    else:
        pool = "prod"
        reason = "default_path"

    return {
        "canonical_key": key,
        "actual_pool": pool,
        "reason": reason,
        "key_sid": hashlib.sha256(key.encode()).hexdigest()[:12] if key else "-",
    }


def evaluate_case(case: dict[str, Any]) -> dict[str, Any]:
    result = evaluate(
        path=case["path"],
        headers=case.get("headers"),
        protected_prod=set(case.get("protected_prod", [])),
        force_prod=set(case.get("force_prod", [])),
        force_gray=set(case.get("force_gray", [])),
        split_percent=case.get("split_percent", 0),
        convergence_mode=case.get("convergence_mode", False),
        bridge_override=case.get("bridge_override", False),
    )
    result["name"] = case["name"]
    result["expected_pool"] = case["expected_pool"]
    return result
