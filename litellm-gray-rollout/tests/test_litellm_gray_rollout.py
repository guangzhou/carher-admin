from __future__ import annotations

import py_compile
import hashlib
import json
import re
import secrets
import stat
import subprocess
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = ROOT / "litellm-gray-rollout" / "scripts"


def source_envelope(data: object, *, captured_at: datetime | None = None) -> str:
    timestamp = (captured_at or datetime.now(timezone.utc)).isoformat().replace(
        "+00:00", "Z"
    )
    rendered = json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return json.dumps(
        {
            "schema_version": 1,
            "source": "fixture:gray-metrics",
            "captured_at": timestamp,
            "payload_sha256": "sha256:" + hashlib.sha256(rendered.encode()).hexdigest(),
            "data": data,
        }
    )

SHELL_TOOLS = (
    "gray-run-init.sh",
    "gray-phase.sh",
    "gray-key-route.sh",
    "gray-split-update.sh",
    "gray-workload-pin.sh",
    "gray-bridge-route.sh",
    "gray-global-rollback.sh",
    "gray-convergence-prepare.sh",
    "gray-convergence-commit.sh",
    "gray-convergence-abort.sh",
    "gray-auto-dispatch.sh",
    "gray-monitor-cycle.sh",
    "schema-snapshot.sh",
)
PYTHON_TOOLS = (
    "audit-runtime.py",
    "check-migration.py",
    "check-monitor-continuity.py",
    "check-pod-spec-shape.py",
    "check-split-capacity.py",
    "check-release-deletion-set.py",
    "collect-metrics.py",
    "collect-migration-evidence.py",
    "compatibility-runner.py",
    "metrics.py",
    "migration-ledger-runner.py",
    "prepare-values.py",
    "collect-runtime.py",
    "prepare-migration-run.py",
    "render-production-nginx.py",
)
GENERATION_FILES = (
    "protected-prod.map",
    "force-prod.map",
    "force-gray.map",
    "key-sid.map",
    "convergence-mode.map",
    "bridge-override.map",
    "split.conf",
    "state.env",
    "SHA256SUMS",
    # Optional on disk (absent hashes as the literal `unbound`), but part of the
    # generation contract: it is the K8s half of the frozen artifact, and it is
    # hashed into config_checksum so a mid-run patch cannot stay invisible.
    "workload.env",
)


def test_all_documented_artifacts_exist() -> None:
    expected = [SCRIPT_DIR / name for name in SHELL_TOOLS + PYTHON_TOOLS]
    expected += [
        SCRIPT_DIR / "fixtures" / "nginx" / "nginx.conf.template",
        SCRIPT_DIR / "fixtures" / "nginx" / "run_fixture.py",
        SCRIPT_DIR / "production-nginx" / "README.md",
        SCRIPT_DIR / "production-nginx" / "base-template.example.conf",
        ROOT / "litellm-gray-rollout" / "chart" / "Chart.yaml",
        ROOT / "litellm-gray-rollout" / "k8s" / "migration-job.yaml",
        ROOT / "litellm-gray-rollout" / "docs" / "litellm-198-gray-operator-manual.md",
        ROOT / "litellm-gray-rollout" / "docs" / "litellm-198-gray-rollout-runbook.md",
    ]

    missing = [str(path.relative_to(ROOT)) for path in expected if not path.exists()]

    assert not missing, f"missing rollout artifacts: {missing}"


def test_shell_and_python_entrypoints_are_parseable_and_executable() -> None:
    for name in SHELL_TOOLS:
        path = SCRIPT_DIR / name
        assert path.stat().st_mode & stat.S_IXUSR, f"not executable: {path}"
        proc = subprocess.run(["bash", "-n", str(path)], text=True, capture_output=True)
        assert proc.returncode == 0, proc.stderr

    for name in PYTHON_TOOLS:
        path = SCRIPT_DIR / name
        assert path.stat().st_mode & stat.S_IXUSR, f"not executable: {path}"
        py_compile.compile(str(path), doraise=True)


def test_every_script_with_a_shebang_is_executable() -> None:
    """Scan the directory instead of trusting the lists above.

    The lists are curated, so a script that nobody remembered to add is invisible to
    the test that checks the mode bit -- and a file with a `#!` line that is not `+x`
    fails exactly where the docs put it: the runbook says
    `scripts/collect-bypass-inventory.sh --output ...` and the manual says
    `v195-concurrent-index-runner.sh inspect`, both of which are "permission denied"
    as written.  On 2026-09-20 five files were in that state, including
    check-split-capacity.py the day it was written.  A shebang is the author saying
    "this is an entrypoint", so that is the thing to enumerate.
    """
    not_executable = sorted(
        path.name
        for path in SCRIPT_DIR.iterdir()
        if path.is_file()
        and path.read_bytes()[:2] == b"#!"
        and not path.stat().st_mode & stat.S_IXUSR
    )

    assert not not_executable, (
        "files declare a shebang but cannot be run: " f"{not_executable}"
    )


def test_generation_contract_is_shared_by_scripts_and_nginx_fixture() -> None:
    shell_source = "\n".join(
        path.read_text()
        for path in SCRIPT_DIR.glob("*.sh")
        if path.name in SHELL_TOOLS or path.name == "_lib.sh"
    )
    fixture = (SCRIPT_DIR / "fixtures" / "nginx" / "nginx.conf.template").read_text()

    for filename in GENERATION_FILES:
        assert filename in shell_source, f"shell tools do not implement {filename}"
        if filename.endswith((".map", ".conf")):
            if filename == "split.conf":
                renderer = (SCRIPT_DIR / "fixtures" / "nginx" / "render_config.py").read_text()
                assert filename in renderer, "nginx renderer does not safely embed split.conf"
            else:
                assert filename in fixture, f"nginx fixture does not consume {filename}"


def test_checked_in_artifacts_contain_no_public_runtime_images_or_secret_values() -> None:
    paths = [
        ROOT / "litellm-gray-rollout" / "chart",
        ROOT / "litellm-gray-rollout" / "k8s",
        ROOT / "litellm-gray-rollout" / "docs" / "litellm-198-gray-operator-manual.md",
        ROOT / "litellm-gray-rollout" / "docs" / "litellm-198-gray-rollout-runbook.md",
    ]
    text = "\n".join(
        path.read_text(errors="replace")
        for root in paths
        for path in ([root] if root.is_file() else root.rglob("*"))
        if path.is_file()
    )

    assert "ghcr.io/" not in text
    assert "docker.io/" not in text
    assert "sk-proj-" not in text
    assert "Bearer sk-" not in text
    master_key_lines = [line.strip() for line in text.splitlines() if "LITELLM_MASTER_KEY:" in line]
    assert master_key_lines == [
        "LITELLM_MASTER_KEY: REPLACE_WITH_DEDICATED_CLONE_ONLY_SK_KEY"
    ]


def test_operator_manual_covers_architecture_usage_and_safe_rollback() -> None:
    manual = ROOT / "litellm-gray-rollout" / "docs" / "litellm-198-gray-operator-manual.md"
    text = manual.read_text(encoding="utf-8")

    required = {
        "功能架构",
        "路由优先级",
        "状态机",
        "Evidence 契约",
        "指定 key 送 gray",
        "单 key 迅速切回 prod",
        "比例放量",
        "全量收敛",
        "故障处理和回滚",
        "gray-global-rollback.sh",
        "gray-convergence-abort.sh",
        "gray-post-commit-rollback.sh",
    }
    assert not {item for item in required if item not in text}
    assert "GRAY_TEST_MODE" not in text
    assert not re.search(r"(?:Bearer\s+)?sk-[A-Za-z0-9._~-]{8,}", text)


def test_schema_snapshot_requires_secure_parent_and_argv_command(tmp_path) -> None:
    output_dir = tmp_path / "schema"
    output_dir.mkdir(mode=0o700)
    output = output_dir / "schema.sql"
    proc = subprocess.run(
        [
            str(SCRIPT_DIR / "schema-snapshot.sh"),
            str(output),
            "--",
            "sh",
            "-c",
            "printf '%s\\n' '-- comment' 'CREATE TABLE x (id int);'",
        ],
        text=True,
        capture_output=True,
        env=os.environ.copy(),
    )
    assert proc.returncode == 0, proc.stderr
    assert output.read_text() == "CREATE TABLE x (id int);\n"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600

    insecure_dir = tmp_path / "insecure"
    insecure_dir.mkdir(mode=0o755)
    denied = subprocess.run(
        [str(SCRIPT_DIR / "schema-snapshot.sh"), str(insecure_dir / "schema.sql"), "--", "true"],
        text=True,
        capture_output=True,
    )
    assert denied.returncode != 0
    assert "owner-only" in denied.stderr.lower()


def test_monitor_cycle_persists_metrics_before_dispatching(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(mode=0o700)
    input_file = tmp_path / "metrics-input.json"
    input_file.write_text('{"input":"redacted"}\n')
    input_file.chmod(0o600)
    metrics = tmp_path / "metrics.py"
    metrics.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'tool':'metrics','status':'PASS'}))\n"
    )
    metrics.chmod(0o700)
    dispatch_log = tmp_path / "dispatch.log"
    dispatcher = tmp_path / "dispatch.sh"
    dispatcher.write_text(
        "#!/bin/sh\n"
        f"test -s \"$2\" && printf '%s' \"$2\" > '{dispatch_log}'\n"
    )
    dispatcher.chmod(0o700)

    result = subprocess.run(
        [
            str(SCRIPT_DIR / "gray-monitor-cycle.sh"),
            "--input",
            str(input_file),
            "--evidence-dir",
            str(evidence_dir),
        ],
        text=True,
        capture_output=True,
        env={
            "PATH": os.environ["PATH"],
            "GRAY_ALLOW_NONROOT": "1",
            "GRAY_TEST_MODE": "1",
            "GRAY_METRICS_TOOL": str(metrics),
            "GRAY_DISPATCH_TOOL": str(dispatcher),
        },
        check=False,
    )

    assert result.returncode == 0, result.stderr
    evidence = Path(dispatch_log.read_text())
    assert evidence.parent == evidence_dir
    assert evidence.exists()
    assert stat.S_IMODE(evidence.stat().st_mode) == 0o600
    assert json.loads(evidence.read_text()) == {"tool": "metrics", "status": "PASS"}

    # Deadman ledger: a cycle that never ran leaves no metrics file, so the
    # only way to prove a window was observed is an append-only record of the
    # cycles that completed.
    ledger = evidence_dir / "monitor-heartbeat.jsonl"
    assert stat.S_IMODE(ledger.stat().st_mode) == 0o600
    lines = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0]["tool"] == "gray-monitor-cycle"
    assert lines[0]["metrics_status"] == "PASS"
    assert lines[0]["evidence"] == evidence.name
    assert lines[0]["cycle_completed_at"].endswith("Z")


