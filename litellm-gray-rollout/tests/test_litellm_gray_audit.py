"""Offline contract tests for LiteLLM gray-rollout evidence tools."""

from __future__ import annotations

import json
import copy
import hashlib
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "litellm-gray-rollout" / "scripts"
RUNBOOK = ROOT / "litellm-gray-rollout" / "docs" / "litellm-198-gray-rollout-runbook.md"


def run_tool(name: str, payload: object) -> tuple[subprocess.CompletedProcess[str], dict]:
    # Existing contract fixtures are wrapped in the same strict evidence
    # envelope used by the production CLI.
    if (
        name == "metrics.py"
        and isinstance(payload, dict)
        and payload.get("rollout_percent", 0) > 0
        and "spend_reconciliation" not in payload
    ):
        payload = {
            **payload,
            "spend_reconciliation": {
                "expected_request_ids": ["r-001"],
                "terminal_request_ids": ["r-001"],
                "failed_request_ids": [],
                "observed_lag_seconds": 1,
            },
        }
    if name == "audit-runtime.py" and isinstance(payload, dict):
        _refresh_runtime_sources(payload)
    if isinstance(payload, dict) and "evidence" not in payload:
        payload = _with_evidence(payload)
    result = subprocess.run(
        [sys.executable, str(TOOLS / name), "--input", "-"],
        input=json.dumps(payload),
        capture_output=True,
        check=False,
        cwd=ROOT,
        text=True,
    )
    output = json.loads(result.stdout)
    return result, output


