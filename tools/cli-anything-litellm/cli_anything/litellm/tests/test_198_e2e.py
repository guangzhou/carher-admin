"""E2E against the real 198 deployment, through the installed command.

Needs ssh to cltx@10.68.13.198 with `sudo -n`. Per HARNESS these FAIL (not skip) when the
backend is unreachable. Everything here is read-only; the one write attempt is the guard
test and is expected to be refused by Postgres.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import date

import pytest

from cli_anything.litellm.utils import remote198


def _resolve_cli(name: str) -> list[str]:
    force = os.environ.get("CLI_ANYTHING_FORCE_INSTALLED", "").strip() == "1"
    path = shutil.which(name)
    if path:
        print(f"[_resolve_cli] Using installed command: {path}")
        return [path]
    if force:
        raise RuntimeError(f"{name} not found in PATH. Install with: pip install -e .")
    return [sys.executable, "-m", "cli_anything.litellm"]


CLI = _resolve_cli("cli-anything-litellm")


def run(args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(CLI + ["--json", *args], capture_output=True, text=True, check=check, timeout=600)


def jrun(args: list[str]):
    return json.loads(run(args).stdout)


@pytest.fixture(scope="module")
def prod_probe():
    return jrun(["patches", "targets", "--lane", "prod"])


def test_db_anchor_is_prod():
    a = jrun(["db", "anchor"])
    assert int(a["proxy_model_rows"]) >= 100, a  # dev DB has 3
    assert int(a["db_fallbacks"]) > 0, a        # dev has no fallbacks key
    assert int(a["connections"]) < int(a["max_connections"])


def test_psql_is_read_only():
    with pytest.raises(remote198.RemoteError) as e:
        remote198.psql("create table _cli_anything_write_probe(x int)")
    assert "read-only" in e.value.stderr


def test_db_catalog_matches_schema():
    c = jrun(["db", "catalog"])
    assert c["missing_in_db"] == []
    assert c["migrations"]["unfinished"] == "0"
    assert c["largest"][0]["table"] == "LiteLLM_SpendLogs"


def test_db_config_owner():
    o = jrun(["db", "config-owner"])
    owners = {i["key"]: i["owner"] for i in o["router_settings"]}
    assert owners["fallbacks"] == "db"
    assert owners["redis_host"] == "config"
    assert all(i["value"] == "redacted" for i in o["general_settings"] if i["key"] in ("master_key", "database_url"))


def test_db_retention_is_running():
    r = jrun(["db", "retention"])
    oldest = date.fromisoformat(r["spendlogs_oldest"][:10])
    assert (date.today() - oldest).days <= 14, r


def test_db_spend_reconcile_arithmetic():
    today = date.today().isoformat()
    r = jrun(["db", "spend-reconcile", today, "--limit", "20"])
    assert int(r["totals"]["daily_requests"]) >= 0 and int(r["totals"]["spendlog_rows"]) >= 0
    for k in r["keys"]:
        assert int(k["gap"]) == int(k["daily_requests"]) - int(k["spendlog_rows"])


def test_src_find_and_show_with_fingerprint():
    hits = jrun(["src", "find", "Router.should_retry_this_error", "--path", "litellm/router.py"])["hits"]
    assert hits and hits[0]["path"] == "litellm/router.py"
    body = jrun(["src", "show", f"litellm/router.py:{hits[0]['line']}"])["source"]
    # dead_deployment_retry's fingerprint guard looks for this string; if it disappears the guard refuses
    assert "_num_healthy_deployments" in body


def test_src_version_gate_rejects_wrong_tag():
    r = run(["src", "find", "x", "--tag", "v0.0.1", "--lane", "prod"], check=False)
    assert r.returncode == 1
    assert "live version" in r.stderr


def test_patches_prod_no_import_errors(prod_probe):
    assert prod_probe["import_errors"] == []
    assert len(prod_probe["callbacks"]) >= 30


def test_patches_process_chunk_stack(prod_probe):
    rows = {r["symbol"]: r for r in prod_probe["rows"]}
    owner = rows["BaseResponsesAPIStreamingIterator._process_chunk"]["owner"]
    assert owner.split(" -> ")[-1] == "deepseek_id_prefix", owner
    assert owner.count("->") == 2, owner


def test_patches_jsonify_two_layers(prod_probe):
    rows = {r["symbol"]: r for r in prod_probe["rows"]}
    assert rows["PrismaClient.jsonify_object"]["owner"] == "sitecustomize -> null_byte_sanitize"


def test_patches_prod_equals_gray():
    rows = jrun(["patches", "compare"])["rows"]
    assert all(r["owners"]["prod"] == r["owners"]["gray"] for r in rows)
