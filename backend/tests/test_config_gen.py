"""Unit tests for backend/config_gen.py.

All tests are pure-function: generate_openclaw_json takes a dict and
returns a dict — no DB, no K8s, no filesystem access.
"""

from __future__ import annotations

import json

import pytest

from backend.config_gen import generate_openclaw_json, generate_json_string


def _inst(overrides: dict | None = None) -> dict:
    """Minimal DB row for generate_openclaw_json."""
    base = {
        "id": 1,
        "name": "测试用户",
        "model": "gpt",
        "app_id": "cli_test",
        "app_secret": "secret123",
        "prefix": "s1",
        "owner": "ou_abc",
        "provider": "wangsu",
        "bot_open_id": "ou_bot1",
        "litellm_key": "",
    }
    if overrides:
        base.update(overrides)
    return base


# ──────────────────────────────────────
# Top-level structure
# ──────────────────────────────────────

class TestTopLevelStructure:
    def test_has_include(self):
        cfg = generate_openclaw_json(_inst())
        assert cfg["$include"] == "./carher-config.json"

    def test_has_agents(self):
        cfg = generate_openclaw_json(_inst())
        assert "agents" in cfg

    def test_has_plugins_gemini(self):
        cfg = generate_openclaw_json(_inst())
        gemini = cfg["plugins"]["entries"]["realtime"]["config"]["gemini"]
        assert "projectId" in gemini
        assert "model" in gemini

    def test_output_is_valid_json(self):
        s = generate_json_string(_inst())
        parsed = json.loads(s)
        assert parsed["$include"] == "./carher-config.json"


# ──────────────────────────────────────
# Primary model selection
# ──────────────────────────────────────

class TestPrimaryModel:
    @pytest.mark.parametrize("provider,model,expected_primary", [
        ("wangsu",      "gpt",    "wangsu/gpt-5.4"),
        ("wangsu",      "sonnet", "wangsu/claude-sonnet-4-6"),
        ("wangsu",      "opus",   "wangsu/claude-opus-4-6"),
        ("wangsu",      "gemini", "wangsu/gemini-3.1-pro-preview"),
        ("anthropic",   "sonnet", "anthropic/claude-sonnet-4-6"),
        ("anthropic",   "opus",   "anthropic/claude-opus-4-6"),
        ("anthropic",   "gpt",    "openrouter/openai/gpt-5.4"),
        ("openrouter",  "gpt",    "openrouter/openai/gpt-5.4"),
        ("openrouter",  "sonnet", "openrouter/anthropic/claude-sonnet-4.6"),
        ("openrouter",  "opus",   "openrouter/anthropic/claude-opus-4.6"),
        ("litellm",     "sonnet", "litellm/claude-sonnet-4-6"),
        ("litellm",     "opus",   "litellm/claude-opus-4-6"),
        ("litellm",     "gpt",    "litellm/gpt-5.4"),
        ("litellm",     "gemini", "litellm/gemini-3.1-pro-preview"),
        ("litellm",     "minimax","litellm/minimax-m2.7"),
        ("litellm",     "glm",    "litellm/glm-5"),
        ("litellm",     "codex",  "litellm/gpt-5.3-codex"),
    ])
    def test_primary_model(self, provider, model, expected_primary):
        cfg = generate_openclaw_json(_inst({"provider": provider, "model": model}))
        primary = cfg["agents"]["defaults"]["model"]["primary"]
        assert primary == expected_primary

    def test_unknown_model_passes_through(self):
        cfg = generate_openclaw_json(_inst({"provider": "wangsu", "model": "custom-future-model"}))
        primary = cfg["agents"]["defaults"]["model"]["primary"]
        assert primary == "custom-future-model"


# ──────────────────────────────────────
# Model aliases per provider
# ──────────────────────────────────────