def test_monitor_cycle_carries_sustain_state_across_cycles(tmp_path: Path) -> None:
    """The streak must survive between cycles, or it can never complete.

    metrics.py only promotes a statistical breach to a trigger once it has held
    for SUSTAIN_WINDOWS cycles, and each cycle is a separate process. If the
    counts are not written back and spliced into the next input, the counter
    never reaches the threshold and the latency/5xx legs are silently dead --
    a stop-loss that looks armed and cannot fire.
    """
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(mode=0o700)
    input_file = tmp_path / "metrics-input.json"
    input_file.write_text(json.dumps({"evidence": {"run_id": "r1", "generation": "g1"}}))
    input_file.chmod(0o600)

    # Stand-in for metrics.py: echoes the sustain_state it was handed back as an
    # incremented count, which is what the real tool does on a repeated breach.
    metrics = tmp_path / "metrics.py"
    metrics.write_text(
        "#!/usr/bin/env python3\n"
        "import argparse, json\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--input')\n"
        "a = p.parse_args()\n"
        "payload = json.load(open(a.input))\n"
        "seen = payload.get('sustain_state', {})\n"
        "count = int(seen.get('P95_RATIO', 0)) + 1\n"
        "print(json.dumps({'tool': 'metrics', 'status': 'PASS',\n"
        "                  'run_id': payload['evidence']['run_id'],\n"
        "                  'generation': payload['evidence']['generation'],\n"
        "                  'observed_sustain_state': seen,\n"
        "                  'sustain': {'counts': {'P95_RATIO': count}}}))\n"
    )
    metrics.chmod(0o700)
    dispatcher = tmp_path / "dispatch.sh"
    dispatcher.write_text("#!/bin/sh\nexit 0\n")
    dispatcher.chmod(0o700)

    def cycle() -> None:
        result = subprocess.run(
            [
                str(SCRIPT_DIR / "gray-monitor-cycle.sh"),
                "--input", str(input_file),
                "--evidence-dir", str(evidence_dir),
            ],
            text=True,
            capture_output=True,
            env={
                "PATH": os.environ["PATH"],
                "GRAY_ALLOW_NONROOT": "1",
                "GRAY_TEST_MODE": "1",
                "GRAY_METRICS_TOOL": str(metrics),
                "GRAY_DISPATCH_TOOL": str(dispatcher),
            },
            check=False,
        )
        assert result.returncode == 0, result.stderr

    def evidence_payloads() -> list[dict[str, Any]]:
        return [
            json.loads(path.read_text())
            for path in sorted(evidence_dir.glob("metrics-*.json"))
        ]

    state = evidence_dir / "monitor-sustain.json"

    cycle()
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    assert json.loads(state.read_text())["counts"] == {"P95_RATIO": 1}
    # First cycle starts from nothing: a lost ledger must cost a delay, not a
    # spurious rollback.
    assert evidence_payloads()[-1]["observed_sustain_state"] == {}

    cycle()
    assert json.loads(state.read_text())["counts"] == {"P95_RATIO": 2}
    assert evidence_payloads()[-1]["observed_sustain_state"] == {"P95_RATIO": 1}

    # A new generation is new bytes on the wire: counts earned against the old
    # config say nothing about the new one and must not half-complete a streak.
    input_file.write_text(json.dumps({"evidence": {"run_id": "r1", "generation": "g2"}}))
    input_file.chmod(0o600)
    cycle()
    assert evidence_payloads()[-1]["observed_sustain_state"] == {}

    # The frozen evidence is never rewritten to carry state; the splice happens
    # in a private copy, and no scratch input is left behind.
    assert "sustain_state" not in json.loads(input_file.read_text())
    assert list(evidence_dir.glob(".metrics-input.*")) == []


def test_monitor_cycle_sustain_splice_survives_the_real_evidence_checksum() -> None:
    """The spliced sustain_state must not break the evidence checksum.

    Every other sustain test hands gray-monitor-cycle.sh a stand-in for
    metrics.py, so none of them exercises the checksum at all -- and the
    fixtures that do reach the real tool used to sign the payload with
    sustain_state already inside it, which no real cycle ever produces.
    Between those two gaps, the pipeline shipped with the splice invalidating
    the signature: collect-metrics.py signs the payload before the counts
    exist, so metrics.py rejected every staged cycle with
    EVIDENCE_CHECKSUM_MISMATCH and the heartbeat ledger stayed empty. A dead
    monitor loop looks exactly like a quiet one, which is the shape the ramp
    gate exists to refuse.

    So this runs the real metrics.py against a payload signed the way the
    collector signs it, then asserts both directions: the splice passes, and
    tampering with a field the checksum does cover still fails.
    """

    def signed(**kwargs: Any) -> dict[str, Any]:
        return _metrics_payload(gray_latency=0.5, stable_latency=0.5, **kwargs)

    # Un-spliced: what collect-metrics.py writes.
    assert _evaluate(signed())["status"] == "PASS"

    # Spliced the way gray-monitor-cycle.sh splices it, with and without a
    # carried streak. Both are real first/second cycles.
    for counts in ({}, {"P95_RATIO": 1}):
        result = _evaluate(signed(sustain_state=counts))
        assert result["status"] == "PASS", result.get("errors")
        assert "EVIDENCE_CHECKSUM_MISMATCH" not in result.get("errors", [])

    # Negative control: the checksum must still protect everything else. If
    # these passed, the fix above would have disarmed the whole envelope
    # rather than narrowed it.
    for mutate in (
        lambda p: p.update(rollout_percent=10),
        lambda p: p["records"][0].update(status=503),
        lambda p: p["backend_health"].update(prod=False),
    ):
        payload = signed(sustain_state={})
        mutate(payload)
        tampered = _evaluate(payload)
        assert tampered["status"] == "ERROR"
        assert tampered["errors"] == ["EVIDENCE_CHECKSUM_MISMATCH"]


def test_monitor_cycle_never_dispatches_invalid_or_failed_metrics(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(mode=0o700)
    input_file = tmp_path / "metrics-input.json"
    input_file.write_text("{}\n")
    input_file.chmod(0o600)
    marker = tmp_path / "dispatched"
    dispatcher = tmp_path / "dispatch.sh"
    dispatcher.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    dispatcher.chmod(0o700)

    for body, exit_code in (("not-json\n", 0), ('{"tool":"metrics"}\n', 2)):
        metrics = tmp_path / f"metrics-{exit_code}-{len(body)}.py"
        metrics.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f"sys.stdout.write({body!r})\n"
            f"raise SystemExit({exit_code})\n"
        )
        metrics.chmod(0o700)
        result = subprocess.run(
            [
                str(SCRIPT_DIR / "gray-monitor-cycle.sh"),
                "--input",
                str(input_file),
                "--evidence-dir",
                str(evidence_dir),
            ],
            text=True,
            capture_output=True,
            env={
                "PATH": os.environ["PATH"],
                "GRAY_ALLOW_NONROOT": "1",
                "GRAY_TEST_MODE": "1",
                "GRAY_METRICS_TOOL": str(metrics),
                "GRAY_DISPATCH_TOOL": str(dispatcher),
            },
            check=False,
        )
        assert result.returncode != 0
        assert not marker.exists()


def test_monitor_cycle_refuses_unfrozen_tool_overrides_outside_test_mode(
    tmp_path: Path,
) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(mode=0o700)
    input_file = tmp_path / "metrics-input.json"
    input_file.write_text("{}\n")
    input_file.chmod(0o600)
    marker = tmp_path / "ran"
    replacement = tmp_path / "replacement"
    replacement.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    replacement.chmod(0o700)

    result = subprocess.run(
        [
            str(SCRIPT_DIR / "gray-monitor-cycle.sh"),
            "--input",
            str(input_file),
            "--evidence-dir",
            str(evidence_dir),
        ],
        text=True,
        capture_output=True,
        env={
            "PATH": os.environ["PATH"],
            "GRAY_ALLOW_NONROOT": "1",
            "GRAY_METRICS_TOOL": str(replacement),
            "GRAY_DISPATCH_TOOL": str(replacement),
        },
        check=False,
    )

    assert result.returncode != 0
    assert "test mode" in result.stderr.lower() or "override" in result.stderr.lower()
    assert not marker.exists()


def test_production_nginx_renderer_embeds_generation_and_replaces_atomically(tmp_path) -> None:
    template = tmp_path / "nginx.template.conf"
    template.write_text(
        "events {}\nhttp {\n"
        "upstream litellm_product { server 127.0.0.1:30402; }\n"
        "# @@LITELLM_GRAY_HTTP_DIRECTIVES@@\n"
        "server { location /pro/ { rewrite ^/pro/(.*)$ /$1 break;\n"
        "# @@LITELLM_GRAY_PRODUCT_PROXY_DIRECTIVES@@\n"
        "} }\n}\n"
    )
    template.chmod(0o600)
    generation = tmp_path / "generation"
    generation.mkdir(mode=0o700)
    for filename, content in {
        "protected-prod.map": "",
        "force-prod.map": "",
        "force-gray.map": '"sk-forcegray0001" 1;\n',
        "key-sid.map": '"sk-forcegray0001" abcdef012345;\n',
        "convergence-mode.map": "default 0;\n",
        "bridge-override.map": "default off;\n",
        "split.conf": "50% litellm_gray;\n* litellm_product;\n",
    }.items():
        path = generation / filename
        path.write_text(content)
        path.chmod(0o600)
    token = tmp_path / "debug.token"
    token.write_text("debug-token-0001\n")
    token.chmod(0o600)
    output = tmp_path / "nginx.conf"
    command = [
        "python3",
        str(SCRIPT_DIR / "render-production-nginx.py"),
        "--base-template",
        str(template),
        "--generation",
        str(generation),
        "--output",
        str(output),
        "--debug-token-file",
        str(token),
    ]
    first = subprocess.run(command, text=True, capture_output=True, check=False)
    assert first.returncode == 0, first.stderr
    rendered = output.read_text()
    assert "@@LITELLM_GRAY" not in rendered
    assert "50% litellm_gray;" in rendered
    assert f"include {generation}/force-gray.map;" in rendered
    assert "proxy_pass http://$product_upstream;" in rendered
    assert stat.S_IMODE(output.stat().st_mode) == 0o600

    (generation / "split.conf").write_text("* litellm_gray;\n")
    (generation / "split.conf").chmod(0o600)
    second = subprocess.run(command, text=True, capture_output=True, check=False)
    assert second.returncode == 0, second.stderr
    assert "* litellm_gray;" in output.read_text()
    assert "50% litellm_gray;" not in output.read_text()


