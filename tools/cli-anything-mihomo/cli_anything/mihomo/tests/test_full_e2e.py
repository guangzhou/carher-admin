"""E2E against the real 198 + real feed + real mihomo, through the installed command.

Needs ssh cltx@10.68.13.198, the 188 SOCKS tunnel on 127.0.0.1:18880, and `sub set-url` done.
Per HARNESS these FAIL (not skip) when a backend is missing. Uses instance `e2e` on its own
ports, and destroys it at the end, so it never touches `default`.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

NAME = "e2e"
MIXED, CTL, PIN = 27890, 29090, 27891


def _resolve_cli(name: str) -> list[str]:
    path = shutil.which(name)
    if path:
        print(f"[_resolve_cli] Using installed command: {path}")
        return [path]
    if os.environ.get("CLI_ANYTHING_FORCE_INSTALLED", "").strip() == "1":
        raise RuntimeError(f"{name} not in PATH; uv tool install --editable tools/cli-anything-mihomo")
    return [sys.executable, "-m", "cli_anything.mihomo"]


CLI = _resolve_cli("cli-anything-mihomo")


def run(args: list[str], check: bool = True, name: str = NAME) -> subprocess.CompletedProcess[str]:
    return subprocess.run(CLI + ["--json", "--name", name, *args], capture_output=True, text=True,
                          check=check, timeout=600)


def jrun(args: list[str]):
    return json.loads(run(args).stdout)


@pytest.fixture(scope="module")
def started():
    run(["instance", "destroy", "--yes"], check=False)
    sub = jrun(["sub", "fetch"])
    versions = jrun(["binary", "list"])
    assert versions, "no mihomo on 198: run `binary fetch` + `binary install --version vX`"
    jrun(["config", "set", "--mixed-port", str(MIXED), "--ctl-port", str(CTL), "--version", versions[-1]])
    jrun(["config", "pin", str(PIN), "♻️ 自动选择 - US"])
    st = jrun(["instance", "start"])
    yield {"sub": sub, "start": st}
    run(["instance", "destroy", "--yes"], check=False)


class TestWorkflow:
    def test_1_subscription(self, started):
        s = started["sub"]
        assert s["proxies"] >= 40
        assert "🔰 节点选择" in {g["name"] for g in s["groups"]}

    def test_1b_quota_from_live_header(self, started):
        rows = json.loads(subprocess.run(CLI + ["--json", "sub", "quota", "--warn-pct", "100", "--warn-days", "0"],
                                         capture_output=True, text=True, timeout=180).stdout)
        q = rows[0]
        assert q["status"] == "OK" and q["total_bytes"] > 0 and q["expire_date"] and q["age_s"] < 120, q

    def test_2_render_is_safe(self, started):
        r = jrun(["config", "render", "--audit-only"])
        assert r["audit"] == "OK" and r["allow-lan"] is False and r["bind-address"] == "127.0.0.1"
        assert r["external-controller"] == f"127.0.0.1:{CTL}"

    def test_3_running_and_localhost_only(self, started):
        s = jrun(["instance", "status"])
        assert s["running"] and s["ctl_listening"]
        assert s["ports"] == {str(MIXED): True, str(PIN): True}
        assert s["exposed"] == []
        assert s["binary"].endswith(started["start"]["binary"].rsplit("/", 1)[-1])

    def test_4_verify_real_scholar_results(self, started):
        rows = jrun(["exit", "verify"])
        for r in rows:
            print(f"\n  port {r['port']}: {r['exit']} scholar={r['scholar_status']} results={r['scholar_results']}")
            assert r["scholar_status"] == 200 and r["scholar_results"] > 0, r
        assert {r["port"]: r for r in rows}[PIN]["exit"]["country"] == "US"

    def test_5_switch_changes_real_exit(self, started):
        r = jrun(["exit", "select", "♻️ 自动选择 - JP"])
        assert r["now"] == "♻️ 自动选择 - JP" and r["chain"][-1].startswith("JP")
        v = jrun(["exit", "verify", "--port", str(MIXED)])[0]
        assert v["exit"]["country"] == "JP" and v["ok"], v

    def test_6_reload_keeps_selection(self, started):
        r = jrun(["instance", "reload"])
        assert "successful" in r["config"]["test"]
        ports = {p["port"]: p for p in jrun(["exit", "ports"])}
        assert ports[MIXED]["chain"][1] == "♻️ 自动选择 - JP"

    def test_7_measure_region(self, started):
        r = jrun(["exit", "measure", "--region", "US"])
        assert r["ok"] >= 1 and r["total"] >= r["ok"]
        delays = [x["delay_ms"] for x in r["results"] if x["delay_ms"] is not None]
        assert delays == sorted(delays)

    def test_7b_exit_table_has_real_country(self, started):
        rows = {r["port"]: r for r in jrun(["exit", "table", "--samples", "2"])}
        assert rows[PIN]["country"] == "US" and rows[PIN]["exit_ip"], rows[PIN]
        assert rows[PIN]["kiro_ms"] is not None and rows[PIN]["kiro_ok"] != "0/2"

    def test_7c_other_account_cannot_touch_it(self, started):
        p = subprocess.run(CLI + ["--json", "--account", "e2e-other", "--name", NAME, "instance", "stop"],
                           capture_output=True, text=True, timeout=120)
        assert p.returncode != 0 and "belongs to account" in p.stderr
        assert jrun(["instance", "status"])["running"]

    def test_7d_instance_list_sees_it(self, started):
        rows = {r["instance"]: r for r in jrun(["instance", "list"])}
        assert rows[NAME]["running"] and rows[NAME]["exposed"] == []

    def test_8_bad_select_fails_loudly(self, started):
        p = run(["exit", "select", "nosuch"], check=False)
        assert p.returncode != 0 and "🔰 节点选择" in p.stderr

    def test_9_destroy_leaves_nothing(self, started):
        jrun(["instance", "destroy", "--yes"])
        s = jrun(["instance", "status"])
        assert not s["present"] and not s["running"]


class TestCLISubprocess:
    def test_help(self):
        p = subprocess.run(CLI + ["--help"], capture_output=True, text=True)
        assert p.returncode == 0 and "instance" in p.stdout and "exit" in p.stdout

    def test_shellcrash_modes_json(self):
        r = json.loads(subprocess.run(CLI + ["--json", "shellcrash", "modes"], capture_output=True, text=True, check=True).stdout)
        assert len(r["firewall_areas"]) == 5 and r["source"].startswith("juewuy/ShellCrash@")