def _load_prepare_values():
    """Reuse prepare-values.py's encoder instead of keeping a copy of it here.

    A contract whose whole purpose is to equal Helm's `toRawJson` must have
    exactly one implementation. The copy that used to live here hard-coded
    `ensure_ascii=True`, which is invisible against ASCII-only fixtures and
    unconditionally fatal against production content (30 of prod's 33 callbacks
    are non-ASCII). See docs/prepare-values-prod-rehearsal-2026-09-13.md §6.2.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "prepare_values_for_audit_tests", TOOLS / "prepare-values.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_PREPARE_VALUES = _load_prepare_values()


def _payload_digest(payload: dict) -> str:
    canonical = {key: value for key, value in payload.items() if key != "evidence"}
    return _PREPARE_VALUES.raw_json_sha256(canonical)


def _value_digest(value: object) -> str:
    return _PREPARE_VALUES.raw_json_sha256(value)


def _refresh_runtime_sources(payload: dict) -> None:
    for item in payload.get("sources", []):
        section = item.get("section")
        if section in payload:
            item["payload_sha256"] = _value_digest(payload[section])


def _with_evidence(
    payload: dict,
    *,
    captured_at: datetime | None = None,
    checksum: str | None = None,
    run_id: str | None = "run-test",
    generation: str | None = "generation-test",
    config_checksum: str | None = "test-mode",
) -> dict:
    result = copy.deepcopy(payload)
    captured_at = captured_at or datetime.now(timezone.utc)
    evidence = {
        "schema_version": 1,
        "captured_at": captured_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "payload_sha256": checksum or _payload_digest(result),
    }
    if run_id is not None:
        evidence["run_id"] = run_id
    if generation is not None:
        evidence["generation"] = generation
    if config_checksum is not None:
        evidence["config_checksum"] = config_checksum
    result["evidence"] = evidence
    return result


def run_strict_tool(name: str, payload: dict) -> tuple[subprocess.CompletedProcess[str], dict]:
    if name == "audit-runtime.py":
        _refresh_runtime_sources(payload)
    wrapped = _with_evidence(payload)
    if name == "audit-runtime.py":
        captured_at = datetime.fromisoformat(
            wrapped["evidence"]["captured_at"].replace("Z", "+00:00")
        )
        for item in wrapped.get("sources", []):
            source_time = datetime.fromisoformat(item["captured_at"].replace("Z", "+00:00"))
            item["freshness_seconds"] = max(
                0, round((captured_at - source_time).total_seconds(), 3)
            )
        wrapped["evidence"]["payload_sha256"] = _payload_digest(wrapped)
    return run_tool_raw(name, wrapped)


def run_tool_raw(name: str, payload: object) -> tuple[subprocess.CompletedProcess[str], dict]:
    result = subprocess.run(
        [sys.executable, str(TOOLS / name), "--input", "-"],
        input=json.dumps(payload),
        capture_output=True,
        check=False,
        cwd=ROOT,
        text=True,
    )
    output = json.loads(result.stdout)
    return result, output


def runtime_payload() -> dict:
    captured_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    callback_names = ["streaming_output_backfill", "weighted_affinity"]
    surfaces = ["chat", "responses", "images"]
    acct_fields = ["deployments", "services", "active_ready", "quota_take", "registered", "recent_requests"]
    def mutation(operation: str, writer: str, reader: str, request_id: str) -> dict:
        return {
            "operation": operation,
            "writer": writer,
            "reader": reader,
            "request_id": request_id,
            "written_at": captured_at,
            "observed_at": captured_at,
            "latency_ms": 0,
            "sla_ms": 5000,
            "result_sha256": "sha256:" + "d" * 64,
            "status": "PASS",
        }
    scheduler = {
        "mode": "disabled",
        "profile": "gray",
        "image_digest": "sha256:" + "a" * 64,
        "config_sha256": "sha256:" + "b" * 64,
        "callbacks_sha256": "sha256:" + "c" * 64,
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": "clone:gray-scheduler-observation",
        "observations": {
            "duplicate_scheduler_runs": 0,
            "duplicate_background_jobs": 0,
            "unexpected_control_writes": 0,
        },
    }
    scheduler["source_payload_sha256"] = _value_digest(scheduler)
    payload = {
        "expected_inventory": {
            "callbacks": callback_names,
            "api_surfaces": surfaces,
            "acct_fields": acct_fields,
            "mutation_operations": {
                "stable_to_gray": ["model_new"],
                "gray_to_stable": ["key_update"],
            },
        },
        "references": [
            {
                "source": "scripts/update-model.py",
                "text": "POST http://10.68.13.198:30402/model/new",
            },
            {
                "source": "scripts/model-audit.py",
                "text": "GET http://10.68.13.198:30402/model/info",
            },
            {
                "source": "scripts/probe.sh",
                "text": "POST http://10.68.13.198:30402/v1/chat/completions",
            },
        ],
        "callbacks": [
            {
                "name": name, "import_ok": True, "behavior_ok": True,
                "probe_sha256": "sha256:" + "1" * 64,
                "image_digest": "sha256:" + "a" * 64,
                "config_sha256": "sha256:" + "b" * 64,
                "captured_at": captured_at,
            }
            for name in callback_names
        ],
        "api_surfaces": {
            "expected": ["chat", "responses", "images"],
            "observed": ["images", "responses", "chat"],
            "smoke": {
                name: {
                    "status": "PASS", "request_id": f"req-{name}",
                    "captured_at": captured_at, "response_sha256": "sha256:" + "2" * 64,
                }
                for name in surfaces
            },
        },
        "acct": {
            "deployments": ["acct-01", "acct-02"],
            "services": ["acct-01", "acct-02"],
            "active_ready": ["acct-01"],
            "quota_take": ["acct-01"],
            "registered": ["acct-01"],
            "recent_requests": ["acct-01"],
            "explanations": {},
        },
        "redis": {"compatible": True},
        "scheduler": scheduler,
        "mutation_visibility": {
            "stable_to_gray": mutation("model_new", "stable", "gray", "req-stable-gray"),
            "gray_to_stable": mutation("key_update", "gray", "stable", "req-gray-stable"),
            "freeze": False,
        },
    }
    payload["sources"] = [
        {
            "section": section,
            "source": f"fixture:{section}",
            "captured_at": captured_at,
            "freshness_seconds": 0,
            "payload_sha256": _value_digest(payload[section]),
        }
        for section in (
            "expected_inventory",
            "references",
            "callbacks",
            "api_surfaces",
            "acct",
            "redis",
            "scheduler",
            "mutation_visibility",
        )
    ]
    return payload


def migration_payload() -> dict:
    change = 'ALTER TABLE "LiteLLM_ProxyModelTable" ADD COLUMN "dcr_bridge" JSONB DEFAULT \'{}\''
    return {
        "schema": {
            "before_checksum": "sha256:" + "1" * 64,
            "after_checksum": "sha256:" + "2" * 64,
            "expected_after_checksum": "sha256:" + "2" * 64,
            "changes": [change],
        },
        "ddl_ledger": {
            "partial_state": "none",
            "lock_timeout_ms": 5000,
            "statement_timeout_ms": 120000,
            "migration_duration_ms": 12,
            "workload_p95_ratio": 1.01,
            "network_policy": {
                "allowed_probe": "PASS",
                "denied_probe": "PASS",
            },
            "snapshot_counts": {
                "expected": {"SpendLogs": 845870, "VerificationToken": 1322},
                "restored": {"SpendLogs": 845870, "VerificationToken": 1322},
            },
            "entries": [
                {
                    "id": "ddl-001",
                    "statement": change,
                    "status": "completed",
                    "online_safe": True,
                    "observed_in_schema": True,
                    "duration_ms": 12,
                    "lock_wait_ms": 0,
                    "lock_mode": "ACCESS EXCLUSIVE",
                    "table_rewrite": False,
                }
            ],
        },
        "thresholds": {
            "max_migration_duration_ms": 900000,
            "max_workload_p95_ratio": 1.20,
            "max_lock_wait_ms": 5000,
        },
        "compatibility": {
            "A": {"status": "PASS"},
            "B": {"status": "PASS"},
            "C": {"status": "PASS"},
        },
    }


def metric_records(pool: str, uri_class: str, count: int, status: int, latency: float) -> list[dict]:
    return [
        {
            "pool_label": pool,
            "uri_class": uri_class,
            "status": status,
            "response_time": latency,
        }
        for _ in range(count)
    ]


def test_audit_runtime_classifies_bypasses_and_passes_closed_sets():
    result, output = run_tool("audit-runtime.py", runtime_payload())

    assert result.returncode == 0, result.stderr
    assert output["tool"] == "audit-runtime"
    assert output["status"] == "PASS"
    assert output["bypass_summary"]["counts"] == {
        "control_read": 1,
        "control_write": 1,
        "inference_probe": 1,
    }
    assert output["callbacks"]["count"] == 2
    assert output["acct"]["unexplained_differences"] == []


def test_audit_runtime_fails_on_unexplained_drift_and_never_echoes_secrets():
    payload = runtime_payload()
    payload["references"][0]["text"] += " Authorization: Bearer sk-secret-value"
    payload["callbacks"][0]["import_ok"] = False
    payload["acct"]["services"] = ["acct-01"]

    result, output = run_tool("audit-runtime.py", payload)
    rendered = json.dumps(output, sort_keys=True)

    assert result.returncode == 1
    assert output["status"] == "FAIL"
    assert "CALLBACK_EVIDENCE_INVALID" in output["reason_codes"]
    assert "ACCT_SET_DRIFT" in output["reason_codes"]
    assert output["acct"]["unexplained_differences"]
    assert "sk-secret-value" not in rendered
    assert "Authorization" not in rendered


def test_audit_runtime_accepts_documented_mutation_freeze_decision():
    payload = runtime_payload()
    payload["mutation_visibility"]["stable_to_gray"]["status"] = "FAIL"
    payload["mutation_visibility"]["gray_to_stable"]["status"] = "FAIL"
    payload["mutation_visibility"]["freeze"] = True
    payload["sources"] = [
        item if item["section"] != "mutation_visibility"
        else {**item, "payload_sha256": _value_digest(payload["mutation_visibility"])}
        for item in payload["sources"]
    ]

    result, output = run_tool("audit-runtime.py", payload)

    assert result.returncode == 1
    assert output["status"] == "FAIL"
    assert output["mutation_visibility"]["decision"] == "freeze"


def test_audit_runtime_explanations_close_known_acct_set_differences():
    payload = runtime_payload()
    payload["acct"]["registered"] = ["acct-01", "acct-02"]
    payload["acct"]["explanations"] = {
        "acct-02": "registered standby intentionally scaled down"
    }

    result, output = run_tool("audit-runtime.py", payload)

    assert result.returncode == 0
    assert output["status"] == "PASS"
    assert output["acct"]["unexplained_differences"] == []


def test_audit_runtime_requires_smoke_for_every_expected_surface():
    payload = runtime_payload()
    payload["api_surfaces"]["smoke"].pop("images")
    payload["sources"] = [
        item
        if item["section"] != "api_surfaces"
        else {**item, "payload_sha256": _value_digest(payload["api_surfaces"])}
        for item in payload["sources"]
    ]

    result, output = run_strict_tool("audit-runtime.py", payload)

    assert result.returncode == 1
    assert "API_SMOKE_COVERAGE_MISSING" in output["reason_codes"]


def test_audit_runtime_rejects_string_booleans_and_missing_mutation_direction():
    for section, key in (("redis", "compatible"), ("scheduler", "safe")):
        payload = runtime_payload()
        payload[section][key] = "false"
        payload["sources"] = [
            item
            if item["section"] != section
            else {**item, "payload_sha256": _value_digest(payload[section])}
            for item in payload["sources"]
        ]
        result, output = run_strict_tool("audit-runtime.py", payload)
        assert result.returncode == 1
        assert f"{section.upper()}_EVIDENCE_INVALID" in output["reason_codes"]

    payload = runtime_payload()
    del payload["mutation_visibility"]["gray_to_stable"]
    payload["sources"] = [
        item
        if item["section"] != "mutation_visibility"
        else {**item, "payload_sha256": _value_digest(payload["mutation_visibility"])}
        for item in payload["sources"]
    ]
    result, output = run_strict_tool("audit-runtime.py", payload)
    assert result.returncode == 1
    assert "MUTATION_EVIDENCE_INVALID" in output["reason_codes"]

    payload = runtime_payload()
    payload["callbacks"][0]["import_ok"] = "false"
    result, output = run_strict_tool("audit-runtime.py", payload)
    assert result.returncode == 1
    assert "CALLBACK_EVIDENCE_INVALID" in output["reason_codes"]

    payload = runtime_payload()
    payload["references"][0]["direct_30402"] = "false"
    result, output = run_strict_tool("audit-runtime.py", payload)
    assert result.returncode == 1
    assert "BYPASS_REFERENCE_INVALID" in output["reason_codes"]


def test_audit_runtime_rejects_opaque_scheduler_self_attestation():
    payload = runtime_payload()
    payload["scheduler"] = {"safe": True}
    result, output = run_strict_tool("audit-runtime.py", payload)

    assert result.returncode == 1
    assert "SCHEDULER_EVIDENCE_INVALID" in output["reason_codes"]


def test_audit_runtime_requires_observed_proof_for_disabled_scheduler():
    payload = runtime_payload()
    result, output = run_strict_tool("audit-runtime.py", payload)
    assert result.returncode == 0, result.stderr
    assert output["status"] == "PASS"

    payload["scheduler"]["observations"]["duplicate_background_jobs"] = 1
    source_payload = {
        key: value
        for key, value in payload["scheduler"].items()
        if key != "source_payload_sha256"
    }
    payload["scheduler"]["source_payload_sha256"] = _value_digest(source_payload)
    result, output = run_strict_tool("audit-runtime.py", payload)
    assert result.returncode == 1
    assert "SCHEDULER_UNSAFE" in output["reason_codes"]


def test_audit_runtime_rejects_stale_or_tampered_individual_sources():
    payload = runtime_payload()
    payload["sources"][0]["captured_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).isoformat().replace("+00:00", "Z")
    result, output = run_strict_tool("audit-runtime.py", payload)
    assert result.returncode == 1
    assert "RUNTIME_SOURCE_STALE" in output["reason_codes"]

    payload = runtime_payload()
    payload["sources"][0]["payload_sha256"] = "sha256:" + "0" * 64
    result, output = run_tool_raw("audit-runtime.py", _with_evidence(payload))
    assert result.returncode == 1
    assert "RUNTIME_SOURCE_CHECKSUM_MISMATCH" in output["reason_codes"]


def test_check_migration_passes_accounted_additive_path_a():
    result, output = run_tool("check-migration.py", migration_payload())

    assert result.returncode == 0, result.stderr
    assert output["tool"] == "check-migration"
    assert output["status"] == "PASS"
    assert output["path"] == "A"
    assert output["schema"]["change_count"] == 1
    assert output["ddl_ledger"]["completed_ids"] == ["ddl-001"]
    assert set(output["compatibility"]) == {"A", "B", "C"}
    assert "statement" not in json.dumps(output)


def test_check_migration_rejects_destructive_partial_or_incomplete_evidence():
    payload = migration_payload()
    destructive = 'ALTER TABLE "LiteLLM_ProxyModelTable" DROP COLUMN "model_name"'
    payload["schema"]["changes"] = [destructive]
    payload["ddl_ledger"]["entries"][0]["statement"] = destructive
    payload["ddl_ledger"]["partial_state"] = "unknown"
    del payload["compatibility"]["C"]

    result, output = run_tool("check-migration.py", payload)

    assert result.returncode == 1
    assert output["status"] == "FAIL"
    assert {
        "DESTRUCTIVE_DDL",
        "PARTIAL_DDL_UNKNOWN",
        "COMPATIBILITY_RESULT_MISSING",
    }.issubset(output["reason_codes"])


def test_check_migration_rejects_unaccounted_schema_change_and_set_not_null():
    payload = migration_payload()
    payload["schema"]["changes"] = [
        'ALTER TABLE "LiteLLM_ProxyModelTable" ALTER COLUMN "model_name" SET NOT NULL'
    ]

    result, output = run_tool("check-migration.py", payload)

    assert result.returncode == 1
    assert output["status"] == "FAIL"
    assert "DESTRUCTIVE_DDL" in output["reason_codes"]
    assert "DDL_SCHEMA_RECONCILIATION_FAILED" in output["reason_codes"]


def test_check_migration_selects_path_b_only_for_empty_diff_and_ledger():
    payload = migration_payload()
    payload["schema"]["after_checksum"] = payload["schema"]["before_checksum"]
    payload["schema"]["expected_after_checksum"] = payload["schema"]["before_checksum"]
    payload["schema"]["changes"] = []
    payload["ddl_ledger"]["entries"] = []

    result, output = run_tool("check-migration.py", payload)

    assert result.returncode == 0
    assert output["status"] == "PASS"
    assert output["path"] == "B"


def test_check_migration_rejects_online_unsafe_or_failed_clone_result():
    payload = migration_payload()
    payload["ddl_ledger"]["entries"][0]["online_safe"] = False
    payload["compatibility"]["B"] = {"status": "FAIL"}

    result, output = run_tool("check-migration.py", payload)

    assert result.returncode == 1
    assert {"ONLINE_DDL_UNSAFE", "COMPATIBILITY_RESULT_FAILED"}.issubset(
        output["reason_codes"]
    )


def test_check_migration_rejects_exceeded_thresholds_and_table_rewrite():
    payload = migration_payload()
    payload["ddl_ledger"]["migration_duration_ms"] = 900001
    payload["ddl_ledger"]["workload_p95_ratio"] = 1.21
    payload["ddl_ledger"]["entries"][0]["lock_wait_ms"] = 5001
    payload["ddl_ledger"]["entries"][0]["table_rewrite"] = True

    result, output = run_tool("check-migration.py", payload)

    assert result.returncode == 1
    assert {
        "MIGRATION_DURATION_THRESHOLD_EXCEEDED",
        "WORKLOAD_P95_THRESHOLD_EXCEEDED",
        "DDL_LOCK_WAIT_THRESHOLD_EXCEEDED",
        "DDL_TABLE_REWRITE_DETECTED",
    }.issubset(output["reason_codes"])


def test_check_migration_requires_bound_thresholds_and_rejects_unapproved_locks():
    payload = migration_payload()
    del payload["thresholds"]
    result, output = run_strict_tool("check-migration.py", payload)
    assert result.returncode == 2
    assert "MIGRATION_THRESHOLDS_MISSING" in output["reason_codes"]

    payload = migration_payload()
    payload["ddl_ledger"]["entries"][0]["lock_mode"] = "SHARE UPDATE EXCLUSIVE"
    result, output = run_strict_tool("check-migration.py", payload)
    assert result.returncode == 1
    assert "DDL_UNAPPROVED_LOCK_MODE" in output["reason_codes"]


def test_check_migration_rejects_any_partial_ddl_state():
    payload = migration_payload()
    payload["ddl_ledger"]["partial_state"] = "reconciled"
    result, output = run_strict_tool("check-migration.py", payload)
    assert result.returncode == 1
    assert "PARTIAL_DDL_DETECTED" in output["reason_codes"]


def test_metrics_recommends_rollback_for_qualified_normal_gray_breach():
    records = metric_records("stable", "chat", 100, 200, 0.1)
    records += metric_records("canary", "chat", 100, 500, 0.2)
    payload = {
        "phase": "normal_gray",
        "rollout_percent": 10,
        "records": records,
        "hard_errors": {},
        "backend_health": {"gray": True, "prod": True},
        "spend_reconciliation": {
            "expected_request_ids": ["r-001"],
            "terminal_request_ids": ["r-001"],
            "failed_request_ids": [],
            "observed_lag_seconds": 1,
        },
    }

    result, output = run_tool("metrics.py", payload)

    assert result.returncode == 1
    assert output["tool"] == "metrics"
    assert output["status"] == "FAIL"
    assert output["dispatcher_recommendation"]["action"] == "rollback"
    assert output["dispatcher_recommendation"]["hard_trigger"] is True
    assert "FIVE_XX_DELTA" in output["dispatcher_recommendation"]["reason_codes"]
    assert output["groups"] == sorted(
        output["groups"], key=lambda group: (group["pool_label"], group["uri_class"])
    )


def test_metrics_sample_guard_alerts_without_triggering_rollback():
    records = metric_records("stable", "responses", 500, 200, 0.1)
    records += metric_records("canary", "responses", 99, 500, 2.0)
    payload = {
        "phase": "normal_gray",
        "rollout_percent": 5,
        "records": records,
        "hard_errors": {},
        "backend_health": {"gray": True, "prod": True},
    }

    result, output = run_tool("metrics.py", payload)

    assert result.returncode == 0
    assert output["status"] == "PASS"
    assert output["dispatcher_recommendation"] == {
        "action": "alert_only",
        "hard_trigger": False,
        "reason_codes": ["INSUFFICIENT_GRAY_SAMPLE"],
    }


def test_metrics_uses_frozen_baseline_at_100_percent_not_prod():
    records = metric_records("canary", "images", 100, 200, 0.4)
    records += metric_records("stable", "images", 100, 200, 99.0)
    payload = {
        "phase": "normal_gray",
        "rollout_percent": 100,
        "records": records,
        "baseline": [
            {
                "uri_class": "images",
                "count": 1000,
                "five_xx_rate": 0.0,
                "p95": 0.1,
                "p99": 0.1,
            }
        ],
        "hard_errors": {},
        "backend_health": {"gray": True, "prod": True},
    }

    result, output = run_tool("metrics.py", payload)

    assert result.returncode == 1
    assert output["comparison_mode"] == "baseline"
    assert output["dispatcher_recommendation"]["action"] == "rollback"
    assert "P95_RATIO" in output["dispatcher_recommendation"]["reason_codes"]
    assert all(item["reference"] == "baseline" for item in output["comparisons"])


def test_metrics_never_recommends_rollback_to_an_unhealthy_prod():
    records = metric_records("stable", "chat", 100, 200, 0.1)
    records += metric_records("canary", "chat", 100, 500, 0.2)
    payload = {
        "phase": "normal_gray",
        "rollout_percent": 10,
        "records": records,
        "hard_errors": {},
        "backend_health": {"gray": False, "prod": False},
        "spend_reconciliation": {
            "expected_request_ids": ["r-001"],
            "terminal_request_ids": ["r-001"],
            "failed_request_ids": [],
            "observed_lag_seconds": 1,
        },
    }

    result, output = run_tool("metrics.py", payload)

    assert result.returncode == 1
    assert output["status"] == "FAIL"
    assert output["dispatcher_recommendation"]["action"] == "alert_only"
    assert output["dispatcher_recommendation"]["hard_trigger"] is True
    assert "PROD_NOT_HEALTHY_FOR_ROLLBACK" in output["dispatcher_recommendation"]["reason_codes"]


def test_metrics_aborts_to_bridge_for_hard_error_while_prod_is_offline():
    payload = {
        "phase": "prod_offline_upgrading",
        "rollout_percent": 100,
        "records": metric_records("canary", "chat", 10, 200, 0.1),
        "hard_errors": {"callback_import_error": 1},
        "backend_health": {"prod": False, "gray": False, "bridge": True},
    }

    result, output = run_tool("metrics.py", payload)

    assert result.returncode == 1
    assert output["dispatcher_recommendation"] == {
        "action": "abort_to_bridge",
        "hard_trigger": True,
        "reason_codes": ["CALLBACK_IMPORT_ERROR"],
    }


def test_metrics_holds_gray_when_prod_is_offline_and_gray_is_healthy():
    payload = {
        "phase": "prod_offline_upgrading",
        "rollout_percent": 100,
        "records": metric_records("canary", "chat", 100, 200, 0.1),
        "baseline": [
            {"uri_class": "chat", "count": 1000, "five_xx_rate": 0, "p95": 0.1, "p99": 0.1}
        ],
        "hard_errors": {},
        "backend_health": {"gray": True, "prod": False},
    }

    result, output = run_tool("metrics.py", payload)

    assert result.returncode == 0
    assert output["status"] == "PASS"
    assert output["dispatcher_recommendation"] == {
        "action": "hold_gray",
        "hard_trigger": False,
        "reason_codes": ["PROD_OFFLINE_GRAY_HEALTHY"],
    }


def test_metrics_rejects_invalid_record_instead_of_silently_dropping_it():
    payload = {
        "phase": "normal_gray",
        "rollout_percent": 5,
        "records": [{"pool_label": "canary", "uri_class": "chat", "status": "secret"}],
        "hard_errors": {},
    }

    result, output = run_tool("metrics.py", payload)

    assert result.returncode == 2
    assert output["status"] == "ERROR"
    assert output["errors"] == ["INVALID_RECORD"]


def test_metrics_normalizes_legacy_pool_aliases_to_nginx_contract():
    records = metric_records("prod", "chat", 100, 200, 0.1)
    records += metric_records("gray", "chat", 100, 200, 0.1)
    records += metric_records("guarded-old", "chat", 1, 200, 0.1)
    payload = {
        "phase": "normal_gray",
        "rollout_percent": 10,
        "records": records,
        "hard_errors": {},
    }

    result, output = run_tool("metrics.py", payload)

    assert result.returncode == 0
    assert {group["pool_label"] for group in output["groups"]} == {
        "stable",
        "canary",
        "guarded-old",
    }
    assert output["comparison_mode"] == "stable"
    assert all(item["reference"] == "stable" for item in output["comparisons"])


def test_metrics_unknown_pool_is_invalid_evidence():
    payload = {
        "phase": "normal_gray",
        "rollout_percent": 10,
        "records": metric_records("surprise-pool", "chat", 1, 200, 0.1),
        "hard_errors": {},
    }

    result, output = run_tool("metrics.py", payload)

    assert result.returncode == 2
    assert output["status"] == "ERROR"
    assert output["errors"] == ["INVALID_RECORD"]


def test_metrics_alerts_when_canary_or_baseline_reference_is_missing():
    no_canary = {
        "phase": "normal_gray",
        "rollout_percent": 10,
        "records": metric_records("stable", "chat", 100, 200, 0.1),
        "hard_errors": {},
    }
    result, output = run_tool("metrics.py", no_canary)

    assert result.returncode == 0
    assert output["dispatcher_recommendation"]["action"] == "alert_only"
    assert output["dispatcher_recommendation"]["reason_codes"] == [
        "CANARY_SAMPLE_MISSING"
    ]

    no_baseline = {
        "phase": "normal_gray",
        "rollout_percent": 100,
        "records": metric_records("canary", "chat", 100, 200, 0.1),
        "hard_errors": {},
    }
    result, output = run_tool("metrics.py", no_baseline)

    assert result.returncode == 2
    assert output["status"] == "ERROR"
    assert output["errors"] == ["BASELINE_EVIDENCE_MISSING"]


def test_metrics_hard_errors_require_numeric_counts_and_known_names():
    base = {
        "phase": "normal_gray",
        "rollout_percent": 10,
        "records": metric_records("stable", "chat", 100, 200, 0.1)
        + metric_records("canary", "chat", 100, 200, 0.1),
    }
    for hard_errors in (
        {"callback_import_error": "many"},
        {"totally_unknown_probe": 1},
    ):
        result, output = run_tool("metrics.py", {**base, "hard_errors": hard_errors})

        assert result.returncode == 2
        assert output["status"] == "ERROR"
        assert output["errors"] == ["INVALID_HARD_ERRORS"]


def test_tools_report_invalid_json_without_echoing_input():
    secret = "sk-invalid-json-secret"
    result = subprocess.run(
        [sys.executable, str(TOOLS / "metrics.py"), "--input", "-"],
        input=f'{{"authorization":"Bearer {secret}"',
        capture_output=True,
        check=False,
        cwd=ROOT,
        text=True,
    )

    assert result.returncode == 2
    output = json.loads(result.stdout)
    assert output["status"] == "ERROR"
    assert output["errors"] == ["INVALID_JSON"]
    assert secret not in result.stdout


def test_audit_runtime_rejects_smoke_results_outside_expected_surface_set():
    payload = runtime_payload()
    payload["api_surfaces"]["smoke"]["unreviewed-surface"] = "PASS"
    result, output = run_tool("audit-runtime.py", payload)
    assert result.returncode == 1
    assert "API_SMOKE_EVIDENCE_INVALID" in output["reason_codes"]


def test_evidence_tools_fail_closed_when_envelope_is_missing_or_empty():
    for name, payload in (
        ("audit-runtime.py", runtime_payload()),
        ("check-migration.py", migration_payload()),
        (
            "metrics.py",
            {
                "phase": "normal_gray",
                "rollout_percent": 10,
                "records": metric_records("stable", "chat", 100, 200, 0.1),
                "hard_errors": {},
            },
        ),
    ):
        result, output = run_tool_raw(name, payload)
        assert result.returncode == 2
        assert output["status"] == "ERROR"
        assert "EVIDENCE_MISSING" in output["errors"]

        empty = dict(payload, evidence={})
        result, output = run_tool_raw(name, empty)
        assert result.returncode == 2
        assert "EVIDENCE_INVALID" in output["errors"]


def test_evidence_tools_reject_stale_and_tampered_envelopes():
    cases = (
        ("audit-runtime.py", runtime_payload(), timedelta(hours=2)),
        ("check-migration.py", migration_payload(), timedelta(hours=25)),
        (
            "metrics.py",
            {
                "phase": "normal_gray",
                "rollout_percent": 0,
                "records": [],
                "hard_errors": {},
            },
            timedelta(minutes=11),
        ),
    )
    for name, payload, age in cases:
        stale = _with_evidence(payload, captured_at=datetime.now(timezone.utc) - age)
        result, output = run_tool_raw(name, stale)
        assert result.returncode == 2
        assert "EVIDENCE_STALE" in output["errors"]

        tampered = _with_evidence(payload, checksum="sha256:" + "0" * 64)
        result, output = run_tool_raw(name, tampered)
        assert result.returncode == 2
        assert "EVIDENCE_CHECKSUM_MISMATCH" in output["errors"]


def test_evidence_tools_reject_unknown_top_level_fields():
    for name, payload in (
        ("audit-runtime.py", runtime_payload()),
        ("check-migration.py", migration_payload()),
        (
            "metrics.py",
            {"phase": "normal_gray", "rollout_percent": 0, "records": [], "hard_errors": {}},
        ),
    ):
        payload["surprise"] = True
        result, output = run_strict_tool(name, payload)
        assert result.returncode == 2
        assert output["errors"] == ["EVIDENCE_SCHEMA_UNKNOWN_FIELD"]


def test_evidence_tools_require_valid_run_state_binding():
    for name, payload in (
        ("audit-runtime.py", runtime_payload()),
        ("check-migration.py", migration_payload()),
        (
            "metrics.py",
            {"phase": "normal_gray", "rollout_percent": 0, "records": [], "hard_errors": {}},
        ),
    ):
        unbound = _with_evidence(payload, run_id=None, generation=None, config_checksum=None)
        result, output = run_tool_raw(name, unbound)
        assert result.returncode == 2
        assert output["errors"] == ["EVIDENCE_INVALID"]

        invalid = _with_evidence(payload, config_checksum="not-a-checksum")
        result, output = run_tool_raw(name, invalid)
        assert result.returncode == 2
        assert output["errors"] == ["EVIDENCE_INVALID"]


def test_audit_runtime_classifies_user_organization_customer_control_writes():
    payload = runtime_payload()
    payload["references"] += [
        {"source": "a", "text": "POST http://10.68.13.198:30402/user/new"},
        {"source": "b", "text": "PATCH http://10.68.13.198:30402/organization/update"},
        {"source": "c", "text": "DELETE http://10.68.13.198:30402/customer/delete"},
    ]
    result, output = run_strict_tool("audit-runtime.py", payload)
    assert result.returncode == 0
    assert output["bypass_summary"]["counts"]["control_write"] == 4
    writes = [
        item
        for item in output["bypass_summary"]["references"]
        if item["category"] == "control_write"
    ]
    assert {item["surface"] for item in writes} == {"model", "user", "organization", "customer"}


def test_audit_runtime_extracts_api_path_after_absolute_script_path():
    payload = runtime_payload()
    payload["references"] = [
        {
            "source": "task",
            "text": "python /root/task.py POST http://10.68.13.198:30402/model/new",
        }
    ]
    result, output = run_strict_tool("audit-runtime.py", payload)
    assert result.returncode == 0, result.stderr
    reference = output["bypass_summary"]["references"][0]
    assert reference["path"] == "/model/new"
    assert reference["category"] == "control_write"


def test_check_migration_rejects_duplicate_or_malformed_ledger_ids_and_timings():
    payload = migration_payload()
    payload["ddl_ledger"]["entries"].append(dict(payload["ddl_ledger"]["entries"][0], duration_ms=-1))
    payload["ddl_ledger"]["entries"][1]["id"] = "ddl-001"
    result, output = run_strict_tool("check-migration.py", payload)
    assert result.returncode == 1
    assert {
        "DDL_LEDGER_DUPLICATE_ID",
        "DDL_LEDGER_INVALID_DURATION",
    }.issubset(output["reason_codes"])


def test_check_migration_rejects_invalid_schema_checksum_format():
    payload = migration_payload()
    payload["schema"]["before_checksum"] = "sha256:not-a-digest"
    result, output = run_strict_tool("check-migration.py", payload)
    assert result.returncode == 1
    assert "SCHEMA_CHECKSUM_INVALID" in output["reason_codes"]


def test_check_migration_accepts_not_null_when_default_precedes_constraint():
    payload = migration_payload()
    change = 'ALTER TABLE "LiteLLM_ProxyModelTable" ADD COLUMN "enabled" BOOLEAN DEFAULT TRUE NOT NULL'
    payload["schema"]["changes"] = [change]
    payload["ddl_ledger"]["entries"][0]["statement"] = change
    result, output = run_strict_tool("check-migration.py", payload)
    assert result.returncode == 0
    assert "NOT_NULL_NO_DEFAULT" not in output["schema"]["destructive_kinds"]


def test_check_migration_requires_lock_and_rewrite_evidence_for_every_ddl():
    payload = migration_payload()
    del payload["ddl_ledger"]["entries"][0]["lock_wait_ms"]
    result, output = run_strict_tool("check-migration.py", payload)
    assert result.returncode == 1
    assert "DDL_LEDGER_INVALID_LOCK_EVIDENCE" in output["reason_codes"]

    payload = migration_payload()
    del payload["ddl_ledger"]["entries"][0]["table_rewrite"]
    result, output = run_strict_tool("check-migration.py", payload)
    assert result.returncode == 1
    assert "DDL_LEDGER_INVALID_REWRITE_EVIDENCE" in output["reason_codes"]


def test_check_migration_rejects_missing_online_gate_summary():
    payload = migration_payload()
    del payload["ddl_ledger"]["network_policy"]
    result, output = run_strict_tool("check-migration.py", payload)
    assert result.returncode == 1
    assert "ONLINE_GATE_EVIDENCE_MISSING" in output["reason_codes"]


def test_check_migration_rejects_snapshot_or_network_policy_mismatch():
    payload = migration_payload()
    payload["ddl_ledger"]["network_policy"]["denied_probe"] = "FAIL"
    payload["ddl_ledger"]["snapshot_counts"]["restored"]["SpendLogs"] -= 1
    result, output = run_strict_tool("check-migration.py", payload)
    assert result.returncode == 1
    assert {
        "NETWORK_POLICY_NOT_ENFORCED",
        "SNAPSHOT_COUNT_MISMATCH",
    }.issubset(output["reason_codes"])


def test_metrics_rejects_status_codes_outside_http_range():
    payload = {
        "phase": "normal_gray",
        "rollout_percent": 10,
        "records": [{"pool_label": "canary", "uri_class": "chat", "status": 999, "response_time": 0.1}],
        "hard_errors": {},
    }
    result, output = run_tool_raw("metrics.py", _with_evidence(payload))
    assert result.returncode == 2
    assert output["errors"] == ["INVALID_RECORD"]


def test_metrics_rejects_unknown_phase_non_integer_split_and_missing_baseline():
    base = {
        "phase": "normal_gray",
        "rollout_percent": 10,
        "records": metric_records("stable", "chat", 100, 200, 0.1),
        "hard_errors": {},
        "spend_reconciliation": {
            "expected_request_ids": ["r-1"],
            "terminal_request_ids": ["r-1"],
            "failed_request_ids": [],
            "observed_lag_seconds": 1,
        },
    }
    for mutation, reason in (
        ({"phase": "not-a-phase"}, "INVALID_PHASE"),
        ({"rollout_percent": 10.5}, "INVALID_SPLIT"),
    ):
        payload = dict(base, **mutation)
        result, output = run_strict_tool("metrics.py", payload)
        assert result.returncode == 2
        assert reason in output["errors"]

    payload = dict(base, phase="normal_gray", rollout_percent=100)
    result, output = run_tool_raw("metrics.py", _with_evidence(payload))
    assert result.returncode == 2
    assert "BASELINE_EVIDENCE_MISSING" in output["errors"]


def test_metrics_reconciles_request_ids_and_triggers_on_missing_terminal_rows():
    payload = {
        "phase": "normal_gray",
        "rollout_percent": 10,
        "records": metric_records("stable", "chat", 100, 200, 0.1)
        + metric_records("canary", "chat", 100, 200, 0.1),
        "hard_errors": {},
        "spend_reconciliation": {
            "expected_request_ids": ["r-1", "r-2"],
            "terminal_request_ids": ["r-1"],
            "failed_request_ids": [],
            "observed_lag_seconds": 2,
        },
    }
    result, output = run_strict_tool("metrics.py", payload)
    assert result.returncode == 1
    assert "SPEND_RECONCILIATION_FAILED" in output["dispatcher_recommendation"]["reason_codes"]
    assert output["spend_reconciliation"]["missing_request_ids"] == ["r-2"]


def test_metrics_requires_spend_reconciliation_when_gray_has_traffic():
    payload = {
        "phase": "normal_gray",
        "rollout_percent": 10,
        "records": metric_records("stable", "chat", 100, 200, 0.1)
        + metric_records("canary", "chat", 100, 200, 0.1),
        "hard_errors": {},
    }
    result, output = run_strict_tool("metrics.py", payload)
    assert result.returncode == 2
    assert output["errors"] == ["SPEND_RECONCILIATION_MISSING"]


def test_metrics_emits_dispatcher_state_binding_from_evidence():
    payload = {
        "phase": "normal_gray",
        "rollout_percent": 0,
        "records": [],
        "hard_errors": {},
    }
    wrapped = _with_evidence(payload, run_id="run-123", generation="gen-009")
    result, output = run_tool_raw("metrics.py", wrapped)
    assert result.returncode == 0
    assert output["run_id"] == "run-123"
    assert output["generation"] == "gen-009"
    assert output["captured_at"] == wrapped["evidence"]["captured_at"]
    assert output["phase"] == "normal_gray"


def test_metrics_output_has_strict_dispatcher_schema_and_checksum():
    payload = {
        "phase": "normal_gray",
        "rollout_percent": 0,
        "records": [],
        "hard_errors": {},
    }
    result, output = run_tool_raw("metrics.py", _with_evidence(payload))
    assert result.returncode == 0
    assert output["tool"] == "metrics"
    assert output["schema_version"] == 1
    checksum = output.pop("payload_sha256")
    assert checksum == _value_digest(output)
    assert output["status"] == "PASS"
    assert output["dispatcher_recommendation"]["hard_trigger"] is False
    assert output["dispatcher_recommendation"]["action"] in {
        "none",
        "alert_only",
        "hold_gray",
    }


def _write_source(path: Path, source: str, data: object, *, captured_at: datetime | None = None) -> None:
    captured_at = captured_at or datetime.now(timezone.utc)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": source,
                "captured_at": captured_at.isoformat().replace("+00:00", "Z"),
                "payload_sha256": _value_digest(data),
                "data": data,
            }
        ),
        encoding="utf-8",
    )


def _collect_runtime_command(tmp_path: Path) -> tuple[list[str], Path]:
    sources = {
        "expected-inventory": runtime_payload()["expected_inventory"],
        "bypass-inventory": ["POST http://10.68.13.198:30402/model/new"],
        "callbacks": runtime_payload()["callbacks"],
        "api-surfaces": runtime_payload()["api_surfaces"],
        "acct": runtime_payload()["acct"],
        "redis": runtime_payload()["redis"],
        "scheduler": runtime_payload()["scheduler"],
        "mutation-visibility": runtime_payload()["mutation_visibility"],
    }
    arguments: list[str] = []
    for option, data in sources.items():
        path = tmp_path / f"{option}.json"
        _write_source(path, f"capture:{option}", data)
        arguments += [f"--{option}", str(path)]
    output = tmp_path / "run" / "runtime.json"
    command = [
        sys.executable,
        str(TOOLS / "collect-runtime.py"),
        "--run-id",
        "run-001",
        "--generation",
        "gen-001",
        "--config-checksum",
        "a" * 64,
        *arguments,
        "--output",
        str(output),
    ]
    return command, output


def test_collect_runtime_preserves_source_freshness_and_rejects_stale_rewrap(tmp_path: Path):
    command, output = _collect_runtime_command(tmp_path)
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    collected = json.loads(output.read_text(encoding="utf-8"))
    assert {item["section"] for item in collected["sources"]} == {
        "expected_inventory",
        "references",
        "callbacks",
        "api_surfaces",
        "acct",
        "redis",
        "scheduler",
        "mutation_visibility",
    }
    assert all(item["source"].startswith("capture:") for item in collected["sources"])
    assert all(item["freshness_seconds"] >= 0 for item in collected["sources"])
    assert output.stat().st_mode & 0o777 == 0o600

    stale_path = Path(command[command.index("--redis") + 1])
    _write_source(
        stale_path,
        "capture:redis",
        runtime_payload()["redis"],
        captured_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    stale_output = tmp_path / "stale" / "runtime.json"
    stale_command = [*command[: command.index("--output") + 1], str(stale_output)]
    rejected = subprocess.run(stale_command, capture_output=True, text=True, check=False)
    assert rejected.returncode != 0
    assert "stale" in rejected.stderr.lower()


def test_collect_runtime_refuses_symlink_or_existing_output(tmp_path: Path):
    command, output = _collect_runtime_command(tmp_path)
    output.parent.mkdir(parents=True)
    target = tmp_path / "target.json"
    target.write_text("untouched", encoding="utf-8")
    output.symlink_to(target)
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert target.read_text(encoding="utf-8") == "untouched"


def _prepare_values_command(tmp_path: Path, config_text: str, env_block: str) -> tuple[list[str], Path]:
    config = tmp_path / "config.yaml"
    config.write_text(config_text, encoding="utf-8")
    callbacks = tmp_path / "callbacks"
    callbacks.mkdir()
    (callbacks / "smoke.py").write_text("def callback():\n    return True\n", encoding="utf-8")
    deployment = tmp_path / "deployment.yaml"
    deployment.write_text(
        f"""apiVersion: apps/v1
