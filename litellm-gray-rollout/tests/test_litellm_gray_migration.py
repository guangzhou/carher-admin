"""Executable contracts for the LiteLLM gray migration gate."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "litellm-gray-rollout" / "scripts"
ROLLOUT = ROOT / "litellm-gray-rollout" / "k8s"
IDC_IMAGE = "127.0.0.1:5000/litellm-carher@sha256:"
RUN_ID = "run-qualification"
GENERATION = "g000001"
RUNNER_SHA = "sha256:" + "c" * 64


def digest(value: object) -> str:
    rendered = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(rendered.encode()).hexdigest()


def load_documents(path: Path) -> list[dict]:
    return [item for item in yaml.safe_load_all(path.read_text(encoding="utf-8")) if item]


def find_job(documents: list[dict], name: str) -> dict:
    return next(item for item in documents if item.get("kind") == "Job" and item.get("metadata", {}).get("name") == name)


def container_env(job: dict, name: str) -> str:
    for item in job["spec"]["template"]["spec"]["containers"][0].get("env", []):
        if item.get("name") == name:
            return str(item.get("value", ""))
    return ""


def ledger_payload() -> dict:
    return {
        "schema_version": 1,
        "statements": [{
            "id": "ddl-001",
            "sql": 'ALTER TABLE "LiteLLM_ProxyModelTable" ADD COLUMN "gray_gate_probe" TEXT',
            "online_safe": True,
            "lock_mode": "ACCESS EXCLUSIVE",
            "table_rewrite": False,
        }],
    }


def render_command(tmp_path: Path, *, target: str = "clone") -> tuple[list[str], Path, dict]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    ledger = ledger_payload()
    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    output = tmp_path / "rendered"
    command = [
        sys.executable, str(TOOLS / "prepare-migration-run.py"),
        "--migration-template", str(ROLLOUT / "migration-job.yaml"),
        "--version-template", str(ROLLOUT / "clone-version-test-jobs.yaml"),
        "--target-image", IDC_IMAGE + "a" * 64,
        "--stable-image", IDC_IMAGE + "b" * 64,
        "--migration-ledger", str(ledger_path),
        "--output-dir", str(output),
        "--run-id", RUN_ID,
        "--generation", GENERATION,
        "--migration-target", target,
    ]
    return command, output, ledger


def test_clone_dump_documents_share_one_mvcc_snapshot_and_restore_counts():
    documents = load_documents(ROLLOUT / "clone-dump-restore-jobs.yaml")
    dump = find_job(documents, "litellm-production-metadata-dump-template")
    command = "\n".join(dump["spec"]["template"]["spec"]["containers"][0]["args"])
    assert "pg_export_snapshot" in command
    assert 'pg_dump --snapshot="$snapshot"' in command
    assert "--schema=public" in command
    assert 'LiteLLM_SpendLogs' in command
    assert 'LiteLLM_SpendLogToolIndex' in command
    assert "--exclude-table-data='public.\"LiteLLM_SpendLogs\"'" in command
    assert "--exclude-table-data='public.\"LiteLLM_SpendLogToolIndex\"'" in command
    assert "metadata dump exceeds the 2 GiB safety limit" in command
    assert dump["spec"]["template"]["spec"]["nodeSelector"] == {
        "kubernetes.io/hostname": "aiyjy-litellm-standby"
    }
    assert dump["spec"]["template"]["spec"]["tolerations"] == [{
        "key": "dedicated", "operator": "Equal", "value": "standby", "effect": "NoSchedule",
    }]
    for name in ("litellm-clone-a-restore-template", "litellm-clone-b-restore-template", "litellm-clone-c-restore-template"):
        restore = find_job(documents, name)
        restore_command = "\n".join(restore["spec"]["template"]["spec"]["containers"][0]["args"])
        assert "restored-counts.tsv" in restore_command
        assert "cmp -s" in restore_command
        assert "DROP SCHEMA IF EXISTS public CASCADE" in restore_command
        assert "CREATE SCHEMA public" in restore_command
        assert 'log_rows="$(psql' in restore_command
        assert 'test "$log_rows" = 0' in restore_command
        assert "DO $$" not in restore_command


def test_compatibility_runner_starts_real_proxy_and_uses_http_paths():
    source = (TOOLS / "compatibility-runner.py").read_text(encoding="utf-8")
    assert "/app/docker/prod_entrypoint.sh" in source
    for path in ("/key/generate", "/key/update", "/key/delete", "/model/new", "/model/info", "/model/delete", "/v1/chat/completions"):
        assert path in source
    assert "LiteLLM_SpendLogs" in source
    assert "pg_advisory_lock_shared" in source
    assert "pg_advisory_xact_lock" not in source
    assert "FOR UPDATE" not in source
    # The runner must NOT carry a Prisma engine override: the Job runs as uid 0
    # like production, so the image's own root-only engine cache resolves.
    # Comments are stripped first — the runner deliberately names the dead
    # override in a comment so nobody reintroduces it.
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    assert "PRISMA_QUERY_ENGINE_BINARY" not in code
    assert "BINARY_PATHS.query_engine" not in code
    # A failed leg has to say why, not just which call broke.
    assert "proxy_log_tail" in source
    # SpendLogs.request_id is the response body id, not the call-id header.
    assert 'response.get("id")' in source


def test_prepare_migration_run_embeds_fixed_contract_and_real_proxy_config(tmp_path: Path):
    command, output, ledger = render_command(tmp_path)
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    config_maps = load_documents(output / "runner-configmaps.yaml")
    data = config_maps[0]["data"]
    assert set(data) == {
        "compatibility-config.yaml", "compatibility-config-suppressed.yaml",
        "compatibility-runner.py", "migration-contract.py",
        "migration-ledger-runner.py", "migration-ledger.json",
    }
    jobs = load_documents(output / "clone-version-test-jobs.yaml")
    for job in jobs:
        runner_volume = next(
            volume for volume in job["spec"]["template"]["spec"]["volumes"]
            if volume["name"] == "gray-runners"
        )
        assert runner_volume["configMap"]["defaultMode"] == 0o444
        assert not job["spec"]["template"]["spec"].get("initContainers")
        assert job["spec"]["template"]["spec"]["securityContext"]["runAsUser"] == 0
        container = job["spec"]["template"]["spec"]["containers"][0]
        assert container["command"] == ["python3"]
        assert "--config" in container["args"]
        env = {item["name"]: item for item in container["env"]}
        assert env["GRAY_RUN_ID"]["value"] == RUN_ID
        assert env["GRAY_GENERATION"]["value"] == GENERATION
        assert "fieldRef" in env["POD_UID"]["valueFrom"]
        assert "secretKeyRef" in env["LITELLM_MASTER_KEY"]["valueFrom"]
        assert "PRISMA_QUERY_ENGINE_BINARY" not in env
        mounts = {mount["name"]: mount for mount in container["volumeMounts"]}
        assert mounts["gray-runners"]["mountPath"] == "/opt/litellm-gray"
        assert mounts["gray-evidence"]["mountPath"] == "/evidence"
    assert json.loads(result.stdout)["ledger_sha256"] == digest(ledger)


def test_prepare_migration_run_version_only_omits_migration_job(tmp_path: Path):
    output = tmp_path / "version-only"
    result = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "prepare-migration-run.py"),
            "--version-template",
            str(ROLLOUT / "clone-version-test-jobs.yaml"),
            "--target-image",
            IDC_IMAGE + "a" * 64,
            "--stable-image",
            IDC_IMAGE + "b" * 64,
            "--output-dir",
            str(output),
            "--run-id",
            "v195-qualification",
            "--generation",
            "compat-1",
            "--version-only",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["migration_target"] == "version-only"
    assert report["ledger_sha256"] is None
    assert not (output / "migration-job.yaml").exists()
    assert (output / "clone-version-test-jobs.yaml").is_file()
    config_maps = load_documents(output / "runner-configmaps.yaml")
    assert "migration-ledger.json" not in config_maps[0]["data"]


@pytest.mark.parametrize(
    "sql",
    [
        'CREATE INDEX CONCURRENTLY "idx" ON "T" ("c")',
        'ALTER TABLE "T" ADD COLUMN "c" TEXT UNIQUE',
        'ALTER TABLE "T" ADD COLUMN "c" TEXT REFERENCES "Other"(id)',
        'ALTER TABLE "T" ADD COLUMN "c" TEXT CHECK (length("c") > 0)',
        'ALTER TABLE "T" ADD COLUMN "c" TIMESTAMP DEFAULT now()',
        'ALTER TABLE "T" ADD COLUMN "c" BIGINT GENERATED ALWAYS AS IDENTITY',
    ],
)
def test_transactional_ledger_rejects_constraints_indexes_and_volatile_defaults(tmp_path: Path, sql: str):
    command, _, ledger = render_command(tmp_path)
    ledger["statements"][0]["sql"] = sql
    Path(command[command.index("--migration-ledger") + 1]).write_text(json.dumps(ledger), encoding="utf-8")
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "approved add column" in result.stderr.lower()


def test_v195_index_runner_only_uses_concurrent_fixed_index_operations():
    source = (TOOLS / "v195-concurrent-index-runner.sh").read_text(encoding="utf-8")

    assert 'CREATE INDEX CONCURRENTLY "LiteLLM_SpendLogToolIndex_start_time_idx"' in source
    assert 'DROP INDEX CONCURRENTLY IF EXISTS public."LiteLLM_SpendLogToolIndex_start_time_idx"' in source
    assert "CREATE INDEX \"LiteLLM_SpendLogToolIndex_start_time_idx\"" not in source
    assert "create_requires_absent" in source
    assert "drop_invalid_requires_invalid" in source
    assert "indisvalid" in source and "indisready" in source and "indislive" in source
    assert "GRAY_INDEX_APPROVAL_SHA256" in source
    # The build may not start without measured headroom, and a failed build may not
    # leave the invalid index it created behind.
    assert "pg_prepared_xacts" in source and "pg_stat_progress_vacuum" in source
    assert "GRAY_INDEX_FREE_BYTES" in source and "GRAY_INDEX_REQUIRED_BYTES" in source
    assert "drop_invalid_leftover" in source
    assert "create_failed_inspect_and_drop_invalid_explicitly" not in source


PSQL_STUB = """#!/bin/sh
for arg in "$@"; do
  case "$arg" in
    'CREATE INDEX'*)
      if [ "${STUB_CREATE_RC:-0}" -ne 0 ]; then
        printf 'invalid\\n' >"$STUB_STATE_FILE"
        exit "$STUB_CREATE_RC"
      fi
      printf 'valid\\n' >"$STUB_STATE_FILE"
      exit 0
      ;;
    'DROP INDEX'*)
      if [ "${STUB_DROP_RC:-0}" -ne 0 ]; then exit "$STUB_DROP_RC"; fi
      printf 'absent\\n' >"$STUB_STATE_FILE"
      exit 0
      ;;
  esac
