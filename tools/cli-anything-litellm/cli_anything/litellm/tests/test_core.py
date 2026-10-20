from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli_anything.litellm.core.config import inspect_config
from cli_anything.litellm.core.session import Session, load_session, normalize_base_url, save_session, session_from_mapping
from cli_anything.litellm.litellm_cli import cli
from cli_anything.litellm.utils.litellm_backend import BackendError, LiteLLMBackend


def test_default_session_for_missing_file(tmp_path: Path) -> None:
    assert load_session(tmp_path / "missing.json") == Session()


def test_session_round_trip_and_permissions(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    value = Session(base_url="https://proxy.example.com", model="router-model")
    save_session(path, value)
    assert load_session(path) == value
    assert path.stat().st_mode & 0o777 == 0o600


def test_malformed_session_rejected(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError, match="cannot load session"):
        load_session(path)


@pytest.mark.parametrize("value", ["localhost:4000", "ftp://host", "https://host/path?key=value", "https://host/#fragment"])
def test_invalid_base_urls_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="base_url"):
        normalize_base_url(value)


def test_base_url_is_normalized() -> None:
    assert normalize_base_url("https://proxy.example.com/") == "https://proxy.example.com"


def test_secret_session_fields_rejected() -> None:
    with pytest.raises(ValueError, match="secret session fields"):
        session_from_mapping({"base_url": "http://localhost:4000", "api_key": "secret"})


def test_config_inspection_summarizes_and_redacts(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "model_list:\n"
        "  - model_name: demo\n"
        "    litellm_params:\n"
        "      model: openai/demo\n"
        "      api_key: super-secret\n"
        "general_settings:\n"
        "  master_key: another-secret\n",
        encoding="utf-8",
    )
    result = inspect_config(path)
    assert result["deployment_count"] == 1
    assert result["models"] == ("demo",)
    assert result["providers"] == ("openai",)
    rendered = json.dumps(result)
    assert "super-secret" not in rendered
    assert "another-secret" not in rendered
    assert rendered.count("[REDACTED]") == 2


def test_invalid_config_top_level_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("- invalid\n", encoding="utf-8")
    with pytest.raises(ValueError, match="top level"):
        inspect_config(path)


def test_invalid_model_list_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("model_list: invalid\n", encoding="utf-8")
    with pytest.raises(ValueError, match="model_list"):
        inspect_config(path)


def test_backend_status_has_real_executable_mapping() -> None:
    result = LiteLLMBackend().status()
    assert set(result["executables"]) == {"litellm", "lite", "litellm-proxy"}


def test_native_forwarding_uses_argument_vector(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, object] = {}

    class Result:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_which(name: str) -> str:
        return f"/bin/{name}"

    def fake_run(args: tuple[str, ...], **kwargs: object) -> Result:
        observed["args"] = args
        observed["kwargs"] = kwargs
        return Result()

    monkeypatch.setattr("cli_anything.litellm.utils.litellm_backend.shutil.which", fake_which)
    monkeypatch.setattr("subprocess.run", fake_run)
    result = LiteLLMBackend().run_native("lite", ("models", "list; touch /tmp/nope"), 5)
    assert observed["args"] == ("/bin/lite", "models", "list; touch /tmp/nope")
    assert "shell" not in observed["kwargs"]
    assert result["returncode"] == 0


def test_missing_native_executable_is_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cli_anything.litellm.utils.litellm_backend.shutil.which", lambda _: None)
    monkeypatch.setattr("cli_anything.litellm.utils.litellm_backend.os.path.isfile", lambda _: False)
    with pytest.raises(BackendError, match="not installed"):
        LiteLLMBackend().run_native("lite", (), 5)


def test_http_request_sends_bearer_and_json(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, object] = {}

    class Response:
        status = 200

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"ok": true}'

    def fake_urlopen(request: object, timeout: float) -> Response:
        observed["request"] = request
        observed["timeout"] = timeout
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = LiteLLMBackend().request("POST", "http://localhost:4000", "/v1/chat/completions", "key", {"model": "demo"}, 3)
    request = observed["request"]
    assert result == {"ok": True}
    assert request.get_header("Authorization") == "Bearer key"
    assert json.loads(request.data) == {"model": "demo"}
    assert observed["timeout"] == 3


def test_organization_list_encodes_alias(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    observed: dict[str, object] = {}

    def fake_request(
        self: LiteLLMBackend,
        method: str,
        base_url: str,
        path: str,
        api_key: str | None,
        payload: dict[str, object] | None,
        timeout: float,
    ) -> object:
        observed.update(method=method, path=path, api_key=api_key, payload=payload)
        return []

    monkeypatch.setattr(LiteLLMBackend, "request", fake_request)
    result = CliRunner().invoke(
        cli,
        ["--session", str(tmp_path / "session.json"), "--json", "--api-key", "secret", "organization", "list", "--alias", "北汽研究院"],
    )
    assert result.exit_code == 0
    assert observed == {
        "method": "GET",
        "path": "/organization/list?org_alias=%E5%8C%97%E6%B1%BD%E7%A0%94%E7%A9%B6%E9%99%A2",
        "api_key": "secret",
        "payload": None,
    }


def test_organization_create_dry_run_does_not_call_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fail_request(*args: object, **kwargs: object) -> object:
        raise AssertionError("backend request should not run")

    monkeypatch.setattr(LiteLLMBackend, "request", fail_request)
    result = CliRunner().invoke(
        cli,
        ["--session", str(tmp_path / "session.json"), "--json", "--dry-run", "organization", "create", "北汽研究院"],
    )
    assert result.exit_code == 0
    assert json.loads(result.output) == {
        "dry_run": True,
        "request": {"organization_alias": "北汽研究院"},
    }
