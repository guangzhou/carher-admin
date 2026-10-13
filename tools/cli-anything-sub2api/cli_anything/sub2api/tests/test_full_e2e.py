"""End-to-end tests against a live local HTTP backend and the installed CLI."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from cli_anything.sub2api.core.client import Sub2APIClient, ConnectionConfig, Sub2APIError
from cli_anything.sub2api.core.profile import save_profile

from cli_anything.sub2api.tests.conftest import ADMIN_KEY


def _resolve_cli() -> list[str]:
    """Use the installed command when available, matching harness release practice."""
    name = "cli-anything-sub2api"
    force = os.environ.get("CLI_ANYTHING_FORCE_INSTALLED", "").strip() == "1"
    path = shutil.which(name)
    if path:
        print(f"[_resolve_cli] Using installed command: {path}")
        return [path]
    if force:
        raise RuntimeError(f"{name} not found in PATH. Install with: pip install -e .")
    module = "cli_anything.sub2api.sub2api_cli"
    print(f"[_resolve_cli] Falling back to: {sys.executable} -m {module}")
    return [sys.executable, "-m", module]


def _run(cli: list[str], args: list[str], *, check: bool = True, env: dict[str, str], input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cli + args,
        capture_output=True,
        text=True,
        check=check,
        env=env,
        input=input_text,
    )


def _env(backend) -> dict[str, str]:
    env = os.environ.copy()
    env["SUB2API_BASE_URL"] = backend.base_url
    env["SUB2API_ADMIN_API_KEY"] = ADMIN_KEY
    env.pop("SUB2API_JWT", None)
    return env


class TestLiveBackend:
    def test_health_and_version(self, backend) -> None:
        client = Sub2APIClient(ConnectionConfig(base_url=backend.base_url, admin_api_key=ADMIN_KEY))
        assert client.health() == {"status": "ok"}
        assert client.admin("GET", "/admin/system/version") == {"version": "1.2.3-test"}
        recorded = backend.log.requests[-1]
        assert recorded["path"] == "/api/v1/admin/system/version"
        assert recorded["headers"]["x-api-key"] == ADMIN_KEY

    def test_bad_admin_key_is_an_actionable_error(self, backend) -> None:
        client = Sub2APIClient(ConnectionConfig(base_url=backend.base_url, admin_api_key="wrong"))
        with pytest.raises(Sub2APIError, match="INVALID_ADMIN_KEY"):
            client.admin("GET", "/admin/system/version")

    def test_account_update_payload_is_exact(self, backend) -> None:
        client = Sub2APIClient(ConnectionConfig(base_url=backend.base_url, admin_api_key=ADMIN_KEY))
        result = client.admin("PUT", "/admin/accounts/1", payload={"concurrency": 10})
        assert result["concurrency"] == 10
        recorded = backend.log.requests[-1]
        assert recorded["method"] == "PUT"
        assert recorded["path"] == "/api/v1/admin/accounts/1"
        assert recorded["body"] == {"concurrency": 10}


class TestCLISubprocess:
    CLI = _resolve_cli()

    def test_help(self, backend) -> None:
        result = _run(self.CLI, ["--help"], env=_env(backend))
        assert result.returncode == 0
        assert "accounts" in result.stdout

    def test_status_json(self, backend) -> None:
        result = _run(self.CLI, ["--json", "status"], env=_env(backend))
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        assert payload["health"] == {"status": "ok"}
        assert payload["system"]["version"] == "1.2.3-test"

    def test_accounts_list_filters_and_json(self, backend) -> None:
        result = _run(self.CLI, ["--json", "accounts", "list", "--status", "active", "--page-size", "200"], env=_env(backend))
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        assert payload["items"][0]["name"] == "upstream@example.com"
        recorded = backend.log.requests[-1]
        assert recorded["query"]["status"] == "active"
        assert recorded["query"]["page_size"] == "200"

    def test_account_update_json(self, backend) -> None:
        result = _run(self.CLI, ["--json", "accounts", "update", "1", "--data", "{\"concurrency\":10}"], env=_env(backend))
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        assert payload["concurrency"] == 10

    def test_users_get_dedicated_command(self, backend) -> None:
        result = _run(self.CLI, ["--json", "users", "get", "7"], env=_env(backend))
        assert result.returncode == 0
        assert json.loads(result.stdout)["email"] == "user@example.com"

    def test_sensitive_export_requires_file(self, backend, tmp_path: Path) -> None:
        without_file = _run(self.CLI, ["--json", "accounts", "export", "--ids", "1"], env=_env(backend), check=False)
        assert without_file.returncode != 0
        assert "--output-file" in without_file.stderr

        out = tmp_path / "accounts-export.json"
        result = _run(self.CLI, ["--json", "accounts", "export", "--ids", "1", "--output-file", str(out)], env=_env(backend))
        assert result.returncode == 0
        exported = json.loads(out.read_text(encoding="utf-8"))
        assert exported[0]["access_token"] == "secret-token"
        assert "secret-token" not in result.stdout
        assert "accounts-export.json" in result.stdout

    def test_dry_run_never_sends_mutations(self, backend) -> None:
        before = len(backend.log.requests)
        result = _run(self.CLI, ["--dry-run", "--json", "accounts", "update", "1", "--data", "{\"concurrency\":99}"], env=_env(backend))
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        assert payload["dry_run"] is True
        assert payload["payload"] == {"concurrency": 99}
        assert len(backend.log.requests) == before

    def test_missing_credentials_returns_a_clean_error(self, backend) -> None:
        env = _env(backend)
        env.pop("SUB2API_ADMIN_API_KEY")
        result = _run(self.CLI, ["accounts", "list"], env=env, check=False)
        assert result.returncode == 1
        assert "Missing administrator credentials" in result.stderr
        assert "Traceback" not in result.stderr

    def test_profile_flow_saves_only_base_url(self, backend, tmp_path: Path) -> None:
        profile = tmp_path / "profile.json"
        result = _run(self.CLI, ["--profile-file", str(profile), "profile", "set", backend.base_url], env=_env(backend))
        assert result.returncode == 0
        assert json.loads(profile.read_text(encoding="utf-8")) == {"base_url": backend.base_url.rstrip("/")}

    def test_api_escape_hatch_records_native_request(self, backend) -> None:
        result = _run(self.CLI, ["--json", "api", "GET", "/admin/users/7"], env=_env(backend))
        assert result.returncode == 0
        assert json.loads(result.stdout)["email"] == "user@example.com"

    def test_users_balance_uses_dedicated_route(self, backend) -> None:
        result = _run(self.CLI, ["--json", "users", "balance", "7", "set", "100000", "--notes", "topup"], env=_env(backend))
        assert result.returncode == 0
        recorded = backend.log.requests[-1]
        assert (recorded["method"], recorded["path"]) == ("POST", "/api/v1/admin/users/7/balance")
        assert recorded["body"] == {"balance": 100000.0, "operation": "set", "notes": "topup"}

    def test_users_balance_rejects_non_positive_amount(self, backend) -> None:
        result = _run(self.CLI, ["--json", "users", "balance", "7", "add", "0"], env=_env(backend), check=False)
        assert result.returncode != 0

    def test_audit_logs_list_passes_filters(self, backend) -> None:
        result = _run(self.CLI, ["--json", "audit-logs", "list", "--page-size", "5", "--param", "action=auth.login"], env=_env(backend))
        assert result.returncode == 0
        assert json.loads(result.stdout)["items"][0]["action"] == "auth.login"
        assert backend.log.requests[-1]["query"] == {"page": "1", "page_size": "5", "action": "auth.login"}

    def test_grok_oauth_capabilities(self, backend) -> None:
        result = _run(self.CLI, ["--json", "grok", "oauth", "capabilities"], env=_env(backend))
        assert result.returncode == 0
        assert json.loads(result.stdout)["password_auth_enabled"] is False

    def test_grok_import_dry_run_redacts_password(self, backend) -> None:
        result = _run(
            self.CLI,
            ["--dry-run", "--json", "grok", "import-password", "--email", "grok@example.com", "--password-stdin"],
            env=_env(backend),
            input_text="secret-password\n",
        )
        assert result.returncode == 0
        assert "<stdin>" in result.stdout
        assert "secret-password" not in result.stdout