done
sql=$(cat)
case "$sql" in
  *pg_stat_activity*) printf '%s\\n' "$STUB_PROBE" ;;
  *) cat "$STUB_STATE_FILE" ;;
esac
"""

HEALTHY_PROBE = "12|0|0|0|3|4|600|1073741824"
HEADROOM_ENV = {
    "GRAY_INDEX_MAX_XACT_AGE_SECONDS": "300",
    "GRAY_INDEX_MAX_DEAD_TUP_PERCENT": "20",
    "GRAY_INDEX_MAX_VACUUM_AGE_SECONDS": "3600",
    "GRAY_INDEX_FREE_BYTES": "8589934592",
    "GRAY_INDEX_REQUIRED_BYTES": "1073741824",
}


def run_index_runner(
    tmp_path: Path, mode: str, *, probe: str = HEALTHY_PROBE, state: str = "absent",
    env_overrides: dict[str, str] | None = None, drop_env: tuple[str, ...] = (),
) -> tuple[subprocess.CompletedProcess[str], dict]:
    """Run the fixed-purpose runner against a stub psql and return its result file."""
    binaries = tmp_path / "bin"
    binaries.mkdir(exist_ok=True)
    # Write each stub once per tmp_path: macOS SIGKILLs an executable that is
    # overwritten in place after it has already run.
    if not (binaries / "psql").exists():
        (binaries / "psql").write_text(PSQL_STUB, encoding="utf-8")
        (binaries / "psql").chmod(0o755)
        # macOS has shasum, not sha256sum; the production image is coreutils-based.
        (binaries / "sha256sum").write_text("#!/bin/sh\nexec shasum -a 256 \"$@\"\n", encoding="utf-8")
        (binaries / "sha256sum").chmod(0o755)
    state_file = tmp_path / "index-state"
    state_file.write_text(state + "\n", encoding="utf-8")
    result_path = tmp_path / "result.json"

    env = {
        "PATH": f"{binaries}:/usr/bin:/bin",
        "DATABASE_URL": "postgresql://stub/litellm",
        "GRAY_RUN_ID": RUN_ID,
        "GRAY_GENERATION": GENERATION,
        "GRAY_INDEX_APPROVAL_SHA256": "sha256:" + "d" * 64,
        "RESULT_PATH": str(result_path),
        "STUB_PROBE": probe,
        "STUB_STATE_FILE": str(state_file),
        **HEADROOM_ENV,
        **(env_overrides or {}),
    }
    for name in drop_env:
        env.pop(name, None)
    process = subprocess.run(
        # The Job mounts this from a ConfigMap with defaultMode 0555; in the repo it
        # is a plain file, so invoke the interpreter explicitly.
        ["/bin/sh", str(TOOLS / "v195-concurrent-index-runner.sh"), mode],
        capture_output=True, text=True, check=False, env=env,
    )
    return process, json.loads(result_path.read_text(encoding="utf-8"))


def test_v195_index_runner_reports_measured_db_headroom_on_inspect(tmp_path: Path):
    process, result = run_index_runner(tmp_path, "inspect")

    assert process.returncode == 0, process.stderr
    assert result["status"] == "PASS"
    assert result["state"] == "absent"
    assert result["schema_version"] == 2
    preflight = result["preflight"]
    assert preflight["oldest_transaction_seconds"] == 12
    assert preflight["prepared_transactions"] == 0
    assert preflight["spendlogs_dead_tuple_percent"] == 4
    assert preflight["spendlogs_vacuum_age_seconds"] == 600
    assert preflight["tool_index_total_bytes"] == 1073741824


def test_v195_index_create_passes_only_with_headroom(tmp_path: Path):
    process, result = run_index_runner(tmp_path, "create")

    assert process.returncode == 0, process.stderr
    assert result["status"] == "PASS"
    assert result["reason"] == "created_concurrently"
    assert result["state"] == "valid"
    assert result["cleanup"] is None


@pytest.mark.parametrize(
    ("probe", "expected"),
    [
        ("9000|0|0|0|3|4|600|1073741824", "oldest_transaction_9000s_exceeds_300s"),
        (f"{HEALTHY_PROBE.split('|')[0]}|0|1|0|3|4|600|1073741824", "prepared_transactions_1"),
        ("12|0|0|1|3|4|600|1073741824", "vacuum_in_progress_on_1_target_tables"),
        ("12|0|0|0|55|4|600|1073741824", "tool_index_dead_tuples_55pct_exceeds_20pct"),
        ("12|0|0|0|3|61|600|1073741824", "spendlogs_dead_tuples_61pct_exceeds_20pct"),
        ("12|0|0|0|3|4|-1|1073741824", "spendlogs_never_vacuumed"),
        ("12|0|0|0|3|4|86400|1073741824", "spendlogs_vacuum_age_86400s_exceeds_3600s"),
    ],
)
def test_v195_index_create_fails_closed_on_a_hostile_database(tmp_path: Path, probe: str, expected: str):
    process, result = run_index_runner(tmp_path, "create", probe=probe)

    assert process.returncode != 0
    assert result["status"] == "FAIL"
    assert result["reason"].startswith(expected)
    # The build must not have been attempted: the index is still absent.
    assert (tmp_path / "index-state").read_text(encoding="utf-8").strip() == "absent"


def test_v195_index_create_requires_measured_free_space(tmp_path: Path):
    tight, _ = run_index_runner(
        tmp_path, "create", env_overrides={"GRAY_INDEX_FREE_BYTES": "1073741825"}
    )
    assert tight.returncode != 0

    process, result = run_index_runner(
        tmp_path, "create", drop_env=("GRAY_INDEX_REQUIRED_BYTES",)
    )
    assert process.returncode != 0
    assert result["reason"] == "GRAY_INDEX_REQUIRED_BYTES_missing_or_not_an_integer"


def test_v195_index_create_drops_the_invalid_index_it_left_behind(tmp_path: Path):
    process, result = run_index_runner(tmp_path, "create", env_overrides={"STUB_CREATE_RC": "3"})

    assert process.returncode != 0
    assert result["status"] == "FAIL"
    assert result["reason"] == "create_failed_invalid_index_dropped"
    assert result["cleanup"] == {"attempted": True, "dropped": True, "state_after_cleanup": "absent"}
    assert (tmp_path / "index-state").read_text(encoding="utf-8").strip() == "absent"


def test_v195_index_create_names_manual_cleanup_when_the_drop_also_fails(tmp_path: Path):
    process, result = run_index_runner(
        tmp_path, "create", env_overrides={"STUB_CREATE_RC": "3", "STUB_DROP_RC": "4"}
    )

    assert process.returncode != 0
    assert result["reason"] == "create_failed_invalid_index_drop_failed_manual_cleanup_required"
    assert result["cleanup"]["dropped"] is False
    assert (tmp_path / "index-state").read_text(encoding="utf-8").strip() == "invalid"


def test_v195_index_job_is_suspended_single_shot_and_uses_local_digest():
    documents = load_documents(ROLLOUT / "v195-concurrent-index-job.yaml")
    assert len(documents) == 1
    job = documents[0]
    pod_spec = job["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]

    assert job["spec"]["suspend"] is True
    assert job["spec"]["backoffLimit"] == 0
    assert pod_spec["restartPolicy"] == "Never"
    assert pod_spec["automountServiceAccountToken"] is False
    assert container["image"].startswith("127.0.0.1:5000/postgres@sha256:")
    assert container["args"] == ["inspect"]
    assert "statement_timeout=6h" in next(
        item["value"] for item in container["env"] if item["name"] == "PGOPTIONS"
    )
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    # Headroom thresholds must be present and must still be placeholders in the
    # repository copy, so an un-reviewed apply fails the gate instead of running.
    env = {item["name"]: item.get("value") for item in container["env"]}
    for name in (
        "GRAY_INDEX_MAX_XACT_AGE_SECONDS", "GRAY_INDEX_MAX_DEAD_TUP_PERCENT",
        "GRAY_INDEX_MAX_VACUUM_AGE_SECONDS", "GRAY_INDEX_FREE_BYTES",
        "GRAY_INDEX_REQUIRED_BYTES",
    ):
        assert env[name].startswith("REPLACE_WITH_"), name


def result_payload(mode: str, checks: list[str], image_char: str, db_identity: str, pod_uid: str) -> dict:
    payload = {
        "tool": "compatibility-runner",
        "schema_version": 2,
        "mode": mode,
        "status": "PASS",
        "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "completed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "binding": {
            "run_id": RUN_ID, "generation": GENERATION, "runner_sha256": RUNNER_SHA,
            "image_digest": "sha256:" + image_char * 64, "db_identity": db_identity,
            "pod_uid": pod_uid, "db_target_sha256": "sha256:" + hashlib.sha256(db_identity.encode()).hexdigest(),
        },
        "checks": checks,
        "rolled_back": True,
    }
    payload["result_sha256"] = digest(payload)
    return payload


def migration_payload(statement: str, ledger: dict, pod_uid: str) -> dict:
    payload = {
        "tool": "migration-ledger-runner", "schema_version": 1, "status": "PASS",
        "partial_state": "none", "ledger_sha256": digest(ledger),
        "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "completed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "binding": {
            "run_id": RUN_ID, "generation": GENERATION, "runner_sha256": RUNNER_SHA,
            "image_digest": "sha256:" + "a" * 64, "db_identity": "clone-a",
            "pod_uid": pod_uid, "db_target_sha256": "sha256:" + hashlib.sha256(b"clone-a").hexdigest(),
        },
        "entries": [{
            "id": "ddl-001", "statement": statement, "status": "completed", "online_safe": True,
            "lock_mode": "ACCESS EXCLUSIVE", "table_rewrite": False,
            "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "completed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "duration_ms": 10,
        }],
    }
    payload["result_sha256"] = digest(payload)
    return payload


def attestation(result: dict, *, job: str, image_char: str, db_identity: str, pod_uid: str) -> dict:
    payload = {
        "tool": "collect-job-attestation", "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "job_name": job, "job_uid": job + "-uid", "pod_uid": pod_uid, "container": "test",
        "container_image_id": "containerd://" + IDC_IMAGE.removesuffix("sha256:") + "sha256:" + image_char * 64,
        "runner_config_name": "litellm-gray-migration-runners-abcdef123456",
        "runner_sha256": RUNNER_SHA, "db_identity": db_identity,
        "db_target_sha256": result["binding"]["db_target_sha256"],
        "run_id": RUN_ID, "generation": GENERATION, "result_sha256": result["result_sha256"],
    }
    payload["payload_sha256"] = digest(payload)
    return payload


def write_json(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def build_collector_command(tmp_path: Path) -> tuple[list[str], Path, dict]:
    ledger = ledger_payload(); statement = ledger["statements"][0]["sql"]
    before = tmp_path / "before.sql"; before.write_text("schema-before", encoding="utf-8")
    after = tmp_path / "after.sql"; after.write_text("schema-after", encoding="utf-8")
    expected = tmp_path / "expected.sql"; expected.write_bytes(after.read_bytes())
    migration = migration_payload(statement, ledger, "pod-migration")
    isolated = ["auth", "key_api", "proxy_model_api", "proxy_startup", "spend_logs"]
    concurrent = ["concurrent_budget", "concurrent_proxy_model", "concurrent_spend_logs"]
    results = {
        "migration": migration,
        "new": result_payload("new", isolated, "a", "clone-a", "pod-new"),
        "old": result_payload("old", isolated, "b", "clone-b", "pod-old"),
        "concurrent-new": result_payload("concurrent-new", concurrent, "a", "clone-c", "pod-cn"),
        "concurrent-old": result_payload("concurrent-old", concurrent, "b", "clone-c", "pod-co"),
    }
    paths: dict[str, Path] = {}
    for name, payload in results.items():
        paths[name] = write_json(tmp_path / f"{name}.json", payload)
        paths[name + "-att"] = write_json(
            tmp_path / f"{name}-att.json",
            attestation(payload, job=f"job-{name}", image_char="a" if name in {"migration", "new", "concurrent-new"} else "b", db_identity=payload["binding"]["db_identity"], pod_uid=payload["binding"]["pod_uid"]),
        )
    output = tmp_path / "migration-evidence.json"
    online = {
        "lock_timeout_ms": 5000, "statement_timeout_ms": 900000, "migration_duration_ms": 10,
        "workload_p95_ratio": 1.0, "network_policy": {"allowed_probe": "PASS", "denied_probe": "PASS"},
        "snapshot_counts": {"expected": {"LiteLLM_SpendLogs": 1}, "restored": {"LiteLLM_SpendLogs": 1}},
        "entries": {"ddl-001": {"lock_wait_ms": 0, "observed_in_schema": True, "lock_mode": "ACCESS EXCLUSIVE", "table_rewrite": False}},
    }
    command = [
        sys.executable, str(TOOLS / "collect-migration-evidence.py"),
        "--schema-before", str(before), "--schema-after", str(after), "--expected-schema-after", str(expected),
        "--schema-changes", str(write_json(tmp_path / "changes.json", [statement])),
        "--migration-result", str(paths["migration"]), "--migration-attestation", str(paths["migration-att"]),
        "--online-gate", str(write_json(tmp_path / "online.json", online)),
        "--new-result", str(paths["new"]), "--new-attestation", str(paths["new-att"]),
        "--old-result", str(paths["old"]), "--old-attestation", str(paths["old-att"]),
        "--concurrent-new-result", str(paths["concurrent-new"]), "--concurrent-new-attestation", str(paths["concurrent-new-att"]),
        "--concurrent-old-result", str(paths["concurrent-old"]), "--concurrent-old-attestation", str(paths["concurrent-old-att"]),
        "--thresholds", str(write_json(tmp_path / "thresholds.json", {"max_migration_duration_ms": 900000, "max_workload_p95_ratio": 1.2, "max_lock_wait_ms": 5000})),
        "--ledger", str(write_json(tmp_path / "ledger.json", ledger)),
        "--target-image", IDC_IMAGE + "a" * 64, "--stable-image", IDC_IMAGE + "b" * 64,
        "--run-id", RUN_ID, "--generation", GENERATION, "--config-checksum", "a" * 64,
        "--output", str(output),
    ]
    return command, output, paths


def test_collector_requires_bound_fresh_job_attestations(tmp_path: Path):
    command, output, paths = build_collector_command(tmp_path)
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    checked = subprocess.run([sys.executable, str(TOOLS / "check-migration.py"), "--input", str(output)], capture_output=True, text=True, check=False)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    qualification = json.loads(checked.stdout)["qualification"]
    assert qualification["attestations_sha256"].startswith("sha256:")
    assert qualification["runner_sha256"] == RUNNER_SHA
    assert set(qualification["db_targets_sha256"]) == {"migration", "new", "old", "concurrent-new", "concurrent-old"}

    tampered = json.loads(paths["new-att"].read_text(encoding="utf-8"))
    tampered["run_id"] = "other-run"
    tampered["payload_sha256"] = digest({k: v for k, v in tampered.items() if k != "payload_sha256"})
    paths["new-att"].write_text(json.dumps(tampered), encoding="utf-8")
    retry = tmp_path / "retry.json"
    rejected = subprocess.run([*command[:-1], str(retry)], capture_output=True, text=True, check=False)
    assert rejected.returncode != 0
    assert "bound" in rejected.stderr.lower()


def test_collector_rejects_stale_or_wrong_image_attestation(tmp_path: Path):
    command, _, paths = build_collector_command(tmp_path)
    payload = json.loads(paths["old-att"].read_text(encoding="utf-8"))
    payload["captured_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    payload["payload_sha256"] = digest({k: v for k, v in payload.items() if k != "payload_sha256"})
    paths["old-att"].write_text(json.dumps(payload), encoding="utf-8")
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "stale" in result.stderr.lower()


def test_production_render_requires_same_run_fresh_qualification_and_live_before_schema(tmp_path: Path):
    collect, evidence, _ = build_collector_command(tmp_path)
    assert subprocess.run(collect, capture_output=True, text=True, check=False).returncode == 0
    check = subprocess.run([sys.executable, str(TOOLS / "check-migration.py"), "--input", str(evidence)], capture_output=True, text=True, check=False)
    assert check.returncode == 0
    qualification = write_json(tmp_path / "qualification.json", json.loads(check.stdout)); qualification.chmod(0o600)
    command, output, _ = render_command(tmp_path / "prod", target="prod")
    live_schema = tmp_path / "prod-live.sql"; live_schema.write_text("schema-before", encoding="utf-8")
    result = subprocess.run([*command, "--clone-qualification", str(qualification), "--live-schema", str(live_schema)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert not (output / "clone-version-test-jobs.yaml").exists()
    live_schema.write_text("drifted-schema", encoding="utf-8")
    drifted_command, _, _ = render_command(tmp_path / "drifted", target="prod")
    drifted = subprocess.run([*drifted_command, "--clone-qualification", str(qualification), "--live-schema", str(live_schema)], capture_output=True, text=True, check=False)
    assert drifted.returncode != 0
    assert "ledger and image pair" in drifted.stderr.lower()


def test_job_attestation_collector_rejects_wrong_pod_or_image(tmp_path: Path):
    result = result_payload("new", ["auth", "key_api", "proxy_model_api", "proxy_startup", "spend_logs"], "a", "clone-a", "pod-uid")
    result_path = write_json(tmp_path / "result.json", result)
    job = {"metadata": {"name": "job-new", "uid": "job-uid"}}
    pod = {
        "metadata": {"uid": "pod-uid", "ownerReferences": [{"kind": "Job", "uid": "job-uid"}]},
        "status": {"containerStatuses": [{"name": "test", "imageID": "containerd://" + IDC_IMAGE.removesuffix("sha256:") + "sha256:" + "b" * 64}]},
    }
    command = [
        sys.executable, str(TOOLS / "collect-job-attestation.py"),
        "--job", str(write_json(tmp_path / "job.json", job)), "--pod", str(write_json(tmp_path / "pod.json", pod)),
        "--result", str(result_path), "--container", "test", "--runner-config-name", "runners-1",
        "--runner-sha256", RUNNER_SHA, "--output", str(tmp_path / "attestation.json"),
    ]
    rejected = subprocess.run(command, capture_output=True, text=True, check=False)
    assert rejected.returncode != 0
    assert "imageid" in rejected.stderr.lower()


# --- scheduler observation probe -------------------------------------------
#
# Measured defect these cover: the three `observations` counts that
# prepare-values.py refuses to pass without had no producer at all. The design
# note (docs/scheduler-suppression-evidence-2026-09-14.md §6) rules out the
# log-based and in-process rulers and lands on a database-side one, which needs
# two releases holding connections to the same clone long enough to tell "the
# job ran once" from "it ran twice". These assert the harness that produces that
# window, not the counts themselves -- the counts come off the cluster.


def _pv_module():
    spec = importlib.util.spec_from_file_location(
        "litellm_gray_prepare_values_for_tests", TOOLS / "prepare-values.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render_scheduler_probe(tmp_path: Path, probe: str, hold: int) -> list[dict]:
    output = tmp_path / f"probe-{probe}-{hold}"
    result = subprocess.run(
        [
            sys.executable, str(TOOLS / "prepare-migration-run.py"),
            "--version-template", str(ROLLOUT / "clone-version-test-jobs.yaml"),
            "--target-image", IDC_IMAGE + "a" * 64,
            "--stable-image", IDC_IMAGE + "b" * 64,
            "--output-dir", str(output),
            "--run-id", RUN_ID, "--generation", GENERATION, "--version-only",
            "--scheduler-probe", probe, "--scheduler-hold-seconds", str(hold),
        ],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return load_documents(output / "clone-version-test-jobs.yaml")


def test_suppressed_compatibility_config_is_exactly_the_prepare_values_overlay():
    """The suppressed config is not a hand-maintained twin of the plain one.

    If it drifts, the leg standing in for gray stops standing in for gray, and
    the measurement answers a question nobody asked.
    """
    module = _pv_module()
    base = (TOOLS / "compatibility-config.yaml").read_text(encoding="utf-8")
    suppressed = (TOOLS / "compatibility-config-suppressed.yaml").read_text(encoding="utf-8")
    expected, changed = module.disable_reset_budget_in_config(base, profile="gray")
    assert changed is True
    assert suppressed == expected
    # And the plain one is genuinely the unsuppressed control, not already overlaid.
    assert yaml.safe_load(base)["general_settings"].get("disable_reset_budget") is None
    assert yaml.safe_load(suppressed)["general_settings"]["disable_reset_budget"] is True


def test_scheduler_probe_suppresses_only_the_target_leg_on_clone_c(tmp_path: Path):
    module = _pv_module()
    jobs = {
        job["metadata"]["name"]: job
        for job in _render_scheduler_probe(tmp_path, "suppressed", 1800)
    }
    gray_leg = jobs["litellm-concurrent-new-test"]["spec"]["template"]["spec"]["containers"][0]
    prod_leg = jobs["litellm-concurrent-old-test"]["spec"]["template"]["spec"]["containers"][0]
    assert "/opt/litellm-gray/compatibility-config-suppressed.yaml" in gray_leg["args"]
    assert "/opt/litellm-gray/compatibility-config.yaml" in prod_leg["args"]
    gray_env = {item["name"]: item.get("value") for item in gray_leg["env"]}
    prod_env = {item["name"]: item.get("value") for item in prod_leg["env"]}
    for name, off in module.BACKGROUND_TASK_SUPPRESSORS.items():
        assert gray_env[name] == off
        # The stable leg stands in for prod, which OWNS the scheduler. Suppress
        # it too and the window has no scheduler running in it at all, which
        # would read as a clean green for the wrong reason.
        assert name not in prod_env
    for leg in (gray_leg, prod_leg):
        assert leg["args"][-2:] == ["--hold-seconds", "1800"]
    for name in ("litellm-concurrent-new-test", "litellm-concurrent-old-test"):
        assert jobs[name]["spec"]["activeDeadlineSeconds"] == 1800 + 900
    # The isolated legs talk to clone A and clone B, one release each. Holding
    # them open would measure a release against nobody.
    for name in ("litellm-new-version-test", "litellm-old-version-test"):
        container = jobs[name]["spec"]["template"]["spec"]["containers"][0]
        assert "--hold-seconds" not in container["args"]
        assert jobs[name]["spec"]["activeDeadlineSeconds"] == 1800


def test_scheduler_probe_unsuppressed_is_the_positive_control(tmp_path: Path):
    """Step 0: both legs unsuppressed, so the ruler MUST see duplicates.

    A ruler that reads zero here is blind, and every zero it later reports on
    the suppressed run is a synthetic green.
    """
    module = _pv_module()
    jobs = {
        job["metadata"]["name"]: job
        for job in _render_scheduler_probe(tmp_path, "unsuppressed", 1560)
    }
    for name in ("litellm-concurrent-new-test", "litellm-concurrent-old-test"):
        container = jobs[name]["spec"]["template"]["spec"]["containers"][0]
        assert "/opt/litellm-gray/compatibility-config.yaml" in container["args"]
        assert container["args"][-2:] == ["--hold-seconds", "1560"]
        env = {item["name"] for item in container["env"]}
        assert not (env & set(module.BACKGROUND_TASK_SUPPRESSORS))


@pytest.mark.parametrize(
    "extra,expected",
    [
        (["--scheduler-probe", "suppressed", "--scheduler-hold-seconds", "600"], "1500"),
        (["--scheduler-probe", "suppressed", "--scheduler-hold-seconds", "7200"], "3600"),
        (["--scheduler-hold-seconds", "1800"], "requires"),
    ],
)
def test_scheduler_probe_hold_bounds_are_enforced_at_render_time(
    tmp_path: Path, extra: list[str], expected: str
):
    result = subprocess.run(
        [
            sys.executable, str(TOOLS / "prepare-migration-run.py"),
            "--version-template", str(ROLLOUT / "clone-version-test-jobs.yaml"),
            "--target-image", IDC_IMAGE + "a" * 64,
            "--stable-image", IDC_IMAGE + "b" * 64,
            "--output-dir", str(tmp_path / "rejected"),
            "--run-id", RUN_ID, "--generation", GENERATION, "--version-only", *extra,
        ],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert expected in result.stderr


def test_scheduler_probe_is_off_by_default(tmp_path: Path):
    jobs = load_documents(
        _default_probe_output(tmp_path) / "clone-version-test-jobs.yaml"
    )
    for job in jobs:
        container = job["spec"]["template"]["spec"]["containers"][0]
        assert "--hold-seconds" not in container["args"]
        assert "compatibility-config-suppressed.yaml" not in " ".join(container["args"])


def _default_probe_output(tmp_path: Path) -> Path:
    output = tmp_path / "default"
    result = subprocess.run(
        [
            sys.executable, str(TOOLS / "prepare-migration-run.py"),
            "--version-template", str(ROLLOUT / "clone-version-test-jobs.yaml"),
            "--target-image", IDC_IMAGE + "a" * 64,
            "--stable-image", IDC_IMAGE + "b" * 64,
            "--output-dir", str(output),
            "--run-id", RUN_ID, "--generation", GENERATION, "--version-only",
        ],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return output


def test_runner_hold_window_is_bounded_by_the_database_clock_and_client_addr():
    """The window bounds and the attribution key both come from the server.

    The log lines being matched carry postgres's clock and postgres's view of
    `%h`. Taking either from the pod introduces a skew or a NAT translation the
    analysis would absorb without complaining.
    """
    source = (TOOLS / "compatibility-runner.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    hold = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "hold_open"
    )
    body = ast.get_source_segment(source, hold) or ""
    assert "inet_client_addr" in body
    assert "now() AT TIME ZONE 'utc'" in body
    # `now()` is this module's own wall-clock helper; the hold must not use it.
    assert not re.search(r"[^.\w]now\(\)(?!\s*AT)", body.replace("now() AT TIME ZONE", "DBNOW"))
    # A dead proxy mid-hold must be a red, not a shorter window that still parses.
    assert "exited mid-hold" in body


def test_runner_refuses_a_hold_shorter_than_two_budget_cycles():
    source = (TOOLS / "compatibility-runner.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    constants = {
        node.targets[0].id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant)
    }
    # reset_budget_job reschedules every 597-605s; one cycle cannot separate
    # "ran once" from "ran twice".
    assert constants["MIN_HOLD_SECONDS"] >= 2 * 605
    assert constants["MAX_HOLD_SECONDS"] <= 3600


def test_runner_attests_its_own_scheduler_knobs():
    source = (TOOLS / "compatibility-runner.py").read_text(encoding="utf-8")
    assert "scheduler_knobs" in source
    tree = ast.parse(source)
    knobs = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "scheduler_knobs"
    )
    body = ast.get_source_segment(source, knobs) or ""
    assert "disable_reset_budget" in body
    assert "os.environ.get(name)" in body
    # The hold runs AFTER the checks: a leg that never proved it can serve a
    # request is not a leg whose scheduler behaviour means anything.
    run_source = source.split("async def run(")[1]
    assert run_source.index("application_probe") < run_source.index("hold_open")


def test_barrier_acquire_never_returns_a_void_column():
    """Measured defect, clone C 2026-09-14: both concurrent legs died on this line.

    `pg_advisory_lock_shared()` is declared to return `void`, and Prisma's raw-query
    layer raises `RawQueryError: Failed to deserialize column of type 'void'` before
    the lock's return value is ever inspected. The lock IS taken server-side, so the
    failure is purely in the deserializer -- which is why the bug survived: the SQL
    is correct, only the shape of the result set is not. Both `concurrent-*` legs
    exited non-zero at the very first barrier, meaning this barrier had never once
    executed in the harness's life.

    The fix keeps the blocking acquire and hides the void column inside a subquery.
    This test pins the shape, not the wording: any `SELECT pg_advisory_lock_shared`
    that is the outermost select list is the bug coming back.
    """
    source = (TOOLS / "compatibility-runner.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    # The void-returning acquire must never be the outermost projection: legal only
    # when it opens a subquery, i.e. immediately preceded by "(".
    assert not re.search(r"(?<!\()SELECT\s+pg_advisory_lock_shared", code)
    assert "(SELECT pg_advisory_lock_shared($1::integer, $2::integer)) AS acquired" in code
    assert "SELECT 1::bigint AS locked, " in code
    # pg_advisory_unlock_shared returns boolean, so it is legal bare and must stay
    # bare -- wrapping it would discard the "did I actually hold it" answer.
    assert "SELECT pg_advisory_unlock_shared($1::integer, $2::integer)" in code


def test_overlap_window_lower_bound_comes_from_the_barrier_not_the_local_leg():
    """Measured defect, clone C 2026-09-14: `new` saw the overlap, `old` did not.

    Both legs ran the same code and both served a successful inference, yet the
    `old` leg failed "concurrent ProxyModel/SpendLogs writes did not overlap
    visibly" while the `new` leg passed and went on to wait at the done barrier.
    The asymmetry was not a version difference: each leg read the database clock
    just before its OWN inference and used that as the lower bound on
    `LiteLLM_SpendLogs."startTime"`, so a peer row written a few hundred
    milliseconds earlier fell outside the window and was never counted.

    The sound bound is the instant this leg ACQUIRED the start barrier: the peer
    cannot leave that barrier until it has seen this leg holding the lock, so the
    acquire instant precedes every post-barrier write by both legs.
    """
    source = (TOOLS / "compatibility-runner.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    barrier = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "wait_for_peer"
    )
    # The barrier hands the instant back instead of returning None.
    assert isinstance(barrier.returns, ast.Name) and barrier.returns.id == "str"
    barrier_body = ast.get_source_segment(source, barrier) or ""
    assert "to_char(now() AT TIME ZONE 'utc'" in barrier_body
    assert 'return str(acquired[0]["t"])' in barrier_body

    probe = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "application_probe"
    )
    assert [arg.arg for arg in probe.args.args] == ["mode", "connection", "window_start"]
    probe_body = ast.get_source_segment(source, probe) or ""
    assert "probe_started = window_start" in probe_body

    # run() must feed the barrier's instant into the probe, not discard it.
    run_source = source.split("async def run(")[1]
    assert "window_start = await wait_for_peer(connection) if barrier else None" in run_source
    assert "application_probe(mode, connection, window_start)" in run_source


def test_overlap_observed_rendezvous_latches_instead_of_sampling_an_interval():
    """Measured defect, clone C 2026-09-14: a 39 ms window nobody could sample.

    Postgres statement log, both legs on clone C:

        01:29:34.804  host=10.42.1.79  acquire done-key
        01:29:34.878  host=10.42.1.78  acquire done-key
        01:29:34.917  host=10.42.1.78  pg_advisory_unlock_shared   <- 39 ms later
        01:31:34.881  host=10.42.1.79  timed out after the full 120 s

    With ONE shared key, "both legs are present" is an interval. The faster leg
    saw it on its first poll and left; the slower leg polls every 200 ms and never
    sampled those 39 ms. Nothing was wrong with either LiteLLM version.

    Per-leg keys turn the same question into a latching fact: each leg takes its
    own key when it observes the overlap and holds it until the process exits, so
    the peer's evidence cannot expire between two polls.
    """
    source = (TOOLS / "compatibility-runner.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    # The single shared done-key is the bug; it must not come back.
    assert "BARRIER_DONE_KEY " not in code
    assert "BARRIER_DONE_KEYS" in code

    tree = ast.parse(source)
    keys = next(
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "BARRIER_DONE_KEYS"
    )
    mapping = ast.literal_eval(keys)
    assert set(mapping) == {"concurrent-new", "concurrent-old"}
    # Distinct keys, and neither may collide with the start barrier.
    assert len(set(mapping.values())) == 2
    start = next(
        node.value.value for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "BARRIER_KEY"
    )
    assert start not in mapping.values()

    rendezvous = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "wait_for_peer_observation"
    )
    body = ast.get_source_segment(source, rendezvous) or ""
    # It waits on the PEER's key, and one holder is enough -- asking for 2 on a
    # per-leg key can never be satisfied.
    assert "peers\"] or 0) >= 1" in body
    assert "BARRIER_NAMESPACE,\n            peer,\n        )" in body
    # No unlock inside the rendezvous: releasing early recreates the window.
    assert "unlock" not in body