kind: Deployment
metadata: {{name: litellm-proxy}}
spec:
  template:
    spec:
      terminationGracePeriodSeconds: 600
      containers:
        - name: litellm
          command: [/app/docker/prod_entrypoint.sh]
          args: [--config, /app/config.yaml]
          env:
{env_block}
          envFrom:
            - secretRef: {{name: litellm-secrets}}
          resources: {{}}
          readinessProbe:
            initialDelaySeconds: 60
            periodSeconds: 10
            failureThreshold: 12
            timeoutSeconds: 5
          livenessProbe:
            initialDelaySeconds: 180
            periodSeconds: 30
            failureThreshold: 10
            timeoutSeconds: 8
          lifecycle:
            preStop:
              exec:
                command: [sh, -c, sleep 15]
""",
        encoding="utf-8",
    )
    config_sha = "sha256:" + hashlib.sha256(config.read_bytes()).hexdigest()
    scheduler = tmp_path / "scheduler.json"
    callback_data = {"smoke.py": (callbacks / "smoke.py").read_text(encoding="utf-8")}
    callback_text = _PREPARE_VALUES.raw_json(callback_data)
    extra_env = [
        {
            "name": "ROUTER_MODE",
            **(
                {"valueFrom": {"configMapKeyRef": {"name": "runtime-flags", "key": "ROUTER_MODE"}}}
                if "configMapKeyRef" in env_block
                else {"value": "safe"}
            ),
        }
    ]
    # Exactly the four keys the chart's `litellm-proxy.runtimeChecksum` digests.
    # The Secret snapshot used to be folded in here too, which made every real
    # values file unrenderable; it now travels as its own recorded field,
    # `schedulerSafety.secretMetadataSha256`, which the chart only
    # placeholder-checks because it never sees Secret contents.
    runtime_payload = {
        "args": ["--config", "/app/config.yaml"],
        "command": ["/app/docker/prod_entrypoint.sh"],
        "extraEnv": extra_env,
        "secretRefs": ["litellm-secrets"],
    }
    secret_metadata_payload = [{
        "name": "litellm-secrets", "uid": "secret-uid-1",
        "resource_version": "123", "data_sha256": "sha256:" + "e" * 64,
    }]
    scheduler_payload = {
        "schema_version": 1,
        "profile": "gray",
        "mode": "disabled",
        "image_digest": "sha256:" + "a" * 64,
        "config_sha256": config_sha,
        "callbacks_sha256": "sha256:" + hashlib.sha256(callback_text.encode("utf-8")).hexdigest(),
        "runtime_sha256": _value_digest(runtime_payload),
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": "clone:scheduler-observation",
        "observations": {
            "duplicate_scheduler_runs": 0,
            "duplicate_background_jobs": 0,
            "unexpected_control_writes": 0,
        },
    }
    scheduler_payload["source_payload_sha256"] = _value_digest(scheduler_payload)
    scheduler.write_text(json.dumps(scheduler_payload), encoding="utf-8")
    secret_metadata = tmp_path / "secret-metadata.json"
    secret_metadata.write_text(json.dumps(secret_metadata_payload), encoding="utf-8")
    output = tmp_path / "run" / "values.yaml"
    command = [
        sys.executable,
        str(TOOLS / "prepare-values.py"),
        "--ingress-cidr",
        "10.68.13.242/32",
        "--profile",
        str(ROOT / "litellm-gray-rollout" / "k8s" / "values-gray.yaml"),
        "--repository",
        "127.0.0.1:5000/litellm-carher",
        "--digest",
        "sha256:" + "a" * 64,
        "--config",
        str(config),
        "--callbacks-dir",
        str(callbacks),
        "--deployment",
        str(deployment),
        "--scheduler-evidence",
        str(scheduler),
        "--secret-metadata",
        str(secret_metadata),
        "--output",
        str(output),
    ]
    return command, output


def test_prepare_values_rejects_inline_credentials_and_preserves_configmap_refs(tmp_path: Path):
    command, _ = _prepare_values_command(
        tmp_path,
        "general_settings:\n  master_key: raw-master-secret\n",
        "            - name: ROUTER_MODE\n              value: safe\n",
    )
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "credential" in result.stderr.lower()

    safe_dir = tmp_path / "safe"
    safe_dir.mkdir()
    command, output = _prepare_values_command(
        safe_dir,
        "general_settings:\n  master_key: os.environ/LITELLM_MASTER_KEY\n",
        "            - name: ROUTER_MODE\n              valueFrom:\n                configMapKeyRef:\n                  name: runtime-flags\n                  key: ROUTER_MODE\n",
    )
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "configMapKeyRef" in output.read_text(encoding="utf-8")


def test_prepare_values_rejects_inline_sensitive_env_and_existing_output(tmp_path: Path):
    command, output = _prepare_values_command(
        tmp_path,
        "model_list: []\n",
        "            - name: OPENAI_API_KEY\n              value: provider-secret\n",
    )
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "credential" in result.stderr.lower()

    safe_dir = tmp_path / "safe"
    safe_dir.mkdir()
    command, output = _prepare_values_command(
        safe_dir,
        "model_list: []\n",
        "            - name: ROUTER_MODE\n              value: safe\n",
    )
    output.parent.mkdir(parents=True)
    output.write_text("do-not-overwrite", encoding="utf-8")
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert output.read_text(encoding="utf-8") == "do-not-overwrite"


def _prepare_migration_command(tmp_path: Path, *, target: str | None = None) -> tuple[list[str], Path]:
    ledger = {
        "schema_version": 1,
        "statements": [
            {
                "id": "ddl-001",
                "sql": 'ALTER TABLE "LiteLLM_ProxyModelTable" ADD COLUMN "gray_gate_probe" TEXT',
                "online_safe": True,
                "lock_mode": "ACCESS EXCLUSIVE",
                "table_rewrite": False,
            }
        ],
    }
    ledger_path = tmp_path / "migration-ledger.json"
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    output = tmp_path / "run"
    command = [
        sys.executable,
        str(TOOLS / "prepare-migration-run.py"),
        "--migration-template",
        str(ROOT / "litellm-gray-rollout" / "k8s" / "migration-job.yaml"),
        "--version-template",
        str(ROOT / "litellm-gray-rollout" / "k8s" / "clone-version-test-jobs.yaml"),
        "--target-image",
        "127.0.0.1:5000/litellm-carher@sha256:" + "a" * 64,
        "--stable-image",
        "127.0.0.1:5000/litellm-carher@sha256:" + "b" * 64,
        "--migration-ledger",
        str(ledger_path),
        "--output-dir",
        str(output),
        "--run-id",
        "run-audit",
        "--generation",
        "g000001",
    ]
    if target is not None:
        command += ["--migration-target", target]
    return command, output


def test_prepare_migration_defaults_to_clone_and_requires_explicit_prod(tmp_path: Path):
    command, output = _prepare_migration_command(tmp_path)
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    migration = (output / "migration-job.yaml").read_text(encoding="utf-8")
    assert "namespace: litellm-clone" in migration
    assert "name: litellm-clone-a-credentials" in migration
    assert "production-schema-migration" not in migration
    assert json.loads(result.stdout)["migration_target"] == "clone"

    prod_dir = tmp_path / "prod"
    prod_dir.mkdir()
    command, output = _prepare_migration_command(prod_dir, target="prod")
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "live schema" in result.stderr.lower() or "clone qualification" in result.stderr.lower()

    ledger_path = Path(command[command.index("--migration-ledger") + 1])
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    qualification = prod_dir / "clone-qualification.json"
    live_schema = prod_dir / "live-schema.sql"
    live_schema.write_text("normalized-live-schema\n", encoding="utf-8")
    migration = migration_payload()
    statement = ledger["statements"][0]["sql"]
    migration["schema"]["before_checksum"] = (
        "sha256:" + hashlib.sha256(live_schema.read_bytes()).hexdigest()
    )
    migration["schema"]["changes"] = [statement]
    migration["ddl_ledger"]["entries"][0]["statement"] = statement
    migration["binding"] = {
        "ledger_sha256": _value_digest(ledger),
        "target_image": command[command.index("--target-image") + 1],
        "stable_image": command[command.index("--stable-image") + 1],
        "attestations_sha256": "sha256:" + "0" * 64,
        "runner_sha256": "sha256:" + "1" * 64,
        "db_targets_sha256": {
            "migration": "sha256:" + "2" * 64,
            "new": "sha256:" + "3" * 64,
            "old": "sha256:" + "4" * 64,
            "concurrent-new": "sha256:" + "5" * 64,
            "concurrent-old": "sha256:" + "6" * 64,
        },
    }
    migration["compatibility"] = {
        clone: {
            "status": "PASS",
            "checks": sorted(checks),
            "result_sha256": "sha256:" + clone.lower() * 64,
        }
        for clone, checks in {
            "A": {"proxy_startup", "key_api", "proxy_model_api", "auth", "spend_logs"},
            "B": {"proxy_startup", "key_api", "proxy_model_api", "auth", "spend_logs"},
            "C": {"concurrent_budget", "concurrent_spend_logs", "concurrent_proxy_model"},
        }.items()
    }
    migration = _with_evidence(
        migration,
        run_id="run-audit",
        generation="g000001",
        config_checksum="a" * 64,
    )
    checked = subprocess.run(
        [sys.executable, str(TOOLS / "check-migration.py"), "--input", "-"],
        input=json.dumps(migration),
        capture_output=True,
        text=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stderr
    qualification.write_text(checked.stdout, encoding="utf-8")
    result = subprocess.run(
        [
            *command,
            "--clone-qualification",
            str(qualification),
            "--live-schema",
            str(live_schema),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    migration = (output / "migration-job.yaml").read_text(encoding="utf-8")
    assert "namespace: litellm-product" in migration
    assert "name: litellm-production-migration-credentials" in migration
    assert "clone-schema-migration" not in migration


def test_prepare_migration_refuses_existing_output(tmp_path: Path):
    command, output = _prepare_migration_command(tmp_path)
    output.mkdir(parents=True)
    existing = output / "migration-job.yaml"
    existing.write_text("do-not-overwrite", encoding="utf-8")
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert existing.read_text(encoding="utf-8") == "do-not-overwrite"


def test_runbook_covers_frozen_artifacts_phases_gates_evidence_and_rollback():
    text = RUNBOOK.read_text(encoding="utf-8")

    required = {
        "Run identity",
        "Owner matrix",
        "Frozen artifacts and checksums",
        "Phase ledger",
        "Migration gate",
        "Runtime gate",
        "Gray traffic gates",
        "Convergence gates",
        "Evidence index",
        "Rollback and abort",
        "Observation and cleanup",
        "chart package SHA-256",
        "guarded-old",
        "prod_offline_upgrading",
        "gray-auto-dispatch.sh",
        "gray-monitor-cycle.sh",
    }
    assert not {item for item in required if item not in text}


def test_runbook_forbids_credentials_and_records_only_secret_metadata():
    text = RUNBOOK.read_text(encoding="utf-8")

    assert "Never paste credentials" in text
    assert "Secret resourceVersion/checksum only" in text
    assert "sk-" not in text
    assert "Bearer " not in text


def _write_manifest(path: Path, objects: list[tuple[str, str, str]]) -> Path:
    path.write_text(
        "\n---\n".join(
            f"apiVersion: {api}\nkind: {kind}\nmetadata:\n"
            f"  name: {name}\n  namespace: litellm-product\n"
            for api, kind, name in objects
        ),
        encoding="utf-8",
    )
    return path


def _deletion_set(tmp_path: Path, *extra: str, live=None, target=None):
    live_objects = live if live is not None else [
        ("apps/v1", "Deployment", "litellm-proxy"),
        ("v1", "Service", "litellm-proxy-nodeport"),
        ("policy/v1", "PodDisruptionBudget", "litellm-proxy-pdb"),
    ]
    target_objects = target if target is not None else [
        ("apps/v1", "Deployment", "litellm-proxy"),
        ("v1", "Service", "litellm-proxy-nodeport"),
    ]
    command = [
        sys.executable,
        str(TOOLS / "check-release-deletion-set.py"),
        "--live-manifest",
        str(_write_manifest(tmp_path / "live.yaml", live_objects)),
        "--target-manifest",
        str(_write_manifest(tmp_path / "target.yaml", target_objects)),
        "--release",
        "litellm-product-proxy",
        "--namespace",
        "litellm-product",
        "--run-id",
        "run-1",
        "--generation",
        "gen-1",
        *extra,
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    return result, json.loads(result.stdout)


def test_release_deletion_set_fails_closed_on_an_unapproved_deletion(tmp_path: Path):
    """`helm upgrade --reset-values` onto a new chart deletes what it no longer renders."""
    result, payload = _deletion_set(tmp_path)
    assert result.returncode == 1
    assert payload["status"] == "FAIL"
    assert payload["errors"] == ["UNAPPROVED_DELETIONS"]
    assert payload["unapproved_deletions"] == [
        "policy/PodDisruptionBudget/litellm-product/litellm-proxy-pdb"
    ]
    # A PDB disappearing is invisible to `kubectl rollout status`.
    assert payload["critical_deletions"] == payload["unapproved_deletions"]


def test_release_deletion_set_requires_a_named_consumer_and_a_matching_diff(tmp_path: Path):
    pdb = "policy/PodDisruptionBudget/litellm-product/litellm-proxy-pdb"
    approval = tmp_path / "approval.json"

    def approve(entries: list[dict]) -> list[str]:
        approval.write_text(json.dumps({"deletions": entries}), encoding="utf-8")
        return ["--approval", str(approval)]

    # An unnamed consumer is the empty data column: refused.
    for placeholder in ("unknown", "TBD", "n/a", "  "):
        _, payload = _deletion_set(
            tmp_path,
            *approve(
                [
                    {
                        "object": pdb,
                        "consumer": placeholder,
                        "disposition": "drop",
                        "approver": "commander",
                    }
                ]
            ),
        )
        assert payload["status"] == "FAIL", placeholder

    good = {
        "object": pdb,
        "consumer": "prod availability during node drain",
        "disposition": "recreate-after-upgrade",
        "approver": "change-commander",
    }
    result, payload = _deletion_set(tmp_path, *approve([good]))
    assert result.returncode == 0
    assert payload["status"] == "PASS"
    assert payload["deleted"][0]["consumer"] == good["consumer"]

    # An approval written against a different render must not silently pass.
    _, payload = _deletion_set(
        tmp_path,
        *approve([good, {**good, "object": "v1/ConfigMap/litellm-product/stale"}]),
    )
    assert payload["status"] == "FAIL"
    assert "APPROVAL_DOES_NOT_MATCH_DIFF" in payload["errors"]


def test_release_deletion_set_reports_additions_without_failing(tmp_path: Path):
    result, payload = _deletion_set(
        tmp_path,
        live=[("apps/v1", "Deployment", "litellm-proxy")],
        target=[
            ("apps/v1", "Deployment", "litellm-proxy"),
            ("networking.k8s.io/v1", "NetworkPolicy", "litellm-proxy-ingress"),
        ],
    )
    assert result.returncode == 0
    assert payload["status"] == "PASS"
    assert payload["added"] == [
        "networking.k8s.io/NetworkPolicy/litellm-product/litellm-proxy-ingress"
    ]
    assert payload["deleted"] == []


POD_SPEC_LIVE = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: litellm-proxy
  namespace: litellm-product
  uid: 11111111-2222-3333-4444-555555555555
  resourceVersion: "987654"
  annotations:
    deployment.kubernetes.io/revision: "42"
spec:
  template:
    spec:
      volumes:
        - name: config
          configMap: {name: litellm-config}
        - name: callbacks
          configMap: {name: litellm-callbacks}
        - name: streaming-handler-patch
          configMap: {name: litellm-streaming-handler-patch}
        - name: deepcopy-patch
          configMap: {name: litellm-deepcopy-patch}
      containers:
        - name: litellm
          command: [/app/docker/prod_entrypoint.sh]
          args: [--config, /app/config.yaml]
          env:
            - name: STORE_PROMPTS_IN_SPEND_LOGS
              value: "True"
          envFrom:
            - secretRef: {name: litellm-secrets}
          volumeMounts:
            - name: config
              mountPath: /app/config.yaml
              subPath: config.yaml
              readOnly: true
            - name: callbacks
              mountPath: /app/budget_notice.py
              subPath: budget_notice.py
            - name: streaming-handler-patch
              mountPath: /app/.venv/lib/python3.13/site-packages/litellm/litellm_core_utils/streaming_handler.py
              subPath: streaming_handler.py
            - name: deepcopy-patch
              mountPath: /patches
          lifecycle:
            postStart:
              exec:
                command: [python3, /patches/patch.py]
status:
  readyReplicas: 4
"""

