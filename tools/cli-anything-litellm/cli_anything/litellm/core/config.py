from __future__ import annotations

from pathlib import Path

import yaml


SECRET_MARKERS = ("api_key", "apikey", "_key", "token", "password", "secret", "credential", "private_key")


def _redact(value: object, key: str = "") -> object:
    if any(marker in key.lower() for marker in SECRET_MARKERS):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): _redact(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def inspect_config(path: Path) -> dict[str, object]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot parse LiteLLM config {path}: {exc}") from exc
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise ValueError("LiteLLM config must contain a YAML mapping at the top level")
    model_list = loaded.get("model_list", [])
    if model_list is None:
        model_list = []
    if not isinstance(model_list, list):
        raise ValueError("model_list must be a list")
    deployments = tuple(item for item in model_list if isinstance(item, dict))
    models = tuple(str(item.get("model_name")) for item in deployments if item.get("model_name"))
    providers = tuple(
        sorted(
            {
                str(params.get("model", "")).split("/", 1)[0]
                for item in deployments
                if isinstance((params := item.get("litellm_params")), dict) and "/" in str(params.get("model", ""))
            }
        )
    )
    return {
        "path": str(path.resolve()),
        "valid": True,
        "deployment_count": len(deployments),
        "models": models,
        "providers": providers,
        "config": _redact(loaded),
    }