def test_production_nginx_renderer_rejects_missing_markers_and_symlink_output(tmp_path) -> None:
    template = tmp_path / "bad.template.conf"
    template.write_text("events {}\nhttp {}\n")
    template.chmod(0o600)
    generation = tmp_path / "generation"
    generation.mkdir(mode=0o700)
    for filename in (
        "protected-prod.map", "force-prod.map", "force-gray.map", "key-sid.map",
        "convergence-mode.map", "bridge-override.map", "split.conf",
    ):
        path = generation / filename
        path.write_text("* litellm_product;\n" if filename == "split.conf" else "")
        path.chmod(0o600)
    token = tmp_path / "debug.token"
    token.write_text("debug-token-0001\n")
    token.chmod(0o600)
    output = tmp_path / "nginx.conf"
    denied = subprocess.run(
        [
            "python3", str(SCRIPT_DIR / "render-production-nginx.py"),
            "--base-template", str(template), "--generation", str(generation),
            "--output", str(output), "--debug-token-file", str(token),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert denied.returncode != 0
    assert "marker" in denied.stderr.lower()

    template.write_text(
        "http { upstream litellm_product { server 127.0.0.1:30402; }\n"
        "# @@LITELLM_GRAY_HTTP_DIRECTIVES@@\nserver { location /pro/ {\n"
        "# @@LITELLM_GRAY_PRODUCT_PROXY_DIRECTIVES@@\n} } }\n"
    )
    real = tmp_path / "real.conf"
    real.write_text("do not overwrite\n")
    output.symlink_to(real)
    denied = subprocess.run(
        [
            "python3", str(SCRIPT_DIR / "render-production-nginx.py"),
            "--base-template", str(template), "--generation", str(generation),
            "--output", str(output), "--debug-token-file", str(token),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert denied.returncode != 0
    assert real.read_text() == "do not overwrite\n"


def test_production_nginx_renderer_routes_entire_pro_prefix_during_overrides(tmp_path) -> None:
    template = tmp_path / "nginx.template.conf"
    template.write_text(
        "http { upstream litellm_product { server 127.0.0.1:30402; }\n"
        "# @@LITELLM_GRAY_HTTP_DIRECTIVES@@\nserver { location /pro/ {\n"
        "# @@LITELLM_GRAY_PRODUCT_PROXY_DIRECTIVES@@\n} } }\n"
    )
    template.chmod(0o600)
    generation = tmp_path / "generation"
    generation.mkdir(mode=0o700)
    for filename, content in {
        "protected-prod.map": "",
        "force-prod.map": "",
        "force-gray.map": "",
        "key-sid.map": "",
        "convergence-mode.map": "default 1;\n",
        "bridge-override.map": "default off;\n",
        "split.conf": "* litellm_product;\n",
    }.items():
        path = generation / filename
        path.write_text(content)
        path.chmod(0o600)
    token = tmp_path / "debug.token"
    token.write_text("debug-token-0001\n")
    token.chmod(0o600)
    output = tmp_path / "nginx.conf"
    result = subprocess.run(
        [
            "python3", str(SCRIPT_DIR / "render-production-nginx.py"),
            "--base-template", str(template), "--generation", str(generation),
            "--output", str(output), "--debug-token-file", str(token),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    rendered = output.read_text()
    assert 'map "$bridge_override:$convergence_mode" $whole_product_override' in rendered
    assert "proxy_pass http://$product_upstream;" in rendered
    assert "map $uri $normal_route_upstream" in rendered


def test_production_nginx_renderer_requires_every_pro_location_to_use_state_machine(
    tmp_path,
) -> None:
    template = tmp_path / "nginx.template.conf"
    template.write_text(
        "http { upstream litellm_product { server 127.0.0.1:30402; }\n"
        "# @@LITELLM_GRAY_HTTP_DIRECTIVES@@\nserver {\n"
        "location ^~ /pro/ui/ { proxy_pass http://litellm_product; }\n"
        "location ^~ /pro/ { # @@LITELLM_GRAY_PRODUCT_PROXY_DIRECTIVES@@\n }\n"
        "} }\n"
    )
    template.chmod(0o600)
    generation = tmp_path / "generation"
    generation.mkdir(mode=0o700)
    for filename, content in {
        "protected-prod.map": "",
        "force-prod.map": "",
        "force-gray.map": "",
        "key-sid.map": "",
        "convergence-mode.map": "default 0;\n",
        "bridge-override.map": "default off;\n",
        "split.conf": "* litellm_product;\n",
    }.items():
        path = generation / filename
        path.write_text(content)
        path.chmod(0o600)
    token = tmp_path / "debug.token"
    token.write_text("debug-token-0001\n")
    token.chmod(0o600)
    output = tmp_path / "nginx.conf"
    command = [
        "python3",
        str(SCRIPT_DIR / "render-production-nginx.py"),
        "--base-template",
        str(template),
        "--generation",
        str(generation),
        "--output",
        str(output),
        "--debug-token-file",
        str(token),
    ]

    rejected = subprocess.run(command, text=True, capture_output=True, check=False)

    assert rejected.returncode != 0
    assert "unmanaged /pro location" in rejected.stderr.lower()

    template.write_text(
        "http { upstream litellm_product { server 127.0.0.1:30402; }\n"
        "# @@LITELLM_GRAY_HTTP_DIRECTIVES@@\nserver {\n"
        "location ^~ /pro/ui/ { # @@LITELLM_GRAY_PRODUCT_PROXY_DIRECTIVES@@\n }\n"
        "location ^~ /pro/ { # @@LITELLM_GRAY_PRODUCT_PROXY_DIRECTIVES@@\n }\n"
        "} }\n"
    )
    accepted = subprocess.run(command, text=True, capture_output=True, check=False)

    assert accepted.returncode == 0, accepted.stderr
    assert output.read_text().count("proxy_pass http://$product_upstream;") == 2


def test_production_nginx_renderer_rejects_shared_environment_regex_location(
    tmp_path,
) -> None:
    template = tmp_path / "nginx.template.conf"
    template.write_text(
        "http { upstream litellm_product { server 127.0.0.1:30402; }\n"
        "# @@LITELLM_GRAY_HTTP_DIRECTIVES@@\nserver {\n"
        "location ~ ^/(dev|pro)/v1/messages { proxy_pass http://litellm_product; }\n"
        "location /pro/ { # @@LITELLM_GRAY_PRODUCT_PROXY_DIRECTIVES@@\n }\n"
        "} }\n"
    )
    template.chmod(0o600)
    generation = tmp_path / "generation"
    generation.mkdir(mode=0o700)
    for filename, content in {
        "protected-prod.map": "",
        "force-prod.map": "",
        "force-gray.map": "",
        "key-sid.map": "",
        "convergence-mode.map": "default 0;\n",
        "bridge-override.map": "default off;\n",
        "split.conf": "* litellm_product;\n",
    }.items():
        path = generation / filename
        path.write_text(content)
        path.chmod(0o600)
    token = tmp_path / "debug.token"
    token.write_text("debug-token-0001\n")
    token.chmod(0o600)

    rejected = subprocess.run(
        [
            "python3",
            str(SCRIPT_DIR / "render-production-nginx.py"),
            "--base-template",
            str(template),
            "--generation",
            str(generation),
            "--output",
            str(tmp_path / "nginx.conf"),
            "--debug-token-file",
            str(token),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert rejected.returncode != 0
    assert "shared" in rejected.stderr.lower() or "unmanaged /pro" in rejected.stderr.lower()


def _render_command(tmp_path, template) -> list[str]:
    generation = tmp_path / "generation"
    if not generation.exists():
        generation.mkdir(mode=0o700)
        for filename, content in {
            "protected-prod.map": "",
            "force-prod.map": "",
            "force-gray.map": "",
            "key-sid.map": "",
            "convergence-mode.map": "default 0;\n",
            "bridge-override.map": "default off;\n",
            "split.conf": "* litellm_product;\n",
        }.items():
            path = generation / filename
            path.write_text(content)
            path.chmod(0o600)
        token = tmp_path / "debug.token"
        token.write_text("debug-token-0001\n")
        token.chmod(0o600)
    return [
        "python3",
        str(SCRIPT_DIR / "render-production-nginx.py"),
        "--base-template",
        str(template),
        "--generation",
        str(generation),
        "--output",
        str(tmp_path / "nginx.conf"),
        "--debug-token-file",
        str(tmp_path / "debug.token"),
    ]


def test_production_nginx_renderer_exempts_redirect_only_pro_locations(tmp_path) -> None:
    """Live 198 has `= /pro` and `= /pro/ui` as bare `return 301` blocks.

    They cannot reach the product upstream, so demanding a marker there is not
    just noise: the marker expands to `access_log ... litellm_gray`, and
    collect-metrics.py samples every matching line, so instant 301s would
    dilute the 5xx denominator and depress p95/p99 — false green on both axes.
    """
    template = tmp_path / "nginx.template.conf"
    template.write_text(
        "http { upstream litellm_product { server 127.0.0.1:30402; }\n"
        "# @@LITELLM_GRAY_HTTP_DIRECTIVES@@\nserver {\n"
        "location = /pro { return 301 /pro/; }\n"
        "location = /pro/ui { return 302 /pro/ui/; }\n"
        "location /pro/ { # @@LITELLM_GRAY_PRODUCT_PROXY_DIRECTIVES@@\n }\n"
        "} }\n"
    )
    template.chmod(0o600)
    command = _render_command(tmp_path, template)

    accepted = subprocess.run(command, text=True, capture_output=True, check=False)

    assert accepted.returncode == 0, accepted.stderr
    rendered = (tmp_path / "nginx.conf").read_text()
    assert rendered.count("proxy_pass http://$product_upstream;") == 1
    assert rendered.count("access_log /var/log/nginx/cc-auto-link.gray.log litellm_gray;") == 1


def test_production_nginx_renderer_rejects_a_marker_inside_a_redirect_only_location(
    tmp_path,
) -> None:
    template = tmp_path / "nginx.template.conf"
    template.write_text(
        "http { upstream litellm_product { server 127.0.0.1:30402; }\n"
        "# @@LITELLM_GRAY_HTTP_DIRECTIVES@@\nserver {\n"
        "location = /pro { return 301 /pro/;\n"
        "# @@LITELLM_GRAY_PRODUCT_PROXY_DIRECTIVES@@\n }\n"
        "location /pro/ { # @@LITELLM_GRAY_PRODUCT_PROXY_DIRECTIVES@@\n }\n"
        "} }\n"
    )
    template.chmod(0o600)

    rejected = subprocess.run(
        _render_command(tmp_path, template), text=True, capture_output=True, check=False
    )

    assert rejected.returncode != 0
    assert "redirect-only" in rejected.stderr.lower()


def test_production_nginx_renderer_still_manages_a_conditional_redirect_location(
    tmp_path,
) -> None:
    """A `return` inside `if {}` is conditional — the fallthrough may proxy."""
    template = tmp_path / "nginx.template.conf"
    template.write_text(
        "http { upstream litellm_product { server 127.0.0.1:30402; }\n"
        "# @@LITELLM_GRAY_HTTP_DIRECTIVES@@\nserver {\n"
        "location ^~ /pro/ui/ {\n"
        "  if ($http_user_agent = bad) { return 301 /pro/; }\n"
        "  proxy_pass http://litellm_product;\n"
        "}\n"
        "location /pro/ { # @@LITELLM_GRAY_PRODUCT_PROXY_DIRECTIVES@@\n }\n"
        "} }\n"
    )
    template.chmod(0o600)

    rejected = subprocess.run(
        _render_command(tmp_path, template), text=True, capture_output=True, check=False
    )

    assert rejected.returncode != 0
    assert "unmanaged /pro location" in rejected.stderr.lower()


def test_production_nginx_renderer_attaches_the_gray_log_format(tmp_path) -> None:
    template = tmp_path / "nginx.template.conf"
    template.write_text(
        "http { upstream litellm_product { server 127.0.0.1:30402; }\n"
        "# @@LITELLM_GRAY_HTTP_DIRECTIVES@@\nserver { access_log /tmp/original.log; location /pro/ {\n"
        "# @@LITELLM_GRAY_PRODUCT_PROXY_DIRECTIVES@@\n} } }\n"
    )
    template.chmod(0o600)
    generation = tmp_path / "generation"
    generation.mkdir(mode=0o700)
    for filename, content in {
        "protected-prod.map": "",
        "force-prod.map": "",
        "force-gray.map": "",
        "key-sid.map": "",
        "convergence-mode.map": "default 0;\n",
        "bridge-override.map": "default off;\n",
        "split.conf": "* litellm_product;\n",
    }.items():
        path = generation / filename
        path.write_text(content)
        path.chmod(0o600)
    token = tmp_path / "debug.token"
    token.write_text("debug-token-0001\n")
    token.chmod(0o600)
    output = tmp_path / "nginx.conf"

    result = subprocess.run(
        [
            "python3", str(SCRIPT_DIR / "render-production-nginx.py"),
            "--base-template", str(template), "--generation", str(generation),
            "--output", str(output), "--debug-token-file", str(token),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    rendered = output.read_text()
    assert "log_format litellm_gray" in rendered
    assert "access_log /var/log/nginx/cc-auto-link.gray.log litellm_gray;" in rendered
    assert "Authorization" not in rendered.split("log_format litellm_gray", 1)[1].split(";", 1)[0]
    assert "x-api-key" not in rendered.split("log_format litellm_gray", 1)[1].split(";", 1)[0]


def test_production_nginx_renderer_preserves_existing_candidate_on_render_failure(tmp_path) -> None:
    template = tmp_path / "nginx.template.conf"
    template.write_text(
        "http { upstream litellm_product { server 127.0.0.1:30402; }\n"
        "# @@LITELLM_GRAY_HTTP_DIRECTIVES@@\nserver { location /pro/ {\n"
        "# @@LITELLM_GRAY_PRODUCT_PROXY_DIRECTIVES@@\n} } }\n"
    )
    template.chmod(0o600)
    generation = tmp_path / "generation"
    generation.mkdir(mode=0o700)
    for filename, content in {
        "protected-prod.map": "",
        "force-prod.map": "",
        "force-gray.map": "",
        "key-sid.map": "",
        "convergence-mode.map": "default 0;\n",
        "bridge-override.map": "default off;\n",
        "split.conf": "* litellm_product;\n",
    }.items():
        path = generation / filename
        path.write_text(content)
        path.chmod(0o600)
    token = tmp_path / "debug.token"
    token.write_text("debug-token-0001\n")
    token.chmod(0o600)
    output = tmp_path / "nginx.conf"
    output.write_text("known-good\n")
    output.chmod(0o600)
    (generation / "split.conf").write_text("include /tmp/evil;\n")
    (generation / "split.conf").chmod(0o600)
    denied = subprocess.run(
        [
            "python3", str(SCRIPT_DIR / "render-production-nginx.py"),
            "--base-template", str(template), "--generation", str(generation),
            "--output", str(output), "--debug-token-file", str(token),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert denied.returncode != 0
    assert output.read_text() == "known-good\n"


def test_production_nginx_renderer_writes_a_bound_mode_0600_attestation(tmp_path) -> None:
    template = tmp_path / "nginx.template.conf"
    template.write_text(
        "http { upstream litellm_product { server 127.0.0.1:30402; }\n"
        "# @@LITELLM_GRAY_HTTP_DIRECTIVES@@\nserver { location /pro/ {\n"
        "# @@LITELLM_GRAY_PRODUCT_PROXY_DIRECTIVES@@\n} } }\n"
    )
    template.chmod(0o600)
    generation = tmp_path / "generation"
    generation.mkdir(mode=0o700)
    for filename, content in {
        "protected-prod.map": "",
        "force-prod.map": "",
        "force-gray.map": "",
        "key-sid.map": "",
        "convergence-mode.map": "default 0;\n",
        "bridge-override.map": "default off;\n",
        "split.conf": "* litellm_product;\n",
    }.items():
        path = generation / filename
        path.write_text(content)
        path.chmod(0o600)
    config_checksum = "a" * 64
    state = generation / "state.env"
    state.write_text(f"config_checksum={config_checksum}\n")
    state.chmod(0o600)
    token = tmp_path / "debug.token"
    token.write_text("debug-token-0001\n")
    token.chmod(0o600)
    output = tmp_path / "nginx.conf"
    attestation = generation / "render-attestation.json"

    result = subprocess.run(
        [
            "python3", str(SCRIPT_DIR / "render-production-nginx.py"),
            "--base-template", str(template), "--generation", str(generation),
            "--output", str(output), "--debug-token-file", str(token),
            "--attestation", str(attestation),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    evidence = json.loads(attestation.read_text())
    assert stat.S_IMODE(attestation.stat().st_mode) == 0o600
    assert evidence["generation_config_checksum"] == config_checksum
    assert evidence["base_template_sha256"] == hashlib.sha256(template.read_bytes()).hexdigest()
    assert evidence["debug_token_sha256"] == hashlib.sha256(token.read_bytes()).hexdigest()
    assert evidence["renderer_sha256"] == hashlib.sha256(
        (SCRIPT_DIR / "render-production-nginx.py").read_bytes()
    ).hexdigest()
    assert evidence["output_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()


def test_collect_metrics_builds_bound_input_without_key_material(tmp_path: Path) -> None:
    access_log = tmp_path / "gray.log"
    captured_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    old_at = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat().replace(
        "+00:00", "Z"
    )
    access_log.write_text(
        f"ts={old_at} 10.0.0.9 503 9.000 pool=canary upstream=127.0.0.1:30405 "
        "rt=8.500 uri_class=messages sid=-\n"
        f"ts={captured_at} 10.0.0.1 200 0.250 pool=canary upstream=127.0.0.1:30405 "
        "rt=0.100, 0.140 uri_class=messages sid=abcdef123456\n"
        f"ts={captured_at} 10.0.0.2 200 0.100 pool=stable upstream=127.0.0.1:30402 "
        "rt=0.090 uri_class=messages sid=-\n"
        f"ts={captured_at} 10.0.0.3 502 0.300 pool=canary upstream=- "
        "rt=- uri_class=messages sid=-\n",
        encoding="utf-8",
    )
    hard_errors = tmp_path / "hard-errors.json"
    hard_errors.write_text(source_envelope({"prisma_error": 0, "gray_pod_restart": 0}))
    backend_health = tmp_path / "backend-health.json"
    backend_health.write_text(source_envelope({"gray": True, "prod": True}))
    spend = tmp_path / "spend.json"
    spend.write_text(
        source_envelope(
            {
                "expected_request_ids": ["req-1"],
                "terminal_request_ids": ["req-1"],
                "failed_request_ids": [],
                "observed_lag_seconds": 1,
            }
        )
    )
    # Required above split 0, and this runs at 1.  Passed here rather than defaulted
    # in the collector: the readiness leg's expected count comes from the run sheet,
    # and a collector that invented one would be the `llm-stab-scrape-down` literal
    # all over again.
    readiness = tmp_path / "readiness.json"
    readiness.write_text(
        source_envelope(
            {
                "lane": "gray",
                "ready_containers": 3,
                "expected_containers": 3,
                "observed_at": captured_at,
            }
        )
    )
    output = tmp_path / "metrics-input.json"

    result = subprocess.run(
        [
            "python3",
            str(SCRIPT_DIR / "collect-metrics.py"),
            "--access-log", str(access_log),
            "--hard-errors", str(hard_errors),
            "--backend-health", str(backend_health),
            "--spend-reconciliation", str(spend),
            "--readiness", str(readiness),
            "--run-id", "run-test",
            "--generation", "g000001",
            "--config-checksum", "a" * 64,
            "--phase", "normal_gray",
            "--rollout-percent", "1",
            "--output", str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(output.read_text())
    assert payload["records"] == [
        {
            "pool_label": "canary",
            "uri_class": "messages",
            "status": 200,
            "response_time": 0.24,
        },
        {
            "pool_label": "stable",
            "uri_class": "messages",
            "status": 200,
            "response_time": 0.09,
        },
        {
            "pool_label": "canary",
            "uri_class": "messages",
            "status": 502,
            "response_time": 0.3,
        },
    ]
    rendered = output.read_text()
    # `abcdef123456` is the sid on the one authenticated line above -- a sha256 head
    # of a real virtual key.  The log format carries it, the collector matches it so
    # the trailing field cannot break the line regex, and nothing keeps it: this
    # payload is signed and shipped, and no leg reads sid (it cannot be the
    # enrolment filter it was meant to be -- `-` means "unknown", not "not
    # enrolled", and the canary pool carried 14614 of those).
    assert "abcdef123456" not in rendered
    assert "sk-" not in rendered
    assert "Authorization" not in rendered
    # Every source the collector was given is declared, so the liveness leg measures
    # what was actually read rather than a list someone maintained by hand.
    assert [source["name"] for source in payload["data_sources"]] == [
        "access_log",
        "hard_errors",
        "backend_health",
        "spend_reconciliation",
        "readiness",
    ]

    evaluated = subprocess.run(
        ["python3", str(SCRIPT_DIR / "metrics.py"), "--input", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert evaluated.returncode == 0, evaluated.stdout + evaluated.stderr


def test_collect_metrics_rejects_secret_bearing_log_lines(tmp_path: Path) -> None:
    access_log = tmp_path / "gray.log"
    access_log.write_text(
        "Authorization: Bearer sk-secret-value pool=canary status=200\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "python3",
            str(SCRIPT_DIR / "collect-metrics.py"),
            "--access-log", str(access_log),
            "--run-id", "run-test",
            "--generation", "g000001",
            "--config-checksum", "a" * 64,
            "--phase", "normal_gray",
            "--rollout-percent", "1",
            "--output", str(tmp_path / "metrics.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "secret" in result.stderr.lower() or "invalid" in result.stderr.lower()


def test_collect_metrics_rejects_stale_or_future_log_windows(tmp_path: Path) -> None:
    for name, captured_at in (
        ("stale", datetime.now(timezone.utc) - timedelta(minutes=6)),
        ("future", datetime.now(timezone.utc) + timedelta(minutes=2)),
    ):
        access_log = tmp_path / f"{name}.log"
        stamp = captured_at.isoformat().replace("+00:00", "Z")
        access_log.write_text(
            f"ts={stamp} 10.0.0.1 200 0.250 pool=canary "
            "upstream=127.0.0.1:30405 rt=0.240 uri_class=messages sid=-\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                "python3",
                str(SCRIPT_DIR / "collect-metrics.py"),
                "--access-log", str(access_log),
                "--run-id", "run-test",
                "--generation", "g000001",
                "--config-checksum", "a" * 64,
                "--phase", "normal_gray",
                "--rollout-percent", "1",
                "--output", str(tmp_path / f"{name}.json"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0
        assert "window" in result.stderr.lower() or "timestamp" in result.stderr.lower()


def test_collect_metrics_rejects_stale_or_tampered_supporting_evidence(
    tmp_path: Path,
) -> None:
    stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    access_log = tmp_path / "gray.log"
    access_log.write_text(
        f"ts={stamp} 10.0.0.1 200 0.250 pool=canary "
        "upstream=127.0.0.1:30405 rt=0.240 uri_class=messages sid=-\n",
        encoding="utf-8",
    )
    for name, body in (
        (
            "stale",
            source_envelope(
                {"prisma_error": 0},
                captured_at=datetime.now(timezone.utc) - timedelta(minutes=6),
            ),
        ),
        (
            "tampered",
            json.dumps(
                {
                    "schema_version": 1,
                    "source": "fixture:gray-metrics",
                    "captured_at": stamp,
                    "payload_sha256": "sha256:" + "0" * 64,
                    "data": {"prisma_error": 0},
                }
            ),
        ),
    ):
        evidence = tmp_path / f"{name}.json"
        evidence.write_text(body, encoding="utf-8")
        result = subprocess.run(
            [
                "python3",
                str(SCRIPT_DIR / "collect-metrics.py"),
                "--access-log", str(access_log),
                "--hard-errors", str(evidence),
                "--run-id", "run-test",
                "--generation", "g000001",
                "--config-checksum", "a" * 64,
                "--phase", "normal_gray",
                "--rollout-percent", "1",
                "--output", str(tmp_path / f"{name}-output.json"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0
        assert "evidence" in result.stderr.lower() or "checksum" in result.stderr.lower()


def _stable_access_log(path: Path, samples: int = 120) -> Path:
    now = datetime.now(timezone.utc)
    lines = []
    for index in range(samples):
        stamp = (now - timedelta(seconds=30 + index)).isoformat().replace("+00:00", "Z")
        lines.append(
            f"ts={stamp} 10.0.0.2 200 0.100 pool=stable upstream=127.0.0.1:30402 "
            "rt=0.090 uri_class=messages sid=-"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _collect_metrics(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(SCRIPT_DIR / "collect-metrics.py"), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_collect_metrics_emits_a_bound_baseline_and_refuses_a_thin_window(tmp_path: Path):
    """The 100% comparison needs a produced baseline, not a hand-written file."""
    access_log = _stable_access_log(tmp_path / "access.log")
    baseline = tmp_path / "baseline.json"

    emitted = _collect_metrics(
        "--access-log", str(access_log),
        "--emit-baseline",
        "--run-id", "run-test",
        "--generation", "g000001",
        "--config-checksum", "a" * 64,
        "--phase", "preflight",
        "--rollout-percent", "0",
        "--output", str(baseline),
    )
    assert emitted.returncode == 0, emitted.stderr
    assert baseline.stat().st_mode & 0o777 == 0o600
    envelope = json.loads(baseline.read_text())
    assert envelope["source"] == "collect-metrics.py --emit-baseline"
    group = envelope["data"][0]
    assert group["uri_class"] == "messages"
    assert group["pool_label"] == "stable"
    assert group["count"] == 120
    assert group["run_id"] == "run-test"
    assert group["generation"] == "g000001"
    assert group["window_started_at"] < group["window_ended_at"]

    # A baseline taken after the ramp started is not a baseline.
    ramped = _collect_metrics(
        "--access-log", str(access_log),
        "--emit-baseline",
        "--run-id", "run-test",
        "--generation", "g000001",
        "--config-checksum", "a" * 64,
        "--phase", "normal_gray",
        "--rollout-percent", "5",
        "--output", str(tmp_path / "ramped.json"),
    )
    assert ramped.returncode != 0
    assert "rollout-percent 0" in ramped.stderr

    # Too few samples per class produces a reference nobody should compare to.
    thin = _collect_metrics(
        "--access-log", str(_stable_access_log(tmp_path / "thin.log", samples=10)),
        "--emit-baseline",
        "--run-id", "run-test",
        "--generation", "g000001",
        "--config-checksum", "a" * 64,
        "--phase", "preflight",
        "--rollout-percent", "0",
        "--output", str(tmp_path / "thin.json"),
    )
    assert thin.returncode != 0
    assert "too thin" in thin.stderr


def test_collect_metrics_rejects_a_forged_stale_or_foreign_baseline(tmp_path: Path):
    access_log = _stable_access_log(tmp_path / "access.log")
    baseline = tmp_path / "baseline.json"
    emitted = _collect_metrics(
        "--access-log", str(access_log),
        "--emit-baseline",
        "--run-id", "run-test",
        "--generation", "g000001",
        "--config-checksum", "a" * 64,
        "--phase", "preflight",
        "--rollout-percent", "0",
        "--output", str(baseline),
    )
    assert emitted.returncode == 0, emitted.stderr

    def consume(path: Path, run_id: str = "run-test") -> subprocess.CompletedProcess[str]:
        output = tmp_path / f"input-{secrets.token_hex(4)}.json"
        return _collect_metrics(
            "--access-log", str(access_log),
            "--baseline", str(path),
            "--run-id", run_id,
            "--generation", "g000002",
            "--config-checksum", "a" * 64,
            "--phase", "normal_gray",
            "--rollout-percent", "100",
            "--output", str(output),
        )

    accepted = consume(baseline)
    assert accepted.returncode == 0, accepted.stderr

    # Bound to the run: a baseline from another run cannot be reused.
    assert consume(baseline, run_id="run-other").returncode != 0

    envelope = json.loads(baseline.read_text())

    forged = dict(envelope, source="fixture:gray-metrics")
    forged_path = tmp_path / "forged.json"
    forged_path.write_text(json.dumps(forged), encoding="utf-8")
    assert consume(forged_path).returncode != 0

    stale = dict(
        envelope,
        captured_at=(datetime.now(timezone.utc) - timedelta(days=3))
        .isoformat()
        .replace("+00:00", "Z"),
    )
    stale_path = tmp_path / "stale.json"
    stale_path.write_text(json.dumps(stale), encoding="utf-8")
    stale_result = consume(stale_path)
    assert stale_result.returncode != 0
    assert "older than the change window" in stale_result.stderr

    tampered = json.loads(baseline.read_text())
    tampered["data"][0]["p95"] = 99.0
    tampered_path = tmp_path / "tampered.json"
    tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
    assert consume(tampered_path).returncode != 0


def _metrics_payload(
    *,
    gray_latency: float,
    stable_latency: float,
    live_samples: int = 300,
    wide_samples: int | None = 900,
    gray_five_xx: int = 0,
    sustain_state: dict[str, int] | None = None,
    latency_window_minutes: int | None = 30,
    ready_containers: int = 3,
    expected_containers: int = 3,
    source_ages: dict[str, int] | None = None,
) -> dict[str, Any]:
    """A minimal well-formed metrics input for the `responses` class."""

    def records(pool: str, count: int, latency: float, five_xx: int = 0) -> list[dict[str, Any]]:
        return [
            {
                "pool_label": pool,
                "uri_class": "responses",
                "status": 500 if index < five_xx else 200,
                "response_time": latency,
            }
            for index in range(count)
        ]

    # Both of these sections are REQUIRED above split 0 and this fixture runs at
    # 50, so they are defaults rather than opt-ins.  Omitting them would make every
    # test here fail with READINESS_MISSING/DATA_SOURCES_MISSING -- which is the
    # guard working, but it would also mean no fixture ever exercised the legs.
    observed = datetime.now(timezone.utc).replace(microsecond=0)
    ages = {"access_log": 0, "hard_errors": 0, "backend_health": 0, "readiness": 0}
    ages.update(source_ages or {})

    payload: dict[str, Any] = {
        "phase": "normal_gray",
        "rollout_percent": 50,
        "records": records("canary", live_samples, gray_latency, gray_five_xx)
        + records("stable", live_samples, stable_latency),
        "hard_errors": {"prisma_error": 0, "gray_pod_restart": 0},
        "backend_health": {"gray": True, "prod": True, "bridge": True},
        "spend_reconciliation": {
            "expected_request_ids": ["req-1"],
            "terminal_request_ids": ["req-1"],
            "failed_request_ids": [],
            "observed_lag_seconds": 1,
        },
        "readiness": {
            "lane": "gray",
            "ready_containers": ready_containers,
            "expected_containers": expected_containers,
            "observed_at": observed.isoformat().replace("+00:00", "Z"),
        },
        # Ages are relative to the newest observation, which is what data_liveness()
        # anchors on -- so a fixture with every age at 0 is a live cycle, and one
        # non-zero age is a single lagging source rather than a stale payload.
        "data_sources": [
            {
                "name": name,
                "observed_at": (observed - timedelta(seconds=age))
                .isoformat()
                .replace("+00:00", "Z"),
            }
            for name, age in sorted(ages.items())
        ],
    }
    if wide_samples is not None:
        payload["latency_records"] = records("canary", wide_samples, gray_latency) + records(
            "stable", wide_samples, stable_latency
        )
    if latency_window_minutes is not None:
        payload["latency_window_minutes"] = latency_window_minutes
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    # The checksum is computed BEFORE sustain_state is spliced in, exactly as
    # collect-metrics.py does it: the collector signs what it observed, and only
    # gray-monitor-cycle.sh later reads the carried counts from the state file
    # beside the ledger.  Signing the counts here instead is what let
    # EVIDENCE_CHECKSUM_MISMATCH reach production green -- every real cycle
    # failed while these fixtures passed.  See UNSIGNED_PAYLOAD_KEYS in
    # metrics.py.
    payload["evidence"] = {
        "schema_version": 1,
        "captured_at": now,
        "source": "collect-metrics.py",
        "run_id": "run-test",
        "generation": "g000001",
        "config_checksum": "test-mode",
        "payload_sha256": "sha256:"
        + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    if sustain_state is not None:
        payload["sustain_state"] = sustain_state
    return payload


def _resign(payload: dict[str, Any]) -> str:
    """Re-checksum a fixture after removing a section, over the same key set.

    A test that deletes a required section has to re-sign, or metrics.py rejects it
    on EVIDENCE_CHECKSUM_MISMATCH before reaching the leg under test -- a green that
    proves only that the envelope works.  Excludes exactly UNSIGNED_PAYLOAD_KEYS,
    matching the collector.
    """
    body = {
        key: value
        for key, value in payload.items()
        if key not in {"evidence", "sustain_state"}
    }
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


def _evaluate(payload: dict[str, Any]) -> dict[str, Any]:
    result = subprocess.run(
        ["python3", str(SCRIPT_DIR / "metrics.py"), "--input", "-"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )
    return json.loads(result.stdout)


def test_metrics_promotes_a_statistical_breach_only_after_it_repeats() -> None:
    """A single window over a sampled threshold must not move traffic.

    Stable split against itself moves p95 by 2.42x between adjacent windows with
    nothing changed at all, and the 5xx rate is no steadier -- so one window over
    a line carries almost no information.  Measured at a 14.8% false-positive rate
    against a negative control at depth 1.  The breach has to survive
    SUSTAIN_WINDOWS before it becomes a trigger, and a real regression must still
    be caught on the window after.

    The vehicle here is FIVE_XX_DELTA rather than P95_RATIO because the latency
    legs no longer trigger anything: their denominators differ ~8x between the
    frozen baseline and the live window, so the ratio measured sample variance.
    The sustain mechanism under test is the same one either way.
    """
    equal = _evaluate(_metrics_payload(gray_latency=1.0, stable_latency=1.0))
    assert equal["dispatcher_recommendation"]["action"] == "none"
    assert equal["sustain"]["counts"] == {}

    first = _evaluate(
        _metrics_payload(gray_latency=1.0, stable_latency=1.0, gray_five_xx=6)
    )
    assert first["dispatcher_recommendation"]["action"] == "none"
    # 6 of 300 is 0.02, which is exactly MAX_USER_FAILURE_RATE, so the user-facing
    # leg reads AT the line and does not breach -- that comparison is strictly
    # greater-than.  Asserted here rather than nudged to 7 because a boundary that
    # nothing pins is a boundary that quietly moves: `>=` here would make the leg
    # fire on the rate we measured as the acceptable ceiling.
    assert first["user_facing_failures"]["failure_rate"] == 0.02
    assert first["sustain"]["counts"] == {"FIVE_XX_DELTA": 1}
    assert first["sustain"]["promoted"] == []

    second = _evaluate(
        _metrics_payload(
            gray_latency=1.0,
            stable_latency=1.0,
            gray_five_xx=6,
            sustain_state=first["sustain"]["counts"],
        )
    )
    assert second["dispatcher_recommendation"]["action"] == "rollback"
    assert second["dispatcher_recommendation"]["reason_codes"] == ["FIVE_XX_DELTA"]

    # One more failure and the user-facing leg crosses too, at its own depth of 2
    # rather than the 5xx delta leg's -- the two legs read the same rows and are
    # gated separately on purpose.
    over = _evaluate(
        _metrics_payload(gray_latency=1.0, stable_latency=1.0, gray_five_xx=7)
    )
    assert over["sustain"]["counts"] == {"FIVE_XX_DELTA": 1, "USER_FAILURE_RATE": 1}
    assert over["dispatcher_recommendation"]["action"] == "none"

    # A window that does not breach resets the streak rather than decaying it:
    # bad, fine, bad, fine is noise, and letting it accumulate would rebuild the
    # very false positive the sustain gate removes.
    recovered = _evaluate(
        _metrics_payload(
            gray_latency=1.0,
            stable_latency=1.0,
            sustain_state={"FIVE_XX_DELTA": 1, "USER_FAILURE_RATE": 1},
        )
    )
    assert recovered["sustain"]["counts"] == {}
    assert recovered["dispatcher_recommendation"]["action"] == "none"


def test_metrics_reports_latency_ratios_without_arming_them() -> None:
    """The percentile ratios are readings now, and must not be able to breach.

    A 3x p95 regression used to be a rollback trigger.  It is reported and it
    triggers nothing, because the ratio's two sides are not comparable: the frozen
    baseline carries 2043-5156 samples per class against a live window of 189-317,
    so the ratio moves with sample depth.  Measured over 148 real cycles of
    litellm-198-v195-20260914 at split=100 with five_xx_count identically 0, the
    median ratio was BELOW 1.0 on every class while 17 windows breached the 1.3
    line -- and one 30-minute `responses` stretch crossed it four times with a
    2.5x swing and no errors at all.

    The `messages` class was worse than noisy: 0 of 80 windows ever qualified, so
    that leg was dark for the entire run while reporting itself armed.

    This test is the guard against re-arming them without first fixing the
    denominator (equal-depth cohorts), and against the softer failure of leaving a
    threshold behind for a leg that cannot fire -- which is how a dark leg keeps
    looking armed.
    """
    result = _evaluate(_metrics_payload(gray_latency=3.0, stable_latency=1.0))
    comparison = result["comparisons"][0]
    assert comparison["p95_ratio"] == 3.0
    assert comparison["p95_above_floor"] is True
    assert comparison["breaches"] == []
    assert result["sustain"]["counts"] == {}
    assert result["dispatcher_recommendation"]["action"] == "none"

    thresholds = result["thresholds"]
    assert thresholds["latency_observed_only"] is True
    assert "p95_ratio" not in thresholds
    assert "p99_ratio" not in thresholds

    # Below the reading floor the ratio is still reported -- that is the honest
    # number -- and it is still not a trigger.
    thin = _evaluate(
        _metrics_payload(gray_latency=3.0, stable_latency=1.0, wide_samples=150)
    )
    thin_comparison = thin["comparisons"][0]
    assert thin_comparison["p95_above_floor"] is False
    assert thin_comparison["p95_ratio"] == 3.0
    assert thin_comparison["breaches"] == []
    assert thin["dispatcher_recommendation"]["action"] == "none"


def test_metrics_keeps_the_stop_loss_leg_armed_when_latency_legs_are_dark() -> None:
    """The 5xx leg must not be switched off by the latency legs' sample floors.

    MIN_SAMPLE used to gate the whole class with a `continue`, so a class thin
    enough to darken the percentiles darkened the 5xx stop-loss with it -- the
    one leg that has to stay armed.  Measured on 2026-09-18 production traffic
    that was not hypothetical: every class was under MIN_SAMPLE in the
    five-minute window, so the stop-loss was dark across the board while the
    gate reported itself as armed.

    Here the wide latency window is below MIN_P95_SAMPLE/MIN_P99_SAMPLE, but the
    live window clears MIN_FIVE_XX_SAMPLE, so the 5xx leg still judges.
    """
    payload = _metrics_payload(
        gray_latency=1.0,
        stable_latency=1.0,
        live_samples=300,
        wide_samples=150,
        gray_five_xx=30,  # 10% against stable's 0% -- far over the 1% threshold
        sustain_state={"FIVE_XX_DELTA": 1},
    )
    result = _evaluate(payload)
    comparison = result["comparisons"][0]
    assert comparison["five_xx_qualified"] is True
    # `p95_qualified`/`p99_qualified` are gone from the payload, replaced by
    # `p95_above_floor`/`p99_above_floor`.  The rename is the point: the old names
    # said "this leg is armed and has the samples for it", and after
    # LATENCY_OBSERVED_ONLY only the second half is true.  Asserting the old keys are
    # absent stops anyone reading a `true` here as a live gate.
    assert comparison["p95_above_floor"] is False
    assert comparison["p99_above_floor"] is False
    assert "p95_qualified" not in comparison
    assert "p99_qualified" not in comparison
    assert comparison["breaches"] == ["FIVE_XX_DELTA"]
    assert result["dispatcher_recommendation"]["action"] == "rollback"

    # Below the 5xx floor the leg goes dark and says so, instead of quietly
    # reporting a rate computed over too few requests.
    thin = _evaluate(
        _metrics_payload(
            gray_latency=1.0,
            stable_latency=1.0,
            live_samples=150,
            wide_samples=150,
            gray_five_xx=15,
            sustain_state={"FIVE_XX_DELTA": 1},
        )
    )
    thin_comparison = thin["comparisons"][0]
    assert thin_comparison["five_xx_qualified"] is False
    assert thin_comparison["breaches"] == []
    assert thin["dispatcher_recommendation"]["action"] != "rollback"
    assert "FIVE_XX_SAMPLE_BELOW_FLOOR" in thin["dispatcher_recommendation"]["reason_codes"]


def test_metrics_refuses_a_latency_window_that_does_not_match_its_records() -> None:
    """Both halves of the wide window, or neither, and never below the floor.

    A caller that ships thirty minutes of records while declaring five -- or
    declares thirty and ships nothing -- would put a number on the evidence that
    the samples do not support.  Refuse rather than compare across windows of
    unknown length.
    """
    short = _metrics_payload(gray_latency=3.0, stable_latency=1.0, latency_window_minutes=5)
    assert _evaluate(short)["errors"] == ["LATENCY_WINDOW_TOO_SHORT"]

    half = _metrics_payload(
        gray_latency=3.0, stable_latency=1.0, latency_window_minutes=None
    )
    assert _evaluate(half)["errors"] == ["INVALID_LATENCY_WINDOW"]

    declared_only = _metrics_payload(
        gray_latency=3.0, stable_latency=1.0, wide_samples=None
    )
    assert _evaluate(declared_only)["errors"] == ["INVALID_LATENCY_WINDOW"]


def test_metrics_takes_error_rate_from_the_live_window_not_the_wide_one() -> None:
    """5xx must stay on the five-minute ruler even when percentiles widen.

    If the wide window fed `five_xx_rate` as well, a fault would be averaged
    across twenty-five extra minutes of health before anyone saw it -- which is
    exactly what a stop-loss must not do.  The wide window carries no 5xx here,
    so a live-window error rate that survives proves the merge kept them apart.
    """
    payload = _metrics_payload(
        gray_latency=1.0, stable_latency=1.0, live_samples=300, gray_five_xx=30
    )
    result = _evaluate(payload)
    comparison = result["comparisons"][0]
    assert comparison["five_xx_delta"] == 0.1
    assert comparison["gray_count"] == 300
    assert comparison["gray_latency_count"] == 900
    # USER_FAILURE_RATE rides along because 30 of 300 is 10%, five times its line --
    # it reads the same live rows off the same window, which is what this test is
    # about.  If it were ever absent here while FIVE_XX_DELTA was present, the two
    # legs would have drifted onto different windows.
    assert result["sustain"]["counts"] == {"FIVE_XX_DELTA": 1, "USER_FAILURE_RATE": 1}
    assert result["user_facing_failures"]["failure_rate"] == 0.1


def test_metrics_reads_ready_containers_and_never_replicas() -> None:
    """Leg 2: a lane short of ready containers must go red immediately.

    `replicas` is the ruler this leg refuses to use, and the refusal is measured,
    not stylistic: a Deployment scaled to zero still reports `Available: True`, and
    on the acct pool 165 deployments with replicas>0 had only 54 actually serving.
    Both failures point the same way -- the spec-side number reads healthy for a
    lane with nothing running.

    This trigger bypasses the sustain gate.  A container that is not ready is an
    observed fact off an exact counter, not a sampled ratio, so holding it for a
    second window would mean watching a known outage for ten minutes before acting.
    """
    short = _evaluate(
        _metrics_payload(
            gray_latency=1.0, stable_latency=1.0, ready_containers=2, expected_containers=3
        )
    )
    assert short["readiness"]["status"] == "FAIL"
    assert short["readiness"]["ruler"] == "ready_containers"
    assert short["dispatcher_recommendation"]["action"] == "rollback"
    assert "READY_CONTAINERS_SHORT" in short["dispatcher_recommendation"]["reason_codes"]
    # Fires on the first window: absent from the sustain ledger entirely, rather
    # than present with a count of 1 waiting for a repeat.
    assert "READY_CONTAINERS_SHORT" not in short["sustain"]["counts"]

    # Zero ready is the case a `grep -c`-style count cannot tell from "the query
    # failed", which is why the collector omits the document when kubectl cannot
    # answer.  When it IS read, zero is a real reading and must act.
    empty = _evaluate(
        _metrics_payload(
            gray_latency=1.0, stable_latency=1.0, ready_containers=0, expected_containers=3
        )
    )
    assert empty["readiness"]["status"] == "FAIL"
    assert empty["dispatcher_recommendation"]["action"] == "rollback"

    healthy = _evaluate(_metrics_payload(gray_latency=1.0, stable_latency=1.0))
    assert healthy["readiness"]["status"] == "PASS"
    assert healthy["dispatcher_recommendation"]["action"] == "none"


def test_metrics_refuses_to_judge_a_split_without_a_readiness_reading() -> None:
    """An absent readiness section above split 0 stops the ramp, it does not pass.

    NOT_PROVIDED and "zero ready containers" are opposite facts: one is a missing
    reading, the other an outage.  The leg keeps them apart, and this is the half
    that matters for traffic -- a payload assembled without the section would
    otherwise move users onto a lane while the leg that detects "the lane is not
    running" was silently switched off.

    Below split 0 there is no lane to be short of, so the section is optional
    there and the collector omits it.
    """
    payload = _metrics_payload(gray_latency=1.0, stable_latency=1.0)
    del payload["readiness"]
    payload["evidence"]["payload_sha256"] = _resign(payload)
    result = _evaluate(payload)
    assert result["status"] == "ERROR"
    assert "READINESS_MISSING" in result["errors"]
    assert result["dispatcher_recommendation"]["action"] != "none"

    liveness_gone = _metrics_payload(gray_latency=1.0, stable_latency=1.0)
    del liveness_gone["data_sources"]
    liveness_gone["evidence"]["payload_sha256"] = _resign(liveness_gone)
    liveness_result = _evaluate(liveness_gone)
    assert liveness_result["status"] == "ERROR"
    assert "DATA_SOURCES_MISSING" in liveness_result["errors"]


def test_metrics_goes_red_when_one_data_source_stops_answering() -> None:
    """Leg 3: a monitor whose input died must not read as a healthy system.

    Every other leg here reports "clean" on an empty window, so a collector that
    stopped producing a source yields a payload indistinguishable from one watching
    a healthy lane.  That is the shape behind the 2026-09-14 run being green
    throughout while three of its five ramp steps had an unarmed stop-loss.

    Ages are relative to the NEWEST observation in the cycle, which makes this leg
    answer "is one input lagging while the others are alive".  The first version
    anchored on `evidence.captured_at` -- the MINIMUM of the source timestamps by
    construction -- so the dead source read as age 0 and everything else read as
    being in the future.  Whole-payload staleness is a different leg
    (MAX_EVIDENCE_AGE) on purpose, so a re-run over an archived cycle can still
    judge that cycle instead of calling every historical file dead.

    Like the readiness leg, this bypasses the sustain gate, and for a stronger
    reason: what it detects is the monitor having stopped, so the window that would
    confirm the breach may never arrive.
    """
    stale = _evaluate(
        _metrics_payload(
            gray_latency=1.0, stable_latency=1.0, source_ages={"hard_errors": 420}
        )
    )
    assert stale["data_liveness"]["status"] == "FAIL"
    assert stale["data_liveness"]["silent_sources"] == ["hard_errors"]
    assert stale["data_liveness"]["anchor"] == "newest_observation"
    assert stale["dispatcher_recommendation"]["action"] == "rollback"
    assert "DATA_SOURCE_SILENT" in stale["dispatcher_recommendation"]["reason_codes"]
    assert "DATA_SOURCE_SILENT" not in stale["sustain"]["counts"]

    # One interval of lag is tolerated; the floor is `>`, not `>=`, so a source
    # exactly one cadence behind is the newest cycle's own read rather than a fault.
    at_floor = _evaluate(
        _metrics_payload(
            gray_latency=1.0, stable_latency=1.0, source_ages={"hard_errors": 300}
        )
    )
    assert at_floor["data_liveness"]["status"] == "PASS"
    assert at_floor["dispatcher_recommendation"]["action"] == "none"

    # The anchor moving is what must NOT rescue a lagging source: shifting every
    # timestamp back together keeps the payload internally live, because this leg
    # measures spread between inputs and not distance from wall clock.
    spread = _evaluate(
        _metrics_payload(
            gray_latency=1.0,
            stable_latency=1.0,
            source_ages={"access_log": 60, "hard_errors": 480, "backend_health": 60},
        )
    )
    assert spread["data_liveness"]["silent_sources"] == ["hard_errors"]


def test_metrics_counts_user_facing_failures_including_proxy_four_xx() -> None:
    """Leg 4: the users'-eye view, at a much lower bar than the stop-loss.

    Counted from the access log's own status per request -- one row per thing a user
    actually saw.  NOT from ERROR log lines: those count a retry layer's upstream
    attempts, so one user-visible failure appears 1-N times depending on how many
    tries it took, and the number moves when retry policy changes with nothing
    wrong.

    429 counts here and nowhere else, which is the reason this leg exists next to
    the absolute 5xx one.  A 429 storm out of a bad build is completely invisible
    to a 5xx ruler and completely visible to the person hitting it.  What does NOT
    count is caller-side 4xx -- see the CLIENT_ERROR test below for why.
    """
    payload = _metrics_payload(gray_latency=1.0, stable_latency=1.0)
    # 12 of 300 = 4%, over the 2% line, and entirely 429: no 5xx leg can see this.
    for record in payload["records"][:12]:
        record["status"] = 429
    payload["evidence"]["payload_sha256"] = _resign(payload)
    result = _evaluate(payload)

    leg = result["user_facing_failures"]
    assert leg["ruler"] == "access_log_status"
    assert leg["proxy_four_xx_count"] == 12
    assert leg["client_four_xx_count"] == 0
    assert leg["five_xx_count"] == 0
    assert leg["failure_rate"] == 0.04
    assert leg["qualified"] is True
    # Sampled ratio, so it goes through the sustain gate rather than firing on one
    # window -- unlike the readiness and liveness legs above.  Depth is 3, not the
    # global 2: on real traffic the proxy-emitted breach runs reach 2 windows, so
    # depth 2 still fired 3.13 times/day on healthy traffic.
    assert result["sustain"]["required_windows_by_code"]["USER_FAILURE_RATE"] == 3
    assert result["sustain"]["counts"] == {"USER_FAILURE_RATE": 1}
    assert result["dispatcher_recommendation"]["action"] == "none"
    # And the 5xx legs stayed silent on the same rows, which is what makes this leg
    # additional rather than a second copy of the stop-loss.
    assert result["comparisons"][0]["breaches"] == []

    # Second breaching window: still pending, because depth is 3.
    pending = _metrics_payload(gray_latency=1.0, stable_latency=1.0)
    for record in pending["records"][:12]:
        record["status"] = 429
    pending["evidence"]["payload_sha256"] = _resign(pending)
    pending["sustain_state"] = {"USER_FAILURE_RATE": 1}
    second = _evaluate(pending)
    assert second["sustain"]["counts"] == {"USER_FAILURE_RATE": 2}
    assert second["dispatcher_recommendation"]["action"] != "rollback"

    confirmed = _metrics_payload(gray_latency=1.0, stable_latency=1.0)
    for record in confirmed["records"][:12]:
        record["status"] = 429
    confirmed["evidence"]["payload_sha256"] = _resign(confirmed)
    confirmed["sustain_state"] = {"USER_FAILURE_RATE": 2}
    third = _evaluate(confirmed)
    assert third["dispatcher_recommendation"]["action"] == "rollback"
    assert third["dispatcher_recommendation"]["reason_codes"] == ["USER_FAILURE_RATE"]

    # Two failures in 40 requests is 5%, over the line, and must not start a streak:
    # below MIN_USER_FAILURE_EVENTS a rate is a coin flip regardless of how far over
    # it lands.  The window still QUALIFIES -- 40 clears the sample floor -- so this
    # is the event minimum doing the work on its own, and no floor alert is raised
    # because nothing about the sample was too thin to read.
    thin = _metrics_payload(gray_latency=1.0, stable_latency=1.0, live_samples=40)
    thin["records"][0]["status"] = 429
    thin["records"][1]["status"] = 429
    thin["evidence"]["payload_sha256"] = _resign(thin)
    thin_result = _evaluate(thin)
    thin_leg = thin_result["user_facing_failures"]
    assert thin_leg["failure_count"] == 2
    assert thin_leg["qualified"] is True
    assert thin_leg["failure_rate"] == 0.05
    assert "USER_FAILURE_RATE" not in thin_result["sustain"]["counts"]
    assert "USER_FAILURE_SAMPLE_BELOW_FLOOR" not in thin_result["alerts"]
    assert thin_result["dispatcher_recommendation"]["action"] != "rollback"


def test_metrics_client_side_four_xx_alerts_but_never_moves_traffic() -> None:
    """Caller-side 4xx is reported and never triggers.  This is a REGRESSION test.

    Until 2026-09-20 this leg counted every status >= 400, and the negative control
    (replaying real stable traffic against itself, so every red is false by
    construction) promoted it to `rollback` on 3 of 6 consecutive windows.  The
    population was the defect, not the threshold: over 185 healthy five-minute
    windows / 66393 stable inference rows,

        client-side 4xx  499x508 400x326 405x296 403x107 401x70
          p50 0.0043  p90 0.0330  max 0.1222   above 0.02 in 39/185 (21.1%)
        proxy-emitted    503x371 500x124 429x92 413x4
          p50 0.0000  p90 0.0065  max 0.2283   above 0.02 in  7/185 ( 3.8%)

    499 is nginx's code for the client hanging up before a reply, 405 is a caller
    using a method the route does not serve, 400 is a malformed body.  A proxy
    cannot be rolled back for any of them, and a gate that tries is a gate nobody
    can leave armed.
    """
    # 60 of 300 = 20%, ten times the trigger line and twice the alert line, all of
    # it caller-side.  Under the old population this was an unconditional rollback
    # after two windows.
    payload = _metrics_payload(gray_latency=1.0, stable_latency=1.0)
    for index, record in enumerate(payload["records"][:60]):
        record["status"] = (499, 405, 400, 401, 403)[index % 5]
    payload["evidence"]["payload_sha256"] = _resign(payload)
    result = _evaluate(payload)

    leg = result["user_facing_failures"]
    assert leg["client_four_xx_count"] == 60
    assert leg["client_four_xx_rate"] == 0.2
    # The trigger's own numbers are untouched by all of it.
    assert leg["proxy_four_xx_count"] == 0
    assert leg["five_xx_count"] == 0
    assert leg["failure_count"] == 0
    assert leg["failure_rate"] == 0.0
    # Visible, and only visible: an alert, no streak, no action on traffic.
    assert "CLIENT_ERROR_RATE_HIGH" in result["alerts"]
    assert "USER_FAILURE_RATE" not in result["sustain"]["counts"]
    assert result["dispatcher_recommendation"]["hard_trigger"] is False
    assert result["dispatcher_recommendation"]["action"] == "alert_only"
    # Repeating it forever still never moves traffic -- the code cannot reach the
    # sustain gate at all, so there is no depth at which it converts.
    repeat = _metrics_payload(gray_latency=1.0, stable_latency=1.0)
    for index, record in enumerate(repeat["records"][:60]):
        record["status"] = (499, 405, 400, 401, 403)[index % 5]
    repeat["evidence"]["payload_sha256"] = _resign(repeat)
    repeat["sustain_state"] = {"USER_FAILURE_RATE": 2}
    second = _evaluate(repeat)
    assert second["dispatcher_recommendation"]["hard_trigger"] is False
    assert second["sustain"]["counts"].get("USER_FAILURE_RATE") in (None, 0)

    # Below the alert line it is not even an alert: 6 of 300 = 2% is real caller
    # noise on this proxy (p50 0.0043, p90 0.0330) and must stay quiet.
    quiet = _metrics_payload(gray_latency=1.0, stable_latency=1.0)
    for record in quiet["records"][:6]:
        record["status"] = 499
    quiet["evidence"]["payload_sha256"] = _resign(quiet)
    quiet_result = _evaluate(quiet)
    assert quiet_result["user_facing_failures"]["client_four_xx_count"] == 6
    assert "CLIENT_ERROR_RATE_HIGH" not in quiet_result["alerts"]
    assert quiet_result["dispatcher_recommendation"]["hard_trigger"] is False


def test_replay_harness_reads_the_same_log_fields_as_the_collector() -> None:
    """The negative control's extractor must not drift from the collector's.

    They are two separate regexes on purpose -- the collector is a CLI that reads a
    live log and refuses a stale window, which are exactly the two behaviours a
    replay must not have -- and that is precisely why this has to be pinned.  If
    nginx's log_format changes and only one of them is updated, the replay stops
    matching lines and reports 0 reds on 0 windows, which reads as the cleanest run
    the gate has ever had.  A negative control that cannot see is worse than none.

    Compared by PATTERN, not by group name: the names differ deliberately
    (`captured_at` vs `ts`, `upstream_times` vs `rt`) because each module names
    fields for its own use.  What must stay identical is the shape of the line each
    one expects, so the comparison strips the group names out.
    """
    harness = (SCRIPT_DIR / "replay-gate-negative-control.py").read_text()
    collector = (SCRIPT_DIR / "collect-metrics.py").read_text()

    def pattern_of(source: str, name: str) -> str:
        block = re.search(
            rf"^{name} = re\.compile\(\n(.*?)^\)", source, re.S | re.M
        )
        assert block, f"{name} not found in the expected form"
        # Keep only the r"..." fragments: comments between them are prose, and the
        # two modules explain themselves differently.
        fragments = re.findall(r'r"((?:[^"\\]|\\.)*)"', block.group(1))
        assert fragments, f"{name} has no pattern fragments"
        joined = "".join(fragments)
        # Drop the group NAMES, keep the grouping.
        return re.sub(r"\(\?P<[A-Za-z_][A-Za-z0-9_]*>", "(", joined)

    harness_pattern = pattern_of(harness, "LOG_LINE_RE")
    collector_pattern = pattern_of(collector, "LOG_LINE_RE")
    # The collector additionally matches (and deliberately does not capture) the
    # trailing `sid=` field.  That suffix is the only permitted difference: the
    # replay reads whole windows rather than enrolment, so it has no use for sid.
    assert collector_pattern.startswith(harness_pattern), (
        "the replay harness and the collector no longer expect the same log line\n"
        f"harness:   {harness_pattern!r}\n"
        f"collector: {collector_pattern!r}"
    )
    suffix = collector_pattern[len(harness_pattern) :]
    assert "sid" in suffix or suffix == "", f"unexpected extra fields: {suffix!r}"

    # And both must actually match a real line in the production log_format, so a
    # pair of regexes that drifted together into matching nothing still fails here.
    line = (
        "ts=2026-09-20T03:12:39+08:00 - 200 1.234 "
        'pool=canary rt=0.900 uri_class=chat sid=abc123'
    )
    for name, pattern in (
        ("harness", harness_pattern),
        ("collector", collector_pattern),
    ):
        assert re.compile(pattern).search(line), f"{name} regex matches no real line"
