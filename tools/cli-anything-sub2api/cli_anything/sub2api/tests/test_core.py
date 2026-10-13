"""Unit tests for connection resolution and native API plumbing."""

from __future__ import annotations

import json

import pytest

from cli_anything.sub2api.core.client import (
    ConnectionConfig,
    Sub2APIClient,
    Sub2APIError,
    encode_query,
    normalize_path,
    unwrap_response,
)
from cli_anything.sub2api.core.operations import (
    account_list_query,
    build_grok_extra,
    build_grok_oauth_credentials,
    is_sensitive_export_path,
    parse_ids,
    parse_json_data,
    parse_params,
)
from cli_anything.sub2api.core.profile import DEFAULT_PROFILE_PATH, load_profile, save_profile


def test_connection_prefers_explicit_values_over_environment(tmp_path) -> None:
    config = ConnectionConfig.from_sources(
        base_url="https://gateway.example.com/",
        admin_api_key="cli-key",
        profile_path=tmp_path / "profile.json",
        environ={"SUB2API_BASE_URL": "https://env.example.com", "SUB2API_ADMIN_API_KEY": "env-key"},
    )
    assert config.base_url == "https://gateway.example.com"
    assert config.admin_api_key == "cli-key"
    assert config.auth_headers() == {"x-api-key": "cli-key"}


def test_connection_falls_back_to_environment_and_profile(tmp_path) -> None:
    profile = tmp_path / "profile.json"
    save_profile("https://profile.example.com", profile)
    config = ConnectionConfig.from_sources(profile_path=profile, environ={"SUB2API_JWT": "jwt-value"})
    assert config.base_url == "https://profile.example.com"
    assert config.jwt == "jwt-value"
    assert config.auth_headers() == {"Authorization": "Bearer jwt-value"}


def test_connection_target_can_be_overridden_by_environment(tmp_path) -> None:
    config = ConnectionConfig.from_sources(
        target="198",
        profile_path=tmp_path / "profile.json",
        environ={"SUB2API_198_BASE_URL": "http://sub2api.internal:31880"},
    )
    assert config.base_url == "http://sub2api.internal:31880"


def test_missing_credentials_are_rejected_without_echoing_values(tmp_path) -> None:
    config = ConnectionConfig.from_sources(base_url="https://example.com", profile_path=tmp_path / "p.json")
    with pytest.raises(Sub2APIError) as exc:
        config.auth_headers()
    assert "SUB2API_ADMIN_API_KEY" in str(exc.value)
    assert "secret" not in str(exc.value)


def test_normalize_path_supports_documented_admin_forms() -> None:
    assert normalize_path("/admin/groups/all") == "/api/v1/admin/groups/all"
    assert normalize_path("/api/v1/admin/groups/all") == "/api/v1/admin/groups/all"


def test_normalize_path_rejects_foreign_urls_and_non_admin() -> None:
    with pytest.raises(Sub2APIError):
        normalize_path("https://evil.example.com/admin/x", admin_only=True)
    with pytest.raises(Sub2APIError):
        normalize_path("/api/v1/user/me", admin_only=True)


def test_encode_query_drops_empty_and_lowercases_booleans() -> None:
    encoded = encode_query({"page": 1, "search": None, "lite": True, "tag": ["a", "b"]})
    assert encoded == "page=1&lite=true&tag=a&tag=b"


def test_unwrap_response_raises_on_error_envelope() -> None:
    with pytest.raises(Sub2APIError, match="INVALID_ADMIN_KEY"):
        unwrap_response({"code": 401, "message": "INVALID_ADMIN_KEY"}, endpoint="/x", status=401)
    assert unwrap_response({"code": 0, "data": {"ok": True}}, endpoint="/x", status=200) == {"ok": True}
    assert unwrap_response({"plain": 1}, endpoint="/x", status=200) == {"plain": 1}


def test_api_error_from_http_status_is_actionable() -> None:
    client = Sub2APIClient(ConnectionConfig(base_url="http://127.0.0.1:9", admin_api_key="k", timeout=0.5))
    with pytest.raises(Sub2APIError, match="Cannot reach Sub2API"):
        client.admin("GET", "/admin/system/version")


def test_sensitive_export_detection() -> None:
    assert is_sensitive_export_path("/admin/accounts/data")
    assert is_sensitive_export_path("/admin/proxies/data")
    assert not is_sensitive_export_path("/admin/accounts")


def test_account_list_query_matches_native_filters() -> None:
    query = account_list_query(page=1, page_size=50, search="outlook", platform="openai", lite=False)
    assert query["page"] == 1
    assert query["search"] == "outlook"
    assert query["lite"] is None


def test_json_parser_requires_exactly_one_source(tmp_path) -> None:
    payload_file = tmp_path / "payload.json"
    payload_file.write_text("{\"a\":1}", encoding="utf-8")
    with pytest.raises(Sub2APIError, match="only one"):
        parse_json_data("{\"a\":1}", payload_file)
    with pytest.raises(Sub2APIError, match="requires"):
        parse_json_data(None, None)
    assert parse_json_data("{\"a\":1}", None) == {"a": 1}


def test_ids_and_params_validation() -> None:
    assert parse_ids("1, 2,3") == [1, 2, 3]
    with pytest.raises(Sub2APIError):
        parse_ids("x")
    with pytest.raises(Sub2APIError):
        parse_ids("0")
    assert parse_params(("platform=openai", "tag=a", "tag=b")) == [("platform", "openai"), ("tag", "a"), ("tag", "b")]
    with pytest.raises(Sub2APIError):
        parse_params(("bad",))


def test_profile_never_persists_credentials(tmp_path) -> None:
    profile = tmp_path / "profile.json"
    result = save_profile("https://gateway.example.com/", profile)
    loaded = json.loads(profile.read_text(encoding="utf-8"))
    assert result["base_url"] == "https://gateway.example.com"
    assert set(loaded) == {"base_url"}
    assert load_profile(profile) == {"base_url": "https://gateway.example.com"}


def test_profile_can_store_non_secret_target(tmp_path) -> None:
    profile = tmp_path / "profile.json"
    save_profile("http://10.68.13.198:31880", profile, target="198")
    assert load_profile(profile) == {"base_url": "http://10.68.13.198:31880", "target": "198"}


def test_known_198_target_resolves_without_persisting_auth(tmp_path) -> None:
    config = ConnectionConfig.from_sources(target="198", profile_path=tmp_path / "p.json", environ={})
    assert config.base_url == "http://10.68.13.198:31880"
    assert config.admin_api_key is None


def test_grok_oauth_payload_filters_transient_fields() -> None:
    token = {
        "access_token": "access",
        "refresh_token": "refresh",
        "email": "grok@example.com",
        "password": "must-not-persist",
        "sso_token": "must-not-persist",
        "subscription_tier": "supergrok",
    }
    credentials = build_grok_oauth_credentials(token)
    assert credentials == {
        "access_token": "access",
        "refresh_token": "refresh",
        "email": "grok@example.com",
        "subscription_tier": "supergrok",
    }
    assert build_grok_extra(token) == {"email": "grok@example.com", "subscription_tier": "supergrok"}