# The chart render: same object, same name, converges fine, health checks pass --
# and every single-file overlay is gone.
POD_SPEC_TARGET = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: litellm-proxy
  namespace: litellm-product
spec:
  template:
    spec:
      volumes:
        - name: config
          configMap: {name: litellm-product-proxy-config-fcc2a2ad}
        - name: callbacks
          configMap: {name: litellm-product-proxy-callbacks-e718bd18}
      containers:
        - name: litellm
          command: [/app/docker/prod_entrypoint.sh]
          args: [--config, /app/config.yaml]
          env:
            - name: STORE_PROMPTS_IN_SPEND_LOGS
              value: "True"
          envFrom:
            - secretRef: {name: litellm-secrets}
          volumeMounts:
            - name: config
              mountPath: /app/config.yaml
              subPath: config.yaml
              readOnly: true
            - name: callbacks
              mountPath: /app/budget_notice.py
              subPath: budget_notice.py
          lifecycle:
            preStop:
              exec:
                command: [sh, -c, sleep 30]
"""


def _pod_spec_shape(
    tmp_path: Path, *extra: str, live: str = POD_SPEC_LIVE, target: str = POD_SPEC_TARGET
):
    live_path = tmp_path / "live.yaml"
    live_path.write_text(live, encoding="utf-8")
    target_path = tmp_path / "target.yaml"
    target_path.write_text(target, encoding="utf-8")
    command = [
        sys.executable,
        str(TOOLS / "check-pod-spec-shape.py"),
        "--live",
        str(live_path),
        "--target",
        str(target_path),
        "--name",
        "litellm-proxy",
        "--namespace",
        "litellm-product",
        "--run-id",
        "run-1",
        "--generation",
        "gen-1",
        *extra,
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    return result, json.loads(result.stdout)


def test_pod_spec_shape_catches_the_mount_loss_every_other_gate_reads_green(tmp_path: Path):
    """The object set is unchanged, the Deployment converges, /health is 200.

    Measured against the real production Deployment on 2026-09-13: 39 mounts
    removed, 1 swapped, the postStart patch runner gone. The deletion-set gate
    sees nothing because no ConfigMap is deleted -- they simply stop being
    mounted.
    """
    result, payload = _pod_spec_shape(tmp_path)
    assert result.returncode == 1
    assert payload["status"] == "FAIL"
    assert payload["errors"] == ["UNAPPROVED_SHAPE_CHANGES"]

    assert payload["mount_removals"] == [
        "container/litellm/mount//app/.venv/lib/python3.13/site-packages/litellm/"
        "litellm_core_utils/streaming_handler.py/streaming_handler.py",
        "container/litellm/mount//patches/",
    ]
    removed = {entry["item"] for entry in payload["removed"]}
    assert "container/litellm/lifecycle/postStart" in removed
    assert "volume/streaming-handler-patch" in removed

    # Same mountPath, different ConfigMap behind it: a silent content swap, which
    # a present/absent comparison alone would pair up and call unchanged.
    changed = {entry["item"]: entry for entry in payload["changed"]}
    swap = changed["container/litellm/mount//app/config.yaml/config.yaml"]
    assert swap["live"] == "configMap/litellm-config|ro"
    assert swap["target"] == "configMap/litellm-product-proxy-config-fcc2a2ad|ro"


def test_pod_spec_shape_never_reads_an_environment_variable_value(tmp_path: Path):
    """Only names enter the diff, so an approval file needs no redaction."""
    live = POD_SPEC_LIVE.replace(
        '            - name: STORE_PROMPTS_IN_SPEND_LOGS\n              value: "True"\n',
        '            - name: LITELLM_MASTER_KEY\n              value: "sk-live-must-never-appear"\n',
    )
    result, payload = _pod_spec_shape(tmp_path, live=live)
    assert result.returncode == 1
    assert "sk-live-must-never-appear" not in result.stdout
    assert "container/litellm/env/LITELLM_MASTER_KEY" in {
        entry["item"] for entry in payload["removed"]
    }
    assert all(entry["live"] == "set" for entry in payload["removed"]
               if "/env/" in entry["item"])


def test_pod_spec_shape_refuses_to_compare_a_render_against_itself(tmp_path: Path):
    """Both self-comparisons are false green, so both fail closed.

    `helm get manifest` reports what Helm believes it applied. litellm-proxy is
    changed with `set image`/`patch` and never `apply`, so the live object can
    have drifted away from it -- feeding a render as the live side compares the
    target against itself and passes by construction.
    """
    _, render_as_live = _pod_spec_shape(tmp_path, live=POD_SPEC_TARGET)
    assert render_as_live["status"] == "FAIL"
    assert "LIVE_SIDE_IS_NOT_A_LIVE_OBJECT" in render_as_live["errors"]

    _, live_as_target = _pod_spec_shape(tmp_path, target=POD_SPEC_LIVE)
    assert live_as_target["status"] == "FAIL"
    assert "TARGET_SIDE_IS_NOT_A_RENDER" in live_as_target["errors"]
    assert live_as_target["counts"]["removed"] == 0  # would have been a clean green

    # A live capture with no volumes at all is a broken capture, not a workload
    # that stopped mounting things.
    _, no_volumes = _pod_spec_shape(
        tmp_path,
        live="""apiVersion: apps/v1
