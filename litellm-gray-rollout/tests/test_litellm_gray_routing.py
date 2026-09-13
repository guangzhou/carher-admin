import os
import hashlib
import json
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = ROOT / "litellm-gray-rollout" / "scripts"
RENDER_ENV_KEYS = (
    "GRAY_RENDER_CMD",
    "GRAY_RENDERER_FILE",
    "GRAY_RENDER_BASE_TEMPLATE",
    "GRAY_RENDER_DEBUG_TOKEN_FILE",
    "GRAY_RENDER_OUTPUT",
)
_PRODUCTION_RENDER_ENVS = {}


def run_script(name, *args, input_text=None, env=None):
    merged = os.environ.copy()
    supplied = dict(env or {})
    merged.update({"GRAY_ALLOW_NONROOT": "1", "GRAY_TEST_MODE": "1"})
    if "GRAY_ROOT" in supplied:
        merged["GRAY_ROOT"] = str(supplied.pop("GRAY_ROOT"))
    if env:
        merged.update({k: str(v) for k, v in supplied.items()})
    return subprocess.run(
        [str(SCRIPT_DIR / name), *args],
        input=input_text,
        text=True,
        capture_output=True,
        env=merged,
    )


def run_script_production(name, *args, input_text=None, env=None):
    """Run without the test-mode bypass used by the legacy routing tests."""
    merged = os.environ.copy()
    merged.pop("GRAY_TEST_MODE", None)
    merged.pop("GRAY_ALLOW_NONROOT", None)
    # The subprocess runs as the developer account; allow the isolated tmp
    # root while still exercising all non-test production contracts.
    merged["GRAY_ALLOW_NONROOT"] = "1"
    merged["GRAY_INITIAL_ROLLBACK_CMD"] = "true"
    merged["GRAY_ABORT_VERIFY_CMD"] = "true"
    supplied = {k: str(v) for k, v in (env or {}).items()}
    run_root = supplied.get("GRAY_ROOT")
    if run_root:
        key = str(Path(run_root).resolve())
        renderer_env = _PRODUCTION_RENDER_ENVS.get(key)
        if renderer_env is None:
            fixture_root = Path(run_root).parent / f".{Path(run_root).name}-production-fixture"
            renderer_env = {k: str(v) for k, v in _renderer_env(fixture_root).items()}
            _PRODUCTION_RENDER_ENVS[key] = renderer_env
        merged.update(renderer_env)
        if name == "gray-run-init.sh" and all(item in supplied for item in RENDER_ENV_KEYS):
            _PRODUCTION_RENDER_ENVS[key] = {item: supplied[item] for item in RENDER_ENV_KEYS}
    merged.update(supplied)
    return subprocess.run(
        [str(SCRIPT_DIR / name), *args],
        input=input_text,
        text=True,
        capture_output=True,
        env=merged,
    )


@pytest.fixture
def run_root(tmp_path):
    return tmp_path / "gray-run"


def init_run(run_root):
    result = run_script(
        "gray-run-init.sh",
        env={
            "GRAY_ROOT": run_root,
            "GRAY_NGINX_TEST_CMD": "true",
            "GRAY_RELOAD_CMD": "true",
            "GRAY_POST_RELOAD_CMD": "true",
            "GRAY_INITIAL_ROLLBACK_CMD": "true",
        },
    )
    assert result.returncode == 0, result.stderr
    return result


def init_production_run(run_root, tmp_path, *, env=None):
    plan = tmp_path / f"{run_root.name}-plan.md"
    summary = tmp_path / f"{run_root.name}-live.json"
    plan.write_text("approved plan\n")
    summary.write_text('{"live":"redacted"}\n')
    result = run_script_production(
        "gray-run-init.sh",
        "--execution-plan",
        str(plan),
        "--execution-plan-sha256",
        _sha(plan),
        "--live-summary",
        str(summary),
        "--expected-live-sha256",
        _sha(summary),
        env={
            "GRAY_ROOT": run_root,
            "GRAY_NGINX_TEST_CMD": "true",
            "GRAY_RELOAD_CMD": "true",
            "GRAY_POST_RELOAD_CMD": "true",
            "GRAY_INITIAL_ROLLBACK_CMD": "true",
            "GRAY_ABORT_VERIFY_CMD": "true",
            **(env or {}),
        },
    )
    assert result.returncode == 0, result.stderr
    return result


def set_split_100(run_root):
    result = run_script(
        "gray-split-update.sh",
        "100",
        env={
            "GRAY_ROOT": run_root,
            "GRAY_SAMPLE_OK": "1",
            "GRAY_MONITOR_CONTINUITY_OK": "1",
            "GRAY_CAPACITY_OK": "1",
            "GRAY_BRIDGE_READY": "1",
        },
    )
    assert result.returncode == 0, result.stderr
    return result


def set_production_split_100(run_root, env):
    evidence_dir = run_root / "evidence"
    for gate in (
        "split_sample",
        "split_monitor_continuity",
        "split_capacity",
        "split_bridge",
    ):
        _write_gate(evidence_dir / f"{gate}.json", run_root, gate)
    supplied = dict(env)
    supplied.pop("GRAY_GATE_EVIDENCE_FILE", None)
    result = run_script_production(
        "gray-split-update.sh",
        "100",
        env={**supplied, "GRAY_GATE_EVIDENCE_DIR": evidence_dir},
    )
    assert result.returncode == 0, result.stderr
    return result


def _state(run_root):
    values = {}
    for line in ((run_root / "active").resolve() / "state.env").read_text().splitlines():
        key, value = line.split("=", 1)
        values[key] = value
    return values


