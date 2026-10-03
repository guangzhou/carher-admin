from __future__ import annotations

from typing import Any
from litellm.integrations.custom_logger import CustomLogger
import litellm

_TARGET_ALIAS = "cursor-baiyu-thga"
_IMAGE_MODEL = "chatgpt-gpt-5.5"
_TEXT_MODELS = {
    "gpt-5.5",
    "chatgpt-gpt-5.5",
    "local-deepseek-v4-flash-responses",
    "local-deepseek-v4-flash",
}


def _get_key_alias(user_api_key_dict: Any) -> str:
    if isinstance(user_api_key_dict, dict):
        return str(user_api_key_dict.get("key_alias") or user_api_key_dict.get("alias") or "")
    return str(getattr(user_api_key_dict, "key_alias", "") or getattr(user_api_key_dict, "alias", "") or "")


def _has_image(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("type") in {"input_image", "image_url"}:
            return True
        if "image_url" in value or "input_image" in value:
            return True
        return any(_has_image(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_image(v) for v in value)
    if isinstance(value, str):
        lower = value.lower()
        if "data:image" in lower:
            return True
        if lower.startswith(("http://", "https://")) and any(ext in lower for ext in (".png", ".jpg", ".jpeg", ".webp")):
            return True
    return False


class BaiyuImageRoute(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        try:
            if not isinstance(data, dict):
                return data
            if _get_key_alias(user_api_key_dict) != _TARGET_ALIAS:
                return data
            if str(data.get("model") or "") not in _TEXT_MODELS:
                return data
            if not _has_image(data.get("input", data.get("messages", data))):
                return data
            old = data.get("model")
            data["model"] = _IMAGE_MODEL
            try:
                litellm.print_verbose(f"[baiyu_image_route] routed image request {old!r} -> {_IMAGE_MODEL!r}")
            except Exception:
                pass
        except Exception as exc:
            try:
                litellm.print_verbose(f"[baiyu_image_route] ERROR: {exc!r}")
            except Exception:
                pass
        return data


baiyu_image_route = BaiyuImageRoute()
