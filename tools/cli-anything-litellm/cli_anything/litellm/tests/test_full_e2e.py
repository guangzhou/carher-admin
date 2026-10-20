from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest


def _resolve_cli(name: str) -> list[str]:
    force = os.environ.get("CLI_ANYTHING_FORCE_INSTALLED", "").strip() == "1"
    path = shutil.which(name)
    if path:
        print(f"[_resolve_cli] Using installed command: {path}")
        return [path]
    if force:
        raise RuntimeError(f"{name} not found in PATH. Install with: pip install -e .")
    return [sys.executable, "-m", "cli_anything.litellm"]


CLI_BASE = _resolve_cli("cli-anything-litellm")


def _run(args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(CLI_BASE + args, capture_output=True, text=True, check=check)


def test_installed_help() -> None:
    result = _run(["--help"])
    assert "Agent-friendly CLI" in result.stdout


def test_backend_status_json_discovers_litellm() -> None:
    result = _run(["--json", "backend", "status"])
    data = json.loads(result.stdout)
    assert data["installed"] is True
    assert data["executables"]["litellm"]


def test_config_inspect_from_installed_command(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "model_list:\n"
        "  - model_name: test-model\n"
        "    litellm_params:\n"
        "      model: openai/test\n"
        "      api_key: should-never-print\n",
        encoding="utf-8",
    )
    result = _run(["--json", "config", "inspect", str(config)])
    assert "should-never-print" not in result.stdout
    data = json.loads(result.stdout)
    assert data["models"] == ["test-model"]


def test_profile_persists_between_processes(tmp_path: Path) -> None:
    session = tmp_path / "session.json"
    _run(["--session", str(session), "--json", "profile", "set", "--base-url", "http://127.0.0.1:4444", "--model", "demo"])
    result = _run(["--session", str(session), "--json", "profile", "show"])
    data = json.loads(result.stdout)
    assert data == {"base_url": "http://127.0.0.1:4444", "model": "demo"}
    assert "api_key" not in session.read_text(encoding="utf-8")


def test_profile_dry_run_does_not_persist(tmp_path: Path) -> None:
    session = tmp_path / "session.json"
    result = _run(
        [
            "--session",
            str(session),
            "--json",
            "--dry-run",
            "profile",
            "set",
            "--base-url",
            "http://127.0.0.1:4444",
            "--model",
            "demo",
        ]
    )
    assert json.loads(result.stdout)["saved"] is False
    assert not session.exists()


@pytest.fixture(scope="module")
def proxy_server(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("proxy")
    config = root / "config.yaml"
    config.write_text(
        "model_list:\n"
        "  - model_name: harness-mock\n"
        "    litellm_params:\n"
        "      model: openai/harness-mock\n"
        "      api_key: mock-key\n"
        "      api_base: http://127.0.0.1:1\n"
        "      mock_response: harness-pong\n",
        encoding="utf-8",
    )
    backend = json.loads(_run(["--json", "backend", "status"]).stdout)
    executable = backend["executables"]["litellm"]
    assert executable
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = subprocess.Popen(
        [executable, "--config", str(config), "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 60
    ready = False
    while time.monotonic() < deadline:
        result = subprocess.run(CLI_BASE + ["--session", str(root / "session.json"), "--json", "profile", "set", "--base-url", base_url, "--model", "harness-mock"], capture_output=True, text=True)
        health = subprocess.run(CLI_BASE + ["--session", str(root / "session.json"), "--json", "--timeout", "2", "proxy", "health", "--ready"], capture_output=True, text=True)
        if result.returncode == 0 and health.returncode == 0:
            ready = True
            break
        time.sleep(0.5)
    if not ready:
        process.terminate()
        output, _ = process.communicate(timeout=10)
        pytest.fail(f"LiteLLM proxy did not become ready:\n{output}")
    yield root, base_url
    process.terminate()
    process.wait(timeout=20)


def test_real_proxy_health(proxy_server: tuple[Path, str]) -> None:
    root, _ = proxy_server
    result = _run(["--session", str(root / "session.json"), "--json", "proxy", "health", "--ready"])
    data = json.loads(result.stdout)
    assert data


def test_real_proxy_models(proxy_server: tuple[Path, str]) -> None:
    root, _ = proxy_server
    result = _run(["--session", str(root / "session.json"), "--json", "proxy", "models"])
    data = json.loads(result.stdout)
    assert any(item["id"] == "harness-mock" for item in data["data"])


def test_real_proxy_chat_completion(proxy_server: tuple[Path, str]) -> None:
    root, _ = proxy_server
    result = _run(["--session", str(root / "session.json"), "--json", "proxy", "chat", "Reply with pong"])
    data = json.loads(result.stdout)
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["content"] == "harness-pong"


def test_native_server_version() -> None:
    result = _run(["--json", "native", "server", "--version"])
    data = json.loads(result.stdout)
    assert data["returncode"] == 0
    assert data["stdout"] or data["stderr"]
