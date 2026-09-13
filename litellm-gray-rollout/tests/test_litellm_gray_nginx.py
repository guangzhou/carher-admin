from __future__ import annotations

import importlib.util
import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "litellm-gray-rollout" / "scripts" / "fixtures" / "nginx"
PRODUCTION_RENDERER = ROOT / "litellm-gray-rollout" / "scripts" / "render-production-nginx.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_route_matrix_matches_expected_pool() -> None:
    route_model = load_module("gray_route_model", FIXTURE / "route_model.py")
    cases = json.loads((FIXTURE / "cases.json").read_text())

    results = [route_model.evaluate_case(case) for case in cases]

    assert all(result["actual_pool"] == result["expected_pool"] for result in results)
    assert {case["name"] for case in cases} >= {
        "empty-key-fails-to-stable",
        "invalid-authorization-does-not-fallback",
        "invalid-x-api-key-fails-to-stable",
        "force-gray-image-generation",
        "force-prod-beats-force-gray",
        "protected-prod-beats-force-gray",
        "split-routes-neutral-key-to-gray",
        "split-zero-keeps-neutral-key-stable",
        "convergence-routes-control-to-canary",
        "bridge-override-beats-convergence",
        "pro-prefix-force-gray",
        "pro-prefix-control-stays-stable",
    }

    assert all(case["path"].startswith("/pro/") for case in cases)

    normalized_paths = {route_model.normalize_path(case["path"]) for case in cases}
    assert {
        "/v1/images/generations",
        "/v1/chat/completions",
        "/v1/responses",
        "/v1/messages",
        "/v1/embeddings",
    } <= normalized_paths


def test_candidate_config_covers_required_surfaces_and_observability() -> None:
    template = (FIXTURE / "nginx.conf.template").read_text()

    for token in (
        "images/generations",
        "messages",
        "responses",
        "embeddings",
        "chat/completions",
        "$effective_upstream",
        "X-LLM-Pool",
        "ts=$time_iso8601",
        "uri_class=$uri_class",
        "sid=$key_sid",
        "bridge-override.map",
        "convergence-mode.map",
    ):
        assert token in template

    assert "Authorization" not in template.split("log_format gray", 1)[1].split(";", 1)[0]
    assert "x-api-key" not in template.split("log_format gray", 1)[1].split(";", 1)[0]
    assert "$effective_inference_upstream" not in template
    assert "map $effective_upstream $pool_label" in template


def test_production_access_log_is_scoped_to_managed_product_locations(tmp_path: Path) -> None:
    renderer = load_module("production_nginx_access_log_scope", PRODUCTION_RENDERER)
    generation = tmp_path / "generation"
    generation.mkdir()

    http = renderer.http_directives(generation, "* litellm_product;", "debug-token")
    proxy = renderer.proxy_directives()

    assert "log_format litellm_gray" in http
    assert "access_log /var/log/nginx/cc-auto-link.gray.log litellm_gray;" not in http
    assert proxy.count("access_log /var/log/nginx/cc-auto-link.gray.log litellm_gray;") == 1


def test_candidate_config_quotes_regexes_with_braces_for_nginx_1_18() -> None:
    template = (FIXTURE / "nginx.conf.template").read_text()
    regex_lines = [line.strip() for line in template.splitlines() if "{8,512}" in line]

    assert regex_lines
    assert all(line.startswith('"~') and '" $' in line for line in regex_lines)


def test_candidate_config_does_not_include_inside_split_clients() -> None:
    template = (FIXTURE / "nginx.conf.template").read_text()
    split_block = template.split('split_clients "$bucket_key" $pct_pool_raw {', 1)[1].split(
        "}", 1
    )[0]

    assert "include" not in split_block
    assert "@@SPLIT_RULES@@" in split_block


def test_invalid_x_api_key_fails_closed_even_when_split_is_full() -> None:
    route_model = load_module("gray_route_model_invalid_key", FIXTURE / "route_model.py")
    result = route_model.evaluate_case(
        {
            "name": "invalid-x-api-key",
            "path": "/v1/messages",
            "headers": {"x-api-key": "not-a-litellm-key"},
            "split_percent": 100,
            "expected_pool": "prod",
        }
    )
    assert result["actual_pool"] == "prod"


def test_renderer_produces_complete_config_and_secure_generation(tmp_path: Path) -> None:
    renderer = load_module("gray_nginx_renderer", FIXTURE / "render_config.py")
    output = tmp_path / "nginx.conf"
    generation = tmp_path / "generation"

    renderer.render(
        template=FIXTURE / "nginx.conf.template",
        output=output,
        generation=generation,
        listen_port=18080,
        prod_port=18082,
        gray_port=18083,
        bridge_port=18084,
        debug_token="fixture-debug-token",
    )

    rendered = output.read_text()
    assert "@@" not in rendered
    assert "fixture-debug-token" in rendered
    assert "18082" in rendered and "18083" in rendered and "18084" in rendered
    assert (generation / "force-gray.map").stat().st_mode & 0o777 == 0o600
    assert (generation / "protected-prod.map").read_text().strip()


def test_renderer_rejects_nginx_directive_injection(tmp_path: Path) -> None:
    renderer = load_module("gray_nginx_renderer_injection", FIXTURE / "render_config.py")
    with pytest.raises(ValueError, match="unsafe"):
        renderer.render(
            template=FIXTURE / "nginx.conf.template",
            output=tmp_path / "nginx.conf",
            generation=tmp_path / "generation",
            listen_port=18080,
            prod_port=18082,
            gray_port=18083,
            bridge_port=18084,
            debug_token='bad"; include /tmp/evil;',
        )