def _write_gate(path, run_root, gate, *, captured_at=None, **overrides):
    state = _state(run_root)
    payload = {
        "gate": gate,
        "status": "PASS",
        "run_id": state["run_id"],
        "generation": state["generation"],
        "config_checksum": state["config_checksum"],
        "captured_at": (captured_at or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z"),
    }
    payload.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    return path


def _write_metrics(path, run_root, action, *, hard_trigger, status):
    state = _state(run_root)
    canonical = {
        "tool": "metrics",
        "schema_version": 1,
        "status": status,
        "run_id": state["run_id"],
        "generation": state["generation"],
        "config_checksum": state["config_checksum"],
        "phase": state["phase"],
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "dispatcher_recommendation": {
            "action": action,
            "reason_codes": ["HARD_ERROR"],
            "hard_trigger": hard_trigger,
        },
    }
    if action == "abort_to_bridge":
        canonical["backend_health"] = {"gray": False, "bridge": True}
    rendered = json.dumps(canonical, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    canonical["payload_sha256"] = "sha256:" + hashlib.sha256(rendered.encode()).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    path.write_text(json.dumps(canonical))
    path.chmod(0o600)
    return path


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _renderer_env(tmp_path):
    support = tmp_path / "renderer-support"
    support.mkdir(mode=0o700, parents=True, exist_ok=True)
    base = support / "nginx.template.conf"
    base.write_text(
        "http { upstream litellm_product { server 127.0.0.1:30402; }\n"
        "# @@LITELLM_GRAY_HTTP_DIRECTIVES@@\nserver { location /pro/ {\n"
        "# @@LITELLM_GRAY_PRODUCT_PROXY_DIRECTIVES@@\n} } }\n"
    )
    base.chmod(0o600)
    token = support / "debug.token"
    token.write_text("debug-token-0001\n")
    token.chmod(0o600)
    renderer = support / "render-production-nginx.py"
    renderer.write_bytes((SCRIPT_DIR / "render-production-nginx.py").read_bytes())
    renderer.chmod(0o700)
    output = support / "nginx.conf"
    command = (
        f"python3 '{renderer}' --base-template '{base}' "
        f'--generation "$GRAY_GENERATION_DIR" --output \'{output}\' '
        f"--debug-token-file '{token}' --attestation \"$GRAY_RENDER_ATTESTATION_FILE\""
    )
    return {
        "GRAY_RENDER_CMD": command,
        "GRAY_RENDERER_FILE": renderer,
        "GRAY_RENDER_BASE_TEMPLATE": base,
        "GRAY_RENDER_DEBUG_TOKEN_FILE": token,
        "GRAY_RENDER_OUTPUT": output,
    }


def test_run_init_is_idempotency_guarded_and_creates_preflight(run_root):
    first = init_run(run_root)
    assert "run_id=" in first.stdout
    active = run_root / "active"
    assert active.is_symlink()
    state = active.resolve() / "state.env"
    assert "phase=preflight" in state.read_text()

    second = run_script("gray-run-init.sh", env={"GRAY_ROOT": run_root})
    assert second.returncode != 0
    assert "active run" in second.stderr.lower()


def test_run_init_checks_live_summary_checksum(run_root, tmp_path):
    summary = tmp_path / "live-summary.json"
    summary.write_text('{"nginx":"snapshot"}\n')
    rejected = run_script(
        "gray-run-init.sh",
        "--live-summary",
        str(summary),
        "--expected-live-sha256",
        "0" * 64,
        env={"GRAY_ROOT": run_root},
    )
    assert rejected.returncode != 0
    assert "drift" in rejected.stderr.lower()
    assert not (run_root / "active").exists()


def test_phase_rejects_illegal_edge_and_allows_normal_gray(run_root):
    init_run(run_root)
    illegal = run_script("gray-phase.sh", "set", "committed", env={"GRAY_ROOT": run_root})
    assert illegal.returncode != 0
    assert "transition" in illegal.stderr.lower()

    allowed = run_script("gray-phase.sh", "set", "normal_gray", env={"GRAY_ROOT": run_root})
    assert allowed.returncode == 0, allowed.stderr
    current = run_script("gray-phase.sh", "current", env={"GRAY_ROOT": run_root})
    assert current.returncode == 0
    assert "normal_gray" in current.stdout


def test_key_route_replaces_target_atomically_and_never_prints_key(run_root):
    init_run(run_root)
    assert run_script("gray-phase.sh", "set", "normal_gray", env={"GRAY_ROOT": run_root}).returncode == 0
    key = "sk-test-key_1234"
    first = run_script("gray-key-route.sh", "force-gray", input_text=key + "\n", env={"GRAY_ROOT": run_root})
    assert first.returncode == 0, first.stderr
    assert key not in (first.stdout + first.stderr)
    second = run_script("gray-key-route.sh", "force-prod", input_text=key + "\n", env={"GRAY_ROOT": run_root})
    assert second.returncode == 0, second.stderr
    active = (run_root / "active").resolve()
    assert '"' + key + '" 1;' not in (active / "force-gray.map").read_text()
    assert '"' + key + '" 1;' in (active / "force-prod.map").read_text()
    verify = run_script("gray-key-route.sh", "verify", env={"GRAY_ROOT": run_root})
    assert verify.returncode == 0
    assert "intersection" not in verify.stdout.lower()


def test_key_route_is_idempotent_without_new_generation(run_root):
    init_run(run_root)
    assert run_script("gray-phase.sh", "set", "normal_gray", env={"GRAY_ROOT": run_root}).returncode == 0
    key = "sk-idempotent_1234"
    assert run_script("gray-key-route.sh", "force-gray", input_text=key + "\n", env={"GRAY_ROOT": run_root}).returncode == 0
    old_target = (run_root / "active").resolve()
    repeated = run_script("gray-key-route.sh", "force-gray", input_text=key + "\n", env={"GRAY_ROOT": run_root})
    assert repeated.returncode == 0
    assert "unchanged" in repeated.stdout
    assert (run_root / "active").resolve() == old_target


def test_key_route_rolls_back_active_generation_when_reload_fails(run_root, tmp_path):
    init_run(run_root)
    assert run_script("gray-phase.sh", "set", "normal_gray", env={"GRAY_ROOT": run_root}).returncode == 0
    old_target = (run_root / "active").resolve()
    fail_reload = tmp_path / "reload-fails.sh"
    fail_reload.write_text("#!/bin/sh\nexit 1\n")
    fail_reload.chmod(0o700)
    result = run_script(
        "gray-key-route.sh",
        "force-gray",
        input_text="sk-reload-failure\n",
        env={"GRAY_ROOT": run_root, "GRAY_RELOAD_CMD": fail_reload},
    )
    assert result.returncode != 0
    assert (run_root / "active").resolve() == old_target


def test_key_route_reports_fatal_when_rollback_reload_also_fails(run_root, tmp_path):
    init_run(run_root)
    assert run_script("gray-phase.sh", "set", "normal_gray", env={"GRAY_ROOT": run_root}).returncode == 0
    reload_count = tmp_path / "reload-count"
    reload_script = tmp_path / "reload.sh"
    reload_script.write_text(
        "#!/bin/sh\n"
        f"n=$(cat '{reload_count}' 2>/dev/null || echo 0)\n"
        "n=$((n + 1))\n"
        f"echo $n > '{reload_count}'\n"
        "exit 1\n"
    )
    reload_script.chmod(0o700)
    result = run_script(
        "gray-key-route.sh",
        "force-gray",
        input_text="sk-double-reload-fail_1234\n",
        env={"GRAY_ROOT": run_root, "GRAY_RELOAD_CMD": reload_script},
    )
    assert result.returncode != 0
    assert "rollback reload failed" in result.stderr.lower()


def test_key_route_rolls_back_when_post_switch_nginx_test_fails(run_root, tmp_path):
    init_run(run_root)
    assert run_script("gray-phase.sh", "set", "normal_gray", env={"GRAY_ROOT": run_root}).returncode == 0
    old_target = (run_root / "active").resolve()
    counter = tmp_path / "nginx-test-count"
    check = tmp_path / "nginx-test.sh"
    check.write_text(
        "#!/bin/sh\n"
        f"n=$(cat '{counter}' 2>/dev/null || echo 0)\n"
        "n=$((n + 1))\n"
        f"echo $n > '{counter}'\n"
        "test $n -lt 2\n"
    )
    check.chmod(0o700)
    result = run_script(
        "gray-key-route.sh",
        "force-gray",
        input_text="sk-second-check_1234\n",
        env={"GRAY_ROOT": run_root, "GRAY_NGINX_TEST_CMD": check},
    )
    assert result.returncode != 0
    assert (run_root / "active").resolve() == old_target


def test_verify_fails_closed_on_phase_tamper_and_list_intersection(run_root):
    init_run(run_root)
    active = (run_root / "active").resolve()
    state = active / "state.env"
    state.write_text(state.read_text().replace("phase=preflight", "phase=normal_gray"))
    tampered = run_script("gray-phase.sh", "verify", env={"GRAY_ROOT": run_root})
    assert tampered.returncode != 0
    assert "checksum" in tampered.stderr.lower()

    other = run_root.parent / "intersection-run"
    init_run(other)
    current = (other / "active").resolve()
    entry = '"sk-intersection_1234" 1;\n'
    (current / "force-prod.map").write_text(entry)
    (current / "force-gray.map").write_text(entry)
    overlap = run_script("gray-key-route.sh", "verify", env={"GRAY_ROOT": other})
    assert overlap.returncode != 0
    assert any(word in overlap.stderr.lower() for word in ("intersection", "checksum"))


def test_key_route_rejects_injection_without_leaking_value(run_root):
    init_run(run_root)
    assert run_script("gray-phase.sh", "set", "normal_gray", env={"GRAY_ROOT": run_root}).returncode == 0
    malicious = 'sk-validprefix";include-/tmp/evil'
    result = run_script("gray-key-route.sh", "force-gray", input_text=malicious + "\n", env={"GRAY_ROOT": run_root})
    assert result.returncode != 0
    assert malicious not in result.stdout + result.stderr


def test_bridge_lifecycle_requires_gates_and_returns_to_preflight(run_root):
    init_run(run_root)
    denied = run_script("gray-bridge-route.sh", "activate", env={"GRAY_ROOT": run_root})
    assert denied.returncode != 0
    assert run_script("gray-bridge-route.sh", "activate", env={"GRAY_ROOT": run_root, "GRAY_BRIDGE_READY": "1"}).returncode == 0
    active = (run_root / "active").resolve()
    assert "phase=bridge_preparing" in (active / "state.env").read_text()
    assert (active / "bridge-override.map").read_text() == "default off;\n"
    assert run_script("gray-bridge-route.sh", "verified", env={"GRAY_ROOT": run_root, "GRAY_BRIDGE_READY": "1"}).returncode == 0
    assert ((run_root / "active").resolve() / "bridge-override.map").read_text() == "default guarded-old;\n"
    drained_only = run_script(
        "gray-bridge-route.sh",
        "deactivate",
        env={"GRAY_ROOT": run_root, "GRAY_PROD_DRAINED": "1"},
    )
    assert drained_only.returncode != 0
    assert run_script(
        "gray-bridge-route.sh",
        "deactivate",
        env={"GRAY_ROOT": run_root, "GRAY_PROD_GUARDED_VERIFIED": "1"},
    ).returncode == 0
    state = ((run_root / "active").resolve() / "state.env").read_text()
    assert "phase=preflight" in state and "bridge=off" in state


def test_split_requires_sample_and_capacity_gates(run_root):
    init_run(run_root)
    assert run_script("gray-phase.sh", "set", "normal_gray", env={"GRAY_ROOT": run_root}).returncode == 0
    denied = run_script("gray-split-update.sh", "1", env={"GRAY_ROOT": run_root})
    assert denied.returncode != 0
    assert "sample" in denied.stderr.lower()
    allowed = run_script(
        "gray-split-update.sh",
        "1",
        env={"GRAY_ROOT": run_root, "GRAY_SAMPLE_OK": "1", "GRAY_MONITOR_CONTINUITY_OK": "1"},
    )
    assert allowed.returncode == 0, allowed.stderr
    assert "split=1" in ((run_root / "active").resolve() / "state.env").read_text()
    assert ((run_root / "active").resolve() / "split.conf").read_text() == "1% litellm_gray;\n* litellm_product;\n"


def test_split_fifty_requires_capacity_and_bridge(run_root):
    init_run(run_root)
    assert run_script("gray-phase.sh", "set", "normal_gray", env={"GRAY_ROOT": run_root}).returncode == 0
    denied = run_script(
        "gray-split-update.sh",
        "50",
        env={"GRAY_ROOT": run_root, "GRAY_SAMPLE_OK": "1", "GRAY_MONITOR_CONTINUITY_OK": "1"},
    )
    assert denied.returncode != 0
    assert "capacity" in denied.stderr.lower()
    allowed = run_script(
        "gray-split-update.sh",
        "50",
        env={
            "GRAY_ROOT": run_root,
            "GRAY_SAMPLE_OK": "1",
            "GRAY_MONITOR_CONTINUITY_OK": "1",
            "GRAY_CAPACITY_OK": "1",
            "GRAY_BRIDGE_READY": "1",
        },
    )
    assert allowed.returncode == 0, allowed.stderr


def test_split_one_hundred_uses_wildcard_gray_without_hash_leak(run_root):
    init_run(run_root)
    assert run_script("gray-phase.sh", "set", "normal_gray", env={"GRAY_ROOT": run_root}).returncode == 0
    result = run_script(
        "gray-split-update.sh",
        "100",
        env={
            "GRAY_ROOT": run_root,
            "GRAY_SAMPLE_OK": "1",
            "GRAY_MONITOR_CONTINUITY_OK": "1",
            "GRAY_CAPACITY_OK": "1",
            "GRAY_BRIDGE_READY": "1",
        },
    )
    assert result.returncode == 0, result.stderr
    assert ((run_root / "active").resolve() / "split.conf").read_text() == "* litellm_gray;\n"


def test_convergence_prepare_rejects_split_below_one_hundred(run_root):
    init_run(run_root)
    assert run_script("gray-phase.sh", "set", "normal_gray", env={"GRAY_ROOT": run_root}).returncode == 0
    old_target = (run_root / "active").resolve()
    generations_before = {path.name for path in (run_root / "generations").iterdir()}
    test_marker = run_root.parent / "convergence-nginx-test"
    reload_marker = run_root.parent / "convergence-reload"
    post_marker = run_root.parent / "convergence-post-reload"
    result = run_script(
        "gray-convergence-prepare.sh",
        env={
            "GRAY_ROOT": run_root,
            "GRAY_GRAY_STABLE": "1",
            "GRAY_CONTROL_PLANE_OK": "1",
            "GRAY_BRIDGE_READY": "1",
            "GRAY_BYPASS_DISPOSITION_OK": "1",
            "GRAY_NGINX_TEST_CMD": f"touch '{test_marker}'",
            "GRAY_RELOAD_CMD": f"touch '{reload_marker}'",
            "GRAY_POST_RELOAD_CMD": f"touch '{post_marker}'",
        },
    )

    assert result.returncode != 0
    assert "split=100" in result.stderr.lower() or "100%" in result.stderr.lower()
    assert (run_root / "active").resolve() == old_target
    assert {path.name for path in (run_root / "generations").iterdir()} == generations_before
    assert not test_marker.exists()
    assert not reload_marker.exists()
    assert not post_marker.exists()


def test_convergence_prepare_commit_and_abort_are_phase_aware(run_root):
    init_run(run_root)
    assert run_script("gray-phase.sh", "set", "normal_gray", env={"GRAY_ROOT": run_root}).returncode == 0
    set_split_100(run_root)
    gates = {
        "GRAY_ROOT": run_root,
        "GRAY_GRAY_STABLE": "1",
        "GRAY_CONTROL_PLANE_OK": "1",
        "GRAY_BRIDGE_READY": "1",
        "GRAY_BYPASS_DISPOSITION_OK": "1",
    }
    prepared = run_script("gray-convergence-prepare.sh", env=gates)
    assert prepared.returncode == 0, prepared.stderr
    state = ((run_root / "active").resolve() / "state.env").read_text()
    assert "phase=convergence_ready" in state and "mode=1" in state

    assert run_script(
        "gray-phase.sh",
        "set",
        "prod_offline_upgrading",
        env={"GRAY_ROOT": run_root, "GRAY_PROD_ZERO": "1"},
    ).returncode == 0
    aborted = run_script("gray-convergence-abort.sh", env=gates)
    assert aborted.returncode == 0, aborted.stderr
    assert "phase=aborted" in ((run_root / "active").resolve() / "state.env").read_text()


def test_convergence_commit_preserves_protected_key_sid(run_root):
    init_run(run_root)
    assert run_script("gray-phase.sh", "set", "normal_gray", env={"GRAY_ROOT": run_root}).returncode == 0
    key = "sk-protected_1234"
    assert run_script("gray-key-route.sh", "protect-prod", input_text=key + "\n", env={"GRAY_ROOT": run_root}).returncode == 0
    set_split_100(run_root)
    gates = {
        "GRAY_ROOT": run_root,
        "GRAY_GRAY_STABLE": "1",
        "GRAY_CONTROL_PLANE_OK": "1",
        "GRAY_BRIDGE_READY": "1",
        "GRAY_BYPASS_DISPOSITION_OK": "1",
    }
    assert run_script("gray-convergence-prepare.sh", env=gates).returncode == 0
    assert run_script(
        "gray-phase.sh",
        "set",
        "prod_offline_upgrading",
        env={"GRAY_ROOT": run_root, "GRAY_PROD_ZERO": "1"},
    ).returncode == 0
    assert run_script("gray-phase.sh", "set", "prod_verified", env={"GRAY_ROOT": run_root}).returncode == 0
    committed = run_script("gray-convergence-commit.sh", env={"GRAY_ROOT": run_root, "GRAY_PROD_HEALTHY": "1"})
    assert committed.returncode == 0, committed.stderr
    active = (run_root / "active").resolve()
    assert key in (active / "protected-prod.map").read_text()
    assert key in (active / "key-sid.map").read_text()


def test_dispatcher_holds_gray_during_offline_upgrade_and_rolls_back_normal(run_root):
    init_run(run_root)
    assert run_script("gray-phase.sh", "set", "normal_gray", env={"GRAY_ROOT": run_root}).returncode == 0
    result = run_script(
        "gray-auto-dispatch.sh",
        input_text='{"dispatcher_recommendation":{"action":"rollback","reason_codes":["hard-error"],"hard_trigger":true}}\n',
        env={"GRAY_ROOT": run_root, "GRAY_PROD_HEALTHY": "1"},
    )
    assert result.returncode == 0, result.stderr
    assert "phase=rolled_back" in ((run_root / "active").resolve() / "state.env").read_text()

    # A fresh run is required for the offline branch.
    other = run_root.parent / "offline-run"
    init_run(other)
    assert run_script("gray-phase.sh", "set", "normal_gray", env={"GRAY_ROOT": other}).returncode == 0
    set_split_100(other)
    gates = {
        "GRAY_ROOT": other,
        "GRAY_GRAY_STABLE": "1",
        "GRAY_CONTROL_PLANE_OK": "1",
        "GRAY_BRIDGE_READY": "1",
        "GRAY_BYPASS_DISPOSITION_OK": "1",
    }
    assert run_script("gray-convergence-prepare.sh", env=gates).returncode == 0
    assert run_script(
        "gray-phase.sh",
        "set",
        "prod_offline_upgrading",
        env={"GRAY_ROOT": other, "GRAY_PROD_ZERO": "1"},
    ).returncode == 0
    held = run_script(
        "gray-auto-dispatch.sh",
        input_text='{"dispatcher_recommendation":{"action":"hold_gray","reason_codes":["prod-offline"],"hard_trigger":false}}\n',
        env={"GRAY_ROOT": other, "GRAY_GRAY_HEALTHY": "1"},
    )
    assert held.returncode == 0
    assert "phase=prod_offline_upgrading" in ((other / "active").resolve() / "state.env").read_text()


def test_dispatcher_invalid_input_never_mutates(run_root):
    init_run(run_root)
    assert run_script("gray-phase.sh", "set", "normal_gray", env={"GRAY_ROOT": run_root}).returncode == 0
    old_target = (run_root / "active").resolve()
    invalid = run_script(
        "gray-auto-dispatch.sh",
        input_text='{"dispatcher_recommendation":{"action":"surprise"}}\n',
        env={"GRAY_ROOT": run_root},
    )
    assert invalid.returncode == 2
    assert (run_root / "active").resolve() == old_target


def test_dispatcher_accepts_metrics_hold_contract_without_mutation(run_root):
    init_run(run_root)
    assert run_script("gray-phase.sh", "set", "normal_gray", env={"GRAY_ROOT": run_root}).returncode == 0
    set_split_100(run_root)
    gates = {
        "GRAY_ROOT": run_root,
        "GRAY_GRAY_STABLE": "1",
        "GRAY_CONTROL_PLANE_OK": "1",
        "GRAY_BRIDGE_READY": "1",
        "GRAY_BYPASS_DISPOSITION_OK": "1",
    }
    assert run_script("gray-convergence-prepare.sh", env=gates).returncode == 0
    assert run_script(
        "gray-phase.sh",
        "set",
        "prod_offline_upgrading",
        env={"GRAY_ROOT": run_root, "GRAY_PROD_ZERO": "1"},
    ).returncode == 0
    old_target = (run_root / "active").resolve()
    metrics_json = (
        '{"tool":"litellm-gray-metrics","status":"PASS","phase":"prod_offline_upgrading",'
        '"dispatcher_recommendation":{"action":"hold_gray",'
        '"reason_codes":["PROD_OFFLINE_GRAY_HEALTHY"],"hard_trigger":false}}\n'
    )
    result = run_script(
        "gray-auto-dispatch.sh",
        input_text=metrics_json,
        env={"GRAY_ROOT": run_root, "GRAY_GRAY_HEALTHY": "1"},
    )
    assert result.returncode == 0, result.stderr
    assert "mutation=none" in result.stdout
    assert (run_root / "active").resolve() == old_target


def test_production_requires_real_nginx_hooks(run_root):
    """A missing reload/test hook must never become a production no-op."""
    init = run_script_production("gray-run-init.sh", env={"GRAY_ROOT": run_root})
    assert init.returncode != 0
    assert "test mode" in init.stderr.lower() or "nginx" in init.stderr.lower()


def test_generic_phase_cannot_bypass_convergence_prepare(run_root):
    init_run(run_root)
    result = run_script(
        "gray-phase.sh",
        "set",
        "convergence_ready",
        env={"GRAY_ROOT": run_root},
    )
    assert result.returncode != 0
    assert "dedicated" in result.stderr.lower() or "transition" in result.stderr.lower()


def test_run_init_requires_frozen_inputs_outside_test_mode(tmp_path):
    result = run_script_production("gray-run-init.sh", env={"GRAY_ROOT": tmp_path / "run"})
    assert result.returncode != 0
    assert "execution plan" in result.stderr.lower() or "live summary" in result.stderr.lower()


def test_convergence_prepare_requires_structured_gate_evidence(run_root):
    init_production_run(run_root, run_root.parent)
    gray_entry = _write_gate(run_root / "evidence" / "gray_entry.json", run_root, "gray_entry")
    common = {
        "GRAY_ROOT": run_root,
        "GRAY_NGINX_TEST_CMD": "true",
        "GRAY_RELOAD_CMD": "true",
        "GRAY_POST_RELOAD_CMD": "true",
    }
    assert run_script_production(
        "gray-phase.sh",
        "set",
        "normal_gray",
        env={
            **common,
            "GRAY_GATE_EVIDENCE_FILE": gray_entry,
        },
    ).returncode == 0
    set_production_split_100(run_root, common)
    result = run_script_production(
        "gray-convergence-prepare.sh",
        env=common,
    )
    assert result.returncode != 0
    assert "evidence" in result.stderr.lower()


def test_production_convergence_prepare_accepts_pre_mode_bypass_disposition(run_root, tmp_path):
    init_production_run(run_root, tmp_path)
    common = {
        "GRAY_ROOT": run_root,
        "GRAY_NGINX_TEST_CMD": "true",
        "GRAY_RELOAD_CMD": "true",
        "GRAY_POST_RELOAD_CMD": "true",
    }
    gray_entry = _write_gate(run_root / "evidence" / "gray_entry.json", run_root, "gray_entry")
    assert run_script_production(
        "gray-phase.sh",
        "set",
        "normal_gray",
        env={**common, "GRAY_GATE_EVIDENCE_FILE": gray_entry},
    ).returncode == 0
    set_production_split_100(run_root, common)
    for gate in (
        "convergence_stable",
        "convergence_control_plane",
        "convergence_bridge",
        "convergence_bypass_disposition",
    ):
        _write_gate(run_root / "evidence" / f"{gate}.json", run_root, gate)

    result = run_script_production(
        "gray-convergence-prepare.sh",
        env={**common, "GRAY_GATE_EVIDENCE_DIR": run_root / "evidence"},
    )

    assert result.returncode == 0, result.stderr
    state = _state(run_root)
    assert state["phase"] == "convergence_ready"
    assert state["mode"] == "1"


def test_prod_offline_transition_requires_post_mode_prod_zero_gate(run_root):
    init_run(run_root)
    assert run_script("gray-phase.sh", "set", "normal_gray", env={"GRAY_ROOT": run_root}).returncode == 0
    set_split_100(run_root)
    prepare = run_script(
        "gray-convergence-prepare.sh",
        env={
            "GRAY_ROOT": run_root,
            "GRAY_GRAY_STABLE": "1",
            "GRAY_CONTROL_PLANE_OK": "1",
            "GRAY_BRIDGE_READY": "1",
            "GRAY_BYPASS_DISPOSITION_OK": "1",
        },
    )
    assert prepare.returncode == 0, prepare.stderr
    prepared_target = (run_root / "active").resolve()

    denied = run_script(
        "gray-phase.sh",
        "set",
        "prod_offline_upgrading",
        env={"GRAY_ROOT": run_root, "GRAY_PROD_DRAINED": "1"},
    )
    assert denied.returncode != 0
    assert "prod_zero" in denied.stderr.lower() or "prod zero" in denied.stderr.lower()
    assert (run_root / "active").resolve() == prepared_target

    allowed = run_script(
        "gray-phase.sh",
        "set",
        "prod_offline_upgrading",
        env={"GRAY_ROOT": run_root, "GRAY_PROD_ZERO": "1"},
    )
    assert allowed.returncode == 0, allowed.stderr
    assert _state(run_root)["phase"] == "prod_offline_upgrading"


def test_production_prod_zero_evidence_binds_convergence_ready_generation(run_root, tmp_path):
    init_production_run(run_root, tmp_path)
    common = {
        "GRAY_ROOT": run_root,
        "GRAY_NGINX_TEST_CMD": "true",
        "GRAY_RELOAD_CMD": "true",
        "GRAY_POST_RELOAD_CMD": "true",
    }
    gray_entry = _write_gate(run_root / "evidence" / "gray_entry.json", run_root, "gray_entry")
    assert run_script_production(
        "gray-phase.sh",
        "set",
        "normal_gray",
        env={**common, "GRAY_GATE_EVIDENCE_FILE": gray_entry},
    ).returncode == 0
    set_production_split_100(run_root, common)
    stale_prod_zero = _write_gate(run_root / "evidence" / "stale-prod-zero.json", run_root, "prod_zero")
    for gate in (
        "convergence_stable",
        "convergence_control_plane",
        "convergence_bridge",
        "convergence_bypass_disposition",
    ):
        _write_gate(run_root / "evidence" / f"{gate}.json", run_root, gate)
    assert run_script_production(
        "gray-convergence-prepare.sh",
        env={**common, "GRAY_GATE_EVIDENCE_DIR": run_root / "evidence"},
    ).returncode == 0
    prepared_target = (run_root / "active").resolve()

    stale = run_script_production(
        "gray-phase.sh",
        "set",
        "prod_offline_upgrading",
        env={**common, "GRAY_GATE_EVIDENCE_FILE": stale_prod_zero},
    )
    assert stale.returncode != 0
    assert "mismatch" in stale.stderr.lower()
    assert (run_root / "active").resolve() == prepared_target

    prod_zero = _write_gate(run_root / "evidence" / "prod-zero.json", run_root, "prod_zero")
    allowed = run_script_production(
        "gray-phase.sh",
        "set",
        "prod_offline_upgrading",
        env={**common, "GRAY_GATE_EVIDENCE_FILE": prod_zero},
    )
    assert allowed.returncode == 0, allowed.stderr


def test_dispatcher_rejects_stale_state_evidence_in_production(run_root, tmp_path):
    init_production_run(run_root, tmp_path)
    gray_entry = _write_gate(run_root / "evidence" / "gray_entry.json", run_root, "gray_entry")
    assert run_script_production(
        "gray-phase.sh", "set", "normal_gray",
        env={
            "GRAY_ROOT": run_root,
            "GRAY_GATE_EVIDENCE_FILE": gray_entry,
            "GRAY_NGINX_TEST_CMD": "true",
            "GRAY_RELOAD_CMD": "true",
            "GRAY_POST_RELOAD_CMD": "true",
        },
    ).returncode == 0
    # The production dispatcher must not consume an unbound/legacy recommendation.
    payload = '{"dispatcher_recommendation":{"action":"alert_only","reason_codes":["x"],"hard_trigger":false}}\n'
    result = run_script_production(
        "gray-auto-dispatch.sh",
        input_text=payload,
        env={"GRAY_ROOT": run_root},
    )
    assert result.returncode != 0
    assert "evidence" in result.stderr.lower() or "stale" in result.stderr.lower()


def test_run_init_snapshots_required_frozen_inputs_and_rejects_drift(tmp_path):
    plan = tmp_path / "plan.md"
    summary = tmp_path / "live.json"
    plan.write_text("approved plan\n")
    summary.write_text('{"live":"redacted"}\n')
    commands = {
        "GRAY_NGINX_TEST_CMD": ": nginx-test",
        "GRAY_RELOAD_CMD": ": reload",
        "GRAY_POST_RELOAD_CMD": ": post-reload",
        "GRAY_INITIAL_ROLLBACK_CMD": ": initial-rollback",
        "GRAY_ABORT_VERIFY_CMD": ": abort-verify",
    }
    hooks = {
        "GRAY_ALLOW_NONROOT": "1",
        "GRAY_ROOT": tmp_path / "ok-run",
        **commands,
    }
    created = run_script_production(
        "gray-run-init.sh",
        "--execution-plan",
        str(plan),
        "--execution-plan-sha256",
        _sha(plan),
        "--live-summary",
        str(summary),
        "--expected-live-sha256",
        _sha(summary),
        env=hooks,
    )
    assert created.returncode == 0, created.stderr
    active = (tmp_path / "ok-run" / "active").resolve()
    assert (active / "execution-plan.snapshot").read_text() == plan.read_text()
    assert (active / "live-summary.snapshot").read_text() == summary.read_text()
    manifest = (active / "input-checksums.env").read_text()
    assert f"nginx_test_command={hashlib.sha256(commands['GRAY_NGINX_TEST_CMD'].encode()).hexdigest()}" in manifest
    assert f"reload_command={hashlib.sha256(commands['GRAY_RELOAD_CMD'].encode()).hexdigest()}" in manifest
    assert f"post_reload_command={hashlib.sha256(commands['GRAY_POST_RELOAD_CMD'].encode()).hexdigest()}" in manifest
    assert f"initial_rollback_command={hashlib.sha256(commands['GRAY_INITIAL_ROLLBACK_CMD'].encode()).hexdigest()}" in manifest
    assert f"abort_verify_command={hashlib.sha256(commands['GRAY_ABORT_VERIFY_CMD'].encode()).hexdigest()}" in manifest

    drifted = run_script_production(
        "gray-run-init.sh",
        "--execution-plan",
        str(plan),
        "--execution-plan-sha256",
        "0" * 64,
        "--live-summary",
        str(summary),
        "--expected-live-sha256",
        _sha(summary),
        env={**hooks, "GRAY_ROOT": tmp_path / "bad-run"},
    )
    assert drifted.returncode != 0
    assert "execution plan checksum drift" in drifted.stderr.lower()


@pytest.mark.parametrize(
    ("hook", "changed_value", "marker_name"),
    (
        ("GRAY_NGINX_TEST_CMD", "touch '{marker}'; true", "nginx-test-ran"),
        ("GRAY_RELOAD_CMD", "touch '{marker}'; true", "reload-ran"),
        ("GRAY_POST_RELOAD_CMD", "touch '{marker}'; true", "post-reload-ran"),
    ),
)
def test_production_transaction_rejects_hook_drift_before_render(
    run_root, tmp_path, hook, changed_value, marker_name
):
    renderer_env = _renderer_env(tmp_path)
    render_marker = tmp_path / f"{marker_name}-render-count"
    renderer_env["GRAY_RENDER_CMD"] = (
        f"{renderer_env['GRAY_RENDER_CMD']}; printf x >> '{render_marker}'"
    )
    init_production_run(run_root, tmp_path, env=renderer_env)
    assert render_marker.read_text() == "x"
    old_target = (run_root / "active").resolve()
    marker = tmp_path / marker_name
    gate = _write_gate(run_root / "evidence" / "gray_entry.json", run_root, "gray_entry")
    result = run_script_production(
        "gray-phase.sh",
        "set",
        "normal_gray",
        env={
            **renderer_env,
            "GRAY_ROOT": run_root,
            "GRAY_NGINX_TEST_CMD": "true",
            "GRAY_RELOAD_CMD": "true",
            "GRAY_POST_RELOAD_CMD": "true",
            "GRAY_GATE_EVIDENCE_FILE": gate,
            hook: changed_value.format(marker=marker),
        },
    )

    assert result.returncode != 0
    assert "frozen" in result.stderr.lower() and "command" in result.stderr.lower()
    assert (run_root / "active").resolve() == old_target
    assert render_marker.read_text() == "x"
    assert not marker.exists()


def test_test_mode_transactions_may_vary_routing_hooks(run_root, tmp_path):
    init_run(run_root)
    marker = tmp_path / "test-mode-reload"
    result = run_script(
        "gray-phase.sh",
        "set",
        "normal_gray",
        env={"GRAY_ROOT": run_root, "GRAY_RELOAD_CMD": f"touch '{marker}'"},
    )

    assert result.returncode == 0, result.stderr
    assert marker.exists()


def test_production_transaction_rejects_gate_max_age_drift(run_root, tmp_path):
    renderer_env = _renderer_env(tmp_path)
    init_production_run(run_root, tmp_path, env=renderer_env)
    old_target = (run_root / "active").resolve()
    gate = _write_gate(run_root / "evidence" / "gray_entry.json", run_root, "gray_entry")
    result = run_script_production(
        "gray-phase.sh",
        "set",
        "normal_gray",
        env={
            **renderer_env,
            "GRAY_ROOT": run_root,
            "GRAY_NGINX_TEST_CMD": "true",
            "GRAY_RELOAD_CMD": "true",
            "GRAY_POST_RELOAD_CMD": "true",
            "GRAY_GATE_MAX_AGE_SECONDS": "60",
            "GRAY_GATE_EVIDENCE_FILE": gate,
        },
    )

    assert result.returncode != 0
    assert "gate max age" in result.stderr.lower()
    assert (run_root / "active").resolve() == old_target


def test_production_abort_rejects_verify_command_drift_before_bridge_switch(run_root, tmp_path):
    init_production_run(run_root, tmp_path)
    common = {
        "GRAY_ROOT": run_root,
        "GRAY_NGINX_TEST_CMD": "true",
        "GRAY_RELOAD_CMD": "true",
        "GRAY_POST_RELOAD_CMD": "true",
        "GRAY_ABORT_VERIFY_CMD": "true",
    }
    gray_entry = _write_gate(run_root / "evidence" / "gray_entry.json", run_root, "gray_entry")
    assert run_script_production(
        "gray-phase.sh", "set", "normal_gray",
        env={**common, "GRAY_GATE_EVIDENCE_FILE": gray_entry},
    ).returncode == 0
    set_production_split_100(run_root, common)
    for gate in (
        "convergence_stable",
        "convergence_control_plane",
        "convergence_bridge",
        "convergence_bypass_disposition",
    ):
        _write_gate(run_root / "evidence" / f"{gate}.json", run_root, gate)
    assert run_script_production(
        "gray-convergence-prepare.sh",
        env={**common, "GRAY_GATE_EVIDENCE_DIR": run_root / "evidence"},
    ).returncode == 0
    prod_zero = _write_gate(run_root / "evidence" / "prod-zero.json", run_root, "prod_zero")
    assert run_script_production(
        "gray-phase.sh", "set", "prod_offline_upgrading",
        env={**common, "GRAY_GATE_EVIDENCE_FILE": prod_zero},
    ).returncode == 0
    abort_gate = _write_gate(run_root / "evidence" / "convergence-abort.json", run_root, "convergence_abort")
    old_target = (run_root / "active").resolve()
    marker = tmp_path / "changed-abort-verify-ran"

    result = run_script_production(
        "gray-convergence-abort.sh",
        env={
            **common,
            "GRAY_GATE_EVIDENCE_FILE": abort_gate,
            "GRAY_ABORT_VERIFY_CMD": f"touch '{marker}'",
        },
    )

    assert result.returncode != 0
    assert "abort verify" in result.stderr.lower()
    assert (run_root / "active").resolve() == old_target
    assert _state(run_root)["phase"] == "prod_offline_upgrading"
    assert not marker.exists()


def test_frozen_input_snapshots_and_manifest_are_bound_to_generation(tmp_path):
    plan = tmp_path / "plan.md"
    summary = tmp_path / "live.json"
    plan.write_text("approved plan\n")
    summary.write_text('{"live":"redacted"}\n')
    root = tmp_path / "run"
    hooks = {
        "GRAY_ROOT": root,
        "GRAY_NGINX_TEST_CMD": "true",
        "GRAY_RELOAD_CMD": "true",
        "GRAY_POST_RELOAD_CMD": "true",
        "GRAY_INITIAL_ROLLBACK_CMD": "true",
    }
    created = run_script_production(
        "gray-run-init.sh",
        "--execution-plan",
        str(plan),
        "--execution-plan-sha256",
        _sha(plan),
        "--live-summary",
        str(summary),
        "--expected-live-sha256",
        _sha(summary),
        env=hooks,
    )
    assert created.returncode == 0, created.stderr
    active = (root / "active").resolve()

    (active / "execution-plan.snapshot").write_text("tampered\n")
    snapshot_tamper = run_script_production("gray-phase.sh", "verify", env={"GRAY_ROOT": root})
    assert snapshot_tamper.returncode != 0
    assert "input" in snapshot_tamper.stderr.lower() or "checksum" in snapshot_tamper.stderr.lower()

    other_root = tmp_path / "other-run"
    created = run_script_production(
        "gray-run-init.sh",
        "--execution-plan",
        str(plan),
        "--execution-plan-sha256",
        _sha(plan),
        "--live-summary",
        str(summary),
        "--expected-live-sha256",
        _sha(summary),
        env={**hooks, "GRAY_ROOT": other_root},
    )
    assert created.returncode == 0, created.stderr
    active = (other_root / "active").resolve()
    manifest = active / "input-checksums.env"
    manifest.write_text(manifest.read_text().replace("execution_plan=", "execution_plan=0"))
    manifest_tamper = run_script_production("gray-phase.sh", "verify", env={"GRAY_ROOT": other_root})
    assert manifest_tamper.returncode != 0
    assert "input" in manifest_tamper.stderr.lower() or "checksum" in manifest_tamper.stderr.lower()


def test_run_init_reloads_and_checks_workers_before_publishing_active(tmp_path):
    plan = tmp_path / "plan.md"
    summary = tmp_path / "live.json"
    plan.write_text("approved plan\n")
    summary.write_text('{"live":"redacted"}\n')
    reload_marker = tmp_path / "reload"
    check_marker = tmp_path / "post-check"
    result = run_script_production(
        "gray-run-init.sh",
        "--execution-plan",
        str(plan),
        "--execution-plan-sha256",
        _sha(plan),
        "--live-summary",
        str(summary),
        "--expected-live-sha256",
        _sha(summary),
        env={
            "GRAY_ROOT": tmp_path / "run",
            "GRAY_NGINX_TEST_CMD": "true",
            "GRAY_RELOAD_CMD": f"touch '{reload_marker}'",
            "GRAY_POST_RELOAD_CMD": f"touch '{check_marker}'",
            "GRAY_INITIAL_ROLLBACK_CMD": "true",
        },
    )
    assert result.returncode == 0, result.stderr
    assert reload_marker.exists()
    assert check_marker.exists()


def test_run_init_restores_live_config_when_initial_post_check_fails(tmp_path):
    plan = tmp_path / "plan.md"
    summary = tmp_path / "live.json"
    plan.write_text("approved plan\n")
    summary.write_text('{"live":"redacted"}\n')
    rollback_marker = tmp_path / "rollback"
    root = tmp_path / "run"
    result = run_script_production(
        "gray-run-init.sh",
        "--execution-plan",
        str(plan),
        "--execution-plan-sha256",
        _sha(plan),
        "--live-summary",
        str(summary),
        "--expected-live-sha256",
        _sha(summary),
        env={
            "GRAY_ROOT": root,
            "GRAY_NGINX_TEST_CMD": "true",
            "GRAY_RELOAD_CMD": "true",
            "GRAY_POST_RELOAD_CMD": "false",
            "GRAY_INITIAL_ROLLBACK_CMD": f"touch '{rollback_marker}'",
        },
    )
    assert result.returncode != 0
    assert rollback_marker.exists()
    assert not (root / "active").exists()
    assert "restored" in result.stderr.lower()


def test_run_init_binds_initial_rollback_before_render(tmp_path):
    plan = tmp_path / "plan.md"
    summary = tmp_path / "live.json"
    plan.write_text("approved plan\n")
    summary.write_text('{"live":"redacted"}\n')
    renderer_env = _renderer_env(tmp_path)
    marker = tmp_path / "render-ran"
    renderer_env["GRAY_RENDER_CMD"] = (
        f"{renderer_env['GRAY_RENDER_CMD']}; printf x >> '{marker}'"
    )
    result = run_script_production(
        "gray-run-init.sh",
        "--execution-plan",
        str(plan),
        "--execution-plan-sha256",
        _sha(plan),
        "--live-summary",
        str(summary),
        "--expected-live-sha256",
        _sha(summary),
        env={
            **renderer_env,
            "GRAY_ROOT": tmp_path / "run",
            "GRAY_NGINX_TEST_CMD": "true",
            "GRAY_RELOAD_CMD": "true",
            "GRAY_POST_RELOAD_CMD": "true",
            "GRAY_INITIAL_ROLLBACK_CMD": "",
            "GRAY_ABORT_VERIFY_CMD": "true",
        },
    )

    assert result.returncode != 0
    assert "initial_rollback" in result.stderr.lower()
    assert not marker.exists()


def test_transactions_render_the_exact_candidate_generation(run_root, tmp_path):
    marker = tmp_path / "rendered-generations"
    render = tmp_path / "render.sh"
    render.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$GRAY_GENERATION_DIR\" >> '{marker}'\n"
    )
    render.chmod(0o700)
    init = run_script(
        "gray-run-init.sh",
        env={
            "GRAY_ROOT": run_root,
            "GRAY_RENDER_CMD": render,
            "GRAY_NGINX_TEST_CMD": "true",
            "GRAY_RELOAD_CMD": "true",
            "GRAY_POST_RELOAD_CMD": "true",
        },
    )
    assert init.returncode == 0, init.stderr
    initial = (run_root / "active").resolve()
    assert marker.read_text().splitlines() == [str(initial)]

    assert run_script(
        "gray-phase.sh",
        "set",
        "normal_gray",
        env={
            "GRAY_ROOT": run_root,
            "GRAY_RENDER_CMD": render,
            "GRAY_NGINX_TEST_CMD": "true",
            "GRAY_RELOAD_CMD": "true",
            "GRAY_POST_RELOAD_CMD": "true",
        },
    ).returncode == 0
    current = (run_root / "active").resolve()
    assert marker.read_text().splitlines()[-1] == str(current)


def test_production_run_freezes_renderer_inputs_and_rejects_base_drift(run_root, tmp_path):
    renderer_env = _renderer_env(tmp_path)
    init_production_run(run_root, tmp_path, env=renderer_env)
    active = (run_root / "active").resolve()
    manifest = (active / "input-checksums.env").read_text()
    assert "render_command=" in manifest
    assert "render_base_template=" in manifest
    assert "render_debug_token=" in manifest
    assert "renderer_identity=" in manifest
    attestation = active / "render-attestation.json"
    assert attestation.exists()
    assert stat.S_IMODE(attestation.stat().st_mode) == 0o600

    base = Path(renderer_env["GRAY_RENDER_BASE_TEMPLATE"])
    base.write_text(base.read_text() + "# drift\n")
    old_target = (run_root / "active").resolve()
    result = run_script_production(
        "gray-phase.sh",
        "set",
        "normal_gray",
        env={
            **renderer_env,
            "GRAY_ROOT": run_root,
            "GRAY_NGINX_TEST_CMD": "true",
            "GRAY_RELOAD_CMD": "true",
            "GRAY_POST_RELOAD_CMD": "true",
            "GRAY_GATE_EVIDENCE_FILE": _write_gate(
                run_root / "evidence" / "gray_entry.json", run_root, "gray_entry"
            ),
        },
    )

    assert result.returncode != 0
    assert (run_root / "active").resolve() == old_target
    assert "render" in result.stderr.lower() or "input" in result.stderr.lower()


def test_production_transaction_rejects_render_command_and_output_drift(run_root, tmp_path):
    renderer_env = _renderer_env(tmp_path)
    init_production_run(run_root, tmp_path, env=renderer_env)
    old_target = (run_root / "active").resolve()
    gate = _write_gate(run_root / "evidence" / "gray_entry.json", run_root, "gray_entry")
    changed_command = dict(renderer_env)
    changed_command["GRAY_RENDER_CMD"] = str(renderer_env["GRAY_RENDER_CMD"]) + " "
    command_result = run_script_production(
        "gray-phase.sh",
        "set",
        "normal_gray",
        env={
            **changed_command,
            "GRAY_ROOT": run_root,
            "GRAY_NGINX_TEST_CMD": "true",
            "GRAY_RELOAD_CMD": "true",
            "GRAY_POST_RELOAD_CMD": "true",
            "GRAY_GATE_EVIDENCE_FILE": gate,
        },
    )
    assert command_result.returncode != 0
    assert (run_root / "active").resolve() == old_target

    output = Path(renderer_env["GRAY_RENDER_OUTPUT"])
    tamper = tmp_path / "tamper-output.sh"
    tamper.write_text(f"#!/bin/sh\nprintf '# tampered\\n' >> '{output}'\n")
    tamper.chmod(0o700)
    output_result = run_script_production(
        "gray-phase.sh",
        "set",
        "normal_gray",
        env={
            **renderer_env,
            "GRAY_ROOT": run_root,
            "GRAY_NGINX_TEST_CMD": tamper,
            "GRAY_RELOAD_CMD": "true",
            "GRAY_POST_RELOAD_CMD": "true",
            "GRAY_GATE_EVIDENCE_FILE": gate,
        },
    )
    assert output_result.returncode != 0
    assert (run_root / "active").resolve() == old_target


def test_generation_verification_rejects_external_active_and_insecure_files(run_root, tmp_path):
    init_run(run_root)
    external = tmp_path / "external"
    external.mkdir()
    (run_root / "active").unlink()
    (run_root / "active").symlink_to(external)
    escaped = run_script("gray-phase.sh", "verify", env={"GRAY_ROOT": run_root})
    assert escaped.returncode != 0
    assert any(word in escaped.stderr.lower() for word in ("generation", "contain", "active"))

    other = tmp_path / "mode-run"
    init_run(other)
    active = (other / "active").resolve()
    (active / "force-gray.map").chmod(0o666)
    insecure = run_script("gray-phase.sh", "verify", env={"GRAY_ROOT": other})
    assert insecure.returncode != 0
    assert "permission" in insecure.stderr.lower() or "mode" in insecure.stderr.lower()


def test_gate_evidence_requires_trusted_regular_0600_file(run_root, tmp_path):
    init_production_run(run_root, tmp_path)
    evidence_dir = run_root / "evidence"
    evidence = _write_gate(evidence_dir / "gray-entry.json", run_root, "gray_entry")
    evidence.chmod(0o666)
    insecure = run_script_production(
        "gray-phase.sh",
        "set",
        "normal_gray",
        env={
            "GRAY_ROOT": run_root,
            "GRAY_GATE_EVIDENCE_DIR": evidence_dir,
            "GRAY_GATE_EVIDENCE_FILE": evidence,
            "GRAY_NGINX_TEST_CMD": "true",
            "GRAY_RELOAD_CMD": "true",
            "GRAY_POST_RELOAD_CMD": "true",
        },
    )
    assert insecure.returncode != 0
    assert "evidence" in insecure.stderr.lower()

    evidence.chmod(0o600)
    real = evidence_dir / "real.json"
    evidence.replace(real)
    evidence.symlink_to(real)
    symlinked = run_script_production(
        "gray-phase.sh",
        "set",
        "normal_gray",
        env={
            "GRAY_ROOT": run_root,
            "GRAY_GATE_EVIDENCE_DIR": evidence_dir,
            "GRAY_GATE_EVIDENCE_FILE": evidence,
            "GRAY_NGINX_TEST_CMD": "true",
            "GRAY_RELOAD_CMD": "true",
            "GRAY_POST_RELOAD_CMD": "true",
        },
    )
    assert symlinked.returncode != 0
    assert "evidence" in symlinked.stderr.lower()


def test_dispatcher_rejects_non_metrics_or_unchecksummed_evidence(run_root, tmp_path):
    init_production_run(run_root, tmp_path)
    gray_entry = _write_gate(run_root / "evidence" / "gray_entry.json", run_root, "gray_entry")
    assert run_script_production(
        "gray-phase.sh", "set", "normal_gray",
        env={
            "GRAY_ROOT": run_root,
            "GRAY_GATE_EVIDENCE_FILE": gray_entry,
            "GRAY_NGINX_TEST_CMD": "true",
            "GRAY_RELOAD_CMD": "true",
            "GRAY_POST_RELOAD_CMD": "true",
        },
    ).returncode == 0
    state = _state(run_root)
    evidence_dir = run_root / "evidence"
    payload = {
        "run_id": state["run_id"],
        "generation": state["generation"],
        "config_checksum": state["config_checksum"],
        "phase": state["phase"],
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "dispatcher_recommendation": {
            "action": "alert_only",
            "reason_codes": ["INSUFFICIENT_SAMPLE"],
            "hard_trigger": False,
        },
    }
    evidence = evidence_dir / "metrics.json"
    evidence_dir.mkdir(mode=0o700, exist_ok=True)
    evidence.write_text(json.dumps(payload))
    evidence.chmod(0o600)
    result = run_script_production(
        "gray-auto-dispatch.sh",
        "--input",
        str(evidence),
        env={"GRAY_ROOT": run_root, "GRAY_GATE_EVIDENCE_DIR": evidence_dir},
    )
    assert result.returncode != 0
    assert "metrics" in result.stderr.lower() or "checksum" in result.stderr.lower()


def test_gate_evidence_must_live_under_default_run_evidence_directory(run_root, tmp_path):
    init_production_run(run_root, tmp_path)
    external_dir = tmp_path / "external-evidence"
    evidence = _write_gate(external_dir / "gray-entry.json", run_root, "gray_entry")
    result = run_script_production(
        "gray-phase.sh",
        "set",
        "normal_gray",
        env={
            "GRAY_ROOT": run_root,
            "GRAY_GATE_EVIDENCE_FILE": evidence,
            "GRAY_NGINX_TEST_CMD": "true",
            "GRAY_RELOAD_CMD": "true",
            "GRAY_POST_RELOAD_CMD": "true",
        },
    )
    assert result.returncode != 0
    assert "contain" in result.stderr.lower() or "insecure" in result.stderr.lower()


def test_gate_evidence_binds_phase_transition_to_current_generation(run_root, tmp_path):
    init_production_run(run_root, tmp_path)
    evidence = _write_gate(run_root / "evidence" / "gray-entry.json", run_root, "gray_entry")
    transitioned = run_script_production(
        "gray-phase.sh",
        "set",
        "normal_gray",
        env={
            "GRAY_ROOT": run_root,
            "GRAY_GATE_EVIDENCE_FILE": evidence,
            "GRAY_NGINX_TEST_CMD": "true",
            "GRAY_RELOAD_CMD": "true",
            "GRAY_POST_RELOAD_CMD": "true",
        },
    )
    assert transitioned.returncode == 0, transitioned.stderr

    other = run_root.parent / "other-run"
    init_production_run(other, tmp_path)
    stale = run_script_production(
        "gray-phase.sh",
        "set",
        "normal_gray",
        env={
            "GRAY_ROOT": other,
            "GRAY_GATE_EVIDENCE_FILE": evidence,
            "GRAY_NGINX_TEST_CMD": "true",
            "GRAY_RELOAD_CMD": "true",
            "GRAY_POST_RELOAD_CMD": "true",
        },
    )
    assert stale.returncode != 0
    assert "mismatch" in stale.stderr.lower() or "insecure" in stale.stderr.lower()


def test_gate_evidence_rejects_expired_capture(run_root, tmp_path):
    init_production_run(run_root, tmp_path)
    evidence = _write_gate(
        run_root / "evidence" / "expired.json",
        run_root,
        "gray_entry",
        captured_at=datetime.now(timezone.utc) - timedelta(days=2),
    )
    result = run_script_production(
        "gray-phase.sh",
        "set",
        "normal_gray",
        env={
            "GRAY_ROOT": run_root,
            "GRAY_GATE_EVIDENCE_FILE": evidence,
            "GRAY_NGINX_TEST_CMD": "true",
            "GRAY_RELOAD_CMD": "true",
            "GRAY_POST_RELOAD_CMD": "true",
        },
    )
    assert result.returncode != 0
    assert "stale" in result.stderr.lower()


def test_convergence_prepare_is_single_use_for_a_run(run_root):
    init_run(run_root)
    assert run_script("gray-phase.sh", "set", "normal_gray", env={"GRAY_ROOT": run_root}).returncode == 0
    set_split_100(run_root)
    gates = {
        "GRAY_ROOT": run_root,
        "GRAY_GRAY_STABLE": "1",
        "GRAY_CONTROL_PLANE_OK": "1",
        "GRAY_BRIDGE_READY": "1",
        "GRAY_BYPASS_DISPOSITION_OK": "1",
    }
    assert run_script("gray-convergence-prepare.sh", env=gates).returncode == 0
    first_target = (run_root / "active").resolve()
    repeated = run_script("gray-convergence-prepare.sh", env=gates)
    assert repeated.returncode != 0
    assert (run_root / "active").resolve() == first_target


def test_dispatcher_accepts_fresh_bound_evidence_file_without_mutation(run_root, tmp_path):
    init_production_run(run_root, tmp_path)
    gray_entry = _write_gate(run_root / "evidence" / "gray_entry.json", run_root, "gray_entry")
    assert run_script_production(
        "gray-phase.sh", "set", "normal_gray",
        env={
            "GRAY_ROOT": run_root,
            "GRAY_GATE_EVIDENCE_FILE": gray_entry,
            "GRAY_NGINX_TEST_CMD": "true",
            "GRAY_RELOAD_CMD": "true",
            "GRAY_POST_RELOAD_CMD": "true",
        },
    ).returncode == 0
    state = _state(run_root)
    payload = {
        "run_id": state["run_id"],
        "generation": state["generation"],
        "config_checksum": state["config_checksum"],
        "phase": state["phase"],
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "dispatcher_recommendation": {
            "action": "alert_only",
            "reason_codes": ["INSUFFICIENT_SAMPLE"],
            "hard_trigger": False,
        },
    }
    evidence = run_root / "evidence" / "metrics.json"
    canonical = {**payload, "tool": "metrics", "schema_version": 1, "status": "PASS"}
    rendered = json.dumps(canonical, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    canonical["payload_sha256"] = "sha256:" + hashlib.sha256(rendered.encode()).hexdigest()
    evidence.write_text(json.dumps(canonical))
    evidence.chmod(0o600)
    old_target = (run_root / "active").resolve()
    result = run_script_production(
        "gray-auto-dispatch.sh",
        "--input",
        str(evidence),
        env={"GRAY_ROOT": run_root},
    )
    assert result.returncode == 0, result.stderr
    assert "mutation=none" in result.stdout
    assert (run_root / "active").resolve() == old_target


def test_production_dispatcher_uses_verified_metrics_as_rollback_authorization(run_root, tmp_path):
    init_production_run(run_root, tmp_path)
    gray_entry = _write_gate(run_root / "evidence" / "gray_entry.json", run_root, "gray_entry")
    hooks = {
        "GRAY_ROOT": run_root,
        "GRAY_GATE_EVIDENCE_FILE": gray_entry,
        "GRAY_NGINX_TEST_CMD": "true",
        "GRAY_RELOAD_CMD": "true",
        "GRAY_POST_RELOAD_CMD": "true",
    }
    assert run_script_production("gray-phase.sh", "set", "normal_gray", env=hooks).returncode == 0
    evidence = _write_metrics(
        run_root / "evidence" / "rollback-metrics.json",
        run_root,
        "rollback",
        hard_trigger=True,
        status="FAIL",
    )

    result = run_script_production(
        "gray-auto-dispatch.sh",
        "--input",
        str(evidence),
        env={
            "GRAY_ROOT": run_root,
            "GRAY_NGINX_TEST_CMD": "true",
            "GRAY_RELOAD_CMD": "true",
            "GRAY_POST_RELOAD_CMD": "true",
        },
    )

    assert result.returncode == 0, result.stderr
    assert _state(run_root)["phase"] == "rolled_back"


def test_dispatch_authorization_rejects_a_different_verified_metrics_action(run_root, tmp_path):
    init_production_run(run_root, tmp_path)
    gray_entry = _write_gate(run_root / "evidence" / "gray_entry.json", run_root, "gray_entry")
    common = {
        "GRAY_ROOT": run_root,
        "GRAY_NGINX_TEST_CMD": "true",
        "GRAY_RELOAD_CMD": "true",
        "GRAY_POST_RELOAD_CMD": "true",
    }
    assert run_script_production(
        "gray-phase.sh", "set", "normal_gray",
        env={**common, "GRAY_GATE_EVIDENCE_FILE": gray_entry},
    ).returncode == 0
    evidence = _write_metrics(
        run_root / "evidence" / "alert-only-metrics.json",
        run_root,
        "alert_only",
        hard_trigger=False,
        status="PASS",
    )

    result = run_script_production(
        "gray-global-rollback.sh",
        env={
            **common,
            "GRAY_DISPATCH_AUTHORIZED_ACTION": "rollback",
            "GRAY_DISPATCH_EVIDENCE_FILE": evidence,
        },
    )

    assert result.returncode != 0
    assert _state(run_root)["phase"] == "normal_gray"


def test_production_dispatcher_uses_verified_metrics_as_abort_authorization(run_root, tmp_path):
    init_production_run(run_root, tmp_path)
    gray_entry = _write_gate(run_root / "evidence" / "gray_entry.json", run_root, "gray_entry")
    common = {
        "GRAY_ROOT": run_root,
        "GRAY_NGINX_TEST_CMD": "true",
        "GRAY_RELOAD_CMD": "true",
        "GRAY_POST_RELOAD_CMD": "true",
    }
    assert run_script_production(
        "gray-phase.sh", "set", "normal_gray",
        env={**common, "GRAY_GATE_EVIDENCE_FILE": gray_entry},
    ).returncode == 0
    set_production_split_100(run_root, common)
    for gate in ("convergence_stable", "convergence_control_plane", "convergence_bridge", "convergence_bypass_disposition"):
        _write_gate(run_root / "evidence" / f"{gate}.json", run_root, gate)
    assert run_script_production(
        "gray-convergence-prepare.sh",
        env={**common, "GRAY_GATE_EVIDENCE_DIR": run_root / "evidence"},
    ).returncode == 0
    prod_zero = _write_gate(run_root / "evidence" / "prod_zero.json", run_root, "prod_zero")
    assert run_script_production(
        "gray-phase.sh", "set", "prod_offline_upgrading",
        env={**common, "GRAY_GATE_EVIDENCE_FILE": prod_zero},
    ).returncode == 0
    evidence = _write_metrics(
        run_root / "evidence" / "abort-metrics.json",
        run_root,
        "abort_to_bridge",
        hard_trigger=True,
        status="FAIL",
    )

    result = run_script_production(
        "gray-auto-dispatch.sh",
        "--input",
        str(evidence),
        env={**common, "GRAY_ABORT_VERIFY_CMD": "true"},
    )

    assert result.returncode == 0, result.stderr
    assert _state(run_root)["phase"] == "aborted"


def test_convergence_abort_can_resume_after_bridge_verification_failure(run_root, tmp_path):
    bridge_ready = tmp_path / "bridge-ready"
    abort_verify = f"test -f '{bridge_ready}'"
    init_production_run(
        run_root,
        tmp_path,
        env={"GRAY_ABORT_VERIFY_CMD": abort_verify},
    )
    gray_entry = _write_gate(run_root / "evidence" / "gray_entry.json", run_root, "gray_entry")
    common = {
        "GRAY_ROOT": run_root,
        "GRAY_NGINX_TEST_CMD": "true",
        "GRAY_RELOAD_CMD": "true",
        "GRAY_POST_RELOAD_CMD": "true",
    }
    assert run_script_production(
        "gray-phase.sh", "set", "normal_gray",
        env={**common, "GRAY_GATE_EVIDENCE_FILE": gray_entry},
    ).returncode == 0
    set_production_split_100(run_root, common)
    for gate in ("convergence_stable", "convergence_control_plane", "convergence_bridge", "convergence_bypass_disposition"):
        _write_gate(run_root / "evidence" / f"{gate}.json", run_root, gate)
    convergence_env = {
        **common,
        "GRAY_GATE_EVIDENCE_DIR": run_root / "evidence",
    }
    assert run_script_production("gray-convergence-prepare.sh", env=convergence_env).returncode == 0
    prod_zero = _write_gate(run_root / "evidence" / "prod_zero.json", run_root, "prod_zero")
    assert run_script_production(
        "gray-phase.sh", "set", "prod_offline_upgrading",
        env={**common, "GRAY_GATE_EVIDENCE_FILE": prod_zero},
    ).returncode == 0
    abort_gate = _write_gate(run_root / "evidence" / "convergence_abort.json", run_root, "convergence_abort")

    failed = run_script_production(
        "gray-convergence-abort.sh",
        env={
            **common,
            "GRAY_GATE_EVIDENCE_FILE": abort_gate,
            "GRAY_ABORT_VERIFY_CMD": abort_verify,
        },
    )
    assert failed.returncode != 0
    first_bridge_generation = (run_root / "active").resolve()
    assert _state(run_root)["phase"] == "aborting_to_bridge"

    bridge_ready.touch()
    resumed = run_script_production(
        "gray-convergence-abort.sh",
        env={**common, "GRAY_ABORT_VERIFY_CMD": abort_verify},
    )
    assert resumed.returncode == 0, resumed.stderr
    assert _state(run_root)["phase"] == "aborted"
    assert first_bridge_generation != (run_root / "active").resolve()