kind: Deployment
metadata:
  name: litellm-proxy
  namespace: litellm-product
  uid: 11111111-2222-3333-4444-555555555555
spec:
  template:
    spec:
      containers:
        - name: litellm
          command: [/app/docker/prod_entrypoint.sh]
""",
    )
    assert "LIVE_SIDE_HAS_NO_VOLUMES" in no_volumes["errors"]


def test_pod_spec_shape_requires_a_named_consumer_and_a_matching_diff(tmp_path: Path):
    _, baseline = _pod_spec_shape(tmp_path)
    items = sorted(
        {entry["item"] for entry in baseline["removed"]}
        | {entry["item"] for entry in baseline["changed"]}
    )
    approval = tmp_path / "approval.json"

    def approve(entries: list[dict]) -> list[str]:
        approval.write_text(json.dumps({"shape_changes": entries}), encoding="utf-8")
        return ["--approval", str(approval)]

    def entry(item: str, consumer: str = "codex /pro/v1/responses long sessions") -> dict:
        return {
            "item": item,
            "consumer": consumer,
            "disposition": "re-expressed via additionalSnapshots subPath",
            "approver": "change-commander",
        }

    # Every removal approved but one: still red. A gate that only checks "is
    # there an approval file" is not a gate.
    _, partial = _pod_spec_shape(tmp_path, *approve([entry(item) for item in items[:-1]]))
    assert partial["status"] == "FAIL"
    assert partial["unapproved_shape_changes"] == [items[-1]]

    result, passed = _pod_spec_shape(tmp_path, *approve([entry(item) for item in items]))
    assert result.returncode == 0
    assert passed["status"] == "PASS"

    for placeholder in ("unknown", "TBD", "n/a", "  "):
        _, payload = _pod_spec_shape(
            tmp_path,
            *approve([entry(item) for item in items[:-1]] + [entry(items[-1], placeholder)]),
        )
        assert payload["status"] == "FAIL", placeholder

    # An approval written against a different render must not silently pass.
    _, stale = _pod_spec_shape(
        tmp_path, *approve([entry(item) for item in items] + [entry("volume/does-not-exist")])
    )
    assert stale["status"] == "FAIL"
    assert "APPROVAL_DOES_NOT_MATCH_DIFF" in stale["errors"]


def test_pod_spec_shape_rejects_a_fill_me_approver(tmp_path: Path):
    """An unsigned approval must be louder than a missing one -- it reads signed.

    Measured 2026-09-14: `k8s/prod-pod-spec-approval.json` shipped with
    `"approver": "<FILL-APPROVER>"` and `load_approval` accepted all 10 entries
    with zero errors. The property everyone was relying on -- "the checked-in
    list cannot rubber-stamp itself, someone has to sign it" -- existed only in
    the prose describing this tool. The `consumer` column had a placeholder
    blacklist; the `approver` column had nothing but a non-empty check, and
    `<FILL-APPROVER>` is non-empty.

    That is this gate's own failure mode turned on itself: green while enforcing
    nothing. Hence a structural rule (anything wrapped in <>, {{}} or [] is a
    replace-me marker) plus a word blacklist, and this test.
    """
    _, baseline = _pod_spec_shape(tmp_path)
    items = sorted(
        {entry["item"] for entry in baseline["removed"]}
        | {entry["item"] for entry in baseline["changed"]}
    )
    approval = tmp_path / "approval.json"

    def run(approver: str):
        approval.write_text(
            json.dumps(
                {
                    "shape_changes": [
                        {
                            "item": item,
                            "consumer": "codex /pro/v1/responses long sessions",
                            "disposition": "re-expressed via additionalSnapshots subPath",
                            "approver": approver,
                        }
                        for item in items
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return _pod_spec_shape(tmp_path, "--approval", str(approval))[1]

    # A real name signs. Non-ASCII on purpose: the approver of record here is a
    # person whose name does not fit in ASCII, and a rule that only rejects
    # placeholders must not reject them too.
    signed = run("刘国现")
    assert signed["status"] == "PASS", signed.get("errors")

    for unsigned in (
        "<FILL-APPROVER>",   # the exact value that shipped
        "{{approver}}",
        "[your name]",
        "TBD",
        "unsigned",
        "placeholder",
    ):
        payload = run(unsigned)
        assert payload["status"] == "FAIL", unsigned
        assert any(
            error.startswith("APPROVAL_UNSIGNED:") for error in payload["errors"]
        ), (unsigned, payload["errors"])

    # Blank is rejected too, but by the earlier non-empty rule -- asserting
    # APPROVAL_UNSIGNED here would be pinning the error code rather than the
    # behaviour, and would go red the day the ordering changes harmlessly.
    blank = run("  ")
    assert blank["status"] == "FAIL"
    assert "APPROVAL_ENTRY_INVALID" in blank["errors"]


# The chart content-addresses its ConfigMaps (`<release>-<snapshot>-<checksum>`)
# while production names them by hand, so the *same bytes* arrive under a
# different ConfigMap name. Captured contents let the gate tell that apart from a
# real content swap; without them every mount reads as changed and the approval
# file becomes 40 lines of "yes, same bytes" -- a rubber stamp, not a gate.
CONFIG_BYTES = "model_list: []\n"
CALLBACK_BYTES = "def notice():\n    return None\n"
PATCH_BYTES = "# deepcopy patch\n"

LIVE_CONFIGMAPS = f"""apiVersion: v1
kind: ConfigMap
metadata:
  name: litellm-config
  namespace: litellm-product
  uid: aaaaaaaa-0000-0000-0000-000000000001
  resourceVersion: "111"