def test_runtime_case_uses_wildcard_for_true_one_hundred_percent_split(tmp_path: Path) -> None:
    runner = load_module("gray_nginx_runner_full_split", FIXTURE / "run_fixture.py")
    generation = tmp_path / "generation"
    generation.mkdir()

    runner._configure_case(
        generation,
        {
            "name": "full-split",
            "path": "/pro/v1/messages",
            "split_percent": 100,
            "expected_pool": "gray",
        },
    )

    assert (generation / "split.conf").read_text() == "* litellm_gray;\n"


def test_renderer_embeds_split_rules_in_split_clients(tmp_path: Path) -> None:
    renderer = load_module("gray_nginx_renderer_split", FIXTURE / "render_config.py")
    output = tmp_path / "nginx.conf"
    generation = tmp_path / "generation"

    renderer.render(
        template=FIXTURE / "nginx.conf.template",
        output=output,
        generation=generation,
        listen_port=18080,
        prod_port=18082,
        gray_port=18083,
        bridge_port=18084,
        debug_token="fixture-debug-token",
        split_rules="50% litellm_gray;\n* litellm_product;",
    )

    rendered = output.read_text()
    assert "@@SPLIT_RULES@@" not in rendered
    assert "50% litellm_gray;" in rendered
    assert "include " + str(generation / "split.conf") not in rendered


def test_runtime_case_rejects_map_directive_injection(tmp_path: Path) -> None:
    runner = load_module("gray_nginx_runner_map_injection", FIXTURE / "run_fixture.py")
    generation = tmp_path / "generation"
    generation.mkdir()

    with pytest.raises(ValueError, match="unsafe key"):
        runner._configure_case(
            generation,
            {
                "name": "map-injection",
                "path": "/pro/v1/messages",
                "force_gray": ['sk-safe0001"; include /tmp/evil;'],
                "expected_pool": "gray",
            },
        )


def test_model_only_fixture_runner_passes() -> None:
    proc = subprocess.run(
        [sys.executable, str(FIXTURE / "run_fixture.py"), "--model-only"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["status"] == "PASS"
    assert result["model_cases"] >= 10
    assert result["nginx_runtime"] == "not_requested"


def test_runtime_fixture_does_not_claim_pass_without_nginx() -> None:
    proc = subprocess.run(
        [sys.executable, str(FIXTURE / "run_fixture.py"), "--nginx-binary", "definitely-missing-nginx"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 2
    result = json.loads(proc.stdout)
    assert result["status"] == "FAIL"
    assert result["nginx_runtime"] == "missing"
    assert result["exit_reason"] == "nginx_missing"


def test_runtime_fixture_can_report_an_explicit_missing_nginx_skip() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(FIXTURE / "run_fixture.py"),
            "--nginx-binary",
            "definitely-missing-nginx",
            "--skip-if-missing",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 77
    result = json.loads(proc.stdout)
    assert result["status"] == "SKIP"
    assert result["nginx_runtime"] == "missing"
    assert result["exit_reason"] == "nginx_missing"


def test_missing_nginx_skip_never_masks_a_model_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = load_module("gray_nginx_runner_failed_model", FIXTURE / "run_fixture.py")
    monkeypatch.setattr(
        runner,
        "evaluate_case",
        lambda case: {
            "name": case["name"],
            "actual_pool": "prod",
            "expected_pool": "gray",
        },
    )
    monkeypatch.setattr(runner.shutil, "which", lambda _binary: None)
    monkeypatch.setattr(
        runner.sys,
        "argv",
        ["run_fixture.py", "--nginx-binary", "missing", "--skip-if-missing"],
    )

    assert runner.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "FAIL"
    assert result["model_failures"]


def test_runtime_fixture_derives_http_requests_from_every_matrix_case(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner = load_module("gray_nginx_runner_matrix", FIXTURE / "run_fixture.py")
    cases = json.loads((FIXTURE / "cases.json").read_text())

    assert list(inspect.signature(runner._run_http_fixture).parameters) == [
        "nginx",
        "root",
        "cases",
        "production_renderer",
    ]

    seen: list[str] = []

    def record_case(**kwargs: object) -> dict[str, object]:
        case = kwargs["case"]
        assert isinstance(case, dict)
        seen.append(str(case["name"]))
        return {"name": case["name"], "ok": True, "actual": {}}

    class FakeServer:
        def __init__(self, port: int) -> None:
            self.server_address = ("127.0.0.1", port)

        def shutdown(self) -> None:
            pass

        def server_close(self) -> None:
            pass

    class FakeThread:
        def join(self, timeout: int) -> None:
            assert timeout == 2

    ports = iter((18082, 18083, 18084))
    stub_names: list[str] = []

    def fake_start_stub(name: str) -> tuple[FakeServer, FakeThread]:
        stub_names.append(name)
        return FakeServer(next(ports)), FakeThread()

    monkeypatch.setattr(
        runner,
        "_start_stub",
        fake_start_stub,
    )
    monkeypatch.setattr(runner, "_run_http_case", record_case)
    results = runner._run_http_fixture("unused-nginx", tmp_path, cases, production_renderer=False)

    assert seen == [case["name"] for case in cases]
    assert len(results) == len(cases)
    assert stub_names == ["stable", "gray", "guarded-old"]


def test_runtime_fixture_exercises_real_http_routes_with_nginx(tmp_path: Path) -> None:
    fake_nginx = tmp_path / "nginx"
    fake_nginx.write_text("#!/bin/sh\nexit 0\n")
    fake_nginx.chmod(0o700)
    proc = subprocess.run(
        [sys.executable, str(FIXTURE / "run_fixture.py"), "--nginx-binary", str(fake_nginx)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode != 0
    result = json.loads(proc.stdout)
    assert result["status"] == "FAIL"
    assert result["nginx_runtime"] == "FAIL"
    assert "http route" in result["nginx_error"].lower()
