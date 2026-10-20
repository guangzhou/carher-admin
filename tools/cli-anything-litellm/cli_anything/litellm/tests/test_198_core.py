"""Unit tests for the 198 read-only layer. No network: remote198.run is stubbed."""
from __future__ import annotations

import importlib.util
import subprocess
from importlib import resources
from types import SimpleNamespace

import pytest

from cli_anything.litellm.core import db as db_core
from cli_anything.litellm.core import patches as patches_core
from cli_anything.litellm.core import source as source_core
from cli_anything.litellm.utils import remote198


@pytest.fixture
def captured(monkeypatch):
    calls: list[str] = []

    def install(output: str = ""):
        def fake_run(script, *, timeout=180, stdin=None):
            calls.append(script)
            return output

        monkeypatch.setattr(remote198, "run", fake_run)
        return calls

    return install


def test_psql_parses_rows_and_drops_noise(captured):
    calls = captured("SET\nSET\nn\tname\n3\tx\n4\ty\n(2 rows)\n")
    rows = remote198.psql("select 1")
    assert rows == [{"n": "3", "name": "x"}, {"n": "4", "name": "y"}]
    assert "default_transaction_read_only=on" in calls[0]
    assert "statement_timeout=20000" in calls[0]


def test_psql_empty_output_is_empty_list(captured):
    captured("SET\nSET\n")
    assert remote198.psql("select 1") == []


def test_run_raises_with_filtered_stderr(monkeypatch):
    def fake(cmd, **kw):
        return SimpleNamespace(returncode=3, stdout="", stderr="Warning: remote port forwarding failed\nboom\n")

    monkeypatch.setattr(subprocess, "run", fake)
    with pytest.raises(remote198.RemoteError) as e:
        remote198.run("false")
    assert e.value.returncode == 3
    assert "boom" in e.value.stderr and "forwarding" not in e.value.stderr


def test_lane_deploy_rejects_unknown():
    with pytest.raises(remote198.RemoteError):
        remote198.lane_deploy("dev")


def test_resolve_tag_requires_match_with_lane(monkeypatch):
    monkeypatch.setattr(remote198, "lane_pod", lambda lane: "pod")
    monkeypatch.setattr(remote198, "live_version", lambda pod: "1.105.1")
    monkeypatch.setattr(remote198, "fork_has_tag", lambda tag: True)
    assert source_core.resolve_tag("prod", None) == ("v1.105.1", "1.105.1")
    assert source_core.resolve_tag("prod", "v1.105.1")[0] == "v1.105.1"
    with pytest.raises(remote198.RemoteError, match="!= live version"):
        source_core.resolve_tag("prod", "v1.106.0")


def test_resolve_tag_missing_in_fork(monkeypatch):
    monkeypatch.setattr(remote198, "fork_has_tag", lambda tag: False)
    with pytest.raises(remote198.RemoteError, match="not in"):
        source_core.resolve_tag(None, "v0.0.0")


def test_show_symbol_bounds_and_decorators(monkeypatch):
    src = "\n".join([
        "class A:",
        "    @property",
        "    def f(self):",
        "        x = 1",
        "",
        "        return x",
        "    def g(self):",
        "        pass",
    ])
    monkeypatch.setattr(remote198, "fork_show", lambda tag, path: src)
    body = source_core.show_symbol("v1", "a.py", 3)
    assert "@property" in body and "return x" in body and "def g" not in body


@pytest.mark.parametrize("bad", ["a'b", "x;drop", "a b", "", "x" * 81])
def test_lit_rejects_unsafe(bad):
    with pytest.raises(remote198.RemoteError):
        db_core._lit(bad)


