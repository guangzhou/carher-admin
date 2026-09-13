#!/usr/bin/env python3
"""Exercise the gray routing model and, when available, a real nginx process."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from render_config import render  # noqa: E402
from route_model import evaluate_case, normalize_path  # noqa: E402

PRODUCTION_RENDERER = HERE.parents[1] / "render-production-nginx.py"


MISSING_NGINX_EXIT = 2
SKIPPED_NGINX_EXIT = 77
POOL_HEADERS = {
    "prod": "stable",
    "gray": "canary",
    "guarded-old": "guarded-old",
}
UPSTREAM_IDENTITIES = {
    "prod": "stable",
    "gray": "gray",
    "guarded-old": "guarded-old",
}
SAFE_CASE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")
SAFE_KEY = re.compile(r"sk-[A-Za-z0-9._~-]{8,512}")


class _StubHandler(BaseHTTPRequestHandler):
    def _respond(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        if length:
            self.rfile.read(length)
        payload = json.dumps(
            {
                "pool": self.server.pool_name,
                "path": self.path,
                "stub_port": self.server.server_address[1],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Upstream-Pool", self.server.pool_name)
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _respond
    do_POST = _respond

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _start_stub(pool_name: str) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    server.pool_name = pool_name
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_listener(port: int, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("nginx exited before HTTP route validation")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("nginx HTTP route listener did not become ready")


def _write_map(path: Path, values: list[str]) -> None:
    unsafe = [value for value in values if not SAFE_KEY.fullmatch(value)]
    if unsafe:
        raise ValueError(f"unsafe key in fixture map {path.name}")
    lines = [f'"{value}" 1;' for value in values]
    path.write_text("\n".join(lines) + ("\n" if lines else ""))
    path.chmod(0o600)


def _configure_case(generation: Path, case: dict[str, Any]) -> None:
    for field, filename in (
        ("protected_prod", "protected-prod.map"),
        ("force_prod", "force-prod.map"),
        ("force_gray", "force-gray.map"),
    ):
        _write_map(generation / filename, list(case.get(field, [])))

    keys = {
        value
        for field in ("protected_prod", "force_prod", "force_gray")
        for value in case.get(field, [])
    }
    headers = {str(key).lower(): str(value) for key, value in case.get("headers", {}).items()}
    for header in ("authorization", "x-api-key"):
        value = headers.get(header, "")
        if value.lower().startswith("bearer "):
            value = value[7:].strip()
        if value.startswith("sk-"):
            keys.add(value)
    sid_lines = [
        f'"{key}" {hashlib.sha256(key.encode()).hexdigest()[:12]};'
        for key in sorted(keys)
    ]
    sid_map = generation / "key-sid.map"
    sid_map.write_text("\n".join(sid_lines) + ("\n" if sid_lines else ""))
    sid_map.chmod(0o600)

    convergence = 1 if case.get("convergence_mode", False) else 0
    (generation / "convergence-mode.map").write_text(f"default {convergence};\n")
    bridge = "guarded-old" if case.get("bridge_override", False) else "off"
    (generation / "bridge-override.map").write_text(f"default {bridge};\n")
    # Retain a diagnostic copy beside the generation maps. nginx does not
    # include it from split_clients; the rules are embedded while rendering.
    (generation / "split.conf").write_text(_split_rules(case) + "\n")
    for name in ("convergence-mode.map", "bridge-override.map", "split.conf"):
        (generation / name).chmod(0o600)

def _split_rules(case: dict[str, Any]) -> str:
    split_percent = int(case.get("split_percent", 0))
    if not 0 <= split_percent <= 100:
        raise ValueError(f"invalid split_percent in case {case['name']!r}")
    if split_percent == 0:
        return "* litellm_product;"
    if split_percent == 100:
        # nginx compares hashes with a strict '<' threshold; '*' is the only
        # representation that includes every uint32 value.
        return "* litellm_gray;"
    return f"{split_percent}% litellm_gray;\n* litellm_product;"


def _request(port: int, case: dict[str, Any]) -> dict[str, Any]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    headers = {str(key): str(value) for key, value in case.get("headers", {}).items()}
    headers.update({"X-LLM-Debug": "fixture-debug-token", "Content-Type": "application/json"})
    try:
        connection.request("POST", str(case["path"]), body="{}", headers=headers)
        response = connection.getresponse()
        raw_payload = response.read()
        try:
            payload = json.loads(raw_payload)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"nginx returned non-JSON HTTP {response.status}: {raw_payload[:200]!r}"
            ) from exc
        return {
            "status": response.status,
            "pool": payload.get("pool"),
            "path": payload.get("path"),
            "stub_port": payload.get("stub_port"),
            "pool_header": response.getheader("X-LLM-Pool"),
            "upstream_header": response.getheader("X-Upstream-Pool"),
        }
    finally:
        connection.close()


def _run_http_case(
    *,
    nginx: str,
    root: Path,
    prod_port: int,
    gray_port: int,
    bridge_port: int,
    case: dict[str, Any],
    production_renderer: bool,
) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    listen_port = _free_port()
    config = root / "nginx.conf"
    generation = root / "generation"
    if production_renderer:
        generation.mkdir(mode=0o700)
        _configure_case(generation, case)
        (generation / "state.env").write_text("config_checksum=" + "a" * 64 + "\n")
        (generation / "state.env").chmod(0o600)
        base = root / "base.conf"
        base.write_text(
            f"worker_processes 1;\npid {root / 'nginx.pid'};\nerror_log {root / 'error.log'} notice;\n"
            "events { worker_connections 128; }\nhttp {\n"
            f"upstream litellm_product {{ server 127.0.0.1:{prod_port}; }}\n"
            "# @@LITELLM_GRAY_HTTP_DIRECTIVES@@\n"
            f"server {{ listen 127.0.0.1:{listen_port};\n"
            "location /pro/ {\nrewrite ^/pro/(.*)$ /$1 break;\n"
            "# @@LITELLM_GRAY_PRODUCT_PROXY_DIRECTIVES@@\n}\n}\n}\n"
        )
        base.chmod(0o600)
        token = root / "debug.token"; token.write_text("fixture-debug-token\n"); token.chmod(0o600)
        rendered = subprocess.run(
            [sys.executable, str(PRODUCTION_RENDERER), "--base-template", str(base), "--generation", str(generation), "--output", str(config), "--debug-token-file", str(token), "--gray-port", str(gray_port), "--bridge-port", str(bridge_port), "--access-log", str(root / "access.log")],
            text=True, capture_output=True, check=False,
        )
        if rendered.returncode:
            raise RuntimeError("production renderer failed: " + (rendered.stderr or rendered.stdout)[-1000:])
    else:
        render(
            template=HERE / "nginx.conf.template", output=config, generation=generation,
            listen_port=listen_port, prod_port=prod_port, gray_port=gray_port,
            bridge_port=bridge_port, debug_token="fixture-debug-token",
            split_rules=_split_rules(case),
        )
        _configure_case(generation, case)

    syntax = subprocess.run(
        [nginx, "-t", "-c", str(config), "-p", str(root)],
        text=True,
        capture_output=True,
        check=False,
    )
    if syntax.returncode:
        raise RuntimeError("nginx -t failed: " + (syntax.stderr or syntax.stdout)[-1000:])

    process = subprocess.Popen(
        [nginx, "-c", str(config), "-p", str(root), "-g", "daemon off;"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_listener(listen_port, process)
        actual = _request(listen_port, case)
        expected_pool = str(case["expected_pool"])
        expected_port = {
            "prod": prod_port,
            "gray": gray_port,
            "guarded-old": bridge_port,
        }[expected_pool]
        ok = (
            actual["status"] == 200
            and actual["pool"] == UPSTREAM_IDENTITIES[expected_pool]
            and actual["stub_port"] == expected_port
            and actual["upstream_header"] == UPSTREAM_IDENTITIES[expected_pool]
            and actual["pool_header"] == POOL_HEADERS[expected_pool]
            and actual["path"] == normalize_path(str(case["path"]))
        )
        return {"name": case["name"], "ok": ok, "actual": actual}
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)


def _run_http_fixture(
    nginx: str, root: Path, cases: list[dict[str, Any]], *, production_renderer: bool
) -> list[dict[str, Any]]:
    stubs = [_start_stub(name) for name in ("stable", "gray", "guarded-old")]
    prod_port, gray_port, bridge_port = [server.server_address[1] for server, _ in stubs]
    try:
        results = []
        for index, case in enumerate(cases):
            case_name = SAFE_CASE_NAME.sub("-", str(case["name"])).strip("-")
            results.append(
                _run_http_case(
                    nginx=nginx,
                    root=root / f"{index:02d}-{case_name}",
                    prod_port=prod_port,
                    gray_port=gray_port,
                    bridge_port=bridge_port,
                    case=case,
                    production_renderer=production_renderer,
                )
            )
        return results
    finally:
        for server, thread in stubs:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-only", action="store_true")
    parser.add_argument("--nginx-binary", default="nginx")
    parser.add_argument("--production-renderer", action="store_true")
    parser.add_argument(
        "--skip-if-missing",
        action="store_true",
        help="return status SKIP and exit 77 instead of failing when nginx is unavailable",
    )
    args = parser.parse_args()

    cases = json.loads((HERE / "cases.json").read_text())
    results = [evaluate_case(case) for case in cases]
    failures = [result for result in results if result["actual_pool"] != result["expected_pool"]]
    report: dict[str, Any] = {
        "tool": "nginx-fixture",
        "status": "FAIL" if failures else "PASS",
        "model_cases": len(results),
        "model_failures": failures,
        "nginx_runtime": "not_requested" if args.model_only else "missing",
    }

    if args.model_only:
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
        return 0 if report["status"] == "PASS" else 1

    nginx = shutil.which(args.nginx_binary)
    if not nginx:
        can_skip = args.skip_if_missing and not failures
        report["status"] = "SKIP" if can_skip else "FAIL"
        report["nginx_runtime"] = "missing"
        report["exit_reason"] = "nginx_missing"
        report["nginx_error"] = "nginx binary is required for runtime fixture validation"
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
        if failures:
            return 1
        return SKIPPED_NGINX_EXIT if can_skip else MISSING_NGINX_EXIT

    with tempfile.TemporaryDirectory(prefix="litellm-gray-nginx-") as raw:
        try:
            http_results = _run_http_fixture(
                nginx, Path(raw), cases, production_renderer=args.production_renderer
            )
            http_failures = [item for item in http_results if not item["ok"]]
            report["http_cases"] = len(http_results)
            report["http_failures"] = http_failures
            report["nginx_runtime"] = "PASS" if not http_failures else "FAIL"
            if http_failures:
                report["status"] = "FAIL"
                report["nginx_error"] = "HTTP route validation failed"
        except Exception as exc:
            report["status"] = "FAIL"
            report["nginx_runtime"] = "FAIL"
            report["nginx_error"] = f"HTTP route validation failed: {exc}"

    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