data:
  config.yaml: |
    {CONFIG_BYTES.strip()}
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: litellm-callbacks
  namespace: litellm-product
  uid: aaaaaaaa-0000-0000-0000-000000000002
  resourceVersion: "112"
data:
  budget_notice.py: |
{textwrap.indent(CALLBACK_BYTES.rstrip(), " " * 4)}
"""

TARGET_CONFIGMAPS = f"""apiVersion: v1
kind: ConfigMap
metadata:
  name: litellm-product-proxy-config-fcc2a2ad
  namespace: litellm-product
data:
  config.yaml: |
    {CONFIG_BYTES.strip()}
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: litellm-product-proxy-callbacks-e718bd18
  namespace: litellm-product
data:
  budget_notice.py: |
{textwrap.indent(CALLBACK_BYTES.rstrip(), " " * 4)}
"""


def _with_configmaps(tmp_path: Path, live: str, target: str) -> list[str]:
    live_path = tmp_path / "live-cm.yaml"
    live_path.write_text(live, encoding="utf-8")
    target_path = tmp_path / "target-cm.yaml"
    target_path.write_text(target, encoding="utf-8")
    return [
        "--live-configmaps",
        str(live_path),
        "--target-configmaps",
        str(target_path),
    ]


def test_pod_spec_shape_separates_a_content_addressed_rename_from_a_content_swap(
    tmp_path: Path,
):
    """Same bytes under a new ConfigMap name is inert; one changed byte is not.

    Without this the gate is technically correct and practically useless: the
    chart renames every ConfigMap it owns, so all 40 prod mounts would demand an
    approval that says nothing, and the real removals would be buried among them.
    """
    baseline_unapproved = set(_pod_spec_shape(tmp_path)[1]["unapproved_shape_changes"])

    _, inert = _pod_spec_shape(
        tmp_path, *_with_configmaps(tmp_path, LIVE_CONFIGMAPS, TARGET_CONFIGMAPS)
    )
    config_mount = "container/litellm/mount//app/config.yaml/config.yaml"
    assert config_mount in set(inert["inert_by_content"])
    assert "volume/config" in set(inert["inert_by_content"])
    assert config_mount not in set(inert["unapproved_shape_changes"])
    # The item still appears in the diff, flagged -- it is excused, not hidden.
    assert {entry["item"]: entry["inert"] for entry in inert["changed"]}[config_mount] is True

    # And the genuine losses are untouched by any of this.
    assert set(inert["mount_removals"]) <= set(inert["unapproved_shape_changes"])
    assert set(inert["unapproved_shape_changes"]) < baseline_unapproved

    # One byte different in the mounted key: back to a change that needs a human.
    swapped_target = TARGET_CONFIGMAPS.replace("model_list: []", "model_list: [{}]")
    _, swap = _pod_spec_shape(
        tmp_path, *_with_configmaps(tmp_path, LIVE_CONFIGMAPS, swapped_target)
    )
    assert config_mount not in set(swap["inert_by_content"])
    assert config_mount in set(swap["unapproved_shape_changes"])


def test_pod_spec_shape_refuses_rendered_configmaps_as_the_live_capture(tmp_path: Path):
    """Feeding the render as the live capture would manufacture content matches.

    This is the nastier twin of LIVE_SIDE_IS_NOT_A_LIVE_OBJECT: a self-comparison
    here does not just read green, it converts a real content swap into a
    *proven* inert difference.
    """
    _, payload = _pod_spec_shape(
        tmp_path, *_with_configmaps(tmp_path, TARGET_CONFIGMAPS, TARGET_CONFIGMAPS)
    )
    assert payload["status"] == "FAIL"
    assert "LIVE_CONFIGMAPS_ARE_NOT_LIVE_OBJECTS" in payload["errors"]
    assert payload["inert_by_content"] == []

    _, reversed_sides = _pod_spec_shape(
        tmp_path, *_with_configmaps(tmp_path, LIVE_CONFIGMAPS, LIVE_CONFIGMAPS)
    )
    assert "TARGET_CONFIGMAPS_ARE_NOT_A_RENDER" in reversed_sides["errors"]
    assert reversed_sides["inert_by_content"] == []


def test_pod_spec_shape_never_excuses_a_removed_mount_by_its_content(tmp_path: Path):
    """A digest cannot excuse a patch that stopped being mounted at all.

    The ConfigMap still exists with identical content on both sides -- that is
    exactly the production failure shape, and precisely why content equality is
    only allowed to excuse a *changed* item, never a removed mount.
    """
    patch_cm = f"""---
