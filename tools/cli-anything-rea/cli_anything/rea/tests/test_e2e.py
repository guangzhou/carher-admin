"""E2E through the installed command against the real REA build on this Mac.

These FAIL (not skip) when REA/deps are missing: a harness that silently skips its real backend
reads green while proving nothing. The sample binary is compiled here from C with a known answer
(x clamped to [0,639], pan = x*200/639 - 100), so "decompiled" is checked against something
we wrote, not against REA's own output.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SAMPLE_C = r"""
#include <stdio.h>
#include <stdlib.h>
int rea_probe_pan(int x) { if (x < 0) x = 0; if (x > 639) x = 639; return (x * 200) / 639 - 100; }
int main(int argc, char **argv) { int x = argc > 1 ? atoi(argv[1]) : 320; printf("%d\n", rea_probe_pan(x)); return 0; }
"""


def _resolve_cli(name: str) -> list[str]:
    path = shutil.which(name)
    if path:
        return [path]
    if os.environ.get("CLI_ANYTHING_FORCE_INSTALLED", "").strip() == "1":
        raise RuntimeError(f"{name} not in PATH; uv tool install --editable tools/cli-anything-rea")
    return [sys.executable, "-m", "cli_anything.rea"]


CLI = _resolve_cli("cli-anything-rea")


def run(args: list[str], check: bool = True, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    return subprocess.run(CLI + ["--json", *args], capture_output=True, text=True, check=check, timeout=timeout)


def jrun(args: list[str], timeout: int = 900):
    return json.loads(run(args, timeout=timeout).stdout)


@pytest.fixture(scope="module")
def sample(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("rea-sample")
    (d / "pan.c").write_text(SAMPLE_C)
    subprocess.run(["clang", "-O1", "-o", str(d / "pan"), str(d / "pan.c")], check=True)
    out = subprocess.run([str(d / "pan"), "639"], capture_output=True, text=True, check=True).stdout.strip()
    assert out == "100"  # the program itself is the ground truth
    return d / "pan"


def test_deps_all_execute():
    r = jrun(["deps", "status"])
    bad = [t["tool"] for t in r["tools"] if not t["ok"]]
    assert not bad, bad


def test_doctor_engines_ghidra_kuna_jadx_healthy():
    r = jrun(["run", "doctor"], timeout=600)
    ok = {c["name"] for c in r["checks"] if c["ok"]}
    for need in ("ghidra-native_decompiler", "ghidra-java", "ghidra-version", "kuna-installation", "jadx"):
        assert need in ok, need


def test_runtime_tool_catalog_equals_source_table():
    r = jrun(["tools", "verify"], timeout=300)
    assert r["ok"], r
    assert r["runtime"] == r["source"] > 150
    assert r["runtime_destructive"] > 0


@pytest.mark.parametrize("provider", ["ghidra", "kuna"])
def test_decompile_recovers_the_clamp_constant(sample: Path, provider: str):
    r = jrun(["run", "decompile", str(sample), "_rea_probe_pan", "--provider", provider])
    code = str(r["pseudocode"])
    assert r["provider"] == provider and r["confidence"] == "observed"
    assert "0x27f" in code or "639" in code, code  # 639 = the clamp we compiled in
    assert "100" in code, code
    assert Path(r["saved"]).is_file()


def test_auto_provider_is_refused_when_two_engines_usable(sample: Path):
    """Upstream rule, observed for real: Ghidra + Kuna both usable ⇒ auto does not pick one."""
    from cli_anything.rea.core import env as env_core
    p = env_core.run_rea(["decompile", str(sample), "_rea_probe_pan", "--format", "json"], timeout=600)
    data = json.loads(p.stdout)
    assert "ambiguous" in json.dumps(data).lower(), data
