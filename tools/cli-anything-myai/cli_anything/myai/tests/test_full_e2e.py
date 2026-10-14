"""E2E against the real 243 namespace (read-only). Needs MYAI_SSH_PASSWORD or ssh keys.

No graceful degradation: if 243 is unreachable these fail, they do not skip.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys


def _resolve_cli(name: str = "cli-anything-myai") -> list[str]:
    force = os.environ.get("CLI_ANYTHING_FORCE_INSTALLED", "").strip() == "1"
    path = shutil.which(name)
    if path:
        print(f"[_resolve_cli] Using installed command: {path}")
        return [path]
    if force:
        raise RuntimeError(f"{name} not found in PATH. Install with: scripts/myai-cli.sh install")
    module = "cli_anything.myai.myai_cli"
    print(f"[_resolve_cli] Falling back to: {sys.executable} -m {module}")
    return [sys.executable, "-m", module]


CLI = _resolve_cli()


def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(CLI + list(args), capture_output=True, text=True, check=check, timeout=600)


def _json(*args: str):
    result = _run("--json", *args)
    return json.loads(result.stdout)


def test_help_and_version():
    assert "diagnose" in _run("--help").stdout
    assert "1.0.0" in _run("--version").stdout


def test_status_points_at_243():
    data = _json("status")
    assert data["status"] in {"ok", "degraded"}
    assert str(data["workerId"]).startswith("usage-sync-worker-")
    assert isinstance(data["findings"], list)


def test_read_only_select_and_server_side_guard():
    assert _json("db", "sql", "SELECT 1 AS one") == [{"one": "1"}]
    # Passes the local validator (starts with SELECT) but takes a row lock:
    # the server's read-only transaction must refuse it.
    result = _run("--json", "db", "sql", "SELECT 1 FROM usage_sync_state FOR UPDATE", check=False)
    assert result.returncode != 0
    assert "read-only" in (result.stderr + result.stdout)


def test_sync_chain_shapes():
    state = _json("sync", "state")
    assert len(state) == 1 and {"last_success_at", "published_at", "worker_id"} <= set(state[0])
    runs = _json("sync", "runs", "--days", "3")
    assert runs and {"day", "status", "runs"} <= set(runs[0])
    queue = _json("queue")
    assert isinstance(queue, list)


def test_diagnose_sync_stuck():
    report = _json("diagnose", "sync-stuck", "--days", "7")
    print("\n  verdict:", report["verdict"])
    assert report["attemptedUsers"] > 0
    assert len(report["days"]) == 7
    assert all({"need", "blocked", "activeUsers"} <= set(day) for day in report["days"])


def test_upstream_daily_her():
    data = _json("upstream", "daily", "--backend", "her", "--days", "3")
    days = data["backends"]["her"]["days"]
    assert len(days) == 3 and all(day.get("status") == 200 for day in days)