apiVersion: v1
kind: ConfigMap
metadata:
  name: litellm-deepcopy-patch
  namespace: litellm-product
  uid: aaaaaaaa-0000-0000-0000-000000000003
  resourceVersion: "113"
data:
  patch.py: |
{textwrap.indent(PATCH_BYTES.rstrip(), " " * 4)}
"""
    _, payload = _pod_spec_shape(
        tmp_path,
        *_with_configmaps(
            tmp_path,
            LIVE_CONFIGMAPS + patch_cm,
            TARGET_CONFIGMAPS + patch_cm.replace("\n  uid: aaaaaaaa-0000-0000-0000-000000000003\n  resourceVersion: \"113\"", ""),
        ),
    )
    assert payload["status"] == "FAIL"
    assert "container/litellm/mount//patches/" in set(payload["unapproved_shape_changes"])
    assert "container/litellm/mount//patches/" not in set(payload["inert_by_content"])


def test_pod_spec_shape_pairs_a_renamed_volume_only_when_the_pairing_is_unambiguous(
    tmp_path: Path,
):
    """The chart prefixes snapshot volumes, so the volume *name* moves too.

    A rename is excused only on a 1:1 content match. Two removals with the same
    digest mean the pairing is a guess, and a guess is not evidence.
    """
    # The chart's `snapshot-` prefix: same ConfigMap content, new volume name,
    # same mountPath.
    target = POD_SPEC_TARGET.replace(
        """        - name: callbacks
          configMap: {name: litellm-product-proxy-callbacks-e718bd18}""",
        """        - name: callbacks
          configMap: {name: litellm-product-proxy-callbacks-e718bd18}
        - name: snapshot-deepcopy-patch
          configMap: {name: litellm-product-proxy-deepcopy-patch-aa11bb22}""",
    ).replace(
        """            - name: callbacks
              mountPath: /app/budget_notice.py
              subPath: budget_notice.py""",
        """            - name: callbacks
              mountPath: /app/budget_notice.py
              subPath: budget_notice.py
            - name: snapshot-deepcopy-patch
              mountPath: /patches""",
    )
    live_patch = f"""---