class TestModelAliases:
    def _models(self, provider: str) -> dict:
        cfg = generate_openclaw_json(_inst({"provider": provider}))
        return cfg["agents"]["defaults"]["models"]

    def test_litellm_only_litellm_keys(self):
        models = self._models("litellm")
        for key in models:
            assert key.startswith("litellm/"), f"Unexpected non-litellm key: {key}"

    def test_litellm_no_openrouter_keys(self):
        models = self._models("litellm")
        for key in models:
            assert not key.startswith("openrouter/")

    def test_litellm_has_all_core_aliases(self):
        models = self._models("litellm")
        alias_set = {v["alias"] for v in models.values()}
        assert {"opus", "sonnet", "gpt", "gemini", "minimax", "glm", "codex"}.issubset(alias_set)

    def test_wangsu_has_openrouter_and_wangsu_keys(self):
        models = self._models("wangsu")
        keys = set(models.keys())
        assert any(k.startswith("openrouter/") for k in keys)
        assert any(k.startswith("wangsu/") for k in keys)

    def test_wangsu_opus_alias_is_opus(self):
        # openrouter/anthropic/claude-opus-4.6 → alias "opus" (primary)
        models = self._models("wangsu")
        assert models["openrouter/anthropic/claude-opus-4.6"]["alias"] == "opus"

    def test_anthropic_openrouter_has_routing_params(self):
        models = self._models("anthropic")
        or_opus = models["openrouter/anthropic/claude-opus-4.6"]
        assert "params" in or_opus

    def test_openrouter_anthropic_models_have_routing_params(self):
        models = self._models("openrouter")
        for key in ("openrouter/anthropic/claude-opus-4.6", "openrouter/anthropic/claude-sonnet-4.6"):
            assert "params" in models[key], f"Missing routing params on {key}"

    def test_wangsu_has_ws_prefixed_aliases(self):
        models = self._models("wangsu")
        alias_set = {v["alias"] for v in models.values()}
        assert "ws-opus" in alias_set
        assert "ws-sonnet" in alias_set

    def test_non_litellm_includes_shared_models(self):
        for provider in ("wangsu", "openrouter", "anthropic"):
            models = self._models(provider)
            # Shared models always present
            assert "openrouter/openai/gpt-5.4" in models
            assert "openrouter/google/gemini-3.1-pro-preview" in models


# ──────────────────────────────────────
# Feishu channel config
# ──────────────────────────────────────

class TestFeishuChannel:
    def test_no_channels_when_no_app_id(self):
        cfg = generate_openclaw_json(_inst({"app_id": ""}))
        assert "channels" not in cfg

    def test_feishu_enabled(self):
        cfg = generate_openclaw_json(_inst())
        assert cfg["channels"]["feishu"]["enabled"] is True

    def test_feishu_app_id(self):
        cfg = generate_openclaw_json(_inst({"app_id": "cli_abc"}))
        assert cfg["channels"]["feishu"]["appId"] == "cli_abc"

    def test_feishu_app_secret(self):
        cfg = generate_openclaw_json(_inst({"app_secret": "mysecret"}))
        assert cfg["channels"]["feishu"]["appSecret"] == "mysecret"

    def test_feishu_groups_enabled(self):
        feishu = generate_openclaw_json(_inst())["channels"]["feishu"]
        assert feishu["groups"]["enabled"] is True

    def test_feishu_no_known_bots(self):
        feishu = generate_openclaw_json(_inst())["channels"]["feishu"]
        assert "knownBots" not in feishu
        assert "knownBotOpenIds" not in feishu

    def test_feishu_bot_open_id(self):
        cfg = generate_openclaw_json(_inst({"bot_open_id": "ou_mybot"}))
        assert cfg["channels"]["feishu"]["botOpenId"] == "ou_mybot"

    def test_feishu_no_bot_open_id_key_when_empty(self):
        cfg = generate_openclaw_json(_inst({"bot_open_id": ""}))
        feishu = cfg["channels"]["feishu"]
        assert "botOpenId" not in feishu


# ──────────────────────────────────────
# OAuth redirect URI
# ──────────────────────────────────────

class TestOAuthRedirectUri:
    @pytest.mark.parametrize("uid,prefix,expected_fragment", [
        (1,   "s1", "s1-u1-auth.carher.net"),
        (14,  "s3", "s3-u14-auth.carher.net"),
        (100, "s2", "s2-u100-auth.carher.net"),
    ])
    def test_oauth_url_format(self, uid, prefix, expected_fragment):
        cfg = generate_openclaw_json(_inst({"id": uid, "prefix": prefix}))
        uri = cfg["channels"]["feishu"]["oauthRedirectUri"]
        assert expected_fragment in uri
        assert uri.endswith("/feishu/oauth/callback")

    def test_prefix_without_dash_normalised(self):
        cfg = generate_openclaw_json(_inst({"id": 5, "prefix": "s1"}))
        uri = cfg["channels"]["feishu"]["oauthRedirectUri"]
        assert "s1-u5-auth" in uri
        # Prefix must not double-dash
        assert "s1--" not in uri


