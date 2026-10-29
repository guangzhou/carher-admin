"""Unit tests: no network, no ssh."""

from __future__ import annotations

import json
import subprocess
from datetime import date, datetime, timezone

import pytest
from click.testing import CliRunner

from cli_anything.myai.core import diagnose, health, queries, upstream
from cli_anything.myai.core.remote import (
    Remote,
    RemoteConfig,
    RemoteError,
    parse_csv,
    psql_remote_command,
    ssh_argv,
)


# ---------------------------------------------------------------- remote

def test_ssh_argv_password_never_in_argv(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/sshpass")
    cfg = RemoteConfig(password="s3cr'et")
    argv = ssh_argv(cfg, "true")
    assert argv[:2] == ["sshpass", "-e"]
    assert not any("s3cr" in part for part in argv)


def test_ssh_argv_key_mode_is_batch():
    argv = ssh_argv(RemoteConfig(password=None), "true")
    assert argv[0] == "ssh" and "BatchMode=yes" in argv


def test_psql_command_forces_read_only():
    cmd = psql_remote_command(RemoteConfig())
    assert "default_transaction_read_only=on" in cmd
    assert "usage-db-0" in cmd and "--csv" in cmd


def _fake_runner(returncode=0, stdout="", stderr=""):
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    return runner, calls


def test_remote_error_on_psql_error_even_when_rc_zero():
    runner, _ = _fake_runner(0, "", "ERROR:  cannot execute INSERT in a read-only transaction")
    with pytest.raises(RemoteError, match="read-only"):
        Remote(RemoteConfig(), runner=runner).sql("SELECT 1")


def test_remote_password_goes_through_env(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/sshpass")
    runner, calls = _fake_runner(0, "a\n1\n")
    rows = Remote(RemoteConfig(password="pw'x"), runner=runner).sql("SELECT 1 AS a")
    assert rows == [{"a": "1"}]
    assert calls[0][1]["env"]["SSHPASS"] == "pw'x"
    assert calls[0][1]["input"].endswith(";\n")


def test_parse_csv_empty():
    assert parse_csv("") == []
    assert parse_csv("x,y\n1,2\n") == [{"x": "1", "y": "2"}]


# ---------------------------------------------------------------- queries

@pytest.mark.parametrize("stmt", ["SELECT 1", "with a as (select 1) select * from a", "EXPLAIN SELECT 1", "  select 1;  "])
def test_validate_adhoc_allows_reads(stmt):
    assert queries.validate_adhoc(stmt)


@pytest.mark.parametrize("stmt", [
    "DELETE FROM usage_daily",
    "update billing_order set status='paid'",
    "INSERT INTO x VALUES (1)",
    "SELECT 1; DROP TABLE usage_daily",
    "\\copy usage_daily to '/tmp/x'",
    "",
])
def test_validate_adhoc_rejects_writes(stmt):
    with pytest.raises(ValueError):
        queries.validate_adhoc(stmt)


def test_identity_find_escapes_quotes():
    sql = queries.identity_find("o'brien")
    assert "'%o''brien%'" in sql


def test_billing_status_literal_escaped():
    assert "status = 'a''b'" in queries.billing_orders("a'b", 10)


def test_day_window_is_clamped():
    assert "interval '3650 days'" in queries.sync_runs_by_day(10**9)
    assert "interval '1 days'" in queries.sync_runs_by_day(-5)


# ---------------------------------------------------------------- diagnose (gate parity with usage_sync.py)

def test_gate_formula_matches_source():
    assert diagnose.expected_nonempty(305) == 15
    assert diagnose.expected_nonempty(10) == 1  # max(1, ...)
    assert diagnose.is_sparse(305, 2)
    assert not diagnose.is_sparse(305, 15)
    assert not diagnose.is_sparse(19, 0)  # below 20 attempted users the gate is off
    assert diagnose.is_sparse(20, 0)  # zero-traffic day still blocked


def test_gate_report_verdict():
    report = diagnose.gate_report(305, [
        {"date": "2026-09-20", "activeUsers": 120, "requests": 23152},
        {"date": "2026-10-08", "activeUsers": 1, "requests": 8},
    ])
    assert report["needNonEmptyUsers"] == 15
    assert report["blockedDays"] == ["2026-10-08"]
    assert "1/2" in report["verdict"]


def test_gate_report_nothing_blocked():
    report = diagnose.gate_report(305, [{"date": "d", "activeUsers": 50}])
    assert report["blockedDays"] == [] and "另有原因" in report["verdict"]


def test_her_gate_script_embeds_days():
    script = diagnose.her_gate_script(["2026-10-01"])
    assert "2026-10-01" in script and "__PARAMS__" not in script
    compile(script, "<her_gate>", "exec")


# ---------------------------------------------------------------- upstream

def test_day_list_order_and_bounds():
    assert upstream.day_list(3, date(2026, 10, 13)) == ["2026-10-11", "2026-10-12", "2026-10-13"]
    assert len(upstream.day_list(1000)) == 90


def test_daily_script_rejects_unknown_backend():
    with pytest.raises(ValueError):
        upstream.daily_activity_script(["nope"], ["2026-10-01"])
    compile(upstream.daily_activity_script(["her"], ["2026-10-01"]), "<daily>", "exec")


def test_parse_script_output_skips_warnings():
    assert upstream.parse_script_output('WARNING x\n{"a": 1}\n') == {"a": 1}
    with pytest.raises(ValueError):
        upstream.parse_script_output("Traceback ...")


# ---------------------------------------------------------------- health

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)

STUCK = {
    "status": "degraded",
    "usageSync": {"workerId": "usage-sync-worker-x:1", "heartbeatLagSeconds": 17, "lastSuccessAt": "2026-09-29T16:35:35+00:00"},
    "organization": {
        "errors": {"organizations": {"statusCode": 403, "detail": "Organizations are only available for LiteLLM Enterprise users."}},
        "outbox": {"pendingCount": 0, "failedCount": 4},
        "settlement": {"reconciliationDifferenceCount": 33, "reconciliationDifferenceUsd": 16458.9},
    },
    "usageRefreshQueue": {"pendingCount": 10, "oldestAgeSeconds": 1122325, "maxAttempts": 760, "lastError": "her: 用户 daily activity 返回非空用户过少"},
    "usageRealtime": {"status": "ok", "maxSettlementLagSeconds": 92},
    "billing": {"pendingSyncCount": 1, "pendingReviewCount": 0},
    "usageDatabase": {"connected": True},
}


def test_health_summary_flags_stuck_sync():
    checks = {f["check"]: f for f in health.summarize(STUCK, NOW)["findings"]}
    assert checks["organization"]["level"] == "known"
    assert "usageSync.lastSuccessAt" in checks
    assert "非空用户过少" in checks["usageRefreshQueue"]["detail"]
    assert "organization.outbox" in checks and "organization.settlement" in checks and "billing" in checks
    assert "usageRealtime" not in checks


def test_health_summary_clean():
    clean = {"status": "ok", "usageSync": {"lastSuccessAt": "2026-10-13T11:00:00+00:00"}, "usageRealtime": {"status": "ok", "maxSettlementLagSeconds": 30}}
    assert health.summarize(clean, NOW)["findings"] == []


def test_health_unknown_org_error_is_warn():
    body = {"organization": {"errors": {"teams": {"statusCode": 500}}}}
    assert health.summarize(body, NOW)["findings"][0]["level"] == "warn"


# ---------------------------------------------------------------- CLI wiring

class FakeRemote:
    def __init__(self):
        self.sql_calls = []

    def sql(self, statement):
        self.sql_calls.append(statement)
        return [{"ok": "1"}]


def _invoke(args, fake):
    from cli_anything.myai import myai_cli

    runner = CliRunner()
    original = myai_cli.Remote
    myai_cli.Remote = lambda config: fake
    try:
        return runner.invoke(myai_cli.cli, args)
    finally:
        myai_cli.Remote = original


def test_cli_db_sql_write_rejected_locally():
    fake = FakeRemote()
    result = _invoke(["db", "sql", "DELETE FROM usage_daily"], fake)
    assert result.exit_code != 0 and fake.sql_calls == []


def test_cli_json_output_parses():
    fake = FakeRemote()
    result = _invoke(["--json", "queue"], fake)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == [{"ok": "1"}]
    assert "usage_refresh_requests" in fake.sql_calls[0]


def test_usage_daily_reads_dashboard_view_by_default():
    assert "FROM usage_query_daily" in queries.usage_daily_totals(3)
    assert "FROM usage_daily\n" in queries.usage_daily_totals(3, snapshot_only=True)


def test_truncation_report_flags_cap_fingerprint():
    up = [
        {"date": "2026-10-08", "requests": 123284, "spend": 22222.84},
        {"date": "2026-10-09", "requests": 153680, "spend": 26059.78},
        {"date": "2026-10-11", "requests": 57616, "spend": 11170.43},
    ]
    view = [
        {"usage_date": "2026-10-08", "requests": "10000", "spend": "1277.98"},
        {"usage_date": "2026-10-09", "requests": "132238", "spend": "23459.05"},
        {"usage_date": "2026-10-11", "requests": "65671", "spend": "14033.26"},
    ]
    report = diagnose.truncation_report(up, view)
    assert report["truncatedDays"] == ["2026-10-08"]
    assert report["missingSpendOnTruncatedDays"] == round(22222.84 - 1277.98, 2)


def test_truncation_report_ignores_tiny_real_days():
    # a genuinely small day equal to 10000 but with upstream not larger is not flagged
    report = diagnose.truncation_report([{"date": "d", "requests": 10000, "spend": 5}], [{"usage_date": "d", "requests": "10000", "spend": "5"}])
    assert report["truncatedDays"] == []


# ---------------------------------------------------------------- 10-14 additions

def test_coverage_report_hints():
    up = [
        {"date": "2026-10-10", "spend": 26618.83},
        {"date": "2026-10-11", "spend": 11170.43},
        {"date": "2026-10-12", "spend": 28550.79},
        {"date": "2026-10-13", "spend": 31829.93},
        {"date": "2026-10-14", "spend": 0},
    ]
    shown = [
        {"usage_date": "2026-10-10", "attribution_source": "unattributed", "requests": "10000", "spend": "1867.88"},
        {"usage_date": "2026-10-11", "attribution_source": "unattributed", "requests": "39501", "spend": "9657.89"},
        {"usage_date": "2026-10-12", "attribution_source": "unattributed", "requests": "114441", "spend": "23703.62"},
        {"usage_date": "2026-10-12", "attribution_source": "key_rollup", "requests": "21458", "spend": "4847.16"},
    ]
    r = diagnose.coverage_report(up, shown)
    hints = {d["date"]: d["hint"] for d in r["days"]}
    assert "10000" in hints["2026-10-10"]
    assert "key_rollup" in hints["2026-10-11"]
    assert hints["2026-10-12"] == "ok"
    assert "0" in hints["2026-10-13"] and "below daily rollup" in hints["2026-10-13"]
    assert hints["2026-10-14"] == ""
    assert r["flaggedDays"] == ["2026-10-10", "2026-10-11", "2026-10-13"]


def test_summarize_keys_classes():
    data = {"start": "a", "end": "b", "users": 2, "totals": {"requests": 10, "spend": 6.0}, "keys": [
        {"keyId": "1", "alias": "alice", "owner": "alice", "class": "known", "requests": 5, "spend": 3.0},
        {"keyId": "2", "alias": "carher-hangzhou", "owner": "", "class": "no_owner", "requests": 4, "spend": 2.0},
        {"keyId": "3", "alias": "x-std", "owner": "x-std", "class": "owner_not_in_usertable", "requests": 1, "spend": 1.0},
    ]}
    s = upstream.summarize_keys(data)
    assert s["byClass"]["no_owner"]["spend"] == 2.0
    assert [r["alias"] for r in s["invisibleToPerUserSync"]] == ["carher-hangzhou", "x-std"]
    compile(upstream.keys_script("2026-10-01", "2026-10-07"), "<keys>", "exec")


def test_probe_target_default_and_override():
    from cli_anything.myai.core.remote import PROBE_POD, probe_pod_manifest, worker_python_command
    assert "deploy/usage-sync-worker" in worker_python_command(RemoteConfig())
    assert f"pod/{PROBE_POD}" in worker_python_command(RemoteConfig(probe_target=f"pod/{PROBE_POD}"))
    m = json.loads(probe_pod_manifest("img:tag", "2Gi", 60))
    c = m["spec"]["containers"][0]
    assert c["resources"]["limits"]["memory"] == "2Gi" and c["command"] == ["sleep", "60"]
    assert m["spec"]["restartPolicy"] == "Never"
    assert {e.get("configMapRef", e.get("secretRef"))["name"] for e in c["envFrom"]} == {"dashboard-config", "dashboard-secrets"}



def test_health_server_known_limitation_marker_is_known():
    """10-29 起服务端在 organization.knownLimitation 标出许可限制（补丁 0014）。"""
    body = {
        "status": "ok",
        "organization": {
            "status": "unavailable",
            "knownLimitation": "upstream_enterprise_license_missing",
            "errors": {"organizations": {"statusCode": 403, "detail": "上游接口失败：Organizations are only available for LiteLLM Enterprise users."}},
        },
    }
    findings = health.summarize(body, NOW)["findings"]
    assert [f["level"] for f in findings if f["check"] == "organization"] == ["known"]


def test_health_rejected_admin_key_is_not_known():
    """阴性对照：10-28 旧 master key 三项全 403 ⇒ warn，不能被当成常驻许可限制。"""
    detail = {"statusCode": 403, "detail": "管理员密钥无权限或已失效"}
    body = {"status": "degraded", "organization": {"errors": {"organizations": detail, "teams": detail, "keys": detail}}}
    assert health.summarize(body, NOW)["findings"][0]["level"] == "warn"


def test_health_normal_settlement_lag_is_not_flagged():
    """正常 60~140s 滞后不报；超过 180s 才报（与服务端阈值一致）。"""
    ok = {"status": "ok", "usageRealtime": {"status": "ok", "maxSettlementLagSeconds": 140}}
    assert not [f for f in health.summarize(ok, NOW)["findings"] if f["check"] == "usageRealtime"]
    slow = {"status": "ok", "usageRealtime": {"status": "ok", "maxSettlementLagSeconds": 240}}
    assert [f for f in health.summarize(slow, NOW)["findings"] if f["check"] == "usageRealtime"]



def test_health_running_queue_item_is_not_backlog():
    """10-29 实测：pendingCount=0 runningCount=1 oldestAge=4582s ⇒ 只是正在跑，不是积压。"""
    body = {"status": "ok", "usageRefreshQueue": {"pendingCount": 0, "runningCount": 1, "oldestAgeSeconds": 4582, "maxAttempts": 4}}
    assert not [f for f in health.summarize(body, NOW)["findings"] if f["check"] == "usageRefreshQueue"]
    stuck = {"status": "ok", "usageRefreshQueue": {"pendingCount": 2, "runningCount": 0, "oldestAgeSeconds": 90000, "maxAttempts": 120}}
    assert [f for f in health.summarize(stuck, NOW)["findings"] if f["check"] == "usageRefreshQueue"]


def test_health_never_settled_org_is_known_not_mismatch():
    body = {"status": "ok", "organization": {"settlement": {"settledDayCount": 0, "reconciliationDifferenceCount": 55, "reconciliationDifferenceUsd": 23832.79}}}
    f = [x for x in health.summarize(body, NOW)["findings"] if x["check"] == "organization.settlement"]
    assert f and f[0]["level"] == "known"
    real = {"status": "ok", "organization": {"settlement": {"settledDayCount": 30, "reconciliationDifferenceCount": 2, "reconciliationDifferenceUsd": 10.0}}}
    assert [x for x in health.summarize(real, NOW)["findings"] if x["check"] == "organization.settlement"][0]["level"] == "warn"