def test_spend_reconcile_sql_is_index_friendly(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(remote198, "psql", lambda sql, timeout_ms=0: seen.append(sql) or [])
    db_core.spend_reconcile("2026-10-20", "kiro-claude-*")
    sql = seen[0]
    assert "l.\"startTime\" >= '2026-10-20'::date" in sql
    assert "date_trunc" not in sql and "::date =" not in sql  # no function wrapped around the indexed column
    # SpendLogs is only touched per key (index point lookup), never grouped over the whole day
    assert "cross join lateral" in sql and "l.api_key=d.api_key" in sql
    assert "group by" not in sql.split("cross join lateral", 1)[1]
    assert "l.model_group like 'kiro-claude-%'" in sql


def test_spend_reconcile_rejects_bad_date():
    with pytest.raises(remote198.RemoteError):
        db_core.spend_reconcile("2026-10-20; select 1")


def _t(**kw):
    base = {"missing": None, "baseline_owner": "litellm.x.f", "final_owner": "litellm.x.f",
            "patched_by": [], "sentinels": [], "wrapped_depth": 0, "layers": ["litellm.x"]}
    return {**base, **kw}


def test_owner_of_rules():
    assert patches_core.owner_of("k", _t(missing="no-module:ImportError")) == "-"
    assert patches_core.owner_of("k", _t()) == "upstream"
    assert patches_core.owner_of("k", _t(patched_by=["a"], layers=["a", "litellm.x"])) == "a"
    # layers are outermost-first from the probe; printed innermost-first
    assert patches_core.owner_of("k", _t(patched_by=["b"], layers=["b", "a", "litellm.x"])) == "a -> b"
    # early owner is only claimed with evidence
    k = "ProxyUpdateSpend.update_spend_logs"
    assert patches_core.owner_of(k, _t()) == "upstream"
    assert patches_core.owner_of(k, _t(sentinels=["_spendlog_batch_rescue_patched"])) == patches_core.EARLY_OWNERS[k]


# The 37 modules mounted on prod/gray on 2026-10-20, copied from the live callbacks CM
# (byte-identical to k8s/litellm-callbacks/ that day). Literal on purpose: deriving it from
# PURPOSE would make the coverage test below unable to fail.
_LIVE_198_MODULES = frozenset({
    'anthropic_bridge_error_surface', 'anthropic_passthrough_pingfix', 'baiyu_image_route', 'budget_notice',
    'chatgpt_responses_normalize', 'chatgpt_responses_output_fallback', 'client_metadata_strip', 'codex_compaction_v2',
    'codex_custom_tool_bridge', 'codex_ua_pool_route', 'cursor_responses_shim', 'cursor_web_fc_sys_rewrite',
    'dead_deployment_retry', 'deepseek_id_prefix', 'deepseek_responses_adapt', 'embedding_sanitize',
    'encrypted_content_degrade_strip', 'error_sanitize', 'force_stream', 'grok_agent_message_flatten',
    'grok_gif_to_png', 'grok_web_search_preview_rename', 'local_gpu_max_tokens_clamp', 'log_truncate',
    'mock_heartbeat', 'null_byte_sanitize', 'observability_attempt_pusher', 'openrouter_provider_pin',
    'opus_47_fix', 'pricing_overlay', 'reasoning_effort_minimal_normalize', 'responses_aclose',
    'responses_tool_args_done_backfill', 'same_group_failover_cap', 'sitecustomize', 'streaming_bridge',
    'streaming_output_backfill', 'weighted_affinity',
})


def test_purpose_catalog_covers_live_modules():
    assert sorted(_LIVE_198_MODULES - set(patches_core.PURPOSE)) == []


def test_live_modules_exist_in_repo():
    """Each live module has its source in k8s/litellm-callbacks/ (git is a usable copy of live)."""
    from pathlib import Path

    repo_cb = Path(__file__).resolve().parents[5] / "k8s" / "litellm-callbacks"
    assert repo_cb.is_dir(), repo_cb
    assert sorted(m for m in _LIVE_198_MODULES if not (repo_cb / f"{m}.py").exists()) == []


def test_compare_lanes_flags_only_differences():
    p1 = {"lane": "prod", "targets": {"s": _t(patched_by=["a"], layers=["a", "litellm"]), "t": _t()}}
    p2 = {"lane": "gray", "targets": {"s": _t(patched_by=["a"], layers=["a", "litellm"]), "t": _t(missing="x")}}
    rows = {r["symbol"]: r for r in patches_core.compare_lanes([p1, p2])}
    assert rows["s"]["same"] and not rows["t"]["same"]


def _load_probe_helpers():
    """Load the in-pod probe's helper functions without running main() (it needs /app/config.yaml)."""
    text = resources.files("cli_anything.litellm.probes").joinpath("patch_targets.py").read_text()
    text = text.replace("\nmain()\n", "\n")
    spec = importlib.util.spec_from_loader("probe_under_test", loader=None)
    mod = importlib.util.module_from_spec(spec)
    exec(compile(text, "patch_targets.py", "exec"), mod.__dict__)
    return mod


def test_probe_layer_chain_follows_closures_and_wraps():
    probe = _load_probe_helpers()
    import functools
    import json as upstream_like  # a real stdlib function stands in for "the original"

    original = upstream_like.dumps

    def make_closure_wrapper(fn):
        def patched(*a, **k):
            return fn(*a, **k)
        patched.__module__ = "layer_closure"
        return patched

    def make_wraps_wrapper(fn):
        @functools.wraps(fn)
        def patched(*a, **k):
            return fn(*a, **k)
        return patched

    two = make_wraps_wrapper(make_closure_wrapper(original))
    chain = probe.layer_chain(two)
    # functools.wraps copied __module__ from the inner wrapper -> both layers report "layer_closure";
    # the point is the walk reaches the original (json) and stops.
    assert chain[0] == "layer_closure" and chain[-1] == "json"
    assert len(chain) == 3