# ──────────────────────────────────────
# Feishu name suffix
# ──────────────────────────────────────

class TestFeishuName:
    @pytest.mark.parametrize("name,expected", [
        ("张三",              "张三的her"),
        ("永兵的her",         "永兵的her"),      # already has suffix
        ("国际法务的Her",     "国际法务的Her"),  # case-insensitive: Her
        ("国现的her(阿里云)", "国现的her(阿里云)"),  # suffix in middle
        ("IT基础设施her",     "IT基础设施her的her"),  # no 的 → code appends 的her
        ("",                  ""),               # empty → empty
    ])
    def test_name_suffix(self, name, expected):
        cfg = generate_openclaw_json(_inst({"name": name}))
        if not name:
            # When name is empty, feishu.name should be empty or absent
            feishu = cfg.get("channels", {}).get("feishu", {})
            assert feishu.get("name", "") == ""
        else:
            assert cfg["channels"]["feishu"]["name"] == expected


# ──────────────────────────────────────
# Owner / DM config
# ──────────────────────────────────────

class TestOwnerConfig:
    def test_no_dm_when_no_owner(self):
        cfg = generate_openclaw_json(_inst({"owner": ""}))
        feishu = cfg["channels"]["feishu"]
        assert "dm" not in feishu

    def test_dm_allow_from_single(self):
        cfg = generate_openclaw_json(_inst({"owner": "ou_abc"}))
        allow_from = cfg["channels"]["feishu"]["dm"]["allowFrom"]
        assert allow_from == ["ou_abc"]

    def test_dm_allow_from_multiple(self):
        cfg = generate_openclaw_json(_inst({"owner": "ou_abc|ou_def|ou_ghi"}))
        allow_from = cfg["channels"]["feishu"]["dm"]["allowFrom"]
        assert set(allow_from) == {"ou_abc", "ou_def", "ou_ghi"}

    def test_owner_strips_whitespace(self):
        cfg = generate_openclaw_json(_inst({"owner": " ou_a | ou_b "}))
        allow_from = cfg["channels"]["feishu"]["dm"]["allowFrom"]
        assert "ou_a" in allow_from
        assert "ou_b" in allow_from

    def test_commands_owner_allow_from(self):
        cfg = generate_openclaw_json(_inst({"owner": "ou_abc|ou_def"}))
        allow_from = cfg["commands"]["ownerAllowFrom"]
        assert set(allow_from) == {"ou_abc", "ou_def"}

    def test_no_commands_when_no_owner(self):
        cfg = generate_openclaw_json(_inst({"owner": ""}))
        assert "commands" not in cfg


# ──────────────────────────────────────
# LiteLLM provider section
# ──────────────────────────────────────

class TestLitellmProviderSection:
    def _litellm_cfg(self, key: str = "sk-test") -> dict:
        return generate_openclaw_json(_inst({
            "provider": "litellm",
            "model": "opus",
            "litellm_key": key,
        }))

    def test_models_section_present(self):
        cfg = self._litellm_cfg()
        assert "models" in cfg

    def test_litellm_base_url(self):
        cfg = self._litellm_cfg()
        prov = cfg["models"]["providers"]["litellm"]
        assert prov["baseUrl"] == "http://litellm-proxy.carher.svc:4000"

    def test_litellm_api_key_from_instance(self):
        cfg = self._litellm_cfg(key="sk-mykey")
        prov = cfg["models"]["providers"]["litellm"]
        assert prov["apiKey"] == "sk-mykey"

    def test_litellm_api_key_env_fallback_when_empty(self):
        cfg = generate_openclaw_json(_inst({"provider": "litellm", "litellm_key": ""}))
        prov = cfg["models"]["providers"]["litellm"]
        assert prov["apiKey"] == "${LITELLM_API_KEY}"

    def test_litellm_provider_has_models_list(self):
        cfg = self._litellm_cfg()
        models = cfg["models"]["providers"]["litellm"]["models"]
        assert isinstance(models, list)
        assert len(models) >= 7

    def test_non_litellm_has_no_models_section(self):
        cfg = generate_openclaw_json(_inst({"provider": "wangsu"}))
        assert "models" not in cfg

    def test_litellm_provider_model_ids_are_strings(self):
        cfg = self._litellm_cfg()
        for m in cfg["models"]["providers"]["litellm"]["models"]:
            assert isinstance(m["id"], str)
            assert isinstance(m["name"], str)
