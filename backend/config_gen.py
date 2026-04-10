"""Config generator: DB row → openclaw.json → K8s ConfigMap.

This is the bridge between the DB (source of truth) and the K8s runtime.
Bot identity is now dynamically registered via Redis bot-registry (no more
static knownBots injection).
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("carher-admin")

GEMINI_PROJECT = "gen-lang-client-0519229117"
GEMINI_MODEL = "gemini-live-2.5-flash-native-audio"

MODEL_MAP = {
    "sonnet": "openrouter/anthropic/claude-sonnet-4.6",
    "opus": "openrouter/anthropic/claude-opus-4.6",
    "gpt": "openrouter/openai/gpt-5.4",
    "gemini": "openrouter/google/gemini-3.1-pro-preview",
}
MODEL_MAP_ANTHROPIC = {
    "sonnet": "anthropic/claude-sonnet-4-6",
    "opus": "anthropic/claude-opus-4-6",
    "gpt": "openrouter/openai/gpt-5.4",
}
MODEL_MAP_WANGSU = {
    "sonnet": "wangsu/claude-sonnet-4-6",
    "opus": "wangsu/claude-opus-4-6",
    "gpt": "wangsu/gpt-5.4",
    "gemini": "wangsu/gemini-3.1-pro-preview",
}
MODEL_MAP_LITELLM = {
    "sonnet": "litellm/claude-sonnet-4-6",
    "opus": "litellm/claude-opus-4-6",
    "gpt": "litellm/gpt-5.4",
    "gemini": "litellm/gemini-3.1-pro-preview",
    "minimax": "litellm/minimax-m2.7",
    "glm": "litellm/glm-5",
    "codex": "litellm/gpt-5.3-codex",
}

GOOGLE_ANTHROPIC_ROUTING = {
    "params": {
        "provider": {
            "order": ["Google", "Anthropic"],
            "allow_fallbacks": True,
        },
    },
}


def generate_openclaw_json(instance: dict) -> dict:
    """Generate a complete openclaw.json from a DB row."""
    uid = instance["id"]
    provider = instance.get("provider", "wangsu")
    model_short = instance.get("model", "gpt")
    prefix = instance.get("prefix", "s1")
    pfx = f"{prefix}-" if not prefix.endswith("-") else prefix

    if provider == "litellm":
        mm = MODEL_MAP_LITELLM
    elif provider == "wangsu":
        mm = MODEL_MAP_WANGSU
    elif provider == "anthropic":
        mm = MODEL_MAP_ANTHROPIC
    else:
        mm = MODEL_MAP
    model_full = mm.get(model_short, model_short)

    def _alias_with_routing(a: str) -> dict:
        return {"alias": a, **GOOGLE_ANTHROPIC_ROUTING}

    if provider == "litellm":
        models: dict[str, Any] = {
            "litellm/claude-opus-4-6": {"alias": "opus"},
            "litellm/claude-sonnet-4-6": {"alias": "sonnet"},
            "litellm/gpt-5.4": {"alias": "gpt"},
            "litellm/gemini-3.1-pro-preview": {"alias": "gemini"},
            "litellm/minimax-m2.7": {"alias": "minimax"},
            "litellm/glm-5": {"alias": "glm"},
            "litellm/gpt-5.3-codex": {"alias": "codex"},
        }
    elif provider == "anthropic":
        models = {
            "anthropic/claude-opus-4-6": {"alias": "opus"},
            "anthropic/claude-sonnet-4-6": {"alias": "sonnet"},
            "openrouter/anthropic/claude-opus-4.6": _alias_with_routing("or-opus"),
            "openrouter/anthropic/claude-sonnet-4.6": _alias_with_routing("or-sonnet"),
        }
    else:
        models = {
            "openrouter/anthropic/claude-opus-4.6": _alias_with_routing("opus"),
            "openrouter/anthropic/claude-sonnet-4.6": _alias_with_routing("sonnet"),
            "anthropic/claude-opus-4-6": {"alias": "or-opus"},
            "anthropic/claude-sonnet-4-6": {"alias": "or-sonnet"},
        }
    if provider != "litellm":
        models.update({
            "openrouter/google/gemini-3.1-pro-preview": {"alias": "gemini"},
            "openrouter/minimax/minimax-m2.7": {"alias": "minimax"},
            "openrouter/z-ai/glm-5": {"alias": "glm"},
            "openrouter/openai/gpt-5.4": {"alias": "gpt"},
            "openrouter/openai/gpt-5.3-codex": {"alias": "codex"},
        })
    if provider == "wangsu":
        models.update({
            "wangsu/claude-opus-4-6": {"alias": "ws-opus"},
            "wangsu/claude-sonnet-4-6": {"alias": "ws-sonnet"},
            "wangsu/gpt-5.4": {"alias": "ws-gpt"},
            "wangsu/gemini-3.1-pro-preview": {"alias": "ws-gemini"},
        })

    cfg: dict[str, Any] = {
        "$include": "./carher-config.json",
        "agents": {"defaults": {"model": {"primary": model_full}, "models": models}},
        "plugins": {"entries": {"realtime": {"config": {"gemini": {
            "projectId": GEMINI_PROJECT, "model": GEMINI_MODEL,
        }}}}},
    }

    if provider == "litellm":
        api_key = instance.get("litellm_key") or "${LITELLM_API_KEY}"
        cfg["models"] = {"providers": {"litellm": {
            "baseUrl": "http://litellm-proxy.carher.svc:4000",
            "apiKey": api_key,
            "models": [
                {"id": "claude-opus-4-6", "name": "Claude Opus 4.6", "api": "openai-completions", "reasoning": True, "input": ["text", "image"], "contextWindow": 200000, "maxTokens": 128000, "cost": {"input": 5, "output": 25, "cacheRead": 0.5}},
                {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6", "api": "openai-completions", "reasoning": True, "input": ["text", "image"], "contextWindow": 200000, "maxTokens": 64000, "cost": {"input": 3, "output": 15, "cacheRead": 0.3}},
                {"id": "gpt-5.4", "name": "GPT-5.4", "api": "openai-completions", "reasoning": True, "input": ["text", "image"], "contextWindow": 200000, "maxTokens": 128000, "cost": {"input": 2.5, "output": 15, "cacheRead": 0.25}},
                {"id": "gemini-3.1-pro-preview", "name": "Gemini 3.1 Pro", "api": "openai-completions", "reasoning": True, "input": ["text", "image"], "contextWindow": 200000, "maxTokens": 65536, "cost": {"input": 2, "output": 12, "cacheRead": 0.2}},
                {"id": "minimax-m2.7", "name": "MiniMax M2.7", "api": "openai-completions", "input": ["text"], "contextWindow": 200000, "maxTokens": 128000, "cost": {"input": 0.5, "output": 1.5}},
                {"id": "glm-5", "name": "GLM-5", "api": "openai-completions", "input": ["text"], "contextWindow": 128000, "maxTokens": 32000, "cost": {"input": 1, "output": 3}},
                {"id": "gpt-5.3-codex", "name": "GPT-5.3 Codex", "api": "openai-completions", "reasoning": True, "input": ["text"], "contextWindow": 200000, "maxTokens": 128000, "cost": {"input": 3, "output": 15}},
            ],
        }}}

    app_id = instance.get("app_id", "")
    app_secret = instance.get("app_secret", "")
    name = instance.get("name", "")
    owner = instance.get("owner", "")
    bot_open_id = instance.get("bot_open_id", "")

    if app_id and app_secret:
        feishu_name = name if "的her" in name.lower() else f"{name}的her" if name else ""
        feishu: dict[str, Any] = {
            "enabled": True,
            "appId": app_id,
            "appSecret": app_secret,
            "name": feishu_name,
            "groups": {"enabled": True, "archive": True},
            "oauthRedirectUri": f"https://{pfx}u{uid}-auth.carher.net/feishu/oauth/callback",
        }
        # knownBots/knownBotOpenIds removed — now populated dynamically via Redis bot-registry.
        if bot_open_id:
            feishu["botOpenId"] = bot_open_id
        if owner:
            feishu["dm"] = {"allowFrom": [o.strip() for o in owner.split("|") if o.strip()]}
        cfg["channels"] = {"feishu": feishu}

    if owner:
        cfg["commands"] = {"ownerAllowFrom": [o.strip() for o in owner.split("|") if o.strip()]}

    return cfg


def generate_json_string(instance: dict) -> str:
    """Generate openclaw.json as a formatted JSON string."""
    return json.dumps(generate_openclaw_json(instance), indent=2, ensure_ascii=False)