apiVersion: v1
kind: ConfigMap
metadata:
  name: litellm-deepcopy-patch
  namespace: litellm-product
  uid: aaaaaaaa-0000-0000-0000-000000000003
  resourceVersion: "113"
data:
  patch.py: |
{textwrap.indent(PATCH_BYTES.rstrip(), " " * 4)}
"""
    target_patch = f"""---
apiVersion: v1
kind: ConfigMap
metadata:
  name: litellm-product-proxy-deepcopy-patch-aa11bb22
  namespace: litellm-product
data:
  patch.py: |
{textwrap.indent(PATCH_BYTES.rstrip(), " " * 4)}
"""
    _, renamed = _pod_spec_shape(
        tmp_path,
        target=target,
        *_with_configmaps(
            tmp_path, LIVE_CONFIGMAPS + live_patch, TARGET_CONFIGMAPS + target_patch
        ),
    )
    assert renamed["renamed_volumes"] == [
        {
            "live": "volume/deepcopy-patch",
            "target": "volume/snapshot-deepcopy-patch",
            "content": renamed["renamed_volumes"][0]["content"],
        }
    ]
    assert "volume/deepcopy-patch" in set(renamed["inert_by_content"])
    assert "volume/deepcopy-patch" not in set(renamed["unapproved_shape_changes"])
    # The mount itself matched by path and by bytes, so it is not even a change.
    assert "container/litellm/mount//patches/" not in set(
        renamed["unapproved_shape_changes"]
    )

    # Two removed volumes carrying the same bytes: the pairing is a guess.
    live_twin = POD_SPEC_LIVE.replace(
        """        - name: deepcopy-patch
          configMap: {name: litellm-deepcopy-patch}""",
        """        - name: deepcopy-patch
          configMap: {name: litellm-deepcopy-patch}
        - name: deepcopy-patch-twin
          configMap: {name: litellm-deepcopy-patch-twin}""",
    )
    twin_cm = live_patch.replace(
        "litellm-deepcopy-patch\n", "litellm-deepcopy-patch-twin\n"
    ).replace("000000000003", "000000000004")
    _, ambiguous = _pod_spec_shape(
        tmp_path,
        live=live_twin,
        target=target,
        *_with_configmaps(
            tmp_path,
            LIVE_CONFIGMAPS + live_patch + twin_cm,
            TARGET_CONFIGMAPS + target_patch,
        ),
    )
    assert ambiguous["renamed_volumes"] == []
    assert "volume/deepcopy-patch" in set(ambiguous["unapproved_shape_changes"])


def _heartbeat_ledger(
    tmp_path: Path, offsets_seconds: list[int], *, run_id: str = "run-1", status: str = "PASS"
) -> Path:
    """Write a heartbeat ledger with cycles at `now - offset` for each offset."""
    now = datetime.now(timezone.utc)
    path = tmp_path / "monitor-heartbeat.jsonl"
    lines = []
    for index, offset in enumerate(sorted(offsets_seconds, reverse=True)):
        stamp = now - timedelta(seconds=offset)
        lines.append(
            json.dumps(
                {
                    "schema_version": 1,
                    "tool": "gray-monitor-cycle",
                    "cycle_completed_at": stamp.isoformat(timespec="seconds").replace(
                        "+00:00", "Z"
                    ),
                    "run_id": run_id,
                    "generation": "gen-1",
                    "metrics_status": status,
                    "evidence": f"metrics-{index}.json",
                },
                sort_keys=True,
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _continuity(ledger: Path, *extra: str, window_seconds: int = 1800):
    window_start = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
    result = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "check-monitor-continuity.py"),
            "--ledger",
            str(ledger),
            "--run-id",
            "run-1",
            "--generation",
            "gen-1",
            "--config-checksum",
            "test-mode",
            "--window-start",
            window_start.isoformat().replace("+00:00", "Z"),
            "--cycle-interval-seconds",
            "300",
            *extra,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result, json.loads(result.stdout)


def test_monitor_continuity_passes_only_when_every_cycle_landed(tmp_path: Path):
    """A ramp step claims the previous window was observed; prove it with data."""
    cycles = list(range(120, 1800, 300))
    result, payload = _continuity(_heartbeat_ledger(tmp_path, cycles))

    assert result.returncode == 0, result.stdout
    assert payload["status"] == "PASS"
    assert payload["gaps"] == []
    assert payload["cycles_in_window"] == len(cycles)
    assert payload["gate"] == "split_monitor_continuity"
    assert payload["max_allowed_gap_seconds"] == 600


def test_monitor_continuity_fails_closed_on_a_silent_scheduler(tmp_path: Path):
    # The scheduler died 25 minutes ago: no evidence file records the gap, which
    # is exactly why the gap has to be derived from the ledger.
    result, payload = _continuity(_heartbeat_ledger(tmp_path, [1700, 1500]))

    assert result.returncode == 1
    assert payload["status"] == "FAIL"
    assert payload["errors"] == ["MONITORING_GAP"]
    assert payload["gaps"][-1]["seconds"] >= 1500

    # An entirely empty window is not "no news is good news" either.
    stale, stale_payload = _continuity(_heartbeat_ledger(tmp_path, [9000]))
    assert stale.returncode == 1
    assert "NO_CYCLES_IN_WINDOW" in stale_payload["errors"]


def test_monitor_continuity_rejects_a_foreign_run_or_a_failed_cycle(tmp_path: Path):
    cycles = list(range(120, 1800, 300))
    _, foreign = _continuity(_heartbeat_ledger(tmp_path, cycles, run_id="run-other"))
    assert foreign["status"] == "FAIL"
    assert "FOREIGN_RUN_ID" in foreign["errors"]

    _, failed = _continuity(_heartbeat_ledger(tmp_path, cycles, status="FAIL"))
    assert failed["status"] == "FAIL"
    assert "FAILED_CYCLE_IN_WINDOW" in failed["errors"]


def test_monitor_continuity_refuses_a_group_readable_or_forged_ledger(tmp_path: Path):
    ledger = _heartbeat_ledger(tmp_path, list(range(120, 1800, 300)))
    ledger.chmod(0o644)
    loose = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "check-monitor-continuity.py"),
            "--ledger",
            str(ledger),
            "--run-id",
            "run-1",
            "--generation",
            "gen-1",
            "--config-checksum",
            "test-mode",
            "--window-start",
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "--cycle-interval-seconds",
            "300",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert loose.returncode != 0
    assert "group/world" in loose.stderr

    ledger.chmod(0o600)
    ledger.write_text(
        json.dumps({"schema_version": 1, "tool": "hand-written", "x": 1}) + "\n",
        encoding="utf-8",
    )
    forged = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "check-monitor-continuity.py"),
            "--ledger",
            str(ledger),
            "--run-id",
            "run-1",
            "--generation",
            "gen-1",
            "--config-checksum",
            "test-mode",
            "--window-start",
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "--cycle-interval-seconds",
            "300",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert forged.returncode != 0
    assert "unexpected shape" in forged.stderr


def test_verify_readiness_excludes_every_non_manifest_under_k8s():
    """A readiness gate that is red for tool noise trains people to ignore it.

    Measured 2026-09-14: `verify-readiness.py` fed all of `k8s/` to kubeconform
    while only excluding `values-*.yaml`.  When `prod-pod-spec-approval.json`
    landed in that directory, kubeconform reported `missing 'kind' key` for it
    and the whole check went FAIL -- with `Invalid: 0` in the same summary.  The
    manifests were fine; the gate was wrong about what it was looking at.

    The exclusion list is a blacklist, so it rots silently the next time a
    non-manifest file is added.  This test is the thing that notices: it derives
    the truth from the files themselves (a K8s manifest has a `kind`) instead of
    trusting the list.
    """
    import importlib.util
    import re

    import yaml

    spec = importlib.util.spec_from_file_location(
        "verify_readiness_for_tests", TOOLS / "verify-readiness.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    patterns = [re.compile(p) for p in module.NON_MANIFEST_PATTERNS]

    # kubeconform only reads these extensions; README.md is never scanned.
    scanned = sorted(
        path
        for suffix in ("*.yaml", "*.yml", "*.json")
        for path in (ROOT / "litellm-gray-rollout" / "k8s").glob(suffix)
    )
    assert scanned, "k8s/ is empty -- the ruler lost its subject"

    excluded, kept = [], []
    for path in scanned:
        (excluded if any(p.search(path.name) for p in patterns) else kept).append(path)

    # Every excluded file must genuinely lack a kind, or the blacklist is hiding
    # a real manifest from validation -- the failure direction that reads green.
    for path in excluded:
        docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        assert not any(isinstance(doc, dict) and "kind" in doc for doc in docs), (
            f"{path.name} is excluded from kubeconform but IS a manifest"
        )

    # And every kept file must have a kind, or the gate goes red for tool noise.
    for path in kept:
        docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        assert docs and all(isinstance(doc, dict) and "kind" in doc for doc in docs if doc), (
            f"{path.name} has no kind but is fed to kubeconform -- add it to "
            "NON_MANIFEST_PATTERNS"
        )

    # No dead pattern: a rule matching nothing is a rule nobody is maintaining.
    for pattern in patterns:
        assert any(pattern.search(path.name) for path in scanned), pattern.pattern


def test_scripts_never_use_brace_intervals_in_awk():
    """mawk silently matches nothing on `{n}` -- the failure reads as a clean empty file.

    Measured 2026-09-14 on the production change host 10.68.13.198 (mawk 1.3.4
    20200120, Ubuntu's default `awk`): the ERE interval quantifier is **not
    supported and not reported**.  `_lib.sh:write_key_map` filtered its sid lines
    with `[0-9a-f]{12}`, so on 198 every line was dropped and `key-sid.map` was
    written empty -- while the same script was green on macOS, whose BWK awk does
    support intervals.  Downstream, nginx's `map $canonical_key $key_sid` would
    have fallen through to the default `-` for every key, with no error anywhere.

    That is the worst failure direction this repo guards against: a tool that is
    wrong only on the machine the change actually runs on, and wrong by going
    quiet.  So the rule is mechanical -- no brace intervals inside awk programs,
    spell the repetition out.

    Only awk programs are in scope: bash `=~` and Python `re` both handle `{n}`
    fine and are used deliberately elsewhere in these scripts.
    """
    import re

    # An awk program is the first single-quoted argument after `awk` (optionally
    # preceded by -v assignments).  Matching the quoted chunk keeps `${var}` shell
    # expansions and bash `=~` patterns out of scope.
    awk_program = re.compile(r"\bawk\b(?:\s+-v\s+\S+)*\s+'([^']*)'")
    interval = re.compile(r"\{\d+(?:,\d*)?\}")

    offenders = []
    for path in sorted(TOOLS.glob("*.sh")):
        text = path.read_text(encoding="utf-8")
        for match in awk_program.finditer(text):
            if interval.search(match.group(1)):
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{path.name}:{line}: {match.group(1)[:80]}")

    assert not offenders, (
        "awk program uses a brace interval, which mawk ignores silently:\n"
        + "\n".join(offenders)
    )
