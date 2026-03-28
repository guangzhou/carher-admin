"""Config generator for the operator.

Similar to backend/config_gen.py but takes knownBots as parameter
instead of querying DB. The operator computes knownBots centrally
from CRDs, not from SQLite.
"""

from __future__ import annotations

from typing import Any

GEMINI_PROJECT = "gen-lang-client-0519229117"
GEMINI_MODEL = "gemini-live-2.5-flash-native-audio"

MODEL_MAP = {
    "sonnet": "openrouter/anthropic/claude-sonnet-4.6",
    "opus": "openrouter/anthropic/claude-opus-4.6",
    "gpt": "openrouter/openai/gpt-5.4",
}
MODEL_MAP_ANTHROPIC = {
    "sonnet": "anthropic/claude-sonnet-4-6",
    "opus": "anthropic/claude-opus-4-6",
    "gpt": "openrouter/openai/gpt-5.4",
}


def generate_openclaw_json(
    instance: dict,
    known_bots: dict[str, str],
    known_bot_open_ids: dict[str, str],
) -> dict:
    """Generate openclaw.json from instance data + precomputed knownBots."""
    uid = instance["id"]
    provider = instance.get("provider", "openrouter")
    model_short = instance.get("model", "gpt")
    prefix = instance.get("prefix", "s1")
    pfx = f"{prefix}-" if not prefix.endswith("-") else prefix

    mm = MODEL_MAP_ANTHROPIC if provider == "anthropic" else MODEL_MAP
    model_full = mm.get(model_short, model_short)

    if provider == "anthropic":
        models = {
            "anthropic/claude-opus-4-6": {"alias": "opus"},
            "anthropic/claude-sonnet-4-6": {"alias": "sonnet"},
            "openrouter/anthropic/claude-opus-4.6": {"alias": "or-opus"},
            "openrouter/anthropic/claude-sonnet-4.6": {"alias": "or-sonnet"},
        }
    else:
        models = {
            "openrouter/anthropic/claude-opus-4.6": {"alias": "opus"},
            "openrouter/anthropic/claude-sonnet-4.6": {"alias": "sonnet"},
            "anthropic/claude-opus-4-6": {"alias": "or-opus"},
            "anthropic/claude-sonnet-4-6": {"alias": "or-sonnet"},
        }
    models.update({
        "openrouter/google/gemini-3.1-pro-preview": {"alias": "gemini"},
        "openrouter/minimax/minimax-m2.5": {"alias": "minimax"},
        "openrouter/z-ai/glm-5": {"alias": "glm"},
        "openrouter/openai/gpt-5.4": {"alias": "gpt"},
        "openrouter/openai/gpt-5.3-codex": {"alias": "codex"},
    })

    cfg: dict[str, Any] = {
        "$include": "./carher-config.json",
        "agents": {"defaults": {"model": {"primary": model_full}, "models": models}},
        "plugins": {"entries": {"realtime": {"config": {"gemini": {
            "projectId": GEMINI_PROJECT, "model": GEMINI_MODEL,
        }}}}},
    }

    app_id = instance.get("app_id", "")
    app_secret = instance.get("app_secret", "")
    name = instance.get("name", "")
    owner = instance.get("owner", "")
    bot_open_id = instance.get("bot_open_id", "")

    if app_id and app_secret:
        feishu: dict[str, Any] = {
            "enabled": True,
            "appId": app_id,
            "appSecret": app_secret,
            "name": name,
            "groups": {"enabled": True, "archive": True},
            "oauthRedirectUri": f"https://{pfx}u{uid}-auth.carher.net/feishu/oauth/callback",
        }
        if known_bots:
            feishu["knownBots"] = known_bots
        if known_bot_open_ids:
            feishu["knownBotOpenIds"] = known_bot_open_ids
        if bot_open_id:
            feishu["botOpenId"] = bot_open_id
        if owner:
            feishu["dm"] = {"allowFrom": [o.strip() for o in owner.split("|") if o.strip()]}
        cfg["channels"] = {"feishu": feishu}

    if owner:
        cfg["commands"] = {"ownerAllowFrom": [o.strip() for o in owner.split("|") if o.strip()]}

    return cfg
